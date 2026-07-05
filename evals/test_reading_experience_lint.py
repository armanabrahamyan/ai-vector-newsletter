"""Unit-style checks for Eval 8 — the deterministic R-8/R-9 reading-experience lint.

Lives in evals/ because the Eval Engineer owns this harness code and is
read-only in tests/. Not auto-discovered by the repo pytest config
(``testpaths = ["tests"]``); run explicitly:

    .venv/bin/python -m pytest evals/test_reading_experience_lint.py

Test Engineer: adopt/move into tests/ at your discretion — no objection
from the Eval Engineer, and nothing here depends on this file's location.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from evals.run_evals import (  # noqa: E402
    _BANNED_NEW_GENERIC_OPENER,
    _find_absence_forms,
    eval_reading_experience_lint,
)


# ---------------------------------------------------------------------------
# R-8 absence-form regex — catches the ruled examples
# ---------------------------------------------------------------------------

def test_absence_forms_catch_ruled_examples():
    """Every absence form named in READING_EXPERIENCE.md R-8 must match."""
    ruled_examples = [
        "No code is public yet.",
        "No code is public",
        "No code yet",
        "No code is linked.",
        "no independent replication yet",
        "no independent benchmarks yet",
        "Single-source, no code released",
        "Single-author post, no benchmark data",
        "peer review pending",
        "No regulatory framework yet",
        "No patch exists yet",
        "no stable tag yet",
    ]
    for text in ruled_examples:
        assert _find_absence_forms(text), f"R-8 example not caught: {text!r}"


def test_absence_forms_leave_presence_forms_alone():
    """Presence-form flags and ordinary prose must NOT match (gate 2 only
    bans absence inventories — the rewrite moves are all presence-form)."""
    allowed = [
        "Benchmarks are self-reported.",
        "Single-source, practitioner-reported.",
        "Dataset and evaluation tooling are public.",
        "Scored by an ensemble of LLM judges.",
        "An arXiv preprint reverse-engineers Claude Code.",
        # R-8 boundary note: a direction-note recommendation, not an
        # evidence inventory — explicitly allowed.
        "No action yet; watch the replication attempts.",
        # Quiet-day Currents intro microcopy (different speech act).
        "Nothing breaking below the fold.",
    ]
    for text in allowed:
        assert _find_absence_forms(text) == [], f"False positive on: {text!r}"


def test_absence_forms_merge_overlapping_matches():
    """'No code is public yet' hits two patterns; it must count once."""
    hits = _find_absence_forms("No code is public yet.")
    assert len(hits) == 1


def test_absence_forms_exempt_until_now_novelty_claims():
    """R-8 boundary (calibrated 2026-07-05): a negative-existential news
    claim resolved by 'until now' in the SAME sentence is a novelty
    assertion (the source's own 'first of its kind' claim), not an
    evidence inventory. First live-gated day misfire: the lint flagged
    the Office Comprehension Bench lede, which the verifier marked
    supported and rubric v0.2 holds up as a trust-flag pass story."""
    novelty = [
        # The observed misfire, verbatim (2026-07-04 staged issue).
        "No benchmark has tested language models on native Word, Excel, "
        "and PowerPoint files until now.",
        "No public dataset covered agent traces until now.",
    ]
    for text in novelty:
        assert _find_absence_forms(text) == [], f"False positive on: {text!r}"


def test_absence_forms_until_now_must_be_same_sentence():
    """The exemption must not leak across a sentence boundary — a bare
    absence-inventory followed by a new 'Until now...' sentence is still
    the banned trust-flag form."""
    still_banned = [
        "No code is public. Until now, adoption was manual.",
        "No independent replication yet. Until now the field waited.",
    ]
    for text in still_banned:
        assert _find_absence_forms(text), f"R-8 example not caught: {text!r}"


# ---------------------------------------------------------------------------
# R-9 headline opener checks
# ---------------------------------------------------------------------------

def test_banned_new_generic_opener():
    banned = [
        "A new benchmark finds pairwise scoring beats rubrics",
        "A new framework cuts inference costs in half",
        "A new tool traces agent runs end to end",
    ]
    fine = [
        "A 9-millisecond CPU model beats GPU baselines",  # identifying modifiers
        "A CUDA-free kernel matches cuBLAS throughput",
        "A quarter of benchmark tasks are unsolvable as written",  # load-bearing article
        "Anthropic ships a more honest flagship model",  # rung 1
        "The best AI systems score under 60% on real Office documents",  # semantic The
        "A newly disclosed CVE affects vLLM deployments",  # "newly" is not "new + generic noun"
    ]
    for h in banned:
        assert _BANNED_NEW_GENERIC_OPENER.match(h), f"Should be banned: {h!r}"
    for h in fine:
        assert not _BANNED_NEW_GENERIC_OPENER.match(h), f"False positive: {h!r}"


# ---------------------------------------------------------------------------
# End-to-end over a synthetic dataset dir
# ---------------------------------------------------------------------------

def _write_issue(tmp_path: Path, name: str, headlines: list[str], summary: str) -> Path:
    ds = tmp_path / name
    ds.mkdir()
    issue = {
        "date": name[:10],
        "pulse": {"name": "pulse", "stories": [
            {"story_id": "s_pulse", "headline": headlines[0], "summary": summary},
        ]},
        "sections": [
            {"name": "big_picture",
             "stories": [
                 {"story_id": f"s_{i}", "headline": h, "summary": "Fine prose."}
                 for i, h in enumerate(headlines[1:])
             ],
             "intro_lead": "Lead.", "intro_body": "Body."},
        ],
    }
    (ds / "issue.json").write_text(json.dumps(issue), encoding="utf-8")
    return ds


def test_lint_fails_on_post_ruling_violations(tmp_path):
    ds = _write_issue(
        tmp_path, "2026-07-05",
        headlines=[
            "A new benchmark finds something",        # banned opener + A-led
            "A tiny model does a thing",              # A-led
            "An open pipeline does another thing",    # A-led -> density 3 > 2
        ],
        summary="Great result. No code is public yet.",  # R-8 hit
    )
    res = eval_reading_experience_lint(ds)
    assert res.status == "fail" and not res.passed
    joined = " ".join(res.details["failures"])
    assert "R-8" in joined and "A new + generic noun" in joined and "density" in joined
    assert res.metric == 3.0  # 1 absence + 1 banned opener + 1 over-cap


def test_lint_passes_clean_post_ruling_issue(tmp_path):
    ds = _write_issue(
        tmp_path, "2026-07-05",
        headlines=[
            "Anthropic ships a more honest flagship model",
            "Character-level tricks bypass safety filters in most open models",
        ],
        summary="An arXiv preprint reports 48% harmful outputs, 3-judge-calibrated.",
    )
    res = eval_reading_experience_lint(ds)
    assert res.status == "pass" and res.passed and res.metric == 0.0


def test_lint_is_informational_before_effective_date(tmp_path):
    """Pre-ruling archive days keep their green bar: counts reported,
    never failed."""
    ds = _write_issue(
        tmp_path, "2026-06-01",
        headlines=["A new tool does things"],
        summary="No code is public yet.",
    )
    res = eval_reading_experience_lint(ds)
    assert res.status == "informational" and res.passed
    assert res.metric > 0  # violations are still counted and visible
