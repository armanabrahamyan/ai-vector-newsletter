# RATIFICATION_PENDING.md
# Phase A — Cluster Labels: Draft for Arman's Ratification
#
# Owner: Eval Engineer (drafted). Arman ratifies (accept/override per item).
# Created: 2026-05-24
# Status: DRAFT — awaiting ratification
#
# This file walks through every drafted cluster label in evals/labels.yaml.
# Read this, then open labels.yaml and fill in the human_relevance values.
# Cross out dedup-miss proposals you disagree with.
#
# SCOPE: 2 dates (2026-05-23 and 2026-05-24), ~20 clusters labelled.
# Plan target was 5 dates. Bootstrap with what we have; corpus grows
# as more dates ship.

---

## HIGH-IMPACT CALLS FIRST: Dedup Misses and Tier Disagreements

Read this section before the per-cluster walkthroughs. These are the
calls where the pipeline's output and my independent ground truth diverge.
Your override here shapes what the dedup and ranking evals will measure.

---

### DEDUP MISS PROPOSAL 1 — llama.cpp b9297 (2026-05-24)

Clusters involved:
- `c_7ec76012cc4eac10` — community Reddit post: "NVFP4 + MTP - voila on llama.cpp" (score 61, on_the_radar)
- `c_58c6a5766fc586a1` — official llama.cpp release entry: "b9297" (score 29, cut)

Both point at exactly the same event: llama.cpp release b9297, which
ships NVFP4 quantisation and multi-token prediction together. The Reddit
post links directly to the GitHub release tag. The pipeline kept them in
separate clusters; they should be one group.

My draft: `ground_truth_group_id: "g_20260524_llamacpp_b9297"` on both.

Implication for ranking: the pipeline correctly scored the community post
(score 61) higher than the bare release tag (score 29), so if they were
merged the group would rank as on_the_radar. The dedup miss is real but
harmless in this case — the better-scored item surfaces.

Arman's call: Do you agree these are the same story? (yes/no)

---

### DEDUP MISS PROPOSAL 2 — llama.cpp patch releases as one story (2026-05-24)

The 2026-05-24 ranked output contains 6 separate clusters for llama.cpp
build-numbered releases: b9286, b9289, b9291, b9292, b9295, b9297, b9301.
Some are grouped (c_1972bc2ac90798c6 holds 4 items: b9286/b9290/b9294/b9296),
others are singletons.

These are all one continuous-delivery story: llama.cpp ships multiple
patch builds per day. The question is whether the eval should treat them
as a single "llama.cpp keeps shipping" group or as separate singleton stories.

My read: for dedup purposes, the meaningful grouping is "what's
editorially interesting about this batch of releases?" On 2026-05-24,
b9297 (NVFP4 + MTP) is the signal story; the rest are routine maintenance.
I have NOT proposed merging all of them into one group — that would
overstate what the clusterer should do. I flagged only the b9297 split
(Proposal 1 above). The maintenance-only releases (b9286, b9289,
b9291, b9292, b9295, b9301) are correctly treated as cut singletons.

Arman's call: Is this framing correct, or should all llama.cpp patch
releases within a 24-hour window be one group?

---

### TIER DISAGREEMENT — FCA/BoE/Treasury AI + Cyber Statement (2026-05-24)

Cluster: `c_ce540f73ee188ea2`
- Rank.py: `on_the_radar` (score 69)
- Eval Engineer draft: `pulse`

This is the FCA, Bank of England, and HM Treasury joint statement directly
naming frontier AI models as a cyber resilience risk to UK-regulated firms.
The rubric scored it: significance 75, big_picture_relevance 90,
financial_services_impact 100, freshness_momentum 75. The overall score
lands at 69 (second highest on the day behind the OCR benchmark at 70).

Why I'm proposing pulse: for an AI newsletter with a finance-services lens,
a tripartite UK regulator statement that names frontier AI as an operational
resilience risk to regulated firms is not merely "on the radar" — it forces
a concrete reprioritisation of AI risk posture for any UK-regulated bank or
insurer's AI governance team. The rubric's significance anchor at 75 reflects
that this hits only two of the three signal-filter dimensions (today and
tomorrow, but limited practical/hands-on), which correctly holds the numeric
score below 80. But the editorial weight of the story for our specific
audience exceeds what the score alone captures.

