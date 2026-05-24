# Regression fixture: continuation routed as Pulse

**Bugs:** tasks #81 (rank.py) + #82 (summarise.py)
**Failure mode:** FM-13 (continuation surfaces as Pulse, displacing fresh
signal)
**Ratification date the bug surfaced on:** 2026-05-25 (staging; not released)
**Anchor cluster:** `c_2e53967d020fb800`
  ("How I do use the recent llama.cpp native tools to do web rag a.k.a.
  web_fetch ... directly from inside the llama-server's webui",
  `data/staging/2026-05-25/`)
**Anchor chain root:** `c_cf0b99c06c42a9ba` (yesterday's llama.cpp server
native-tools announcement)

## Surface

The 2026-05-25 staging run produced a 2-story issue with the Pulse pointing
at `c_2e53967d020fb800` -- a how-to follow-up to yesterday's llama.cpp
native-tools announcement. The cluster correctly carried
`cross_time_ref = "c_cf0b99c06c42a9ba"` (cluster.py did its job). But:

1. **`rank.py` ignored the cross_time_ref.** It scored the continuation
   44 (`tier: on_the_radar`) with `breakdown.significance = 65`. The
   rubric anchor 50 = "single signal-filter dimension hit"; 65 = "two";
   75 = "three". A how-to follow-up to an announcement we already
   covered should not score above anchor 50 on significance -- the
   significance is mostly in the original.

2. **`summarise.py` picked it as Pulse.** With only 2 surviving stories,
   `_pick_pulse` selected the highest-scored surviving cluster -- which
   was the continuation. The lead of the issue thus became "here is a
   how-to for something we already told you about yesterday", which is
   not what the Pulse exists to do.

## What the cross-check should produce

Two complementary fixes (sequenced #82 first, then #81):

1. **`summarise.py` Pulse selection bias** (#82, in `_pick_pulse`,
   v0.2 -> v0.3): prefer FRESH (`cross_time_ref is None`) stories over
   continuations regardless of score. Within the chosen pool, the
   existing >= 2 signal-dimensions quality bar still applies. Degraded
   mode (every surviving story is a continuation): pick the best
   continuation and log a WARNING; the Issue model requires
   `pulse` to hold exactly 1 block so we cannot emit no-pulse.

2. **`rank.py` continuation penalty** (#81, deterministic post-LLM):
   `_apply_continuation_penalty` caps `breakdown["significance"]` at
   50 when `cluster.cross_time_ref is not None`. Score is recomputed
   from the modified breakdown via the same `_weighted_score` formula
   the pydantic validator uses, so the on-disk record reflects the
   final score. NOT a prompt change -- the #75 prompt-sharpening
   experiment caused a cliff in #77.

For this fixture cluster specifically:
- `breakdown.significance` MUST be capped at 50 in `ranked.jsonl`.
- `score` MUST be recomputed and consistent with the modified breakdown
  (the pydantic `_score_matches_weighted_breakdown` validator enforces
  this).
- The rendered Pulse MUST point at a fresh story (cross_time_ref null),
  not at `c_2e53967d020fb800`.
- The log line `Pulse non-continuation bias fired` MUST appear when the
  rule changes the outcome.

## Why both fixes (not just one)

Fix #82 alone keeps the continuation in Pulse-runner-up position, scored
44; the next time it might be lonely enough at the top that even another
continuation beats it on score and the bias rule doesn't help. Fix #81
alone caps significance but a continuation could still be the highest-
scored survivor (depending on other dimensions). The two together close
both the input shape (score now reflects "this is a follow-up") and the
section routing ("Pulse means today's fresh anchor, not yesterday's
re-airing").

## Re-test recipe

```bash
aiv run --date 2026-05-25 --stages rank,summarise --skip-preflight
jq 'select(.cluster_id == "c_2e53967d020fb800") | {score, sig: .breakdown.significance}' \
   data/staging/2026-05-25/ranked.jsonl
# expect: significance == 50 (capped), score recomputed accordingly
jq '.pulse.stories[0] | {story_id, cross_time_ref}' \
   data/staging/2026-05-25/issue.json
# expect: cross_time_ref == null (fresh story chosen as Pulse)
```

## Validation log line patterns

The deterministic fixes are visible via these log lines (FM-13 detection
signals):

- `continuation penalty applied to <id>: significance N->50, score N->M (cross_time_ref=...; #81)`
- `Pulse non-continuation bias fired -- demoted continuation <id> (score=N) in favour of fresh story <id> (score=M). #82.`
- `NO FRESH SIGNAL FOR PULSE -- every surviving story is a continuation ...` (degraded mode)
