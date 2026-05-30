# A1 Misses Audit — 2026-05-30
**Scope:** 2026-05-23 through 2026-05-29 (7 days)
**Method:** clusters with score >= 40 in ranked.jsonl absent from all sections of issue.json
**Total above-threshold misses: 184**

---

## Miss count by day

| Date | Location | Published | Score>=40 | Misses |
|------|----------|-----------|-----------|--------|
| 2026-05-23 | released | 11 | 11 | 0 |
| 2026-05-24 | released | 12 | 10 | 0 |
| 2026-05-25 | released | 12 | 32 | 20 |
| 2026-05-26 | released | 12 | 60 | 48 |
| 2026-05-27 | staging  | 8  | 37 | 37 |
| 2026-05-28 | released | 10 | 50 | 40 |
| 2026-05-29 | staging  | 9  | 48 | 39 |

Days 23-24 have zero misses because the full above-threshold pool fit in the issue. The miss problem begins May 25 and worsens through the week as arXiv volume grows.

---

## Top categories (strong patterns, 5+ misses across 3+ days)

### 1. papers-pool-overflow — 142 misses (77%), present in 5 of 7 days

**Mechanism:** The cluster is sourced from arXiv cs.CL or Hugging Face Daily Papers and scores 40-60, placing it in on_the_radar tier. Two sub-phases exist within this category:
- **Pre-cap (May 25-26):** No papers cap in editorial.yaml yet. Sections filled by score order; arXiv dominated (360 of 516 raw items on May 26, 70%). Papers consumed 6 and 10 of 12 issue slots respectively, leaving 14 and 40 qualifying papers unplaced.
- **Post-cap (May 27-29):** editorial.yaml papers cap = 4/issue is active. Papers that ranked below the top-4 are structurally excluded regardless of score. 27-32 paper misses per day result.

In both phases, a score-ceiling effect compounds the cap: all missed papers score 40-60, well below the promote_to_section floor of 70. They cannot graduate to Big Picture or Hands-On regardless of cap.

**Example cluster IDs:** c_af994d18422ce1fa (score=60, May 26 — epistemic resilience under clinical pressure), c_0300024ced2c5348 (score=58, May 27 — AgentAtlas trajectory taxonomy), c_57b85d4b9c8b2182 (score=58, May 28 — TRACES proactive safety auditing)

### 2. practitioner-only-no-big-picture — 18 misses (10%), present in 5 of 7 days

**Mechanism:** Cluster carries audience_tags=['hands_on'] only (no big_picture tag) and scores big_picture_relevance < 30. These stories compete exclusively for the Hands-On section (cap 4-5 slots). When higher-scoring dual-tagged content fills those slots, practitioner-only stories are displaced. Score range: 40-55. Sources: predominantly r/LocalLLaMA (Reddit) and r/MachineLearning.

These are often genuinely useful practitioner content — benchmarks, local-inference tricks, tool comparisons — that lack a senior-leader angle and therefore cannot overflow into Big Picture if Hands-On is full.

**Example cluster IDs:** c_ea77c479eb2f57a3 (score=55, May 26 — SkillOpt skill-file optimisation), c_48e09c3ce8ebf591 (score=55, May 27 — Qwen3.6-35B sub-agent failure modes), c_f94f6c2453fb6c0d (score=53, May 28 — KV cache quant benchmarks)

### 3. section-capacity-displacement — 10 misses (5%), present in 4 of 7 days

**Mechanism:** Non-paper, non-specialist clusters scoring 40-49 with mixed audience tags. No structural cap blocks them; they simply lose in score-ordered selection when 10-12 slots fill above them. Sources: Ars Technica AI, Reddit, Simon Willison's Blog, Normal Technology. Several of these (Illinois AI law, Heretic/FT, DeepSWE cheating benchmark) have real editorial value but land too low in the score distribution to be selected.

**Example cluster IDs:** c_cea664169bba39bf (score=48, May 29 — Illinois AI regulation), c_7588ff3d5cf27568 (score=45, May 26 — FT article on Heretic guardrail tool), c_fac9cba369905a0a (score=45, May 27 — DeepSWE finds Claude Opus gaming evals)

---

## Categories retired (appeared < 3 times or not structurally distinct enough)

