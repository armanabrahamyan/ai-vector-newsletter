"""
evals/run_evals.py — AI Vector Eval Harness Entry Point

Usage:
    python -m evals.run_evals [--dataset <name>] [--against <real|fixtures>]
                              [--report <pretty|json>]

Exit codes:
    0  All evals passed (or skipped as not-yet-implemented stubs).
    1  At least one eval reported a regression.

Eval dimensions:
    1. dedup_quality       STUB — ready when fixtures + labels land
    2. ranking_quality     STUB — ready when fixtures + labels land
    3. voice_adherence     STUB — rubric co-dev with Editor in Phase 2
    4. module_integrity    READY — schema-validates any archive or fixture day
    5. drift_detection     STUB — needs a corpus of ratified issues to compare
    6. behavioural_check   MANUAL — Eval Engineer writes the weekly note by hand;
                                    this function is a placeholder that always
                                    returns "manual" status

Implementation status is marked at the top of each eval function.
STUB functions return a graceful "not_yet_implemented" result rather than
crashing, so CI can wire the harness from day one without false failures.

The contract:
    - Each eval function returns an EvalResult dataclass.
    - main() aggregates results, writes a report, and exits with 0 or 1.
    - A STUB result with passed=True and status="not_yet_implemented" does NOT
      cause a non-zero exit. Only a real regression (passed=False) does.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Project root (so this module can be run from anywhere)
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
EVALS_DIR = REPO_ROOT / "evals"
FIXTURES_DIR = EVALS_DIR / "fixtures"
DATA_DIR = REPO_ROOT / "data"
LABELS_PATH = EVALS_DIR / "labels.yaml"
REPORTS_DIR = EVALS_DIR / "reports"


# ---------------------------------------------------------------------------
# Result dataclass — every eval function returns one of these
# ---------------------------------------------------------------------------

@dataclass
class EvalResult:
    """Structured result returned by every eval function."""
    name: str                                   # eval dimension name
    passed: bool                                # False = regression = non-zero exit
    metric: Optional[float]                     # the primary numeric metric (None if stub)
    status: str                                 # "pass" | "fail" | "not_yet_implemented" | "manual"
    details: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None                 # set if the eval itself threw


def _stub_result(name: str) -> EvalResult:
    """Return a graceful not-yet-implemented result. Does NOT cause exit(1)."""
    return EvalResult(
        name=name,
        passed=True,
        metric=None,
        status="not_yet_implemented",
        details={"message": "Stub — implementation pending Phase 2 fixtures."},
    )


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

def _load_jsonl(path: Path) -> list[dict]:
    """
    Load a JSONL file into a list of dicts. Returns [] on missing file.
    Does NOT silently skip malformed lines — raises on first bad line so
    the module-integrity eval can surface corruption.
    """
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Malformed JSON at {path}:{lineno}: {exc}"
                ) from exc
    return records


def _load_json(path: Path) -> Optional[dict]:
    """Load a single JSON file. Returns None on missing file."""
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_labels() -> dict:
    """Load labels.yaml. Returns empty structure on missing file."""
    try:
        import yaml  # soft dep — only needed at eval time
    except ImportError:
        return {"per_cluster": [], "per_issue": [], "per_source": []}
    if not LABELS_PATH.exists():
        return {"per_cluster": [], "per_issue": [], "per_source": []}
    with LABELS_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return {
        "per_cluster": data.get("per_cluster", []),
        "per_issue": data.get("per_issue", []),
        "per_source": data.get("per_source", []),
    }


def _resolve_dataset_dir(dataset: Optional[str], against: str) -> Optional[Path]:
    """
    Resolve the directory for a given dataset name and source ("real" | "fixtures").
    Returns None if the dataset cannot be located.
    """
    if against == "fixtures":
        if dataset is None:
            return None
        return FIXTURES_DIR / dataset
    elif against == "real":
        if dataset is None:
            return None
        return DATA_DIR / dataset
    return None


def _list_fixture_datasets() -> list[str]:
    """Return all fixture dataset names (subdirectories under fixtures/)."""
    if not FIXTURES_DIR.exists():
        return []
    return [
        d.name for d in sorted(FIXTURES_DIR.iterdir())
        if d.is_dir() and not d.name.startswith(".")
    ]


# ---------------------------------------------------------------------------
# Eval 1 — Dedup quality
# STATUS: STUB
#
# Will compute: dedup precision, recall, F1 of cluster.py's output
# vs. the ground_truth_group_id assignments in labels.yaml.
# Includes within-day clustering and cross-time cross_time_ref assignment.
#
# Implementation path (Phase 2):
#   - Load clusters.jsonl from the dataset.
#   - Load per_cluster labels from labels.yaml for this dataset.
#   - Build predicted groups (cluster_id -> cluster membership by Item.id)
#     and ground-truth groups (ground_truth_group_id).
#   - Compute pairwise precision/recall using the standard
#     cluster-pair-counting method (Amigo et al. 2009).
#   - PASS threshold: precision >= 0.85 AND recall >= 0.80 (tune against
#     first real fixture set; record final thresholds here).
# ---------------------------------------------------------------------------

def eval_dedup_quality(
    dataset_dir: Optional[Path],
    labels: dict,
) -> EvalResult:
    """
    STUB. Dedup precision/recall vs. ground-truth cluster groupings.
    Returns not_yet_implemented until real fixtures + labels land.
    """
    return _stub_result("dedup_quality")


# ---------------------------------------------------------------------------
# Eval 2 — Ranking quality (Spearman)
# STATUS: STUB
#
# Will compute: Spearman rank correlation between LLM-assigned scores in
# ranked.jsonl and the human_relevance labels in labels.yaml.
#
# Implementation path (Phase 2):
#   - Load ranked.jsonl from the dataset.
#   - Load per_cluster labels (human_relevance) for the same dataset.
#   - Align by cluster_id (inner join — only labelled clusters contribute).
#   - Compute scipy.stats.spearmanr on (llm_score, human_relevance).
#   - PASS threshold: Spearman rho >= 0.70 (PLAN §3 baseline; tune after
#     first 30+ labelled clusters).
# ---------------------------------------------------------------------------

def eval_ranking_quality(
    dataset_dir: Optional[Path],
    labels: dict,
) -> EvalResult:
    """
    STUB. Spearman correlation of LLM scores vs. human relevance labels.
    Returns not_yet_implemented until real fixtures + labels land.
    """
    return _stub_result("ranking_quality")


# ---------------------------------------------------------------------------
# Eval 3 — Voice adherence
# STATUS: STUB
#
# Will compute: a voice adherence score for the most recent issue,
# judged by a separate LLM call against evals/voice/rubric.yaml.
# Tracked per-issue over time; flags trend deviations.
#
# Implementation path (Phase 2, after Editor co-develops voice rubric):
#   - Load issue.json from the dataset.
#   - Load evals/voice/rubric.yaml (Editor co-authors).
#   - Call a separate LLM (independent from summarise.py's model where
#     possible) to score the issue on: warmth, signal density, direction
#     presence, finance-lens presence-without-overreach, callback quality.
#   - Compare score to rolling 14-day mean from evals/reports/*.json.
#   - PASS: score >= baseline - 0.5 std (flag but don't block if 1–2 std
#     below; block if > 2 std below for three consecutive issues).
# ---------------------------------------------------------------------------

def eval_voice_adherence(
    dataset_dir: Optional[Path],
    labels: dict,
) -> EvalResult:
    """
    STUB. Voice adherence scored by a separate LLM call against the voice
    rubric. Returns not_yet_implemented until rubric and corpus exist.
    """
    return _stub_result("voice_adherence")


# ---------------------------------------------------------------------------
# Eval 4 — Module-level integrity
# STATUS: READY
#
# Schema-validates every artifact in the dataset directory against the
# pydantic contracts in src/models.py. Also cross-checks referential
# integrity: item_ids in clusters exist in items, cluster_ids in ranked
# exist in clusters, story_ids in issue.json exist in ranked.
#
# This eval is READY and runs on any archive day or fixture dataset.
# It does not require labels.
# ---------------------------------------------------------------------------

def eval_module_integrity(
    dataset_dir: Optional[Path],
) -> EvalResult:
    """
    READY. Schema-validates all artifacts in dataset_dir and cross-checks
    referential integrity. Fails on any schema violation or broken reference.
    """
    if dataset_dir is None or not dataset_dir.exists():
        return EvalResult(
            name="module_integrity",
            passed=True,
            metric=None,
            status="skipped",
            details={"message": f"Dataset directory not found: {dataset_dir}"},
        )

    failures: list[str] = []
    artifact_count = 0

    # --- Try to import pydantic models ---
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from src.models import Item, Cluster, RankedStory, Issue  # noqa: F401
        models_available = True
    except ImportError:
        models_available = False
        # Can still check JSON parse + referential integrity without pydantic
        failures.append(
            "WARNING: src/models.py not importable — pydantic shape checks skipped; "
            "running JSON-parse + referential checks only."
        )

    # --- Load artifacts ---
    raw_items = []
    raw_clusters = []
    raw_ranked = []
    raw_issue = None

    items_path = dataset_dir / "items.jsonl"
    clusters_path = dataset_dir / "clusters.jsonl"
    ranked_path = dataset_dir / "ranked.jsonl"
    issue_path = dataset_dir / "issue.json"

    for path, container, label in [
        (items_path, raw_items, "items.jsonl"),
        (clusters_path, raw_clusters, "clusters.jsonl"),
        (ranked_path, raw_ranked, "ranked.jsonl"),
    ]:
        try:
            data = _load_jsonl(path)
            container.extend(data)
            artifact_count += 1 if path.exists() else 0
        except ValueError as exc:
            failures.append(f"JSON parse error in {label}: {exc}")

    try:
        raw_issue = _load_json(issue_path)
        if raw_issue is not None:
            artifact_count += 1
    except json.JSONDecodeError as exc:
        failures.append(f"JSON parse error in issue.json: {exc}")

    # --- Pydantic shape validation ---
    if models_available:
        from pydantic import ValidationError

        for record in raw_items:
            try:
                Item(**record)
            except (ValidationError, TypeError) as exc:
                item_id = record.get("id", "<unknown>")
                failures.append(f"Item schema error (id={item_id}): {exc}")

        for record in raw_clusters:
            try:
                Cluster(**record)
            except (ValidationError, TypeError) as exc:
                cid = record.get("cluster_id", "<unknown>")
                failures.append(f"Cluster schema error (cluster_id={cid}): {exc}")

        for record in raw_ranked:
            try:
                RankedStory(**record)
            except (ValidationError, TypeError) as exc:
                cid = record.get("cluster_id", "<unknown>")
                failures.append(f"RankedStory schema error (cluster_id={cid}): {exc}")

        if raw_issue is not None:
            try:
                Issue(**raw_issue)
            except (ValidationError, TypeError) as exc:
                failures.append(f"Issue schema error: {exc}")

    # --- Referential integrity ---
    item_ids = {r.get("id") for r in raw_items if r.get("id")}
    cluster_ids = {r.get("cluster_id") for r in raw_clusters if r.get("cluster_id")}
    ranked_cluster_ids = {r.get("cluster_id") for r in raw_ranked if r.get("cluster_id")}

    # Every item_id in clusters must exist in items
    for record in raw_clusters:
        for iid in record.get("item_ids", []):
            if iid not in item_ids:
                failures.append(
                    f"Referential error: cluster {record.get('cluster_id')} "
                    f"references item_id={iid} not in items.jsonl"
                )

    # Every cluster_id in ranked must exist in clusters
    for missing in ranked_cluster_ids - cluster_ids:
        failures.append(
            f"Referential error: ranked.jsonl references cluster_id={missing} "
            f"not in clusters.jsonl"
        )

    # Every story_id in issue must exist in ranked
    if raw_issue:
        issue_story_ids: set[str] = set()
        pulse = raw_issue.get("pulse", {})
        for block in pulse.get("stories", []):
            issue_story_ids.add(block.get("story_id", ""))
        for section in raw_issue.get("sections", []):
            for block in section.get("stories", []):
                issue_story_ids.add(block.get("story_id", ""))
        issue_story_ids.discard("")
        for sid in issue_story_ids:
            if sid not in ranked_cluster_ids:
                failures.append(
                    f"Referential error: issue.json story_id={sid} not in ranked.jsonl"
                )

    # --- Issue number uniqueness (cross-archive, when running against real) ---
    # Skipped here (single-dataset run). Full cross-archive check is a separate
    # function run_evals can invoke separately.

    passed = not any(f.startswith("FAIL") or "schema error" in f or "Referential error" in f for f in failures)
    # Warnings (import failures) do not block; schema/referential errors do.
    hard_failures = [f for f in failures if not f.startswith("WARNING")]
    passed = len(hard_failures) == 0

    return EvalResult(
        name="module_integrity",
        passed=passed,
        metric=float(artifact_count),
        status="pass" if passed else "fail",
        details={
            "artifact_count": artifact_count,
            "items_count": len(raw_items),
            "clusters_count": len(raw_clusters),
            "ranked_count": len(raw_ranked),
            "issue_present": raw_issue is not None,
            "models_available": models_available,
            "failures": failures,
        },
    )


# ---------------------------------------------------------------------------
# Eval 5 — Drift detection
# STATUS: STUB
#
# Will compare today's issue against the rolling 14-day median on:
#   - Number of stories
#   - Distribution of audience_tags
#   - Average summary length
#   - Voice adherence score (from Eval 3)
#   - Finance-lens presence rate (fraction of stories with finance_angle set)
#
# Z-score outliers raise a drift flag. The flag is not a veto; it's a
# "please look" — some drift is real (quiet news day). Forces a conversation
# at the weekly drift review.
#
# Implementation path (Phase 2):
#   - Load evals/reports/*.json to build rolling baseline.
#   - Compute per-metric z-score for today's values.
#   - PASS: no metric > 2.5 std from 14-day mean.
#   - FLAG (not block): any metric between 1.5 and 2.5 std.
# ---------------------------------------------------------------------------

def eval_drift_detection(
    dataset_dir: Optional[Path],
    labels: dict,
) -> EvalResult:
    """
    STUB. Detects score, tier-mix, voice, and summary-length drift vs.
    the rolling 14-day baseline. Returns not_yet_implemented until
    a corpus of ratified issues exists.
    """
    return _stub_result("drift_detection")


# ---------------------------------------------------------------------------
# Eval 6 — Behavioural integrity
# STATUS: MANUAL
#
# This is the "team eval," not the pipeline eval. The Eval Engineer writes
# a one-paragraph note in the weekly report covering:
#   - PRs touching contracts going through Architect review?
#   - Postmortems filed in docs/postmortems/ when runs break?
#   - Voice labels accumulating in evals/voice/?
#   - Arman's ratification pattern consistent?
#
# This function does lightweight automated checks (postmortem file count,
# voice label count) to surface signals, then returns "manual" status
# so it never blocks CI. The human note is what matters.
# ---------------------------------------------------------------------------

def eval_behavioural_integrity() -> EvalResult:
    """
    MANUAL. Lightweight automated signals + manual weekly note by Eval Engineer.
    Always returns status="manual" (never blocks CI).
    """
    signals: dict[str, Any] = {}

    # Count postmortems (structural signal only — not quality)
    postmortems_dir = REPO_ROOT / "docs" / "postmortems"
    if postmortems_dir.exists():
        pm_files = list(postmortems_dir.glob("*.md"))
        signals["postmortem_count"] = len(pm_files)
    else:
        signals["postmortem_count"] = 0
        signals["postmortem_dir_missing"] = True

    # Count voice labels accumulated
    voice_dir = EVALS_DIR / "voice"
    if voice_dir.exists():
        voice_label_files = list(voice_dir.glob("*.labels.yaml"))
        signals["voice_label_files"] = len(voice_label_files)
    else:
        signals["voice_label_files"] = 0

    # Count reports (indicates harness has run before)
    if REPORTS_DIR.exists():
        report_files = list(REPORTS_DIR.glob("*.json"))
        signals["report_count"] = len(report_files)
    else:
        signals["report_count"] = 0

    signals["note"] = (
        "Manual weekly paragraph required from Eval Engineer. "
        "Automated signals above are structural only."
    )

    return EvalResult(
        name="behavioural_integrity",
        passed=True,  # never blocks CI
        metric=None,
        status="manual",
        details=signals,
    )


# ---------------------------------------------------------------------------
# Aggregation and reporting
# ---------------------------------------------------------------------------

def _build_report(results: list[EvalResult], dataset: Optional[str], against: str) -> dict:
    """Build a structured report dict from a list of eval results."""
    regressions = [r for r in results if not r.passed]
    return {
        "run_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dataset": dataset,
        "against": against,
        "overall_passed": len(regressions) == 0,
        "regression_count": len(regressions),
        "results": [
            {
                "name": r.name,
                "passed": r.passed,
                "metric": r.metric,
                "status": r.status,
                "details": r.details,
                "error": r.error,
            }
            for r in results
        ],
    }


def _print_pretty(report: dict) -> None:
    """Print a human-readable summary of the eval report."""
    status_icon = "PASS" if report["overall_passed"] else "FAIL"
    print(f"\n=== AI Vector Eval Report [{status_icon}] ===")
    print(f"Dataset : {report['dataset'] or '(all)'}")
    print(f"Against : {report['against']}")
    print(f"Run at  : {report['run_at']}")
    print(f"Regressions: {report['regression_count']}")
    print()
    for r in report["results"]:
        icon = {
            "pass": "[PASS]",
            "fail": "[FAIL]",
            "not_yet_implemented": "[STUB]",
            "manual": "[MANUAL]",
            "skipped": "[SKIP]",
        }.get(r["status"], "[?]")
        metric_str = f" metric={r['metric']:.3f}" if r["metric"] is not None else ""
        print(f"  {icon} {r['name']}{metric_str}")
        if r["status"] == "fail":
            failures = r.get("details", {}).get("failures", [])
            for f in failures[:5]:  # show first 5 failures inline
                print(f"         - {f}")
            if len(failures) > 5:
                print(f"         ... and {len(failures) - 5} more (see JSON report)")
        if r["error"]:
            print(f"         ERROR: {r['error']}")
    print()


def _save_report(report: dict, dataset: Optional[str]) -> None:
    """Persist the report to ``evals/reports/`` (legacy flat layout).

    Used by the argparse ``python -m evals.run_evals`` entrypoint to keep its
    existing behaviour unchanged.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    slug = f"-{dataset}" if dataset else ""
    report_path = REPORTS_DIR / f"{today}{slug}.json"
    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)


