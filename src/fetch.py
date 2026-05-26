"""
src/fetch.py — AI Vector source fetcher.

Reads config/sources.yaml, fetches every enabled source (RSS/Atom via
feedparser; HN Algolia, HF Daily Papers, and Reddit via httpx), emits:

    data/staging/YYYY-MM-DD/items.jsonl         — one Item per line
    data/staging/YYYY-MM-DD/source_health.json  — SourceHealthReport for the run

Output goes to STAGING per the Round B refactor (DESIGN.md "Archive: staging
vs canonical"). Canonical paths (`data/<date>/`) are written only by
`python -m src.run --release` via `src/render.py:release_promote`.

Public surface (per DESIGN.md module-boundaries table):

    fetch_day(run_date, config_path, out_dir) -> tuple[list[Item], list[SourceHealth]]

Convenience wrapper (for run.py and __main__):

    fetch(date) -> SourceHealthReport

Owner: Source Engineer.  No LLM calls here. Subscribe, don't scrape.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse as _urlparse

import feedparser  # type: ignore[import-untyped]
import httpx
import yaml
from pydantic import BaseModel

from src import paths
from src.models import Item, MissedReason, SourceHealth, SourceHealthReport

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = (
    "AI-Vector/0.1 (https://github.com/armanabrahamyan/ai-vector;"
    " daily-newsletter)"
)

# Seconds before a single source fetch is abandoned.
FETCH_TIMEOUT_SECONDS: int = 15

# HN Algolia: only include stories whose points meet or exceed this threshold.
# Tune this constant to calibrate volume vs. signal.
HN_POINTS_THRESHOLD: int = 50

# Drop items whose `published_at` is older than this at fetch time.
# Many feeds return their full archive; we only want today + recent yesterday
# for a daily newsletter. Items dropped here do NOT count toward items_kept.
MAX_ITEM_AGE_DAYS: int = 2

# Bug #70 — BIS future timestamps.
# Clamp any `published_at` more than this many hours ahead of `fetched_at`
# to `fetched_at`.  Feeds occasionally embed bogus far-future dates (observed:
# BIS reshub_papers.rss entry with published_at=2035-08-31).  The item is real;
# the timestamp is a typo.  We treat it as "just-arrived" so freshness scoring
# stays accurate.  A WARNING is logged with the original value so the Eval
# Engineer can spot patterns.
MAX_FUTURE_CLOCK_SKEW_HOURS: int = 24

# Bug #72 — Lil'Log FAQ ingestion.
# Hugo-generated feeds include non-post pages (FAQ, about, etc.) as entries.
# Drop any item whose URL path matches one of these prefixes/patterns.
# Extend this list if new sources surface similar junk pages.
_JUNK_URL_PATH_PREFIXES: tuple[str, ...] = ("/faq/", "/about/", "/tags/", "/categories/")

# Reddit: number of top hot posts to pull per subreddit.
REDDIT_LIMIT: int = 30

# Maximum characters for raw_summary (Item.raw_summary max_length = 8000 chars
# per models.py; we cap here before constructing the model).
RAW_SUMMARY_MAX_CHARS: int = 8000
RAW_SUMMARY_TRUNCATION_SUFFIX: str = "…"

# HF Daily Papers API
HF_DAILY_PAPERS_URL: str = "https://huggingface.co/api/daily_papers"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers — HTML stripping
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    no_tags = _HTML_TAG_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", no_tags).strip()


def _cap_summary(text: str) -> str:
    """Truncate to RAW_SUMMARY_MAX_CHARS, appending ellipsis if cut."""
    if len(text) <= RAW_SUMMARY_MAX_CHARS:
        return text
    cut = text[: RAW_SUMMARY_MAX_CHARS - len(RAW_SUMMARY_TRUNCATION_SUFFIX)]
    return cut + RAW_SUMMARY_TRUNCATION_SUFFIX


# ---------------------------------------------------------------------------
# Helpers — stable ID
# ---------------------------------------------------------------------------


def _url_hash(url: str) -> str:
    """Return 16-char hex sha256 of the URL — stable per URL."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Helpers — timestamp parsing
# ---------------------------------------------------------------------------


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.timezone.utc)


def _feedparser_time_to_datetime(
    t: time.struct_time | None,
) -> datetime.datetime | None:
    """Convert feedparser's time.struct_time (parsed_at UTC) to a tz-aware datetime."""
    if t is None:
        return None
    try:
        ts = time.mktime(t)
        return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    except (OverflowError, ValueError):
        return None


