# AI Vector — Build Plan

> **AI Vector** — *Today's AI, with a heading.*
> A daily, agent-assisted AI newsletter for engineers, data scientists, and senior
> leaders — with a sharp eye on what it means for financial services.

**Author:** Arman
**Repo:** `ai-vector`
**Status:** shipping daily. Phases 0–3 complete; the engine runs on a
weekday schedule and the unattended-publish gate is rolling out by phase.
This document keeps the original plan text and marks what has landed —
sections describing shipped work say so in their heading.

---

## 0. Working philosophy (read first)

This project is **design-first and eval-first**. Do not start writing the
collector until the contracts and the eval harness exist. Concretely:

1. **Design before code.** Lock the data contracts (what an "item" is, what a
   "story cluster" is, what an "issue" is) before implementing anything.
2. **Evals before features.** Before tuning ranking or summaries, build a small
   labelled fixture set and a scoring script. We optimise against the rubric,
   not against vibes.
3. **Determinism in code, judgment in the LLM.** Fetching, parsing, scheduling,
   rendering = plain code. Dedup, ranking, summarisation = the LLM. Never pay
   LLM latency/cost/non-determinism for work code can do reliably.
4. **Subscribe, don't scrape.** RSS/Atom/API first. HTML scraping is a rare,
   isolated fallback — never the default. (Compliance + fragility.)

---

## 1. What we're building

A single repo that is **engine + published site + scheduler**, all on GitHub:

```
cron (GitHub Actions, weekday mornings — Sydney Mon–Fri)
  → fetch sources (RSS/API)            [code]
  → normalise + cluster duplicates     [code + embeddings]
  → rank clusters vs. interest rubric  [LLM]
  → summarise + add "direction" note   [LLM]
  → verify claims against sources      [LLM, advisory]
  → render HTML issue                  [code/templates]
  → review against the editorial bar   [LLM; verdict computed by code]
  → revise what the review flagged     [LLM inside; loop control in code]
  → gate: may this publish unattended? [code — no LLM, fails closed]
  → write to /docs, commit             [code]
  → GitHub Pages publishes             [automatic]
```

The last three steps are the 2026-08 additions and they divide cleanly along
the No Token Wasted line. `review` and `revise` are judgment: is this good
enough, and what would make it better. The **verdict** that comes out of the
review is computed by code from finding severities under a ratified
threshold table, and the **gate** that acts on it is code end to end. Asking
a model "should we publish?" when the inputs are two verdict tokens and a
hash comparison would buy non-determinism exactly where we least want it.

No external infra. Publishing surface is **GitHub Pages** (`/docs` folder),
chosen because internal bank GitHub permits it where other deployments are hard.

### Editorial identity (this is the product, not the plumbing)
Every issue must **point**, not just list. A vector has direction: each issue
says *where the field moved today and which way it's heading*. Sections should
carry a clear point of view and an explicit financial-services lens. The name is
~10% of the warmth; the **voice** is the rest.

---

## 2. Phase 0 — DESIGN — **SHIPPED**

Deliverables, all as files in the repo:

- `docs/DESIGN.md` — the data contracts below, written out and agreed.
- `config/sources.yaml` — the source list (see §6), each with: name, url,
  type (`rss|atom|api`), category, trust weight (1–5), enabled flag.
- `config/rubric.yaml` — the relevance rubric (see §5).
- Decision log of open questions (§8) with answers as they're resolved.

### Core data contracts (define as typed models — pydantic)
- **Item**: one raw entry from one source.
  `{id, source, url, title, published_at, raw_summary, fetched_at}`
- **Cluster**: a set of Items judged to be the same story.
  `{cluster_id, item_ids[], canonical_title, sources[], earliest_published}`
- **RankedStory**: a scored cluster ready to write.
  `{cluster_id, score, audience_tags[], rationale}`
- **IssueSection / Issue**: the rendered structure (see §4).

---

## 3. Phase 1 — EVAL HARNESS — **SHIPPED**

- `evals/fixtures/` — ~30–50 hand-saved real items across a few days,
  including deliberate near-duplicates (same story from 5 sources).
