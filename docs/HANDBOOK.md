# AI Vector — Operator's Handbook

A friendly, problem-first guide. Skim the section that matches what
you're trying to do; everything is in plain language and copy-pasteable.

If you're new, start at **§1 Quickstart**. Otherwise, jump to the section
that matches your problem.

---

## Quick reference

| I want to... | Command |
|---|---|
| See today's draft | `aiv run` then `open docs/staging/$(date +%F).html` |
| Ship today's issue | `aiv release` |
| Roll back a bad issue | `aiv unrelease --date YYYY-MM-DD` |
| Re-do a single stage | `aiv run --stage <stage>` |
| Re-process an earlier day | `aiv run --date YYYY-MM-DD` |
| See what would happen | add `--dry-run` to any command |
| Get more logging | add `--verbose` to any command |
| Just check setup | `aiv check` |

Pipeline stages, in order:
`fetch` → `cluster` → `rank` → `summarise` → `render`

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

## 2. "I tweaked X — what do I need to re-run?"

You don't need to re-run the full pipeline every time. Each stage reads
its predecessor's file and writes its own. Touch the smallest surface.

| You changed... | Re-run these stages |
|---|---|
| `config/sources.yaml` | `fetch` (then the rest) |
| Clustering threshold in `src/cluster.py` | `cluster` onwards |
| `config/rubric.yaml` | `rank` onwards |
| A prompt in `src/rank.py` | `rank` onwards |
| A prompt in `src/summarise.py` | `summarise` onwards |
| `templates/issue.html.j2` | `render` only |

Examples:

```bash
# Just re-rank and re-summarise (you tweaked the rubric):
aiv run --stages rank,summarise,render --date 2026-05-24

# Just the HTML (you tweaked CSS):
aiv run --stage render --date 2026-05-24
```

Stages always run in pipeline order regardless of the order you pass them.

---

## 3. "The draft looks wrong. How do I fix it?"

Three levers, escalating from cheapest to most disruptive.

**3a. Re-render only** — typos, wording in HTML, CSS:
```bash
# Edit data/staging/<date>/issue.json or templates/issue.html.j2 directly
aiv run --stage render --date <date>
```

**3b. Re-summarise** — voice off, missing direction note, wrong tone:
```bash
# Edit the summarise prompt in src/summarise.py
aiv run --stages summarise,render --date <date>
```

**3c. Re-rank** — wrong story made the cut, weird audience tags:
```bash
# Tweak config/rubric.yaml or the rank prompt in src/rank.py
aiv run --stages rank,summarise,render --date <date>
```

For pure copy-fixes (one word, one headline), editing
`data/staging/<date>/issue.json` by hand and re-rendering is fastest.

---

## 4. "I want to try a different LLM"

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

## 5. "I want to tune the LLM behaviour"

```ini
LLM_TIMEOUT_SECONDS=60         # per-call timeout
LLM_TEMPERATURE_RANK=0.2       # low = stable rankings across re-runs
LLM_TEMPERATURE_SUMMARISE=0.6  # higher = more voice texture
```

Rule of thumb: keep rank temperature low so your ranked.jsonl doesn't
churn between re-runs. Summarise temperature can move with taste.

---

## 6. "I want to release yesterday, not today"

```bash
aiv release --date 2026-05-23
```

Issue numbers go up monotonically — a back-release gets `max(existing) + 1`,
not retrofitted into the sequence. So if your last released was issue #5
on Saturday, releasing Friday's draft on Sunday gives Friday issue #6.

---

## 7. "I released something bad. How do I undo?"

```bash
aiv unrelease --date 2026-05-24 --dry-run   # see what would happen
aiv unrelease --date 2026-05-24             # actually do it
```

`--date` is required — no implicit "today" so you can't fumble it.

Unrelease:
- Deletes `data/released/<date>/` and `docs/released/<date>.html`
- Rebuilds `data/published_urls.txt` from the remaining released issues
- Preserves the issue-number gap (the deleted number doesn't get reused)

The staging draft survives. You can edit it, then `aiv release` again — but
it gets a **new** issue number. The old one is gone forever, by design.

---

## 8. "Something feels off. How do I peek under the hood?"

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

## 9. "A source went dead. What now?"

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

## 10. "I want to see what would happen before doing it"

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

## 11. "It's running slow / wasting tokens. What can I skip?"

```bash
aiv run --skip-preflight       # skip embedding + LLM endpoint checks
aiv run --stage fetch          # fetch alone — no LLM cost at all
aiv run --stages fetch,cluster # gather + group, still no LLM
```

A full `aiv run` is ~$0.10–0.20 of Anthropic spend (depends on item count
and model). Skipping `rank` + `summarise` removes 100% of the LLM cost.

For iteration: do one full run to get fresh data, then loop on
`--stages rank,summarise,render` while tweaking prompts.

---

## 12. "What's safe to edit by hand?"

| File | Edit by hand? | Notes |
|---|---|---|
| `data/staging/*/issue.json` | Yes | Re-render after with `aiv run --stage render` |
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

## 13. "Help — I'm worried I'll break something"

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

## 14. Troubleshooting

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
in `data/released/`. Use `aiv unrelease --date <d>` first, then re-release.

---

## 15. When to bring in the team

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
