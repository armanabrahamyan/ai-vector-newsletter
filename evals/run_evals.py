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
import math
import os
import re
import sys
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

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


def _resolve_dataset_dir(
    dataset: Optional[str],
    against: str,
    staging: bool = False,
) -> Optional[Path]:
    """
    Resolve the directory for a given dataset name and source ("real" | "fixtures").
    Returns None if the dataset cannot be located.

    For ``against="real"`` the archive layout is::

        data/released/<YYYY-MM-DD>/   ← primary (published/ratified issues)
        data/staging/<YYYY-MM-DD>/    ← staging (pre-publication, when staging=True)

    When ``staging=True``, ``data/staging/<YYYY-MM-DD>/`` is used directly;
    the released subtree is not consulted.  When ``staging=False`` (the
    default), the released subtree is checked first so evals always run
    against the canonical ratified output.
    """
    if against == "fixtures":
        if dataset is None:
            return None
        return FIXTURES_DIR / dataset
    elif against == "real":
        if dataset is None:
            return None
        if staging:
            return DATA_DIR / "staging" / dataset
        # Check released/ first (canonical), then raw staging dir
        released = DATA_DIR / "released" / dataset
        if released.exists():
            return released
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
# STATUS: READY (Phase B)
#
# Computes: dedup precision, recall, F1 of cluster.py's output vs. the
# ground_truth_group_id assignments in labels.yaml.
#
# Within-day: pairwise pair-counting (Amigo et al. 2009).
#   - For every pair of labelled clusters sharing a ground_truth_group_id,
#     we ask: did the pipeline also group them into a single cluster?
#     Since cluster.py currently represents "a cluster" as a single cluster_id
#     (not sub-grouping within ranked.jsonl), we treat the pipeline as having
#     grouped two clusters together iff they share the same cluster_id — which
#     by construction never happens for distinct clusters. Therefore pipeline
#     precision = 1.0 (it never falsely merges labelled clusters) and recall
#     is the fraction of same-group pairs that were actually merged. With the
#     corpus as it stands, both labelled dedup groups are pipeline misses
#     (the two clusters in each group kept separate cluster_ids), so recall
#     will surface below 1.0 and the miss names are reported explicitly.
#
# Cross-time: for each labelled cross-time chain, verify that the
#   continuation cluster's cross_time_ref points at the right earlier cluster.
#
# PASS thresholds (per PLAN §3, tune after 30+ labelled clusters):
#   precision >= 0.85 AND recall >= 0.80
# ---------------------------------------------------------------------------

def _pairwise_dedup_metrics(
    dataset_name: str,
    per_cluster_labels: list[dict],
    clusters_by_id: dict[str, dict],
) -> dict:
    """
    Compute within-day dedup precision, recall, F1 using pairwise counting.

    Ground-truth positive pairs: every pair (A, B) of labelled clusters for
    this dataset that share a non-null ground_truth_group_id.

    Predicted positive pairs: every pair (A, B) where the pipeline placed
    both in the *same* physical cluster_id (i.e., they are the same cluster).
    With current cluster.py semantics each cluster_id is unique so two distinct
    labelled clusters can only be a predicted-positive pair if they literally
    have the same cluster_id — which is impossible. The only way to score TP
    is if the pipeline assigned them the same cluster_id, which means they
    appeared as one cluster in clusters.jsonl. This is consistent: dedup
    success = two stories appearing as one cluster; miss = two separate
    cluster_ids for the same story.

    Returns a dict with keys: precision, recall, f1, tp, fp, fn,
    gt_pairs, pred_pairs, miss_groups, miss_cluster_pairs.
    """
    # Filter to this dataset
    ds_labels = [
        lbl for lbl in per_cluster_labels
        if lbl.get("dataset") == dataset_name
        and lbl.get("ground_truth_group_id") is not None
    ]

    # Group by ground_truth_group_id
    from collections import defaultdict
    groups: dict[str, list[str]] = defaultdict(list)
    for lbl in ds_labels:
        groups[lbl["ground_truth_group_id"]].append(lbl["cluster_id"])

    # Build ground-truth positive pairs (unordered)
    gt_pairs: set[frozenset] = set()
    for members in groups.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                gt_pairs.add(frozenset([members[i], members[j]]))

    if not gt_pairs:
        return {
            "precision": None,
            "recall": None,
            "f1": None,
            "tp": 0,
            "fp": 0,
            "fn": 0,
            "gt_pairs": 0,
            "pred_pairs": 0,
            "miss_groups": [],
            "miss_cluster_pairs": [],
            "note": "No labelled dedup groups for this dataset.",
        }

    # Build predicted positive pairs: pairs that share the same cluster_id.
    # Since each cluster_id in clusters.jsonl is unique, two distinct labelled
    # cluster_ids can only be merged if the pipeline produced a single cluster
    # covering both. We check: are any two labelled cluster_ids the SAME id?
    all_labelled_ids = [lbl["cluster_id"] for lbl in ds_labels]
    id_to_group: dict[str, str] = {
        lbl["cluster_id"]: lbl["ground_truth_group_id"]
        for lbl in ds_labels
    }

    # Predicted pairs: two labelled cluster_ids share a cluster_id in the
    # pipeline output iff the pipeline merged them. We check by looking at
    # clusters.jsonl — if multiple labelled cluster_ids appear as item_ids
    # within a single cluster, they've been merged (de-duped). But more
    # directly: if two labelled cluster_ids are literally the same string, the
    # pipeline merged them into one entry. In practice we also check whether
    # both appear as distinct entries in clusters.jsonl or not.
    pipeline_cluster_ids = set(clusters_by_id.keys())
    pred_pairs: set[frozenset] = set()

    # A predicted merge: two labelled cluster_ids resolve to the same
    # pipeline cluster (i.e., one of the two is not present as a standalone
    # cluster_id because the pipeline absorbed it into the other).
    # With current cluster.py they always remain separate, so pred_pairs = {}.
    # We leave the loop here for correctness when a future pipeline version
    # does merge them.
    seen_ids = set()
    for cid in all_labelled_ids:
        if cid in seen_ids:
            # Exact same cluster_id seen twice → the pipeline merged them
            # (shouldn't happen with current schema but guards future cases)
            pass
        seen_ids.add(cid)

    # Detect merges: if a labelled cluster_id is NOT in pipeline clusters,
    # it was absorbed into another cluster. This is the primary merge signal.
    # For now, check whether any labelled pair shares a physical pipeline cluster.
    # We look at clusters.jsonl item_ids: if item_ids of cluster A overlap with
    # item_ids of cluster B (by the pipeline's own grouping), they are merged.
    # Simpler: two labelled cluster_ids are predicted-positive if one is
    # absent from pipeline_cluster_ids (meaning the pipeline didn't produce it
    # separately → it was folded into the other).

    # Build: for each labelled cluster_id, find what pipeline cluster contains
    # it or its items. If absent from pipeline_cluster_ids, it was merged.
    # Since cluster_ids are the canonical grouping key, absence = absorbed.
    absorbed: dict[str, Optional[str]] = {}  # labelled_id -> pipeline_id that absorbed it, or None
    for cid in all_labelled_ids:
        if cid in pipeline_cluster_ids:
            absorbed[cid] = None  # present as standalone
        else:
            absorbed[cid] = "merged"  # absent = absorbed by some other cluster

    # Two labelled clusters are a predicted-positive pair if both map to the
    # same physical pipeline cluster. In the absence of cluster absorption info
    # (we don't know *which* cluster absorbed a missing one), we use a
    # conservative rule: a pair is predicted-positive iff exactly one member is
    # present and the other is absent, OR neither is present (suggesting they
    # were both merged into one entry). The most common case: pipeline produces
    # a single cluster for the pair → one of the two labelled cluster_ids
    # is the surviving cluster_id; the other doesn't appear.
    # We check all labelled pairs in each ground-truth group for this.

    miss_groups: list[str] = []
    miss_cluster_pairs: list[list[str]] = []

    tp = 0
    fn = 0
    for group_id, members in groups.items():
        any_tp = False
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                pair = frozenset([a, b])
                # Predicted positive: both cluster_ids absent (merged into one
                # new id) OR one present and one absent (the absent one was
                # folded into the present one).
                a_present = a in pipeline_cluster_ids
                b_present = b in pipeline_cluster_ids
                if not a_present and not b_present:
                    # Both gone — likely merged into a third cluster_id (unlikely
                    # without knowing that id; conservatively treat as TP since
                    # both were absorbed, meaning the pipeline did merge them).
                    pred_pairs.add(pair)
                    tp += 1
                    any_tp = True
                elif a_present != b_present:
                    # One survived, one was absorbed → pipeline merged them.
                    pred_pairs.add(pair)
                    tp += 1
                    any_tp = True
                else:
                    # Both present as separate clusters → pipeline missed the merge.
                    fn += 1
                    miss_cluster_pairs.append([a, b])
        if not any_tp:
            miss_groups.append(group_id)

    # FP: predicted pairs that are NOT in gt_pairs.
    fp = len(pred_pairs - gt_pairs)

    # Precision / recall / F1
    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0  # no false positives → perfect precision
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "gt_pairs": len(gt_pairs),
        "pred_pairs": len(pred_pairs),
        "miss_groups": miss_groups,
        "miss_cluster_pairs": miss_cluster_pairs,
    }


def _cross_time_dedup_check(
    dataset_name: str,
    per_cluster_labels: list[dict],
    clusters_by_id: dict[str, dict],
) -> dict:
    """
    Verify cross-time dedup chains: for each cluster in the dataset that has
    a cross_time_ref in the pipeline output, confirm it matches the labelled
    expectation. Structured to scale as more chains are labelled.

    Currently checks:
      2026-05-24: c_1e720df7574da5b7 -> cross_time_ref == c_60339c2e21a15eb7

    Returns a dict: chains_checked, chains_correct, chains_wrong, details.
    """
    # Hardcoded cross-time chains as labelled ground truth.
    # Schema: {today_cluster_id: expected_cross_time_ref}
    # As the corpus grows, these can be pulled from labels.yaml
    # (add a cross_time_ref field to the per_cluster schema).
    LABELLED_CHAINS: dict[str, str] = {
        "c_1e720df7574da5b7": "c_60339c2e21a15eb7",
    }

    chains_checked = 0
    chains_correct = 0
    chains_wrong = 0
    details: list[dict] = []

    for cluster_id, expected_ref in LABELLED_CHAINS.items():
        # Only check if the cluster belongs to the current dataset
        cluster_data = clusters_by_id.get(cluster_id)
        if cluster_data is None:
            # This chain doesn't exist in the current date's data — skip
            continue

        chains_checked += 1
        actual_ref = cluster_data.get("cross_time_ref")

        if actual_ref == expected_ref:
            chains_correct += 1
            details.append({
                "cluster_id": cluster_id,
                "expected_ref": expected_ref,
                "actual_ref": actual_ref,
                "status": "correct",
            })
        else:
            chains_wrong += 1
            details.append({
                "cluster_id": cluster_id,
                "expected_ref": expected_ref,
                "actual_ref": actual_ref,
                "status": "wrong" if actual_ref is not None else "missing",
            })

    return {
        "chains_checked": chains_checked,
        "chains_correct": chains_correct,
        "chains_wrong": chains_wrong,
        "details": details,
    }


def eval_dedup_quality(
    dataset_dir: Optional[Path],
    labels: dict,
) -> EvalResult:
    """
    Phase B. Dedup precision/recall/F1 vs. ground-truth cluster groupings,
    plus cross-time cross_time_ref verification.

    Within-day: pairwise pair-counting against ground_truth_group_id labels.
    Cross-time: verifies cross_time_ref fields against labelled chains.

    PASS thresholds: precision >= 0.85 AND recall >= 0.80.
    """
    if dataset_dir is None or not dataset_dir.exists():
        return EvalResult(
            name="dedup_quality",
            passed=True,
            metric=None,
            status="skipped",
            details={"message": f"Dataset directory not found: {dataset_dir}"},
        )

    per_cluster = labels.get("per_cluster", [])

    # Determine dataset name from directory (e.g. "2026-05-24")
    dataset_name = dataset_dir.name

    # Load clusters.jsonl
    clusters_path = dataset_dir / "clusters.jsonl"
    try:
        raw_clusters = _load_jsonl(clusters_path)
    except ValueError as exc:
        return EvalResult(
            name="dedup_quality",
            passed=False,
            metric=None,
            status="fail",
            error=f"Failed to load clusters.jsonl: {exc}",
        )

    clusters_by_id: dict[str, dict] = {
        r["cluster_id"]: r for r in raw_clusters if r.get("cluster_id")
    }

    # --- Within-day dedup metrics ---
    within_day = _pairwise_dedup_metrics(dataset_name, per_cluster, clusters_by_id)

    # --- Cross-time dedup check ---
    cross_time = _cross_time_dedup_check(dataset_name, per_cluster, clusters_by_id)

    # Determine pass/fail
    precision = within_day.get("precision")
    recall = within_day.get("recall")

    PRECISION_THRESHOLD = 0.85
    RECALL_THRESHOLD = 0.80

    failures: list[str] = []
    if precision is not None and precision < PRECISION_THRESHOLD:
        failures.append(
            f"Dedup precision {precision:.4f} < threshold {PRECISION_THRESHOLD}"
        )
    if recall is not None and recall < RECALL_THRESHOLD:
        failures.append(
            f"Dedup recall {recall:.4f} < threshold {RECALL_THRESHOLD} "
            f"(miss groups: {within_day.get('miss_groups', [])})"
        )
    if cross_time["chains_wrong"] > 0:
        failures.append(
            f"Cross-time dedup: {cross_time['chains_wrong']} chain(s) have wrong "
            f"cross_time_ref"
        )

    passed = len(failures) == 0
    f1 = within_day.get("f1")

    return EvalResult(
        name="dedup_quality",
        passed=passed,
        metric=f1,
        status="pass" if passed else "fail",
        details={
            "dataset": dataset_name,
            "within_day": within_day,
            "cross_time": cross_time,
            "thresholds": {
                "precision": PRECISION_THRESHOLD,
                "recall": RECALL_THRESHOLD,
            },
            "failures": failures,
        },
    )


