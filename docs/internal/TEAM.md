# AI Vector — Team Working Agreements

*"Today's AI, with a heading."* — Author: **Arman**. Project plan:
[`PLAN.md`](PLAN.md) (this directory). Agent definitions:
[`/.claude/agents/`](../../.claude/agents/).

This document tells a new contributor, in five minutes, **who decides what
and where the seams are**. If something here disagrees with PLAN.md, PLAN.md
wins; open a PR to fix this file.

---

## What the team is for

**The team builds the engine. The engine produces the newsletter.**

The pipeline (`fetch → cluster → rank → summarise → verify → render →
review`, i.e. `src/fetch.py` → `src/cluster.py` → `src/rank.py` →
`src/summarise.py` → `src/verify.py` → `src/render.py` → `src/review.py`,
wired by `src/run.py`'s `aiv` CLI and triggered
on weekday mornings by `.github/workflows/daily.yml`) is the engine. The
engine runs **without any agent in the runtime loop** — Python
code + LLM API calls inside `rank.py`, `summarise.py`, `verify.py`,
`review.py` and `revise.py`
produce `data/staging/<date>/issue.json` and a previewable HTML at
`docs/staging/<date>.html`. After the review, the **revision loop**
(`src/revise.py`, driven by `src/run.py`) may act on the reviewer's
findings — capped at two cycles, stopping as soon as the verdict stops
improving. Then the **publish gate** (`src/gate.py`) answers one question:
may this issue go out without a human?

**Two stages are advisory — and the phrase now means something precise:
advisory to the pipeline, decisive to the gate.** `verify`
(factual-accuracy flags) and `review` (the computed editorial verdict in
`review.json`) never block the pipeline and never block a **human-ratified**
`aiv release`. They *do* govern the unattended path: an `unavailable`
verdict from either holds an automatic publish. The distinction is the whole
design. A human who reads an `unavailable` review and ships anyway has made
a judgment; a scheduler that ships past one has made no judgment at all.
What is blocking is not the advice — it is the absence of a human.

**Arman is the only required participant in the daily loop, and how required
depends on the rollout phase.** In `shadow` (the default) he ratifies every
issue exactly as before, and the gate merely records what it *would* have
decided. As phases advance, the gate acts on its own `auto_merge` decisions
and Arman's ratification moves from per-issue to per-policy. He sets the
phase; the gate never widens itself.

The team's job, in order:

1. **Phase 0–3 of PLAN.md** — design contracts, build pipeline modules, wire
   the eval harness, schedule and ship v0.1. This is the bulk of the work.
2. **Operate the engine after it ships** — episodic, not per-issue: Source
   maintains feeds when one breaks; Retrieval re-tunes when dedup drifts;
   LLM Engineer revises prompts when evals flag regressions; Eval watches
   the gate; Release handles incidents; Architect adjudicates scope.
3. **Optional daily second-reader** — Arman may invoke the **Editor** agent
   between engine output and his ratification, for an editorial pass. This
   is *his tool to use when wanted*, not a hard daily dependency. The engine
   runs and ratification happens with or without it.

If you find yourself doing daily work that isn't operating the engine or
helping Arman ratify, you're out of scope.

---

## Roster (one line per seat)