Counterargument: the significance weight (30) deliberately keeps stories
without practitioner artefacts from dominating. A regulatory statement with
no new capability or code may not merit pulse over a benchmark with direct
adoption paths. The rubric is working as designed.

Arman's call: pulse or on_the_radar? This is pure editorial judgment —
yours, not mine. I've marked it as a disagreement so you see the flag.

---

## PER-CLUSTER WALKTHROUGH — 2026-05-23

Ten clusters labelled. All human_relevance values are blank for you to fill.
Rank.py's score and tier are noted for comparison.

---

### c_56849ea45c325178 — NuExtract3 4B VLM for structured extraction

Source: r/MachineLearning (Reddit)
URL: Reddit post [P]
Rank.py: score 65, tier on_the_radar
Pipeline audience tags: [hands_on, finance]
My draft: expected_tier on_the_radar, tags [hands_on, finance]

Story: NuExtract3 is a 4B vision-language model for structured extraction
from PDFs, invoices, forms, images. Apache 2.0, self-hostable, available on
HuggingFace with a Space for trying it live. The FS angle is real and named:
document processing under data-residency constraints (banks cannot send
customer documents to cloud APIs).

Relevance 1-5: ___

Tier agreement: Yes — on_the_radar is right. Docs extraction is Tier-1
(agentic/GenAI), two signal-filter dimensions (today + practical), solid.
Pulse would require it to also shift what to anticipate tomorrow.

---

### c_66d8cbbdd7440a46 — BeeLlama v0.2.0 DFlash: 4-5x speedup on RTX 3090

Source: r/LocalLLaMA (Reddit)
Rank.py: score 58, tier on_the_radar
My draft: expected_tier on_the_radar, tags [hands_on]

Story: BeeLlama v0.2.0 ships a major DFlash update delivering 4-5x
inference speedup on a single RTX 3090 for Qwen3.6-27B (164 tps, 4.40x)
and Gemma4-31B (177.8 tps, 4.93x). GitHub repo with quickstart docs.

Relevance 1-5: ___

Tier agreement: Yes — on_the_radar. Drop-in speedup with a repo and
quickstart is practically usable now. Limited to a narrow hardware
(RTX 3090) and the DFlash approach is experimental. No FS angle.

---

### c_281ac65379335626 — Nemotron-Labs Diffusion LMs (speed-of-light generation)

Source: Hugging Face Blog
Rank.py: score 57, tier on_the_radar
My draft: expected_tier on_the_radar, tags [hands_on, general]

Story: NVIDIA's Nemotron-Labs presents diffusion language models on HF as
a structural alternative to autoregressive decoding. The blog post claims
large inference speed improvements. Practitioners can run benchmarks today.
Production readiness and FS-specific applicability are unproven.

Relevance 1-5: ___

Tier agreement: Yes — on_the_radar. Real topic, benchmarkable today, but
"production readiness unproven" keeps it off pulse.

---

### c_20a06e5b2a467c72 — Apex-Testing: agentic coding benchmark

Source: r/LocalLLaMA (Reddit)
Rank.py: score 55, tier on_the_radar
My draft: expected_tier on_the_radar, tags [hands_on, general]

Story: Apex-Testing is a real-world agentic coding benchmark using 65-70
private GitHub repos with real bugs and feature requests. ELO leaderboard.
Updated with recent models. Engineers evaluating which model to deploy in
coding-agent workflows can use this today. Funded by the community; single
source but linked website (apex-testing.org).

Relevance 1-5: ___

Tier agreement: Yes — on_the_radar.

---

### c_4a9fa5a7bf386dee — Safe Bilevel Delegation (multi-agent trust handoff)

Source: LLMQuant Newsletter
Rank.py: score 44, tier on_the_radar
My draft: expected_tier on_the_radar, tags [big_picture, finance]
NOTE: finance tag is low-confidence. The agentic FS angle is implied
(multi-agent trust in financial workflows) but not named in the source.

