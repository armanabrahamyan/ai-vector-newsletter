"""Unit tests for src/paths.py — archive path resolution + enumeration.

The path helpers are the contract between every pipeline stage and the
filesystem. They encode the staging-vs-released model: every read site
declares its intent (`canonical=True` for released, `False` for staging),
so a future relocation of the archive root is surgical.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from src import paths


D = _dt.date(2026, 5, 24)


# ---------------------------------------------------------------------------
# Day-directory helpers.
# ---------------------------------------------------------------------------

class TestDayDirs:
    def test_staging_dir_uses_iso_date(self, tmp_data_root: Path) -> None:
        assert paths.staging_dir(D) == tmp_data_root / "staging" / "2026-05-24"

    def test_released_dir_uses_iso_date(self, tmp_data_root: Path) -> None:
        assert paths.released_dir(D) == tmp_data_root / "released" / "2026-05-24"

    def test_staging_and_released_are_distinct(self, tmp_data_root: Path) -> None:
        assert paths.staging_dir(D) != paths.released_dir(D)


# ---------------------------------------------------------------------------
# Per-file helpers — verify the canonical kwarg routes to the right tree.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("helper,filename", [
    (paths.items_path,        "items.jsonl"),
    (paths.source_health_path, "source_health.json"),
    (paths.clusters_path,     "clusters.jsonl"),
    (paths.ranked_path,       "ranked.jsonl"),
    (paths.issue_path,        "issue.json"),
])
class TestPerFileHelpers:
    def test_staging_routes_to_staging_dir(
        self, helper, filename, tmp_data_root: Path
    ) -> None:
        assert helper(D, canonical=False) == paths.staging_dir(D) / filename

    def test_canonical_routes_to_released_dir(
        self, helper, filename, tmp_data_root: Path
    ) -> None:
        assert helper(D, canonical=True) == paths.released_dir(D) / filename
    # `test_filename_is_stable` cut: redundant with the two equality checks
    # above (both already pin the filename via `paths.staging_dir(D) / filename`).


class TestCentroidsPath:
    def test_staging_centroids_under_embeddings_subdir(self, tmp_data_root: Path) -> None:
        p = paths.centroids_path(D, canonical=False)
        assert p == paths.staging_dir(D) / "embeddings" / "centroids.npz"

    def test_released_centroids_under_embeddings_subdir(self, tmp_data_root: Path) -> None:
        p = paths.centroids_path(D, canonical=True)
        assert p == paths.released_dir(D) / "embeddings" / "centroids.npz"


# ---------------------------------------------------------------------------
# Render-output helpers.
# ---------------------------------------------------------------------------

class TestRenderPaths:
    def test_staging_html_path(self) -> None:
        assert paths.staging_html_path(D) == paths.STAGING_HTML_DIR / "2026-05-24.html"

    def test_released_html_path(self) -> None:
        assert paths.released_html_path(D) == paths.RELEASED_HTML_DIR / "2026-05-24.html"

    def test_html_paths_are_distinct(self) -> None:
        assert paths.staging_html_path(D) != paths.released_html_path(D)


# ---------------------------------------------------------------------------
# all_released_dates — used by release_promote (issue numbering) and
# unrelease (URL rebuild).
# ---------------------------------------------------------------------------

class TestAllReleasedDates:
    def test_empty_when_released_root_missing(self, tmp_data_root: Path) -> None:
        # tmp_data_root creates data/ but not data/released/.
        assert paths.all_released_dates() == []

    def test_returns_only_dirs_with_issue_json(self, tmp_data_root: Path) -> None:
        released = tmp_data_root / "released"
        released.mkdir()
        # Two dates: one has issue.json, one doesn't (partial-release leftover).
        good = released / "2026-05-23"
        good.mkdir()
        (good / "issue.json").write_text("{}")
        partial = released / "2026-05-24"
        partial.mkdir()  # no issue.json
        result = paths.all_released_dates()
        assert result == [_dt.date(2026, 5, 23)]

    def test_returns_sorted_ascending(self, tmp_data_root: Path) -> None:
        released = tmp_data_root / "released"
        released.mkdir()
        for d in ("2026-05-23", "2026-05-21", "2026-05-22"):
            sub = released / d
            sub.mkdir()
            (sub / "issue.json").write_text("{}")
        result = paths.all_released_dates()
        assert result == [
            _dt.date(2026, 5, 21),
            _dt.date(2026, 5, 22),
            _dt.date(2026, 5, 23),
        ]

    def test_ignores_non_iso_dir_names(self, tmp_data_root: Path) -> None:
        released = tmp_data_root / "released"
        released.mkdir()
        garbage = released / "not-a-date"
        garbage.mkdir()
        (garbage / "issue.json").write_text("{}")
        assert paths.all_released_dates() == []

    def test_ignores_files_at_released_root(self, tmp_data_root: Path) -> None:
        released = tmp_data_root / "released"
        released.mkdir()
        (released / "stray.txt").write_text("not a date dir")
        assert paths.all_released_dates() == []


# ---------------------------------------------------------------------------
# Roots and constants — the things every other helper composes from.
# ---------------------------------------------------------------------------

class TestRoots:
    def test_published_urls_is_at_data_root(self) -> None:
        """published_urls.txt lives at data/ root, NEVER under any date dir.
        Surface for the cross-time-dedup contract -- if it ever moves under
        a date dir, every dedup lookup breaks silently."""
        assert paths.PUBLISHED_URLS_PATH.parent == paths.DATA_ROOT
        assert paths.PUBLISHED_URLS_PATH.name == "published_urls.txt"
    # `test_docs_index_is_at_docs_root`, `test_staging_html_dir_under_docs`,
    # `test_released_html_dir_under_docs` cut: each asserted a module
    # constant equalled its definition (tautology). The behavioural
    # contract (staging vs released distinction, files end up under docs/)
    # is already covered by TestRenderPaths and the test_render.py suite.
