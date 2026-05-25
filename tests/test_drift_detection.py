"""Tests for evals.run_evals drift detection (check_drift + eval_drift_detection).

Covers:
  - Degraded mode when fewer than 7 released issues exist in the window.
  - Z-score computation with 8 stable + 1 outlier candidate (drift_high fired).
  - Distribution shift detection (JS divergence > 0.20) on audience tags.
  - Snapshot file written at evals/drift/baselines/<date>.json.
  - passed=True even with extreme z-scores (drift never blocks).
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from evals.run_evals import check_drift, eval_drift_detection, DRIFT_BASELINES_DIR


# ---------------------------------------------------------------------------
# Helpers: build a minimal released archive in a tmp directory.
# ---------------------------------------------------------------------------

# A minimal valid ranked.jsonl row.  We keep score/breakdown in sync with the
# RankedStory validator; the validator requires breakdown keys to match
# RUBRIC_WEIGHTS. Since we read raw JSON dicts (not Pydantic) in the drift
# code, we can use any keys here — the drift extractor only reads
# audience_tags from ranked records.
_RUBRIC_BREAKDOWN = {
    "significance": 50,
    "hands_on_utility": 50,
    "big_picture_relevance": 50,
    "financial_services_impact": 50,
    "freshness_momentum": 50,
}

# Weighted score: 0.30*50 + 0.25*50 + 0.20*50 + 0.15*50 + 0.10*50 = 50
_BASE_SCORE = 50


def _make_ranked_row(
    cluster_id: str,
    audience_tags: list[str],
    tier: str = "on_the_radar",
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "cluster_id": cluster_id,
        "score": _BASE_SCORE,
        "breakdown": _RUBRIC_BREAKDOWN,
        "audience_tags": audience_tags,
        "rationale": "test",
        "tier": tier,
        "prompt_version": "v1",
    }


def _make_story_block(
    story_id: str,
    signal: str | None = "act",
    summary_len: int = 200,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "story_id": story_id,
        "headline": f"Headline for {story_id}",
        "summary": "x" * summary_len,
        "source_urls": ["https://example.com/1"],
        "signal": signal,
    }


def _make_issue(
    d: datetime.date,
    stories_cfg: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a minimal issue.json dict.

    stories_cfg: list of dicts with keys:
        story_id, signal (optional), summary_len (optional)
    All stories are placed in the pulse section for simplicity.
    """
    stories = [
        _make_story_block(
            s["story_id"],
            signal=s.get("signal", "act"),
            summary_len=s.get("summary_len", 200),
        )
        for s in stories_cfg
    ]
    return {
        "schema_version": 2,
        "issue_number": 1,
        "revision": 1,
        "date": d.isoformat(),
        "pulse": {
            "schema_version": 2,
            "name": "pulse",
            "stories": stories,
            "intro_lead": "lead",
            "intro_body": "body",
        },
        "sections": [],
        "generated_at": f"{d.isoformat()}T12:00:00Z",
        "prompt_versions": {"rank": "v1", "summarise": "v1"},
    }


def _write_released_day(
    released_root: Path,
    d: datetime.date,
    issue: dict[str, Any],
    ranked_rows: list[dict[str, Any]],
) -> None:
    """Write issue.json + ranked.jsonl for a released day."""
    day_dir = released_root / d.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    with (day_dir / "issue.json").open("w", encoding="utf-8") as fh:
        json.dump(issue, fh)
    with (day_dir / "ranked.jsonl").open("w", encoding="utf-8") as fh:
        for row in ranked_rows:
            fh.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# 1. Degraded mode when under 7 issues
# ---------------------------------------------------------------------------


