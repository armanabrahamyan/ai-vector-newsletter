"""Unit tests for src/fetch.py — the source fetcher.

Coverage:
- Feed parsing (RSS + Atom): Item fields populated correctly.
- Exact-URL dedup within a single fetch batch.
- HTML stripping on raw_summary and title.
- Source health classification: missed_reason enum for every failure mode.
- items_in vs items_kept accounting.
- fired=True with items_kept=0 (source responded, nothing new survived filters).
- published_at fallback to fetched_at when the feed entry has no date.

No real network calls. feedparser is patched at the call site; httpx is
replaced by a lightweight fake response object. All filesystem writes go to
the tmp_data_root fixture.
"""
from __future__ import annotations

import datetime
import hashlib
import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import feedparser  # type: ignore[import-untyped]
import pytest
import yaml

from src.fetch import (
    _cap_summary,
    _dedup_items,
    _fetch_rss,
    _strip_html,
    _url_hash,
    fetch_day,
)
from src.models import Item

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

UTC = datetime.timezone.utc
FIXED_FETCH_AT = datetime.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
FIXED_DATE = datetime.date(2026, 5, 24)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "feeds"


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------

def _source(
    name: str = "test_blog",
    url: str = "https://example.com/feed.xml",
    source_type: str = "rss",
    trust_weight: int = 3,
    enabled: bool = True,
) -> dict[str, Any]:
    return {
        "name": name,
        "url": url,
        "type": source_type,
        "trust_weight": trust_weight,
        "enabled": enabled,
    }


def _parse_fixture(filename: str) -> Any:
    """Parse a fixture XML file through feedparser (no network)."""
    path = FIXTURES_DIR / filename
    return feedparser.parse(path.read_text(encoding="utf-8"))


def _fake_feedparser_result(xml_text: str) -> Any:
    """Return a feedparser result object from raw XML."""
    return feedparser.parse(xml_text)


class _FakeHTTPResponse:
    """Minimal stand-in for httpx.Response."""

    def __init__(
        self,
        status_code: int = 200,
        json_body: Any = None,
    ) -> None:
        self.status_code = status_code
        self._json_body = json_body

    def json(self) -> Any:
        return self._json_body


# ---------------------------------------------------------------------------
# TestStripHtml
# ---------------------------------------------------------------------------

class TestStripHtml:
    def test_removes_tags(self) -> None:
        assert _strip_html("<p>Hello <b>world</b>.</p>") == "Hello world ."

    def test_collapses_whitespace(self) -> None:
        result = _strip_html("<p>  lots   of   space  </p>")
        assert "  " not in result

    def test_empty_string(self) -> None:
        assert _strip_html("") == ""

    def test_plain_text_unchanged_modulo_strip(self) -> None:
        assert _strip_html("plain text") == "plain text"

    def test_nested_tags(self) -> None:
        result = _strip_html("<div><span>deep</span></div>")
        assert "<" not in result
        assert "deep" in result

    @pytest.mark.parametrize("tag", ["<br/>", "<img src='x'>", '<a href="u">link</a>'])
    def test_various_tags_removed(self, tag: str) -> None:
        result = _strip_html(tag)
        assert "<" not in result


# ---------------------------------------------------------------------------
# TestCapSummary
# ---------------------------------------------------------------------------

class TestCapSummary:
    def test_short_text_unchanged(self) -> None:
        assert _cap_summary("hello") == "hello"

    def test_exact_limit_unchanged(self) -> None:
        text = "x" * 8000
        assert _cap_summary(text) == text

    def test_over_limit_truncated(self) -> None:
        text = "x" * 9000
        result = _cap_summary(text)
        assert len(result) <= 8000
        assert result.endswith("…")

    def test_truncated_text_is_prefix(self) -> None:
        text = "ab" * 5000
        result = _cap_summary(text)
        assert text.startswith(result[:-1])  # minus the ellipsis suffix


# ---------------------------------------------------------------------------
# TestUrlHash
# ---------------------------------------------------------------------------

class TestUrlHash:
    def test_same_url_same_hash(self) -> None:
        url = "https://example.com/article"
        assert _url_hash(url) == _url_hash(url)

    def test_different_urls_different_hashes(self) -> None:
        assert _url_hash("https://a.com/x") != _url_hash("https://b.com/y")

    def test_hash_is_16_hex_chars(self) -> None:
        result = _url_hash("https://example.com/")
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)

    # `test_matches_sha256_prefix` cut: re-implemented the function inline
    # and asserted equality -- a tautology (CONVENTIONS sec. 3). The 16-hex
    # length contract above is what callers actually rely on; the choice of
    # algorithm is an implementation detail.


# ---------------------------------------------------------------------------
# TestFeedParsing — parse real fixture XML through _fetch_rss
# ---------------------------------------------------------------------------