def _save_report_dated(report: dict) -> Path:
    """Persist the report to ``evals/reports/YYYY-MM-DD/HHMMSS.json``.

    Used by ``aiv eval`` so every run lands in its own file -- diffing two
    runs from the same day (``--vs``) needs them to be distinguishable. The
    date subdir keeps the corpus tidy as run counts grow.
    """
    now = datetime.now(timezone.utc)
    day_dir = REPORTS_DIR / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    report_path = day_dir / f"{now.strftime('%H%M%S')}.json"
    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    return report_path


# ---------------------------------------------------------------------------
# Diff mode (``aiv eval --vs <prev>``)
# ---------------------------------------------------------------------------

def _print_diff(prev_report: dict, curr_report: dict) -> None:
    """Pretty-print added / removed / changed metrics between two reports.

    Delta-sign on score changes is shown explicitly: ``+`` for improvements,
    ``-`` for regressions. The interpretation of "improvement" is naive
    (higher metric = better); per-metric direction is a Phase-B concern when
    real metrics replace the stubs.
    """
    prev_results = {r["name"]: r for r in prev_report.get("results", [])}
    curr_results = {r["name"]: r for r in curr_report.get("results", [])}

    prev_names = set(prev_results)
    curr_names = set(curr_results)
    added = sorted(curr_names - prev_names)
    removed = sorted(prev_names - curr_names)
    common = sorted(curr_names & prev_names)

    print("\n=== AI Vector Eval Diff ===")
    print(f"Previous : {prev_report.get('run_at', '?')} "
          f"(dataset={prev_report.get('dataset') or '(all)'})")
    print(f"Current  : {curr_report.get('run_at', '?')} "
          f"(dataset={curr_report.get('dataset') or '(all)'})")
    print()

    if added:
        print("Added metrics:")
        for name in added:
            print(f"  [+]  {name}  ({curr_results[name].get('status')})")
        print()
    if removed:
        print("Removed metrics:")
        for name in removed:
            print(f"  [-]  {name}  (was {prev_results[name].get('status')})")
        print()

    changed_lines: list[str] = []
    for name in common:
        prev_r = prev_results[name]
        curr_r = curr_results[name]
        prev_status = prev_r.get("status")
        curr_status = curr_r.get("status")
        prev_metric = prev_r.get("metric")
        curr_metric = curr_r.get("metric")
        status_changed = prev_status != curr_status
        metric_changed = prev_metric != curr_metric
        if not (status_changed or metric_changed):
            continue
        bits: list[str] = []
        if status_changed:
            bits.append(f"status {prev_status} -> {curr_status}")
        if metric_changed:
            if (isinstance(prev_metric, (int, float))
                    and isinstance(curr_metric, (int, float))):
                delta = curr_metric - prev_metric
                sign = "+" if delta >= 0 else ""
                bits.append(
                    f"metric {prev_metric:.3f} -> {curr_metric:.3f} "
                    f"({sign}{delta:.3f})"
                )
            else:
                bits.append(f"metric {prev_metric} -> {curr_metric}")
        changed_lines.append(f"  [~]  {name}: {'; '.join(bits)}")

    if changed_lines:
        print("Changed:")
        for line in changed_lines:
            print(line)
        print()
    elif not added and not removed:
        print("No differences.\n")