class TestDriftDegradedMode:
    def test_degraded_mode_when_under_seven_issues(self, tmp_path: Path) -> None:
        """With 3 issues in the archive, result is PASS + status insufficient_baseline."""
        released_root = tmp_path / "released"
        candidate_date = datetime.date(2026, 6, 10)

        # Write 3 issues: 2 baseline + 1 candidate.
        base_dates = [
            datetime.date(2026, 6, 8),
            datetime.date(2026, 6, 9),
        ]
        for bd in base_dates:
            stories = [{"story_id": f"c_{'a' * 12}", "signal": "act"}]
            ranked = [_make_ranked_row(f"c_{'a' * 12}", ["hands_on"])]
            _write_released_day(released_root, bd, _make_issue(bd, stories), ranked)

        candidate_stories = [{"story_id": f"c_{'b' * 12}", "signal": "act"}]
        candidate_ranked = [_make_ranked_row(f"c_{'b' * 12}", ["hands_on"])]
        _write_released_day(
            released_root,
            candidate_date,
            _make_issue(candidate_date, candidate_stories),
            candidate_ranked,
        )

        result = check_drift(candidate_date, released_root=released_root)

        assert result.passed is True, "Drift must never block"
        assert result.status == "insufficient_baseline"
        assert "insufficient_baseline" in result.status
        detail_msg = result.details.get("message", "")
        assert "7" in detail_msg or "insufficient" in result.status
        assert "3" in detail_msg, f"Expected count=3 in message; got: {detail_msg!r}"

    def test_degraded_mode_detail_names_count(self, tmp_path: Path) -> None:
        """The detail message names the actual issue count."""
        released_root = tmp_path / "released"
        candidate_date = datetime.date(2026, 6, 15)

        # 4 issues total (3 baseline + 1 candidate) = still insufficient
        for i in range(3):
            bd = datetime.date(2026, 6, 12 + i)
            stories = [{"story_id": f"c_{'a' * 11}{i}", "signal": "act"}]
            ranked = [_make_ranked_row(f"c_{'a' * 11}{i}", ["hands_on"])]
            _write_released_day(released_root, bd, _make_issue(bd, stories), ranked)

        c_stories = [{"story_id": f"c_{'b' * 12}", "signal": "act"}]
        c_ranked = [_make_ranked_row(f"c_{'b' * 12}", ["hands_on"])]
        _write_released_day(
            released_root,
            candidate_date,
            _make_issue(candidate_date, c_stories),
            c_ranked,
        )

        result = check_drift(candidate_date, released_root=released_root)
        assert result.passed is True
        assert result.status == "insufficient_baseline"
        assert result.details["window_count"] == 4


# ---------------------------------------------------------------------------
# 2. Z-score computation with outlier
# ---------------------------------------------------------------------------


class TestDriftZScores:
    def _build_stable_archive(
        self, released_root: Path, start: datetime.date, count: int
    ) -> list[datetime.date]:
        """Write `count` baseline issues with stable story_count=8."""
        dates = []
        for i in range(count):
            bd = start + datetime.timedelta(days=i)
            dates.append(bd)
            # 8 stories, each 200 chars summary
            stories = [
                {
                    "story_id": f"c_{'a' * 11}{j}",
                    "signal": "act",
                    "summary_len": 200,
                }
                for j in range(8)
            ]
            ranked = [
                _make_ranked_row(f"c_{'a' * 11}{j}", ["hands_on"])
                for j in range(8)
            ]
            _write_released_day(released_root, bd, _make_issue(bd, stories), ranked)
        return dates

    def test_drift_computes_z_scores_when_baseline_present(
        self, tmp_path: Path
    ) -> None:
        """8 stable issues + 1 outlier candidate fires drift_high on story_count."""
        released_root = tmp_path / "released"
        start = datetime.date(2026, 6, 1)

        # Write 8 baseline issues (stable: 8 stories each).
        self._build_stable_archive(released_root, start, 8)

        # Candidate date: 1 story (way below stable 8 → high |z|).
        candidate_date = start + datetime.timedelta(days=8)
        c_stories = [{"story_id": "c_" + "z" * 12, "signal": "act", "summary_len": 200}]
        c_ranked = [_make_ranked_row("c_" + "z" * 12, ["hands_on"])]
        _write_released_day(
            released_root,
            candidate_date,
            _make_issue(candidate_date, c_stories),
            c_ranked,
        )

        result = check_drift(candidate_date, released_root=released_root)

        assert result.passed is True, "Drift must never block"
        assert result.status == "pass"

        z_scores = result.details.get("z_scores", {})
        assert "story_count" in z_scores

        # story_count z-score: (1 - 8) / max(stdev, 0.5).
        # stdev of [8,8,8,8,8,8,8,8] = 0, floor = 0.5.
        # z = (1 - 8) / 0.5 = -14.0
        assert abs(z_scores["story_count"]) > 2.0, (
            f"Expected |z|>2 for story_count; got {z_scores['story_count']}"
        )

        # drift_high flag should be present
        flags = result.details.get("flags", [])
        flagged_metrics = [f for f in flags if "drift_high:story_count" in f]
        assert flagged_metrics, f"Expected drift_high:story_count flag; got flags={flags}"

    def test_drift_no_flags_on_stable_candidate(self, tmp_path: Path) -> None:
        """A candidate identical to baseline should produce no flags."""
        released_root = tmp_path / "released"
        start = datetime.date(2026, 6, 1)

        self._build_stable_archive(released_root, start, 8)

        # Candidate identical to baseline (8 stories).
        candidate_date = start + datetime.timedelta(days=8)
        c_stories = [
            {
                "story_id": f"c_{'b' * 11}{j}",
                "signal": "act",
                "summary_len": 200,
            }
            for j in range(8)
        ]
        c_ranked = [
            _make_ranked_row(f"c_{'b' * 11}{j}", ["hands_on"])
            for j in range(8)
        ]
        _write_released_day(
            released_root,
            candidate_date,
            _make_issue(candidate_date, c_stories),
            c_ranked,
        )

        result = check_drift(candidate_date, released_root=released_root)
        assert result.passed is True
        flags = result.details.get("flags", [])
        assert flags == [], f"Expected no flags on stable candidate; got {flags}"