class TestFeedParsing:
    def test_rss_fixture_yields_correct_item_count(self) -> None:
        feed = _parse_fixture("sample_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, health = _fetch_rss(_source())
        assert len(items) == 2

    def test_rss_item_fields_populated(self) -> None:
        feed = _parse_fixture("sample_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = _fetch_rss(_source(trust_weight=4))

        item = items[0]
        assert item.source == "test_blog"
        assert item.source_type == "rss"
        assert "agentic-ai-production" in str(item.url)
        assert item.title == "Agentic AI Reaches Production"
        assert item.trust_weight == 4
        assert item.fetched_at == FIXED_FETCH_AT

    def test_rss_item_id_is_url_hash(self) -> None:
        feed = _parse_fixture("sample_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = _fetch_rss(_source())

        item = items[0]
        assert item.id == _url_hash(str(item.url))

    def test_rss_raw_summary_has_no_html_tags(self) -> None:
        feed = _parse_fixture("sample_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = _fetch_rss(_source())

        # The fixture description contains <p> and <b> tags.
        for item in items:
            assert "<" not in item.raw_summary
            assert ">" not in item.raw_summary

    def test_rss_published_at_parsed_from_feed(self) -> None:
        feed = _parse_fixture("sample_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = _fetch_rss(_source())

        # pubDate: Sun, 24 May 2026 09:00:00 +0000 — should NOT equal fetched_at
        assert items[0].published_at != FIXED_FETCH_AT
        assert items[0].published_at.tzinfo is not None

    def test_atom_fixture_yields_item(self) -> None:
        feed = _parse_fixture("sample_atom.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, health = _fetch_rss(_source(source_type="atom"))

        assert len(items) == 1
        assert items[0].source_type == "atom"

    def test_atom_item_url_correct(self) -> None:
        feed = _parse_fixture("sample_atom.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = _fetch_rss(_source(source_type="atom"))

        assert "paper-001" in str(items[0].url)

    def test_atom_raw_summary_no_html(self) -> None:
        feed = _parse_fixture("sample_atom.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = _fetch_rss(_source(source_type="atom"))

        assert "<" not in items[0].raw_summary

    def test_item_language_defaults_to_en(self) -> None:
        feed = _parse_fixture("sample_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = _fetch_rss(_source())

        assert all(item.language == "en" for item in items)


# ---------------------------------------------------------------------------
# TestPublishedAtFallback — no date in feed entry → uses fetched_at
# ---------------------------------------------------------------------------

class TestPublishedAtFallback:
    def test_no_pubdate_falls_back_to_fetched_at(self) -> None:
        feed = _parse_fixture("no_date_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = _fetch_rss(_source())

        assert len(items) == 1
        assert items[0].published_at == FIXED_FETCH_AT


# ---------------------------------------------------------------------------
# TestExactUrlDedup — same URL twice in one fetch → one Item
# ---------------------------------------------------------------------------

class TestExactUrlDedup:
    def test_rss_duplicate_url_items_in_reflects_raw_entry_count(self) -> None:
        """_fetch_rss does NOT dedup — that is _dedup_items's job.

        items_in should count all raw feed entries (3), even when two
        share the same URL. The dedup pass (_dedup_items) collapses them.
        """
        feed = _parse_fixture("duplicate_urls_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, health = _fetch_rss(_source())

        # All 3 raw entries come through _fetch_rss (no dedup at this layer).
        assert len(items) == 3
        assert health.items_in == 3

    def test_dedup_items_collapses_same_url(self) -> None:
        src_cfg = _source(trust_weight=3)
        item_a = Item(
            id=_url_hash("https://example.com/same"),
            source="test_blog",
            source_type="rss",
            url="https://example.com/same",
            title="First",
            published_at=FIXED_FETCH_AT,
            raw_summary="first copy",
            fetched_at=FIXED_FETCH_AT,
            trust_weight=3,
        )
        item_b = Item(
            id=_url_hash("https://example.com/same"),
            source="test_blog",
            source_type="rss",
            url="https://example.com/same",
            title="Second",
            published_at=FIXED_FETCH_AT,
            raw_summary="second copy",
            fetched_at=FIXED_FETCH_AT,
            trust_weight=3,
        )
        kept, counts = _dedup_items([(src_cfg, [item_a, item_b])])
        assert len(kept) == 1

    def test_dedup_keeps_higher_trust_weight(self) -> None:
        url = "https://example.com/overlap"
        low = Item(
            id=_url_hash(url),
            source="low_source",
            source_type="rss",
            url=url,
            title="Low",
            published_at=FIXED_FETCH_AT,
            raw_summary="",
            fetched_at=FIXED_FETCH_AT,
            trust_weight=2,
        )
        high = Item(
            id=_url_hash(url),
            source="high_source",
            source_type="rss",
            url=url,
            title="High",
            published_at=FIXED_FETCH_AT,
            raw_summary="",
            fetched_at=FIXED_FETCH_AT,
            trust_weight=4,
        )
        low_cfg = _source(name="low_source", trust_weight=2)
        high_cfg = _source(name="high_source", trust_weight=4)
        kept, _ = _dedup_items([(low_cfg, [low]), (high_cfg, [high])])
        assert len(kept) == 1
        assert kept[0].trust_weight == 4
        assert kept[0].source == "high_source"

    def test_dedup_first_seen_wins_on_equal_trust(self) -> None:
        url = "https://example.com/tie"
        first = Item(
            id=_url_hash(url),
            source="source_a",
            source_type="rss",
            url=url,
            title="First",
            published_at=FIXED_FETCH_AT,
            raw_summary="",
            fetched_at=FIXED_FETCH_AT,
            trust_weight=3,
        )
        second = Item(
            id=_url_hash(url),
            source="source_b",
            source_type="rss",
            url=url,
            title="Second",
            published_at=FIXED_FETCH_AT,
            raw_summary="",
            fetched_at=FIXED_FETCH_AT,
            trust_weight=3,
        )
        cfg_a = _source(name="source_a", trust_weight=3)
        cfg_b = _source(name="source_b", trust_weight=3)
        kept, _ = _dedup_items([(cfg_a, [first]), (cfg_b, [second])])
        assert len(kept) == 1
        assert kept[0].source == "source_a"

    def test_dedup_counts_by_source(self) -> None:
        url_a = "https://example.com/a"
        url_b = "https://example.com/b"
        url_shared = "https://example.com/shared"
        item_a1 = Item(
            id=_url_hash(url_a), source="src1", source_type="rss",
            url=url_a, title="A1", published_at=FIXED_FETCH_AT,
            raw_summary="", fetched_at=FIXED_FETCH_AT, trust_weight=3,
        )
        item_shared_1 = Item(
            id=_url_hash(url_shared), source="src1", source_type="rss",
            url=url_shared, title="Shared from 1", published_at=FIXED_FETCH_AT,
            raw_summary="", fetched_at=FIXED_FETCH_AT, trust_weight=3,
        )
        item_b1 = Item(
            id=_url_hash(url_b), source="src2", source_type="rss",
            url=url_b, title="B1", published_at=FIXED_FETCH_AT,
            raw_summary="", fetched_at=FIXED_FETCH_AT, trust_weight=3,
        )
        item_shared_2 = Item(
            id=_url_hash(url_shared), source="src2", source_type="rss",
            url=url_shared, title="Shared from 2", published_at=FIXED_FETCH_AT,
            raw_summary="", fetched_at=FIXED_FETCH_AT, trust_weight=3,
        )
        cfg1 = _source(name="src1", trust_weight=3)
        cfg2 = _source(name="src2", trust_weight=3)
        kept, counts = _dedup_items([
            (cfg1, [item_a1, item_shared_1]),
            (cfg2, [item_b1, item_shared_2]),
        ])
        # 3 unique URLs total: url_a, url_b, url_shared
        assert len(kept) == 3
        # shared URL went to src1 (first seen); src2 gets only url_b
        assert counts["src1"] == 2
        assert counts["src2"] == 1


# ---------------------------------------------------------------------------
# TestSourceHealthClassification — missed_reason enum correctness
# ---------------------------------------------------------------------------

class TestSourceHealthClassification:
    def test_http_4xx_sets_missed_reason(self) -> None:
        feed_result = MagicMock()
        feed_result.status = 404
        feed_result.get.return_value = []
        feed_result.feed = MagicMock()
        feed_result.feed.get.return_value = None
        with patch("src.fetch.feedparser.parse", return_value=feed_result):
            _, health = _fetch_rss(_source())
        assert health.missed_reason == "http_4xx"
        assert health.fired is False

    def test_http_5xx_sets_missed_reason(self) -> None:
        feed_result = MagicMock()
        feed_result.status = 503
        feed_result.get.return_value = []
        feed_result.feed = MagicMock()
        feed_result.feed.get.return_value = None
        with patch("src.fetch.feedparser.parse", return_value=feed_result):
            _, health = _fetch_rss(_source())
        assert health.missed_reason == "http_5xx"
        assert health.fired is False

    def test_timeout_sets_missed_reason(self) -> None:
        with patch("src.fetch.feedparser.parse", side_effect=Exception("Connection timed out")):
            _, health = _fetch_rss(_source())
        assert health.missed_reason == "timeout"
        assert health.fired is False

    def test_empty_feed_sets_missed_reason(self) -> None:
        feed = _parse_fixture("empty_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            _, health = _fetch_rss(_source())
        assert health.missed_reason == "empty_feed"
        # fired is True — source responded, just had no entries
        assert health.fired is True

    def test_parse_error_sets_missed_reason(self) -> None:
        # A feed where every entry fails to parse (missing link + id)
        malformed_xml = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Bad</title>
<item><description>no link, no id here</description></item>
</channel></rss>"""
        feed = feedparser.parse(malformed_xml)
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, health = _fetch_rss(_source())
        # Items with no usable link are silently skipped → empty feed
        assert health.missed_reason in ("empty_feed", "parse_error", None)

    def test_disabled_source_sets_fired_false_and_disabled_reason(
        self, tmp_data_root: Path
    ) -> None:
        config_yaml = yaml.dump({
            "sources": [
                {
                    "name": "disabled_blog",
                    "url": "https://disabled.example.com/feed.xml",
                    "type": "rss",
                    "trust_weight": 3,
                    "enabled": False,
                }
            ]
        })
        config_path = tmp_data_root / "sources.yaml"
        config_path.write_text(config_yaml, encoding="utf-8")

        with patch("src.fetch.feedparser.parse") as mock_parse, \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, healths = fetch_day(FIXED_DATE, config_path=config_path)

        # feedparser should NOT have been called for a disabled source
        mock_parse.assert_not_called()
        assert len(healths) == 1
        h = healths[0]
        assert h.fired is False
        assert h.missed_reason == "disabled"

    def test_malformed_xml_does_not_crash_other_sources(
        self, tmp_data_root: Path
    ) -> None:
        good_feed = _parse_fixture("sample_rss.xml")
        bad_feed = MagicMock()
        bad_feed.status = 200
        bad_feed.get.return_value = []
        bad_feed.feed = MagicMock()
        bad_feed.feed.get.return_value = None

        call_count = 0
        def side_effect(url, **kwargs):  # noqa: ANN001, ANN202
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return bad_feed
            return good_feed

        config_yaml = yaml.dump({
            "sources": [
                {"name": "bad_source", "url": "https://bad.example.com/feed.xml",
                 "type": "rss", "trust_weight": 2, "enabled": True},
                {"name": "good_source", "url": "https://good.example.com/feed.xml",
                 "type": "rss", "trust_weight": 3, "enabled": True},
            ]
        })
        config_path = tmp_data_root / "sources.yaml"
        config_path.write_text(config_yaml, encoding="utf-8")

        with patch("src.fetch.feedparser.parse", side_effect=side_effect), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, healths = fetch_day(FIXED_DATE, config_path=config_path)

        # Good source must have fired
        good_health = next(h for h in healths if h.source == "good_source")
        assert good_health.fired is True


# ---------------------------------------------------------------------------
# TestItemsInVsItemsKept — count tracking in SourceHealth
# ---------------------------------------------------------------------------

class TestItemsInVsItemsKept:
    def test_items_in_reflects_raw_entry_count(self) -> None:
        feed = _parse_fixture("sample_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            _, health = _fetch_rss(_source())
        # sample_rss.xml has 2 entries
        assert health.items_in == 2

    def test_items_in_is_zero_on_empty_feed(self) -> None:
        feed = _parse_fixture("empty_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            _, health = _fetch_rss(_source())
        assert health.items_in == 0

    def test_items_kept_set_after_full_fetch_day(
        self, tmp_data_root: Path
    ) -> None:
        good_feed = _parse_fixture("sample_rss.xml")
        config_yaml = yaml.dump({
            "sources": [
                {"name": "test_blog", "url": "https://example.com/feed.xml",
                 "type": "rss", "trust_weight": 3, "enabled": True},
            ]
        })
        config_path = tmp_data_root / "sources.yaml"
        config_path.write_text(config_yaml, encoding="utf-8")

        with patch("src.fetch.feedparser.parse", return_value=good_feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, healths = fetch_day(FIXED_DATE, config_path=config_path)

        h = healths[0]
        assert h.items_in == 2
        # items_kept should equal kept items (may be 0 if recency filter dropped them)
        assert h.items_kept >= 0
        assert h.items_kept <= h.items_in


# ---------------------------------------------------------------------------
# TestFiredTrueWithZeroKept — valid state: source responded but nothing new
# ---------------------------------------------------------------------------

class TestFiredTrueWithZeroKept:
    # `test_fired_true_items_kept_zero_is_valid_state` cut: duplicated
    # test_models.py::TestSourceHealth::test_fired_true_with_zero_items_is_ok.
    # The fetch-level test below exercises the actual seam (fetch_day producing
    # the state), which is what we care about.

    def test_fetch_day_produces_fired_true_zero_kept_when_all_old(
        self, tmp_data_root: Path
    ) -> None:
        """Items older than MAX_ITEM_AGE_DAYS are dropped; health.fired stays True."""
        old_pub = datetime.datetime(2020, 1, 1, tzinfo=UTC)
        old_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Old Blog</title>
<link>https://old.example.com/</link>
<item>
  <title>Ancient Post</title>
  <link>https://old.example.com/ancient</link>
  <description>This is very old.</description>
  <pubDate>Wed, 01 Jan 2020 00:00:00 +0000</pubDate>
  <guid>https://old.example.com/ancient</guid>
</item>
</channel></rss>"""
        old_feed = feedparser.parse(old_xml)

        config_yaml = yaml.dump({
            "sources": [
                {"name": "old_blog", "url": "https://old.example.com/feed.xml",
                 "type": "rss", "trust_weight": 3, "enabled": True},
            ]
        })
        config_path = tmp_data_root / "sources.yaml"
        config_path.write_text(config_yaml, encoding="utf-8")

        with patch("src.fetch.feedparser.parse", return_value=old_feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, healths = fetch_day(FIXED_DATE, config_path=config_path)

        h = healths[0]
        assert h.fired is True
        assert h.items_in == 1
        assert h.items_kept == 0
        assert items == []


# ---------------------------------------------------------------------------
# TestAtomicWrites — fetch_day writes items.jsonl and source_health.json
# ---------------------------------------------------------------------------

class TestAtomicWrites:
    def test_items_jsonl_written_to_staging(self, tmp_data_root: Path) -> None:
        feed = _parse_fixture("sample_rss.xml")
        config_yaml = yaml.dump({
            "sources": [
                {"name": "test_blog", "url": "https://example.com/feed.xml",
                 "type": "rss", "trust_weight": 3, "enabled": True},
            ]
        })
        config_path = tmp_data_root / "sources.yaml"
        config_path.write_text(config_yaml, encoding="utf-8")

        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            fetch_day(FIXED_DATE, config_path=config_path)

        staging = tmp_data_root / "staging" / "2026-05-24"
        assert (staging / "items.jsonl").exists()
        assert (staging / "source_health.json").exists()

    def test_source_health_json_is_valid_report(self, tmp_data_root: Path) -> None:
        from src.models import SourceHealthReport

        feed = _parse_fixture("sample_rss.xml")
        config_yaml = yaml.dump({
            "sources": [
                {"name": "test_blog", "url": "https://example.com/feed.xml",
                 "type": "rss", "trust_weight": 3, "enabled": True},
            ]
        })
        config_path = tmp_data_root / "sources.yaml"
        config_path.write_text(config_yaml, encoding="utf-8")

        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            fetch_day(FIXED_DATE, config_path=config_path)

        health_path = tmp_data_root / "staging" / "2026-05-24" / "source_health.json"
        report = SourceHealthReport.model_validate_json(health_path.read_text())
        assert len(report.sources) == 1
        assert report.sources[0].source == "test_blog"

    def test_idempotent_rerun_overwrites_not_appends(
        self, tmp_data_root: Path
    ) -> None:
        """Running fetch_day twice on the same date replaces the JSONL, not appends."""
        feed = _parse_fixture("sample_rss.xml")
        config_yaml = yaml.dump({
            "sources": [
                {"name": "test_blog", "url": "https://example.com/feed.xml",
                 "type": "rss", "trust_weight": 3, "enabled": True},
            ]
        })
        config_path = tmp_data_root / "sources.yaml"
        config_path.write_text(config_yaml, encoding="utf-8")

        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            fetch_day(FIXED_DATE, config_path=config_path)
            fetch_day(FIXED_DATE, config_path=config_path)

        items_path = tmp_data_root / "staging" / "2026-05-24" / "items.jsonl"
        lines = [l for l in items_path.read_text().splitlines() if l.strip()]
        # Should not have doubled up — each line is a unique URL
        urls = [json.loads(l)["url"] for l in lines]
        assert len(urls) == len(set(urls))


# ---------------------------------------------------------------------------
# Bug #70 — BIS future timestamps: far-future published_at is clamped to
# fetched_at, item is kept, WARNING is logged.
# ---------------------------------------------------------------------------

class TestFutureTimestampClamp:
    def test_future_published_at_is_clamped_to_fetched_at(self) -> None:
        """An item with published_at 9 years in the future must be clamped."""
        feed = _parse_fixture("future_timestamp_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = _fetch_rss(_source())

        # Both items are kept (item is not dropped, only clamped)
        assert len(items) == 2
        future_item = next(i for i in items if "payments-paper" in str(i.url))
        assert future_item.published_at == FIXED_FETCH_AT

    def test_future_published_at_clamp_logged_as_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Clamping a future timestamp emits a WARNING with the original value."""
        import logging as _logging
        feed = _parse_fixture("future_timestamp_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT), \
             caplog.at_level(_logging.WARNING, logger="src.fetch"):
            _fetch_rss(_source())

        warning_texts = [r.message for r in caplog.records if r.levelno >= _logging.WARNING]
        assert any("future published_at clamped" in t for t in warning_texts)
        # The original bad timestamp should appear in the log message
        assert any("2035" in t for t in warning_texts)

    def test_normal_recent_item_published_at_unchanged(self) -> None:
        """An item with a sane pubDate must keep its original published_at."""
        feed = _parse_fixture("future_timestamp_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = _fetch_rss(_source())

        normal_item = next(i for i in items if "normal-post" in str(i.url))
        # pubDate is Sun, 24 May 2026 09:00:00 +0000 — must not equal fetched_at
        assert normal_item.published_at != FIXED_FETCH_AT
        assert normal_item.published_at.year == 2026


# ---------------------------------------------------------------------------
# Bug #71 — FCA missing pubdates: when all items share published_at=fetched_at,
# SourceHealth is flagged and items carry extras["freshness_inferred"]="true".
# ---------------------------------------------------------------------------

class TestFreshnessInferredDetection:
    def test_all_same_pubdate_tags_items_with_freshness_inferred(self) -> None:
        """All items sharing published_at=fetched_at get freshness_inferred=true."""
        feed = _parse_fixture("all_same_pubdate_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = _fetch_rss(_source())

        assert len(items) == 3
        for item in items:
            assert item.extras.get("freshness_inferred") == "true", (
                f"Expected freshness_inferred=true on {item.url!r}"
            )

    def test_all_same_pubdate_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The freshness_inferred detection emits a WARNING for the source."""
        import logging as _logging
        feed = _parse_fixture("all_same_pubdate_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT), \
             caplog.at_level(_logging.WARNING, logger="src.fetch"):
            _fetch_rss(_source())

        warning_texts = [r.message for r in caplog.records if r.levelno >= _logging.WARNING]
        assert any("freshness_inferred" in t for t in warning_texts)

    def test_feed_with_real_dates_not_tagged(self) -> None:
        """A feed with genuine per-item dates must NOT get freshness_inferred."""
        feed = _parse_fixture("sample_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = _fetch_rss(_source())

        for item in items:
            assert item.extras.get("freshness_inferred") is None, (
                f"Unexpected freshness_inferred on {item.url!r}"
            )

    def test_single_item_feed_not_tagged(self) -> None:
        """A single-item feed (no pattern to detect) must not be tagged."""
        feed = _parse_fixture("no_date_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = _fetch_rss(_source())

        # Detection requires >= 2 items — single-item feed must not fire
        assert len(items) == 1
        assert items[0].extras.get("freshness_inferred") is None


# ---------------------------------------------------------------------------
# Bug #72 — Lil'Log FAQ ingestion: the /faq/ page is filtered out; the real
# blog post survives.
# ---------------------------------------------------------------------------

class TestJunkPageFilter:
    def test_faq_page_is_dropped(self) -> None:
        """An RSS entry whose URL path is /faq/ must be dropped."""
        feed = _parse_fixture("lil_log_with_faq_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = _fetch_rss(_source())

        urls = [str(i.url) for i in items]
        assert not any("/faq/" in u for u in urls), (
            "FAQ page must not appear in fetched items"
        )

    def test_real_blog_post_survives(self) -> None:
        """The real blog post in the fixture must be kept after the filter."""
        feed = _parse_fixture("lil_log_with_faq_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = _fetch_rss(_source())

        assert len(items) == 1
        assert "posts/2025-05-01-thinking" in str(items[0].url)

    def test_items_in_counts_all_entries_including_junk(self) -> None:
        """items_in reflects raw feed entry count (before junk filter)."""
        feed = _parse_fixture("lil_log_with_faq_rss.xml")
        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            _, health = _fetch_rss(_source())

        # The fixture has 2 entries; items_in counts both
        assert health.items_in == 2


# ---------------------------------------------------------------------------
# Task #87 — per-source max_age_days override in the recency filter.
#
# Four cases tested:
#   1. No override → global MAX_ITEM_AGE_DAYS (2) applies.
#   2. Override max_age_days=14 → keeps items up to 14 days old, drops at 15.
#   3. Override max_age_days=7 → keeps up to 7, drops at 8.
#   4. Mixed batch — each source's items get *its own* cutoff; one source's
#      override must not bleed into another source.
# ---------------------------------------------------------------------------

def _make_item(
    source: str,
    url: str,
    published_at: datetime.datetime,
) -> "Item":
    """Minimal valid Item for age-filter tests."""
    return Item(
        id=_url_hash(url),
        source=source,
        source_type="rss",
        url=url,
        title="Test item",
        published_at=published_at,
        raw_summary="summary",
        fetched_at=FIXED_FETCH_AT,
        trust_weight=3,
    )


def _config_yaml(sources: list[dict]) -> str:
    return yaml.dump({"sources": sources})


class TestPerSourceMaxAgeDays:
    """Per-source max_age_days override — Task #87."""

    def test_no_override_uses_global_default(self, tmp_data_root: Path) -> None:
        """A source with no max_age_days field uses MAX_ITEM_AGE_DAYS (= 2)."""
        # Item published exactly 3 days ago — must be dropped under global=2.
        pub_3d_ago = FIXED_FETCH_AT - datetime.timedelta(days=3)
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Blog</title>
<item>
  <title>Three-day-old post</title>
  <link>https://blog.example.com/post-3d</link>
  <description>Old.</description>
  <pubDate>{pub_3d_ago.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>
  <guid>https://blog.example.com/post-3d</guid>
</item>
</channel></rss>"""
        feed = feedparser.parse(xml)

        config_yaml = _config_yaml([{
            "name": "default_source",
            "url": "https://blog.example.com/feed.xml",
            "type": "rss",
            "trust_weight": 3,
            "enabled": True,
            # no max_age_days field
        }])
        config_path = tmp_data_root / "sources.yaml"
        config_path.write_text(config_yaml)

        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = fetch_day(FIXED_DATE, config_path=config_path)

        assert items == [], "Item 3 days old must be dropped by global 2-day window"

    def test_override_14_keeps_items_within_14_days(self, tmp_data_root: Path) -> None:
        """Source with max_age_days=14 keeps items ≤ 14 days old."""
        pub_10d_ago = FIXED_FETCH_AT - datetime.timedelta(days=10)
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>FS Research</title>
<item>
  <title>Ten-day-old research post</title>
  <link>https://research.example.com/post-10d</link>
  <description>A BIS-style paper.</description>
  <pubDate>{pub_10d_ago.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>
  <guid>https://research.example.com/post-10d</guid>
</item>
</channel></rss>"""
        feed = feedparser.parse(xml)

        config_yaml = _config_yaml([{
            "name": "fs_research_source",
            "url": "https://research.example.com/feed.xml",
            "type": "rss",
            "trust_weight": 3,
            "enabled": True,
            "max_age_days": 14,
        }])
        config_path = tmp_data_root / "sources.yaml"
        config_path.write_text(config_yaml)

        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = fetch_day(FIXED_DATE, config_path=config_path)

        assert len(items) == 1, "Item 10 days old must be kept under max_age_days=14"

    def test_override_14_drops_items_older_than_14_days(self, tmp_data_root: Path) -> None:
        """Source with max_age_days=14 drops items > 14 days old."""
        pub_15d_ago = FIXED_FETCH_AT - datetime.timedelta(days=15)
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>FS Research</title>
<item>
  <title>Fifteen-day-old research post</title>
  <link>https://research.example.com/post-15d</link>
  <description>Too old.</description>
  <pubDate>{pub_15d_ago.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>
  <guid>https://research.example.com/post-15d</guid>
</item>
</channel></rss>"""
        feed = feedparser.parse(xml)

        config_yaml = _config_yaml([{
            "name": "fs_research_source",
            "url": "https://research.example.com/feed.xml",
            "type": "rss",
            "trust_weight": 3,
            "enabled": True,
            "max_age_days": 14,
        }])
        config_path = tmp_data_root / "sources.yaml"
        config_path.write_text(config_yaml)

        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = fetch_day(FIXED_DATE, config_path=config_path)

        assert items == [], "Item 15 days old must be dropped under max_age_days=14"

    def test_override_7_keeps_within_7_drops_at_8(self, tmp_data_root: Path) -> None:
        """Source with max_age_days=7 keeps 5-day-old items and drops 8-day-old ones.

        We use 5 days (comfortably inside the 7-day window) for the "keep"
        case because feedparser's pubDate parsing goes through time.mktime()
        which applies local-timezone rounding; testing at the exact boundary
        is fragile on non-UTC machines.
        """
        pub_5d = FIXED_FETCH_AT - datetime.timedelta(days=5)
        pub_8d = FIXED_FETCH_AT - datetime.timedelta(days=8)

        def make_xml(pub: datetime.datetime, slug: str) -> str:
            return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Regulator</title>
<item>
  <title>Post {slug}</title>
  <link>https://regulator.example.com/{slug}</link>
  <description>Regulatory item.</description>
  <pubDate>{pub.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>
  <guid>https://regulator.example.com/{slug}</guid>
</item>
</channel></rss>"""

        feed_5d = feedparser.parse(make_xml(pub_5d, "post-5d"))
        feed_8d = feedparser.parse(make_xml(pub_8d, "post-8d"))

        config_yaml = _config_yaml([{
            "name": "regulator_source",
            "url": "https://regulator.example.com/feed.xml",
            "type": "rss",
            "trust_weight": 3,
            "enabled": True,
            "max_age_days": 7,
        }])
        config_path = tmp_data_root / "sources.yaml"
        config_path.write_text(config_yaml)

        # 5-day-old item must be kept (inside the 7-day window)
        with patch("src.fetch.feedparser.parse", return_value=feed_5d), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items_5d, _ = fetch_day(FIXED_DATE, config_path=config_path)

        assert len(items_5d) == 1, "Item 5 days old must be kept under max_age_days=7"

        # 8-day-old item must be dropped (outside the 7-day window)
        with patch("src.fetch.feedparser.parse", return_value=feed_8d), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items_8d, _ = fetch_day(FIXED_DATE, config_path=config_path)

        assert items_8d == [], "Item 8 days old must be dropped under max_age_days=7"

    def test_mixed_sources_each_get_own_cutoff(self, tmp_data_root: Path) -> None:
        """In a mixed batch, each source's override applies only to its own items.

        - Source A: max_age_days=14  → its 10-day-old item is KEPT.
        - Source B: no override (global=2)  → its 3-day-old item is DROPPED.
        The 14-day override on source A must not cause source B's item to survive.
        """
        pub_10d = FIXED_FETCH_AT - datetime.timedelta(days=10)
        pub_3d = FIXED_FETCH_AT - datetime.timedelta(days=3)

        xml_a = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>FS Research</title>
<item>
  <title>FS post 10 days old</title>
  <link>https://fs.example.com/post-10d</link>
  <description>Deep research.</description>
  <pubDate>{pub_10d.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>
  <guid>https://fs.example.com/post-10d</guid>
</item>
</channel></rss>"""

        xml_b = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Daily Blog</title>
<item>
  <title>Daily post 3 days old</title>
  <link>https://daily.example.com/post-3d</link>
  <description>News item.</description>
  <pubDate>{pub_3d.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>
  <guid>https://daily.example.com/post-3d</guid>
</item>
</channel></rss>"""

        feed_a = feedparser.parse(xml_a)
        feed_b = feedparser.parse(xml_b)

        call_seq = [feed_a, feed_b]
        call_idx = {"i": 0}

        def side_effect(url: str, **kwargs: object) -> object:
            result = call_seq[call_idx["i"]]
            call_idx["i"] += 1
            return result

        config_yaml = _config_yaml([
            {
                "name": "fs_research_source",
                "url": "https://fs.example.com/feed.xml",
                "type": "rss",
                "trust_weight": 3,
                "enabled": True,
                "max_age_days": 14,
            },
            {
                "name": "daily_source",
                "url": "https://daily.example.com/feed.xml",
                "type": "rss",
                "trust_weight": 3,
                "enabled": True,
                # no max_age_days — uses global default of 2
            },
        ])
        config_path = tmp_data_root / "sources.yaml"
        config_path.write_text(config_yaml)

        with patch("src.fetch.feedparser.parse", side_effect=side_effect), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, healths = fetch_day(FIXED_DATE, config_path=config_path)

        kept_sources = {item.source for item in items}

        assert "fs_research_source" in kept_sources, (
            "fs_research_source's 10-day-old item must survive under max_age_days=14"
        )
        assert "daily_source" not in kept_sources, (
            "daily_source's 3-day-old item must be dropped by global 2-day window; "
            "the 14-day override on fs_research_source must not bleed over"
        )


# ---------------------------------------------------------------------------
# TestPublishedUrlsDedup — cross-issue dedup reads published_urls.txt, not
# items.jsonl.  Three cases:
#   1. URLs in published_urls.txt are filtered; a third URL survives.
#   2. Missing published_urls.txt → no filtering (fresh-repo safe).
#   3. A released items.jsonl whose URLs are absent from published_urls.txt
#      must NOT filter — proves we're reading the right file.
# ---------------------------------------------------------------------------

def _rss_xml_for_urls(urls: list[str]) -> str:
    """Build a minimal RSS feed with one item per URL."""
    items = ""
    for i, url in enumerate(urls):
        items += f"""
  <item>
    <title>Post {i}</title>
    <link>{url}</link>
    <description>Content {i}.</description>
    <pubDate>Sun, 24 May 2026 09:00:00 +0000</pubDate>
    <guid>{url}</guid>
  </item>"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Test Feed</title>{items}
</channel></rss>"""


class TestPublishedUrlsDedup:

    def test_dedup_filters_items_in_published_urls_txt(
        self, tmp_data_root: Path
    ) -> None:
        """Items whose URLs appear in published_urls.txt are dropped; the rest survive."""
        from src import paths as _paths

        url_a = "https://example.com/already-published-1"
        url_b = "https://example.com/already-published-2"
        url_c = "https://example.com/new-item"

        # Write two published URLs
        published_path = _paths.PUBLISHED_URLS_PATH
        published_path.write_text(f"{url_a}\n{url_b}\n", encoding="utf-8")

        feed = feedparser.parse(_rss_xml_for_urls([url_a, url_b, url_c]))
        config_yaml = _config_yaml([{
            "name": "test_source",
            "url": "https://example.com/feed.xml",
            "type": "rss",
            "trust_weight": 3,
            "enabled": True,
        }])
        config_path = tmp_data_root / "sources.yaml"
        config_path.write_text(config_yaml)

        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = fetch_day(FIXED_DATE, config_path=config_path)

        surviving_urls = {str(item.url) for item in items}
        assert url_c in surviving_urls, "New item must survive"
        assert url_a not in surviving_urls, "Published URL A must be filtered"
        assert url_b not in surviving_urls, "Published URL B must be filtered"
        assert len(items) == 1

    def test_dedup_no_published_urls_file_means_no_filtering(
        self, tmp_data_root: Path
    ) -> None:
        """When published_urls.txt does not exist, dedup is a no-op (fresh repo)."""
        from src import paths as _paths

        assert not _paths.PUBLISHED_URLS_PATH.exists(), (
            "Test precondition: published_urls.txt must not exist"
        )

        url_a = "https://example.com/item-one"
        url_b = "https://example.com/item-two"

        feed = feedparser.parse(_rss_xml_for_urls([url_a, url_b]))
        config_yaml = _config_yaml([{
            "name": "test_source",
            "url": "https://example.com/feed.xml",
            "type": "rss",
            "trust_weight": 3,
            "enabled": True,
        }])
        config_path = tmp_data_root / "sources.yaml"
        config_path.write_text(config_yaml)

        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = fetch_day(FIXED_DATE, config_path=config_path)

        assert len(items) == 2, (
            "All items must survive when published_urls.txt does not exist"
        )

    def test_published_urls_dedup_independent_of_items_jsonl(
        self, tmp_data_root: Path
    ) -> None:
        """URLs in a released items.jsonl that are absent from published_urls.txt
        must NOT be filtered — proves we're reading the right file."""
        from src import paths as _paths

        url_in_jsonl = "https://example.com/in-jsonl-not-published"
        url_new = "https://example.com/truly-new"

        # Write a released items.jsonl containing url_in_jsonl
        released_day = FIXED_DATE - datetime.timedelta(days=1)
        released_dir = _paths.RELEASED_ROOT / released_day.isoformat()
        released_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = released_dir / "items.jsonl"
        jsonl_path.write_text(
            json.dumps({"url": url_in_jsonl, "source": "old_source"}) + "\n",
            encoding="utf-8",
        )

        # published_urls.txt is empty (url_in_jsonl was never released to readers)
        _paths.PUBLISHED_URLS_PATH.write_text("", encoding="utf-8")

        feed = feedparser.parse(_rss_xml_for_urls([url_in_jsonl, url_new]))
        config_yaml = _config_yaml([{
            "name": "test_source",
            "url": "https://example.com/feed.xml",
            "type": "rss",
            "trust_weight": 3,
            "enabled": True,
        }])
        config_path = tmp_data_root / "sources.yaml"
        config_path.write_text(config_yaml)

        with patch("src.fetch.feedparser.parse", return_value=feed), \
             patch("src.fetch._utcnow", return_value=FIXED_FETCH_AT):
            items, _ = fetch_day(FIXED_DATE, config_path=config_path)

        surviving_urls = {str(item.url) for item in items}
        assert url_in_jsonl in surviving_urls, (
            "URL present in released items.jsonl but absent from published_urls.txt "
            "must NOT be filtered — we dedup against published, not fetched"
        )
        assert url_new in surviving_urls, "Fresh URL must also survive"
        assert len(items) == 2