# ---------------------------------------------------------------------------
# Eval 2 — Ranking quality (Spearman)
# STATUS: READY (Phase B)
#
# Computes: Spearman rank correlation between LLM-assigned scores in
# ranked.jsonl and the human_relevance labels in labels.yaml.
#
# Inner join on cluster_id: only labelled clusters contribute.
# Per-tier Spearman: separate rho for on_the_radar bucket vs. cut bucket
# to surface the rank-vs-human disagreement on borderline items.
#
# Tier disagreements: clusters where expected_tier != pipeline tier are
# reported explicitly (e.g. FCA/BoE statement c_ce540f73ee188ea2 which
# Arman ratified as pulse but rank.py called on_the_radar).
#
# PASS threshold: overall Spearman rho >= 0.70 (tune after 30+ labelled).
# ---------------------------------------------------------------------------

def eval_ranking_quality(
    dataset_dir: Optional[Path],
    labels: dict,
) -> EvalResult:
    """
    Phase B. Spearman correlation of LLM scores vs. human relevance labels.

    Runs overall Spearman plus per-tier breakdown (on_the_radar vs. cut
    bucket). Reports tier disagreements between expected_tier labels and
    pipeline tier assignments.

    PASS threshold: overall rho >= 0.70.
    """
    if dataset_dir is None or not dataset_dir.exists():
        return EvalResult(
            name="ranking_quality",
            passed=True,
            metric=None,
            status="skipped",
            details={"message": f"Dataset directory not found: {dataset_dir}"},
        )

    try:
        from scipy.stats import spearmanr
    except ImportError:
        return EvalResult(
            name="ranking_quality",
            passed=False,
            metric=None,
            status="fail",
            error="scipy not installed — cannot compute Spearman correlation.",
        )

    dataset_name = dataset_dir.name
    per_cluster = labels.get("per_cluster", [])

    # Build label index: cluster_id -> label record (for this dataset only)
    label_index: dict[str, dict] = {
        lbl["cluster_id"]: lbl
        for lbl in per_cluster
        if lbl.get("dataset") == dataset_name
        and lbl.get("human_relevance") is not None
    }

    if not label_index:
        return EvalResult(
            name="ranking_quality",
            passed=True,
            metric=None,
            status="skipped",
            details={"message": f"No labelled clusters with human_relevance for dataset {dataset_name!r}."},
        )

    # Load ranked.jsonl
    ranked_path = dataset_dir / "ranked.jsonl"
    try:
        raw_ranked = _load_jsonl(ranked_path)
    except ValueError as exc:
        return EvalResult(
            name="ranking_quality",
            passed=False,
            metric=None,
            status="fail",
            error=f"Failed to load ranked.jsonl: {exc}",
        )

    # Inner join: keep only ranked entries that have a label
    joined: list[dict] = []
    for record in raw_ranked:
        cid = record.get("cluster_id")
        if cid and cid in label_index:
            joined.append({
                "cluster_id": cid,
                "score": record.get("score"),
                "pipeline_tier": record.get("tier"),
                "human_relevance": label_index[cid]["human_relevance"],
                "expected_tier": label_index[cid].get("expected_tier"),
            })

    if len(joined) < 2:
        return EvalResult(
            name="ranking_quality",
            passed=True,
            metric=None,
            status="skipped",
            details={"message": f"Fewer than 2 labelled+ranked clusters for {dataset_name!r}; cannot compute Spearman."},
        )

    # --- Overall Spearman ---
    all_scores = [r["score"] for r in joined]
    all_relevances = [r["human_relevance"] for r in joined]
    overall_result = spearmanr(all_scores, all_relevances)
    overall_rho = float(overall_result.statistic)
    overall_pvalue = float(overall_result.pvalue)

    # --- Per-tier Spearman ---
    # Tier split is based on PIPELINE tier (not expected_tier) so we're
    # measuring rank.py's own behaviour within each tier bucket.
    on_radar_rows = [r for r in joined if r["pipeline_tier"] == "on_the_radar"]
    cut_rows = [r for r in joined if r["pipeline_tier"] == "cut"]

    tier_spearman: dict[str, Any] = {}
    def _safe_spearmanr(xs: list, ys: list) -> dict:
        """Compute Spearman rho, returning None values on constant-input or
        other degenerate cases. Suppresses scipy's ConstantInputWarning so
        the output is clean when a tier bucket has uniform human_relevance
        (common with small corpora)."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = spearmanr(xs, ys)
        rho = float(res.statistic)
        pval = float(res.pvalue)
        # NaN results from constant input — report as None
        if math.isnan(rho):
            return {"rho": None, "pvalue": None, "note": "constant_input"}
        return {"rho": round(rho, 4), "pvalue": round(pval, 4)}

    if len(on_radar_rows) >= 2:
        tier_spearman["on_the_radar"] = {
            **_safe_spearmanr(
                [r["score"] for r in on_radar_rows],
                [r["human_relevance"] for r in on_radar_rows],
            ),
            "n": len(on_radar_rows),
        }
    else:
        tier_spearman["on_the_radar"] = {"rho": None, "pvalue": None, "n": len(on_radar_rows)}

    if len(cut_rows) >= 2:
        tier_spearman["cut"] = {
            **_safe_spearmanr(
                [r["score"] for r in cut_rows],
                [r["human_relevance"] for r in cut_rows],
            ),
            "n": len(cut_rows),
        }
    else:
        tier_spearman["cut"] = {"rho": None, "pvalue": None, "n": len(cut_rows)}

    # --- Tier disagreements ---
    # Report clusters where expected_tier (from labels) != pipeline tier.
    # Spearman uses scores directly so tier overrides don't change the
    # numeric correlation, but disagreements are valuable editorial signal.
    tier_disagreements: list[dict] = []
    for r in joined:
        if r["expected_tier"] is not None and r["expected_tier"] != r["pipeline_tier"]:
            tier_disagreements.append({
                "cluster_id": r["cluster_id"],
                "pipeline_tier": r["pipeline_tier"],
                "expected_tier": r["expected_tier"],
                "score": r["score"],
                "human_relevance": r["human_relevance"],
            })

    # --- Pass / fail ---
    RHO_THRESHOLD = 0.70
    failures: list[str] = []
    if overall_rho < RHO_THRESHOLD:
        failures.append(
            f"Overall Spearman rho {overall_rho:.4f} < threshold {RHO_THRESHOLD}"
        )

    passed = len(failures) == 0

    return EvalResult(
        name="ranking_quality",
        passed=passed,
        metric=round(overall_rho, 4),
        status="pass" if passed else "fail",
        details={
            "dataset": dataset_name,
            "n_labelled": len(joined),
            "overall": {
                "rho": round(overall_rho, 4),
                "pvalue": round(overall_pvalue, 4),
            },
            "per_tier": tier_spearman,
            "tier_disagreements": tier_disagreements,
            "tier_disagreement_count": len(tier_disagreements),
            "threshold": RHO_THRESHOLD,
            "failures": failures,
        },
    )


# ---------------------------------------------------------------------------
# Eval 3 — Voice adherence (Phase C: LLM-as-judge)
# STATUS: READY
#
# Implements LLM-as-judge quality scoring for:
#   - Full issue: 5 voice dimensions (warmth, signal_density, direction,
#     finance_lens_presence, callback_quality)
#   - Per-story: headline quality, summary quality, signal appropriateness
#   - Per-section: intro quality
#
# Judge model: Anthropic Opus (or Haiku) — different from generation model.
# Caching: SHA-256(artifact_json + prompt_version) — unchanged artifacts
# cost zero on re-runs.
#
# PASS threshold: aggregate fail-rate across all dimensions < 25%
# (VOICE_FAIL_THRESHOLD). This is the v0 threshold; tune after 5 issues.
#
# Agreement with Editor labels: loaded from evals/voice/YYYY-MM-DD.labels.yaml
# when available. Reported as a sanity metric (disagreement is information,
# not a test failure).
# ---------------------------------------------------------------------------

# Aggregate fail-rate threshold. Tunable — v0 default per the plan.
VOICE_FAIL_THRESHOLD = 0.25


def _score_to_numeric(score: str) -> float:
    """Convert pass/borderline/fail/error to a 0-1 value for aggregation."""
    return {"pass": 1.0, "borderline": 0.5, "fail": 0.0, "error": 0.0,
            "not_applicable": 1.0}.get(score, 0.0)


def _load_voice_labels(date_str: str) -> Optional[dict]:
    """Load the Editor's draft labels for a given issue date, if they exist."""
    labels_path = EVALS_DIR / "voice" / f"{date_str}.labels.yaml"
    if not labels_path.exists():
        return None
    try:
        import yaml
    except ImportError:
        return None
    with labels_path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _compute_agreement_rate(judge_results: list[dict], editor_labels: Optional[dict]) -> Optional[float]:
    """
    Compute judge-vs-editor agreement rate for stories and voice dimensions.

    Agreement = exact match on pass/borderline/fail verdict.
    Returns None when no editor labels are available.
    """
    if editor_labels is None:
        return None

    agreed = 0
    compared = 0

    # Compare voice dimensions
    editor_dims = editor_labels.get("voice_dimensions", {})
    for dim_name, dim_data in editor_dims.items():
        if not isinstance(dim_data, dict):
            continue
        editor_label = dim_data.get("label")
        if editor_label in ("not_applicable", None):
            continue
        # Find matching judge result in judge_results
        for jr in judge_results:
            if jr.get("dimension") == "voice" and "per_dimension" in jr:
                pd = jr["per_dimension"]
                judge_label = pd.get(dim_name, {}).get("score") if isinstance(pd, dict) else None
                if judge_label and judge_label != "error":
                    compared += 1
                    if judge_label == editor_label:
                        agreed += 1
                break

    # Compare story-level verdicts
    editor_stories = editor_labels.get("stories", [])
    for story_label in editor_stories:
        story_id = story_label.get("story_id")
        if not story_id:
            continue
        # headline
        hl_overall = None
        hl = story_label.get("headline_quality", {})
        if isinstance(hl, dict):
            # Derive overall: if any sub-test is fail -> fail; if any borderline -> borderline; else pass
            sub_vals = [v for k, v in hl.items() if k in ("consequence_led", "length", "no_forbidden_elements")]
            if "fail" in sub_vals:
                hl_overall = "fail"
            elif "borderline" in sub_vals:
                hl_overall = "borderline"
            elif all(v == "pass" for v in sub_vals if v):
                hl_overall = "pass"

        for jr in judge_results:
            if jr.get("story_id") == story_id and jr.get("dimension") == "headline_quality":
                judge_hl = jr.get("score")
                if hl_overall and judge_hl and judge_hl != "error":
                    compared += 1
                    if judge_hl == hl_overall:
                        agreed += 1
                break

        # summary overall
        sq = story_label.get("summary_quality", {})
        if isinstance(sq, dict):
            sq_sub = [v for k, v in sq.items() if k in ("word_count", "concrete_specific", "trust_flag", "decision_tied_close")]
            if "fail" in sq_sub:
                sq_overall = "fail"
            elif "borderline" in sq_sub:
                sq_overall = "borderline"
            elif all(v == "pass" for v in sq_sub if v):
                sq_overall = "pass"
            else:
                sq_overall = None

            for jr in judge_results:
                if jr.get("story_id") == story_id and jr.get("dimension") == "summary_quality":
                    judge_sq = jr.get("score")
                    if sq_overall and judge_sq and judge_sq != "error":
                        compared += 1
                        if judge_sq == sq_overall:
                            agreed += 1
                    break

        # signal
        sig = story_label.get("signal_appropriateness", {})
        if isinstance(sig, dict):
            editor_sig = sig.get("label")
            for jr in judge_results:
                if jr.get("story_id") == story_id and jr.get("dimension") == "signal_appropriateness":
                    judge_sig = jr.get("score")
                    if editor_sig and judge_sig and judge_sig != "error":
                        compared += 1
                        if judge_sig == editor_sig:
                            agreed += 1
                    break

    return (agreed / compared) if compared > 0 else None


