"""
src/cluster.py — Retrieval Engineer's module.

Reads `data/staging/YYYY-MM-DD/items.jsonl`, embeds titles+summaries,
clusters near-duplicates within the day, links clusters to prior-day chains
(reading the **canonical** last-14-day archive), and writes
`data/staging/YYYY-MM-DD/clusters.jsonl` +
`data/staging/YYYY-MM-DD/embeddings/centroids.npz`.

Round B (DESIGN.md "Archive: staging vs canonical"):
  * Today's outputs go to STAGING.
  * Cross-time dedup reads CANONICAL ONLY -- staging is invisible to history,
    so a draft Arman never released cannot influence today's continuations.
  * `data/published_urls.txt` is canonical-only (lives at the data root).

No LLM calls. Embedding model only (BAAI/bge-base-en-v1.5 via sentence-transformers).
No .env loading — embeddings are fully local.

Public entry point (per DESIGN.md module boundary table):
    cluster_day(run_date, data_dir, lookback_days) -> list[Cluster]

Standalone debug entry point (per spec):
    python -m src.cluster
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
from pathlib import Path

import numpy as np
import yaml

from src import paths
from src.models import Cluster, Item

# ---------------------------------------------------------------------------
# Tunable constants — Eval Engineer tunes these against evals/labels.yaml.
# ---------------------------------------------------------------------------

EMBEDDING_MODEL_NAME = "BAAI/bge-base-en-v1.5"  # local HF model, ~440 MB fp32
EMBEDDING_DIM = 768                              # output dimension of bge-base-en-v1.5
WITHIN_DAY_COSINE_THRESHOLD = 0.78              # two items share a cluster when cosine >= this
CROSS_TIME_COSINE_THRESHOLD = 0.82              # higher bar: cross-day similarity to set cross_time_ref
CROSS_TIME_LOOKBACK_DAYS = 14                   # days of history to consult for cross-time dedup
MAX_CHAIN_DEPTH = 30                             # cycle-guard: max hops when resolving chain root
BATCH_SIZE = 32                                  # sentence-transformers encode batch size

# ---------------------------------------------------------------------------
# Module-level logger (structured fields appended by callers via extra=).
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_published_urls() -> set[str]:
    """Load `data/published_urls.txt` (canonical) into a `set[str]`.

    Returns an empty set when the file is missing (first run, fresh checkout).
    DESIGN.md cross-issue dedup contract: this file lives at `data/` root and
    is canonical-only (no staging variant).
    """
    path = paths.PUBLISHED_URLS_PATH
    if not path.exists():
        return set()
    urls: set[str] = set()
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                urls.add(line)
    return urls


def _load_items(
    items_file: Path, published_set: set[str]
) -> tuple[list[Item], int]:
    """Parse items.jsonl; drop items whose URL appears in published_set.

    Returns (items, filtered_count).  Returns ([], 0) with an error log if
    items.jsonl is missing, per DESIGN.md read-contract: consumers must not
    crash on missing files.
    """
    path = items_file
    if not path.exists():
        logger.error(
            "items.jsonl not found",
            extra={"component": "cluster", "path": str(path)},
        )
        return [], 0

    items: list[Item] = []
    filtered = 0
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = Item.model_validate_json(line)
            except Exception as exc:
                logger.warning(
                    "Failed to parse Item line",
                    extra={"component": "cluster", "lineno": lineno, "error": str(exc)},
                )
                continue
            url_str = str(item.url)
            if url_str in published_set:
                filtered += 1
                continue
            items.append(item)

    logger.info(
        "Loaded items",
        extra={
            "component": "cluster",
            "loaded": len(items),
            "filtered_published": filtered,
        },
    )
    return items, filtered


def _load_trust_weights(config_path: Path) -> dict[str, int]:
    """Return {source_name: trust_weight} from sources.yaml.

    Falls back to empty dict if file is missing; canonical_title selection
    will fall back to alphabetic ordering when weights are equal.
    """
    if not config_path.exists():
        logger.warning(
            "sources.yaml not found; trust weights unavailable",
            extra={"component": "cluster", "path": str(config_path)},
        )
        return {}
    with config_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    weights: dict[str, int] = {}
    for src in data.get("sources", []):
        name = src.get("name")
        tw = src.get("trust_weight")
        if name and tw is not None:
            weights[name] = int(tw)
    return weights


def _embed(items: list[Item]) -> np.ndarray:
    """Batch-encode all items using BAAI/bge-base-en-v1.5.

    Returns float32 ndarray of shape (n_items, EMBEDDING_DIM).
    Normalised embeddings are returned so dot-product == cosine similarity.
    Model is loaded once per call; callers are expected to cache the result
    and not call _embed multiple times per invocation.
    """
    from sentence_transformers import SentenceTransformer  # lazy import; heavy

    model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    texts = [
        f"{item.title}. {item.raw_summary or ''}".strip()
        for item in items
    ]
    embeddings: np.ndarray = model.encode(
        texts,
        batch_size=BATCH_SIZE,
        show_progress_bar=False,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )
    return embeddings.astype(np.float32)


def _cluster_within_day(
    items: list[Item],
    embeddings: np.ndarray,
    trust_weights: dict[str, int],
) -> list[Cluster]:
    """Agglomerative clustering with cosine distance threshold.

    sklearn.cluster.AgglomerativeClustering with:
      - metric="cosine"         (1 - cosine_similarity as the pairwise distance)
      - linkage="average"       (average linkage = UPGMA; more stable than single/complete
                                 for finding natural story clusters)
      - distance_threshold = 1 - WITHIN_DAY_COSINE_THRESHOLD
      - n_clusters=None         (threshold-based, not fixed-count)

    DESIGN.md note: distance = 1 - cosine_similarity for L2-normalised vectors.
    With normalize_embeddings=True from sentence-transformers, ||v||=1, so
    dot(v_i, v_j) = cos(v_i, v_j) and distance = 1 - dot = ||v_i - v_j||^2 / 2.
    sklearn's cosine metric computes 1 - cos directly, which is correct here.

    Each sklearn label maps to a list of items; we build Cluster objects from those.
    """
    if not items:
        return []

    if len(items) == 1:
        # sklearn requires >= 2 samples; handle singleton directly.
        return [_build_cluster([items[0]], trust_weights)]

    from sklearn.cluster import AgglomerativeClustering

    distance_threshold = 1.0 - WITHIN_DAY_COSINE_THRESHOLD
    model = AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=distance_threshold,
        metric="cosine",
        linkage="average",
    )
    labels: np.ndarray = model.fit_predict(embeddings)

    # Group items by cluster label.
    label_to_items: dict[int, list[tuple[int, Item]]] = {}
    for idx, label in enumerate(labels.tolist()):
        label_to_items.setdefault(label, []).append((idx, items[idx]))

    clusters: list[Cluster] = []
    for label_items in label_to_items.values():
        cluster_items = [pair[1] for pair in label_items]
        clusters.append(_build_cluster(cluster_items, trust_weights))

    return clusters


def _build_cluster(
    cluster_items: list[Item],
    trust_weights: dict[str, int],
) -> Cluster:
    """Construct a Cluster from a list of Items.

    cluster_id: deterministic SHA-256 of sorted item_ids (prefix 'c_').
    canonical_title: title of the item with the highest trust_weight; ties broken
        alphabetically on title.  Trust weight comes from Item.trust_weight
        (mirrored from sources.yaml at fetch time — DESIGN.md).
    centroid_ref: populated later by cluster_day() after _write_centroids runs.
    """
    item_ids = sorted(item.id for item in cluster_items)
    raw_hash = hashlib.sha256(",".join(item_ids).encode()).hexdigest()
    cluster_id = "c_" + raw_hash[:16]

    # Canonical title: highest trust_weight item; ties broken alphabetically
    # (ascending on title — "Anthropic…" before "Zeta…").
    best_item = sorted(
        cluster_items,
        key=lambda it: (-it.trust_weight, it.title),
    )[0]

    seen_sources: list[str] = []
    seen_sources_set: set[str] = set()
    for it in cluster_items:
        if it.source not in seen_sources_set:
            seen_sources.append(it.source)
            seen_sources_set.add(it.source)

    earliest_published = min(it.published_at for it in cluster_items)

    # Compute centroid for later use (returned via the returned array; stored in
    # parallel with the Cluster list by the caller).
    # centroid_ref is filled in after _write_centroids runs.
    return Cluster(
        cluster_id=cluster_id,
        item_ids=item_ids,
        canonical_title=best_item.title,
        sources=seen_sources,
        earliest_published=earliest_published,
        size=len(item_ids),
        embedding_dim=EMBEDDING_DIM,
        centroid_ref=None,
        cross_time_ref=None,
    )


def _compute_centroids(
    clusters: list[Cluster],
    items: list[Item],
    embeddings: np.ndarray,
) -> dict[str, np.ndarray]:
    """Compute L2-normalised centroid for each cluster.

    Returns {cluster_id: centroid_vector (float32, shape EMBEDDING_DIM)}.
    """
    item_id_to_idx: dict[str, int] = {item.id: idx for idx, item in enumerate(items)}
    centroids: dict[str, np.ndarray] = {}
    for cluster in clusters:
        indices = [item_id_to_idx[iid] for iid in cluster.item_ids if iid in item_id_to_idx]
        if not indices:
            # Defensive: all item ids map to real items, but guard anyway.
            logger.warning(
                "Cluster has no matching item embeddings; skipping centroid",
                extra={"component": "cluster", "cluster_id": cluster.cluster_id},
            )
            continue
        vecs = embeddings[indices]
        centroid = np.mean(vecs, axis=0)
        norm = np.linalg.norm(centroid)
        if norm > 0:
            centroid = centroid / norm
        centroids[cluster.cluster_id] = centroid.astype(np.float32)
    return centroids


def _write_centroids(
    out_path: Path,
    centroids: dict[str, np.ndarray],
) -> Path:
    """Write centroids.npz atomically.

    `out_path` is the full path (e.g. via `paths.centroids_path`); the parent
    `embeddings/` dir is created if missing. Keys are cluster_ids; values are
    768-dim float32 vectors.

    Returns the path to the written file.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")

    # Pass a file handle (not a path) so numpy writes EXACTLY tmp_path --
    # `np.savez(str_path, ...)` would auto-append ".npz" to a path that
    # doesn't already end with it, producing "centroids.npz.tmp.npz".
    with open(tmp_path, "wb") as fh:
        np.savez(fh, **{cid: vec for cid, vec in centroids.items()})
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, out_path)
    return out_path


