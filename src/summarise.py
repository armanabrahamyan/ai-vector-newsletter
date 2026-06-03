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
2. For prior-coverage stories (``cluster.prior_coverage_ref`` is set), load the
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
   Hands-On, Currents. Each top-N story is placed in exactly one section.
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
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

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

SUMMARISE_PROMPT_VERSION = "v0.13"
"""Pydantic-validated version string. Audit tag:
``summarise-v0.13-2026-06-03``. v0.13 (voice diversity injection):
  - Two new pieces of context inlined into the per-story prompt and the
    section-intro prompt. (A) Intros + first-story closings from the last
    ``VOICE_DIVERSITY_LOOKBACK`` released issues, framed as RECENTLY USED
    CONSTRUCTIONS - do not repeat. (B) Anti-patterns parsed from
    EDITORIAL.md's "Anti-patterns the editor will flag" section, framed
    as ANTI-PATTERNS - do not use today. Both blocks degrade gracefully:
    missing past issues skip with INFO, missing anti-patterns section
    falls back to no injection with INFO. Fixes the recurring drift
    pattern caught by editor reviews on issues #8-#11 (May 30 - Jun 2)
    where Big Picture / Hands-On intros and closings collapsed into
    repeated constructions ("X outruns Y", "Verify before you X") across
    consecutive issues.
v0.12 (Pulse re-summarise):
  - After ``_pick_pulse`` chooses the winning cluster_id, the head-tier
    summary for that cluster is DISCARDED and the story is re-summarised
    under a Pulse-specific prompt variant (``section_override="pulse"``).
    The head-tier draft was written under either the Big-Picture STRATEGIC
    QUESTION or the Hands-On IMPERATIVE ACTION closing shape; the Pulse
    needs the PLAIN TAKE landing instead. Carrying both shapes in one
    head-tier prompt didn't work in v0.11 (the concrete section shape
    always won the LLM's attention); the cleaner fix is one extra LLM call
    on the chosen cluster. Failure (parse, validation, LLM error) falls
    back to the original head-tier SummaryBlock and logs a WARNING.
v0.11 (per-section closing shapes):
  - Each section now gets a distinct closing rhythm so the last sentence
    itself signals the section. Pulse closes on a PLAIN TAKE (sharp
    editorial judgement); Big Picture closes on a STRATEGIC QUESTION the
    news raises but does not answer; Hands-On closes on a SHARPENED
    IMPERATIVE ACTION (specific verb on a specific artefact with a
    trigger or condition); Currents closes on a CALIBRATED STAKE ("if X,
    Y; if not, Z"). Frames documented at
    ``_scratch/2026-05-31-closing-frames.md`` and EDITORIAL.md "Closing
    shape" rules per section. Existing voice + length rules unchanged.
v0.10 (Phase 2 section taxonomy + voice):
  - Section value ``on_the_radar`` renamed to ``currents``; per-story
    prompt now branches on the destination section and injects 3-5 lines
    of section-specific voice guidance from EDITORIAL.md "Voice rules per
    section". Pulse opens on a verb; Big Picture names actors + first-
    order consequence; Hands-On puts the artefact in the noun phrase;
    Currents opens conditional / hedged.
  - Section-intro prompt for Currents now requires the LEAD to name the
    aggregate direction (not just the section posture) -- EDITORIAL.md
    promotes it from "nice-to-have" to mandatory for Currents.
v0.9 hardens length caps (tasks #73 + #74):
  - headline: HARD 90 chars / 12 words (was "ideally <= 90 / <= 12"); the
    LLM is told strings that exceed get rejected, must count before returning
  - body: 60 words HARD; collision allowance still applies but the prose
    no longer hints "61-62 is acceptable" in the user-facing prompt
  - post-LLM enforcement in ``_call_and_parse_summary``: a single retry
    with a corrective prompt when either cap is breached; if a second
    attempt still breaches, the story is kept but a warning is logged
    (better to ship than to silently drop a top-N story)
v0.8 vocabulary big_picture / hands_on / currents (v0.10 rename)."""

PULSE_PROMPT_VERSION = "v0.10"
"""Audit tag: ``pulse-v0.10-2026-05-26``. v0.10 (2026-05-26 fix): the Pulse
SELECTION RULE now gates candidacy on sourcing credibility BEFORE the
fresh/recurring partition. A cluster must clear at least one of
(size > 1) | (canonical_id present) | (max trust_weight >= floor) to be
Pulse-eligible. If zero candidates clear, fall back to the unfiltered pool
with a loud WARNING (operator sees it at ratification). The previous
fresh-over-prior-coverage bias and the >=2 signal-dimensions Pulse-class
bar still run inside the eligible pool. This is a behavioural change in
``_pick_pulse``, not a prompt change. v0.9 (#82) biased against prior
coverage."""

PULSE_ELIGIBILITY_TRUST_FLOOR = 3
"""Minimum trust_weight (from ``config/sources.yaml``) such that a single
cluster source carrying this weight or higher clears the Pulse eligibility
gate on its own. Established-source threshold: trust 3+ covers OpenAI /
Anthropic / Hugging Face blogs, regulatory feeds, top independent
authors. Reddit subs (trust 2) and similar community sources require
multi-source corroboration or a canonical artefact instead. Tunable in
one place so eval-engineer can calibrate against labels."""

HEAD_TIER_SUMMARISE_BUDGET = 12
"""How many head-tier (`big_picture` + `hands_on`) stories to summarise.
Covers Pulse (1) + Big Picture (cap 4) + Hands-On (cap 5) with buffer.
Tier-aware truncation introduced 2026-05-30 alongside Shape A: the picker
honours tier as a hard boundary, so the upstream summarise budget must
honour it too -- otherwise a head-tier-heavy day starves the radar pool
even though radar candidates exist in `ranked.jsonl`."""

CURRENTS_TIER_SUMMARISE_BUDGET = 8
"""How many ``currents`` tier stories to summarise. Phase 2 (2026-05-30):
renamed from ``RADAR_TIER_SUMMARISE_BUDGET`` in lockstep with the section
rename. The authoritative HARD ceiling on Currents is now
``editorial.yaml: section_caps.currents.max_stories`` (8), enforced inside
``_pick_currents``; this constant is the upstream INPUT bound (how many
candidates we'll spend LLM tokens on before the picker decides). Keeping
both layers prevents a runaway summarise spend on a paper-heavy day even
if the cap is raised."""

CALLBACK_LOOKBACK_DAYS = 14
"""How many days of past ``issue.json`` to scan for callback context. Matches
the cross-time-dedup lookback Retrieval Engineer uses."""

MAX_CALLBACK_REFERENCES = 3
"""At most this many prior appearances are inlined per cluster -- keeps the
prompt focused and prevents the model getting lost in history."""

VOICE_DIVERSITY_LOOKBACK = 5
"""How many recently-released issues to scan for intros/closings to inject
as 'do not repeat' context. 5 matches the editor's review window."""

EDITORIAL_ANTI_PATTERNS_HEADING = "## Anti-patterns the editor will flag"
"""The exact heading in EDITORIAL.md that the summarise prompt parses for
anti-pattern constructions. If editor renames the section, this constant
moves in lockstep."""

_EDITORIAL_MD_PATH = Path("EDITORIAL.md")
"""Source of the anti-patterns catalogue. Repo-root markdown that the
editor owns; we read it best-effort (missing file = no injection)."""

_VOICE_DIVERSITY_CLOSING_TRUNC = 80
"""Recent-issues closings are truncated to this character count before
inlining. Keeps the do-not-repeat block compact -- the LLM only needs the
construction's SHAPE, not the full sentence."""

JSON_RETRY_BUDGET = 1
"""Mirrors rank.py: one retry on JSON parse failure; second failure -> the
story is dropped from the issue (logged)."""

# ---------------------------------------------------------------------------
# Source-diversity caps (task added 2026-05-27).
#
# Two-layer deterministic post-rank rule, fixes the May 27 single-category
# dominance pattern (9 of 12 stories from papers because arxiv cs.CL alone
# supplied 252 of 424 fetched items + the recent rubric rebalance favoured
# paper-shaped content).
#
# Layer 1 -- universal: no single section may carry > N stories from the
# same source name. Default N=2, baked into code so a forker with no config
# still gets it.
#
# Layer 2 -- per-issue per-category: AI Vector caps `papers` at 4. Forkers
# set their own caps in config/editorial.yaml; absent file = no category cap.
#
# Both layers are pure code -- no LLM, no prompt. Mirrors the architectural
# shape of the v0.10 Pulse-eligibility gate.
# ---------------------------------------------------------------------------

DEFAULT_PER_SOURCE_PER_SECTION = 2
"""Universal per-section cap: no section may carry more than this many
stories from the same source name. Default 2; overridable via
``config/editorial.yaml`` -> ``section_caps.per_source_per_section``. Applies
to every fork by default -- no configuration needed."""

DEFAULT_CURRENTS_MAX_STORIES = 8
"""Phase 2 (2026-05-30): hard ceiling on the Currents section, enforced
in ``_pick_currents``. Overridable via ``config/editorial.yaml`` ->
``section_caps.currents.max_stories``. Default 8 matches the upstream
``CURRENTS_TIER_SUMMARISE_BUDGET`` so a fork without editorial.yaml sees
the same shape as AI Vector's editorial intent."""

_EDITORIAL_YAML_PATH = Path("config/editorial.yaml")
"""Editorial assembly rules (post-rank, deterministic). Separate from
sources.yaml and rubric.yaml; this file governs HOW we ASSEMBLE the issue,
not what we fetch or how we score."""

_SOURCES_YAML_PATH = Path("config/sources.yaml")
"""Reused from rank.py -- we read it for the ``name -> category`` and
``name -> trust_weight`` mappings the cap logic needs. Best-effort load;
missing file degrades to empty mappings (no category cap, no tie-breaks)."""

_UNKNOWN_CATEGORY = "unknown"
"""Bucket label for sources whose category is missing from sources.yaml.
Treated as UNCAPPED by Layer 2 -- a forker who hasn't filled categories
yet should not be silently penalised."""


@dataclass(frozen=True)
class EditorialConfig:
    """Resolved editorial assembly config, threaded through the pickers.

    Built once at ``summarise()`` entry; immutable after that. Holds the
    cap values plus the source -> category and source -> trust lookups so
    the pickers can resolve a cluster to its category without touching
    sources.yaml again. If editorial.yaml is missing, defaults apply
    (per_source_per_section=2, no category cap)."""

    per_source_per_section: int = DEFAULT_PER_SOURCE_PER_SECTION
    per_category_per_issue: dict[str, int] = field(default_factory=dict)
    source_to_category: dict[str, str] = field(default_factory=dict)
    source_to_trust: dict[str, int] = field(default_factory=dict)
    currents_max_stories: int = DEFAULT_CURRENTS_MAX_STORIES
    """Phase 2 cap: hard ceiling on the Currents section. Loaded from
    ``editorial.yaml: section_caps.currents.max_stories``; falls back to
    ``DEFAULT_CURRENTS_MAX_STORIES`` (8) when absent."""


