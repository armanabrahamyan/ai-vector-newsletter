"""
src/render.py -- Release Engineer. Jinja2 HTML render + the release transition.

Round B (DESIGN.md "Archive: staging vs canonical") -- three top-level surfaces:

  * ``render(date, mode="preview")``   -- reads STAGING ``issue.json``,
    writes ``docs/preview/<date>.html``. Safe; never touches canonical
    state or ``data/published_urls.txt``.

  * ``render(date, mode="release")``   -- reads CANONICAL ``issue.json``,
    writes ``docs/index.html`` and ``docs/archive/<date>.html``. Called
    internally by ``release_promote`` after the canonical issue.json is
    in place.

  * ``release_promote(date, revise=False)`` -- the full 7-step release
    transition per DESIGN.md: idempotency check, validate staging, copy
    peripheral files, write canonical ``issue.json`` LAST (the commit
    marker), render canonical HTML, append URLs to
    ``data/published_urls.txt``. When ``revise=True`` and the date is
    already released, instead of raising ``AlreadyReleased`` the call
    promotes a new revision: ``issue_number`` is preserved (same
    integer); ``revision`` is bumped by 1 (rendered as ``#N.M``).

  * ``unrelease(date)``                -- reverse a release: delete canonical
    files (issue.json FIRST), then rebuild ``data/published_urls.txt`` from
    the surviving canonical archive. Removes the entire date dir, so the
    revision counter implicitly resets on the next first release of that
    date. Leaves the issue-number gap intact (no renumbering of subsequent
    issues; see DESIGN.md "Issue Number Registry" -> "Gap recovery").

Standalone:
    python -m src.render                              # preview today's staging
    python -m src.render --mode preview --date ...    # preview specific date
    python -m src.render --mode release --date ...    # full release transition
    python -m src.render --unrelease --date ...       # reverse a release
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import os
import shutil
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src import paths
from src.models import Issue

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEMPLATE_NAME = "issue.html.j2"
INDEX_TEMPLATE_NAME = "index.html.j2"
TEMPLATE_DIR = Path("templates")

# Peripheral files copied from staging -> canonical during release_promote
# (and removed during unrelease, in reverse). Order is for log readability
# only; the canonical ``issue.json`` is the commit marker and is handled
# separately (written LAST during release; deleted FIRST during unrelease).
_PERIPHERAL_FILES: tuple[str, ...] = (
    "items.jsonl",
    "source_health.json",
    "clusters.jsonl",
    "ranked.jsonl",
)
_PERIPHERAL_EMBEDDINGS: str = "embeddings/centroids.npz"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions -- the orchestrator (run.py) catches these to format
# clear messages and exit non-zero without printing a traceback.
# ---------------------------------------------------------------------------

class ReleaseError(Exception):
    """Base class for release / unrelease errors."""


class AlreadyReleased(ReleaseError):
    """Raised when ``release_promote`` is asked to promote a date that is
    already canonical. The 7-step idempotency check is the gate."""

    def __init__(self, date: datetime.date) -> None:
        super().__init__(
            f"already released: {date}. To ship a corrected revision, "
            f"run 'aiv release --revise --date {date}'. To start over, "
            f"run 'aiv unrelease --date {date}' first."
        )
        self.date = date


class NoStagingDraft(ReleaseError):
    """Raised when ``release_promote`` cannot find a staging draft to
    promote. The expected workflow is engine run -> review preview -> release."""

    def __init__(self, date: datetime.date) -> None:
        super().__init__(
            f"no staging draft for {date}: expected "
            f"{paths.issue_path(date, canonical=False)} (run the engine first)."
        )
        self.date = date


class NotReleased(ReleaseError):
    """Raised when ``unrelease`` is asked to reverse a date that was never
    released (no canonical ``issue.json``)."""

    def __init__(self, date: datetime.date) -> None:
        super().__init__(
            f"not released: {date}: no canonical "
            f"{paths.issue_path(date, canonical=True)} to remove."
        )
        self.date = date


class IncompleteStaging(ReleaseError):
    """Raised when ``release_promote`` finds a staging draft whose required
    peripheral file is missing. Canonical archive is complete-by-construction;
    refuse to release a partial archive. Empty-but-existing files are OK."""

    def __init__(self, date: datetime.date, missing_path: Path) -> None:
        super().__init__(
            f"incomplete staging for {date}: missing {missing_path}. "
            f"Re-run the pipeline (or the specific stage) before releasing."
        )
        self.date = date
        self.missing_path = missing_path


class StagingIntegrityFailure(ReleaseError):
    """Raised when ``release_promote`` runs ``check_integrity()`` against the
    staging archive and one or more assertions fail. The release is refused
    so that we never ship an issue with broken pipeline health (e.g. too few
    hands_on stories, source fire rate below 0.80, missing pulse, or a
    score-≥35 cluster wrongly tiered as ``cut``).

    The full list of human-readable failure strings is exposed as
    ``.failures`` so the orchestrator (run.py) can render them one per line.
    Pass ``--force`` to ``aiv release`` (or ``force=True`` to ``release_promote``)
    to bypass the gate; the operator's intent is recorded in the warning
    log line for audit."""

    def __init__(self, date: datetime.date, failures: list[str]) -> None:
        joined = "\n  - ".join(failures) if failures else "(none reported)"
        super().__init__(
            f"staging integrity check FAILED for {date}: refusing to release."
            f"\n  - {joined}"
            f"\nRe-run the pipeline (or the specific stage) to fix the staging "
            f"draft, or pass --force to bypass (logged as a WARNING for audit)."
        )
        self.date = date
        self.failures = list(failures)


# ---------------------------------------------------------------------------
# Jinja2 helpers
# ---------------------------------------------------------------------------

_SECTION_TITLES: dict[str, str] = {
    "big_picture": "The Big Picture",
    "hands_on": "Hands-On",
    "on_the_radar": "On the Radar",
}


def _section_title(name: str) -> str:
    """Map a SectionName to a display title. Falls back to the raw name."""
    return _SECTION_TITLES.get(name, name)


_SYDNEY_TZ = ZoneInfo("Australia/Sydney")


def _aest(dt: datetime.datetime) -> str:
    """Format a (timezone-aware) datetime in Sydney local time, with the
    correct AEST/AEDT abbreviation for the date. Falls back to UTC label
    if the datetime is naive."""
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    local = dt.astimezone(_SYDNEY_TZ)
    return local.strftime("%Y-%m-%d %H:%M ") + local.tzname()


def _source_label(url) -> str:
    """Extract a clean source label from a URL: hostname with the leading
    `www.` stripped. Falls back to the raw URL if parsing fails."""
    s = str(url)
    try:
        host = urlparse(s).hostname or s
    except Exception:  # noqa: BLE001
        return s
    if host.startswith("www."):
        host = host[4:]
    return host


def _read_minutes(issue: Issue) -> int:
    """Rough read-time estimate: total summary word count / 200 wpm,
    rounded up. Minimum 1 minute. Used in the masthead meta line."""
    words = 0
    for block in issue.pulse.stories:
        words += len(block.summary.split())
    for section in issue.sections:
        for block in section.stories:
            words += len(block.summary.split())
    return max(1, math.ceil(words / 200))


def _build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    env.globals["section_title"] = _section_title
    env.filters["aest"] = _aest
    env.filters["source_label"] = _source_label
    return env


# ---------------------------------------------------------------------------
# Atomic write helpers
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via .tmp + fsync + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _atomic_write_lines(path: Path, lines: list[str]) -> None:
    """Write a list of lines (with LF terminator) atomically."""
    content = "".join(line + "\n" for line in lines)
    _atomic_write(path, content)


def _atomic_copy(src: Path, dst: Path) -> None:
    """Copy ``src`` to ``dst`` via .tmp + fsync + rename. Creates parent
    directories as needed. Raises ``IncompleteStaging`` if ``src`` does
    not exist -- canonical archive is complete-by-construction; refuse
    to release a partial archive. Empty-but-existing files are OK."""
    if not src.exists():
        # Derive the date from the canonical destination path (data/YYYY-MM-DD/...).
        try:
            date = datetime.date.fromisoformat(dst.parent.name)
        except ValueError:
            # If we're copying into an unexpected layout (e.g. embeddings/ subdir),
            # walk up one more level.
            date = datetime.date.fromisoformat(dst.parent.parent.name)
        raise IncompleteStaging(date, src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    try:
        # shutil.copyfile preserves byte content; we then fsync + rename
        # to commit atomically.
        shutil.copyfile(src, tmp)
        with tmp.open("rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, dst)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _atomic_write_issue(path: Path, issue: Issue) -> None:
    """Atomic write of an ``Issue`` as pretty JSON. Mirrors the writer in
    ``summarise.py``; lives here because the release path is the only
    writer for canonical issue.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(issue.model_dump_json())
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------

