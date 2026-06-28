# evals/failure_modes.md — AI Vector Failure Modes Registry

*Owner: Eval Engineer. Updated after every incident and weekly drift review.*
*Format: each entry gets a name, detection signal, mitigation, severity, and last occurrence.*
*Postmortems file their findings here. This is a living document.*

---

## How to add an entry

When an incident happens or a new failure mode is identified:

1. Add or update the entry below.
2. Note the detection signal (what metric or pattern surfaced it).
3. Fill in `last_occurrence` (ISO date or "never" if not yet observed).
4. Set severity: **low** (nuisance, can be addressed next sprint) /
   **medium** (degrades quality, should be addressed this week) /
   **high** (blocks publication or materially corrupts the corpus).

This is evidence, not blame. The goal is to notice the failure mode
earlier next time.

---

## Risk-register failures (seeded from TEAM.md §Risk Register)

### FM-01: Source rot

**What it is.** A feed stops publishing, starts returning errors, or goes
silently stale, and the pipeline doesn't notice — it just stops pulling
items from that source without alerting anyone.

**Detection signal.**
- `source_health.json` shows `fired: false` for the same source for ≥3
  consecutive days.
- `items_kept` drops to 0 for a source that previously contributed > 3
  items/day on average.
- Trust-weight decay in `sources.yaml` hasn't been triggered despite the gap.

**Eval mechanism.** Module-integrity eval (`eval_module_integrity`) checks
`source_health.json` on every run. Drift detection additionally watches
per-source item counts over the rolling 14-day window.

**Mitigation.**
1. Source Engineer re-probes the feed URL immediately.
2. If the feed URL has changed: update `sources.yaml` in the same PR.
3. If the source is structurally dead (no RSS, site gone): disable with
   `enabled: false` and a dated note; flag to Editor if the source was
   high-trust-weight (≥4).
4. If the community-proxy feed (Olshansk/rss-feeds, taobojlen) went stale:
   check the upstream GitHub project; consider maintaining a direct scraper
   (isolated module, ToS-checked) as fallback.

**Severity.** Medium for a low-trust source; high for a trust-weight-4 source
(Hugging Face, Import AI, Simon Willison, Lilian Weng).

**Last occurrence.** Never (as of 2026-05-23 — first entry).

---

### FM-02: Eval gaming

**What it is.** The team optimises against the fixed fixtures rather than
against real reader value. Spearman correlation looks good on fixtures;
Arman's ratification rate drops over months. The fixtures become the
publication's hidden editorial filter rather than a proxy for it.

**Detection signal.**
- Spearman rho trending up on fixtures while Arman's ratification rate
  (stories kept / stories in issue.json draft) trends down week-over-week.
- "Arman cut reasons" in `labels.yaml` accumulate new reasons not captured
  in the fixture set (the fixture set is no longer representative).
- LLM Engineer asks Eval Engineer for the exact fixture IDs before a
  ranking prompt change (treat as a red flag).

**Eval mechanism.** Behavioural integrity weekly note tracks Arman's
ratification patterns. Eval Engineer rotates fixtures quarterly using fresh
archive days — the fixture set is not the same every week.

**Mitigation.**
1. Rotate at least one fixture dataset per quarter with a fresh archive day.
2. Treat ratified issues as the growing positive corpus — voice and ranking
   evals run against the full ratified archive, not just the small fixture set.
3. When Arman cuts a story, log the cut reason in `labels.yaml`
   (`arman_cut_reasons`). If a new cut reason appears that has no fixture
   coverage, add a fixture that exercises it.

**Severity.** High — if undetected, the publication slowly stops being
what it was designed to be.

**Last occurrence.** Never (as of 2026-05-23).

---

### FM-03: Voice drift

**What it is.** The publication gradually sounds less like Arman. Summaries
are technically correct and on-topic but the warmth, specificity, direction-
giving, and wit erode issue by issue. Individually each issue looks fine;
the trend is visible only over weeks.

**Detection signal.**
- Voice adherence score (Eval 3) trends down over 3+ consecutive weeks
  without a documented prompt change.
- Editor's weekly voice labels show increasing `borderline` and `off-voice`
  rates.