def eval_voice_adherence(
    dataset_dir: Optional[Path],
    labels: dict,
) -> EvalResult:
    """
    Phase C. LLM-as-judge quality scoring for voice dimensions, headline,
    summary, signal pill, and section intros.

    Requires:
      - evals/voice/rubric.yaml (Phase A output)
      - dataset_dir/issue.json
      - anthropic SDK + ANTHROPIC_API_KEY (or LLM_API_KEY)
      - EVAL_JUDGE_MODEL env var (default: claude-opus-4-7)

    PASS threshold: aggregate fail-rate < VOICE_FAIL_THRESHOLD (25% v0).

    Agreement with Editor labels in evals/voice/YYYY-MM-DD.labels.yaml is
    computed as a sanity metric but does NOT affect pass/fail.
    """
    if dataset_dir is None or not dataset_dir.exists():
        return EvalResult(
            name="voice_adherence",
            passed=True,
            metric=None,
            status="skipped",
            details={"message": f"Dataset directory not found: {dataset_dir}"},
        )

    # Determine date string from directory name (e.g. "2026-05-23")
    dataset_name = dataset_dir.name

    # Load issue.json
    issue_path = dataset_dir / "issue.json"
    issue = _load_json(issue_path)
    if issue is None:
        return EvalResult(
            name="voice_adherence",
            passed=True,
            metric=None,
            status="skipped",
            details={"message": f"issue.json not found in {dataset_dir}"},
        )

    # Import judge module (lazy -- only when judge eval runs)
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from evals.judge.judge import judge_artifact, _load_rubric, cache_entry_count
    except ImportError as exc:
        return EvalResult(
            name="voice_adherence",
            passed=False,
            metric=None,
            status="fail",
            error=f"Failed to import evals.judge.judge: {exc}",
        )

    # Load rubric once -- passed to all judge calls
    try:
        rubric = _load_rubric()
    except (FileNotFoundError, RuntimeError) as exc:
        return EvalResult(
            name="voice_adherence",
            passed=True,
            metric=None,
            status="skipped",
            details={"message": f"Rubric unavailable: {exc}"},
        )

    # Load editor labels for agreement rate (optional)
    editor_labels = _load_voice_labels(dataset_name)

    # -------------------------------------------------------------------------
    # Judge calls: collect all results with metadata tags for aggregation.
    # -------------------------------------------------------------------------
    all_judge_results: list[dict] = []
    cache_before = cache_entry_count()

    # (1) Full issue -- voice dimensions
    # Build a compact representation for the voice judge. The full issue JSON
    # can exceed 1500 tokens; we distill to: section intros + all stories with
    # their headline, summary (truncated to 80 chars for context), and signal.
    # This is sufficient for the 5 voice dimensions (warmth, signal_density,
    # direction, finance_lens_presence, callback_quality).
    def _compact_story(s: dict) -> dict:
        return {
            "story_id": s.get("story_id"),
            "headline": s.get("headline", ""),
            "summary": s.get("summary", "")[:300],  # enough for voice assessment
            "signal": s.get("signal"),
        }

    compact_sections = []
    pulse_obj = issue.get("pulse", {})
    compact_pulse = {
        "name": "pulse",
        "stories": [_compact_story(s) for s in pulse_obj.get("stories", [])],
    }
    for sec in issue.get("sections", []):
        compact_sections.append({
            "name": sec.get("name"),
            "intro_lead": sec.get("intro_lead"),
            "intro_body": sec.get("intro_body"),
            "stories": [_compact_story(s) for s in sec.get("stories", [])],
        })
    issue_artifact = {
        "date": issue.get("date"),
        "issue_number": issue.get("issue_number"),
        "pulse": compact_pulse,
        "sections": compact_sections,
    }
    voice_result = judge_artifact(
        "voice",
        "full issue (voice dimensions)",
        issue_artifact,
        rubric=rubric,
    )
    voice_result["artifact_type"] = "full_issue"
    all_judge_results.append(voice_result)

    # (2) Per-story: headline, summary, signal
    # Collect stories from pulse + all sections
    all_stories: list[tuple[str, dict]] = []  # (section_name, story_dict)
    pulse = issue.get("pulse", {})
    for story in pulse.get("stories", []):
        all_stories.append(("pulse", story))
    for section in issue.get("sections", []):
        section_name = section.get("name", "unknown")
        for story in section.get("stories", []):
            all_stories.append((section_name, story))

    for section_name, story in all_stories:
        story_id = story.get("story_id", "unknown")
        # Headline artifact: headline text + story_id for reference
        hl_artifact = {
            "story_id": story_id,
            "section": section_name,
            "headline": story.get("headline", ""),
        }
        hl_result = judge_artifact(
            "headline_quality",
            "story headline",
            hl_artifact,
            rubric=rubric,
        )
        hl_result["story_id"] = story_id
        hl_result["section"] = section_name
        hl_result["artifact_type"] = "headline"
        all_judge_results.append(hl_result)

        # Summary artifact: headline + summary + source_urls (for trust-flag assessment)
        summary_artifact = {
            "story_id": story_id,
            "section": section_name,
            "headline": story.get("headline", ""),
            "summary": story.get("summary", ""),
            "source_urls": story.get("source_urls", []),
        }
        summary_result = judge_artifact(
            "summary_quality",
            "story summary",
            summary_artifact,
            rubric=rubric,
        )
        summary_result["story_id"] = story_id
        summary_result["section"] = section_name
        summary_result["artifact_type"] = "summary"
        all_judge_results.append(summary_result)

        # Signal artifact: headline + summary + signal pill
        signal_artifact = {
            "story_id": story_id,
            "section": section_name,
            "headline": story.get("headline", ""),
            "summary": story.get("summary", ""),
            "signal": story.get("signal", ""),
        }
        signal_result = judge_artifact(
            "signal_appropriateness",
            "story headline+summary+signal",
            signal_artifact,
            rubric=rubric,
        )
        signal_result["story_id"] = story_id
        signal_result["section"] = section_name
        signal_result["artifact_type"] = "signal"
        all_judge_results.append(signal_result)

    # (3) Per-section intros (non-pulse sections only)
    for section in issue.get("sections", []):
        section_name = section.get("name", "unknown")
        intro_artifact = {
            "section": section_name,
            "intro_lead": section.get("intro_lead"),
            "intro_body": section.get("intro_body"),
        }
        intro_result = judge_artifact(
            "section_intro_quality",
            "section intro",
            intro_artifact,
            rubric=rubric,
        )
        intro_result["section"] = section_name
        intro_result["artifact_type"] = "intro"
        all_judge_results.append(intro_result)

    cache_after = cache_entry_count()
    new_cache_entries = cache_after - cache_before
    llm_call_count = new_cache_entries  # Each new cache entry == one LLM call

    # -------------------------------------------------------------------------
    # Aggregate: per-dimension pass/borderline/fail rates
    # -------------------------------------------------------------------------
    dim_groups: dict[str, list[str]] = {}
    for jr in all_judge_results:
        dim = jr.get("dimension", "unknown")
        score = jr.get("score", "error")
        if dim not in dim_groups:
            dim_groups[dim] = []
        dim_groups[dim].append(score)

    # Compute rates per dimension
    per_dimension_rates: dict[str, dict] = {}
    for dim, scores in dim_groups.items():
        n = len(scores)
        pass_n = scores.count("pass")
        borderline_n = scores.count("borderline")
        fail_n = scores.count("fail")
        error_n = scores.count("error")
        not_applicable_n = scores.count("not_applicable")
        effective_n = n - error_n - not_applicable_n
        per_dimension_rates[dim] = {
            "n": n,
            "pass": pass_n,
            "borderline": borderline_n,
            "fail": fail_n,
            "error": error_n,
            "not_applicable": not_applicable_n,
            "pass_rate": round(pass_n / effective_n, 4) if effective_n > 0 else None,
            "fail_rate": round(fail_n / effective_n, 4) if effective_n > 0 else None,
        }

    # Overall aggregate fail rate (across all dimensions, excluding errors + N/A)
    all_scores = [jr.get("score", "error") for jr in all_judge_results]
    effective_total = sum(1 for s in all_scores if s not in ("error", "not_applicable"))
    total_fails = all_scores.count("fail")
    aggregate_fail_rate = (total_fails / effective_total) if effective_total > 0 else 0.0

    # Overall pass rate (primary metric -- complement of fail rate)
    total_passes = all_scores.count("pass")
    aggregate_pass_rate = (total_passes / effective_total) if effective_total > 0 else 0.0

    # -------------------------------------------------------------------------
    # Agreement rate with Editor labels
    # -------------------------------------------------------------------------
    agreement_rate = _compute_agreement_rate(all_judge_results, editor_labels)

    # -------------------------------------------------------------------------
    # PASS/FAIL determination
    # -------------------------------------------------------------------------
    failures: list[str] = []
    if aggregate_fail_rate >= VOICE_FAIL_THRESHOLD:
        failures.append(
            f"Aggregate fail-rate {aggregate_fail_rate:.1%} >= threshold "
            f"{VOICE_FAIL_THRESHOLD:.1%} ({total_fails}/{effective_total} verdicts failed)"
        )

    # Per-dimension thresholds (same threshold applied per dimension)
    for dim, rates in per_dimension_rates.items():
        fr = rates.get("fail_rate")
        if fr is not None and fr >= VOICE_FAIL_THRESHOLD:
            failures.append(
                f"Dimension '{dim}' fail-rate {fr:.1%} >= threshold {VOICE_FAIL_THRESHOLD:.1%}"
            )

    passed = len(failures) == 0

    return EvalResult(
        name="voice_adherence",
        passed=passed,
        metric=round(aggregate_pass_rate, 4),
        status="pass" if passed else "fail",
        details={
            "dataset": dataset_name,
            "judge_model": os.getenv("EVAL_JUDGE_MODEL", "claude-opus-4-7"),
            "total_artifacts_judged": len(all_judge_results),
            "llm_calls_this_run": llm_call_count,
            "cache_entries_before": cache_before,
            "cache_entries_after": cache_after,
            "aggregate_pass_rate": round(aggregate_pass_rate, 4),
            "aggregate_fail_rate": round(aggregate_fail_rate, 4),
            "fail_threshold": VOICE_FAIL_THRESHOLD,
            "per_dimension": per_dimension_rates,
            "agreement_with_editor_labels": (
                round(agreement_rate, 4) if agreement_rate is not None else None
            ),
            "editor_labels_available": editor_labels is not None,
            "failures": failures,
        },
    )


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
#
# The Phase B pipeline-health assertions are extracted into the importable
# ``check_integrity()`` function so that ``release_promote`` can call them
# as a publish gate (task #79).
# ---------------------------------------------------------------------------


def check_integrity(
    issue_date: date,
    *,
    staging: bool,
) -> tuple[list[str], bool]:
    """Run integrity assertions against the date's archive (staging or released).

    Importable entry point for use by ``release_promote`` and other callers
    that need to gate on pipeline health without going through the full eval
    harness.

    Resolves the dataset directory as:
      - ``staging=True``  -> ``data/staging/<YYYY-MM-DD>/``
      - ``staging=False`` -> ``data/released/<YYYY-MM-DD>/`` (canonical)

    Runs ALL Phase B assertions:
      1. Schema + referential integrity (items, clusters, ranked, issue.json).
      2. Source fire rate >= 0.80 (from source_health.json).
      3. pulse.stories | length >= 1.
      4. sum(hands_on section stories) >= 3.
      5. No cluster with score >= 35 tiered as "cut" in ranked.jsonl,
         excluding ``novelty == "none"`` (legitimate prior-coverage cuts).

    Returns:
        ``(failures, all_passed)`` where ``failures`` is a list of
        human-readable assertion-failure strings; empty when
        ``all_passed=True``.

    Usage::

        from evals.run_evals import check_integrity
        import datetime
        failures, ok = check_integrity(datetime.date(2026, 5, 25), staging=True)
    """
    date_str = issue_date.isoformat()
    if staging:
        dataset_dir = DATA_DIR / "staging" / date_str
    else:
        dataset_dir = DATA_DIR / "released" / date_str

    failures: list[str] = []

    if not dataset_dir.exists():
        failures.append(
            f"Dataset directory not found: {dataset_dir}"
        )
        return failures, False

    # ------------------------------------------------------------------
    # (A) JSON parse
    # ------------------------------------------------------------------
    raw_items: list[dict] = []
    raw_clusters: list[dict] = []
    raw_ranked: list[dict] = []
    raw_issue: Optional[dict] = None

    items_fp = dataset_dir / "items.jsonl"
    clusters_fp = dataset_dir / "clusters.jsonl"
    ranked_fp = dataset_dir / "ranked.jsonl"
    issue_fp = dataset_dir / "issue.json"

    for path, container, label in [
        (items_fp, raw_items, "items.jsonl"),
        (clusters_fp, raw_clusters, "clusters.jsonl"),
        (ranked_fp, raw_ranked, "ranked.jsonl"),
    ]:
        try:
            container.extend(_load_jsonl(path))
        except ValueError as exc:
            failures.append(f"JSON parse error in {label}: {exc}")

    try:
        raw_issue = _load_json(issue_fp)
    except json.JSONDecodeError as exc:
        failures.append(f"JSON parse error in issue.json: {exc}")

    # ------------------------------------------------------------------
    # (B) Pydantic shape validation
    # ------------------------------------------------------------------
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from src.models import Cluster, Issue, Item, RankedStory  # noqa: F401
        models_available = True
    except ImportError:
        models_available = False
        failures.append(
            "WARNING: src/models.py not importable — pydantic shape checks skipped; "
            "running JSON-parse + referential checks only."
        )

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

    # ------------------------------------------------------------------
    # (C) Referential integrity
    # ------------------------------------------------------------------
    item_ids = {r.get("id") for r in raw_items if r.get("id")}
    cluster_ids = {r.get("cluster_id") for r in raw_clusters if r.get("cluster_id")}
    ranked_cluster_ids = {r.get("cluster_id") for r in raw_ranked if r.get("cluster_id")}

    for record in raw_clusters:
        for iid in record.get("item_ids", []):
            if iid not in item_ids:
                failures.append(
                    f"Referential error: cluster {record.get('cluster_id')} "
                    f"references item_id={iid} not in items.jsonl"
                )

    for missing in ranked_cluster_ids - cluster_ids:
        failures.append(
            f"Referential error: ranked.jsonl references cluster_id={missing} "
            f"not in clusters.jsonl"
        )

    if raw_issue:
        issue_story_ids: set[str] = set()
        pulse_blk = raw_issue.get("pulse", {})
        for block in (pulse_blk.get("stories", []) if isinstance(pulse_blk, dict) else []):
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

    # ------------------------------------------------------------------
    # (D) Phase B pipeline-health assertions
    # ------------------------------------------------------------------

    # (D1) Source fire rate: sources_fired / sources_enabled >= 0.8
    source_health_raw = _load_json(dataset_dir / "source_health.json")
    if source_health_raw is not None:
        sources = source_health_raw.get("sources", [])
        sources_enabled = len(sources)
        sources_fired = sum(1 for s in sources if s.get("fired") is True)
        fire_rate = (sources_fired / sources_enabled) if sources_enabled > 0 else 0.0
        SOURCE_FIRE_THRESHOLD = 0.80
        if fire_rate < SOURCE_FIRE_THRESHOLD:
            failures.append(
                f"PIPELINE HEALTH: source fire rate {fire_rate:.4f} < {SOURCE_FIRE_THRESHOLD} "
                f"({sources_fired}/{sources_enabled} sources fired)"
            )

    # (D2) Tier mix in issue.json: >= 1 pulse story, >= 3 hands_on stories
    if raw_issue is not None:
        pulse_block = raw_issue.get("pulse", {})
        pulse_stories = pulse_block.get("stories", []) if isinstance(pulse_block, dict) else []
        pulse_count = len(pulse_stories)

        hands_on_count = 0
        for section in raw_issue.get("sections", []):
            if section.get("name") == "hands_on":
                hands_on_count = len(section.get("stories", []))
                break

        if pulse_count < 1:
            failures.append(
                "PIPELINE HEALTH: issue.json has 0 pulse stories (minimum 1 required)"
            )
        if hands_on_count < 3:
            failures.append(
                f"PIPELINE HEALTH: issue.json has {hands_on_count} hands_on "
                f"{'story' if hands_on_count == 1 else 'stories'} "
                f"(minimum 3 required)"
            )

    # (D3) No cluster with score >= cut_floor was tagged 'cut' in ranked.jsonl
    # (routing inconsistency guard).
    #
    # CUT_SCORE_CEILING == cut_floor (40): matches the floor exactly, no margin.
    # A cluster at or above this threshold should have been promoted, not cut.
    #
    # Schema-version split:
    #   schema_version >= 6: per-section weights drive routing. The ceiling
    #     applies to max(score_by_section[big_picture, hands_on, currents]) >= 40.
    #     The aggregate `score` field (global RUBRIC_WEIGHTS) is populated for
    #     backwards compat but no longer drives tier assignment, so comparing it
    #     to the cut_floor would produce false positives (May 29 incident).
    #   schema_version <= 5: aggregate `score` field drives tier. Fall back to
    #     the original check.
    #
    # novelty="none" cuts are a legitimate prior-coverage dedup outcome
    # (rank.py caps significance at 25 to trip the cut), so they are excluded.
    CUT_SCORE_CEILING = 40
    high_score_cuts: list[dict] = []
    for record in raw_ranked:
        tier = record.get("tier")
        novelty = record.get("novelty")
        cid = record.get("cluster_id", "<unknown>")
        if tier != "cut" or novelty == "none":
            continue

        schema_version = record.get("schema_version", 1)
        if schema_version >= 6:
            score_by_section = record.get("score_by_section")
            if score_by_section is None:
                failures.append(
                    f"WARNING: ranked cluster {cid} is schema_version={schema_version} "
                    f"but missing score_by_section — skipping D3 check for this cluster"
                )
                continue
            effective_score = max(
                score_by_section.get("big_picture", 0),
                score_by_section.get("hands_on", 0),
                score_by_section.get("currents", 0),
            )
            if effective_score >= CUT_SCORE_CEILING:
                high_score_cuts.append(
                    {"cluster_id": cid, "max_section_score": effective_score}
                )
        else:
            score = record.get("score")
            if score is not None and score >= CUT_SCORE_CEILING:
                high_score_cuts.append({"cluster_id": cid, "score": score})

    if high_score_cuts:
        def _fmt(r: dict) -> str:
            if "max_section_score" in r:
                return f"{r['cluster_id']}(max_section_score={r['max_section_score']})"
            return f"{r['cluster_id']}(score={r['score']})"

        failures.append(
            f"PIPELINE HEALTH: {len(high_score_cuts)} cluster(s) within reach of "
            f"promote threshold (>= {CUT_SCORE_CEILING}) were tiered 'cut' — "
            f"rank.py inconsistency: "
            + ", ".join(_fmt(r) for r in high_score_cuts)
        )

    # Warnings don't count as hard failures for the pass/fail gate
    hard_failures = [f for f in failures if not f.startswith("WARNING")]
    all_passed = len(hard_failures) == 0
    return failures, all_passed


