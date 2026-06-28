# AI Vector — Operator's Handbook

A friendly, problem-first guide. Skim the section that matches what
you're trying to do; everything is in plain language and copy-pasteable.

If you're new, read the next two sections first — they're the whole
picture in 60 seconds. Then jump to whatever you need.

For the full system design (C4 context → containers → components →
data contracts), see [DESIGN.html](DESIGN.html).

---

## How AI Vector works

A daily AI newsletter with a financial-services lens, **curated, not
aggregated**. Each morning the pipeline reads ~60 sources, deduplicates the
inevitable cross-posts and re-reports, scores each story against an
editorial rubric, drafts the ones that earn a slot, and renders a single
readable HTML issue. Every story carries a *direction note* — where this
is heading. A human reviews every draft and ships when ready. Nothing
auto-publishes, by design.

> ### Principle: No Token Wasted
>
> The LLM does the **judgment work** — what matters, what to say, what to
> frame as the Pulse, where the financial-services angle earns a mention.
> Code does **everything else** — fetching, parsing, grouping, rendering,
> scheduling. We never spend LLM tokens or accept LLM non-determinism on
> work that plain code can do reliably.
>
> This is the test for any new piece of work: *could code do this
> reliably?* If yes, code does it. The LLM is reserved for the calls
> only judgment can make.

### Why these six stages

Each stage solves a problem the others can't.

| Stage | What would break without it |
|---|---|
| `fetch`     | No signal at all |
| `cluster`   | Ten feeds produce ten copies of the same story |
| `rank`      | The issue is a chronological list, not an edit |
| `summarise` | The reader gets a link dump, not a newsletter |
| `verify`    | Factual claims go unchecked before Arman reviews (advisory; never blocks) |
| `render`    | The output is JSON, not something a human reads |

Each stage writes a typed file (`items.jsonl` → `clusters.jsonl` →
`ranked.jsonl` → `issue.json`) that the next stage reads. That handoff
contract is what lets you re-run any subset cheaply (see §3) and roll
back cleanly (see §8). Detailed mechanics — what each stage reads, writes,
costs, and how long it takes — live in §2.

---

## Quick reference