# ---------------------------------------------------------------------------
# 3. Distribution shift detection (JS divergence)
# ---------------------------------------------------------------------------


class TestDriftDistributionShift:
    def test_drift_distribution_shift_detected(self, tmp_path: Path) -> None:
        """8 issues heavily hands_on; candidate dominated by big_picture → JS > 0.20."""
        released_root = tmp_path / "released"
        start = datetime.date(2026, 6, 1)

        # Baseline: 8 issues, all stories tagged [hands_on] only.
        for i in range(8):
            bd = start + datetime.timedelta(days=i)
            stories = [
                {"story_id": f"c_{'a' * 11}{j}", "signal": "act", "summary_len": 200}
                for j in range(5)
            ]
            ranked = [
                _make_ranked_row(f"c_{'a' * 11}{j}", ["hands_on"])
                for j in range(5)
            ]
            _write_released_day(released_root, bd, _make_issue(bd, stories), ranked)

        # Candidate: all stories tagged [big_picture] only — big distribution shift.
        candidate_date = start + datetime.timedelta(days=8)
        c_stories = [
            {"story_id": f"c_{'b' * 11}{j}", "signal": "act", "summary_len": 200}
            for j in range(5)
        ]
        c_ranked = [
            _make_ranked_row(f"c_{'b' * 11}{j}", ["big_picture"])
            for j in range(5)
        ]
        _write_released_day(
            released_root,
            candidate_date,
            _make_issue(candidate_date, c_stories),
            c_ranked,
        )

        result = check_drift(candidate_date, released_root=released_root)

        assert result.passed is True
        assert result.status == "pass"

        js_divs = result.details.get("js_divergences", {})
        aud_js = js_divs.get("audience_tag_distribution", 0.0)
        assert aud_js > 0.20, (
            f"Expected JS > 0.20 for audience_tag_distribution; got {aud_js}"
        )

        flags = result.details.get("flags", [])
        shift_flags = [f for f in flags if "distribution_shift:audience_tag_distribution" in f]
        assert shift_flags, f"Expected distribution_shift flag; got flags={flags}"

    def test_drift_stable_distribution_no_flag(self, tmp_path: Path) -> None:
        """Identical distribution in baseline and candidate produces no distribution flag."""
        released_root = tmp_path / "released"
        start = datetime.date(2026, 6, 1)

        for i in range(8):
            bd = start + datetime.timedelta(days=i)
            stories = [
                {"story_id": f"c_{'a' * 11}{j}", "signal": "act", "summary_len": 200}
                for j in range(5)
            ]
            ranked = [
                _make_ranked_row(f"c_{'a' * 11}{j}", ["hands_on", "big_picture"])
                for j in range(5)
            ]
            _write_released_day(released_root, bd, _make_issue(bd, stories), ranked)

        candidate_date = start + datetime.timedelta(days=8)
        c_stories = [
            {"story_id": f"c_{'b' * 11}{j}", "signal": "act", "summary_len": 200}
            for j in range(5)
        ]
        c_ranked = [
            _make_ranked_row(f"c_{'b' * 11}{j}", ["hands_on", "big_picture"])
            for j in range(5)
        ]
        _write_released_day(
            released_root,
            candidate_date,
            _make_issue(candidate_date, c_stories),
            c_ranked,
        )

        result = check_drift(candidate_date, released_root=released_root)
        flags = result.details.get("flags", [])
        dist_flags = [f for f in flags if "distribution_shift" in f]
        assert dist_flags == [], f"Expected no distribution flags; got {dist_flags}"


# ---------------------------------------------------------------------------
# 4. Snapshot file written
# ---------------------------------------------------------------------------


