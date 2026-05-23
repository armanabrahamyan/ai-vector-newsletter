"""
src/run.py -- AI Vector local pipeline orchestrator.

Three top-level modes (mutually exclusive):

  * STAGING run (default) -- ``python -m src.run`` (with optional ``--date``,
    ``--stage``, ``--stages``, ``--dry-run``, ``--verbose``). Runs
    fetch -> cluster -> rank -> summarise -> render(preview) and writes
    under ``data/staging/<date>/``. The default render mode is ``preview``;
    nothing canonical is touched.

  * RELEASE  -- ``python -m src.run --release [--date YYYY-MM-DD]``.
    Promotes an existing staging draft to canonical via
    ``render.release_promote``: derives ``issue_number``, copies peripheral
    files, writes canonical ``issue.json`` LAST, renders
    ``docs/index.html`` + archive, appends URLs to
    ``data/published_urls.txt``. ``--date`` defaults to today; passing
    yesterday's date back-releases a draft.

  * UNRELEASE -- ``python -m src.run --unrelease --date YYYY-MM-DD``.
    Reverses a release: deletes canonical files (issue.json FIRST),
    rebuilds ``data/published_urls.txt`` from the surviving canonical
    archive. Preserves the issue-number gap (DESIGN.md "Gap recovery").

Round B (DESIGN.md "Archive: staging vs canonical"):
  * The default run writes ONLY to staging. Cross-time dedup, callbacks,
    eval, and ``published_urls.txt`` all read canonical-only.
  * Release is a deliberate, separate command (Arman runs it after
    reviewing ``docs/preview/<date>.html``).
  * Unrelease is the documented reversal path.

Module owners (per docs/TEAM.md): orchestration shell is the Architect's;
the individual stages are owned by their respective engineers.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Callable, Iterable

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

# Providers we ship with for v0. Others (openai/litellm/ollama) raise
# NotImplementedError inside rank.py; we surface a clean halt before then.
SUPPORTED_PROVIDERS: frozenset[str] = frozenset({"anthropic", "bedrock"})

_LOG = logging.getLogger("ai_vector.run")


# ---------------------------------------------------------------------------
# Argument parsing.
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build and parse the CLI.

    Top-level modes (mutually exclusive): ``--release``, ``--unrelease``,
    or default (run the staging pipeline). ``--stage`` and ``--stages`` are
    valid only in the default mode.
    """
    parser = argparse.ArgumentParser(
        prog="python -m src.run",
        description=(
            "AI Vector pipeline orchestrator. Default: run fetch -> cluster -> "
            "rank -> summarise -> render(preview) for one date, writing to "
            "data/staging/<date>/. Use --release to promote a staging draft "
            "to canonical, or --unrelease to reverse a release."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--date",
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "Issue date to process (default: today). For --unrelease, "
            "--date is REQUIRED (no implicit 'today')."
        ),
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--release",
        action="store_true",
        default=False,
        help=(
            "Promote an existing staging draft to canonical. Derives "
            "issue_number, ships docs/index.html + docs/archive/<date>.html, "
            "appends URLs to data/published_urls.txt. --date defaults to "
            "today; pass an earlier date to back-release."
        ),
    )
    mode_group.add_argument(
        "--unrelease",
        action="store_true",
        default=False,
        help=(
            "Reverse a release for --date (REQUIRED). Deletes canonical "
            "files (issue.json first) and rebuilds data/published_urls.txt "
            "from the surviving canonical archive. The issue-number gap is "
            "preserved (DESIGN.md 'Gap recovery')."
        ),
    )

    # Back-compat alias: --publish maps to --release with a deprecation
    # warning. Aliased rather than removed so existing notes/aliases don't
    # break in one go.
    mode_group.add_argument(
        "--publish",
        action="store_true",
        default=False,
        help="DEPRECATED alias for --release (will be removed in a future PR).",
    )
    mode_group.add_argument(
        "--check",
        action="store_true",
        default=False,
        help=(
            "Run pre-flight checks and exit (embedding model + LLM endpoint). "
            "Auto-runs before the pipeline by default; use this flag to run "
            "checks standalone."
        ),
    )

    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        default=False,
        help=(
            "Skip the auto-preflight checks before staging pipeline stages. "
            "Use when iterating quickly and you know your setup is good "
            "(or when you want to run --stage fetch despite a broken LLM)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Print the plan and exit without executing. For --unrelease, "
            "lists the files that would be deleted."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Set logging level to DEBUG (default: INFO).",
    )

    stage_group = parser.add_mutually_exclusive_group()
    stage_group.add_argument(
        "--stage",
        default=None,
        metavar="STAGE",
        choices=STAGE_ORDER,
        help=(
            "Run only one stage of the staging pipeline. One of: "
            f"{', '.join(STAGE_ORDER)}. Ignored in --release / --unrelease modes."
        ),
    )
    stage_group.add_argument(
        "--stages",
        default=None,
        metavar="A,B,...",
        help=(
            "Comma-separated subset of stages to run, e.g. "
            "'fetch,cluster'. Executed in pipeline order regardless of the "
            "order given here. Ignored in --release / --unrelease modes."
        ),
    )

    return parser.parse_args(argv)


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


