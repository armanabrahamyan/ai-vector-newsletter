"""
src/summarise.py -- AI Vector summarisation stage.

Reads ``data/staging/YYYY-MM-DD/ranked.jsonl``, takes top-N stories, writes
prose summaries via the LLM, assembles the four sections + The Pulse, and
writes ``data/staging/YYYY-MM-DD/issue.json``.

Owner: LLM Engineer (per docs/TEAM.md, .claude/agents/llm-engineer.md).
Contract: docs/DESIGN.md "Issue / IssueSection / SummaryBlock" + "Cross-time
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
   guidance (Australian English, signal-dense, embed-direction-and-finance-
   in-prose), the editorial-focus skill, and the finance-lens skill; the
   LLM returns ``{headline, summary}``. Direction and finance lens are
   woven into the summary prose when relevant -- never as separate fields
   or labels (v0.2 / schema v4).
4. Assemble sections per editorial rules: Pulse, For leaders, For geeks,
   Also notable. Each top-N story is placed in exactly one section.
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

SUMMARISE_PROMPT_VERSION = "v0.2"
"""Pydantic-validated version string. Audit tag:
``summarise-v0.2-2026-05-23``. v0.2: Australian English, shorter budgets
(50-80 words standard / 90-120 Pulse), direction + finance lens embedded in
prose (no separate fields), sections collapsed to pulse / leaders / geeks /
notable."""

PULSE_PROMPT_VERSION = "v0.2"
"""Audit tag: ``pulse-v0.2-2026-05-23``. v0.2 mirrors summarise."""

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

A daily AI newsletter for engineers, data scientists, and senior leaders
in financial services. Warm but not chummy. Specific not generic.
Signal-dense not word-dense. Wit lives in nouns and verbs.

The reader is busy and intelligent. Give them enough to decide whether to
click through to the source -- not a re-read of the article. They keep up
with us; they deep-dive when they want to.

AUSTRALIAN ENGLISH throughout. Examples that matter:
  organise / optimise / prioritise / realise / recognise / analyse
  behaviour / colour / favourable / centre / fibre / theatre / defence
  licence (noun) / license (verb) / practise (verb) / practice (noun)
  programme (plan) / program (software) / grey / travelled / modelled
  sceptical / judgement (general use)
Dates: "23 May 2026" (not "May 23, 2026" or "5/23/26").
Times: "9 a.m." or "09:00" (not "9 AM").

LENGTH BUDGETS (hard)
  - headline: <= ~90 chars (tight noun phrase, ideally <= 12 words).
  - summary: 50-80 words for standard stories.
              90-120 words for the Pulse -- a story is Pulse-class when its
              score is >= 70 AND the breakdown clearly addresses what
              shifts today / what to anticipate / what's practical now.
              Otherwise standard.

EMBED, DON'T LABEL

The newsletter's first principles -- direction ("where this points") and
the financial-services lens -- are RHYTHM, not scaffolding. Weave them
into the prose WHEN RELEVANT. NEVER write "Where this points:" or "Finance
lens:" as labelled sentences or phrases. When a story has no direction
worth pointing to and no finance angle, just write the news cleanly.
Silence beats filler.

  GOOD: "Diffusion LMs are not new, but Nemotron's release makes them
  practical for latency-sensitive serving today; vLLM and TGI will likely
  add diffusion backends within a quarter."
    -- direction is baked into the second clause. No header.

  GOOD: "Banks under SR 11-7 get a credible new option for documenting LLM
  evals -- the methodology is public and citable."
    -- finance lens is woven into the verb-frame. No "Finance lens:" prefix.

  BAD:  "Where this points: open-source catches up."
    -- labelled, generic, filler.

  BAD:  "Finance lens: this could apply to fraud teams."
    -- labelled, speculative, no nameable angle.

DON'T do these
  - Don't open with "In the fast-paced world of AI..." or any cousin.
  - Don't say "in conclusion," "moreover," "furthermore," "notably."
  - Don't moralise ("this raises important questions about...").
  - Point, don't list. Bullets only when load-bearing (tools, repos, steps).
  - Link out; never reproduce full articles.
  - Don't pad. Adjectives must earn their place. "Major" is almost always
    cuttable.
"""

_EDITORIAL_FOCUS_BLOCK = """\
EDITORIAL FOCUS -- a reminder while writing

AI Vector is heavier on Agentic AI and Generative AI; traditional ML
appears only when load-bearing. The signal filter:

  1. TODAY -- does this change something a DS / engineer would do this week?
  2. TOMORROW -- shifts what to anticipate in the next 1-6 months?
  3. PRACTICAL -- is there a repo / API / technique / eval to use NOW?

Land at least one of these in the summary. Two is great. Don't drift into
generic "AI is changing X" territory -- if the story didn't earn its place,
the ranker dropped it; if it's here, name WHY.
"""

