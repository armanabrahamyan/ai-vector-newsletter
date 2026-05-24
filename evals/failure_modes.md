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
