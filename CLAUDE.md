# AI Vector — Claude Code instructions

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
| `eval-engineer` | `evals/`, quality gates, regression detection |
| `release-engineer` | `src/render.py`, templates, CI, GitHub Pages |

Use `AskUserQuestion` to surface tradeoffs to Arman when the team is split or
when the decision is irreversible. Don't make structural calls unilaterally.

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
docs/              GitHub Pages surface — HTML only, no markdown
docs/internal/     Living docs — update with the code
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

- No `Co-Authored-By: Claude` lines in commit messages.
- `.env` is never committed. Use `.env.example` as the template.
- Staging is never promoted automatically — Arman reviews and runs `--release`.
- `docs/` serves GitHub Pages. Never put markdown files directly in `docs/` root.