_FINANCE_LENS_BLOCK = """\
FINANCE LENS -- guidance for when to weave the FS angle into prose

AI Vector is an AI newsletter with a finance eye, not a finance newsletter
with AI as a topic. The lens ADDS value when it earns its place; it does
not FILTER OUT the field. When you spot a tight FS angle, embed it in the
summary prose -- never label it. Most days, most stories will NOT carry
a finance angle. That is correct.

Where an angle earns its place (use as cue, not checklist):
  - Trading / markets ML; fraud / AML / KYC; model risk & governance
    (SR 11-7, PRA SS1/23); productionising under regulatory constraints
    (on-prem, data residency, audit, redaction); agents in finance;
    benchmark / eval relevance to FS teams.
  - Leadership cues: vendor lock-in shifts, regulatory movement, build-
    vs-buy implications.

If the angle is speculative ("could apply to a bank") or generic ("affects
financial services"), skip it. Name a role, a constraint, or a regulatory
hook -- or don't bring it up.
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
    run_date = date or _dt.datetime.now(_dt.timezone.utc).date()

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

    # --- Section assembly ------------------------------------------------
    # v0.2: pulse -> leaders -> geeks -> notable (no where_heading; builders
    # subsumed into geeks). "For leaders" first per Arman's reading order.
    pulse_section, leaders_section, geeks_section, notable_section = \
        _assemble_sections(blocks)

    # --- Construct + validate -------------------------------------------
    # issue_number is intentionally None in staging output. Numbering is a
    # release-time operation; see DESIGN.md "Issue Number Registry" +
    # "Archive: staging vs canonical".
    issue = Issue(
        issue_number=None,
        date=run_date,
        pulse=pulse_section,
        sections=[leaders_section, geeks_section, notable_section],
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
        "summarised top %d: pulse=%r / leaders: %d / geeks: %d / "
        "notable: %d | issue #(staging -- not yet numbered) -> %s",
        len(blocks), pulse_headline,
        len(leaders_section.stories),
        len(geeks_section.stories),
        len(notable_section.stories),
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
    finance_angle no longer separate fields -- both live in summary prose."""
    headline: str
    summary: str


def _summarise_one(
    story: RankedStory,
    cluster: Cluster,
    items: list[Item],
    callbacks: list[_CallbackRef],
) -> SummaryBlock | None:
    """One LLM call. Returns a validated ``SummaryBlock`` or ``None`` if
    the call / parse / validation failed after the retry budget."""
    temperature = float(os.getenv("LLM_TEMPERATURE_SUMMARISE", "0.6"))
    prompt = _build_summary_prompt(story, cluster, items, callbacks)

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
        )
    except Exception:  # noqa: BLE001
        _LOG.exception(
            "summarise: SummaryBlock validation failed for cluster_id=%s -- "
            "skipping. draft=%s",
            cluster.cluster_id, draft,
        )
        return None
    return block


