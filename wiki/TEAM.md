# AI Vector — Team Working Agreements

*"Today's AI, with a heading."* — Author: **Arman**. Project plan:
[`/PLAN.md`](../PLAN.md). Agent definitions: [`/.claude/agents/`](../.claude/agents/).

This document tells a new contributor, in five minutes, **who decides what
and where the seams are**. If something here disagrees with PLAN.md, PLAN.md
wins; open a PR to fix this file.

---

## What the team is for

**The team builds the engine. The engine produces the newsletter.**

The pipeline (`src/fetch.py` → `src/cluster.py` → `src/rank.py` →
`src/summarise.py` → `src/render.py`, wired by `src/run.py` and triggered
daily by `.github/workflows/daily.yml`) is the engine. Once v0.1 ships, the
engine runs every morning **without any agent in the runtime loop** — Python
code + LLM API calls (LiteLLM/Bedrock) inside `rank.py` and `summarise.py`
produce `data/YYYY-MM-DD/issue.json` and a previewable HTML.

**Arman is the only required participant in the daily loop.** He ratifies;
Release pushes; Pages serves.

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
| **Architect & Tech Lead** | Opus | Owns contracts, repo structure, DESIGN.md, archive schema. The buck stops here on "does the shape make sense." |
| **Source Engineer** | Sonnet | Owns `config/sources.yaml`, `src/fetch.py`, source-health tracking. Subscribe, don't scrape. |
| **Retrieval Engineer** | Sonnet | Owns `src/cluster.py`. Makes 10 feeds not produce 10 copies of one story — today or across the last 14 days. |
| **LLM Engineer** | Opus | Owns `src/rank.py`, `src/summarise.py`, the prompts, and the rubric content. Implements voice. |
| **Eval Engineer** | Sonnet (independent) | Owns `evals/`. Hard veto on regressions to dedup, ranking, voice, module integrity, drift. |
| **Editor** | Opus | Owns `docs/EDITORIAL.md` and voice labels. Managing-editor assistant — Arman ratifies every issue. |
| **Release Engineer** | Sonnet | Owns `src/render.py`, templates, workflow, GitHub Pages, §7 day-one validation, liaison drafting. |

No Haiku in v0. No sub-agent spawning in v0.

---

## Decision rights (who decides, who consults, who is informed)

| Domain | Decides | Consults | Informed |
|---|---|---|---|
| Pydantic contracts (Item, Cluster, RankedStory, Issue, archive) | Architect | All pipeline engineers, Eval | Editor |
| `data/YYYY-MM-DD/` schema | Architect | All who read/write it | All |
| Repo structure, module boundaries | Architect | Module owners | All |
| Source list (`sources.yaml`), trust weights | Source | Editor, LLM Engineer | Eval |
| Embedding model, cluster thresholds | Retrieval | Architect (availability), Eval | LLM Engineer |
| Prompts, rubric content | LLM Engineer | Editor, Eval | Architect, Arman |
| LLM model per stage | LLM Engineer | Architect, Arman (cost) | Eval |
| Eval rubric mechanics, harness | Eval | Editor (voice rubric co-dev) | All |
| Voice itself | **Arman** | Editor proposes, LLM Engineer implements | All |
| Per-issue release | **Arman** (runs `python -m src.run --release`) | Editor flags tradeoffs | Release Engineer (renders + archives + appends URLs) |
| Template / layout / CSS | Release | Editor (visual voice) | Architect |
| `daily.yml`, Pages, deployment | Release | Architect | Arman |
| §7 internal-platform asks | Release **drafts** | Arman **sends** | Architect |
| Adding/removing a source | Source | Editor, LLM Engineer | Eval |
| Promoting `index.py` (SQLite over JSONL) | Architect | Eval, LLM Engineer | All |
| Postmortem facilitation | Architect | Eval (evidence) | All |

**Veto powers:**
- **Eval Engineer — hard veto** on PRs touching `src/cluster.py`,
  `src/rank.py`, `src/summarise.py`, `config/rubric.yaml`, and LLM
  Engineer's prompts. Mechanism: CI runs the harness; non-zero exit blocks
  merge.
