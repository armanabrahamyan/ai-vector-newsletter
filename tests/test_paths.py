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
# all_staging_dates + unreleased_predecessors — the duplicate-risk guard.
# Cross-time dedup reads released-only, so a staged-but-unreleased earlier
# issue is invisible to it; a later issue may silently repeat its stories.
# ---------------------------------------------------------------------------

def _seed(root: Path, sub: str, date: str) -> None:
    """Create data/<sub>/<date>/issue.json under the tmp data root."""
    d = root / sub / date
    d.mkdir(parents=True)
    (d / "issue.json").write_text("{}")


class TestAllStagingDates:
    def test_empty_when_staging_root_missing(self, tmp_data_root: Path) -> None:
        assert paths.all_staging_dates() == []

    def test_returns_only_dirs_with_issue_json(self, tmp_data_root: Path) -> None:
        _seed(tmp_data_root, "staging", "2026-05-23")
        partial = tmp_data_root / "staging" / "2026-05-24"
        partial.mkdir(parents=True)  # no issue.json
        assert paths.all_staging_dates() == [_dt.date(2026, 5, 23)]

    def test_returns_sorted_ascending(self, tmp_data_root: Path) -> None:
        for d in ("2026-05-23", "2026-05-21", "2026-05-22"):
            _seed(tmp_data_root, "staging", d)
        assert paths.all_staging_dates() == [
            _dt.date(2026, 5, 21),
            _dt.date(2026, 5, 22),
            _dt.date(2026, 5, 23),
        ]


class TestUnreleasedPredecessors:
    def test_none_when_all_earlier_staging_is_released(
        self, tmp_data_root: Path
    ) -> None:
        # Earlier days exist in BOTH staging and released -> dedup saw them.
        for d in ("2026-05-29", "2026-05-30"):
            _seed(tmp_data_root, "staging", d)
            _seed(tmp_data_root, "released", d)
        _seed(tmp_data_root, "staging", "2026-05-31")
        assert paths.unreleased_predecessors(_dt.date(2026, 5, 31)) == []

    def test_flags_earlier_staged_but_unreleased(self, tmp_data_root: Path) -> None:
        # The real May-31 case: May 29 + 30 staged, none released.
        for d in ("2026-05-29", "2026-05-30", "2026-05-31"):
            _seed(tmp_data_root, "staging", d)
        assert paths.unreleased_predecessors(_dt.date(2026, 5, 31)) == [
            _dt.date(2026, 5, 29),
            _dt.date(2026, 5, 30),
        ]

    def test_excludes_the_date_itself_and_later(self, tmp_data_root: Path) -> None:
        for d in ("2026-05-30", "2026-05-31", "2026-06-01"):
            _seed(tmp_data_root, "staging", d)
        # Only strictly-earlier unreleased days count.
        assert paths.unreleased_predecessors(_dt.date(2026, 5, 31)) == [
            _dt.date(2026, 5, 30),
        ]

    def test_respects_lookback_window(self, tmp_data_root: Path) -> None:
        # 20 days before is outside the 14-day dedup window -> not flagged.
        _seed(tmp_data_root, "staging", "2026-05-11")
        _seed(tmp_data_root, "staging", "2026-05-31")
        assert paths.unreleased_predecessors(_dt.date(2026, 5, 31)) == []
        # ...but a wider explicit window includes it.
        assert paths.unreleased_predecessors(
            _dt.date(2026, 5, 31), lookback_days=30
        ) == [_dt.date(2026, 5, 11)]

    def test_window_matches_dedup_constant(self) -> None:
        # Guard against drift: the warning window MUST equal the dedup window.
        from src import cluster
        assert cluster.CROSS_TIME_LOOKBACK_DAYS == paths.DEDUP_LOOKBACK_DAYS


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