def _load_prior_centroids(
    today: datetime.date,
    lookback_days: int = CROSS_TIME_LOOKBACK_DAYS,
) -> dict[str, np.ndarray]:
    """Load centroid vectors from the last `lookback_days` of CANONICAL history.

    Returns {cluster_id: centroid_vector}.  Missing day directories are skipped
    gracefully — per DESIGN.md: "Never crash today because yesterday is absent."

    Round B note: this reads CANONICAL only (`data/<date>/embeddings/...`);
    staging is invisible to cross-time dedup.
    """
    all_centroids: dict[str, np.ndarray] = {}
    for delta in range(1, lookback_days + 1):
        prior_date = today - datetime.timedelta(days=delta)
        npz_path = paths.centroids_path(prior_date, canonical=True)
        if not npz_path.exists():
            continue
        try:
            npz = np.load(str(npz_path))
            for key in npz.files:
                all_centroids[key] = npz[key].astype(np.float32)
        except Exception as exc:
            logger.warning(
                "Failed to load prior centroids",
                extra={"component": "cluster", "path": str(npz_path), "error": str(exc)},
            )
    return all_centroids


def _load_prior_clusters(
    today: datetime.date,
    lookback_days: int = CROSS_TIME_LOOKBACK_DAYS,
) -> dict[str, Cluster]:
    """Load Cluster records from the last `lookback_days` of CANONICAL clusters.jsonl.

    Returns {cluster_id: Cluster}.  Missing/unparseable files are skipped.

    Round B note: canonical-only read; staging clusters never seed today's
    continuation chains.
    """
    prior_clusters: dict[str, Cluster] = {}
    for delta in range(1, lookback_days + 1):
        prior_date = today - datetime.timedelta(days=delta)
        path = paths.clusters_path(prior_date, canonical=True)
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        cluster = Cluster.model_validate_json(line)
                        prior_clusters[cluster.cluster_id] = cluster
                    except Exception as exc:
                        logger.warning(
                            "Failed to parse prior Cluster line",
                            extra={
                                "component": "cluster",
                                "path": str(path),
                                "error": str(exc),
                            },
                        )
        except Exception as exc:
            logger.warning(
                "Failed to open prior clusters.jsonl",
                extra={"component": "cluster", "path": str(path), "error": str(exc)},
            )
    return prior_clusters


