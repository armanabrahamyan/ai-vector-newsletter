"""Canonical path helpers for AI Vector's archive.

See docs/DESIGN.md "Archive: staging vs canonical" for the model.
- Staging path:  data/staging/<date>/
- Canonical path: data/<date>/
- Published URLs index: data/published_urls.txt (canonical only -- written on release)

Modules should import paths from here instead of constructing them inline,
so a future refactor (e.g. relocating data/ to a configurable root) is surgical.

Read rules per DESIGN.md "Archive: staging vs canonical":
  * Engine stages WRITE to staging by default (canonical=False).
  * Cross-time dedup, callback lookback, eval, published_urls.txt all READ
    canonical only (canonical=True) -- staging is invisible to history.
  * The release transition copies staging -> canonical and writes
    issue.json LAST as the commit marker.
"""
from __future__ import annotations

import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Root constants -- one place to change if the archive ever relocates.
# ---------------------------------------------------------------------------

DATA_ROOT = Path("data")
"""Root of the archive (canonical days live directly under this)."""

STAGING_ROOT = DATA_ROOT / "staging"
"""Root of the staging area (work-in-progress days live under this)."""

PUBLISHED_URLS_PATH = DATA_ROOT / "published_urls.txt"
"""Cumulative released-URL exclusion index. Canonical-only; never under staging."""

DOCS_ROOT = Path("docs")
"""GitHub Pages publish surface."""

PREVIEW_DIR = DOCS_ROOT / "preview"
"""Where staging preview HTML lives (`docs/preview/<date>.html`)."""

ARCHIVE_DIR = DOCS_ROOT / "archive"
"""Where the per-issue archive HTML lives (`docs/archive/<date>.html`)."""

DOCS_INDEX = DOCS_ROOT / "index.html"
"""The "latest issue" surface served by GitHub Pages."""


# ---------------------------------------------------------------------------
# Day-directory helpers.
# ---------------------------------------------------------------------------

def staging_dir(date: datetime.date) -> Path:
    """Staging directory for a given date: `data/staging/YYYY-MM-DD/`."""
    return STAGING_ROOT / date.isoformat()


def canonical_dir(date: datetime.date) -> Path:
    """Canonical directory for a given date: `data/YYYY-MM-DD/`."""
    return DATA_ROOT / date.isoformat()


# ---------------------------------------------------------------------------
# Per-file helpers. `canonical=False` -> staging; `canonical=True` -> canonical.
# Callers should pass the keyword explicitly so the read/write intent reads
# clearly at the call site.
# ---------------------------------------------------------------------------

def items_path(date: datetime.date, *, canonical: bool) -> Path:
    """Path to items.jsonl for the given date and archive state."""
    return (canonical_dir(date) if canonical else staging_dir(date)) / "items.jsonl"


def source_health_path(date: datetime.date, *, canonical: bool) -> Path:
    """Path to source_health.json for the given date and archive state."""
    return (canonical_dir(date) if canonical else staging_dir(date)) / "source_health.json"


def clusters_path(date: datetime.date, *, canonical: bool) -> Path:
    """Path to clusters.jsonl for the given date and archive state."""
    return (canonical_dir(date) if canonical else staging_dir(date)) / "clusters.jsonl"


def ranked_path(date: datetime.date, *, canonical: bool) -> Path:
    """Path to ranked.jsonl for the given date and archive state."""
    return (canonical_dir(date) if canonical else staging_dir(date)) / "ranked.jsonl"


def issue_path(date: datetime.date, *, canonical: bool) -> Path:
    """Path to issue.json for the given date and archive state.

    In staging, `Issue.issue_number` is None; in canonical, it is an integer
    assigned at release time. See DESIGN.md "Issue Number Registry".
    """
    return (canonical_dir(date) if canonical else staging_dir(date)) / "issue.json"


def centroids_path(date: datetime.date, *, canonical: bool) -> Path:
    """Path to the embedding centroids sidecar for the given date and state."""
    base = canonical_dir(date) if canonical else staging_dir(date)
    return base / "embeddings" / "centroids.npz"


# ---------------------------------------------------------------------------
# Render-output helpers.
# ---------------------------------------------------------------------------

def preview_html_path(date: datetime.date) -> Path:
    """Where the staging preview HTML lands: `docs/preview/<date>.html`."""
    return PREVIEW_DIR / f"{date.isoformat()}.html"


def archive_html_path(date: datetime.date) -> Path:
    """Where the released archive HTML lands: `docs/archive/<date>.html`."""
    return ARCHIVE_DIR / f"{date.isoformat()}.html"


# ---------------------------------------------------------------------------
# Canonical-archive enumeration -- the source of truth for issue numbering
# and for the unrelease URL-rebuild walk.
# ---------------------------------------------------------------------------

def all_canonical_dates() -> list[datetime.date]:
    """Return every date that has a canonical issue.json (sorted ascending).

    Used by:
      * `render.release_promote` to derive the next `issue_number`.
      * `render.unrelease` to rebuild `data/published_urls.txt` from scratch.

    Skips `data/staging/` entirely; directories whose name does not parse as
    an ISO date are ignored; directories without an `issue.json` are
    excluded (they may be partial-release leftovers awaiting cleanup).
    """
    out: list[datetime.date] = []
    if not DATA_ROOT.exists():
        return out
    for child in sorted(DATA_ROOT.iterdir()):
        if not child.is_dir() or child.name == "staging":
            continue
        try:
            d = datetime.date.fromisoformat(child.name)
        except ValueError:
            continue
        if (child / "issue.json").exists():
            out.append(d)
    return out