def eval_module_integrity(
    dataset_dir: Optional[Path],
) -> EvalResult:
    """
    READY. Schema-validates all artifacts in dataset_dir and cross-checks
    referential integrity. Fails on any schema violation or broken reference.

    This is a thin wrapper around ``check_integrity()`` which holds the
    actual assertion logic and is directly importable by ``release_promote``
    (task #79).
    """
    if dataset_dir is None or not dataset_dir.exists():
        return EvalResult(
            name="module_integrity",
            passed=True,
            metric=None,
            status="skipped",
            details={"message": f"Dataset directory not found: {dataset_dir}"},
        )

    # Determine date and staging flag from path.  The convention is:
    #   data/released/<date>/  -> staging=False
    #   data/staging/<date>/   -> staging=True
    #   evals/fixtures/...     -> neither; fall back to the date-less path
    # When called from run_evals() the dataset_dir is already resolved by
    # _resolve_dataset_dir(); we infer staging from the path components so
    # eval_module_integrity() doesn't need its own staging param (the
    # callers pass the pre-resolved Path).
    path_parts = dataset_dir.parts
    is_staging_path = "staging" in path_parts

    # Try to derive a date from the directory name; fall back to today.
    try:
        import datetime as _dt_mod
        dir_date = _dt_mod.date.fromisoformat(dataset_dir.name)
    except ValueError:
        # Fixture dataset names are not ISO dates; run check_integrity's
        # logic directly via the legacy inline path below.
        dir_date = None

    if dir_date is not None:
        # Fast path: delegate entirely to check_integrity().
        failures, all_passed = check_integrity(dir_date, staging=is_staging_path)

        # Count artifacts for the metric field (best-effort; no error on
        # missing files since check_integrity already caught those).
        artifact_count = sum(
            1 for name in ("items.jsonl", "clusters.jsonl", "ranked.jsonl", "issue.json")
            if (dataset_dir / name).exists()
        )

        # Separate schema/referential from health failures for the details
        # dict (preserves the existing report shape).
        health_failures = [f for f in failures if f.startswith("PIPELINE HEALTH")]
        schema_failures = [f for f in failures if not f.startswith("PIPELINE HEALTH")]

        def _count_jsonl_lines(p: Path) -> int:
            if not p.exists():
                return 0
            with p.open("r", encoding="utf-8") as _fh:
                return sum(1 for line in _fh if line.strip())

        raw_items_count = _count_jsonl_lines(dataset_dir / "items.jsonl")
        raw_clusters_count = _count_jsonl_lines(dataset_dir / "clusters.jsonl")
        raw_ranked_count = _count_jsonl_lines(dataset_dir / "ranked.jsonl")

        return EvalResult(
            name="module_integrity",
            passed=all_passed,
            metric=float(artifact_count),
            status="pass" if all_passed else "fail",
            details={
                "artifact_count": artifact_count,
                "items_count": raw_items_count,
                "clusters_count": raw_clusters_count,
                "ranked_count": raw_ranked_count,
                "issue_present": (dataset_dir / "issue.json").exists(),
                "staging": is_staging_path,
                "failures": schema_failures,
                "pipeline_health_failures": health_failures,
                "all_failures": failures,
            },
        )

    # Fallback: fixture datasets (non-ISO dir names) — run the legacy
    # inline logic so fixture tests continue to work without a real date.
    failures_inline: list[str] = []
    artifact_count = 0

    try:
        sys.path.insert(0, str(REPO_ROOT))
        from src.models import Item, Cluster, RankedStory, Issue  # noqa: F401
        models_available = True
    except ImportError:
        models_available = False
        failures_inline.append(
            "WARNING: src/models.py not importable — pydantic shape checks skipped; "
            "running JSON-parse + referential checks only."
        )

    raw_items: list[dict] = []
    raw_clusters: list[dict] = []
    raw_ranked: list[dict] = []
    raw_issue: Optional[dict] = None

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
            failures_inline.append(f"JSON parse error in {label}: {exc}")

    try:
        raw_issue = _load_json(issue_path)
        if raw_issue is not None:
            artifact_count += 1
    except json.JSONDecodeError as exc:
        failures_inline.append(f"JSON parse error in issue.json: {exc}")

    if models_available:
        from pydantic import ValidationError

        for record in raw_items:
            try:
                Item(**record)
            except (ValidationError, TypeError) as exc:
                item_id = record.get("id", "<unknown>")
                failures_inline.append(f"Item schema error (id={item_id}): {exc}")

        for record in raw_clusters:
            try:
                Cluster(**record)
            except (ValidationError, TypeError) as exc:
                cid = record.get("cluster_id", "<unknown>")
                failures_inline.append(f"Cluster schema error (cluster_id={cid}): {exc}")

        for record in raw_ranked:
            try:
                RankedStory(**record)
            except (ValidationError, TypeError) as exc:
                cid = record.get("cluster_id", "<unknown>")
                failures_inline.append(f"RankedStory schema error (cluster_id={cid}): {exc}")

        if raw_issue is not None:
            try:
                Issue(**raw_issue)
            except (ValidationError, TypeError) as exc:
                failures_inline.append(f"Issue schema error: {exc}")

    item_ids = {r.get("id") for r in raw_items if r.get("id")}
    cluster_ids = {r.get("cluster_id") for r in raw_clusters if r.get("cluster_id")}
    ranked_cluster_ids = {r.get("cluster_id") for r in raw_ranked if r.get("cluster_id")}

    for record in raw_clusters:
        for iid in record.get("item_ids", []):
            if iid not in item_ids:
                failures_inline.append(
                    f"Referential error: cluster {record.get('cluster_id')} "
                    f"references item_id={iid} not in items.jsonl"
                )

    for missing in ranked_cluster_ids - cluster_ids:
        failures_inline.append(
            f"Referential error: ranked.jsonl references cluster_id={missing} "
            f"not in clusters.jsonl"
        )

    if raw_issue:
        issue_story_ids_inline: set[str] = set()
        pulse = raw_issue.get("pulse", {})
        for block in pulse.get("stories", []):
            issue_story_ids_inline.add(block.get("story_id", ""))
        for section in raw_issue.get("sections", []):
            for block in section.get("stories", []):
                issue_story_ids_inline.add(block.get("story_id", ""))
        issue_story_ids_inline.discard("")
        for sid in issue_story_ids_inline:
            if sid not in ranked_cluster_ids:
                failures_inline.append(
                    f"Referential error: issue.json story_id={sid} not in ranked.jsonl"
                )

    health_failures_inline: list[str] = []
    health_details: dict[str, Any] = {}

    source_health_path = dataset_dir / "source_health.json"
    source_health_raw = _load_json(source_health_path)
    if source_health_raw is not None:
        sources = source_health_raw.get("sources", [])
        sources_enabled = len(sources)
        sources_fired = sum(1 for s in sources if s.get("fired") is True)
        fire_rate = (sources_fired / sources_enabled) if sources_enabled > 0 else 0.0
        SOURCE_FIRE_THRESHOLD = 0.80
        health_details["source_fire_rate"] = round(fire_rate, 4)
        health_details["sources_enabled"] = sources_enabled
        health_details["sources_fired"] = sources_fired
        if fire_rate < SOURCE_FIRE_THRESHOLD:
            health_failures_inline.append(
                f"PIPELINE HEALTH: source fire rate {fire_rate:.4f} < {SOURCE_FIRE_THRESHOLD} "
                f"({sources_fired}/{sources_enabled} sources fired)"
            )
        unfired = [
            s.get("source", "<unknown>") for s in sources if not s.get("fired")
        ]
        if unfired:
            health_details["unfired_sources"] = unfired
    else:
        health_details["source_health_missing"] = True

    if raw_issue is not None:
        pulse_block = raw_issue.get("pulse", {})
        pulse_stories = pulse_block.get("stories", []) if isinstance(pulse_block, dict) else []
        pulse_count = len(pulse_stories)

        hands_on_count = 0
        for section in raw_issue.get("sections", []):
            if section.get("name") == "hands_on":
                hands_on_count = len(section.get("stories", []))
                break

        health_details["issue_pulse_count"] = pulse_count
        health_details["issue_hands_on_count"] = hands_on_count

        if pulse_count < 1:
            health_failures_inline.append(
                "PIPELINE HEALTH: issue.json has 0 pulse stories (minimum 1 required)"
            )
        if hands_on_count < 3:
            health_failures_inline.append(
                f"PIPELINE HEALTH: issue.json has {hands_on_count} hands_on stories "
                f"(minimum 3 required)"
            )
    else:
        health_details["issue_tier_mix_skipped"] = "issue.json not present"

    CUT_SCORE_CEILING = 35
    high_score_cuts: list[dict] = []
    for record in raw_ranked:
        score = record.get("score")
        tier = record.get("tier")
        novelty = record.get("novelty")
        cid = record.get("cluster_id", "<unknown>")
        if (
            tier == "cut"
            and novelty != "none"
            and score is not None
            and score >= CUT_SCORE_CEILING
        ):
            high_score_cuts.append({"cluster_id": cid, "score": score})

    health_details["high_score_cuts"] = high_score_cuts
    if high_score_cuts:
        health_failures_inline.append(
            f"PIPELINE HEALTH: {len(high_score_cuts)} cluster(s) with score >= {CUT_SCORE_CEILING} "
            f"were tiered 'cut' — rank.py inconsistency: "
            + ", ".join(f"{r['cluster_id']}(score={r['score']})" for r in high_score_cuts)
        )

    hard_failures_inline = [f for f in failures_inline if not f.startswith("WARNING")]
    all_hard_failures_inline = hard_failures_inline + health_failures_inline
    passed = len(all_hard_failures_inline) == 0

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
            "failures": failures_inline,
            "pipeline_health": health_details,
            "pipeline_health_failures": health_failures_inline,
        },
    )


# ---------------------------------------------------------------------------
# Eval 5 — Drift detection
# STATUS: READY (degraded mode until 7-issue baseline is available)
#
# Compares today's issue feature vector against the rolling 14-day baseline
# of released issues. Raises drift flags (never blocks) on:
#   - |z| > 2 on any scalar metric (story_count, avg_summary_length,
#     finance_tag_rate)
#   - Jensen-Shannon divergence > 0.20 on distribution metrics
#     (signal_pill_distribution, audience_tag_distribution)
#
# Minimum 7 released issues in the 14-day window are required to compute
# z-scores meaningfully; fewer returns PASS with status "insufficient_baseline".
#
# Snapshots are written to evals/drift/baselines/<date>.json after each run.
# ---------------------------------------------------------------------------

# Minimum number of baseline issues before z-scores are computed.
_DRIFT_MIN_BASELINE = 7

# Rolling window (days) for the baseline.
_DRIFT_WINDOW_DAYS = 14

# Z-score threshold for scalar drift flag.
_DRIFT_Z_THRESHOLD = 2.0

# Z-score denominator floor (prevents division-by-near-zero on stable metrics).
_DRIFT_STDEV_FLOOR = 0.5

# Jensen-Shannon divergence threshold for distribution drift flag.
_DRIFT_JS_THRESHOLD = 0.20

# Known signal pill values (spec-mandated).
_SIGNAL_PILLS = ("act", "try", "read", "watch", "discuss", None)

# Known audience tag values (spec-mandated).
_AUDIENCE_TAGS = ("hands_on", "big_picture", "finance", "general")

DRIFT_BASELINES_DIR = EVALS_DIR / "drift" / "baselines"