def _resolve_chain_root(
    cluster_id: str,
    prior_clusters: dict[str, Cluster],
) -> str:
    """Follow cross_time_ref links to find the earliest cluster in the chain.

    Walks up to MAX_CHAIN_DEPTH hops.  If a cycle is detected, logs a warning
    and returns the cluster_id directly (no root resolution possible).

    DESIGN.md §Setting Cluster.cross_time_ref step 4:
        "cross_time_ref is set to the cluster_id of the *earliest* cluster in
         the continuation chain — not the immediately previous day, but the root."
    """
    visited: set[str] = set()
    current = cluster_id
    for _ in range(MAX_CHAIN_DEPTH):
        if current in visited:
            logger.warning(
                "Cycle detected in cross_time_ref chain; using cluster_id directly",
                extra={"component": "cluster", "cluster_id": cluster_id, "cycle_at": current},
            )
            return cluster_id
        visited.add(current)
        node = prior_clusters.get(current)
        if node is None or node.cross_time_ref is None:
            return current
        current = node.cross_time_ref
    # Exhausted MAX_CHAIN_DEPTH without hitting None — return current.
    logger.warning(
        "MAX_CHAIN_DEPTH exceeded resolving chain root; using current node",
        extra={"component": "cluster", "cluster_id": cluster_id, "depth": MAX_CHAIN_DEPTH},
    )
    return current