def _resolve_stages(args: argparse.Namespace) -> list[str]:
    """Resolve the requested stage list for the STAGING pipeline mode.

    Precedence:
      1. ``--stage`` -> [that stage].
      2. ``--stages a,b`` -> validated subset, re-ordered to pipeline order.
      3. Default -> full pipeline order.

    Returns the list in pipeline execution order.
    """
    if args.stage is not None:
        return [args.stage]

    if args.stages is not None:
        requested = [s.strip() for s in args.stages.split(",") if s.strip()]
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
            "(see requirements.txt). LLM stages will rely on the ambient "
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
    cross_linked = sum(1 for c in clusters if c.cross_time_ref is not None)
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
    "render":    "docs/preview/{date}.html",
}


def _dry_run_staging(run_date: _dt.date, stages: list[str]) -> None:
    date_str = run_date.isoformat()
    print(f"[dry-run] STAGING for date={date_str}:")
    for idx, name in enumerate(stages, start=1):
        artifact = _STAGE_ARTIFACTS[name].format(date=date_str)
        print(f"  {idx}. {name:<9} -> {artifact}")


def _dry_run_release(run_date: _dt.date) -> None:
    date_str = run_date.isoformat()
    canonical_dir = paths.canonical_dir(run_date)
    staging_dir = paths.staging_dir(run_date)
    print(f"[dry-run] RELEASE for date={date_str}:")
    print(f"  1. idempotency check: {paths.issue_path(run_date, canonical=True)} "
          "must NOT exist")
    print(f"  2. validate staging:  {paths.issue_path(run_date, canonical=False)} "
          "must exist")
    print( "  3. derive issue_number: max(canonical) + 1")
    print(f"  4. copy peripherals:  {staging_dir}/ -> {canonical_dir}/")
    print("       items.jsonl, source_health.json, clusters.jsonl, "
          "ranked.jsonl, embeddings/centroids.npz")
    print(f"  5. write canonical issue.json LAST -> "
          f"{paths.issue_path(run_date, canonical=True)}")
    print(f"  6. render canonical -> {paths.DOCS_INDEX} + "
          f"{paths.archive_html_path(run_date)}")
    print(f"  7. append URLs -> {paths.PUBLISHED_URLS_PATH}")


