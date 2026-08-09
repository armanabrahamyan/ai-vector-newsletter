# Ratification note — Eval 9 result cache

- Owner: Eval Engineer (proposed). Arman ratifies (yes/no).
- Created: 2026-08-09
- Status: RATIFIED — 2026-08-09. Code and tests are merged-ready
  but the cache stays empty until this note is ratified; nothing in CI
  changes meanwhile (the harness registry still runs Eval 9 in seam mode).

## Why now

Today an Eval 9 run died at 118 of 126 reviewer calls on exhausted API
credits, and the rerun re-paid for all 118 calls that had already
succeeded. The fixtures are frozen files; an identical call under an
identical configuration always buys the same answer.

## What changes about eval semantics

1. **Nothing about gate meaning.** Thresholds, the hold/publish mapping,
   recall and precision definitions, and pass/fail logic are untouched.
   A cached verdict is the same parsed verdict the same configuration
   already produced once.

2. **Reruns of unchanged configurations become cheap.** Reviewer results
   (verdict + findings) are cached on disk keyed by fixture content hash,
   review prompt version, threshold-table version, model id, and
   temperature. Change any one and the cache misses naturally. A
   calibration run (rerun_n=1) over an unchanged configuration costs zero
   LLM calls; results are written per fixture as they complete, so a run
   killed mid-flight resumes from where it died. Failure states
   (`unavailable`, `unparseable`) are never cached — an outage can never
   be frozen in as a verdict.

3. **Stability is exempt by construction.** `gate_stability` measures
   fresh-call variance, so its samples must be live calls — a cached
   verdict served there would report fake perfect stability. The bypass
   is structural, not a flag: the only function that produces stability
   samples has no access to the cache, and a mutation test goes red if
   that changes. Consequence: a stability-bearing run (rerun_n > 1) pays
   full fresh cost every time, deliberately. Its crash protection is a
   separate per-run journal that can only replay the same run's own
   already-paid fresh samples (same config, same rerun_n, same UTC day);
   `stability_resume=False` forces a fully fresh audit.

4. **One reported number changes.** At rerun_n=1 the old code reported
   `gate_stability = 1.0` (a single sample is trivially "stable"). It now
   reports None with an explanatory note — a 1.0 there was exactly the
   fake stability the carve-out forbids.

## Files

- `evals/review_cache/__init__.py` — cache implementation (two stores:
  config-keyed primary cache + run-scoped stability journal).
- `evals/run_evals.py` — Eval 9 integration (`eval_reviewer_gate` gains
  `use_cache`, `stability_resume`, `cache_context`; new structurally
  cache-blind `_fresh_reviewer_verdicts`).
- `evals/test_review_cache.py` — 18 tests; the three mutation guards
  (stability-reads-cache, journal-write-removed, failure-verdicts-cached)
  were each applied, shown red, and reverted on 2026-08-09.
- Cache entries are gitignored via directory-local `.gitignore` files
  (root `.gitignore` is outside evals/ write scope).

## The yes/no

Ratify: cached primary verdicts for unchanged configurations + per-fixture
crash resume, with stability measurements structurally exempt from the
cache. If no: the code carries a `use_cache=False`/`stability_resume=False`
path that restores today's behaviour exactly.
