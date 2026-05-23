---
name: editorial-focus
description: Topic weighting + signal filter for AI Vector — heavier on Agentic AI and Generative AI; traditional ML only when load-bearing; ruthless on "strong signal" (what shifts work today, what to anticipate tomorrow, what's practical now). Invoke before any coverage decision — ranking, labelling, sourcing.
---

# Editorial focus — what AI Vector covers and why

AI Vector is a daily, agent-assisted AI newsletter for DS + engineers
(primary) and senior leaders (secondary), with a financial-services lens.
This skill is **how we decide what's in and what's out** before the
finance-lens, the voice, or the rubric weights get involved. It is upstream
of all of those. It's the first cut on coverage.

The editorial DNA is **strong signal, narrow lane**:

- **Heavier on Agentic AI and Generative AI.**
- **Traditional ML** appears only when it's load-bearing for the field today.
- **Always**: what shifts how readers work today, what to anticipate
  tomorrow, what's practical to use right now.

## The three-tier filter

### Tier 1 — covers by default (the heart of the publication)

**Agentic AI**
- Tool-use agents, multi-step reasoning, autonomous workflows
- Agent frameworks (Claude Code, LangGraph, Letta, etc.), agent runtimes,
  orchestration patterns
- Coding agents, research copilots, customer-facing agents, ops agents
- Agent evals, agent failure modes, agent observability
- Computer-use, browser-use, agentic infrastructure

**Generative AI**
- Foundation-model releases that change the capability ceiling or floor
- New training / post-training techniques with practical implications
- Inference advances (latency, cost, context window, modality) that change
  what builders can ship
- Multimodal — vision, voice, video — when the capability is actually new
- Open-source model launches that change the deployment calculus
- RAG, structured outputs, prompt techniques, context engineering — when
  there's real signal, not yet-another-prompt-tip

If a story is in Tier-1 territory **and** clears the signal filter (below),
it's in.

### Tier 2 — covers only when load-bearing

**Traditional ML / classical AI** — covered only when one of these is true:

- Productionised at meaningful scale in a way that changes the practitioner's
  playbook.
- Hybrid systems where classical ML + LLMs are doing something neither could
  alone.
- New methodology the field will absorb (not a one-off paper).
- A FS-specific application (fraud, AML, credit, trading) where the technique
  materially moves the needle.

If you can't name *why this changes how a DS or engineer works this
quarter*, it's not load-bearing. Skip it.

### Tier 3 — out (don't waste the reader's time)

- Vendor announcements with no capability shift ("we partnered with X")
- Model-numbers-go-brrr news with no practical takeaway
- AI-tangential stories with no agentic or gen-AI hook (generic "AI in
  healthcare" / "AI in retail" tropes)
- Hype-cycle pieces, opinion essays with no underlying news, "thought
  leadership" with no specifics
- Re-summaries of last week's news without new information
- Stock / earnings commentary that's really finance news, not AI news

## The signal filter (applied on top of the tiers)

For every Tier-1 or Tier-2 candidate, ask:

1. **Today** — Does this change something a DS or engineer would do *this
   week*? A new tool to try, a new constraint to plan around, a new technique
   to adopt.
2. **Tomorrow** — Does this shift what we should anticipate in the next 1–6
   months? A capability or constraint that's not here yet but will reshape
   the playbook.
3. **Practical** — Can the reader *use* something from this now? A repo, a
   paper with code, a technique, an API, an eval, a benchmark.

A story should hit **at least one** of these clearly. Two is great. Three is
Pulse material.

A story that hits none — even if it's in Tier 1 — is buzz. It goes in *Also
notable* at best; more often, out.

## What this skill is NOT

- Not a topic ban list. We don't refuse to ever cover X. We refuse to cover X
  *unless it earns its place*.
- Not anti-research. Hugging Face Daily Papers stay. We curate harder.
- Not anti-traditional-ML. We are anti-*irrelevant*-traditional-ML.
- Not the finance lens (that's `finance-lens.md`). This is upstream.
- Not the voice (Editor + EDITORIAL.md). This is upstream of voice too.

## How to apply (by role)

### LLM Engineer — ranking + summarising

- Apply this filter **before** scoring against `config/rubric.yaml`. Tier-3
  stories should be cut, or floored at very low scores; the rubric is for
  the survivors.
- In the rubric, **Significance** (currently 30) is where this lens mostly
  lives. Significance ≈ "tier match × signal-filter passes."
- A healthy issue is **heavy on Tier 1, light on Tier 2, none on Tier 3.**
  If your ranked output isn't this shape, something upstream (sources or
  signal threshold) needs attention.

### Editor — labelling + critique

- When labelling, flag any story that's in but doesn't clear the signal
  filter — even if voice is OK. *Voice-OK + signal-weak* is a publication's
  slow death.
- In EDITORIAL.md, examples of off-focus stories matter as much as off-voice
  ones. Keep both kinds of examples up to date.
- When proposing The Pulse, prefer a Tier-1 story hitting two or three of the
  signal-filter dimensions over a Tier-2 story hitting one.

### Source Engineer — curation

- A source that publishes mostly Tier-3 content is not a source. Trust-weight
  floor or disable.
- A source that publishes mostly Tier-1 with high signal density is a 4–5
  trust source.
- When sourcing finance-AI feeds, use this skill **alongside**
  `finance-lens.md`: a finance-AI feed must pass both — Tier-1 (or
  load-bearing Tier-2) topics *and* the finance angle.
- Track per-source tier mix in `source_health.json` notes — over months, the
  pattern decides trust.

## Calibration — when to relax the tiers

Rare but real:

- **Field-shaping events.** A major safety paper, a major outage, a
  significant regulatory decision: cover it even if it's classical or
  non-agentic, because the *meta-story* is Tier-1 — what should builders do
  about it.
- **Slow days.** Better a quieter, honest *Also notable* tail than a forced
  Pulse on a Tier-3 story. On slow days, *fewer* stories — not weaker ones.

The Eval Engineer watches drift: weeks where Tier-2 or Tier-3 stories creep
above ~25% of ranked output are a signal to recalibrate sources or
prompts, not a signal to relax further.