Story: LLMQuant Newsletter covers a framework for quantifying when one
AI should hand control to another (trust-threshold between 0 and 1). Relevant
to multi-agent agentic FS workflows conceptually. Single-source newsletter
coverage; no repo, no paper link, no code to act on.

Relevance 1-5: ___

Tier agreement: Yes — on_the_radar is the right call given no artefacts.
Borderline; could argue cut on "no repo/paper" basis.

---

### c_13639cc37f23d132 — Latent Space: "All Model Labs are now Agent Labs"

Source: Latent Space
Rank.py: score 40, tier on_the_radar
My draft: expected_tier on_the_radar, tags [big_picture, general]

Story: Latent Space synthesis piece using quotes from Google, OpenAI,
Anthropic, Meta, Mistral and others to argue that all model labs are
pivoting to focus on agents. Useful strategic framing. No new artefacts,
repos, or techniques — big-picture signal only.

Relevance 1-5: ___

Tier agreement: Yes — on_the_radar. Strategic framing with no artefacts
is the right middle ground.

---

### c_c997d4c916785564 — Liveness detection generalisation gap (KYC)

Source: r/MachineLearning (Reddit)
Rank.py: score 41, tier on_the_radar
My draft: expected_tier on_the_radar, tags [big_picture, finance]

Story: Reddit discussion raising a concrete generalisation question: can
liveness/deepfake detection models that were trained on one generation of
synthetic media detect the next generation? The vendor-update-cycle problem
is real for KYC/identity fraud teams at banks. No code, no paper, no
benchmark — but the problem statement is clear.

Relevance 1-5: ___

Tier agreement: Yes — on_the_radar. Real problem, FS-relevant, but no
artefacts to act on immediately.

---

### c_8750df63d65640fc — Needle 26M beats Qwen3-0.6B on CPU function calling

Source: r/LocalLLaMA (Reddit)
Rank.py: score 57, tier on_the_radar
My draft: expected_tier on_the_radar, tags [hands_on, finance]
NOTE: finance tag is low-confidence. Air-gapped inference angle is FS-
relevant (banks running models on constrained hardware) but not explicit
in the post.

Story: Benchmark of Needle 26M (a 26M-parameter specialist model) vs
Qwen3-0.6B on 50 CPU-only function-calling queries across 5 difficulty
tiers. The 23x smaller model wins on both accuracy and speed (4.4x faster).
Directly actionable for engineers deploying tool-use agents on constrained
or air-gapped hardware.

Relevance 1-5: ___

Tier agreement: Yes — on_the_radar.

---

### c_5522007d061bfb5b — DeepSeek $10.29B round + open-source commitment

Source: r/LocalLLaMA (Reddit)
Rank.py: score 27, tier cut
My draft: expected_tier cut, tags [big_picture, general]

Story: DeepSeek advancing a $10.29B financing round; Liang Wenfeng
committing to continue developing open-source AI models rather than
short-term commercialisation. Strategic open-weight ecosystem news.
No new capability or practitioner artefact this week.

Relevance 1-5: ___

Tier agreement: Yes — cut. Funding story only.

---

### c_8fd3af4b47beee7e — "The Business Case for Running Your Own AI Models"

Source: AI in Finance (Christophe Atten)
Rank.py: score 34, tier cut
My draft: expected_tier cut, tags [big_picture, finance] (low-confidence)

Story: Opinion-led build-vs-buy framing on local model hosting for
financial services. Data residency and cost angle is real and named. But
no new capability, benchmark, or artefact makes this more than a
strategy-deck footnote. Thought leadership without news.

Relevance 1-5: ___

Tier agreement: Yes — cut.

---

## PER-CLUSTER WALKTHROUGH — 2026-05-24

Ten clusters labelled. Same format.

---

### c_557d8de6f20e0a3b — Vision-LLMs vs OCR benchmark (30 image-heavy PDFs)

