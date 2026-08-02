"""
src/run.py -- AI Vector pipeline orchestrator.

CLI entry point: ``aiv`` (installed via pyproject.toml).
Backwards-compatible: ``python -m src.run`` still works.

Subcommands:
  aiv run       -- fetch -> cluster -> rank -> summarise -> render (staging)
  aiv revise    -- act on the reviewer's findings (owned by src/revise.py)
  aiv gate      -- decide whether the staged issue may publish unattended
  aiv release   -- promote staging draft to canonical
  aiv unrelease -- reverse a release
  aiv check     -- pre-flight checks only

Module owners (per docs/internal/TEAM.md): orchestration shell is the Architect's;
the individual stages are owned by their respective engineers.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple, Optional

import typer

from src import llm_usage, paths

# ---------------------------------------------------------------------------
# Stage registry (staging-pipeline only). Release / unrelease are separate
# modes, not stages.
# ---------------------------------------------------------------------------

STAGE_ORDER: tuple[str, ...] = (
    "fetch",
    "cluster",
    "rank",
    "summarise",
    "verify",
    "render",
    "review",
)

# Stages that require the LLM env vars to be valid before they can run.
# ``review`` and ``verify`` call the LLM but are failure-soft: a misconfigured
# env produces an ``unavailable`` artifact, not a crash. We still want the
# validator to fire when one of these is the only stage requested standalone
# so the operator sees a clear error instead of a silent ``unavailable``.
LLM_STAGES: frozenset[str] = frozenset({"rank", "summarise", "verify", "review"})

# Providers we ship with. anthropic + bedrock have native clients;
# openai/litellm/ollama share the OpenAI-compatible Chat Completions path
# in rank.py (works with any OpenAI-API-compatible gateway).
SUPPORTED_PROVIDERS: frozenset[str] = frozenset(
    {"anthropic", "bedrock", "openai", "litellm", "ollama"}
)

# ---------------------------------------------------------------------------
# Revision loop policy (2026-08-02).
#
# The loop runs AFTER the review stage and is not itself a stage: it does not
# appear in STAGE_ORDER, `--stages` never names it, and it produces no
# artifact of its own (`src/revise.py` writes `revisions.jsonl`; the loop's
# visible output is a rewritten issue.json plus a fresh review of it).
#
# Keeping it out of STAGE_ORDER is deliberate. A stage runs once and hands
# its artifact forward; this thing re-enters three stages that already ran.
# Modelling it as a stage would have made `--stages revise` and the
# auto-fire rules mean something we would then have to explain away.
# ---------------------------------------------------------------------------

REVISE_MODE_ENV_VAR = "AIV_REVISE_MODE"
"""Environment variable selecting what the revision loop is allowed to do."""

DEFAULT_REVISE_MODE = "shadow"
"""Unset or unrecognised resolves here: propose, change nothing. Same
posture as the publish gate's default phase -- a control that can rewrite a
draft unattended defaults to observation."""

VALID_REVISE_MODES: tuple[str, ...] = ("off", "shadow", "live")
"""``off`` does not call the reviser at all (no tokens spent). ``shadow``
makes one proposal-only call and leaves the issue untouched. ``live`` runs
the full apply/re-verify/re-render/re-review loop."""

MAX_REVISION_CYCLES = 2
"""Hard cap on live cycles per run. Two is a judgment about diminishing
returns, not a technical limit: a reviser that has not fixed the issue in
two passes is not converging, and a third pass mostly buys tokens, latency,
and more chances to make the issue worse."""

_VERDICT_RANK: dict[str, int] = {"red": 0, "amber": 1, "green": 2}
"""Ordering used for the "did it improve?" test. Anything not in this map --
``unavailable``, an unrecognised token, a review that could not be read --
ranks BELOW ``red`` (see `_verdict_rank`), so a cycle that destroys the
review's readability can never look like progress."""

_BEST_VERDICT_RANK = max(_VERDICT_RANK.values())

_LOG = logging.getLogger("ai_vector.run")


# ---------------------------------------------------------------------------
# Date + stage resolution helpers.
# ---------------------------------------------------------------------------

def _resolve_date(arg_value: str | None) -> _dt.date:
    """Parse ``--date`` or default to today (LOCAL time).

    Local-first by design: a Sydney evening run still belongs to "today"
    in Sydney, not yesterday in UTC. Item ``published_at`` timestamps
    stay in UTC under the hood (those are absolute moments); only the
    issue-date folder naming uses local time. Pass ``--date YYYY-MM-DD``
    to override.
    """
    if arg_value is None:
        return _dt.date.today()
    try:
        return _dt.date.fromisoformat(arg_value)
    except ValueError as exc:
        raise SystemExit(
            f"--date must be YYYY-MM-DD (got {arg_value!r}): {exc}"
        )


def _resolve_stages(
    stage: str | None,
    stages: str | None,
    *,
    no_review: bool = False,
    no_verify: bool = False,
) -> list[str]:
    """Resolve the requested stage list for the STAGING pipeline mode.

    Precedence:
      1. ``--stage`` -> [that stage].
      2. ``--stages a,b`` -> validated subset, re-ordered to pipeline order.
      3. Default -> full pipeline order.

    Returns the list in pipeline execution order.

    Auto-verify contract: ``verify`` runs automatically whenever
    ``summarise`` runs, unless ``no_verify=True``. So a default run,
    ``--stages summarise``, and ``--stages rank,summarise`` all gain
    ``verify`` immediately after ``summarise``. A subset that excludes
    ``summarise`` (``--stages render``, ``--stages fetch,cluster``) does
    NOT pull verify in -- factual-accuracy checking is meaningful only
    against a freshly summarised staging issue.
    ``--stages verify`` is the explicit-standalone path and is honoured
    verbatim. The ``--stage verify`` single-stage form is likewise honoured.
    ``--no-verify`` strips verify from the resolved list, the escape hatch
    for full runs and summarise-only runs alike.

    Auto-review contract: ``review`` runs automatically whenever ``render``
    runs, unless ``no_review=True``. So a default run, ``--stages render``,
    and ``--stages summarise,render`` all gain ``review`` at the tail.
    A subset that excludes ``render`` (``--stages summarise``,
    ``--stages fetch,cluster``) does NOT pull review in -- the editorial
    pass is meaningful only against a freshly rendered staging draft.
    ``--stages review`` is the explicit-standalone path and is honoured
    verbatim. The ``--stage review`` single-stage form is likewise honoured.
    ``--no-review`` strips review from the resolved list, which is the escape
    hatch for full runs and render-only runs alike.
    """
    if stage is not None:
        resolved = [stage]
    elif stages is not None:
        requested = [s.strip() for s in stages.split(",") if s.strip()]
        unknown = [s for s in requested if s not in STAGE_ORDER]
        if unknown:
            raise SystemExit(
                f"--stages contains unknown stage(s): {unknown}. "
                f"Valid stages: {list(STAGE_ORDER)}"
            )
        requested_set = set(requested)
        resolved = [s for s in STAGE_ORDER if s in requested_set]
        # Auto-fire verify whenever summarise runs (unless --no-verify).
        if "summarise" in requested_set and "verify" not in requested_set:
            resolved = _insert_after(resolved, "summarise", "verify")
        # Auto-fire review whenever render runs (unless --no-review).
        if "render" in requested_set and "review" not in requested_set:
            resolved.append("review")
    else:
        resolved = list(STAGE_ORDER)

    if no_verify:
        resolved = [s for s in resolved if s != "verify"]
    if no_review:
        resolved = [s for s in resolved if s != "review"]

    return resolved


def _insert_after(lst: list[str], after: str, new: str) -> list[str]:
    """Return a copy of ``lst`` with ``new`` inserted immediately after
    ``after``. If ``after`` is not in the list, ``new`` is appended.
    If ``new`` is already in the list, the list is returned unchanged."""
    if new in lst:
        return list(lst)
    out: list[str] = []
    for item in lst:
        out.append(item)
        if item == after:
            out.append(new)
    if new not in out:
        out.append(new)
    return out


# ---------------------------------------------------------------------------
# Logging + env loading.
# ---------------------------------------------------------------------------

def _setup_logging(verbose: bool) -> None:
    """Configure root + 'ai_vector' logger. Idempotent across calls."""
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            stream=sys.stderr,
        )
    root.setLevel(level)
    logging.getLogger("ai_vector").setLevel(level)


def _load_env() -> None:
    """Populate ``os.environ`` from a local ``.env`` if present.

    Lazy import of python-dotenv so a missing extra surfaces as a clear
    warning rather than a hard import error.
    """
    try:
        from dotenv import load_dotenv  # type: ignore[import-untyped]
    except ImportError:
        _LOG.warning(
            ".env loading skipped: python-dotenv is not installed "
            "(install with `pip install -e .`). LLM stages will rely on the ambient "
            "environment only."
        )
        return
    load_dotenv(override=False)