| I want to... | Command |
|---|---|
| See today's draft | `aiv run` then `open docs/staging/$(date +%F).html` |
| Ship today's issue | `aiv release` |
| Re-ship a corrected issue (#N.M) | `aiv release --revise --date YYYY-MM-DD` |
| Roll back a bad issue | `aiv unrelease --date YYYY-MM-DD` |
| Re-do a single stage | `aiv run --stage <stage>` |
| Re-process an earlier day | `aiv run --date YYYY-MM-DD` |
| Re-run just verify | `aiv run --stage verify --date YYYY-MM-DD` |
| Skip the verify pass | add `--no-verify` to `aiv run` |
| See what would happen | add `--dry-run` to any command |
| Get more logging | add `--verbose` to any command |
| Just check setup | `aiv check` |

Pipeline stages, in order:
`fetch` → `cluster` → `rank` → `summarise` → `verify` → `render`

---

## 1. Quickstart

```bash
source .venv/bin/activate         # prompt should show (ai-vector)
aiv check                         # confirm setup is healthy
aiv run                           # produce today's staging draft
open docs/staging/$(date +%F).html # review it
aiv release                       # ship if you're happy
```

That's the whole loop. Everything below is for when you want more control
or something goes sideways.

---

## 2. How the pipeline works

Five stages, run in order. Each reads its predecessor's file and writes
its own — that's why you can re-run subsets cheaply.

### `fetch` — pull from sources
- **Reads:** `config/sources.yaml`
- **Writes:** `data/staging/<date>/items.jsonl` + `source_health.json`
- **What it does:** hits ~60 RSS/Atom/API feeds in parallel, normalises each entry into an `Item` record, exact-URL deduplicates.
- **No LLM.** Cost ≈ 0. Time ≈ 15-30 s, dominated by the slowest source.
- **When it goes wrong:** check `source_health.json` for `missed_reason`. Usually a feed redirect, a 4xx/5xx, or a parse error.

### `cluster` — group near-duplicates
- **Reads:** `items.jsonl` + the last 14 days of released centroids
- **Writes:** `clusters.jsonl` + `embeddings/centroids.npz`
- **What it does:** embeds each item with BAAI/bge-base-en-v1.5 locally, runs agglomerative clustering at a cosine threshold, links to prior-day clusters where similar.
- **No LLM.** Cost ≈ 0. Time ≈ 20-60 s; first run downloads the ~440 MB model.
- **When it goes wrong:** cluster count looks weird → threshold or model regression. Cross-time linking broken → check released centroids exist.

### `rank` — score against the rubric
- **Reads:** `clusters.jsonl`
- **Writes:** `ranked.jsonl`
- **What it does:** one LLM call per cluster — scores 0-100 against `config/rubric.yaml` (significance / hands_on_utility / big_picture_relevance / financial_services_impact / freshness_momentum), tags audiences, assigns a tier (`pulse` / `on_the_radar` / `cut`).
- **Uses LLM.** Cost ≈ $0.05-0.10 with `claude-sonnet-4-6` (~50 clusters × small prompt). Time ≈ 60-90 s.
- **When it goes wrong:** wrong stories surfacing → rubric weights or rank prompt drift. Re-run with `--verbose` to see per-cluster reasoning.

### `summarise` — write headlines + bodies
- **Reads:** `ranked.jsonl`
- **Writes:** `issue.json`
- **What it does:** for top-N stories, fetches the article body via `trafilatura`, then LLM-drafts: headline, body, direction note, signal pill. Writes section intros per section. Assembles into `Issue` model.
- **Uses LLM.** Cost ≈ $0.10-0.15 (~12 stories × larger prompt + section intros). Time ≈ 60-180 s.
- **When it goes wrong:** voice off → prompt drift in `src/summarise.py`. Empty bodies → trafilatura blocked on the source URL.

### `verify` — advisory factual check
- **Reads:** `issue.json` + `source_excerpts.jsonl` (the exact excerpts `summarise` used)
- **Writes:** `data/staging/<date>/verify.json` and updates `issue.json` with per-story flags
- **What it does:** For each story, decomposes the headline and body into atomic factual claims and checks each one against the source excerpt. Each claim receives a verdict: `supported`, `unsupported`, `contradicted`, or `unverifiable`. Headline errors are flagged most prominently because readers trust the headline first. Rolls up to a per-story `has_contradiction`, `has_unsupported`, `headline_flagged`, and a report-level verdict of `clean`, `flagged`, or `unavailable`.
- **Uses LLM.** Cost ≈ $0.02-0.05 (~12 stories × short prompt). Time ≈ 15-30 s.
- **Advisory — never blocks.** If the LLM call fails, the network is unavailable, or `source_excerpts.jsonl` is missing, `verify` writes a `verdict: unavailable` report and the pipeline continues to render. The staging preview shows no flags; the release proceeds normally.
- **Flags are for Arman's review only.** When flags are present, the staging HTML preview shows a small amber badge on each flagged story: "unsupported claim" or "headline claim flagged." These badges do not appear in the released reader-facing HTML.
- **When it goes wrong:** `verify.json` will say `unavailable` with a note. To re-run it alone: `aiv run --stage verify --date <date>`. To skip it entirely: `--no-verify`.

### `render` — produce HTML
- **Reads:** `issue.json`
- **Writes:** `docs/staging/<date>.html`
- **What it does:** Jinja2 template → static HTML. No LLM, no network.
- **No LLM.** Cost = 0. Time < 1 s.
- **When it goes wrong:** template syntax error or model-field mismatch after a contract change.

### The pipeline as a whole

```
config/sources.yaml ──► fetch ──► items.jsonl ──► cluster ──► clusters.jsonl
                                                                     │
                                                                     ▼
docs/staging/<date>.html ◄── render ◄── verify ◄── issue.json ◄── summarise ◄── ranked.jsonl
                                          │                          ▲              ▲
                                     verify.json                     │              │
                                                                  rank ─────────────┘
```

Total: 2-5 minutes end-to-end. Most of it is `summarise`. `verify` adds 15-30 s.

---

## 3. "I tweaked X — what do I need to re-run?"

You don't need to re-run the full pipeline every time. Each stage reads
its predecessor's file and writes its own. Touch the smallest surface.

| You changed... | Re-run these stages |
|---|---|
| `config/sources.yaml` | `fetch` (then the rest) |
| Clustering threshold in `src/cluster.py` | `cluster` onwards |
| `config/rubric.yaml` | `rank` onwards |
| A prompt in `src/rank.py` | `rank` onwards |
| A prompt in `src/summarise.py` | `summarise` onwards |
| A prompt in `src/verify.py` | `verify` only |
| `templates/issue.html.j2` | `render` only |

Examples:

```bash
# Just re-rank and re-summarise (you tweaked the rubric):
aiv run --stages rank,summarise,render --date 2026-05-24

# Just re-run the factual-verify pass:
aiv run --stage verify --date 2026-05-24

# Just the HTML (you tweaked CSS):
aiv run --stage render --date 2026-05-24
```

Stages always run in pipeline order regardless of the order you pass them.

---

## 4. "The draft looks wrong. How do I fix it?"

Three levers, escalating from cheapest to most disruptive.

**4a. Re-render only** — typos, wording in HTML, CSS:
```bash
# Edit data/staging/<date>/issue.json or templates/issue.html.j2 directly
aiv run --stage render --date <date>
```

**4b. Re-summarise** — voice off, missing direction note, wrong tone:
```bash
# Edit the summarise prompt in src/summarise.py
aiv run --stages summarise,render --date <date>
```

**4c. Re-rank** — wrong story made the cut, weird audience tags:
```bash
# Tweak config/rubric.yaml or the rank prompt in src/rank.py
aiv run --stages rank,summarise,render --date <date>
```

For pure copy-fixes (one word, one headline), editing
`data/staging/<date>/issue.json` by hand and re-rendering is fastest.

---

## 5. "I want to try a different LLM"

Swap one line in `.env`. No code change.

```ini
# OpenAI
LLM_PROVIDER=openai
LLM_ENDPOINT=https://api.openai.com/v1
LLM_MODEL=gpt-4o
LLM_API_KEY=sk-...

# LiteLLM proxy (you've set it up locally)
LLM_PROVIDER=litellm
LLM_ENDPOINT=http://localhost:4000/v1
LLM_MODEL=claude-sonnet-4-6   # whatever alias your proxy maps
LLM_API_KEY=...

# Ollama (local)
LLM_PROVIDER=ollama
LLM_ENDPOINT=http://localhost:11434/v1
LLM_MODEL=llama3.1:70b
LLM_API_KEY=                  # often blank for localhost

# AWS Bedrock
LLM_PROVIDER=bedrock
LLM_ENDPOINT=https://bedrock-runtime.us-east-1.amazonaws.com
LLM_MODEL=anthropic.claude-sonnet-4-6-v1:0
LLM_API_KEY=                  # blank uses ambient AWS creds
```

For a one-off experiment without touching `.env`:

```bash
LLM_MODEL=claude-opus-4-7 aiv run --stages rank,summarise --date <date>
```

Inline env override — runs once, your `.env` is untouched.

---

## 6. "I want to tune the LLM behaviour"

```ini
LLM_TIMEOUT_SECONDS=60         # per-call timeout
LLM_TEMPERATURE_RANK=0.2       # low = stable rankings across re-runs
LLM_TEMPERATURE_SUMMARISE=0.6  # higher = more voice texture
```

Rule of thumb: keep rank temperature low so your ranked.jsonl doesn't
churn between re-runs. Summarise temperature can move with taste.

---

## 7. "I want to release yesterday, not today"

```bash
aiv release --date 2026-05-23
```

Issue numbers go up monotonically — a back-release gets `max(existing) + 1`,
not retrofitted into the sequence. So if your last released was issue #5
on Saturday, releasing Friday's draft on Sunday gives Friday issue #6.

---

## 7a. "I shipped issue #2 but a prompt fix just landed — re-ship it"

You want the corrected issue to be **#2.1**, not #3. Use `--revise`:

```bash
# Re-run the affected stages so staging carries the corrected draft
aiv run --stages summarise,render --date 2026-05-24

# Review the staging preview
open docs/staging/2026-05-24.html

# Ship the revision (preserves issue_number, bumps revision)
aiv release --revise --date 2026-05-24
```

What `--revise` does:
- Reads the existing released `data/released/<date>/issue.json` to learn
  its `issue_number` (e.g. 2).
- Bumps `revision` by 1 (0 → 1, or 1 → 2, etc.).
- Re-runs the standard release transition: overwrites canonical
  `issue.json` in place, re-copies peripherals, re-renders HTML,
  unions URLs into `published_urls.txt`.
- The masthead reads "Issue No. 2.1"; the archive listing reads
  "No. 2.1". The integer registry is **not** touched — no #3 is burned.

Without `--revise`, `aiv release` on an already-released date errors
(`AlreadyReleased`) so accidental double-fires are safe.

There is only one `issue.json` per date on disk — revisions overwrite.
The audit trail is `git log data/released/<date>/issue.json`.

---

## 7c. "⚠ DUPLICATE RISK — earlier issues are staged but not released"

You staged several days in a row without releasing them, and now the
pipeline prints a loud `DUPLICATE RISK` banner (and the staging preview
HTML shows a red banner at the top). Here's what it means and how to fix it.

**Why it happens.** Cross-time dedup — the thing that stops "OpenAI launches
GPT-X" running three days straight — reads the **released** archive only.
A draft still sitting in `data/staging/` is invisible to it. So if you build
May 31 while May 29 and May 30 are staged-but-unreleased, May 31's dedup
never saw them and can repeat their stories verbatim.

**The fix — release oldest-first, then re-run the later day:**

```bash
aiv release --date 2026-05-29     # now dedup can see it
aiv release --date 2026-05-30     # now dedup can see this too
aiv run     --date 2026-05-31     # rebuild — dedup now catches the repeats
```

The banner lists the exact dates and the exact commands; copy them straight
out. After the re-run, the banner disappears (nothing earlier is unreleased)
and the repeated stories will carry a `prior_coverage_ref` callback instead
of appearing fresh.

**It's a warning, not a block.** Releasing out of order is legitimate, so
nothing is gated — `aiv release` still works. The guard just makes sure you
*know* before you publish. The window it checks matches the dedup lookback
(14 days), so a long-abandoned staging dir from a month ago won't trip it.

---

## 7d. "The staging preview shows amber flags on a story. What does that mean?"

The advisory verify stage checks factual claims in each story's headline and
body against the exact source excerpt that `summarise` used. When it finds a
claim that appears to be unsupported or contradicted by the source, it marks
the story with a flag. These flags are visible only in the staging preview
(`docs/staging/<date>.html`) and are for your review before release. They do
not appear in the published reader-facing HTML.

**Flag meanings:**

- **"headline claim flagged"** (red badge): a claim in the headline is either
  unsupported by the source or directly contradicted. This is the most severe
  finding — the headline is the first thing readers see and trust.
- **"contradicted"** (red badge): a body claim is directly contradicted by
  the source excerpt (the source says something incompatible with the claim).
- **"unsupported claim"** (amber badge): a body claim is absent from the
  source — it may be editorial framing rather than something the source
  states. Often benign; read the note in `verify.json` to decide.

**These are advisory.** The verify stage is a calibrated sanity check, not a
gating judge. It will occasionally surface false positives (for example, when
the claim is drawn from a secondary source or from common knowledge not
present in the excerpt). Use your judgment.

**What to do when you see a flag:**

1. Open `data/staging/<date>/verify.json` and find the story. Each flagged
   claim has a `source_span` and `note` field showing exactly what the
   verifier saw and why it flagged.
2. If the concern is real — edit the headline or body in
   `data/staging/<date>/issue.json` and re-render:
   ```bash
   aiv run --stage render --date <date>
   ```
3. To re-run the verifier after editing (so the flag clears):
   ```bash
   aiv run --stage verify --date <date>
   ```
4. If the flag is a false positive — proceed with `aiv release` as normal.
   Flags do not block release.

**To skip verify entirely on a given run:**

```bash
aiv run --no-verify --date <date>
aiv run --stages rank,summarise,render --date <date>   # verify auto-fires with summarise; this skips it
aiv run --stages rank,summarise,render --no-verify --date <date>  # explicit
```

**To check what verify found without re-running the pipeline:**

```bash
jq '.verdict, .verdict_counts' data/staging/<date>/verify.json
jq '.stories[] | select(.has_contradiction or .has_unsupported or .headline_flagged) | {story_id, headline_flagged, has_contradiction, has_unsupported}' data/staging/<date>/verify.json
```

---

## 8. "I released something bad. How do I undo?"

```bash
aiv unrelease --date 2026-05-24 --dry-run   # see what would happen
aiv unrelease --date 2026-05-24             # actually do it
```

`--date` is required — no implicit "today" so you can't fumble it.

Unrelease:
- Deletes `data/released/<date>/` and `docs/released/<date>.html`
- Rebuilds `data/published_urls.txt` from the remaining released issues
- Preserves the issue-number gap (the deleted integer doesn't get reused)

The staging draft survives. You can edit it, then `aiv release` again — but
it gets a **new** integer issue number (`max(existing) + 1`). The old
integer is gone forever, by design.

**When to use unrelease vs `aiv release --revise`:**

- **`--revise`** (see §7a) is the right path for a *correction* to an
  already-released date: keeps the integer, bumps revision (#N → #N.1).
  No gap. Use this for prompt-drift fixes, copy edits, or any time you
  want the public identifier to signal "update" rather than "new issue."
- **Unrelease** is the right path when the issue was *published in
  error* and the entire archive entry should be reset. The integer
  becomes a permanent gap; the next release of that date starts at
  `revision = 0` again.

Revision counters do **not** survive a full unrelease — once you
unrelease, the next first release of that date starts at `#N` not
`#N.M`.

---

## 9. "Something feels off. How do I peek under the hood?"

Everything is JSON / JSONL in `data/staging/<date>/`:

```bash
DATE=2026-05-24

# How many items survived fetching?
wc -l data/staging/$DATE/items.jsonl

# Which sources fired today?
jq '.sources[] | select(.fired) | .name' data/staging/$DATE/source_health.json

# Which sources missed and why?
jq '.sources[] | select(.fired | not) | {name, missed_reason}' \
  data/staging/$DATE/source_health.json

# Top 5 ranked clusters
jq -s 'sort_by(-.score) | .[0:5] | .[] | {score, canonical_title, tier}' \
  data/staging/$DATE/ranked.jsonl

# Today's section structure
jq '.sections[] | {name, count: (.stories | length)}' \
  data/staging/$DATE/issue.json
```

Files are atomic-written (`.tmp` + rename), so you'll never see a
half-written file mid-pipeline. Safe to inspect during a run.

---

## 10. "A source went dead. What now?"

`source_health.json` will show it with `fired: false` and a `missed_reason`
like `http_error`, `timeout`, or `parse_error`.

1. **Confirm it's not transient** — re-run `aiv run --stage fetch --date <date>`.
2. **If persistent**, disable in `config/sources.yaml`:
   ```yaml
   - name: that_source
     enabled: false
     # ... add a note about when and why
   ```
3. **Document** in `docs/internal/SOURCES_RESEARCH.md` so you remember why.

If it was load-bearing, log it as a task and find a replacement source.

---

## 11. "I want to see what would happen before doing it"

Add `--dry-run` to any command:

```bash
aiv run --dry-run                     # lists each stage's intended outputs
aiv release --dry-run                 # lists the release transition steps
aiv unrelease --date <d> --dry-run    # lists files that would be deleted
```

Always use `--dry-run` before:
- A back-release (`aiv release --date <earlier>`)
- An unrelease (you're about to delete tracked files)
- The first run after a config or code change you're not sure about

---

## 12. "It's running slow / wasting tokens. What can I skip?"

```bash
aiv run --skip-preflight       # skip embedding + LLM endpoint checks
aiv run --no-verify            # skip the advisory factual-verify pass
aiv run --stage fetch          # fetch alone — no LLM cost at all
aiv run --stages fetch,cluster # gather + group, still no LLM
```

A full `aiv run` is ~$0.10–0.20 of Anthropic spend (depends on item count
and model). Skipping `rank` + `summarise` removes the bulk of the LLM cost.
`verify` adds a small incremental cost (~$0.02-0.05); skip it with
`--no-verify` when you just want to re-render quickly or when the LLM
endpoint is unavailable.

For iteration: do one full run to get fresh data, then loop on
`--stages rank,summarise,render` while tweaking prompts.

---

## 13. "What's safe to edit by hand?"

| File | Edit by hand? | Notes |
|---|---|---|
| `data/staging/*/issue.json` | Yes | Re-render after with `aiv run --stage render` |
| `data/staging/*/verify.json` | No | Auto-written by verify stage; re-run `aiv run --stage verify` |
| `data/staging/*/ranked.jsonl` | Yes | Re-summarise after |
| `data/staging/*/clusters.jsonl` | Cautious | Usually easier to re-cluster |
| `data/staging/*/items.jsonl` | No | Re-run fetch instead |
| `data/released/*/*` | **No** | Released = immutable. Use `aiv unrelease` first |
| `data/published_urls.txt` | No | Auto-managed by release/unrelease |
| `config/sources.yaml` | Yes | Source Engineer's domain; document why |
| `config/rubric.yaml` | Yes | LLM Engineer's domain; affects all future issues |
| `templates/issue.html.j2` | Yes | Release Engineer's domain |
| `templates/index.html.j2` | Yes | Release Engineer's domain |

---

## 14. "Help — I'm worried I'll break something"

The cheapest, lowest-cost safety net is git itself:

```bash
git add -A && git stash push -m "before-$(date -u +%Y%m%dT%H%M%SZ)"
# ... do the risky thing ...
# if it goes wrong:
git stash pop
```

For released issues, worst case you can `git checkout HEAD~1 -- data/released/<d>/ docs/released/<d>.html` to recover the prior state from git history.

Released files are tracked. Staging is gitignored — when in doubt, you can
always blow away `data/staging/<d>/` and re-run.

---

## 15. Troubleshooting

**`aiv: command not found`** — the venv isn't activated. Run
`source .venv/bin/activate`. Your prompt should show `(ai-vector)`.

**`LLM_PROVIDER is unset`** — `.env` didn't load. Either:
- `python-dotenv` isn't installed (you're not in the venv)
- `.env` doesn't exist (copy from `.env.example`)

**`No module named 'huggingface_hub'`** — venv missing dependencies.
`pip install -e .` from the repo root.

**Pre-flight LLM check fails with auth error** — wrong API key or wrong
endpoint. Confirm with: `curl -i $LLM_ENDPOINT/v1/messages` (Anthropic)
or `/chat/completions` (OpenAI-compatible).

**A stage hangs forever** — the LLM call may be slow. `LLM_TIMEOUT_SECONDS`
defaults to 60. Bump if you're using a slow model or local Ollama.

**Released issue HTML looks broken locally but fine on GitHub Pages** —
fonts are loaded via relative paths (`../fonts/fonts.css`). Open via a
local server (`python -m http.server` from `docs/`) rather than `file://`.

**`aiv release` says "no staging draft"** — you haven't run `aiv run` for
that date yet, or the date you passed doesn't match what's in
`data/staging/`.

**`aiv release` says "already released"** — that date has an `issue.json`
in `data/released/`. Two options:
- `aiv release --revise --date <d>` if you want a corrected re-release
  that bumps the revision (#N → #N.1). Preserves the integer; no gap.
  See §7a.
- `aiv unrelease --date <d>` then re-release if you want to scrap the
  entry entirely. Burns the old integer as a permanent gap; next
  release starts fresh at `revision = 0`. See §8.

---

## 17. Running evals before push

The pre-push convention is one command:

```bash
aiv eval && git push
```

If the eval passes, the push proceeds. If it fails, you see why before
anything hits the remote. This is the main loop for any change touching
`src/cluster.py`, `src/rank.py`, `src/summarise.py`, `config/rubric.yaml`,
or any LLM Engineer prompt.

### Flags

```bash
aiv eval                   # full suite: reference metrics + LLM judge
aiv eval --no-judge        # fast + free: skip the LLM judge; use for tight iteration
aiv eval --judge-only      # voice checks only: skip reference-based metrics
aiv eval --date 2026-05-24 # run against a specific archive date
aiv eval --fixture _synthetic  # plumbing test: confirms the harness doesn't crash
aiv eval --vs evals/reports/<prev>.json  # diff today's results against a prior report
```

`--no-judge` costs nothing — it runs dedup, ranking Spearman, and module
integrity checks without any LLM calls. Use it every time you change
deterministic code. Run the full suite (with judge) before pushing prompt
or rubric changes.

### What the eval gate covers

| Change type | Minimum eval to run |
|---|---|
| `config/rubric.yaml` or rank/summarise prompts | `aiv eval` (full, with judge) |
| `src/verify.py` or verify prompt | `aiv eval --no-judge` (integrity check) |
| `src/cluster.py` or clustering threshold | `aiv eval --no-judge` |
| `src/fetch.py` or `config/sources.yaml` | `aiv eval --no-judge` |
| Template or CSS only | skip — render has no eval gate |
| Schema change in `src/models.py` | `aiv eval --no-judge` (integrity check) |

### Output

The eval writes a timestamped JSON report to
`evals/reports/YYYY-MM-DD/HHMMSS.json`. That directory is gitignored
(generated outputs), but `evals/reports/weekly/` is tracked — it holds
the Eval Engineer's curated weekly behavioural-integrity notes.

### The publish gate (you can't accidentally ship a broken issue)

`aiv release` now runs `check_integrity()` against the staging draft
automatically. If staging has fewer than 3 hands_on stories, no pulse,
a source fire rate below 0.80, or a `score ≥ 35` cluster wrongly
tiered as `cut`, the release is **refused** with a clear list of
failures:

```
release: staging integrity check FAILED for 2026-05-25: refusing to release.
  - PIPELINE HEALTH: issue.json has 1 hands_on story (minimum 3 required)
release: refusing to publish. Fix the staging draft (re-run the pipeline
or the failing stage) OR pass --force to bypass (logged as a WARNING for audit).
```

Two paths from here:
- **Fix the staging draft** — usually a re-run with `aiv run --stages
  rank,summarise,render --date <date>` after addressing the cause
  (often a thin news day or a prompt that just landed).
- **`aiv release --force`** — bypass the gate. Each bypassed assertion
  is logged at WARNING level. Use sparingly; the audit log is the only
  thing standing between intentional override and silent regression.

The gate was added after a 3-story draft shipped publicly because nothing
prevented it (Issue #2.1 on 2026-05-24, postmortem in
`evals/failure_modes.md` FM-13). The gate makes that class of mistake
structurally impossible without explicit `--force`.

---

## 16. When to bring in the team

For anything beyond daily operation, see `docs/internal/TEAM.md` for the
agent roster. In short:

- Source not fetching, source health weirdness → **Source Engineer**
- Stories not deduplicating, embedding tuning → **Retrieval Engineer**
- Ranking off, voice drift, prompt changes → **LLM Engineer**
- Editorial calls (what's a Pulse-worthy story?) → **Editor**
- HTML / CSS / templates / GitHub Pages → **Release Engineer**
- Quality regressions, eval gates → **Eval Engineer**
- Cross-module changes, data contracts → **Architect**

CLAUDE.md has the full table with when-to-invoke guidance.
