"""
evals/fixtures/reviewer-gate/build_fixtures.py -- Eval 9 fixture generator.

Regenerates every fixture under evals/fixtures/reviewer-gate/issues/ and the
evals/fixtures/reviewer-gate/cases.yaml manifest from the REAL released
archive (data/released/<date>/issue.json), per the SCHEMA.md instruction
"do not hand-curate fixtures from web sources" -- these are hand-curated
MUTATIONS of real, already-ratified archive days, not invented content.

Why a generator script and not 42 hand-typed JSON files: every fixture must
validate against `src.models.Issue` (the Architect's pydantic contract).
Mutating a real, already-valid Issue and re-validating the result is far
less error-prone than hand-typing 42 nested JSON documents and hoping the
`pulse` cardinality invariant, the cluster-id pattern, the field lengths,
etc. all happen to line up. Re-run this script after a schema change to
confirm every fixture still parses; it will raise loudly if not.

Usage:
    python -m evals.fixtures.reviewer-gate.build_fixtures   # (not a package;
    run instead as:)
    python evals/fixtures/reviewer-gate/build_fixtures.py

Fixture taxonomy (see evals/fixtures/reviewer-gate/README.md for the full
rationale):

  25 seeded-defect cases, 5 per class:
    A. contradicted_claim              -- reuses the Eval 7 (factual-accuracy)
                                           mutation taxonomy; single-instance
                                           severity (a false claim is
                                           blocking-severity on its own).
    B. voice_collapse                  -- 3 stories per issue rewritten to
                                           hedge-heavy, direction-free prose;
                                           pattern-level severity (need >= 3
                                           major findings to cross the
                                           proposed red threshold without
                                           relying on a single-instance
                                           "blocking" judgment call).
    C. absence_inventory_trust_flag     -- 3 stories per issue carry a banned
                                           R-8 absence-form sentence
                                           (evals/run_evals.py
                                           _ABSENCE_FORM_PATTERNS); pattern-
                                           level severity, same reasoning as B.
    D. broken_closing_shape             -- 3 stories per issue end on the
                                           IDENTICAL templated closing
                                           sentence, zero variation; pattern-
                                           level severity, same reasoning.
    E. shape_integrity_routing_failure  -- adapts FM-12 (signal="act" story
                                           stranded in Currents) and FM-13
                                           (a continuation occupies Pulse
                                           while a fresh story sits
                                           elsewhere); single-instance
                                           severity (a structural shape
                                           failure, not a style nitpick).

  15 clean cases: real released issues, lightly modified (generated_at
  timestamp nudge + an optional benign synonym swap in an intro_body), never
  touching a factual claim, a headline, or section routing.

  2 real-bug replays: FM-12 (the actual 2026-05-24 archive, signal forced
  back to the documented "act" value) and FM-13 (hand-authored from the
  regression note's documented smoking-gun values, since the original
  2026-05-25 staging snapshot was overwritten by a later re-run of that
  date and no longer reproduces the bug shape on disk).

See evals/fixtures/reviewer-gate/README.md for the severity-modelling
assumptions (single-instance vs. pattern-level) that Arman is asked to
ratify or amend.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.models import Issue  # noqa: E402  (validation only -- read-only import)

FIXTURE_DIR = Path(__file__).resolve().parent
ISSUES_DIR = FIXTURE_DIR / "issues"
CASES_PATH = FIXTURE_DIR / "cases.yaml"
RELEASED_DIR = REPO_ROOT / "data" / "released"

ISSUES_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Base archive days.
# ---------------------------------------------------------------------------

# Defect-base days: recent, schema-mature issues (post the 2026-07-04
# reading-experience ruling and the schema_version=6 verification field),
# disjoint from the CLEAN_DATES pool below so no fixture is derived from the
# same source day as another fixture in this set.
DEFECT_BASE_DATES = [
    "2026-07-11",
    "2026-07-10",
    "2026-07-09",
    "2026-07-08",
    "2026-07-06",
]

# Clean-case days: a spread across the archive's early history, disjoint
# from DEFECT_BASE_DATES.
CLEAN_DATES = [
    "2026-05-23", "2026-05-24", "2026-05-25", "2026-05-26", "2026-05-27",
    "2026-05-28", "2026-05-29", "2026-05-30", "2026-05-31", "2026-06-01",
    "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05", "2026-06-22",
]


def _load_real_issue(date_str: str) -> dict:
    path = RELEASED_DIR / date_str / "issue.json"
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _validate(issue: dict, case_id: str) -> None:
    """Raise loudly if a mutated fixture does not validate as an Issue."""
    try:
        Issue.model_validate(issue)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Fixture {case_id!r} failed Issue validation: {exc}") from exc


def _write_issue(case_id: str, issue: dict) -> None:
    target = ISSUES_DIR / f"{case_id}.json"
    with target.open("w", encoding="utf-8") as fh:
        json.dump(issue, fh, indent=2, sort_keys=False)
        fh.write("\n")


def _all_stories(issue: dict) -> list[dict]:
    out = list(issue["pulse"]["stories"])
    for section in issue["sections"]:
        out.extend(section["stories"])
    return out


def _section_by_name(issue: dict, name: str) -> dict | None:
    for section in issue["sections"]:
        if section["name"] == name:
            return section
    return None


def _pulse_story(issue: dict) -> dict:
    return issue["pulse"]["stories"][0]


def _pick_target_stories(issue: dict, n: int) -> list[dict]:
    """Return up to n stories: pulse, first big_picture, first hands_on, ..."""
    candidates = [_pulse_story(issue)]
    bp = _section_by_name(issue, "big_picture")
    if bp and bp["stories"]:
        candidates.append(bp["stories"][0])
    ho = _section_by_name(issue, "hands_on")
    if ho and ho["stories"]:
        candidates.append(ho["stories"][0])
    cu = _section_by_name(issue, "currents")
    if cu and cu["stories"]:
        candidates.append(cu["stories"][0])
    return candidates[:n]


CASES: list[dict] = []  # manifest rows, written to cases.yaml


def _add_case(**kwargs: Any) -> None:
    CASES.append(kwargs)


# ---------------------------------------------------------------------------
# Class A -- contradicted_claim (reuses the Eval 7 mutation taxonomy).
# ---------------------------------------------------------------------------

ENTITY_SWAP = {
    "Anthropic": "OpenAI",
    "OpenAI": "Anthropic",
    "Google": "Meta",
    "Meta": "Google",
    "Microsoft": "Amazon",
    "Amazon": "Microsoft",
    "Hugging Face": "Cohere",
    "Mistral": "Cohere",
    "DeepMind": "OpenAI",
}

_MUTATION_A_PLAN = [
    "numeric_substitution",
    "entity_substitution",
    "directional_inversion",
    "headline_error",
    "headline_error",
]


def build_class_a() -> None:
    for i, (date_str, mutation_type) in enumerate(zip(DEFECT_BASE_DATES, _MUTATION_A_PLAN), start=1):
        issue = copy.deepcopy(_load_real_issue(date_str))
        case_id = f"rg_a{i}_{mutation_type}_{date_str}"

        if mutation_type == "headline_error":
            target = _pulse_story(issue)
            original_headline = target["headline"]
            swapped = None
            for k, v in ENTITY_SWAP.items():
                if k in original_headline:
                    swapped = original_headline.replace(k, v, 1)
                    break
            if swapped is None:
                swapped = f"OpenAI: {original_headline[0].lower()}{original_headline[1:]}"
            target["headline"] = swapped
            defect_span = swapped
            note = (
                f"Headline mutated from {original_headline!r} to {swapped!r} -- "
                "a wrong-actor attribution the reader sees before anything else. "
                "AI Vector headlines carry named actors (recognition rule); a "
                "misattributed actor in the headline is the most severe class "
                "of factual error per FM-14."
            )
            claim_location = "headline"
        else:
            # Target: first big_picture story (not the pulse, so headline_error
            # and the other three mutation types exercise different positions).
            bp = _section_by_name(issue, "big_picture")
            target = bp["stories"][0] if bp and bp["stories"] else _pulse_story(issue)
            if mutation_type == "numeric_substitution":
                injected = " Internal benchmarks reportedly showed a 42% latency reduction."
                contradiction = "the benchmark showed a 9% latency INCREASE, not a reduction"
            elif mutation_type == "entity_substitution":
                injected = " The capability was announced by OpenAI."
                contradiction = "the announcement came from Anthropic, not OpenAI"
            else:  # directional_inversion
                injected = " Developer adoption declined sharply in the week after launch."
                contradiction = "the source reports adoption grew sharply in the week after launch, the opposite of what is claimed"
            target["summary"] = (target["summary"].rstrip() + injected)[:1200]
            defect_span = injected.strip()
            note = (
                f"Appended claim {injected.strip()!r} directly contradicts the "
                f"(fabricated) source excerpt: {contradiction}."
            )
            claim_location = "body"

        # Attach a StoryVerification denormalisation matching the injected
        # contradiction, exactly as verify.py would have written it --
        # this makes the case a joint probe of gate.py's own deterministic
        # no_contradicted_claims check AND the reviewer's independent
        # editorial judgment (defence in depth, same pattern as FM-12's
        # rank-prompt-fix + summarise cross-check).
        target["verification"] = {
            "schema_version": 1,
            "story_id": target["story_id"],
            "prompt_version": "v1",
            "claims": [
                {
                    "schema_version": 1,
                    "claim": defect_span,
                    "verdict": "contradicted",
                    "location": claim_location,
                    "summary_span": defect_span,
                    "source_span": (
                        f"(fabricated source excerpt) {contradiction}"
                        if mutation_type != "headline_error"
                        else "(fabricated source excerpt) the actor named in the "
                        "headline does not match the source's byline"
                    ),
                    "note": "Eval 9 seeded defect -- see evals/fixtures/reviewer-gate/README.md",
                }
            ],
            "has_contradiction": True,
            "has_unsupported": False,
            "headline_flagged": mutation_type == "headline_error",
        }

        _validate(issue, case_id)
        _write_issue(case_id, issue)
        _add_case(
            id=case_id,
            category="seeded_defect",
            defect_class="contradicted_claim",
            mutation_type=mutation_type,
            severity_model="single_instance_blocking",
            ground_truth_gate="hold",
            defect_span=defect_span,
            story_id=target["story_id"],
            source_date=date_str,
            notes=note,
        )


# ---------------------------------------------------------------------------
# Class B -- voice_collapse.
# ---------------------------------------------------------------------------

_VOICE_COLLAPSE_TEMPLATES = [
    (
        "This development may have broad implications for the industry and "
        "could shape how teams think about their approach going forward. It "
        "remains to be seen exactly how things will play out, but "
        "stakeholders across the space are likely to be watching closely as "
        "more details become available in the coming weeks."
    ),
    (
        "There are a number of factors that could influence how this is "
        "received, and it is possible that different organisations will "
        "respond in different ways depending on their priorities. Whether "
        "this ultimately matters will likely depend on how things develop "
        "over time."
    ),
    (
        "This is one of several changes happening in the space right now, "
        "and it may end up being significant for some users. It is too "
        "early to say for certain what the broader impact will be, though "
        "some are already discussing the possibilities."
    ),
]


def build_class_b() -> None:
    for i, date_str in enumerate(DEFECT_BASE_DATES, start=1):
        issue = copy.deepcopy(_load_real_issue(date_str))
        case_id = f"rg_b{i}_voice_collapse_{date_str}"
        targets = _pick_target_stories(issue, 3)
        for story, template in zip(targets, _VOICE_COLLAPSE_TEMPLATES):
            story["summary"] = template
        primary_span = _VOICE_COLLAPSE_TEMPLATES[0]
        _validate(issue, case_id)
        _write_issue(case_id, issue)
        _add_case(
            id=case_id,
            category="seeded_defect",
            defect_class="voice_collapse",
            mutation_type=None,
            severity_model="pattern_3x_major",
            ground_truth_gate="hold",
            defect_span=primary_span,
            story_id=targets[0]["story_id"],
            source_date=date_str,
            notes=(
                f"{len(targets)} stories ({', '.join(s['story_id'] for s in targets)}) "
                "rewritten to hedge-heavy, direction-free prose -- no concrete "
                "claim, no direction, no warmth. Mirrors FM-03/FM-10 (voice "
                "drift / direction erosion), compressed into a single issue so "
                "an in-issue reviewer can detect it without a multi-week "
                "baseline."
            ),
        )


# ---------------------------------------------------------------------------
# Class C -- absence_inventory_trust_flag.
# ---------------------------------------------------------------------------

_ABSENCE_SENTENCES = [
    " No independent benchmarks have replicated the results yet.",
    " No public code has been released, and peer review is pending.",
    " There's no regulatory framework yet to govern this kind of deployment.",
]


def build_class_c() -> None:
    for i, date_str in enumerate(DEFECT_BASE_DATES, start=1):
        issue = copy.deepcopy(_load_real_issue(date_str))
        case_id = f"rg_c{i}_absence_trust_flag_{date_str}"
        targets = _pick_target_stories(issue, 3)
        for story, sentence in zip(targets, _ABSENCE_SENTENCES):
            story["summary"] = (story["summary"].rstrip() + sentence)[:1200]
        primary_span = _ABSENCE_SENTENCES[0].strip()
        _validate(issue, case_id)
        _write_issue(case_id, issue)
        _add_case(
            id=case_id,
            category="seeded_defect",
            defect_class="absence_inventory_trust_flag",
            mutation_type=None,
            severity_model="pattern_3x_major",
            ground_truth_gate="hold",
            defect_span=primary_span,
            story_id=targets[0]["story_id"],
            source_date=date_str,
            notes=(
                f"{len(targets)} stories carry a banned R-8 absence-form trust "
                "flag (evals/run_evals.py _ABSENCE_FORM_PATTERNS) -- describing "
                "what is ABSENT from the evidence rather than what is present. "
                "Every one of these sentences also independently fails Eval 8 "
                "(eval_reading_experience_lint) on datasets dated >= 2026-07-04; "
                "Eval 9 tests whether the EDITORIAL reviewer also catches the "
                "pattern as a trust problem, not just the deterministic lint."
            ),
        )


# ---------------------------------------------------------------------------
# Class D -- broken_closing_shape.
# ---------------------------------------------------------------------------

_IDENTICAL_CLOSE = " This is one to watch."


def build_class_d() -> None:
    for i, date_str in enumerate(DEFECT_BASE_DATES, start=1):
        issue = copy.deepcopy(_load_real_issue(date_str))
        case_id = f"rg_d{i}_broken_closing_{date_str}"
        targets = _pick_target_stories(issue, 3)
        for story in targets:
            base = story["summary"].rstrip()
            # Strip a trailing sentence-ending punctuation run so the
            # identical close reads naturally, then append it verbatim.
            base = re.sub(r"[.!?]+$", "", base)
            story["summary"] = (base + "." + _IDENTICAL_CLOSE)[:1200]
        _validate(issue, case_id)
        _write_issue(case_id, issue)
        _add_case(
            id=case_id,
            category="seeded_defect",
            defect_class="broken_closing_shape",
            mutation_type=None,
            severity_model="pattern_3x_major",
            ground_truth_gate="hold",
            defect_span=_IDENTICAL_CLOSE.strip(),
            story_id=targets[0]["story_id"],
            source_date=date_str,
            notes=(
                f"{len(targets)} stories ({', '.join(s['story_id'] for s in targets)}) "
                f"end on the byte-identical closing sentence {_IDENTICAL_CLOSE.strip()!r} "
                "with zero grammatical variation -- exactly the pattern the "
                "regeneration-quality ruleset (commit fc9291d, close-form "
                "diversity) was built to prevent. A single repeat could be "
                "coincidence; three in one issue is a mechanical template."
            ),
        )


# ---------------------------------------------------------------------------
# Class E -- shape_integrity_routing_failure (adapts FM-12 and FM-13).
# ---------------------------------------------------------------------------

def build_class_e() -> None:
    # Sub-type 1 (FM-12-style): 3 cases -- a story sitting in `currents`
    # carries signal="act" (Big Picture territory per summarise.py's own
    # pill definition), stranded outside Big Picture.
    for i, date_str in enumerate(DEFECT_BASE_DATES[:3], start=1):
        issue = copy.deepcopy(_load_real_issue(date_str))
        case_id = f"rg_e{i}_fm12_signal_mismatch_{date_str}"
        currents = _section_by_name(issue, "currents")
        target = currents["stories"][0]
        target["signal"] = "act"
        _validate(issue, case_id)
        _write_issue(case_id, issue)
        _add_case(
            id=case_id,
            category="seeded_defect",
            defect_class="shape_integrity_routing_failure",
            mutation_type="fm12_signal_section_mismatch",
            severity_model="single_instance_blocking",
            ground_truth_gate="hold",
            defect_span=target["headline"],
            story_id=target["story_id"],
            source_date=date_str,
            notes=(
                "FM-12 shape: signal=\"act\" (summarise.py's own definition: "
                "\"vendor / contract / architecture decision worth making this "
                "quarter\" -- explicitly Big Picture territory) on a story "
                "sitting in Currents, not Big Picture. A structural routing "
                "failure, not a style nitpick -- single instance is "
                "blocking-severity by the same logic as a contradicted claim: "
                "the pipeline shipped the wrong shape, not a suboptimal one."
            ),
        )

    # Sub-type 2 (FM-13-style): 2 cases -- Pulse carries prior_coverage_ref
    # (a continuation) while a fresh (prior_coverage_ref=None) story sits
    # elsewhere in the issue.
    for i, date_str in enumerate(DEFECT_BASE_DATES[3:], start=4):
        issue = copy.deepcopy(_load_real_issue(date_str))
        case_id = f"rg_e{i}_fm13_continuation_as_pulse_{date_str}"
        pulse = _pulse_story(issue)
        pulse["prior_coverage_ref"] = "c_deadbeefcafe01"
        # Confirm at least one fresh story exists elsewhere (true for every
        # base issue in this archive; assert defensively so a future base-day
        # swap fails loudly instead of silently producing a vacuous fixture).
        others = [s for s in _all_stories(issue) if s["story_id"] != pulse["story_id"]]
        assert any(s.get("prior_coverage_ref") is None for s in others), (
            f"{date_str}: no fresh (non-continuation) story available elsewhere "
            "in the issue -- FM-13 shape requires one; pick a different base day."
        )
        _validate(issue, case_id)
        _write_issue(case_id, issue)
        _add_case(
            id=case_id,
            category="seeded_defect",
            defect_class="shape_integrity_routing_failure",
            mutation_type="fm13_continuation_as_pulse",
            severity_model="single_instance_blocking",
            ground_truth_gate="hold",
            defect_span=pulse["headline"],
            story_id=pulse["story_id"],
            source_date=date_str,
            notes=(
                "FM-13 shape: Pulse carries prior_coverage_ref (a continuation "
                "of a previously covered story) while at least one fresh "
                "(prior_coverage_ref=None) story sits elsewhere in the issue. "
                "The lead should be today's freshest anchor, not yesterday's "
                "re-airing; single instance is blocking-severity for the same "
                "reason as sub-type 1."
            ),
        )


# ---------------------------------------------------------------------------
# Clean cases -- real released issues, lightly modified.
# ---------------------------------------------------------------------------

_SYNONYM_SWAPS = {
    "shows": "demonstrates",
    "helps": "assists",
    "builds": "constructs",
    "shifts": "moves",
}


def build_clean_cases() -> None:
    for i, date_str in enumerate(CLEAN_DATES, start=1):
        issue = copy.deepcopy(_load_real_issue(date_str))
        case_id = f"rg_clean_{i:02d}_{date_str}"

        # Light edit 1: nudge generated_at by 90 seconds. Purely a
        # provenance timestamp; touches no editorial content.
        gen_at = datetime.fromisoformat(issue["generated_at"].replace("Z", "+00:00"))
        issue["generated_at"] = (gen_at + timedelta(seconds=90)).isoformat().replace("+00:00", "Z")

        # Light edit 2 (when available): a single benign synonym swap in an
        # intro_body -- meaning-preserving, touches no fact, no headline, no
        # section routing. Only present on Phase-B issues with intro fields.
        swapped_note = "no intro_body present on this issue -- edit 2 skipped"
        for section in [issue["pulse"], *issue["sections"]]:
            body = section.get("intro_body")
            if not body:
                continue
            for old, new in _SYNONYM_SWAPS.items():
                if old in body:
                    section["intro_body"] = body.replace(old, new, 1)
                    swapped_note = f"synonym swap {old!r} -> {new!r} in {section['name']} intro_body"
                    break
            if swapped_note.startswith("synonym"):
                break

        _validate(issue, case_id)
        _write_issue(case_id, issue)
        _add_case(
            id=case_id,
            category="clean",
            defect_class=None,
            mutation_type=None,
            severity_model=None,
            ground_truth_gate="publish",
            defect_span=None,
            story_id=None,
            source_date=date_str,
            notes=(
                f"Real ratified issue, lightly modified: generated_at +90s; {swapped_note}."
            ),
        )


# ---------------------------------------------------------------------------
# Real-bug replays -- FM-12 (real archive) and FM-13 (hand-authored replay).
# ---------------------------------------------------------------------------

def build_replay_fm12() -> None:
    """FM-12 real replay: the 2026-05-24 released archive already contains
    the documented smoking-gun story (c_78dcc648119217a1); the archived
    signal field currently reads "discuss" (per the FM-12 note's own
    mitigation-section caveat: the cross-check safety net did not fire on
    the original case because the body-grounded signal came back as
    "discuss" not "act"). This replay forces signal back to the documented
    "act" value from the regression note's smoking-gun paragraph so the
    fixture faithfully reproduces the FAILURE MODE the note describes
    (signal=act stranded outside Big Picture), not the post-hoc archive
    value. See evals/fixtures/_regressions/c_78dcc648119217a1_signal_section_mismatch.md.
    """
    issue = copy.deepcopy(_load_real_issue("2026-05-24"))
    case_id = "rg_replay_fm12_2026-05-24"
    target = None
    for story in _all_stories(issue):
        if story["story_id"] == "c_78dcc648119217a1":
            target = story
            break
    assert target is not None, "c_78dcc648119217a1 not found in 2026-05-24 archive"
    archived_signal = target.get("signal")
    target["signal"] = "act"
    _validate(issue, case_id)
    _write_issue(case_id, issue)
    _add_case(
        id=case_id,
        category="real_bug_replay",
        defect_class="shape_integrity_routing_failure",
        mutation_type="fm12_signal_section_mismatch",
        severity_model="single_instance_blocking",
        ground_truth_gate="hold",
        defect_span=target["headline"],
        story_id=target["story_id"],
        source_date="2026-05-24",
        source_bug="FM-12",
        notes=(
            "Real replay of the escaped 2026-05-24 bug (task #75; regression "
            "note evals/fixtures/_regressions/c_78dcc648119217a1_signal_section_mismatch.md). "
            f"Archived signal value was {archived_signal!r}; forced to \"act\" "
            "here to reproduce the documented failure mode faithfully -- see "
            "the docstring on build_replay_fm12() for why the two differ. "
            "The rest of the issue (all other stories, section membership) is "
            "the real ratified 2026-05-24 archive, unmodified."
        ),
    )


def build_replay_fm13() -> None:
    """FM-13 real replay: the 2026-05-25 STAGING bug never released (caught
    same day, tasks #81 + #82). The original data/staging/2026-05-25/
    snapshot on disk today no longer reproduces the bug shape -- that date
    was re-run since, and a re-run of an old date against live feeds does
    not reproduce the same fetch result. This replay is hand-authored
    directly from the regression note's documented values (cluster ids,
    headline, scores) rather than copied from a currently-nonexistent
    on-disk snapshot. See
    evals/fixtures/_regressions/2026-05-25_continuation_as_pulse.md.
    """
    case_id = "rg_replay_fm13_2026-05-25"
    issue = {
        "schema_version": 6,
        "issue_number": None,
        "revision": 0,
        "date": "2026-05-25",
        "pulse": {
            "schema_version": 3,
            "name": "pulse",
            "stories": [
                {
                    "schema_version": 3,
                    "story_id": "c_2e53967d020fb800",
                    "headline": (
                        "How I use llama.cpp's native web_fetch RAG tools "
                        "directly from the llama-server webui"
                    ),
                    "summary": (
                        "A practitioner walkthrough of llama.cpp's new native "
                        "web_fetch tool for retrieval-augmented generation, run "
                        "directly from the llama-server webui without any extra "
                        "scaffolding. Useful, but it is a how-to layered on top "
                        "of yesterday's native-tools announcement, not new "
                        "signal in its own right."
                    ),
                    "source_urls": ["https://example.com/llamacpp-webui-rag-howto"],
                    "prior_coverage_ref": "c_cf0b99c06c42a9ba",
                    "signal": "try",
                }
            ],
            "intro_lead": None,
            "intro_body": None,
        },
        "sections": [
            {"schema_version": 3, "name": "big_picture", "stories": [], "intro_lead": None, "intro_body": None},
            {"schema_version": 3, "name": "hands_on", "stories": [], "intro_lead": None, "intro_body": None},
            {
                "schema_version": 3,
                "name": "currents",
                "stories": [
                    {
                        "schema_version": 3,
                        "story_id": "c_78dabe7884f76ef8",
                        "headline": "Hugging Face ships a new open benchmark tracker for local models",
                        "summary": (
                            "Hugging Face's new benchmark tracker gives local-model "
                            "builders a single, continuously updated view of "
                            "leaderboard movement across open-weight releases -- "
                            "fresh signal with no prior coverage."
                        ),
                        "source_urls": ["https://example.com/hf-benchmark-tracker"],
                        "prior_coverage_ref": None,
                        "signal": "watch",
                    }
                ],
                "intro_lead": None,
                "intro_body": None,
            },
        ],
        "generated_at": "2026-05-25T21:10:00Z",
        "prompt_versions": {"rank": "v0.1", "summarise": "v0.9"},
        "notes": "",
    }
    _validate(issue, case_id)
    _write_issue(case_id, issue)
    _add_case(
        id=case_id,
        category="real_bug_replay",
        defect_class="shape_integrity_routing_failure",
        mutation_type="fm13_continuation_as_pulse",
        severity_model="single_instance_blocking",
        ground_truth_gate="hold",
        defect_span=issue["pulse"]["stories"][0]["headline"],
        story_id="c_2e53967d020fb800",
        source_date="2026-05-25",
        source_bug="FM-13",
        notes=(
            "Hand-authored replay of the caught-in-staging 2026-05-25 bug "
            "(tasks #81 + #82; regression note "
            "evals/fixtures/_regressions/2026-05-25_continuation_as_pulse.md). "
            "The original staging snapshot for this date no longer reproduces "
            "the bug shape (see docstring on build_replay_fm13()); this "
            "fixture reconstructs it directly from the note's documented "
            "cluster ids, headline, and prior_coverage_ref value."
        ),
    )


def main() -> None:
    build_class_a()
    build_class_b()
    build_class_c()
    build_class_d()
    build_class_e()
    build_clean_cases()
    build_replay_fm12()
    build_replay_fm13()

    seeded = sum(1 for c in CASES if c["category"] == "seeded_defect")
    clean = sum(1 for c in CASES if c["category"] == "clean")
    replay = sum(1 for c in CASES if c["category"] == "real_bug_replay")
    print(f"Built {len(CASES)} cases: {seeded} seeded_defect, {clean} clean, {replay} real_bug_replay")

    try:
        import yaml
    except ImportError:
        print("PyYAML not available -- writing cases.json instead of cases.yaml")
        with (FIXTURE_DIR / "cases.json").open("w", encoding="utf-8") as fh:
            json.dump({"cases": CASES}, fh, indent=2)
        return

    with CASES_PATH.open("w", encoding="utf-8") as fh:
        fh.write(
            "# evals/fixtures/reviewer-gate/cases.yaml -- Eval 9 manifest.\n"
            "# Generated by build_fixtures.py. Do not hand-edit; re-run the\n"
            "# generator and diff instead, so the issue JSON under issues/\n"
            "# always matches this manifest.\n\n"
        )
        yaml.safe_dump({"cases": CASES}, fh, sort_keys=False, allow_unicode=True, width=100)


if __name__ == "__main__":
    main()