def _collect_urls(issue: Issue) -> list[str]:
    """
    Return every source URL referenced in the issue, deduplicated,
    preserving first-seen order.
    """
    seen: set[str] = set()
    result: list[str] = []

    def _add(url_obj: object) -> None:
        url = str(url_obj)
        if url not in seen:
            seen.add(url)
            result.append(url)

    for story in issue.pulse.stories:
        for url in story.source_urls:
            _add(url)

    for section in issue.sections:
        for story in section.stories:
            for url in story.source_urls:
                _add(url)

    return result


def _load_published_urls() -> set[str]:
    """Read ``data/published_urls.txt`` into a set. Missing file -> empty set."""
    if not paths.PUBLISHED_URLS_PATH.exists():
        return set()
    out: set[str] = set()
    with paths.PUBLISHED_URLS_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped:
                out.add(stripped)
    return out


# ---------------------------------------------------------------------------
# Landing-page index -- the docs/index.html that lists every released issue.
# ---------------------------------------------------------------------------


def _format_display_number(issue_number: object, revision: object) -> str:
    """Render `#N` or `#N.M` for a released issue. Mirrors
    ``Issue.display_number`` for raw-payload callers (the landing-page
    builder reads JSON directly to stay tolerant of legacy shapes).
    `revision` defaults to 0 for archives written before schema v5."""
    if not isinstance(issue_number, int):
        return ""
    rev = revision if isinstance(revision, int) else 0
    if rev <= 0:
        return f"{issue_number}"
    return f"{issue_number}.{rev}"


