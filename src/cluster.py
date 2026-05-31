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

No LLM calls. Embedding model only (BAAI/bge-base-en-v1.5 via sentence-transformers)
plus a cross-encoder verifier (BAAI/bge-reranker-v2-m3).
No .env loading — both models are fully local.

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

Two-stage clustering (Stage 2 — pair verification):
  After agglomerative clustering, each multi-item candidate cluster is
  verified by a cross-encoder reranker (BAAI/bge-reranker-v2-m3).  For
  every pair in the candidate cluster the verifier scores how likely the
  two items are the *same story*.  Pairs that fall below
  VERIFICATION_THRESHOLD are split out; clusters that survive all pairwise
  checks are emitted unchanged.  This second stage catches the "topically
  adjacent but different speech act" failure mode — e.g. a recommendation
  post vs. a help-request thread about the same model family — which
  bi-encoder cosine cannot distinguish.

  The verifier uses only item titles as input (not full body text).  Titles
  carry the clearest intent signal; body text introduces topical noise that
  degrades the verifier's precision on the intent-collision failure mode.
  Tested against the May 26, 2026 incident: cosine=0.79 (above threshold),
  cross-encoder title score=0.0004 (well below VERIFICATION_THRESHOLD=0.5).

  Algorithm: peel-off (greedy single-linkage split).  Starting from the
  bi-encoder candidate cluster, find the pair with the lowest verifier
  score.  If that score is below VERIFICATION_THRESHOLD, split the lower-
  scored item into its own singleton and repeat.  This preserves legitimate
  multi-item merges (true near-duplicates score >> 0.9) while peeling off
  false merges.  See _verify_and_split_cluster() for the implementation.

  DESIGN.md note: VERIFICATION_THRESHOLD (0.5) is higher than the implicit
  same-day bi-encoder threshold and lower than the cross-time threshold.
  Tuned against: 4 intent-collision pairs (all score < 0.25 on titles) and
  2 true-duplicate pairs (both score > 0.99 on titles).
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
# Revision SHA pinned to the HF commit that was calibrated against.
# Pinning is non-optional: the bi-encoder threshold (0.78) and the cross-encoder
# threshold (0.50) were tuned against these specific weight snapshots.  A silent
# HF weight update would shift embedding distances and verifier scores, potentially
# invalidating both thresholds without any test failures.  Re-calibrate and update
# the SHA whenever an intentional model upgrade is made.
EMBEDDING_MODEL_REVISION = "a5beb1e3e68b9ab74eb54cfd186867f64f240e1a"
EMBEDDING_DIM = 768                              # output dimension of bge-base-en-v1.5
WITHIN_DAY_COSINE_THRESHOLD = 0.78              # two items share a cluster when cosine >= this
CROSS_TIME_COSINE_THRESHOLD = 0.82              # higher bar: cross-day similarity to set prior_coverage_ref
CROSS_TIME_LOOKBACK_DAYS = paths.DEDUP_LOOKBACK_DAYS  # days of history for cross-time dedup (single source of truth: paths.DEDUP_LOOKBACK_DAYS)
MAX_CHAIN_DEPTH = 30                             # cycle-guard: max hops when resolving chain root
BATCH_SIZE = 32                                  # sentence-transformers encode batch size

# ---------------------------------------------------------------------------
# Stage-2 verifier constants
# ---------------------------------------------------------------------------

# Cross-encoder model used for pairwise verification of candidate clusters.
# BAAI/bge-reranker-v2-m3: MIT license, ~440 MB fp32, CPU-runnable, no
# trust_remote_code required.  Apache-2.0 alternative (mxbai-rerank-large-v1)
# tested but scores intent-collision pairs higher (0.32 vs 0.00), giving worse
# precision on the target failure mode.  DeBERTa-MNLI tested but produces
# near-neutral scores for both true duplicates and intent-collision pairs —
# NLI contradiction/entailment labels don't map cleanly to same-story detection
# in this domain.  bge-reranker-v2-m3 chosen for clean bimodal score
# distribution: intent-collision pairs score < 0.25; true duplicates > 0.87.
VERIFIER_MODEL_NAME = "BAAI/bge-reranker-v2-m3"
# Revision SHA pinned for the same reason as EMBEDDING_MODEL_REVISION above.
# VERIFICATION_THRESHOLD (0.50) was calibrated against this exact revision.
VERIFIER_MODEL_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"