Sources: r/LocalLLaMA + r/MachineLearning (same post, cross-posted)
Rank.py: score 70, tier on_the_radar (highest score on the day)
My draft: expected_tier on_the_radar, tags [hands_on, finance]
Dedup: CORRECTLY MERGED — same post cross-posted to two subreddits.

Story: Empirical benchmark of vision-capable LLMs (native PDF reading)
vs OCR pipelines on 30 image-heavy PDFs (MMLongBench-Doc benchmark, 171
questions). Key finding: premium OCR (LlamaCloud/Azure) beats native vision
at ~59% vs ~52% accuracy, and native PDF is the most expensive arm.
The "vision LLMs make OCR obsolete" claim does NOT hold on chart/table-heavy
pages. Statistical validation via McNemar's test. Full writeup linked.

FS relevance: Document-heavy workflows are pervasive in banking (contracts,
regulatory filings, financial statements). This constrains architecture
choices for RAG systems this week.

Relevance 1-5: ___

Tier agreement: Yes — on_the_radar. Could argue pulse but score 70 reflects
that it hits today + practical but not strongly "shifts what to anticipate
tomorrow" at a strategic level.

---

### c_ce540f73ee188ea2 — FCA/BoE/Treasury joint AI + cyber resilience statement

Source: FCA News
Rank.py: score 69, tier on_the_radar
My draft: expected_tier PULSE (see disagreement section above)
Tags: [big_picture, finance]

Story: Joint statement from the FCA, Bank of England, and HM Treasury
on frontier AI models and cyber resilience. Directly flags AI as a risk
to operational resilience of UK-regulated firms. Forces a concrete
reprioritisation of AI risk posture and governance frameworks under
existing PRA/FCA mandates.

Relevance 1-5: ___

Tier: on_the_radar (rank.py) vs pulse (my draft). Your call, Arman.

---

### c_7eb67898d3d843bd — Command A+ (218B MoE) on Apple Silicon via MLX

Source: r/LocalLLaMA (Reddit)
Rank.py: score 61, tier on_the_radar
My draft: expected_tier on_the_radar, tags [hands_on, general]

Story: Community implementation of mlx-lm support for Cohere Command A+
(218B total / 25B active, Apache 2.0). PR open on ml-explore/mlx-lm.
Engineers with 192GB+ Apple Silicon can test this week. Architecture
notes included (sigmoid routing, sliding window 3:1, parallel attn+MLP).

Relevance 1-5: ___

Tier agreement: Yes — on_the_radar.

---

### c_7ec76012cc4eac10 — NVFP4 + MTP on llama.cpp (community post)

Source: r/LocalLLaMA (Reddit)
Rank.py: score 61, tier on_the_radar
My draft: expected_tier on_the_radar, tags [hands_on]
DEDUP MISS: paired with c_58c6a5766fc586a1 (see above)

Story: Community post announcing that llama.cpp b9297 ships both NVFP4
quantisation and multi-token prediction together. Links directly to the
GitHub release. Directly actionable for engineers running open-weight
models on NVIDIA GPUs today.

Relevance 1-5: ___

Tier agreement: Yes — on_the_radar.

---

### c_755e105526b003bd — Ollama v0.30.0

Source: Ollama releases
Rank.py: score 61, tier on_the_radar
My draft: expected_tier on_the_radar, tags [hands_on]

Story: Ollama v0.30.0 switches from its own fork to direct llama.cpp
integration and adds MLX acceleration on Apple Silicon. This is an
architecture change that affects inference performance and GGUF
compatibility for practitioners running local models. Drop-in update
for existing Ollama users.

Relevance 1-5: ___

Tier agreement: Yes — on_the_radar.

---

### c_cf0b99c06c42a9ba — llama.cpp server native tools (exec_shell, edit_file, etc.)

Source: r/LocalLLaMA (Reddit)
Rank.py: score 60, tier on_the_radar
My draft: expected_tier on_the_radar, tags [hands_on, finance]

Story: The experimental --tools flag in llama.cpp server adds native
exec_shell, edit_file, read_file, file_glob_search, grep_search, write_file,
edit_file, apply_diff, get_datetime. This turns llama-server into a mini
agent harness without Python orchestration or MCP. No security sandboxing
yet — engineers need to handle that themselves. For FS: relevant to
air-gapped/on-prem LLM deployments where adding orchestration layers is
constrained.