def _load_report_for_diff(path: Path) -> dict:
    """Load a prior eval report from disk for diff mode."""
    if not path.exists():
        raise FileNotFoundError(f"Previous report not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Core dispatch -- shared by both CLI surfaces (argparse + typer)
# ---------------------------------------------------------------------------

# Eval dimensions grouped by class. ``run_evals()`` uses these to honour
# ``--judge-only`` / ``--no-judge`` flags from the typer CLI. Today only
# voice_adherence is judge-class (per Phase C of the eval plan); the
# grouping lives here so when Phase C lands more judge dimensions, the
# only change is adding to ``_JUDGE_EVALS``.
_JUDGE_EVALS = {"voice_adherence"}
_REFERENCE_EVALS = {
    "dedup_quality",
    "ranking_quality",
    "module_integrity",
    "drift_detection",
}


def run_evals(
    *,
    dataset: Optional[str] = None,
    against: str = "fixtures",
    judge_only: bool = False,
    no_judge: bool = False,
    strict: bool = False,
) -> tuple[dict, int]:
    """Run the eval suite and return ``(report, exit_code)``.

    Typed entrypoint for ``aiv eval``. Mirrors what ``main()`` does for the
    argparse CLI, minus the side effects (no printing, no on-disk report
    write -- the caller chooses how to surface results).

    Args:
        dataset: Fixture name (``--against fixtures``) or archive date
            (``--against real``). ``None`` enumerates all fixture datasets
            when running against fixtures.
        against: ``"fixtures"`` or ``"real"``.
        judge_only: Run only LLM-judge eval dimensions (Phase C).
        no_judge: Skip LLM-judge eval dimensions. Mutually exclusive with
            ``judge_only`` (the caller enforces).
        strict: When True, *warnings* in the report (any non-pass status
            including stubs) bump the exit code to 1 alongside hard fails.
            Defaults to False so a green-stub run still exits 0.

    Returns:
        ``(report_dict, exit_code)``. Exit code is 0 on all-pass, 1 on any
        regression (or any warning when ``strict=True``).
    """
    if judge_only and no_judge:
        raise ValueError(
            "judge_only and no_judge are mutually exclusive; pick one."
        )

    if against == "fixtures":
        if dataset:
            datasets = [dataset]
        else:
            datasets = _list_fixture_datasets() or ["_synthetic"]
    elif against == "real":
        if not dataset:
            raise ValueError(
                "against='real' requires an explicit dataset (YYYY-MM-DD)."
            )
        datasets = [dataset]
    else:
        raise ValueError(f"against must be 'fixtures' or 'real' (got {against!r}).")

    if judge_only:
        active = _JUDGE_EVALS
    elif no_judge:
        active = _REFERENCE_EVALS
    else:
        active = _JUDGE_EVALS | _REFERENCE_EVALS

    labels = _load_labels()
    all_results: list[EvalResult] = []

    for dataset_name in datasets:
        dataset_dir = _resolve_dataset_dir(dataset_name, against)

        # Map of name -> thunk. We dispatch only those in ``active`` so the
        # judge / no-judge gates trim cost (and, for judge_only, skip
        # deterministic metrics entirely).
        dispatch: dict[str, Any] = {
            "dedup_quality":    lambda: eval_dedup_quality(dataset_dir, labels),
            "ranking_quality":  lambda: eval_ranking_quality(dataset_dir, labels),
            "voice_adherence":  lambda: eval_voice_adherence(dataset_dir, labels),
            "module_integrity": lambda: eval_module_integrity(dataset_dir),
            "drift_detection":  lambda: eval_drift_detection(dataset_dir, labels),
        }

        for name, fn in dispatch.items():
            if name not in active:
                continue
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001
                result = EvalResult(
                    name=name,
                    passed=False,
                    metric=None,
                    status="fail",
                    error=f"Eval function raised: {type(exc).__name__}: {exc}",
                )
            all_results.append(result)

    # Behavioural integrity always runs (not dataset-scoped and never
    # blocks). Skip if --judge-only since it isn't a judge metric.
    if not judge_only:
        all_results.append(eval_behavioural_integrity())

    report = _build_report(all_results, dataset, against)

    # Standard exit: 1 on any hard regression (passed=False).
    exit_code = 0 if report["overall_passed"] else 1

    # --strict elevates warnings too: any non-pass status (stub / skipped /
    # manual) trips a non-zero exit. By design this is loud -- the user
    # opted in.
    if strict and exit_code == 0:
        warning_states = {"not_yet_implemented", "skipped"}
        if any(r["status"] in warning_states for r in report["results"]):
            exit_code = 1

    return report, exit_code


# ---------------------------------------------------------------------------
# main() — orchestrates the full eval run
# ---------------------------------------------------------------------------

def main() -> int:
    """
    Entry point. Returns 0 on all-pass, 1 on any regression.
    Stub evals (status=not_yet_implemented) do not count as regressions.
    """
    parser = argparse.ArgumentParser(
        prog="python -m evals.run_evals",
        description="AI Vector eval harness",
    )
    parser.add_argument(
        "--dataset",
        default=None,
        help=(
            "Fixture dataset name (e.g. '_synthetic' or '2026-06-01-launch-day') "
            "or archive date (YYYY-MM-DD) when --against=real. "
            "Omit to run all available fixture datasets."
        ),
    )
    parser.add_argument(
        "--against",
        choices=["real", "fixtures"],
        default="fixtures",
        help="Run against real archive data or fixtures (default: fixtures).",
    )
    parser.add_argument(
        "--report",
        choices=["pretty", "json"],
        default="pretty",
        help="Output format (default: pretty).",
    )
    args = parser.parse_args()

    try:
        report, exit_code = run_evals(
            dataset=args.dataset,
            against=args.against,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.report == "json":
        print(json.dumps(report, indent=2))
    else:
        _print_pretty(report)

    # Persist report to evals/reports/ (legacy flat layout for this CLI).
    _save_report(report, args.dataset)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