def _build_summary_prompt(
    story: RankedStory,
    cluster: Cluster,
    items: list[Item],
    callbacks: list[_CallbackRef],
) -> str:
    """Assemble the per-story summarisation prompt with voice + skills
    inlined and callback context attached when present."""
    item_lines: list[str] = []
    for it in items[:5]:  # a bit more context than the rank prompt
        title = it.title.strip()
        summary = (it.raw_summary or "").strip()
        if len(summary) > 800:
            summary = summary[:800].rstrip() + "..."
        item_lines.append(
            f"- [{it.source}, trust={it.trust_weight}] {title}\n"
            f"  url: {it.url}\n"
            f"  summary: {summary}"
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
You are writing one story for AI Vector -- a daily AI newsletter for
engineers, data scientists, and senior leaders, with a financial-services
lens. The cluster was already RANKED and selected for the issue; your job
is to write it well.

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
- Write a headline (<= ~90 chars, tight noun phrase) and a summary
  (50-80 words standard; 90-120 words if Pulse-class per the budget rule
  above).
- Weave direction ("where this points") and finance lens INTO the prose
  WHEN RELEVANT. NEVER use labels like "Where this points:" or "Finance
  lens:". If neither earns its place, write the news cleanly without.
- If callback context is present and the connection is tight, weave a
  brief reference in ("last week we flagged X; today's update is..."). If
  the connection is weak, skip it.
- Australian English throughout (organise, optimise, behaviour, etc.).
- Link out; never reproduce full articles.

Return ONLY a single JSON object (no markdown fences, no commentary):

{{
  "headline": "<editorial headline>",
  "summary": "<50-80 word body, 90-120 if Pulse-class>"
}}
"""


def _call_and_parse_summary(
    prompt: str, temperature: float, cluster_id: str
) -> _SummaryDraft | None:
    """LLM call + retry on parse failure (one retry, mirrors rank.py)."""
    attempts = JSON_RETRY_BUDGET + 1
    current_prompt = prompt
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
            return draft
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
    return None


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
    return _SummaryDraft(
        headline=headline.strip(),
        summary=summary.strip(),
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


def _pick_source_urls(items: list[Item], k: int) -> list[str]:
    """Top-k unique URLs from cluster members, sorted by trust_weight
    (then by recency as a tiebreaker). Deterministic given the inputs."""
    sorted_items = sorted(
        items,
        key=lambda it: (it.trust_weight, it.published_at),
        reverse=True,
    )
    seen: set[str] = set()
    out: list[str] = []
    for it in sorted_items:
        url = str(it.url)
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= k:
            break
    return out


# ---------------------------------------------------------------------------
# Section assembly.
# ---------------------------------------------------------------------------

def _assemble_sections(
    blocks: list[tuple[RankedStory, SummaryBlock]],
) -> tuple[IssueSection, IssueSection, IssueSection, IssueSection]:
    """Place every summarised story into exactly one section. Returns the
    four sections in display order: pulse, leaders, geeks, notable.

    Editorial routing rules (v0.2 -- post 2026-05-23 voice update):
      - Pulse: highest-scoring story that hits >= 2 signal-filter dimensions
        (significance, builder_utility, freshness_momentum >= 70). Fallback
        (logged): highest breakdown.significance.
      - Leaders: stories tagged `leader`. Hard cap at 4. First, per Arman.
      - Geeks: stories tagged `builder`, OR tagged `general` with
        builder_utility >= 70. Hard cap at 5. Subsumes the old "builders"
        section.
      - Notable: everything left, in score-desc order.

    Direction notes and finance angles are now embedded in summary prose,
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

    # --- For leaders (first per Arman's reading order) ------------------
    leader_ids = _pick_leaders(blocks, unplaced)
    for cid in leader_ids:
        unplaced.discard(cid)

    # --- For geeks (absorbs the old "builders" section) -----------------
    geek_ids = _pick_geeks(blocks, unplaced)
    for cid in geek_ids:
        unplaced.discard(cid)

    # --- Also notable ---------------------------------------------------
    notable_ids = [
        story.cluster_id for story, _ in blocks if story.cluster_id in unplaced
    ]

    pulse_section = IssueSection(
        name="pulse",
        stories=[by_id[pulse_id][1]],
    )
    leaders_section = IssueSection(
        name="leaders",
        stories=[by_id[cid][1] for cid in leader_ids],
    )
    geeks_section = IssueSection(
        name="geeks",
        stories=[by_id[cid][1] for cid in geek_ids],
    )
    notable_section = IssueSection(
        name="notable",
        stories=[by_id[cid][1] for cid in notable_ids],
    )
    return (pulse_section, leaders_section, geeks_section, notable_section)


def _signal_dimensions_hit(story: RankedStory) -> int:
    """Approximate "signal-filter dimensions hit" from the rank breakdown.
    The editorial-focus skill names three dimensions: today / tomorrow /
    practical. Mapping (best-effort, documented here):
      - today      ~ freshness_momentum >= 70
      - tomorrow   ~ significance       >= 70
      - practical  ~ builder_utility    >= 70
    Counts how many of those clear the 70 anchor."""
    b = story.breakdown
    hits = 0
    if b.get("freshness_momentum", 0) >= 70:
        hits += 1
    if b.get("significance", 0) >= 70:
        hits += 1
    if b.get("builder_utility", 0) >= 70:
        hits += 1
    return hits


def _pick_pulse(
    blocks: list[tuple[RankedStory, SummaryBlock]],
) -> str | None:
    """The Pulse selection rule (v0.2 -- direction_note is no longer a
    separate field, so we no longer filter on its presence).

    Primary: highest-scoring story that hits >= 2 signal-filter dimensions
    (significance, builder_utility, freshness_momentum >= 70).

    Fallback (logged): highest breakdown.significance among all blocks.
    Returns None only if blocks is empty (caller aborts)."""
    if not blocks:
        return None
    primary = [
        (story, block) for story, block in blocks
        if _signal_dimensions_hit(story) >= 2
    ]
    if primary:
        # blocks already arrives in score-desc order; pick the first.
        return primary[0][0].cluster_id
    _LOG.warning(
        "summarise: no Pulse-class story today (none hit >= 2 signal "
        "dimensions); using top-significance fallback"
    )
    fallback = max(
        blocks,
        key=lambda sb: (sb[0].breakdown.get("significance", 0), sb[0].score),
    )
    return fallback[0].cluster_id


def _pick_leaders(
    blocks: list[tuple[RankedStory, SummaryBlock]],
    available: set[str],
) -> list[str]:
    """Tagged 'leader' and not yet placed. Hard cap at 4. v0.2: leaders is
    the first section after Pulse, per editorial direction."""
    out: list[str] = []
    for story, _block in blocks:
        if story.cluster_id not in available:
            continue
        if "leader" in set(story.audience_tags):
            out.append(story.cluster_id)
        if len(out) >= 4:
            break
    return out


def _pick_geeks(
    blocks: list[tuple[RankedStory, SummaryBlock]],
    available: set[str],
) -> list[str]:
    """Tagged 'builder', OR tagged 'general' with builder_utility >= 70.
    Hard cap at 5. v0.2: subsumes the old "builders" section -- most
    builders read as geeks; one warmer section beats two narrower ones."""
    out: list[str] = []
    for story, _block in blocks:
        if story.cluster_id not in available:
            continue
        tags = set(story.audience_tags)
        if "builder" in tags or (
            "general" in tags and story.breakdown.get("builder_utility", 0) >= 70
        ):
            out.append(story.cluster_id)
        if len(out) >= 5:
            break
    return out


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
