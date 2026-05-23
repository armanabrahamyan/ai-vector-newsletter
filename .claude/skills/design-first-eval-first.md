---
name: design-first-eval-first
description: Pre-PR checklist enforcing PLAN.md §0 — design before code, evals before features, determinism in code and judgment in the LLM, subscribe don't scrape. Invoke before opening any PR that touches src/, config/, templates/, evals/, or docs/DESIGN.md.
---

# Design-first, eval-first — the working philosophy

This is the project's working philosophy as a working checklist. Run it before
you ask anyone for review. If a box is unchecked, fix it before you push — or
write down explicitly why it's deferred (and link the follow-up).

## 1. Did I update the contract before I changed the code?

- [ ] If my change touches an `Item`, `Cluster`, `RankedStory`, `IssueSection`,
      `Issue`, or any `data/YYYY-MM-DD/*.{jsonl,json}` artifact: the pydantic
      model was updated **first**, in the same PR or an earlier one.
- [ ] `docs/DESIGN.md` reflects the new shape (owner: Architect; if you're not
      the Architect, you've pinged them or proposed the diff yourself).
- [ ] Backward compatibility for the archive: either I can still read yesterday's
      JSONL, or I've added a `schema_version` bump + a migration note.

## 2. Are there fixtures for the new behaviour?

- [ ] Dedup or ranking change → fixture added in `evals/fixtures/` covering the
      case I'm changing (positive **and** at least one near-miss).
- [ ] Summarisation/voice change → at least one labelled example in
      `evals/voice/` showing the before/after expectation.
- [ ] Module-level change (any of `fetch / cluster / rank / summarise / render`)
      → there is a fixture that flows end-to-end through that module.

## 3. Did I run the evals, and are they green?

- [ ] `python -m evals.run_evals` (or the module-scoped equivalent) was run
      locally against this branch.
- [ ] No regression on:
  - Dedup precision/recall
  - Ranking Spearman vs. labels
  - Voice adherence score
  - Per-module integrity checks
  - Drift vs. last 14 days of ratified issues
- [ ] If a number moved, I've explained **why** in the PR description. "Moved up"
      is not an answer; *what changed in the rubric or the prompt* is.

## 4. Determinism vs. judgment — did I respect the seam?

- [ ] Fetching, parsing, scheduling, rendering, archive I/O → plain code. No LLM
      calls in these paths.
- [ ] Dedup *similarity*, ranking, summarisation, voice → LLM (or embeddings).
      Not regex. Not hardcoded heuristics dressed as policy.
- [ ] If I introduced an LLM call where code would do, I have a one-line
      justification. If I introduced code where the LLM would do, same.

## 5. Subscribe, don't scrape

- [ ] If I added a source: it's RSS / Atom / API. If it's HTML scraping, the PR
      body has a paragraph on *why no feed exists* and an *isolation plan*
      (separate module, separate failure mode, ToS-checked).
- [ ] No source reproduces full article text. We link out + summarise.

## 6. Archive hygiene (the JSON-per-day corpus)

- [ ] My change writes to `data/YYYY-MM-DD/` in the documented schema, or it
      does not write there at all. No silent partial writes.
- [ ] If I read from past days, I tolerate missing files (a day where a stage
      didn't run shouldn't crash today).
- [ ] `source_health.json` is updated if I touched fetch.

## 7. The eval gate is not advisory

- [ ] I have not bypassed the Eval Engineer's veto by merging "just this once."
- [ ] If my change is provably eval-positive but the harness is the wrong shape,
      I've opened a parallel PR against `evals/` and asked the Eval Engineer to
      review **before** mine merges.

## 8. The 30-second sanity questions

Before you click "create PR," answer these in one breath:

1. What did I change, in one sentence?
2. Who reads this artifact downstream, and does it still parse for them?
3. If today's issue regressed because of this, would I be able to tell from the
   archive alone?

If you cannot answer all three, you are not ready to push. Sit with it.