# Cross-encoder score below which a pair is considered "not the same story."
# Tuned against 4 intent-collision pairs (max score 0.24) and 2 true-duplicate
# pairs (min score 0.87).  A threshold of 0.5 leaves a gap > 0.60 between
# the highest false-positive score (0.24) and the lowest true-positive (0.87).
# See module docstring for the per-pair evidence.
VERIFICATION_THRESHOLD = 0.5

# Cross-encoder batch size for prediction.  Larger batches amortise tokeniser
# overhead; 16 is conservative for CPU with the 280M-param verifier.
VERIFIER_BATCH_SIZE = 16

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

    model = SentenceTransformer(EMBEDDING_MODEL_NAME, revision=EMBEDDING_MODEL_REVISION)
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
# Stage-2 verifier helpers
# ---------------------------------------------------------------------------


def _load_verifier():
    """Load the cross-encoder verifier model (lazy import).

    Returns a ``sentence_transformers.CrossEncoder`` instance.  Called once
    per ``_cluster_within_day`` call; the caller does not cache the model
    across invocations (process-level caching via HF model cache is enough).

    Model: BAAI/bge-reranker-v2-m3 (MIT, ~440 MB fp32, no trust_remote_code).
    """
    from sentence_transformers import CrossEncoder  # lazy import; heavy

    return CrossEncoder(
        VERIFIER_MODEL_NAME,
        revision=VERIFIER_MODEL_REVISION,
        trust_remote_code=False,
        max_length=512,
    )


def _verifier_text(item: Item) -> str:
    """Return the text string fed to the cross-encoder for a single item.

    Design choice: title only (not title+summary).

    Rationale: the title captures the speech act and intent most directly.
    Body text introduces topical vocabulary that degrades verifier precision
    on the intent-collision failure mode.  Evidence: on the May 26, 2026
    incident, title-only gives scores of 0.0004 (collision) vs 1.0 (duplicate);
    title+full-body gives 0.6353 (collision) — above the threshold, causing a
    false-positive merge.  Title+150-char body gives 0.014 — correct, but the
    margin is smaller.  Title-only provides the largest decision margin.
    """
    return item.title.strip()


def _verify_and_split_cluster(
    cluster_items: list[Item],
    verifier,
) -> list[list[Item]]:
    """Peel-off algorithm: split a candidate cluster into verified sub-clusters.

    For a singleton or two-item cluster that passes the verifier, returns the
    original group unchanged (fast path).  For larger clusters, scores every
    pair and repeatedly peels off items that fail pairwise verification.

    Algorithm (greedy peel-off):
      1. Score all pairs in the candidate cluster.
      2. While any pair scores below VERIFICATION_THRESHOLD:
         a. Find the item with the most sub-threshold pair memberships.
            (Tie-break: item with the lowest average score across its pairs.)
         b. Peel that item off into its own singleton group.
         c. Re-score remaining items (reuse cached scores; no new model call).
      3. Return the remaining multi-item group (if any) plus all singletons.

    This is greedy and O(n^2) in the number of items per cluster.  In practice
    each candidate cluster has 2–5 items, so this is cheap.  A cluster with
    10+ items would already be suspicious at the bi-encoder stage.

    Returns a list of item groups (each group is a non-empty list[Item]).
    The caller converts each group into a Cluster via _build_cluster().
    """
    if len(cluster_items) <= 1:
        return [cluster_items]

    # Fast path: two-item cluster, score the single pair.
    if len(cluster_items) == 2:
        a, b = cluster_items
        texts = [_verifier_text(a), _verifier_text(b)]
        scores = verifier.predict([texts], batch_size=VERIFIER_BATCH_SIZE)
        if float(scores[0]) >= VERIFICATION_THRESHOLD:
            return [cluster_items]
        else:
            logger.debug(
                "Verifier split: score=%.4f < threshold=%.2f | '%s' vs '%s'",
                float(scores[0]),
                VERIFICATION_THRESHOLD,
                a.title[:60],
                b.title[:60],
                extra={"component": "cluster"},
            )
            return [[a], [b]]

    # General case: score all pairs, then peel.
    n = len(cluster_items)
    # pair_score[i][j] for i < j
    all_pairs: list[list[str]] = []
    pair_idx: list[tuple[int, int]] = []
    for i in range(n):
        for j in range(i + 1, n):
            all_pairs.append([_verifier_text(cluster_items[i]), _verifier_text(cluster_items[j])])
            pair_idx.append((i, j))

    raw_scores = verifier.predict(all_pairs, batch_size=VERIFIER_BATCH_SIZE)
    # Build score matrix (symmetric).
    score_matrix: dict[tuple[int, int], float] = {}
    for (i, j), s in zip(pair_idx, raw_scores):
        score_matrix[(i, j)] = float(s)
        score_matrix[(j, i)] = float(s)

    # Peel-off loop.
    remaining: list[int] = list(range(n))
    groups: list[list[Item]] = []

    while len(remaining) > 1:
        # Collect failing pairs (both indices still in remaining).
        failing_pairs = [
            (i, j)
            for i, j in pair_idx
            if i in remaining and j in remaining
            and score_matrix[(i, j)] < VERIFICATION_THRESHOLD
        ]
        if not failing_pairs:
            break  # All remaining pairs pass.

        # Count failures per item; tie-break on average score (lowest = most outlying).
        failure_count: dict[int, int] = {}
        score_sum: dict[int, float] = {}
        for i, j in failing_pairs:
            for idx in (i, j):
                failure_count[idx] = failure_count.get(idx, 0) + 1
                score_sum[idx] = score_sum.get(idx, 0.0) + score_matrix[(idx, i if idx == j else j)]

        # Item to peel: most failures, then lowest average score.
        peel_idx = max(
            failure_count,
            key=lambda idx: (failure_count[idx], -score_sum[idx] / failure_count[idx]),
        )
        logger.debug(
            "Verifier peel: peeling item '%s' (failures=%d, avg_score=%.4f)",
            cluster_items[peel_idx].title[:60],
            failure_count[peel_idx],
            score_sum[peel_idx] / failure_count[peel_idx],
            extra={"component": "cluster"},
        )
        remaining.remove(peel_idx)
        groups.append([cluster_items[peel_idx]])

    if remaining:
        groups.append([cluster_items[idx] for idx in remaining])

    return groups


