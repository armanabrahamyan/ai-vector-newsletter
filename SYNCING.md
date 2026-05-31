# SYNCING.md — keeping a downstream copy in sync

This repo is the upstream **engine**. A downstream copy runs the same pipeline
with a different brand, source set, and infrastructure.

Principle: **engine changes flow upstream → downstream; site config stays
local to each copy.** Keep the boundary clean and a sync is a near
conflict-free `git merge`.

## Engine vs site

| Syncs (the **engine**) | Stays local (the **site**) |
|---|---|
| `src/*.py` | `config/brand.yaml` |
| `templates/*.j2` | `config/sources.yaml` |
| `tests/`, `evals/` | `config/editorial.yaml` (if tuned) |
| `config/rubric.yaml` | `EDITORIAL.md` |
| `.claude/agents`, `.claude/skills` | `data/`, `docs/released/`, `docs/staging/` |
| `pyproject.toml` | `.env`, `.github/workflows/`, `README.md` |

Rule of thumb: decides **how the pipeline works** → syncs. Decides **what the
publication is** → stays local.

## Branding is config

The published brand lives in `config/brand.yaml` only — `render.py` reads it
and feeds the templates. A downstream copy rebrands by editing that one file;
no template or `src/` edits, so engine syncs never conflict on the name. It
covers reader-facing copy only (title, wordmark, tagline, footer). Omitting
the file falls back to the defaults baked into `render.py`.

It does **not** cover LLM voice prompts (`rank.py`, `summarise.py`,
`review.py`) — those carry voice as content and diverge with `EDITORIAL.md` —
or operator/dev strings, which keep the engine's origin name.

## Direction

Engine flows upstream → downstream. Site config (`config/sources.yaml`,
`EDITORIAL.md`, `data/`) never flows back upstream — each copy's content stays
in that copy.

## Mechanics

```bash
git remote add upstream <url-or-path-to-upstream>   # once
git fetch upstream
git merge upstream/main                              # mostly fast-forward on src/
```

Conflicts, if any, land only in site config — resolve by keeping the local
copy. A sync that wants to touch a site file in the table above is the signal
to stop and resolve by hand.