def _load_editorial_config(
    editorial_yaml: Path = _EDITORIAL_YAML_PATH,
    sources_yaml: Path = _SOURCES_YAML_PATH,
) -> EditorialConfig:
    """Best-effort load of editorial.yaml + sources.yaml mappings. Missing
    files / unexpected shapes degrade to defaults (per_source_per_section=2,
    no category cap, empty source maps -- every source category resolves to
    "unknown" and is uncapped). Forkers can drop editorial.yaml entirely and
    the per-source-per-section default still applies."""
    per_source = DEFAULT_PER_SOURCE_PER_SECTION
    per_category: dict[str, int] = {}
    currents_max = DEFAULT_CURRENTS_MAX_STORIES

    if editorial_yaml.exists():
        try:
            data = yaml.safe_load(editorial_yaml.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            _LOG.warning(
                "summarise: could not parse %s -- proceeding with defaults",
                editorial_yaml,
            )
            data = {}
        caps = data.get("section_caps") if isinstance(data, dict) else None
        if isinstance(caps, dict):
            n = caps.get("per_source_per_section")
            if isinstance(n, int) and n >= 1:
                per_source = n
            pc = caps.get("per_category_per_issue")
            if isinstance(pc, dict):
                for cat, val in pc.items():
                    if isinstance(cat, str) and isinstance(val, int) and val >= 0:
                        per_category[cat] = val
            # Phase 2 (2026-05-30): currents.max_stories hard ceiling.
            currents_block = caps.get("currents")
            if isinstance(currents_block, dict):
                m = currents_block.get("max_stories")
                if isinstance(m, int) and m >= 0:
                    currents_max = m

    source_to_category: dict[str, str] = {}
    source_to_trust: dict[str, int] = {}
    if sources_yaml.exists():
        try:
            sdata = yaml.safe_load(sources_yaml.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            _LOG.warning(
                "summarise: could not parse %s -- proceeding without category map",
                sources_yaml,
            )
            sdata = {}
        slist = sdata.get("sources") if isinstance(sdata, dict) else None
        if isinstance(slist, list):
            for entry in slist:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                if not isinstance(name, str):
                    continue
                cat = entry.get("category")
                if isinstance(cat, str):
                    source_to_category[name] = cat
                tw = entry.get("trust_weight")
                if isinstance(tw, int):
                    source_to_trust[name] = tw

    return EditorialConfig(
        per_source_per_section=per_source,
        per_category_per_issue=per_category,
        source_to_category=source_to_category,
        source_to_trust=source_to_trust,
        currents_max_stories=currents_max,
    )


def _cluster_category(cluster: Cluster, cfg: EditorialConfig) -> str:
    """Resolve a cluster to a single category for the per-issue cap.

    Rule: pick the category of the highest-trust source in the cluster.
    Ties broken deterministically by source name (ascending). This matches
    the ``canonical_title`` selection style in ``_build_cluster`` -- the
    highest-trust voice is the one we attribute the cluster to. Sources
    without an entry in sources.yaml resolve to ``"unknown"`` (uncapped).

    A cluster carries `Cluster.sources: list[str]` (distinct source names).
    We do NOT iterate items here -- one trust value per source name is
    enough, and sources.yaml is the system-of-record for that mapping."""
    if not cluster.sources:
        return _UNKNOWN_CATEGORY

    # Deterministic sort: highest trust first, then source name ascending
    # for stable tie-breaks across re-runs.
    def _key(src: str) -> tuple[int, str]:
        # Negative trust so descending sort by trust falls out of asc sort.
        trust = cfg.source_to_trust.get(src, 0)
        return (-trust, src)

    chosen_source = sorted(cluster.sources, key=_key)[0]
    return cfg.source_to_category.get(chosen_source, _UNKNOWN_CATEGORY)


def _would_exceed_section_cap(
    cluster: Cluster | None,
    sources_used_this_section: Counter[str],
    cfg: EditorialConfig,
) -> bool:
    """Layer 1 check: would accepting this cluster push any of its sources
    over the per-section cap? If the cluster carries multiple source names,
    EVERY source is incremented on acceptance -- a single over-cap source
    blocks the whole cluster. Missing cluster degrades to ``False`` (we
    cannot evaluate; let the caller decide)."""
    if cluster is None:
        return False
    cap = cfg.per_source_per_section
    if cap <= 0:
        return False
    for src in cluster.sources:
        if sources_used_this_section[src] + 1 > cap:
            return True
    return False


def _would_exceed_category_cap(
    category: str,
    categories_used_this_issue: Counter[str],
    cfg: EditorialConfig,
) -> bool:
    """Layer 2 check: would accepting one more story of this category exceed
    its per-issue cap? Categories not present in ``per_category_per_issue``
    are UNCAPPED (return False). The ``unknown`` bucket is, by definition,
    not in the cap map -- so unknown-category clusters are uncapped."""
    cap = cfg.per_category_per_issue.get(category)
    if cap is None:
        return False
    return categories_used_this_issue[category] + 1 > cap


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
  - Pad to length. UNDER 60 is fine. "Currents" items should run
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

# Phase 2 (2026-05-30): per-section voice rules distilled from
# EDITORIAL.md "Voice rules per section". The summary LLM does not know
# which section the story will land in (the picker decides downstream),
# but the tier is a clean 1:1 proxy for the destination section:
#   tier=big_picture -> Big Picture voice rules
#   tier=hands_on    -> Hands-On voice rules
#   tier=currents    -> Currents voice rules
# Stories tiered ``cut`` never reach summarisation.
#
# A head-tier story may also become the Pulse (picked downstream from the
# union of big_picture + hands_on). The Pulse voice asks for an
# imperative, verb-led opener -- which is compatible with the head-tier
# guidance below, and we surface the Pulse hint in the prompt so a story
# the picker might elevate already reads in voice.
_VOICE_PER_SECTION: dict[str, str] = {
    "big_picture": (
        "BIG PICTURE VOICE -- named actors + first-order consequence\n"
        "Lead with WHO (organisation, regulator, market) and WHAT CHANGES\n"
        "for them. The first sentence names a real actor; the close ties\n"
        "to a decision a senior leader would make THIS WEEK. Avoid\n"
        "abstract paper-abstract framings (\"Researchers find X\", \"AI\n"
        "agents now act in ways pre-deployment cannot anticipate\") --\n"
        "those are off-voice here. Prefer \"X is moving; here's what\n"
        "shifts\" over \"X has been released.\""
    ),
    "hands_on": (
        "HANDS-ON VOICE -- artefact in the noun phrase\n"
        "The TOOL / REPO / VERSION / CONFIG must be present in the\n"
        "headline noun phrase OR in the first sentence of the body. The\n"
        "reader should be able to tell what they would clone, install, or\n"
        "evaluate without reading the rest. The direction-note prescribes\n"
        "the ACTION (\"clone before X\"; \"run against your eval\"; \"wait\n"
        "for the repo\"). No leader pull-quotes tacked on (\"raise this at\n"
        "your model-risk review\" is Big Picture voice, off-voice here)."
    ),
    "currents": (
        "CURRENTS VOICE -- conditional / hedged opening; signal of motion,\n"
        "not arrival. Open with a hedge: \"If this holds...\", \"Early\n"
        "signal that...\", \"Worth watching: X moving toward Y.\" The\n"
        "direction-note explicitly says \"no action yet\" and WHY -- thin\n"
        "sourcing, early trajectory, single benchmark. A Currents story\n"
        "that reads as a confirmed arrival is mis-tiered; pull the hedge\n"
        "forward to make the maturity visible. Shorter than head-section\n"
        "bodies; cap at 50 words when in doubt."
    ),
}

_PULSE_HINT_FOR_HEAD_TIER = (
    "PULSE NOTE -- if this story is the most significant of the day, the\n"
    "downstream picker may elevate it to The Pulse. The Pulse opens on a\n"
    "VERB where possible (\"Run autonomous coding agents safely.\" /\n"
    "\"Stop defaulting to frontier models.\"), with the direction-note\n"
    "in the body. Writing the headline in that imperative shape now\n"
    "means the picker doesn't need a separate rewrite."
)


# Phase v0.11 (2026-05-31): per-section CLOSING SHAPES. Each section ends
# with a distinct rhythm so the final sentence itself signals the section
# without the reader needing the section header. The frames are documented
# at _scratch/2026-05-31-closing-frames.md and mirrored in EDITORIAL.md
# "Closing shape" rules per section. Voice + length rules above are
# unchanged; closing-shape sits at the END of the section-voice block as
# the final instruction for how the summary LANDS.
_CLOSING_SHAPE_PER_SECTION: dict[str, str] = {
    "big_picture": (
        "CLOSING SHAPE -- STRATEGIC QUESTION\n"
        "End on the sharp unresolved QUESTION the news raises but doesn't\n"
        "answer. The reader carries it into their next strategy review and\n"
        "has to take a position. This OVERRIDES the generic body-close\n"
        "rule above (\"close with a judgement tied to a specific decision\")\n"
        "for Big Picture stories: the question IS the decision-tied close.\n"
        "Do NOT also append an imperative (\"Raise this at your next\n"
        "review\") -- the question alone is the landing. NOT a rhetorical\n"
        "question with an obvious answer; NOT a prescription dressed as a\n"
        "question (\"shouldn't you test X?\"); NOT a vague \"what does\n"
        "this mean?\". Anchor the question to a specific role, decision,\n"
        "or constraint in the reader's org.\n"
        "Examples (use as calibration, not templates):\n"
        "  - \"When the agent ships 80% of commits unsupervised, what does\n"
        "    the human reviewer still own -- and is that role staffed in\n"
        "    your org?\"\n"
        "  - \"When the safety filter and the regulator's rulebook\n"
        "    disagree, which one governs your customer-facing deployment?\""
    ),
    "hands_on": (
        "CLOSING SHAPE -- IMPERATIVE ACTION (SHARPENED)\n"
        "End on a SPECIFIC prescription with a trigger or condition, on a\n"
        "SPECIFIC artefact. A practitioner can copy the closing into team\n"
        "chat. Generic verbs without specific targets FAIL this shape\n"
        "(\"just test it\", \"bench it before you trust it\", \"run it\n"
        "against your eval\" -- all too vague). Name what to do, on what\n"
        "object, with what trigger.\n"
        "Examples (use as calibration, not templates):\n"
        "  - \"Swap one production agentic-coding loop to Opus 4.8 this\n"
        "    week and measure the unflagged-flaw rate against your\n"
        "    incident baseline.\"\n"
        "  - \"Run v0.22.0 against your own latency baseline this week;\n"
        "    if you confirm even half the 28.9% claim, ship the upgrade --\n"
        "    the cost-per-token math justifies the migration.\""
    ),
    "currents": (
        "CLOSING SHAPE -- CALIBRATED STAKE\n"
        "End on a two-sided watch-condition with stakes on BOTH branches.\n"
        "Structure: \"If X holds, Y; if not, Z.\" Both branches must carry\n"
        "REAL stakes -- one-sided \"if X, Y\" without an inverse FAILS;\n"
        "false-binaries where one branch is impossible FAIL; placebo\n"
        "stakes (\"if not, we'll know more next quarter\") FAIL. The\n"
        "reader should know what to watch for AND what it will matter for\n"
        "on either outcome.\n"
        "Examples (use as calibration, not templates):\n"
        "  - \"If ITS-Mina replicates, attention-free forecasting is a\n"
        "    real architecture line and your shortlist needs revisiting.\n"
        "    If it doesn't, the benchmark suite itself becomes the story\n"
        "    -- and that matters more than any single model claim.\"\n"
        "  - \"If the FinGuard claim holds under audit, every customer-\n"
        "    facing deployment without a regulation-grounded check is a\n"
        "    compliance gap waiting to be found. If it doesn't, the gap\n"
        "    is the audit.\""
    ),
}

# v0.12 (2026-05-31): Pulse-specific voice block, used by the Pulse
# re-summarise pass (``_resummarise_as_pulse``). When a story is elevated
# to The Pulse, the head-tier voice (Big Picture: named actors + strategic
# question; Hands-On: artefact-in-noun-phrase + imperative action) is the
# WRONG framing -- the Pulse is the day's editorial anchor, not a section
# entry. This block reads as the PRIMARY voice rule with the
# ``_PULSE_CLOSING_SHAPE`` (plain take) as the landing. The head-tier
# voice + closing shape are NOT attached when ``section_override="pulse"``.
_PULSE_VOICE_BLOCK = (
    "PULSE VOICE (HIGHEST PRECEDENCE) -- this story has been elevated to\n"
    "The Pulse, today's editorial anchor. The previous head-tier framing\n"
    "(Big Picture strategic question, Hands-On imperative action) does NOT\n"
    "apply here. Rewrite under these rules instead:\n"
    "  - HEADLINE: open on the VERB where possible. Imperative shape lands\n"
    "    The Pulse cleanly (\"Run autonomous coding agents safely.\" /\n"
    "    \"Stop defaulting to frontier models.\"). Stake or consequence-led\n"
    "    declaratives are also fine; the verb-first opener is the strong\n"
    "    default, not a hard rule.\n"
    "  - BODY: the day's direction in plain editorial prose. Open on the\n"
    "    verb where possible. Direction-note is MANDATORY and lives in the\n"
    "    body, not the headline. NO section-trope opening (\"Researchers\n"
    "    found...\"; \"A new paper shows...\"; \"X is moving;...\"). The\n"
    "    Pulse is a single editorial position, not a paper summary or a\n"
    "    section-pattern summary."
)


# Pulse closing shape (Plain take) -- attached to head-tier stories so a
# story the picker might elevate to The Pulse already reads with the right
# landing. Paired with _PULSE_HINT_FOR_HEAD_TIER above. v0.12: also used as
# the PRIMARY closing shape inside the Pulse re-summarise prompt
# (``section_override="pulse"`` in ``_build_summary_prompt``).
_PULSE_CLOSING_SHAPE = (
    "PULSE CLOSING SHAPE -- PLAIN TAKE (HIGHEST PRECEDENCE)\n"
    "If this story is elevated to The Pulse, the close is a short editorial\n"
    "JUDGEMENT (1-2 declarative sentences) -- the publication's position on\n"
    "this story today. This OVERRIDES BOTH the generic body-close rule and\n"
    "the Big-Picture STRATEGIC QUESTION shape above. NEVER a question\n"
    "(\"Who owns...?\"); NEVER a prescription (\"Test this against X\");\n"
    "the last character must be a FULL STOP. A plain take NAMES WHAT'S\n"
    "TRUE NOW given this story -- the shift and what it means -- not what\n"
    "the reader should do about it.\n"
    "Examples (use as calibration, not templates):\n"
    "  - \"General safety filters built for the open web are reaching the\n"
    "    limits of their fit for regulated work. Domain-grounded\n"
    "    filtering is where the credible safety story starts now.\"\n"
    "  - \"Anthropic just told you the truth about a release. That's the\n"
    "    news. Whether to swap is a procurement question; whether the lab\n"
    "    is honest is the strategic one.\""
)


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

    # Tier-aware truncation: split the summarise budget by tier so a
    # head-tier-heavy day doesn't starve Currents. Schema v0.7 (2026-05-
    # 31): within each tier pool we order by that tier's per-section
    # weighted score (from RankedStory.score_by_section). The aggregate
    # ``score`` is no longer the routing authority for picking which top-N
    # to summarise -- we want the candidate ordering inside each pool to
    # match the section the picker will route to. Falls back to the legacy
    # ``score`` for archived rows without score_by_section.
    #
    # For the head-tier budget we MERGE big_picture + hands_on candidates
    # sorted by their respective section-specific scores -- a head-tier-
    # heavy day with 30+ big_picture-tier stories shouldn't starve the
    # Hands-On pool. Each candidate's sort key is its OWN tier-section
    # score (big_picture stories ranked by their big_picture score;
    # hands_on stories ranked by their hands_on score).
    def _section_score_or_legacy(r: RankedStory, section: str) -> int:
        if r.score_by_section is None:
            return r.score
        return r.score_by_section.get(section, r.score)

    head_pool = sorted(
        (r for r in ranked if r.tier in ("big_picture", "hands_on")),
        key=lambda r: _section_score_or_legacy(r, r.tier),
        reverse=True,
    )
    head_top = head_pool[:HEAD_TIER_SUMMARISE_BUDGET]
    currents_top = sorted(
        (r for r in ranked if r.tier == "currents"),
        key=lambda r: _section_score_or_legacy(r, "currents"),
        reverse=True,
    )[:CURRENTS_TIER_SUMMARISE_BUDGET]
    top = head_top + currents_top
    clusters_by_id = _load_clusters_index(clusters_in)
    items_by_id = _load_items_index(items_in)

    # Build callback context for any cluster with a prior_coverage_ref.
    # Round B: callback lookback reads CANONICAL only -- drafts Arman
    # discarded must not seed callbacks.
    callbacks_by_root = _load_callback_context(
        run_date,
        roots={c.prior_coverage_ref for c in clusters_by_id.values()
               if c.prior_coverage_ref},
    )

    # v0.13 (2026-06-03): voice-diversity context loaded ONCE per run and
    # threaded through both the per-story summarise prompt and the
    # section-intro prompt. Empty string when the released archive and
    # EDITORIAL.md have nothing to contribute (forker day-one, test paths
    # that don't seed history).
    recent_voices = _load_recent_intros_and_closings(run_date)
    anti_patterns = _load_editorial_anti_patterns()
    voice_diversity_block = _render_voice_diversity_block(
        recent_voices, anti_patterns,
    )
    if recent_voices or anti_patterns:
        _LOG.info(
            "summarise: voice-diversity injection active "
            "(recent_issues=%d, anti_patterns=%d)",
            len(recent_voices), len(anti_patterns),
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
        if cluster.prior_coverage_ref:
            callbacks = callbacks_by_root.get(cluster.prior_coverage_ref, [])
        try:
            block = _summarise_one(
                story=story, cluster=cluster, items=items, callbacks=callbacks,
                voice_diversity_block=voice_diversity_block,
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
    # v0.10 (Phase 2, 2026-05-30): pulse -> big_picture -> hands_on -> currents.
    # The Big Picture comes first per Arman's reading order.
    # clusters_by_id + items_by_id are threaded through so the v0.10 Pulse
    # eligibility gate can read cluster size, canonical_id, and item-level
    # trust_weight without re-reading JSONL. editorial_config is loaded
    # once here (source-diversity caps, 2026-05-27) and threaded through
    # the pickers; defaults apply when config/editorial.yaml is missing.
    editorial_config = _load_editorial_config()
    pulse_section, big_picture_section, hands_on_section, currents_section = \
        _assemble_sections(
            blocks,
            clusters_by_id=clusters_by_id,
            items_by_id=items_by_id,
            editorial_config=editorial_config,
            callbacks_by_root=callbacks_by_root,
            voice_diversity_block=voice_diversity_block,
        )

    # --- Section intros (Phase B) ---------------------------------------
    # One LLM call per non-pulse section, fed the section's stories so the
    # intro reads the day's pattern. Pulse never carries an intro -- its
    # whole job is to BE the framing. Failures degrade gracefully for Big
    # Picture / Hands-On: the template hides missing intros, the issue
    # still ships. For Currents (Phase 2): the aggregate-direction lead
    # is editorially mandatory per EDITORIAL.md -- ``_populate_section_intro``
    # retries once on failure and logs a WARNING when both attempts miss.
    _populate_section_intro(big_picture_section, voice_diversity_block)
    _populate_section_intro(hands_on_section, voice_diversity_block)
    _populate_section_intro(currents_section, voice_diversity_block)

    # --- Shape post-condition (schema v3, 2026-05-30) -------------------
    # With tier as authority in section routing, an under-fed section is
    # an upstream signal -- either rank.py didn't promote enough stories,
    # or the rubric thresholds are misset for today's input. We compute
    # the issue shape here and stamp it (plus a one-line reason) into
    # Issue.notes so the editor / Arman / release banner see it without
    # re-deriving from section counts. Does NOT block on red; that's a
    # render-side editorial banner concern.
    shape, shape_reason = _compute_issue_shape(
        pulse_section, big_picture_section, hands_on_section, currents_section,
    )
    if shape in {"amber", "red"}:
        _LOG.warning(
            "summarise: issue shape %s -- %s", shape, shape_reason,
        )

    # --- Construct + validate -------------------------------------------
    # issue_number is intentionally None in staging output. Numbering is a
    # release-time operation; see DESIGN.md "Issue Number Registry" +
    # "Archive: staging vs canonical".
    issue = Issue(
        issue_number=None,
        date=run_date,
        pulse=pulse_section,
        sections=[big_picture_section, hands_on_section, currents_section],
        generated_at=_dt.datetime.now(_dt.timezone.utc),
        prompt_versions={
            "rank": _read_rank_version(),
            "summarise": SUMMARISE_PROMPT_VERSION,
            "pulse": PULSE_PROMPT_VERSION,
        },
        notes=f"shape: {shape} -- {shape_reason}",
    )

    _write_issue_json(issue_out, issue)

    pulse_headline = issue.pulse.stories[0].headline if issue.pulse.stories else "?"
    _LOG.info(
        "summarised top %d: pulse=%r / big_picture: %d / hands_on: %d / "
        "currents: %d | issue #(staging -- not yet numbered) -> %s",
        len(blocks), pulse_headline,
        len(big_picture_section.stories),
        len(hands_on_section.stories),
        len(currents_section.stories),
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
        # Pulse block + every section's stories carry prior_coverage_ref
        # (older archives may serialise the field under its v1 name
        # ``cross_time_ref`` -- accept both for backwards compatibility).
        for block in _iter_blocks(payload):
            ref = block.get("prior_coverage_ref") or block.get("cross_time_ref")
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
# Voice diversity injection (v0.13, 2026-06-03).
#
# Two pieces of context inlined into BOTH the per-story summarise prompt
# AND the section-intro prompt so the LLM does not slip into the recurring
# default constructions the editor caught on issues #8-#11.
#
# (A) RECENTLY USED CONSTRUCTIONS -- pulled from the last
#     ``VOICE_DIVERSITY_LOOKBACK`` RELEASED issues. For each past issue
#     we extract the four section intro leads + the closing sentence of
#     the Pulse story + the closing sentence of each section's first
#     story. The prompt instructs the LLM not to reuse these
#     constructions today.
#
# (B) ANTI-PATTERNS -- a parsed list from EDITORIAL.md's
#     ``EDITORIAL_ANTI_PATTERNS_HEADING`` section. Editor-owned catalogue
#     of constructions the LLM keeps falling into ("X outruns Y",
#     "Verify before you X", etc).
#
# Both pieces are best-effort: a missing past issue is skipped with
# INFO, an unparseable JSON is skipped with INFO, and the anti-patterns
# section being absent (not yet added by editor, or rolled back) falls
# back to no injection with a single INFO log. Nothing in this block
# can crash the issue.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _PastIssueVoice:
    """One past issue's intros + closings, in the shape the prompt needs.
    ``intro_leads`` maps section name -> the LEAD phrase used that day
    (None when the section had no intro -- Pulse always; older issues
    occasionally for the others). ``first_story_closings`` maps section
    name (including ``pulse``) -> the closing sentence of that section's
    first story, truncated to ``_VOICE_DIVERSITY_CLOSING_TRUNC`` chars."""
    issue_date: _dt.date
    intro_leads: dict[str, str]
    first_story_closings: dict[str, str]


def _closing_sentence(summary: str) -> str:
    """Pull the last sentence-shaped fragment from a summary body, truncated
    to ``_VOICE_DIVERSITY_CLOSING_TRUNC`` chars. We split on full stops
    (the body rule mandates a full-stop close), keep the last non-empty
    fragment, and strip whitespace. Empty input -> empty string."""
    s = (summary or "").strip()
    if not s:
        return ""
    # Split on full stop; drop trailing empties from "...sentence." -> ["...sentence", ""].
    parts = [p.strip() for p in s.split(".") if p.strip()]
    if not parts:
        return ""
    last = parts[-1]
    if len(last) > _VOICE_DIVERSITY_CLOSING_TRUNC:
        last = last[:_VOICE_DIVERSITY_CLOSING_TRUNC].rstrip() + "..."
    return last


def _load_recent_intros_and_closings(
    today: _dt.date,
    lookback: int = VOICE_DIVERSITY_LOOKBACK,
) -> list[_PastIssueVoice]:
    """Walk back from ``today`` (exclusive) up to ``CALLBACK_LOOKBACK_DAYS``
    calendar days and collect the first ``lookback`` released issues'
    intro leads + first-story closings.

    Returns newest-first. Tolerates missing or unparseable issues (skipped
    with INFO log). When the released archive is empty -- a forker on day
    one, or eval / test paths that don't seed history -- returns an empty
    list and the caller renders no recent-issue context.

    We walk by calendar day rather than directory listing because
    ``paths.issue_path`` is the single source of truth for archive layout
    (staging vs released split landed in Round B). A directory-listing
    approach would couple the helper to the on-disk shape and break in
    tests that monkeypatch ``RELEASED_ROOT``.
    """
    if lookback <= 0:
        return []
    out: list[_PastIssueVoice] = []
    # Calendar window: scan back as far as the callback window so a slow
    # weekend doesn't starve the injection. We stop as soon as we have
    # ``lookback`` issues, so the worst-case cost is one stat per missing
    # day up to the callback lookback (cheap).
    max_days_back = max(lookback * 3, CALLBACK_LOOKBACK_DAYS)
    for delta in range(1, max_days_back + 1):
        if len(out) >= lookback:
            break
        day = today - _dt.timedelta(days=delta)
        canonical_issue = paths.issue_path(day, canonical=True)
        if not canonical_issue.exists():
            continue
        try:
            payload = json.loads(canonical_issue.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 -- never crash the issue on a bad past file
            _LOG.info(
                "summarise: voice-diversity loader could not parse %s -- "
                "skipping that day",
                canonical_issue,
            )
            continue
        if not isinstance(payload, dict):
            _LOG.info(
                "summarise: voice-diversity loader saw non-object payload "
                "at %s -- skipping that day",
                canonical_issue,
            )
            continue

        intro_leads: dict[str, str] = {}
        closings: dict[str, str] = {}

        # Pulse: no intro_lead (the Pulse IS the framing); only the closing.
        pulse = payload.get("pulse") or {}
        if isinstance(pulse, dict):
            stories = pulse.get("stories") or []
            if isinstance(stories, list) and stories:
                first = stories[0]
                if isinstance(first, dict):
                    closing = _closing_sentence(first.get("summary") or "")
                    if closing:
                        closings["pulse"] = closing

        # The other three sections: each may carry an intro_lead and a
        # first-story closing. Currents legacy alias on_the_radar also
        # captured -- some archived issues used the old name and we still
        # want to know the construction was used recently.
        for section in payload.get("sections") or []:
            if not isinstance(section, dict):
                continue
            name = section.get("name")
            if not isinstance(name, str):
                continue
            # Normalise legacy ``on_the_radar`` -> ``currents`` so the prompt
            # block reads with a single section vocabulary.
            section_key = "currents" if name == "on_the_radar" else name
            lead = section.get("intro_lead")
            if isinstance(lead, str) and lead.strip():
                intro_leads[section_key] = lead.strip()
            stories = section.get("stories") or []
            if isinstance(stories, list) and stories:
                first = stories[0]
                if isinstance(first, dict):
                    closing = _closing_sentence(first.get("summary") or "")
                    if closing:
                        closings[section_key] = closing

        out.append(_PastIssueVoice(
            issue_date=day,
            intro_leads=intro_leads,
            first_story_closings=closings,
        ))
    return out


def _load_editorial_anti_patterns(
    editorial_md_path: Path = _EDITORIAL_MD_PATH,
) -> list[str]:
    """Parse the ``EDITORIAL_ANTI_PATTERNS_HEADING`` section of EDITORIAL.md
    into a list of bullet contents (the text after the leading ``- ``).

    Defensive: skip blank lines, skip lines that don't start with ``- ``,
    stop at the next ``## `` heading or EOF. If the section is missing
    entirely (editor hasn't authored it yet, or rolled back), log a single
    INFO line and return an empty list. The summarise prompt then falls
    back to no anti-pattern injection -- the recent-issues block still
    fires.

    The heading match is exact (case-sensitive, including the leading
    ``## ``). The editor and LLM Engineer move ``EDITORIAL_ANTI_PATTERNS_HEADING``
    in lockstep if the section is renamed.
    """
    if not editorial_md_path.exists():
        _LOG.info(
            "summarise: voice-diversity anti-patterns -- %s not found, "
            "skipping anti-pattern injection",
            editorial_md_path,
        )
        return []
    try:
        text = editorial_md_path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        _LOG.info(
            "summarise: voice-diversity anti-patterns -- could not read %s, "
            "skipping anti-pattern injection",
            editorial_md_path,
        )
        return []

    target = EDITORIAL_ANTI_PATTERNS_HEADING
    lines = text.splitlines()
    # Find the heading line. Match the trimmed line for robustness against
    # trailing whitespace; the rest of the parse uses the original line.
    start_idx: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == target:
            start_idx = i + 1
            break
    if start_idx is None:
        _LOG.info(
            "summarise: voice-diversity anti-patterns -- heading %r not "
            "found in %s, skipping anti-pattern injection",
            target, editorial_md_path,
        )
        return []

    out: list[str] = []
    for line in lines[start_idx:]:
        stripped = line.strip()
        if stripped.startswith("## "):
            # Next ## heading -- end of our section.
            break
        if not stripped:
            continue
        if not stripped.startswith("- "):
            # Body prose inside the section -- skip without aborting.
            continue
        content = stripped[2:].strip()
        if content:
            out.append(content)
    return out


def _render_voice_diversity_block(
    recent: list[_PastIssueVoice],
    anti_patterns: list[str],
) -> str:
    """Format the two pieces into the prompt segment. Returns an empty
    string when both pieces are empty -- the caller then injects nothing
    (no header for an empty constraint).

    Layout (compact, low token cost):

        VOICE DIVERSITY -- the editor will flag repeats from recent issues

        RECENTLY USED CONSTRUCTIONS -- do not repeat:
          [pulse]
            - 2026-06-02 close: "..."
          [big_picture]
            - 2026-06-02 lead: "..."
            - 2026-06-02 close: "..."
            ...
          ...

        Today's intros and closings must NOT reuse the constructions above.
        Vary the sentence shape AND the underlying epistemic posture. Two
        sections of today's issue cannot share a thesis statement. If
        today's news genuinely is similar to a recent issue's, the
        editorial POSITION may recur -- but the PROSE must not.

        ANTI-PATTERNS -- do not use these constructions today (editor will
        flag them in review):
          - "X outruns Y" / "X is outpacing Y" / ...
          - "Verify before you [verb]" / ...
    """
    if not recent and not anti_patterns:
        return ""

    parts: list[str] = [
        "VOICE DIVERSITY -- the editor will flag repeats from recent issues",
    ]

    if recent:
        parts.append("")
        parts.append("RECENTLY USED CONSTRUCTIONS -- do not repeat:")
        # Group by section so the LLM can see "lead" vs "close" cleanly.
        section_order = ("pulse", "big_picture", "hands_on", "currents")
        for section_name in section_order:
            section_lines: list[str] = []
            for past in recent:
                date_iso = past.issue_date.isoformat()
                lead = past.intro_leads.get(section_name)
                if lead:
                    section_lines.append(
                        f"    - {date_iso} lead: {lead!r}"
                    )
                close = past.first_story_closings.get(section_name)
                if close:
                    section_lines.append(
                        f"    - {date_iso} close: {close!r}"
                    )
            if section_lines:
                parts.append(f"  [{section_name}]")
                parts.extend(section_lines)

        parts.append("")
        parts.append(
            "Today's intros and closings must NOT reuse the constructions "
            "above. Vary the sentence shape AND the underlying epistemic "
            "posture. Two sections of today's issue cannot share a thesis "
            "statement. If today's news genuinely is similar to a recent "
            "issue's, the editorial POSITION may recur -- but the PROSE "
            "must not."
        )

    if anti_patterns:
        parts.append("")
        parts.append(
            "ANTI-PATTERNS -- do not use these constructions today (editor "
            "will flag them in review):"
        )
        for ap in anti_patterns:
            parts.append(f"  - {ap}")

    return "\n".join(parts) + "\n"


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
    voice_diversity_block: str = "",
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

    prompt = _build_summary_prompt(
        story, cluster, items, callbacks, excerpts,
        voice_diversity_block=voice_diversity_block,
    )

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
            prior_coverage_ref=cluster.prior_coverage_ref,
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


def _resummarise_as_pulse(
    story: RankedStory,
    cluster: Cluster,
    items: list[Item],
    callbacks: list[_CallbackRef],
    original_block: SummaryBlock,
    voice_diversity_block: str = "",
) -> SummaryBlock | None:
    """Re-run the per-story summarise prompt under the Pulse-specific
    voice + closing shape (v0.12, 2026-05-31).

    Why this exists. ``_summarise_one`` runs once per top-N story, BEFORE
    ``_pick_pulse`` chooses the Pulse. The head-tier prompt has to carry
    both the section's closing shape (Big Picture strategic question OR
    Hands-On imperative action) AND the Pulse plain-take shape, because
    any head-tier story might become the Pulse. The LLM writes ONE
    ending; the concrete section shape always wins, the Pulse plain-take
    loses. Re-summarising the chosen Pulse cluster under a Pulse-only
    prompt fixes the landing without churning the rest of the pipeline.

    One extra LLM call per day (~5c). The replacement happens before the
    four section ``IssueSection`` objects are built so the Pulse section's
    ``stories[0]`` carries the re-summarised content.

    Failure handling. If the LLM call fails (timeout, parse, validation),
    return ``None``. The caller falls back to ``original_block`` and logs
    a WARNING. The publication still ships, with the original head-tier
    closing rhythm -- one off-shape Pulse is better than a missed issue.
    """
    temperature = float(os.getenv("LLM_TEMPERATURE_SUMMARISE", "0.6"))

    # Reuse the per-process excerpt cache populated by ``_summarise_one``
    # -- the head-tier pass already fetched the source bodies for this
    # cluster's top-3 items. No second HTTP round-trip; the cache is the
    # whole point. Falls back to a fresh fetch if (somehow) we got here
    # without the head-tier pass running.
    excerpts: dict[str, str] = {}
    for it in items[:3]:
        url = str(it.url)
        excerpts[url] = _fetch_source_excerpt(url)

    prompt = _build_summary_prompt(
        story, cluster, items, callbacks, excerpts,
        section_override="pulse",
        voice_diversity_block=voice_diversity_block,
    )

    draft = _call_and_parse_summary(prompt, temperature, cluster.cluster_id)
    if draft is None:
        return None

    # Reuse the source_urls + prior_coverage_ref from the original block.
    # These are deterministic (URL-trust ordering + cluster metadata); the
    # re-summarise pass only changes the prose. Re-deriving from items
    # would be equivalent but wasteful and adds a divergence surface.
    try:
        block = SummaryBlock(
            story_id=cluster.cluster_id,
            headline=draft.headline,
            summary=draft.summary,
            source_urls=list(original_block.source_urls),  # type: ignore[arg-type]
            prior_coverage_ref=original_block.prior_coverage_ref,
            signal=draft.signal,  # type: ignore[arg-type]
        )
    except Exception:  # noqa: BLE001
        _LOG.exception(
            "summarise: Pulse-resummarise SummaryBlock validation failed "
            "for cluster_id=%s -- falling back to original. draft=%s",
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
    section_override: str | None = None,
    voice_diversity_block: str = "",
) -> str:
    """Assemble the per-story summarisation prompt with voice + skills
    inlined and callback context attached when present.

    ``excerpts`` maps item URL -> source body text (fetched lazily by
    ``_summarise_one`` for the top items in the cluster). When provided,
    each item line carries a ``source_excerpt`` block so the LLM writes
    from real source text instead of an empty raw_summary.

    ``section_override`` (v0.12, 2026-05-31): when set, overrides the tier-
    derived voice routing. Today only ``"pulse"`` is supported -- used by
    ``_resummarise_as_pulse`` after the picker fires. The Pulse-override
    branch:
      - drops the head-tier voice (Big Picture / Hands-On) and the head-
        tier closing shape (strategic question / imperative action);
      - injects ``_PULSE_VOICE_BLOCK`` as the PRIMARY section voice;
      - injects ``_PULSE_CLOSING_SHAPE`` (plain take) as the PRIMARY
        closing rhythm -- not appended after another shape that would win
        the LLM's attention.
    Anything other than ``"pulse"`` is ignored (degrades to tier-derived
    routing) so a stray override value can't silently disable voice.
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

    # Phase 2 (2026-05-30): section-specific voice rules, keyed on tier.
    # Tier is a 1:1 proxy for destination section (big_picture / hands_on
    # / currents). For head-tier stories we also include the Pulse-shape
    # hint so a story the picker might elevate already reads in voice.
    #
    # v0.11 (2026-05-31): each section's voice block is followed by the
    # section's CLOSING SHAPE -- the distinct rhythm the summary must land
    # on. The Pulse PLAIN TAKE shape is appended LAST for head-tier stories
    # (the picker may elevate to The Pulse) so its highest-precedence
    # framing is the final landing rule the LLM sees.
    #
    # v0.12 (2026-05-31): when ``section_override == "pulse"`` we drop the
    # head-tier voice + head-tier closing shape entirely and use the Pulse
    # voice + Pulse closing shape as the PRIMARY framing. The head-tier
    # framings were the rule the LLM kept honouring (concrete shape wins);
    # giving Pulse exclusive precedence is the fix.
    if section_override == "pulse":
        section_voice = _PULSE_VOICE_BLOCK + "\n\n" + _PULSE_CLOSING_SHAPE
        voice_header = (
            "SECTION VOICE (override=pulse; this story has been elevated\n"
            "to The Pulse and is being re-summarised under Pulse rules):"
        )
    else:
        section_voice = _VOICE_PER_SECTION.get(story.tier, "")
        closing_shape = _CLOSING_SHAPE_PER_SECTION.get(story.tier, "")
        if closing_shape:
            section_voice = section_voice + "\n\n" + closing_shape
        if story.tier in ("big_picture", "hands_on"):
            section_voice = (
                section_voice
                + "\n\n" + _PULSE_HINT_FOR_HEAD_TIER
                + "\n\n" + _PULSE_CLOSING_SHAPE
            )
        voice_header = (
            f"SECTION VOICE (tier={story.tier}; this story will land in the\n"
            f"matching section unless the editor relabels):"
        )
    section_voice_block = (
        f"\n{voice_header}\n{section_voice}\n"
        if section_voice else ""
    )

    # v0.13 (2026-06-03): voice-diversity injection sits AFTER section voice
    # so the LLM reads the section's rules first, then the "do not reuse
    # these recent constructions" guard. Empty string when the loader has
    # nothing to report (empty archive, missing EDITORIAL.md section).
    voice_diversity_segment = (
        f"\n{voice_diversity_block}\n" if voice_diversity_block else ""
    )

    # v0.12 (2026-05-31): when re-summarising under the Pulse override, we
    # repeat the plain-take rule as the LAST instruction the LLM sees --
    # right before the JSON schema. LLM attention skews to the most recent
    # instruction; the first Pulse re-summarise pass (May 27 fixture) still
    # produced a prescriptive close ("raise this taxonomy at your next
    # pipeline review.") because the body-rules block earlier in the prompt
    # said "close tied to a SPECIFIC DECISION." This terse final reminder
    # makes the override stick.
    if section_override == "pulse":
        pulse_override_tail = (
            "\n- PULSE OVERRIDE (FINAL REMINDER): this is The Pulse. The "
            "close MUST be a PLAIN TAKE -- a short editorial JUDGEMENT "
            "(1-2 declarative sentences) naming what is TRUE NOW given "
            "the day's shift. NOT a question. NOT a prescription "
            "(\"raise this at...\", \"test against...\", \"audit your...\"). "
            "NOT an imperative verb at the end. The Pulse names where "
            "the field moved today; the rest of the issue is for "
            "decisions to make about it.\n"
        )
    else:
        pulse_override_tail = ""

    return f"""\
You are writing one story for AI Vector -- a daily newsletter about
Agentic AI and Generative AI. The cluster was already RANKED and
selected for the issue; your job is to write it well.

{_VOICE_BLOCK}
{_EDITORIAL_FOCUS_BLOCK}
{_FINANCE_LENS_BLOCK}{section_voice_block}{voice_diversity_segment}
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
  has_prior_coverage: {"yes (chain root=" + cluster.prior_coverage_ref + ")" if cluster.prior_coverage_ref else "no"}

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
                   Default for Currents items.
    * "discuss" -- design concept worth raising at a review, not yet
                   shippable. Right call for single-source frameworks
                   without code / benchmarks.
  Choose by what the body actually argues. If the body says "raise this
  at your next architecture review", that's "discuss", not "act".
{pulse_override_tail}
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
# Pulse re-summarise (v0.12, 2026-05-31).
# ---------------------------------------------------------------------------

def _maybe_resummarise_pulse(
    *,
    pulse_id: str,
    by_id: dict[str, tuple[RankedStory, SummaryBlock]],
    clusters_by_id: dict[str, Cluster] | None,
    items_by_id: dict[str, Item] | None,
    callbacks_by_root: dict[str, list[_CallbackRef]] | None,
    voice_diversity_block: str = "",
) -> None:
    """Re-summarise the Pulse-elected cluster under the Pulse-specific
    prompt and replace its entry in ``by_id`` in place.

    Why a separate helper. Keeping the orchestration outside
    ``_assemble_sections`` proper makes the call site read as a single
    discrete step (and matches the structure of
    ``_reconcile_signal_with_audience_tags`` upstream). Pure side-effect:
    mutates ``by_id[pulse_id]`` on success, no-ops on failure (the
    original head-tier SummaryBlock stands).

    Operator log line. On success: ``INFO`` with the message
    ``"pulse re-summarise: <cluster_id> rewritten under pulse-specific
    prompt (was <section>-shaped)"`` so the daily run log shows the extra
    call fired. On failure: ``WARNING`` naming the cluster id so the
    operator sees which Pulse fell back at ratification.
    """
    entry = by_id.get(pulse_id)
    if entry is None:
        # Defensive: the picker returned a cluster_id outside the by_id
        # index. Shouldn't happen given _pick_pulse picks from blocks; if
        # it does, skipping the re-summarise is the safe degrade (the
        # surrounding code will still raise on missing by_id[pulse_id]).
        _LOG.warning(
            "summarise: pulse re-summarise skipped -- cluster_id=%s not in "
            "by_id index (unexpected; head-tier block stands)",
            pulse_id,
        )
        return
    story, original_block = entry

    cluster = clusters_by_id.get(pulse_id) if clusters_by_id is not None else None
    if cluster is None:
        _LOG.warning(
            "summarise: pulse re-summarise skipped -- cluster_id=%s missing "
            "from clusters_by_id (head-tier block stands)",
            pulse_id,
        )
        return

    items = (
        _items_for_cluster(cluster, items_by_id)
        if items_by_id is not None else []
    )
    callbacks: list[_CallbackRef] = []
    if cluster.prior_coverage_ref and callbacks_by_root is not None:
        callbacks = callbacks_by_root.get(cluster.prior_coverage_ref, [])

    original_section = story.tier  # "big_picture" / "hands_on" / "currents"
    try:
        new_block = _resummarise_as_pulse(
            story=story,
            cluster=cluster,
            items=items,
            callbacks=callbacks,
            original_block=original_block,
            voice_diversity_block=voice_diversity_block,
        )
    except Exception:  # noqa: BLE001 -- never crash the issue on the re-summarise
        _LOG.exception(
            "summarise: pulse re-summarise raised for cluster_id=%s -- "
            "falling back to original head-tier summary",
            pulse_id,
        )
        return

    if new_block is None:
        _LOG.warning(
            "summarise: pulse re-summarise failed for cluster_id=%s "
            "(LLM error, parse fail, or validation) -- falling back to "
            "original head-tier summary",
            pulse_id,
        )
        return

    by_id[pulse_id] = (story, new_block)
    _LOG.info(
        "pulse re-summarise: %s rewritten under pulse-specific prompt "
        "(was %s-shaped)",
        pulse_id, original_section,
    )


# ---------------------------------------------------------------------------
# Section assembly.
# ---------------------------------------------------------------------------

def _assemble_sections(
    blocks: list[tuple[RankedStory, SummaryBlock]],
    clusters_by_id: dict[str, Cluster] | None = None,
    items_by_id: dict[str, Item] | None = None,
    editorial_config: EditorialConfig | None = None,
    callbacks_by_root: dict[str, list[_CallbackRef]] | None = None,
    voice_diversity_block: str = "",
) -> tuple[IssueSection, IssueSection, IssueSection, IssueSection]:
    """Place every summarised story into exactly one section. Returns the
    four sections in display order: pulse, big_picture, hands_on, currents.

    Editorial routing rules (v0.10 -- Phase 2, 2026-05-30 section rename):
      - Pulse: highest-scoring head-tier story that passes the eligibility
        gate AND hits >= 2 signal-filter dimensions (significance,
        hands_on_utility, freshness_momentum >= 70). Fallback (logged):
        highest breakdown.significance among eligibles.
      - The Big Picture: stories tiered `big_picture`. Hard cap at 4.
        First, per Arman's reading order. AUDIENCE-only routing now --
        rank.py tiered the story `big_picture`; the picker does not
        re-gate on maturity / signal-filter dimensions.
      - Hands-On: stories tiered `hands_on`. Hard cap at 5. AUDIENCE-only.
      - Currents: stories tiered `currents`, in score-desc order. Hard
        ceiling from ``editorial_config.currents_max_stories`` (Phase 2
        addition).

    Direction notes and finance angles are embedded in summary prose,
    not separate fields (schema v4); the assembler no longer filters on
    direction_note presence.

    ``clusters_by_id`` + ``items_by_id`` are threaded into ``_pick_pulse``
    for the eligibility gate (PULSE v0.10, 2026-05-26). When omitted (only
    happens in narrow unit tests that don't exercise the gate), the gate
    degrades to current-behaviour fallback with a warning.

    Source-diversity caps (2026-05-27). Two-layer deterministic filter:
      - Layer 1: per-source-per-section cap (default 2).
      - Layer 2: per-category-per-issue cap (config-driven; AI Vector caps
        ``papers`` at 4). Categories resolve via the highest-trust source
        in each cluster.
    Hands-On has a minimum-of-3 requirement (the eval gate); if caps
    starve it, the picker degrades with a WARNING and fills from over-cap
    candidates. ``editorial_config=None`` (older test paths) loads from
    disk; pass a config explicitly to control test isolation.
    """
    # blocks already arrive in score-desc order (ranked.jsonl order,
    # preserved by the loop above).
    by_id = {story.cluster_id: (story, block) for story, block in blocks}
    unplaced = set(by_id.keys())

    cfg = editorial_config if editorial_config is not None else _load_editorial_config()

    # State threaded through the pickers. Per-section source counters live
    # inside each picker (Layer 1 binds per-section, not per-issue). The
    # per-issue category counter is shared so Pulse's category counts
    # toward the cap before Big Picture, Hands-On, Currents run.
    categories_used_this_issue: Counter[str] = Counter()

    # --- Pulse ----------------------------------------------------------
    pulse_id = _pick_pulse(blocks, clusters_by_id=clusters_by_id,
                            items_by_id=items_by_id)
    if pulse_id is None:
        raise RuntimeError(
            "summarise: cannot select a Pulse story -- no surviving stories."
        )
    unplaced.discard(pulse_id)

    # --- Pulse re-summarise (v0.12, 2026-05-31) -------------------------
    # The head-tier summary for the picked Pulse story was written under
    # the wrong closing shape (Big Picture's STRATEGIC QUESTION or
    # Hands-On's IMPERATIVE ACTION won the LLM's attention over the Pulse
    # plain-take rule co-attached for the elevation case). Re-summarise
    # the chosen cluster under a Pulse-only prompt and REPLACE the entry
    # in ``by_id`` before the sections are built. On any failure (LLM
    # error, parse fail, validation), keep the original head-tier
    # SummaryBlock and log a WARNING -- the issue still ships.
    _maybe_resummarise_pulse(
        pulse_id=pulse_id,
        by_id=by_id,
        clusters_by_id=clusters_by_id,
        items_by_id=items_by_id,
        callbacks_by_root=callbacks_by_root,
        voice_diversity_block=voice_diversity_block,
    )

    # Pulse's category counts toward the per-issue cap. Pulse is a single
    # story so Layer 1 (per-section cap, n<2) never binds; Layer 2 must
    # see it.
    _accept_into_counters(
        pulse_id, clusters_by_id, cfg,
        sources_in_section=None,
        categories_used_this_issue=categories_used_this_issue,
    )

    # --- The Big Picture (first per Arman's reading order) --------------
    big_picture_ids = _pick_big_picture(
        blocks, unplaced,
        clusters_by_id=clusters_by_id,
        cfg=cfg,
        categories_used_this_issue=categories_used_this_issue,
    )
    for cid in big_picture_ids:
        unplaced.discard(cid)

    # --- Hands-On -------------------------------------------------------
    hands_on_ids = _pick_hands_on(
        blocks, unplaced,
        clusters_by_id=clusters_by_id,
        cfg=cfg,
        categories_used_this_issue=categories_used_this_issue,
    )
    for cid in hands_on_ids:
        unplaced.discard(cid)

    # --- Currents -------------------------------------------------------
    currents_ids = _pick_currents(
        blocks, unplaced,
        clusters_by_id=clusters_by_id,
        cfg=cfg,
        categories_used_this_issue=categories_used_this_issue,
    )

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
    currents_section = IssueSection(
        name="currents",
        stories=[by_id[cid][1] for cid in currents_ids],
    )
    return (pulse_section, big_picture_section, hands_on_section, currents_section)


def _compute_issue_shape(
    pulse_section: IssueSection,
    big_picture_section: IssueSection,
    hands_on_section: IssueSection,
    currents_section: IssueSection,
) -> tuple[str, str]:
    """Compute the issue's "shape" (green / amber / red) + a one-line reason.

    Schema v3 (2026-05-30): the publish gate becomes a post-condition. Under
    the tier-as-authority routing, an under-fed section is a real editorial
    signal -- rank.py either didn't promote enough head-section stories OR
    the rubric thresholds are misset for today's input. This function
    surfaces that signal via ``Issue.notes`` (not blocking) so the editor
    and Arman see it at ratification.

    Bands (Phase 2 rename, 2026-05-30: ``on_the_radar`` -> ``currents``):
      green  -- pulse present AND hands_on >= 3 AND currents >= 3
      amber  -- pulse present AND (
                  hands_on in {1, 2} OR currents in {1, 2} OR
                  big_picture < 2
                )
      red    -- pulse missing OR (hands_on == 0 AND big_picture == 0)

    The bands are precedence-ordered: red overrides amber overrides green.
    A reason string names the binding constraint (e.g. "hands_on: 2 (tier
    pool exhausted)") so the post-condition is auditable.
    """
    pulse_count = len(pulse_section.stories)
    bp_count = len(big_picture_section.stories)
    ho_count = len(hands_on_section.stories)
    cur_count = len(currents_section.stories)

    # Red is the hard floor. Pulse missing is a contract violation upstream
    # (Issue.pulse mandates exactly one block), so this branch fires only
    # in narrow unit-test paths -- but the shape post-condition models it.
    if pulse_count == 0:
        return "red", "pulse missing"
    if ho_count == 0 and bp_count == 0:
        return (
            "red",
            f"hands_on: 0 AND big_picture: 0 (currents: {cur_count}, "
            "tier pool exhausted)",
        )

    # Amber bands. Order matters only for the reason string -- the band
    # itself is one bucket; we pick the most-binding constraint as reason.
    if ho_count in (1, 2):
        return (
            "amber",
            f"hands_on: {ho_count} (tier pool exhausted)",
        )
    if cur_count in (1, 2):
        return (
            "amber",
            f"currents: {cur_count} (tier pool exhausted)",
        )
    if bp_count < 2:
        return (
            "amber",
            f"big_picture: {bp_count} (tier pool exhausted)",
        )

    # Green path: pulse present, hands_on >= 3, currents >= 3,
    # big_picture >= 2. Reason names the counts so the audit trail is
    # uniform across bands.
    return (
        "green",
        f"pulse: 1, big_picture: {bp_count}, "
        f"hands_on: {ho_count}, currents: {cur_count}",
    )


def _accept_into_counters(
    cluster_id: str,
    clusters_by_id: dict[str, Cluster] | None,
    cfg: EditorialConfig,
    *,
    sources_in_section: Counter[str] | None,
    categories_used_this_issue: Counter[str],
) -> None:
    """Bookkeeping helper -- update the cap counters on acceptance. Pure
    side-effecting; mirrors the small helpers in `_pick_pulse` for clarity.

    ``sources_in_section=None`` is the Pulse case: a section of one story
    cannot trigger the per-section cap by definition (cap >= 2 by default),
    so the per-section counter is skipped. The per-issue category counter
    is ALWAYS updated."""
    cluster = clusters_by_id.get(cluster_id) if clusters_by_id else None
    if cluster is None:
        # No category resolvable -- count as unknown (uncapped, harmless).
        categories_used_this_issue[_UNKNOWN_CATEGORY] += 1
        return
    if sources_in_section is not None:
        for src in cluster.sources:
            sources_in_section[src] += 1
    categories_used_this_issue[_cluster_category(cluster, cfg)] += 1


def _section_score_or_aggregate(story: RankedStory, section: str) -> int:
    """Return the per-section weighted score for ``story``, falling back
    to the legacy aggregate ``score`` when ``score_by_section`` is absent
    (archived rows written before schema_version=6).

    Used by the section pickers (v0.7, 2026-05-31) to rank candidates
    within each section's tier pool. Single seam for the fallback rule
    so all pickers behave identically when an old row is mixed with new
    ones (in practice that won't happen within a single ranked.jsonl;
    the helper exists so cross-day eval / debug paths don't crash)."""
    if story.score_by_section is None:
        return story.score
    return story.score_by_section.get(section, story.score)


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


def _pulse_eligibility(
    cluster: Cluster | None,
    items_by_id: dict[str, Item] | None,
) -> tuple[bool, str]:
    """Sourcing-credibility gate for Pulse candidacy (v0.10, 2026-05-26).

    A cluster is Pulse-eligible if it clears at least one of:

      1. ``cluster.size > 1`` -- multiple feeds independently surfaced the
         story (the natural near-dedup signal that a story is real).
      2. ``cluster.canonical_id is not None`` -- the cluster carries a
         verifiable artefact identifier (arxiv abs ID, GitHub release tag,
         DOI). The thing exists; readers can check it.
      3. At least one source in the cluster carries trust_weight >=
         ``PULSE_ELIGIBILITY_TRUST_FLOOR`` (currently 3). Established
         outlets, regulatory feeds, top independent authors -- not Reddit
         and not vendor newsroom hype channels.

    Why these three. Each is an independent sourcing-credibility signal:
    multiple feeds = corroboration, canonical_id = verifiability,
    established source = curator stamp. A Pulse should carry at least one;
    a story with none belongs in Hands-On or Currents.

    Returns ``(eligible, reason)`` where ``reason`` is a short human-
    readable string (single source + no canonical_id + trust_max=N) used
    in INFO logs at ratification time. The full check still runs even
    when one criterion already passes -- the reason string is built from
    the same fields regardless so logs are uniform.
    """
    if cluster is None:
        # Missing cluster: we cannot evaluate sourcing. Conservatively
        # treat as ineligible (caller falls back with a warning).
        return False, "cluster_missing"

    size_ok = cluster.size > 1
    canonical_ok = cluster.canonical_id is not None

    # Compute max trust_weight across the cluster's items. Items carry
    # their trust_weight from fetch time (sources.yaml mirrored onto
    # Item.trust_weight per src/models.py). If items_by_id is missing
    # (some test paths), we cannot read trust; default conservatively
    # so the gate doesn't silently always pass.
    max_trust: int | None
    if items_by_id is None:
        max_trust = None
    else:
        trust_vals = [
            items_by_id[iid].trust_weight
            for iid in cluster.item_ids
            if iid in items_by_id
        ]
        max_trust = max(trust_vals) if trust_vals else None

    trust_ok = (max_trust is not None
                and max_trust >= PULSE_ELIGIBILITY_TRUST_FLOOR)

    eligible = size_ok or canonical_ok or trust_ok

    reason = (
        f"size={cluster.size} "
        f"canonical_id={'present' if canonical_ok else 'none'} "
        f"trust_max={max_trust if max_trust is not None else 'unknown'} "
        f"floor={PULSE_ELIGIBILITY_TRUST_FLOOR}"
    )
    return eligible, reason


def _pick_pulse(
    blocks: list[tuple[RankedStory, SummaryBlock]],
    clusters_by_id: dict[str, Cluster] | None = None,
    items_by_id: dict[str, Item] | None = None,
) -> str | None:
    """The Pulse selection rule (v0.10 -- 2026-05-26: add eligibility gate).

    Selection order (precedence: eligible fresh > eligible recurring >
    ineligible-fallback with WARNING):

      0. **Eligibility gate (v0.10).** Before anything else, filter the
         candidate set to clusters that carry at least one piece of
         sourcing-credibility evidence -- multi-source, canonical artefact,
         or a trust_weight >= floor source. The thin-sourced singleton
         that became the May 26 PII-scrubber Pulse fails this test. If
         zero candidates pass, fall back to the original logic on the
         unfiltered blocks with a loud WARNING so the operator sees the
         smell at ratification.

      1. **Prefer fresh (no prior coverage) for Pulse.** A story with prior
         coverage (the SummaryBlock carries a non-null
         ``prior_coverage_ref``) is a topical recurrence of something we
         covered on a previous day. The Pulse is meant to be the day's
         freshest editorial anchor; leading with a recurrence tells the
         reader "we have nothing new today". So: among surviving blocks,
         any FRESH (prior_coverage_ref is None) story beats any
         prior-coverage story regardless of score.

      2. Within the chosen pool (FRESH if any exist; else prior-coverage),
         prefer stories that hit >= 2 signal-filter dimensions
         (significance, hands_on_utility, freshness_momentum >= 70). This
         is the Pulse-class quality bar inherited from v0.2.

      3. Within the chosen pool, prefer the highest score (blocks arrive in
         score-desc order; the sort below preserves that as the tiebreaker).

    Degraded mode. If ALL surviving stories have prior coverage, we still
    have to fill ``Issue.pulse`` (the model requires exactly 1 story). We
    pick the best prior-coverage story and log a WARNING -- the operator
    sees the issue is light on fresh signal. ``Issue.pulse=None`` is not
    allowed by the schema, so we ship with the smell loud rather than crash.

    Returns None only if ``blocks`` is empty (caller aborts).
    """
    if not blocks:
        return None

    # --- Tier-pool gate (schema v3, 2026-05-30) -------------------------
    # Pulse is picked from the union of the two head-section tiers
    # (big_picture + hands_on). rank.py writes these tiers when a story
    # clears the promote threshold; currents / cut stories are not
    # Pulse-eligible. When the pool is empty (no head-section tiers today),
    # fall back to the unfiltered set with a WARNING -- Issue.pulse
    # requires exactly one story, so we ship the smell rather than crash.
    #
    # Schema v0.7 (2026-05-31): within the head-tier pool, candidates are
    # ranked by the Pulse-specific weighted score
    # (score_by_section["pulse"]) rather than the aggregate ``score``. The
    # aggregate is still fallback-only for any archived row that doesn't
    # carry score_by_section.
    def _pulse_score(story: RankedStory) -> int:
        if story.score_by_section is None:
            return story.score
        return story.score_by_section.get("pulse", story.score)

    head_tier_blocks = sorted(
        ((s, b) for s, b in blocks if s.tier in {"big_picture", "hands_on"}),
        key=lambda sb: _pulse_score(sb[0]),
        reverse=True,
    )
    if head_tier_blocks:
        tier_pool = head_tier_blocks
    else:
        _LOG.warning(
            "summarise: NO HEAD-SECTION TIER FOR PULSE -- zero stories tiered "
            "big_picture or hands_on today (%d candidates). Falling back to "
            "the full block list; Pulse will pick from currents / cut.",
            len(blocks),
        )
        tier_pool = list(blocks)

    # --- v0.10 eligibility gate ----------------------------------------
    # Partition into eligible / ineligible *before* the fresh/recurring
    # partition. Ineligible candidates are excluded from the normal pool;
    # only when EVERY candidate is ineligible do we fall back to the
    # unfiltered set (with a WARNING).
    eligible_blocks: list[tuple[RankedStory, SummaryBlock]] = []
    ineligible_blocks: list[tuple[RankedStory, SummaryBlock]] = []
    for story, block in tier_pool:
        cluster = (
            clusters_by_id.get(story.cluster_id)
            if clusters_by_id is not None
            else None
        )
        eligible, reason = _pulse_eligibility(cluster, items_by_id)
        if eligible:
            eligible_blocks.append((story, block))
        else:
            ineligible_blocks.append((story, block))
            _LOG.info(
                "summarise: Pulse eligibility gate filtered %s "
                "(headline=%r): %s",
                story.cluster_id, block.headline, reason,
            )

    using_fallback = False
    if eligible_blocks:
        gate_blocks = eligible_blocks
    elif tier_pool:
        # Hard fallback. Every candidate failed the gate. Pick from the
        # tier pool anyway so the issue still ships, but log loudly --
        # Arman sees this at ratification and decides whether to ship.
        using_fallback = True
        top = tier_pool[0][0]  # tier_pool is score-desc; top is the chosen-anyway
        _LOG.warning(
            "summarise: PULSE ELIGIBILITY GATE FOUND NO ELIGIBLE CANDIDATES "
            "(%d candidates, all failed sourcing-credibility test). "
            "Falling back to unfiltered pool; chosen-anyway top story is %s "
            "(score=%d, headline=%r). Consider whether today's issue should "
            "ship at all -- no story carries multi-source, a canonical "
            "artefact, or a trust>=%d source.",
            len(ineligible_blocks), top.cluster_id, top.score,
            tier_pool[0][1].headline, PULSE_ELIGIBILITY_TRUST_FLOOR,
        )
        gate_blocks = tier_pool
    else:
        # No tier_pool AND no eligible (shouldn't happen given the guard
        # above sets tier_pool = blocks when head-tier is empty, but the
        # `blocks` list itself could conceivably be empty in a unit-test
        # path that bypasses the entry check; signal it loudly and bail).
        return None

    # Partition by prior-coverage status. prior_coverage_ref lives on the
    # SummaryBlock (mirrored from Cluster at construction time in
    # _summarise_one). This is the deterministic seam -- no LLM, no prompt.
    fresh = [(s, b) for s, b in gate_blocks if b.prior_coverage_ref is None]
    recurring = [(s, b) for s, b in gate_blocks if b.prior_coverage_ref is not None]

    pool: list[tuple[RankedStory, SummaryBlock]]
    if fresh:
        pool = fresh
    else:
        # Degraded mode: every surviving story has prior coverage. Still
        # must pick one (Issue.pulse requires exactly 1 block).
        _LOG.warning(
            "summarise: NO FRESH SIGNAL FOR PULSE -- every surviving story "
            "has prior coverage (carries prior_coverage_ref). Using best "
            "prior-coverage story as Pulse and shipping the smell. "
            "Consider whether today's issue should ship at all."
        )
        pool = recurring

    # Within the pool: Pulse-class quality bar first, then pulse-score order.
    # pool inherits the score_by_section["pulse"]-desc order from the
    # tier-pool sort above; so taking the head of pulse_class lands the
    # highest-pulse-scored Pulse-class story.
    pulse_class = [sb for sb in pool if _signal_dimensions_hit(sb[0]) >= 2]
    if pulse_class:
        chosen = pulse_class[0]  # pool preserves pulse-score-desc order
    else:
        # Fallback within the pool: highest breakdown.significance, then
        # the Pulse-specific weighted score (v0.7).
        chosen = max(
            pool,
            key=lambda sb: (sb[0].breakdown.get("significance", 0),
                            _pulse_score(sb[0])),
        )
        if fresh:
            _LOG.warning(
                "summarise: no Pulse-class FRESH story today (none hit >= 2 "
                "signal dimensions); using top-significance fresh fallback"
            )

    # Operator visibility: log when we demoted a higher-scored prior-coverage
    # story for a lower-scored fresh story. This is the rule firing.
    # v0.7: compare on the Pulse-specific weighted score (the score the
    # picker actually used to rank candidates).
    if fresh and recurring:
        top_recurring = recurring[0][0]  # pool was pulse-score-desc
        if _pulse_score(top_recurring) > _pulse_score(chosen[0]):
            _LOG.info(
                "summarise: Pulse fresh-over-prior-coverage bias fired -- "
                "demoted prior-coverage story %s (pulse_score=%d) in favour "
                "of fresh story %s (pulse_score=%d). #82.",
                top_recurring.cluster_id, _pulse_score(top_recurring),
                chosen[0].cluster_id, _pulse_score(chosen[0]),
            )

    # v0.10: when the eligibility gate fires and the unfiltered top
    # candidate is NOT what we chose, log INFO with both ids. (Skip in
    # fallback mode -- the WARNING above already says everything.)
    # v0.7: compare on the Pulse-specific weighted score (the score the
    # picker used to rank); fall back to aggregate ``score`` for archived
    # rows without ``score_by_section``.
    if (not using_fallback and ineligible_blocks and tier_pool
            and tier_pool[0][0].cluster_id != chosen[0].cluster_id):
        top_overall = tier_pool[0][0]
        _LOG.info(
            "summarise: Pulse eligibility gate demoted top-scored "
            "ineligible story %s (pulse_score=%d) in favour of eligible "
            "story %s (pulse_score=%d). v0.10.",
            top_overall.cluster_id, _pulse_score(top_overall),
            chosen[0].cluster_id, _pulse_score(chosen[0]),
        )

    return chosen[0].cluster_id


_BIG_PICTURE_HARD_CAP = 4
_HANDS_ON_HARD_CAP = 5
_HANDS_ON_MIN_COUNT = 3
"""Mirrors the eval harness assertion (``evals/run_evals.py`` check_integrity:
"PIPELINE HEALTH: issue.json has N hands_on stories (minimum 3 required)").
If source-diversity caps would starve Hands-On below this floor, the picker
degrades and relaxes caps -- better to ship with a smell loud than fail
the integrity gate."""


def _pick_big_picture(
    blocks: list[tuple[RankedStory, SummaryBlock]],
    available: set[str],
    *,
    clusters_by_id: dict[str, Cluster] | None = None,
    cfg: EditorialConfig | None = None,
    categories_used_this_issue: Counter[str] | None = None,
) -> list[str]:
    """Stories tiered 'big_picture' and not yet placed. Hard cap at 4. The
    Big Picture is the first section after Pulse, per editorial direction.

    Schema v3 (2026-05-30): pool is strictly ``tier == "big_picture"``. No
    audience_tags scavenging, no cross-tier fallback. rank.py routes via
    `_assign_initial_tier` -- if the section is short, the upstream signal
    is "not enough promoted big_picture stories today," not "the picker
    is starving."

    Schema v0.7 (2026-05-31): within the tier pool, stories are ordered
    by the Big-Picture-specific weighted score
    (``score_by_section["big_picture"]``) rather than by the legacy
    aggregate ``score`` / file order. Archived rows without
    ``score_by_section`` fall back to the aggregate.

    Phase 2 (2026-05-30): AUDIENCE-ONLY routing pinned. The picker does
    NOT impose a maturity gate (no freshness / novelty / signal-dimensions
    filter inside this function). Maturity is carried per-story by
    ``SummaryBlock.signal`` (act / try / watch / ...) and surfaces in the
    rendered direction-note prose. EDITORIAL.md "head-section eligibility
    is audience-primary" -- enforced structurally here by gating only on
    ``tier`` (the audience-derived editorial slot).

    Source-diversity caps (2026-05-27): when ``cfg`` is provided, accept
    only stories that don't push any of their sources over the per-section
    cap and don't push their category over the per-issue cap. Big Picture
    has no minimum, so no degraded-mode fill -- under-cap is fine here.
    """
    out: list[str] = []
    sources_in_section: Counter[str] = Counter()
    pool = sorted(
        (sb for sb in blocks if sb[0].tier == "big_picture"),
        key=lambda sb: _section_score_or_aggregate(sb[0], "big_picture"),
        reverse=True,
    )
    for story, _block in pool:
        if story.cluster_id not in available:
            continue
        if cfg is not None:
            cluster = clusters_by_id.get(story.cluster_id) if clusters_by_id else None
            if _would_exceed_section_cap(cluster, sources_in_section, cfg):
                continue
            cat = _cluster_category(cluster, cfg) if cluster is not None else _UNKNOWN_CATEGORY
            if categories_used_this_issue is not None and _would_exceed_category_cap(
                cat, categories_used_this_issue, cfg
            ):
                continue
            if cluster is not None:
                for src in cluster.sources:
                    sources_in_section[src] += 1
            if categories_used_this_issue is not None:
                categories_used_this_issue[cat] += 1
        out.append(story.cluster_id)
        if len(out) >= _BIG_PICTURE_HARD_CAP:
            break
    return out


def _pick_hands_on(
    blocks: list[tuple[RankedStory, SummaryBlock]],
    available: set[str],
    *,
    clusters_by_id: dict[str, Cluster] | None = None,
    cfg: EditorialConfig | None = None,
    categories_used_this_issue: Counter[str] | None = None,
) -> list[str]:
    """Stories tiered 'hands_on' and not yet placed. Hard cap at 5.

    Schema v3 (2026-05-30): pool is strictly ``tier == "hands_on"``. The
    audience_tags / hands_on_utility-fallback heuristic is gone -- rank.py
    routes via `_assign_initial_tier`. The degraded-mode Pass 2 (relax
    caps when the section is below minimum) is gone too: cross-tier
    scavenging is what was producing empty Currents sections (pre-Phase-2:
    On-the-Radar), and the shape post-condition in summarise.py now
    surfaces under-fill instead.

    Phase 2 (2026-05-30): AUDIENCE-ONLY routing pinned. As with Big
    Picture, no maturity / signal-dimensions filter is applied here -- a
    promoted ``hands_on`` story lands regardless of freshness. Maturity is
    carried per-story by ``SummaryBlock.signal`` and the direction-note.

    Schema v0.7 (2026-05-31): within the tier pool, stories are ordered
    by the Hands-On-specific weighted score
    (``score_by_section["hands_on"]``) rather than by the legacy aggregate
    ``score`` / file order. Archived rows without ``score_by_section``
    fall back to the aggregate.

    Source-diversity caps (2026-05-27) still apply within the tier pool:
    skip a candidate that would push a source over the per-section cap or
    a category over the per-issue cap. No minimum enforced here; the issue
    shape post-condition logs a WARNING if Hands-On comes out short.
    """
    out: list[str] = []
    sources_in_section: Counter[str] = Counter()
    pool = sorted(
        (sb for sb in blocks if sb[0].tier == "hands_on"),
        key=lambda sb: _section_score_or_aggregate(sb[0], "hands_on"),
        reverse=True,
    )

    for story, _block in pool:
        if story.cluster_id not in available:
            continue
        if len(out) >= _HANDS_ON_HARD_CAP:
            break
        cluster = clusters_by_id.get(story.cluster_id) if clusters_by_id else None
        cat = (
            _cluster_category(cluster, cfg)
            if (cluster is not None and cfg is not None)
            else _UNKNOWN_CATEGORY
        )
        if cfg is not None:
            if _would_exceed_section_cap(cluster, sources_in_section, cfg):
                continue
            if categories_used_this_issue is not None and _would_exceed_category_cap(
                cat, categories_used_this_issue, cfg
            ):
                continue
        if cluster is not None:
            for src in cluster.sources:
                sources_in_section[src] += 1
        if categories_used_this_issue is not None:
            categories_used_this_issue[cat] += 1
        out.append(story.cluster_id)

    return out


def _pick_currents(
    blocks: list[tuple[RankedStory, SummaryBlock]],
    available: set[str],
    *,
    clusters_by_id: dict[str, Cluster] | None = None,
    cfg: EditorialConfig | None = None,
    categories_used_this_issue: Counter[str] | None = None,
) -> list[str]:
    """Stories tiered 'currents' and not yet placed, in score-desc order.

    Schema v3 (2026-05-30): pool is strictly ``tier == "currents"``
    (renamed from ``on_the_radar`` in Phase 2). Previously this picker
    was a catch-all that scavenged every unplaced story; that masked the
    empty-section shape signal whenever a head-section starved (Big
    Picture / Hands-On would pull from the catch-all pool, leaving
    Currents empty). With tier as authority, an empty Currents means
    rank.py wrote zero ``currents`` stories today -- a real editorial
    signal, not a routing bug.

    Phase 2 (2026-05-30): a HARD ceiling on the section is enforced from
    ``cfg.currents_max_stories`` (config: ``editorial.yaml ->
    section_caps.currents.max_stories``; default 8). The cap binds even
    when no ``cfg`` is passed (older test paths) -- the default-config
    path uses ``DEFAULT_CURRENTS_MAX_STORIES`` so a fork without
    editorial.yaml still sees a bounded Currents section. The upstream
    ``CURRENTS_TIER_SUMMARISE_BUDGET`` is now a safety bound on input
    volume; this cap is the editorial authority.

    Schema v0.7 (2026-05-31): within the tier pool, stories are ordered
    by the Currents-specific weighted score
    (``score_by_section["currents"]``) rather than by the legacy aggregate
    ``score`` / file order. Archived rows without ``score_by_section``
    fall back to the aggregate.

    Source-diversity caps (2026-05-27) still apply: the per-issue category
    cap is a HARD ceiling -- a paper that would push us over the cap is
    dropped from the issue entirely rather than landing here. The
    per-section cap (Layer 1) gates this section independently. No minimum,
    no degraded-mode fill."""
    out: list[str] = []
    sources_in_section: Counter[str] = Counter()
    # Phase 2: read the Currents hard ceiling from the config (default
    # path when cfg is None, matching the fork-friendly default elsewhere).
    max_stories = (
        cfg.currents_max_stories if cfg is not None
        else DEFAULT_CURRENTS_MAX_STORIES
    )
    pool = sorted(
        (sb for sb in blocks if sb[0].tier == "currents"),
        key=lambda sb: _section_score_or_aggregate(sb[0], "currents"),
        reverse=True,
    )
    for story, _block in pool:
        if story.cluster_id not in available:
            continue
        if len(out) >= max_stories:
            break
        if cfg is not None:
            cluster = clusters_by_id.get(story.cluster_id) if clusters_by_id else None
            if _would_exceed_section_cap(cluster, sources_in_section, cfg):
                continue
            cat = _cluster_category(cluster, cfg) if cluster is not None else _UNKNOWN_CATEGORY
            if categories_used_this_issue is not None and _would_exceed_category_cap(
                cat, categories_used_this_issue, cfg
            ):
                continue
            if cluster is not None:
                for src in cluster.sources:
                    sources_in_section[src] += 1
            if categories_used_this_issue is not None:
                categories_used_this_issue[cat] += 1
        out.append(story.cluster_id)
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
    "currents": (
        "Early-signal framing: items thin on sourcing, early in trajectory, "
        "or moving but not yet arrived. EDITORIAL.md makes the Currents "
        "intro_lead MANDATORY -- the section's whole purpose is the "
        "AGGREGATE DIRECTION, and without an intro the section degrades to "
        "an enumeration of early signals. The lead phrase MUST name where "
        "the field is moving today across these items (\"Regulators are "
        "circling agentic payments.\" / \"Open-weights are catching the "
        "frontier on reasoning.\"); the body reads WHY these sit here "
        "rather than higher up, in one short sentence."
    ),
}


# Phase 2 (2026-05-30): section names where ``intro_lead`` is editorially
# mandatory. Currents is the only one today -- EDITORIAL.md puts the
# aggregate-direction lead at the heart of what the section is for. Used
# by ``_populate_section_intro`` to retry once on failure and to log a
# WARNING rather than degrade silently.
_SECTIONS_WITH_MANDATORY_INTRO: set[str] = {"currents"}


def _populate_section_intro(
    section: IssueSection,
    voice_diversity_block: str = "",
) -> None:
    """Generate {intro_lead, intro_body} for a section via one LLM call.
    Mutates the section in place.

    Phase 2 (2026-05-30): for sections in ``_SECTIONS_WITH_MANDATORY_INTRO``
    (Currents today), an LLM failure or parse miss triggers ONE retry. If
    the second attempt also fails, the failure is logged at WARNING so the
    editor / Arman see the smell at ratification -- the issue still ships
    (we'd rather render Currents without an intro than abort the whole
    issue), but the audit trail records that the aggregate-direction lead
    was unavailable today. Other sections continue to degrade silently
    (the template hides missing intros)."""
    if not section.stories:
        return
    hint = _SECTION_INTRO_HINTS.get(section.name)
    if hint is None:
        return
    temperature = float(os.getenv("LLM_TEMPERATURE_SUMMARISE", "0.6"))
    mandatory = section.name in _SECTIONS_WITH_MANDATORY_INTRO

    story_lines: list[str] = []
    for st in section.stories:
        body = st.summary if len(st.summary) <= 280 else st.summary[:280] + "..."
        story_lines.append(f"- HEADLINE: {st.headline}\n  BODY: {body}")
    stories_block = "\n".join(story_lines)

    # Phase 2: Currents intros get an extra prose nudge so the LEAD names
    # the AGGREGATE DIRECTION rather than a generic posture phrase. Other
    # sections retain the existing 2-5-word bold-phrase shape.
    currents_lead_addendum = ""
    if section.name == "currents":
        currents_lead_addendum = (
            "\n- CURRENTS LEAD: name the AGGREGATE DIRECTION today's items "
            "point at, not just a posture. \"For awareness only.\" is "
            "off-voice for Currents; \"Regulators are circling agentic "
            "payments.\" is in voice. The lead must be a directional "
            "claim the reader can hold in their head."
        )

    # v0.13 (2026-06-03): voice-diversity injection sits right above the
    # INSTRUCTIONS block so the LLM reads the section context, the recent
    # constructions to avoid, then the writing rules. Empty string when
    # the caller has nothing to inject -- the prompt collapses to the
    # pre-v0.13 shape.
    voice_diversity_segment = (
        f"\n{voice_diversity_block}\n" if voice_diversity_block else ""
    )

    prompt = f"""\
You are writing the section intro for the "{section.name}" section of
today's AI Vector issue -- a daily AI newsletter with a financial-services
lens, McKinsey-tagline voice, plain English, no em-dashes.

SECTION CONTEXT
{hint}

STORIES IN THIS SECTION
{stories_block}
{voice_diversity_segment}
INSTRUCTIONS
- LEAD: a tight bold phrase (2-5 words, full-stop at the end). It IS
  the section's editorial posture for today.{currents_lead_addendum}
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

    def _attempt_once(use_prompt: str) -> tuple[str, str] | None:
        """One LLM round-trip + parse. Returns (lead, body) on success or
        None on any failure. Side-effect: logs warnings on failure mode."""
        try:
            raw = _llm_call(use_prompt, temperature=temperature, max_tokens=400)
        except Exception:  # noqa: BLE001
            _LOG.warning(
                "summarise: section-intro LLM call failed for %s",
                section.name,
            )
            return None
        payload = _extract_json_object(raw)
        if not isinstance(payload, dict):
            _LOG.warning(
                "summarise: section-intro JSON parse failed for %s",
                section.name,
            )
            return None
        lead_raw = payload.get("lead")
        body_raw = payload.get("body")
        if not isinstance(lead_raw, str) or not isinstance(body_raw, str):
            return None
        lead_s = lead_raw.strip()
        body_s = body_raw.strip()
        if not lead_s or not body_s:
            return None
        return lead_s, body_s

    result = _attempt_once(prompt)
    if result is None and mandatory:
        # Phase 2: Currents intro is editorially required. Try once more
        # with a corrective preface naming the aggregate-direction rule.
        _LOG.info(
            "summarise: mandatory intro for %s missed on first pass -- "
            "retrying once (Phase 2)",
            section.name,
        )
        corrective = (
            "Your previous response was missing or malformed. The Currents "
            "intro is EDITORIALLY MANDATORY: the LEAD must name the "
            "aggregate direction these items point at (a directional "
            "claim, not a posture), and both LEAD and BODY are required. "
            "Return ONLY a JSON object with non-empty string fields "
            "'lead' and 'body'. Original request follows.\n\n"
            + prompt
        )
        result = _attempt_once(corrective)

    if result is None:
        if mandatory:
            _LOG.warning(
                "summarise: MANDATORY intro missing for %s after retry "
                "(Phase 2) -- shipping the section without an intro_lead. "
                "Editor / Arman: review the Currents section for "
                "aggregate-direction lead.",
                section.name,
            )
        return

    lead, body = result
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