Relevance 1-5: ___

Tier agreement: Yes — on_the_radar.

---

### c_6f9686fc752c4d97 — Local GUI for TradingAgents (Ollama)

Source: r/LocalLLaMA (Reddit)
Rank.py: score 56, tier on_the_radar
My draft: expected_tier on_the_radar, tags [hands_on, finance]

Story: Fork of the TradingAgents multi-agent stock analysis framework
with a web GUI. Works with Ollama (local LLMs) and all major API providers.
Apache 2.0. Live pipeline visualisation, 3-pane report reader, multi-session
chat with pinned past reports. FS angle: local inference for trading analysis
without cloud API costs or data-residency concerns. Sample reports for AAPL
and NVDA included.

Relevance 1-5: ___

Tier agreement: Yes — on_the_radar. The TradingAgents base framework is
real (github.com/TauricResearch/TradingAgents); the GUI adds polish.

---

### c_25efe5b1351f4e55 — FCA/BoE tokenisation vision for UK wholesale markets

Source: FCA News
Rank.py: score 29, tier cut
My draft: expected_tier cut, tags [big_picture, finance]

Story: FCA and Bank of England set out a joint vision for tokenisation
of assets in UK wholesale financial markets. Regulatory strategy, FS-relevant,
but zero AI/ML content. By AI Vector's editorial focus rules, this is Tier-3
regardless of FS relevance.

Relevance 1-5: ___

Tier agreement: Yes — cut.

---

### c_eb385cd3c25906f6 — BIS working paper: conditional predictive density tests

Source: BIS Central Bank Research Hub
Rank.py: score 18, tier cut
My draft: expected_tier cut, tags [finance] (low-confidence: niche quant)
DATA BUG: published_at is "2035-08-31" — impossible future date in the feed.

Story: Bank of Spain working paper on specification tests for conditional
predictive densities. Classical econometrics. No GenAI or agentic angle.
Niche relevance for macro forecasters doing density forecast validation.
Not what AI Vector is for.

Relevance 1-5: ___

Tier agreement: Yes — cut.

DATA BUG NOTE: The published_at timestamp "2035-08-31T14:00:00Z" is clearly
erroneous (9 years in the future). This came from the BIS Central Bank Research
Hub RSS feed. The fetch pipeline accepted it without validation. This is a
source data quality issue — do not fix in src/; raise as a bug to the
Source Engineer or LLM Engineer. The freshness_momentum score of 10/100 suggests
rank.py downweighted it, but the bug may affect cross-time dedup and drift
baseline calculations if such papers appear regularly.

---

### c_58c6a5766fc586a1 — llama.cpp b9297 (official release entry)

Source: llama.cpp releases
Rank.py: score 29, tier cut
My draft: expected_tier cut for this entry (it's the redundant half of the
dedup miss), tags [hands_on]
DEDUP MISS: paired with c_7ec76012cc4eac10 (see above)

Story: Official release entry for llama.cpp b9297 (title "b9297", links to
GitHub release). Bare release tag entry; the community Reddit post in
c_7ec76012cc4eac10 is the same release with more context. These should
be one group; the pipeline's decision to score the release entry as cut
and the community post as on_the_radar is correct editorially — the merged
group should surface once under the community post's score.

Relevance 1-5: ___

Tier agreement: Yes — cut for this specific entry (the bare release tag).
The merged dedup group as a whole is on_the_radar.

---

### c_0a75d43e90fe5e1a — FCA motor finance legal challenges (4 items)

Source: FCA News (4 items, all same timestamp)
Rank.py: score 16, tier cut
My draft: expected_tier cut, tags [finance]

Story: FCA RSS batch containing 4 items about motor finance compensation
schemes, claims management market review, and legal challenges. Zero AI
content. All 4 items have identical timestamp (feed batch dump). The
pipeline grouped them correctly as one cluster.

Relevance 1-5: ___

Tier agreement: Yes — cut.

---

