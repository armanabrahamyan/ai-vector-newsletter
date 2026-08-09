"""Unit-style checks for the Eval 9 result cache (evals/review_cache).

STATUS: PROPOSAL (2026-08-09), pending ratification alongside the cache
itself. Lives in evals/ because the Eval Engineer owns this harness code
and is read-only in tests/. Not auto-discovered by the repo pytest config
(``testpaths = ["tests"]``); run explicitly:

    .venv/bin/python -m pytest evals/test_review_cache.py

Load-bearing mutation guards (each test names the code change that turns
it red):

- test_stability_never_reads_primary_cache: rewiring the rerun_n>1 path
  (or _fresh_reviewer_verdicts) to consult the config-keyed cache makes
  the flip-flopping reviewer look perfectly stable and drops the call
  count -- both assertions go red. Verified red against that exact
  mutation on 2026-08-09 before this file was finalised.
- test_midrun_crash_resume_repays_only_incomplete_fixtures: removing the
  per-fixture run_entry_write (writing only at end-of-run instead) makes
  the resumed run re-pay every call -- the zero-new-calls assertion for
  completed fixtures goes red. Verified red against that mutation.
- test_failure_verdicts_never_cached: caching "unavailable" (the incident
  failure state) would serve the outage back as a verdict -- the
  second-run call-count assertion goes red.

Test Engineer: adopt/move into tests/ at your discretion.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import evals.review_cache as review_cache  # noqa: E402
import evals.run_evals as run_evals  # noqa: E402
from evals.run_evals import eval_reviewer_gate  # noqa: E402

# Explicit key context so tests never depend on src.review / env vars.
CTX = {
    "prompt_version": "vTEST",
    "thresholds_version": "tTEST",
    "model": "model-test",
    "temperature": 0.0,
}

N_CASES = 4  # 2 hold + 2 publish


# ---------------------------------------------------------------------------
# Fixtures: a tiny synthetic reviewer-gate fixture set + isolated stores
# ---------------------------------------------------------------------------

def _make_fixture_set(root: Path, n: int = N_CASES):
    issues_dir = root / "issues"
    issues_dir.mkdir()
    cases = []
    for i in range(n):
        cid = f"case_{i:02d}"
        truth = "hold" if i % 2 == 0 else "publish"
        payload = {
            "issue_number": i,
            "title": f"Synthetic fixture {i}",
            "stories": [{"headline": f"Story {i}", "summary": "x" * 20}],
        }
        (issues_dir / f"{cid}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        cases.append({
            "id": cid,
            "category": "seeded_defect" if truth == "hold" else "clean",
            "defect_class": "test_defect" if truth == "hold" else None,
            "ground_truth_gate": truth,
        })
    manifest = root / "cases.yaml"
    manifest.write_text(yaml.safe_dump({"cases": cases}), encoding="utf-8")
    return manifest, issues_dir


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Point the harness at synthetic fixtures and empty, temp-dir stores."""
    manifest, issues_dir = _make_fixture_set(tmp_path)
    monkeypatch.setattr(run_evals, "_REVIEWER_GATE_CASES_PATH", manifest)
    monkeypatch.setattr(run_evals, "_REVIEWER_GATE_ISSUES_DIR", issues_dir)
    monkeypatch.setattr(review_cache, "CACHE_DIR", tmp_path / "rc_cache")
    monkeypatch.setattr(review_cache, "RUNS_DIR", tmp_path / "rc_runs")
    return tmp_path


def _payloads():
    """The synthetic issue payloads, keyed by case id, rebuilt from the loader."""
    return {c["id"]: c["issue"] for c in run_evals._load_reviewer_gate_fixtures()}


class CountingReviewer:
    """Reviewer stub that logs every call and answers via verdict_fn(call_no)."""

    def __init__(self, verdict_fn):
        self.calls = 0
        self.verdict_fn = verdict_fn

    def __call__(self, issue):
        self.calls += 1
        v = self.verdict_fn(self.calls)
        if isinstance(v, BaseException):
            raise v
        return {"verdict": v, "findings": [{"severity": "note", "quote": "q"}]}


class PerFixtureReviewer:
    """Deterministic reviewer: verdict depends on the fixture (issue_number),
    optionally on the per-fixture call index; logs calls per fixture."""

    def __init__(self, verdict_for):
        self.calls_by_issue: dict[int, int] = {}
        self.total_calls = 0
        self.verdict_for = verdict_for  # fn(issue_number, per_fixture_call_idx)

    def __call__(self, issue):
        num = issue["issue_number"]
        idx = self.calls_by_issue.get(num, 0)
        self.calls_by_issue[num] = idx + 1
        self.total_calls += 1
        v = self.verdict_for(num, idx)
        if isinstance(v, BaseException):
            raise v
        return {
            "verdict": v,
            "findings": [{"severity": "note", "quote": f"issue {num}"}],
        }


