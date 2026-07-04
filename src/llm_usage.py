r"""
src/llm_usage.py -- LLM token-usage + cost accumulator for one pipeline run.

Single choke point for LLM calls is ``src.rank._llm_call`` (rank, summarise,
verify, and review all funnel through it -- see rank.py's module docstring
and review.py's ``_call_review_llm``). This module is the equally single
choke point for *recording* what those calls cost: the three provider
branches inside rank.py (`_llm_call_anthropic`, `_llm_call_bedrock`,
`_llm_call_openai_compatible`) each call `record()` once they have a
response with usage counts.

Per "No Token Wasted": this is bookkeeping, not judgment -- plain counters
and a static pricing table, no LLM calls of its own.

Process model: one pipeline run = one Python process = one accumulator.
No locking -- `aiv run` is single-threaded at the stage level (rank.py's
internal ThreadPoolExecutor fans out within a stage, but Python's GIL makes
the dict increments here safe in practice for this call volume; this module
makes no concurrency promises beyond that).

Owner: Release Engineer (docs/internal/TEAM.md). Consumed by src/run.py's
stage dispatcher and end-of-run summary.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Pricing table -- USD per million tokens, keyed by model-id PREFIX (dated
# model ids like "claude-sonnet-4-6-20260115" resolve via prefix match, see
# `_price_for_model`). Longest matching prefix wins, so entry order below
# doesn't matter for correctness.
#
# PRICES DRIFT -- verify against current Anthropic pricing before trusting
# a cost figure for a budget decision.
# ---------------------------------------------------------------------------
_PRICING: dict[str, tuple[float, float]] = {
    # (usd_per_mtok_input, usd_per_mtok_output)
    "claude-sonnet-4-6": (3.00, 15.00),   # verify against current Anthropic pricing
    "claude-opus-4": (15.00, 75.00),      # verify against current Anthropic pricing
    "claude-haiku-4-5": (1.00, 5.00),     # verify against current Anthropic pricing
}

_UNKNOWN_STAGE = "unknown"

_current_stage: str = _UNKNOWN_STAGE

# stage -> model -> {"input_tokens": int, "output_tokens": int}
_usage: dict[str, dict[str, dict[str, int]]] = {}


def reset() -> None:
    """Clear all accumulated usage. Call once at the start of a run (or a
    test) so stale counts from a prior invocation never leak in."""
    global _current_stage
    _current_stage = _UNKNOWN_STAGE
    _usage.clear()


def set_stage(stage: str) -> None:
    """Tag subsequent `record()` calls with `stage` (e.g. "rank", "summarise",
    "verify", "review"). Called by run.py's stage dispatcher immediately
    before invoking each stage's handler.
    """
    global _current_stage
    _current_stage = stage or _UNKNOWN_STAGE


def record(model: str, input_tokens: int, output_tokens: int) -> None:
    """Accumulate one LLM call's token usage under the currently active
    stage. Safe to call even if `set_stage()` was never invoked -- e.g. an
    ad-hoc debug entrypoint (`python -m src.rank`) that calls into rank()
    directly without going through run.py's dispatcher -- in which case
    usage is tagged "unknown" rather than raising.
    """
    stage = _current_stage or _UNKNOWN_STAGE
    model_key = model or _UNKNOWN_STAGE
    by_model = _usage.setdefault(stage, {})
    entry = by_model.setdefault(model_key, {"input_tokens": 0, "output_tokens": 0})
    entry["input_tokens"] += max(0, int(input_tokens or 0))
    entry["output_tokens"] += max(0, int(output_tokens or 0))


def _price_for_model(model: str) -> tuple[float, float] | None:
    """Prefix-match `model` against `_PRICING`. Returns the rates for the
    LONGEST matching prefix (so a more specific entry wins over a shorter
    accidental substring match), or None if no prefix matches."""
    best: tuple[float, float] | None = None
    best_len = -1
    for prefix, rates in _PRICING.items():
        if model.startswith(prefix) and len(prefix) > best_len:
            best = rates
            best_len = len(prefix)
    return best


def _stage_snapshot(by_model: dict[str, dict[str, int]]) -> dict[str, Any]:
    input_tokens = sum(m["input_tokens"] for m in by_model.values())
    output_tokens = sum(m["output_tokens"] for m in by_model.values())
    cost_usd = 0.0
    cost_known = True
    for model, tok in by_model.items():
        rates = _price_for_model(model)
        if rates is None:
            cost_known = False
            continue
        in_rate, out_rate = rates
        cost_usd += (tok["input_tokens"] / 1_000_000) * in_rate
        cost_usd += (tok["output_tokens"] / 1_000_000) * out_rate
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost_usd, 4) if cost_known else None,
    }


def snapshot() -> dict[str, Any]:
    """Return accumulated usage: `{"stages": {stage: {...}}, "total": {...}}`.

    Each stage/total dict has `input_tokens`, `output_tokens`, `cost_usd`.
    `cost_usd` is None whenever any contributing model isn't in the pricing
    table -- we report tokens either way, but never guess at a cost.
    """
    stages: dict[str, Any] = {}
    total_input = 0
    total_output = 0
    total_cost = 0.0
    total_cost_known = True
    for stage, by_model in _usage.items():
        stage_snap = _stage_snapshot(by_model)
        stages[stage] = stage_snap
        total_input += stage_snap["input_tokens"]
        total_output += stage_snap["output_tokens"]
        if stage_snap["cost_usd"] is None:
            total_cost_known = False
        else:
            total_cost += stage_snap["cost_usd"]
    return {
        "stages": stages,
        "total": {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cost_usd": round(total_cost, 4) if total_cost_known else None,
        },
    }


# ---------------------------------------------------------------------------
# Formatting -- one line for the run.py closing banner.
# ---------------------------------------------------------------------------

def _format_tokens(n: int) -> str:
    """Abbreviate token counts: 25100 -> "25.1k", 850 -> "850"."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def format_summary_line(
    snap: dict[str, Any] | None = None, *, stage_order: Any = None
) -> str | None:
    """Build the one-line cost summary, e.g.:

        LLM usage: rank 25.1k/3.2k ($0.12) | summarise 88.0k/12.0k ($0.44) |
        verify 41.0k/6.0k ($0.21) | review 9.0k/4.0k ($0.09) | TOTAL $0.86

    Returns None if no LLM stage recorded any usage (e.g. a `--stages
    fetch,cluster` run) -- callers should skip printing entirely rather than
    print an empty/zero line.

    `stage_order` (optional) fixes the left-to-right ordering of stages that
    did run (falls back to insertion order, i.e. the order stages executed
    in, when omitted).
    """
    if snap is None:
        snap = snapshot()
    stages = snap.get("stages") or {}
    if not stages:
        return None

    order = list(stage_order) if stage_order is not None else list(stages.keys())
    ordered_names = [name for name in order if name in stages]
    # Any stage present but not in the supplied order (shouldn't normally
    # happen) is appended so nothing is silently dropped.
    ordered_names += [name for name in stages if name not in ordered_names]

    parts: list[str] = []
    for name in ordered_names:
        s = stages[name]
        tok = f"{_format_tokens(s['input_tokens'])}/{_format_tokens(s['output_tokens'])}"
        cost = f"(${s['cost_usd']:.2f})" if s["cost_usd"] is not None else "(cost unknown)"
        parts.append(f"{name} {tok} {cost}")

    total = snap["total"]
    if total["cost_usd"] is not None:
        parts.append(f"TOTAL ${total['cost_usd']:.2f}")
    else:
        parts.append("TOTAL cost unknown (model not in pricing table)")

    return "LLM usage: " + " | ".join(parts)