- Arman's `arman_cut_reasons` include `"off-voice"` more than once per week
  on average (once is normal; weekly pattern is a signal).
- Specific symptom: `direction_landed: false` appearing in per-issue labels
  more than once in a rolling 14-day window (summaries listing without pointing).

**Eval mechanism.** Voice adherence eval (Eval 3, stub until Phase 2)
tracks per-issue score against 14-day baseline. Drift detection (Eval 5)
monitors the rolling z-score.

**Mitigation.**
1. Bring the drift report to the weekly voice review. Do not wait.
2. LLM Engineer and Editor jointly identify which prompt or rubric weight
   changed (or which model endpoint silently shifted — see FM-06).
3. If no code change explains it: suspect FM-06 (prompt drift under the team).
4. Re-anchor the voice rubric against the last 10 ratified issues (the positive
   corpus). At least 3 of those issues should score 4+ on the rubric; if not,
   the rubric itself needs calibration.

**Severity.** High — voice is the product, not the plumbing.

**Last occurrence.** Never (as of 2026-05-23).

---

### FM-04: Contract drift

**What it is.** A pydantic shape changes in `src/models.py` without a
corresponding update to `docs/DESIGN.md` and without bumping `schema_version`.
Downstream readers break silently or (worse) parse corrupt data successfully.

**Detection signal.**
- Module integrity eval (`eval_module_integrity`) fails on an archive day
  that was written by a different pipeline version.
- `schema_version` in a `data/YYYY-MM-DD/*.jsonl` record does not match what
  `src/models.py` expects.
- Architect is not listed as a reviewer on a merged PR that touched `models.py`.

**Eval mechanism.** Module integrity eval validates every artifact against
pydantic models on every run. Any shape mismatch surfaces immediately.

**Mitigation.**
1. The Architect is the required reviewer on any PR touching `src/models.py`.
   This is a process guardrail; if a PR merged without Architect review, the
   postmortem asks how.
2. `schema_version` must be bumped whenever the shape changes; the diff is
   recorded in DESIGN.md's schema changelog.
3. If a backward-compat read path is needed (old archive still parseable),
   the migration note in DESIGN.md specifies it and Eval Engineer adds a
   fixture for the old shape.

**Severity.** High — archive corruption is hard to repair retroactively.

**Last occurrence.** Never (as of 2026-05-23).

---

### FM-05: Archive corruption

**What it is.** A half-written JSONL file becomes the canonical file (the
`.tmp` + fsync + rename pattern was skipped or crashed at the wrong moment).
Downstream readers load partial data, possibly silently producing wrong
clusters or rankings for the day.

**Detection signal.**
- Module integrity eval finds a JSONL file that fails JSON-parse on any line.
- A `.tmp` file exists alongside a canonical file for more than one pipeline
  run (indicates a crash mid-write).
- `items_kept` in `source_health.json` is nonzero but `items.jsonl` has
  fewer records than expected.

**Eval mechanism.** `eval_module_integrity` raises on any malformed JSONL
line (intentionally does not silently skip). Watching for `.tmp` remnants is
a daily-run health check the module-integrity eval can add (TODO: Phase 2).

**Mitigation.**
1. Every pipeline writer must use the `.tmp` + fsync + rename pattern
   documented in DESIGN.md. No exceptions.
2. If a `.tmp` file is found: do not rename it manually. Re-run the pipeline
   for that day (`python -m src.run --date YYYY-MM-DD`) — the idempotent
   re-run overwrites it cleanly.
3. If a canonical JSONL is corrupt and the `.tmp` is also gone: the day is
   unrecoverable from the archive alone. If the run was recent enough, check
   CI logs for the raw pipeline output.

**Severity.** High — data loss; the archive is the publication's corpus.

**Last occurrence.** Never (as of 2026-05-23).

---

### FM-06: Prompt drift under the team

**What it is.** The model endpoint (LiteLLM, Bedrock, Anthropic direct)
silently shifts behaviour — a model version update, a provider-side change,
or a temperature/system-prompt tweak that wasn't logged. The prompts in
the repo haven't changed, but the outputs have.