def _correct(num, _idx):
    """Verdict matching ground truth: even issue_number = hold-labelled."""
    return "red" if num % 2 == 0 else "green"


# ---------------------------------------------------------------------------
# Key semantics
# ---------------------------------------------------------------------------

def test_every_key_component_changes_the_key():
    payload = {"a": 1, "b": [2, 3]}
    base = review_cache.cache_key(payload, CTX)
    assert review_cache.cache_key(payload, CTX) == base  # deterministic
    # Content change.
    assert review_cache.cache_key({"a": 1, "b": [2, 4]}, CTX) != base
    # Key-order-only rewrite of the same content does NOT change the key.
    assert review_cache.cache_key({"b": [2, 3], "a": 1}, CTX) == base
    # Each config component change invalidates.
    for field, value in [
        ("prompt_version", "vOTHER"),
        ("thresholds_version", "tOTHER"),
        ("model", "model-other"),
        ("temperature", 0.7),
    ]:
        assert review_cache.cache_key(payload, {**CTX, field: value}) != base, field


def test_corrupt_or_failure_entries_read_as_miss(isolated):
    key = review_cache.cache_key({"x": 1}, CTX)
    review_cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # Corrupt JSON.
    (review_cache.CACHE_DIR / f"{key}.json").write_text("{not json", encoding="utf-8")
    assert review_cache.read(key) is None
    # A persisted failure state (whatever wrote it) is never served.
    (review_cache.CACHE_DIR / f"{key}.json").write_text(
        json.dumps({"verdict": "unavailable"}), encoding="utf-8"
    )
    assert review_cache.read(key) is None


def test_write_refuses_failure_verdicts(isolated):
    key = review_cache.cache_key({"x": 2}, CTX)
    assert review_cache.write(key, {"verdict": "unavailable", "findings": []}) is False
    assert review_cache.write(key, {"verdict": "unparseable", "findings": []}) is False
    assert review_cache.read(key) is None
    assert review_cache.write(key, {"verdict": "amber", "findings": []}) is True
    assert review_cache.read(key)["verdict"] == "amber"


# ---------------------------------------------------------------------------
# Calibration path (rerun_n == 1): cold run pays, warm run is free
# ---------------------------------------------------------------------------

def test_calibration_cold_then_warm(isolated):
    r1 = PerFixtureReviewer(_correct)
    res1 = eval_reviewer_gate(reviewer=r1, rerun_n=1, cache_context=CTX)
    assert r1.total_calls == N_CASES
    assert res1.details["result_cache"]["seeded"] == N_CASES
    assert res1.details["recall_hold_worthy"] == 1.0
    # gate_stability must be None at rerun_n=1 (no fake trivial 1.0).
    assert res1.details["gate_stability"] is None

    r2 = PerFixtureReviewer(_correct)
    res2 = eval_reviewer_gate(reviewer=r2, rerun_n=1, cache_context=CTX)
    assert r2.total_calls == 0  # fully served from cache
    assert res2.details["result_cache"]["primary_cache_hits"] == N_CASES
    assert res2.details["recall_hold_worthy"] == res1.details["recall_hold_worthy"]
    assert res2.details["precision_publish_safe"] == res1.details["precision_publish_safe"]
    assert all(c["primary_cache_hit"] for c in res2.details["case_results"])


def test_calibration_key_component_change_invalidates(isolated):
    r1 = PerFixtureReviewer(_correct)
    eval_reviewer_gate(reviewer=r1, rerun_n=1, cache_context=CTX)
    r2 = PerFixtureReviewer(_correct)
    other = {**CTX, "model": "model-upgraded"}
    eval_reviewer_gate(reviewer=r2, rerun_n=1, cache_context=other)
    assert r2.total_calls == N_CASES  # nothing served across a model change


def test_failure_verdicts_never_cached(isolated):
    # First run: every call raises (the incident's credits-exhausted state).
    r1 = PerFixtureReviewer(lambda n, i: RuntimeError("credits exhausted"))
    res1 = eval_reviewer_gate(reviewer=r1, rerun_n=1, cache_context=CTX)
    assert res1.passed is False  # reviewer errors still fail the run
    assert res1.details["result_cache"]["seeded"] == 0
    # Second run: the outage must NOT have been frozen into the cache.
    r2 = PerFixtureReviewer(_correct)
    res2 = eval_reviewer_gate(reviewer=r2, rerun_n=1, cache_context=CTX)
    assert r2.total_calls == N_CASES
    assert res2.details["recall_hold_worthy"] == 1.0


