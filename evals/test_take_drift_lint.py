"""Unit-style checks for Eval 10 — the deterministic take-drift lint.

STATUS: PROPOSAL (2026-08-08), pending ratification with the Eval 10
thresholds. Lives in evals/ because the Eval Engineer owns this harness
code and is read-only in tests/. Not auto-discovered by the repo pytest
config (``testpaths = ["tests"]``); run explicitly:

    .venv/bin/python -m pytest evals/test_take_drift_lint.py

Every labelled case in evals/fixtures/take-drift/cases.yaml is a mutation
guard: the case's ``note`` names the threshold change that would flip its
verdict, which is what makes these tests load-bearing rather than
decorative. Test Engineer: adopt/move into tests/ at your discretion.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import evals.run_evals as run_evals  # noqa: E402
from evals.run_evals import (  # noqa: E402
    _extract_feature_vector,
    _take_ngrams,
    _take_opener_family,
    check_take_drift,
    eval_take_drift,
)

CASES_PATH = REPO_ROOT / "evals" / "fixtures" / "take-drift" / "cases.yaml"


def _load_cases() -> dict:
    with CASES_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _issue_from_takes(takes: list[str | None], date_str: str = "2026-08-08") -> dict:
    """Minimal issue.json dict: one story per take (None = story w/o take)."""
    stories = []
    for i, take in enumerate(takes):
        story = {"story_id": f"c_{i:016x}", "headline": "h", "summary": "s"}
        if take is not None:
            story["take"] = take
        stories.append(story)
    return {
        "date": date_str,
        "pulse": {"name": "pulse", "stories": stories[:1]},
        "sections": [{"name": "big_picture", "stories": stories[1:]}],
    }


def _write_released_issue(root: Path, date_str: str, takes: list[str]) -> None:
    d = root / date_str
    d.mkdir(parents=True, exist_ok=True)
    (d / "issue.json").write_text(
        json.dumps(_issue_from_takes(takes, date_str)), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Normalisation primitives
# ---------------------------------------------------------------------------

def test_opener_family_strips_articles_case_punctuation():
    assert _take_opener_family("The bar has moved for agent evals.") == "bar has moved"
    assert _take_opener_family("bar has moved; procurement follows.") == "bar has moved"
    assert _take_opener_family("A bar has moved, and budgets with it.") == "bar has moved"


def test_opener_family_distinct_frames_stay_distinct():
    assert _take_opener_family("Latency is the new accuracy fight.") != \
        _take_opener_family("Cheap models are now good enough for triage.")


def test_opener_family_short_take_uses_what_remains():
    assert _take_opener_family("The bar moved.") == "bar moved"
    assert _take_opener_family("") == ""


def test_take_ngrams_keep_articles():
    grams = _take_ngrams("Latency is the new accuracy fight.")
    assert "is the new accuracy" in grams
    assert "the new accuracy fight" in grams


# ---------------------------------------------------------------------------
# Counter (a) — labelled opener cases (each case notes its mutation guard)
# ---------------------------------------------------------------------------

def test_opener_cases_match_labels(tmp_path):
    cases = _load_cases()["opener_cases"]
    assert cases, "cases.yaml lost its opener cases"
    for case in cases:
        issue = _issue_from_takes(case["takes"])
        result = check_take_drift(issue, "2026-08-08", released_root=tmp_path)
        got = sorted(v["family"] for v in result["opener_violations"])
        want = sorted(case["expect_violation_families"])
        assert got == want, (
            f"opener case {case['id']!r}: expected violations {want}, "
            f"got {got}"
        )


def test_opener_rule_reason_is_reported(tmp_path):
    """The count case reports rule=count; the share case rule=share —
    proves both limbs of the predicate are alive, not one shadowing the
    other."""
    cases = {c["id"]: c for c in _load_cases()["opener_cases"]}
    count_case = check_take_drift(
        _issue_from_takes(cases["family_count_violation"]["takes"]),
        "2026-08-08", released_root=tmp_path,
    )
    assert count_case["opener_violations"][0]["rule"] == "count"
    share_case = check_take_drift(
        _issue_from_takes(cases["family_share_violation"]["takes"]),
        "2026-08-08", released_root=tmp_path,
    )
    assert share_case["opener_violations"][0]["rule"] == "share"


# ---------------------------------------------------------------------------
# Counter (b) — labelled cross-issue 4-gram cases
# ---------------------------------------------------------------------------

def test_ngram_cases_match_labels(tmp_path):
    cases = _load_cases()["ngram_cases"]
    assert cases, "cases.yaml lost its ngram cases"
    for case in cases:
        root = tmp_path / case["id"]
        for date_str, takes in case["lookback"].items():
            _write_released_issue(root, str(date_str), takes)
        issue = _issue_from_takes(
            case["candidate_takes"], case["candidate_date"]
        )
        result = check_take_drift(
            issue, case["candidate_date"], released_root=root
        )
        got = sorted(v["ngram"] for v in result["ngram_violations"])
        want = sorted(case["expect_violation_ngrams"])
        assert got == want, (
            f"ngram case {case['id']!r}: expected violations {want}, "
            f"got {got}"
        )


def test_ngram_lookback_skipped_for_undated_datasets(tmp_path):
    """Fixture datasets like '_synthetic' have no archive position: the
    opener counter still runs, the cross-issue counter reports
    computed=False instead of guessing a window."""
    issue = _issue_from_takes(["Latency is the new accuracy fight."])
    result = check_take_drift(issue, "_synthetic", released_root=tmp_path)
    assert result["lookback"]["computed"] is False
    assert result["ngram_violations"] == []


def test_ngram_candidate_date_excluded_from_own_lookback(tmp_path):
    """A released candidate must not count itself: lookback is strictly
    before the candidate date."""
    _write_released_issue(tmp_path, "2026-08-08", ["Latency is the new accuracy fight."])
    _write_released_issue(tmp_path, "2026-08-04", ["Cost is the new accuracy debate."])
    _write_released_issue(tmp_path, "2026-08-02", ["Grounding is the new accuracy war."])
    issue = _issue_from_takes(["Latency is the new accuracy fight."])
    result = check_take_drift(issue, "2026-08-08", released_root=tmp_path)
    # 2 prior issues carry the frame; the candidate's own release must not
    # be the third vote that trips the threshold.
    assert result["ngram_violations"] == []
    assert "2026-08-08" not in result["lookback"]["lookback_dates"]


# ---------------------------------------------------------------------------
# Eval wiring — status contract + the enforcement seam
# ---------------------------------------------------------------------------

def _write_dataset(tmp_path: Path, takes: list[str | None]) -> Path:
    d = tmp_path / "2026-08-08"
    d.mkdir(parents=True, exist_ok=True)
    (d / "issue.json").write_text(
        json.dumps(_issue_from_takes([t for t in takes], "2026-08-08")),
        encoding="utf-8",
    )
    return d


def test_eval_no_takes_is_green(tmp_path):
    dataset = _write_dataset(tmp_path, [None, None, None])
    result = eval_take_drift(dataset, released_root=tmp_path / "released")
    assert result.status == "no_takes"
    assert result.passed is True


def test_eval_violations_are_informational_until_ratified(tmp_path):
    """PROPOSAL seam: TAKE_LINT_ENFORCED=False today, so a violating issue
    reports loudly but passes."""
    assert run_evals.TAKE_LINT_ENFORCED is False, (
        "TAKE_LINT_ENFORCED flipped without updating the ratification "
        "record — this test is the tripwire."
    )
    cases = {c["id"]: c for c in _load_cases()["opener_cases"]}
    dataset = _write_dataset(tmp_path, cases["family_count_violation"]["takes"])
    result = eval_take_drift(dataset, released_root=tmp_path / "released")
    assert result.status == "informational"
    assert result.passed is True
    assert result.metric == 1.0
    assert result.details["failures"], "violation must be reported loudly"


def test_eval_enforced_seam_fails_on_violation(tmp_path, monkeypatch):
    """Mutation proof of the future gate: flipping the ratification switch
    turns the same violating issue into passed=False."""
    monkeypatch.setattr(run_evals, "TAKE_LINT_ENFORCED", True)
    cases = {c["id"]: c for c in _load_cases()["opener_cases"]}
    dataset = _write_dataset(tmp_path, cases["family_count_violation"]["takes"])
    result = eval_take_drift(dataset, released_root=tmp_path / "released")
    assert result.status == "fail"
    assert result.passed is False


def test_eval_enforced_clean_issue_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(run_evals, "TAKE_LINT_ENFORCED", True)
    cases = {c["id"]: c for c in _load_cases()["opener_cases"]}
    dataset = _write_dataset(tmp_path, cases["clean_full_issue"]["takes"])
    result = eval_take_drift(dataset, released_root=tmp_path / "released")
    assert result.status == "pass"
    assert result.passed is True


# ---------------------------------------------------------------------------
# Drift features — take_presence_rate / take_frame_diversity
# ---------------------------------------------------------------------------

def test_feature_vector_none_when_field_absent():
    fv = _extract_feature_vector(_issue_from_takes([None, None]), {})
    assert fv["take_presence_rate"] is None
    assert fv["take_frame_diversity"] is None


def test_feature_vector_presence_and_diversity():
    fv = _extract_feature_vector(
        _issue_from_takes([
            "The bar has moved for agent evals.",
            "bar has moved; procurement follows.",   # same family
            "Your test suite is the moat now.",
            None,                                     # story without a take
        ]),
        {},
    )
    assert fv["take_presence_rate"] == 0.75            # 3 of 4 stories
    assert abs(fv["take_frame_diversity"] - 2 / 3) < 1e-9  # 2 families / 3 takes


def test_feature_vector_empty_take_counts_as_absent_take():
    fv = _extract_feature_vector(
        _issue_from_takes(["  ", "Your test suite is the moat now."]), {}
    )
    assert fv["take_presence_rate"] == 0.5
    assert fv["take_frame_diversity"] == 1.0


# ---------------------------------------------------------------------------
# Drift floor rules — constant-series blindness guards (via check_drift)
# ---------------------------------------------------------------------------

def _build_archive(root: Path, days: list[tuple[str, list[str]]]) -> None:
    for date_str, takes in days:
        _write_released_issue(root, date_str, takes)


def _distinct_takes(n: int, salt: str) -> list[str]:
    """n takes with n distinct opener families."""
    stems = [
        "Latency wins the {s} argument {i}.",
        "Vendors lost the {s} debate {i}.",
        "Auditors set the {s} pace {i}.",
        "Retrieval beat the {s} pitch {i}.",
        "Budgets follow the {s} shift {i}.",
        "Regulators read the {s} papers {i}.",
        "Benchmarks fail the {s} test {i}.",
        "Routers carry the {s} load {i}.",
        "Contracts price the {s} risk {i}.",
        "Evals gate the {s} release {i}.",
        "Weights move the {s} market {i}.",
    ]
    return [stems[i % len(stems)].format(s=salt, i=i) for i in range(n)]


def _templated_takes(n: int, salt: str) -> list[str]:
    """n takes where exactly two share an opener family (diversity < 1)."""
    takes = _distinct_takes(n, salt)
    # Same first-3-non-article-words family as takes[0]
    # ("latency wins <salt>"), different tail.
    takes[1] = f"Latency wins the {salt} rematch."
    return takes


def test_take_floors_fire_and_stay_quiet(tmp_path, monkeypatch):
    """One integration pass through check_drift:
    - a 12-day archive where every day's takes template (diversity < 1.0)
      must raise floor:take_frame_diversity;
    - a candidate whose stories carry NO take field after 12 take-habit
      days must raise floor:takes_vanished (the z-score cannot: None is
      excluded from the baseline by design)."""
    import datetime as dt

    monkeypatch.setattr(run_evals, "DRIFT_BASELINES_DIR", tmp_path / "baselines")
    root = tmp_path / "released"
    days = []
    for i in range(12):
        d = (dt.date(2026, 8, 8) - dt.timedelta(days=12 - i)).isoformat()
        days.append((d, _templated_takes(5, f"day{i}")))
    _build_archive(root, days)
    # Candidate: released issue with NO take field on any story.
    cand_dir = root / "2026-08-08"
    cand_dir.mkdir(parents=True)
    (cand_dir / "issue.json").write_text(
        json.dumps(_issue_from_takes([None] * 5, "2026-08-08")),
        encoding="utf-8",
    )

    result = run_evals.check_drift(dt.date(2026, 8, 8), released_root=root)
    assert result.status == "pass"
    floors = result.details["take_floors"]
    assert floors["takes_vanished"] is True
    assert floors["frame_diversity_floor"] is True
    assert any(f.startswith("floor:takes_vanished") for f in result.details["flags"])
    assert any(f.startswith("floor:take_frame_diversity") for f in result.details["flags"])


def test_take_floors_quiet_on_healthy_archive(tmp_path, monkeypatch):
    """Same archive size, but takes present today and one fully-distinct
    day in the window: neither floor fires. Proves the floors are
    condition-bound, not always-on."""
    import datetime as dt

    monkeypatch.setattr(run_evals, "DRIFT_BASELINES_DIR", tmp_path / "baselines")
    root = tmp_path / "released"
    days = []
    for i in range(12):
        d = (dt.date(2026, 8, 8) - dt.timedelta(days=12 - i)).isoformat()
        takes = _templated_takes(5, f"day{i}") if i else _distinct_takes(5, "day0")
        days.append((d, takes))
    _build_archive(root, days)
    _write_released_issue(root, "2026-08-08", _distinct_takes(5, "today"))

    result = run_evals.check_drift(dt.date(2026, 8, 8), released_root=root)
    floors = result.details["take_floors"]
    assert floors["takes_vanished"] is False
    assert floors["frame_diversity_floor"] is False
    # And with 11 non-None baseline days, the z-scores must exist.
    assert "take_presence_rate" in result.details["z_scores"]
    assert "take_frame_diversity" in result.details["z_scores"]


def test_take_floors_insufficient_history_is_quiet(tmp_path, monkeypatch):
    """Below min_samples (10), 'no fully-distinct day' is sparse history,
    not a signal — the floor must stay down (same rationale as the
    reviewer red floor's min_samples)."""
    import datetime as dt

    monkeypatch.setattr(run_evals, "DRIFT_BASELINES_DIR", tmp_path / "baselines")
    root = tmp_path / "released"
    days = []
    for i in range(8):  # >= drift baseline min (7), < floor min (10)
        d = (dt.date(2026, 8, 8) - dt.timedelta(days=8 - i)).isoformat()
        days.append((d, _templated_takes(5, f"day{i}")))
    _build_archive(root, days)
    _write_released_issue(root, "2026-08-08", _templated_takes(5, "today"))

    result = run_evals.check_drift(dt.date(2026, 8, 8), released_root=root)
    floors = result.details["take_floors"]
    assert floors["frame_diversity_floor"] is False
    assert floors["takes_vanished"] is False
