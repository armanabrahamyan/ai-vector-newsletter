# Regression fixture: signal=act / audience_tags=hands_on-only mismatch

**Bug:** task #75
**Failure mode:** FM-12 (signal/audience-tag mismatch evicts Big Picture stories)
**Ratification date the bug escaped on:** 2026-05-24
**Anchor cluster:** `c_78dcc648119217a1`
  ("Spec-driven development is the new way", `data/released/2026-05-24/`)

## Surface

In the ratified 2026-05-24 issue, the spec-driven-development cluster
landed in **On the Radar** despite carrying `signal: "act"` from the
per-story summarise call. The signal pill is defined in
`src/summarise.py` as:

> "act" -- vendor / contract / architecture decision worth making this
>          quarter. Typical for Big Picture stories with a nameable
>          prioritisation change.

So the body-grounded LLM said "Big Picture territory", but the rank-side
audience tags read `["hands_on", "general"]` (no `big_picture`).
`_pick_big_picture` in `src/summarise.py` routes strictly on
`audience_tags`, so the story was evicted to On the Radar.

Rank's own rationale apologised for the miscall in plain text:

> "thin on FS specificity and big-picture reprioritisation"

`big_picture_relevance` scored 30 (anchor 25 = "tangential"). For a
team-level workflow shift adopted across 5 repos with academic backing,
anchor 50 ("minor priority shift") is the floor; 60 is more honest.

## What the cross-check should produce

Two complementary fixes:

1. **rank.py prompt** (`RANK_PROMPT_VERSION` bumped v0.1 -> v0.2) now
   includes concrete `big_picture` examples covering architecture
   decisions, workflow shifts, vendor calls, and regulatory moves --
   so rank.py tags these stories correctly at source.

2. **summarise.py cross-check** (`_reconcile_signal_with_audience_tags`)
   runs after the per-story summarise loop and before section routing.
   When `signal == "act"` and `big_picture` is missing from
   `audience_tags`, it adds the tag and logs the augmentation. Body-
   grounded signal corrects rank's lighter-context miscalls.

For this fixture cluster specifically:
- `big_picture` MUST appear in the final `audience_tags`.
- `big_picture_relevance` SHOULD be >= 60 after the prompt fix.
- The rendered story MUST sit in the Big Picture section, not On the Radar.

## Why both fixes (not just one)

Fix 1 (prompt) is the right-place fix; rank.py should tag correctly at
source. Fix 2 (cross-check) is the safety net for the residual cases
where the lighter-context rank call still misses what the body makes
plain. The two should agree most of the time; when Fix 2 fires often,
that's the diagnostic that Fix 1 needs another revision.

## Re-test recipe

```bash
aiv run --stages rank,summarise,render --date 2026-05-24 --skip-preflight
jq 'select(.cluster_id == "c_78dcc648119217a1")' data/staging/2026-05-24/ranked.jsonl
# expect: "big_picture" in audience_tags AND big_picture_relevance >= 60
jq '.sections[] | select(.name=="big_picture") | .stories[].story_id' \
   data/staging/2026-05-24/issue.json
# expect: c_78dcc648119217a1 in the list
aiv eval --date 2026-05-24 --no-judge
# expect: tier disagreements drop from 4 toward 3 or fewer
```