| Seat | Model | One line |
|---|---|---|
| **Architect & Tech Lead** | Opus | Owns contracts, repo structure, DESIGN.md, archive schema, `src/run.py` (orchestration shell + the revision loop's control flow) and `src/gate.py` (the unattended-publish gate). The buck stops here on "does the shape make sense." |
| **Source Engineer** | Sonnet | Owns `config/sources.yaml`, `src/fetch.py`, source-health tracking. Subscribe, don't scrape. |
| **Retrieval Engineer** | Sonnet | Owns `src/cluster.py`. Makes 10 feeds not produce 10 copies of one story — today or across the last 14 days. |
| **LLM Engineer** | Opus | Owns `src/rank.py`, `src/summarise.py`, `src/verify.py` (advisory factual verifier), `src/review.py` (structured editorial pass), `src/revise.py` (the revision engine — decides *what* to change; `run.py` decides how many cycles), the prompts (rank/summarise/verify/review/revise prompt versions), and the rubric content. Implements voice. |
| **Eval Engineer** | Sonnet (independent) | Owns `evals/`. Hard veto on regressions to dedup, ranking, voice, module integrity, drift, verifier calibration (Eval 7). |
| **Test Engineer** | Sonnet (independent) | Owns `tests/` in full, including `tests/CONVENTIONS.md`. Hard veto on test PRs — keeps the unit-test suite load-bearing, not performative. Files bugs in `src/`; never fixes them. |
| **Editor** | Opus | Owns `EDITORIAL.md` (repo root) and voice labels. Managing-editor assistant — Arman ratifies every issue. |
| **Experience Designer** | Opus | Owns `docs/internal/READING_EXPERIENCE.md`, presentation specs, text-unit patterns, and product microcopy. The reader's advocate — how the issue reads, scans, converses, and feels. Specifies; Release Engineer implements; Editor keeps story prose; never touches code. |
| **Release Engineer** | Sonnet | Owns `src/render.py`, templates, workflow, GitHub Pages, §7 day-one validation, liaison drafting. |

No Haiku in v0. No sub-agent spawning in v0.

---

## Decision rights (who decides, who consults, who is informed)

| Domain | Decides | Consults | Informed |
|---|---|---|---|
| Pydantic contracts (Item, Cluster, RankedStory, Issue, verification models, archive) | Architect | All pipeline engineers, Eval | Editor |
| Archive schema (`data/staging/` + `data/released/`) | Architect | All who read/write it | All |
| Repo structure, module boundaries | Architect | Module owners | All |
| Source list (`sources.yaml`), trust weights | Source | Editor, LLM Engineer | Eval |
| Embedding model, cluster thresholds | Retrieval | Architect (availability), Eval | LLM Engineer |
| Prompts, rubric content | LLM Engineer | Editor, Eval | Architect, Arman |
| LLM model per stage | LLM Engineer | Architect, Arman (cost) | Eval |
| Eval rubric mechanics, harness | Eval | Editor (voice rubric co-dev) | All |
| Voice itself | **Arman** | Editor proposes, LLM Engineer implements | All |
| Per-issue release, **when the phase is `shadow`** | **Arman** (runs `aiv release`) | Editor flags tradeoffs; advisory verify + review verdicts inform | Release Engineer (renders + archives + appends URLs) |
| Per-issue release, **once phases advance** | **the gate** (`src/gate.py`, on `green` — and on `amber` in `green_amber`) | nobody per-issue; the checks encode the ratified policy | Arman (reads `gate.json` + the held-issue notification) |
| **Publish policy** — which phase is in force, which checks block, the hold vocabulary | **Arman** (sets `AIV_AUTO_PUBLISH_PHASE`) | Architect proposes the check set; Eval reports hold-rate | All |
| Revision policy — modes, cycle cap, stop condition | Architect | LLM Engineer (owns what gets changed), Eval | Arman |
| Revision engine — what a finding turns into | LLM Engineer | Editor (voice), Eval | Architect |
| Verify prompt + calibration thresholds | LLM Engineer | Eval (Eval 7 gates) | Editor, Arman |
| Test conventions (`tests/CONVENTIONS.md`) | Test Engineer | Module engineers (they write module tests) | All |
| Reading experience / presentation specs | Experience Designer (proposes) | Release (implements), Editor (prose stays theirs) | Arman ratifies visible changes |
| Template / layout / CSS | Release | Experience Designer (specs), Editor (visual voice) | Architect |
| `daily.yml`, Pages, deployment | Release | Architect | Arman |
| §7 internal-platform asks | Release **drafts** | Arman **sends** | Architect |
| Adding/removing a source | Source | Editor, LLM Engineer | Eval |
| Promoting `index.py` (SQLite over JSONL) | Architect | Eval, LLM Engineer | All |
| Postmortem facilitation | Architect | Eval (evidence) | All |

**Veto powers:**
- **Eval Engineer — hard veto** on PRs touching `src/cluster.py`,
  `src/rank.py`, `src/summarise.py`, `src/verify.py`,
  `config/rubric.yaml`, and LLM
  Engineer's prompts. Mechanism: CI runs the harness (`aiv eval`);
  non-zero exit blocks merge.
- **Eval governance (2026-07-04): gates always run; eval changes only on
  ratification.** Every LLM-stage prompt/model change runs its eval gate
  before merge, results recorded in the PR. Changes to `evals/` itself
  (thresholds, fixtures, gate scope, harness semantics) require Arman's
  explicit ratification before commit — the Eval Engineer proposes; the
  meaning of the gates moves only on ratification.
- **Test Engineer — hard veto** on PRs touching anything under `tests/`.
  Mechanism: `tests/CONVENTIONS.md` compliance review; the suite's green
  bar in CI.
- **Architect — required reviewer** on any PR touching pydantic models,
  archive schema, or a module's public interface.
- **Editor — labelling authority** on voice. Labels accumulate in
  `evals/voice/`; the Eval Engineer's voice-adherence rubric is built from
  them. Editor does not auto-block merges (Eval's rubric does, once
  voice-adherence is wired in).
- **Arman — the publish policy, and the release itself while the phase is
  `shadow`.** The split matters and is worth stating twice: **the per-issue
  decision may move to the gate; the decision to let it move never does.**
  `AIV_AUTO_PUBLISH_PHASE` is Arman's variable. An unset or misspelled value
  resolves to `shadow`, so the gate can only ever be *granted* authority,
  never assume it. A local `aiv release` stays entirely manual in every
  phase — the gate governs the unattended path only.

---

## Handoff diagram

```
┌──────────────────┐
│ Source Engineer  │  reads:  config/sources.yaml
│  src/fetch.py    │  writes: data/staging/<date>/items.jsonl
└────────┬─────────┘          data/staging/<date>/source_health.json
         │ items.jsonl
         ▼
┌──────────────────┐  reads:  data/staging/<date>/items.jsonl
│ Retrieval Eng.   │          data/released/(last 14d)/clusters.jsonl + centroids  ← cross-time dedup
│  src/cluster.py  │  writes: data/staging/<date>/clusters.jsonl
└────────┬─────────┘          data/staging/<date>/embeddings/centroids.npz
         │ clusters.jsonl
         ▼
┌──────────────────┐  reads:  data/staging/<date>/clusters.jsonl
│ LLM Engineer     │          data/released/(last 14d)/issue.json   ← callbacks, direction
│  src/rank.py     │          data/released/(last 14d)/ranked.jsonl
│  src/summarise.py│  writes: data/staging/<date>/ranked.jsonl
│  src/verify.py   │          data/staging/<date>/issue.json
│                  │          data/staging/<date>/source_excerpts.jsonl (staging-only)
│                  │          data/staging/<date>/verify.json  ← ADVISORY: factual flags,
└────────┬─────────┘          never blocks release; badges in staging preview only
         │ issue.json (draft) + advisory verify flags
         ▼
┌──────────────────┐  render (staging) → docs/staging/<date>.html
│ render + review  │  review (Editor persona via src/review.py)
│ (engine stages)  │  writes: data/staging/<date>/review.json (findings +
└────────┬─────────┘          computed_verdict) and its rendered review.md
         │ staging preview + computed verdict
         ▼
┌──────────────────┐  revise loop (src/revise.py, driven by src/run.py)
│ revision loop    │  shadow (default): proposals only, issue untouched
│ (post-review)    │  live: ≤2 × [revise → verify touched → render → review],
│                  │        stops when the verdict stops improving
└────────┬─────────┘  writes: data/staging/<date>/revisions.jsonl (append)
         │ possibly-revised issue + a review that matches it
         ▼
┌──────────────────┐  gate (src/gate.py — deterministic, no LLM)
│ publish gate     │  reads: issue.json, review.json, verify.json,
│ (Architect)      │         revisions.jsonl
│                  │  writes: data/staging/<date>/gate.json
└────────┬─────────┘  auto_merge | hold — fails closed; phase decides who acts
         │ decision
         ▼
┌──────────────────┐  reads:  data/staging/<date>/issue.json + ranked.jsonl + verify.json
│ Editor           │          EDITORIAL.md, past released issue.json files
│ (assistant,      │  writes: evals/voice/YYYY-MM-DD.labels.yaml
│  optional)       │          daily editorial note for Arman
└────────┬─────────┘
         │ flagged tradeoffs, Pulse proposal
         ▼
┌──────────────────┐  phase=shadow  → Arman ratifies per-issue: `aiv release`
│   Arman          │  phase advanced → the gate acts on auto_merge; Arman
│   (human gate)   │                   ratifies the POLICY, not each issue,
└────────┬─────────┘                   and reads every hold
         │ released
         ▼
┌──────────────────┐  release_promote: data/staging/<date>/ → data/released/<date>/
│ Release Engineer │  (incl. verify.json), assigns issue_number, appends
│  src/render.py   │  data/published_urls.txt
│  .github/...     │  writes: docs/index.html + docs/released/<date>.html (no badges)
└──────────────────┘  commits & pushes; GitHub Pages serves

      ╔════════════════════════════════════════════════════════╗
      ║ Eval Engineer (independent) — reads everything in      ║
      ║ data/, src/, config/. Writes only to evals/ and        ║
      ║ derived eval sidecars. Hard-veto gate at PR time.      ║
      ╚════════════════════════════════════════════════════════╝

      ╔════════════════════════════════════════════════════════╗
      ║ Test Engineer (independent) — owns tests/ in full.     ║
      ║ Hard veto on test PRs; files (never fixes) src/ bugs.  ║
      ╚════════════════════════════════════════════════════════╝

      ╔════════════════════════════════════════════════════════╗
      ║ Experience Designer — reads staged/released HTML;      ║
      ║ writes READING_EXPERIENCE.md + presentation specs      ║
      ║ (Release implements; Editor keeps story prose).        ║
      ╚════════════════════════════════════════════════════════╝

      ╔════════════════════════════════════════════════════════╗
      ║ Architect — orchestrates seams. Required reviewer on   ║
      ║ contract changes. Doesn't run the pipeline; makes the  ║
      ║ pipeline's seams crisp enough that others can.         ║
      ╚════════════════════════════════════════════════════════╝
```

**The artifacts at each seam are the contracts.** If a stage breaks its
artifact's pydantic shape, downstream blows up loudly — not silently.

---

## Rituals

### Phase 0 kickoff (one-off, before any pipeline code)
- **Owner:** Architect.
- **Deliverables:** `docs/internal/DESIGN.md` (contracts written out), first cut of
  `config/sources.yaml` (Source), first cut of `config/rubric.yaml` (LLM
  Engineer, reviewed by Editor + Eval), README with Arman as author.
- **In parallel:** Release Engineer drafts the §7 asks; Arman sends them.
- **Exit gate:** Arman signs off. Per PLAN §10: *"Stop and let me review
  before Phase 1."*

### Design review (one-off, between Phase 0 and Phase 2)
- **Owner:** Architect facilitates.
- **Attendees:** everyone.
- **Deliverable:** DESIGN.md revised; team-wide alignment on contracts.

### Eval gate (continuous, in CI)
- **Owner:** Eval Engineer.
- **Trigger:** every PR touching gated paths (cluster.py, rank.py,
  summarise.py, verify.py, rubric.yaml, prompts).
- **Mechanism:** the harness (`aiv eval` / `python -m evals.run_evals`)
  runs on PR. Non-zero exit blocks merge. Soft gates rot; this one doesn't.

### Daily draft loop (the weekday heartbeat)

**Cadence: weekdays only.** The scheduled workflow fires on Sydney
Monday–Friday mornings. Monday's issue absorbs the weekend through the
gap-aware fetch window, which widens each source's recency window by the
days elapsed since the last release — so a two-day gap produces one fuller
issue, not two thin ones.

The loop, end to end:

1. **The engine drafts.** `fetch → cluster → rank → summarise → verify →
   render → review` writes `data/staging/<date>/`: `issue.json`, the
   advisory `verify.json`, the structured `review.json` (plus its rendered
   `review.md`), and a preview at `docs/staging/<date>.html` with the
   factual-flag badges visible in staging only.
2. **The revision loop may act on the findings.** Default mode is `shadow`:
   proposals are logged to `revisions.jsonl` and nothing changes. In `live`
   mode it runs up to two cycles of
   [revise → re-verify the touched stories → render → review], stopping the
   moment the computed verdict stops improving. Any failure inside it is
   logged and the run continues with the issue as it stands.
3. **The gate decides.** `aiv gate` writes `gate.json`: `auto_merge` or
   `hold`, with every check recorded — passes included — so the file reads
   as an audit trail rather than a list of complaints. It fails closed:
   anything it cannot positively confirm is a hold.
4. **What happens next depends on the phase.**
   - `shadow` (default): nothing acts on the decision. **Arman** reviews the
     preview and runs `aiv release` exactly as before. The gate's answer is
     recorded so the observation period measures the policy we are rolling
     towards.
   - `green_only` / `green_amber`: the workflow acts on `auto_merge` and the
     issue publishes unattended. A `hold` opens or leaves a release PR for
     Arman, with the hold reasons named.
5. **Release promotes.** `data/staging/<date>/` → `data/released/<date>/`
   (including `verify.json`, `review.json`, `review.md`, `gate.json`,
   `revisions.jsonl`), assigns `issue_number`, ships to `docs/index.html` +
   `docs/released/<date>.html`, appends URLs to `data/published_urls.txt`.

- **A local `aiv release` is always manual**, in every phase. The gate
  governs the unattended path only; it never runs inside `aiv run` and never
  publishes anything itself.
- **Optional editorial pass:** Arman may invoke the **Editor** agent between
  engine output and ratification. Editor reads the staging draft, labels
  off-voice candidates, flags tradeoffs, proposes The Pulse. Editor never
  releases.
- **Experimentation:** Arman re-runs the pipeline freely. Same-day re-runs
  overwrite `data/staging/<date>/` atomically; nothing touches the released
  archive or `data/published_urls.txt` until release. Cross-time dedup,
  callbacks, and the URL exclusion index all read released only, so staging
  churn is invisible to tomorrow's run. (`revisions.jsonl` is the one
  exception to "overwrite on re-run": it appends, because it is a log, and a
  log that forgets is not one.)
