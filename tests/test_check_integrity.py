"""Tests for evals.run_evals.check_integrity() and staging mode of run_evals().

Covers:
  - Happy path: May 24 released data passes all assertions.
  - Failure path: _thin_staging fixture fails hands_on + source fire rate.
  - Path resolution: staging=True resolves to data/staging/<date>/,
    staging=False resolves to data/released/<date>/.
  - run_evals() staging=True skips label-dependent evals with the correct
    "skipped: no labels for unreleased date" status.
  - Mutual exclusion: --staging + --fixture is rejected by _run_eval().
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import pytest

# Ensure repo root is on sys.path so evals.run_evals is importable.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from evals.run_evals import (
    DATA_DIR,
    FIXTURES_DIR,
    _resolve_dataset_dir,
    check_integrity,
    run_evals,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_THIN_FIXTURE_DIR = FIXTURES_DIR / "_thin_staging"
_RELEASED_2026_05_24 = DATA_DIR / "released" / "2026-05-24"
_STAGING_2026_05_25 = DATA_DIR / "staging" / "2026-05-25"


# ---------------------------------------------------------------------------
# check_integrity() — path resolution
# ---------------------------------------------------------------------------


def test_check_integrity_resolves_released_path(tmp_path, monkeypatch):
    """check_integrity with staging=False reads data/released/<date>/."""
    from evals import run_evals as _eh
    monkeypatch.setattr(_eh, "DATA_DIR", tmp_path)

    # Create a minimal released directory that will fail (missing files is
    # detectable via "not found" failure).
    date = datetime.date(2026, 6, 1)
    # Don't create the directory — should surface "Dataset directory not found"
    failures, ok = check_integrity(date, staging=False)
    assert not ok
    assert any("not found" in f.lower() for f in failures)
    # Confirm it tried the released path, not staging
    assert any("released" in f for f in failures)


def test_check_integrity_resolves_staging_path(tmp_path, monkeypatch):
    """check_integrity with staging=True reads data/staging/<date>/."""
    from evals import run_evals as _eh
    monkeypatch.setattr(_eh, "DATA_DIR", tmp_path)

    date = datetime.date(2026, 6, 1)
    failures, ok = check_integrity(date, staging=True)
    assert not ok
    assert any("staging" in f for f in failures)


# ---------------------------------------------------------------------------
# check_integrity() — happy path (May 24 released)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _RELEASED_2026_05_24.exists(),
    reason="data/released/2026-05-24/ not present in this environment",
)
def test_check_integrity_may24_released_passes():
    """May 24 released data satisfies all Phase B assertions."""
    failures, ok = check_integrity(datetime.date(2026, 5, 24), staging=False)
    assert ok, f"Expected pass but got failures: {failures}"
    assert failures == []


# ---------------------------------------------------------------------------
# check_integrity() — failure path (_thin_staging fixture)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _THIN_FIXTURE_DIR.exists(),
    reason="evals/fixtures/_thin_staging/ not present",
)
def test_check_integrity_thin_staging_fails_hands_on(tmp_path, monkeypatch):
    """_thin_staging fixture fails because hands_on count < 3."""
    from evals import run_evals as _eh

    # Point DATA_DIR at a temp tree that contains the thin staging fixture
    # under data/staging/2026-05-25/ so check_integrity can find it.
    staging_date_dir = tmp_path / "staging" / "2026-05-25"
    staging_date_dir.mkdir(parents=True)

    for fname in ("items.jsonl", "clusters.jsonl", "ranked.jsonl",
                  "issue.json", "source_health.json"):
        src = _THIN_FIXTURE_DIR / fname
        if src.exists():
            (staging_date_dir / fname).write_bytes(src.read_bytes())

    monkeypatch.setattr(_eh, "DATA_DIR", tmp_path)

    failures, ok = check_integrity(datetime.date(2026, 5, 25), staging=True)
    assert not ok, "Expected failure but check_integrity returned ok=True"

    # The hands_on assertion must be in the failure list.
    hands_on_failures = [f for f in failures if "hands_on" in f.lower()]
    assert hands_on_failures, (
        f"Expected a hands_on failure but failures were: {failures}"
    )

    # Specifically: the fixture has 1 hands_on story (minimum 3 required).
    assert any("1" in f for f in hands_on_failures), (
        f"Expected '1 hands_on' in failure message, got: {hands_on_failures}"
    )


@pytest.mark.skipif(
    not _THIN_FIXTURE_DIR.exists(),
    reason="evals/fixtures/_thin_staging/ not present",
)
def test_check_integrity_thin_staging_fails_source_fire_rate(tmp_path, monkeypatch):
    """_thin_staging fixture also fails because source fire rate < 0.80."""
    from evals import run_evals as _eh

    staging_date_dir = tmp_path / "staging" / "2026-05-25"
    staging_date_dir.mkdir(parents=True)

    for fname in ("items.jsonl", "clusters.jsonl", "ranked.jsonl",
                  "issue.json", "source_health.json"):
        src = _THIN_FIXTURE_DIR / fname
        if src.exists():
            (staging_date_dir / fname).write_bytes(src.read_bytes())

    monkeypatch.setattr(_eh, "DATA_DIR", tmp_path)

    failures, ok = check_integrity(datetime.date(2026, 5, 25), staging=True)
    assert not ok

    fire_failures = [f for f in failures if "source fire rate" in f.lower()]
    assert fire_failures, (
        f"Expected a source fire rate failure but failures were: {failures}"
    )


# ---------------------------------------------------------------------------
# check_integrity() — minimal synthetic pass (no source_health.json)
# ---------------------------------------------------------------------------


def test_check_integrity_passes_without_source_health(tmp_path, monkeypatch):
    """Integrity passes a well-formed issue with no source_health.json present.

    source_health.json absence is a soft concern — not a hard failure.
    """
    from evals import run_evals as _eh
    monkeypatch.setattr(_eh, "DATA_DIR", tmp_path)

    date = datetime.date(2026, 6, 1)
    released_dir = tmp_path / "released" / date.isoformat()
    released_dir.mkdir(parents=True)

    # Cluster IDs must match ^c_[0-9a-f]{12,}$ — use valid hex IDs.
    PULSE_CID  = "c_" + "a0b1c2d3e4f5"  # 12 hex chars
    HANDS_CID1 = "c_" + "b0c1d2e3f4a5"
    HANDS_CID2 = "c_" + "c0d1e2f3a4b5"
    HANDS_CID3 = "c_" + "d0e1f2a3b4c5"
    ALL_CIDS   = [PULSE_CID, HANDS_CID1, HANDS_CID2, HANDS_CID3]

    # Items: one per cluster (item IDs are free-form strings).
    items_text = "\n".join(
        json.dumps({
            "schema_version": 1,
            "id": f"item_{i:03d}",
            "source": "test_blog",
            "source_type": "rss",
            "url": f"https://example.com/item-{i}",
            "title": f"Test item {i}",
            "published_at": "2026-06-01T09:00:00Z",
            "raw_summary": "Test",
            "fetched_at": "2026-06-01T10:00:00Z",
        })
        for i in range(len(ALL_CIDS))
    )
    (released_dir / "items.jsonl").write_text(items_text + "\n", encoding="utf-8")

    clusters_text = "\n".join(
        json.dumps({
            "schema_version": 1,
            "cluster_id": cid,
            "item_ids": [f"item_{i:03d}"],
            "canonical_title": f"Test item {i}",
            "sources": ["test_blog"],
            "earliest_published": "2026-06-01T09:00:00Z",
            "size": 1,
            "cross_time_ref": None,
        })
        for i, cid in enumerate(ALL_CIDS)
    )
    (released_dir / "clusters.jsonl").write_text(clusters_text + "\n", encoding="utf-8")

    def _rs(cid: str, tier: str) -> dict:
        return {
            "schema_version": 1,
            "cluster_id": cid,
            "score": 60,
            "breakdown": {
                "significance": 60, "hands_on_utility": 60,
                "big_picture_relevance": 60, "financial_services_impact": 60,
                "freshness_momentum": 60,
            },
            "audience_tags": ["hands_on"],
            "rationale": "Test",
            "tier": tier,
            "prompt_version": "v1",
        }

    # Under Shape A (RankedStory v3+), pulse is not a stored tier -- the
    # pulse story is picked from head-tier candidates. All four clusters
    # tier=hands_on here so the fixture parses; the issue.json below still
    # places PULSE_CID into the pulse section via the picker contract.
    ranked_text = "\n".join(
        json.dumps(_rs(cid, "hands_on"))
        for cid in ALL_CIDS
    )
    (released_dir / "ranked.jsonl").write_text(ranked_text + "\n", encoding="utf-8")

    def _story(sid: str) -> dict:
        return {
            "schema_version": 1,
            "story_id": sid,
            "headline": "Test headline",
            "summary": "Test summary.",
            "source_urls": ["https://example.com/item-0"],
            "signal": "watch",
        }

    issue = {
        "schema_version": 5,
        "issue_number": 1,
        "revision": 0,
        "date": "2026-06-01",
        "pulse": {
            "schema_version": 1,
            "name": "pulse",
            "stories": [_story(PULSE_CID)],
        },
        "sections": [
            {
                "schema_version": 1,
                "name": "hands_on",
                "intro_lead": "Hands on.",
                "intro_body": "Build this week.",
                "stories": [
                    _story(HANDS_CID1),
                    _story(HANDS_CID2),
                    _story(HANDS_CID3),
                ],
            },
        ],
        "generated_at": "2026-06-01T10:00:00Z",
        "prompt_versions": {"rank": "v1", "summarise": "v1", "pulse": "v1"},
        "notes": "",
    }
    (released_dir / "issue.json").write_text(json.dumps(issue), encoding="utf-8")

    failures, ok = check_integrity(date, staging=False)
    assert ok, f"Expected pass but got failures: {failures}"


# ---------------------------------------------------------------------------
# run_evals() — staging=True skips label-dependent evals
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _STAGING_2026_05_25.exists(),
    reason="data/staging/2026-05-25/ not present in this environment",
)
def test_run_evals_staging_skips_label_dependent():
    """run_evals(staging=True) skips dedup_quality and ranking_quality with
    the correct 'skipped: no labels for unreleased date' message."""
    report, exit_code = run_evals(
        dataset="2026-05-25",
        against="real",
        no_judge=True,
        staging=True,
    )

    result_map = {r["name"]: r for r in report["results"]}

    assert result_map["dedup_quality"]["status"] == "skipped"
    assert "no labels" in result_map["dedup_quality"]["details"].get("message", "").lower()

    assert result_map["ranking_quality"]["status"] == "skipped"
    assert "no labels" in result_map["ranking_quality"]["details"].get("message", "").lower()

    # module_integrity should still run (not skipped)
    assert result_map["module_integrity"]["status"] in ("pass", "fail")


# ---------------------------------------------------------------------------
# run_evals() — staging=True + fixture raises ValueError (logical guard,
#   tested at the _run_eval layer in src/run.py — not run_evals itself)
# ---------------------------------------------------------------------------


def test_run_evals_staging_against_fixtures_not_a_conflict():
    """run_evals() with staging=True and against='fixtures' is valid:
    staging only affects path resolution for against='real', so it's
    silently ignored for fixtures. No ValueError expected."""
    # _thin_staging fixture must exist; if not, skip gracefully.
    if not _THIN_FIXTURE_DIR.exists():
        pytest.skip("_thin_staging fixture not present")

    # Should not raise — staging is a no-op for against='fixtures'.
    report, _ = run_evals(
        dataset="_thin_staging",
        against="fixtures",
        no_judge=True,
        staging=True,
    )
    assert "results" in report


# ---------------------------------------------------------------------------
# _resolve_dataset_dir() — explicit path resolution checks
# ---------------------------------------------------------------------------


def test_resolve_dataset_dir_staging_path():
    """staging=True resolves to data/staging/<date>/."""
    result = _resolve_dataset_dir("2026-05-25", "real", staging=True)
    assert result is not None
    assert "staging" in result.parts
    assert result.name == "2026-05-25"


def test_resolve_dataset_dir_released_path():
    """staging=False (default) resolves to data/released/<date>/ when it exists."""
    # Use a date we know is released.
    if not _RELEASED_2026_05_24.exists():
        pytest.skip("data/released/2026-05-24/ not present")
    result = _resolve_dataset_dir("2026-05-24", "real", staging=False)
    assert result is not None
    assert "released" in result.parts


def test_resolve_dataset_dir_fixtures():
    """against='fixtures' ignores staging flag."""
    result = _resolve_dataset_dir("_synthetic", "fixtures", staging=True)
    assert result is not None
    assert result == FIXTURES_DIR / "_synthetic"