# ---------------------------------------------------------------------------
# Canonical-ID helpers (tasks #80 + #83)
# ---------------------------------------------------------------------------

# Compiled once at module load for performance.
_RE_ARXIV_URL = re.compile(
    r"arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?",
    re.IGNORECASE,
)
# Hugging Face Daily Papers (huggingface.co/papers/<arxiv_id>) maps directly to
# an arxiv abstract — the URL path IS the arxiv ID. Treated as an arxiv-domain
# alias so HF Papers items collapse onto the arxiv canonical key. Anchored on
# the literal "/papers/<arxiv_id>" path and the strict arxiv-ID shape
# (YYMM.NNNNN with optional vN suffix), which is mutually exclusive with HF's
# other URL spaces (e.g. /datasets/, /spaces/, model repos).
_RE_HF_PAPERS_URL = re.compile(
    r"huggingface\.co/papers/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?",
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

# ---------------------------------------------------------------------------
# Model-version canonical-ID patterns (task: Problem 1)
#
# Items mentioning the same released model token (e.g. "claude-opus-4-8",
# "GPT-5", "gemini-2-0-flash") are force-grouped under rule A.  Items
# mentioning *different* model tokens are forbidden from merging under rule B.
#
# Design decisions:
#   - URL path match first (highest precision: anthropic.com/news/claude-opus-4-8,
#     simonwillison.net/.../claude-opus-4-8/).  The URL path is a deliberate slug
#     chosen by the author, not incidental body text.
#   - Title match second (Latent Space "[AINews] ... Opus 4.8", Vercel "Opus 4.8
#     on AI Gateway").  Prose form ("Claude Opus 4.8", "GPT 5") normalised to the
#     API slug form.
#   - Body match is NOT performed for model tokens.  Body text is noisy ("we
#     benchmarked against Opus 4.6 too") and URL+title precision is sufficient.
#     This is the same signal-hierarchy choice as _RE_HF_PAPERS_URL.
#   - Multi-model titles (Latent Space "[AINews] ... Opus 4.8 and ultracode")
#     produce exactly ONE token if only one model-version appears; they join the
#     Opus 4.8 cluster correctly.  If two distinct model tokens appeared in the
#     same title, we'd return the first match (caller takes the first hit from
#     _extract_model_version_id, which processes URL then title in order).
#
# Canonical form: "model:<normalised-slug>", e.g. "model:claude-opus-4-8".
# Normalisation: lowercase, spaces and dots -> hyphens, strip leading "claude-"
#   prefix from Anthropic prose forms that don't start with "claude-".
#
# Vendor coverage (initial list — extend as new families appear):
#   Anthropic:  claude-(opus|sonnet|haiku)-\d(-\d+)?
#   OpenAI:     gpt-\d(\.\d+)?  (and prose "GPT 5", "GPT-5")
#   Google:     gemini-\d(-\d+)?-(pro|flash|nano|ultra)
#   Meta:       llama-\d(\.\d+)?  (and prose "Llama 4", "Llama-4")
# ---------------------------------------------------------------------------

# URL-path model slug: matches the hyphenated API slug form embedded in a URL path.
# Examples: /news/claude-opus-4-8, /claude-sonnet-4-5, /claude-haiku-4, /claude-opus-4
_RE_MODEL_URL_ANTHROPIC = re.compile(
    r"/claude-(opus|sonnet|haiku)-(\d+(?:-\d+)?)(?:[/?#\s]|$)",
    re.IGNORECASE,
)
_RE_MODEL_URL_OPENAI = re.compile(
    r"/gpt-(\d+(?:\.\d+)?)(?:[/?#\s]|$)",
    re.IGNORECASE,
)
_RE_MODEL_URL_GOOGLE = re.compile(
    r"/gemini-(\d+(?:-\d+)?)-?(pro|flash|nano|ultra)?(?:[/?#\s]|$)",
    re.IGNORECASE,
)
_RE_MODEL_URL_META = re.compile(
    r"/llama-?(\d+(?:\.\d+)?)(?:[/?#\s]|$)",
    re.IGNORECASE,
)

# Title/prose model patterns — normalised prose to API slug.
# These must be anchored on word boundaries to avoid false-positive substring matches.
_RE_MODEL_TITLE_ANTHROPIC = re.compile(
    r"\b(?:claude\s+)?(opus|sonnet|haiku)\s+(\d+(?:[.\-]\d+)?)(?:\b|$)",
    re.IGNORECASE,
)
_RE_MODEL_TITLE_OPENAI = re.compile(
    r"\bGPT[\s\-](\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
_RE_MODEL_TITLE_GOOGLE = re.compile(
    r"\bGemini[\s\-](\d+(?:[.\-]\d+)?)[\s\-]?(Pro|Flash|Nano|Ultra)?\b",
    re.IGNORECASE,
)
_RE_MODEL_TITLE_META = re.compile(
    r"\bLlama[\s\-]?(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)


def _normalise_version(v: str) -> str:
    """Normalise a version string: spaces and dots become hyphens, lowercase."""
    return re.sub(r"[\s.]+", "-", v.strip()).lower()


def _extract_model_version_id(url_str: str, title: str) -> Optional[str]:
    """Return a ``model:<slug>`` canonical ID for a model-version token, or None.

    Search order:
      1. URL path (highest precision — author chose the slug deliberately).
      2. Item title (covers prose announcements like "Opus 4.8 on AI Gateway").

    Returns the FIRST match found.  If the title mentions two model families
    (e.g. "GPT-5 vs Gemini 2.0 Flash benchmark"), only the first hit is
    returned.  This is intentional: multi-model comparison posts should not
    be force-grouped with either model's announcement cluster.  (In practice,
    such posts don't match rule A because neither side matches a single model
    token strongly enough — they fall through to embedding clustering.)

    Normalised slug examples:
      "claude-opus-4-8"   <- Anthropic API slug form (URL or title)
      "gpt-5"             <- OpenAI
      "gemini-2-0-flash"  <- Google
      "llama-4"           <- Meta
    """
    # --- URL path matches ---
    m = _RE_MODEL_URL_ANTHROPIC.search(url_str)
    if m:
        family = m.group(1).lower()
        version = _normalise_version(m.group(2))
        return f"model:claude-{family}-{version}"

    m = _RE_MODEL_URL_OPENAI.search(url_str)
    if m:
        version = _normalise_version(m.group(1))
        return f"model:gpt-{version}"

    m = _RE_MODEL_URL_GOOGLE.search(url_str)
    if m:
        version = _normalise_version(m.group(1))
        suffix = ("-" + m.group(2).lower()) if m.group(2) else ""
        return f"model:gemini-{version}{suffix}"

    m = _RE_MODEL_URL_META.search(url_str)
    if m:
        version = _normalise_version(m.group(1))
        return f"model:llama-{version}"

    # --- Title matches ---
    m = _RE_MODEL_TITLE_ANTHROPIC.search(title)
    if m:
        family = m.group(1).lower()
        version = _normalise_version(m.group(2))
        return f"model:claude-{family}-{version}"

    m = _RE_MODEL_TITLE_OPENAI.search(title)
    if m:
        version = _normalise_version(m.group(1))
        return f"model:gpt-{version}"

    m = _RE_MODEL_TITLE_GOOGLE.search(title)
    if m:
        version = _normalise_version(m.group(1))
        suffix = ("-" + m.group(2).lower()) if m.group(2) else ""
        return f"model:gemini-{version}{suffix}"

    m = _RE_MODEL_TITLE_META.search(title)
    if m:
        version = _normalise_version(m.group(1))
        return f"model:llama-{version}"

    return None


def _extract_canonical_id_from_url(url_str: str) -> Optional[str]:
    """Return a canonical identity string from a single URL string, or None.

    Patterns handled:
      arxiv.org/abs/<ID>                    -> "arxiv:<ID>"  (version suffix stripped)
      huggingface.co/papers/<arxiv_id>      -> "arxiv:<ID>"  (HF Daily Papers alias;
                                                              same key as arxiv.org/abs)
      github.com/<o>/<r>/releases/tag/<tag> -> "github_release:<o>/<r>:<tag>"
      doi.org/<doi> / dx.doi.org/<doi>      -> "doi:<doi>"

    HF Papers URLs collapse onto the arxiv canonical space deliberately:
    huggingface.co/papers/2605.26494 and arxiv.org/abs/2605.26494 refer to the
    same paper, so rule A force-merges items pointing at either URL. Different
    arxiv IDs (under either domain) remain forbidden from merging by rule B.

    Returns None when the URL matches none of the above patterns.
    """
    m = _RE_ARXIV_URL.search(url_str)
    if m:
        return f"arxiv:{m.group(1)}"

    m = _RE_HF_PAPERS_URL.search(url_str)
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

    Step 0: model-version token extraction (title + URL slug).  Items about
            the same released model version (e.g. "claude-opus-4-8") are
            force-grouped under rule A; items about different model versions
            are forbidden from merging under rule B.  This runs before URL
            pattern matching because many model-release items (blog posts,
            changelogs, commentary) have no canonical arxiv/GitHub/DOI URL
            but DO carry a distinctive model slug in their URL path or title.

    Step 1: check the item's primary URL against known canonical patterns
            (arxiv, HF Papers, GitHub releases, DOI).

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

    # Step 0: model-version token (URL slug first, then title).
    model_cid = _extract_model_version_id(primary_url, item.title)
    if model_cid is not None:
        return model_cid

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

    # HF Papers URLs in body-text map to the same arxiv:<ID> key as arxiv.org
    # so a body mentioning both forms of the same paper does NOT register as
    # ambiguous (two distinct canonical IDs).
    for m in _RE_HF_PAPERS_URL.finditer(body):
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
        cluster = _build_cluster(cluster_items, trust_weights)
        # Persist the bucketing key onto the cluster so downstream stages
        # (e.g. summarise._pick_pulse eligibility) can read it without
        # re-running URL pattern matching.
        cluster.canonical_id = cid
        canonical_clusters.append(cluster)
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
    skip_verification: bool = False,
) -> list[Cluster]:
    """Two-stage clustering: bi-encoder candidate generation + pair verification.

    Stage 1 — canonical-ID rules (tasks #80 + #83):
      - Rule A: items sharing the same canonical ID (arxiv abs, GitHub release
        tag, DOI) are force-grouped, bypassing embedding similarity.
      - Rule B: items with distinct canonical IDs are separated as individual
        clusters and excluded from the embedding pass — different arxiv IDs
        are by definition different papers and must not merge on thematic
        similarity.
      Items with no canonical ID are collected for Stage 2.

    Stage 2 — embedding-based agglomerative clustering:
      sklearn.cluster.AgglomerativeClustering with:
        - metric="cosine"
        - linkage="average"
        - distance_threshold = 1 - WITHIN_DAY_COSINE_THRESHOLD

      DESIGN.md note: distance = 1 - cosine_similarity for L2-normalised
      vectors.  With normalize_embeddings=True, dot(v_i, v_j) == cosine.
      sklearn's cosine metric computes 1 - cos directly.

    Stage 3 — pairwise cross-encoder verification:
      Each multi-item embedding cluster is submitted to the verifier
      (_verify_and_split_cluster).  Pairs that score below
      VERIFICATION_THRESHOLD on the verifier are split.  Singletons and
      canonical-ID clusters skip verification (they carry a stronger signal
      than embedding similarity alone and do not exhibit the intent-collision
      failure mode).

      Verification is skipped when skip_verification=True (used in tests
      that monkeypatch _embed but do not need the second model loaded).

    Args:
        items: Items to cluster.
        embeddings: L2-normalised embeddings for items (shape n x EMBEDDING_DIM).
        trust_weights: {source_name: weight} for canonical_title selection.
        skip_verification: When True, omit Stage 3 (useful for unit tests).
    """
    if not items:
        return []

    # Stage 1: canonical-ID rules (fire before any cosine comparison).
    canonical_clusters, free_items, free_embeddings = _apply_canonical_id_rules(
        items, embeddings, trust_weights
    )

    # Stage 2: embedding-based clustering for free-text items only.
    candidate_groups: list[list[Item]] = []  # groups before verification

    if not free_items:
        pass  # Nothing left for the embedding pass.
    elif len(free_items) == 1:
        # sklearn requires >= 2 samples; handle singleton directly.
        candidate_groups = [[free_items[0]]]
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
        label_to_items: dict[int, list[Item]] = {}
        for idx, label in enumerate(labels.tolist()):
            label_to_items.setdefault(label, []).append(free_items[idx])

        candidate_groups = list(label_to_items.values())

    # Stage 3: pairwise cross-encoder verification.
    # Only multi-item candidate clusters are submitted to the verifier.
    # Singletons pass through unchanged.
    embedding_clusters: list[Cluster] = []

    # Check if any multi-item candidate exists — avoid loading the verifier
    # model when all candidates are singletons (common on sparse days).
    multi_candidates = [g for g in candidate_groups if len(g) > 1]

    if multi_candidates and not skip_verification:
        verifier = _load_verifier()
        for group in candidate_groups:
            if len(group) == 1:
                embedding_clusters.append(_build_cluster(group, trust_weights))
            else:
                # Verify; may return multiple sub-groups if pairs fail.
                sub_groups = _verify_and_split_cluster(group, verifier)
                for sub in sub_groups:
                    embedding_clusters.append(_build_cluster(sub, trust_weights))

        n_before = len(candidate_groups)
        n_after = len(embedding_clusters)
        n_splits = n_after - n_before
        if n_splits > 0:
            logger.info(
                "Verifier stage: %d candidate cluster(s) -> %d cluster(s) "
                "(%d split(s))",
                n_before,
                n_after,
                n_splits,
                extra={"component": "cluster"},
            )
    else:
        # Verification skipped (all singletons, or skip_verification=True).
        for group in candidate_groups:
            embedding_clusters.append(_build_cluster(group, trust_weights))

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

            # Self-match guard: a slow-cadence feed item that recurs with the
            # same item_id across consecutive days produces an identical
            # cluster_id (deterministic SHA of item_ids) and therefore matches
            # itself in prior_centroids with cosine=1.0.  Without this guard,
            # _resolve_chain_root follows the self-referencing chain, hits the
            # cycle detector, and returns cluster_id — producing a useless
            # self-ref (prior_coverage_ref == cluster_id).  The fix: if the
            # best match IS the cluster itself, resolve the chain of THAT prior
            # node's own prior_coverage_ref, skipping the self-hop.  If no
            # non-self ancestor exists (first day the item appeared), leave
            # prior_coverage_ref as None (correct: no real predecessor).
            if matched_id == cluster.cluster_id:
                prior_node = prior_clusters.get(matched_id)
                if prior_node is None or prior_node.prior_coverage_ref is None:
                    # No ancestor — this IS the root; nothing to link to.
                    continue
                if prior_node.prior_coverage_ref == matched_id:
                    # Self-loop on the prior node too — skip.
                    continue
                # Follow the chain from the prior node's ancestor.
                root_id = _resolve_chain_root(prior_node.prior_coverage_ref, prior_clusters)
            else:
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