def test_use_cache_false_is_always_fresh(isolated):
    r1 = PerFixtureReviewer(_correct)
    eval_reviewer_gate(reviewer=r1, rerun_n=1, cache_context=CTX)
    r2 = PerFixtureReviewer(_correct)
    res2 = eval_reviewer_gate(
        reviewer=r2, rerun_n=1, use_cache=False, stability_resume=False,
        cache_context=CTX,
    )
    assert r2.total_calls == N_CASES
    assert res2.details["result_cache"]["enabled"] is False


def test_unresolvable_context_disables_caching_fail_closed(isolated, monkeypatch):
    monkeypatch.setattr(review_cache, "resolve_context", lambda: None)
    r1 = PerFixtureReviewer(_correct)
    res = eval_reviewer_gate(reviewer=r1, rerun_n=1)  # no explicit context
    assert r1.total_calls == N_CASES
    assert res.details["result_cache"]["enabled"] is False
    assert "unresolved" in res.details["result_cache"]["reason_disabled"]


# ---------------------------------------------------------------------------
# THE CARVE-OUT: stability never reads the config-keyed cache (mutation guard)
# ---------------------------------------------------------------------------

def test_stability_never_reads_primary_cache(isolated):
    # Pre-warm the config cache with a rock-stable "green" for EVERY fixture.
    for payload in _payloads().values():
        key = review_cache.cache_key(payload, CTX)
        assert review_cache.write(key, {"verdict": "green", "findings": []})

    # A reviewer whose fresh answers flip-flop per call: any real fresh
    # sampling MUST observe instability on every fixture.
    flip = PerFixtureReviewer(lambda n, i: "red" if i % 2 == 0 else "green")
    res = eval_reviewer_gate(
        reviewer=flip, rerun_n=2, stability_resume=False, use_cache=True,
        cache_context=CTX,
    )

    # MUTATION GUARD: if the stability path consulted the cache, calls
    # drop below 2*N and/or gate_stability reports the cached fake 1.0.
    assert flip.total_calls == 2 * N_CASES
    assert res.details["gate_stability"] == 0.0
    assert res.details["result_cache"]["primary_cache_hits"] == 0
    # Primary verdicts came from the fresh first call, not the cached green.
    assert all(
        c["primary_verdict"] == "red" and not c["primary_cache_hit"]
        for c in res.details["case_results"]
    )


def test_fresh_reviewer_verdicts_is_cache_blind(isolated, monkeypatch):
    """Belt-and-braces on the structural claim: even with a fully warmed
    cache, the sample collector issues exactly n live calls and never
    touches review_cache.read (poisoned to explode if consulted)."""
    payload = list(_payloads().values())[0]
    key = review_cache.cache_key(payload, CTX)
    review_cache.write(key, {"verdict": "green", "findings": []})

    def _poisoned_read(_key):  # pragma: no cover -- must never run
        raise AssertionError("stability sample collector consulted the cache")

    monkeypatch.setattr(review_cache, "read", _poisoned_read)
    r = CountingReviewer(lambda n: "red")
    verdicts, findings, errors = run_evals._fresh_reviewer_verdicts(r, payload, 3)
    assert r.calls == 3
    assert verdicts == ["red", "red", "red"]
    assert errors == []


# ---------------------------------------------------------------------------
# Partial-run resilience: per-fixture journal + same-day resume
# ---------------------------------------------------------------------------

def test_midrun_crash_resume_repays_only_incomplete_fixtures(isolated):
    """Simulates the 2026-08-09 incident: a stability run (rerun_n=3) dies
    mid-flight; the same-day rerun replays completed fixtures from the run
    journal and only pays for the incomplete ones."""
    kill_at = 2 * 3 + 1  # die on the 7th call: fixtures 0,1 complete; 2 mid-set

    class DyingReviewer(PerFixtureReviewer):
        def __call__(self, issue):
            if self.total_calls >= kill_at - 1:  # this would be call #kill_at
                raise KeyboardInterrupt  # a killed process, not a caught error
            return super().__call__(issue)

    dying = DyingReviewer(_correct)
    with pytest.raises(KeyboardInterrupt):
        eval_reviewer_gate(reviewer=dying, rerun_n=3, cache_context=CTX)
    assert dying.total_calls == kill_at - 1  # 6 paid calls, then death

    # Resume: same config, same rerun_n, same UTC day => same run journal.
    fresh = PerFixtureReviewer(_correct)
    res = eval_reviewer_gate(reviewer=fresh, rerun_n=3, cache_context=CTX)

    # MUTATION GUARD: without per-fixture journal writes the resumed run
    # re-pays all 12 calls; with them it pays only the 2 incomplete
    # fixtures (2 * 3 = 6).
    assert fresh.total_calls == 2 * 3
    assert sorted(res.details["result_cache"]["stability_resumed_case_ids"]) == [
        "case_00", "case_01",
    ]
    resumed = {c["id"]: c["resumed_from_run_journal"] for c in res.details["case_results"]}
    assert resumed == {
        "case_00": True, "case_01": True, "case_02": False, "case_03": False,
    }
    # The resumed verdicts are the first run's genuinely fresh samples.
    assert res.details["recall_hold_worthy"] == 1.0
    assert res.details["gate_stability"] == 1.0
    assert res.passed is True