def _link_cross_time(
    clusters: list[Cluster],
    today_centroids: dict[str, np.ndarray],
    prior_centroids: dict[str, np.ndarray],
    prior_clusters: dict[str, Cluster],
) -> int:
    """Set Cluster.cross_time_ref for clusters that continue a prior story.

    Mutates clusters in place.  Returns count of clusters linked.

    Algorithm:
    1. Stack prior centroid matrix + cluster_id index for vectorised cosine
       computation (dot product of L2-normalised vecs).
    2. For each today-cluster: compute dot against all prior centroids,
       take the argmax.  If max >= CROSS_TIME_COSINE_THRESHOLD, resolve the
       chain root and set cross_time_ref.
    """
    if not prior_centroids:
        return 0

    prior_ids = list(prior_centroids.keys())
    prior_matrix = np.stack([prior_centroids[cid] for cid in prior_ids])  # (P, D)

    linked = 0
    for cluster in clusters:
        today_vec = today_centroids.get(cluster.cluster_id)
        if today_vec is None:
            continue
        # Cosine similarities: (P,) since vecs are normalised.
        sims: np.ndarray = prior_matrix @ today_vec
        best_idx = int(np.argmax(sims))
        best_sim = float(sims[best_idx])
        if best_sim >= CROSS_TIME_COSINE_THRESHOLD:
            matched_id = prior_ids[best_idx]
            root_id = _resolve_chain_root(matched_id, prior_clusters)
            # Direct attribute mutation is fine: cross_time_ref is a declared
            # field on Cluster.  extra="forbid" only blocks undeclared fields.
            cluster.cross_time_ref = root_id
            linked += 1
    return linked


