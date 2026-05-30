r"""
src/rank.py -- AI Vector ranking stage.

Reads `data/staging/YYYY-MM-DD/clusters.jsonl`, scores each surviving cluster
with the LLM against `config/rubric.yaml`, and writes
`data/staging/YYYY-MM-DD/ranked.jsonl` sorted by score descending.

Round B (DESIGN.md "Archive: staging vs canonical"):
  * Reads + writes today under STAGING.
  * Reads `data/published_urls.txt` (canonical-only) for the post-rank
    URL guard. A URL Arman drafted but never released stays eligible.

Owner: LLM Engineer (per docs/internal/TEAM.md, .claude/agents/llm-engineer.md).
Contract: docs/internal/DESIGN.md "RankedStory" + "ranked.jsonl" + "Cross-issue
article-level dedup" (post-rank guard).

Key responsibilities
--------------------
1. Load today's clusters; tolerate missing file.
2. Apply the **post-rank cross-issue URL guard**: drop any cluster whose
   every constituent item URL is in `data/published_urls.txt`.
3. For each surviving cluster, call the LLM with a prompt that **inlines**
   (a) the editorial-focus tier filter + signal filter and (b) the rubric
   from `config/rubric.yaml`. Ask for JSON conforming to the rubric's
   `llm_output` schema.
4. Parse the JSON (one retry on parse failure). Recompute `score` from
   `breakdown` x weights (pydantic enforces consistency; we recompute to
   absorb LLM arithmetic noise *before* pydantic raises).
5. Construct `RankedStory` (pydantic validates). Skip on validation failure;
   never crash the issue.
6. Atomic write `ranked.jsonl` sorted by score desc.

LLM client
----------
Branch on `LLM_PROVIDER`:
- `anthropic` (default) -- uses the `anthropic` SDK.
- `bedrock` -- uses `boto3.client("bedrock-runtime", ...)`.
- `openai` / `litellm` / `ollama` -- NotImplementedError for v0; punt to
  LiteLLM later.

Prompt versioning
-----------------
`RANK_PROMPT_VERSION` is the pydantic-validated version string written into
`RankedStory.prompt_version`. The dated tag below is recorded in logs and
in module-level comments for human audit; pydantic requires `v\d+(\.\d+)*`
so the on-disk version is "v0.1" -- bump on prompt content changes.

Dated audit tag: rank-v0.1-2026-05-23.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml
from pydantic import ValidationError

from src import paths
from src.models import RUBRIC_WEIGHTS, Cluster, Item, RankedStory


# ---------------------------------------------------------------------------
# Module constants -- declared at top per the LLM Engineer spec.
# ---------------------------------------------------------------------------

RANK_PROMPT_VERSION = "v0.5"
r"""Pydantic-validated version string (pattern: ^v\d+(\.\d+)*$).