def test_stability_resume_false_forces_full_fresh_audit(isolated):
    r1 = PerFixtureReviewer(_correct)
    eval_reviewer_gate(reviewer=r1, rerun_n=2, cache_context=CTX)
    assert r1.total_calls == 2 * N_CASES
    # A deliberate fresh audit ignores the same-day journal.
    r2 = PerFixtureReviewer(_correct)
    res = eval_reviewer_gate(
        reviewer=r2, rerun_n=2, stability_resume=False, cache_context=CTX
    )
    assert r2.total_calls == 2 * N_CASES
    assert res.details["result_cache"]["stability_resumed_case_ids"] == []


def test_journal_invalidated_by_config_change(isolated):
    r1 = PerFixtureReviewer(_correct)
    eval_reviewer_gate(reviewer=r1, rerun_n=2, cache_context=CTX)
    r2 = PerFixtureReviewer(_correct)
    eval_reviewer_gate(
        reviewer=r2, rerun_n=2, cache_context={**CTX, "prompt_version": "vNEXT"}
    )
    assert r2.total_calls == 2 * N_CASES  # different run_key: nothing resumed


def test_journal_scoped_to_rerun_n(isolated):
    """A completed rerun_n=2 run must not feed a rerun_n=3 audit."""
    r1 = PerFixtureReviewer(_correct)
    eval_reviewer_gate(reviewer=r1, rerun_n=2, cache_context=CTX)
    r2 = PerFixtureReviewer(_correct)
    res = eval_reviewer_gate(reviewer=r2, rerun_n=3, cache_context=CTX)
    assert r2.total_calls == 3 * N_CASES
    assert res.details["result_cache"]["stability_resumed_case_ids"] == []


def test_incomplete_or_failed_rerun_sets_are_never_journaled(isolated):
    # One fixture's reruns contain an "unavailable" (caught error): its set
    # must not be journaled, so the same-day resume re-runs it fresh.
    def flaky(num, idx):
        if num == 0 and idx == 1:
            return RuntimeError("transient outage")
        return _correct(num, idx)

    r1 = PerFixtureReviewer(flaky)
    res1 = eval_reviewer_gate(reviewer=r1, rerun_n=2, cache_context=CTX)
    assert res1.passed is False  # the raised error is still surfaced
    r2 = PerFixtureReviewer(_correct)
    res2 = eval_reviewer_gate(reviewer=r2, rerun_n=2, cache_context=CTX)
    # Only the flaky fixture (issue 0 / case_00) is re-paid.
    assert r2.total_calls == 2
    assert r2.calls_by_issue == {0: 2}
    assert res2.passed is True


def test_stability_run_seeds_calibration_cache(isolated):
    """Cross-mode payoff: a paid stability run makes the next rerun_n=1
    calibration run free."""
    r1 = PerFixtureReviewer(_correct)
    eval_reviewer_gate(reviewer=r1, rerun_n=3, cache_context=CTX)
    r2 = PerFixtureReviewer(_correct)
    res = eval_reviewer_gate(reviewer=r2, rerun_n=1, cache_context=CTX)
    assert r2.total_calls == 0
    assert res.details["result_cache"]["primary_cache_hits"] == N_CASES


# ---------------------------------------------------------------------------
# Seam + hygiene
# ---------------------------------------------------------------------------

def test_seam_mode_unchanged(isolated):
    res = eval_reviewer_gate(reviewer=None)
    assert res.passed is True
    assert res.status == "reviewer_not_wired"


def test_store_entries_are_gitignored():
    """Matches the judge-cache contract (root .gitignore is outside the
    Eval Engineer's write scope; directory-local .gitignore files provide
    the same effect). Paths need not exist for git check-ignore."""
    for rel in (
        "evals/review_cache/cache/deadbeef.json",
        "evals/review_cache/runs/abc123/case_00.json",
    ):
        proc = subprocess.run(
            ["git", "check-ignore", "-q", rel],
            cwd=REPO_ROOT, capture_output=True, timeout=10,
        )
        assert proc.returncode == 0, f"{rel} is not gitignored"
