# evals/ — AI Vector Eval Harness

*Owner: Eval Engineer (independent). Read-only everywhere else.*

---

## What this directory is for

`evals/` is the Eval Engineer's yard. It measures whether the AI Vector
pipeline consistently produces what it was designed to produce — not just
whether the code runs, but whether the output is *correct by the system's own
standards*.

### Scope (broader than PLAN §3)

PLAN §3 specified dedup precision/recall and ranking Spearman. The scope has
been expanded to **agentic system evals**: does the end-to-end system
consistently produce what it was designed for?

| Dimension | What it measures | Status |
|---|---|---|
| **Dedup quality** | Precision/recall of clusters vs. labelled ground truth (within-day + cross-time) | Stub — ready when fixtures land |
| **Ranking quality** | Spearman correlation of LLM scores vs. human relevance labels | Stub — ready when fixtures land |
| **Voice adherence** | Does the LLM stay on voice over time vs. ratified archive baseline? | Stub — rubric co-dev with Editor in Phase 2 |
| **Module-level integrity** | Every artifact in `data/YYYY-MM-DD/` schema-validates against DESIGN.md contracts | Ready — runs on any archive day |
| **Drift detection** | Scores, tier mix, voice signatures, summary length drifting from rolling 14-day baseline? | Stub — needs a corpus to detect drift against |
| **Behavioural integrity** | Is the team following its own rules? PRs reviewed, postmortems filed, labels accumulating? | Manual — Eval Engineer writes weekly note |

"Stub" means: function signature and structure are real; implementation is
TODO with graceful not-yet-implemented output. The harness is runnable and
CI-wireable from day one. Implementations fill in as fixtures land in Phase 2.

### Evals read canonical only

Per DESIGN.md "Archive: staging vs canonical," every eval dimension in
this harness reads the **canonical** archive only -- `data/<date>/` for
each archive date and `data/published_urls.txt` at the `data/` root.
Staging artifacts under `data/staging/<date>/` are **invisible** to
evals: drafts Arman discarded must not influence drift baselines, the
labelled corpus, or module-integrity checks. Drift is measured against
what readers actually saw; the labelled corpus is exactly the set of
released `issue.json` files; module-integrity regressions are reported
only on canonical days (a staging day that fails integrity is an
in-progress draft, not a regression). The harness loads from
`data/<date>/` and `data/published_urls.txt`; never from
`data/staging/`.

---

## The hard-veto contract

Any regression on any tracked eval **blocks merge** to:

- `src/cluster.py`
- `src/rank.py`
- `src/summarise.py`
- `config/rubric.yaml`
- Any prompt file owned by the LLM Engineer

**Mechanism:** CI runs `python -m evals.run_evals` on every PR touching those
paths. Non-zero exit = regression = PR blocked. The veto is not advisory and
does not soften over time.

If a regression is expected (deliberate tradeoff, rubric recalibration),
the path is: Eval Engineer acknowledges, documents the accepted tradeoff
in the PR description, and Arman explicitly approves. The PR does not merge
without that sequence.

**Soft gate** on `src/fetch.py`, schema changes, and render layer: Eval
Engineer can flag and require a postmortem; does not block merges unless
they indirectly regress a hard-vetoed metric.

---

## Directory structure

```
evals/
  README.md                  # this file
  run_evals.py               # harness entry point (runnable, stubs inside)
  labels.yaml                # ground-truth labels (cluster + issue + source)
  failure_modes.md           # living catalogue of known failure modes
  fixtures/
    SCHEMA.md                # fixture file format documentation
    _synthetic/              # plumbing-test data (NOT real evals)
      items.jsonl
      clusters.jsonl
      ranked.jsonl
      issue.json
    <YYYY-MM-DD-topic>/      # real fixture datasets (Phase 2+)
      items.jsonl
      clusters.jsonl
      ranked.jsonl
      issue.json
  voice/                     # labelled voice examples (Editor co-curates)
    rubric.yaml              # voice eval rubric (co-developed with Editor)
    YYYY-MM-DD.labels.yaml   # per-day voice labels from Editor
  drift/                     # snapshots and drift detection artifacts
    YYYY-MM-DD.snapshot.json # per-day rolling baseline snapshot
  reports/                   # dated reports, one per CI run + one per ratified issue
    YYYY-MM-DD.json          # per-issue eval report
```

---

## How fixtures get populated (Phase 2)

The pipeline has not run yet. Fixtures come from the real archive once Phase 2
starts producing data. See `fixtures/SCHEMA.md` for the format and the
minimum fixture set the team should aim for.

**Phase 2 fixture target:** ~30–50 items across 3–5 dates, including deliberate
near-duplicates (same story, different sources/URLs) to validate dedup logic.
At least one cross-time continuation pair (same story chain across two dates).

**Do not hand-curate fixtures from web sources.** Fixtures come from the real
pipeline archive. `_synthetic/` is the sole exception: plumbing-test data only.

---

## How to run evals locally

```bash
# Against the synthetic plumbing fixtures (confirms the harness doesn't crash)
python -m evals.run_evals --dataset _synthetic --against fixtures --report pretty

# Against a real archive date (once Phase 2 has produced data)
python -m evals.run_evals --dataset 2026-06-01 --against real --report pretty

# JSON output for CI or scripting
python -m evals.run_evals --dataset _synthetic --against fixtures --report json

# Run all available fixture datasets
python -m evals.run_evals --against fixtures --report pretty
```

Exit code `0` = all evals passed (or not-yet-implemented stubs skipped).
Exit code `1` = at least one regression detected.

---

## How the CI hook is wired (contract for Release Engineer)

The Release Engineer wires a GitHub Actions check step that:

1. **Trigger:** `paths:` filter matching any of the hard-vetoed files:
   ```yaml
   paths:
     - 'src/cluster.py'
     - 'src/rank.py'
     - 'src/summarise.py'
     - 'config/rubric.yaml'
     - 'src/prompts/**'
   ```
   Also wire a manual `workflow_dispatch` so Eval can trigger on demand.

2. **Step:**
   ```yaml
   - name: Run eval harness
     run: python -m evals.run_evals --against fixtures --report json
   ```

3. **Gate:** The step has no `continue-on-error: true`. A non-zero exit
   from `run_evals.py` fails the check, which blocks merge on a protected
   branch.

4. **Artefact upload:** Upload `evals/reports/` as a CI artefact so Arman
   and the team can read the report without checking out the branch.

Until Phase 2 fixtures land, only the integrity and synthetic-plumbing evals
run meaningfully. The stub evals return `passed: true` with a
`not_yet_implemented` details field — they don't block. Once real fixtures
arrive, the team updates the stubs to real implementations and the gate
tightens automatically.

---

## Where failure modes are catalogued

`evals/failure_modes.md` — the living catalogue. Each entry covers:
- What the failure is
- Detection signal (what metric or pattern surfaces it)
- Last known occurrence
- Mitigation
- Severity (low / medium / high)

Postmortems file their findings into `failure_modes.md`. The Eval Engineer
updates it after each weekly drift review and after any incident.

---

## Behavioural integrity (weekly note)

Once a week, Eval Engineer appends a short paragraph to the weekly eval report
(`evals/reports/week-YYYY-WNN.md`) covering:

- Are PRs touching contracts going through Architect review?
- Are postmortems being filed in `docs/postmortems/` when runs break?
- Are voice labels accumulating in `evals/voice/`?
- Is Arman's ratification pattern consistent, or are cut rates rising?

This is the "team eval," not the pipeline eval. It is short, specific, and
factual — not a performance review.