- `evals/labels.yaml` — for the fixtures: which items are the same story
  (dedup ground truth) + a human relevance score 1–5 per story.
- `evals/run_evals.py` — reports two numbers:
  - **Dedup quality**: precision/recall of clusters vs. labelled groupings.
  - **Ranking quality**: rank correlation (Spearman) of LLM scores vs. human
    labels.
- Rule: any change to dedup or ranking logic must be run through this and not
  regress. This is the guardrail that keeps "viral" honest — quality is measured.

---

## 4. Phase 2 — PIPELINE — **SHIPPED** (built in this order)

Each step is its own module in `src/`, independently testable.

1. **`fetch.py`** — read `sources.yaml`, pull via `feedparser` (RSS/Atom) and
   `httpx` (APIs: Hacker News Algolia, HF Daily Papers). Dedup *exact* URLs.
   Emit `Item[]`. No LLM here.
2. **`cluster.py`** — embed titles+summaries (embeddings via LiteLLM/Bedrock),
   cluster near-duplicates (cosine threshold or agglomerative). Emit `Cluster[]`.
   *This is the step that makes 10 feeds not produce 10 copies of one story.*
3. **`rank.py`** — one LLM pass per cluster: score against `rubric.yaml`, tag
   audiences, write a one-line rationale. Emit sorted `RankedStory[]`.
4. **`summarise.py`** — per top-N story: write the summary + the **"direction"
   note** (where this points) + financial-services angle when relevant. For
   distribution, **link out + summarise; never reproduce full articles**
   (copyright).
5. **`verify.py`** *(added 2026-06)* — decompose each headline and summary
   into atomic factual claims and check each against the source excerpt
   `summarise` used. Advisory: on any failure it writes an `unavailable`
   report and the pipeline continues.
6. **`render.py`** — Jinja2 → HTML. Mobile-first, clean, fast. Writes
   `docs/index.html` (latest) + `docs/released/YYYY-MM-DD.html`.
7. **`review.py`** *(added 2026-07; structured 2026-08)* — the editor's
   pre-release pass. Emits `review.json`: findings, each resolved to one
   editable field, plus a `computed_verdict` that **code** derives from the
   finding severities under `config/review_thresholds.yaml`. `review.md` is
   rendered from it.
8. **`revise.py`** *(added 2026-08)* — acts on the findings. Rewrites the
   flagged field, one LLM call per field, with a code-side validation gate
   on every replacement. Logs every proposal, application and refusal to
   `revisions.jsonl`.
9. **`gate.py`** *(added 2026-08)* — the unattended-publish decision.
   Deterministic code, no LLM, fails closed. Not a pipeline stage: it runs
   after the pipeline, from the publish workflow.
10. **`run.py`** — orchestrates the above, idempotent, safe to re-run the
    same day. Also owns the **revision loop**: after the review, up to two
    cycles of [revise → re-verify the touched stories → render → review],
    stopping the moment the computed verdict stops improving. What to change
    is judgment and lives in `revise.py`; when to stop is arithmetic and
    lives here.

### Issue structure (current, v0.8 — 2026-05-24 section rename)
- **The Pulse** — the single most important thing today, 2–3 sentences. (warmth
  + signal; quiet nod to the heartbeat lineage)
- **The Big Picture** — strategic + financial-services implications (audience tag: `big_picture`).
- **Hands-On** — practical tips / tools / repos for engineers & DS (audience tag: `hands_on`).
- **On the Radar** — terse linked list of the remaining stories.
- Footer: author, date, "Today's AI, with a heading," archive link.

> Earlier drafts split this into Pulse / Where it's heading / For builders /
> For leaders / Also notable. "Where it's heading" was absorbed into prose
> (direction lives in each summary, not a separate section); builders +
> geeks merged into Hands-On; "Also notable" relabelled to On the Radar.
> v0.8 (2026-05-24) also renamed audience tags and rubric criteria to
> match section names: `leader` → `big_picture`, `builder` → `hands_on`,
> `leadership_relevance` → `big_picture_relevance`,
> `builder_utility` → `hands_on_utility`.

---