## Cross-Time Chain (Labelled)

The following cross-time reference exists in the pipeline data and should
be validated by the dedup eval:

- 2026-05-23 `c_60339c2e21a15eb7` — "ByteShape Qwen3.6-35B-A3B: 30%
  faster than Unsloth IQ on 6GB VRAM laptop" (consumer VRAM quant benchmarks)
- 2026-05-24 `c_1e720df7574da5b7` — "Did a 30 runs of llama-bench to find
  optimal settings for my use case (Frigate and HomeAssistant) on my MI60
  32gb VRAM GPU" — has `cross_time_ref: c_60339c2e21a15eb7`

The pipeline correctly set cross_time_ref on the 2026-05-24 cluster pointing
at the 2026-05-23 cluster. This is a correctly detected cross-time
continuation. Both clusters are about llama.cpp benchmarking on specific
consumer/prosumer GPU hardware. The eval harness should verify this
cross_time_ref is present and correct.

Note: c_60339c2e21a15eb7 was not selected for labelling (it ranked cut,
score 30, consumer quant hobbyist content). I have not added it to the
labels corpus because it adds no signal for the dedup eval beyond what the
cross_time_ref field already captures.

---

## Data Quality Issues Found (Not Fixed — Eval Engineer is read-only in src/)

1. BIS published_at timestamp "2035-08-31T14:00:00Z" (cluster
   c_eb385cd3c25906f6, date 2026-05-24). The BIS Central Bank Research Hub
   RSS feed emitted an impossible future date. The fetch pipeline accepted
   it without validation. Freshness scoring downweighted it (10/100), but
   the erroneous timestamp could corrupt cross-time dedup and drift baselines
   if the pattern recurs. Recommend: add a published_at sanity check in
   fetch.py (reject dates more than N days in the future).

2. FCA News RSS batch dump: all 15 FCA items on 2026-05-24 have identical
   timestamps (2026-05-24T05:45:29.139535Z, which is the fetched_at
   timestamp). The feed appears to provide no per-item published_at, so the
   fetcher defaulted to fetched_at. This makes freshness_momentum
   indeterminate for all FCA items. The clustering still works (content-based
   not time-based), but freshness scoring is wrong for this source.

3. Cluster c_d14e131bbf97dcde (2026-05-23): canonical_title "FAQ", single
   item from Lilian Weng's Blog. The item URL is
   https://lilianweng.github.io/faq/ — the fetcher grabbed the FAQ page
   itself rather than a blog post. This looks like a feed-level issue where
   the Lil'Log RSS feed included the FAQ page. Rank.py correctly scored it 2
   (unscoreable). Recommend the Source Engineer verify the Lil'Log feed
   configuration.

---

## How to Ratify

1. Open this file and `evals/labels.yaml` side by side.
2. For each cluster's `human_relevance: ~  # RATIFY: Arman, 1-5`,
   replace the `~` with your score (1–5 per the scale at the top of labels.yaml).
3. For the two dedup-miss proposals and the one tier disagreement (marked
   in the HIGH-IMPACT CALLS FIRST section), add a one-line comment with
   your decision. That's all I need.
4. Tell the Eval Engineer (me) when done. I commit the ratified file and
   unblock Phase B.

---

## Voice + Quality (Editor's draft)

**Author:** Editor
**Date drafted:** 2026-05-24
**Corpus basis:** Issues #1 (2026-05-23) and #2 (2026-05-24).
**Files for ratification:**
- `evals/voice/rubric.yaml` -- voice rubric (NEW). 5 issue-level voice
  dimensions + 4 per-story quality criteria, anchored 0/25/50/75/100
  with exemplars drawn from Issues #1-#2.
- `evals/voice/2026-05-23.labels.yaml` -- Issue #1 labels: every
  dimension, every story.
- `evals/voice/2026-05-24.labels.yaml` -- Issue #2 labels: same shape.

### Counts (pass / borderline / fail)

**Issue-level voice dimensions (10 verdicts across 2 issues; callback
N/A both days, no chain yet):**
- pass: 6 (warmth x2, signal_density x1, direction x2, finance_lens x1)
- borderline: 2 (signal_density 2026-05-23, finance_lens 2026-05-23)
- fail: 0
- n/a: 2 (callback both issues)