- **A day that does not ship:** yesterday's released issue stays live with
  its "as of" date. No silent skips. A staging draft that never gets
  released stays in `data/staging/<date>/` as evidence of the attempt.
  (`run.py` warns loudly when earlier issues sit staged-but-unreleased:
  dedup couldn't see them, so a later issue risks repeating their stories.)

### Voice review (weekly, lightweight — changed shape, see below)
- **Owner:** Editor, with Eval Engineer + LLM Engineer.
- **Time-box:** ~30 minutes.
- **Inputs:** Eval Engineer's drift report, Editor's accumulated weekly
  labels, recent Arman ratification patterns (what got cut, what got
  rewritten).
- **Output:** one or two concrete adjustments — to EDITORIAL.md, to a
  prompt, or to the voice rubric.

> **Why weekly, given Arman ratifies daily?** Because daily ratification
> resolves the per-issue voice question already. The weekly review is for
> **trends**: are we sliding, are we tightening, is the lens earning its
> place over time. Per-issue voice = Editor + Arman. Trend voice = this
> ritual.

### Daily-run postmortem (only when breakage)
- **Owner:** Architect facilitates; the engineer whose surface broke writes
  the note in `docs/postmortems/YYYY-MM-DD.md`.
- **Format:** what broke, what we noticed, what we changed, what we'll do
  to notice it earlier next time. No blame, all signal.

### Behavioural-integrity weekly note (Eval-led, very short)
- **Owner:** Eval Engineer.
- **Output:** one paragraph in the weekly eval report on whether the team
  is following its own rules (contract reviews happening, postmortems
  filed, voice labels accumulating).

---

## Risk register

| # | Risk | Owner | Guardrail |
|---|---|---|---|
| 1 | **§7 blockers** (Actions `schedule:`, egress, Pages) silently kill the project after weeks of build-out | Release | §7 asks sent on day one; answers logged in DESIGN.md; pipeline build-out gated on at least workarounds being identified |
| 2 | **Eval gaming** — the team optimises to the fixtures, not to readers | Eval | Fixtures rotated quarterly; ratified issues become labelled data (growing corpus); behavioural-integrity note flags "Spearman up, ratification down" |
| 3 | **Voice drift** — the publication slowly sounds less like Arman | Editor + Arman | Daily ratification catches per-issue drift; weekly voice review catches trend drift; Eval Engineer's voice-adherence metric anchors against ratified archive |
| 4 | **Source rot** — a feed stops publishing, the pipeline doesn't notice | Source | `source_health.json` per run; ≥3 days `fired: false` triggers Source review; trust weights decay slowly |
| 5 | **Contract drift** — pydantic shapes change in code, DESIGN.md lags | Architect | Architect required reviewer on contract PRs; `schema_version` per artifact; Eval module-integrity check schema-validates every archive write |
| 6 | **Prompt drift under us** — model endpoint shifts, outputs change with no code change | LLM Engineer + Eval | Voice-adherence eval is on a *separate* judging path where possible; sudden score changes flag the failure mode "prompt drift" in `evals/failure_modes.md` |
| 7 | **Archive corruption** — half-written JSONL poisons months of history | Architect (schema) + each writer | Atomic writes (`.tmp` + rename) enforced in every pipeline stage; Eval cross-checks references (item_id → cluster_id → ranked_id → issue) |
| 8 | **Unattended publish ships something nobody should have shipped** — the gate says `auto_merge` on an issue a human would have held | Architect (gate policy) + Arman (phase) | The gate fails closed: every state it cannot positively confirm is a hold. Hard blocks (contradicted claim, verifier unavailable, stale review) hold in *every* phase, `shadow` included. Rollout is phased and the phase variable is Arman's; unset or misspelled resolves to `shadow`. A held issue is loud, not silent. Eval watches the hold-rate and the hold-reason distribution — a rate that collapses is the early signal that a check stopped meaning anything |
| 9 | **The reviser degrades voice** — the revision loop "fixes" a finding and makes the issue worse | LLM Engineer (what changes) + Architect (when to stop) | The loop stops the moment the computed verdict stops improving, on `red < amber < green` with unreadable ranked below `red`; two cycles maximum; `shadow` is the default mode. It never rolls back, so the publish gate is the backstop — a verdict that fell to `amber` (in `green_only`) or `red` holds and a human looks. Every change is logged in `revisions.jsonl` with full before/after text, which is the evidence for tuning the revise prompt. Editor reviews accumulated rejections and applied edits at the weekly voice review |

---

## Archive — `data/staging/<date>/` and `data/released/<date>/`

Locked for v0: **JSON-per-day, no SQLite.** If query patterns warrant
SQLite later, a lazy `src/index.py` builds it from JSONL on demand;
Architect authorises.

> **Two archive states (staging vs released).** The table below describes
> the **released** archive at `data/released/<date>/` -- the canonical,
> immutable record. Every promoted file in the table also lives, with the
> same shape, under
> `data/staging/<date>/` while the engine is iterating on a draft. Default
> `aiv run` writes to staging; `aiv release`
> promotes staging to released, assigns `Issue.issue_number`, and
> appends URLs to `data/published_urls.txt`. Cross-time dedup,
> callbacks, the URL exclusion index, and the eval harness all read
> **released only** -- staging is invisible to history. See DESIGN.md
> "Archive: staging vs canonical" for the full state model.

| File | Writer | Schema owner | Read by |
|---|---|---|---|
| `items.jsonl` | Source Engineer | Architect | Retrieval, Eval, Release (provenance views) |
| `source_health.json` | Source Engineer | Architect | Eval, Release, Source (trust-weight decay) |
| `clusters.jsonl` (+ `embeddings/centroids.npz`) | Retrieval Engineer | Architect | LLM Engineer (current + last 14 days), Retrieval (cross-time dedup), Eval |
| `ranked.jsonl` | LLM Engineer | Architect | Editor, Eval, Release (archive views) |
| `issue.json` | LLM Engineer (staging); `aiv release` (released, assigns `issue_number`) | Architect | Editor, Arman, Release, Eval, LLM Engineer (callbacks -- released only) |
| `verify.json` | LLM Engineer (`src/verify.py`, advisory) | Architect | Render (staging badges), Editor, Eval (drift `verifier_flag_rate`); promoted on release |
| `source_excerpts.jsonl` | LLM Engineer (`src/summarise.py`) | Architect | Verify stage only. **Staging-only** — never promoted |
| `review.json` | LLM Engineer (`src/review.py`) | Architect | `src/gate.py` (`computed_verdict` + `issue_sha256` — the primary review artifact), `src/revise.py` (the actionable findings), Eval; promoted on release |
| `review.md` | LLM Engineer (`src/review.py`, rendered from `review.json`) | Architect | Arman (pre-release verdict); `src/gate.py` only when `review.json` is absent; promoted on release |
| `revisions.jsonl` | LLM Engineer (`src/revise.py`) | Architect | Arman (what did the engine touch?), `src/gate.py` (informational `revision_status` — reads `cycle` and change `status` only), Eval; promoted on release. **Appends** rather than overwriting on re-run |
| `gate.json` | **Architect** (`src/gate.py`) | Architect | The publish workflow (acts per phase), Eval (hold-rate + hold-reason distribution), Arman; promoted on release. A missing `gate.json` means **hold**, never proceed |

**Rules:**
- Atomic writes (`.tmp` + fsync + rename). Half-written files are worse
  than missing ones.
- `schema_version` on every artifact. Bump on shape change; record diff in
  DESIGN.md.
- Readers tolerate missing days. A stage that didn't run yesterday must
  not crash today.
- Every released `issue.json` is **labelled data**. In `shadow` the label is
  "Arman approved it"; once phases advance it is "the ratified policy
  approved it, and Arman did not intervene." Both are signal, and
  `gate.json` sitting next to the issue is what tells them apart. Over
  months the archive becomes the most valuable artifact in the repo —
  cross-time dedup, trend reads, voice baselines, and callbacks all depend
  on it.

---

## Day-one validation (PLAN §7) — named owner, three blocking questions

**Owner:** Release Engineer (drafts) + Architect (records answers) +
Arman (sends, decides). Ask/response status is tracked in
`docs/internal/PLATFORM_ASKS.md`.

1. Does internal bank GitHub have **Actions + `schedule:` triggers**
   enabled? Fallback if no: `workflow_dispatch` + external nudge.
2. Is there **outbound egress** from Actions runners to (a) the RSS/API
   sources, (b) the LiteLLM/Bedrock endpoint? Fallback if no: fetch from
   an approved network; surface before Source invests in fetch.py.
3. Is **GitHub Pages on `/docs`** enabled on internal GitHub for this org?
   Fallback if no: identify substitute publish surface; escalate.

**Pipeline build-out is gated on at least workarounds being identified.**
Don't invest in Phase 2 with all three unanswered.

---

## How a new contributor reads this file in 5 minutes

You are joining AI Vector. Five minutes from now you should know:

1. **What we ship** — an engine that produces a daily AI newsletter,
   agent-assisted, with a financial-services lens. Arman authors and
   ratifies every issue. **You build the engine; the engine runs daily.**
2. **Where the code lives** — `src/` (pipeline modules), `config/`
   (sources + rubric + editorial + brand), `templates/` (HTML), `evals/`
   (Eval Engineer's yard), `tests/` (Test Engineer's yard), `docs/` (Pages
   publish surface + `docs/internal/` living docs), `data/`
   (per-day archive, staging + released — the project's labelled corpus
   over time).
3. **The seams** — see the handoff diagram. Artifacts at every seam are
   pydantic-typed; Architect owns the shapes.
4. **The non-negotiables** — design before code; evals before features;
   determinism in code, judgment in the LLM; subscribe, don't scrape;
   nothing publishes except under a policy Arman ratified, and the gate that
   applies that policy fails closed.
5. **Who to ask** — see the decision-rights table. When in doubt, ask the
   Architect.
6. **The three skills** — `design-first-eval-first` (your pre-PR
   checklist), `editorial-focus` (what AI Vector covers and why — Agentic +
   Gen-AI heavy, signal-first), and `finance-lens` (your "does this matter
   to FS?" rubric). Read them once before your first PR; invoke them in
   flow.
7. **Veto powers** — Eval Engineer has hard veto on dedup/ranking/voice/
   integrity/verifier-calibration regressions; Test Engineer has hard veto
   on test PRs. Don't try to route around them; they're the
   guardrails keeping the publication honest.

Then read `docs/internal/PLAN.md`. Then read `docs/internal/DESIGN.md`.
Then read your seat's
agent file in `.claude/agents/`. Then ship something small.

Welcome.