def _extract_feature_vector(
    issue_data: dict,
    ranked_by_id: dict[str, dict],
) -> dict[str, Any]:
    """Extract a scalar + distribution feature vector from a released issue.

    Args:
        issue_data: Parsed issue.json content.
        ranked_by_id: Dict mapping cluster_id -> ranked.jsonl record for
            the same issue date (used for audience_tags and finance detection).

    Returns a dict with keys:
        story_count, avg_summary_length, finance_tag_rate,
        signal_pill_distribution (dict of signal -> fraction),
        audience_tag_distribution (dict of tag -> fraction).
    """
    # Collect all stories (pulse + sections)
    stories: list[dict] = []
    pulse_block = issue_data.get("pulse", {})
    if isinstance(pulse_block, dict):
        stories.extend(pulse_block.get("stories", []))
    for section in issue_data.get("sections", []):
        stories.extend(section.get("stories", []))

    story_count = len(stories)

    # avg_summary_length — mean character count of SummaryBlock.summary
    summaries = [s.get("summary", "") for s in stories]
    avg_summary_length = (
        sum(len(s) for s in summaries) / len(summaries)
        if summaries else 0.0
    )

    # finance_tag_rate — fraction of stories whose RankedStory has "finance"
    # in audience_tags; matched by story_id == cluster_id.
    finance_count = 0
    for story in stories:
        sid = story.get("story_id", "")
        ranked_record = ranked_by_id.get(sid)
        if ranked_record and "finance" in ranked_record.get("audience_tags", []):
            finance_count += 1
    finance_tag_rate = (finance_count / story_count) if story_count > 0 else 0.0

    # signal_pill_distribution — fraction per signal value (including null)
    signal_counts: dict[Any, int] = {pill: 0 for pill in _SIGNAL_PILLS}
    for story in stories:
        sig = story.get("signal")  # may be absent → None
        # normalise: if value not in known pills, treat as None
        if sig not in signal_counts:
            sig = None
        signal_counts[sig] += 1
    signal_pill_distribution = {
        (str(k) if k is not None else "null"): (v / story_count)
        for k, v in signal_counts.items()
    }

    # audience_tag_distribution — fraction of stories that carry each tag.
    # A story may carry multiple tags; each tag counted independently
    # (fractions can exceed 1.0 when stories carry multiple tags).
    tag_counts: dict[str, int] = {tag: 0 for tag in _AUDIENCE_TAGS}
    for story in stories:
        sid = story.get("story_id", "")
        ranked_record = ranked_by_id.get(sid)
        if ranked_record:
            for tag in ranked_record.get("audience_tags", []):
                if tag in tag_counts:
                    tag_counts[tag] += 1
    audience_tag_distribution = {
        tag: (count / story_count)
        for tag, count in tag_counts.items()
    }

    # verifier_flag_rate — fraction of stories that have a factual flag in the
    # per-issue verifier sidecar (data/<date>/verify_sidecar.json), if present.
    # Populated by the verify stage once it lands; defaults to None until then.
    # Bidirectional interpretation: a rate spiking HIGH suggests the verifier is
    # overclaiming (or the pipeline is hallucinating); a rate dropping to ZERO
    # suggests the verifier stopped running or is trivially passing everything.
    verifier_sidecar_path = None
    # Try to locate the sidecar alongside issue.json (released archive only).
    # We can't resolve the path here without knowing whether we're in staging
    # or released mode, so we probe the most likely released path from issue_data.
    _issue_date = issue_data.get("date")
    if _issue_date:
        _candidate = DATA_DIR / "released" / _issue_date / "verify_sidecar.json"
        if _candidate.exists():
            verifier_sidecar_path = _candidate

    verifier_flag_rate: Optional[float] = None
    if verifier_sidecar_path is not None:
        try:
            sidecar = _load_json(verifier_sidecar_path) or {}
            flagged = sum(
                1 for entry in sidecar.get("stories", [])
                if entry.get("has_flag", False)
            )
            total_in_sidecar = len(sidecar.get("stories", []))
            if total_in_sidecar > 0:
                verifier_flag_rate = flagged / total_in_sidecar
        except Exception:  # noqa: BLE001
            pass  # sidecar malformed; leave None rather than crashing drift

    return {
        "story_count": story_count,
        "avg_summary_length": avg_summary_length,
        "finance_tag_rate": finance_tag_rate,
        "signal_pill_distribution": signal_pill_distribution,
        "audience_tag_distribution": audience_tag_distribution,
        "verifier_flag_rate": verifier_flag_rate,  # None until verify stage lands
    }


def _js_divergence(p: dict[str, float], q: dict[str, float]) -> float:
    """Jensen-Shannon divergence between two categorical distributions.

    Both p and q must share the same key set; values are treated as
    unnormalised (re-normalised internally so that missing keys = 0 mass).
    Returns a value in [0, 1] (JS is bounded by ln2 ~ 0.693; we use
    log base 2 so the bound is 1.0).

    Returns 0.0 if both distributions are uniform zero (degenerate input).
    """
    keys = set(p) | set(q)
    p_vals = [p.get(k, 0.0) for k in keys]
    q_vals = [q.get(k, 0.0) for k in keys]

    p_sum = sum(p_vals)
    q_sum = sum(q_vals)

    # Degenerate: if either is all-zero, return 0 (undefined divergence;
    # this can happen when story_count is 0).
    if p_sum == 0 or q_sum == 0:
        return 0.0

    p_norm = [x / p_sum for x in p_vals]
    q_norm = [x / q_sum for x in q_vals]
    m = [(pp + qq) / 2 for pp, qq in zip(p_norm, q_norm)]

    def _kl(a: list[float], b: list[float]) -> float:
        total = 0.0
        for ai, bi in zip(a, b):
            if ai > 0 and bi > 0:
                total += ai * math.log2(ai / bi)
        return total

    return (_kl(p_norm, m) + _kl(q_norm, m)) / 2.0


def _load_ranked_by_id(date_dir: Path) -> dict[str, dict]:
    """Load ranked.jsonl from a released date directory as {cluster_id: record}."""
    ranked_path = date_dir / "ranked.jsonl"
    records = _load_jsonl(ranked_path)
    return {r["cluster_id"]: r for r in records if r.get("cluster_id")}


def _write_drift_snapshot(
    candidate_date: str,
    feature_vector: dict[str, Any],
    baseline_size: int,
    z_scores: dict[str, float],
    js_divergences: dict[str, float],
    flags: list[str],
) -> None:
    """Write a drift snapshot to evals/drift/baselines/<date>.json atomically."""
    DRIFT_BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "date": candidate_date,
        "feature_vector": feature_vector,
        "baseline_size": baseline_size,
        "z_scores": z_scores,
        "js_divergences": js_divergences,
        "flags": flags,
    }
    target = DRIFT_BASELINES_DIR / f"{candidate_date}.json"
    tmp = target.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2)
    tmp.rename(target)


