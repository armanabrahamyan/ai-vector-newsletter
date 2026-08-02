# evals/fixtures/reviewer-gate/ — Eval 9 fixture set

*Owner: Eval Engineer. STATUS: PROPOSAL, pending ratification (built 2026-08-02).*

This directory holds the labelled fixture set for **Eval 9 — reviewer-gate
calibration**, the eval that answers: *when the auto-publish gate's
editorial reviewer looks at a shaped issue, does it hold the ones that
should not ship unattended, and let through the ones that should?*

This is a companion to Eval 7 (`evals/fixtures/factual-accuracy/`), not a
replacement — Eval 7 calibrates the sentence-level factual verifier; Eval 9
calibrates the issue-level editorial reviewer that the Phase 2 auto-publish
gate (`src/gate.py`) reads before deciding `auto_merge` vs. `hold`.

## What's here

```
reviewer-gate/
  README.md            this file
  build_fixtures.py     generator — regenerates issues/ + cases.yaml from
                         the real archive; re-run after any Issue schema change
  cases.yaml             manifest: 42 cases, one row per fixture
  issues/<id>.json       the actual issue.json payload per case (Issue-schema-valid)
```

Every `issues/<id>.json` file validates against `src.models.Issue` — see
`build_fixtures.py`'s module docstring for why a generator script and not
42 hand-typed files.

## The 42 cases

| Category | Count | ground_truth_gate |
|---|---|---|
| `seeded_defect` | 25 (5 per class × 5 classes) | `hold` |
| `clean` | 15 | `publish` |
| `real_bug_replay` | 2 (FM-12, FM-13) | `hold` |

### The 5 defect classes

1. **`contradicted_claim`** — reuses the Eval 7 mutation taxonomy
   (numeric_substitution, entity_substitution, directional_inversion,
   headline_error). One false claim injected into a story's summary or
   headline, with a matching `verification.claims` entry (a `contradicted`
   `ClaimVerdict`) attached, exactly as `src/verify.py` would have written
   it. This makes each case a joint probe: gate.py's own deterministic
   `no_contradicted_claims` check should already hold on these; Eval 9
   additionally checks whether the reviewer's *independent editorial
   judgment* also catches it (defence in depth, the same pattern as FM-12's
   rank-prompt-fix + summarise cross-check).

2. **`voice_collapse`** — 3 stories per issue rewritten wholesale to
   hedge-heavy, direction-free, generic prose. No concrete claim, no
   direction, no warmth. Compresses FM-03/FM-10 (voice drift / direction
   erosion — normally a multi-week trend) into a single issue so an
   in-issue reviewer can catch it without a baseline.

3. **`absence_inventory_trust_flag`** — 3 stories per issue carry a banned
   R-8 absence-form sentence (see `evals/run_evals.py`
   `_ABSENCE_FORM_PATTERNS`) — describing what is ABSENT from the evidence
   ("no independent benchmarks... yet") rather than what is present. Every
   one of these sentences also independently fails Eval 8
   (`eval_reading_experience_lint`) on datasets dated ≥ 2026-07-04; Eval 9
   tests whether the reviewer treats the pattern as a trust problem on its
   own editorial terms, not just via the deterministic lint.

4. **`broken_closing_shape`** — 3 stories per issue end on the
   byte-identical closing sentence, zero variation. The pattern the
   regeneration-quality ruleset (commit `fc9291d`, close-form diversity)
   was built to prevent.

5. **`shape_integrity_routing_failure`** — adapts two real bugs:
   - **FM-12 style** (3 cases): a story sitting in Currents carries
     `signal: "act"` (Big Picture territory, per `summarise.py`'s own pill
     definition), stranded outside Big Picture.
   - **FM-13 style** (2 cases): Pulse carries `prior_coverage_ref` (a
     continuation) while a fresh (`prior_coverage_ref: None`) story sits
     elsewhere in the issue — the lead should be today's freshest anchor,
     not yesterday's re-airing.