**Detection signal.**
- Voice adherence score (Eval 3) drops materially with no code change in
  the surrounding week.
- Ranking Spearman (Eval 2) shifts more than 0.1 rho without a prompt
  version bump in `RankedStory.prompt_version`.
- `prompt_version` in `ranked.jsonl` hasn't changed, but the score
  distribution has shifted (compare rolling percentile histograms).
- Identical re-run on same day produces materially different scores
  (temperature set too high, or model is genuinely non-deterministic under
  this provider).

**Eval mechanism.** Eval 3 (voice) and Eval 2 (Spearman) both run against
the historical baseline; z-score drift is the primary signal. Additionally,
Eval Engineer can maintain a "canary cluster" — one fixed item embedded in
the fixtures — and track its score over time. A canary score shift without
a prompt version change is a strong prompt-drift signal.

**Mitigation.**
1. LLM Engineer bumps `prompt_version` in `RankedStory` and `Issue.prompt_versions`
   on every non-trivial prompt change. This is the audit trail.
2. If drift is detected with no version bump: compare `LLM_MODEL` env var
   in CI logs this week vs. last week. Check provider release notes.
3. If provider silently changed a default: pin the model version in the
   `LLM_MODEL` env var (e.g. `claude-opus-4-7` not `claude-opus-latest`).
4. Consider running the ranking eval on a fixed set of clusters (from the
   synthetic fixture) on every CI run as a cheaply-detectable canary.

**Severity.** Medium initially; high if undetected for > 2 weeks (corpus
corrupted).

**Last occurrence.** Never (as of 2026-05-23).

---

### FM-07: §7 platform blockers (GitHub Actions / egress / Pages)

**What it is.** Internal bank GitHub does not have `schedule:` triggers
enabled, or outbound egress to RSS/API sources or the LLM endpoint is
blocked, or GitHub Pages is unavailable. The pipeline can't run on schedule
and/or can't publish.

**Detection signal.**
- No `data/YYYY-MM-DD/` directory created for a given day when the cron
  was expected to run.
- Actions workflow exits with a network error against a source URL or the
  LLM endpoint.
- `docs/` is not updated after a ratified issue.

**Eval mechanism.** This is primarily a Release Engineer and Architect
concern. Eval Engineer surfaces it in the behavioural integrity weekly note
if the daily run hasn't produced an archive day in > 2 days.

**Mitigation.**
Per TEAM.md §Day-one validation: Release Engineer drafts the three asks;
Arman sends them; Architect records the answers. Pipeline build-out is
explicitly gated on workarounds being identified. Workarounds:
- `schedule:` blocked → `workflow_dispatch` + external nudge (calendar
  reminder or Arman manually triggers).
- Egress blocked → fetch from an approved network; archive committed
  manually into `data/`.
- Pages blocked → identify substitute publish surface before investing
  in the render layer.

**Severity.** High — blocks the publication entirely if unresolved.

**Last occurrence.** Never (as of 2026-05-23 — platform validation TBD).

---

## Additional failure modes (Eval Engineer additions beyond TEAM.md)

### FM-08: Finance-lens overuse (lens becomes a quota)

**What it is.** The finance angle is forced onto stories that don't earn it.
Over weeks, every story in the issue carries a `finance_angle` note — even
Tier-1 purely-technical stories — because the LLM has been prompted to always
find a finance hook. The publication starts reading like a FS newsletter that
mentions AI, rather than an AI newsletter with a finance eye.

**Detection signal.**
- `finance_angle` is set on > 80% of `SummaryBlock` records in ranked.jsonl
  over a rolling 7-day window (healthy baseline: ~40–60% on a normal day).
- Editor's voice labels show `finance-angle-forced` in `arman_cut_reasons`
  more than once per week.
- The `financial_services_impact` rubric criterion is consistently scoring
  ≥75 even for stories with no FS-specific hook (compare against human
  labels on fixture stories known to have no FS angle).

**Eval mechanism.** Drift detection (Eval 5, stub) will track finance-lens
presence rate over the 14-day window. Until then: Eval Engineer's manual
check during weekly voice review.