def check_drift(
    candidate_date: date,
    *,
    released_root: Optional[Path] = None,
) -> EvalResult:
    """Run drift detection for *candidate_date* against the rolling baseline.

    This is the importable entry point (analogous to ``check_integrity``).
    ``eval_drift_detection`` is a thin wrapper that delegates here.

    Args:
        candidate_date: The date of the issue to evaluate.
        released_root: Override for the released archive root (used in tests
            to point at a tmp directory). Defaults to DATA_DIR / "released".

    Returns:
        An EvalResult with passed=True always (drift is informational).
        status is one of:
          "insufficient_baseline" — fewer than 7 issues in the 14-day window.
          "pass"                  — z-scores and JS divergences computed;
                                   flags (if any) surfaced in details.
    """
    released_root = released_root or (DATA_DIR / "released")

    # ------------------------------------------------------------------
    # 1. Enumerate released issues within the 14-day window.
    # ------------------------------------------------------------------
    # Use paths.all_released_dates() if released_root matches DATA_DIR,
    # else enumerate manually (test override path).
    window_start = candidate_date - __import__("datetime").timedelta(days=_DRIFT_WINDOW_DAYS)

    all_released: list[date] = []
    if released_root.exists():
        for child in sorted(released_root.iterdir()):
            if not child.is_dir():
                continue
            try:
                d = date.fromisoformat(child.name)
            except ValueError:
                continue
            if (child / "issue.json").exists():
                all_released.append(d)

    # Split: candidate (today) vs baseline (trailing issues before today,
    # within the 14-day window).
    baseline_dates = [
        d for d in all_released
        if window_start <= d < candidate_date
    ]

    # Count total issues in window including the candidate (for the
    # "insufficient_baseline" message).
    window_issues = [d for d in all_released if window_start <= d <= candidate_date]
    window_count = len(window_issues)

    if len(baseline_dates) < _DRIFT_MIN_BASELINE:
        # Degraded mode — not enough history.
        return EvalResult(
            name="drift_detection",
            passed=True,
            metric=None,
            status="insufficient_baseline",
            details={
                "message": (
                    f"need at least {_DRIFT_MIN_BASELINE} released issues in the "
                    f"{_DRIFT_WINDOW_DAYS}-day window; have {window_count}"
                ),
                "window_count": window_count,
                "baseline_min": _DRIFT_MIN_BASELINE,
                "window_days": _DRIFT_WINDOW_DAYS,
            },
        )

    # ------------------------------------------------------------------
    # 2. Load candidate feature vector.
    # ------------------------------------------------------------------
    candidate_dir = released_root / candidate_date.isoformat()
    candidate_issue = _load_json(candidate_dir / "issue.json")
    if candidate_issue is None:
        return EvalResult(
            name="drift_detection",
            passed=True,
            metric=None,
            status="skipped",
            details={"message": f"Candidate issue.json not found: {candidate_dir}"},
        )

    candidate_ranked = _load_ranked_by_id(candidate_dir)
    candidate_fv = _extract_feature_vector(candidate_issue, candidate_ranked)

    # ------------------------------------------------------------------
    # 3. Load baseline feature vectors and compute baseline statistics.
    # ------------------------------------------------------------------
    baseline_fvs: list[dict[str, Any]] = []
    for bd in baseline_dates:
        bd_dir = released_root / bd.isoformat()
        bd_issue = _load_json(bd_dir / "issue.json")
        if bd_issue is None:
            continue
        bd_ranked = _load_ranked_by_id(bd_dir)
        baseline_fvs.append(_extract_feature_vector(bd_issue, bd_ranked))

    if len(baseline_fvs) < _DRIFT_MIN_BASELINE:
        return EvalResult(
            name="drift_detection",
            passed=True,
            metric=None,
            status="insufficient_baseline",
            details={
                "message": (
                    f"need at least {_DRIFT_MIN_BASELINE} released issues in the "
                    f"{_DRIFT_WINDOW_DAYS}-day window; have {window_count}"
                ),
                "window_count": window_count,
                "baseline_min": _DRIFT_MIN_BASELINE,
                "window_days": _DRIFT_WINDOW_DAYS,
            },
        )

    # ------------------------------------------------------------------
    # 4. Compute z-scores for scalar metrics.
    #
    # verifier_flag_rate is included as a scalar metric once the verify
    # stage lands and populates the sidecar. Until then, most/all values
    # in the baseline will be None and the metric is skipped gracefully.
    # When present: a high z-score means the verifier is flagging
    # unusually often (possible hallucination spike or prompt regression);
    # a low (negative) z-score means unusually few flags (verifier may
    # have stopped running or is trivially passing everything).
    # ------------------------------------------------------------------
    _SCALAR_METRICS_CORE = ("story_count", "avg_summary_length", "finance_tag_rate")
    z_scores: dict[str, float] = {}

    for metric in _SCALAR_METRICS_CORE:
        values = [fv[metric] for fv in baseline_fvs]
        n = len(values)
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n
        stdev = math.sqrt(variance)
        effective_stdev = max(stdev, _DRIFT_STDEV_FLOOR)
        today_val = candidate_fv[metric]
        z = (today_val - mean) / effective_stdev
        z_scores[metric] = round(z, 4)

    # verifier_flag_rate: only include in z-scores when enough non-None
    # baseline values exist (at least half the baseline must have data).
    _vfr_baseline = [
        fv.get("verifier_flag_rate")
        for fv in baseline_fvs
        if fv.get("verifier_flag_rate") is not None
    ]
    _vfr_candidate = candidate_fv.get("verifier_flag_rate")
    if len(_vfr_baseline) >= max(1, len(baseline_fvs) // 2) and _vfr_candidate is not None:
        n = len(_vfr_baseline)
        mean = sum(_vfr_baseline) / n
        variance = sum((v - mean) ** 2 for v in _vfr_baseline) / n
        stdev = math.sqrt(variance)
        effective_stdev = max(stdev, _DRIFT_STDEV_FLOOR)
        z = (_vfr_candidate - mean) / effective_stdev
        z_scores["verifier_flag_rate"] = round(z, 4)

    # ------------------------------------------------------------------
    # 5. Compute Jensen-Shannon divergences for distribution metrics.
    # ------------------------------------------------------------------
    _DIST_METRICS = ("signal_pill_distribution", "audience_tag_distribution")
    js_divergences: dict[str, float] = {}

    for dist_metric in _DIST_METRICS:
        # Baseline mean distribution: average each key's fraction across baseline.
        all_keys: set[str] = set()
        for fv in baseline_fvs:
            all_keys.update(fv[dist_metric].keys())
        all_keys.update(candidate_fv[dist_metric].keys())

        mean_dist: dict[str, float] = {}
        for k in all_keys:
            mean_dist[k] = sum(fv[dist_metric].get(k, 0.0) for fv in baseline_fvs) / len(baseline_fvs)

        js = _js_divergence(candidate_fv[dist_metric], mean_dist)
        js_divergences[dist_metric] = round(js, 4)

    # ------------------------------------------------------------------
    # 6. Build flags (informational only — never blocks).
    # ------------------------------------------------------------------
    flags: list[str] = []
    for metric, z in z_scores.items():
        if abs(z) > _DRIFT_Z_THRESHOLD:
            flags.append(
                f"drift_high:{metric} z={z:+.2f} "
                f"(threshold |z|>{_DRIFT_Z_THRESHOLD})"
            )
    for dist_metric, js in js_divergences.items():
        if js > _DRIFT_JS_THRESHOLD:
            flags.append(
                f"distribution_shift:{dist_metric} JS={js:.3f} "
                f"(threshold>{_DRIFT_JS_THRESHOLD})"
            )

    # ------------------------------------------------------------------
    # 7. Write snapshot.
    # ------------------------------------------------------------------
    try:
        _write_drift_snapshot(
            candidate_date=candidate_date.isoformat(),
            feature_vector=candidate_fv,
            baseline_size=len(baseline_fvs),
            z_scores=z_scores,
            js_divergences=js_divergences,
            flags=flags,
        )
    except OSError:
        # Snapshot write failure is non-fatal — log in details.
        flags.append("snapshot_write_failed")

    return EvalResult(
        name="drift_detection",
        passed=True,  # drift is never a blocker
        metric=None,
        status="pass",
        details={
            "candidate_date": candidate_date.isoformat(),
            "baseline_size": len(baseline_fvs),
            "baseline_dates": [d.isoformat() for d in baseline_dates],
            "candidate_feature_vector": candidate_fv,
            "z_scores": z_scores,
            "js_divergences": js_divergences,
            "flags": flags,
            "flag_count": len(flags),
            "thresholds": {
                "z_score": _DRIFT_Z_THRESHOLD,
                "stdev_floor": _DRIFT_STDEV_FLOOR,
                "js_divergence": _DRIFT_JS_THRESHOLD,
                "min_baseline": _DRIFT_MIN_BASELINE,
                "window_days": _DRIFT_WINDOW_DAYS,
            },
        },
    )


def eval_drift_detection(
    dataset_dir: Optional[Path],
    labels: dict,
) -> EvalResult:
    """Drift detection: compares today's issue against the rolling 14-day baseline.

    Reads the released archive only. Degraded mode (PASS, status
    "insufficient_baseline") when fewer than 7 released issues exist in the
    14-day window. Drift flags are informational — never blocks CI.
    """
    if dataset_dir is None:
        return EvalResult(
            name="drift_detection",
            passed=True,
            metric=None,
            status="skipped",
            details={"message": "No dataset directory provided."},
        )

    # Derive the candidate date from the directory name (must be YYYY-MM-DD).
    try:
        import datetime as _dt_mod
        candidate_date = _dt_mod.date.fromisoformat(dataset_dir.name)
    except ValueError:
        return EvalResult(
            name="drift_detection",
            passed=True,
            metric=None,
            status="skipped",
            details={
                "message": (
                    f"Cannot derive a date from dataset directory name "
                    f"{dataset_dir.name!r}; drift detection requires YYYY-MM-DD."
                )
            },
        )

    return check_drift(candidate_date)


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
# Eval 7 — Factual accuracy verifier calibration
# STATUS: READY (fixture-only; verifier callable is a seam — plugged in later)
#
# This eval defines the calibration gate for the future ``verify`` stage.
# It does NOT run the verifier itself — the verifier prompt does not yet exist.
# When the verifier lands, the LLM Engineer wires it by implementing the
# VerifierCallable protocol (see below) and passing it to eval_factual_accuracy().
#
# Until the verifier is wired, the eval runs in SKIP mode:
#   status="verifier_not_wired"  passed=True  metric=None
# This is NOT a stub (the fixtures and labels are real and complete); it is
# a seam that stays green in CI until the verifier lands.
#
# The verifier must check BOTH the headline (title) AND the summary body.
# A factual error in the headline is the most severe kind — it is what readers
# see and trust first, and AI Vector headlines carry named actors (the
# recognition rule). Each returned claim dict must include a `location` field
# indicating where the claim was drawn from: "headline" or "body".
#
# The seam — the function the LLM Engineer implements:
# ---------------------------------------------------------------------------
#
#   def verify(
#       headline: str,
#       body: str,
#       source_excerpt: str,
#   ) -> list[dict]:
#       """Run the factual-accuracy verifier on one (headline, body, source) triple.
#
#       Args:
#           headline:       The published headline (title) string.
#           body:           The published summary body string.
#           source_excerpt: The ~1000-word trafilatura excerpt the summary was
#                           derived from.
#
#       Returns:
#           A list of per-claim verdict dicts. Each dict must have:
#               {
#                   "claim":    str,   # the atomic claim text
#                   "verdict":  str,   # one of: supported | unsupported |
#                                      #         contradicted | unverifiable
#                   "location": str,   # one of: "headline" | "body"
#                                      # where the claim was drawn from
#               }
#           The list must be in the same order as the claims in the fixture case,
#           or claim-text matching is used (see _match_claims() below).
#           The `location` field is used for per-location recall reporting.
#           Returning an empty list causes all claims to score as errors.
#       """
#       ...
#
# The LLM Engineer passes a function with this exact signature as the
# ``verifier`` argument to eval_factual_accuracy().
#
# HARD GATE thresholds (block merges to verifier prompt when violated):
#   recall on contradicted claims      >= 0.85   (reliable classes only:
#                                                 numeric_substitution,
#                                                 entity_substitution,
#                                                 directional_inversion,
#                                                 headline_error)
#   precision on supported claims      >= 0.80
#   unverifiable accuracy              >= 0.80
#
# dropped_trust_flag is EXCLUDED from the hard-gate recall and is reported
# as the diagnostic metric dropped_trust_flag_recall_advisory. It is
# intentionally visible in the report output but does NOT gate. Rationale:
# catching a dropped epistemic caveat is inherently debatable — the de-hedged
# claim is factually true; only the epistemic framing was removed. The verifier
# reliably catches the four clear-cut mutation classes at ~100% recall.
# Shipping the advisory-mode verifier with dropped_trust_flag as a diagnostic
# is the responsible path: visible, honest, not hidden, not a blocker.
# Accepted by Arman: 2026-06-29. See FM-14 in evals/failure_modes.md.
#
# Per-location recall (headline vs body) is reported for diagnosis but is
# NOT a separate hard gate. A verifier that catches body errors but misses
# headline errors will be visible in the per-location breakdown.
# Per-mutation-type recall is also reported (informational, not a hard gate).
# ---------------------------------------------------------------------------

# Factual accuracy fixture path and gate thresholds.
# Thresholds are defined here as constants so they can be compared against the
# values in labels.yaml (mismatch = misconfiguration, surfaced as a warning).
_FA_FIXTURE_PATH = EVALS_DIR / "fixtures" / "factual-accuracy" / "cases.yaml"
_FA_RECALL_CONTRADICTED_THRESHOLD = 0.85
_FA_PRECISION_SUPPORTED_THRESHOLD = 0.80
_FA_UNVERIFIABLE_ACCURACY_THRESHOLD = 0.80

# Mutation types included in the hard-gate recall_contradicted metric.
# dropped_trust_flag is EXCLUDED from the hard gate (see FM-14 in
# evals/failure_modes.md and the advisory note in cases.yaml).
# Rationale: detecting a dropped epistemic caveat is an inherently debatable
# judgment — the de-hedged claim is factually true; only the epistemic framing
# was removed. The verifier reliably catches clear-cut errors (numeric swap,
# entity swap, directional inversion, headline error) at ~100% recall. Including
# dropped_trust_flag in the gate would pin the aggregate recall at 0.73 even
# when reliable detection is at 1.0, preventing a demonstrably shippable tool
# from passing calibration. Decision: track dropped_trust_flag as a visible
# diagnostic metric (dropped_trust_flag_recall_advisory), never hide it, and
# ship the gate with recall over reliable classes only.
# Accepted by Arman: 2026-06-29.
_FA_RELIABLE_MUTATION_TYPES = frozenset({
    "numeric_substitution",
    "entity_substitution",
    "directional_inversion",
    "headline_error",
})


# ---------------------------------------------------------------------------
# Protocol definition: what the verifier callable must implement.
# The LLM Engineer imports this and implements a function with this signature.
# Stored here (not in src/) to keep the seam in the eval layer, where this
# eval engineer maintains it.
# ---------------------------------------------------------------------------
class VerifierCallable(Protocol):
    """Type protocol for the factual-accuracy verifier function.

    The LLM Engineer implements a function matching this protocol and passes
    it as the ``verifier`` argument to eval_factual_accuracy(). Once wired,
    the harness will call it for each (headline, body, source_excerpt) triple
    in the fixture set.

    The verifier must check claims drawn from BOTH the headline and the body.
    A factual error in the headline is the most severe kind — readers see and
    trust the headline first, and AI Vector headlines carry named actors.

    Each returned claim dict must include a ``location`` field:
        "headline" — claim drawn from the headline (title)
        "body"     — claim drawn from the summary body

    The function must be pure (no side effects, no global state) so the harness
    can call it multiple times without interference. Caching for cost reduction
    is the verifier's own concern; the harness does not cache at this layer.
    """
    def __call__(
        self,
        headline: str,
        body: str,
        source_excerpt: str,
    ) -> list[dict]:
        """Return per-claim verdicts for the (headline, body, source) triple.

        Each dict in the returned list must have:
            {
                "claim":    str,   # atomic claim text
                "verdict":  str,   # supported | unsupported | contradicted | unverifiable
                "location": str,   # "headline" | "body"
            }
        """
        ...


def _load_fa_fixtures() -> list[dict]:
    """Load the factual-accuracy fixture cases from cases.yaml.

    Returns a list of case dicts as parsed from YAML. Returns [] if the file
    does not exist or cannot be parsed; the eval function handles the empty
    case gracefully.
    """
    try:
        import yaml
    except ImportError:
        return []
    if not _FA_FIXTURE_PATH.exists():
        return []
    with _FA_FIXTURE_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("cases", [])


def _match_claims(
    fixture_claims: list[dict],
    verifier_output: list[dict],
) -> list[tuple[dict, Optional[dict]]]:
    """Match fixture claims to verifier-output verdicts using token-overlap.

    Matching strategy: for each ground-truth fixture claim, find the predicted
    claim at the same location (headline / body) with the highest Jaccard
    token-overlap. If no same-location candidate exists, fall back to the
    full pool. A match is accepted when best Jaccard > 0. Unmatched fixture
    claims (no overlap > 0 anywhere) get verdict=None (treated as errors).

    This sidesteps the brittle prefix approach that failed when the verifier
    paraphrases the atomic claim text differently from the fixture wording.
    The location filter prevents cross-location mismatches (a body claim must
    not be credited to a headline prediction and vice versa).

    Reference: _scratch/fa_tuning/score.py::best_match / toks.
    """
    import re as _re

    _STOP = frozenset(
        "a an the of to in on for and or with by is are was were be been "
        "that this its their it they as at from than more less fewer "
        "shows show test tests results result internal".split()
    )

    def _toks(s: str) -> frozenset:
        return frozenset(
            w for w in _re.findall(r"[a-z0-9.]+", (s or "").lower())
            if w not in _STOP and len(w) > 1
        )

    def _best_match(gt_claim: str, gt_loc: str, preds: list[dict]) -> Optional[dict]:
        """Return the predicted claim dict with highest Jaccard overlap.

        Prefers same-location candidates; falls back to the full pool when
        no same-location candidate exists. Returns None when nothing overlaps
        (Jaccard == 0 for all candidates).
        """
        gt = _toks(gt_claim)
        same_loc = [c for c in preds if (c.get("location") or "") == gt_loc]
        pool = same_loc if same_loc else preds
        best: Optional[dict] = None
        best_score = 0.0
        for c in pool:
            pt = _toks(c.get("claim", ""))
            union = len(gt | pt)
            if union == 0:
                score = 0.0
            else:
                score = len(gt & pt) / union
            if score > best_score:
                best, best_score = c, score
        return best if best_score > 0.0 else None

    if not verifier_output:
        return [(fc, None) for fc in fixture_claims]

    matched = []
    for fc in fixture_claims:
        gt_claim_text = fc.get("claim", "") or ""
        gt_loc = (fc.get("location", "") or "").strip().lower()
        vd = _best_match(gt_claim_text, gt_loc, verifier_output)
        matched.append((fc, vd))
    return matched


def eval_factual_accuracy(
    verifier: Optional[Callable] = None,
) -> EvalResult:
    """Eval 7. Factual-accuracy verifier calibration against labelled fixtures.

    Loads the fixture set from evals/fixtures/factual-accuracy/cases.yaml and
    runs the provided ``verifier`` callable against each (headline, body,
    source_excerpt) triple. Computes:
        - recall on contradicted claims (PRIMARY HARD GATE >= 0.85, over
          RELIABLE MUTATION CLASSES ONLY: numeric_substitution,
          entity_substitution, directional_inversion, headline_error)
        - precision on supported claims (primary hard gate >= 0.80)
        - unverifiable accuracy (secondary hard gate >= 0.80)
        - dropped_trust_flag_recall_advisory (DIAGNOSTIC ONLY, not a gate):
          recall over dropped_trust_flag cases, tracked and printed but does
          not affect pass/fail. See FM-14 in evals/failure_modes.md.
        - per-location recall on contradicted claims (headline vs body)
          — informational, not a hard gate; surfaces a verifier that catches
          body errors but misses headline errors
        - per-mutation-type recall (informational, all mutation types)

    The verifier must check claims from BOTH the headline and the summary body.
    A factual error in the headline is the most severe kind. Each returned claim
    dict must include a ``location`` field: "headline" or "body".

    SEAM BEHAVIOUR:
        When ``verifier`` is None (the verifier has not been wired yet), returns
        status="verifier_not_wired" with passed=True. CI remains green.
        This is intentional: the fixtures define the goal; the verifier is built
        to pass them.

    HARD GATE:
        When ``verifier`` is provided, a regression on recall_contradicted or
        precision_supported blocks merges to the verifier prompt (via CI non-zero
        exit). The unverifiable_accuracy gate also blocks.

    The ``verifier`` callable must match the VerifierCallable protocol:
        def verifier(headline: str, body: str, source_excerpt: str) -> list[dict]:
            # Returns: [{"claim": str, "verdict": str, "location": str}, ...]
            # verdict values:  supported | unsupported | contradicted | unverifiable
            # location values: "headline" | "body"

    Args:
        verifier: The factual-accuracy verifier function to evaluate.
                  Pass None (or omit) when the verifier does not yet exist.

    Returns:
        EvalResult with:
            name="factual_accuracy"
            metric=recall_contradicted (primary metric; None when no verifier)
            status="verifier_not_wired" | "pass" | "fail" | "error"
    """
    # ------------------------------------------------------------------
    # 1. Seam: skip gracefully when no verifier is wired.
    # ------------------------------------------------------------------
    if verifier is None:
        return EvalResult(
            name="factual_accuracy",
            passed=True,
            metric=None,
            status="verifier_not_wired",
            details={
                "message": (
                    "Verifier callable not wired. "
                    "Implement the VerifierCallable protocol and pass it as "
                    "verifier=... to eval_factual_accuracy(). "
                    "The verifier must accept (headline, body, source_excerpt) "
                    "and return per-claim dicts with 'claim', 'verdict', and "
                    "'location' fields. "
                    "Fixture set is loaded and counts are reported below."
                ),
                "fixture_path": str(_FA_FIXTURE_PATH),
                "fixture_exists": _FA_FIXTURE_PATH.exists(),
                "thresholds": {
                    "recall_contradicted": _FA_RECALL_CONTRADICTED_THRESHOLD,
                    "recall_contradicted_scope": "reliable_classes_only (excl. dropped_trust_flag)",
                    "precision_supported": _FA_PRECISION_SUPPORTED_THRESHOLD,
                    "unverifiable_accuracy": _FA_UNVERIFIABLE_ACCURACY_THRESHOLD,
                    "dropped_trust_flag_recall_advisory": "informational_only",
                },
                **_fa_fixture_summary(),
            },
        )

    # ------------------------------------------------------------------
    # 2. Load fixtures.
    # ------------------------------------------------------------------
    cases = _load_fa_fixtures()
    if not cases:
        return EvalResult(
            name="factual_accuracy",
            passed=True,
            metric=None,
            status="verifier_not_wired",
            details={
                "message": (
                    f"Fixture file not found or empty: {_FA_FIXTURE_PATH}. "
                    "Skipping eval."
                )
            },
        )

    # ------------------------------------------------------------------
    # 3. Run verifier on each case and collect verdicts.
    # ------------------------------------------------------------------
    # Tracking structures
    # For precision (supported): count how many supported-category claims
    # the verifier did NOT flag (i.e., returned "supported" or "unverifiable"
    # but NOT "contradicted" or "unsupported").
    # For recall (contradicted): count how many contradicted-category claims
    # the verifier flagged as "contradicted".
    # For unverifiable accuracy: count how many unverifiable-category claims
    # the verifier returned "unverifiable".

    supported_claims_total = 0
    supported_claims_not_flagged = 0   # verifier said "supported" (TP for precision)
    supported_claims_flagged = 0       # verifier said "contradicted"/"unsupported" (FP)

    # Reliable classes (numeric, entity, directional, headline) — hard-gate recall.
    # Excludes dropped_trust_flag, which is tracked separately as a diagnostic.
    contradicted_reliable_total = 0
    contradicted_reliable_caught = 0   # TP for hard-gate recall
    contradicted_reliable_missed = 0   # FN for hard-gate recall

    # dropped_trust_flag — advisory diagnostic only, NOT in the hard gate.
    # Tracked and printed so the metric is visible and honest; not hidden.
    dtf_claims_total = 0
    dtf_claims_caught = 0

    unverifiable_claims_total = 0
    unverifiable_claims_correct = 0    # verifier said "unverifiable"
    unverifiable_claims_wrong = 0

    # Per-mutation-type recall tracking
    mutation_type_recall: dict[str, dict] = {}

    # Per-location recall tracking for contradicted claims.
    # Surfaces a verifier that catches body errors but misses headline errors.
    location_recall: dict[str, dict] = {
        "headline": {"total": 0, "caught": 0},
        "body":     {"total": 0, "caught": 0},
    }

    failures: list[str] = []
    case_results: list[dict] = []

    for case in cases:
        case_id = case.get("id", "<unknown>")
        category = case.get("category", "unknown")
        mutation_type = case.get("mutation_type")
        headline_text = case.get("headline", "")
        summary_text = case.get("summary_text", "")
        source_excerpt = case.get("source_excerpt", "")
        fixture_claims = case.get("claims", [])

        # Call verifier with the updated three-argument signature.
        # Gracefully handle verifiers that still use the old two-argument
        # signature (headline + body combined, no location) by falling back
        # to passing body only. This allows a transitional period while the
        # LLM Engineer updates the verifier prompt.
        try:
            import inspect as _inspect
            _sig = _inspect.signature(verifier)
            _params = list(_sig.parameters)
            if len(_params) >= 3:
                # New signature: (headline, body, source_excerpt)
                verifier_output = verifier(headline_text, summary_text, source_excerpt)
            else:
                # Old signature: (summary_text, source_excerpt) — transitional compat
                verifier_output = verifier(summary_text, source_excerpt)
        except Exception as exc:  # noqa: BLE001
            verifier_output = []
            failures.append(
                f"Case {case_id}: verifier raised {type(exc).__name__}: {exc}"
            )

        # Match claims to verifier output
        matched = _match_claims(fixture_claims, verifier_output)

        case_claim_results: list[dict] = []
        for fixture_claim, verifier_verdict in matched:
            gt = fixture_claim.get("ground_truth_verdict", "unknown")
            gt_location = fixture_claim.get("location", "body")  # default: body
            vv = (verifier_verdict or {}).get("verdict", "error") if verifier_verdict else "error"
            # Location reported by the verifier (if present); fall back to
            # the fixture's ground-truth location for accounting purposes.
            vv_location = (
                (verifier_verdict or {}).get("location", gt_location)
                if verifier_verdict else gt_location
            )
            match_correct = (vv == gt)

            case_claim_results.append({
                "claim": fixture_claim.get("claim", ""),
                "ground_truth": gt,
                "ground_truth_location": gt_location,
                "verifier_verdict": vv,
                "verifier_location": vv_location,
                "correct": match_correct,
            })

            # "Flagging" verdicts: the verifier considers the claim problematic.
            # Both "contradicted" and "unsupported" are flagging verdicts.
            # This set is used for recall (catching injected errors) and for
            # precision (not incorrectly flagging clean claims).
            # Reference: _scratch/fa_tuning/score.py::FLAG.
            _FLAGGING_VERDICTS = frozenset({"contradicted", "unsupported"})

            if category == "supported":
                if gt == "supported":
                    supported_claims_total += 1
                    if vv not in _FLAGGING_VERDICTS:
                        # Not incorrectly flagged — counts toward precision
                        supported_claims_not_flagged += 1
                    else:
                        supported_claims_flagged += 1

            elif category == "contradicted":
                # Count this claim toward recall if the ground-truth verdict is a
                # flagging verdict (contradicted OR unsupported). In all non-trust-flag
                # mutation types, the injected-error claims have gt="contradicted". In
                # dropped_trust_flag cases, the re-anchored injected-error claims have
                # gt="unsupported" because the verifier correctly marks de-hedged bare
                # factual claims as unsupported rather than contradicted.
                # Non-error claims in contradicted fixtures (gt="supported") are ignored
                # for recall (only the injected error claim is the needle).
                if gt in _FLAGGING_VERDICTS:
                    caught = (vv in _FLAGGING_VERDICTS)

                    if mutation_type == "dropped_trust_flag":
                        # Advisory-only: not in the hard-gate recall.
                        dtf_claims_total += 1
                        if caught:
                            dtf_claims_caught += 1
                    else:
                        # Reliable class: counts toward hard-gate recall.
                        contradicted_reliable_total += 1
                        if caught:
                            contradicted_reliable_caught += 1
                        else:
                            contradicted_reliable_missed += 1

                    # Per-mutation-type tracking (all mutation types, including DTF).
                    if mutation_type:
                        if mutation_type not in mutation_type_recall:
                            mutation_type_recall[mutation_type] = {
                                "total": 0,
                                "caught": 0,
                            }
                        mutation_type_recall[mutation_type]["total"] += 1
                        if caught:
                            mutation_type_recall[mutation_type]["caught"] += 1

                    # Per-location tracking (use fixture's ground-truth location
                    # so we measure whether the verifier catches errors at each
                    # location, regardless of what location the verifier reported).
                    loc_bucket = gt_location if gt_location in location_recall else "body"
                    location_recall[loc_bucket]["total"] += 1
                    if caught:
                        location_recall[loc_bucket]["caught"] += 1

            elif category == "unverifiable":
                if gt == "unverifiable":
                    unverifiable_claims_total += 1
                    if vv == "unverifiable":
                        unverifiable_claims_correct += 1
                    else:
                        unverifiable_claims_wrong += 1

        case_results.append({
            "id": case_id,
            "category": category,
            "mutation_type": mutation_type,
            "claims": case_claim_results,
        })

    # ------------------------------------------------------------------
    # 4. Compute aggregate metrics.
    # ------------------------------------------------------------------

    # Hard-gate recall: reliable mutation classes only (excludes dropped_trust_flag).
    recall_contradicted = (
        contradicted_reliable_caught / contradicted_reliable_total
        if contradicted_reliable_total > 0 else None
    )

    # Advisory diagnostic: dropped_trust_flag recall. Tracked and printed.
    # NOT part of the hard gate. See FM-14 in evals/failure_modes.md.
    dtf_recall_advisory = (
        dtf_claims_caught / dtf_claims_total
        if dtf_claims_total > 0 else None
    )

    precision_supported = (
        supported_claims_not_flagged / supported_claims_total
        if supported_claims_total > 0 else None
    )
    unverifiable_accuracy = (
        unverifiable_claims_correct / unverifiable_claims_total
        if unverifiable_claims_total > 0 else None
    )

    per_mutation_recall = {
        mt: (
            round(counts["caught"] / counts["total"], 4)
            if counts["total"] > 0 else None
        )
        for mt, counts in mutation_type_recall.items()
    }

    # Per-location recall: informational only (not a hard gate).
    # A verifier that catches body errors but misses headline errors is visible here.
    per_location_recall: dict[str, Any] = {}
    for loc, counts in location_recall.items():
        total = counts["total"]
        caught = counts["caught"]
        per_location_recall[loc] = {
            "total": total,
            "caught": caught,
            "recall": round(caught / total, 4) if total > 0 else None,
        }

    # ------------------------------------------------------------------
    # 5. Apply hard gate thresholds.
    # ------------------------------------------------------------------
    # recall_contradicted gates only over _FA_RELIABLE_MUTATION_TYPES.
    # dropped_trust_flag is deliberately excluded from the gate — it is
    # inherently debatable (the de-hedged claim is factually true; the
    # missing epistemic caveat is a harder/softer judgment). It is reported
    # as dropped_trust_flag_recall_advisory below. Decision accepted by
    # Arman: 2026-06-29. See FM-14 in evals/failure_modes.md.
    if recall_contradicted is not None and recall_contradicted < _FA_RECALL_CONTRADICTED_THRESHOLD:
        failures.append(
            f"Recall on contradicted claims (reliable classes) {recall_contradicted:.4f} < "
            f"threshold {_FA_RECALL_CONTRADICTED_THRESHOLD} "
            f"({contradicted_reliable_caught}/{contradicted_reliable_total} caught; "
            f"reliable classes: {sorted(_FA_RELIABLE_MUTATION_TYPES)})"
        )
    if precision_supported is not None and precision_supported < _FA_PRECISION_SUPPORTED_THRESHOLD:
        failures.append(
            f"Precision on supported claims {precision_supported:.4f} < "
            f"threshold {_FA_PRECISION_SUPPORTED_THRESHOLD} "
            f"({supported_claims_not_flagged}/{supported_claims_total} not flagged)"
        )
    if unverifiable_accuracy is not None and unverifiable_accuracy < _FA_UNVERIFIABLE_ACCURACY_THRESHOLD:
        failures.append(
            f"Unverifiable accuracy {unverifiable_accuracy:.4f} < "
            f"threshold {_FA_UNVERIFIABLE_ACCURACY_THRESHOLD} "
            f"({unverifiable_claims_correct}/{unverifiable_claims_total} correct)"
        )

    passed = len(failures) == 0

    return EvalResult(
        name="factual_accuracy",
        passed=passed,
        metric=round(recall_contradicted, 4) if recall_contradicted is not None else None,
        status="pass" if passed else "fail",
        details={
            "fixture_path": str(_FA_FIXTURE_PATH),
            "total_cases": len(cases),
            # Hard-gate metrics (block merge on regression).
            "recall_contradicted": (
                round(recall_contradicted, 4) if recall_contradicted is not None else None
            ),
            "recall_contradicted_scope": "reliable_classes_only",
            "recall_contradicted_reliable_classes": sorted(_FA_RELIABLE_MUTATION_TYPES),
            "precision_supported": (
                round(precision_supported, 4) if precision_supported is not None else None
            ),
            "unverifiable_accuracy": (
                round(unverifiable_accuracy, 4) if unverifiable_accuracy is not None else None
            ),
            # Advisory diagnostic: dropped_trust_flag recall.
            # NOT a hard gate. Tracked so it is visible and honest.
            # When it improves, we will notice. When it falls, we will notice.
            # Excluded from the gate because catching a dropped epistemic caveat
            # is inherently debatable — the de-hedged claim is factually true;
            # only the epistemic framing was removed. See FM-14.
            "dropped_trust_flag_recall_advisory": (
                round(dtf_recall_advisory, 4) if dtf_recall_advisory is not None else None
            ),
            "dropped_trust_flag_advisory_raw": {
                "total": dtf_claims_total,
                "caught": dtf_claims_caught,
            },
            # Per-mutation breakdown (all types, informational).
            "per_mutation_type_recall": per_mutation_recall,
            # Per-location recall: informational (not a hard gate).
            # Use this to diagnose a verifier that catches body errors but
            # misses headline errors. Example: headline recall = 0.50 while
            # body recall = 0.92 means the verifier is not reading the headline
            # carefully enough.
            "per_location_recall": per_location_recall,
            "raw_counts": {
                "supported_total": supported_claims_total,
                "supported_not_flagged": supported_claims_not_flagged,
                "supported_flagged": supported_claims_flagged,
                "contradicted_reliable_total": contradicted_reliable_total,
                "contradicted_reliable_caught": contradicted_reliable_caught,
                "contradicted_reliable_missed": contradicted_reliable_missed,
                "dropped_trust_flag_total": dtf_claims_total,
                "dropped_trust_flag_caught": dtf_claims_caught,
                "unverifiable_total": unverifiable_claims_total,
                "unverifiable_correct": unverifiable_claims_correct,
                "unverifiable_wrong": unverifiable_claims_wrong,
            },
            "thresholds": {
                "recall_contradicted": _FA_RECALL_CONTRADICTED_THRESHOLD,
                "recall_contradicted_note": (
                    "Hard gate applies to reliable mutation classes only: "
                    "numeric_substitution, entity_substitution, "
                    "directional_inversion, headline_error. "
                    "dropped_trust_flag is excluded from this gate — "
                    "see dropped_trust_flag_recall_advisory."
                ),
                "precision_supported": _FA_PRECISION_SUPPORTED_THRESHOLD,
                "unverifiable_accuracy": _FA_UNVERIFIABLE_ACCURACY_THRESHOLD,
                "per_location_recall": "informational_only",
                "dropped_trust_flag_recall_advisory": "informational_only",
            },
            "failures": failures,
            "case_results": case_results,
        },
    )


def _fa_fixture_summary() -> dict:
    """Return a summary of fixture case counts per category (used in the seam path)."""
    cases = _load_fa_fixtures()
    counts: dict[str, int] = {}
    for case in cases:
        cat = case.get("category", "unknown")
        counts[cat] = counts.get(cat, 0) + 1
    return {
        "fixture_case_count": len(cases),
        "fixture_case_counts_by_category": counts,
    }


# ---------------------------------------------------------------------------
# Eval 8 — Reading-experience regression lint (R-8 / R-9, deterministic)
# STATUS: READY
#
# Cheap, no-LLM code checks over an issue.json, encoding the deterministic
# halves of the 2026-07-04 rulings in docs/internal/READING_EXPERIENCE.md:
#
#   (a) R-8  — banned absence-form trust flags ("No code is public yet",
#       "no independent replication yet", "peer review pending", …) must
#       not appear in story summaries or section intros. Boundary note in
#       R-8 is honoured: "no action yet" in a direction-note is a
#       recommendation, not an evidence inventory — allowed.
#   (b) R-9  — "A new + generic noun" headline opener is banned
#       (framework / method / tool / benchmark / system).
#   (c) R-9  — density guardrail: at most two "A/An"-led headlines per
#       issue. (The per-story prompt states this as a preference; the
#       deterministic count lives here, per No Token Wasted.)
#
# Enforcement window: the rulings were ratified 2026-07-04. Datasets dated
# BEFORE that run the same counts but report status="informational" and
# never fail — the released archive up to #23 legitimately predates the
# rule and must stay green. Datasets whose name is not a date (synthetic
# fixtures) are also informational-only.
# ---------------------------------------------------------------------------

READING_LINT_EFFECTIVE_DATE = date(2026, 7, 4)
"""First issue date the R-8/R-9 lint gates. Earlier days: informational."""

A_AN_HEADLINE_CAP = 2
"""R-9 density guardrail: max "A/An"-led headlines per issue."""

# R-8 absence-inventory forms. Each pattern is one observed family from the
# audit in READING_EXPERIENCE.md R-8; the catch-all "no X … yet" covers the
# unbounded-set exotics ("No regulatory framework yet", "no stable tag yet").
_ABSENCE_FORM_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("no-X-yet", re.compile(r"\bno\s+(?:\w+[-'’]?\w*\s+){0,4}?yet\b", re.IGNORECASE)),
    ("no-code", re.compile(r"\bno\s+(?:public\s+|open[- ]source\s+)?code\b", re.IGNORECASE)),
    ("no-independent-X", re.compile(r"\bno\s+independent\s+\w+", re.IGNORECASE)),
    ("peer-review-pending", re.compile(r"\bpeer[- ]review\s+(?:is\s+|still\s+)?pending\b", re.IGNORECASE)),
    ("no-benchmarks", re.compile(r"\bno\s+(?:benchmark(?:s|\s+data)?|replication|validation)\b", re.IGNORECASE)),
]

