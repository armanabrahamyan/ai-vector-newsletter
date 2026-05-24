# Weekly Eval — Behavioural Integrity Note

<!-- Copy this file to YYYY-WNN.md (ISO week number), fill in each field, commit. -->
<!-- Example filename: 2026-W22.md -->

**Week:** <!-- e.g. 2026-W22 (2026-05-25 – 2026-05-31) -->
**Author:** Eval Engineer
**Date filed:** <!-- ISO date when you wrote this -->

---

## 1. PRs reviewed by right owners?

<!-- List any PRs that touched gated paths (cluster.py, rank.py, summarise.py,
     rubric.yaml, LLM Engineer prompts) this week. For each: -->

| PR | Gated path(s) touched | Architect reviewed? | Eval gate passed? | Notes |
|----|-----------------------|---------------------|-------------------|-------|
| #  |                       | yes / no            | yes / no / N/A    |       |

**Finding:** <!-- one sentence — process healthy / gap noted -->

---

## 2. Postmortems filed?

<!-- Did any daily run fail or produce a materially wrong issue this week?
     If yes: is there a file in docs/postmortems/ for it? -->

| Incident date | What happened | Postmortem filed? | Link |
|---------------|---------------|-------------------|------|
|               |               | yes / no          |      |

**Finding:** <!-- "No incidents this week" is a valid answer -->

---

## 3. Voice labels accumulating?

<!-- Check evals/voice/ for new YYYY-MM-DD.labels.yaml files since last week. -->

- Labels files added this week: <!-- count + dates -->
- Cumulative labelled days: <!-- count -->
- On track for Phase A target (5 days labelled)? <!-- yes / no / already done -->

**Finding:**

---

## 4. Failure-modes log changes

<!-- Any new entries added to evals/failure_modes.md this week?
     Any existing entries updated (last_occurrence bumped)? -->

| FM entry | Change | Trigger |
|----------|--------|---------|
|          |        |         |

**Finding:** <!-- "No changes" is fine -->

---

## 5. Drift flags this week

<!-- Summarise any drift flags raised by aiv eval during the week.
     Each flag is a |z| > 2 on one of the tracked dimensions. -->

| Date | Dimension | z-score | Investigated? | Verdict |
|------|-----------|---------|---------------|---------|
|      |           |         | yes / no      | real drift / noise / TBD |

**Finding:** <!-- "No flags this week" or summary of what was found -->

---

## 6. Commentary

<!-- One paragraph. Factual, specific. Not a performance review.
     Cover: is the system doing what it was designed to do? Any trend
     worth naming — positive or negative? Anything the team should
     discuss at the next weekly drift review? -->

<!-- WRITE HERE -->

---

*Filed by Eval Engineer. Next note due: <!-- date of next Monday -->*