def _collect_index_entries() -> list[dict]:
    """Walk every canonical issue.json and pull the fields the landing page
    needs: date, issue_number, revision, display_number, pulse headline.
    Returns newest-first, with same-date revisions (impossible by
    construction today -- one canonical issue.json per date -- but the
    sort key is defensive for future schema moves) ordered
    revision-descending within a date. Tolerant of unreadable / partial
    files (skipped with a warning)."""
    entries: list[dict] = []
    for d in paths.all_released_dates():
        issue_path = paths.issue_path(d, canonical=True)
        try:
            payload = json.loads(issue_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "index: skipping %s during landing-page build (%s)",
                issue_path, exc,
            )
            continue
        pulse_stories = (payload.get("pulse") or {}).get("stories") or []
        headline = ""
        if pulse_stories and isinstance(pulse_stories[0], dict):
            headline = (pulse_stories[0].get("headline") or "").strip()
        # `revision` is absent from pre-v5 archive issues; treat as 0.
        revision = payload.get("revision", 0)
        issue_number = payload.get("issue_number")
        entries.append({
            "date": d,
            "issue_number": issue_number,
            "revision": revision,
            "display_number": _format_display_number(issue_number, revision),
            "pulse_headline": headline,
        })
    # Date-descending, then revision-descending within a date.
    entries.sort(key=lambda e: (e["date"], e["revision"]), reverse=True)
    return entries