**Mitigation.**
1. Remind LLM Engineer: `financial_services_impact` at 15 weight is
   intentionally moderate. "Most days, most stories land at 25–50."
2. If `finance_angle` is being set mechanically: add a fixture where
   the correct label is `finance_angle: null` and the rubric score for
   `financial_services_impact` should be 25 or below.
3. Check the prompt for language like "always consider the finance angle"
   or "add a finance note to each story" — remove it.

**Severity.** Medium — erodes the editorial identity over weeks; not an
acute failure but a slow-motion one.

**Last occurrence.** Never (as of 2026-05-23).

---

### FM-09: Rubric overfitting on fixtures

**What it is.** Ranking prompt and rubric weights are tuned so tightly
against the fixture set that they score the fixture clusters well but fail
on live data. Spearman rho on fixtures is excellent; stories Arman keeps vs.
cuts in practice diverge from what the rubric predicts.

**Detection signal.**
- Spearman rho on fixtures ≥ 0.80 but Arman's ratification patterns
  (logged in `per_issue.arman_cut_reasons`) show "low-relevance" or
  "off-voice" on stories the rubric ranked highly.
- Fixture clusters remain unchanged for > 3 months while prompt tuning
  is actively happening (stale fixtures are the proximate cause).

**Eval mechanism.** Behavioural integrity note tracks the ratio
"Spearman on fixtures / Arman's in-practice cut rate." If the former
rises while the latter also rises, overfitting is likely. Fixture rotation
(quarterly minimum) is the structural mitigation.

**Mitigation.**
1. Rotate at least one fixture dataset per quarter (fresh archive days).
2. Use the growing ratified archive as a second test corpus: Spearman
   should hold against recently-ratified issues, not just the fixture set.
3. If Arman's cut rate is rising: convene a voice review session. Do not
   just re-tune the rubric to match his cuts without understanding them.

**Severity.** Medium — degrades usefulness of the eval harness as a signal.

**Last occurrence.** Never (as of 2026-05-23).

---

### FM-10: Voice softening (direction erosion)

**What it is.** Summaries gradually become more hedged, more neutral, more
"here are the things that happened" and less "here is where the field moved
and why it matters to you." The editorial DNA — "a vector has direction" —
erodes. This is a sub-case of FM-03 (voice drift) but specifically about
the direction-giving property, not just warmth or signal density.

**Detection signal.**
- `direction_landed: false` in per-issue labels more than once in a
  rolling 14-day window.
- `direction_note` field in `SummaryBlock` is consistently empty or
  formulaic ("This will shape future development.") across multiple
  consecutive issues.
- The Pulse section reads like an event summary rather than a point of view.

**Eval mechanism.** Voice rubric (Eval 3, stub) will include a
"direction" sub-score. Until then: Editor flags in weekly labels.

**Mitigation.**
1. LLM Engineer revisits the `summarise.py` prompt for the "direction note"
   instruction. The prompt should require a concrete direction statement
   ("by next quarter, X teams should..."), not accept placeholder language.
2. Add a fixture with a deliberately weak direction note and a failing label;
   add a contrasting fixture with a strong direction note and a passing label.
   These become calibration examples for the prompt.
3. Editor uses the EDITORIAL.md examples section to document an off-direction
   and an on-direction example side by side.

**Severity.** High — direction is the product's core promise ("All of it,
sorted for you" implies sorting by what matters and where it points).

**Last occurrence.** Never (as of 2026-05-23).

---

### FM-11: Cross-time dedup threshold miscalibration