def _dry_run_unrelease(run_date: _dt.date) -> None:
    date_str = run_date.isoformat()
    canonical_dir = paths.canonical_dir(run_date)
    print(f"[dry-run] UNRELEASE for date={date_str}:")
    print(f"  would delete (in this order):")
    print(f"    1. {paths.issue_path(run_date, canonical=True)}  (commit marker, FIRST)")
    # Reverse order matches the implementation.
    for name in ("ranked.jsonl", "clusters.jsonl", "source_health.json", "items.jsonl"):
        p = canonical_dir / name
        existence = "exists" if p.exists() else "absent"
        print(f"    -. {p}  ({existence})")
    embeddings_file = canonical_dir / "embeddings" / "centroids.npz"
    print(f"    -. {embeddings_file}  "
          f"({'exists' if embeddings_file.exists() else 'absent'})")
    print(f"    -. {canonical_dir}/embeddings/  (rmdir if empty)")
    print(f"    -. {canonical_dir}/             (rmdir if empty)")
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
    return 0 if failed_stage is None else 1


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


def _run_release(run_date: _dt.date, dry_run: bool, back_release: bool) -> int:
    """Run the release transition. Returns Unix exit code."""
    _banner_release(run_date, back_release)
    if dry_run:
        _dry_run_release(run_date)
        return 0

    from src import render as render_mod

    try:
        before_published = _count_published_urls()
        issue = render_mod.release_promote(run_date)
    except render_mod.AlreadyReleased as exc:
        _LOG.error("release: %s", exc)
        return 1
    except render_mod.NoStagingDraft as exc:
        _LOG.error("release: %s", exc)
        return 1

    after_published = _count_published_urls()
    grew_by = max(0, after_published - before_published)
    _LOG.info(_BANNER_RULE)
    _LOG.info(
        " released as issue #%d | %s updated | %s grew by %d URLs | "
        "archive: %s",
        issue.issue_number,
        paths.DOCS_INDEX,
        paths.PUBLISHED_URLS_PATH,
        grew_by,
        paths.archive_html_path(run_date),
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
            _LOG.info(" preview: %s", paths.preview_html_path(run_date))
        _LOG.info(
            " status: OK -- run 'python -m src.run --release' to ship."
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
# Main entry point.
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Programmatic entry point.

    Returns
    -------
    int
        ``0`` -- success (or dry-run finished).
        ``1`` -- a stage failed (staging) or the requested transition could
                 not be completed (release / unrelease).
        ``2`` -- argument error (raised by argparse via SystemExit).
    """
    args = _parse_args(argv)
    _setup_logging(args.verbose)
    _load_env()

    # --- Deprecation alias --------------------------------------------------
    if args.publish:
        _LOG.warning(
            "--publish is DEPRECATED; use --release instead. Mapping to "
            "--release for this run."
        )
        args.release = True

    # --- Mode resolution ---------------------------------------------------
    if args.check:
        if args.date or args.stage or args.stages or args.dry_run:
            _LOG.warning(
                "--date / --stage / --stages / --dry-run are ignored in --check mode."
            )
        return _run_check()

    if args.unrelease:
        # --unrelease requires an explicit --date (no implicit 'today').
        if args.date is None:
            raise SystemExit(
                "--unrelease requires an explicit --date YYYY-MM-DD "
                "(no implicit 'today' to avoid accidental reversals)."
            )
        run_date = _resolve_date(args.date)
        if args.stage or args.stages:
            _LOG.warning(
                "--stage / --stages are ignored in --unrelease mode."
            )
        return _run_unrelease(run_date, args.dry_run)

    if args.release:
        run_date = _resolve_date(args.date)
        back_release = run_date != _dt.date.today()
        if args.stage or args.stages:
            _LOG.warning(
                "--stage / --stages are ignored in --release mode."
            )
        return _run_release(run_date, args.dry_run, back_release)

    # Default: staging pipeline.
    run_date = _resolve_date(args.date)
    stages = _resolve_stages(args)
    return _run_pipeline(run_date, stages, args.dry_run, skip_preflight=args.skip_preflight)


# ---------------------------------------------------------------------------
# __main__ guard.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(main())