def _write_clusters(out_path: Path, clusters: list[Cluster]) -> None:
    """Atomically write clusters.jsonl at `out_path`, sorted by size descending.

    Uses .tmp + fsync + os.replace per DESIGN.md atomic-write contract.
    """
    sorted_clusters = sorted(clusters, key=lambda c: c.size, reverse=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        for cluster in sorted_clusters:
            fh.write(cluster.model_dump_json() + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, out_path)


# ---------------------------------------------------------------------------
# Public entry point (DESIGN.md module boundary table)
# ---------------------------------------------------------------------------


def cluster_day(
    run_date: datetime.date | None = None,
    data_dir: Path | None = None,  # kept for backward-compat; ignored
    lookback_days: int = CROSS_TIME_LOOKBACK_DAYS,
    config_path: Path = Path("config/sources.yaml"),
) -> list[Cluster]:
    """Read staging items.jsonl, embed, cluster within-day, link cross-time
    against canonical history, write outputs to staging.

    Returns the list of Clusters for the caller (run.py).

    Args:
        run_date:    The date to process; defaults to today.
        data_dir:    Deprecated/ignored in Round B; paths come from `src.paths`.
        lookback_days: Days of history for cross-time dedup (default 14).
        config_path: Path to sources.yaml for trust weights.

    Round B path model (DESIGN.md "Archive: staging vs canonical"):
      * Reads today's items from `data/staging/<date>/items.jsonl`.
      * Reads `data/published_urls.txt` (canonical-only exclusion index).
      * Reads cross-time history from CANONICAL `data/<date>/...` only.
      * Writes today's clusters + centroids under `data/staging/<date>/`.
    """
    if run_date is None:
        run_date = datetime.date.today()

    if data_dir is not None and data_dir != paths.DATA_ROOT:
        logger.warning(
            "cluster_day: data_dir=%s is ignored in Round B; using %s",
            data_dir,
            paths.STAGING_ROOT,
        )

    staging_day = paths.staging_dir(run_date)
    staging_day.mkdir(parents=True, exist_ok=True)

    # --- Step 0: cross-issue URL filter (canonical-only exclusion index) -------
    published_set = _load_published_urls()

    items_file = paths.items_path(run_date, canonical=False)
    items, filtered_count = _load_items(items_file, published_set)
    if not items:
        logger.info(
            "No items to cluster",
            extra={"component": "cluster", "date": str(run_date)},
        )
        return []

    trust_weights = _load_trust_weights(config_path)

    # --- Step 1: embed ----------------------------------------------------------
    embeddings = _embed(items)  # (N, 768) float32

    # --- Step 2: within-day clustering ------------------------------------------
    clusters = _cluster_within_day(items, embeddings, trust_weights)

    # --- Step 3: compute + write centroids sidecar (staging) -------------------
    today_centroids = _compute_centroids(clusters, items, embeddings)
    centroids_out = paths.centroids_path(run_date, canonical=False)
    _write_centroids(centroids_out, today_centroids)

    # DESIGN.md note: centroid_ref format chosen as
    #   "embeddings/centroids.npz#<cluster_id>"
    # giving downstream readers an unambiguous pointer: file path + key within
    # the npz archive. DESIGN.md §clusters.jsonl says 'centroid_ref records the
    # filename'; appending '#<cluster_id>' extends that minimally to identify
    # the key, which is necessary since one .npz holds all clusters for the day.
    for cluster in clusters:
        cluster.centroid_ref = f"embeddings/centroids.npz#{cluster.cluster_id}"

    # --- Step 4: cross-time linking (CANONICAL history only) -------------------
    prior_centroids = _load_prior_centroids(run_date, lookback_days)
    prior_clusters = _load_prior_clusters(run_date, lookback_days)
    cross_linked = _link_cross_time(
        clusters, today_centroids, prior_centroids, prior_clusters
    )

    # --- Step 5: write clusters.jsonl (staging) --------------------------------
    clusters_out = paths.clusters_path(run_date, canonical=False)
    _write_clusters(clusters_out, clusters)

    # --- Summary log line -------------------------------------------------------
    logger.info(
        "clustered %d items -> %d clusters | %d cross-time linked (canonical only)"
        " | %d filtered as previously released -> %s",
        len(items),
        len(clusters),
        cross_linked,
        filtered_count,
        clusters_out,
        extra={"component": "cluster", "date": str(run_date)},
    )

    return clusters


# ---------------------------------------------------------------------------
# Backward-compat alias — spec says entry point is named `cluster()`.
# DESIGN.md names it cluster_day() in the module boundary table.
# Expose both; cluster() proxies cluster_day() with today's date.
# ---------------------------------------------------------------------------


def cluster(date: datetime.date | None = None) -> list[Cluster]:
    """Read items.jsonl, embed, cluster within-day, link cross-time, write clusters.jsonl.

    Returns the list of Clusters for the caller (run.py).

    This is the simplified entry point specified in the task brief.
    cluster_day() is the full signature per DESIGN.md.
    """
    return cluster_day(run_date=date)


# ---------------------------------------------------------------------------
# __main__ — standalone debug entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    clusters = cluster()
    print(f"{len(clusters)} clusters")