# R-8 boundary note: "no action yet" in a direction-note is a
# recommendation to the reader (a different speech act) — allowed.
_ABSENCE_ALLOWED = re.compile(r"\bno action(?:\s+(?:needed|required))?(?:\s+yet)?\b", re.IGNORECASE)

# R-8 boundary (calibrated 2026-07-05, first live-gated day): a
# negative-existential NEWS claim that the SAME SENTENCE resolves with
# "until now" is a novelty assertion — presence-in-disguise, the source's
# own "first of its kind" claim — not an evidence inventory about the
# story's sourcing. Observed misfire: "No benchmark has tested language
# models on native Word, Excel, and PowerPoint files until now."
# (2026-07-04 staged; verifier verdict: supported, source span "the first
# public benchmark to jointly evaluate..."; rubric v0.2 cites this story's
# calibration beats as trust-flag pass exemplars.) The exemption is
# deliberately narrow: the resolution must appear before the next sentence
# terminator, so "No code is public. Until now..." still counts as R-8.
_NOVELTY_RESOLVED = re.compile(r"\buntil\s+now\b", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?]")

# R-9: "A new + generic noun" opener ban. Exactly the five generics named
# in the ruling — do not widen without a new ratification.
_BANNED_NEW_GENERIC_OPENER = re.compile(
    r"^(?:A|An)\s+new\s+(?:framework|method|tool|benchmark|system)\b",
    re.IGNORECASE,
)