**What it is.** The cosine threshold for `cross_time_ref` is set too low
(false continuations — today's story is flagged as a continuation of a
vaguely similar story from 10 days ago) or too high (real continuations
missed — the same story is re-reported without a callback because the
clusterer didn't connect them).

**Detection signal.**
- Too low: stories in `ranked.jsonl` with `cross_time_ref` set but no
  meaningful relationship to the referenced cluster (reader notices "why
  is this a callback?").
- Too high: Arman notes "we already covered this" in cut reasons, but
  `cross_time_ref` is null on the cluster (the chain wasn't detected).
- Eval: dedup recall on cross-time fixture pairs drops below 0.70, or
  precision on non-continuation pairs drops below 0.85.

**Eval mechanism.** Dedup eval (Eval 1, stub) will cover cross-time pairs
explicitly once a cross-time fixture dataset exists. Until then: manual
check at weekly drift review.

**Mitigation.**
1. Retrieval Engineer tunes both thresholds (same-day ~0.82, cross-time
   ~0.85) against the first cross-time fixture pair once it lands.
2. Add at least one "false continuation" near-miss to fixtures
   (topically similar stories from different story chains; should NOT
   get a `cross_time_ref`).
3. Tuned thresholds are recorded in DESIGN.md's embedding model section.

**Severity.** Medium — readers notice recycled stories or missed callbacks;
damages trust in the curation.

**Last occurrence.** Never (as of 2026-05-23).

---

### FM-12: Signal / audience-tag mismatch evicts Big Picture stories

**What it is.** The rank LLM (working from titles + raw_summary) under-
tags workflow / governance / decision-process shifts as `hands_on` only,
missing the senior-leader angle. The per-story summarise LLM (working
from the article body) then assigns `signal: "act"` -- the editorial
verdict pill explicitly defined as "Big Picture territory". The two
labels contradict. `_pick_big_picture` in `src/summarise.py` routes
strictly on `audience_tags`, so the story is evicted to On the Radar
even though the body-grounded signal said Big Picture.

The smoking gun: `c_78dcc648119217a1` ("Spec-driven development is the
new way", 2026-05-24). `audience_tags=["hands_on", "general"]`,
`big_picture_relevance=30` (anchor 25 = "tangential"), `signal="act"`.
Routed to On the Radar; should have been Big Picture.

**Detection signal.**
- Any cluster where `signal == "act"` and `big_picture not in
  audience_tags` in `ranked.jsonl` (rank-side undertag uncorrected).
- The `signal=act forced big_picture tag for ...` log line firing more
  than ~once per issue -- means the rank prompt is systematically missing
  workflow/governance/decision-process stories and Fix 1 needs revision.
- Tier disagreements in `aiv eval` accumulating on workflow/governance
  stories (CTO-level architecture decisions placed in On the Radar).

**Eval mechanism.** Regression fixture
(`evals/fixtures/_regressions/c_78dcc648119217a1_signal_section_mismatch.md`)
documents the cluster shape. The `_reconcile_signal_with_audience_tags`
helper in `src/summarise.py` is the deterministic check; its log line
is the operational detection signal. A future eval extension can assert
the invariant directly on `issue.json` (no story has `signal=="act"`
sitting in On the Radar).

**Mitigation.**
1. ~~Rank prompt sharpened (v0.1 -> v0.2)~~ **Reverted 2026-05-24 (#77).**
   The v0.2 prompt sharpening attempt (concrete `big_picture` examples plus
   the "workflow shifts should score >= 60" anchor) improved metrics on the
   26-cluster labelled subset (Spearman 0.569 -> 0.654, tier disagreements
   2 -> 0) but collapsed scores on the other 17 unlabelled clusters:
   staging went from ~11 surviving stories to 2 (1 Pulse + 1 Big Picture,
   zero Hands-On, zero On the Radar). Probable culprit: the LLM inverted
   the ">= 60 anchor" into "< 60 for everything else". `RANK_PROMPT_VERSION`
   is back at v0.1. The prompt-level fix for FM-12 remains open.
2. Cross-check in summarise.py (`_reconcile_signal_with_audience_tags`,
   Fix 2 of #75, **retained**) augments `audience_tags` when signal says
   Big Picture and rank disagrees -- safety net for the residual miscall
   rate. Note: on the original c_78dcc648119217a1 case this cross-check
   did not fire because the body-grounded signal came back as "discuss"
   not "act"; the cross-check covers the strict signal="act" arm only.
3. Future prompt re-attempt must include full-corpus shape assertions
   (sections populated, tier mix healthy on the full unlabelled corpus),
   not just labelled-corpus Spearman / tier-disagreement metrics. See
   task #77 postmortem and follow-up tasks #78 (eval on staging) and
   #79 (publish gate refuses thin issues).

**Severity.** Medium -- doesn't crash the issue, but a Big Picture
story sitting in On the Radar materially degrades the senior-leader
read of the publication.

**Last occurrence.** 2026-05-24 (regression #75; prompt-level fix
attempted and reverted same day in #77; cross-check mitigation in
place; root cause at the prompt level still open).

---

### FM-13: Continuation surfaces as Pulse, displacing fresh signal

**What it is.** A cluster correctly tagged as a continuation (carrying
`Cluster.cross_time_ref` set to a prior day's chain root) reaches the
issue's Pulse slot. The reader opens the publication and the lead is "we
already told you about X yesterday; here is more of it." The Pulse is
meant to be the day's freshest editorial anchor; leading with a follow-
up signals "there is nothing new today" even when fresher (lower-scored)
stories are available.

Two upstream contributions made this possible:
1. `rank.py` scored the continuation purely on its content, with no
   downweighting for the fact that the original signal had already been
   recognised on a prior day. A how-to follow-up to yesterday's
   announcement scored `significance = 65` (rubric anchor 65 sits
   between "two signal-filter dimensions" and "three").
2. `summarise.py`'s `_pick_pulse` selected the highest-scored surviving
   cluster with no preference between FRESH and continuation stories.
   When the continuation outscored the fresh competition, it landed as
   Pulse by default.

The smoking gun: `c_2e53967d020fb800` ("How I do use the recent llama.cpp
native tools to do web rag ... directly from inside the llama-server's
webui", 2026-05-25). `cross_time_ref = c_cf0b99c06c42a9ba`,
`significance = 65`, `score = 44`. Routed to Pulse despite a fresh
Hugging Face benchmark tracker (`c_78dabe7884f76ef8`, score 39) being
available.

**Detection signal.**
- Any `issue.json` where the Pulse story's `cross_time_ref` is not null
  AND at least one non-pulse story in the issue has `cross_time_ref ==
  null`. The fixed deterministic logic in `_pick_pulse` (#82) makes this
  invariant strict for new issues; staging surveillance for the rule
  would catch regressions.
- The log line `Pulse non-continuation bias fired -- demoted
  continuation <id> ...` (#82) firing more than ~1-2 times per week is a
  signal that rank.py is systematically over-rating continuations on the
  pre-cap significance dimension.
- The log line `continuation penalty applied to <id>: significance
  N->50, score N->M ...` (#81) firing on every continuation in a daily
  run is expected; a sudden absence is the signal (rule turned off,
  cross_time_ref attribution broke, or there are no continuations
  today).
- The degraded-mode log `NO FRESH SIGNAL FOR PULSE` firing means the
  whole top of the issue is follow-ups. The operator should consider
  whether the issue should ship at all.

**Eval mechanism.** Regression fixture
(`evals/fixtures/_regressions/2026-05-25_continuation_as_pulse.md`)
documents the cluster shape and the expected post-fix behaviour. Unit
tests pin both rules: `tests/test_rank.py::TestContinuationPenalty`
covers the deterministic penalty (cross_time_ref None -> no change;
set -> significance capped, score recomputed); `tests/test_summarise.py
::TestPulseSelectionContinuationBias` covers the selection rule (fresh
beats continuation regardless of score; degraded mode when all
continuations; Pulse-class quality bar still applies within the chosen
pool). A future eval extension can assert the on-disk invariant
directly: no `issue.json` ships with a continuation Pulse when a fresh
story was available.

**Mitigation.**
1. `summarise.py` Pulse selection biased against continuations (#82,
   `_pick_pulse` v0.3 -- `PULSE_PROMPT_VERSION` bumped to v0.9 to record
   the behavioural change). Fresh stories beat continuations regardless
   of score; within the chosen pool the >= 2 signal-dimensions quality
   bar still applies; degraded mode (all continuations) picks the best
   and ships with a WARNING log.
2. `rank.py` deterministic continuation penalty (#81,
   `_apply_continuation_penalty`). Caps
   `breakdown["significance"]` at 50 when `cluster.cross_time_ref` is
   set; score recomputed via the same `_weighted_score` the pydantic
   validator uses. NOT a prompt change -- the #75/#77 cliff is the
   precedent that deterministic post-processing is the safer path for
   hard-constraint rules. `RANK_PROMPT_VERSION` stays at v0.1 because
   the prompt text is unchanged.

**Severity.** Medium -- doesn't crash the issue, but a continuation
sitting in Pulse materially degrades the editorial first impression and
trains the reader to skim past the lead.

**Last occurrence.** 2026-05-25 (staging; caught and fixed before
release in tasks #81 + #82).

---

---

### FM-14: Verifier calibration decay

**What it is.** The factual-accuracy verifier (the future `verify` stage, which
decomposes summaries into atomic claims and checks each against the source excerpt)
drifts away from its calibration baseline. Decay takes two forms:

- **Precision decay (over-flagging):** the verifier starts marking legitimate
  transformations (number-rounding, generalisation, paraphrase) as contradicted.
  Issues pass the hard gate but Arman sees spurious flags and stops trusting
  the signal.
- **Recall decay (under-flagging):** the verifier stops catching real errors —
  numeric substitutions, entity swaps, directional inversions, dropped trust flags,
  or errors in the headline. Issues ship with factual errors the verifier was
  designed to catch.

Both directions degrade the verifier's value. The calibration gate exists precisely
to catch each direction. Since v2 of the fixture set, the verifier is tested on both
the headline (title) and the summary body. A factual error in the headline is the
most severe kind — readers see and trust the headline first, and AI Vector headlines
carry named actors (the recognition rule). The verifier callable signature is now:
`verifier(headline: str, body: str, source_excerpt: str) -> list[dict]`
where each returned dict carries a `location` field: "headline" or "body".

**Detection signal.**
- Eval 7 (`factual_accuracy`) in CI goes red on any of the three hard gates:
  - `recall_contradicted < 0.85` — under-flagging (FM-14 recall decay). This is
    computed over RELIABLE MUTATION CLASSES ONLY: numeric_substitution,
    entity_substitution, directional_inversion, headline_error. dropped_trust_flag
    is intentionally excluded from the hard gate (see advisory note below).
  - `precision_supported < 0.80` — over-flagging (FM-14 precision decay).
  - `unverifiable_accuracy < 0.80` — confusing "not in source" with "contradicts source".
- `dropped_trust_flag_recall_advisory` — DIAGNOSTIC METRIC, NOT a hard gate.
  Tracks whether the verifier catches dropped epistemic caveats (e.g. vendor-reported
  numbers stated as bare facts). Reported in every eval run and visible in the report.
  Advisory-only because the de-hedged claim is factually true; only the epistemic
  framing was removed, making this class inherently debatable. If this metric
  improves over time, the team can revisit adding it to the gate. If it falls,
  the weekly drift review will catch it. Not hidden, not gated. Decision accepted
  by Arman: 2026-06-29.
- Per-location recall in `per_location_recall` (informational, not a hard gate):
  - `headline` recall drops while `body` recall holds → the verifier prompt has
    regressed on headline reading. The headline_error cases (fa_601–fa_604) exercise
    this split directly.
  - Both drop together → general recall decay, not headline-specific.
- Per-mutation-type recall breakdown surfaces which class of error the verifier
  is missing: numeric_substitution, entity_substitution, directional_inversion,
  dropped_trust_flag (advisory), headline_error.
- Drift detection: `verifier_flag_rate` z-score > 2 (spiking, over-flagging) or
  z-score < -2 (collapsing, under-flagging) in `evals/drift/baselines/`.
  Both directions surface in the weekly drift review.
- Arman's editorial feedback: "the verifier keeps flagging things that are fine"
  (precision decay) or "this summary has a wrong number and the verifier missed it"
  (recall decay).

**Eval mechanism.** Eval 7 (`eval_factual_accuracy`) runs the verifier against
31 labelled fixture cases in `evals/fixtures/factual-accuracy/cases.yaml` (schema v2).
It is a hard gate (blocks merges to the verifier prompt) on all three primary metrics.
Per-location recall (headline vs body) is computed and reported for surgical diagnosis
of headline-specific regressions. Per-mutation-type recall is computed for all five
mutation types (numeric_substitution, entity_substitution, directional_inversion,
dropped_trust_flag, headline_error). The four headline_error cases (fa_601–fa_604)
are the primary calibration surface for the verifier's headline-reading capability.

**Drift interpretation for the `location` dimension.**
A drop in `per_location_recall["headline"]` without a corresponding drop in
`per_location_recall["body"]` is a strong signal that the verifier prompt was
changed in a way that stopped it from reading the headline carefully. This pattern
can also appear from a model endpoint shift (FM-06) if the new model is more body-
focused. In either case, the headline_error fixture cases (fa_601–fa_604) will
be the failing cases in the case_results dict.

**Mitigation.**
1. If `recall_contradicted` (reliable classes) drops: check `per_location_recall` first.
   - If headline recall dropped: examine the four headline_error cases. A prompt
     regression that dropped the "also check the headline" instruction is the most
     likely cause. Restore the instruction and re-run.
   - If body recall dropped: examine per-mutation-type recall to isolate the failing
     class. Numeric substitutions are typically the easiest to catch.
2. If `dropped_trust_flag_recall_advisory` drops to zero or stays at zero over
   multiple releases: document this in the weekly drift review note. Do NOT gate on
   it, but track whether it ever improves with prompt iterations. If it rises
   consistently above 0.50, consider adding it to the hard gate in a future release.
3. If `precision_supported` drops: the verifier is over-literalising. Check whether
   legitimate transformations (rounding, generalisation, jargon→plain English) are
   in the failing cases. The fixture `transformation_type` field identifies which
   transformation the case exercises. Also check the supported headline claims —
   a verifier that misreads a correctly-named recognition-rule actor as wrong will
   show up as supported headline claims failing.
4. If `unverifiable_accuracy` drops: the verifier is conflating "the source doesn't
   address this" with "the source contradicts this". The prompt needs clearer
   instruction on the unverifiable verdict.
5. After fixing the verifier prompt: re-run `python -m evals.run_evals` and confirm
   all three hard-gate metrics pass before merging.
6. If calibration decay is due to a model endpoint shift (not a prompt change), this
   is a joint FM-06 + FM-14 incident. See FM-06 mitigation. Pin the model version.

**Severity.** Medium initially (the advisory-only verifier does not block publication);
high if Arman starts acting on verifier flags (either trusting false positives or
ignoring genuine issues because recall is low). Headline errors that reach Arman
are particularly damaging because the headline is what readers see first.

**Last occurrence.** Never (calibration decay not yet observed). FM-14 first seeded
2026-06-28; fixture set updated to schema v2 (headline coverage) 2026-06-28; calibration
gate scoped to reliable classes only (dropped_trust_flag advisory) 2026-06-29.
Verifier ships as advisory tool on 2026-06-29 — all three hard gates pass at
recall_contradicted (reliable) = 1.000, precision_supported >= 0.96,
unverifiable_accuracy = 1.000 (v0/v1/v3 from _scratch/fa_tuning/preds.json).
dropped_trust_flag_recall_advisory = 0.000 (known limitation; excluded from gate by
decision; tracked as diagnostic).

---

## Regression discipline

Every bug that escapes to ratification gets three things added to this repo
**before** the fix lands. The fix PR shows the eval going red to green.

1. **A fixture case** in `evals/fixtures/<dataset>/` that reproduces the
   bug's surface — the cluster shape, issue shape, or artifact state that
   should have failed eval. Name the dataset `YYYY-MM-DD-<slug>` where the
   date is the ratification date the bug escaped on.

2. **An entry in this file** using the schema above: name, detection signal,
   last occurrence (the date it escaped), mitigation, severity.

3. **An eval assertion** that would have caught it — either extending an
   existing check in `evals/run_evals.py` or adding a new one. The assertion
   must reference the fixture case by name.

This mirrors `tests/CONVENTIONS.md` rule 7 for the eval layer. The rule is
not advisory. If a bug escapes and no fixture + entry + assertion follows,
the Eval Engineer flags it in the next behavioural-integrity note.

The point is not the paperwork. The point is that the next instance of the
same class of failure gets caught before Arman sees it.

*End of seeded failure modes. New entries added here after incidents and weekly reviews.*