- **Architect — required reviewer** on any PR touching pydantic models,
  archive schema, or a module's public interface.
- **Editor — labelling authority** on voice. Labels accumulate in
  `evals/voice/`; the Eval Engineer's voice-adherence rubric is built from
  them. Editor does not auto-block merges (Eval's rubric does, once
  voice-adherence is wired in).
- **Arman — release on every daily issue.** Nothing reaches
  `docs/index.html` or `data/published_urls.txt` without `python -m
  src.run --release`. Staging drafts may come and go; canonical only
  grows when Arman says so.

---

## Handoff diagram

```
┌──────────────────┐
│ Source Engineer  │  reads:  config/sources.yaml
│  src/fetch.py    │  writes: data/YYYY-MM-DD/items.jsonl
└────────┬─────────┘          data/YYYY-MM-DD/source_health.json
         │ items.jsonl
         ▼
┌──────────────────┐  reads:  data/YYYY-MM-DD/items.jsonl
│ Retrieval Eng.   │          data/(last 14 days)/clusters.jsonl  ← cross-time dedup
│  src/cluster.py  │  writes: data/YYYY-MM-DD/clusters.jsonl
└────────┬─────────┘
         │ clusters.jsonl
         ▼
┌──────────────────┐  reads:  data/YYYY-MM-DD/clusters.jsonl
│ LLM Engineer     │          data/(last 14 days)/issue.json     ← callbacks, direction
│  src/rank.py     │          data/(last 14 days)/ranked.jsonl
│  src/summarise.py│  writes: data/YYYY-MM-DD/ranked.jsonl
└────────┬─────────┘          data/YYYY-MM-DD/issue.json
         │ issue.json (draft)
         ▼
┌──────────────────┐  reads:  data/YYYY-MM-DD/issue.json + ranked.jsonl
│ Editor           │          docs/EDITORIAL.md, past issue.json files
│ (assistant)      │  writes: evals/voice/YYYY-MM-DD.labels.yaml
└────────┬─────────┘          daily editorial note for Arman
         │ flagged tradeoffs, Pulse proposal
         ▼
┌──────────────────┐
│   Arman          │  ratifies the issue (per-issue, daily)
│   (human gate)   │
└────────┬─────────┘
         │ ratified
         ▼
┌──────────────────┐  reads:  ratified data/YYYY-MM-DD/issue.json
│ Release Engineer │  writes: docs/index.html
│  src/render.py   │          docs/archive/YYYY-MM-DD.html
│  .github/...     │  commits & pushes; GitHub Pages serves
└──────────────────┘

      ╔════════════════════════════════════════════════════════╗
      ║ Eval Engineer (independent) — reads everything in      ║
      ║ data/, src/, config/. Writes only to evals/ and        ║
      ║ derived eval sidecars. Hard-veto gate at PR time.      ║
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
- **Deliverables:** `docs/DESIGN.md` (contracts written out), first cut of
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
  summarise.py, rubric.yaml, prompts).
- **Mechanism:** `python -m evals.run_evals` runs on PR. Non-zero exit
  blocks merge. Soft gates rot; this one doesn't.

### Daily draft loop (the daily heartbeat)
- **Trigger:** engine produces `data/staging/YYYY-MM-DD/issue.json` and a
  preview HTML at `docs/preview/<date>.html`.
- **Default flow:** engine writes staging draft → **Arman** reviews preview
  → **Arman** runs `python -m src.run --release` → Release promotes staging
  to canonical, assigns `issue_number`, ships to `docs/index.html`,
  archives, appends URLs to `data/published_urls.txt`.
- **Optional editorial pass:** Arman may invoke the **Editor** agent
  between engine output and his ratification. Editor reads the staging
  draft, labels off-voice candidates, flags tradeoffs, proposes The Pulse.
  Editor never auto-releases; Arman runs `--release` himself.
- **Experimentation:** Arman re-runs the pipeline freely. Same-day re-runs
  overwrite `data/staging/<date>/` atomically; nothing touches canonical
  (`data/<date>/`) or `data/published_urls.txt` until `--release`.
  Cross-time dedup, callbacks, and the URL exclusion index all read
  canonical only, so staging churn is invisible to tomorrow's run.
- **Cutoff:** if Arman hasn't released by the configured cutoff
  (operationally: he just hasn't run `--release`), yesterday's canonical
  issue stays live (with its "as of" date). No silent skips. A staging
  draft that never gets released is fine -- it stays in
  `data/staging/<date>/` as evidence of the attempt and gets overwritten
  next time the engine runs for that date.

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

---

## Archive — `data/YYYY-MM-DD/`

Locked for v0: **JSON-per-day, no SQLite.** If query patterns warrant
SQLite later, a lazy `src/index.py` builds it from JSONL on demand;
Architect authorises.

> **Two archive states (staging vs canonical).** The table below describes
> the **canonical** archive at `data/<date>/` -- the released, immutable
> record. Every file in the table also lives, with the same shape, under
> `data/staging/<date>/` while the engine is iterating on a draft. Default
> `python -m src.run` writes to staging; `python -m src.run --release`
> promotes staging to canonical, assigns `Issue.issue_number`, and
> appends URLs to `data/published_urls.txt`. Cross-time dedup,
> callbacks, the URL exclusion index, and the eval harness all read
> **canonical only** -- staging is invisible to history. See DESIGN.md
> "Archive: staging vs canonical" for the full state model.

| File | Writer | Schema owner | Read by |
|---|---|---|---|
| `items.jsonl` | Source Engineer | Architect | Retrieval, Eval, Release (provenance views) |
| `source_health.json` | Source Engineer | Architect | Eval, Release, Source (trust-weight decay) |
| `clusters.jsonl` | Retrieval Engineer | Architect | LLM Engineer (current + last 14 days), Eval |
| `ranked.jsonl` | LLM Engineer | Architect | Editor, Eval, Release (archive views) |
| `issue.json` | LLM Engineer (staging); `src/run.py --release` (canonical, assigns `issue_number`) | Architect | Editor, Arman, Release, Eval, LLM Engineer (callbacks -- canonical only) |

**Rules:**
- Atomic writes (`.tmp` + fsync + rename). Half-written files are worse
  than missing ones.
- `schema_version` on every artifact. Bump on shape change; record diff in
  DESIGN.md.
- Readers tolerate missing days. A stage that didn't run yesterday must
  not crash today.
- Every ratified `issue.json` is **labelled data** — Arman approved it.
  Over months, the archive becomes the most valuable artifact in the
  repo. Cross-time dedup, "Where it's heading" trend reads, voice
  baselines, and callbacks all depend on it.

---

## Day-one validation (PLAN §7) — named owner, three blocking questions

**Owner:** Release Engineer (drafts) + Architect (records answers) +
Arman (sends, decides).

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
   (sources + rubric), `templates/` (HTML), `evals/` (Eval Engineer's
   yard), `docs/` (Pages publish surface + design + editorial), `data/`
   (per-day archive — the project's labelled corpus over time).
3. **The seams** — see the handoff diagram. Artifacts at every seam are
   pydantic-typed; Architect owns the shapes.
4. **The non-negotiables** — design before code; evals before features;
   determinism in code, judgment in the LLM; subscribe, don't scrape;
   nothing publishes without Arman's ratification.
5. **Who to ask** — see the decision-rights table. When in doubt, ask the
   Architect.
6. **The three skills** — `design-first-eval-first` (your pre-PR
   checklist), `editorial-focus` (what AI Vector covers and why — Agentic +
   Gen-AI heavy, signal-first), and `finance-lens` (your "does this matter
   to FS?" rubric). Read them once before your first PR; invoke them in
   flow.
7. **Veto powers** — Eval Engineer has hard veto on dedup/ranking/voice/
   integrity regressions. Don't try to route around it; it's the
   guardrail keeping the publication honest.

Then read `PLAN.md`. Then read `docs/DESIGN.md`. Then read your seat's
agent file in `.claude/agents/`. Then ship something small.

Welcome.