# R-9 density count: headlines whose first word is the indefinite article.
_A_AN_OPENER = re.compile(r"^(?:A|An)\b")


def _find_absence_forms(text: str) -> list[str]:
    """Return deduplicated banned absence-form snippets found in ``text``.

    Overlapping matches from different patterns (e.g. "No code is public
    yet" hits both no-code and no-X-yet) are merged: the longest span
    covering each region is kept, so one written defect counts once.
    """
    if not text:
        return []
    spans: list[tuple[int, int]] = []
    for _label, pattern in _ABSENCE_FORM_PATTERNS:
        for m in pattern.finditer(text):
            if _ABSENCE_ALLOWED.match(text, m.start()):
                continue  # R-8 boundary: "no action yet" is a recommendation
            # R-8 boundary: novelty claim resolved by "until now" in the
            # same sentence is presence-in-disguise, not an inventory.
            terminator = _SENTENCE_END.search(text, m.end())
            sentence_end = terminator.start() if terminator else len(text)
            if _NOVELTY_RESOLVED.search(text, m.start(), sentence_end):
                continue
            spans.append((m.start(), m.end()))
    if not spans:
        return []
    # Merge overlapping spans so each defect is reported once.
    spans.sort()
    merged: list[tuple[int, int]] = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return [text[s:e] for s, e in merged]


def _iter_issue_texts(issue: dict) -> list[tuple[str, str, str]]:
    """Yield ``(location_id, kind, text)`` for every linted text unit.

    kind ∈ {"headline", "summary", "intro"}. Locations are story_ids for
    stories and ``section:<name>`` for intros, so a failure names the
    exact unit to fix.
    """
    units: list[tuple[str, str, str]] = []
    pulse = issue.get("pulse", {}) or {}
    sections = [pulse] + list(issue.get("sections", []) or [])
    for section in sections:
        if not isinstance(section, dict):
            continue
        name = section.get("name", "unknown")
        for story in section.get("stories", []) or []:
            sid = story.get("story_id", "unknown")
            units.append((sid, "headline", story.get("headline") or ""))
            units.append((sid, "summary", story.get("summary") or ""))
        for field_name in ("intro_lead", "intro_body"):
            val = section.get(field_name)
            if val:
                units.append((f"section:{name}", "intro", val))
    return units


def eval_reading_experience_lint(dataset_dir: Optional[Path]) -> EvalResult:
    """Deterministic R-8/R-9 regression lint over a dataset's issue.json.

    FAIL conditions (datasets dated >= READING_LINT_EFFECTIVE_DATE only):
      - any banned absence-form in a story summary or section intro (R-8);
      - any "A new + generic noun" headline opener (R-9);
      - more than A_AN_HEADLINE_CAP "A/An"-led headlines (R-9 density).

    Pre-ruling and non-dated datasets report the same counts with
    status="informational" and always pass.
    """
    if dataset_dir is None or not dataset_dir.exists():
        return EvalResult(
            name="reading_experience_lint",
            passed=True,
            metric=None,
            status="skipped",
            details={"message": f"Dataset directory not found: {dataset_dir}"},
        )

    issue = _load_json(dataset_dir / "issue.json")
    if issue is None:
        return EvalResult(
            name="reading_experience_lint",
            passed=True,
            metric=None,
            status="skipped",
            details={"message": f"issue.json not found in {dataset_dir}"},
        )

    # Enforcement window: gate only dated datasets on/after the ruling.
    dataset_name = dataset_dir.name
    try:
        dataset_date = date.fromisoformat(dataset_name[:10])
        enforced = dataset_date >= READING_LINT_EFFECTIVE_DATE
    except ValueError:
        dataset_date = None
        enforced = False  # synthetic fixtures: informational only

    absence_hits: list[dict] = []
    banned_openers: list[dict] = []
    a_an_headlines: list[str] = []

    for location, kind, text in _iter_issue_texts(issue):
        if kind in ("summary", "intro"):
            for snippet in _find_absence_forms(text):
                absence_hits.append({
                    "location": location,
                    "kind": kind,
                    "matched": snippet,
                })
        if kind == "headline":
            if _BANNED_NEW_GENERIC_OPENER.match(text):
                banned_openers.append({"location": location, "headline": text})
            if _A_AN_OPENER.match(text):
                a_an_headlines.append(text)

    failures: list[str] = []
    if absence_hits:
        failures.append(
            f"R-8: {len(absence_hits)} banned absence-form flag(s) found: "
            + "; ".join(
                f"{h['location']} ({h['kind']}): \"{h['matched']}\""
                for h in absence_hits[:5]
            )
        )
    if banned_openers:
        failures.append(
            f"R-9: {len(banned_openers)} banned \"A new + generic noun\" "
            "opener(s): "
            + "; ".join(f"{b['location']}: \"{b['headline']}\"" for b in banned_openers)
        )
    if len(a_an_headlines) > A_AN_HEADLINE_CAP:
        failures.append(
            f"R-9 density: {len(a_an_headlines)} \"A/An\"-led headlines "
            f"> cap {A_AN_HEADLINE_CAP}: "
            + "; ".join(f"\"{h}\"" for h in a_an_headlines)
        )

    violation_count = (
        len(absence_hits)
        + len(banned_openers)
        + max(0, len(a_an_headlines) - A_AN_HEADLINE_CAP)
    )

    if enforced:
        passed = len(failures) == 0
        status = "pass" if passed else "fail"
    else:
        passed = True
        status = "informational"

    return EvalResult(
        name="reading_experience_lint",
        passed=passed,
        metric=float(violation_count),
        status=status,
        details={
            "dataset": dataset_name,
            "enforced": enforced,
            "effective_date": READING_LINT_EFFECTIVE_DATE.isoformat(),
            "absence_form_hits": absence_hits,
            "banned_new_generic_openers": banned_openers,
            "a_an_headline_count": len(a_an_headlines),
            "a_an_headline_cap": A_AN_HEADLINE_CAP,
            "a_an_headlines": a_an_headlines,
            "failures": failures,
        },
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
            "insufficient_baseline": "[DEGRADED]",
            "verifier_not_wired": "[SEAM]",   # Eval 7: fixture-ready, verifier pending
            "informational": "[INFO]",        # Eval 8: pre-ruling day, counts only
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
    "factual_accuracy",   # Eval 7: verifier calibration (seam: green until verifier wired)
    "reading_experience_lint",  # Eval 8: deterministic R-8/R-9 lint (no LLM)
}


def run_evals(
    *,
    dataset: Optional[str] = None,
    against: str = "fixtures",
    judge_only: bool = False,
    no_judge: bool = False,
    strict: bool = False,
    staging: bool = False,
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
        staging: When True and ``against="real"``, resolve the dataset
            directory to ``data/staging/<date>/`` instead of
            ``data/released/<date>/``.  Dedup precision/recall and Spearman
            are skipped with a ``"skipped: no labels for unreleased date"``
            status because ``evals/labels.yaml`` only covers released dates.
            Integrity and LLM-judge evals run normally.  Drift detection
            reads the released-only baseline (staging never affects history).

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

    # When running against staging, label-dependent evals are skipped
    # because labels.yaml only covers released dates.
    _LABEL_DEPENDENT_EVALS = {"dedup_quality", "ranking_quality"}

    labels = _load_labels()
    all_results: list[EvalResult] = []

    for dataset_name in datasets:
        dataset_dir = _resolve_dataset_dir(dataset_name, against, staging=staging)

        # Map of name -> thunk. We dispatch only those in ``active`` so the
        # judge / no-judge gates trim cost (and, for judge_only, skip
        # deterministic metrics entirely).
        dispatch: dict[str, Any] = {
            "dedup_quality":    lambda: eval_dedup_quality(dataset_dir, labels),
            "ranking_quality":  lambda: eval_ranking_quality(dataset_dir, labels),
            "voice_adherence":  lambda: eval_voice_adherence(dataset_dir, labels),
            "module_integrity": lambda: eval_module_integrity(dataset_dir),
            "drift_detection":  lambda: eval_drift_detection(dataset_dir, labels),
            # Eval 7: factual accuracy verifier calibration.
            # The verifier=None seam means this runs green (status=verifier_not_wired)
            # until the LLM Engineer wires the VerifierCallable. To run with a real
            # verifier, pass it via the VERIFIER_CALLABLE environment variable or
            # by importing and calling eval_factual_accuracy(verifier=my_fn) directly.
            "factual_accuracy": lambda: eval_factual_accuracy(verifier=None),
            # Eval 8: deterministic R-8/R-9 reading-experience lint (no LLM).
            # Gates only datasets dated >= READING_LINT_EFFECTIVE_DATE;
            # earlier days and synthetic fixtures report informational counts.
            "reading_experience_lint": lambda: eval_reading_experience_lint(dataset_dir),
        }

        for name, fn in dispatch.items():
            if name not in active:
                continue

            # Staging mode: skip label-dependent evals with an explicit status.
            if staging and against == "real" and name in _LABEL_DEPENDENT_EVALS:
                all_results.append(EvalResult(
                    name=name,
                    passed=True,
                    metric=None,
                    status="skipped",
                    details={
                        "message": (
                            "skipped: no labels for unreleased date "
                            f"(staging=True, dataset={dataset_name!r})"
                        )
                    },
                ))
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
        # "verifier_not_wired" is excluded from warning_states intentionally:
        # the fixture set is complete and the seam is by design. --strict
        # should not penalise this state (it is not a gap; it's a planned seam).
        warning_states = {"not_yet_implemented", "skipped", "insufficient_baseline"}
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
