# AI Vector

*All of it, sorted for you.*

AI Vector is a daily, agent-assisted AI newsletter for data scientists and
engineers in financial services (primary) and the senior leaders they
report to (secondary), with a moderate financial-services lens applied on
top of broader AI coverage. The publication is heavier on Agentic AI and
Generative AI — what shifts how readers work today, what to anticipate
tomorrow, what's practical to use right now. Traditional ML appears only
when load-bearing. Every issue points: each story carries a direction
("where this is heading") and, where it earns one, an explicit FS angle.

**Author:** Arman

**Status:** v0 — first cut

## Links

- [`PLAN.md`](./PLAN.md) — the full build plan and working philosophy
- [`docs/TEAM.md`](./docs/TEAM.md) — team working agreements, roster, decision rights
- [`docs/DESIGN.md`](./docs/DESIGN.md) — technical design and data contracts

## Local setup

```
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # then fill in LLM_PROVIDER, LLM_ENDPOINT, LLM_API_KEY, LLM_MODEL
python -m src.run --check  # verify embedding model is cached + LLM endpoint reachable
python -m src.run          # runs the engine — produces docs/preview/ and data/staging/YYYY-MM-DD/
```

The pre-flight checks (embedding-model presence + LLM endpoint reachable)
also auto-run before every default pipeline invocation. Use `--skip-preflight`
to bypass when iterating.

### Daily flow

```
python -m src.run                              # produces a staging draft + docs/preview/<date>.html
open docs/preview/<date>.html                  # review
python -m src.run --release                    # promote to canonical + ship to docs/index.html
python -m src.run --unrelease --date <date>    # reverse a release if needed
```

Run on schedule via local cron (CI/CD comes later).
