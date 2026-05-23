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

  * ``release_promote(date)``          -- the full 7-step release transition
    per DESIGN.md: idempotency check, validate staging, copy peripheral
    files, write canonical ``issue.json`` LAST (the commit marker), render
    canonical HTML, append URLs to ``data/published_urls.txt``.

  * ``unrelease(date)``                -- reverse a release: delete canonical
    files (issue.json FIRST), then rebuild ``data/published_urls.txt`` from
    the surviving canonical archive. Leaves the issue-number gap intact
    (no renumbering of subsequent issues; see DESIGN.md "Issue Number
    Registry" -> "Gap recovery").

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
import os
import shutil
from pathlib import Path
from typing import Literal

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src import paths
from src.models import Issue

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEMPLATE_NAME = "issue.html.j2"
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
            f"already released: {date}. To re-release, run "
            f"'python -m src.run --unrelease --date {date}' first."
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


# ---------------------------------------------------------------------------
# Jinja2 helpers
# ---------------------------------------------------------------------------

_SECTION_TITLES: dict[str, str] = {
    "leaders": "For leaders",
    "geeks": "For geeks",
    "notable": "On the Radar",
}


def _section_title(name: str) -> str:
    """Map a SectionName to a display title. Falls back to the raw name."""
    return _SECTION_TITLES.get(name, name)


def _build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    env.globals["section_title"] = _section_title
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

    env = _build_env()
    template = env.get_template(TEMPLATE_NAME)
    html = template.render(issue=issue)

    total_stories = len(issue.pulse.stories) + sum(
        len(s.stories) for s in issue.sections
    )
    issue_label = (
        f"#{issue.issue_number}" if issue.issue_number is not None
        else "(staging -- not yet numbered)"
    )

    if mode == "preview":
        out = paths.preview_html_path(date)
        _atomic_write(out, html)
        log.info(
            "rendered preview issue %s to %s (%d stories)",
            issue_label, out, total_stories,
        )
        return out

    # mode == "release": write both index.html and the archive page.
    index_out = paths.DOCS_INDEX
    archive_out = paths.archive_html_path(date)
    _atomic_write(index_out, html)
    _atomic_write(archive_out, html)
    log.info(
        "rendered canonical issue %s to %s + %s (%d stories)",
        issue_label, index_out, archive_out, total_stories,
    )
    return index_out


# ---------------------------------------------------------------------------
# release_promote -- the 7-step release transition per DESIGN.md.
# ---------------------------------------------------------------------------

def release_promote(date: datetime.date) -> Issue:
    """
    Promote ``data/staging/<date>/`` to canonical ``data/<date>/`` and ship.

    Implements the 7-step release transition (DESIGN.md "Archive: staging
    vs canonical -> The release transition") in this exact order:

      1. Idempotency check -- if canonical issue.json already exists, raise
         ``AlreadyReleased``. The orchestrator catches and reports.
      2. Validate staging exists -- otherwise ``NoStagingDraft``.
      3. Read + pydantic-validate the staging ``Issue``. Warn if
         ``issue_number`` was somehow already set in staging (overwrite).
      4. Derive the next ``issue_number`` from
         ``paths.all_canonical_dates()`` -> max + 1 (or 1 if no canonical
         history). Log the derivation.
      5. Copy peripheral files atomically (items.jsonl, source_health.json,
         clusters.jsonl, ranked.jsonl, embeddings/centroids.npz) from
         staging -> canonical.
      6. Write canonical ``issue.json`` LAST (atomic). This is the commit
         marker: its presence == "released."
      7. Render canonical HTML to ``docs/index.html`` + ``docs/archive/...``,
         then append URLs to ``data/published_urls.txt`` (idempotent union).

    Returns the final ``Issue`` (with ``issue_number`` set) so the
    orchestrator can log it.
    """
    # --- Step 1: idempotency check ----------------------------------------
    canonical_issue = paths.issue_path(date, canonical=True)
    if canonical_issue.exists():
        log.info(
            "already released: %s exists -- to re-release, run "
            "--unrelease --date %s first.",
            canonical_issue, date,
        )
        raise AlreadyReleased(date)

    # --- Step 2: validate staging exists ----------------------------------
    staging_issue = paths.issue_path(date, canonical=False)
    if not staging_issue.exists():
        raise NoStagingDraft(date)

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

    # --- Step 4: derive issue_number --------------------------------------
    canonical_dates = paths.all_canonical_dates()
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
                "release: could not read canonical issue.json for %s while "
                "deriving issue_number -- skipping",
                d,
            )
    next_number = (max(existing_numbers) + 1) if existing_numbers else 1
    log.info(
        "release: derived issue_number=%d (max canonical=%s, %d canonical "
        "issues scanned)",
        next_number,
        max(existing_numbers) if existing_numbers else "(none)",
        len(existing_numbers),
    )

    # --- Step 5: copy peripheral files staging -> canonical --------------
    canonical_dir = paths.canonical_dir(date)
    canonical_dir.mkdir(parents=True, exist_ok=True)
    (canonical_dir / "embeddings").mkdir(parents=True, exist_ok=True)

    staging_dir = paths.staging_dir(date)
    for name in _PERIPHERAL_FILES:
        _atomic_copy(staging_dir / name, canonical_dir / name)
    _atomic_copy(
        staging_dir / _PERIPHERAL_EMBEDDINGS,
        canonical_dir / _PERIPHERAL_EMBEDDINGS,
    )

    # --- Step 6: write canonical issue.json LAST (the commit marker) -----
    # Construct a fresh Issue with the assigned number. We use model_copy
    # to keep the original validated shape intact.
    final = staged.model_copy(update={"issue_number": next_number})
    _atomic_write_issue(canonical_issue, final)
    log.info(
        "release: committed canonical %s (issue #%d) -- date %s is now "
        "released.",
        canonical_issue, next_number, date,
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
    canonical_dir = paths.canonical_dir(date)
    for name in reversed(_PERIPHERAL_FILES):
        p = canonical_dir / name
        if p.exists():
            p.unlink()
            log.debug("unrelease: removed %s", p)
    embeddings_file = canonical_dir / _PERIPHERAL_EMBEDDINGS
    if embeddings_file.exists():
        embeddings_file.unlink()
        log.debug("unrelease: removed %s", embeddings_file)

    # --- Step 3: best-effort empty-directory cleanup ----------------------
    embeddings_dir = canonical_dir / "embeddings"
    if embeddings_dir.exists():
        try:
            embeddings_dir.rmdir()
        except OSError:
            log.debug("unrelease: %s not empty, leaving in place", embeddings_dir)
    if canonical_dir.exists():
        try:
            canonical_dir.rmdir()
        except OSError:
            log.debug("unrelease: %s not empty, leaving in place", canonical_dir)

    # --- Step 4: rebuild data/published_urls.txt from canonical history --
    surviving_urls: set[str] = set()
    for d in paths.all_canonical_dates():
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
            issue = release_promote(run_date)
            print(f"released {run_date} as issue #{issue.issue_number}")
        else:
            out = render(run_date, mode=args.mode)
            print(out)
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
