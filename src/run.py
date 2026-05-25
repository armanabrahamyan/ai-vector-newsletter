"""
src/run.py -- AI Vector pipeline orchestrator.

CLI entry point: ``aiv`` (installed via pyproject.toml).
Backwards-compatible: ``python -m src.run`` still works.

Subcommands:
  aiv run       -- fetch -> cluster -> rank -> summarise -> render (staging)
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
from typing import Any, Callable, Iterable, Optional

import typer

from src import paths

# ---------------------------------------------------------------------------
# Stage registry (staging-pipeline only). Release / unrelease are separate
# modes, not stages.
# ---------------------------------------------------------------------------

STAGE_ORDER: tuple[str, ...] = (
    "fetch",
    "cluster",
    "rank",
    "summarise",
    "render",
)

# Stages that require the LLM env vars to be valid before they can run.
LLM_STAGES: frozenset[str] = frozenset({"rank", "summarise"})

# Providers we ship with. anthropic + bedrock have native clients;
# openai/litellm/ollama share the OpenAI-compatible Chat Completions path
# in rank.py (works with any OpenAI-API-compatible gateway).
SUPPORTED_PROVIDERS: frozenset[str] = frozenset(
    {"anthropic", "bedrock", "openai", "litellm", "ollama"}
)

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


def _resolve_stages(stage: str | None, stages: str | None) -> list[str]:
    """Resolve the requested stage list for the STAGING pipeline mode.

    Precedence:
      1. ``--stage`` -> [that stage].
      2. ``--stages a,b`` -> validated subset, re-ordered to pipeline order.
      3. Default -> full pipeline order.

    Returns the list in pipeline execution order.
    """
    if stage is not None:
        return [stage]

    if stages is not None:
        requested = [s.strip() for s in stages.split(",") if s.strip()]
        unknown = [s for s in requested if s not in STAGE_ORDER]
        if unknown:
            raise SystemExit(
                f"--stages contains unknown stage(s): {unknown}. "
                f"Valid stages: {list(STAGE_ORDER)}"
            )
        requested_set = set(requested)
        return [s for s in STAGE_ORDER if s in requested_set]

    return list(STAGE_ORDER)


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


def _run_render_preview(run_date: _dt.date) -> str:
    """Invoke render.render(date, mode='preview') and summarise."""
    from src import render as render_mod

    out_path = render_mod.render(date=run_date, mode="preview")
    return f"preview -> {out_path}"


# Map stage name -> callable. ``render`` always runs in preview mode under
# the staging pipeline; release-mode rendering happens inside release_promote.
_STAGE_HANDLERS: dict[str, Callable[[_dt.date], str]] = {
    "fetch": _run_fetch,
    "cluster": _run_cluster,
    "rank": _run_rank,
    "summarise": _run_summarise,
    "render": _run_render_preview,
}


def _run_stage(name: str, run_date: _dt.date) -> tuple[bool, str]:
    """Execute one staging-pipeline stage with structured logging."""
    _LOG.info("--- stage: %s ---", name)
    handler = _STAGE_HANDLERS[name]
    t0 = time.monotonic()
    try:
        summary = handler(run_date)
    except SystemExit:
        raise
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
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
    "render":    "docs/staging/{date}.html",
}


def _dry_run_staging(run_date: _dt.date, stages: list[str]) -> None:
    date_str = run_date.isoformat()
    print(f"[dry-run] STAGING for date={date_str}:")
    for idx, name in enumerate(stages, start=1):
        artifact = _STAGE_ARTIFACTS[name].format(date=date_str)
        print(f"  {idx}. {name:<9} -> {artifact}")


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
          "ranked.jsonl, embeddings/centroids.npz")
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
    run_date: _dt.date, stages: list[str], dry_run: bool, skip_preflight: bool = False,
) -> int:
    """Run the staging pipeline. Returns Unix exit code."""
    _banner_staging(run_date, stages)
    if dry_run:
        _dry_run_staging(run_date, stages)
        return 0

    _validate_env_for_stages(stages)

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

    elapsed = time.monotonic() - wall_t0
    _print_staging_summary(
        run_date, stages_succeeded, failed_stage, failure_reason, elapsed,
    )
    # Council Phase-1: append a metrics line for trend observation. Best-
    # effort -- never fails the run on logging error.
    try:
        _append_run_metrics(run_date, stages_succeeded, failed_stage, elapsed)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("metrics: failed to append run-metrics log: %s", exc)
    return 0 if failed_stage is None else 1


def _append_run_metrics(
    run_date: _dt.date,
    stages_succeeded: list[str],
    failed_stage: str | None,
    elapsed_s: float,
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
    verbose: bool = typer.Option(False, "--verbose", help=_VERB_HELP),
) -> None:
    """Fetch, cluster, rank, summarise and render a staging draft."""
    _setup_logging(verbose)
    _load_env()
    run_date = _resolve_date(date)
    stage_list = _resolve_stages(stage, stages)
    sys.exit(_run_pipeline(run_date, stage_list, dry_run, skip_preflight=skip_preflight))


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
# __main__ guard — keeps `python -m src.run` working.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