**sub-threshold-near-promote (n=5):** Clusters scoring 50-57 from non-paper, non-specialist sources with clear editorial signal (AlphaSignal, Simon Willison, Reddit) that fell just short of the promote floor. Only 5 occurrences across 2 days (May 27-28); too few to treat as a structural pattern. Retained in the CSV for A2 inspection — these are the most interesting individual misses but not a systemic category.

**finance-specialist-under-threshold (n=9):** Borderline. 9 misses across 5 days, but two cluster_ids account for 7 of the 9 (same stories re-appearing across days — see dataset oddities). The underlying mechanism is low significance + big_picture scores for niche FS newsletters, not a distinct path from section-capacity-displacement. Kept as a separate category in the CSV because the source type is a useful signal for A2.

---

## Dataset oddities

**1. Cross-day dedup failures.** Two cluster_ids appear in multiple days' ranked.jsonl with no cross_time_ref set: c_491e0b408f3bab95 (EU AI Act Newsletter #102) surfaces in May 25, 28, and 29 with the same ID; c_6830490c1b6fb884 (LLMQuant "When the Alerts Outrun the Analysts") surfaces every day May 25-29. A third case, c_764523e706e29ccd (sqlite AGENTS.md), appears May 28 and 29. These should have been suppressed by cross-time dedup on days 2+ of appearance. None carry a cross_time_ref field.

**2. Same-day within-day dedup failure.** On May 26, the title "Anticipate and Learn: Unleashing Idle-Time Compute in Proactive Agents" appears under two different cluster_ids (c_4ade81a3ea6a91fb and c_3756abcb6e8becc5) in the same ranked.jsonl.

**3. per_source_per_section cap violated in May 25 and 26.** editorial.yaml sets per_source_per_section=2, but arXiv cs.CL appears 3 times in the hands_on section on both May 25 and May 26, and 4 times in big_picture on May 26. The cap was not enforced before the May 27 editorial.yaml update.

**4. on_the_radar section contracted to 0 slots on May 26-29 without a corresponding editorial.yaml cap.** The section went from 5 slots (May 24) to 2 (May 25-26) to 0 (May 27-29). There is no on_the_radar cap in editorial.yaml. The contraction appears to be driven by Shape A's tier-as-authority change in summarise.py, but the mechanism is not surfaced in config. 10+ qualifying stories per day score in the on_the_radar tier with audience tags that should land them in a section — but no section accepts them.

**5. arXiv cs.CL dominance is structural, not day-specific.** Raw item counts: 62% arXiv on May 25, 70% on May 26, 54% on May 27, 58% on May 28, 63% on May 29. This is not a spike; it is the baseline supply profile. The papers cap addresses it at the output stage but 27-40 qualifying papers are generated every day regardless.

**6. May 27 staging issue.json was generated under a different ranked.jsonl than the current one.** All 8 published story IDs in the May 27 issue.json are absent from the May 27 staged ranked.jsonl (which was re-run under Shape A). This is expected given the task description ("re-ranked under Shape A today") but means the 37 May 27 misses are all against the NEW ranked.jsonl, not the OLD issue. The analysis is still valid as a miss audit.

---

## Low-confidence coding

**finance-specialist-under-threshold** cluster c_491e0b408f3bab95 (EU AI Act Newsletter #102) appears on May 25, 28, and 29 with the same cluster_id. I coded it as a miss on each day independently (because it IS above threshold in each day's ranked.jsonl and absent from each day's issue.json). This inflates that category count by 2. The real miss is one story that keeps re-surfacing because dedup isn't catching it.

**section-capacity-displacement vs sub-threshold-near-promote** is a judgment call at score=50. I used score >= 50 as the sub-threshold-near-promote boundary; the line is somewhat arbitrary. A2 may want to revisit.

---

## Single-paragraph read

If I had to name the single most impactful intervention based purely on these misses, it would be resolving the score-ceiling that traps 77% of above-threshold misses below the promote_to_section floor of 70. Scores of 40-60 map to on_the_radar tier, but the on_the_radar section now holds zero slots in practice — so these stories have nowhere to land. The papers cap addresses source concentration, but it does not address the underlying problem: the rubric is producing a large mass of "good enough to surface, not good enough to promote" content that the current section architecture has no capacity to absorb. Either the promote floor needs to come down (accepting more stories into Big Picture and Hands-On), or the on_the_radar section needs real slots again with explicit cap logic, or the issue needs a fourth structural outlet for the 40-69 tier. Without one of these changes, every day with a large ranked pool will produce 30-50 misses that are above the editorial threshold by definition but invisible to the reader.