def _latest_kicker_label(d: datetime.date) -> str:
    """The chip text shown next to "Latest issue" in the hero block.
    "Today" / "Yesterday" when applicable, otherwise the issue's weekday."""
    today = datetime.date.today()
    if d == today:
        return "Today"
    if d == today - datetime.timedelta(days=1):
        return "Yesterday"
    return d.strftime("%a %d %b")


def _render_index_landing() -> Path:
    """Render docs/index.html as the landing page: latest issue hero
    block + monthly-grouped archive list below. Writes atomically.
    Called from `render(mode='release')` and from `unrelease()` so the
    landing always reflects the surviving canonical archive.

    Template contract:
      latest          -- newest entry dict (date, issue_number, pulse_headline), or None
      latest_kicker   -- chip label string ("Today", "Yesterday", "Wed 22 May")
      archive_data    -- JSON-ready list of past entries (entries[1:]) shaped
                         as {date, num, headline, href} for the embedded
                         search/accordion script. May be empty.
      generated_at    -- UTC datetime, formatted by the `aest` filter.
    """
    env = _build_env()
    template = env.get_template(INDEX_TEMPLATE_NAME)
    entries = _collect_index_entries()

    latest = entries[0] if entries else None
    latest_kicker = _latest_kicker_label(latest["date"]) if latest else ""
    archive_data = [
        {
            "date": e["date"].isoformat(),
            "num": e["issue_number"],
            "num_display": e["display_number"],
            "headline": e["pulse_headline"],
            "href": f"released/{e['date'].isoformat()}.html",
        }
        for e in entries[1:]
    ]

    html = template.render(
        latest=latest,
        latest_kicker=latest_kicker,
        archive_data=archive_data,
        generated_at=datetime.datetime.now(datetime.timezone.utc),
    )
    out = paths.DOCS_INDEX
    _atomic_write(out, html)
    return out


# ---------------------------------------------------------------------------
# render() -- the Jinja2 entry point, used by both preview and release flows.
# ---------------------------------------------------------------------------

def render(
    date: datetime.date | None = None,
    *,
    mode: Literal["preview", "release"] = "preview",
) -> Path:
    """
    Render the Issue for ``date`` to HTML.

    Modes:
      * ``"preview"`` (default) -- reads ``data/staging/<date>/issue.json``;
        writes ``docs/preview/<date>.html``. Safe; idempotent; never touches
        canonical state or ``data/published_urls.txt``. Returns the preview
        path.
      * ``"release"`` -- reads ``data/<date>/issue.json`` (canonical);
        writes ``docs/index.html`` AND ``docs/archive/<date>.html``. Called
        internally by ``release_promote`` after canonical issue.json is in
        place. Returns the docs/index.html path.

    Raises ``FileNotFoundError`` if the expected issue.json is missing
    (caller's responsibility to handle).
    """
    if date is None:
        date = datetime.date.today()

    canonical = (mode == "release")
    issue_in = paths.issue_path(date, canonical=canonical)
    if not issue_in.exists():
        raise FileNotFoundError(
            f"render({mode=}): issue.json not found at {issue_in}"
        )

    issue = Issue.model_validate_json(issue_in.read_text(encoding="utf-8"))

    total_stories = len(issue.pulse.stories) + sum(
        len(s.stories) for s in issue.sections
    )

    env = _build_env()
    template = env.get_template(TEMPLATE_NAME)
    html = template.render(issue=issue, read_minutes=_read_minutes(issue))
    issue_label = (
        f"#{issue.display_number}" if issue.display_number is not None
        else "(staging -- not yet numbered)"
    )

    if mode == "preview":
        out = paths.staging_html_path(date)
        _atomic_write(out, html)
        log.info(
            "rendered preview issue %s to %s (%d stories)",
            issue_label, out, total_stories,
        )
        return out

    # mode == "release": write the per-issue archive page, then refresh
    # the landing index (which now lists all canonical issues; the full
    # issue HTML lives only at its dated permalink).
    archive_out = paths.released_html_path(date)
    _atomic_write(archive_out, html)
    index_out = _render_index_landing()
    log.info(
        "rendered canonical issue %s to %s (%d stories); refreshed %s",
        issue_label, archive_out, total_stories, index_out,
    )
    return archive_out


