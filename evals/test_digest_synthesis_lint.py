"""Unit-style checks for Eval 11 — the deterministic digest/synthesis lint.

STATUS: PROPOSAL (2026-08-09), pending ratification with the Eval 11
thresholds. Lives in evals/ because the Eval Engineer owns this harness
code and is read-only in tests/. Not auto-discovered by the repo pytest
config (``testpaths = ["tests"]``); run explicitly:

    .venv/bin/python -m pytest evals/test_digest_synthesis_lint.py

Every labelled case in evals/fixtures/digest-lint/cases.yaml is a
mutation guard: the case's ``note`` names the threshold or check change
that would flip its verdict, which is what makes these tests
load-bearing rather than decorative. Test Engineer: adopt/move into
tests/ at your discretion.
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
    _find_banned_phrases,
    _issue_tag_override_rate,
    _iter_issue_texts,
    _normalise_surface,
    _sentence_count,
    _word_count,
    check_digest_synthesis,
    eval_digest_synthesis,
)

CASES_PATH = REPO_ROOT / "evals" / "fixtures" / "digest-lint" / "cases.yaml"


def _load_cases() -> list[dict]:
    with CASES_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)["cases"]


def _case(case_id: str) -> dict:
    for case in _load_cases():
        if case["id"] == case_id:
            return case
    raise KeyError(case_id)


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def test_word_count_matches_take_tokeniser():
    assert _word_count("Agents run locally now") == 4
    assert _word_count("Model-risk sign-off widens") == 5  # hyphens split
    assert _word_count("") == 0


def test_sentence_count_ignores_decimals_and_versions():
    assert _sentence_count("Run v0.22 against the 28.9% claim today.") == 1
    assert _sentence_count("One sentence. Then another.") == 2
    assert _sentence_count("No terminal punctuation at all") == 1
    assert _sentence_count("") == 0


def test_normalise_surface_catches_case_and_punctuation_variants():
    assert _normalise_surface("Model-risk sign-off, now!") == \
        _normalise_surface("model risk sign off now")


def test_banned_phrase_patterns_hit_and_miss():
    assert _find_banned_phrases("Trust, but verify the claim.") == ["trust-but-verify"]
    assert _find_banned_phrases("it remains to be seen whether") == ["it-remains-to-be-seen"]
    assert _find_banned_phrases("Only time will tell.") == ["time-will-tell"]
    # Near-misses must NOT flag: the catalogue's specific phrases only.
    assert _find_banned_phrases("Verify the release against your baseline.") == []
    assert _find_banned_phrases("Trust in the process grew.") == []


# ---------------------------------------------------------------------------
# Labelled cases — each is a mutation guard (see cases.yaml notes)
# ---------------------------------------------------------------------------

def test_all_labelled_cases():
    for case in _load_cases():
        result = check_digest_synthesis(case["issue"])
        got = sorted(v["kind"] for v in result["violations"])
        expected = sorted(case.get("expect_violation_kinds", []))
        assert got == expected, (
            f"case {case['id']}: expected violations {expected}, got {got} "
            f"(failures: {result['failures']})"
        )
        for seam_key, seam_value in (case.get("expect_seams") or {}).items():
            assert result["seams"].get(seam_key) == seam_value, (
                f"case {case['id']}: expected seam {seam_key}={seam_value}, "
                f"got {result['seams']}"
            )


def test_verify_seam_is_flagged_loudly_but_not_a_violation():
    case = _case("verify_seam_digest_never_verified")
    result = check_digest_synthesis(case["issue"])
    assert result["violations"] == []
    assert any("SEAM digest_claims_absent" in f for f in result["failures"])


# ---------------------------------------------------------------------------
# Eval wrapper status contract (mirrors Eval 10's)
# ---------------------------------------------------------------------------

def test_eval_skips_on_missing_dataset(tmp_path):
    result = eval_digest_synthesis(tmp_path / "nope")
    assert result.status == "skipped" and result.passed


def test_eval_no_redesign_surfaces_on_pre_v023_issue(tmp_path):
    d = tmp_path / "2026-08-08"
    d.mkdir()
    issue = {
        "date": "2026-08-08",
        "pulse": {"name": "pulse", "stories": [
            {"story_id": "c_" + "a" * 12, "headline": "h", "summary": "s",
             "take": "The bar for agent audits just moved."},
        ]},
        "sections": [{"name": "big_picture", "intro_lead": "Lead here.",
                      "intro_body": "Body here.", "stories": []}],
    }
    (d / "issue.json").write_text(json.dumps(issue), encoding="utf-8")
    result = eval_digest_synthesis(d)
    assert result.status == "no_redesign_surfaces" and result.passed
    # The route gap stays loud even pre-digest: takes exist, routes don't.
    assert result.details["seams"].get("take_routes") == "not_exposed"


def test_eval_informational_until_ratified(tmp_path):
    assert run_evals.DIGEST_LINT_ENFORCED is False, (
        "DIGEST_LINT_ENFORCED flipped without a ratification note in this "
        "test — the gate moves only on ratification"
    )
    case = _case("budget_and_bands_violations")
    d = tmp_path / "2026-08-09"
    d.mkdir()
    (d / "issue.json").write_text(json.dumps(case["issue"]), encoding="utf-8")
    result = eval_digest_synthesis(d)
    assert result.status == "informational" and result.passed
    assert result.metric == float(len(case["expect_violation_kinds"]))


def test_eval_would_fail_when_enforced(tmp_path, monkeypatch):
    """The enforcement seam is live: flipping the constant makes the same
    dataset fail — proof the gate is one ratified line away."""
    monkeypatch.setattr(run_evals, "DIGEST_LINT_ENFORCED", True)
    case = _case("surface_collision_verbatim")
    d = tmp_path / "2026-08-09"
    d.mkdir()
    (d / "issue.json").write_text(json.dumps(case["issue"]), encoding="utf-8")
    result = eval_digest_synthesis(d)
    assert result.status == "fail" and not result.passed


# ---------------------------------------------------------------------------
# Eval 8 surface extension: _iter_issue_texts learned synthesis + digest
# (the DESIGN.md-flagged iteration). Mutation guard: revert the field
# tuple to ("intro_lead", "intro_body") and these go red.
# ---------------------------------------------------------------------------

def test_iter_issue_texts_includes_synthesis_and_digest():
    issue = {
        "pulse": {"name": "pulse", "stories": [
            {"story_id": "c_" + "a" * 12, "headline": "H", "summary": "S"},
        ]},
        "sections": [
            {"name": "big_picture", "synthesis": "The synthesis paragraph.",
             "stories": []},
        ],
        "digest": [
            {"lead": "Bold lead", "sentence": "The sentence.",
             "story_ids": ["c_" + "a" * 12]},
        ],
    }
    units = _iter_issue_texts(issue)
    kinds = {(loc, kind) for loc, kind, _text in units}
    assert ("section:big_picture", "intro") in kinds
    assert ("digest:0", "digest") in kinds
    texts = {text for _loc, _kind, text in units}
    assert "The synthesis paragraph." in texts
    assert "Bold lead The sentence." in texts


def test_absence_form_in_digest_is_linted():
    """R-8 absence-inventory in a digest bullet must be caught by Eval 8's
    absence scan now that digest units are iterated. Mutation guard for
    the kind-set extension in eval_reading_experience_lint."""
    from evals.run_evals import _find_absence_forms
    hits = _find_absence_forms("No code is public yet for the runtime.")
    assert hits, "sanity: the absence pattern itself must match"
    issue = {
        "pulse": {"name": "pulse", "stories": []},
        "sections": [],
        "digest": [
            {"lead": "Runtime ships quietly",
             "sentence": "No code is public yet for the runtime.",
             "story_ids": ["c_" + "a" * 12]},
        ],
    }
    digest_units = [
        (loc, kind, text) for loc, kind, text in _iter_issue_texts(issue)
        if kind == "digest"
    ]
    assert digest_units and _find_absence_forms(digest_units[0][2])


# ---------------------------------------------------------------------------
# Drift feature: tag_override_rate (constant-series floor pattern)
# ---------------------------------------------------------------------------

def _issue_with_signals(section_name: str, signals: list) -> dict:
    return {
        "pulse": {"name": "pulse", "stories": []},
        "sections": [{
            "name": section_name,
            "stories": [
                {"story_id": f"c_{i:012x}", "headline": "h", "summary": "s",
                 "signal": sig}
                for i, sig in enumerate(signals)
            ],
        }],
    }


def test_tag_override_rate_counts_only_default_beating_signals():
    # hands_on default is "try": one override ("act"), one default-match
    # ("try"), one absent (falls back to default — not an override).
    issue = _issue_with_signals("hands_on", ["act", "try", None])
    assert _issue_tag_override_rate(issue) == 1 / 3


def test_tag_override_rate_none_on_empty_issue():
    issue = {"pulse": {"name": "pulse", "stories": []}, "sections": []}
    assert _issue_tag_override_rate(issue) is None


def test_tag_override_rate_legacy_section_name():
    # on_the_radar (pre-v3 archive) defaults to "watch" like currents.
    issue = _issue_with_signals("on_the_radar", ["watch", "read"])
    assert _issue_tag_override_rate(issue) == 1 / 2


def test_feature_vector_carries_tag_override_rate():
    from evals.run_evals import _extract_feature_vector
    issue = _issue_with_signals("hands_on", ["act", "try"])
    fv = _extract_feature_vector(issue, {})
    assert fv["tag_override_rate"] == 1 / 2
