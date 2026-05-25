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
import sys
import warnings
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
      5. No cluster with score >= 35 tiered as "cut" in ranked.jsonl.

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

    # (D3) No cluster with score >= 35 tiered 'cut' in ranked.jsonl
    CUT_SCORE_CEILING = 35
    high_score_cuts: list[dict] = []
    for record in raw_ranked:
        score = record.get("score")
        tier = record.get("tier")
        cid = record.get("cluster_id", "<unknown>")
        if tier == "cut" and score is not None and score >= CUT_SCORE_CEILING:
            high_score_cuts.append({"cluster_id": cid, "score": score})

    if high_score_cuts:
        failures.append(
            f"PIPELINE HEALTH: {len(high_score_cuts)} cluster(s) with score >= "
            f"{CUT_SCORE_CEILING} were tiered 'cut' — rank.py inconsistency: "
            + ", ".join(f"{r['cluster_id']}(score={r['score']})" for r in high_score_cuts)
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
        cid = record.get("cluster_id", "<unknown>")
        if tier == "cut" and score is not None and score >= CUT_SCORE_CEILING:
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