# ---------------------------------------------------------------------------
# release_promote -- the 7-step release transition per DESIGN.md.
# ---------------------------------------------------------------------------

def release_promote(
    date: datetime.date,
    *,
    revise: bool = False,
    force: bool = False,
) -> Issue:
    """
    Promote ``data/staging/<date>/`` to canonical ``data/released/<date>/``
    and ship.

    Implements the 7-step release transition (DESIGN.md "Archive: staging
    vs canonical -> The release transition") in this exact order:

      1. Idempotency check -- if canonical issue.json already exists AND
         ``revise=False``, raise ``AlreadyReleased``. If ``revise=True``
         and a canonical issue.json exists, this is a *same-date
         re-release*: preserve the original ``issue_number``, bump
         ``revision`` by 1, and overwrite the canonical files.
      2. Validate staging exists -- otherwise ``NoStagingDraft``.
      2b. **Staging integrity gate** -- call
         ``evals.run_evals.check_integrity(date, staging=True)``. On
         failure, raise ``StagingIntegrityFailure`` with the full list
         of failed assertions so the caller can render them. The gate is
         bypassed when ``force=True``; the bypassed assertions are
         emitted at WARNING level for audit (an operator who knows
         better must be able to justify the override after the fact).
      3. Read + pydantic-validate the staging ``Issue``. Warn if
         ``issue_number`` was somehow already set in staging (overwrite).
      4. Derive ``issue_number`` + ``revision``:
           * First release (no prior canonical for this date): scan
             ``paths.all_released_dates()`` -> ``max(issue_number) + 1``
             (or 1 if no canonical history). ``revision = 0``.
           * Revision (``revise=True`` and canonical exists): read the
             existing canonical issue.json, keep its ``issue_number``,
             and bump ``revision`` by 1.
         Log the derivation.
      5. Copy peripheral files atomically (items.jsonl, source_health.json,
         clusters.jsonl, ranked.jsonl, embeddings/centroids.npz) from
         staging -> canonical. On a revision, these overwrite in place.
      6. Write canonical ``issue.json`` LAST (atomic). This is the commit
         marker: its presence == "released." On a revision, the file is
         replaced (single canonical issue.json per date -- git holds
         prior content).
      7. Render canonical HTML to ``docs/index.html`` + ``docs/archive/...``,
         then append URLs to ``data/published_urls.txt`` (idempotent union;
         revisions usually re-use the same URL set, so the file rarely
         grows).

    Returns the final ``Issue`` (with ``issue_number`` and ``revision``
    set) so the orchestrator can log it.
    """
    # --- Step 1: idempotency check / revision detection ------------------
    canonical_issue = paths.issue_path(date, canonical=True)
    existing_canonical: dict | None = None
    if canonical_issue.exists():
        if not revise:
            log.info(
                "already released: %s exists -- to ship a corrected "
                "revision, run --revise; to start over, run "
                "--unrelease --date %s first.",
                canonical_issue, date,
            )
            raise AlreadyReleased(date)
        # Revision path: read the existing canonical so we can preserve
        # issue_number and bump revision.
        try:
            existing_canonical = json.loads(
                canonical_issue.read_text(encoding="utf-8")
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "release --revise: cannot read existing canonical %s "
                "(%s); refusing to bump revision over an unreadable file.",
                canonical_issue, exc,
            )
            raise

    # --- Step 2: validate staging exists ----------------------------------
    staging_issue = paths.issue_path(date, canonical=False)
    if not staging_issue.exists():
        raise NoStagingDraft(date)

    # --- Step 2b: staging integrity gate ---------------------------------
    # The eval-engineer-owned ``check_integrity()`` asserts source fire
    # rate >= 0.80, pulse >= 1, hands_on >= 3, no score-≥35 cluster tiered
    # as ``cut``, and full schema + referential integrity. A failing
    # staging draft is refused unless the operator explicitly opts in via
    # ``force=True`` (audited with a WARNING log line).
    #
    # Import is lazy so the standalone ``python -m src.render`` entry
    # point and unit tests that don't exercise the gate don't pay the
    # eval-module import cost. ``evals/`` is not a package installed by
    # pyproject (packages are scoped to ``src*``); bootstrap the repo
    # root onto sys.path the same way run.py's eval command does.
    import sys as _sys
    _repo_root = Path(__file__).resolve().parent.parent
    if str(_repo_root) not in _sys.path:
        _sys.path.insert(0, str(_repo_root))
    from evals.run_evals import check_integrity as _check_integrity

    integrity_failures, integrity_ok = _check_integrity(date, staging=True)
    if not integrity_ok:
        if force:
            log.warning(
                "release --force: BYPASSING staging integrity gate for %s "
                "(%d failing assertion(s)). Audit trail:",
                date, len(integrity_failures),
            )
            for failure in integrity_failures:
                log.warning("  - %s", failure)
        else:
            raise StagingIntegrityFailure(date, integrity_failures)

    # --- Step 3: read + validate staging issue ----------------------------
    staged = Issue.model_validate_json(
        staging_issue.read_text(encoding="utf-8")
    )
    if staged.issue_number is not None:
        log.warning(
            "release: staging issue.json for %s carried issue_number=%d; "
            "release is the authority -- overwriting.",
            date, staged.issue_number,
        )

    # --- Step 4: derive issue_number + revision --------------------------
    if existing_canonical is not None:
        # Revision: preserve issue_number, bump revision.
        prior_number = existing_canonical.get("issue_number")
        if not isinstance(prior_number, int) or prior_number < 1:
            log.error(
                "release --revise: existing canonical %s has invalid "
                "issue_number=%r; refusing to revise.",
                canonical_issue, prior_number,
            )
            raise ValueError(
                f"cannot revise {date}: existing canonical issue.json has "
                f"invalid issue_number={prior_number!r}"
            )
        prior_revision = existing_canonical.get("revision", 0)
        if not isinstance(prior_revision, int) or prior_revision < 0:
            prior_revision = 0
        next_number = prior_number
        next_revision = prior_revision + 1
        log.info(
            "release --revise: same-date re-release for %s -- preserving "
            "issue_number=%d, bumping revision %d -> %d (display: #%d.%d)",
            date, next_number, prior_revision, next_revision,
            next_number, next_revision,
        )
    else:
        # First release of this date: derive a fresh integer issue_number.
        canonical_dates = paths.all_released_dates()
        existing_numbers: list[int] = []
        for d in canonical_dates:
            try:
                payload = json.loads(
                    paths.issue_path(d, canonical=True).read_text(encoding="utf-8")
                )
                n = payload.get("issue_number")
                if isinstance(n, int) and n >= 1:
                    existing_numbers.append(n)
            except Exception:  # noqa: BLE001
                log.warning(
                    "release: could not read canonical issue.json for %s "
                    "while deriving issue_number -- skipping",
                    d,
                )
        next_number = (max(existing_numbers) + 1) if existing_numbers else 1
        next_revision = 0
        log.info(
            "release: derived issue_number=%d (max canonical=%s, %d "
            "canonical issues scanned)",
            next_number,
            max(existing_numbers) if existing_numbers else "(none)",
            len(existing_numbers),
        )

    # --- Step 5: copy peripheral files staging -> canonical --------------
    released_dir = paths.released_dir(date)
    released_dir.mkdir(parents=True, exist_ok=True)
    (released_dir / "embeddings").mkdir(parents=True, exist_ok=True)

    staging_dir = paths.staging_dir(date)
    for name in _PERIPHERAL_FILES:
        _atomic_copy(staging_dir / name, released_dir / name)
    _atomic_copy(
        staging_dir / _PERIPHERAL_EMBEDDINGS,
        released_dir / _PERIPHERAL_EMBEDDINGS,
    )

    # --- Step 6: write canonical issue.json LAST (the commit marker) -----
    # Construct a fresh Issue with the assigned number + revision. We use
    # model_copy to keep the original validated shape intact.
    final = staged.model_copy(update={
        "issue_number": next_number,
        "revision": next_revision,
    })
    _atomic_write_issue(canonical_issue, final)
    log.info(
        "release: committed canonical %s (issue #%s) -- date %s is now "
        "released.",
        canonical_issue, final.display_number, date,
    )

    # --- Step 7: render canonical + append URLs --------------------------
    render(date, mode="release")

    existing = _load_published_urls()
    new_urls = [u for u in _collect_urls(final) if u not in existing]
    if new_urls or not paths.PUBLISHED_URLS_PATH.exists():
        all_urls = sorted(existing | set(new_urls))
        paths.PUBLISHED_URLS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_lines(paths.PUBLISHED_URLS_PATH, all_urls)
    log.info(
        "release: appended %d new URLs to %s (total now %d)",
        len(new_urls), paths.PUBLISHED_URLS_PATH, len(existing) + len(new_urls),
    )

    return final