**Per-story verdicts (overall, 21 stories across both issues):**
- pass: 11
- borderline: 10
- fail: 0

**Section intros (6 verdicts across 2 issues):**
- pass: 5
- borderline: 1 (on_the_radar body 2026-05-23 -- "the pattern here is"
  tic + lifts language from section name)

**Per-criterion failures (sub-level):**
- headline length: 1 fail (2026-05-23 Pulse, 17 words / ~100 chars),
  plus ~10 borderlines on word-count or char-cap. This is the
  systematic finding.
- summary word count: 1 fail (2026-05-24 Pulse, ~64 words vs 60 cap)
- everything else: borderline or pass.

### 3-5 highest-priority items for Arman's eye first

1. **Pulse bodies running long, two issues in a row.** 2026-05-23
   Pulse body is ~63 words; 2026-05-24 Pulse body is ~64 words. The
   60-word cap is HARD per the voice block. Two-in-two suggests the
   summarise prompt is treating the cap as soft for Pulse specifically.
   Flag to LLM Engineer.

2. **Headline length is the systematic miss across both issues.** 13 of
   21 headlines exceed the 12-word ideal or the ~90 char cap (or
   both). Most are borderline, not fail, but the volume is the story.
   Worth a targeted prompt nudge on headline compression for the next
   summarise rev.

3. **The 2026-05-23 Pulse headline is borderline twice over.** 17 words,
   ~100 chars, three-clause feature list ("PDFs, invoices, tables,
   screenshots" -> "documents"). The Pulse is the section we watch
   hardest; this one wants a sharper hand. Proposed tighter tagline
   in the labels file.

4. **Section-placement question on c_78dcc648119217a1 (Issue #2).**
   "Writing specs before code..." is tagged "act" but sits in On the
   Radar. An "act"-pill in the awareness-only section is a tell that
   editorial routing and editorial verdict disagree. Body has
   senior-leader posture ("if you're setting coding-agent guidelines
   this quarter"); arguably belongs in Big Picture. Not a voice fail;
   a section-placement question for the weekly review.

5. **Finance lens missed an organic angle on 2026-05-23.**
   c_c997d4c916785564 (deepfake / liveness detection vendor piece)
   sits directly on KYC and customer-onboarding workflows. Framing
   stayed generic-vendor. Not a fail (forced FS framing is worse),
   but a missed sharpening. Worth noting; finance-lens skill exists
   for exactly this kind of case.

### rubric.yaml structure (for ratification context)

- **schema_version: 1, rubric_version: v0.1-2026-05-24** with explicit
  bootstrap caveat (2-issue corpus; re-anchor at 5 issues, again
  at 14).
- **voice_dimensions:** 5 dimensions, each weighted, each anchored at
  0/25/50/75/100 with exemplars pulled from Issues #1-#2 prose.
  Dimensions: warmth (15), signal_density (25), direction (25),
  finance_lens_presence (15), callback_quality (10).
- **per_story_criteria:** 4 criteria with sub-tests and pass /
  borderline / fail exemplars from the corpus. Criteria:
  headline_quality, summary_quality, signal_appropriateness,
  section_intro_quality.
- **judge_instructions:** the contract for the Phase-C LLM-judge
  prompt -- output shape, borderline handling, never-auto-fail rule.
- **reanchoring_schedule:** corpus-size milestones (5, 14) plus an
  on-drift-signal trigger tied to Eval Engineer's detector.

The rubric mirrors `config/rubric.yaml`'s 0/25/50/75/100 anchor
pattern and decision-rights model. Eval Engineer owns weight tuning
and judge mechanics; Editor owns anchor prose; Arman ratifies any
anchor change.

### Posture

Both issues land in-voice on the meaningful axes. Nothing here is a
cut; everything is a sharpening. Recommend ratify the rubric + both
label files as the corpus baseline; flag the headline-length and
Pulse-body patterns to LLM Engineer for the next summarise prompt
pass.

---