### Severity-modelling assumption — THIS NEEDS RATIFICATION

Every seeded-defect fixture carries a `severity_model` label:

- **`single_instance_blocking`** (classes A and E): a single instance of
  the defect is modelled as `blocking`-severity — one blocking finding is
  enough to force `red` under the proposed threshold table
  (`config/review_thresholds.yaml`, now landed: "any blocking → red").
  Rationale: a factual contradiction (class A) or a structural
  shape/routing failure (class E) is not a matter of degree — the pipeline
  shipped the *wrong* thing, not a suboptimal one, so a single occurrence
  is treated the same way `gate.py`'s own contradicted-claim check treats
  a single contradiction (hard-blocking regardless of count).

- **`pattern_3x_major`** (classes B, C, D): the defect is seeded 3 times
  in one issue, modelled as `major`-severity findings, to cross the
  proposed "≥ 3 major → red" threshold cleanly — WITHOUT relying on an
  argument that a single voice-collapsed story, or a single banned
  trust-flag sentence, or a single repeated closing sentence, is
  individually `blocking`-severity. That argument is genuinely debatable
  (is one hedge-heavy paragraph really reputational exposure, or just a
  paragraph that needs a rewrite?); seeding the pattern 3× sidesteps the
  debate and tests the threshold table's OWN stated logic instead.

**Both assumptions are testable NOW against the real, landed
`config/review_thresholds.yaml`** (see the "Executed so far" section
below) — but whether "3 major findings in the same issue" is the right
bar for classes B/C/D (vs., say, requiring only 2, or accepting a single
`major` finding on the Pulse story specifically as its own signal) is an
editorial judgment call for Arman/Editor, not something this harness can
settle by itself. Flag this explicitly at ratification.

## Executed so far (no LLM credentials available in this build environment)

Full Eval 9 execution needs `src.review.run_review`'s LLM call (finding
generation from issue text), which requires `LLM_MODEL`/API credentials
not present in this environment. Two things WERE run for real, though,
once `src/review.py` and `config/review_thresholds.yaml` landed
concurrently with this fixture build:

1. **Threshold-computation consistency** (`_threshold_consistency_check`
   in `evals/run_evals.py`, runs automatically inside `eval_reviewer_gate`'s
   seam-mode report): feeds each fixture's `severity_model`-implied finding
   counts through the REAL `src.review.compute_verdict` +
   `config/review_thresholds.yaml` (version `v1.0-2026-08-02`). Result:
   **recall_hold_worthy = 1.0, precision_publish_safe = 1.0, 0 mismatches**
   across all 42 cases. This validates that the two severity-model
   assumptions above are compatible with the real threshold table as
   written — it does NOT validate that the LLM will actually produce those
   finding counts when it reads the fixture's issue text.

2. **Fail-soft integration smoke test**: one fixture (`rg_a1_...`) was
   copied into a throwaway `data/staging/2099-01-01/` directory (removed
   immediately after) and run through the real `src.review.run_review()`.
   With no `LLM_MODEL` configured, it returned `verdict: unavailable`
   exactly per its documented fail-soft contract — no crash, a
   `ReviewReport` written with `computed_verdict: unavailable`,
   `thresholds_version: v1.0-2026-08-02`. This confirms the
   `_REVIEWER_GATE_HOLD_VERDICTS` mapping (`unavailable` → hold) matches a
   real observed state, not just a documented one.

Neither of these is a substitute for the real recall/precision numbers
`eval_reviewer_gate(reviewer=<the real end-to-end reviewer>)` would produce
once an LLM is reachable. That execution is PENDING.

## Regenerating

```bash
python3 evals/fixtures/reviewer-gate/build_fixtures.py
```

Re-run after any change to `src.models.Issue` (a schema change could make
an existing fixture stop validating) or if the defect/clean base-day pool
should be rotated (see FM-02/FM-09 on fixture staleness — same discipline
applies here as to the dedup/ranking fixtures).