## 5. Relevance rubric (`config/rubric.yaml` — first cut)

Score each story 0–100, weighted:
- **Significance** (is this a real shift vs. noise?) — 30
- **Hands-on utility** (`hands_on_utility`, can an engineer/DS act on it?) — 25
- **Big-picture relevance** (`big_picture_relevance`, strategy, risk, governance) — 20
- **Financial-services impact** (banking, regulation, risk, fraud, agents in
  finance) — 15
- **Freshness / momentum** (breaking, or building) — 10

Tag each story with audiences: `hands_on | big_picture | finance | general`.
The LLM returns score + per-criterion breakdown + rationale (for the eval
harness and for transparency).

---

## 6. Sources (`config/sources.yaml` — starting set)

All RSS/API; trust-weighted. (Engineer to add/remove freely.)

- **Labs/primary:** Anthropic, OpenAI, Google DeepMind, Google Research, Meta AI,
  Microsoft Research, Mistral, Cohere, Hugging Face blog, LangChain, LlamaIndex.
- **Papers (curated):** Hugging Face Daily Papers (preferred over raw arXiv);
  arXiv cs.AI/cs.CL/cs.LG as optional firehose.
- **Newsletters/analysis:** Import AI, The Batch, Ahead of AI, Latent Space,
  TLDR AI, Last Week in AI, Simon Willison's blog.
- **News/community:** Hacker News (Algolia API, points threshold), Ars Technica
  AI, MIT Tech Review, The Verge AI, VentureBeat AI, r/LocalLLaMA + r/ML (API).
- **Finance-AI lens (to source):** add 3–5 fintech/banking-AI feeds so the
  finance angle has primary inputs, not just inference.
- **Excluded:** X/Twitter, LinkedIn (APIs closed/expensive, scraping = ToS
  violation).

---

## 7. Phase 3 — SCHEDULE & PUBLISH — **SHIPPED 2026-08-02**

- `.github/workflows/daily.yml`: `schedule:` cron at 21:00 UTC on days 0–4,
  which is **Monday–Friday mornings in Sydney** — the newsletter publishes
  on weekdays only. The issue date is computed from Sydney wall-clock, never
  from the runner's UTC clock, so only the wake-up hour drifts seasonally.
  `workflow_dispatch` remains for manual and backfill runs.
- **Monday absorbs the weekend.** The gap-aware fetch window in
  `src/fetch.py` widens each source's recency window by the days elapsed
  since the last release, so a two-day gap produces one fuller issue rather
  than two thin ones.
- Job: checkout → install → `aiv run` → `aiv release` on a
  `release/<date>-issue-<N>` branch → open a PR. Merging the PR is the
  ratification, and — because Pages serves `docs/` from `main` — the
  publish. The workflow never touches `main` directly.
- **Unattended publish is gated, and the gate is phased.** `aiv gate` writes
  `data/staging/<date>/gate.json` after every run. What acts on that
  decision depends on `AIV_AUTO_PUBLISH_PHASE`: `shadow` (default — compute
  and record only) → `green_only` → `green_amber`. Unset or unrecognised
  resolves to `shadow`. See DESIGN.md "Unattended publish".
- At most one `release/` PR may be open at a time; two would both derive the
  same issue number from `main`.
- Secrets: LiteLLM/Bedrock endpoint + key as repo secrets.
- GitHub Pages serves from `/docs`.

### ⚠️ Day-one validation (can break the whole plan — check before building far)
1. Does internal bank GitHub have **Actions + `schedule:` triggers** enabled?
2. Is there **outbound egress** from Actions runners to (a) the RSS/API sources
   and (b) the LiteLLM/Bedrock endpoint?
3. Is **GitHub Pages** enabled on internal GitHub for this org?

If `schedule:` is blocked → fallback to `workflow_dispatch` + an external nudge.
If egress is blocked → fetch may need to run from an approved network.
**Resolve these before investing in the pipeline.**

---

## 8. Open questions (decision log — answer as we go)

