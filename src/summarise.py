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
    DigestBullet,
    Issue,
    IssueSection,
    Item,
    RankedStory,
    SummaryBlock,
)


# ---------------------------------------------------------------------------
# Module constants -- declared at top per the LLM Engineer spec.
# ---------------------------------------------------------------------------

SUMMARISE_PROMPT_VERSION = "v0.23"
"""Pydantic-validated version string. Audit tag:
``summarise-v0.23-2026-08-09``. v0.23 (the 2026-08-09 layout redesign,
wave two -- take v2 + section synthesis + the digest; editor's voice
contracts and Architect's v8 data contracts ratified in wave one):
  - TAKE v2 -- THE COLD OPEN. The take is no longer the italic last line;
    it renders FIRST in the story unit, above the body. ``_TAKE_BLOCK``
    and all four tier take-shape blocks rewritten from relocation
    instructions ("the close MOVES") to authoring instructions for the
    before-the-body slot: reader-world grammatical subject (no deixis
    openers -- This/That/It/These/Those; headline info may be assumed,
    body info may NOT), finite verb by word six, band 12-18 words (aim
    near 12), HARD CAPS 18 words AND 118 characters universal (the
    Currents 22 exception is retired; the caps follow the experience
    designer's adjudication -- two rendered lines at the real metrics
    is the ceiling). Headline-repetition rule added: headline and take
    are co-read; a take derivable from the headline fails (prompt
    teaching + code overlap proxy). The generative mechanism is
    taught by name: THE OLD STATE IS THE CONTEXT, with three routes --
    R1 displacement ("X can now do A instead of B"), R2 named-owner
    consequence, R3 priced tradeoff (new number vs old number in reader
    units). The model returns a ``take_route`` label; the take
    feed-forward now carries route labels, and the write-site reminder
    names the previous same-section route as do-not-repeat (route
    diversity: no route > ~40% of an issue's takes, never two
    consecutive same-route in a section -- issue-level counting is
    review/eval territory per No Token Wasted). "It is now the case
    that" is demoted to necessary-not-sufficient; the COLD-OPEN TEST
    governs (cover the body: can the reader name what changed and
    from-what/to-what?). Thin-sourcing rule (assert at the level the
    sourcing supports; incident-class exemplar) and
    calibration-as-woven-modifier (never a parenthetical) added. NEW
    named anti-pattern: the stacked-modifier garden path (at most one
    reduced relative, never on a coordinated subject, no coordination
    of two post-modified noun phrases, one-pass parse). The six
    ratified exemplars (FCA regulator-text, incident-log nineteen
    entries, incident-class thin-sourcing, stdout/scriptable,
    $2.68-vs-$6.17, one-preprint-scores-the-turn) inlined.
    ``_take_violations`` gains code checks: word cap 18, character
    ceiling 118, deixis-opener, headline restatement (shared overlap
    helper), comma <= 1, semicolon <= 1, no coordinating "and" before
    word 8 (finite-verb-position proxies -- POS tagging is out of
    scope; the judge carries the residue). Cache discipline unchanged:
    tier-independent teaching in the STATIC prefix, tier shapes in the
    variable part, retry-append preserved; golden byte-equality test
    updated to v0.23.
  - SECTION SYNTHESIS (IssueSection v4). ``_populate_section_synthesis``
    replaces the intro pass: one italic paragraph (28-45 words, 2-3
    sentences) written to ``IssueSection.synthesis``; the legacy
    ``intro_lead``/``intro_body`` pair stays None on new issues (the
    model's XOR validator enforces the migration direction; the
    renderer already handles both). Register: quieter than the old
    intro -- the editor's aside, not a headline; NO standalone
    aphoristic opening sentence (the synthesis opens ON the pattern
    with subject + verb, which kills the "X outruns Y" family
    structurally); the pattern across stories, never one story; no two
    sections sharing a thesis (prior syntheses fed forward). Designer
    adjudication: a section with exactly ONE story gets NO synthesis (it
    would duplicate that story's dek). The quiet-day allowance survives
    at n=0 (``_ensure_quiet_day_currents_synthesis`` is the
    deterministic guard). A synthesis may not open with the same word as
    the digest bullet covering its section -- enforced at digest
    validation time (the digest is generated after the syntheses).
  - THE DIGEST (Issue v8, ``DigestBullet``). New issue-level LLM call
    AFTER all stories + takes + syntheses exist (sequential position
    matters: deconfliction is defined against the takes). Structure:
    bullet 1 is ALWAYS The Pulse; bullets 2..n cover sections in layout
    order; a section with < 2 stories gets NO bullet; floor 3 bullets
    else ``digest=None`` entirely (never pad); ceiling 5 (one section
    may split into two bullets only when it genuinely splits into two
    threads). Lead: 3-6 words + full stop, a NAMING (never imperative,
    never a question, no unrecognised artifact names; EDITORIAL.md
    anti-pattern catalogue injected). Sentence: one sentence, 14-22
    words (26 for a semicolon-list of <= 3 clauses). Total budget 100
    words HARD (code check). Deconfliction in code via the shared
    ``_content_overlap_fraction`` helper (0.6 threshold shared with the
    take-restates-body check): no digest sentence vs any take >= 0.6,
    no digest lead vs any synthesis >= 0.6, bullet 1 must not
    paraphrase the Pulse take, no lead opening on its section
    synthesis's first word. Designer adjudication: bullets are
    STORY-ANCHORED (one story's specifics, falsifiable against exactly
    one story) while syntheses are SECTION-ANCHORED -- enforced in
    prompt plus a deterministic trigram check between each lead and its
    section synthesis's first sentence (the observed collision: "Ship
    the plumbing first." verbatim on both surfaces). ``story_ids``
    populated from the generation
    context (the prompt carries the exact id lists; code validates the
    subset). Version under ``prompt_versions["digest"]``
    (DIGEST_PROMPT_VERSION). VERIFY BAR: the pipeline order is
    summarise -> verify (run.py auto-fires verify after summarise), so
    per-story verification does not exist yet when the digest is
    generated; the bar ("a claim the verifier marked unverifiable or
    contradicted may not appear in the digest") is therefore a
    POST-VERIFY code check -- ``digest_verify_violations`` is the
    deterministic helper, applied defensively at generation time (no-op
    while verification is absent) and to be wired into verify.py after
    denormalisation in the wave-three verify/review/revise integration
    task (explicitly deferred).
v0.22 ("the take" -- ratified
2026-08-08): every story returned a ``take`` field alongside headline
/ summary / signal -- one declarative sentence stating the publication's
position, rendered as an italic final line (schema: ``SummaryBlock.take``,
Architect's contract). Changes:
  - ``_TAKE_BLOCK`` (tier-independent teaching: speech act, the "It is
    now the case that [take]" test, 8-16 word budget with HARD 18-word
    cap, banned forms, anti-pattern catalogue) appended to the STATIC
    cache prefix -- identical across tiers, so prefix material per the
    2026-08-08 cache-split discipline. The prefix hash changes; the
    structural properties (concat == joined prompt, one prefix across
    all calls) are re-pinned by the updated golden test.
  - Per-tier take SHAPE in the tier block (variable part,
    ``_TAKE_SHAPE_PER_SECTION`` + ``_PULSE_TAKE_SHAPE``): Pulse -- the
    plain-take close LIFTS OUT of the body into the take field (no new
    sentence); Big Picture -- the first-order consequence sentence
    previously written penultimate moves to the take, the strategic
    question stays the body's last sentence; Currents -- the calibrated
    stake moves from the body's end into the take, both branches
    consequence-form, compressed toward <= 18 words (soft; up to 22
    allowed rather than butchering two-sidedness -- the one section
    exception); Hands-On -- genuinely new: the take is the stake the
    imperative close lacks, and the imperative close stays in the body.
  - Code-side post-generation validation (``_take_violations``): missing
    take, '?', hedge words (may/could/potentially/appears/arguably),
    label prefixes ("So what:", "Bottom line:", "This matters because"),
    body restatement (substring, or >= 60% content-word overlap with any
    single body sentence), and the word caps. One corrective retry
    (cache-safe: appended to the variable part); still-violating drafts
    ship with a WARNING (soft fail; review flags them). Un-checkable
    bans (leading imperative, second person, universalisms) live in the
    prompt + reviewer.
  - Feed-forward extends to takes across ALL tiers including
    big_picture (``prior_takes`` -> TAKES ALREADY WRITTEN block; takes
    feed forward directly as a field, no sentence extraction), and the
    cross-day voice-diversity lookback now also collects past takes.
  - The body keeps its 30-60 word cap unchanged; the take has its OWN
    budget in its own field.
v0.21.3 (structural fix for the
thrice-failed Hands-On close-mould veto; root cause finally
identified): close-variety is a CROSS-STORY property, but stories are
summarised independently -- the model writing story 3 has never seen
stories 1-2's closes, so "consecutive stories must never share the
scaffold" was physically unfollowable however it was phrased.
Evidence: three regens under three different prompt emphases (v0.21
variety list, v0.21.1 reorder, v0.21.2 write-site reminder) all
produced 3/3 "before X" tails in Hands-On, while every PER-story rule
(BP turn-type, trigger support) bound fine. Fix: feed-forward close
context -- close-variety is cross-story; per-story prompting cannot
coordinate it -- feed prior section closes into each subsequent story
prompt (durable home for this property class is the future polish
stage). Mechanics: the per-story loop in ``summarise()`` was already
sequential; as each hands_on / currents summary is accepted, its
closing sentence (``_extract_closing_sentence``) is collected per
tier, and the NEXT story in the SAME tier gets a "CLOSES ALREADY
WRITTEN IN THIS SECTION" block (``_render_prior_closes_block``,
closing sentences only, token-lean) injected immediately before the
close reminder tail; the v0.21.2 HANDS-ON CLOSE (FINAL REMINDER) now
references the list ("vary against the closes listed above") when it
is present. big_picture is NOT fed forward (its strategic-question
turn-type bound fine per-story); the Pulse override never is.
v0.21.2 (gate re-run on v0.21.1,
condition-2 finding: Hands-On close-moulds did NOT move -- 3/3 closes
still "[imperative on artefact] + before + [milestone]"; reordering
the SURFACE VARIETY list was not enough, while the write-site FINAL
REMINDER mechanism DID bind for Big Picture (4/4 strategic questions)
and Pulse):
  - the close_reminder_tail mechanism extended with a HANDS-ON CLOSE
    (FINAL REMINDER) branch for tier == "hands_on", injected
    immediately before the JSON schema: imperative with a named
    artefact and a source-supported trigger; the "... before
    [milestone]" scaffold capped at ~2/section, never on consecutive
    stories; trigger-first ("Before X, do Y") counts as the SAME
    scaffold -- vary the FRAME (condition-first, bare imperative with
    stakes), not just word order.
  - invented-trigger rule (the v0.21.1 regen fabricated "before your
    token budget resets" for a one-time price change): the TRIGGER in
    a close is a factual claim -- an event the source supports or a
    generic practitioner milestone, never an invented source-specific
    cadence.
v0.21.1 (voice-eval gate VETO surgery
on v0.21, 2026-07-04 nightly run; exactly the three veto grounds, all
else preserved verbatim):
  - Big Picture closes collapsed to imperatives (3/4 against a 3/3
    strategic-question baseline; the model reproduced the closing-shape
    block's own DON'T example nearly verbatim, twice -- attractive
    concrete phrasings inside negative examples get imitated regardless
    of the DON'T framing, same mechanism as the v0.17 trust-hedge
    decoration). Structural fix, not more emphasis: (a) the quotable
    imperative inside the negative example REMOVED -- the violation is
    now described abstractly (closing on an instruction to the reader
    instead of a question) with a positive question-mark cue; the block
    still ends on strategic-question examples (recency wins); (b) the
    SIGNAL "discuss" example's quotable direct-speech imperative
    ("raise this at your next architecture review") rewritten abstract
    -- same attractor; (c) the BP turn-type RESTATED at the per-story
    instruction site as a FINAL REMINDER tail (same recency mechanism
    as the v0.12 Pulse plain-take tail), because the generic "close
    tied to a SPECIFIC decision" body rule otherwise wins recency.
  - "before X" close-mould density (5/8 stories, Hands-On 3/3
    consecutive, against the prompt's own ~2/section cap and
    no-consecutive rule; first-listed realisations get over-sampled):
    the Hands-On SURFACE VARIETY block now LEADS with the alternative
    realisations and lists the classic "[do X] before [milestone]"
    mould LAST with its cap stated inline; the condition-first example
    no longer carries a "before [milestone]" tail.
  - Sharper-than-source (the Pulse upgraded "listed first among five
    values", supportable, to "authority sits above safety",
    unsupported): one line added to the REGENERATION INVARIANT --
    sharper is the only acceptable direction AND sharper must remain
    source-supported; a vivid claim the source doesn't state is a
    factual error, not sharpness (composes with, and references, the
    HONESTY + TRUST HEDGES rules).
v0.21 (regeneration quality: the
headline-opener contract + prose sharpness under constraint + the
empty-Currents quiet-day intro; READING_EXPERIENCE.md §3 "The
headline-opener contract" + "Prose sharpness under constraint", R-9 +
R-10; the three BETTER/WORSE calibration pairs ratified by Arman
2026-07-04):
  - HEADLINE RULES rule 1 fused with the OPENER LADDER so the
    recognition rule and the article policy cannot fight (R-9). Rungs:
    (1) recognised anchor first, including VENUE-DERIVED attribution
    (a microsoft.com/research post is Microsoft's work; a first-party
    vendor blog is the vendor's; third-party coverage does NOT
    transfer its name); a regeneration must NEVER trade a recognised
    anchor for an abstract benefit ("your coding agent" for "Claude
    Code" is a regression). (2) Plural class-finding when the finding
    holds for the class. (3) Single unnamed artifact keeps "A/An" --
    NEVER delete the article from a full-sentence headline (wire
    headlinese is off-voice) -- with every word between article and
    verb identifying the artifact; "A new + generic noun"
    (framework/method/tool/benchmark/system) BANNED as an opener.
    (4) "The" only as a semantic operator. The old "A new benchmark
    finds..." in-voice example replaced (it taught the banned opener);
    the "'A new tool' is acceptable" escape hatch now requires
    identifying modifiers.
  - Rule 3's colon ban amended: a GENRE LABEL on a recognised anchor
    ("Claude Code tip: ...") is the ONE licensed exception -- only
    when a declarative would oversell a small practical move as news;
    at most one labelled headline per issue; tiny label vocabulary.
  - Density preference: at most two "A/An"-opening headlines per
    issue, stated in-prompt as a preference only. No LLM self-counting
    machinery; deterministic counting belongs in review/eval code per
    No Token Wasted.
  - PROSE SHARPNESS UNDER CONSTRAINT block added to the body teaching
    (anti-flattening contract, R-10): direct over reported speech;
    quote the replaced thing verbatim on stop-X-do-Y stories; keep
    contrast pairs where the insight IS a contrast; recognised names
    and hard numbers survive regeneration. REGENERATION INVARIANT: a
    rewrite must not net-lose verbatim quotes, recognised names, hard
    numbers, or contrast beats relative to the draft it replaces --
    sharper is the only acceptable direction.
  - Empty-Currents quiet-day intro (template-integrity fix; the null
    intros defect shipped three times): when Currents has zero
    stories, the section-intro pass now still runs and MUST emit
    intro_lead + intro_body acknowledging the quiet day in the
    Currents register (wording varied day to day), and a DETERMINISTIC
    code fallback (``_ensure_quiet_day_currents_intro``) injects a
    default quiet-day intro when the intros still land null/empty, so
    the template contract can never break. The fallback is code, not
    another LLM call, per No Token Wasted.
v0.20 (third gate: informative vs class
default + affirmative-presence obligation (reader-needs study, R-8)):
  - The source-class attribution in the body ("an arXiv preprint",
    "Anthropic's release notes", "a Reddit thread") carries the
    reader's default calibration for free; an explicit trust flag earns
    its words ONLY on DEVIATION from that class default (independent
    replication present on a preprint; a competitor ran the vendor's
    benchmark; non-obvious scoring method; claim far heavier than its
    evidence). Restating the default ("a preprint from a single
    research team", "single-source" on a podcast interview) is banned;
    the fix is DELETE the flag and let the class name in the body carry
    it. Study-validated (five-persona panel + verifier-verdict archive
    evidence, READING_EXPERIENCE.md §3 + R-8; ratified by Arman
    2026-07-04).
    Changes: TRUST HEDGES block composes THREE gates (source-supported
    AND presence-form AND informative-vs-default) with the compact
    deviation taxonomy (preprint / vendor blog-release notes /
    named-author experiment / forum thread); "one research team's
    analysis" RECLASSIFIED from positive to negative example (restates
    the preprint default); per-story trust-flag probe rewritten
    deviation-shaped; claim-magnitude routing added (claim-vs-evidence
    mismatch belongs in the Currents stake / Big Picture question, not
    the flag, except "thin sourcing, one Reddit thread" on a big claim;
    say it once, flag or stake, never both); AFFIRMATIVE-PRESENCE
    OBLIGATION added to the HONESTY rule and trust-flag teaching (when
    the source states an artifact IS available, the summary MUST say so
    affirmatively -- "dataset and tooling are public"); rewrite-move
    examples updated so no fix manufactures a default-restating flag;
    Currents WHY constrained to presence-form AND non-default.
v0.19 (trust flags are presence-form;
Arman's direction via Experience Designer, R-8):
  - Trust flags must characterise the evidence that EXISTS
    (presence-characterisation), never inventory what is missing
    (READING_EXPERIENCE.md §3 "Presence, not absence" + R-8; ratified
    by Arman 2026-07-04). Absence-statements ("no code yet", "no
    independent replication yet", "not yet peer-reviewed") are banned
    forms: naming one absence from an unbounded set reads as
    filler-hedging, the trailing "yet" adds a promise the source never
    made, and a global negative asserted from a local excerpt is
    usually unverifiable (the #22 Pulse "No code is linked" case).
    Changes: house-style trust-flag teaching rewritten with
    presence-form examples + the ban; the strong worked example's "No
    code yet" flag rewritten presence-form; HONESTY rule no longer
    suggests stating what is unknown (describe what IS stated, stay
    silent about the rest); TRUST HEDGES block drops absence-forms from
    the approved list and states the two-gate composition
    (source-supported AND presence-form); negative examples from real
    released issues added; Currents WHY constrained to presence-form
    (the "no action yet" direction-note recommendation is a different
    speech act and stays).
v0.18 (reading-experience: close-form
grammar diversification, turn-types unchanged):
  - Experience Designer's #1 finding (READING_EXPERIENCE.md R-1 + R-2):
    the Hands-On and Currents CLOSE TURN-TYPES are correct, but their
    surface grammar has collapsed into single moulds -- Hands-On ~95%
    "[imperative] ... before [milestone]"; Currents nearly always "If X
    replicates, Y; if it doesn't, Z" -- so the reader's eye predicts the
    scaffold and skips the most actionable line. Fix adds a SURFACE
    VARIETY instruction to each of the two closing-shape blocks
    (``_CLOSING_SHAPE_PER_SECTION['hands_on'|'currents']``): 3-4
    alternative grammatical realisations of each turn-type, the old mould
    demoted to one option among several (~2 per section), plus a
    within-issue line barring consecutive stories from sharing a mould.
    The turn-type CONTRACT is unchanged (Hands-On still closes on a
    specific imperative action; Currents still on a two-sided calibrated
    stake with real stakes on both branches) -- src/review.py's shape
    checks are untouched. Illustrative phrasings are voice-adjacent;
    Editor may tune them.
v0.17 (Trust hedges are factual claims):
  - New HONESTY rule + house-style guardrail: the verifier caught the
    summariser INVENTING trust hedges ("self-reported", "no independent
    replication") the source never supports -- three times in three days
    (2026-07-02 crypto-risk; 2026-07-04 Office-docs, both drafts;
    2026-07-04 Reverse-engineering-Claude-Code Pulse). Root cause: the
    "THREE THINGS THAT MUST SURVIVE" block rewards the trust flag as a
    style move ("judgement is the product") with no constraint that the
    hedge be source-supported, so the model applied it decoratively.
    Fix asserts trust hedges are FACTUAL CLAIMS about sourcing -- allowed
    only when the source explicitly supports the characterisation -- with
    three negative examples drawn from the real failures. Added next to
    the HONESTY rule (instructions block) and the trust-flag teaching
    (house-style block) so the reward and the constraint co-locate.
v0.15 (Headlines that land + wider excerpt cap):
  - Three new HEADLINE RULES inlined into every per-story summarise prompt
    (head-tier and currents-tier alike; not Pulse-specific). (1) Name the
    artifact when the source names one; do not invent one when it doesn't.
    (2) Preserve distinctions the source makes (existing repo vs new
    paper that reinterprets it; existing benchmark vs new technique
    tested against it). (3) Headlines land in one beat -- concrete
    subject in the first three words, specific objects not abstract
    nouns, one core idea (no colons or em-dashes joining two), no jargon
    that needs a dictionary lookup, imperative only when the artifact is
    already named. Fixes the 2026-06-02 colleague.skill story where the
    headline "Capture a departing engineer's judgment as a versioned,
    editable file" dropped both the COLLEAGUE.SKILL and dot-skill names
    and read as a puzzle. EDITORIAL.md "Headlines that land" subsection
    is the prose source of truth; this prompt is the operational mirror.
  - _SOURCE_EXCERPT_MAX_WORDS raised from 500 to 1000. Substantive
    articles (formal contracts, install steps, gallery numbers, key
    distinctions between an existing tool and a new paper) often sit
    deeper than 500 words; the headline rules need that material to be
    in the prompt window. Cost impact: ~37,500 extra input tokens per
    issue at top-3 items x ~25 clusters x 500 extra words; roughly 5c
    per issue at current Bedrock pricing.
v0.14 was never shipped externally (skipped to keep the version-bump
audit trail aligned with the externally-visible feature shift).
v0.13 (voice diversity injection):
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

DIGEST_PROMPT_VERSION = "v1.0"
"""Audit tag: ``digest-v1.0-2026-08-09``. First version of the issue-level
digest prompt ("The 30-second read" -- Issue v8 ``digest`` field, 2026-08-09
layout redesign). Recorded under ``Issue.prompt_versions["digest"]`` only
when a digest was actually produced; absent when the digest degraded to
``None`` (floor not met, LLM failure, or validation failure after retry)."""

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
  2. THE TRUST FLAG when warranted -- and it is warranted ONLY on
     DEVIATION. Naming the source class in the body ("an arXiv
     preprint", "Anthropic's release notes", "a Reddit thread") already
     tells the reader what to assume (preprint = single team, not
     peer-reviewed, authors' own scoring); the DEFAULT needs no flag.
     An explicit flag earns its words only when the evidence deviates
     from its class default: "a second lab replicated it," "the vendor
     benchmarked its competitor's model," "scored by an ensemble of
     LLM judges," "thin sourcing, one Reddit thread" under a big claim.
     Never drop a deviation flag -- judgement is the product. THREE
     GATES, all mandatory: (a) SOURCE-SUPPORTED -- a trust flag is a
     FACTUAL CLAIM about the sourcing; assert it ONLY when the source
     explicitly supports it, never decorate. (b) PRESENCE-FORM -- it
     characterises the evidence that EXISTS; "no code yet", "no
     independent replication yet", "not yet peer-reviewed" are BANNED
     forms. (c) NON-DEFAULT -- restating the class default ("a preprint
     from a single research team") is banned; DELETE the flag and let
     the class name in the body carry it. AND the positive duty: when
     the source states an artifact IS available (code, weights,
     dataset, tooling), SAY SO affirmatively ("dataset and tooling are
     public") -- acknowledging presence is obligatory; only
     inventorying absence is banned.
  3. A RELEVANCE LINE tied to a DECISION, not a department or group.
       Group (weak):     "useful for teams managing vendor risk"
       Decision (strong): "useful when you're renegotiating a closed-
                          model contract"

WHEN CONSTRAINTS COLLIDE (thin item, won't all fit), resolve in this
order. Drop from the bottom, never the top.

  1. Trust flag (when a deviation warrants one) -- never sacrificed.
     Judgement is the product.
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
           the handoff was safe? Safe Bilevel Delegation (an arXiv
           preprint, via LLMQuant) scores that moment 0-to-1 at runtime,
           gating execution when confidence drops; auditable for
           model-risk teams. Pressure-test it in your next architecture
           review, don't ship it."
           -- 50 words. Sharp opener. The class name in the body ("an
           arXiv preprint") carries the calibration; NO separate trust
           flag ("the scoring is the authors' own" would restate the
           preprint default -- gate 3). Decision-tied close
           ("pressure-test in your next architecture review, don't
           ship it").

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

PROSE SHARPNESS UNDER CONSTRAINT -- the anti-flattening contract

The rules above say what the prose must NOT do. These four moves are
what it MUST KEEP DOING. Vividness IS clarity; compliance bought with
flat prose is a regression, not an improvement.

  1. DIRECT OVER REPORTED SPEECH. "The Claude Code team's tip: give
     [the agent] a goal, not a rulebook" -- not "The Claude Code team
     recommends telling your agent to judge when...". Indirect
     discourse ("recommends / suggests / notes that") adds a hedging
     frame the reader must unwrap; the direct form hands over the
     insight itself.
  2. QUOTE THE REPLACED THING VERBATIM. When a story's point is STOP
     doing X, do Y, X appears in quotation marks: 'Instead of "run
     tests only for large features," tell it to use its own
     judgement.' The before/after lands in one glance; the abstraction
     ("rather than encoding rules") makes the reader reconstruct the
     example the writer already had.
  3. KEEP CONTRAST PAIRS WHERE THE INSIGHT IS A CONTRAST. "A goal, not
     a rulebook" / "a first-order constraint, not a safety layer
     bolted on after the fact" / "an audit problem, not an
     architectural one". The X-not-Y frame is the fastest encoding of
     a reframe, and it is the beat readers quote when they forward the
     issue. (The headline "X, NOT Y" tic rule still applies; this is
     about the BODY, where a genuine reframe earns the frame.)
  4. RECOGNISED NAMES AND HARD NUMBERS SURVIVE. "Fable", "OpenClaw and
     Hermes Agent", "58.8 to 82.3" stay in the prose; "your agent",
     "two open-source agents", and a vanished number are flattening,
     not compression. (Distinct from the trust-flag gates: those
     govern HEDGES; names and numbers are CONTENT.)

REGENERATION INVARIANT: a rewrite may restructure, but it must not
net-lose verbatim quotes, recognised names, hard numbers, or contrast
beats relative to the draft it replaces. Sharper is the only
acceptable direction, AND sharper must remain SOURCE-SUPPORTED: a
vivid claim the source doesn't state is a factual error, not
sharpness (the HONESTY + TRUST HEDGES rules govern).

CALIBRATION (prose sharpness -- ratified by Arman, 2026-07-04; same
story, two renders):
  BETTER: "The Claude Code team's tip: give [the agent] a goal, not a
          rulebook"
  WORSE:  "The Claude Code team recommends telling your agent to judge
          when..." -- reported speech wraps the insight in a frame; the
          verbatim quote and the contrast pair vanish.

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

# v0.15 (2026-06-03): HEADLINE RULES that apply to EVERY story (head-tier
# AND currents-tier; not Pulse-specific). Source of truth is EDITORIAL.md
# "Headlines that land" subsection; this block is the operational mirror
# the LLM reads. The three rules correspond 1:1 with the three editorial
# rules in that subsection. Triggered by a 2026-06-02 reader-decode
# failure on the colleague.skill story where the headline dropped both
# the COLLEAGUE.SKILL paper name and the existing dot-skill repo name.
#
# v0.21 (2026-07-04): rule 1 fused with the OPENER LADDER from
# READING_EXPERIENCE.md §3 "The headline-opener contract" (R-9) so the
# recognition rule and the article policy cannot fight. The ladder's
# density caps (<= 2 "A/An" openers, <= 1 genre-labelled headline per
# issue) are stated in-prompt as PREFERENCES only -- the per-story prompt
# cannot count across the issue, and we do not build LLM self-counting
# machinery. Deterministic per-issue counting belongs in review/eval
# code, per No Token Wasted.
_HEADLINE_RULES_BLOCK = """\
HEADLINE RULES (apply to every story -- head-tier and currents-tier alike)