Audit tag: ``rank-v0.5-2026-05-26``. Bump (e.g. ``v0.5``) when the prompt
content changes -- so the eval harness can correlate score movement against
prompt revisions (risk-register item #6 in docs/internal/TEAM.md).
"""

# Failed experiment: v0.2 (2026-05-24, #75) sharpened the `big_picture`
# audience-tag definition with workflow/governance/decision-process examples
# and an anchor reminder ("workflow shifts should score >= 60"). Eval improved
# on the 26-cluster labelled subset (Spearman 0.569 -> 0.654, tier
# disagreements 2 -> 0) but the prompt collapsed scores on the other 17
# unlabelled clusters -- staging went from ~11 surviving stories to 2.
# Reverted in #77. Probable culprit: the LLM inverted the ">= 60 anchor"
# into "< 60 for everything else". If you're tempted to re-try this change,
# read the #77 postmortem first; full-corpus shape assertions (not just
# labelled-corpus quality) are a precondition.
#
# Task #81 (2026-05-25): continuation penalty added as DETERMINISTIC
# post-LLM logic (see _apply_prior_coverage_penalty), not a prompt change.
# The penalty caps `breakdown["significance"]` at 50 for any cluster with
# `prior_coverage_ref` set, then score is recomputed via the normal
# weighted-sum path. Lesson from #75/#77: prefer deterministic post-
# processing over prompt edits where the rule is a hard constraint.
#
# v0.3 (2026-05-25, task #88): prompt text is unchanged from v0.1; the
# audit-tagged log message wording was renamed from "continuation penalty"
# to "prior-coverage penalty" to match the schema rename
# (Cluster.cross_time_ref -> Cluster.prior_coverage_ref). The rename
# makes the semantic honest: the field flags topical RECURRENCE, not
# temporal progression. Bumping the prompt-version string here so the
# audit trail records the wording change at the rank-output level even
# though scoring behaviour is byte-identical.
#
# v0.4 (2026-05-25, task #89): NOVELTY DETECTION. Prior-coverage clusters
# previously got a flat significance cap of 50 regardless of whether the
# recurrence carried new info. Anchor case: c_fe59351a8d336457 (NuExtract3
# Reddit thread linking to HuggingFace -- pure duplicate of Issue #1
# Pulse) cleared the bar with sig=50 + hands_on_utility=100, landing at
# score=55 in Hands-On as an effective duplicate. The fix is in two parts:
# (a) PROMPT: when a cluster carries prior_coverage_ref, look up the prior
# headline + summary excerpt from data/released/*/issue.json (last 14 days)
# and inject a PRIOR COVERAGE block, asking the LLM to set a "novelty"
# field: "none" / "minor" / "major". Tightly scoped: one new block, no
# rewrite of existing prompt body (lesson from #75/#77).
# (b) DETERMINISTIC CAP: novelty="none" caps significance at 25 (cuts the
# story via _assign_initial_tier's sig<=25 rule), "minor" at 40, "major"
# keeps the existing 50 cap. Missing/invalid novelty defaults to 50 (don't
# punish when uncertain). The novelty value is persisted on RankedStory
# so the eval harness can see which calls fired.
#
# v0.5 (2026-05-26): AUDIENCE_TAGS TIGHTENING. Runtime log on 2026-05-26
# showed cluster c_fb359151221d4e62 lost to a pydantic ValidationError --
# the LLM returned audience_tags=[] and the model (List min_length=1)
# rejected. The existing skip-on-validation-failure path worked as the
# safety net intended, but burning a cluster on one bad sample is wasteful.
# Two-part fix, surgical so we don't repeat the #75/#77 cliff:
# (a) PROMPT: add an explicit AUDIENCE TAGS block stating the list must
# never be empty, quoting the allowed values, and naming "general" as the
# fallback when nothing else fits. Additive only -- no rewrite of the
# existing prompt body, no anchor reminders the LLM could invert.
# (b) RETRY: _call_and_parse_rank now catches pydantic.ValidationError in
# addition to JSON parse failure, with a corrective nudge that quotes the
# specific validation error back to the LLM. Reuses the existing single-
# retry budget; second failure still skips the cluster.

MAX_ITEMS_IN_CLUSTER_PROMPT = 3
"""How many member items to inline in the per-cluster prompt body."""

JSON_RETRY_BUDGET = 1
"""One retry on JSON parse failure; second failure -> skip the cluster."""

_LLM_CONCURRENCY_DEFAULT = 8
"""Default per-stage LLM concurrency (read from ``LLM_CONCURRENCY`` env var).

The per-cluster ranking calls are fully independent -- scoring cluster A
doesn't depend on cluster B -- so we fan them out across a thread pool.
The Anthropic SDK is sync and thread-safe (each call constructs its own
client in ``_llm_call_anthropic``); ThreadPoolExecutor wraps the existing
sync code without an asyncio migration.

Default 8 is conservative for Anthropic build-tier-1 (50 RPM). Tier-4
(4000 RPM) users can crank this to 50+. Cap at ``_LLM_CONCURRENCY_MAX``
so a typo (``LLM_CONCURRENCY=800``) can't melt the API.
"""

_LLM_CONCURRENCY_MAX = 50
"""Sanity ceiling on ``LLM_CONCURRENCY`` -- prevents typo disasters."""

_DEFAULT_RUBRIC_PATH = Path("config/rubric.yaml")
_SOURCES_YAML_PATH = Path("config/sources.yaml")

_LOG = logging.getLogger("ai_vector.rank")


# ---------------------------------------------------------------------------
# Tier-threshold defaults. Mirrors config/rubric.yaml ``tier_thresholds`` for
# the fallback path when the YAML is unreadable. Keep in lockstep with the
# YAML or _assign_initial_tier silently drifts -- the eval-harness module-
# integrity check should flag drift, but defaults here matter when the
# YAML round-trips through a corrupt edit.
#
# Schema (mirrors the YAML):
#   cut: {max_score: int, max_significance: int}
#   on_the_radar: {min_score: int}
#   promote_to_section: {min_score: int}
# ---------------------------------------------------------------------------

_DEFAULT_TIER_THRESHOLDS: dict[str, dict[str, int]] = {
    "cut": {"max_score": 39, "max_significance": 25},
    "on_the_radar": {"min_score": 40},
    "promote_to_section": {"min_score": 70},
}


# ---------------------------------------------------------------------------
# Editorial-focus filter -- INLINED into the prompt per the LLM Engineer
# spec. Pasted (not linked) so the prompt is self-contained for audit and
# offline review. Source of truth: .claude/skills/editorial-focus.md.
# When the skill changes substantively, mirror it here and bump
# RANK_PROMPT_VERSION.
# ---------------------------------------------------------------------------

_EDITORIAL_FOCUS_BLOCK = """\
EDITORIAL FOCUS (apply BEFORE scoring against the rubric):

AI Vector is heavier on Agentic AI and Generative AI. Traditional ML lands
only when load-bearing for the field today. We optimise for strong signal:
what shifts how readers work today, what to anticipate tomorrow, what's
practical to use right now.

THREE-TIER FILTER

Tier 1 -- covers by default (the heart of the publication):
  Agentic AI: tool-use agents, multi-step reasoning, autonomous workflows,
  agent frameworks (Claude Code, LangGraph, Letta, ...), agent runtimes,
  orchestration patterns, coding/research/customer/ops agents, agent evals
  and failure modes, computer-use / browser-use, agentic infrastructure.
  Generative AI: foundation-model releases that change the capability
  ceiling or floor, new training / post-training techniques with practical
  implications, inference advances (latency, cost, context window,
  modality), multimodal when the capability is actually new, open-source
  launches that change deployment calculus, RAG / structured outputs /
  prompt techniques / context engineering when there's real signal.

Tier 2 -- covers only when LOAD-BEARING:
  Traditional / classical ML, only when one of: productionised at scale
  in a way that changes the practitioner's playbook; hybrid classical+LLM
  doing something neither could alone; new methodology the field will
  absorb; an FS-specific application (fraud / AML / credit / trading)
  where the technique materially moves the needle.
  Default-skeptical. If you can't name WHY this changes how a DS or
  engineer works this quarter, it's not load-bearing.

Tier 3 -- OUT (these should score significance <= 25):
  Vendor announcements with no capability shift; model-numbers-go-brrr
  news with no practical takeaway; AI-tangential ("AI in X" tropes);
  hype-cycle pieces; opinion essays with no underlying news; "thought
  leadership" with no specifics; re-summaries of last week's news;
  stock / earnings commentary masquerading as AI news.

SIGNAL FILTER (applied on top of the tiers)
For every Tier-1 or Tier-2 candidate, ask:
  1. TODAY  -- changes something a DS or engineer would do this week?
  2. TOMORROW -- shifts what to anticipate over the next 1-6 months?
  3. PRACTICAL -- can the reader USE something from this now (repo,
     paper with code, technique, API, eval, benchmark)?

A story should hit at least one clearly. Two is great. Three is Pulse
material. Zero -- even in Tier 1 -- is buzz; floor it.

CALIBRATION REMINDER
Significance = (tier match) x (signal-filter dimensions passed).
Tier-3 stories MUST score significance <= 25 per the rubric's pre-filter
rule. The rubric is for the survivors.
"""


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def rank(date: _dt.date | None = None) -> list[RankedStory]:
    """Score every cluster with the LLM. Returns the ranked list (score desc)
    and writes ``data/staging/<date>/ranked.jsonl`` atomically.

    Parameters
    ----------
    date
        Issue date (UTC). Defaults to today's UTC date.

    Returns
    -------
    list[RankedStory]
        Sorted by ``score`` descending. May be empty if there are no
        clusters or every cluster errors out.

    Round B path model (DESIGN.md "Archive: staging vs canonical"):
      * Reads today's clusters/items from STAGING.
      * Reads `data/published_urls.txt` (canonical-only exclusion index).
      * Writes ranked.jsonl to STAGING.
    """
    run_date = date or _dt.date.today()

    clusters_in = paths.clusters_path(run_date, canonical=False)
    items_in = paths.items_path(run_date, canonical=False)
    ranked_out = paths.ranked_path(run_date, canonical=False)

    clusters = _load_clusters(clusters_in)
    if not clusters:
        _LOG.warning("rank: no clusters found at %s; writing empty ranked.jsonl",
                     clusters_in)
        _atomic_write_jsonl(ranked_out, [])
        return []

    items_by_id = _load_items_index(items_in)
    published_urls = _load_published_urls(paths.PUBLISHED_URLS_PATH)

    # --- Step 2: cross-issue post-cluster guard ----------------------------
    survivors, dropped = _drop_all_previously_published(
        clusters, items_by_id, published_urls
    )
    if dropped:
        _LOG.info(
            "rank: dropped %d clusters where every member URL was previously "
            "published", dropped
        )

    rubric_block = _build_rubric_block(_DEFAULT_RUBRIC_PATH)
    tier_thresholds = _load_tier_thresholds(_DEFAULT_RUBRIC_PATH)

    # --- Step 3+4: LLM scoring ---------------------------------------------
    # Fan out per-cluster LLM calls across a thread pool. The calls are fully
    # independent and the Anthropic / Bedrock / OpenAI-compatible client paths
    # each construct their own client per call, so sharing nothing across
    # threads is the default. Concurrency is configurable via
    # ``LLM_CONCURRENCY`` (default 8, capped at ``_LLM_CONCURRENCY_MAX`` to
    # bound typo-disasters); same-day re-runs still overwrite ``ranked.jsonl``
    # atomically because results are sorted by score below, not by future
    # completion order.
    trust_weights = _load_trust_weights(_SOURCES_YAML_PATH)
    concurrency = _resolve_llm_concurrency()
    ranked: list[RankedStory] = []
    llm_errors = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        future_to_cluster = {
            pool.submit(
                _rank_one,
                cluster=cluster,
                items_by_id=items_by_id,
                rubric_block=rubric_block,
                trust_weights=trust_weights,
                tier_thresholds=tier_thresholds,
                today=run_date,
            ): cluster
            for cluster in survivors
        }
        for future in as_completed(future_to_cluster):
            cluster = future_to_cluster[future]
            try:
                story = future.result()
            except Exception:  # noqa: BLE001 -- never crash the issue
                # ``_rank_one`` already catches its own known failure modes
                # (JSON parse, pydantic validation, LLM call) and returns
                # ``None`` -- this outer except is the belt-and-braces guard
                # for truly-unexpected exceptions (thread-level failures,
                # network panics) so one bad cluster doesn't poison the issue.
                _LOG.exception(
                    "rank: LLM error for cluster_id=%s; skipping",
                    cluster.cluster_id,
                )
                llm_errors += 1
                continue
            if story is None:
                llm_errors += 1
                continue
            ranked.append(story)
            _LOG.info(
                "[cluster %s] score=%d (sig:%d ho:%d bp:%d fs:%d fm:%d) tags=%s",
                cluster.cluster_id,
                story.score,
                story.breakdown.get("significance", 0),
                story.breakdown.get("hands_on_utility", 0),
                story.breakdown.get("big_picture_relevance", 0),
                story.breakdown.get("financial_services_impact", 0),
                story.breakdown.get("freshness_momentum", 0),
                list(story.audience_tags),
            )

    # Sort by score desc so on-disk ordering is deterministic from the
    # rubric, not from future-completion order.
    ranked.sort(key=lambda r: r.score, reverse=True)

    # --- Step 5: atomic write (to staging) --------------------------------
    _atomic_write_jsonl(
        ranked_out,
        (json.loads(r.model_dump_json()) for r in ranked),
    )

    scores = [r.score for r in ranked]
    top_score = max(scores) if scores else 0
    median_score = int(statistics.median(scores)) if scores else 0
    _LOG.info(
        "ranked %d clusters | %d dropped (all-previously-released) | "
        "%d LLM errors | top-score %d / median %d -> %s",
        len(ranked), dropped, llm_errors, top_score, median_score, ranked_out,
    )
    return ranked


# ---------------------------------------------------------------------------
# Concurrency knob.
# ---------------------------------------------------------------------------

def _resolve_llm_concurrency() -> int:
    """Read ``LLM_CONCURRENCY`` from the environment, clamp to sane bounds.

    Returns an int in ``[1, _LLM_CONCURRENCY_MAX]``. On parse failure (non-int
    value) we log a warning and fall back to ``_LLM_CONCURRENCY_DEFAULT`` --
    a typo in the env var should not crash the issue.
    """
    raw = os.getenv("LLM_CONCURRENCY")
    if raw is None or not raw.strip():
        return _LLM_CONCURRENCY_DEFAULT
    try:
        value = int(raw.strip())
    except ValueError:
        _LOG.warning(
            "rank: LLM_CONCURRENCY=%r is not an int; falling back to %d",
            raw, _LLM_CONCURRENCY_DEFAULT,
        )
        return _LLM_CONCURRENCY_DEFAULT
    return max(1, min(value, _LLM_CONCURRENCY_MAX))


# ---------------------------------------------------------------------------
# Loading helpers.
# ---------------------------------------------------------------------------

def _load_clusters(path: Path) -> list[Cluster]:
    """Read ``clusters.jsonl`` line-by-line. Tolerate missing file."""
    if not path.exists():
        return []
    out: list[Cluster] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                out.append(Cluster.model_validate(payload))
            except Exception:  # noqa: BLE001
                _LOG.exception(
                    "rank: bad cluster record at %s:%d -- skipping",
                    path, lineno,
                )
                continue
    return out


def _load_items_index(path: Path) -> dict[str, Item]:
    """Read ``items.jsonl`` into ``{Item.id: Item}``. Tolerate missing file."""
    if not path.exists():
        _LOG.warning("rank: items.jsonl missing at %s -- URLs/summaries will "
                     "be unavailable for prompt + dedup guard", path)
        return {}
    out: dict[str, Item] = {}
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
                    "rank: bad item record at %s:%d -- skipping",
                    path, lineno,
                )
                continue
            out[item.id] = item
    return out


def _load_published_urls(path: Path) -> set[str]:
    """Read the cumulative URL exclusion index. Tolerate missing file."""
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8") as fh:
        return {line.strip() for line in fh if line.strip()}


_PRIOR_COVERAGE_LOOKBACK_DAYS = 14
"""How far back to walk the released archive for `_lookup_prior_coverage`.
Matches the cluster.py prior-coverage chain window so we never look up a
ref that the cluster stage couldn't have produced."""

_PRIOR_COVERAGE_EXCERPT_CHARS = 200
"""Length of the prior summary excerpt injected into the rank prompt. The
LLM needs the gist, not the whole story; 200 chars is roughly the first
sentence or two -- enough to disambiguate "same product, different thread"
from "substantive update"."""


def _lookup_prior_coverage(
    prior_cluster_id: str,
    *,
    today: _dt.date | None = None,
) -> tuple[str, str] | None:
    """Find a previously-released SummaryBlock matching ``prior_cluster_id``.

    Walks ``data/released/*/issue.json`` over the last
    ``_PRIOR_COVERAGE_LOOKBACK_DAYS`` days (oldest first -- the first match
    is the chain root, the most useful baseline). For each issue, scans
    ``pulse.stories`` and ``sections[*].stories`` for a SummaryBlock whose
    ``story_id`` equals ``prior_cluster_id``.

    Returns ``(headline, summary_excerpt)`` on the first hit, where
    ``summary_excerpt`` is the first ``_PRIOR_COVERAGE_EXCERPT_CHARS`` of
    the prior summary (suffixed with ``"..."`` on truncation). Returns
    ``None`` when no match is found within the window -- the caller then
    skips the PRIOR COVERAGE prompt injection (the deterministic cap still
    fires; we just don't ask the LLM for novelty).

    Tolerates missing files, malformed JSON, and unexpected shapes -- this
    is a best-effort enrichment, not a contract. A bad release should not
    crash ranking.

    Task #89.
    """
    today = today or _dt.date.today()
    cutoff = today - _dt.timedelta(days=_PRIOR_COVERAGE_LOOKBACK_DAYS)
    released = [d for d in paths.all_released_dates() if d >= cutoff]
    # Oldest-first so the chain root wins when the same story_id appears
    # on multiple released days (it shouldn't, by design, but be defensive).
    for d in sorted(released):
        path = paths.issue_path(d, canonical=True)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for story in _iter_issue_stories(payload):
            if story.get("story_id") != prior_cluster_id:
                continue
            headline = story.get("headline")
            summary = story.get("summary")
            if not isinstance(headline, str) or not isinstance(summary, str):
                continue
            excerpt = summary.strip()
            if len(excerpt) > _PRIOR_COVERAGE_EXCERPT_CHARS:
                excerpt = excerpt[:_PRIOR_COVERAGE_EXCERPT_CHARS].rstrip() + "..."
            return headline.strip(), excerpt
    return None


def _iter_issue_stories(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield every SummaryBlock dict in an issue.json payload, across the
    pulse section and the remaining sections. Defensive against missing /
    malformed keys -- skips anything that isn't a dict."""
    pulse = payload.get("pulse")
    if isinstance(pulse, dict):
        for s in pulse.get("stories") or []:
            if isinstance(s, dict):
                yield s
    sections = payload.get("sections") or []
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict):
                continue
            for s in section.get("stories") or []:
                if isinstance(s, dict):
                    yield s


def _load_trust_weights(sources_yaml: Path) -> dict[str, int]:
    """Best-effort read of ``config/sources.yaml`` for trust weights. Used to
    prioritise which item summaries land in the prompt body. Tolerates a
    missing file or unexpected shape (returns empty dict)."""
    if not sources_yaml.exists():
        return {}
    try:
        data = yaml.safe_load(sources_yaml.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        _LOG.warning("rank: could not parse %s -- proceeding without trust "
                     "weights", sources_yaml)
        return {}
    out: dict[str, int] = {}
    sources = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(sources, list):
        return out
    for entry in sources:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        weight = entry.get("trust_weight", 3)
        if isinstance(name, str) and isinstance(weight, int):
            out[name] = weight
    return out


# ---------------------------------------------------------------------------
# Cross-issue post-cluster guard (DESIGN.md "Cross-issue article-level dedup").
# ---------------------------------------------------------------------------

def _drop_all_previously_published(
    clusters: list[Cluster],
    items_by_id: dict[str, Item],
    published_urls: set[str],
) -> tuple[list[Cluster], int]:
    """Drop any cluster whose EVERY member URL is already in
    ``data/published_urls.txt``. A cluster with at least one fresh URL
    survives."""
    if not published_urls:
        return clusters, 0
    survivors: list[Cluster] = []
    dropped = 0
    for cluster in clusters:
        urls = _cluster_urls(cluster, items_by_id)
        if urls and all(u in published_urls for u in urls):
            dropped += 1
            continue
        survivors.append(cluster)
    return survivors, dropped


def _cluster_urls(cluster: Cluster, items_by_id: dict[str, Item]) -> list[str]:
    """Resolve a cluster's member URLs (as plain strings) via the items
    index. Items not found are silently skipped -- the dedup guard then
    correctly does NOT drop a cluster whose items we cannot resolve."""
    urls: list[str] = []
    for item_id in cluster.item_ids:
        item = items_by_id.get(item_id)
        if item is None:
            continue
        urls.append(str(item.url))
    return urls


# ---------------------------------------------------------------------------
# Rubric prompt block -- loaded from config/rubric.yaml at runtime.
# ---------------------------------------------------------------------------

def _build_rubric_block(rubric_path: Path) -> str:
    """Serialise the rubric YAML into a prompt-ready text block. The LLM
    sees the criteria, weights, descriptions, and per-anchor calibration --
    everything needed to score without ambiguity. Falls back to a minimal
    weights-only block if the YAML is unreadable."""
    try:
        data = yaml.safe_load(rubric_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        _LOG.exception("rank: could not load rubric at %s -- using minimal "
                       "weights-only block", rubric_path)
        return _minimal_rubric_block()

    if not isinstance(data, dict):
        return _minimal_rubric_block()

    criteria = data.get("criteria") or []
    lines: list[str] = []
    lines.append("RUBRIC (config/rubric.yaml @ "
                 f"{data.get('rubric_version', 'unknown')}):")
    lines.append("")
    lines.append("Score each criterion 0-100. Weights sum to 100; the final")
    lines.append("score is the weighted sum. Anchors describe what each band")
    lines.append("means -- use them to calibrate.")
    lines.append("")
    for crit in criteria:
        if not isinstance(crit, dict):
            continue
        name = crit.get("name", "?")
        weight = crit.get("weight", "?")
        desc = (crit.get("description") or "").strip()
        anchors = crit.get("anchors") or {}
        lines.append(f"- {name} (weight {weight})")
        if desc:
            lines.append(f"  description: {desc}")
        if isinstance(anchors, dict):
            for anchor_value in ("0", "25", "50", "75", "100"):
                anchor_text = anchors.get(anchor_value)
                if isinstance(anchor_text, str) and anchor_text.strip():
                    lines.append(f"    @{anchor_value}: {anchor_text.strip()}")
        lines.append("")

    audience_tags = data.get("audience_tags", {})
    if isinstance(audience_tags, dict):
        taxonomy = audience_tags.get("taxonomy") or []
        guidance = (audience_tags.get("guidance") or "").strip()
        if taxonomy:
            lines.append(f"AUDIENCE TAGS taxonomy: {taxonomy}")
        if guidance:
            lines.append(f"AUDIENCE TAGS guidance: {guidance}")
        lines.append("")

    lines.append(
        "Pre-filter reminder: Tier-3 clusters (per EDITORIAL FOCUS above) "
        "MUST score significance <= 25."
    )
    return "\n".join(lines)


def _minimal_rubric_block() -> str:
    """Backstop rubric block from in-code ``RUBRIC_WEIGHTS`` -- used only
    when ``config/rubric.yaml`` cannot be read. Keeps rank.py runnable in
    degraded-config conditions, surfaced by the warning above."""
    crits = ", ".join(f"{k} ({v})" for k, v in RUBRIC_WEIGHTS.items())
    return (
        "RUBRIC (fallback -- YAML unreadable):\n"
        f"Weighted criteria: {crits}.\n"
        "Tier-3 stories MUST score significance <= 25."
    )


def _load_tier_thresholds(
    rubric_path: Path = _DEFAULT_RUBRIC_PATH,
) -> dict[str, dict[str, int]]:
    """Load the ``tier_thresholds`` block from config/rubric.yaml.

    Returns the in-code ``_DEFAULT_TIER_THRESHOLDS`` (copied) when the YAML
    is unreadable or missing the block -- keeps rank.py runnable in
    degraded-config conditions. We only copy fields we recognise; unknown
    keys are ignored to keep the schema additive-only.
    """
    fallback = {k: dict(v) for k, v in _DEFAULT_TIER_THRESHOLDS.items()}
    try:
        data = yaml.safe_load(rubric_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        _LOG.exception(
            "rank: could not load rubric at %s for tier_thresholds -- "
            "using in-code defaults", rubric_path,
        )
        return fallback
    if not isinstance(data, dict):
        return fallback
    block = data.get("tier_thresholds") or {}
    if not isinstance(block, dict):
        _LOG.warning(
            "rank: tier_thresholds in %s is not a mapping; using defaults",
            rubric_path,
        )
        return fallback
    out = fallback
    for top_key in ("cut", "on_the_radar", "promote_to_section"):
        section = block.get(top_key)
        if isinstance(section, dict):
            for sub_key, value in section.items():
                if isinstance(value, int) and sub_key in out[top_key]:
                    out[top_key][sub_key] = value
    return out


# ---------------------------------------------------------------------------
# Per-cluster ranking call.
# ---------------------------------------------------------------------------

@dataclass
class _ParsedScore:
    """Intermediate shape -- LLM-returned breakdown, recomputed score, and
    bookkeeping. Not persisted; just a clean structure between the parser
    and the pydantic constructor."""
    breakdown: dict[str, int]
    audience_tags: list[str]
    rationale: str
    novelty: str | None = None
    """Task #89: LLM-returned novelty assessment relative to prior coverage.
    One of {"none", "minor", "major"} when the cluster carries
    ``prior_coverage_ref`` and the LLM produced a valid value; ``None``
    when the cluster is fresh OR when the LLM omitted / returned an
    invalid value. Drives ``_apply_prior_coverage_penalty``'s cap selection."""


def _rank_one(
    cluster: Cluster,
    items_by_id: dict[str, Item],
    rubric_block: str,
    trust_weights: dict[str, int],
    *,
    tier_thresholds: dict[str, dict[str, int]] | None = None,
    today: _dt.date | None = None,
) -> RankedStory | None:
    """Score one cluster with the LLM. Returns ``None`` on parse/validation
    failure after retries -- the caller logs and skips. Never raises on
    LLM/parse errors so a single bad cluster doesn't poison the issue."""
    prompt = _build_rank_prompt(
        cluster, items_by_id, rubric_block, trust_weights, today=today,
    )
    temperature = float(os.getenv("LLM_TEMPERATURE_RANK", "0.2"))
    # Default to the in-code constants when no thresholds were threaded
    # through (older test paths). Mirrors _load_tier_thresholds's
    # fallback shape so _assign_initial_tier's contract is uniform.
    thresholds = (
        tier_thresholds
        if tier_thresholds is not None
        else {k: dict(v) for k, v in _DEFAULT_TIER_THRESHOLDS.items()}
    )

    def _build_story_from_parsed(parsed: _ParsedScore) -> RankedStory:
        """Apply deterministic post-LLM penalties + tier assignment, then
        construct ``RankedStory``. Raises ``pydantic.ValidationError`` on
        validation failure so the retry loop in ``_call_and_parse_rank``
        can re-prompt (v0.5 -- task: audience_tags=[] case).

        Side effects on ``parsed.breakdown`` are idempotent across retries
        because each retry produces a fresh ``_ParsedScore``. Score is
        recomputed AFTER the caps mutate breakdown, so the pydantic
        invariant ``score == weighted_sum(breakdown)`` holds exactly.
        """
        # Task #81: deterministic post-LLM prior-coverage penalty. If the
        # cluster carries a prior_coverage_ref (we've covered this topic on
        # a previous day), cap breakdown["significance"] at 50 (rubric
        # anchor 50 = "single signal-filter dimension hit"). A recurring
        # topic is rarely the day's freshest signal; allowing it to score
        # 65+ on significance crowds genuinely-new stories out of Pulse /
        # Big Picture slots. Score is recomputed below so the pydantic
        # invariant `score == weighted_sum(breakdown)` still holds. Logged
        # when fired so operators see the rule's effect.
        _apply_prior_coverage_penalty(parsed, cluster)

        # Task #86: deterministic post-LLM freshness-inferred penalty. If
        # EVERY item in the cluster carries
        # `extras["freshness_inferred"] == "true"` (fetch.py couldn't trust
        # the feed's pubdates -- FCA News pattern), cap
        # `breakdown["freshness_momentum"]` at 30. Composes cleanly with
        # the continuation penalty above: that one caps significance, this
        # one caps freshness_momentum, both recompute score via
        # `_weighted_score`.
        _apply_freshness_inferred_penalty(parsed, cluster, items_by_id)

        # Recompute score from breakdown x weights -- ignore any
        # LLM-returned `score` field. Pydantic enforces the same invariant;
        # recomputing here absorbs LLM arithmetic noise (and the penalties
        # above) before pydantic raises.
        score = _weighted_score(parsed.breakdown)

        # The LLM picks audience tags; tier is the editorial slot. rank.py
        # now writes the FULL slot (schema v3, 2026-05-30): cut, on_the_radar,
        # hands_on, or big_picture. summarise.py gates its pickers strictly
        # on this -- no scavenging across tiers. Pulse is picked downstream
        # from the union of the two head-section tiers.
        tier = _assign_initial_tier(
            score, parsed.breakdown, parsed.audience_tags, thresholds,
        )

        return RankedStory(
            cluster_id=cluster.cluster_id,
            score=score,
            breakdown=parsed.breakdown,
            audience_tags=parsed.audience_tags,  # type: ignore[arg-type]
            rationale=parsed.rationale,
            tier=tier,
            prompt_version=RANK_PROMPT_VERSION,
            novelty=parsed.novelty,  # type: ignore[arg-type]
        )

    return _call_and_parse_rank(
        prompt, temperature, cluster.cluster_id,
        build_story=_build_story_from_parsed,
    )


def _build_prior_coverage_block(
    cluster: Cluster, *, today: _dt.date | None = None
) -> str:
    """Render the PRIOR COVERAGE prompt block for a cluster, or empty string.

    Returns ``""`` when:
      * the cluster has no ``prior_coverage_ref`` (fresh story), OR
      * the lookup found no matching released SummaryBlock within the
        14-day window (released archive doesn't have what we need to ground
        the LLM's novelty judgment -- skip the injection and rely on the
        deterministic cap alone).

    Task #89. The block is surgical: one new section, no rewrite of the
    existing prompt body. Lesson from #75/#77 -- keep prompt changes
    additive and the LLM-facing wording explicit ("MUST include a `novelty`
    field"). The schema fragment is added at the call site so the prompt
    JSON shape mirrors what we inject here.
    """
    if cluster.prior_coverage_ref is None:
        return ""
    found = _lookup_prior_coverage(cluster.prior_coverage_ref, today=today)
    if found is None:
        return ""
    prior_headline, prior_excerpt = found
    return f"""
PRIOR COVERAGE -- this cluster topically matches a previously-published story:

  Headline: {prior_headline}
  Summary excerpt: {prior_excerpt}

Given this prior coverage, your JSON response MUST include a "novelty" field:
  - "none"  = no material new information vs the prior coverage (same product,
              different Reddit thread, same paper announcement)
  - "minor" = small updates only (v2 patch release, performance number, minor
              correction)
  - "major" = substantive new information (new findings, fundamental update,
              change in conclusions)
"""


def _build_rank_prompt(
    cluster: Cluster,
    items_by_id: dict[str, Item],
    rubric_block: str,
    trust_weights: dict[str, int],
    *,
    today: _dt.date | None = None,
) -> str:
    """Assemble the per-cluster ranking prompt. Editorial-focus + rubric
    are inlined (the prompt is self-contained for offline audit).

    When ``cluster.prior_coverage_ref`` is set, attempts to inject a PRIOR
    COVERAGE block (task #89) with the prior story's headline + summary
    excerpt so the LLM can return a ``novelty`` field discriminating true
    continuation from effective duplicate.
    """
    items = _select_items_for_prompt(cluster, items_by_id, trust_weights)
    items_block_lines: list[str] = []
    for it in items:
        title = it.title.strip()
        summary = (it.raw_summary or "").strip()
        # Cap each item's summary at 600 chars in the prompt to keep input
        # tokens predictable; raw_summary is already capped at 8 KB by Item.
        if len(summary) > 600:
            summary = summary[:600].rstrip() + "..."
        items_block_lines.append(
            f"- [{it.source}, trust={it.trust_weight}] {title}\n  {summary}"
        )
    items_block = "\n".join(items_block_lines) or "  (no item summaries available)"

    prior_coverage_block = _build_prior_coverage_block(cluster, today=today)
    # When we injected a PRIOR COVERAGE block, ask for the novelty field in
    # the JSON schema; otherwise leave the schema unchanged so fresh stories
    # don't get an extra knob to tweak. The schema fragment is appended after
    # `rationale`, separated by the necessary comma.
    novelty_schema_hint = (
        ',\n  "novelty": "<one of: \\"none\\", \\"minor\\", \\"major\\">"'
        if prior_coverage_block
        else ""
    )

    return f"""\
You are scoring a single AI-news cluster for AI Vector -- a daily,
agent-assisted AI newsletter for engineers, data scientists, and the
senior leaders they work with, with a financial-services lens.

{_EDITORIAL_FOCUS_BLOCK}
{rubric_block}

CLUSTER
canonical_title: {cluster.canonical_title}
cluster_id: {cluster.cluster_id}
sources: {list(cluster.sources)}
earliest_published: {cluster.earliest_published.isoformat()}
size: {cluster.size}
has_prior_coverage: {"yes (prior_coverage_ref=" + cluster.prior_coverage_ref + ")" if cluster.prior_coverage_ref else "no"}

ITEMS (top {MAX_ITEMS_IN_CLUSTER_PROMPT} by source trust):
{items_block}
{prior_coverage_block}
INSTRUCTIONS
Score the cluster against the rubric. Apply the EDITORIAL FOCUS pre-filter
first -- Tier-3 stories MUST score significance <= 25. Audience tags are
independent of score: pick the subset of {{hands_on, big_picture, finance, general}}
that this story is actually for. `hands_on` = practitioner (DS / engineer)
audience; `big_picture` = senior-leader audience.

AUDIENCE TAGS -- REQUIRED: pick at least one tag from exactly this set:
"hands_on", "big_picture", "finance", "general". The list must never be
empty. If no other tag fits, use "general".

Return ONLY a single JSON object (no markdown fences, no commentary):

{{
  "cluster_id": "{cluster.cluster_id}",
  "score": <int 0-100>,
  "breakdown": {{
    "significance": <int 0-100>,
    "hands_on_utility": <int 0-100>,
    "big_picture_relevance": <int 0-100>,
    "financial_services_impact": <int 0-100>,
    "freshness_momentum": <int 0-100>
  }},
  "audience_tags": [<at least one of: "hands_on", "big_picture", "finance", "general">],
  "rationale": "<one sentence, <= 240 chars, specific not generic>"{novelty_schema_hint}
}}
"""


def _select_items_for_prompt(
    cluster: Cluster,
    items_by_id: dict[str, Item],
    trust_weights: dict[str, int],
) -> list[Item]:
    """Pick up to ``MAX_ITEMS_IN_CLUSTER_PROMPT`` items, prioritising by
    trust weight (sources.yaml mapping, fallback to item's own trust_weight)
    then by recency. Deterministic given the inputs."""
    resolved: list[Item] = []
    for item_id in cluster.item_ids:
        it = items_by_id.get(item_id)
        if it is not None:
            resolved.append(it)
    resolved.sort(
        key=lambda it: (
            trust_weights.get(it.source, it.trust_weight),
            it.published_at,
        ),
        reverse=True,
    )
    return resolved[:MAX_ITEMS_IN_CLUSTER_PROMPT]


def _call_and_parse_rank(
    prompt: str,
    temperature: float,
    cluster_id: str,
    *,
    build_story: "_BuildStoryFn | None" = None,
) -> "_ParsedScore | RankedStory | None":
    """Issue the LLM call, parse JSON, retry once on parse OR pydantic
    validation failure with a corrective nudge. Returns ``None`` after the
    retry budget is spent -- the caller logs and skips.

    Two modes:

    * Legacy (``build_story=None``): returns the parsed shape (or ``None``).
      Retries on JSON parse failure only. Kept for test compatibility and
      for any future caller that wants the intermediate ``_ParsedScore``.

    * Validator-aware (``build_story`` callable): calls ``build_story`` on
      the parsed shape -- typically constructs a ``RankedStory`` which can
      raise ``pydantic.ValidationError``. Both ``JSONDecodeError`` /
      structural parse failures AND ``ValidationError`` consume from the
      same single-retry budget. v0.5 (2026-05-26) -- ships the
      audience_tags=[] retry path.
    """
    attempts = JSON_RETRY_BUDGET + 1
    current_prompt = prompt
    for attempt in range(1, attempts + 1):
        try:
            raw = _llm_call(current_prompt, temperature=temperature, max_tokens=800)
        except Exception:  # noqa: BLE001
            _LOG.exception(
                "rank: LLM call failed for cluster_id=%s (attempt %d/%d)",
                cluster_id, attempt, attempts,
            )
            return None

        parsed = _parse_rank_json(raw)
        if parsed is None:
            _LOG.warning(
                "rank: JSON parse failed for cluster_id=%s (attempt %d/%d)",
                cluster_id, attempt, attempts,
            )
            if attempt < attempts:
                current_prompt = (
                    "Your previous response was not valid JSON matching the "
                    "schema below. Return JSON ONLY (no markdown fences, no "
                    "prose) matching the schema. Original request follows.\n\n"
                    + prompt
                )
            continue

        # Legacy callers (and the unit tests for the parser) want the raw
        # parsed shape; skip the build_story validation hop entirely.
        if build_story is None:
            return parsed

        try:
            return build_story(parsed)
        except ValidationError as exc:
            # Quote the SPECIFIC validation error back to the LLM so the
            # retry has signal to act on. We pass the full str(exc) -- it's
            # already structured ("audience_tags: List should have at least
            # 1 item after validation, not 0") and short enough not to
            # blow up the input tokens.
            err_msg = str(exc)
            _LOG.warning(
                "rank: pydantic validation failed for cluster_id=%s "
                "(attempt %d/%d): %s",
                cluster_id, attempt, attempts, err_msg,
            )
            if attempt < attempts:
                current_prompt = (
                    "Your prior response failed validation: "
                    f"{err_msg}\n\n"
                    "Fix the failing field(s) and return the SAME JSON "
                    "schema. Reminder: `audience_tags` must contain at "
                    "least one of \"hands_on\", \"big_picture\", "
                    "\"finance\", \"general\" -- use \"general\" if no "
                    "other tag fits. Original request follows.\n\n"
                    + prompt
                )
    return None


# Forward declaration -- the callable shape `_rank_one` passes into
# `_call_and_parse_rank`. Exposed as a module-level alias so the signature
# stays readable; the actual return type is RankedStory.
from typing import Callable as _Callable
_BuildStoryFn = _Callable[["_ParsedScore"], "RankedStory"]


def _parse_rank_json(raw: str) -> _ParsedScore | None:
    """Parse and shallow-validate the rank LLM output. Returns ``None`` on
    any structural failure -- defers deep validation to pydantic via the
    ``RankedStory`` constructor."""
    payload = _extract_json_object(raw)
    if payload is None:
        return None
    try:
        breakdown = payload["breakdown"]
        if not isinstance(breakdown, dict):
            return None
        # Coerce values to int. Reject anything non-numeric.
        breakdown_int: dict[str, int] = {}
        for k, v in breakdown.items():
            if not isinstance(k, str):
                return None
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return None
            breakdown_int[k] = int(round(float(v)))
        # Rubric-key check: pydantic enforces exactness, but reject early
        # so the retry prompt has a chance to fix it before pydantic.
        if set(breakdown_int.keys()) != set(RUBRIC_WEIGHTS.keys()):
            return None
        tags = payload.get("audience_tags") or []
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            return None
        rationale = payload.get("rationale", "")
        if not isinstance(rationale, str) or not rationale.strip():
            return None
        # Task #89: optional novelty field (only present when the prompt
        # injected the PRIOR COVERAGE block). Accept only the three valid
        # tokens; anything else (including missing) becomes None and lets
        # the cap helper apply its default-to-major behaviour.
        novelty_raw = payload.get("novelty")
        if isinstance(novelty_raw, str) and novelty_raw.strip().lower() in {
            "none", "minor", "major",
        }:
            novelty = novelty_raw.strip().lower()
        else:
            novelty = None
        return _ParsedScore(
            breakdown=breakdown_int,
            audience_tags=list(tags),
            rationale=rationale.strip(),
            novelty=novelty,
        )
    except (KeyError, TypeError):
        return None


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Tolerant JSON extractor. Tries direct parse first; falls back to
    finding the outermost ``{...}`` span. Returns ``None`` on failure --
    triggers the retry path upstream."""
    raw = raw.strip()
    # Strip common markdown fences.
    if raw.startswith("```"):
        lines = raw.splitlines()
        # drop first fence line + last fence line if present
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(raw[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _weighted_score(breakdown: dict[str, int]) -> int:
    """Compute the final 0-100 score from the breakdown via
    ``RUBRIC_WEIGHTS`` -- mirrors the invariant in
    ``RankedStory._score_matches_weighted_breakdown``. Rounded to int so
    pydantic equality holds exactly."""
    raw = sum(
        (RUBRIC_WEIGHTS[name] / 100.0) * value
        for name, value in breakdown.items()
    )
    return max(0, min(100, round(raw)))


_PRIOR_COVERAGE_SIGNIFICANCE_CAP = 50
"""Default significance ceiling for stories with ``cluster.prior_coverage_ref``
set -- rubric anchor 50 = "single signal-filter dimension hit". Used when the
LLM did NOT return a usable ``novelty`` field (don't punish on uncertainty).
See ``_apply_prior_coverage_penalty``."""


_PRIOR_COVERAGE_NOVELTY_CAPS: dict[str, int] = {
    "none":  25,   # effective duplicate -- cuts via _assign_initial_tier sig<=25
    "minor": 40,   # incremental update -- still below typical Pulse threshold
    "major": 50,   # substantive new info -- existing #81 cap
}
"""Task #89: novelty-aware significance caps for prior-coverage clusters.

Anchor: the rubric's significance anchors map "we've seen this" (25) ->
"meaningful single-signal recurrence" (40) -> "new dimension on a known
story" (50). The caps mirror that ladder.

The 25 cap on novelty=="none" intentionally trips ``_assign_initial_tier``'s
``sig <= 25`` rule, so an effective duplicate is tiered as "cut" without
needing a separate cut-list. This is the smoking-gun fix for the 2026-05-25
NuExtract3 staging case (cluster c_fe59351a8d336457): a Reddit thread
linking to the same HuggingFace page we covered in Issue #1 Pulse landed in
Hands-On at score 55 because the flat cap-at-50 still let
hands_on_utility=100 carry the weighted total above the cut floor.
Capping significance at 25 instead drops the weighted score to ~40 AND
flips the tier to "cut" via the significance gate -- belt + braces."""


_FRESHNESS_INFERRED_CAP = 30
"""Freshness-momentum ceiling for clusters whose every member item carries
``extras["freshness_inferred"] == "true"`` (set by ``src/fetch.py`` when a
feed lacks per-item pubdates -- the FCA News pattern). Anchored between
rubric anchor 25 ("we don't actually know when this dropped") and 50 ("the
story has a fresh angle"). See ``_apply_freshness_inferred_penalty``."""


def _apply_prior_coverage_penalty(parsed: "_ParsedScore", cluster: Cluster) -> None:
    """Cap ``breakdown["significance"]`` for any cluster that has PRIOR
    COVERAGE (``cluster.prior_coverage_ref is not None``), with the cap
    chosen by the LLM-returned ``novelty`` value (task #89).

    Caps (see ``_PRIOR_COVERAGE_NOVELTY_CAPS``):
      * ``novelty == "none"``  -> 25  (effective duplicate; will be cut)
      * ``novelty == "minor"`` -> 40  (incremental update)
      * ``novelty == "major"`` -> 50  (substantive new info; #81 behaviour)
      * missing / invalid      -> 50  (don't punish on uncertainty)

    Task #81 origin -- a cluster with prior coverage is a topical recurrence
    of something we covered on a previous day. Letting the LLM score the
    recurrence at 65+ on significance crowded genuinely-new stories out
    (cf. the 2026-05-25 llama.cpp how-to-as-Pulse incident). #81 shipped a
    flat cap at 50; #89 splits it by novelty because flat-50 still allowed
    effective duplicates with high ``hands_on_utility`` to clear the
    weighted-score cut bar (the 2026-05-25 NuExtract3 Reddit-thread case).

    Deterministic, NOT a prompt change to the rubric body. The PRIOR
    COVERAGE block (added in v0.4) prompts the LLM for the novelty token;
    this function applies the cap. Mutates ``parsed.breakdown`` in place;
    score is recomputed by the caller via ``_weighted_score``.

    No-op when the cluster is fresh OR when the chosen cap is already at
    or above the LLM-returned significance. Logs the novelty + cap when
    the rule fires so operators can see which branch hit.
    """
    if cluster.prior_coverage_ref is None:
        return
    cap = _PRIOR_COVERAGE_NOVELTY_CAPS.get(
        parsed.novelty or "",
        _PRIOR_COVERAGE_SIGNIFICANCE_CAP,
    )
    before = parsed.breakdown.get("significance", 0)
    if before <= cap:
        return
    score_before = _weighted_score(parsed.breakdown)
    parsed.breakdown["significance"] = cap
    score_after = _weighted_score(parsed.breakdown)
    _LOG.info(
        "prior-coverage penalty applied to %s: novelty=%s, significance "
        "%d->%d, score %d->%d (prior_coverage_ref=%s; #81/#89)",
        cluster.cluster_id, parsed.novelty or "<missing>",
        before, cap, score_before, score_after, cluster.prior_coverage_ref,
    )


def _apply_freshness_inferred_penalty(
    parsed: "_ParsedScore",
    cluster: Cluster,
    items_by_id: dict[str, Item],
) -> None:
    """Task #86: cap ``breakdown["freshness_momentum"]`` at 30 when EVERY
    item in the cluster carries ``extras["freshness_inferred"] == "true"``.

    Rationale. Some feeds (anchor case: the FCA News RSS) publish entries
    without per-item ``pubDate`` elements; ``src/fetch.py`` (task #71)
    detects the pattern (all items share ``published_at == fetched_at``)
    and tags each with ``extras["freshness_inferred"] = "true"``. For those
    items we don't actually know when the story dropped -- it may be hours
    old, it may be a month old. Allowing the LLM to score them as if they
    were fresh silently inflates ``freshness_momentum`` and skews ranking.
    The cap of 30 sits between the rubric anchors at 25 ("we don't know")
    and 50 ("the story has a fresh angle"): inferred-freshness items
    shouldn't compete for the freshness-momentum ceiling, but we don't
    want to floor them either -- a real new FCA enforcement could still
    matter on its own merits via the other dimensions.

    Mixed-cluster guard. We only fire when EVERY resolved item carries the
    flag. A cluster with at least one item that has a real per-item pubdate
    (typically because other sources covered the same story) gives us a
    trustworthy freshness signal -- leave it untouched.

    Deterministic, NOT a prompt change. Mirrors ``_apply_prior_coverage_penalty``:
    mutate ``parsed.breakdown`` in place; the caller recomputes ``score``
    via ``_weighted_score``. Logs when the rule fires.
    """
    resolved = [items_by_id[i] for i in cluster.item_ids if i in items_by_id]
    if not resolved:
        return
    if not all(it.extras.get("freshness_inferred") == "true" for it in resolved):
        return
    before = parsed.breakdown.get("freshness_momentum", 0)
    if before <= _FRESHNESS_INFERRED_CAP:
        return
    score_before = _weighted_score(parsed.breakdown)
    parsed.breakdown["freshness_momentum"] = _FRESHNESS_INFERRED_CAP
    score_after = _weighted_score(parsed.breakdown)
    _LOG.info(
        "freshness-inferred penalty applied to %s: freshness_momentum "
        "%d->%d, score %d->%d (#86)",
        cluster.cluster_id, before, _FRESHNESS_INFERRED_CAP,
        score_before, score_after,
    )


def _assign_initial_tier(
    score: int,
    breakdown: dict[str, int],
    audience_tags: list[str],
    thresholds: dict[str, dict[str, int]],
) -> str:
    """Initial tier assignment per DESIGN.md note that tier is "the bridge
    between rank and summarise." Schema v3 (2026-05-30): rank.py now writes
    the FULL editorial slot, not just a floor. summarise.py gates its
    pickers strictly on tier -- no cross-tier scavenging -- so the routing
    decision lives here, where breakdown + audience_tags + thresholds are
    co-located.

    Logic (in order):
      1. Cut: score < cut.max_score OR significance <= cut.max_significance.
         The significance trapdoor is the editorial-focus skill's Tier-3
         rule -- vendor fluff and AI-tangential hype get floored at
         significance <= 25 by the prompt and cut here even if their other
         dimensions inflate the weighted score.
      2. On the Radar: score < promote_to_section.min_score. The
         middle band -- surfaced but not promoted.
      3. Promoted (score >= promote threshold) -- route by audience_tags:
         * has_hands_on XOR has_big_picture -> the matching tier.
         * BOTH -> tiebreak: pick hands_on iff
           breakdown[hands_on_utility] >= breakdown[big_picture_relevance];
           ties go to big_picture (the more strategic surface).
         * NEITHER (only general / finance) -> on_the_radar regardless of
           score. The head sections require an explicit audience match;
           a high-scoring general-interest story still belongs in the
           terse linked list, not Big Picture / Hands-On.

    Pulse is NOT a stored tier -- summarise.py picks the Pulse from the
    union of big_picture + hands_on stories.
    """
    sig = breakdown.get("significance", 0)
    cut = thresholds.get("cut", _DEFAULT_TIER_THRESHOLDS["cut"])
    radar = thresholds.get(
        "on_the_radar", _DEFAULT_TIER_THRESHOLDS["on_the_radar"]
    )
    promote = thresholds.get(
        "promote_to_section", _DEFAULT_TIER_THRESHOLDS["promote_to_section"]
    )
    # `radar` is intentionally unused in the conditions below -- it serves
    # as the documented floor between cut and the promote band, equivalent
    # to (cut.max_score + 1). Read on rubric reload to validate via the
    # schema; the routing itself is driven by cut.* and promote.min_score.
    _ = radar

    if score < cut["max_score"] or sig <= cut["max_significance"]:
        return "cut"
    if score < promote["min_score"]:
        return "on_the_radar"

    tag_set = set(audience_tags)
    has_hands_on = "hands_on" in tag_set
    has_big_picture = "big_picture" in tag_set

    if has_hands_on and not has_big_picture:
        return "hands_on"
    if has_big_picture and not has_hands_on:
        return "big_picture"
    if has_hands_on and has_big_picture:
        ho = breakdown.get("hands_on_utility", 0)
        bp = breakdown.get("big_picture_relevance", 0)
        # Strict >= so ties (ho == bp) flow to big_picture per spec.
        return "hands_on" if ho > bp else "big_picture"
    # Neither head-section tag -> general / finance only -> on_the_radar.
    return "on_the_radar"


# ---------------------------------------------------------------------------
# LLM client -- branches on LLM_PROVIDER. Anthropic + Bedrock implemented;
# other providers raise NotImplementedError for v0 (LiteLLM later).
# ---------------------------------------------------------------------------

def _llm_call(prompt: str, *, temperature: float, max_tokens: int) -> str:
    """Issue one LLM call and return the raw response text.

    Reads ``LLM_PROVIDER``, ``LLM_ENDPOINT``, ``LLM_API_KEY``, ``LLM_MODEL``,
    ``LLM_TIMEOUT_SECONDS`` from the environment. Does NOT log the API key.

    Raises
    ------
    NotImplementedError
        For providers other than ``anthropic`` and ``bedrock`` (v0 scope).
    RuntimeError
        If a required env var (``LLM_MODEL``) is missing.
    """
    provider = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
    model = os.getenv("LLM_MODEL", "").strip()
    if not model:
        raise RuntimeError("LLM_MODEL is required")
    timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))

    if provider == "anthropic":
        return _llm_call_anthropic(prompt, model=model, temperature=temperature,
                                   max_tokens=max_tokens, timeout=timeout)
    if provider == "bedrock":
        return _llm_call_bedrock(prompt, model=model, temperature=temperature,
                                 max_tokens=max_tokens, timeout=timeout)
    if provider in {"openai", "litellm", "ollama"}:
        return _llm_call_openai_compatible(
            prompt, model=model, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout,
        )
    raise NotImplementedError(
        f"unknown LLM_PROVIDER={provider!r}; expected one of "
        "{anthropic, bedrock, openai, litellm, ollama}"
    )


# Models known to reject the `temperature` parameter (Anthropic 4.7+ family
# uses adaptive sampling; temperature/top_p/top_k all rejected at runtime).
# Populated lazily on the first 400-deprecation response per process so
# subsequent calls skip temperature upfront and don't pay a wasted round-trip.
# Confirmed (2026-05-23): claude-opus-4-7 family rejects temperature.
_MODELS_REJECTING_TEMPERATURE: set[str] = set()


def _llm_call_anthropic(
    prompt: str, *, model: str, temperature: float, max_tokens: int, timeout: float
) -> str:
    """Anthropic-SDK call. Honours ``LLM_ENDPOINT`` if set (e.g. for a
    bank-internal Anthropic-compatible proxy).

    Some Claude 4.7+ models reject ``temperature``/``top_p``/``top_k`` at
    runtime (the model uses adaptive sampling). We try with temperature
    first; on the specific deprecation error we cache the model name in
    ``_MODELS_REJECTING_TEMPERATURE`` and retry without -- subsequent calls
    in the same process skip the parameter upfront.
    """
    import anthropic  # local import -- optional dep on bedrock-only deploys

    api_key = os.getenv("LLM_API_KEY") or None
    base_url = os.getenv("LLM_ENDPOINT") or None
    client_kwargs: dict[str, Any] = {"timeout": timeout}
    if api_key:
        client_kwargs["api_key"] = api_key
    if base_url:
        client_kwargs["base_url"] = base_url
    client = anthropic.Anthropic(**client_kwargs)

    create_kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if model not in _MODELS_REJECTING_TEMPERATURE:
        create_kwargs["temperature"] = temperature

    try:
        resp = client.messages.create(**create_kwargs)
    except anthropic.BadRequestError as exc:
        msg = str(exc).lower()
        if "temperature" in msg and ("deprecated" in msg or "not supported" in msg):
            _MODELS_REJECTING_TEMPERATURE.add(model)
            create_kwargs.pop("temperature", None)
            resp = client.messages.create(**create_kwargs)
        else:
            raise
    # Concatenate text blocks. SDK returns a list of content blocks; the
    # ranking + summarisation prompts both request plain text/JSON, so we
    # join all text blocks defensively.
    chunks: list[str] = []
    for block in resp.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks)


def _llm_call_bedrock(
    prompt: str, *, model: str, temperature: float, max_tokens: int, timeout: float
) -> str:
    """AWS Bedrock call via boto3. Uses ambient AWS credentials when
    ``LLM_API_KEY`` is empty -- common in bank environments with attached
    IAM roles. Targets the Bedrock Anthropic Messages API."""
    import boto3  # local import -- optional dep on anthropic-only deploys
    from botocore.config import Config

    config = Config(read_timeout=timeout, connect_timeout=timeout)
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    endpoint = os.getenv("LLM_ENDPOINT") or None

    client_kwargs: dict[str, Any] = {"config": config}
    if region:
        client_kwargs["region_name"] = region
    if endpoint:
        client_kwargs["endpoint_url"] = endpoint

    # If LLM_API_KEY is set on Bedrock, treat it as a bearer token via the
    # newer Bedrock API key path. Otherwise rely on ambient creds.
    api_key = os.getenv("LLM_API_KEY") or ""
    if api_key:
        # Bedrock now supports API keys via the AWS_BEARER_TOKEN_BEDROCK
        # env var; we set it in-process if not already present.
        os.environ.setdefault("AWS_BEARER_TOKEN_BEDROCK", api_key)

    client = boto3.client("bedrock-runtime", **client_kwargs)

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    })
    resp = client.invoke_model(
        modelId=model,
        contentType="application/json",
        accept="application/json",
        body=body,
    )
    payload = json.loads(resp["body"].read())
    chunks: list[str] = []
    for block in payload.get("content", []) or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            chunks.append(block["text"])
    return "".join(chunks)


def _llm_call_openai_compatible(
    prompt: str, *, model: str, temperature: float, max_tokens: int, timeout: float
) -> str:
    """OpenAI Chat Completions call via httpx.

    Works with any OpenAI-API-compatible endpoint: OpenAI itself, a LiteLLM
    proxy, Ollama in OpenAI-compat mode, Together, Groq, vLLM, etc.

    ``LLM_ENDPOINT`` is the base URL up through the API-version segment
    (e.g. ``https://api.openai.com/v1`` or ``http://localhost:4000/v1``).
    ``/chat/completions`` is appended here.
    """
    import httpx

    base_url = (os.getenv("LLM_ENDPOINT") or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError(
            "LLM_ENDPOINT is required for openai/litellm/ollama "
            "(e.g. https://api.openai.com/v1 or http://localhost:4000/v1)"
        )
    api_key = os.getenv("LLM_API_KEY") or ""

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    resp = httpx.post(
        f"{base_url}/chat/completions",
        headers=headers, json=body, timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError(
            f"openai-compatible response had no choices: {payload!r}"
        )
    return (choices[0].get("message") or {}).get("content") or ""


# ---------------------------------------------------------------------------
# Atomic JSONL writer.
# ---------------------------------------------------------------------------

def _atomic_write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    """Atomic write: ``.tmp`` + fsync + rename. Mirrors the pattern in
    DESIGN.md "Atomic writes" -- a crash mid-write leaves the canonical
    name untouched."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, default=_json_default,
                                ensure_ascii=False))
            fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _json_default(value: Any) -> Any:
    """JSON fallback for datetimes / pydantic url types. Anything else
    raises -- we want loud failures on unexpected types."""
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    # Pydantic v2 HttpUrl is a str subclass at runtime; this branch is a
    # belt for objects that aren't directly serialisable.
    return str(value)


# ---------------------------------------------------------------------------
# Standalone entrypoint for ad-hoc debugging.
# ---------------------------------------------------------------------------

def _parse_cli_date(argv: list[str]) -> _dt.date | None:
    """Minimal CLI: ``python -m src.rank [YYYY-MM-DD]``. No argparse to
    keep this debug-only."""
    if len(argv) <= 1:
        return None
    try:
        return _dt.date.fromisoformat(argv[1])
    except ValueError:
        print(f"usage: python -m src.rank [YYYY-MM-DD]", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":  # pragma: no cover -- debug runner only
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    rank(_parse_cli_date(sys.argv))