- [ ] Language/stack: Python assumed (feedparser, httpx, pydantic, jinja2). OK?
- [ ] Embeddings model available via your LiteLLM/Bedrock? Which one?
- [ ] How many stories per issue? (suggest 8–12 ranked + an "On the Radar" tail)
- [ ] Archive: flat dated HTML now; index page later?
- [ ] Distribution beyond Pages later (email digest)? Out of scope for v0.
- [ ] Finance-AI sources: which specific feeds? (need your input)
- [ ] Post-Phase A ratification: (a) implement `empty_feed_expected` MissedReason per architect design (needs eval-engineer co-review for `evals/` filter updates; arXiv weekend skipDays currently flagged as `empty_feed`); (b) trial-enable arXiv `cs.LG` with follow-up eval to test the "signal gap" gate from `config/sources.yaml`.

- [ ] **Voice drift in section intros and closings** (flagged across issues #8 – #11, 2026-05-30 to 2026-06-02): the editor's pre-release reviews caught a recurring pattern across four consecutive issues. Two layers:

  - **Symptom layer**: the LLM repeats constructions across days because it has no memory of what it just wrote. Examples: "X outruns Y" appearing in three consecutive Big Picture intros; "Verify before you [X]" recurring as Hands-On opener; on June 2 the Big Picture and Currents intros shared the same thesis statement in one issue.

  - **Root layer**: when the editorial position is under-specified, the LLM falls back on its training default — balanced, hedged, cautious. That register reads as voice drift because it is voice absence, not voice presence.

  **Proposed fix (two parts):**

  1. **Voice-diversity prompt injection in `src/summarise.py`**: pass the last 3 – 5 published issues' intros and closings into the per-story and section-intro prompts with an explicit instruction to vary the register from these. Maintain a growing anti-pattern list seeded from editor reviews. Owner: llm-engineer. Effort: ~2 hrs. Cost: a few hundred extra input tokens per call (≈ free at scale).

  2. **`EDITORIAL.md` strengthening**: shift framing from "be in voice" to "AI Vector holds positions; hedge-y register is voice absence." Add positive examples per section that exemplify *strong* register, not just *competent* register. Owner: editor. Effort: ~1 hr.

  **Verification**: after the fix lands, the next issue's editor review should not flag the same constructions. Voice-similarity check against the last 5 issues' intros could be added later as a register-drift eval signal.

  Reference: structured discussion in conversation on 2026-06-02 ("what are ways to fix the voice drift"). Anti-patterns to seed the prompt with are catalogued across `data/released/2026-05-{29,30,31}/review.md` and `data/released/2026-06-{01,02}/review.md`.

---

## 9. Definition of done (v0.1) — **MET 2026-08-02**

- ✅ One command (`aiv run`) produces a real, good-looking HTML issue from
  live sources, deduped and ranked, written to `docs/`.
- ✅ The scheduled Action runs it unattended on weekday mornings and Pages
  serves it. Unattended *publishing* is separately gated and phased — see §7.
- ✅ Eval harness runs green (dedup + ranking don't regress) and gates every
  prompt or model change before merge.
- ✅ README credits Arman as author, dated, from commit one.

**What v0.1 does not yet include:** email distribution (§8, out of scope),
and phases beyond `shadow` on the unattended-publish gate.

---

## 10. First commands for Claude Code *(historical — Phase 0, 2026-05)*

> Kept as the record of how the project started. Superseded by the shipped
> repo; do not follow it as instructions today.
> Start with Phase 0 only. Do not build the pipeline yet.
1. Scaffold the repo structure (see tree below) with empty/typed stubs.
2. Write `docs/DESIGN.md` with the data contracts from §2.
3. Write first-cut `config/sources.yaml` and `config/rubric.yaml`.
4. Write the README with authorship baked in.
5. Stop and let me review before Phase 1.

```
ai-vector/
├─ .github/workflows/daily.yml
├─ src/{fetch,cluster,rank,summarise,render,run}.py
├─ config/{sources.yaml,rubric.yaml}
├─ templates/issue.html.j2
├─ evals/{fixtures/,labels.yaml,run_evals.py}
├─ docs/                      # GitHub Pages serves this
│  ├─ index.html
│  └─ archive/
├─ docs/DESIGN.md
└─ README.md
```