def _validate_env_for_stages(stages: Iterable[str]) -> None:
    """Pre-flight env var validation for any stages that talk to an LLM.

    Halts (raises ``SystemExit``) on hard failures (missing/unsupported
    provider, missing endpoint or model). Warns on soft failures (empty
    LLM_API_KEY for non-bedrock providers). Skipped entirely when no LLM
    stage is in scope -- a ``--stage fetch`` run does not need credentials,
    and release / unrelease are pure file manipulation.
    """
    stage_set = set(stages)
    if not (stage_set & LLM_STAGES):
        _LOG.debug("env validation skipped: no LLM stages in scope (%s)",
                   sorted(stage_set))
        return

    provider = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    endpoint = (os.getenv("LLM_ENDPOINT") or "").strip()
    api_key = (os.getenv("LLM_API_KEY") or "").strip()
    model = (os.getenv("LLM_MODEL") or "").strip()

    problems: list[str] = []
    if not provider:
        problems.append(
            "LLM_PROVIDER is unset -- set to 'anthropic' or 'bedrock' "
            "(see .env.example)."
        )
    elif provider not in SUPPORTED_PROVIDERS:
        problems.append(
            f"LLM_PROVIDER={provider!r} is not supported in v0. "
            f"Set to one of: {sorted(SUPPORTED_PROVIDERS)}."
        )
    if not endpoint:
        problems.append(
            "LLM_ENDPOINT is unset -- fill it in in .env (see .env.example)."
        )
    if not model:
        problems.append(
            "LLM_MODEL is unset -- fill it in in .env (see .env.example)."
        )
    if problems:
        for p in problems:
            _LOG.error("env validation: %s", p)
        raise SystemExit(
            "LLM env vars are misconfigured for the requested stages "
            f"({sorted(stage_set & LLM_STAGES)}). Fix the issues above in "
            ".env and re-run."
        )
    if not api_key and provider != "bedrock":
        _LOG.warning(
            "LLM_API_KEY is empty for provider=%s. Most providers require "
            "an API key; bedrock can rely on ambient AWS creds, but %s "
            "typically cannot. Proceeding -- expect auth failures.",
            provider, provider,
        )


# ---------------------------------------------------------------------------
# Stage runners (staging pipeline only). One per stage.
# ---------------------------------------------------------------------------

def _run_fetch(run_date: _dt.date) -> str:
    """Invoke src.fetch.fetch(date) and summarise its return value."""
    from src import fetch as fetch_mod

    report = fetch_mod.fetch(date=run_date)
    total_sources = len(report.sources)
    fired = sum(1 for s in report.sources if s.fired)
    items_kept = sum(s.items_kept for s in report.sources)
    duration_ms = int(
        (report.run_finished_at - report.run_started_at).total_seconds() * 1000
    )
    return (
        f"{total_sources} sources / {fired} fired / "
        f"{items_kept} items kept / {duration_ms}ms"
    )


def _run_cluster(run_date: _dt.date) -> str:
    """Invoke src.cluster.cluster(date) and summarise the result."""
    from src import cluster as cluster_mod

    clusters = cluster_mod.cluster(date=run_date)
    cross_linked = sum(1 for c in clusters if c.prior_coverage_ref is not None)
    return f"{len(clusters)} clusters ({cross_linked} cross-time linked)"


def _run_rank(run_date: _dt.date) -> str:
    """Invoke src.rank.rank(date) and summarise the result."""
    from src import rank as rank_mod

    ranked = rank_mod.rank(date=run_date)
    dropped = sum(1 for r in ranked if r.tier == "cut")
    top_score = max((r.score for r in ranked), default=0)
    return f"{len(ranked)} ranked stories ({dropped} cut tier) / top score {top_score}"


def _run_summarise(run_date: _dt.date) -> str:
    """Invoke src.summarise.summarise(date) and summarise the result.

    Note: ``issue_number`` is None in staging (assigned at release time).
    """
    from src import summarise as summarise_mod

    issue = summarise_mod.summarise(date=run_date)
    section_counts = ", ".join(
        f"{s.name}={len(s.stories)}" for s in issue.sections
    )
    pulse_count = len(issue.pulse.stories)
    return (
        f"issue (staging -- not yet numbered) / pulse={pulse_count}, "
        f"{section_counts}"
    )


def _run_verify(run_date: _dt.date) -> str:
    """Invoke src.verify.verify_day(date) and summarise the result.

    ``verify`` is failure-soft by contract -- the underlying module catches
    LLM/transport errors and returns a ``VerificationReport`` with
    ``verdict="unavailable"`` rather than raising. This handler adds a second
    defensive layer: even an unexpected raise (import error, missing module)
    is caught, logged, and turned into a non-blocking warning so the pipeline
    continues to render regardless.
    """
    from src import verify as verify_mod

    report = verify_mod.verify_day(run_date)
    flagged = sum(
        1 for s in report.stories
        if s.has_contradiction or s.has_unsupported or s.headline_flagged
    )
    if report.verdict == "unavailable":
        return f"unavailable -- {report.note[:120] if report.note else '(no detail)'}"
    return (
        f"{report.verdict.upper()} -- "
        f"{len(report.stories)} stories verified / "
        f"{flagged} flagged"
    )


def _run_render_preview(run_date: _dt.date) -> str:
    """Invoke render.render(date, mode='preview') and summarise."""
    from src import render as render_mod

    out_path = render_mod.render(date=run_date, mode="preview")
    return f"preview -> {out_path}"


def _run_review(run_date: _dt.date) -> str:
    """Invoke src.review.run_review(date) and summarise the result.

    ``review`` is failure-soft by contract -- the underlying module catches
    LLM/transport errors and writes a ``verdict: unavailable`` review.md
    rather than raising. This handler just returns the verdict + one_line
    so the pipeline summary can surface them.
    """
    from src import review as review_mod

    artifact = review_mod.run_review(date=run_date)
    return f"{artifact.verdict.upper()} -- {artifact.one_line}"


# Map stage name -> callable. ``render`` always runs in preview mode under
# the staging pipeline; release-mode rendering happens inside release_promote.
_STAGE_HANDLERS: dict[str, Callable[[_dt.date], str]] = {
    "fetch": _run_fetch,
    "cluster": _run_cluster,
    "rank": _run_rank,
    "summarise": _run_summarise,
    "verify": _run_verify,
    "render": _run_render_preview,
    "review": _run_review,
}


# Stages that are advisory: an unexpected exception at the dispatch level
# is caught, logged at WARNING, and treated as a non-blocking soft failure
# so the pipeline continues (the stage's own module is expected to be
# failure-soft internally, but this is the belt-and-suspenders guard).
_ADVISORY_STAGES: frozenset[str] = frozenset({"verify", "review"})


def _run_stage(name: str, run_date: _dt.date) -> tuple[bool, str]:
    """Execute one staging-pipeline stage with structured logging.

    Returns ``(ok, summary)``. For advisory stages (verify, review),
    unexpected exceptions are caught, logged at WARNING, and returned as
    ``(True, "<error>")`` -- they never halt the pipeline. For all other
    stages, an unexpected exception returns ``(False, reason)`` which halts
    the pipeline at that point.
    """
    _LOG.info("--- stage: %s ---", name)
    handler = _STAGE_HANDLERS[name]
    # Tag any LLM calls this stage makes so src/llm_usage.py's accumulator
    # can attribute tokens/cost to the right stage in the end-of-run summary.
    # Cheap no-op for stages that never call the LLM (fetch, cluster, render).
    llm_usage.set_stage(name)
    t0 = time.monotonic()
    try:
        summary = handler(run_date)
    except SystemExit:
        raise
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if name in _ADVISORY_STAGES:
            _LOG.warning(
                "stage %s raised unexpectedly after %dms (advisory -- pipeline "
                "continues) -- %s: %s",
                name, elapsed_ms, type(exc).__name__, exc,
            )
            _LOG.warning("traceback:\n%s", traceback.format_exc())
            return True, f"[advisory-guard] {type(exc).__name__}: {exc}"
        _LOG.error(
            "stage %s FAILED after %dms -- %s: %s",
            name, elapsed_ms, type(exc).__name__, exc,
        )
        _LOG.error("traceback:\n%s", traceback.format_exc())
        return False, f"{type(exc).__name__}: {exc}"

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _LOG.info("stage %s complete in %dms -- %s", name, elapsed_ms, summary)
    return True, summary