def _parse_unix_timestamp(value: int | float | None) -> datetime.datetime | None:
    """Convert a Unix epoch number to a UTC-aware datetime."""
    if value is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(float(value), tz=datetime.timezone.utc)
    except (OverflowError, ValueError, OSError):
        return None


def _parse_iso_timestamp(value: str | None) -> datetime.datetime | None:
    """Parse an ISO 8601 string into a UTC-aware datetime. Returns None on failure."""
    if not value:
        return None
    try:
        # Python 3.11+ fromisoformat handles 'Z'; for 3.10 we normalise first.
        normalised = value.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(normalised)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Helpers — atomic writes
# ---------------------------------------------------------------------------


def _atomic_write_jsonl(path: Path, items: Iterable[BaseModel]) -> None:
    """Write pydantic models as JSONL, atomically (tmp → fsync → rename)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            for item in items:
                fh.write(item.model_dump_json() + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, obj: BaseModel) -> None:
    """Write a single pydantic model as JSON, atomically (tmp → fsync → rename)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(obj.model_dump_json(indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Source-specific fetch helpers
# ---------------------------------------------------------------------------


def _fetch_rss(source: dict[str, Any]) -> tuple[list[Item], SourceHealth]:
    """
    Fetch an RSS or Atom source via feedparser.

    Returns (items, health) where health records the outcome.  Never raises —
    all exceptions are caught and recorded in health.missed_reason.
    """
    name: str = source["name"]
    url: str = source["url"]
    trust_weight: int = int(source.get("trust_weight", 3))
    source_type: str = source.get("type", "rss")  # "rss" or "atom"

    fetched_at = _utcnow()
    t0 = time.monotonic()

    try:
        feed = feedparser.parse(
            url,
            request_headers={"User-Agent": USER_AGENT},
            # feedparser uses urllib under the hood; we set a socket-level
            # timeout via the agent string and rely on feedparser's built-in
            # timeout parameter where supported.
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        # feedparser never raises on parse failure; it sets bozo=True instead.
        # Treat HTTP errors from status explicitly.
        http_status: int | None = getattr(feed, "status", None)
        if http_status is not None and http_status >= 400:
            reason: MissedReason = (
                "http_4xx" if 400 <= http_status < 500 else "http_5xx"
            )
            log.error(
                "[%s] HTTP %s from feed, marking as missed", name, http_status
            )
            return [], SourceHealth(
                source=name,
                fired=False,
                items_in=0,
                items_kept=0,
                latency_ms=latency_ms,
                missed_reason=reason,
            )

        entries = feed.get("entries", [])
        items: list[Item] = []
        parse_errors = 0

        for entry in entries:
            try:
                raw_link: str = (
                    entry.get("link")
                    or entry.get("id")
                    or ""
                ).strip()
                if not raw_link:
                    continue

                title_raw = entry.get("title", "").strip()
                title = _strip_html(title_raw) or raw_link

                # Summary: prefer summary, fall back to content[0].value
                summary_raw = entry.get("summary", "")
                if not summary_raw:
                    content_list = entry.get("content", [])
                    if content_list:
                        summary_raw = content_list[0].get("value", "")
                raw_summary = _cap_summary(_strip_html(summary_raw))

                # Bug #72 — drop Hugo-generated junk pages (FAQ, about, etc.)
                # that appear as RSS entries alongside real blog posts.
                _parsed_url = _urlparse(raw_link)
                _path = _parsed_url.path.rstrip("/") + "/"
                if any(_path.startswith(prefix) for prefix in _JUNK_URL_PATH_PREFIXES):
                    log.debug(
                        "[%s] dropping non-post page entry: %s", name, raw_link
                    )
                    continue

                # Timestamps: prefer published_parsed then updated_parsed
                pub_dt = (
                    _feedparser_time_to_datetime(entry.get("published_parsed"))
                    or _feedparser_time_to_datetime(entry.get("updated_parsed"))
                )
                published_at = pub_dt or fetched_at

                # Bug #70 — clamp far-future timestamps.
                # Some feeds (observed: BIS reshub_papers.rss) emit published_at
                # values years in the future (typo in feed metadata).  The item
                # is real; treat it as just-arrived so freshness scoring is
                # accurate.  Log a WARNING so the Eval Engineer can spot
                # recurring patterns.
                skew_limit = datetime.timedelta(hours=MAX_FUTURE_CLOCK_SKEW_HOURS)
                if published_at > fetched_at + skew_limit:
                    log.warning(
                        "[%s] future published_at clamped: original=%s url=%s",
                        name,
                        published_at.isoformat(),
                        raw_link,
                    )
                    published_at = fetched_at

                # last_modified from feed-level metadata
                last_mod = _feedparser_time_to_datetime(
                    feed.feed.get("updated_parsed")
                )

                item = Item(
                    id=_url_hash(raw_link),
                    source=name,
                    source_type=source_type,  # type: ignore[arg-type]
                    url=raw_link,  # type: ignore[arg-type]
                    title=title[:512],
                    published_at=published_at,
                    raw_summary=raw_summary,
                    fetched_at=fetched_at,
                    trust_weight=trust_weight,
                )
                items.append(item)
            except Exception as exc:
                parse_errors += 1
                log.debug("[%s] skipping entry due to parse error: %s", name, exc)

        # Bug #71 — detect sources where every item's published_at fell back
        # to fetched_at (i.e. the feed provides no per-item pubdates, or they
        # are all identical and match fetched_at to the second).  When this
        # pattern is detected: tag every item with extras["freshness_inferred"]
        # = "true" so rank.py can downweight freshness_momentum, and emit a
        # WARNING so the Eval Engineer sees the pattern in logs.
        # Detection: >= 2 items, all share the same published_at (truncated to
        # second), and that shared timestamp equals fetched_at (to the second).
        if len(items) >= 2:
            fetched_at_sec = fetched_at.replace(microsecond=0)
            pub_secs = {item.published_at.replace(microsecond=0) for item in items}
            if len(pub_secs) == 1 and fetched_at_sec in pub_secs:
                log.warning(
                    "[%s] all %d items share published_at=fetched_at — "
                    "feed likely has no per-item pubdates; "
                    "marking freshness_inferred=true on all items",
                    name,
                    len(items),
                )
                tagged: list[Item] = []
                for _item in items:
                    tagged.append(
                        Item(
                            id=_item.id,
                            source=_item.source,
                            source_type=_item.source_type,
                            url=str(_item.url),  # type: ignore[arg-type]
                            title=_item.title,
                            published_at=_item.published_at,
                            raw_summary=_item.raw_summary,
                            fetched_at=_item.fetched_at,
                            trust_weight=_item.trust_weight,
                            language=_item.language,
                            extras={**_item.extras, "freshness_inferred": "true"},
                        )
                    )
                items = tagged

        missed_reason: MissedReason | None = None
        fired = True
        if parse_errors and not items:
            missed_reason = "parse_error"
            fired = False
        elif not items:
            missed_reason = "empty_feed"

        log.info(
            "[%s] fetched %d items in %dms%s",
            name,
            len(items),
            latency_ms,
            f" (parse_errors={parse_errors})" if parse_errors else "",
        )

        last_mod = _feedparser_time_to_datetime(
            feed.feed.get("updated_parsed")  # type: ignore[attr-defined]
        )

        return items, SourceHealth(
            source=name,
            fired=fired,
            items_in=len(entries),
            items_kept=0,  # filled in after global dedup
            latency_ms=latency_ms,
            last_modified=last_mod,
            missed_reason=missed_reason,
        )

    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        log.error("[%s] network/parse error: %s", name, exc)
        return [], SourceHealth(
            source=name,
            fired=False,
            items_in=0,
            items_kept=0,
            latency_ms=latency_ms,
            missed_reason="timeout",
        )


def _fetch_hn(
    source: dict[str, Any], client: httpx.Client
) -> tuple[list[Item], SourceHealth]:
    """
    Fetch the Hacker News front page via the Algolia search API.

    URL in sources.yaml already encodes query params (tags, numericFilters).
    We additionally filter client-side by HN_POINTS_THRESHOLD in case the
    YAML URL uses a different threshold.

    HN item URL resolution: if the story has an external URL, use it;
    otherwise fall back to the HN discussion page.
    """
    name: str = source["name"]
    url: str = source["url"]
    trust_weight: int = int(source.get("trust_weight", 3))
    fetched_at = _utcnow()
    t0 = time.monotonic()

    def _attempt() -> httpx.Response:
        return client.get(url, timeout=FETCH_TIMEOUT_SECONDS)

    try:
        try:
            response = _attempt()
        except (httpx.TimeoutException, httpx.NetworkError) as first_exc:
            log.warning("[%s] first attempt failed (%s), retrying", name, first_exc)
            time.sleep(2)
            response = _attempt()

        latency_ms = int((time.monotonic() - t0) * 1000)

        if response.status_code >= 400:
            reason: MissedReason = (
                "http_4xx" if response.status_code < 500 else "http_5xx"
            )
            log.error("[%s] HTTP %s", name, response.status_code)
            return [], SourceHealth(
                source=name,
                fired=False,
                items_in=0,
                items_kept=0,
                latency_ms=latency_ms,
                missed_reason=reason,
            )

        data = response.json()
        hits: list[dict[str, Any]] = data.get("hits", [])
        items: list[Item] = []
        parse_errors = 0

        for hit in hits:
            try:
                points: int = int(hit.get("points") or 0)
                if points < HN_POINTS_THRESHOLD:
                    continue

                object_id = hit.get("objectID", "")
                story_url: str = (
                    hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}"
                ).strip()

                title = (hit.get("title") or "").strip()
                if not title:
                    continue

                created_iso = hit.get("created_at")
                published_at = _parse_iso_timestamp(created_iso) or fetched_at

                # raw_summary: story text first; fall back to title
                summary_raw = _strip_html(hit.get("story_text") or "")
                if not summary_raw:
                    summary_raw = title
                raw_summary = _cap_summary(summary_raw)

                item = Item(
                    id=_url_hash(story_url),
                    source=name,
                    source_type="api",
                    url=story_url,  # type: ignore[arg-type]
                    title=title[:512],
                    published_at=published_at,
                    raw_summary=raw_summary,
                    fetched_at=fetched_at,
                    trust_weight=trust_weight,
                    extras={"hn_points": str(points), "hn_object_id": str(object_id)},
                )
                items.append(item)
            except Exception as exc:
                parse_errors += 1
                log.debug("[%s] skipping HN hit: %s", name, exc)

        missed_reason: MissedReason | None = None
        fired = True
        if not items and not hits:
            missed_reason = "empty_feed"
        elif not items and parse_errors:
            missed_reason = "parse_error"
            fired = False

        log.info("[%s] fetched %d items in %dms", name, len(items), latency_ms)

        return items, SourceHealth(
            source=name,
            fired=fired,
            items_in=len(hits),
            items_kept=0,
            latency_ms=latency_ms,
            missed_reason=missed_reason,
        )

    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        log.error("[%s] network error after retry: %s", name, exc)
        return [], SourceHealth(
            source=name,
            fired=False,
            items_in=0,
            items_kept=0,
            latency_ms=latency_ms,
            missed_reason="timeout",
        )
    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        log.error("[%s] unexpected error: %s", name, exc)
        return [], SourceHealth(
            source=name,
            fired=False,
            items_in=0,
            items_kept=0,
            latency_ms=latency_ms,
            missed_reason="parse_error",
        )


def _fetch_reddit(
    source: dict[str, Any], client: httpx.Client
) -> tuple[list[Item], SourceHealth]:
    """
    Fetch a subreddit's hot posts via the Reddit JSON API (no auth needed for
    public read at this volume).

    URL pattern in sources.yaml: https://www.reddit.com/r/<sub>.json
    We append ?limit=N for controlled volume.
    """
    name: str = source["name"]
    base_url: str = source["url"]
    trust_weight: int = int(source.get("trust_weight", 3))
    fetched_at = _utcnow()
    t0 = time.monotonic()

    # Append query params
    separator = "&" if "?" in base_url else "?"
    url = f"{base_url}{separator}limit={REDDIT_LIMIT}"

    def _attempt() -> httpx.Response:
        return client.get(url, timeout=FETCH_TIMEOUT_SECONDS)

    try:
        try:
            response = _attempt()
        except (httpx.TimeoutException, httpx.NetworkError) as first_exc:
            log.warning("[%s] first attempt failed (%s), retrying", name, first_exc)
            time.sleep(2)
            response = _attempt()

        latency_ms = int((time.monotonic() - t0) * 1000)

        if response.status_code >= 400:
            reason: MissedReason = (
                "http_4xx" if response.status_code < 500 else "http_5xx"
            )
            log.error("[%s] HTTP %s", name, response.status_code)
            return [], SourceHealth(
                source=name,
                fired=False,
                items_in=0,
                items_kept=0,
                latency_ms=latency_ms,
                missed_reason=reason,
            )

        data = response.json()
        children: list[dict[str, Any]] = (
            data.get("data", {}).get("children", [])
        )
        items: list[Item] = []
        parse_errors = 0

        for child in children:
            try:
                post: dict[str, Any] = child.get("data", {})

                permalink: str = post.get("permalink", "")
                story_url = f"https://reddit.com{permalink}".rstrip("/")
                if not permalink:
                    continue

                title = (post.get("title") or "").strip()
                if not title:
                    continue

                created_utc = post.get("created_utc")
                published_at = _parse_unix_timestamp(created_utc) or fetched_at

                # Prefer self-text for raw_summary; empty for link posts
                selftext = _strip_html(post.get("selftext") or "")
                raw_summary = _cap_summary(selftext) if selftext else ""

                score: int = int(post.get("score") or 0)

                item = Item(
                    id=_url_hash(story_url),
                    source=name,
                    source_type="api",
                    url=story_url,  # type: ignore[arg-type]
                    title=title[:512],
                    published_at=published_at,
                    raw_summary=raw_summary,
                    fetched_at=fetched_at,
                    trust_weight=trust_weight,
                    extras={"reddit_score": str(score)},
                )
                items.append(item)
            except Exception as exc:
                parse_errors += 1
                log.debug("[%s] skipping Reddit post: %s", name, exc)

        missed_reason: MissedReason | None = None
        fired = True
        if not items and not children:
            missed_reason = "empty_feed"
        elif not items and parse_errors:
            missed_reason = "parse_error"
            fired = False

        log.info("[%s] fetched %d items in %dms", name, len(items), latency_ms)

        return items, SourceHealth(
            source=name,
            fired=fired,
            items_in=len(children),
            items_kept=0,
            latency_ms=latency_ms,
            missed_reason=missed_reason,
        )

    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        log.error("[%s] network error after retry: %s", name, exc)
        return [], SourceHealth(
            source=name,
            fired=False,
            items_in=0,
            items_kept=0,
            latency_ms=latency_ms,
            missed_reason="timeout",
        )
    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        log.error("[%s] unexpected error: %s", name, exc)
        return [], SourceHealth(
            source=name,
            fired=False,
            items_in=0,
            items_kept=0,
            latency_ms=latency_ms,
            missed_reason="parse_error",
        )


def _fetch_hf_daily_papers(
    source: dict[str, Any], client: httpx.Client
) -> tuple[list[Item], SourceHealth]:
    """
    Fetch Hugging Face Daily Papers from the HF API endpoint.

    Returns papers from today's curated list. Each paper becomes one Item.
    """
    name: str = source["name"]
    trust_weight: int = int(source.get("trust_weight", 3))
    fetched_at = _utcnow()
    t0 = time.monotonic()

    def _attempt() -> httpx.Response:
        return client.get(HF_DAILY_PAPERS_URL, timeout=FETCH_TIMEOUT_SECONDS)

    try:
        try:
            response = _attempt()
        except (httpx.TimeoutException, httpx.NetworkError) as first_exc:
            log.warning("[%s] first attempt failed (%s), retrying", name, first_exc)
            time.sleep(2)
            response = _attempt()

        latency_ms = int((time.monotonic() - t0) * 1000)

        if response.status_code >= 400:
            reason: MissedReason = (
                "http_4xx" if response.status_code < 500 else "http_5xx"
            )
            log.error("[%s] HTTP %s", name, response.status_code)
            return [], SourceHealth(
                source=name,
                fired=False,
                items_in=0,
                items_kept=0,
                latency_ms=latency_ms,
                missed_reason=reason,
            )

        papers: list[dict[str, Any]] = response.json()
        if not isinstance(papers, list):
            # API may return {"error": ...} on bad days
            log.error("[%s] unexpected response format: %s", name, type(papers))
            return [], SourceHealth(
                source=name,
                fired=False,
                items_in=0,
                items_kept=0,
                latency_ms=latency_ms,
                missed_reason="parse_error",
            )

        items: list[Item] = []
        parse_errors = 0

        for paper in papers:
            try:
                paper_id: str = paper.get("paper", {}).get("id") or paper.get("id", "")
                if not paper_id:
                    continue

                story_url = f"https://huggingface.co/papers/{paper_id}"
                title = (
                    paper.get("paper", {}).get("title")
                    or paper.get("title", "")
                ).strip()
                if not title:
                    continue

                # Abstract as summary
                abstract = _strip_html(
                    paper.get("paper", {}).get("abstract")
                    or paper.get("abstract", "")
                    or ""
                )
                raw_summary = _cap_summary(abstract)

                # publishedAt field from HF API
                pub_iso = (
                    paper.get("paper", {}).get("publishedAt")
                    or paper.get("publishedAt")
                    or paper.get("createdAt")
                )
                published_at = _parse_iso_timestamp(pub_iso) or fetched_at

                item = Item(
                    id=_url_hash(story_url),
                    source=name,
                    source_type="api",
                    url=story_url,  # type: ignore[arg-type]
                    title=title[:512],
                    published_at=published_at,
                    raw_summary=raw_summary,
                    fetched_at=fetched_at,
                    trust_weight=trust_weight,
                )
                items.append(item)
            except Exception as exc:
                parse_errors += 1
                log.debug("[%s] skipping HF paper: %s", name, exc)

        missed_reason: MissedReason | None = None
        fired = True
        if not items and not papers:
            missed_reason = "empty_feed"
        elif not items and parse_errors:
            missed_reason = "parse_error"
            fired = False

        log.info("[%s] fetched %d items in %dms", name, len(items), latency_ms)

        return items, SourceHealth(
            source=name,
            fired=fired,
            items_in=len(papers),
            items_kept=0,
            latency_ms=latency_ms,
            missed_reason=missed_reason,
        )

    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        log.error("[%s] network error after retry: %s", name, exc)
        return [], SourceHealth(
            source=name,
            fired=False,
            items_in=0,
            items_kept=0,
            latency_ms=latency_ms,
            missed_reason="timeout",
        )
    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        log.error("[%s] unexpected error: %s", name, exc)
        return [], SourceHealth(
            source=name,
            fired=False,
            items_in=0,
            items_kept=0,
            latency_ms=latency_ms,
            missed_reason="parse_error",
        )


# ---------------------------------------------------------------------------
# API source router
# ---------------------------------------------------------------------------

# Substrings used to identify which API handler to call, keyed on source URL.
_HN_URL_FRAGMENT = "hn.algolia.com"
_REDDIT_URL_FRAGMENT = "reddit.com"
_HF_PAPERS_URL_FRAGMENT = "huggingface.co/api/daily_papers"


def _dispatch_api_source(
    source: dict[str, Any], client: httpx.Client
) -> tuple[list[Item], SourceHealth]:
    """Route a type:api source to its specific handler."""
    url: str = source["url"]
    if _HN_URL_FRAGMENT in url:
        return _fetch_hn(source, client)
    if _REDDIT_URL_FRAGMENT in url:
        return _fetch_reddit(source, client)
    if _HF_PAPERS_URL_FRAGMENT in url:
        return _fetch_hf_daily_papers(source, client)
    # Fallback: unknown API type — record as missed
    name = source["name"]
    log.error("[%s] unknown API type, no handler for URL: %s", name, url)
    return [], SourceHealth(
        source=name,
        fired=False,
        items_in=0,
        items_kept=0,
        latency_ms=0,
        missed_reason="parse_error",
    )


# ---------------------------------------------------------------------------
# Cross-issue dedup against published_urls.txt
# ---------------------------------------------------------------------------


def _load_published_urls() -> set[str]:
    """Return the set of URLs that have already been shipped to readers.

    Reads ``data/published_urls.txt`` — one URL per line, atomically
    maintained by ``aiv release`` on every publish.  This is the correct
    dedup target: once a URL is in a released issue it must never reappear,
    regardless of how long ago it was shipped.

    Tolerant of a missing file (fresh repo or pre-first-release run returns
    an empty set — no filtering applied).
    """
    p = paths.PUBLISHED_URLS_PATH
    if not p.exists():
        return set()
    try:
        return set(
            line.strip()
            for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    except Exception as exc:  # noqa: BLE001 -- never crash fetch on read error
        log.warning(
            "fetch: could not read %s for dedup: %s",
            p, exc,
        )
        return set()


# ---------------------------------------------------------------------------
# Within-batch URL deduplication
# ---------------------------------------------------------------------------


def _dedup_items(
    source_items: list[tuple[dict[str, Any], list[Item]]]
) -> tuple[list[Item], dict[str, int]]:
    """
    Deduplicate items across all sources by exact URL (str comparison on the
    string form of Item.url).

    Tie-breaking: when two items share a URL, keep the one from the source
    with the higher trust_weight.  If trust weights are equal, keep the
    first item encountered (source order = order in sources.yaml).

    Returns:
        (all_kept_items, kept_count_by_source_name)
    """
    # Map url → Item, resolving ties via trust_weight
    url_to_item: dict[str, Item] = {}

    for source_cfg, items in source_items:
        for item in items:
            url_str = str(item.url)
            if url_str not in url_to_item:
                url_to_item[url_str] = item
            else:
                existing = url_to_item[url_str]
                # Higher trust_weight wins; ties go to first-seen (no change)
                if item.trust_weight > existing.trust_weight:
                    url_to_item[url_str] = item

    kept = list(url_to_item.values())

    # Count kept items per source
    kept_count: dict[str, int] = {}
    for item in kept:
        kept_count[item.source] = kept_count.get(item.source, 0) + 1

    return kept, kept_count


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_day(
    run_date: datetime.date,
    config_path: Path = Path("config/sources.yaml"),
    out_dir: Path | None = None,  # kept for backward-compat; ignored
) -> tuple[list[Item], list[SourceHealth]]:
    """
    Fetch all enabled sources for `run_date`.

    Writes (per Round B / DESIGN.md "Archive: staging vs canonical"):
        data/staging/YYYY-MM-DD/items.jsonl
        data/staging/YYYY-MM-DD/source_health.json

    The legacy `out_dir` parameter is retained for compatibility with older
    callers but is ignored -- staging paths come from `src.paths`.

    Returns (items, source_healths) for the caller.  Disabled sources appear
    in source_healths with fired=False, missed_reason="disabled".
    """
    run_started_at = _utcnow()
    wall_t0 = time.monotonic()

    if out_dir is not None and out_dir != paths.DATA_ROOT:
        log.warning(
            "fetch_day: out_dir=%s is ignored in Round B; writing to %s",
            out_dir,
            paths.staging_dir(run_date),
        )

    # ---- Load config -------------------------------------------------------
    with config_path.open("r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    sources: list[dict[str, Any]] = config.get("sources", [])

    # ---- Ensure output directory (staging) ---------------------------------
    day_dir = paths.staging_dir(run_date)
    day_dir.mkdir(parents=True, exist_ok=True)

    # ---- Fetch all enabled sources -----------------------------------------
    # We collect (source_cfg, items) pairs to hand to the dedup pass.
    source_items: list[tuple[dict[str, Any], list[Item]]] = []
    health_map: dict[str, SourceHealth] = {}

    fired_count = 0
    total_in = 0

    # Shared httpx client for all API sources (connection pooling, single UA)
    with httpx.Client(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        for source in sources:
            name: str = source["name"]
            enabled: bool = bool(source.get("enabled", True))

            if not enabled:
                health_map[name] = SourceHealth(
                    source=name,
                    fired=False,
                    items_in=0,
                    items_kept=0,
                    latency_ms=0,
                    missed_reason="disabled",
                )
                source_items.append((source, []))
                continue

            src_type: str = source.get("type", "rss")

            if src_type in ("rss", "atom"):
                items, health = _fetch_rss(source)
            elif src_type == "api":
                items, health = _dispatch_api_source(source, client)
            else:
                log.error("[%s] unrecognised source type: %s", name, src_type)
                items = []
                health = SourceHealth(
                    source=name,
                    fired=False,
                    items_in=0,
                    items_kept=0,
                    latency_ms=0,
                    missed_reason="parse_error",
                )

            health_map[name] = health
            source_items.append((source, items))

            if health.fired:
                fired_count += 1
            total_in += health.items_in

    # ---- Within-batch URL dedup -------------------------------------------
    kept_items, kept_count_by_source = _dedup_items(source_items)

    # ---- Recency filter (per-source max_age_days) --------------------------
    # Drop items whose `published_at` is older than the source's max_age_days
    # window.  Sources publish on different cadences: daily news feeds need 2
    # days; weekly/monthly FS research sources (BIS, FRBNY, Bank Underground,
    # etc.) need 7–14 days so their items are not silently discarded.
    #
    # Per-source override: read `max_age_days` from the source config entry.
    # When absent, fall back to the global MAX_ITEM_AGE_DAYS constant.
    #
    # Build a lookup from source name → max_age_days for fast per-item lookup.
    source_max_age: dict[str, int] = {
        src["name"]: int(src["max_age_days"])
        for src in sources
        if "max_age_days" in src
    }
    now = _utcnow()
    before_age_filter = len(kept_items)
    kept_after_age: list[Item] = []
    for item in kept_items:
        age_days = source_max_age.get(item.source, MAX_ITEM_AGE_DAYS)
        cutoff = now - datetime.timedelta(days=age_days)
        if item.published_at >= cutoff:
            kept_after_age.append(item)
    dropped_old = before_age_filter - len(kept_after_age)
    kept_items = kept_after_age
    if dropped_old > 0:
        log.info(
            "filtered %d items older than their source max_age_days window",
            dropped_old,
        )

    # ---- Cross-issue item dedup (against published_urls.txt) ---------------
    # Drop items whose URL has already been shipped to readers.  The source
    # of truth is data/published_urls.txt — one URL per line, atomically
    # maintained by `aiv release` on every publish.  Once a URL is in a
    # released issue it must never reappear, regardless of age.
    #
    # This replaces the old "walk released items.jsonl for last N days"
    # approach, which deduped against *everything ever fetched* (227 items/day)
    # rather than *what we actually shipped* (≈12 stories/day).  That caused
    # arxiv and FS sources to be wiped entirely on re-fetch days.
    published_urls = _load_published_urls()
    before_published_filter = len(kept_items)
    kept_items = [item for item in kept_items if str(item.url) not in published_urls]
    dropped_published = before_published_filter - len(kept_items)
    if dropped_published > 0:
        log.info(
            "filtered %d items already in data/published_urls.txt (%d total published)",
            dropped_published, len(published_urls),
        )

    # Recompute kept_count_by_source after both filters so
    # source_health.items_kept reflects what actually made it through.
    kept_count_by_source = {}
    for item in kept_items:
        kept_count_by_source[item.source] = kept_count_by_source.get(item.source, 0) + 1

    # ---- Update items_kept in health records --------------------------------
    updated_healths: list[SourceHealth] = []
    for source in sources:
        name = source["name"]
        h = health_map[name]
        count_kept = kept_count_by_source.get(name, 0)
        # Rebuild with correct items_kept (pydantic model is frozen after init,
        # so we construct a new instance)
        updated_healths.append(
            SourceHealth(
                schema_version=h.schema_version,
                source=h.source,
                fired=h.fired,
                items_in=h.items_in,
                items_kept=count_kept,
                latency_ms=h.latency_ms,
                last_modified=h.last_modified,
                missed_reason=h.missed_reason,
            )
        )

    run_finished_at = _utcnow()
    total_ms = int((time.monotonic() - wall_t0) * 1000)

    # ---- Build SourceHealthReport ------------------------------------------
    report = SourceHealthReport(
        run_started_at=run_started_at,
        run_finished_at=run_finished_at,
        sources=updated_healths,
    )

    # ---- Atomic writes -----------------------------------------------------
    items_out = paths.items_path(run_date, canonical=False)
    health_out = paths.source_health_path(run_date, canonical=False)

    _atomic_write_jsonl(items_out, kept_items)
    _atomic_write_json(health_out, report)

    # ---- Summary log line --------------------------------------------------
    # Include staging path so Arman knows where the output went.
    log.info(
        "fetch complete: %d sources / %d fired / %d items / %dms -> %s/",
        len(sources),
        fired_count,
        len(kept_items),
        total_ms,
        day_dir,
    )

    return kept_items, updated_healths


def fetch(date: datetime.date | None = None) -> SourceHealthReport:
    """
    Fetch all enabled sources.  Writes items.jsonl + source_health.json under
    `data/staging/<date>/` (Round B).
    Returns the SourceHealthReport for the caller (run.py).

    If `date` is None, uses datetime.date.today() (local time).
    Caller may pass an explicit date for backfill / testing.
    """
    run_date = date or datetime.date.today()
    started_at = _utcnow()
    _, healths = fetch_day(run_date)

    # Read the SourceHealthReport back from disk — it carries the accurate
    # run_started_at / run_finished_at written by fetch_day (staging path).
    health_path = paths.source_health_path(run_date, canonical=False)
    try:
        with health_path.open("r", encoding="utf-8") as fh:
            return SourceHealthReport.model_validate_json(fh.read())
    except Exception:
        # Fallback: build from available data if disk read fails.
        return SourceHealthReport(
            run_started_at=started_at,
            run_finished_at=_utcnow(),
            sources=healths,
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    report = fetch()
    print(json.dumps(report.model_dump(), indent=2, default=str))
