# evals/fixtures/ — Fixture File Format

*Owner: Eval Engineer. Phase 2+ fixture collection from real archive.*

---

## Overview

Fixtures are the labelled datasets the eval harness runs against. They
share **the same file schema as `data/YYYY-MM-DD/`** — one format, two
locations. This means `run_evals.py` can be pointed at either real archive
days or fixture snapshots without changing anything.

---

## Directory naming

Each fixture dataset lives under `evals/fixtures/<dataset_name>/`.

**Naming convention:** `YYYY-MM-DD-<slug>`

- `YYYY-MM-DD` is the date the fixture represents (or the collection date
  for synthetic data).
- `<slug>` is a short, lowercase, hyphenated descriptor of what the dataset
  is testing. Keep it under ~30 characters.

**Examples:**
```
evals/fixtures/2026-06-03-launch-day-collision/   # multiple sources, same launch
evals/fixtures/2026-06-05-cross-time-chain/       # continuation story across days
evals/fixtures/2026-06-07-slow-day-sparse/        # a quiet day to test floor behaviour
evals/fixtures/_synthetic/                        # plumbing-test only (NOT real evals)
```

The `_synthetic/` prefix is reserved for hand-crafted plumbing tests that
are NOT real evaluation data. See `_synthetic/items.jsonl` for the plumbing
fixture that ships with the harness on day one.

---

## Files per dataset

Each dataset directory contains up to four files, matching the `data/YYYY-MM-DD/`
archive schema exactly:

| File | Schema | Required? | Notes |
|---|---|---|---|
| `items.jsonl` | `Item` (one per line) | Yes | Raw fetch output from that day |
| `clusters.jsonl` | `Cluster` (one per line) | Yes | Clustering output; needed for dedup eval |
| `ranked.jsonl` | `RankedStory` (one per line) | Yes | Rank output; needed for Spearman eval |
| `issue.json` | `Issue` (single JSON object) | Recommended | Needed for integrity + voice evals |

All pydantic shapes are defined in `src/models.py` (Architect-owned).
The fixture files must validate against those shapes — `run_evals.py`'s
integrity eval checks this on load.

---

## Labels

Fixture labels live in `evals/labels.yaml`, not inside the fixture
directories. Each fixture item or cluster is referenced by its `id` or
`cluster_id`; the labels file maps those IDs to ground-truth annotations.

See `evals/labels.yaml` for the label schema. The connection between
fixture files and labels is keyed on:
- `Item.id` → per-item labels (rare; mostly cluster-level)
- `Cluster.cluster_id` → per-cluster dedup ground truth + relevance labels

---

## Minimum fixture set (Phase 2 target)

PLAN §3 specifies ~30–50 items across a few days. The expanded scope adds:

| Requirement | Target | Why |
|---|---|---|
| Total items | 30–50 | Enough to produce non-trivial clusters |
| Distinct dates | 3–5 | Cross-time dedup needs at least 2 days |
| Near-duplicate pairs | ≥5 pairs | Each pair = same story from 2+ sources; tests dedup precision |
| Cross-time chain | ≥1 pair | Same story across 2 different dates; tests `cross_time_ref` eval |
| Slow day | ≥1 date | A sparse day (~5 items); tests floor behaviour |
| Finance-lens items | ≥5 | Items that clearly pass/fail the finance lens; for FS-angle drift detection |
| Tier-3 items | ≥3 | Items that should be cut; tests that Significance floor works |

Collect these from the **real archive** once Phase 2 has run for at least
a week. Do not hand-curate from web sources — see `evals/README.md`.

---

## How to add a fixture (the recipe)

1. **Collect the raw archive day.** Copy `data/YYYY-MM-DD/` into
   `evals/fixtures/YYYY-MM-DD-<slug>/` verbatim.

2. **Add labels.** Open `evals/labels.yaml` and add entries for the
   new fixture's clusters (dedup ground truth + human relevance scores).
   Minimum: label which clusters should have been merged together
   (`ground_truth_group_id`) and assign a `human_relevance` score (1–5).

3. **Run the harness.**
   ```bash
   python -m evals.run_evals --dataset YYYY-MM-DD-<slug> --against fixtures --report pretty
   ```
   Confirm it loads without errors and the integrity eval passes.

4. **Commit both** the fixture directory and the updated `labels.yaml`
   in the same PR. The Eval Engineer reviews; no other approval required
   for fixture-only PRs.

5. **If adding a near-duplicate pair:** add a comment in `items.jsonl`
   (a `# note:` field in the JSON is not valid; use the `extras` dict
   with key `"eval_note"`) so the harness and future readers can identify
   the intended pair. Document the pair in `labels.yaml` under the relevant
   cluster.

---

## Source-of-truth reminder

The pydantic models in `src/models.py` are the authoritative schema.
If a fixture file fails to load, the fixture is wrong, not the harness.
Fix the fixture or re-collect from a fresh archive run.
