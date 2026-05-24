"""Canonical path helpers for AI Vector's archive.

See docs/DESIGN.md "Archive: staging vs released" for the model.
- Staging path:   data/staging/<date>/
- Released path:  data/released/<date>/
- Published URLs index: data/published_urls.txt (released only -- written on release)

Modules should import paths from here instead of constructing them inline,
so a future refactor (e.g. relocating data/ to a configurable root) is surgical.

Read rules per DESIGN.md "Archive: staging vs released":
  * Engine stages WRITE to staging by default (canonical=False).
  * Cross-time dedup, callback lookback, eval, published_urls.txt all READ
    released only (canonical=True) -- staging is invisible to history.
  * The release transition copies staging -> released and writes
    issue.json LAST as the commit marker.

Note on the ``canonical`` kwarg: kept as an internal API word meaning "the
authoritative / released version". Reads at every callsite as
"canonical=True -> the released copy", which is what we want.
"""
from __future__ import annotations

import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Root constants -- one place to change if the archive ever relocates.
# ---------------------------------------------------------------------------

DATA_ROOT = Path("data")
"""Root of the data tree."""

STAGING_ROOT = DATA_ROOT / "staging"
"""Root of the staging area: work-in-progress days live under this."""

RELEASED_ROOT = DATA_ROOT / "released"
"""Root of the released archive: canonical days live under this."""

PUBLISHED_URLS_PATH = DATA_ROOT / "published_urls.txt"
"""Cumulative released-URL exclusion index. Released-only; never under staging."""

DOCS_ROOT = Path("docs")
"""GitHub Pages publish surface."""

STAGING_HTML_DIR = DOCS_ROOT / "staging"
"""Where staging preview HTML lives (`docs/staging/<date>.html`)."""

RELEASED_HTML_DIR = DOCS_ROOT / "released"
"""Where the per-issue released HTML lives (`docs/released/<date>.html`)."""

DOCS_INDEX = DOCS_ROOT / "index.html"
"""The landing page served by GitHub Pages."""


# ---------------------------------------------------------------------------
# Day-directory helpers.
# ---------------------------------------------------------------------------

def staging_dir(date: datetime.date) -> Path:
    """Staging directory for a given date: `data/staging/YYYY-MM-DD/`."""
    return STAGING_ROOT / date.isoformat()


def released_dir(date: datetime.date) -> Path:
    """Released directory for a given date: `data/released/YYYY-MM-DD/`."""
    return RELEASED_ROOT / date.isoformat()


# ---------------------------------------------------------------------------
# Per-file helpers. `canonical=False` -> staging; `canonical=True` -> released.
# Callers should pass the keyword explicitly so the read/write intent reads
# clearly at the call site.
# ---------------------------------------------------------------------------

def items_path(date: datetime.date, *, canonical: bool) -> Path:
    """Path to items.jsonl for the given date and archive state."""
    return (released_dir(date) if canonical else staging_dir(date)) / "items.jsonl"


def source_health_path(date: datetime.date, *, canonical: bool) -> Path:
    """Path to source_health.json for the given date and archive state."""
    return (released_dir(date) if canonical else staging_dir(date)) / "source_health.json"


def clusters_path(date: datetime.date, *, canonical: bool) -> Path:
    """Path to clusters.jsonl for the given date and archive state."""
    return (released_dir(date) if canonical else staging_dir(date)) / "clusters.jsonl"


def ranked_path(date: datetime.date, *, canonical: bool) -> Path:
    """Path to ranked.jsonl for the given date and archive state."""
    return (released_dir(date) if canonical else staging_dir(date)) / "ranked.jsonl"


def issue_path(date: datetime.date, *, canonical: bool) -> Path:
    """Path to issue.json for the given date and archive state.

    In staging, `Issue.issue_number` is None; in released, it is an integer
    assigned at release time. See DESIGN.md "Issue Number Registry".
    """
    return (released_dir(date) if canonical else staging_dir(date)) / "issue.json"


def centroids_path(date: datetime.date, *, canonical: bool) -> Path:
    """Path to the embedding centroids sidecar for the given date and state."""
    base = released_dir(date) if canonical else staging_dir(date)
    return base / "embeddings" / "centroids.npz"


# ---------------------------------------------------------------------------
# Render-output helpers.
# ---------------------------------------------------------------------------

def staging_html_path(date: datetime.date) -> Path:
    """Where the staging HTML lands: `docs/staging/<date>.html`."""
    return STAGING_HTML_DIR / f"{date.isoformat()}.html"


def released_html_path(date: datetime.date) -> Path:
    """Where the released HTML lands: `docs/released/<date>.html`."""
    return RELEASED_HTML_DIR / f"{date.isoformat()}.html"


# ---------------------------------------------------------------------------
# Released-archive enumeration -- the source of truth for issue numbering
# and for the unrelease URL-rebuild walk.
# ---------------------------------------------------------------------------

def all_released_dates() -> list[datetime.date]:
    """Return every date that has a released issue.json (sorted ascending).

    Used by:
      * `render.release_promote` to derive the next `issue_number`.
      * `render.unrelease` to rebuild `data/published_urls.txt` from scratch.
      * `render._collect_index_entries` to populate the landing page.

    Directories whose name does not parse as an ISO date are ignored;
    directories without an `issue.json` are excluded (they may be
    partial-release leftovers awaiting cleanup).
    """
    out: list[datetime.date] = []
    if not RELEASED_ROOT.exists():
        return out
    for child in sorted(RELEASED_ROOT.iterdir()):
        if not child.is_dir():
            continue
        try:
            d = datetime.date.fromisoformat(child.name)
        except ValueError:
            continue
        if (child / "issue.json").exists():
            out.append(d)
    return out