# ---------------------------------------------------------------------------
# The revision loop.
#
# What it is: after the editor's review has run, give the pipeline a chance
# to act on the findings instead of just recording them. `src/revise.py`
# (LLM Engineer) decides WHAT to change; this module decides HOW MANY TIMES
# and WHEN TO STOP. That split is the whole point -- judgment about a
# headline belongs in a prompt, and a termination condition belongs in a
# `for` loop with a hard cap, per No Token Wasted.
#
# The live cycle:
#
#     revise -> re-verify (touched stories only) -> render -> review
#
# and then one question: did the computed verdict actually improve? If not,
# stop. The loop never rolls back an edit it made -- rolling back needs a
# snapshot contract the reviser does not have, and the publish gate is
# already the backstop for "the issue got worse" (a degraded verdict holds).
# What the loop guarantees is that it stops making things worse, not that it
# undoes what it already did.
# ---------------------------------------------------------------------------

def resolve_revise_mode(explicit: str | None = None) -> tuple[str, str]:
    """Resolve the revision mode. Returns ``(mode, note)``.

    Precedence: the ``explicit`` argument (test / CLI override), then
    ``AIV_REVISE_MODE``, then `DEFAULT_REVISE_MODE`. An unrecognised value
    falls back to ``shadow`` and says so -- a typo'd variable must never
    silently license the loop to rewrite the issue.
    """
    raw = explicit if explicit is not None else os.getenv(REVISE_MODE_ENV_VAR)
    if raw is None or not raw.strip():
        return DEFAULT_REVISE_MODE, ""
    value = raw.strip().lower()
    if value in VALID_REVISE_MODES:
        return value, ""
    note = (
        f"unrecognised {REVISE_MODE_ENV_VAR}={raw!r}; falling back to "
        f"{DEFAULT_REVISE_MODE!r} (valid: {', '.join(VALID_REVISE_MODES)})"
    )
    _LOG.warning("revise: %s", note)
    return DEFAULT_REVISE_MODE, note


def _verdict_rank(verdict: str | None) -> int:
    """Rank a review verdict for the improvement test. Unknown ranks last.

    ``red`` < ``amber`` < ``green``; everything else (``unavailable``, an
    unrecognised token, no readable review at all) ranks -1. Ranking the
    unreadable states BELOW the worst readable one is what makes "did it
    improve?" safe: a cycle that leaves the review unparseable scores worse
    than the red it started from, so the loop stops instead of continuing
    into a state nobody can evaluate.
    """
    if verdict is None:
        return -1
    return _VERDICT_RANK.get(verdict.strip().lower(), -1)


