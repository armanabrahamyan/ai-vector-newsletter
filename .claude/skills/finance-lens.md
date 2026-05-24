---
name: finance-lens
description: Shared rubric for "does this matter to financial services?" — primary lens for DS + engineers in FS (agents in trading/fraud/KYC, model risk, productionising under regs), with senior-leader angle as secondary. Invoke when ranking, summarising, sourcing, or editing for the finance angle.
---

# Finance lens — what makes a story land for AI Vector's audience

Primary readers: **data scientists and engineers in financial services.**
Secondary: senior leaders in the same firms. The lens is **moderate, not
maximum** — we are an AI newsletter with a finance eye, not a finance newsletter
with AI as a topic. If a story is huge for AI generally, it ships; the lens
*adds value*, it doesn't *filter out the field*.

## The two-tier test

### Tier 1 — does it land for hands-on readers in FS? (the heavier weight)

Ask, in order, until you get a "yes" or you've exhausted the list:

1. **Trading / markets** — Does this change how a quant or ML engineer might
   approach signal generation, execution, microstructure modelling, RL for
   trading, or alt-data ingestion?
2. **Fraud / AML / KYC** — Does this affect detection models, transaction
   monitoring, document understanding for onboarding, synthetic identity, or
   adversarial robustness in those pipelines?
3. **Model risk & governance (SR 11-7, PRA SS1/23, etc.)** — Does this change
   how a model owner documents, validates, monitors, or explains a model?
   Anything on LLM evals, lineage, reproducibility, drift, or "explainable
   enough for regulators" lands here.
4. **Productionising under regulatory constraints** — On-prem / air-gapped
   inference, data residency, audit trails, vendor lock-in, redaction, PII
   handling, model cards, change management. The boring stuff that decides
   whether an idea actually ships inside a bank.
5. **Agents in finance** — Tool-use agents for ops, research copilots for
   analysts, agentic workflows for compliance review, code agents in
   trading-system codebases. This is a recurring beat — flag it when it appears.
6. **Eval / benchmark relevance** — A new benchmark or eval methodology that an
   FS team could actually adopt (financial reasoning, tabular, time series,
   long-context document analysis).

If **two or more** of the above are clear yeses, this is a strong finance-lens
story for the **hands-on** audience. Tag `audience: hands_on, finance`.

### Tier 2 — does it land for senior leaders in FS? (the lighter weight)

Only after Tier 1, ask:

7. **Strategic shift** — Does this change what a Head of AI / CDO / CTO would
   reasonably prioritise next quarter? (New capability tier, new vendor risk,
   new talent need.)
8. **Regulatory or policy movement** — EU AI Act, FCA / PRA / OCC / APRA / MAS
   guidance, US executive orders, NIST AI RMF updates. Light touch — we are not
   a policy tracker, but we flag what shifts.
9. **Vendor / platform consolidation** — Hyperscaler announcements,
   model-provider terms, on-prem options changing. Affects build-vs-buy.

Tag `audience: big_picture, finance` when Tier 2 fires without Tier 1.

## What the lens is *not*

- Not a board-level strategy memo. We don't moralise about "AI transformation."
- Not earnings commentary. We don't care that Bank X "is exploring AI."
- Not regulatory cheerleading. Cite the rule, name the impact, move on.
- Not anti-vendor or pro-vendor. Note the lock-in honestly; don't editorialise.

## How to write the angle (for LLM Engineer and Editor)

When a story passes the lens, the finance angle in the summary should:

- **Be specific.** "Affects model risk teams" is filler. "Means a model risk
  team can now justify reproducible LLM evals under SR 11-7 expectations" is
  signal.
- **Be one or two sentences inside the story summary**, not a separate section,
  unless the story *is* a finance-AI story (then it leads).
- **Name the role**, not the abstraction. "A fraud DS shipping a new model can
  use this for…" beats "this has implications for fraud detection."
- **Flag the constraint**, not just the opportunity. If a capability is great
  but probably not deployable on-prem yet, say so.

## Sourcing signal (for Source Engineer)

When evaluating a candidate feed for `config/sources.yaml`:

- Does it publish at least weekly?
- Does it cover Tier-1 territory (trading ML, fraud ML, model risk, agentic
  ops in FS) and not just generic fintech press releases?
- Does it cite primary work (papers, repos, products) rather than recycling?
- Trust weight starts at 2; earn the way to 4–5 over months of observed quality
  in `source_health.json`.

## Calibration — when to *de-prioritise* the lens

If the day's news is dominated by a field-shaping event (a new foundation model
tier, a major safety finding, a market-moving outage), the lens steps **back**.
The Pulse is honest about the day. We don't shoehorn a finance angle onto a
story that doesn't have one.

The lens is a **lens**, not a quota. One day in five may be light on finance,
and that's correct. Drift detection (Eval Engineer) watches for the opposite
failure mode: weeks where the lens silently disappears.