class TestDriftSnapshot:
    def test_drift_writes_baseline_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify snapshot lands at DRIFT_BASELINES_DIR/<date>.json with correct shape."""
        released_root = tmp_path / "released"

        # Redirect DRIFT_BASELINES_DIR to tmp so we don't pollute the real evals/drift/.
        from evals import run_evals as _eh
        fake_baselines_dir = tmp_path / "baselines"
        monkeypatch.setattr(_eh, "DRIFT_BASELINES_DIR", fake_baselines_dir)

        start = datetime.date(2026, 7, 1)

        for i in range(8):
            bd = start + datetime.timedelta(days=i)
            stories = [
                {"story_id": f"c_{'a' * 11}{j}", "signal": "act", "summary_len": 200}
                for j in range(5)
            ]
            ranked = [
                _make_ranked_row(f"c_{'a' * 11}{j}", ["hands_on"])
                for j in range(5)
            ]
            _write_released_day(released_root, bd, _make_issue(bd, stories), ranked)

        candidate_date = start + datetime.timedelta(days=8)
        c_stories = [
            {"story_id": f"c_{'b' * 11}{j}", "signal": "act", "summary_len": 200}
            for j in range(5)
        ]
        c_ranked = [
            _make_ranked_row(f"c_{'b' * 11}{j}", ["hands_on"])
            for j in range(5)
        ]
        _write_released_day(
            released_root,
            candidate_date,
            _make_issue(candidate_date, c_stories),
            c_ranked,
        )

        result = check_drift(candidate_date, released_root=released_root)
        assert result.status == "pass"

        snapshot_path = fake_baselines_dir / f"{candidate_date.isoformat()}.json"
        assert snapshot_path.exists(), f"Snapshot not written to {snapshot_path}"

        with snapshot_path.open() as fh:
            snap = json.load(fh)

        assert snap["date"] == candidate_date.isoformat()
        assert "feature_vector" in snap
        assert "baseline_size" in snap
        assert snap["baseline_size"] == 8
        assert "z_scores" in snap
        assert "js_divergences" in snap
        assert "flags" in snap
        assert isinstance(snap["flags"], list)

        # Feature vector shape
        fv = snap["feature_vector"]
        assert "story_count" in fv
        assert "avg_summary_length" in fv
        assert "finance_tag_rate" in fv
        assert "signal_pill_distribution" in fv
        assert "audience_tag_distribution" in fv

        # Z-score keys
        for metric in ("story_count", "avg_summary_length", "finance_tag_rate"):
            assert metric in snap["z_scores"], f"z_scores missing key {metric!r}"

        # JS divergence keys
        for dist in ("signal_pill_distribution", "audience_tag_distribution"):
            assert dist in snap["js_divergences"], f"js_divergences missing key {dist!r}"


# ---------------------------------------------------------------------------
# 5. Drift never blocks (even with extreme z-scores)
# ---------------------------------------------------------------------------


class TestDriftNeverBlocks:
    def test_drift_never_blocks_with_extreme_z_scores(
        self, tmp_path: Path
    ) -> None:
        """Even with astronomically large z-scores, passed=True."""
        released_root = tmp_path / "released"
        start = datetime.date(2026, 6, 1)

        # Baseline: 8 issues with 100 stories each (extreme stability).
        for i in range(8):
            bd = start + datetime.timedelta(days=i)
            stories = [
                {
                    "story_id": f"c_{'a' * 10}{i:1d}{j:1d}",
                    "signal": "act",
                    "summary_len": 1000,
                }
                for j in range(10)
            ]
            ranked = [
                _make_ranked_row(f"c_{'a' * 10}{i:1d}{j:1d}", ["hands_on", "finance"])
                for j in range(10)
            ]
            _write_released_day(released_root, bd, _make_issue(bd, stories), ranked)

        # Candidate: 1 story with 10-char summary → extreme outlier on all scalars.
        candidate_date = start + datetime.timedelta(days=8)
        c_stories = [{"story_id": "c_" + "z" * 12, "signal": "watch", "summary_len": 10}]
        c_ranked = [_make_ranked_row("c_" + "z" * 12, ["general"])]
        _write_released_day(
            released_root,
            candidate_date,
            _make_issue(candidate_date, c_stories),
            c_ranked,
        )

        result = check_drift(candidate_date, released_root=released_root)

        # Core invariant: drift never blocks.
        assert result.passed is True, (
            f"Drift should never set passed=False; got passed={result.passed}, "
            f"flags={result.details.get('flags')}"
        )

        # But flags should be present (confirming they were computed).
        flags = result.details.get("flags", [])
        assert len(flags) > 0, "Expected flags with extreme outlier; got none"

    def test_drift_result_passed_true_degraded_mode(
        self, tmp_path: Path
    ) -> None:
        """Degraded mode also returns passed=True."""
        released_root = tmp_path / "released"
        candidate_date = datetime.date(2026, 6, 10)
        # Write no issues at all.
        released_root.mkdir(parents=True, exist_ok=True)

        result = check_drift(candidate_date, released_root=released_root)
        assert result.passed is True
        assert result.status == "insufficient_baseline"