def _current_review_verdict(run_date: _dt.date) -> str | None:
    """Read the computed editorial verdict for ``run_date``.

    Delegates to `gate.read_review_state`, which prefers ``review.json`` and
    falls back to ``review.md`` frontmatter. Deliberately the same reader the
    gate uses: if the loop and the gate disagreed about what today's verdict
    is, the loop could stop on "good enough" while the gate holds, and the
    two would be arguing about a file rather than a decision.
    """
    from src import gate as gate_mod

    return gate_mod.read_review_state(run_date).verdict


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` off a pydantic model or a plain mapping.

    `RevisionCycle` is the LLM Engineer's shape. Reading it through one
    tolerant accessor means a rename downstream degrades to "the loop stops
    early", which is a bad morning, rather than "the pipeline raises", which
    is no issue at all.
    """
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _revision_changes(report: Any) -> list[Any] | None:
    """The `RevisionChange` list behind a revise call, or ``None``.

    `revise_day` returns a `RevisionReport` -- a caller-facing summary that
    carries the substantive `RevisionCycle` on ``.cycle`` -- so the changes
    live one level down. We also accept a bare cycle, because that is the
    shape on disk in ``revisions.jsonl`` and it costs one line to read both.
    """
    for candidate in (report, _attr(report, "cycle")):
        if candidate is None:
            continue
        changes = _attr(candidate, "changes")
        if isinstance(changes, (list, tuple)):
            return list(changes)
    return None


class RevisionTally(NamedTuple):
    """How one revision call's changes resolved."""

    applied: int
    rejected: int
    proposed: int


def _revision_tally(report: Any) -> RevisionTally:
    """Count the change statuses from one `revise_day` call.

    Prefers the report's own integer counters (`RevisionReport.applied` /
    `.proposed` / `.rejected`) because that is what the writer computed;
    falls back to counting the `RevisionChange` statuses when they are not
    there. ``applied`` and ``rejected`` are the live-mode outcomes;
    ``proposed`` is what a shadow call produces (it computes the rewrite and
    writes nothing).

    An unreadable report tallies to zeros, which the loop reads as "nothing
    happened" and stops on. That is the safe interpretation: a loop that
    guesses high keeps spending.
    """
    counters = {
        name: _attr(report, name) for name in ("applied", "rejected", "proposed")
    }
    if any(isinstance(v, int) for v in counters.values()):
        return RevisionTally(
            *(v if isinstance(v, int) else 0
              for v in (counters["applied"], counters["rejected"],
                        counters["proposed"]))
        )

    changes = _revision_changes(report) or []
    counts = {"applied": 0, "rejected": 0, "proposed": 0}
    for change in changes:
        status = _attr(change, "status")
        if status in counts:
            counts[status] += 1
    return RevisionTally(counts["applied"], counts["rejected"], counts["proposed"])


def _revision_touched_story_ids(report: Any) -> list[str] | None:
    """Which stories the call actually rewrote. Three-state on purpose.

    - a **list of ids** -- these stories' text changed; re-verify exactly
      them.
    - an **empty list** -- we read the changes and none of them touched story
      text (a cycle that only rewrote section intros, say). Nothing the
      verifier looks at moved, so re-verifying would be pure cost. Section
      intros carry no factual claims tied to a source excerpt; verify works
      over `SummaryBlock` headlines and summaries only.
    - ``None`` -- we could not tell. Callers re-verify everything, because
      "I don't know what changed" and "nothing changed" are different states
      and only one of them is safe to skip.
    """
    changes = _revision_changes(report)
    if changes is None:
        explicit = _attr(report, "touched_story_ids")
        if isinstance(explicit, (list, tuple)):
            return [s for s in explicit if isinstance(s, str) and s]
        return None

    ids: list[str] = []
    seen: set[str] = set()
    for change in changes:
        if _attr(change, "status") != "applied":
            continue
        target = _attr(change, "target")
        if target is None:
            # An applied change we cannot locate. Fall back to "unknown"
            # rather than silently narrowing the re-verify scope.
            return None
        story_id = _attr(target, "story_id")
        if isinstance(story_id, str) and story_id and story_id not in seen:
            seen.add(story_id)
            ids.append(story_id)
    return ids


def _reverify_stories(run_date: _dt.date, story_ids: list[str] | None) -> str:
    """Re-run the factual verifier over the stories the reviser rewrote.

    The contract is ``verify_day(date, *, story_ids=None)`` where ``None``
    means the whole issue. When the installed `verify.py` does not accept the
    keyword yet, we re-verify EVERYTHING and log why. That costs tokens we
    hoped to save, and it is still the right fallback: `verify.json` and the
    denormalised `SummaryBlock.verification` copies are what the gate scans
    for contradictions, so leaving them stale about a story we just rewrote
    would let the gate reason about text that no longer exists.
    """
    import inspect

    from src import verify as verify_mod

    if story_ids is not None and not story_ids:
        return "skipped (no story text changed this cycle)"

    kwargs: dict[str, Any] = {}
    if story_ids:
        try:
            accepts = "story_ids" in inspect.signature(verify_mod.verify_day).parameters
        except (TypeError, ValueError):  # pragma: no cover -- exotic callables
            accepts = False
        if accepts:
            kwargs["story_ids"] = story_ids
        else:
            _LOG.warning(
                "revise: verify_day() does not accept story_ids yet; "
                "re-verifying the whole issue instead of just %d touched "
                "story/stories", len(story_ids),
            )
    report = verify_mod.verify_day(run_date, **kwargs)
    scope = f"{len(story_ids)} touched" if kwargs else "all"
    return f"{report.verdict} ({scope} stories)"


def _revise_once(run_date: _dt.date, *, shadow: bool) -> Any:
    """One call into `src/revise.py`. Isolated so tests can patch one seam."""
    from src import revise as revise_mod

    return revise_mod.revise_day(run_date, shadow=shadow)


class RevisionOutcome(NamedTuple):
    """What the revision phase did, for the end-of-run summary.

    ``summary`` is the revision line itself. ``review_summary`` is the
    verdict line from the LAST re-review the loop ran, or ``None`` when the
    loop never re-reviewed (shadow mode, mode ``off``, no cycle applied
    anything, or the loop failed) -- in which case the caller keeps the
    verdict the review stage already reported.
    """

    summary: str | None
    review_summary: str | None = None


def _run_revision_loop(run_date: _dt.date, *, mode: str) -> RevisionOutcome:
    """Run the revision phase. Returns the summary lines for the run report.

    Raises nothing that the caller has to handle beyond the advisory guard in
    `_maybe_revise` -- see there for the containment contract.
    """
    if mode == "shadow":
        llm_usage.set_stage("revise")
        report = _revise_once(run_date, shadow=True)
        tally = _revision_tally(report)
        # A shadow cycle resolves every change to "proposed" and writes no
        # issue.json, which is why nothing re-verifies, re-renders or
        # re-reviews after it.
        return RevisionOutcome(
            f"shadow -- {tally.proposed} proposed / {tally.rejected} rejected; "
            "issue unchanged, no re-review"
        )

    start_verdict = _current_review_verdict(run_date)
    verdict = start_verdict
    cycles_run = 0
    applied_total = 0
    rejected_total = 0
    last_review_summary: str | None = None
    stop_reason = f"reached the {MAX_REVISION_CYCLES}-cycle cap"

    for cycle in range(1, MAX_REVISION_CYCLES + 1):
        _LOG.info(
            "--- revision cycle %d/%d (verdict in: %s) ---",
            cycle, MAX_REVISION_CYCLES, verdict or "unreadable",
        )
        llm_usage.set_stage("revise")
        report = _revise_once(run_date, shadow=False)
        cycles_run = cycle
        tally = _revision_tally(report)
        applied_total += tally.applied
        rejected_total += tally.rejected

        if tally.applied == 0:
            # Nothing on disk changed, so re-verifying, re-rendering and
            # re-reviewing would all produce byte-identical outputs at full
            # LLM price. Stop.
            #
            # `ran=False` is the engine REFUSING to act (no review, a stale
            # review, nothing actionable) rather than trying and changing
            # nothing. A refusal is not a failure, but it is also not a
            # revision, and the log should not read as though it were.
            if _attr(report, "ran") is False:
                note = str(_attr(report, "note", "") or "no reason given")
                stop_reason = f"reviser declined: {note}"
            else:
                stop_reason = "reviser applied nothing"
            break

        touched = _revision_touched_story_ids(report)
        _LOG.info(
            "revise: cycle %d applied %d change(s) across %s",
            cycle, tally.applied,
            f"{len(touched)} story/stories" if touched is not None
            else "an undetermined set of stories",
        )
        llm_usage.set_stage("verify")
        _reverify_stories(run_date, touched)
        llm_usage.set_stage("render")
        _run_render_preview(run_date)
        llm_usage.set_stage("review")
        last_review_summary = _run_review(run_date)

        new_verdict = _current_review_verdict(run_date)
        if _verdict_rank(new_verdict) <= _verdict_rank(verdict):
            stop_reason = (
                f"verdict did not improve ({verdict or 'unreadable'} -> "
                f"{new_verdict or 'unreadable'})"
            )
            verdict = new_verdict
            break
        verdict = new_verdict
        if _verdict_rank(verdict) >= _BEST_VERDICT_RANK:
            stop_reason = "verdict reached the ceiling"
            break

    return RevisionOutcome(
        f"live -- {cycles_run} cycle(s), {applied_total} applied / "
        f"{rejected_total} rejected, verdict "
        f"{start_verdict or 'unreadable'} -> {verdict or 'unreadable'} "
        f"(stopped: {stop_reason})",
        last_review_summary,
    )


def _maybe_revise(run_date: _dt.date, *, mode: str | None = None) -> RevisionOutcome:
    """Run the revision phase if the mode calls for it. Never raises.

    The containment contract, and why it is absolute: the revision loop is
    an improvement, and an improvement that can destroy the day's issue is
    not one. Any failure inside it -- a missing `revise.py`, a shape change
    in `RevisionReport`, an LLM timeout mid-cycle -- is caught here, logged
    with a traceback, and the pipeline continues with whatever issue.json is
    on disk.

    Note the honest edge: "whatever is on disk" is the PRE-revision issue
    only if the failure happened before any write. If a cycle applied edits
    and then the re-review failed, the issue on disk is the revised one with
    a review that no longer matches it -- and the gate holds it as
    `hold:stale-review`. That is the correct outcome, and it is the reason
    the freshness check is blocking rather than advisory.
    """
    active_mode, _note = resolve_revise_mode(mode)
    if active_mode == "off":
        _LOG.debug("revise: mode=off -- skipping the revision phase")
        return RevisionOutcome(None)
    t0 = time.monotonic()
    try:
        outcome = _run_revision_loop(run_date, mode=active_mode)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- advisory by contract
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _LOG.warning(
            "revise: the revision loop failed after %dms (advisory -- "
            "pipeline continues with the issue as it stands) -- %s: %s",
            elapsed_ms, type(exc).__name__, exc,
        )
        _LOG.warning("traceback:\n%s", traceback.format_exc())
        return RevisionOutcome(f"[advisory-guard] {type(exc).__name__}: {exc}")
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _LOG.info(
        "revise: revision phase complete in %dms -- %s", elapsed_ms, outcome.summary,
    )
    return outcome


# ---------------------------------------------------------------------------
# Banners.
# ---------------------------------------------------------------------------

_BANNER_RULE = "=" * 60


def _banner_staging(run_date: _dt.date, stages: list[str]) -> None:
    _LOG.info(_BANNER_RULE)
    _LOG.info(" AI Vector -- STAGING run")
    _LOG.info(" date:    %s", run_date.isoformat())
    _LOG.info(" stages:  %s", ", ".join(stages))
    _LOG.info(_BANNER_RULE)


def _banner_release(run_date: _dt.date, back_release: bool) -> None:
    suffix = " (back-release)" if back_release else ""
    _LOG.info(_BANNER_RULE)
    _LOG.info(" AI Vector -- RELEASE")
    _LOG.info(" date:    %s%s", run_date.isoformat(), suffix)
    _LOG.info(_BANNER_RULE)


def _banner_unrelease(run_date: _dt.date) -> None:
    _LOG.info(_BANNER_RULE)
    _LOG.info(" AI Vector -- UNRELEASE")
    _LOG.info(" date:    %s", run_date.isoformat())
    _LOG.info(_BANNER_RULE)


# ---------------------------------------------------------------------------
# Dry-run printers (per mode).
# ---------------------------------------------------------------------------

_STAGE_ARTIFACTS: dict[str, str] = {
    "fetch":     ("data/staging/{date}/items.jsonl + "
                  "data/staging/{date}/source_health.json"),
    "cluster":   ("data/staging/{date}/clusters.jsonl + "
                  "data/staging/{date}/embeddings/centroids.npz"),
    "rank":      "data/staging/{date}/ranked.jsonl",
    "summarise": "data/staging/{date}/issue.json (issue_number=None)",
    "verify":    ("data/staging/{date}/verify.json + "
                  "data/staging/{date}/issue.json (verification populated)"),
    "render":    "docs/staging/{date}.html",
    "review":    ("data/staging/{date}/review.json (findings + computed "
                  "verdict) + review.md"),
}


def _dry_run_staging(
    run_date: _dt.date, stages: list[str], *, revise_mode: str | None = None,
) -> None:
    date_str = run_date.isoformat()
    print(f"[dry-run] STAGING for date={date_str}:")
    for idx, name in enumerate(stages, start=1):
        artifact = _STAGE_ARTIFACTS[name].format(date=date_str)
        print(f"  {idx}. {name:<9} -> {artifact}")
    if "review" not in stages:
        return
    active_mode, _note = resolve_revise_mode(revise_mode)
    print(f"  then. revise   -> mode={active_mode} ({REVISE_MODE_ENV_VAR})")
    if active_mode == "off":
        print("        the revision phase is disabled; nothing runs after review")
    elif active_mode == "shadow":
        print(f"        one proposal-only pass -> data/staging/{date_str}/"
              "revisions.jsonl; issue.json untouched")
    else:
        print(f"        up to {MAX_REVISION_CYCLES} cycles of "
              "[revise -> verify (touched stories) -> render -> review],")
        print("        stopping early when the computed verdict stops improving")


def _dry_run_release(run_date: _dt.date) -> None:
    date_str = run_date.isoformat()
    released_dir = paths.released_dir(run_date)
    staging_dir = paths.staging_dir(run_date)
    print(f"[dry-run] RELEASE for date={date_str}:")
    print(f"  1. idempotency check: {paths.issue_path(run_date, canonical=True)} "
          "must NOT exist")
    print(f"  2. validate staging:  {paths.issue_path(run_date, canonical=False)} "
          "must exist")
    print( "  3. derive issue_number: max(canonical) + 1")
    print(f"  4. copy peripherals:  {staging_dir}/ -> {released_dir}/")
    print("       items.jsonl, source_health.json, clusters.jsonl, "
          "ranked.jsonl, embeddings/centroids.npz, verify.json (if present)")
    print(f"  5. write canonical issue.json LAST -> "
          f"{paths.issue_path(run_date, canonical=True)}")
    print(f"  6. render canonical -> {paths.DOCS_INDEX} + "
          f"{paths.released_html_path(run_date)}")
    print(f"  7. append URLs -> {paths.PUBLISHED_URLS_PATH}")


def _dry_run_unrelease(run_date: _dt.date) -> None:
    date_str = run_date.isoformat()
    released_dir = paths.released_dir(run_date)
    print(f"[dry-run] UNRELEASE for date={date_str}:")
    print(f"  would delete (in this order):")
    print(f"    1. {paths.issue_path(run_date, canonical=True)}  (commit marker, FIRST)")
    # Reverse order matches the implementation.
    for name in ("ranked.jsonl", "clusters.jsonl", "source_health.json", "items.jsonl"):
        p = released_dir / name
        existence = "exists" if p.exists() else "absent"
        print(f"    -. {p}  ({existence})")
    embeddings_file = released_dir / "embeddings" / "centroids.npz"
    print(f"    -. {embeddings_file}  "
          f"({'exists' if embeddings_file.exists() else 'absent'})")
    print(f"    -. {released_dir}/embeddings/  (rmdir if empty)")
    print(f"    -. {released_dir}/             (rmdir if empty)")
    print(f"  would rebuild: {paths.PUBLISHED_URLS_PATH} from surviving canonical "
          "issue.json files")
    print( "  issue-number gap will be preserved (no renumbering)")


# ---------------------------------------------------------------------------
# Mode dispatchers.
# ---------------------------------------------------------------------------

def _run_pipeline(
    run_date: _dt.date,
    stages: list[str],
    dry_run: bool,
    skip_preflight: bool = False,
    *,
    revise_mode: str | None = None,
) -> int:
    """Run the staging pipeline. Returns Unix exit code."""
    _banner_staging(run_date, stages)
    if dry_run:
        _dry_run_staging(run_date, stages, revise_mode=revise_mode)
        return 0

    # Fresh accumulator per run -- a re-run in the same process (tests, or a
    # long-lived shell) must not carry over a prior run's token counts.
    llm_usage.reset()

    # The env validator hard-fails when LLM stages can't run. Both ``review``
    # and ``verify`` are failure-soft -- a misconfigured env should still let
    # render+release ship -- so we skip the strict check for both when they
    # are the only LLM stages in scope and there are other non-LLM stages
    # running too. When either is the SOLE stage (standalone), we let it fail
    # soft on its own (writes verdict: unavailable) rather than crashing the
    # CLI.
    stages_for_validation = [s for s in stages if s not in ("review", "verify")]
    if stages_for_validation:
        _validate_env_for_stages(stages_for_validation)

    if not skip_preflight:
        from src import preflight
        results, all_passed = preflight.run_checks_for_stages(stages)
        if results:
            print("Pre-flight checks...")
            print(preflight.format_results(results))
            print()
            if not all_passed:
                fail_count = sum(1 for r in results if not r.passed)
                print(
                    f"Pre-flight failed ({fail_count} of {len(results)} checks). "
                    f"Fix the issue above or pass --skip-preflight to bypass."
                )
                return 1

    wall_t0 = time.monotonic()
    stages_succeeded: list[str] = []
    failed_stage: str | None = None
    failure_reason: str | None = None
    verify_summary: str | None = None
    review_summary: str | None = None

    for name in stages:
        ok, message = _run_stage(name, run_date)
        if not ok:
            failed_stage = name
            failure_reason = message
            skipped = [s for s in stages if s != name and s not in stages_succeeded]
            if skipped:
                _LOG.warning(
                    "skipping remaining stages due to %s failure: %s",
                    name, ", ".join(skipped),
                )
            break
        stages_succeeded.append(name)
        if name == "verify":
            verify_summary = message
        if name == "review":
            review_summary = message

    # The revision phase follows the review stage, and only that stage: it
    # acts on the reviewer's findings, so with no review there is nothing to
    # act on. A pipeline that failed earlier never reaches here, which keeps
    # "the reviser edited a broken draft" out of the failure modes.
    revise_summary: str | None = None
    if failed_stage is None and "review" in stages_succeeded:
        outcome = _maybe_revise(run_date, mode=revise_mode)
        revise_summary = outcome.summary
        # A live cycle re-runs review, so the verdict printed at the end of
        # the run must be the one that survived the loop, not the one that
        # triggered it.
        if outcome.review_summary is not None:
            review_summary = f"{outcome.review_summary} (after revision)"

    elapsed = time.monotonic() - wall_t0
    llm_usage_snapshot = llm_usage.snapshot()
    _print_staging_summary(
        run_date, stages_succeeded, failed_stage, failure_reason, elapsed,
        verify_summary=verify_summary,
        review_summary=review_summary,
        revise_summary=revise_summary,
        llm_usage_snapshot=llm_usage_snapshot,
    )
    # Duplicate-risk guard: fires when this run (re)built the issue and
    # earlier issues are staged-but-unreleased -- dedup was blind to them,
    # so the fresh issue may repeat their stories. Printed AFTER the summary
    # so it is the last thing on screen.
    if failed_stage is None and ({"summarise", "render"} & set(stages_succeeded)):
        _warn_unreleased_predecessors(run_date)
    # Council Phase-1: append a metrics line for trend observation. Best-
    # effort -- never fails the run on logging error.
    try:
        _append_run_metrics(
            run_date, stages_succeeded, failed_stage, elapsed,
            llm_usage_snapshot=llm_usage_snapshot,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("metrics: failed to append run-metrics log: %s", exc)
    return 0 if failed_stage is None else 1


def _append_run_metrics(
    run_date: _dt.date,
    stages_succeeded: list[str],
    failed_stage: str | None,
    elapsed_s: float,
    *,
    llm_usage_snapshot: dict[str, Any] | None = None,
) -> None:
    """Append a single JSONL record to ``data/metrics_log.jsonl`` after a
    pipeline run. Phase-1 observability per the council brainstorm.

    Counts are read from the staging archive at end-of-run, so even partial
    runs (e.g. ``--stage fetch`` only) write what they have. Missing files
    are noted but don't break logging.
    """
    import json
    metrics: dict[str, Any] = {
        "date": run_date.isoformat(),
        "run_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "stages_succeeded": stages_succeeded,
        "failed_stage": failed_stage,
        "elapsed_s": round(elapsed_s, 1),
        "llm_usage": llm_usage_snapshot if llm_usage_snapshot is not None else llm_usage.snapshot(),
    }

    staging_dir = paths.staging_dir(run_date)

    # items count
    items_path = staging_dir / "items.jsonl"
    if items_path.exists():
        metrics["items_kept"] = sum(1 for _ in items_path.open("r", encoding="utf-8"))

    # source health summary
    health_path = staging_dir / "source_health.json"
    if health_path.exists():
        try:
            health = json.loads(health_path.read_text(encoding="utf-8"))
            sources = health.get("sources", [])
            metrics["sources_enabled"] = len(sources)
            metrics["sources_fired"] = sum(1 for s in sources if s.get("fired"))
        except Exception:  # noqa: BLE001
            pass

    # clusters count
    clusters_path = staging_dir / "clusters.jsonl"
    if clusters_path.exists():
        metrics["clusters"] = sum(1 for _ in clusters_path.open("r", encoding="utf-8"))

    # ranked tier breakdown
    ranked_path = staging_dir / "ranked.jsonl"
    if ranked_path.exists():
        tier_counts: dict[str, int] = {}
        for line in ranked_path.open("r", encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                tier = json.loads(line).get("tier", "?")
            except json.JSONDecodeError:
                continue
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
        metrics["ranked_total"] = sum(tier_counts.values())
        metrics["ranked_tier_counts"] = tier_counts

    # issue section counts
    issue_path = staging_dir / "issue.json"
    if issue_path.exists():
        try:
            issue = json.loads(issue_path.read_text(encoding="utf-8"))
            section_counts: dict[str, int] = {}
            section_counts["pulse"] = len(issue.get("pulse", {}).get("stories", []))
            for section in issue.get("sections", []):
                section_counts[section.get("name", "?")] = len(section.get("stories", []))
            metrics["section_counts"] = section_counts
            metrics["issue_story_count"] = sum(section_counts.values())
        except Exception:  # noqa: BLE001
            pass

    log_path = paths.DATA_ROOT / "metrics_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(metrics) + "\n")
    _LOG.info("metrics: appended run record to %s", log_path)


def _run_check() -> int:
    """Standalone `--check` mode: run every pre-flight check and exit."""
    from src import preflight
    print("=" * 60)
    print(" AI Vector -- PRE-FLIGHT CHECK")
    print("=" * 60)
    print()
    print("Running all pre-flight checks...")
    results, all_passed = preflight.run_all_checks()
    print(preflight.format_results(results))
    print()
    if all_passed:
        print("All checks passed. Pipeline ready.")
        return 0
    fail_count = sum(1 for r in results if not r.passed)
    print(f"Pre-flight failed ({fail_count} of {len(results)} checks).")
    return 1


def _run_release(
    run_date: _dt.date,
    dry_run: bool,
    back_release: bool,
    *,
    revise: bool = False,
    force: bool = False,
) -> int:
    """Run the release transition. Returns Unix exit code.

    ``revise=True`` opts into the same-date re-release path: instead of
    erroring with AlreadyReleased, the existing canonical's
    ``issue_number`` is preserved and ``revision`` is bumped (rendered
    as ``#N.M``). See DESIGN.md "Issue Number Registry -> Same-date
    re-release (revision bump)" for the full state model.

    ``force=True`` bypasses the staging integrity gate (publish gate) --
    failing assertions are logged at WARNING for audit but the release
    proceeds anyway. For the rare case the operator knows better.
    """
    _banner_release(run_date, back_release)
    if dry_run:
        _dry_run_release(run_date)
        return 0

    from src import render as render_mod

    try:
        before_published = _count_published_urls()
        issue = render_mod.release_promote(run_date, revise=revise, force=force)
    except render_mod.AlreadyReleased as exc:
        _LOG.error("release: %s", exc)
        return 1
    except render_mod.NoStagingDraft as exc:
        _LOG.error("release: %s", exc)
        return 1
    except render_mod.StagingIntegrityFailure as exc:
        _LOG.error("release: %s", exc)
        _LOG.error("release: refusing to publish. Fix the staging draft "
                   "(re-run the pipeline or the failing stage) OR pass "
                   "--force to bypass (logged as a WARNING for audit).")
        return 1

    after_published = _count_published_urls()
    grew_by = max(0, after_published - before_published)
    _LOG.info(_BANNER_RULE)
    _LOG.info(
        " released as issue #%s | %s updated | %s grew by %d URLs | "
        "archive: %s",
        issue.display_number,
        paths.DOCS_INDEX,
        paths.PUBLISHED_URLS_PATH,
        grew_by,
        paths.released_html_path(run_date),
    )
    _LOG.info(_BANNER_RULE)
    return 0


def _run_unrelease(run_date: _dt.date, dry_run: bool) -> int:
    """Run the unrelease reversal. Returns Unix exit code."""
    _banner_unrelease(run_date)
    if dry_run:
        _dry_run_unrelease(run_date)
        return 0

    from src import render as render_mod

    try:
        removed = render_mod.unrelease(run_date)
    except render_mod.NotReleased as exc:
        _LOG.error("unrelease: %s", exc)
        return 1

    _LOG.info(_BANNER_RULE)
    _LOG.info(
        " unreleased %s | %s rebuilt (%d URLs removed) | canonical "
        "issue.json deleted",
        run_date.isoformat(), paths.PUBLISHED_URLS_PATH, removed,
    )
    _LOG.info(_BANNER_RULE)
    return 0


# ---------------------------------------------------------------------------
# Staging end-of-run summary (preserves the Round-2 fail-soft message shape).
# ---------------------------------------------------------------------------

def _print_staging_summary(
    run_date: _dt.date,
    stages_succeeded: list[str],
    failed_stage: str | None,
    failure_reason: str | None,
    elapsed_seconds: float,
    *,
    verify_summary: str | None = None,
    review_summary: str | None = None,
    revise_summary: str | None = None,
    llm_usage_snapshot: dict[str, Any] | None = None,
) -> None:
    mm = int(elapsed_seconds // 60)
    ss = int(elapsed_seconds % 60)
    elapsed_str = f"{mm:02d}:{ss:02d}"

    _LOG.info(_BANNER_RULE)
    if failed_stage is None:
        _LOG.info(" pipeline complete in %s", elapsed_str)
        if "summarise" in stages_succeeded:
            _LOG.info(
                " issue (staging): %s (issue not yet numbered)",
                paths.issue_path(run_date, canonical=False),
            )
        if "render" in stages_succeeded:
            _LOG.info(" preview: %s", paths.staging_html_path(run_date))
        _LOG.info(
            " status: OK -- run 'aiv release' to ship."
        )
    else:
        _LOG.info(" pipeline FAILED at stage: %s", failed_stage)
        _LOG.info(" reason: %s", failure_reason)
        run_part = (
            ", ".join(stages_succeeded) if stages_succeeded else "(none)"
        )
        _LOG.info(" stages run: %s", run_part)
        _LOG.info(" elapsed: %s", elapsed_str)
    _LOG.info(_BANNER_RULE)
    # Advisory verdicts surface AFTER the closing banner so they sit as
    # scannable last lines for Arman.
    if verify_summary is not None:
        _LOG.info("verify: %s", verify_summary)
    if revise_summary is not None:
        _LOG.info("revise: %s", revise_summary)
    if review_summary is not None:
        _LOG.info("review: %s", review_summary)
    # Cost line -- last, so it's the final thing on screen. Silent (no line
    # at all) for stage subsets that never touch the LLM (e.g. fetch/cluster/
    # render only).
    usage_line = llm_usage.format_summary_line(
        llm_usage_snapshot, stage_order=STAGE_ORDER
    )
    if usage_line is not None:
        _LOG.info(usage_line)


_WARN_RULE = "!" * 60


def _warn_unreleased_predecessors(run_date: _dt.date) -> None:
    """Print a loud, unmissable warning when earlier issues are staged but
    not yet released.

    Cross-time dedup (``cluster.py``) reads the released archive only, so any
    issue still sitting in staging is invisible to it. When a later issue is
    built while earlier ones remain unreleased, the later issue can silently
    repeat stories already covered -- the exact "same story three days
    running" failure dedup exists to prevent. The remedy is to release the
    earlier issues oldest-first (so dedup can see them) and then re-run this
    date. Pure file/date check -- No Token Wasted.
    """
    preds = paths.unreleased_predecessors(run_date)
    if not preds:
        return
    pred_strs = [d.isoformat() for d in preds]
    plural = "s" if len(preds) != 1 else ""
    _LOG.warning(_WARN_RULE)
    _LOG.warning(
        "  ⚠  DUPLICATE RISK -- %d earlier issue%s staged but NOT released.",
        len(preds), plural,
    )
    _LOG.warning("     Cross-time dedup could not see %s, so %s may",
                 "them" if plural else "it", run_date.isoformat())
    _LOG.warning("     repeat stories already covered in:")
    for s in pred_strs:
        _LOG.warning("       • %s", s)
    _LOG.warning("  REMEDY: release them oldest-first, then re-run this date:")
    for s in pred_strs:
        _LOG.warning("       aiv release --date %s", s)
    _LOG.warning("       aiv run --date %s", run_date.isoformat())
    _LOG.warning(_WARN_RULE)


def _count_published_urls() -> int:
    """Count lines in ``data/published_urls.txt`` (0 if missing). Cheap +
    used only for the end-of-release summary."""
    if not paths.PUBLISHED_URLS_PATH.exists():
        return 0
    n = 0
    with paths.PUBLISHED_URLS_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                n += 1
    return n


# ---------------------------------------------------------------------------
# CLI (typer). Entry point: `aiv` (pyproject.toml [project.scripts]).
# `python -m src.run` still works via the __main__ guard below.
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="aiv",
    help="AI Vector pipeline orchestrator.",
    no_args_is_help=True,
)

_DATE_HELP = "Issue date YYYY-MM-DD (default: today)."
_DRY_HELP  = "Print the plan and exit without writing anything."
_VERB_HELP = "Set logging level to DEBUG."


@app.command()
def run(
    date: Optional[str] = typer.Option(None, metavar="YYYY-MM-DD", help=_DATE_HELP),
    stage: Optional[str] = typer.Option(
        None, help=f"Run one stage only. One of: {', '.join(STAGE_ORDER)}."
    ),
    stages: Optional[str] = typer.Option(
        None, metavar="A,B,...",
        help="Comma-separated subset of stages, e.g. 'fetch,cluster'.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=_DRY_HELP),
    skip_preflight: bool = typer.Option(
        False, "--skip-preflight",
        help="Skip embedding + LLM pre-flight checks.",
    ),
    no_verify: bool = typer.Option(
        False, "--no-verify",
        help="Skip the advisory factual-verify pass. Verify auto-fires "
             "whenever 'summarise' runs; use this flag to suppress it. "
             "Verify is failure-soft and advisory -- skipping it does not "
             "affect whether the issue can be released.",
    ),
    no_review: bool = typer.Option(
        False, "--no-review",
        help="Skip the post-render editorial review pass. Review auto-fires "
             "whenever 'render' runs; use this flag to suppress it.",
    ),
    no_revise: bool = typer.Option(
        False, "--no-revise",
        help="Skip the revision phase that follows the review. Equivalent to "
             f"{REVISE_MODE_ENV_VAR}=off for this run.",
    ),
    revise_mode: Optional[str] = typer.Option(
        None, "--revise-mode", metavar="MODE",
        help="Override the revision mode for this run: off | shadow | live. "
             f"Default comes from {REVISE_MODE_ENV_VAR}, and from 'shadow' "
             "when that is unset or unrecognised.",
    ),
    verbose: bool = typer.Option(False, "--verbose", help=_VERB_HELP),
) -> None:
    """Fetch, cluster, rank, summarise, verify, render, and review a staging draft.

    The advisory factual-verify pass auto-fires after ``summarise`` runs
    (full pipeline or any stage subset that includes ``summarise``). Pass
    ``--no-verify`` to skip it. Verify is failure-soft and never blocks the
    pipeline or the release.

    The editor's pre-release review auto-fires after ``render`` runs (full
    pipeline or any stage subset that includes ``render``). Pass
    ``--no-review`` to skip it.

    The revision phase follows the review whenever the review ran. In the
    default ``shadow`` mode it makes one proposal-only pass and changes
    nothing; in ``live`` mode it runs up to two cycles of
    [revise, re-verify the touched stories, render, review] and stops as
    soon as the computed verdict stops improving. It is advisory in the
    strongest sense: any failure inside it is logged and the run continues
    with the issue as it stands.
    """
    _setup_logging(verbose)
    _load_env()
    run_date = _resolve_date(date)
    stage_list = _resolve_stages(stage, stages, no_verify=no_verify, no_review=no_review)
    # --no-revise is a shorthand for mode "off" and wins over --revise-mode:
    # a flag that says "don't" should not be overridable by a flag that says
    # "how".
    effective_revise_mode = "off" if no_revise else revise_mode
    sys.exit(_run_pipeline(
        run_date, stage_list, dry_run,
        skip_preflight=skip_preflight,
        revise_mode=effective_revise_mode,
    ))


@app.command()
def review(
    date: Optional[str] = typer.Option(None, metavar="YYYY-MM-DD", help=_DATE_HELP),
    dry_run: bool = typer.Option(False, "--dry-run", help=_DRY_HELP),
    verbose: bool = typer.Option(False, "--verbose", help=_VERB_HELP),
) -> None:
    """Run the editor's pre-release review against a staged issue.

    Reads ``data/staging/<date>/issue.json``, runs one LLM call against the
    review prompt (drawing on EDITORIAL.md), writes
    ``data/staging/<date>/review.md`` with a YAML frontmatter verdict, and
    prints the verdict + one-line summary.

    The review never publishes; it just surfaces concerns to Arman before
    he runs ``aiv release``. Useful when Arman wants a second editorial
    pass after his own edit, without re-rendering.

    Failure-soft: if the LLM call cannot complete, ``review.md`` is
    written with ``verdict: unavailable`` and the command exits 0 -- the
    review never blocks publication.
    """
    _setup_logging(verbose)
    _load_env()
    run_date = _resolve_date(date)
    _banner_review(run_date)
    if dry_run:
        date_str = run_date.isoformat()
        print(f"[dry-run] REVIEW for date={date_str}:")
        print(f"  1. read data/staging/{date_str}/issue.json")
        print(f"  2. read last {3} released issues for drift-watch context")
        print(f"  3. call LLM with review prompt (REVIEW_PROMPT_VERSION)")
        print(f"  4. write data/staging/{date_str}/review.md")
        sys.exit(0)
    from src import review as review_mod

    artifact = review_mod.run_review(date=run_date)
    _LOG.info(_BANNER_RULE)
    _LOG.info(
        " review: %s -- %s -> %s",
        artifact.verdict.upper(), artifact.one_line, artifact.path,
    )
    _LOG.info(_BANNER_RULE)
    print(f"review: {artifact.verdict.upper()} -- \"{artifact.one_line}\"")
    sys.exit(0)


def _banner_review(run_date: _dt.date) -> None:
    _LOG.info(_BANNER_RULE)
    _LOG.info(" AI Vector -- EDITORIAL REVIEW")
    _LOG.info(" date:    %s", run_date.isoformat())
    _LOG.info(_BANNER_RULE)


@app.command()
def gate(
    date: Optional[str] = typer.Option(None, metavar="YYYY-MM-DD", help=_DATE_HELP),
    phase: Optional[str] = typer.Option(
        None, metavar="PHASE",
        help="Override the rollout phase for this invocation: shadow | "
             "green_only | green_amber. Default comes from the "
             "AIV_AUTO_PUBLISH_PHASE environment variable, and from "
             "'shadow' when that is unset or unrecognised.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Compute and print the decision without writing gate.json.",
    ),
    verbose: bool = typer.Option(False, "--verbose", help=_VERB_HELP),
) -> None:
    """Decide whether the staged issue may be published unattended.

    Reads ``data/staging/<date>/{issue.json, review.md, verify.json}``,
    applies the ratified publish policy, and writes the verdict to
    ``data/staging/<date>/gate.json``.

    **The decision lives in the artifact, not the exit code.** This command
    exits 0 whether it decided ``auto_merge`` or ``hold``, so a non-zero
    exit means the gate itself failed to run -- which the publish workflow
    must also treat as a hold. Nothing here publishes anything: acting on
    the decision is the workflow's job, and in the ``shadow`` phase it acts
    on nothing at all.

    No LLM call is made. The gate is deterministic code reading artifacts
    that judgment already produced.
    """
    _setup_logging(verbose)
    _load_env()
    run_date = _resolve_date(date)
    from src import gate as gate_mod

    _LOG.info(_BANNER_RULE)
    _LOG.info(" AI Vector -- PUBLISH GATE")
    _LOG.info(" date:    %s", run_date.isoformat())
    _LOG.info(_BANNER_RULE)

    decision = gate_mod.decide(run_date, phase=phase)

    reasons = ", ".join(decision.hold_reasons) if decision.hold_reasons else "none"
    print(
        f"gate: {decision.decision.upper()} "
        f"(phase={decision.phase}, gate={decision.gate_version}) -- {reasons}"
    )
    for check in decision.checks:
        if not check.passed and check.blocking and check.hold_reason:
            print(f"  - {check.hold_reason}: {check.detail}")
    if decision.note:
        print(f"  note: {decision.note}")

    if dry_run:
        print(f"[dry-run] would write {gate_mod.gate_path(run_date, canonical=False)}")
    else:
        out_path = gate_mod.write_decision(decision)
        print(f"  -> {out_path}")
    sys.exit(0)


@app.command()
def release(
    date: Optional[str] = typer.Option(None, metavar="YYYY-MM-DD", help=_DATE_HELP),
    dry_run: bool = typer.Option(False, "--dry-run", help=_DRY_HELP),
    revise: bool = typer.Option(
        False, "--revise",
        help="Re-release an already-released date as a revision: keeps "
             "issue_number, bumps revision (#N -> #N.1 -> #N.2). Required "
             "to overwrite a released date; without it, an already-released "
             "date errors with AlreadyReleased.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Bypass the staging integrity gate. Without this, release "
             "refuses staging that fails check_integrity() (e.g. fewer "
             "than 3 hands_on stories, missing pulse, source fire rate "
             "below 0.80). Each bypassed assertion is logged at WARNING "
             "for audit. Use sparingly.",
    ),
    verbose: bool = typer.Option(False, "--verbose", help=_VERB_HELP),
) -> None:
    """Promote a staging draft to released and rebuild the index."""
    _setup_logging(verbose)
    _load_env()
    run_date = _resolve_date(date)
    back_release = run_date != _dt.date.today()
    sys.exit(_run_release(run_date, dry_run, back_release, revise=revise, force=force))


@app.command()
def unrelease(
    date: str = typer.Option(..., metavar="YYYY-MM-DD", help="Date to unrelease (required)."),
    dry_run: bool = typer.Option(False, "--dry-run", help=_DRY_HELP),
    verbose: bool = typer.Option(False, "--verbose", help=_VERB_HELP),
) -> None:
    """Reverse a release. Rebuilds published_urls.txt; preserves issue-number gap."""
    _setup_logging(verbose)
    _load_env()
    run_date = _resolve_date(date)
    sys.exit(_run_unrelease(run_date, dry_run))


@app.command()
def check(
    verbose: bool = typer.Option(False, "--verbose", help=_VERB_HELP),
) -> None:
    """Run pre-flight checks (embedding model + LLM endpoint) and exit."""
    _setup_logging(verbose)
    _load_env()
    sys.exit(_run_check())


# ---------------------------------------------------------------------------
# `aiv eval` -- Phase E ergonomic surface for the eval harness.
# Mirrors run/release/unrelease/check. The heavy lifting lives in
# evals/run_evals.py::run_evals; this command is wiring, flag validation,
# and the diff-mode shim.
# ---------------------------------------------------------------------------

_EVAL_DATE_HELP = (
    "Archive date YYYY-MM-DD to eval (default: today + the last 14 released "
    "days)."
)
_EVAL_FIXTURE_HELP = (
    "Run against a fixture dataset under evals/fixtures/ instead of the real "
    "released archive (e.g. '_synthetic' for plumbing tests)."
)
_EVAL_VS_HELP = (
    "Diff today's report against a previous report JSON "
    "(e.g. evals/reports/2026-05-23/091530.json)."
)
_EVAL_STAGING_HELP = (
    "Run integrity and judge evals against the staging archive "
    "(data/staging/<date>/) instead of the released archive. "
    "Dedup precision/recall and Spearman are skipped — labels.yaml only "
    "covers released dates. Mutually exclusive with --fixture."
)


def _run_eval(
    run_date: _dt.date | None,
    judge_only: bool,
    no_judge: bool,
    fixture: str | None,
    vs_path: str | None,
    strict: bool,
    staging: bool = False,
) -> int:
    """Drive the eval harness for the typer `aiv eval` command.

    All flag validation happens here so the subcommand body stays a thin
    shell. The heavy dispatch is owned by ``evals.run_evals.run_evals``;
    this function only translates flags into kwargs, picks fixture vs. real
    mode, prints results, persists the dated report, and runs diff mode
    when ``--vs`` is set.
    """
    if judge_only and no_judge:
        _LOG.error(
            "--judge-only and --no-judge are mutually exclusive; pick one."
        )
        return 1

    if staging and fixture is not None:
        _LOG.error(
            "--staging and --fixture are mutually exclusive; "
            "--staging reads data/staging/<date>/, "
            "--fixture reads evals/fixtures/<name>/."
        )
        return 1

    # Lazy import so `aiv --help` stays cheap and the eval harness only
    # loads when actually invoked. ``evals/`` is not an installed package
    # (pyproject scopes packages to ``src*``), so we bootstrap the repo
    # root onto sys.path -- the harness itself does the same trick for
    # ``from src.models import ...``.
    _repo_root = Path(__file__).resolve().parent.parent
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
    from evals import run_evals as _eh

    # Fixture mode wins when `--fixture` is set; otherwise we run against
    # real archive data (released by default; staging when --staging is set).
    # `--date` selects the day; absent date means "today".
    if fixture is not None:
        against = "fixtures"
        dataset: str | None = fixture
    else:
        against = "real"
        dataset = (run_date or _dt.date.today()).isoformat()

    print("=" * 60)
    print(" AI Vector -- EVAL")
    if fixture is not None:
        print(f" fixture : {fixture}")
    else:
        print(f" date    : {dataset}")
        if staging:
            print(" source  : staging (data/staging/<date>/)")
        else:
            print(" source  : released (data/released/<date>/)")
    flag_bits: list[str] = []
    if judge_only:
        flag_bits.append("judge-only")
    if no_judge:
        flag_bits.append("no-judge")
    if staging:
        flag_bits.append("staging")
    if strict:
        flag_bits.append("strict")
    if flag_bits:
        print(f" flags   : {', '.join(flag_bits)}")
    print("=" * 60)
    print()

    try:
        report, exit_code = _eh.run_evals(
            dataset=dataset,
            against=against,
            judge_only=judge_only,
            no_judge=no_judge,
            strict=strict,
            staging=staging,
        )
    except ValueError as exc:
        _LOG.error("eval: %s", exc)
        return 1

    _eh._print_pretty(report)

    # Dated layout: evals/reports/YYYY-MM-DD/HHMMSS.json -- one per run.
    report_path = _eh._save_report_dated(report)
    print(f"Report written: {report_path}")

    # Diff mode: load the previous report and pretty-print the delta.
    if vs_path is not None:
        prev_path = Path(vs_path)
        try:
            prev_report = _eh._load_report_for_diff(prev_path)
        except FileNotFoundError as exc:
            _LOG.error("eval --vs: %s", exc)
            return 1
        except json.JSONDecodeError as exc:
            _LOG.error("eval --vs: failed to parse %s -- %s", prev_path, exc)
            return 1
        _eh._print_diff(prev_report, report)

    return exit_code


@app.command(name="eval")
def eval_cmd(
    date: Optional[str] = typer.Option(
        None, metavar="YYYY-MM-DD", help=_EVAL_DATE_HELP,
    ),
    judge_only: bool = typer.Option(
        False, "--judge-only",
        help="Run only the LLM-judge eval dimensions.",
    ),
    no_judge: bool = typer.Option(
        False, "--no-judge",
        help="Skip the LLM-judge eval dimensions (fast + free).",
    ),
    fixture: Optional[str] = typer.Option(
        None, "--fixture", metavar="NAME", help=_EVAL_FIXTURE_HELP,
    ),
    vs: Optional[str] = typer.Option(
        None, "--vs", metavar="PATH", help=_EVAL_VS_HELP,
    ),
    strict: bool = typer.Option(
        False, "--strict",
        help="Exit 1 on any warning (stub / skipped), not just hard fails.",
    ),
    staging: bool = typer.Option(
        False, "--staging", help=_EVAL_STAGING_HELP,
    ),
    verbose: bool = typer.Option(False, "--verbose", help=_VERB_HELP),
) -> None:
    """Run the eval harness against the released archive or a fixture.

    Examples:
      aiv eval                              # full suite, today's released day
      aiv eval --date 2026-05-23            # specific date
      aiv eval --judge-only                 # LLM judge only
      aiv eval --no-judge                   # fast + free, skip judge
      aiv eval --fixture _synthetic         # plumbing test
      aiv eval --staging --no-judge         # integrity check on today's staging
      aiv eval --date 2026-05-25 --staging --no-judge  # staging gate for specific date
      aiv eval --vs evals/reports/2026-05-23/091530.json
      aiv eval --strict                     # warnings also exit 1
    """
    _setup_logging(verbose)
    _load_env()
    # `--date` is optional and only meaningful in real (non-fixture) mode.
    # `_resolve_date` raises on malformed input.
    run_date = _resolve_date(date) if date is not None else None
    sys.exit(_run_eval(run_date, judge_only, no_judge, fixture, vs, strict, staging=staging))


# ---------------------------------------------------------------------------
# `aiv revise` -- registered here, implemented in src/revise.py.
#
# The CLI surface is the Architect's (run.py owns `app`); the command's
# behaviour is the LLM Engineer's (revise.py owns the reviser). So the
# registration is a one-liner here and the function body lives there --
# neither seat has to edit the other's file to ship a change.
#
# Registration is fail-soft on purpose. `aiv` is the entry point for
# release, unrelease and gate as well; a syntax error in a module that only
# `aiv revise` needs must not take the whole CLI down at 06:00. When
# revise.py is absent we log at DEBUG (a normal state before it lands); when
# it exists but cannot be registered we log at WARNING, because that is a
# real breakage someone should see.
# ---------------------------------------------------------------------------

_REVISE_COMMAND_CANDIDATES: tuple[str, ...] = ("revise_command", "revise_cmd", "revise")
"""Accepted names for the typer command function in ``src/revise.py``, in
preference order. ``revise_command`` is the one that shipped; the others are
accepted so a rename on that side does not cost the CLI its command."""


def _register_revise_command(typer_app: typer.Typer = app) -> bool:
    """Attach ``aiv revise`` from `src.revise`. Returns True when registered."""
    try:
        from src import revise as revise_mod
    except ImportError:
        _LOG.debug("cli: src/revise.py not present; `aiv revise` not registered")
        return False
    except Exception as exc:  # noqa: BLE001 -- a broken module must not kill `aiv`
        _LOG.warning(
            "cli: src/revise.py could not be imported; `aiv revise` is "
            "unavailable but the rest of the CLI still works -- %s: %s",
            type(exc).__name__, exc,
        )
        return False

    for name in _REVISE_COMMAND_CANDIDATES:
        command = getattr(revise_mod, name, None)
        if callable(command):
            typer_app.command(name="revise")(command)
            return True
    _LOG.warning(
        "cli: src/revise.py exposes none of %s; `aiv revise` not registered",
        ", ".join(_REVISE_COMMAND_CANDIDATES),
    )
    return False


_register_revise_command()


# ---------------------------------------------------------------------------
# __main__ guard — keeps `python -m src.run` working.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