1. RECOGNITION DECIDES NAMING; THE OPENER LADDER DECIDES THE SUBJECT.
   Names belong in the headline ONLY when a senior practitioner reads
   them and immediately connects to a mental model they already have.
   Widely-recognised names earn the headline: OpenAI, Anthropic, Claude,
   Claude Code, GPT-5, Llama, vLLM, EU AI Act, Nvidia, Microsoft,
   PyTorch, etc. New tools, new papers, new benchmarks, new methods, new
   datasets do NOT earn the headline -- their names do not yet trigger
   recognition. Examples that should be DESCRIBED in the headline (with
   the name appearing in the first one or two sentences of the summary):
   JudgmentBench, COLLEAGUE.SKILL, dot-skill, ITS-Mina, FinGuard.

   The test: would a senior AI practitioner reading the headline alone
   recognise this name without further context? If no, the name does not
   belong in the headline -- describe the artifact instead.

   Why: a name in a headline that does not trigger recognition is noise.
   It is a string of letters the reader has to mentally park while they
   figure out what the headline is about. The summary is where new names
   earn their place -- by then the reader has the mental model to attach
   the name to.

   THE OPENER LADDER. The first three words are the reader's hottest
   fixation window. Choose the headline subject from the HIGHEST rung
   available:

   RUNG 1 -- RECOGNISED ANCHOR FIRST, AND NEVER TRADE IT AWAY. If a
   recognised tool, vendor, lab, or regulation anchors the story, it
   opens the headline ("Claude Code tip: ...", "Microsoft trains...",
   "Vercel redesigns..."). Attribution derivable from the SOURCE VENUE
   counts as recognition material: a post on microsoft.com/research is
   Microsoft's work; a first-party vendor blog is the vendor's.
   Third-party coverage and aggregators do NOT transfer their name to
   the work they cover. When rewriting or regenerating a headline,
   NEVER drop an existing recognised anchor for an abstract benefit --
   swapping "Claude Code" for "your coding agent" is a regression, not
   a polish.

   RUNG 2 -- GENERAL FINDING, PLURAL SUBJECT. If no recognised name and
   the finding holds for the class, state it as the class:
   "Character-level tricks bypass safety filters in most open models
   tested." Plural subjects need no article and remain full sentences.

   RUNG 3 -- SINGLE UNNAMED ARTIFACT: "A/An" + IDENTIFYING MODIFIERS.
   Keep the article. NEVER delete an article from a full-sentence
   headline -- "Small open safety classifier beats..." is wire
   headlinese, off-voice. But every word between the article and the
   verb must IDENTIFY the artifact: "A 9-millisecond CPU model", "A
   CUDA-free kernel", "A character-level trick" are in voice. BANNED
   OPENER: "A new" + a generic noun (framework / method / tool /
   benchmark / system) -- "new" is entailed by appearing in today's
   issue, and the empty subject wastes the fixation window.

   RUNG 4 -- "THE" ONLY AS A SEMANTIC OPERATOR: superlative ("The best
   AI systems score under 60%..."), unique referent ("The UK financial
   regulator..."), definite scope, or a restrictive relative ("The
   benchmark reproducibility guide that most evals quietly violate").
   Never as decoration.

   DENSITY PREFERENCE: an issue carries at most two "A/An"-opening
   headlines. Before settling on rung 3, check whether rung 1 or 2 is
   available -- it usually is.

   Examples:
   - In voice (rung 1, known name): "Anthropic ships a more honest
     flagship model"
   - In voice (rung 1, venue-derived anchor): "Microsoft trains agent
     instruction files instead of rewriting them by hand"
   - In voice (rung 3, unknown name, identifying modifiers): "A
     Shanghai AI Lab paper turns a departing engineer's traces into an
     editable skill file"
   - In voice (rung 3): "A pairwise-scoring benchmark finds
     head-to-head comparison beats rubrics"
   - Off voice: "COLLEAGUE.SKILL packages a departing engineer's
     judgment as a skill file" -- reader does not know what
     COLLEAGUE.SKILL is
   - Off voice (banned opener): "A new benchmark finds pairwise scoring
     beats rubrics for judging language-model output" -- "A new
     benchmark" is an empty subject; identify the artifact instead

   CALIBRATION PAIRS (ratified by Arman, 2026-07-04 -- same stories,
   two renders):
   - BETTER: "Claude Code tip: let the agent decide when to write
     tests" / WORSE: "Let your coding agent choose its own tools and
     save tokens" -- the regeneration dropped the recognised anchor,
     oversold the genre, and displaced the actual insight with an
     abstract benefit; an unanchored imperative opener is also
     off-voice under rule 3.
   - BETTER: "Microsoft trains agent instruction files instead of
     rewriting them by hand" / WORSE: "SkillOpt trains an agent's
     instruction file without touching the model" -- SkillOpt is
     unrecognised, a parked string in position 1; the venue supplies
     the recognised actor; the artifact name belongs in the body's
     first sentences.

   If the source does not name a thing at all, do NOT invent one -- and
   do NOT fall back to a bare "A new tool" or "a new paper" either.
   Pick identifying modifiers from what the source DOES state ("A
   CUDA-free kernel", "A 9-millisecond CPU model"). The artifact may
   stay unnamed; it never stays unidentified.

2. PRESERVE DISTINCTIONS IN THE SOURCE. If the source distinguishes
   between an existing artifact and a new one (an existing repo vs a new
   paper that reinterprets it; an existing benchmark vs a new technique
   tested against it; an existing model vs a fine-tune of it), the
   summary preserves that distinction. Naming both is fine; using them
   interchangeably is not -- the reader loses what is new.

3. HEADLINES LAND IN ONE BEAT, NOT TWO. A reader should not have to back
   up and re-read.
     - Concrete subject in the first three words (actor, vendor, paper,
       lab, or artifact).
     - Specific objects, not abstract nouns ("an editable file format
       for engineer expertise" beats "a versioned, editable container
       for tacit knowledge").
     - One core idea. If the headline needs a colon, semicolon, or
       em-dash to hold two ideas together, it is two headlines -- pick
       the more important one. ONE licensed exception: a GENRE LABEL on
       a recognised anchor ("Claude Code tip: let the agent decide when
       to write tests") -- that colon declares the genre, it does not
       join a second idea. Use it ONLY when the item is a small
       practical move a declarative headline would oversell as news,
       ONLY with a recognised anchor, and at most ONE labelled headline
       per issue. The label vocabulary stays tiny ("tip" today); do not
       coin new labels casually.
     - No words that need an editorial dictionary lookup. "Versioned"
       -> "tracked"; "operationalise" -> "make routine"; "primitives"
       -> "building blocks"; "instrumentation" -> "monitoring".
     - Imperative voice ("Do X") is allowed ONLY when the artifact is
       already named OR widely known. For a newly-introduced artifact,
       declarative is clearer: "A new tool packages X as Y" lands; "Do X
       as Y" obscures.

4. NO INLINE VENUE CITATIONS IN THE BODY. Attribution is the source URL's
   job, not the prose's. Do NOT include parenthetical venue citations
   like "(arXiv, updated 2 June 2026)", "(per arXiv 2603.12345)",
   "(Substack post, 29 May)", "(via AlphaSignal)", or "(MIT Tech Review)"
   in the summary body. The reader can see where the link goes from the
   source URL at the bottom of the story. Inline citations sound
   bureaucratic, cost signal density (a 60-word summary cannot spare
   three words on a venue tag), and read as hedge-y academic register
   that does not match AI Vector's voice.

   Exceptions (rare, judge carefully):
     - The venue IS the news. "The FDA approved X" -- FDA naming matters.
     - The venue's recency IS the news. "A paper updated last week
       reverses an earlier claim" -- the update is the editorial point.
     - The channel itself is editorial signal. "Posted to an internal
       engineering blog rather than published" -- the channel choice is
       part of the story.

   Otherwise, drop the citation. Trust the source URL to do the work.
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
        "sourcing, early trajectory, single benchmark. The WHY must be\n"
        "PRESENCE-FORM AND NON-DEFAULT (\"Vercel's own eve framework\",\n"
        "not \"no independent validation yet\", and not the empty \"a\n"
        "single vendor\" when the body already names the vendor). A\n"
        "Currents story\n"
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
        "Closing on an instruction to the reader instead of a question\n"
        "FAILS this shape; the question alone is the landing, and the\n"
        "final sentence ends in a question mark. NOT a rhetorical\n"
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
        "    the cost-per-token math justifies the migration.\"\n"
        "SURFACE VARIETY (the imperative turn-type is FIXED; its grammar is\n"
        "NOT). Vary the construction story to story:\n"
        "  - bare imperative with stakes: \"Pull the weights and run your\n"
        "    own guardrail eval; the vendor's F1 numbers won't transfer.\"\n"
        "  - trigger-first: \"Before your next eval cycle, install it and\n"
        "    diff the guardrail scores against your current stack.\"\n"
        "  - condition-first: \"Already on the older CLI? Upgrade this\n"
        "    week; the token savings compound on every batch job.\"\n"
        "  - the classic \"[do X] before [milestone]\" -- at most ~2 per\n"
        "    section across the issue, never the reflex.\n"
        "Within an issue, consecutive Hands-On stories must NOT share the\n"
        "same closing mould; vary the grammar story to story."
    ),
    "currents": (
        "CLOSING SHAPE -- CALIBRATED STAKE LIVES IN THE TAKE (v0.23)\n"
        "The stake-carrying judgement belongs to the \"take\" field (the\n"
        "cold open ABOVE the body -- see the TAKE SHAPE below). Do NOT\n"
        "also write it into the body. The body's final sentence instead\n"
        "lands on the maturity signal: what EXISTS and what it is worth\n"
        "today (presence-form, per the Currents WHY rule above) -- e.g.\n"
        "\"a single-vendor benchmark, but the harness is public\". The\n"
        "body close stays hedged-arrival in register; the position was\n"
        "already stated up top, so the body never re-announces it.\n"
        "Within an issue, consecutive Currents items must NOT share the\n"
        "same closing mould; vary the grammar item to item."
    ),
}


# v0.23 (2026-08-09): per-tier TAKE SHAPE -- the tier-specific half of the
# take contract, rewritten for the COLD-OPEN slot (the take renders FIRST
# in the story unit, above the body). The tier-independent rules (slot
# grammar, routes, banned forms, anti-patterns) live in _TAKE_BLOCK inside
# the STATIC prefix; these blocks are variable-part material because they
# differ per tier.
_TAKE_SHAPE_PER_SECTION: dict[str, str] = {
    "big_picture": (
        "TAKE SHAPE (BIG PICTURE) -- THE CONSEQUENCE, COLD\n"
        "The take opens the story unit with the first-order consequence\n"
        "for named actors: whose obligation, scope, or calculus shifted,\n"
        "and from what to what. Route R2 (named-owner consequence) is the\n"
        "natural fit; R1 or R3 when the story genuinely is a displacement\n"
        "or a price move. The BODY follows the take: it builds the\n"
        "context and still ENDS on the STRATEGIC QUESTION (the closing\n"
        "shape above). The take asserts; the close hands the reader the\n"
        "open decision. SAID ONCE: the consequence lives in the take and\n"
        "ONLY in the take -- the body must not carry a residual\n"
        "consequence or so-what sentence where the take now holds it.\n"
        "Example take: \"Model-risk sign-off now covers agent plans, not\n"
        "just model outputs.\""
    ),
    "hands_on": (
        "TAKE SHAPE (HANDS-ON) -- THE STAKE, COLD\n"
        "The take opens the story unit with the stake: what became\n"
        "possible, cheaper, or safer for the practitioner, with the old\n"
        "state in view (R1 displacement and R3 priced tradeoff are the\n"
        "natural routes). The subject is the workflow or artefact class\n"
        "the reader owns -- name the repo itself only when recognised.\n"
        "Never an instruction and never a verb telling the reader to do\n"
        "something: the IMPERATIVE CLOSE stays the body's last sentence,\n"
        "exactly as the closing shape above says. SAID ONCE: the stake\n"
        "lives in the take; the imperative close prescribes the action\n"
        "and must NOT restate why it matters.\n"
        "Example take: \"Agent reasoning now lands on stderr, leaving\n"
        "stdout clean enough to pipe into the next tool.\""
    ),
    "currents": (
        "TAKE SHAPE (CURRENTS) -- THE CALIBRATED POSITION, COLD\n"
        "The take opens the story unit by pricing the signal at exactly\n"
        "the level the sourcing supports: what is now documented, first\n"
        "public, or converging -- and from what prior state. The THIN\n"
        "SOURCING rule bites hardest here: assert the class, never\n"
        "harden the anecdote. Calibration rides inside the sentence as a\n"
        "woven modifier (\"on one preprint's benchmark\", \"with the\n"
        "weights promised\"), never a parenthetical. Where the position\n"
        "genuinely is two-sided, state both branches as consequences\n"
        "within the same caps -- and never with the \"Replicated, X;\n"
        "unreplicated, Y\" frame (named anti-pattern). SAID ONCE: the\n"
        "stake lives in the take; the body still closes on the maturity\n"
        "signal per the closing shape above, never a residual stake.\n"
        "Example take: \"A single documented wipe moves destructive agent\n"
        "commands from hypothetical risk to recorded precedent.\""
    ),
}


# v0.23: take shape under the Pulse override -- the cold open of the whole
# issue. Paired with the amended _PULSE_CLOSING_SHAPE below.
_PULSE_TAKE_SHAPE = (
    "TAKE SHAPE (PULSE) -- THE DAY'S POSITION, COLD\n"
    "The Pulse take is the first editorial sentence of the whole issue:\n"
    "the day's position, stated against the state it displaces. Any of\n"
    "the three routes may carry it -- pick the one the story actually\n"
    "runs on and return its label. The BODY follows the take and must\n"
    "land cleanly WITHOUT it: the day's direction in plain editorial\n"
    "prose, satisfying the PULSE VOICE rules above on its own, ending on\n"
    "the direction -- never restating the take, never re-announcing the\n"
    "position the unit already opened with. SAID ONCE: the day's so-what\n"
    "lives in the take; the body ends on direction, not on judgement.\n"
    "Example take: \"Compliance teams can now cite the regulator's own\n"
    "text instead of somebody's scrape of it.\""
)

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
    "PULSE CLOSING SHAPE -- PLAIN TAKE, COLD OPEN (v0.23)\n"
    "If this story is elevated to The Pulse, the publication's plain-take\n"
    "judgement is the \"take\" FIELD, rendered ABOVE the body as the cold\n"
    "open of the whole issue -- one declarative sentence naming what is\n"
    "TRUE NOW against the old state. NEVER a question (\"Who owns...?\");\n"
    "NEVER a prescription (\"Test this against X\"); the last character\n"
    "is a FULL STOP. The BODY then ends on the day's direction in the\n"
    "Pulse voice, WITHOUT restating the take. This overrides BOTH the\n"
    "generic body-close rule and the Big-Picture STRATEGIC QUESTION\n"
    "shape above.\n"
    "Example takes (use as calibration, not templates):\n"
    "  - \"Domain-grounded filtering now anchors the credible safety\n"
    "    story, where open-web filters anchored it before.\"\n"
    "  - \"Lab honesty is now the strategic variable; model swaps are\n"
    "    just procurement.\""
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


# v0.23 (2026-08-09): THE TAKE -- tier-independent teaching, rewritten for
# the COLD-OPEN slot. This block is identical across every tier and the
# Pulse override, so it is STATIC PREFIX material (cache discipline). The
# per-tier take SHAPE lives in the tier block (variable part) -- see
# _TAKE_SHAPE_PER_SECTION below.
_TAKE_BLOCK = """\
THE TAKE -- the publication's position, read FIRST

Alongside headline / summary / signal you return "take": ONE declarative
sentence stating AI Vector's position on this story. It renders as the
COLD OPEN of the story unit -- bold, ABOVE the body, the first prose the
reader meets after the headline. It is a SEPARATE field with its OWN
budget; the body keeps its 30-60 word cap unchanged.

WRITE FOR THE COLD-OPEN SLOT. The reader has seen the headline and
NOTHING else. The take may assume headline information; it may NOT lean
on the body: no "This" / "That" / "It" / "These" / "Those" opener, and
no pronoun whose referent lives in prose the reader has not reached
yet. The grammatical subject is a READER-WORLD subject -- a role, a
workflow, a cost line, an artefact class the reader owns -- not "the
paper", "the release", or "the researchers".

GRAMMAR OF THE SLOT (checked in code):
  - 12-18 words; aim NEAR 12. HARD CAPS: 18 words AND 118 characters --
    every tier, no exceptions (the rendered slot holds two lines; a
    longer take is rejected and you will be asked to rewrite).
  - Finite verb by word six. A subject that runs longer buries the
    verb and stalls the parse.
  - At most ONE comma and at most ONE semicolon. No coordinating "and"
    inside the first seven words (a coordinated subject stalls the
    verb).
  - One-pass parse: read it once aloud; if the reader must back up,
    rewrite.

HEADLINE AND TAKE ARE CO-READ -- one unit, twelve pixels apart. The
HEADLINE states what happened; the TAKE states what we hold. The take
must carry information the headline withheld: if the take is derivable
from the headline alone, it fails the slot (checked in code as content-
word overlap). Assume the headline; never echo it.

THE OLD STATE IS THE CONTEXT -- the generative mechanism. A position
lands only against what it displaces. Every take carries the old state,
named or implied strongly enough to reconstruct. Three routes; pick the
one the story actually runs on and return its label in "take_route":

  R1 -- DISPLACEMENT: X can now do A instead of B. The old practice is
    named and displaced. "Compliance teams can now cite the regulator's
    own text instead of somebody's scrape of it."
  R2 -- NAMED-OWNER CONSEQUENCE: a reader-world owner's obligation,
    scope, or baseline shifts. "Model-risk sign-off now covers agent
    plans, not just model outputs."
  R3 -- PRICED TRADEOFF: the new number against the old number, in
    units the reader budgets in. "The same agent workload now clears
    at $2.68 a day against $6.17 on the old pricing."

ROUTE DIVERSITY. The TAKES ALREADY WRITTEN block (when present) carries
route labels. Never use the same route as the previous take in the same
section, and across an issue no single route should carry more than
about four takes in ten.

THE COLD-OPEN TEST governs. "It is now the case that [take]" must still
parse -- that is necessary, NOT sufficient (a news recap passes it).
The real test: cover the body. From the headline and the take alone,
can the reader name WHAT CHANGED and FROM WHAT to WHAT? A take that
names a topic, restates the headline, or waits for the body to supply
the old state fails the slot.

THIN SOURCING. The take asserts at the level the sourcing supports --
never harden an unverifiable claim into a field fact. One forum thread
reporting an agent wiping a repository does not license "Agents now
delete production repos"; it licenses the CLASS claim: "A single
documented wipe moves destructive agent commands from hypothetical
risk to recorded precedent."

CALIBRATION IS WOVEN, NEVER PARENTHESISED. Sourcing calibration rides
inside the sentence as a modifier ("on one preprint's benchmark",
"with the weights promised"), never as a bracketed aside. A
parenthetical in a cold open is a hedge wearing punctuation.

BANNED FORMS (checked in code and by the editor; a take that trips one
gets flagged or rejected):
  - No question mark, anywhere.
  - No hedges: may / could / potentially / appears / arguably.
  - No leading imperative verb -- the take asserts, it never instructs.
  - No labels: "So what:", "Bottom line:", "This matters because".
  - No deixis opener: This / That / It / These / Those as first word.
  - No second person UNLESS anchored to a concrete noun the reader owns
    ("your retrieval stack" is fine; "you should care" is not).
  - No universalisms ("changes everything", "nothing will be the same").
  - No body restatement: the take must not repeat a body sentence or
    share most of its content words with one. The body is written to
    FOLLOW the take -- it deepens the position; it never re-announces
    it.

THE STACKED-MODIFIER GARDEN PATH -- named anti-pattern. Post-modified
noun phrases stack into a sentence the reader must re-parse: "A
benchmark released Tuesday scored by judges trained on synthetic data
now anchors procurement" strands the reader three modifiers deep before
the verb. Rules: at most ONE reduced relative ("released Tuesday",
"scored by judges") per take; never a reduced relative on a coordinated
subject; never coordinate two post-modified noun phrases. If the
modifiers matter, spend them in the body.

ANTI-PATTERN FRAMES the editor has already flagged; do not reach for:
  - "...is the [X] that was missing"
  - "That moves X from [abstract] to [concrete]"
  - "The [metric] masks the [failure]"
  - "Replicated, X; unreplicated, Y" (named repeat offender: three times
    in two issues despite guards -- do not use it in a take)
  - "X is now a Y problem, not a Z one" (at most ONE per issue; assume a
    prior story already used it)
  - consultant filler: "table stakes", "the new X", "the moat"
  - self-referential: "and that is the story"

CALIBRATION EXEMPLARS (the ratified set -- study the shape, do not copy
the sentences):
  [R1] "Compliance teams can now cite the regulator's own text instead
       of somebody's scrape of it."
  [R2] "Agent incident reviews now start from a public log of nineteen
       entries rather than from anecdote."
  [R2] "A single documented wipe moves destructive agent commands from
       hypothetical risk to recorded precedent." (thin sourcing: the
       class claim, never the hardened incident claim)
  [R1] "Agent reasoning now lands on stderr, leaving stdout clean
       enough to pipe into the next tool."
  [R3] "The same agent workload now clears at $2.68 a day against
       $6.17 on the old pricing."
  [R1] "Turn-level scoring now beats whole-conversation grading on one
       preprint's benchmark, harness public to rerun."
"""


# ---------------------------------------------------------------------------
# Prompt-cache static prefix (2026-08-08).
#
# The first ~9k tokens of every per-story summarise prompt (header +
# voice + headline rules + editorial focus + finance lens + the v0.22
# tier-independent take teaching) are assembled purely from the module
# constants above -- no per-story, per-day, or per-config interpolation.
# Assembled ONCE at import time so byte identity across all N per-story
# calls (and the Pulse re-summarise) is structural, not incidental.
# ``_build_summary_prompt`` returns this as the cacheable prefix;
# ``rank._llm_call_anthropic`` marks it ``cache_control: ephemeral``. The
# per-day voice-diversity block sits AFTER the section voice block, so it
# is variable-part material, never prefix material. The per-TIER take
# shape is variable-part material too; only the tier-independent take
# rules live here (they are byte-identical across tiers by construction).
# ---------------------------------------------------------------------------

_SUMMARY_PROMPT_STATIC_PREFIX = f"""\
You are writing one story for AI Vector -- a daily newsletter about
Agentic AI and Generative AI. The cluster was already RANKED and
selected for the issue; your job is to write it well.

{_VOICE_BLOCK}
{_HEADLINE_RULES_BLOCK}
{_EDITORIAL_FOCUS_BLOCK}
{_FINANCE_LENS_BLOCK}
{_TAKE_BLOCK}"""


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
    # This loop is SEQUENTIAL by construction (one blocking LLM call per
    # story) -- the v0.21.3 feed-forward close context depends on that:
    # close-variety is a cross-story property, so each hands_on / currents
    # story's prompt carries the closes already accepted for its tier.
    # If this loop is ever parallelised, keep the two mould-prone tiers
    # serial (or move close coordination to a post-pass polish stage).
    # Note: some summarised stories are later dropped by the section caps
    # in _assemble_sections; feeding closes at generation time is the best
    # available approximation, and a dropped story's close in the list is
    # harmless (it only widens the do-not-reuse set).
    blocks: list[tuple[RankedStory, SummaryBlock]] = []
    closes_by_tier: dict[str, list[str]] = {}
    takes_so_far: list[str] = []
    take_routes: dict[str, str] = {}
    last_route_by_tier: dict[str, str] = {}
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
        prior_section_closes = (
            list(closes_by_tier.get(story.tier, []))
            if story.tier in _CLOSE_FEEDFORWARD_TIERS else []
        )
        try:
            block = _summarise_one(
                story=story, cluster=cluster, items=items, callbacks=callbacks,
                voice_diversity_block=voice_diversity_block,
                prior_section_closes=prior_section_closes,
                prior_takes=list(takes_so_far),
                last_route_same_tier=last_route_by_tier.get(story.tier),
                route_sink=take_routes,
            )
        except Exception:  # noqa: BLE001 -- never crash the issue on one bad story
            _LOG.exception(
                "summarise: failed to summarise cluster_id=%s -- skipping",
                story.cluster_id,
            )
            continue
        if block is None:
            continue
        if story.tier in _CLOSE_FEEDFORWARD_TIERS:
            close = _extract_closing_sentence(block.summary)
            if close:
                closes_by_tier.setdefault(story.tier, []).append(close)
        # v0.22: takes feed forward across ALL tiers (issue-wide frame
        # diversity). Read from the draft via the block when the model
        # carries the field; a None take (soft-fail path) feeds nothing.
        # v0.23: entries carry the R1/R2/R3 route label when the model
        # returned one, and the last route per tier is tracked so the
        # NEXT same-tier story's reminder can name it as do-not-repeat.
        accepted_take = getattr(block, "take", None)
        if accepted_take:
            route = take_routes.get(story.cluster_id)
            takes_so_far.append(
                f"[{route}] {accepted_take}" if route else accepted_take
            )
            if route:
                last_route_by_tier[story.tier] = route
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
            take_routes=take_routes,
        )

    # --- Section syntheses (v0.23; supersedes the Phase-B intro pair) ---
    # One LLM call per non-pulse section, fed the section's stories so the
    # synthesis reads the day's pattern. Pulse never carries one -- its
    # whole job is to BE the framing. Sections with exactly ONE story get
    # no synthesis either (designer adjudication: a synthesis of one
    # story duplicates its dek). Failures degrade gracefully for Big
    # Picture / Hands-On: the template hides a missing synthesis. For
    # Currents the framing is editorially mandatory (>= 2 stories or the
    # quiet day) -- one retry, then a WARNING. Already-written syntheses
    # feed forward so no two sections share a thesis.
    prior_syntheses: list[str] = []
    for _sec in (big_picture_section, hands_on_section, currents_section):
        _populate_section_synthesis(
            _sec, voice_diversity_block, prior_syntheses=list(prior_syntheses),
        )
        if _sec.synthesis:
            prior_syntheses.append(_sec.synthesis)
    # v0.21 (carried into v0.23): template-contract guard -- an empty
    # Currents section must never ship without its quiet-day framing.
    # Deterministic code fallback; no-op when the LLM quiet-day synthesis
    # landed.
    _ensure_quiet_day_currents_synthesis(currents_section)

    # --- The digest ("The 30-second read", v0.23 / Issue v8) ------------
    # One issue-level LLM call AFTER all stories + takes + syntheses
    # exist -- sequential position matters: the deconfliction checks are
    # defined against the takes and syntheses. Failure-soft: any error
    # degrades to digest=None (no skim section), never a partial digest.
    digest: list[DigestBullet] | None = None
    try:
        digest = _generate_digest(
            pulse_section,
            [big_picture_section, hands_on_section, currents_section],
            anti_patterns=anti_patterns,
        )
    except Exception:  # noqa: BLE001 -- the digest never blocks the issue
        _LOG.exception(
            "summarise: digest generation raised -- shipping without a "
            "digest (the skim section is omitted)"
        )
        digest = None

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

    # --- Persist take routes (SummaryBlock v5, wave three) ---------------
    _persist_take_routes(
        [pulse_section, big_picture_section, hands_on_section,
         currents_section],
        take_routes,
    )

    # --- Construct + validate -------------------------------------------
    # issue_number is intentionally None in staging output. Numbering is a
    # release-time operation; see DESIGN.md "Issue Number Registry" +
    # "Archive: staging vs canonical".
    prompt_versions = {
        "rank": _read_rank_version(),
        "summarise": SUMMARISE_PROMPT_VERSION,
        "pulse": PULSE_PROMPT_VERSION,
    }
    if digest is not None:
        # Recorded only when a digest was actually produced -- absence of
        # the key in the archive means the digest degraded that day.
        prompt_versions["digest"] = DIGEST_PROMPT_VERSION
    issue = Issue(
        issue_number=None,
        date=run_date,
        pulse=pulse_section,
        sections=[big_picture_section, hands_on_section, currents_section],
        digest=digest,
        generated_at=_dt.datetime.now(_dt.timezone.utc),
        prompt_versions=prompt_versions,
        notes=f"shape: {shape} -- {shape_reason}",
    )

    # Defensive application of the post-verify digest bar. run.py's order
    # is summarise -> verify, so ``verification`` is None on every block
    # here and this is a no-op today; it exists so the seam is concrete
    # and exercised. The real enforcement point is verify.py after it
    # denormalises verdicts (wave-three integration -- see
    # ``digest_verify_violations``).
    if issue.digest is not None:
        bar = digest_verify_violations(issue)
        if bar:
            _LOG.warning(
                "summarise: digest verify bar fired at generation time "
                "(%s) -- nulling the digest",
                "; ".join(bar),
            )
            issue = issue.model_copy(update={
                "digest": None,
                "prompt_versions": {
                    k: v for k, v in issue.prompt_versions.items()
                    if k != "digest"
                },
            })

    _write_issue_json(issue_out, issue)

    # Persist the source excerpts the summaries were grounded on so the
    # advisory verify stage judges against identical text (DESIGN.md
    # "source_excerpts.jsonl"). Best-effort: a sidecar write failure must
    # never lose the issue we just wrote -- verify degrades to "unavailable"
    # if the sidecar is missing/unreadable.
    try:
        _write_source_excerpts(
            paths.source_excerpts_path(run_date, canonical=False),
            issue,
            fetched_at=_dt.datetime.now(_dt.timezone.utc),
        )
    except Exception:  # noqa: BLE001 -- excerpt sidecar is advisory, never fatal
        _LOG.exception(
            "summarise: failed to persist source_excerpts.jsonl for %s -- "
            "verify will degrade to unavailable; issue.json was written",
            run_date.isoformat(),
        )

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
    """One past issue's intros + closings + takes, in the shape the prompt
    needs. ``intro_leads`` maps section name -> the LEAD phrase used that
    day (None when the section had no intro -- Pulse always; older issues
    occasionally for the others). ``first_story_closings`` maps section
    name (including ``pulse``) -> the closing sentence of that section's
    first story, truncated to ``_VOICE_DIVERSITY_CLOSING_TRUNC`` chars.
    ``first_story_takes`` (v0.22) maps section name (including ``pulse``)
    -> the first story's ``take``, same truncation; empty for pre-v0.22
    archive issues (the field is simply absent there)."""
    issue_date: _dt.date
    intro_leads: dict[str, str]
    first_story_closings: dict[str, str]
    first_story_takes: dict[str, str] = field(default_factory=dict)


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
        takes: dict[str, str] = {}

        def _truncated_take(block: dict[str, Any]) -> str:
            """First-story take, truncated like closings. Empty for
            pre-v0.22 archive issues (no ``take`` key) -- v0.22."""
            raw_take = block.get("take")
            if not isinstance(raw_take, str) or not raw_take.strip():
                return ""
            t = raw_take.strip()
            if len(t) > _VOICE_DIVERSITY_CLOSING_TRUNC:
                t = t[:_VOICE_DIVERSITY_CLOSING_TRUNC].rstrip() + "..."
            return t

        # Pulse: no intro_lead (the Pulse IS the framing); only the closing
        # (+ the take since v0.22).
        pulse = payload.get("pulse") or {}
        if isinstance(pulse, dict):
            stories = pulse.get("stories") or []
            if isinstance(stories, list) and stories:
                first = stories[0]
                if isinstance(first, dict):
                    closing = _closing_sentence(first.get("summary") or "")
                    if closing:
                        closings["pulse"] = closing
                    take = _truncated_take(first)
                    if take:
                        takes["pulse"] = take

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
            else:
                # v0.23: synthesis-era issues carry no intro_lead; the
                # synthesis (truncated) joins the do-not-repeat set in
                # its place so cross-day framing diversity keeps working.
                syn = section.get("synthesis")
                if isinstance(syn, str) and syn.strip():
                    syn_s = syn.strip()
                    if len(syn_s) > _VOICE_DIVERSITY_CLOSING_TRUNC:
                        syn_s = (
                            syn_s[:_VOICE_DIVERSITY_CLOSING_TRUNC].rstrip()
                            + "..."
                        )
                    intro_leads[section_key] = syn_s
            stories = section.get("stories") or []
            if isinstance(stories, list) and stories:
                first = stories[0]
                if isinstance(first, dict):
                    closing = _closing_sentence(first.get("summary") or "")
                    if closing:
                        closings[section_key] = closing
                    take = _truncated_take(first)
                    if take:
                        takes[section_key] = take

        out.append(_PastIssueVoice(
            issue_date=day,
            intro_leads=intro_leads,
            first_story_closings=closings,
            first_story_takes=takes,
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
                # v0.22: past takes join the do-not-repeat set -- take
                # frame diversity spans days as well as stories.
                take = past.first_story_takes.get(section_name)
                if take:
                    section_lines.append(
                        f"    - {date_iso} take: {take!r}"
                    )
            if section_lines:
                parts.append(f"  [{section_name}]")
                parts.extend(section_lines)

        parts.append("")
        parts.append(
            "Today's intros, closings, and takes must NOT reuse the "
            "constructions above. Vary the sentence shape AND the "
            "underlying epistemic posture. Two sections of today's issue "
            "cannot share a thesis statement. If today's news genuinely "
            "is similar to a recent issue's, the editorial POSITION may "
            "recur -- but the PROSE must not."
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
    v0.9 (Phase B): adds ``signal`` (editorial verdict pill). v0.22: adds
    ``take`` (the publication's one-sentence position; None when the LLM
    failed to produce one -- shipped as-is and flagged by review). v0.23:
    adds ``take_route`` (the R1/R2/R3 old-state route label; generation-
    time only -- it feeds the route-diversity feed-forward and is NOT
    persisted on SummaryBlock)."""
    headline: str
    summary: str
    signal: str | None = None
    take: str | None = None
    take_route: str | None = None


_CLOSE_FEEDFORWARD_TIERS = ("hands_on", "currents")
"""Tiers whose closes are fed forward into subsequent same-tier story
prompts (v0.21.3). These are the two mould-prone sections (Hands-On
imperative closes, Currents body closes); big_picture's
strategic-question turn-type binds fine per-story and is not fed.
NOTE (v0.22): TAKES feed forward separately, across ALL tiers including
big_picture -- take frame diversity is issue-wide, not per-section. See
``_render_prior_takes_block``."""

_SUMMARY_BLOCK_HAS_TAKE = "take" in SummaryBlock.model_fields
"""v0.22 contract seam: ``SummaryBlock.take`` is added by the Architect's
concurrent models.py change (schema bump). Feature-detected so summarise
keeps producing issues (with a loud WARNING, takes dropped) if this module
lands first -- SummaryBlock is ``extra=\"forbid\"``, so passing an unknown
field would otherwise fail EVERY block and kill the issue. Once the model
change lands this constant is True and the seam is inert."""

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
"""Sentence-boundary splitter for ``_extract_closing_sentence``. Splits
after ., ! or ? followed by whitespace. Abbreviations with internal
stops ("9 a.m. tomorrow") can over-split; acceptable here -- the output
feeds a do-not-reuse list, never published prose."""


def _extract_closing_sentence(summary: str) -> str:
    """Return the last sentence of a summary body (the close).

    Deterministic code, no LLM (No Token Wasted). Used by the v0.21.3
    feed-forward mechanism: the close of each accepted hands_on /
    currents summary is injected into the NEXT same-tier story's prompt
    so the model can actually vary against it -- close-variety is a
    cross-story property that per-story prompting cannot coordinate.
    Empty / whitespace-only input returns "".
    """
    text = (summary or "").strip()
    if not text:
        return ""
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]
    return parts[-1] if parts else text


def _render_prior_closes_block(prior_closes: list[str]) -> str:
    """Render the CLOSES ALREADY WRITTEN prompt block (v0.21.3).

    Closing sentences only, numbered and quoted -- not whole summaries;
    the model needs the constructions to vary against, nothing more.
    Empty input renders to "" (first story in a section sees no block).
    """
    if not prior_closes:
        return ""
    lines = [
        "CLOSES ALREADY WRITTEN IN THIS SECTION (do not reuse their "
        "closing construction or scaffold):"
    ]
    for i, close in enumerate(prior_closes, start=1):
        lines.append(f'  {i}. "{close}"')
    return "\n".join(lines)


def _render_prior_takes_block(prior_takes: list[str]) -> str:
    """Render the TAKES ALREADY WRITTEN prompt block (v0.22; route labels
    v0.23).

    Takes feed forward directly as a field -- no sentence extraction
    needed (they ARE single sentences by contract). Issue-wide, across
    all tiers including big_picture, because take frame diversity is an
    issue-wide property: the reviewer flags two takes sharing a
    syntactic frame at minor, three at major. v0.23: entries carry the
    R1/R2/R3 route label when the model returned one ("[R2] Model-risk
    sign-off now covers..."), so the route-diversity rule (no route >
    ~40% of an issue's takes, never two consecutive same-route in a
    section) has concrete labels to vary against. Empty input renders ""
    (the first story of the run sees no block).
    """
    if not prior_takes:
        return ""
    lines = [
        "TAKES ALREADY WRITTEN IN THIS ISSUE (with route labels; do not "
        "reuse their syntactic frame, and keep the routes varied -- no "
        "route should carry more than about four takes in ten):"
    ]
    for i, take in enumerate(prior_takes, start=1):
        lines.append(f'  {i}. "{take}"')
    return "\n".join(lines)


def _summarise_one(
    story: RankedStory,
    cluster: Cluster,
    items: list[Item],
    callbacks: list[_CallbackRef],
    voice_diversity_block: str = "",
    prior_section_closes: list[str] | None = None,
    prior_takes: list[str] | None = None,
    last_route_same_tier: str | None = None,
    route_sink: dict[str, str] | None = None,
) -> SummaryBlock | None:
    """One LLM call. Returns a validated ``SummaryBlock`` or ``None`` if
    the call / parse / validation failed after the retry budget.

    ``prior_section_closes`` (v0.21.3): closing sentences of summaries
    already accepted for the SAME tier in this run, threaded into the
    prompt so the close-variety rules become followable (feed-forward
    close context). ``None`` / empty means no block is injected.

    ``prior_takes`` (v0.22): takes already accepted in this run, across
    ALL tiers -- take frame diversity is issue-wide. Fed forward as-is
    (a take IS one sentence; no extraction). v0.23: entries may carry a
    leading route label ("[R2] ...") -- the block passes them through.

    ``last_route_same_tier`` (v0.23): the R1/R2/R3 route of the previous
    accepted take in THIS tier, injected into the write-site take
    reminder as do-not-repeat (never two consecutive same-route takes in
    a section).

    ``route_sink`` (v0.23): when provided, the accepted draft's
    ``take_route`` is recorded under ``cluster.cluster_id`` so the caller
    can label the feed-forward entry. An out-param rather than a changed
    return type so every existing call site keeps working unchanged."""
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
        prior_section_closes=prior_section_closes,
        prior_takes=prior_takes,
        last_route_same_tier=last_route_same_tier,
    )

    draft = _call_and_parse_summary(prompt, temperature, cluster.cluster_id)
    if draft is None:
        return None
    if route_sink is not None and draft.take and draft.take_route:
        route_sink[cluster.cluster_id] = draft.take_route

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
            **_take_field_kwargs(draft.take, cluster.cluster_id),
        )
    except Exception:  # noqa: BLE001
        _LOG.exception(
            "summarise: SummaryBlock validation failed for cluster_id=%s -- "
            "skipping. draft=%s",
            cluster.cluster_id, draft,
        )
        return None
    return block


_TAKE_MODEL_MAX_CHARS = 200
"""Mirror of ``SummaryBlock.take``'s pydantic ``max_length`` (schema v4,
2026-08-08). A take past this would fail block validation and cost the
WHOLE story; ``_take_field_kwargs`` degrades it to None instead (review
then flags the missing take -- one flagged line beats one lost story)."""


def _take_field_kwargs(take: str | None, cluster_id: str) -> dict[str, Any]:
    """SummaryBlock kwargs for the v0.22 ``take`` field, guarded by the
    ``_SUMMARY_BLOCK_HAS_TAKE`` contract seam. Returns ``{}`` (with a loud
    WARNING when a take would be lost) if the models.py schema bump is
    absent -- SummaryBlock is ``extra="forbid"``, so an unguarded pass
    would fail every block and cost the whole issue. Also drops (to None,
    WARNING) a take that would fail the model's own 200-char cap."""
    if _SUMMARY_BLOCK_HAS_TAKE:
        if take and len(take) > _TAKE_MODEL_MAX_CHARS:
            _LOG.warning(
                "summarise: take for cluster_id=%s is %d chars (> model cap "
                "%d) even after the corrective retry -- shipping take=None "
                "so the story survives; review will flag the missing take",
                cluster_id, len(take), _TAKE_MODEL_MAX_CHARS,
            )
            take = None
        return {"take": take}
    if take:
        _LOG.warning(
            "summarise: SummaryBlock has no 'take' field (models.py schema "
            "bump not landed?) -- DROPPING the generated take for "
            "cluster_id=%s: %r", cluster_id, take,
        )
    return {}


def _resummarise_as_pulse(
    story: RankedStory,
    cluster: Cluster,
    items: list[Item],
    callbacks: list[_CallbackRef],
    original_block: SummaryBlock,
    voice_diversity_block: str = "",
    prior_takes: list[str] | None = None,
    route_sink: dict[str, str] | None = None,
) -> SummaryBlock | None:
    """Re-run the per-story summarise prompt under the Pulse-specific
    voice + closing shape (v0.12, 2026-05-31). ``route_sink`` (v0.23):
    records the rewrite's ``take_route`` under the cluster id, same
    out-param seam as ``_summarise_one``.

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
        prior_takes=prior_takes,
    )

    draft = _call_and_parse_summary(prompt, temperature, cluster.cluster_id)
    if draft is None:
        return None
    if route_sink is not None and draft.take and draft.take_route:
        route_sink[cluster.cluster_id] = draft.take_route

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
            **_take_field_kwargs(draft.take, cluster.cluster_id),
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

_SOURCE_EXCERPT_MAX_WORDS = 1000
"""Soft cap on the excerpt the prompt sees. Raised from 500 to 1000 in
v0.15 (2026-06-03): substantive articles often carry the load-bearing
detail (named artifacts, distinctions between an existing tool and a new
paper that reinterprets it, install steps, formal contracts) deeper than
the first 500 words. The HEADLINE RULES added in v0.15 (name the
artifact; preserve distinctions in the source) need that material in the
prompt window. Cost impact: ~37,500 extra input tokens per issue (top-3
items per cluster x ~25 clusters x extra 500 words), roughly 5c per
issue at current Bedrock pricing -- worth it for stories where the
distinction sits in the middle of the article."""


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
    prior_section_closes: list[str] | None = None,
    prior_takes: list[str] | None = None,
    last_route_same_tier: str | None = None,
) -> tuple[str, str]:
    """Assemble the per-story summarisation prompt with voice + skills
    inlined and callback context attached when present.

    2026-08-08 -- prompt-cache split (message-structure change ONLY; not a
    SUMMARISE_PROMPT_VERSION bump): returns ``(static_prefix,
    variable_part)`` instead of one string. The prefix is
    ``_SUMMARY_PROMPT_STATIC_PREFIX`` (header + voice + headline rules +
    editorial focus + finance lens -- byte-identical across every story,
    tier, and the Pulse override); the split boundary sits immediately
    after ``_FINANCE_LENS_BLOCK``, right before the tier-dependent section
    voice block. ``prefix + variable`` equals the pre-split single-string
    prompt BYTE FOR BYTE (pinned by
    tests/test_summarise.py::TestSummaryPromptCacheSplit), so the model
    reads exactly the same bytes -- caching changes billing, not
    behaviour. ``rank._llm_call`` accepts the tuple and sends
    ``cache_control: ephemeral`` on the prefix block (Anthropic provider;
    other providers get the joined string).

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

    ``prior_section_closes`` (v0.21.3): closing sentences already written
    for the same tier in this run. When non-empty, a CLOSES ALREADY
    WRITTEN block is injected immediately before the close reminder tail
    (write-site recency is the mechanism that binds, per the v0.21.1/2
    findings), and the Hands-On FINAL REMINDER references it. Ignored
    under the Pulse override (the body close has no scaffold to vary).

    ``prior_takes`` (v0.22): takes already accepted in this run, ACROSS
    ALL tiers including big_picture (frame diversity on takes is an
    issue-wide property: two takes sharing a syntactic frame anywhere in
    the issue get flagged by the reviewer). Fed forward directly as a
    field -- no sentence extraction needed. Injected at the write site,
    same recency mechanism as prior_section_closes; also injected under
    the Pulse override (the Pulse take counts toward frame diversity).
    v0.23: entries may carry a leading route label ("[R2] ...") so the
    route-diversity rule has something concrete to vary against.

    ``last_route_same_tier`` (v0.23): the R1/R2/R3 route of the previous
    accepted take in the SAME tier. When set, the write-site take
    reminder names it as do-not-repeat (never two consecutive same-route
    takes in a section). Ignored under the Pulse override (the Pulse is
    not "consecutive" with any section).
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
        section_voice = (
            _PULSE_VOICE_BLOCK
            + "\n\n" + _PULSE_CLOSING_SHAPE
            + "\n\n" + _PULSE_TAKE_SHAPE
        )
        voice_header = (
            "SECTION VOICE (override=pulse; this story has been elevated\n"
            "to The Pulse and is being re-summarised under Pulse rules):"
        )
    else:
        section_voice = _VOICE_PER_SECTION.get(story.tier, "")
        closing_shape = _CLOSING_SHAPE_PER_SECTION.get(story.tier, "")
        if closing_shape:
            section_voice = section_voice + "\n\n" + closing_shape
        take_shape = _TAKE_SHAPE_PER_SECTION.get(story.tier, "")
        if take_shape:
            section_voice = section_voice + "\n\n" + take_shape
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
    #
    # v0.21.1 (2026-07-04): the same recency mechanism now covers Big
    # Picture stories -- the gate caught 3/4 BP closes landing as
    # imperatives because the generic "close tied to a SPECIFIC decision"
    # body rule was the instruction nearest the schema. Restating the BP
    # turn-type here, at the site where the model writes the close, is
    # the structural fix.
    if section_override == "pulse":
        close_reminder_tail = (
            "\n- PULSE OVERRIDE (FINAL REMINDER): this is The Pulse. The "
            "plain-take JUDGEMENT goes in the \"take\" FIELD -- one "
            "declarative sentence naming what is TRUE NOW given the "
            "day's shift. NOT a question. NOT a prescription (\"raise "
            "this at...\", \"test against...\", \"audit your...\"). NOT "
            "an imperative verb. The BODY ends on the day's direction "
            "and must NOT restate the take. The Pulse names where the "
            "field moved today; the rest of the issue is for decisions "
            "to make about it.\n"
        )
    elif story.tier == "big_picture":
        close_reminder_tail = (
            "\n- BIG PICTURE CLOSE (FINAL REMINDER): this story closes on "
            "the STRATEGIC QUESTION shape above; the final sentence ends "
            "in a question mark. For this tier the question IS the "
            "decision-tied close; closing on an instruction to the reader "
            "instead of a question FAILS the shape. (If the picker later "
            "elevates this story to The Pulse, it is re-summarised under "
            "Pulse rules in a separate pass.)\n"
        )
    elif story.tier == "hands_on":
        # v0.21.2 (2026-07-05): the gate re-run showed the v0.21.1
        # variety-list reorder alone did NOT move Hands-On closes (3/3
        # still "[imperative on artefact] + before + [milestone]"); the
        # write-site FINAL REMINDER is the mechanism that binds.
        # v0.21.3: when prior same-section closes are fed forward, the
        # reminder points at the concrete list -- the model can now SEE
        # what "consecutive stories must never share" refers to.
        vary_reference = (
            " against the CLOSES ALREADY WRITTEN list above (reuse NONE "
            "of their closing constructions or scaffolds)"
            if prior_section_closes else ""
        )
        close_reminder_tail = (
            "\n- HANDS-ON CLOSE (FINAL REMINDER): the close is an "
            "imperative with a NAMED artefact and a source-supported "
            "trigger. VARY THE CONSTRUCTION" + vary_reference +
            ": the trailing \"... before "
            "[milestone]\" scaffold is capped at ~2 per section and "
            "consecutive stories must NEVER share it. Trigger-first "
            "(\"Before X, do Y\") is the SAME scaffold -- vary the "
            "FRAME, not just the word order. When the scaffold is "
            "already used, prefer: condition-first (\"Already on "
            "N? ...\") or a bare imperative with stakes (\"...; the "
            "vendor numbers won't transfer\"). The TRIGGER is a factual "
            "claim: an event the source supports or a generic "
            "practitioner milestone (\"your next eval cycle\", \"before "
            "you rely on it in production\") -- NEVER an invented "
            "source-specific cadence.\n"
        )
    else:
        close_reminder_tail = ""

    # v0.23: terse take reminder at the write site (recency is the
    # mechanism that binds, per the v0.21.1/2 findings; the full teaching
    # lives in the static-prefix _TAKE_BLOCK). The cap is universal; the
    # only dynamic part is the previous same-section route (route
    # diversity: never two consecutive same-route takes in a section).
    if section_override != "pulse" and last_route_same_tier:
        route_note = (
            " The previous take in this section used route "
            + last_route_same_tier + "; do NOT use "
            + last_route_same_tier + " for this take."
        )
    else:
        route_note = ""
    take_reminder_tail = (
        "\n- THE TAKE (FINAL REMINDER): return \"take\" -- the COLD OPEN, "
        "read before the body: ONE declarative sentence, 12-18 words (aim "
        "near 12; HARD caps 18 words AND 118 characters), reader-world "
        "subject, finite verb by word six, the OLD STATE in view, and it "
        "must carry the position the headline withheld. It must pass the "
        "cold-open test (from headline + take alone: what changed, from "
        "what, to what?). No This/That/It opener, no question mark, no "
        "leading imperative verb, no hedges (may / could / potentially / "
        "appears / arguably), no labels, at most one comma and one "
        "semicolon, no \"and\" inside the first seven words, and it must "
        "NOT restate a body sentence or the headline. Also return "
        "\"take_route\": R1 (displacement), R2 (named-owner consequence), "
        "or R3 (priced tradeoff)." + route_note + "\n"
    )

    # v0.21.3: feed-forward close context. Closes already accepted for
    # the SAME tier in this run, injected at the write site (immediately
    # before the close reminder tail / JSON schema -- recency binds).
    # Suppressed under the Pulse override: the plain take is a different
    # speech act with no scaffold to vary against.
    prior_closes_segment = ""
    if section_override != "pulse" and prior_section_closes:
        prior_closes_block = _render_prior_closes_block(
            list(prior_section_closes)
        )
        if prior_closes_block:
            prior_closes_segment = f"\n{prior_closes_block}\n"

    # v0.22: feed-forward take context -- issue-wide (all tiers AND the
    # Pulse override), because take frame diversity is an issue-wide
    # property the reviewer checks (2 shared frames = minor, 3 = major).
    prior_takes_segment = ""
    if prior_takes:
        prior_takes_block = _render_prior_takes_block(list(prior_takes))
        if prior_takes_block:
            prior_takes_segment = f"\n{prior_takes_block}\n"

    # 2026-08-08: the return became (static_prefix, variable_part) -- see
    # the docstring. The variable part starts at the first tier-dependent
    # byte (the section voice block).
    variable_part = f"""\
{section_voice_block}{voice_diversity_segment}
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
  number or mechanism; a trust flag if warranted (does the evidence
  DEVIATE from its class default -- replication present, competitor-run,
  non-obvious scoring method, claim heavier than evidence? If not, name
  the source class in the body and write no flag.); a close tied to a
  SPECIFIC decision, not a department or group. If you cannot fit all
  three in 60 words, cut a clause or sharpen a verb -- the cap holds.
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
  source, do NOT assert it -- and do NOT inventory its absence either
  ("benchmarks not yet published", "licence not specified" are both
  out). Describe what the source DOES state; stay silent about the
  rest. AFFIRMATIVE-PRESENCE OBLIGATION: when the source STATES an
  artifact is available (code, weights, dataset, tooling public), the
  summary MUST say so affirmatively ("dataset and tooling are
  public") -- that is the signal a builder acts on, and it clears all
  three gates.
- TRUST HEDGES ARE FACTUAL CLAIMS -- PRESENCE-FORM -- AND NON-DEFAULT.
  THREE GATES, all mandatory; a flag that fails ANY one does not appear:
    1. SOURCE-SUPPORTED: the characterisation is one the source
       explicitly supports (the paper says the benchmarks are its own;
       the post is the vendor's own numbers). Never invented, never
       decoration.
    2. PRESENCE-FORM: it describes evidence that EXISTS, never what is
       missing.
    3. INFORMATIVE VS THE CLASS DEFAULT: it tells the reader something
       the source-class name in the body did not already tell them.
  The class attribution in the body carries the default calibration
  for FREE. Deviation taxonomy (default needs NO flag; deviation EARNS
  one):
    * arXiv preprint. Default: single team, not peer-reviewed, authors'
      own scoring. Deviation: independent replication PRESENT;
      third-party scoring; multi-lab authorship on a dramatic claim.
    * Vendor blog / release notes. Default: vendor's own numbers and
      framing. Deviation: benchmark presented as if neutral; a
      COMPETITOR ran the comparison; an independent audit is cited.
    * Named-author experiment / blog. Default: one practitioner's
      setup, n=1. Deviation: reproduced by others; production scale.
    * Reddit / forum thread. Default: anecdote, n=1. Deviation: rarely;
      a big claim resting on it (see magnitude routing below).
  Non-obvious scoring METHOD also deviates: "scored by an ensemble of
  LLM judges" is informative in a way "authors' own" is not.
  RESTATING THE DEFAULT IS BANNED; the fix is DELETE the flag and let
  the class name in the body carry it.
    Wrong (restates default): "a preprint from a single research team."
      Right: body says "an arXiv preprint", no separate flag.
    Wrong: "Vendor-published benchmark" when the body already names the
      vendor. Right (deviation): "the vendor benchmarked its
      competitor's model."
    Wrong: "one research team's analysis" (single-team IS the preprint
      default). Wrong: "single-source" on a podcast interview (of
      course it is).
    Wrong: "self-reported" when the source describes scoring by an
      ensemble of LLM judges (name the ensemble; that IS the flag).
    Wrong: "no independent replication" when the paper triangulates
      against two independent systems.
    Wrong: "benchmarks are self-reported" when the source describes
      experiments on real data with no such framing.
- CLAIM-MAGNITUDE ROUTING: a claim far heavier than its evidence (a
  field-redefining result in a single-team preprint; a big number from
  one thread) belongs in the CLOSE -- the Currents calibrated stake or
  the Big Picture strategic question -- NOT in the flag. One exception:
  "thin sourcing, one Reddit thread" under a big claim is a legitimate
  FLAG, because there the deviation IS the magnitude/evidence mismatch.
  Either way, say it ONCE: flag or stake, never both.
- THREE-GATE REWRITES (real defects from released issues; the most
  common fix is now: name the class in the body, write NO flag):
    "No code is public yet." -> delete; if the source states
      availability, say it affirmatively ("code and weights are
      public").
    "Single-source interview; no independent benchmarks." -> delete
      both clauses; "a single podcast interview" in the body carries
      the calibration.
    "...the paper's own LLM judges, with no independent replication
      yet" -> "scored by the paper's own LLM-judge ensemble." (the
      METHOD is the informative part; the absence clause goes).
    "Vendor release notes only; no independent benchmarks." -> name
      the class in the body ("Anthropic's release notes"); no flag.
    "Single research team; peer review pending." -> body says "an
      arXiv preprint"; no separate flag.
  ALLOWED: when the source ITSELF states the absence, report the
  actor's statement or decision -- "the paper says code will be
  released on acceptance", "weights withheld for safety" are
  actor-statements, not voids (all three gates still apply).
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
  Choose by what the body actually argues. A body whose close sends the
  design to a review rather than to production is "discuss", not "act".
{prior_closes_segment}{prior_takes_segment}{close_reminder_tail}{take_reminder_tail}
Return ONLY a single JSON object (no markdown fences, no commentary):

{{
  "headline": "<consequence-led headline, HARD <= 90 chars AND <= 12 words>",
  "summary": "<30-60 word body, HARD 60-word cap (same for the Pulse)>",
  "take": "<ONE declarative cold-open sentence, 12-18 words, HARD caps 18 words / 118 chars: the publication's position>",
  "take_route": "<one of: R1 | R2 | R3>",
  "signal": "<one of: act | try | read | watch | discuss>"
}}
"""
    return _SUMMARY_PROMPT_STATIC_PREFIX, variable_part


# Length caps -- mirrored from the prompt + the judge rubric in
# evals/judge/prompts/headline.yaml and summary.yaml. Single source of
# truth for the post-LLM enforcement check below.
_HEADLINE_MAX_WORDS = 12
_HEADLINE_MAX_CHARS = 90
_BODY_MIN_WORDS = 30
_BODY_MAX_WORDS = 60

# v0.23 take caps + banned-form machinery. The take targets 12 words
# (band 12-18) per the ratified cold-open spec as adjudicated by the
# experience designer: at the real rendered metrics (semibold 18px at a
# 620px measure, ~66-72 characters per line) two lines is the ceiling,
# so the caps are 18 words AND 118 characters, both HARD, both
# UNIVERSAL. The v0.22 Currents 22-word exception is retired. Only the
# hard caps are enforced in code; the 12-word target is prompt +
# reviewer territory (an 11-word take is thin, not broken).
_TAKE_MAX_WORDS = 18
_TAKE_MAX_CHARS = 118

_TAKE_DEIXIS_OPENERS = frozenset({"this", "that", "it", "these", "those"})
"""First words banned by the cold-open contract (v0.23): a deixis opener
points at body prose the reader has not reached yet. Compared lowercased
after stripping leading punctuation."""

_TAKE_MAX_COMMAS = 1
_TAKE_MAX_SEMICOLONS = 1
"""Finite-verb-position proxies (v0.23): more than one comma or more than
one semicolon in a 22-word cold open almost always means a stacked or
coordinated structure that stalls the parse. POS tagging is out of scope;
these are deterministic proxies and the judge carries the residue."""

_TAKE_NO_AND_BEFORE_WORD = 8
"""No coordinating "and" before this 1-indexed word position (i.e. not in
words 1-7): a coordinated subject delays the finite verb past the
by-word-six contract. Deterministic proxy, same caveat as above."""

_TAKE_HEDGE_RE = re.compile(
    r"\b(?:may|could|potentially|appears|arguably)\b", re.IGNORECASE
)
"""The checkable hedge vocabulary from the ratified spec. Word-boundary,
case-insensitive. ("May" the month is a theoretical false positive; in an
editorial take the cost of a spurious corrective retry is one cheap call,
and the soft-fail path keeps the draft either way.)"""

_TAKE_BANNED_LABELS = ("so what:", "bottom line:", "this matters because")
"""Label constructions banned anywhere in a take (case-insensitive
substring check). The take asserts a position; it never announces one."""

_CONTENT_OVERLAP_LIMIT = 0.6
"""Shared restatement threshold (v0.23 refactor): two texts sharing >=
this fraction of content words (relative to the smaller content-word
set) count as restating each other. Used by BOTH the take-vs-body check
(`_take_restates_body`) and the digest deconfliction checks
(`_digest_violations`) -- one threshold, one meaning."""

_TAKE_CONTENT_STOPWORDS = frozenset({
    "the", "a", "an", "of", "to", "and", "or", "on", "in", "is", "are",
    "was", "were", "be", "been", "being", "for", "with", "that", "this",
    "these", "those", "its", "it", "as", "at", "by", "from", "not", "now",
    "but", "has", "have", "had", "will", "your", "you", "their", "they",
})
"""Function words excluded from the take/body overlap check -- shared
grammar must not count as shared content."""


def _content_words(text: str) -> set[str]:
    """Lowercased content-word set for the restatement check. Plain code,
    deterministic (No Token Wasted)."""
    tokens = re.findall(r"[a-z0-9']+", (text or "").lower())
    return {t for t in tokens if t not in _TAKE_CONTENT_STOPWORDS}


def _content_overlap_fraction(a: str, b: str) -> float:
    """Fraction of shared content words between two texts, relative to the
    SMALLER content-word set (containment, not Jaccard): 1.0 means the
    smaller text's content is fully inside the larger. Shared seam for the
    take-restates-body check and the digest deconfliction checks (v0.23
    refactor). Plain code, deterministic (No Token Wasted)."""
    wa, wb = _content_words(a), _content_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def _take_restates_body(take: str, body: str) -> bool:
    """True when the take is a body restatement: a verbatim substring of
    the body (whitespace-collapsed, case-insensitive), OR sharing >=
    ``_CONTENT_OVERLAP_LIMIT`` of content words with any SINGLE body
    sentence (via the shared `_content_overlap_fraction`). Under the
    cold-open contract the body FOLLOWS the take, so a restatement is a
    body that re-announces the position the unit opened with -- either
    direction is dead weight."""
    take_norm = re.sub(r"\s+", " ", (take or "")).strip().lower()
    body_norm = re.sub(r"\s+", " ", (body or "")).strip().lower()
    if not take_norm or not body_norm:
        return False
    if take_norm.rstrip(".") and take_norm.rstrip(".") in body_norm:
        return True
    if not _content_words(take):
        return False
    for sentence in _SENTENCE_SPLIT_RE.split(body):
        if not _content_words(sentence):
            continue
        if _content_overlap_fraction(take, sentence) >= _CONTENT_OVERLAP_LIMIT:
            return True
    return False


def _take_violations(
    draft: _SummaryDraft, take_word_cap: int = _TAKE_MAX_WORDS
) -> list[str]:
    """Code-checkable take violations against the ratified cold-open spec
    (v0.23). Returns human-readable strings; empty list = within spec.

    Checked here (deterministic): presence, the universal hard word cap
    (18) and character ceiling (118 -- two rendered lines, designer
    adjudication), '?', the hedge vocabulary, label constructions, body
    restatement, headline restatement, the deixis-opener ban (This /
    That / It / These / Those as first word), and the
    finite-verb-position proxies (comma <= 1, semicolon <= 1, no
    coordinating "and" in the first seven words -- POS tagging is out of
    scope; the judge carries the residue). NOT
    checked here (needs judgment -- prompt + reviewer own it): leading
    imperative verb, reader-world subject, the old-state requirement,
    second-person outside "your <concrete noun>", universalisms, the
    stacked-modifier garden path beyond the proxies, and the
    anti-pattern catalogue."""
    issues: list[str] = []
    take = (draft.take or "").strip()
    if not take:
        issues.append(
            'the "take" field is missing or empty -- it is REQUIRED: one '
            "declarative cold-open sentence stating the publication's "
            "position"
        )
        return issues
    words = take.split()
    tw = len(words)
    if tw > take_word_cap:
        issues.append(
            f"take is {tw} words (HARD cap is {take_word_cap})"
        )
    if len(take) > _TAKE_MAX_CHARS:
        issues.append(
            f"take is {len(take)} characters (HARD ceiling is "
            f"{_TAKE_MAX_CHARS} -- two rendered lines at the real "
            "metrics; cut words, not letters)"
        )
    first_word = words[0].strip("\"'([{").rstrip(".,;:!\"')]}").lower()
    if first_word in _TAKE_DEIXIS_OPENERS:
        issues.append(
            f"take opens with the deixis word {words[0]!r} (banned: the "
            "cold open cannot point at body prose the reader has not "
            "reached yet -- use a reader-world subject)"
        )
    if "?" in take:
        issues.append(
            "take contains a question mark (a take asserts; the question "
            "belongs to the body close)"
        )
    if take.count(",") > _TAKE_MAX_COMMAS:
        issues.append(
            f"take has {take.count(',')} commas (at most "
            f"{_TAKE_MAX_COMMAS} -- one-pass parse; the finite verb must "
            "arrive by word six)"
        )
    if take.count(";") > _TAKE_MAX_SEMICOLONS:
        issues.append(
            f"take has {take.count(';')} semicolons (at most "
            f"{_TAKE_MAX_SEMICOLONS})"
        )
    early_tokens = [
        w.strip("\"'([{").rstrip(".,;:!\"')]}").lower()
        for w in words[: _TAKE_NO_AND_BEFORE_WORD - 1]
    ]
    if "and" in early_tokens:
        issues.append(
            'take has a coordinating "and" inside the first seven words '
            "(a coordinated subject stalls the finite verb past word six)"
        )
    hedge = _TAKE_HEDGE_RE.search(take)
    if hedge:
        issues.append(
            f"take contains the hedge {hedge.group(0)!r} (banned: may / "
            "could / potentially / appears / arguably)"
        )
    lowered = take.lower()
    for label in _TAKE_BANNED_LABELS:
        if label in lowered:
            issues.append(
                f"take contains the banned label construction {label!r}"
            )
    if _take_restates_body(take, draft.summary):
        issues.append(
            "take restates the body (verbatim overlap or >= 60% shared "
            "content words with a body sentence) -- the body follows the "
            "cold open and must not re-announce it"
        )
    # v0.23 (designer adjudication): headline and take are co-read as one
    # unit; a take derivable from the headline carries no information.
    # Code proxy via the shared overlap helper; the semantic half
    # (headline = what happened, take = what we hold) lives in the prompt.
    if (
        _content_overlap_fraction(take, draft.headline)
        >= _CONTENT_OVERLAP_LIMIT
    ):
        issues.append(
            "take restates the headline (>= 60% shared content words) -- "
            "headline and take are co-read; the take must carry the "
            "position the headline withheld"
        )
    return issues


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
    prompt: "str | tuple[str, str]", temperature: float, cluster_id: str,
    take_word_cap: int = _TAKE_MAX_WORDS,
) -> _SummaryDraft | None:
    """LLM call + retry on parse failure (one retry, mirrors rank.py) +
    a separate single retry on length-cap OR take-spec violation (tasks
    #73 + #74; take checks added at v0.22; v0.23 makes the cap universal
    -- ``take_word_cap`` defaults to the 18-word hard cap for every tier,
    the Currents exception is retired).

    ``prompt`` is either the legacy single string or the cache-split
    ``(static_prefix, variable_part)`` tuple from
    ``_build_summary_prompt``. Retry discipline (2026-08-08, mirrors
    rank's v0.6.1 fix): corrective text is APPENDED to the variable part,
    never prepended to the whole prompt -- prepending would change byte 0
    of the cached prefix and force a full cache miss plus a wasted cache
    write on every retry. The prefix block is byte-identical across
    attempts (pinned by
    tests/test_summarise.py::TestSummaryPromptCacheSplit), so retries hit
    the cache the first attempt just wrote.

    Order of operations:
      1. Call the LLM. If JSON parse fails, retry once with a corrective
         prompt; if it fails again, return None (story is dropped).
      2. With a valid draft in hand, check length caps. If any are
         breached, retry ONCE with a corrective prompt that quotes the
         specific overruns. If the second attempt still breaches, KEEP
         the draft (log a warning) -- we'd rather ship a marginally-
         overlong headline than lose a top-N story.
    """
    is_split = isinstance(prompt, tuple)
    prefix, variable = prompt if is_split else ("", prompt)

    def _assemble(var: str) -> "str | tuple[str, str]":
        return (prefix, var) if is_split else var

    attempts = JSON_RETRY_BUDGET + 1
    current_variable = variable
    draft: _SummaryDraft | None = None
    for attempt in range(1, attempts + 1):
        try:
            raw = _llm_call(
                _assemble(current_variable),
                temperature=temperature, max_tokens=1600,
            )
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
            # Appended AFTER the variable part (cache discipline -- see
            # docstring): the cached prefix stays byte-identical.
            current_variable = (
                variable
                + "\n\nCORRECTION -- Your previous response was not valid "
                "JSON matching the schema above. Return JSON ONLY (no "
                "markdown fences, no prose) matching the schema."
            )
    if draft is None:
        return None

    # --- Length-cap + take-spec enforcement (single corrective retry) ----
    violations = _length_violations(draft) + _take_violations(
        draft, take_word_cap
    )
    if not violations:
        return draft

    _LOG.info(
        "summarise: caps/take-spec breached for cluster_id=%s on first "
        "pass: %s -- requesting one corrective regenerate",
        cluster_id, "; ".join(violations),
    )
    # Corrective text APPENDED to the variable part (cache discipline --
    # same reasoning as the JSON-parse retry above).
    corrective_variable = (
        variable
        + "\n\nCORRECTION -- Your previous response to the request above "
        "BREACHED the HARD caps or the take spec. The following "
        "violations were found:\n\n"
        + "\n".join(f"  - {v}" for v in violations)
        + "\n\nRewrite the JSON so that:\n"
        f"  - headline is AT MOST {_HEADLINE_MAX_WORDS} words AND AT MOST "
        f"{_HEADLINE_MAX_CHARS} characters\n"
        f"  - summary is BETWEEN {_BODY_MIN_WORDS} AND {_BODY_MAX_WORDS} "
        "words (the Pulse is held to the same cap)\n"
        '  - "take" is PRESENT: ONE declarative cold-open sentence, 12-18 '
        f"words (hard caps {take_word_cap} words / {_TAKE_MAX_CHARS} "
        "characters), no This/That/It opener, no question mark, no hedges "
        "(may / could / potentially / appears / arguably), no labels, at "
        'most one comma and one semicolon, no "and" in the first seven '
        "words, not a restatement of a body sentence or the headline; "
        'return "take_route" (R1 | R2 | R3) alongside it\n\n'
        "COUNT THE WORDS AND CHARACTERS before returning. Keep the same "
        "facts, tone, trust flag, and decision-tied close; just tighten "
        "the language. Cut adjectives, hedges, and spec-sheet detail "
        "first. Return ONLY JSON, no markdown fences, no commentary."
    )
    try:
        raw = _llm_call(
            _assemble(corrective_variable),
            temperature=temperature, max_tokens=1600,
        )
    except Exception:  # noqa: BLE001
        _LOG.warning(
            "summarise: corrective LLM call failed for cluster_id=%s -- "
            "keeping first-pass draft (still in violation)", cluster_id,
        )
        return draft
    retried = _parse_summary_json(raw)
    if retried is None:
        _LOG.warning(
            "summarise: corrective response failed to parse for cluster_id=%s "
            "-- keeping first-pass draft (still in violation)", cluster_id,
        )
        return draft
    new_violations = _length_violations(retried) + _take_violations(
        retried, take_word_cap
    )
    if new_violations:
        _LOG.warning(
            "summarise: cluster_id=%s STILL in violation after corrective "
            "retry: %s -- keeping the better of the two drafts (this is a "
            "soft fail; the issue ships but the judge/review will flag it)",
            cluster_id, "; ".join(new_violations),
        )
        # Prefer the retried draft when it resolves more violations, or
        # when it is strictly tighter on every length axis and no worse.
        # Cheap, deterministic.
        if len(new_violations) < len(violations):
            return retried
        if (
            len(retried.headline) <= len(draft.headline)
            and len(retried.headline.split()) <= len(draft.headline.split())
            and len(retried.summary.split()) <= len(draft.summary.split())
        ):
            return retried
        return draft
    _LOG.info(
        "summarise: corrective retry brought cluster_id=%s within spec "
        "(headline=%dw/%dc, body=%dw, take=%dw)",
        cluster_id,
        len(retried.headline.split()), len(retried.headline),
        len(retried.summary.split()),
        len((retried.take or "").split()),
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
    # v0.22: take is required by the prompt but optional in the parsed
    # shape -- a missing take triggers the corrective retry in
    # _call_and_parse_summary, and a still-missing take ships as None
    # (review flags it as major). Non-string garbage degrades to None.
    take_raw = payload.get("take")
    take: str | None = None
    if isinstance(take_raw, str) and take_raw.strip():
        take = take_raw.strip()
    # v0.23: the old-state route label (R1 displacement / R2 named-owner
    # consequence / R3 priced tradeoff). Optional and tolerant -- a
    # missing or garbage label degrades to None (the feed-forward then
    # carries the take unlabelled; route diversity is prompt + reviewer
    # territory, so absence is never a retry trigger).
    route_raw = payload.get("take_route")
    take_route: str | None = None
    if isinstance(route_raw, str):
        candidate_route = route_raw.strip().upper()
        if candidate_route in {"R1", "R2", "R3"}:
            take_route = candidate_route
    return _SummaryDraft(
        headline=headline.strip(),
        summary=summary.strip(),
        signal=signal,
        take=take,
        take_route=take_route,
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
    take_routes: dict[str, str] | None = None,
) -> None:
    """Re-summarise the Pulse-elected cluster under the Pulse-specific
    prompt and replace its entry in ``by_id`` in place.

    v0.22: the re-summarise prompt is fed the takes of every OTHER
    summarised story (derived from ``by_id``) so the Pulse take also
    honours issue-wide frame diversity. v0.23: ``take_routes`` maps
    cluster_id -> R1/R2/R3 for takes accepted earlier in the run; when
    present, feed-forward entries carry the label and the rewrite's own
    route is recorded back into the same dict.

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

    # v0.22: issue-wide take feed-forward for the Pulse rewrite -- every
    # other story's take, in by_id iteration order (insertion order = the
    # order they were written). The pulse story's own head-tier take is
    # excluded: it is being rewritten, not varied against. v0.23: entries
    # carry the route label when known.
    routes = take_routes or {}
    prior_takes = []
    for cid, (_s, b) in by_id.items():
        if cid == pulse_id:
            continue
        t = getattr(b, "take", None)
        if not t:
            continue
        r = routes.get(cid)
        prior_takes.append(f"[{r}] {t}" if r else t)

    original_section = story.tier  # "big_picture" / "hands_on" / "currents"
    try:
        new_block = _resummarise_as_pulse(
            story=story,
            cluster=cluster,
            items=items,
            callbacks=callbacks,
            original_block=original_block,
            voice_diversity_block=voice_diversity_block,
            prior_takes=prior_takes,
            route_sink=take_routes,
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
    take_routes: dict[str, str] | None = None,
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
        take_routes=take_routes,
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
"""Summarise's own target for a healthy Hands-On section, kept at 3.

No longer a mirror of the integrity gate: since 2026-08-04 the harness
floor (``evals/run_evals.py`` check_integrity) requires only >= 1 story
per named section, and a shortfall force-releases with a [THIN] notice
rather than failing the run. This constant remains the *picker's*
aspiration -- if source-diversity caps would starve Hands-On below it,
the picker degrades and relaxes caps so thin sections come from a thin
pool, never from our own caps."""


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
# Section syntheses (v0.23, 2026-08-09 layout redesign; supersedes the
# Phase-B intro_lead/intro_body pair -- IssueSection v4).
#
# One LLM call per non-pulse section, fed the section's already-written
# stories so the synthesis reads the day's pattern rather than restating
# it. Writes ``IssueSection.synthesis`` ONLY; the legacy intro pair stays
# None on new issues (the model's XOR validator enforces the migration
# direction; the renderer prefers synthesis and joins the legacy pair for
# archived issues). Failures degrade gracefully -- synthesis stays None
# and the template hides the block. Sections with exactly ONE story get
# no synthesis at all (designer adjudication: a synthesis of one story
# duplicates that story's dek).
# ---------------------------------------------------------------------------

_SECTION_SYNTHESIS_HINTS: dict[str, str] = {
    "big_picture": (
        "Senior-leader framing: strategic shifts, vendor calculus, risk, "
        "governance, regulation. The synthesis reads the strategic PATTERN "
        "across these stories -- what a leader should notice moving "
        "underneath them, not a restatement of any one story."
    ),
    "hands_on": (
        "Practitioner framing: tools, repos, benchmarks, techniques. The "
        "synthesis reads the day's practical PATTERN -- are the wins "
        "single-source benchmarks? Drop-in releases? Capability shifts? -- "
        "and what that pattern is worth to someone deciding what to touch "
        "this week."
    ),
    "currents": (
        "Early-signal framing: items thin on sourcing, early in "
        "trajectory, or moving but not yet arrived. The Currents synthesis "
        "is editorially MANDATORY -- the section's whole purpose is the "
        "AGGREGATE DIRECTION, and without the framing the section degrades "
        "to an enumeration of early signals. Name where the field is "
        "moving across these items, and why they sit here rather than "
        "higher up."
    ),
}


# Section names where the synthesis is editorially mandatory. Currents is
# the only one today -- EDITORIAL.md puts the aggregate direction at the
# heart of what the section is for. Used by ``_populate_section_synthesis``
# to retry once on failure and to log a WARNING rather than degrade
# silently.
_SECTIONS_WITH_MANDATORY_SYNTHESIS: set[str] = {"currents"}

# v0.23 code-side synthesis band (the editorial word budget; the pydantic
# cap on the field is 500 chars of structural headroom). Quiet-day
# Currents is exempt from the floor -- the quiet-day register is shorter
# by design.
_SYNTHESIS_MIN_WORDS = 28
_SYNTHESIS_MAX_WORDS = 45
_SYNTHESIS_MIN_SENTENCES = 2
_SYNTHESIS_MAX_SENTENCES = 3
_SYNTHESIS_MIN_FIRST_SENTENCE_WORDS = 7
"""Aphorism proxy: a first sentence under 7 words is almost always the
standalone aphoristic opener the redesign bans ("Costs precede clarity.",
"X outruns Y."). The register rule itself (open ON the pattern with
subject + verb) is prompt + reviewer territory; this is the cheap
structural kill for the family. Quiet-day Currents is exempt (the ratified
quiet-day line is 5 words)."""


_SIMPLE_MARKUP_RE = re.compile(
    r"</?(?:p|em|i|strong|b|span)\s*>|<\*|\*>", re.IGNORECASE
)
"""Presentation-wrapper tags the LLM sometimes emits around a synthesis
("<p><em>...</em></p>" -- observed on the 2026-08-09 gate run: the field
renders in italics, so the model 'helpfully' supplied the markup), plus
the "<*...*>" pseudo-italic wrapper (observed on the 2026-08-10 quiet-day
synthesis: "<" followed by "*" matched neither this stripper nor the
residual detector below, so it shipped). Stripped deterministically
before validation; any OTHER residual markup is a violation (retry
material)."""

_ANY_MARKUP_RE = re.compile(r"<[a-zA-Z/!*][^>]*>")
"""Residual-markup detector for the violation check after the simple
wrappers are stripped. Includes "*" in the opening class so unknown
star-wrapper variants retry instead of shipping."""


def _strip_simple_markup(text: str) -> str:
    """Remove presentation-wrapper tags and collapse the whitespace they
    leave behind. Plain code, deterministic (No Token Wasted): the field
    is plain text by contract; italics are the renderer's job."""
    cleaned = _SIMPLE_MARKUP_RE.sub("", text or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _synthesis_violations(text: str, *, quiet_day: bool = False) -> list[str]:
    """Code-checkable synthesis violations against the v0.23 spec: word
    band 28-45, 2-3 sentences, no aphoristic opener (first-sentence floor),
    no em-dashes, no residual markup. Quiet-day Currents relaxes the
    floors (the quiet-day register is deliberately shorter). Returns
    human-readable strings; empty list = within spec."""
    issues: list[str] = []
    body = (text or "").strip()
    if not body:
        issues.append("synthesis is missing or empty")
        return issues
    words = len(body.split())
    min_words = 3 if quiet_day else _SYNTHESIS_MIN_WORDS
    if words < min_words:
        issues.append(
            f"synthesis is {words} words (minimum {min_words})"
        )
    if words > _SYNTHESIS_MAX_WORDS:
        issues.append(
            f"synthesis is {words} words (maximum {_SYNTHESIS_MAX_WORDS})"
        )
    sentences = [
        s for s in _SENTENCE_SPLIT_RE.split(body) if s.strip()
    ]
    min_sentences = 1 if quiet_day else _SYNTHESIS_MIN_SENTENCES
    if len(sentences) < min_sentences:
        issues.append(
            f"synthesis has {len(sentences)} sentence(s) (minimum "
            f"{min_sentences})"
        )
    if len(sentences) > _SYNTHESIS_MAX_SENTENCES:
        issues.append(
            f"synthesis has {len(sentences)} sentences (maximum "
            f"{_SYNTHESIS_MAX_SENTENCES})"
        )
    if not quiet_day and sentences:
        first_words = len(sentences[0].split())
        if first_words < _SYNTHESIS_MIN_FIRST_SENTENCE_WORDS:
            issues.append(
                f"synthesis opens with a {first_words}-word sentence -- "
                "the standalone aphoristic opener is banned; the first "
                "sentence must already state the pattern (subject + verb)"
            )
    if "--" in body or "—" in body:
        issues.append("synthesis contains an em-dash (banned punctuation)")
    if _ANY_MARKUP_RE.search(body):
        issues.append(
            "synthesis contains HTML markup (the field is plain text; "
            "italics are the renderer's job)"
        )
    return issues


def _populate_section_synthesis(
    section: IssueSection,
    voice_diversity_block: str = "",
    prior_syntheses: list[str] | None = None,
) -> None:
    """Generate ``IssueSection.synthesis`` for a section via one LLM call.
    Mutates the section in place; the legacy intro pair is never touched
    (it stays None on new issues -- the model's XOR validator enforces the
    migration direction).

    Skip rules (v0.23):
      - A section with exactly ONE story gets NO synthesis (designer
        adjudication: a synthesis of one story duplicates its dek).
      - A zero-story Big Picture / Hands-On is never rendered, so it gets
        none either. A zero-story CURRENTS still runs -- the quiet-day
        contract survives from v0.21, now as a single quiet-day synthesis
        paragraph (deterministic guard:
        ``_ensure_quiet_day_currents_synthesis``).

    For sections in ``_SECTIONS_WITH_MANDATORY_SYNTHESIS`` (Currents), a
    failure or a code-check violation triggers ONE corrective retry; if
    the second attempt also fails, the miss is logged at WARNING so the
    editor / Arman see the smell at ratification. Other sections get the
    same single corrective retry but degrade silently (the template hides
    a missing synthesis). A second attempt that still carries violations
    ships anyway with a WARNING (soft fail; the reviewer flags it) --
    losing the framing entirely is worse than shipping an off-band one.

    ``prior_syntheses``: syntheses already written for earlier sections
    this run, injected so no two sections share a thesis."""
    quiet_day = not section.stories
    if quiet_day and section.name != "currents":
        # Big Picture / Hands-On never render empty; only Currents has a
        # quiet-day contract with the template.
        return
    if quiet_day:
        # Quiet day (2026-08-11 ruling): the line is fixed -- "Nothing
        # surfaced for Currents today." -- one plain statement, no
        # explanation. Fixed wording is a constant, not an LLM call
        # (No Token Wasted), which also removes the whole class of
        # LLM-emitted wrapper garbage from the quiet-day path.
        section.synthesis = _QUIET_DAY_CURRENTS_SYNTHESIS
        _LOG.info(
            "summarise: zero Currents stories -- set the deterministic "
            "quiet-day line (no LLM call)"
        )
        return
    if len(section.stories) == 1:
        _LOG.info(
            "summarise: section %s has exactly one story -- no synthesis "
            "(n=1 rule: it would duplicate the story's dek)",
            section.name,
        )
        return
    hint = _SECTION_SYNTHESIS_HINTS.get(section.name)
    if hint is None:
        return
    temperature = float(os.getenv("LLM_TEMPERATURE_SUMMARISE", "0.6"))
    mandatory = section.name in _SECTIONS_WITH_MANDATORY_SYNTHESIS

    # quiet_day never reaches here since the 2026-08-11 ruling: the
    # quiet-day line is a fixed constant set above, no LLM call. Every
    # prompt assembled below is byte-identical to v0.23's non-quiet
    # prompts.
    story_lines: list[str] = []
    for st in section.stories:
        body = st.summary if len(st.summary) <= 280 else st.summary[:280] + "..."
        take_line = ""
        st_take = getattr(st, "take", None)
        if st_take:
            take_line = f"\n  TAKE (do not restate): {st_take}"
        story_lines.append(
            f"- HEADLINE: {st.headline}\n  BODY: {body}{take_line}"
        )
    stories_block = "\n".join(story_lines)

    if section.name == "currents":
        register_addendum = (
            "\n- CURRENTS DIRECTION: the synthesis must name the "
            "AGGREGATE DIRECTION today's items point at, as a claim the "
            "reader can hold (\"Regulators are circling agentic "
            "payments...\"), not a posture (\"For awareness only\" is "
            "off-voice)."
        )
    else:
        register_addendum = ""

    prior_block = ""
    if prior_syntheses:
        listed = "\n".join(f'  - "{s}"' for s in prior_syntheses)
        prior_block = (
            "\nSYNTHESES ALREADY WRITTEN FOR TODAY'S OTHER SECTIONS -- no "
            "two sections may share a thesis; take a genuinely different "
            "angle:\n" + listed + "\n"
        )

    # v0.13 (carried forward): voice-diversity injection sits right above
    # the INSTRUCTIONS block. Empty string when the caller has nothing to
    # inject.
    voice_diversity_segment = (
        f"\n{voice_diversity_block}\n" if voice_diversity_block else ""
    )

    prompt = f"""\
You are writing the section SYNTHESIS for the "{section.name}" section of
today's AI Vector issue -- a daily AI newsletter with a financial-services
lens, plain English, Australian English, no em-dashes. The synthesis is
ONE italic paragraph rendered under the section head, before the stories.

SECTION CONTEXT
{hint}

STORIES IN THIS SECTION
{stories_block}
{prior_block}{voice_diversity_segment}
INSTRUCTIONS
- REGISTER: the editor's aside, not a headline. Quieter than the old
  bold intro -- the voice of an editor leaning over to say what they
  noticed across today's stories. It frames; it never announces or
  sells.
- SHAPE: ONE paragraph, 28-45 words, 2-3 sentences.
- OPEN ON THE PATTERN with a full grammatical subject and a verb. NO
  standalone aphoristic opening sentence: "Costs precede clarity." /
  "Capability outruns control." are banned shapes -- the first sentence
  must already say what the pattern IS, not decorate it.
- THE PATTERN ACROSS STORIES, never a summary of one story. If the
  stories genuinely don't rhyme, say what the spread itself signals.
- Do not restate any story's take or headline; the reader is about to
  read them.{register_addendum}
- HONESTY: if the section's pattern is "these are all single-source
  benchmarks", say so. Don't oversell weak signal.

Return ONLY a single JSON object (no markdown fences, no commentary):

{{
  "synthesis": "<one 28-45 word paragraph, 2-3 sentences>"
}}
"""

    def _attempt_once(use_prompt: str) -> str | None:
        """One LLM round-trip + parse. Returns the synthesis text on
        success or None on any failure."""
        try:
            raw = _llm_call(use_prompt, temperature=temperature, max_tokens=400)
        except Exception:  # noqa: BLE001
            _LOG.warning(
                "summarise: section-synthesis LLM call failed for %s",
                section.name,
            )
            return None
        payload = _extract_json_object(raw)
        if not isinstance(payload, dict):
            _LOG.warning(
                "summarise: section-synthesis JSON parse failed for %s",
                section.name,
            )
            return None
        text_raw = payload.get("synthesis")
        if not isinstance(text_raw, str) or not text_raw.strip():
            return None
        # Deterministic wrapper-tag strip (observed defect: the model
        # returns "<p><em>...</em></p>" because the field renders italic).
        # Any residual markup is caught by _synthesis_violations -> retry.
        cleaned = _strip_simple_markup(text_raw)
        return cleaned or None

    text = _attempt_once(prompt)
    violations = (
        _synthesis_violations(text, quiet_day=quiet_day)
        if text is not None else ["no parseable synthesis returned"]
    )
    if violations:
        _LOG.info(
            "summarise: synthesis for %s missed on first pass (%s) -- "
            "retrying once",
            section.name, "; ".join(violations),
        )
        corrective = (
            "Your previous response was missing, malformed, or off-spec: "
            + "; ".join(violations) + ". "
            + (
                "The Currents synthesis is EDITORIALLY MANDATORY. "
                if mandatory else ""
            )
            + "Return ONLY a JSON object with a non-empty string field "
            "'synthesis': ONE paragraph, 28-45 words, 2-3 sentences, "
            "opening on the pattern with a full subject and verb (no "
            "aphoristic opener), no em-dashes. Original request "
            "follows.\n\n" + prompt
        )
        retried = _attempt_once(corrective)
        if retried is not None:
            retry_violations = _synthesis_violations(
                retried, quiet_day=quiet_day
            )
            if retry_violations:
                _LOG.warning(
                    "summarise: synthesis for %s STILL off-spec after "
                    "retry (%s) -- shipping it anyway (soft fail; review "
                    "flags it)",
                    section.name, "; ".join(retry_violations),
                )
            text = retried
        elif text is None:
            # Both attempts unusable.
            if mandatory:
                _LOG.warning(
                    "summarise: MANDATORY synthesis missing for %s after "
                    "retry -- shipping the section without a synthesis. "
                    "Editor / Arman: review the Currents section for its "
                    "aggregate-direction framing.",
                    section.name,
                )
            return
        else:
            # Retry unusable but the first attempt parsed: ship the
            # off-spec first draft (soft fail; the reviewer flags it).
            _LOG.warning(
                "summarise: synthesis retry for %s failed to parse -- "
                "shipping the off-spec first draft (%s)",
                section.name, "; ".join(violations),
            )

    if text is None:
        return
    try:
        section.synthesis = text
    except Exception:  # noqa: BLE001 -- the 500-char structural cap may fire
        _LOG.warning(
            "summarise: section-synthesis validation failed for %s "
            "(text=%r)",
            section.name, text[:120],
        )


# v0.21 (2026-07-04), migrated to the synthesis field at v0.23: a
# deterministic quiet-day framing for an empty Currents section. Three
# shipped issues carried an empty Currents with null framing and needed a
# manual fix (template-integrity failure per review). Since the 2026-08-11
# ruling the wording is FIXED and set directly by
# ``_populate_section_synthesis`` -- no LLM call, per No Token Wasted; the
# ruling: one plain statement, no explanation.
_QUIET_DAY_CURRENTS_SYNTHESIS = "Nothing surfaced for Currents today."
"""The ``synthesis`` for a zero-story Currents section. Ratified wording
(2026-08-11); change only on a ruling."""


def _ensure_quiet_day_currents_synthesis(section: IssueSection) -> None:
    """Template-contract guard (v0.21, synthesis form since v0.23): a
    zero-story Currents section must NEVER ship without its quiet-day
    framing.

    Called by the pipeline after ``_populate_section_synthesis``. When the
    section is Currents, has zero stories, and ``synthesis`` is still
    null/empty (LLM failed twice, or its output failed validation), inject
    the deterministic quiet-day default. No-op in every other case -- a
    successful LLM quiet-day synthesis is left untouched, and sections
    with stories keep whatever the synthesis pass produced.
    """
    if section.name != "currents" or section.stories:
        return
    if (section.synthesis or "").strip():
        return
    section.synthesis = _QUIET_DAY_CURRENTS_SYNTHESIS
    _LOG.warning(
        "summarise: empty Currents landed with a null/empty synthesis -- "
        "injected the deterministic quiet-day synthesis. Editor / Arman: "
        "the LLM quiet-day framing did not land; the default wording "
        "shipped instead."
    )


# ---------------------------------------------------------------------------
# The digest -- "The 30-second read" (v0.23, Issue v8, 2026-08-09).
#
# One issue-level LLM call, made AFTER every story, take, and synthesis
# exists: the deconfliction contract is defined against the takes (no
# digest sentence may paraphrase a take) and the syntheses (no digest
# lead may echo a synthesis), so sequential position matters. Structure
# is deterministic scaffold + LLM prose: code decides HOW MANY bullets
# and WHICH stories each may cite; the LLM writes the words. Degradation
# is always ``None`` (no skim section), never a padded or partial digest.
#
# PIPELINE SEAM (verify bar). run.py's stage order is summarise ->
# verify -> render: verify auto-fires AFTER summarise, so per-story
# verification does NOT exist yet when the digest is generated. The
# ratified bar -- a claim the verifier marked unverifiable or
# contradicted may not appear in the digest -- is therefore a POST-VERIFY
# code check: ``digest_verify_violations`` below is the deterministic
# helper. summarise applies it defensively at generation time (a no-op
# while ``SummaryBlock.verification`` is None everywhere), and verify.py
# (wave three, 2026-08-09) calls it after denormalising verdicts onto the
# issue, surfacing violations in the verify report note -- advisory; the
# gate consumes them separately, and verify never mutates the digest.
# ---------------------------------------------------------------------------

_DIGEST_SECTION_ORDER = ("big_picture", "hands_on", "currents")
"""Layout order for digest bullets 2..n -- mirrors the rendered issue."""

_DIGEST_MIN_BULLETS = 3
_DIGEST_MAX_BULLETS = 5
_DIGEST_MIN_SECTION_STORIES = 2
"""A section earns a digest bullet only with >= 2 stories -- a one-story
section's bullet would duplicate that story's dek (same n=1 logic as the
synthesis rule)."""

_DIGEST_LEAD_MIN_WORDS = 3
_DIGEST_LEAD_MAX_WORDS = 6
_DIGEST_SENTENCE_MIN_WORDS = 14
_DIGEST_SENTENCE_MAX_WORDS = 22
_DIGEST_SENTENCE_MAX_WORDS_SEMILIST = 26
_DIGEST_SEMILIST_MAX_CLAUSES = 3
_DIGEST_TOTAL_MAX_WORDS = 100
"""The ratified digest budgets: bold lead 3-6 words (full stop), one
sentence of 14-22 words (26 allowed only for a semicolon-list of <= 3
clauses), and a 100-word HARD total across the whole digest, leads
included."""


def _first_word(text: str) -> str:
    """Lowercased first word of a text, stripped of surrounding
    punctuation. Empty string for empty input."""
    words = (text or "").split()
    if not words:
        return ""
    return words[0].strip("\"'([{").rstrip(".,;:!?\"')]}").lower()


def _word_ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    """Lowercased word n-grams (stopwords KEPT -- verbatim-collision
    detection needs the articles: "Ship the plumbing first" collides on
    its exact wording, not its content words)."""
    tokens = re.findall(r"[a-z0-9'$.%-]+", (text or "").lower())
    if len(tokens) < n:
        return set()
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def _shares_ngram(a: str, b: str, n: int = 3) -> bool:
    """True when the two texts share any word n-gram (default trigram).
    The deterministic check for verbatim-family collisions between a
    digest lead and a synthesis first sentence (the observed defect:
    "Ship the plumbing first." appearing on both surfaces)."""
    return bool(_word_ngrams(a, n) & _word_ngrams(b, n))


def _synthesis_first_sentence(synthesis: str | None) -> str:
    """First sentence of a synthesis paragraph, or "" when absent."""
    text = (synthesis or "").strip()
    if not text:
        return ""
    parts = [p for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]
    return parts[0].strip() if parts else text


def _digest_sentence_count_ok(sentence: str) -> bool:
    """ONE sentence check: the text must not contain an internal sentence
    boundary (a ./!/? followed by whitespace). Semicolons are allowed --
    the semicolon-list is a sanctioned form."""
    parts = [p for p in _SENTENCE_SPLIT_RE.split(sentence.strip()) if p.strip()]
    return len(parts) <= 1


def _digest_bullet_words(lead: str, sentence: str) -> int:
    """Word count of one bullet toward the 100-word total (lead included,
    trailing punctuation irrelevant to the split)."""
    return len(lead.split()) + len(sentence.split())


def _digest_violations(
    bullets: list[dict[str, Any]],
    *,
    pulse_id: str,
    eligible_names: list[str],
    allowed_ids: dict[str, list[str]],
    takes: list[str],
    syntheses: dict[str, str | None],
) -> list[str]:
    """Code-checkable digest violations against the ratified spec.
    Deterministic, no LLM (No Token Wasted). Empty list = within spec.

    ``bullets`` is the parsed LLM payload: dicts with ``section``,
    ``lead``, ``sentence``, ``story_ids``. ``allowed_ids`` maps section
    name (including "pulse") -> the story ids a bullet of that section
    may cite. ``takes`` are the plain take texts (no route labels).
    ``syntheses`` maps section name -> synthesis (None allowed)."""
    issues: list[str] = []
    n = len(bullets)
    if n < _DIGEST_MIN_BULLETS or n > _DIGEST_MAX_BULLETS:
        issues.append(
            f"digest has {n} bullets (must be {_DIGEST_MIN_BULLETS}-"
            f"{_DIGEST_MAX_BULLETS})"
        )
        return issues

    # --- Structural: bullet 1 is the Pulse; 2..n follow layout order. ---
    first = bullets[0]
    if first.get("section") != "pulse":
        issues.append('bullet 1 must cover The Pulse (section="pulse")')
    first_ids = first.get("story_ids") or []
    if not first_ids or first_ids[0] != pulse_id:
        issues.append(
            f"bullet 1 primary story_id must be the Pulse story "
            f"({pulse_id!r})"
        )
    seq = [b.get("section") for b in bullets[1:]]
    deduped: list[str] = []
    for name in seq:
        if not deduped or deduped[-1] != name:
            deduped.append(name)
    if deduped != eligible_names:
        issues.append(
            f"bullets 2..n must cover the eligible sections in layout "
            f"order {eligible_names} (one bullet each; at most one "
            f"section split into two ADJACENT bullets) -- got {seq}"
        )
    elif len(seq) - len(deduped) > 1:
        issues.append(
            "at most ONE section may split into two bullets -- got "
            f"{len(seq) - len(deduped)} splits"
        )

    # --- Per-bullet text + provenance checks. ---------------------------
    total_words = 0
    for i, bullet in enumerate(bullets):
        label = f"bullet {i + 1}"
        section_name = bullet.get("section") or ""
        lead = (bullet.get("lead") or "").strip()
        sentence = (bullet.get("sentence") or "").strip()
        story_ids = bullet.get("story_ids") or []

        if not lead:
            issues.append(f"{label}: lead is missing or empty")
        else:
            if not lead.endswith("."):
                issues.append(f"{label}: lead must end with a full stop")
            if "?" in lead:
                issues.append(f"{label}: lead must not be a question")
            lw = len(lead.rstrip(".").split())
            if lw < _DIGEST_LEAD_MIN_WORDS or lw > _DIGEST_LEAD_MAX_WORDS:
                issues.append(
                    f"{label}: lead is {lw} words (must be "
                    f"{_DIGEST_LEAD_MIN_WORDS}-{_DIGEST_LEAD_MAX_WORDS})"
                )
            if len(lead) > 80:
                issues.append(f"{label}: lead exceeds 80 characters")
        if not sentence:
            issues.append(f"{label}: sentence is missing or empty")
        else:
            if not _digest_sentence_count_ok(sentence):
                issues.append(
                    f"{label}: sentence must be exactly ONE sentence "
                    "(semicolons allowed; internal full stops are not)"
                )
            sw = len(sentence.split())
            semilist = (
                ";" in sentence
                and len(sentence.split(";")) <= _DIGEST_SEMILIST_MAX_CLAUSES
            )
            cap = (
                _DIGEST_SENTENCE_MAX_WORDS_SEMILIST if semilist
                else _DIGEST_SENTENCE_MAX_WORDS
            )
            if sw < _DIGEST_SENTENCE_MIN_WORDS:
                issues.append(
                    f"{label}: sentence is {sw} words (minimum "
                    f"{_DIGEST_SENTENCE_MIN_WORDS})"
                )
            if sw > cap:
                issues.append(
                    f"{label}: sentence is {sw} words (cap {cap}"
                    + (
                        "" if semilist
                        else "; 26 only for a semicolon-list of <= 3 clauses"
                    )
                    + ")"
                )
            if len(sentence) > 300:
                issues.append(f"{label}: sentence exceeds 300 characters")
        if _ANY_MARKUP_RE.search(lead) or _ANY_MARKUP_RE.search(sentence):
            issues.append(
                f"{label}: contains HTML markup (plain text only; the "
                "template supplies the bold/plain treatment)"
            )

        total_words += _digest_bullet_words(lead, sentence)

        # Provenance: every cited id must be one the section may cite.
        allowed = set(allowed_ids.get(section_name, []))
        if not story_ids:
            issues.append(f"{label}: story_ids is empty")
        else:
            unknown = [sid for sid in story_ids if sid not in allowed]
            if unknown:
                issues.append(
                    f"{label}: story_ids {unknown} are not in the "
                    f"{section_name!r} id list"
                )

        # Deconfliction: the digest compresses STORIES, never the takes.
        for take in takes:
            if take and sentence and (
                _content_overlap_fraction(sentence, take)
                >= _CONTENT_OVERLAP_LIMIT
            ):
                issues.append(
                    f"{label}: sentence paraphrases a take (>= 60% shared "
                    "content words) -- compress the story, not its take"
                )
                break
        for syn_name, syn in syntheses.items():
            if not syn or not lead:
                continue
            if (
                _content_overlap_fraction(lead, syn)
                >= _CONTENT_OVERLAP_LIMIT
            ):
                issues.append(
                    f"{label}: lead echoes the {syn_name} synthesis "
                    "(>= 60% shared content words)"
                )
                break
        # Story-anchored vs section-anchored (designer adjudication): a
        # lead sharing a verbatim trigram with its own section's
        # synthesis FIRST SENTENCE is the observed collision class
        # ("Ship the plumbing first." on both surfaces); a lead opening
        # on the same word as its section's synthesis is the cheaper
        # cousin. Both checked against the bullet's OWN section.
        own_syn = syntheses.get(section_name)
        if own_syn and lead:
            if _shares_ngram(lead, _synthesis_first_sentence(own_syn), n=3):
                issues.append(
                    f"{label}: lead shares a verbatim 3-word run with the "
                    f"{section_name} synthesis's first sentence -- the "
                    "digest is story-anchored, the synthesis is "
                    "section-anchored; they must not collide"
                )
            if (
                _first_word(lead)
                and _first_word(lead) == _first_word(own_syn)
            ):
                issues.append(
                    f"{label}: lead opens with the same word as the "
                    f"{section_name} synthesis"
                )

    if total_words > _DIGEST_TOTAL_MAX_WORDS:
        issues.append(
            f"digest totals {total_words} words across leads + sentences "
            f"(HARD budget {_DIGEST_TOTAL_MAX_WORDS})"
        )
    return issues


def _build_digest_prompt(
    pulse_story: SummaryBlock,
    eligible: list[IssueSection],
    *,
    can_split: bool,
    takes: list[str],
    anti_patterns: list[str],
) -> str:
    """Assemble the digest prompt. Single string, one call per day -- no
    cache split needed at this volume."""
    def _story_lines(st: SummaryBlock) -> str:
        take = getattr(st, "take", None) or "(none)"
        return (
            f"  - story_id: {st.story_id}\n"
            f"    headline: {st.headline}\n"
            f"    body: {st.summary}\n"
            f"    take (do NOT paraphrase): {take}"
        )

    section_blocks: list[str] = []
    for sec in eligible:
        ids = [st.story_id for st in sec.stories]
        syn = (sec.synthesis or "").strip() or "(none)"
        section_blocks.append(
            f"SECTION {sec.name} -- allowed story_ids: {ids}\n"
            f"  synthesis (do NOT echo): {syn}\n"
            + "\n".join(_story_lines(st) for st in sec.stories)
        )
    sections_block = "\n\n".join(section_blocks)

    takes_block = "\n".join(f'  - "{t}"' for t in takes if t)
    anti_block = ""
    if anti_patterns:
        anti_block = (
            "\nANTI-PATTERNS -- the editor's catalogue applies in full; do "
            "not use these constructions:\n"
            + "\n".join(f"  - {ap}" for ap in anti_patterns)
            + "\n"
        )
    expected = 1 + len(eligible)
    if can_split:
        split_rule = (
            f"- Return EXACTLY {expected} bullets -- or {expected + 1} "
            "ONLY when one single section's stories genuinely split into "
            "two distinct threads (the two bullets sit adjacent). The "
            "DEFAULT is one bullet per section; never split to fill "
            "space, and NEVER split two sections.\n"
        )
    else:
        split_rule = (
            f"- Return EXACTLY {expected} bullets -- one per section "
            "listed below; the 5-bullet ceiling is already reached, so "
            "no section may split.\n"
        )

    return f"""\
You are writing "The 30-second read" for today's AI Vector issue -- the
skim digest rendered ABOVE The Pulse. A reader who reads ONLY this block
leaves knowing what moved today. Plain English, Australian English, no
em-dashes, no HTML markup (the template supplies the bold treatment).

STRUCTURE (fixed; code rejects violations):
- Bullet 1 covers The Pulse story.
- Then one bullet per section, in this order: {[s.name for s in eligible]}.
{split_rule}- Every bullet cites story_ids from the allowed lists below, primary
  story FIRST. A bullet is STORY-ANCHORED: it names a thing that
  happened -- one story's specifics, falsifiable against exactly that
  story. Naming a section-wide pattern is the SYNTHESIS's job, not the
  digest's; a bullet that reads as a pattern fails. Cite extra ids only
  when the sentence genuinely draws a fact from them.

EACH BULLET = a bold LEAD + one SENTENCE.
- LEAD: 3-6 words ending in a full stop. A NAMING -- what is this
  about -- never an imperative, never a question, and no artifact names
  a senior practitioner would not recognise (describe the artifact
  instead). It must not echo any section synthesis below, must not
  share a 3-word run with one, and must not open with the same word as
  its section's synthesis.
- SENTENCE: exactly ONE sentence, 14-22 words (up to 26 ONLY as a
  semicolon-list of at most 3 clauses). Concrete: the number, the
  actor, the mechanism. Falsifiable against the primary story.
- TOTAL BUDGET: 100 words across ALL bullets, leads included. HARD.
  Do the arithmetic before writing: with {expected} bullets that is
  about {100 // max(expected, 1)} words per bullet INCLUDING its lead,
  so most sentences must sit near the 14-word floor. COUNT the total
  before returning.

DECONFLICTION (checked in code):
- The takes listed below each OPEN their story on the page; the digest
  must NOT paraphrase any of them. Compress the STORIES -- different
  words, different angle.
- Bullet 1 compresses the Pulse story afresh; it must not paraphrase
  the Pulse take.
{anti_block}
THE PULSE (bullet 1) -- allowed story_ids: ['{pulse_story.story_id}']
{_story_lines(pulse_story)}

{sections_block}

TAKES ALREADY OPENING TODAY'S STORIES (do not paraphrase any):
{takes_block}

Return ONLY a single JSON object (no markdown fences, no commentary):

{{
  "bullets": [
    {{"section": "pulse", "lead": "<3-6 words.>", "sentence": "<one 14-22 word sentence>", "story_ids": ["<primary first>"]}},
    ...
  ]
}}
"""


def _parse_digest_json(raw: str) -> list[dict[str, Any]] | None:
    """Parse the digest LLM output into a list of bullet dicts. Structural
    shape only; the spec checks live in ``_digest_violations``."""
    payload = _extract_json_object(raw)
    if not isinstance(payload, dict):
        return None
    bullets = payload.get("bullets")
    if not isinstance(bullets, list) or not bullets:
        return None
    out: list[dict[str, Any]] = []
    for entry in bullets:
        if not isinstance(entry, dict):
            return None
        story_ids = entry.get("story_ids")
        out.append({
            "section": str(entry.get("section") or "").strip(),
            # Same deterministic wrapper-tag strip as the synthesis parse
            # (the bold lead invites "<strong>" the way the italic
            # synthesis invited "<em>").
            "lead": _strip_simple_markup(str(entry.get("lead") or "")),
            "sentence": _strip_simple_markup(
                str(entry.get("sentence") or "")
            ),
            "story_ids": [
                str(s) for s in story_ids if isinstance(s, str)
            ] if isinstance(story_ids, list) else [],
        })
    return out


def _generate_digest(
    pulse_section: IssueSection,
    sections: list[IssueSection],
    *,
    anti_patterns: list[str] | None = None,
) -> list[DigestBullet] | None:
    """Generate the issue digest ("The 30-second read"). Returns validated
    ``DigestBullet``s or ``None`` -- the degradation path is ALWAYS no
    digest, never a padded or partial one.

    Deterministic scaffold (code): bullet 1 is the Pulse; one bullet per
    section with >= 2 stories, in layout order; floor 3 bullets (else no
    digest today); ceiling 5 (the LLM may split ONE section into two
    adjacent bullets only when headroom exists). LLM judgment: the words.
    One corrective retry on JSON failure or spec violations; a second
    miss degrades to None with a WARNING."""
    if not pulse_section.stories:
        return None
    pulse_story = pulse_section.stories[0]
    by_name = {sec.name: sec for sec in sections}
    eligible = [
        by_name[name] for name in _DIGEST_SECTION_ORDER
        if name in by_name
        and len(by_name[name].stories) >= _DIGEST_MIN_SECTION_STORIES
    ]
    expected = 1 + len(eligible)
    if expected < _DIGEST_MIN_BULLETS:
        _LOG.info(
            "summarise: digest floor not met (pulse + %d eligible "
            "section(s) = %d bullet(s) < %d) -- no digest today (never "
            "pad)",
            len(eligible), expected, _DIGEST_MIN_BULLETS,
        )
        return None

    takes: list[str] = []
    pulse_take = getattr(pulse_story, "take", None)
    if pulse_take:
        takes.append(pulse_take)
    for sec in sections:
        for st in sec.stories:
            t = getattr(st, "take", None)
            if t:
                takes.append(t)
    syntheses: dict[str, str | None] = {
        sec.name: sec.synthesis for sec in sections
    }
    allowed_ids: dict[str, list[str]] = {
        "pulse": [pulse_story.story_id],
        **{
            sec.name: [st.story_id for st in sec.stories]
            for sec in eligible
        },
    }
    eligible_names = [sec.name for sec in eligible]
    can_split = expected < _DIGEST_MAX_BULLETS

    prompt = _build_digest_prompt(
        pulse_story, eligible,
        can_split=can_split,
        takes=takes,
        anti_patterns=list(anti_patterns or []),
    )
    temperature = float(os.getenv("LLM_TEMPERATURE_SUMMARISE", "0.6"))

    def _attempt(use_prompt: str) -> list[dict[str, Any]] | None:
        try:
            raw = _llm_call(use_prompt, temperature=temperature,
                            max_tokens=1200)
        except Exception:  # noqa: BLE001
            _LOG.warning("summarise: digest LLM call failed")
            return None
        return _parse_digest_json(raw)

    bullets = _attempt(prompt)
    violations = (
        _digest_violations(
            bullets, pulse_id=pulse_story.story_id,
            eligible_names=eligible_names, allowed_ids=allowed_ids,
            takes=takes, syntheses=syntheses,
        )
        if bullets is not None else ["no parseable digest returned"]
    )
    if violations:
        _LOG.info(
            "summarise: digest missed on first pass (%s) -- retrying once",
            "; ".join(violations),
        )
        corrective = (
            prompt
            + "\n\nCORRECTION -- Your previous response violated the "
            "digest spec:\n"
            + "\n".join(f"  - {v}" for v in violations)
            + "\n\nRewrite the JSON fixing every violation. Keep the "
            "structure (bullet 1 = Pulse, then the sections in order), "
            "count the words, and return ONLY JSON."
        )
        bullets = _attempt(corrective)
        violations = (
            _digest_violations(
                bullets, pulse_id=pulse_story.story_id,
                eligible_names=eligible_names, allowed_ids=allowed_ids,
                takes=takes, syntheses=syntheses,
            )
            if bullets is not None else ["no parseable digest returned"]
        )
        if violations:
            _LOG.warning(
                "summarise: digest STILL off-spec after retry (%s) -- "
                "shipping WITHOUT a digest (the degradation path is no "
                "skim section, never a degenerate one)",
                "; ".join(violations),
            )
            return None

    assert bullets is not None  # violations empty implies parse succeeded
    try:
        out = [
            DigestBullet(
                lead=b["lead"], sentence=b["sentence"],
                story_ids=b["story_ids"],
            )
            for b in bullets
        ]
    except Exception:  # noqa: BLE001 -- model-level structural caps
        _LOG.exception(
            "summarise: DigestBullet validation failed -- shipping "
            "without a digest"
        )
        return None
    _LOG.info(
        "summarise: digest produced (%d bullets, %d words total)",
        len(out),
        sum(_digest_bullet_words(b.lead, b.sentence) for b in out),
    )
    return out


def _persist_take_routes(
    sections: list[IssueSection], take_routes: dict[str, str]
) -> None:
    """Stamp the R1/R2/R3 route label onto each block that still carries
    its take (SummaryBlock v5, wave three).

    The label has been tracked in ``take_routes`` (keyed by cluster_id,
    updated again on the pulse rewrite path) since v0.23 but was never
    written. It is a GENERATION judgment -- the wave-two ruling: derived
    tags live at render, routes persist -- so it must be stored or it is
    lost. A block whose take was cut keeps ``take_route=None`` (no orphan
    labels), and an out-of-vocabulary label is dropped rather than fed to
    the model field (the parse normalises to R1/R2/R3, so this is a guard
    against a future sink writer, not a live path)."""
    valid = {"R1", "R2", "R3"}
    for section in sections:
        for block in section.stories:
            if not block.take:
                continue
            route = take_routes.get(block.story_id)
            block.take_route = route if route in valid else None


def digest_verify_violations(issue: Issue) -> list[str]:
    """The POST-VERIFY digest bar (ratified with the digest contract): a
    claim the verifier marked ``unverifiable`` or ``contradicted`` may not
    appear in the digest.

    Deterministic code, no LLM. For every digest bullet, every flagged
    claim of every story the bullet cites is compared against the
    bullet's full text (lead + sentence): a near-verbatim containment or
    a >= 60% content-word overlap (shared helper/threshold) is a
    violation.

    SEAM NOTE: run.py's order is summarise -> verify, so at generation
    time ``SummaryBlock.verification`` is None everywhere and this
    returns [] (summarise still calls it defensively). The enforcement
    point is verify.py AFTER it denormalises verdicts onto issue.json
    (wired in wave three, 2026-08-09: ``verify._digest_bar_violations``):
    violations surface in the verify report note -- advisory, the gate
    consumes them separately; verify never mutates the digest."""
    if not issue.digest:
        return []
    verification_by_story: dict[str, Any] = {}
    for block in issue.pulse.stories:
        verification_by_story[block.story_id] = block.verification
    for section in issue.sections:
        for block in section.stories:
            verification_by_story[block.story_id] = block.verification

    flagged_verdicts = {"unverifiable", "contradicted"}
    issues: list[str] = []
    for i, bullet in enumerate(issue.digest):
        bullet_text = f"{bullet.lead} {bullet.sentence}"
        bullet_norm = re.sub(r"\s+", " ", bullet_text).strip().lower()
        for sid in bullet.story_ids:
            verification = verification_by_story.get(sid)
            if verification is None:
                continue
            for claim in verification.claims:
                if claim.verdict not in flagged_verdicts:
                    continue
                claim_norm = re.sub(
                    r"\s+", " ", claim.claim or ""
                ).strip().lower().rstrip(".")
                contained = bool(claim_norm) and claim_norm in bullet_norm
                overlapping = (
                    _content_overlap_fraction(claim.claim, bullet_text)
                    >= _CONTENT_OVERLAP_LIMIT
                )
                if contained or overlapping:
                    issues.append(
                        f"digest bullet {i + 1} carries a claim the "
                        f"verifier marked {claim.verdict!r} (story {sid}): "
                        f"{claim.claim!r}"
                    )
    return issues


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
# source_excerpts.jsonl -- the summarise -> verify hand-off sidecar.
#
# DESIGN.md "source_excerpts.jsonl": summarise grounds each story's summary on
# the source bodies it fetched into ``_SOURCE_EXCERPT_CACHE`` (per-process,
# keyed by URL). Historically those bodies were thrown away after the prompt
# was built ("bodies are NOT persisted to items.jsonl"). The advisory verify
# stage needs the EXACT text the summary was grounded on so it judges against
# identical source material. We persist, keyed by URL, only the excerpts the
# final issue's blocks actually reference (``SummaryBlock.source_urls``).
#
# Why join on source_urls (not the raw top-3 item URLs): verify reads each
# block's ``source_urls`` and unions the excerpts for exactly those URLs. The
# cache holds every URL we fetched (top-3 items per cluster); ``source_urls``
# is a deterministic trust-sorted subset. Persisting the source_urls set keeps
# the sidecar aligned 1:1 with what verify will look up -- no orphan keys, no
# missing keys for URLs verify cares about.
#
# Empty-fetch policy: a 403 / empty / failed fetch lands in the cache as an
# empty string (see ``_fetch_source_excerpt``). We RECORD it with an empty
# ``excerpt`` rather than omitting it -- consistent and explicit. Verify then
# sees "this URL was used but yielded no text" and (per its rubric) marks the
# affected claims ``unverifiable`` rather than mistaking a missing key for a
# join failure.
#
# Staging-only, ephemeral: never promoted to released (paths helper docstring).
#
# Re-fetch fallback (2026-08-10): because the sidecar never leaves staging, a
# re-verify after ``aiv revise --released`` on a checkout without the staging
# dir finds it gone. ``verify._refetch_source_excerpts`` then re-fetches the
# issue blocks' source_urls via ``_fetch_source_excerpt``, seeds
# ``_SOURCE_EXCERPT_CACHE``, and rewrites the sidecar via
# ``_write_source_excerpts`` (same pinned record shape). That text is a FRESH
# fetch -- it can differ from what the summariser originally grounded on, and
# verify logs that caveat loudly. No behaviour change when the sidecar exists.
# ---------------------------------------------------------------------------

_SOURCE_EXCERPTS_SCHEMA_VERSION = 1
"""Record-shape version for source_excerpts.jsonl lines. Bump if the per-line
record shape changes (per the architect's pinned contract)."""


def _write_source_excerpts(
    path: Path, issue: Issue, fetched_at: _dt.datetime
) -> None:
    """Persist the source excerpts the issue's summaries were grounded on.

    One JSON object per line, keyed by the source URL, in the architect's
    pinned record shape:

        {"schema_version": 1, "url": <str>, "excerpt": <str>,
         "fetched_at": <ISO-8601 UTC>, "story_id": <cluster_id>}

    Only URLs that appear in some block's ``source_urls`` are written, read
    from the per-run ``_SOURCE_EXCERPT_CACHE`` populated during
    summarisation. A URL absent from the cache (defensive: it should always
    be present, since source_urls is a subset of the fetched item URLs) is
    recorded with an empty excerpt so the join surface stays complete.

    De-duped by URL across the whole issue (the first block that references a
    URL wins its ``story_id``); a URL shared by two stories is written once.

    Atomic: write to ``<path>.tmp``, fsync, then ``os.replace`` -- same-day
    re-runs overwrite cleanly, and a crash mid-write never leaves a partial
    sidecar that verify would mis-parse.
    """
    fetched_iso = fetched_at.isoformat()
    seen: set[str] = set()
    records: list[dict[str, Any]] = []

    def _collect(block: SummaryBlock) -> None:
        for raw_url in block.source_urls:
            url = str(raw_url)
            if url in seen:
                continue
            seen.add(url)
            excerpt = _SOURCE_EXCERPT_CACHE.get(url, "")
            records.append({
                "schema_version": _SOURCE_EXCERPTS_SCHEMA_VERSION,
                "url": url,
                "excerpt": excerpt,
                "fetched_at": fetched_iso,
                "story_id": block.story_id,
            })

    for story in issue.pulse.stories:
        _collect(story)
    for section in issue.sections:
        for story in section.stories:
            _collect(story)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False))
            fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _LOG.info(
        "summarise: persisted %d source excerpt(s) -> %s",
        len(records), path,
    )


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
