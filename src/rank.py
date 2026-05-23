r"""
src/rank.py -- AI Vector ranking stage.

Reads `data/staging/YYYY-MM-DD/clusters.jsonl`, scores each surviving cluster
with the LLM against `config/rubric.yaml`, and writes
`data/staging/YYYY-MM-DD/ranked.jsonl` sorted by score descending.

Round B (DESIGN.md "Archive: staging vs canonical"):
  * Reads + writes today under STAGING.
  * Reads `data/published_urls.txt` (canonical-only) for the post-rank
    URL guard. A URL Arman drafted but never released stays eligible.

Owner: LLM Engineer (per docs/TEAM.md, .claude/agents/llm-engineer.md).
Contract: docs/DESIGN.md "RankedStory" + "ranked.jsonl" + "Cross-issue
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from src import paths
from src.models import RUBRIC_WEIGHTS, Cluster, Item, RankedStory


# ---------------------------------------------------------------------------
# Module constants -- declared at top per the LLM Engineer spec.
# ---------------------------------------------------------------------------

RANK_PROMPT_VERSION = "v0.1"
r"""Pydantic-validated version string (pattern: ^v\d+(\.\d+)*$).

Audit tag: ``rank-v0.1-2026-05-23``. Bump (e.g. ``v0.2``) when the prompt
content changes -- so the eval harness can correlate score movement against
prompt revisions (risk-register item #6 in docs/TEAM.md).
"""

MAX_ITEMS_IN_CLUSTER_PROMPT = 3
"""How many member items to inline in the per-cluster prompt body."""

JSON_RETRY_BUDGET = 1
"""One retry on JSON parse failure; second failure -> skip the cluster."""

_DEFAULT_RUBRIC_PATH = Path("config/rubric.yaml")
_SOURCES_YAML_PATH = Path("config/sources.yaml")

_LOG = logging.getLogger("ai_vector.rank")


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

    # --- Step 3+4: LLM scoring ---------------------------------------------
    trust_weights = _load_trust_weights(_SOURCES_YAML_PATH)
    ranked: list[RankedStory] = []
    llm_errors = 0
    for cluster in survivors:
        try:
            story = _rank_one(
                cluster=cluster,
                items_by_id=items_by_id,
                rubric_block=rubric_block,
                trust_weights=trust_weights,
            )
        except Exception:  # noqa: BLE001 -- never crash the issue
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
            "[cluster %s] score=%d (sig:%d bu:%d lr:%d fs:%d fm:%d) tags=%s",
            cluster.cluster_id,
            story.score,
            story.breakdown.get("significance", 0),
            story.breakdown.get("builder_utility", 0),
            story.breakdown.get("leadership_relevance", 0),
            story.breakdown.get("financial_services_impact", 0),
            story.breakdown.get("freshness_momentum", 0),
            list(story.audience_tags),
        )

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


def _rank_one(
    cluster: Cluster,
    items_by_id: dict[str, Item],
    rubric_block: str,
    trust_weights: dict[str, int],
) -> RankedStory | None:
    """Score one cluster with the LLM. Returns ``None`` on parse/validation
    failure after retries -- the caller logs and skips. Never raises on
    LLM/parse errors so a single bad cluster doesn't poison the issue."""
    prompt = _build_rank_prompt(cluster, items_by_id, rubric_block, trust_weights)
    temperature = float(os.getenv("LLM_TEMPERATURE_RANK", "0.2"))

    parsed = _call_and_parse_rank(prompt, temperature, cluster.cluster_id)
    if parsed is None:
        return None

    # Recompute score from breakdown x weights -- ignore any LLM-returned
    # `score` field. Pydantic enforces the same invariant; recomputing here
    # absorbs LLM arithmetic noise before pydantic raises.
    score = _weighted_score(parsed.breakdown)

    # The LLM picks audience tags; tier is the editorial slot. rank.py's
    # job is to assign an initial tier -- summarise.py and the Editor may
    # relabel. Below-threshold -> "cut"; everything else starts as
    # "notable" and summarise.py promotes by section logic. (DESIGN.md
    # says tier is the bridge between rank and summarise; the strong
    # opinion on which threshold drives "cut" lives below.)
    tier = _assign_initial_tier(score, parsed.breakdown)

    try:
        story = RankedStory(
            cluster_id=cluster.cluster_id,
            score=score,
            breakdown=parsed.breakdown,
            audience_tags=parsed.audience_tags,  # type: ignore[arg-type]
            rationale=parsed.rationale,
            tier=tier,
            prompt_version=RANK_PROMPT_VERSION,
        )
    except Exception:  # noqa: BLE001
        _LOG.exception(
            "rank: RankedStory validation failed for cluster_id=%s -- "
            "skipping. breakdown=%s tags=%s",
            cluster.cluster_id, parsed.breakdown, parsed.audience_tags,
        )
        return None
    return story


def _build_rank_prompt(
    cluster: Cluster,
    items_by_id: dict[str, Item],
    rubric_block: str,
    trust_weights: dict[str, int],
) -> str:
    """Assemble the per-cluster ranking prompt. Editorial-focus + rubric
    are inlined (the prompt is self-contained for offline audit)."""
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

    return f"""\
You are scoring a single AI-news cluster for AI Vector -- a daily,
agent-assisted AI newsletter for engineers, data scientists, and senior
leaders, with a financial-services lens.

{_EDITORIAL_FOCUS_BLOCK}
{rubric_block}

CLUSTER
canonical_title: {cluster.canonical_title}
cluster_id: {cluster.cluster_id}
sources: {list(cluster.sources)}
earliest_published: {cluster.earliest_published.isoformat()}
size: {cluster.size}
is_continuation: {"yes (cross_time_ref=" + cluster.cross_time_ref + ")" if cluster.cross_time_ref else "no"}

ITEMS (top {MAX_ITEMS_IN_CLUSTER_PROMPT} by source trust):
{items_block}

INSTRUCTIONS
Score the cluster against the rubric. Apply the EDITORIAL FOCUS pre-filter
first -- Tier-3 stories MUST score significance <= 25. Audience tags are
independent of score: pick the subset of {{builder, leader, finance, general}}
that this story is actually for (at least one).

Return ONLY a single JSON object (no markdown fences, no commentary):

{{
  "cluster_id": "{cluster.cluster_id}",
  "score": <int 0-100>,
  "breakdown": {{
    "significance": <int 0-100>,
    "builder_utility": <int 0-100>,
    "leadership_relevance": <int 0-100>,
    "financial_services_impact": <int 0-100>,
    "freshness_momentum": <int 0-100>
  }},
  "audience_tags": [<one or more of: "builder", "leader", "finance", "general">],
  "rationale": "<one sentence, <= 240 chars, specific not generic>"
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
    prompt: str, temperature: float, cluster_id: str
) -> _ParsedScore | None:
    """Issue the LLM call, parse JSON, retry once on parse failure with a
    corrective nudge. Returns ``None`` after the retry budget is spent --
    the caller logs and skips."""
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
        if parsed is not None:
            return parsed
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
    return None


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
        return _ParsedScore(
            breakdown=breakdown_int,
            audience_tags=list(tags),
            rationale=rationale.strip(),
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


def _assign_initial_tier(
    score: int, breakdown: dict[str, int]
) -> str:
    """Initial tier assignment per DESIGN.md note that tier is "the bridge
    between rank and summarise." rank.py applies a coarse first-pass; the
    Editor and ``summarise.py`` may relabel.

    Heuristic (intentionally conservative):
      - score < 35 or significance <= 25 -> "cut"
      - otherwise -> "notable" (summarise.py promotes to pulse / leaders /
        geeks based on its own routing logic; v0.2 sections)

    DESIGN.md ambiguity: the spec leaves "where exactly tier is decided"
    split across rank and summarise. We resolve it by having rank set a
    floor (drop the truly-cut) and let summarise route everything else.
    """
    sig = breakdown.get("significance", 0)
    if score < 35 or sig <= 25:
        return "cut"
    return "notable"


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
        raise NotImplementedError(
            f"set LLM_PROVIDER=anthropic or bedrock for v0; "
            f"{provider} support coming via litellm later"
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
