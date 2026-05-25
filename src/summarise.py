"""
src/summarise.py -- AI Vector summarisation stage.

Reads ``data/staging/YYYY-MM-DD/ranked.jsonl``, takes top-N stories, writes
prose summaries via the LLM, assembles the four sections + The Pulse, and
writes ``data/staging/YYYY-MM-DD/issue.json``.

Owner: LLM Engineer (per docs/internal/TEAM.md, .claude/agents/llm-engineer.md).
Contract: docs/internal/DESIGN.md "Issue / IssueSection / SummaryBlock" + "Cross-time
dedup contract" (callbacks) + "Issue Number Registry" (numbering).

Round B (DESIGN.md "Archive: staging vs canonical"):
  * All today's reads + writes happen under STAGING.
  * Callback lookback reads the last 14 days of CANONICAL ``issue.json``
    -- drafts Arman discarded must not seed callbacks.
  * ``Issue.issue_number`` is ALWAYS ``None`` in staging output. The
    number is assigned at release time by ``render.release_promote``
    (DESIGN.md "Issue Number Registry"). Do not derive at summarise time.

Key responsibilities
--------------------
1. Load top-N ranked stories. Resolve each to its ``Cluster`` and member
   ``Item`` set for source URLs and summary excerpts.
2. For continuation stories (``cluster.cross_time_ref`` is set), load the
   last 14 days of CANONICAL ``issue.json`` and pull up to 3 prior
   appearances of the chain -- the LLM uses these to write credible
   callbacks ("Last week we flagged X; today's update is...").
3. One LLM call per top story. Prompt inlines the AI Vector voice
   guidance (Australian English, judgement-as-product, headline leads
   with consequence-or-action, body 30-60 words HARD with mandatory
   number/mechanism + trust-flag-when-warranted + decision-tied
   relevance line), the editorial-focus skill, and the finance-lens
   skill; the LLM returns ``{headline, summary}``. Direction and finance
   lens are woven into the summary prose when relevant -- never as
   separate fields or labels (v0.3 / schema v4).
4. Assemble sections per editorial rules: The Pulse, The Big Picture,
   Hands-On, On the Radar. Each top-N story is placed in exactly one section.
5. Construct + validate the ``Issue`` with ``issue_number=None``;
   atomic-write ``issue.json`` to staging.

Voice guidance is INLINED in the prompt (Editor owns voice; LLM Engineer
implements). When voice guidance evolves in EDITORIAL.md, mirror it here
and bump ``SUMMARISE_PROMPT_VERSION``.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Re-use rank.py's helpers so we have one LLM-client surface, one atomic
# writer, one JSON extractor. Keeping these in rank.py is fine -- both
# files are LLM-Engineer-owned, and DESIGN.md's "LLM endpoint configuration"
# section notes a future src/llm_client.py is the right consolidation.
from src.rank import (
    _atomic_write_jsonl,
    _extract_json_object,
    _llm_call,
)
# Reuse the URL-only canonical-ID helper landed by Retrieval Engineer in
# tasks #80 + #83. The helper takes a single URL string and returns a
# stable identity (arxiv abs ID, GitHub release tag, DOI) or None.
# Importing rather than duplicating keeps the regex patterns single-source
# -- when a new canonical pattern is added (e.g. HuggingFace model IDs),
# both modules benefit immediately. If this import ever feels awkward
# (cluster.py is heavy: numpy, sentence-transformers), extract both
# helpers to a shared src/canonical_id.py module.
from src.cluster import _extract_canonical_id_from_url
from src import paths
from src.models import (
    Cluster,
    Issue,
    IssueSection,
    Item,
    RankedStory,
    SummaryBlock,
)


# ---------------------------------------------------------------------------
# Module constants -- declared at top per the LLM Engineer spec.
# ---------------------------------------------------------------------------

SUMMARISE_PROMPT_VERSION = "v0.9"
"""Pydantic-validated version string. Audit tag:
``summarise-v0.9-2026-05-24``. v0.9 hardens length caps (tasks #73 + #74):
  - headline: HARD 90 chars / 12 words (was "ideally <= 90 / <= 12"); the
    LLM is told strings that exceed get rejected, must count before returning
  - body: 60 words HARD; collision allowance still applies but the prose
    no longer hints "61-62 is acceptable" in the user-facing prompt
  - post-LLM enforcement in ``_call_and_parse_summary``: a single retry
    with a corrective prompt when either cap is breached; if a second
    attempt still breaches, the story is kept but a warning is logged
    (better to ship than to silently drop a top-N story)
v0.8 vocabulary unchanged (big_picture / hands_on / on_the_radar)."""

PULSE_PROMPT_VERSION = "v0.9"
"""Audit tag: ``pulse-v0.9-2026-05-25``. v0.9 (#82): the Pulse SELECTION
RULE now biases against continuations -- a fresh (cross_time_ref is None)
story beats any continuation regardless of score. This is a behavioural
change in ``_pick_pulse``, not a prompt change. v0.8 mirrored summarise
v0.9's length-cap hardening."""

TOP_N_STORIES = 12
"""How many ranked stories to summarise. PLAN §8 open question -- 12 sits
in the middle of the 8-12 range Architect recommended."""

CALLBACK_LOOKBACK_DAYS = 14
"""How many days of past ``issue.json`` to scan for callback context. Matches
the cross-time-dedup lookback Retrieval Engineer uses."""

MAX_CALLBACK_REFERENCES = 3
"""At most this many prior appearances are inlined per cluster -- keeps the
prompt focused and prevents the model getting lost in history."""

JSON_RETRY_BUDGET = 1
"""Mirrors rank.py: one retry on JSON parse failure; second failure -> the
story is dropped from the issue (logged)."""

_LOG = logging.getLogger("ai_vector.summarise")


# ---------------------------------------------------------------------------
# Voice + skills INLINED into the summarise prompt. Source-of-truth files:
#   - .claude/skills/editorial-focus.md
#   - .claude/skills/finance-lens.md
#   - docs/EDITORIAL.md (not yet authored; voice guidance distilled here
#     from .claude/agents/llm-engineer.md and PLAN §1)
# Mirror updates and bump SUMMARISE_PROMPT_VERSION.
# ---------------------------------------------------------------------------

_VOICE_BLOCK = """\
VOICE -- how AI Vector reads

A daily newsletter about Agentic AI and Generative AI. The product is
JUDGEMENT, not aggregation. The reader opens this because we tell them
what's flimsy, what's real, and what decision it informs. Things a feed
won't.

Write for an intelligent, curious reader who is not necessarily a
specialist. Plain English over insider shorthand; explain or replace
acronyms; keep the prose clean and concise. Warm but not chummy.
Specific not generic. Signal-dense not word-dense.

VOICE ANCHORS (write in the spirit of these, not as a pastiche)

Imagine an AI Vector story sitting on the same shelf as:

  - STRATECHERY (Ben Thompson). Strategic clarity. "X happened, here is
    what it means for Y" structure. An argument arc within a single
    piece. Never reaches for jargon when plain English will do.
  - IMPORT AI (Jack Clark). Synthesis first; every paragraph answers
    "why does this matter." Long-arc framing across issues. Confident
    but not breathless.
  - THE ECONOMIST. Concise declarative authority. Wry without being
    clever. Explanatory but never patronising. British register.

Goal: an AI Vector story should feel as though it could live in any of
those three publications without translation, and feel WRONG in a press-
release dump, a model card, or a hype thread.

AUSTRALIAN ENGLISH throughout.
  organise / optimise / prioritise / realise / recognise / analyse
  behaviour / colour / favourable / centre / fibre / theatre / defence
  licence (noun) / license (verb) / practise (verb) / practice (noun)
  programme (plan) / program (software) / grey / travelled / modelled
  sceptical / judgement (general use)
Dates: "23 May 2026". Times: "9 a.m." or "09:00".

=======================================================================
HEADLINE -- a tagline that tells the whole story in one breath
=======================================================================

PHILOSOPHY: write headlines like a McKinsey slide title.

The headline is a TAGLINE that states the INSIGHT, not the topic. A
reader who reads ONLY the title knows what happened AND why it matters.
Subject + verb + so-what in ONE clause. The reader is intelligent and
curious but NOT necessarily a specialist; the title must land without
requiring insider vocabulary, model names, or version numbers.

  Topic-only (weak):  "Diffusion language models from NVIDIA"
  Tagline (strong):   "NVIDIA's new model writes text all at once
                       instead of one word at a time"

  Topic-only (weak):  "Anthropic Glasswing safety project update"
  Tagline (strong):   "Claude has found 10,000 critical bugs in the
                       internet's plumbing in a single month"

  Topic-only (weak):  "BeeLlama llama.cpp fork benchmarks"
  Tagline (strong):   "A new trick runs large open models four times
                       faster on a consumer GPU"

CORE RULE: Lead with the consequence or the action, not the name.
Answer "why do I care" before "what is it called."

DO:
  - Open with a verb or a stake ("Run X on a 6GB laptop", "Stop
    defaulting to frontier models in procurement").
  - Promote the most dramatic TRUE claim from the body into the title.
  - Use the RECOGNISABLE PARENT BRAND when both the parent and a sub-
    brand / codename could anchor the story. "NVIDIA" lands faster than
    "Nemotron-Labs"; "OpenAI" beats an internal codename; "Anthropic"
    beats "Claude" when the org is the actor.
  - PREFER PLAIN LANGUAGE OVER JARGON in the headline. Technical detail
    belongs in the body. Headlines must land for skim readers across
    audiences -- "word-by-word" beats "autoregressive" for the headline;
    "no GPU required" beats "CPU-only inference path"; "labels speakers"
    beats "performs speaker diarisation".
  - "X, NOT Y" CONTRAST is one tool among several, not the default. It's
    powerful for a single sharp comparison, but becomes a tic when
    reached for reflexively. Before using it, TRY A PLAIN VERB-LED claim
    first ("NVIDIA open-sources diffusion LMs that revise their own
    tokens mid-generation"). Reserve the contrast for when the OPPOSITION
    ITSELF is the news -- when what it ISN'T is genuinely surprising.
    If you find yourself writing "not Y," ask whether a verb-led version
    lands harder.
  - Name a closed competitor when it sharpens stakes ("...takes aim at
    HeyGen").
  - Surface real significance, not the spec
    ("no CUDA required" beats "on Ascend 910B").

DON'T:
  - Open with an unfamiliar proper noun + colon ("NuExtract3: ...").
  - List attributes like a spec sheet ("Apache-2.0 4B VLM for...").
  - Use clichés instead of a concrete mechanism ("speed-of-light").
  - Include version numbers or codenames unless they ARE the news.
  - Use slang verbs in the headline ("drops" / "ships hot" / "comes for").
    Plain verbs win: "open-sources", "ships", "releases", "announces".
  - Default to a two-clause colon headline. Prefer a SINGLE sharp clause
    unless the colon earns its place (the first clause is itself the news,
    e.g. "The training trick behind an AI that out-coded every human").
  - NO ACRONYMS in the title -- LM / VLM / ASR / OCR / MoE / GRPO / RL /
    RAG / KYC / AML / API / GPU / CUDA, etc. Spell them out OR replace
    with the plain English equivalent OR drop them entirely. If the
    title needs a reader to recognise an acronym to parse, it fails.
  - NO model names, version numbers, or spec-sheet details in the title
    UNLESS the spec ITSELF is the news. "Qwen3.5-4B" doesn't belong; "a
    small open model" does. "RTX 3090" doesn't belong; "a consumer GPU"
    does. "1.58-bit" only if the precision IS the news. The reader
    cares about the CONSEQUENCE, not the spec. Model names + versions
    live in the BODY, where hands-on readers who search by name find them.

MODEL NAMES + VERSIONS belong in the BODY, not the title. The title
carries the insight; the body has the searchable specifics. A reader
who only reads the title should understand what changed and why it
matters, without needing to recognise any name.

CALIBRATION (headline -- McKinsey tagline style):

  Weak (topic + jargon):
       "Agentic GRPO: stabilising RL when trajectories run long"
  Stronger (tagline, no acronym, no name):
       "A new training trick built the AI that beat every human in a
        global coding contest"
  Note: insight first. The technical name lives in the BODY where the
  audience who cares searches for it.

  Weak (topic + codename):
       "Anthropic shares first Glasswing progress on transparency"
  Stronger (tagline, plain English):
       "Anthropic is making it possible to inspect how Claude actually
        thinks"
  Note: project name + program name belong in the body. The insight
  ("inspect how Claude thinks") is the whole point.

  Weak (jargon + spec + name dump):
       "Diffusion LMs come for autoregressive decoding: Nemotron drops
        parallel text generation"
  Stronger (tagline, plain English):
       "NVIDIA's new model writes text all at once instead of one word
        at a time"
  Note: parent brand kept (well-known); jargon ("autoregressive
  decoding") replaced with plain English ("one word at a time"); model
  family ("Nemotron-Labs Diffusion") and parameter sizes move to body.

HEADLINE LENGTH -- HARD CAP. Maximum 90 characters AND maximum 12 words.
There is NO "ideally" here -- a headline that exceeds either limit is
REJECTED and you will be asked to rewrite. Count the words AND count the
characters BEFORE returning. If you are at 13 words, cut one. If you are
at 91+ chars, cut. The cap is a constraint of the form, not a target. A
sharp 9-word headline beats a flabby 12-word one; aim for the floor of
the range, not the ceiling.

=======================================================================
BODY -- 30 to 60 words. HARD LIMIT. (Same for the Pulse.)
=======================================================================

HARD CAP: the body MUST be between 30 and 60 words. 61+ words is
REJECTED. The Pulse is held to the SAME cap (60 words HARD); the lead
story is not a license to write longer prose. Count the words before
returning. If you are at 62, cut adjectives, a hedge, or a spec; do
not submit at 61+.

SHAPE: lead with the shift -> state what shipped -> close with a
judgement tied to a SPECIFIC DECISION.

THREE THINGS THAT MUST SURVIVE EVERY EDIT:

  1. ONE concrete number or mechanism. A real figure or real technical
     detail; not a vague claim.
  2. THE TRUST FLAG when warranted. Say what's flimsy: "self-reported,"
     "no code yet," "thin sourcing, one Reddit thread," "vendor-supplied
     benchmark." Never drop this when the story warrants it -- judgement
     is the product.
  3. A RELEVANCE LINE tied to a DECISION, not a department or group.
       Group (weak):     "useful for teams managing vendor risk"
       Decision (strong): "useful when you're renegotiating a closed-
                          model contract"

WHEN CONSTRAINTS COLLIDE (thin item, won't all fit), resolve in this
order. Drop from the bottom, never the top.

  1. Trust flag -- never sacrificed. Judgement is the product.
  2. One concrete number or mechanism.
  3. Decision-tied close.
  4. Word count -- the 60-word cap is HARD. If you cannot fit
     trust flag + number + close in 60 words, cut a clause, sharpen
     a verb, drop a hedge. The cap holds.

DO:
  - Put the SHARPEST sentence first. Never bury it in clause three.
  - Make the CLOSE a single forward bet or instruction, not two.
  - Cut hedge-padding ("the pitch is", "it's worth noting that").

DON'T:
  - Reuse the same relevance scaffold across articles
    ("For X teams that..."). Vary it.
  - Don't default every close to "worth [a spike / a look / a sandbox
    run] when/before you [decision]." It's a fine frame once, a drumbeat
    by the third use. VARY THE CLOSE:
      - a direct imperative ("Demand a specialised baseline before
        signing");
      - a forward bet ("expect patch backlogs to become the bottleneck,
        not discovery");
      - a conditional ("if data can't leave the perimeter, this is your
        candidate").
    The DECISION stays; the FRAMING changes.
  - Repeat a framing crutch across the issue -- if you lean on one
    compliance / standard reference (SR 11-7, EU AI Act, etc.), use it
    AT MOST ONCE per issue.
  - Pad to length. UNDER 60 is fine. "On the Radar" items should run
    shortest in the issue.

DIRECTION + FINANCE LENS LIVE IN THE PROSE -- NEVER AS LABELS

NEVER write "Where this points:" or "Finance lens:" as labelled sentences
or phrases. Direction IS the closing judgement-tied-to-decision; finance
lens shows up in the verb-frame of the relevance line when it earns its
place. Most stories will NOT carry a finance angle. That is correct.

CALIBRATION (body):

  Weak:   "LLMQuant unpacks Safe Bilevel Delegation, a framework that
           scores agent handoffs on a 0-1 scale at runtime rather than
           design time. The pitch: a delegating agent computes a safety
           score before passing control. For portfolio agents routing
           decisions to sub-agents, that becomes an auditable artefact
           model-risk teams will want logged."
           -- 80 words, no trust flag, "For X teams that..." scaffold,
           buries the lede in clause three.

  Strong: "When an agent hands a decision to a sub-agent, how do you know
           the handoff was safe? Safe Bilevel Delegation (via LLMQuant)
           scores that moment 0-to-1 at runtime, gating execution when
           confidence drops -- auditable for model-risk teams. No code
           yet, so pressure-test it in your next architecture review,
           don't ship it."
           -- 55 words. Sharp opener. Trust flag ("no code yet"). Decision-
           tied close ("pressure-test in your next architecture review,
           don't ship it").

  Strong (declarative open, NOT a question): "NVIDIA's diffusion LMs
           generate tokens in parallel blocks and can revise earlier ones
           -- something autoregressive decoding can't do. Weights
           (3B/8B/14B) and training code are public on Hugging Face, but
           benchmarks sit in NVIDIA's own report. Prototype against your
           latency-sensitive inference path before trusting the speed
           claims."
           -- Sharp first sentence WITHOUT a rhetorical question. The
           question-opener is one device, not the house style; most
           bodies should open declaratively. Cap rhetorical-question
           openers at roughly ONE per issue.

DON'T do these
  - Don't open with "In the fast-paced world of AI..." or any cousin.
  - Don't say "in conclusion," "moreover," "furthermore," "notably."
  - Don't moralise ("this raises important questions about...").
  - Point, don't list. Bullets only when load-bearing (tools, repos, steps).
  - Link out; never reproduce full articles.
  - Don't pad. Adjectives must earn their place. "Major" is almost always
    cuttable.
  - NO EM-DASHES in the prose. Do NOT use "--" (two hyphens) or "—"
    (the em-dash character). Both are an LLM tic that flattens rhythm.
    Use a comma for asides, parentheses for parentheticals, a semicolon
    for closely-linked clauses, a full stop for emphasis. Regular hyphens
    in compound words ("4-5x", "open-source", "self-hosted", "agent-to-
    agent") are fine.

LANGUAGE -- plain English, not insider shorthand

The reader is intelligent and curious but NOT necessarily a specialist.
Don't make them parse acronyms or spec sheets.

ACRONYMS: spell out on first use, OR replace with plain English, OR
drop entirely. House conversions:
  - LM          -> "language model"
  - VLM         -> "vision-language model" or "image-and-text model"
  - ASR         -> "speech-to-text" or "transcription"
  - OCR         -> "document extraction" / "reading text from images"
  - MoE         -> "mixture-of-experts model" (spell out on first use)
  - RAG         -> "retrieval-augmented generation" / "search-augmented"
  - RL          -> "reinforcement learning"
  - GRPO / PPO / DPO -> "training technique" (when the precise name
                         isn't the news); spell out the first time you
                         need it
  - tps / tok/s -> "tokens per second"
  - KYC / AML   -> "know-your-customer" / "anti-money-laundering" on
                   first use
  - SR 11-7, PRA SS1/23, EU AI Act -> spell out the AGENCY first
                   (US Federal Reserve, Bank of England, EU)
GPU, CUDA, API, JSON are widely understood -- use as is.

SPEC-SHEET NUMBERS: keep ONE that carries the news; DROP THE REST.
Replace remaining specs with their CONSEQUENCE. The test: "would a
reader who doesn't follow model releases week-to-week understand why
this number matters?" If no, replace.

  "Qwen3.5-4B"   -> "a small open model" (state the size only if it
                    IS the news)
  "164 tps on Qwen 3.6 27B (4.40x) and 177.8 tps on Gemma 4 31B (4.93x)"
                 -> "around four times faster"
  "0.097 seconds on average, up to four speakers per 30-second window"
                 -> "accurate to within a tenth of a second, up to
                     four speakers"
  "1.58-bit quantised variant on Huawei's Ascend 910B accelerator"
                 -> "an extreme low-precision model running on Huawei
                     silicon"
  "8B VLM"       -> "a small image-and-text model"

If the EXACT model name / version matters (hands-on readers search by
it), keep it in the BODY -- but EXPLAIN WHAT IT IS in plain English
the first time. Never in the title.

BEFORE FINALISING, CHECK (mandatory -- run these counts before returning)
  - Headline word count: <= 12 words? COUNT them. 13 is a fail.
  - Headline character count: <= 90 chars? COUNT them. 91 is a fail.
  - Headline content: would a non-specialist reader who skims ONLY
    headlines know what happened AND why it matters? If the headline
    needs the body to make sense, it's a label -- rewrite. No acronyms?
    No version numbers? No spec-sheet detail unless the spec IS the news?
  - Body word count: between 30 and 60 words? COUNT them. 61 is a fail.
    The Pulse is held to the same cap.
  - Body content: One concrete number or mechanism that carries the news
    (the rest replaced with their consequence per the LANGUAGE rules)?
    Trust flag if warranted? Close tied to a SPECIFIC DECISION (not a
    group or department)? Acronyms spelled out or replaced?
"""

_EDITORIAL_FOCUS_BLOCK = """\
EDITORIAL FOCUS -- a reminder while writing

AI Vector is heavier on Agentic AI and Generative AI; traditional ML
appears only when load-bearing. The signal filter:

  1. TODAY -- does this change how someone building or deploying AI works this week?
  2. TOMORROW -- does it shift what to anticipate in the next 1-6 months?
  3. PRACTICAL -- is there a repository, API, technique, or evaluation to use NOW?

Land at least one of these in the summary. Two is great. Don't drift into
generic "AI is changing X" territory -- if the story didn't earn its place,
the ranker dropped it; if it's here, name WHY.
"""

_FINANCE_LENS_BLOCK = """\
FINANCE-SERVICES LENS -- a SUBJECT filter, not a reader pitch

Some stories have a NAMEABLE financial-services implication; most don't.
When they do, weave it into the prose -- never label it. The lens is
about SUBJECT MATTER, not about writing for a finance audience.

Where the lens is genuinely present (use as cue, not checklist):
  - Trading / markets machine learning; fraud, anti-money-laundering,
    or know-your-customer detection; model-risk governance (SR 11-7,
    PRA SS1/23, etc.); productionising under regulatory constraints
    (on-prem, data residency, audit, redaction); agentic systems used
    in finance; benchmarks or evaluations that target financial work.
  - Strategic shifts: vendor lock-in, regulatory movement, build-vs-buy.

If the angle is speculative ("could apply to a bank") or generic
("affects financial services"), skip it. Name a role, a constraint,
or a regulatory hook -- or don't bring it up. Most stories will NOT
carry a finance angle. That is correct.
"""


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def summarise(date: _dt.date | None = None) -> Issue:
    """Take top-N ranked stories, write summaries + Pulse + sections,
    construct and write the ``Issue`` to STAGING.

    Parameters
    ----------
    date
        Issue date (UTC). Defaults to today's UTC date.

    Returns
    -------
    Issue
        The validated issue object (also written to disk as
        ``data/staging/<date>/issue.json``). ``issue_number`` is ``None``;
        it is assigned at release time by ``render.release_promote``.

    Raises
    ------
    RuntimeError
        If a valid ``Issue`` cannot be constructed (e.g. no stories
        survive, or the Pulse cannot be filled). Better to surface than
        write a broken issue.
    """
    run_date = date or _dt.date.today()

    ranked_in = paths.ranked_path(run_date, canonical=False)
    clusters_in = paths.clusters_path(run_date, canonical=False)
    items_in = paths.items_path(run_date, canonical=False)
    issue_out = paths.issue_path(run_date, canonical=False)

    ranked = _load_ranked(ranked_in)
    if not ranked:
        raise RuntimeError(
            f"summarise: no ranked stories at {ranked_in} "
            "-- nothing to publish"
        )

    # Drop the "cut" tier here (rank.py marks below-threshold stories as
    # "cut"; they stay in ranked.jsonl for audit but never reach an Issue).
    ranked = [r for r in ranked if r.tier != "cut"]
    if not ranked:
        raise RuntimeError(
            "summarise: every ranked story was tier='cut' -- no stories "
            "qualify for the issue"
        )

    top = ranked[:TOP_N_STORIES]
    clusters_by_id = _load_clusters_index(clusters_in)
    items_by_id = _load_items_index(items_in)

    # Build callback context for any cluster with a cross_time_ref.
    # Round B: callback lookback reads CANONICAL only -- drafts Arman
    # discarded must not seed callbacks.
    callbacks_by_root = _load_callback_context(
        run_date,
        roots={c.cross_time_ref for c in clusters_by_id.values()
               if c.cross_time_ref},
    )

    # --- Per-story summarisation -----------------------------------------
    blocks: list[tuple[RankedStory, SummaryBlock]] = []
    for story in top:
        cluster = clusters_by_id.get(story.cluster_id)
        if cluster is None:
            _LOG.warning(
                "summarise: cluster %s missing from clusters.jsonl -- "
                "skipping (ranked.jsonl references it)", story.cluster_id,
            )
            continue
        items = _items_for_cluster(cluster, items_by_id)
        callbacks = []
        if cluster.cross_time_ref:
            callbacks = callbacks_by_root.get(cluster.cross_time_ref, [])
        try:
            block = _summarise_one(
                story=story, cluster=cluster, items=items, callbacks=callbacks
            )
        except Exception:  # noqa: BLE001 -- never crash the issue on one bad story
            _LOG.exception(
                "summarise: failed to summarise cluster_id=%s -- skipping",
                story.cluster_id,
            )
            continue
        if block is None:
            continue
        blocks.append((story, block))

    if not blocks:
        raise RuntimeError(
            "summarise: every top-N story failed summarisation -- aborting"
        )

    # --- Audience-tag reconciliation (FM-12, regression #75) ------------
    # The rank LLM sees titles + raw_summary; the per-story summarise LLM
    # sees the article body. When the body-grounded `signal` says "act"
    # (the Big Picture pill -- vendor / contract / architecture decision
    # worth making this quarter), but rank.py undertagged the story as
    # hands_on-only, trust the body-grounded signal and add big_picture.
    # Lets workflow / governance / decision-process shifts surface in the
    # right section even when rank.py missed the senior-leader angle.
    _reconcile_signal_with_audience_tags(blocks)

    # --- Section assembly ------------------------------------------------
    # v0.8: pulse -> big_picture -> hands_on -> on_the_radar.
    # The Big Picture comes first per Arman's reading order.
    pulse_section, big_picture_section, hands_on_section, on_the_radar_section = \
        _assemble_sections(blocks)

    # --- Section intros (Phase B) ---------------------------------------
    # One LLM call per non-pulse section, fed the section's stories so the
    # intro reads the day's pattern. Pulse never carries an intro -- its
    # whole job is to BE the framing. Failures degrade gracefully: the
    # template hides missing intros, the issue still ships.
    _populate_section_intro(big_picture_section)
    _populate_section_intro(hands_on_section)
    _populate_section_intro(on_the_radar_section)

    # --- Construct + validate -------------------------------------------
    # issue_number is intentionally None in staging output. Numbering is a
    # release-time operation; see DESIGN.md "Issue Number Registry" +
    # "Archive: staging vs canonical".
    issue = Issue(
        issue_number=None,
        date=run_date,
        pulse=pulse_section,
        sections=[big_picture_section, hands_on_section, on_the_radar_section],
        generated_at=_dt.datetime.now(_dt.timezone.utc),
        prompt_versions={
            "rank": _read_rank_version(),
            "summarise": SUMMARISE_PROMPT_VERSION,
            "pulse": PULSE_PROMPT_VERSION,
        },
    )

    _write_issue_json(issue_out, issue)

    pulse_headline = issue.pulse.stories[0].headline if issue.pulse.stories else "?"
    _LOG.info(
        "summarised top %d: pulse=%r / big_picture: %d / hands_on: %d / "
        "on_the_radar: %d | issue #(staging -- not yet numbered) -> %s",
        len(blocks), pulse_headline,
        len(big_picture_section.stories),
        len(hands_on_section.stories),
        len(on_the_radar_section.stories),
        issue_out,
    )
    return issue


# ---------------------------------------------------------------------------
# Loaders.
# ---------------------------------------------------------------------------

def _load_ranked(path: Path) -> list[RankedStory]:
    """Read ``ranked.jsonl`` preserving file order (which is score desc per
    rank.py). Tolerates missing file (returns empty)."""
    if not path.exists():
        return []
    out: list[RankedStory] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                out.append(RankedStory.model_validate(payload))
            except Exception:  # noqa: BLE001
                _LOG.exception(
                    "summarise: bad ranked record at %s:%d -- skipping",
                    path, lineno,
                )
                continue
    return out


def _load_clusters_index(path: Path) -> dict[str, Cluster]:
    """Read ``clusters.jsonl`` into ``{cluster_id: Cluster}``."""
    out: dict[str, Cluster] = {}
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                cluster = Cluster.model_validate(payload)
            except Exception:  # noqa: BLE001
                _LOG.exception(
                    "summarise: bad cluster record at %s:%d -- skipping",
                    path, lineno,
                )
                continue
            out[cluster.cluster_id] = cluster
    return out


def _load_items_index(path: Path) -> dict[str, Item]:
    """Read ``items.jsonl`` into ``{Item.id: Item}``."""
    out: dict[str, Item] = {}
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                item = Item.model_validate(payload)
            except Exception:  # noqa: BLE001
                _LOG.exception(
                    "summarise: bad item record at %s:%d -- skipping",
                    path, lineno,
                )
                continue
            out[item.id] = item
    return out


# ---------------------------------------------------------------------------
# Callback context -- past issues that featured this chain.
# ---------------------------------------------------------------------------

@dataclass
class _CallbackRef:
    """One prior appearance of a chain. Used as LLM context only, not
    persisted -- callback framing happens in prose inside the summary."""
    issue_date: _dt.date
    issue_number: int
    headline: str
    direction_note: str
    summary_excerpt: str


def _load_callback_context(
    run_date: _dt.date, roots: set[str]
) -> dict[str, list[_CallbackRef]]:
    """Walk the last ``CALLBACK_LOOKBACK_DAYS`` days of CANONICAL
    ``issue.json``; return ``{chain_root_cluster_id: [latest..oldest
    CallbackRef]}``, capped at ``MAX_CALLBACK_REFERENCES`` per root.

    Round B: canonical-only read (`data/<date>/issue.json`). Drafts Arman
    discarded must not seed callbacks. Staging is invisible to this
    lookback. Tolerates missing days, missing files, and legacy issues
    (which may have ``issue_number = None`` or absent).
    """
    if not roots:
        return {}
    out: dict[str, list[_CallbackRef]] = {root: [] for root in roots}
    for delta in range(1, CALLBACK_LOOKBACK_DAYS + 1):
        day = run_date - _dt.timedelta(days=delta)
        canonical_issue = paths.issue_path(day, canonical=True)
        if not canonical_issue.exists():
            continue
        try:
            payload = json.loads(canonical_issue.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _LOG.warning(
                "summarise: could not read past issue %s -- skipping for "
                "callbacks", canonical_issue,
            )
            continue
        issue_number = int(payload.get("issue_number") or 0)
        # Pulse block + every section's stories carry cross_time_ref.
        for block in _iter_blocks(payload):
            ref = block.get("cross_time_ref")
            if not ref or ref not in out:
                continue
            if len(out[ref]) >= MAX_CALLBACK_REFERENCES:
                continue
            summary = (block.get("summary") or "").strip()
            if len(summary) > 280:
                summary = summary[:280].rstrip() + "..."
            out[ref].append(_CallbackRef(
                issue_date=day,
                issue_number=issue_number,
                headline=(block.get("headline") or "").strip(),
                direction_note=(block.get("direction_note") or "").strip(),
                summary_excerpt=summary,
            ))
    return out


def _iter_blocks(issue_payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield every ``SummaryBlock``-shaped dict in a past issue payload,
    across both ``pulse`` and ``sections``. Defensive: tolerates absent
    fields rather than KeyError-ing on v1 / partial archives."""
    pulse = issue_payload.get("pulse") or {}
    for block in (pulse.get("stories") or []):
        if isinstance(block, dict):
            yield block
    for section in (issue_payload.get("sections") or []):
        if not isinstance(section, dict):
            continue
        for block in (section.get("stories") or []):
            if isinstance(block, dict):
                yield block


# ---------------------------------------------------------------------------
# Per-story summarisation.
# ---------------------------------------------------------------------------

@dataclass
class _SummaryDraft:
    """Intermediate shape parsed from the LLM JSON, before pydantic. Keeps
    parser logic and constructor logic clean. v0.2: direction_note and
    finance_angle no longer separate fields -- both live in summary prose.
    v0.9 (Phase B): adds ``signal`` (editorial verdict pill)."""
    headline: str
    summary: str
    signal: str | None = None


def _summarise_one(
    story: RankedStory,
    cluster: Cluster,
    items: list[Item],
    callbacks: list[_CallbackRef],
) -> SummaryBlock | None:
    """One LLM call. Returns a validated ``SummaryBlock`` or ``None`` if
    the call / parse / validation failed after the retry budget."""
    temperature = float(os.getenv("LLM_TEMPERATURE_SUMMARISE", "0.6"))

    # v0.4: fetch the article body for up to the top-3 items so the LLM
    # sees real source text instead of an empty raw_summary. Closes the
    # single biggest quality gap (vague / invented numbers / missing trust
    # flags). Lazy per-top-N -- bodies are NOT persisted to items.jsonl.
    excerpts: dict[str, str] = {}
    for it in items[:3]:
        url = str(it.url)
        excerpts[url] = _fetch_source_excerpt(url)

    prompt = _build_summary_prompt(story, cluster, items, callbacks, excerpts)

    draft = _call_and_parse_summary(prompt, temperature, cluster.cluster_id)
    if draft is None:
        return None

    source_urls = _pick_source_urls(items, k=3)
    if not source_urls:
        _LOG.warning(
            "summarise: cluster %s has no resolvable source URLs -- "
            "skipping (SummaryBlock requires at least one)",
            cluster.cluster_id,
        )
        return None

    try:
        block = SummaryBlock(
            story_id=cluster.cluster_id,
            headline=draft.headline,
            summary=draft.summary,
            source_urls=source_urls,  # type: ignore[arg-type]
            cross_time_ref=cluster.cross_time_ref,
            signal=draft.signal,  # type: ignore[arg-type]
        )
    except Exception:  # noqa: BLE001
        _LOG.exception(
            "summarise: SummaryBlock validation failed for cluster_id=%s -- "
            "skipping. draft=%s",
            cluster.cluster_id, draft,
        )
        return None
    return block


_SOURCE_EXCERPT_CACHE: dict[str, str] = {}
"""Per-process cache: URL -> extracted body. A run never refetches the same
URL twice (rare across clusters but possible). Trafilatura extraction is
~50-300ms per page; cache hits are free."""

_SOURCE_EXCERPT_TIMEOUT_S = 12.0
"""Per-fetch hard timeout. Whole-issue source-fetch budget is ~12 items x
this = ~144s worst case, usually 20-40s in practice."""

_SOURCE_EXCERPT_MAX_WORDS = 500
"""Soft cap on the excerpt the prompt sees. Beyond this, the LLM doesn't
get more signal -- it gets more tokens to attend to."""


def _fetch_source_excerpt(url: str) -> str:
    """Fetch ``url`` and extract the main article body via trafilatura.

    Returns ~150-500 words of clean text on success, empty string on any
    failure (the prompt's honesty rule will then have the LLM say "source
    body not retrievable" instead of inventing).

    Cached per-process (per-run) -- same URL returns the same excerpt.
    """
    if url in _SOURCE_EXCERPT_CACHE:
        return _SOURCE_EXCERPT_CACHE[url]
    try:
        import httpx
        import trafilatura
        with httpx.Client(
            timeout=_SOURCE_EXCERPT_TIMEOUT_S,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "AI-Vector/0.1 (https://github.com/armanabrahamyan/"
                    "ai-vector; daily-newsletter)"
                ),
                # Some publishers serve different markup to bots vs browsers;
                # asking for HTML explicitly avoids JSON / RSS surprises.
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            },
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            # trafilatura.extract returns None when extraction fails outright.
            extracted = trafilatura.extract(
                resp.text,
                include_comments=False,
                include_tables=False,
                favor_recall=False,  # prefer precision -- noise hurts the LLM
            )
            text = (extracted or "").strip()
    except Exception as exc:  # noqa: BLE001 -- summarise tolerates excerpt loss
        _LOG.warning(
            "summarise: source excerpt fetch failed for %s: %s: %s",
            url, type(exc).__name__, exc,
        )
        text = ""
    if text:
        words = text.split()
        if len(words) > _SOURCE_EXCERPT_MAX_WORDS:
            text = " ".join(words[:_SOURCE_EXCERPT_MAX_WORDS]) + " ..."
    _SOURCE_EXCERPT_CACHE[url] = text
    return text


def _build_summary_prompt(
    story: RankedStory,
    cluster: Cluster,
    items: list[Item],
    callbacks: list[_CallbackRef],
    excerpts: dict[str, str] | None = None,
) -> str:
    """Assemble the per-story summarisation prompt with voice + skills
    inlined and callback context attached when present.

    ``excerpts`` maps item URL -> source body text (fetched lazily by
    ``_summarise_one`` for the top items in the cluster). When provided,
    each item line carries a ``source_excerpt`` block so the LLM writes
    from real source text instead of an empty raw_summary.
    """
    excerpts = excerpts or {}
    item_lines: list[str] = []
    for it in items[:5]:  # a bit more context than the rank prompt
        title = it.title.strip()
        summary = (it.raw_summary or "").strip()
        if len(summary) > 800:
            summary = summary[:800].rstrip() + "..."
        url_str = str(it.url)
        excerpt = (excerpts.get(url_str) or "").strip()
        if excerpt:
            # Indent for readability inside the prompt; LLMs handle this fine.
            indented = "\n".join(f"    {line}" for line in excerpt.splitlines())
            excerpt_block = f"  source_excerpt: |\n{indented}"
        else:
            excerpt_block = (
                "  source_excerpt: (not retrievable -- source-body fetch "
                "failed or returned empty; write only from title + summary "
                "and SAY what's unknown)"
            )
        item_lines.append(
            f"- [{it.source}, trust={it.trust_weight}] {title}\n"
            f"  url: {it.url}\n"
            f"  summary: {summary}\n"
            f"{excerpt_block}"
        )
    items_block = "\n".join(item_lines) or "  (no items resolved)"

    callback_block = ""
    if callbacks:
        cb_lines = ["CALLBACK CONTEXT -- past appearances of this story chain:"]
        for cb in callbacks:
            num_part = f"issue #{cb.issue_number}" if cb.issue_number else "earlier"
            cb_lines.append(
                f"  - {cb.issue_date.isoformat()} ({num_part}): "
                f"headline={cb.headline!r}\n"
                f"    direction_note={cb.direction_note!r}\n"
                f"    summary_excerpt={cb.summary_excerpt!r}"
            )
        cb_lines.append(
            "If today's piece is a meaningful update on what we previously "
            "flagged, consider a brief callback (\"Last week we flagged X; "
            "today's update is...\"). Don't force it. If the past coverage "
            "and today's update don't connect tightly, skip the callback."
        )
        callback_block = "\n".join(cb_lines) + "\n\n"

    rationale = (story.rationale or "").strip()
    breakdown_str = ", ".join(
        f"{k}:{v}" for k, v in story.breakdown.items()
    )

    return f"""\
You are writing one story for AI Vector -- a daily newsletter about
Agentic AI and Generative AI. The cluster was already RANKED and
selected for the issue; your job is to write it well.

{_VOICE_BLOCK}
{_EDITORIAL_FOCUS_BLOCK}
{_FINANCE_LENS_BLOCK}
RANKER NOTES (from the rank stage, for context only -- not for echoing):
  score: {story.score} / 100
  breakdown: {breakdown_str}
  audience_tags: {list(story.audience_tags)}
  rationale: {rationale}

CLUSTER
  cluster_id: {cluster.cluster_id}
  canonical_title: {cluster.canonical_title}
  sources: {list(cluster.sources)}
  earliest_published: {cluster.earliest_published.isoformat()}
  is_continuation: {"yes (chain root=" + cluster.cross_time_ref + ")" if cluster.cross_time_ref else "no"}

ITEMS:
{items_block}

{callback_block}INSTRUCTIONS
- HEADLINE: follow the HEADLINE rules above. Lead with the consequence
  or action, not the name. HARD CAPS: maximum 12 words AND maximum 90
  characters. Both are enforced -- a headline that exceeds either is
  rejected and you will be asked to rewrite. COUNT the words AND
  characters before returning. Model names and version numbers belong
  in the BODY, not the title.
- BODY: 30 to 60 words HARD CAP. 61 words is a fail. The Pulse is held
  to the SAME cap (60 words). Count before returning. SHAPE: shift ->
  shipped -> judgement-tied-to-decision. Must include: one concrete
  number or mechanism; a trust flag if warranted (vendor benchmark?
  no code? thin sourcing?); a close tied to a SPECIFIC decision, not
  a department or group. If you cannot fit all three in 60 words,
  cut a clause or sharpen a verb -- the cap holds.
- LANGUAGE: plain English. No acronyms a non-specialist wouldn't
  recognise (spell out, replace, or drop). No spec-sheet stacking:
  ONE news number, the rest replaced with their consequence. Model
  names and versions live in the body, never the title.
- PUNCTUATION: NO em-dashes. Do NOT use "--" or "—" anywhere in the
  headline or body. Use commas, parentheses, semicolons, or full stops
  instead. Regular hyphens in compound words are fine.
- HONESTY: use ONLY facts present in source_excerpt (or the title /
  summary / cluster metadata if the excerpt is missing). If a number,
  licence, or artefact (weights / code / demo) is NOT stated in the
  source, do NOT assert it. Say what is genuinely unknown ("benchmarks
  not yet published", "licence not specified") rather than inventing.
- Direction and finance lens live in the prose -- NEVER labels.
- If callback context is present and the connection is tight, weave a
  brief reference in ("last week we flagged X; today's update is..."). If
  the connection is weak, skip it.
- Australian English throughout.
- Link out; never reproduce full articles.
- SIGNAL: pick ONE verdict pill that captures what the reader should DO:
    * "act"     -- vendor / contract / architecture decision worth making
                   this quarter. Typical for Big Picture stories with a
                   nameable prioritisation change.
    * "try"     -- drop into a sandbox this week. Typical for Hands-On
                   tools / repos / techniques you can clone or pip-install.
    * "read"    -- absorb the framing; no clear action yet. Use sparingly.
    * "watch"   -- too thin / too early to act on; monitor for follow-up.
                   Default for On the Radar items.
    * "discuss" -- design concept worth raising at a review, not yet
                   shippable. Right call for single-source frameworks
                   without code / benchmarks.
  Choose by what the body actually argues. If the body says "raise this
  at your next architecture review", that's "discuss", not "act".

Return ONLY a single JSON object (no markdown fences, no commentary):

{{
  "headline": "<consequence-led headline, HARD <= 90 chars AND <= 12 words>",
  "summary": "<30-60 word body, HARD 60-word cap (same for the Pulse)>",
  "signal": "<one of: act | try | read | watch | discuss>"
}}
"""


# Length caps -- mirrored from the prompt + the judge rubric in
# evals/judge/prompts/headline.yaml and summary.yaml. Single source of
# truth for the post-LLM enforcement check below.
_HEADLINE_MAX_WORDS = 12
_HEADLINE_MAX_CHARS = 90
_BODY_MIN_WORDS = 30
_BODY_MAX_WORDS = 60


def _length_violations(draft: _SummaryDraft) -> list[str]:
    """Return a list of human-readable length-cap violations against the
    HARD caps stated in the prompt. Empty list means the draft is within
    spec. Used by ``_call_and_parse_summary`` to trigger a single corrective
    retry (tasks #73 + #74)."""
    issues: list[str] = []
    hw = len(draft.headline.split())
    hc = len(draft.headline)
    bw = len(draft.summary.split())
    if hw > _HEADLINE_MAX_WORDS:
        issues.append(
            f"headline is {hw} words (HARD cap is {_HEADLINE_MAX_WORDS})"
        )
    if hc > _HEADLINE_MAX_CHARS:
        issues.append(
            f"headline is {hc} characters (HARD cap is {_HEADLINE_MAX_CHARS})"
        )
    if bw > _BODY_MAX_WORDS:
        issues.append(
            f"summary body is {bw} words (HARD cap is {_BODY_MAX_WORDS}); "
            "the Pulse is held to the same cap"
        )
    if bw < _BODY_MIN_WORDS:
        issues.append(
            f"summary body is {bw} words (minimum is {_BODY_MIN_WORDS})"
        )
    return issues


def _call_and_parse_summary(
    prompt: str, temperature: float, cluster_id: str
) -> _SummaryDraft | None:
    """LLM call + retry on parse failure (one retry, mirrors rank.py) +
    a separate single retry on length-cap violation (tasks #73 + #74).

    Order of operations:
      1. Call the LLM. If JSON parse fails, retry once with a corrective
         prompt; if it fails again, return None (story is dropped).
      2. With a valid draft in hand, check length caps. If any are
         breached, retry ONCE with a corrective prompt that quotes the
         specific overruns. If the second attempt still breaches, KEEP
         the draft (log a warning) -- we'd rather ship a marginally-
         overlong headline than lose a top-N story.
    """
    attempts = JSON_RETRY_BUDGET + 1
    current_prompt = prompt
    draft: _SummaryDraft | None = None
    for attempt in range(1, attempts + 1):
        try:
            raw = _llm_call(current_prompt, temperature=temperature, max_tokens=1600)
        except Exception:  # noqa: BLE001
            _LOG.exception(
                "summarise: LLM call failed for cluster_id=%s (attempt %d/%d)",
                cluster_id, attempt, attempts,
            )
            return None
        draft = _parse_summary_json(raw)
        if draft is not None:
            break
        _LOG.warning(
            "summarise: JSON parse failed for cluster_id=%s (attempt %d/%d)",
            cluster_id, attempt, attempts,
        )
        if attempt < attempts:
            current_prompt = (
                "Your previous response was not valid JSON matching the "
                "schema below. Return JSON ONLY (no markdown fences, no "
                "prose) matching the schema. Original request follows.\n\n"
                + prompt
            )
    if draft is None:
        return None

    # --- Length-cap enforcement (single corrective retry) ---------------
    violations = _length_violations(draft)
    if not violations:
        return draft

    _LOG.info(
        "summarise: length cap breached for cluster_id=%s on first pass: %s -- "
        "requesting one corrective regenerate",
        cluster_id, "; ".join(violations),
    )
    corrective = (
        "Your previous response BREACHED the HARD length caps. The "
        "following violations were found:\n\n"
        + "\n".join(f"  - {v}" for v in violations)
        + "\n\nRewrite the JSON so that:\n"
        f"  - headline is AT MOST {_HEADLINE_MAX_WORDS} words AND AT MOST "
        f"{_HEADLINE_MAX_CHARS} characters\n"
        f"  - summary is BETWEEN {_BODY_MIN_WORDS} AND {_BODY_MAX_WORDS} "
        "words (the Pulse is held to the same cap)\n\n"
        "COUNT THE WORDS AND CHARACTERS before returning. Keep the same "
        "facts, tone, trust flag, and decision-tied close; just tighten "
        "the language. Cut adjectives, hedges, and spec-sheet detail "
        "first. Return ONLY JSON, no markdown fences, no commentary. "
        "Original request follows.\n\n"
        + prompt
    )
    try:
        raw = _llm_call(corrective, temperature=temperature, max_tokens=1600)
    except Exception:  # noqa: BLE001
        _LOG.warning(
            "summarise: corrective LLM call failed for cluster_id=%s -- "
            "keeping first-pass draft (still over cap)", cluster_id,
        )
        return draft
    retried = _parse_summary_json(raw)
    if retried is None:
        _LOG.warning(
            "summarise: corrective response failed to parse for cluster_id=%s "
            "-- keeping first-pass draft (still over cap)", cluster_id,
        )
        return draft
    new_violations = _length_violations(retried)
    if new_violations:
        _LOG.warning(
            "summarise: cluster_id=%s STILL over cap after corrective retry: "
            "%s -- keeping the tighter of the two drafts (this is a soft "
            "fail; the issue ships but the judge will flag it)",
            cluster_id, "; ".join(new_violations),
        )
        # Prefer the retried draft if it's strictly tighter than the
        # original on at least one axis and no worse on the others; otherwise
        # keep the first draft. Cheap, deterministic.
        if (
            len(retried.headline) <= len(draft.headline)
            and len(retried.headline.split()) <= len(draft.headline.split())
            and len(retried.summary.split()) <= len(draft.summary.split())
        ):
            return retried
        return draft
    _LOG.info(
        "summarise: corrective retry brought cluster_id=%s within caps "
        "(headline=%dw/%dc, body=%dw)",
        cluster_id,
        len(retried.headline.split()), len(retried.headline),
        len(retried.summary.split()),
    )
    return retried


def _parse_summary_json(raw: str) -> _SummaryDraft | None:
    """Parse the summary LLM output. Defers detailed validation (lengths,
    types) to pydantic via ``SummaryBlock`` -- here we only check the
    structural shape.

    v0.2: only ``headline`` and ``summary`` are required. Any stray
    ``direction_note`` / ``finance_angle`` keys the LLM emits are ignored
    -- the model is told not to produce them, but tolerance avoids retries
    if the LLM falls back to old habits."""
    payload = _extract_json_object(raw)
    if payload is None:
        return None
    try:
        headline = payload["headline"]
        summary = payload["summary"]
    except (KeyError, TypeError):
        return None
    if not isinstance(headline, str) or not headline.strip():
        return None
    if not isinstance(summary, str) or not summary.strip():
        return None
    # Signal is optional in the parsed shape; pydantic enforces the Literal
    # set when SummaryBlock is constructed. Garbage values get dropped here.
    signal_raw = payload.get("signal")
    signal: str | None = None
    if isinstance(signal_raw, str):
        candidate = signal_raw.strip().lower()
        if candidate in {"act", "try", "read", "watch", "discuss"}:
            signal = candidate
    return _SummaryDraft(
        headline=headline.strip(),
        summary=summary.strip(),
        signal=signal,
    )


def _items_for_cluster(
    cluster: Cluster, items_by_id: dict[str, Item]
) -> list[Item]:
    """Resolve a cluster's members from the items index, preserving the
    cluster's declared item order (the cluster writer picks first-seen)."""
    out: list[Item] = []
    for item_id in cluster.item_ids:
        it = items_by_id.get(item_id)
        if it is not None:
            out.append(it)
    return out


_REDDIT_SLUG_RE = re.compile(
    r"^https?://(?:www\.|old\.|new\.)?reddit\.com/r/[^/]+/comments/[^/]+/([^/?#]+)",
    re.IGNORECASE,
)


def _url_dedup_key(url: str) -> str:
    """Compute a semantic dedup key for source URLs.

    For Reddit URLs, two cross-posts of the same content live at different
    URLs (different subreddits, different comment IDs) but share the same
    URL slug. We dedup on that slug so a story doesn't render two "[1] [2]"
    links pointing at the same discussion.

    For everything else the key is the raw URL string -- no semantic
    grouping, just string identity.
    """
    m = _REDDIT_SLUG_RE.match(url)
    if m:
        return f"reddit::{m.group(1).lower()}"
    return url


def _pick_source_urls(items: list[Item], k: int) -> list[str]:
    """Top-k unique URLs from cluster members, sorted by trust_weight
    (then by recency as a tiebreaker). Deterministic given the inputs.

    Two-pass dedup, in this order:

    1. **Reddit cross-post slug** (existing). Two subreddit URLs to the
       same article slug collapse to one URL; the higher-trust subreddit
       wins by sort order.

    2. **Canonical-ID collapse** (task #84). Items can legitimately end
       up in one cluster while pointing at the same stable artefact via
       different feed URLs (e.g. an arxiv paper cross-posted to HF Daily
       Papers AND linked in a Reddit thread; rule A in
       ``cluster._apply_canonical_id_rules`` force-groups them). After
       Reddit-slug dedup, group remaining URLs by canonical ID
       (arxiv:<abs>, github_release:<repo>:<tag>, doi:<id>) and keep ONE
       per group. The higher-trust source wins (first-seen breaks ties);
       this mirrors precedence above and stays deterministic.

       URLs with ``canonical_id == None`` (free-text blogs, news, plain
       Reddit threads without canonical links) pass through unchanged --
       only stable-ID URLs are collapsed.

    The narrowed scope here exists because cluster.py rule B (different
    canonical IDs forbidden from merging) eliminates the over-collapse
    failure mode that would have required deeper changes. What remains
    is the cosmetic redundancy of two URLs that resolve to the same
    paper showing up side-by-side in the rendered HTML.
    """
    sorted_items = sorted(
        items,
        key=lambda it: (it.trust_weight, it.published_at),
        reverse=True,
    )

    # --- Pass 1: existing Reddit-slug + exact-URL dedup ----------------
    seen: set[str] = set()
    pass1: list[str] = []
    for it in sorted_items:
        url = str(it.url)
        key = _url_dedup_key(url)
        if key in seen:
            continue
        seen.add(key)
        pass1.append(url)

    # --- Pass 2: canonical-ID collapse (#84) ---------------------------
    # For each URL, derive its canonical ID. URLs with None canonical ID
    # are untouched. URLs sharing a canonical ID collapse to the first
    # one seen in pass1 (which is already trust-sorted, so the highest-
    # trust source wins).
    seen_canonical: set[str] = set()
    out: list[str] = []
    for url in pass1:
        cid = _extract_canonical_id_from_url(url)
        if cid is not None:
            if cid in seen_canonical:
                continue
            seen_canonical.add(cid)
        out.append(url)
        if len(out) >= k:
            break
    return out


# ---------------------------------------------------------------------------
# Audience-tag reconciliation (FM-12, regression #75).
# ---------------------------------------------------------------------------

def _reconcile_signal_with_audience_tags(
    blocks: list[tuple[RankedStory, SummaryBlock]],
) -> None:
    """Deterministic cross-check that runs AFTER the per-story summarise
    LLM and BEFORE section routing.

    Rule: if a story's body-grounded ``signal == "act"`` -- the editorial
    verdict pill defined as "vendor / contract / architecture decision
    worth making this quarter, typical for Big Picture stories" -- but
    rank.py did not tag it ``big_picture``, add the tag.

    Rationale. Two LLM stages can disagree. The rank call sees titles +
    short raw_summary; the summarise call sees the article body. The
    body-grounded signal is the more reliable senior-leader-relevance
    cue. Trusting it here closes the gap where rank.py undertagged
    workflow/governance/decision-process shifts as ``hands_on`` only.

    Mutates ``story.audience_tags`` in place. Logs every augmentation so
    operators can spot when the rule fires often (a signal that the rank
    prompt itself needs another revision).
    """
    for story, block in blocks:
        if block.signal != "act":
            continue
        tags = list(story.audience_tags)
        if "big_picture" in tags:
            continue
        tags.append("big_picture")
        story.audience_tags = tags  # type: ignore[assignment]
        _LOG.info(
            "signal=act forced big_picture tag for %s "
            "(rank-side tags were %s; FM-12 cross-check)",
            story.cluster_id, list(story.audience_tags),
        )


# ---------------------------------------------------------------------------
# Section assembly.
# ---------------------------------------------------------------------------

def _assemble_sections(
    blocks: list[tuple[RankedStory, SummaryBlock]],
) -> tuple[IssueSection, IssueSection, IssueSection, IssueSection]:
    """Place every summarised story into exactly one section. Returns the
    four sections in display order: pulse, big_picture, hands_on, on_the_radar.

    Editorial routing rules (v0.8 -- 2026-05-24 section rename):
      - Pulse: highest-scoring story that hits >= 2 signal-filter dimensions
        (significance, hands_on_utility, freshness_momentum >= 70). Fallback
        (logged): highest breakdown.significance.
      - The Big Picture: stories tagged `big_picture`. Hard cap at 4.
        First, per Arman's reading order.
      - Hands-On: stories tagged `hands_on`, OR tagged `general` with
        hands_on_utility >= 70. Hard cap at 5.
      - On the Radar: everything left, in score-desc order.

    Direction notes and finance angles are embedded in summary prose,
    not separate fields (schema v4); the assembler no longer filters on
    direction_note presence.
    """
    # blocks already arrive in score-desc order (ranked.jsonl order,
    # preserved by the loop above).
    by_id = {story.cluster_id: (story, block) for story, block in blocks}
    unplaced = set(by_id.keys())

    # --- Pulse ----------------------------------------------------------
    pulse_id = _pick_pulse(blocks)
    if pulse_id is None:
        raise RuntimeError(
            "summarise: cannot select a Pulse story -- no surviving stories."
        )
    unplaced.discard(pulse_id)

    # --- The Big Picture (first per Arman's reading order) --------------
    big_picture_ids = _pick_big_picture(blocks, unplaced)
    for cid in big_picture_ids:
        unplaced.discard(cid)

    # --- Hands-On -------------------------------------------------------
    hands_on_ids = _pick_hands_on(blocks, unplaced)
    for cid in hands_on_ids:
        unplaced.discard(cid)

    # --- On the Radar ---------------------------------------------------
    on_the_radar_ids = [
        story.cluster_id for story, _ in blocks if story.cluster_id in unplaced
    ]

    pulse_section = IssueSection(
        name="pulse",
        stories=[by_id[pulse_id][1]],
    )
    big_picture_section = IssueSection(
        name="big_picture",
        stories=[by_id[cid][1] for cid in big_picture_ids],
    )
    hands_on_section = IssueSection(
        name="hands_on",
        stories=[by_id[cid][1] for cid in hands_on_ids],
    )
    on_the_radar_section = IssueSection(
        name="on_the_radar",
        stories=[by_id[cid][1] for cid in on_the_radar_ids],
    )
    return (pulse_section, big_picture_section, hands_on_section, on_the_radar_section)


def _signal_dimensions_hit(story: RankedStory) -> int:
    """Approximate "signal-filter dimensions hit" from the rank breakdown.
    The editorial-focus skill names three dimensions: today / tomorrow /
    practical. Mapping (best-effort, documented here):
      - today      ~ freshness_momentum >= 70
      - tomorrow   ~ significance       >= 70
      - practical  ~ hands_on_utility   >= 70
    Counts how many of those clear the 70 anchor."""
    b = story.breakdown
    hits = 0
    if b.get("freshness_momentum", 0) >= 70:
        hits += 1
    if b.get("significance", 0) >= 70:
        hits += 1
    if b.get("hands_on_utility", 0) >= 70:
        hits += 1
    return hits


def _pick_pulse(
    blocks: list[tuple[RankedStory, SummaryBlock]],
) -> str | None:
    """The Pulse selection rule (v0.3 -- task #82: bias against continuations).

    Selection order:

      1. **Prefer non-continuation for Pulse.** A continuation (the
         SummaryBlock carries a non-null ``cross_time_ref``) is a follow-up
         to a story we covered on a previous day. The Pulse is meant to be
         the day's freshest editorial anchor; leading with a continuation
         tells the reader "we have nothing new today". So: among surviving
         blocks, any FRESH (cross_time_ref is None) story beats any
         continuation regardless of score.

      2. Within the chosen pool (FRESH if any exist; else continuations),
         prefer stories that hit >= 2 signal-filter dimensions
         (significance, hands_on_utility, freshness_momentum >= 70). This
         is the Pulse-class quality bar inherited from v0.2.

      3. Within the chosen pool, prefer the highest score (blocks arrive in
         score-desc order; the sort below preserves that as the tiebreaker).

    Degraded mode. If ALL surviving stories are continuations, we still
    have to fill ``Issue.pulse`` (the model requires exactly 1 story). We
    pick the best continuation and log a WARNING -- the operator sees the
    issue is light on fresh signal. ``Issue.pulse=None`` is not allowed by
    the schema, so we ship with the smell loud rather than crash.

    Returns None only if ``blocks`` is empty (caller aborts).
    """
    if not blocks:
        return None

    # Partition by continuation status. cross_time_ref lives on the
    # SummaryBlock (mirrored from Cluster at construction time in
    # _summarise_one). This is the deterministic seam -- no LLM, no prompt.
    fresh = [(s, b) for s, b in blocks if b.cross_time_ref is None]
    continuations = [(s, b) for s, b in blocks if b.cross_time_ref is not None]

    pool: list[tuple[RankedStory, SummaryBlock]]
    if fresh:
        pool = fresh
    else:
        # Degraded mode: every surviving story is a follow-up. Still must
        # pick one (Issue.pulse requires exactly 1 block).
        _LOG.warning(
            "summarise: NO FRESH SIGNAL FOR PULSE -- every surviving story "
            "is a continuation (carries cross_time_ref). Using best "
            "continuation as Pulse and shipping the smell. Consider whether "
            "today's issue should ship at all."
        )
        pool = continuations

    # Within the pool: Pulse-class quality bar first, then score order.
    pulse_class = [sb for sb in pool if _signal_dimensions_hit(sb[0]) >= 2]
    if pulse_class:
        chosen = pulse_class[0]  # pool preserves score-desc order
    else:
        # Fallback within the pool: highest breakdown.significance, then score.
        chosen = max(
            pool,
            key=lambda sb: (sb[0].breakdown.get("significance", 0), sb[0].score),
        )
        if fresh:
            _LOG.warning(
                "summarise: no Pulse-class FRESH story today (none hit >= 2 "
                "signal dimensions); using top-significance fresh fallback"
            )

    # Operator visibility: log when we demoted a higher-scored continuation
    # for a lower-scored fresh story. This is the rule firing.
    if fresh and continuations:
        top_continuation = continuations[0][0]  # blocks were score-desc
        if top_continuation.score > chosen[0].score:
            _LOG.info(
                "summarise: Pulse non-continuation bias fired -- demoted "
                "continuation %s (score=%d) in favour of fresh story %s "
                "(score=%d). #82.",
                top_continuation.cluster_id, top_continuation.score,
                chosen[0].cluster_id, chosen[0].score,
            )

    return chosen[0].cluster_id


def _pick_big_picture(
    blocks: list[tuple[RankedStory, SummaryBlock]],
    available: set[str],
) -> list[str]:
    """Tagged 'big_picture' and not yet placed. Hard cap at 4. The Big
    Picture is the first section after Pulse, per editorial direction."""
    out: list[str] = []
    for story, _block in blocks:
        if story.cluster_id not in available:
            continue
        if "big_picture" in set(story.audience_tags):
            out.append(story.cluster_id)
        if len(out) >= 4:
            break
    return out


def _pick_hands_on(
    blocks: list[tuple[RankedStory, SummaryBlock]],
    available: set[str],
) -> list[str]:
    """Tagged 'hands_on', OR tagged 'general' with hands_on_utility >= 70.
    Hard cap at 5."""
    out: list[str] = []
    for story, _block in blocks:
        if story.cluster_id not in available:
            continue
        tags = set(story.audience_tags)
        if "hands_on" in tags or (
            "general" in tags and story.breakdown.get("hands_on_utility", 0) >= 70
        ):
            out.append(story.cluster_id)
        if len(out) >= 5:
            break
    return out


# ---------------------------------------------------------------------------
# Section intros (Phase B).
#
# One LLM call per non-pulse section, fed the section's already-written
# stories so the intro reads the day's pattern rather than restating it.
# Failures degrade gracefully -- the section's intro_lead / intro_body
# stay None and the template hides the block.
# ---------------------------------------------------------------------------

_SECTION_INTRO_HINTS: dict[str, str] = {
    "big_picture": (
        "Senior-leader framing: strategic shifts, vendor calculus, risk, "
        "governance, regulation. The lead phrase should orient (\"What to "
        "watch.\" / \"Decisions to weigh.\"); the body reads the PATTERN "
        "across these stories in one or two sentences."
    ),
    "hands_on": (
        "Practitioner framing: tools, repos, benchmarks, techniques. The "
        "lead phrase should orient (\"Bench before you budget.\" / "
        "\"Sandbox tonight.\"); the body reads the PATTERN -- are the day's "
        "wins single-source benchmarks? Drop-in releases? Capability "
        "shifts? -- in one or two sentences."
    ),
    "on_the_radar": (
        "Awareness-only framing: items thin on sourcing or early in "
        "trajectory. The lead phrase should signal posture (\"For "
        "awareness only.\" / \"Worth a glance.\"); the body explains "
        "WHY these sit here rather than higher up, in one short sentence."
    ),
}


def _populate_section_intro(section: IssueSection) -> None:
    """Generate {intro_lead, intro_body} for a section via one LLM call.
    Mutates the section in place. Silent on failure -- the rendered
    issue still ships without an intro for the affected section."""
    if not section.stories:
        return
    hint = _SECTION_INTRO_HINTS.get(section.name)
    if hint is None:
        return
    temperature = float(os.getenv("LLM_TEMPERATURE_SUMMARISE", "0.6"))

    story_lines: list[str] = []
    for st in section.stories:
        body = st.summary if len(st.summary) <= 280 else st.summary[:280] + "..."
        story_lines.append(f"- HEADLINE: {st.headline}\n  BODY: {body}")
    stories_block = "\n".join(story_lines)

    prompt = f"""\
You are writing the section intro for the "{section.name}" section of
today's AI Vector issue -- a daily AI newsletter with a financial-services
lens, McKinsey-tagline voice, plain English, no em-dashes.

SECTION CONTEXT
{hint}

STORIES IN THIS SECTION
{stories_block}

INSTRUCTIONS
- LEAD: a tight bold phrase (2-5 words, full-stop at the end). It IS
  the section's editorial posture for today.
- BODY: one or two sentences, 20-35 words total, that reads the DAY'S
  PATTERN across these stories. What does the editor want the reader
  to notice? Frame, don't restate. Don't list. Don't reference specific
  headlines verbatim.
- VOICE: McKinsey tagline (insight, not topic). Plain language.
  Australian English. No em-dashes (use comma, parenthesis, semicolon,
  full stop). No jargon a non-specialist couldn't parse.
- HONESTY: if the section's pattern is "these are all single-source
  benchmarks", say so. Don't oversell weak signal.

Return ONLY a single JSON object (no markdown fences, no commentary):

{{
  "lead": "<2-5 words, full-stop at end>",
  "body": "<20-35 word framing sentence(s)>"
}}
"""
    try:
        raw = _llm_call(prompt, temperature=temperature, max_tokens=400)
    except Exception:  # noqa: BLE001
        _LOG.warning(
            "summarise: section-intro LLM call failed for %s -- skipping intro",
            section.name,
        )
        return
    payload = _extract_json_object(raw)
    if not isinstance(payload, dict):
        _LOG.warning(
            "summarise: section-intro JSON parse failed for %s -- skipping",
            section.name,
        )
        return
    lead = payload.get("lead")
    body = payload.get("body")
    if not isinstance(lead, str) or not isinstance(body, str):
        return
    lead = lead.strip()
    body = body.strip()
    if not lead or not body:
        return
    try:
        section.intro_lead = lead
        section.intro_body = body
    except Exception:  # noqa: BLE001 -- length validators may fire
        _LOG.warning(
            "summarise: section-intro validation failed for %s "
            "(lead=%r, body=%r)",
            section.name, lead[:80], body[:80],
        )


# ---------------------------------------------------------------------------
# Writing.
#
# Round B note: `issue_number` is intentionally NOT derived here -- it is a
# release-time operation (see `src/render.py:release_promote` and DESIGN.md
# "Issue Number Registry"). Staging writes always carry
# `Issue.issue_number = None`.
# ---------------------------------------------------------------------------

def _write_issue_json(path: Path, issue: Issue) -> None:
    """Atomic write of ``issue.json`` (NOT JSONL -- DESIGN.md is explicit:
    a single ``Issue`` object as JSON). Re-uses rank.py's atomic-JSONL
    writer pattern with a single-record wrapper."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.loads(issue.model_dump_json())
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    # Suppress an unused-import warning -- _atomic_write_jsonl is intended
    # for re-use by anyone else who needs to write per-line in this module
    # later (e.g. if we ever emit a sidecar). Keep the import live.
    _ = _atomic_write_jsonl


# ---------------------------------------------------------------------------
# Prompt-version cross-read.
# ---------------------------------------------------------------------------

def _read_rank_version() -> str:
    """Read rank.py's prompt version. Imported lazily to avoid a hard
    coupling at module top -- if rank.py is absent for any reason, we
    fall back to a sentinel that still passes the pydantic pattern."""
    try:
        from src.rank import RANK_PROMPT_VERSION  # local, lazy
        return RANK_PROMPT_VERSION
    except Exception:  # noqa: BLE001
        return "v0.0"


# ---------------------------------------------------------------------------
# Standalone entrypoint for ad-hoc debugging.
# ---------------------------------------------------------------------------

def _parse_cli_date(argv: list[str]) -> _dt.date | None:
    """Minimal CLI: ``python -m src.summarise [YYYY-MM-DD]``."""
    if len(argv) <= 1:
        return None
    try:
        return _dt.date.fromisoformat(argv[1])
    except ValueError:
        print(f"usage: python -m src.summarise [YYYY-MM-DD]", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":  # pragma: no cover -- debug runner only
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    summarise(_parse_cli_date(sys.argv))