# ---------------------------------------------------------------------------
# unrelease -- the reverse of release_promote.
# ---------------------------------------------------------------------------

def unrelease(date: datetime.date) -> int:
    """
    Reverse a release for ``date``. Returns the count of URLs removed from
    ``data/published_urls.txt`` (informational).

    Steps:
      1. If canonical issue.json does not exist -> ``NotReleased``.
      2. Delete canonical files for the date, ``issue.json`` FIRST (so the
         date becomes observably "not released" before peripheral cleanup;
         protects readers from a half-deleted state where issue.json is
         gone but peripheral files remain).
      3. Try to remove the now-empty ``embeddings/`` and date directories;
         if other files exist (e.g. user-added notes), leave them in place.
      4. Rebuild ``data/published_urls.txt`` from scratch by walking every
         REMAINING canonical issue.json, extracting source_urls from
         ``pulse.stories[*]`` + ``sections[*].stories[*]``, dedup, sort,
         atomic write. Staging is not consulted.
      5. Log: number of URLs removed, issue-number gap preserved.

    The issue-number gap is intentional -- per DESIGN.md "Issue Number
    Registry -> Gap recovery", we do NOT renumber subsequent issues.
    External references ("see issue #N") must keep pointing at the same
    content.
    """
    canonical_issue = paths.issue_path(date, canonical=True)
    if not canonical_issue.exists():
        raise NotReleased(date)

    # Capture URL count before mutation (for the informational return value).
    before = _load_published_urls()

    # --- Step 2: delete issue.json FIRST ----------------------------------
    canonical_issue.unlink()
    log.info("unrelease: removed canonical commit marker %s", canonical_issue)

    # --- ...then peripheral files in reverse listing order ----------------
    released_dir = paths.released_dir(date)
    for name in reversed(_PERIPHERAL_FILES):
        p = released_dir / name
        if p.exists():
            p.unlink()
            log.debug("unrelease: removed %s", p)
    embeddings_file = released_dir / _PERIPHERAL_EMBEDDINGS
    if embeddings_file.exists():
        embeddings_file.unlink()
        log.debug("unrelease: removed %s", embeddings_file)

    # --- Step 3: best-effort empty-directory cleanup ----------------------
    embeddings_dir = released_dir / "embeddings"
    if embeddings_dir.exists():
        try:
            embeddings_dir.rmdir()
        except OSError:
            log.debug("unrelease: %s not empty, leaving in place", embeddings_dir)
    if released_dir.exists():
        try:
            released_dir.rmdir()
        except OSError:
            log.debug("unrelease: %s not empty, leaving in place", released_dir)

    # --- Step 4: rebuild data/published_urls.txt from canonical history --
    surviving_urls: set[str] = set()
    for d in paths.all_released_dates():
        try:
            payload = json.loads(
                paths.issue_path(d, canonical=True).read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001
            log.warning(
                "unrelease: could not read %s during rebuild -- skipping",
                paths.issue_path(d, canonical=True),
            )
            continue
        for block in _iter_issue_blocks(payload):
            for url in block.get("source_urls") or []:
                if isinstance(url, str) and url.strip():
                    surviving_urls.add(url.strip())

    paths.PUBLISHED_URLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_lines(paths.PUBLISHED_URLS_PATH, sorted(surviving_urls))

    # --- Step 5: clean up the published HTML surface ---------------------
    archive_html = paths.released_html_path(date)
    if archive_html.exists():
        archive_html.unlink()
        log.debug("unrelease: removed %s", archive_html)
    index_out = _render_index_landing()
    log.debug("unrelease: refreshed landing index %s", index_out)

    removed = len(before - surviving_urls)
    log.info(
        "unreleased date %s -- removed %d URLs from %s | issue-number gap "
        "preserved",
        date, removed, paths.PUBLISHED_URLS_PATH,
    )
    return removed


def _iter_issue_blocks(payload: dict) -> "list[dict]":
    """Yield every SummaryBlock-shaped dict in an issue payload (pulse +
    sections). Defensive against partial / legacy archives."""
    out: list[dict] = []
    pulse = payload.get("pulse") or {}
    for block in (pulse.get("stories") or []):
        if isinstance(block, dict):
            out.append(block)
    for section in (payload.get("sections") or []):
        if not isinstance(section, dict):
            continue
        for block in (section.get("stories") or []):
            if isinstance(block, dict):
                out.append(block)
    return out


# ---------------------------------------------------------------------------
# Standalone __main__
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Render / release / unrelease an AI Vector issue.",
    )
    parser.add_argument(
        "--mode",
        choices=("preview", "release"),
        default="preview",
        help="Render mode (preview reads staging; release reads canonical "
             "and writes docs/index.html + archive).",
    )
    parser.add_argument(
        "--unrelease",
        action="store_true",
        default=False,
        help="Reverse a release for the given date (requires --date).",
    )
    parser.add_argument(
        "--promote",
        action="store_true",
        default=False,
        help="Run the full release_promote() transition for the given date.",
    )
    parser.add_argument(
        "--revise",
        action="store_true",
        default=False,
        help="With --promote, allow re-releasing an already-released date: "
             "preserves issue_number, bumps revision (#N -> #N.1 -> #N.2).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="With --promote, bypass the staging integrity gate even if "
             "check_integrity() reports failures. Each bypassed assertion is "
             "logged at WARNING for audit.",
    )
    parser.add_argument(
        "--date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Issue date (default: today).",
    )
    args = parser.parse_args()

    run_date: datetime.date
    if args.date:
        run_date = datetime.date.fromisoformat(args.date)
    else:
        run_date = datetime.date.today()

    try:
        if args.unrelease:
            removed = unrelease(run_date)
            print(f"unreleased {run_date} ({removed} URLs removed)")
        elif args.promote:
            issue = release_promote(
                run_date, revise=args.revise, force=args.force,
            )
            print(f"released {run_date} as issue #{issue.display_number}")
        else:
            out = render(run_date, mode=args.mode)
            print(out)
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
