# AI Vector — Claude Code instructions

## Principle: No Token Wasted

The LLM does the judgment work — what matters, what to say. Code does
everything else — fetching, parsing, grouping, rendering, scheduling. We
never spend LLM tokens or accept LLM non-determinism on work that plain
code can do reliably.

Apply this as a test before adding LLM calls anywhere new: *could code
do this reliably?* If yes, code does it. If you're about to write a
prompt for something a regex, parser, or `if` statement can handle, stop
and write the code instead. If you're about to add deterministic
post-processing because the LLM keeps getting one detail wrong, ask
whether the LLM should be making that call at all.

This principle governs all engineering decisions in this repo and is
called out as a named principle in README.md and `docs/HANDBOOK.md`.

---

## Team

Seven specialist agents live in `.claude/agents/`. For decisions that cross
module boundaries, affect the data contracts, or touch repo structure — consult
the team before acting. Don't assume; ask.

| Agent | When to invoke |
|---|---|
| `architect` | Data contracts, module boundaries, repo structure, cross-cutting refactors |
| `source-engineer` | `config/sources.yaml`, `src/fetch.py`, source health |
| `retrieval-engineer` | `src/cluster.py`, embeddings, near-dedup |
| `llm-engineer` | `src/rank.py`, `src/summarise.py`, prompts, rubric |
| `editor` | Editorial voice, section labels, issue drafting |
| `eval-engineer` | `evals/`, output-quality gates, drift detection |
| `test-engineer` | `tests/`, unit-test quality bar, regression discipline (independent veto on test PRs) |
| `release-engineer` | `src/render.py`, templates, CI, GitHub Pages |

Use `AskUserQuestion` to surface tradeoffs to Arman when the team is split or
when the decision is irreversible. Don't make structural calls unilaterally.

---

## Operator's handbook (`docs/HANDBOOK.md`)

Daily-driver reference for Arman: problem-first, copy-pasteable, covers
every granular control lever (stage subsets, date overrides, dry runs,
LLM provider swaps, manual edits, troubleshooting, etc).

**Hydrate when:** a CLI flag changes; a new operator scenario surfaces
(e.g. "I want to do X" comes up and isn't already covered); a
troubleshooting tip is worth remembering. This doc earns its keep by
being the first thing Arman reaches for when something feels off.
**Owner:** Release Engineer maintains; any agent can propose updates.

---

## Living docs (`docs/internal/`)

These files need to stay in sync with the code. When you change something they
describe, update them in the same commit — not later.

### `docs/internal/DESIGN.md`
The technical source of truth: data contracts (Item, Cluster, RankedStory,
IssueSection, Issue), archive model (staging vs released), embedding contract,
issue numbering registry, schema changelog.

**Hydrate when:** any field is added/removed/renamed in `src/models.py`;
the staging/released path model changes; a new pipeline stage is added;
the embedding model changes; issue numbering logic changes.
**Owner:** Architect. Any change requires Architect review.

### `docs/internal/TEAM.md`
Working agreements: agent roster, decision rights, handoff artifacts, rituals,
risk register.

**Hydrate when:** an agent's scope changes; a new handoff artifact is defined;
a risk item is resolved or a new one appears; decision rights shift.
**Owner:** Architect (structure); each agent updates their own section.

### `docs/internal/PLAN.md`
The build plan and working philosophy. Phases 0–3, open questions, definition
of done.

**Hydrate when:** a phase completes or a significant milestone ships; an open
question is resolved; the definition of done changes. Don't let it describe
things that have already been built as future work.
**Owner:** Arman ratifies; any agent can propose updates.

### `docs/internal/SOURCES_RESEARCH.md`
Research notes behind `config/sources.yaml`: why sources were included or
excluded, feed health history, dependency risks.

**Hydrate when:** a source is added or removed; a feed goes dead; a new
dependency risk is identified.
**Owner:** Source Engineer.

### `docs/internal/PLATFORM_ASKS.md`
Asks to internal-platform owners (GitHub Actions, egress, Pages). Tracks what
was asked, when, and current status.

**Hydrate when:** a new ask is drafted; a response comes back; a blocker is
resolved.
**Owner:** Release Engineer drafts; Arman sends.

---

## Key paths

```
docs/              GitHub Pages surface — HTML + HANDBOOK.md (operator-facing only)
docs/internal/     Living internal docs — update with the code
docs/released/     Published issue HTML (tracked)
docs/staging/      Staging preview HTML (gitignored)
docs/fonts/        Self-hosted woff2 + fonts.css
data/released/     Canonical archive — read by dedup, evals, published_urls
data/staging/      Work-in-progress — invisible to history
data/published_urls.txt  Cumulative URL exclusion index
.claude/agents/    Team agent definitions
.claude/skills/    Shared skills
config/            rubric.yaml, sources.yaml
evals/             Eval harness (fixtures, labels, run_evals.py)
_scratch/          Throwaway working notes — gitignored
```

---

## Conventions

- **Commit messages use Conventional Commits:** `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `style:`, `test:`, `ci:`. No free-form prefixes, no trailing summaries.
- No `Co-Authored-By: Claude` lines in commit messages.
- `.env` is never committed. Use `.env.example` as the template.
- Staging is never promoted automatically — Arman reviews and runs `--release`.
- `docs/` serves GitHub Pages. The only markdown allowed at `docs/` root is `HANDBOOK.md` (operator-facing). Internal living docs go in `docs/internal/`.
