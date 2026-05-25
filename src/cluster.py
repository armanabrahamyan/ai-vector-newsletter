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

Canonical-ID-aware clustering (tasks #80 + #83):
  Items with a stable canonical identifier (arxiv abs ID, GitHub release tag,
  DOI) are clustered by that identifier BEFORE embedding-based clustering runs.
  Two rules fire before the embedding pass:
    Rule A — same canonical ID: force-grouped into one cluster.
    Rule B — different canonical IDs: forbidden from merging via embeddings.
  Items without a canonical ID fall through to the existing embedding path.
  See _canonical_id() and _apply_canonical_id_rules() for implementation.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Optional

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
CROSS_TIME_COSINE_THRESHOLD = 0.82              # higher bar: cross-day similarity to set prior_coverage_ref
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


# ---------------------------------------------------------------------------
# Canonical-ID helpers (tasks #80 + #83)
# ---------------------------------------------------------------------------

# Compiled once at module load for performance.
_RE_ARXIV_URL = re.compile(
    r"arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?",
    re.IGNORECASE,
)
_RE_GITHUB_RELEASE_URL = re.compile(
    r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/releases/tag/([^)\s\"'>\]]+)",
    re.IGNORECASE,
)
_RE_DOI_URL = re.compile(
    r"(?:dx\.)?doi\.org/(.+?)(?:[\s\"'>\]\)]|$)",
    re.IGNORECASE,
)


def _extract_canonical_id_from_url(url_str: str) -> Optional[str]:
    """Return a canonical identity string from a single URL string, or None.

    Patterns handled:
      arxiv.org/abs/<ID>             -> "arxiv:<ID>"  (version suffix stripped)
      github.com/<o>/<r>/releases/tag/<tag> -> "github_release:<o>/<r>:<tag>"
      doi.org/<doi> / dx.doi.org/<doi>     -> "doi:<doi>"

    Returns None when the URL matches none of the above patterns.
    """
    m = _RE_ARXIV_URL.search(url_str)
    if m:
        return f"arxiv:{m.group(1)}"

    m = _RE_GITHUB_RELEASE_URL.search(url_str)
    if m:
        repo = m.group(1).rstrip("/")
        tag = m.group(2).rstrip("/")
        return f"github_release:{repo}:{tag}"

    m = _RE_DOI_URL.search(url_str)
    if m:
        doi = m.group(1).rstrip("/.,;")
        return f"doi:{doi}"

    return None


def _canonical_id(item: Item) -> Optional[str]:
    """Return a stable canonical identity string for an item, or None.

    Step 1: check the item's primary URL against known canonical patterns.
    Step 2: if no match (free-text item — blog, news, Reddit), scan
            item.raw_summary for the FIRST occurrence of a canonical URL.
            If exactly one canonical URL is found, return its ID.
            If zero or two+ distinct canonical IDs appear, return None
            (ambiguous or self-referential; fall through to embedding).

    This two-step approach is what bridges Reddit posts that link to a GitHub
    release entry: the Reddit post's primary URL is reddit.com/... (no ID),
    but its raw_summary contains the release URL.
    """
    primary_url = str(item.url)
    cid = _extract_canonical_id_from_url(primary_url)
    if cid is not None:
        return cid

    # Primary URL has no canonical ID — scan the body for secondary signals.
    body = item.raw_summary or ""
    if not body:
        return None

    # Find all canonical IDs mentioned in the body.
    found: list[str] = []

    for m in _RE_ARXIV_URL.finditer(body):
        cid_body = f"arxiv:{m.group(1)}"
        if cid_body not in found:
            found.append(cid_body)

    for m in _RE_GITHUB_RELEASE_URL.finditer(body):
        repo = m.group(1).rstrip("/")
        tag = m.group(2).rstrip("/")
        cid_body = f"github_release:{repo}:{tag}"
        if cid_body not in found:
            found.append(cid_body)

    for m in _RE_DOI_URL.finditer(body):
        doi = m.group(1).rstrip("/.,;")
        cid_body = f"doi:{doi}"
        if cid_body not in found:
            found.append(cid_body)

    if len(found) == 1:
        return found[0]

    # Zero or multiple distinct canonical IDs in body — ambiguous; fall through.
    return None


def _apply_canonical_id_rules(
    items: list[Item],
    embeddings: np.ndarray,
    trust_weights: dict[str, int],
) -> tuple[list[Cluster], list[Item], np.ndarray]:
    """Apply rule A (force-group) and rule B (forbid-merge) before embeddings.

    Rule A — same canonical ID: items sharing an identical canonical ID are
    force-grouped into one Cluster immediately, bypassing cosine clustering.
    This fixes e.g. a Reddit post + an official GitHub release entry for b9297.

    Rule B — different canonical IDs: items with *distinct* canonical IDs are
    individually extracted as singleton Clusters so they cannot be merged by
    the embedding step. This prevents distinct arxiv papers from collapsing
    on thematic similarity.

    Items with no canonical ID (None) are passed through unchanged to the
    embedding-based clustering step.

    Returns:
        canonical_clusters:  Cluster list produced by rules A + B.
        free_items:          Items with no canonical ID; go to embedding pass.
        free_embeddings:     Corresponding embedding rows for free_items.
    """
    # Assign a canonical ID to each item.
    cids: list[Optional[str]] = [_canonical_id(item) for item in items]

    # Bucket items by canonical ID.
    # None -> free-text items (embedding pass).
    # string -> canonical-id-bucketed items.
    bucket: dict[str, list[int]] = {}  # cid -> list of item indices
    free_indices: list[int] = []

    for idx, cid in enumerate(cids):
        if cid is None:
            free_indices.append(idx)
        else:
            bucket.setdefault(cid, []).append(idx)

    canonical_clusters: list[Cluster] = []

    # Rule A: items sharing the same canonical ID -> one forced cluster.
    # Rule B: each distinct canonical ID -> its own singleton (or multi-item
    #         group from rule A), forbidden from merging with other buckets.
    for cid, indices in bucket.items():
        cluster_items = [items[i] for i in indices]
        canonical_clusters.append(_build_cluster(cluster_items, trust_weights))
        logger.debug(
            "Canonical-ID cluster: cid=%s items=%d",
            cid,
            len(cluster_items),
            extra={"component": "cluster"},
        )

    # Collect free items and their embeddings.
    free_items = [items[i] for i in free_indices]
    free_embeddings = (
        embeddings[free_indices] if free_indices else np.empty((0, embeddings.shape[1]), dtype=np.float32)
    )

    logger.info(
        "Canonical-ID rules: %d canonical clusters | %d free items -> embedding pass",
        len(canonical_clusters),
        len(free_items),
        extra={"component": "cluster"},
    )

    return canonical_clusters, free_items, free_embeddings


def _cluster_within_day(
    items: list[Item],
    embeddings: np.ndarray,
    trust_weights: dict[str, int],
) -> list[Cluster]:
    """Agglomerative clustering with cosine distance threshold.

    Two-phase approach:
      Phase 1 — canonical-ID rules (tasks #80 + #83):
        - Rule A: items sharing the same canonical ID (arxiv abs, GitHub release
          tag, DOI) are force-grouped, bypassing embedding similarity.
        - Rule B: items with distinct canonical IDs are separated as individual
          clusters and excluded from the embedding pass — different arxiv IDs
          are by definition different papers and must not merge on thematic
          similarity.
        Items with no canonical ID are collected for Phase 2.
      Phase 2 — embedding-based agglomerative clustering (unchanged):
        sklearn.cluster.AgglomerativeClustering with:
          - metric="cosine"
          - linkage="average"
          - distance_threshold = 1 - WITHIN_DAY_COSINE_THRESHOLD

    DESIGN.md note: distance = 1 - cosine_similarity for L2-normalised vectors.
    With normalize_embeddings=True from sentence-transformers, ||v||=1, so
    dot(v_i, v_j) = cos(v_i, v_j) and distance = 1 - dot = ||v_i - v_j||^2 / 2.
    sklearn's cosine metric computes 1 - cos directly, which is correct here.
    """
    if not items:
        return []

    # Phase 1: canonical-ID rules (fire before any cosine comparison).
    canonical_clusters, free_items, free_embeddings = _apply_canonical_id_rules(
        items, embeddings, trust_weights
    )

    # Phase 2: embedding-based clustering for free-text items only.
    embedding_clusters: list[Cluster] = []

    if not free_items:
        pass  # Nothing left for the embedding pass.
    elif len(free_items) == 1:
        # sklearn requires >= 2 samples; handle singleton directly.
        embedding_clusters = [_build_cluster([free_items[0]], trust_weights)]
    else:
        from sklearn.cluster import AgglomerativeClustering

        distance_threshold = 1.0 - WITHIN_DAY_COSINE_THRESHOLD
        model = AgglomerativeClustering(
            n_clusters=None,
            distance_threshold=distance_threshold,
            metric="cosine",
            linkage="average",
        )
        labels: np.ndarray = model.fit_predict(free_embeddings)

        # Group items by cluster label.
        label_to_items: dict[int, list[tuple[int, Item]]] = {}
        for idx, label in enumerate(labels.tolist()):
            label_to_items.setdefault(label, []).append((idx, free_items[idx]))

        for label_items in label_to_items.values():
            cluster_items = [pair[1] for pair in label_items]
            embedding_clusters.append(_build_cluster(cluster_items, trust_weights))

    return canonical_clusters + embedding_clusters


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
        prior_coverage_ref=None,
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
    """Follow prior_coverage_ref links to find the earliest cluster in the chain.

    Walks up to MAX_CHAIN_DEPTH hops.  If a cycle is detected, logs a warning
    and returns the cluster_id directly (no root resolution possible).

    DESIGN.md §Setting Cluster.prior_coverage_ref step 4:
        "prior_coverage_ref is set to the cluster_id of the *earliest* cluster
         in the chain -- not the immediately previous day, but the root."
    """
    visited: set[str] = set()
    current = cluster_id
    for _ in range(MAX_CHAIN_DEPTH):
        if current in visited:
            logger.warning(
                "Cycle detected in prior_coverage_ref chain; using cluster_id directly",
                extra={"component": "cluster", "cluster_id": cluster_id, "cycle_at": current},
            )
            return cluster_id
        visited.add(current)
        node = prior_clusters.get(current)
        if node is None or node.prior_coverage_ref is None:
            return current
        current = node.prior_coverage_ref
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
    """Set Cluster.prior_coverage_ref for clusters that match a prior story.

    Mutates clusters in place.  Returns count of clusters linked.

    Algorithm:
    1. Stack prior centroid matrix + cluster_id index for vectorised cosine
       computation (dot product of L2-normalised vecs).
    2. For each today-cluster: compute dot against all prior centroids,
       take the argmax.  If max >= CROSS_TIME_COSINE_THRESHOLD, resolve the
       chain root and set prior_coverage_ref.
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
            # Direct attribute mutation is fine: prior_coverage_ref is a
            # declared field on Cluster. extra="forbid" only blocks undeclared
            # fields.
            cluster.prior_coverage_ref = root_id
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
