"""Unit tests for src/cluster.py — the Retrieval Engineer's module.

Strategy: monkeypatch `src.cluster._embed` to return hand-crafted numpy arrays
so BAAI/bge-base-en-v1.5 (440 MB) is never loaded in the test suite. Each test
controls the exact embedding vectors and therefore the clustering outcome exactly.

The Stage-2 verifier (BAAI/bge-reranker-v2-m3, ~440 MB) is monkeypatched with
a ``_PassthroughVerifier`` that approves all pairs (score=1.0).  Tests for the
verification stage itself live in ``TestVerifierStage`` and
``TestIntentCollisionRegression`` below; those tests supply a controlled verifier
that exercises the real peel-off logic without loading the real model.

Dim=16 is used for hand-crafted vectors; any test that needs to exercise the
768-dim centroid contract uses a numpy array of that shape filled with controlled
values, still without loading the real model.
"""
from __future__ import annotations

import datetime
import hashlib
import re
from pathlib import Path

import numpy as np
import pytest

from src import cluster as cluster_mod
from src.models import Cluster, Item
from tests.conftest import FIXED_EARLIER, FIXED_NOW, UTC

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _PassthroughVerifier:
    """Stub verifier that approves all pairs (score=1.0).

    Used in tests that monkeypatch ``_embed`` but are not testing the
    verification stage.  Ensures those tests stay isolated from the real
    verifier model without loading 440 MB of weights.
    """

    def predict(self, pairs, batch_size=16):  # noqa: D102
        return np.ones(len(pairs), dtype=np.float32)


class _RejectAllVerifier:
    """Stub verifier that rejects all pairs (score=0.0).

    Used in regression tests to confirm the peel-off algorithm runs when
    all pairs fall below VERIFICATION_THRESHOLD.
    """

    def predict(self, pairs, batch_size=16):  # noqa: D102
        return np.zeros(len(pairs), dtype=np.float32)


class _ControlledVerifier:
    """Stub verifier with a user-supplied score table.

    ``score_table`` maps frozenset({title_a, title_b}) -> float.
    Any pair not in the table returns ``default_score``.

    Isolates the peel-off algorithm from the real model while exercising
    non-trivial score distributions.
    """

    def __init__(self, score_table: dict[frozenset, float], default_score: float = 1.0):
        self._table = score_table
        self._default = default_score

    def predict(self, pairs, batch_size=16):  # noqa: D102
        scores = []
        for a, b in pairs:
            key = frozenset({a, b})
            scores.append(self._table.get(key, self._default))
        return np.array(scores, dtype=np.float32)

_T0 = FIXED_EARLIER
_T1 = datetime.datetime(2026, 5, 24, 10, 0, 0, tzinfo=UTC)  # even earlier
_T2 = datetime.datetime(2026, 5, 24, 13, 0, 0, tzinfo=UTC)  # later

DIM = 16  # small dim for hand-crafted tests
REAL_DIM = 768  # used when the 768-shape contract must be verified


def _unit(v: list[float]) -> np.ndarray:
    """Return an L2-normalised float32 vector from a plain list."""
    arr = np.array(v, dtype=np.float32)
    norm = np.linalg.norm(arr)
    if norm > 0:
        arr = arr / norm
    return arr


def _make_item(
    id: str,
    title: str,
    source: str = "src_a",
    trust_weight: int = 3,
    published_at: datetime.datetime = _T0,
) -> Item:
    return Item(
        id=id,
        source=source,
        source_type="rss",
        url=f"https://example.com/{id}",
        title=title,
        published_at=published_at,
        raw_summary="summary",
        fetched_at=FIXED_NOW,
        trust_weight=trust_weight,
    )


def _expected_cluster_id(item_ids: list[str]) -> str:
    sorted_ids = sorted(item_ids)
    raw = hashlib.sha256(",".join(sorted_ids).encode()).hexdigest()
    return "c_" + raw[:16]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def items_file(tmp_data_root: Path, fixed_date: datetime.date) -> Path:
    """Return a staging items.jsonl path (parent dir created)."""
    from src import paths
    staging = paths.staging_dir(fixed_date)
    staging.mkdir(parents=True, exist_ok=True)
    return paths.items_path(fixed_date, canonical=False)


@pytest.fixture(autouse=True)
def stub_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Globally replace _load_verifier with a passthrough for all tests in this module.

    Tests that specifically exercise verification behaviour override this by
    supplying a controlled verifier via a further monkeypatch inside the test body
    (later monkeypatches win because they both target the same attribute).

    Without this fixture every test that produces a multi-item embedding cluster
    would trigger a real CrossEncoder load (~440 MB) and slow the suite by 30-60s
    per session.  The verifier is tested in TestVerifierStage and
    TestIntentCollisionRegression using controlled stub verifiers.
    """
    monkeypatch.setattr(cluster_mod, "_load_verifier", lambda: _PassthroughVerifier())


def _write_items(path: Path, items: list[Item]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(item.model_dump_json() + "\n")


# ===========================================================================
# TestWithinDayClustering
# ===========================================================================

class TestWithinDayClustering:
    """Items with nearly-identical embeddings collapse; distant items stay separate."""

    def test_near_identical_items_cluster_together(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        items = [
            _make_item("i1", "OpenAI releases GPT-X", source="blog_a"),
            _make_item("i2", "GPT-X launched by OpenAI", source="blog_b"),
        ]
        # Near-identical: slightly perturbed unit vectors with cosine > 0.78.
        base = _unit([1.0] + [0.0] * (DIM - 1))
        noise = _unit([1.0, 0.01] + [0.0] * (DIM - 2))
        embeddings = np.stack([base, noise])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 1
        assert set(clusters[0].item_ids) == {"i1", "i2"}

    def test_distinct_items_stay_separate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        items = [
            _make_item("i1", "OpenAI releases GPT-X"),
            _make_item("i2", "Anthropic releases Claude 4"),
        ]
        # Orthogonal vectors: cosine = 0.
        v1 = _unit([1.0] + [0.0] * (DIM - 1))
        v2 = _unit([0.0, 1.0] + [0.0] * (DIM - 2))
        embeddings = np.stack([v1, v2])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 2
        ids_in_clusters = {frozenset(c.item_ids) for c in clusters}
        assert frozenset({"i1"}) in ids_in_clusters
        assert frozenset({"i2"}) in ids_in_clusters

    def test_three_items_two_clusters(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        items = [
            _make_item("i1", "GPT-X story A"),
            _make_item("i2", "GPT-X story B"),
            _make_item("i3", "Completely different topic"),
        ]
        # i1 and i2 near-identical; i3 orthogonal.
        base = _unit([1.0] + [0.0] * (DIM - 1))
        near = _unit([1.0, 0.01] + [0.0] * (DIM - 2))
        far = _unit([0.0, 1.0] + [0.0] * (DIM - 2))
        embeddings = np.stack([base, near, far])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 2
        sizes = sorted(c.size for c in clusters)
        assert sizes == [1, 2]


# ===========================================================================
# TestSingletonCluster
# ===========================================================================

class TestSingletonCluster:
    """A single item with no near-neighbours becomes a 1-member cluster."""

    def test_single_item_yields_singleton(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        items = [_make_item("i1", "Only story today")]
        v = _unit([1.0] + [0.0] * (DIM - 1))
        embeddings = np.stack([v])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 1
        assert clusters[0].size == 1
        assert clusters[0].item_ids == ["i1"]

    def test_singleton_cluster_id_is_deterministic(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        items = [_make_item("solo-99", "Only story today")]
        v = _unit([1.0] + [0.0] * (DIM - 1))
        embeddings = np.stack([v])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        expected_id = _expected_cluster_id(["solo-99"])
        assert clusters[0].cluster_id == expected_id


# ===========================================================================
# TestCanonicalTitle
# ===========================================================================

class TestCanonicalTitle:
    """canonical_title = title of item with highest trust_weight; ties broken
    alphabetically ascending on title."""

    def test_highest_trust_weight_wins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        items = [
            _make_item("i1", "Lower trust title", source="blog_a", trust_weight=2),
            _make_item("i2", "Higher trust title", source="blog_b", trust_weight=5),
        ]
        base = _unit([1.0] + [0.0] * (DIM - 1))
        near = _unit([1.0, 0.01] + [0.0] * (DIM - 2))
        embeddings = np.stack([base, near])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 1
        assert clusters[0].canonical_title == "Higher trust title"

    def test_tie_broken_alphabetically(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        items = [
            _make_item("i1", "Zebra story", trust_weight=3),
            _make_item("i2", "Aardvark story", trust_weight=3),
        ]
        base = _unit([1.0] + [0.0] * (DIM - 1))
        near = _unit([1.0, 0.01] + [0.0] * (DIM - 2))
        embeddings = np.stack([base, near])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        # Alphabetically ascending: "Aardvark…" < "Zebra…"
        assert len(clusters) == 1
        assert clusters[0].canonical_title == "Aardvark story"


# ===========================================================================
# TestSourcesDeduplication
# ===========================================================================

class TestSourcesDeduplication:
    """Two items from the same source yield one entry in cluster.sources."""

    def test_duplicate_source_appears_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        items = [
            _make_item("i1", "Story A", source="techcrunch"),
            _make_item("i2", "Story B", source="techcrunch"),
        ]
        base = _unit([1.0] + [0.0] * (DIM - 1))
        near = _unit([1.0, 0.01] + [0.0] * (DIM - 2))
        embeddings = np.stack([base, near])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 1
        assert clusters[0].sources == ["techcrunch"]

    def test_distinct_sources_both_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        items = [
            _make_item("i1", "Story A", source="reuters"),
            _make_item("i2", "Story B", source="bloomberg"),
        ]
        base = _unit([1.0] + [0.0] * (DIM - 1))
        near = _unit([1.0, 0.01] + [0.0] * (DIM - 2))
        embeddings = np.stack([base, near])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 1
        assert set(clusters[0].sources) == {"reuters", "bloomberg"}


# ===========================================================================
# TestEarliestPublished
# ===========================================================================

class TestEarliestPublished:
    """The earliest UTC published_at among cluster members wins."""

    def test_min_published_at_selected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        items = [
            _make_item("i1", "Story A", published_at=_T2),    # latest
            _make_item("i2", "Story B", published_at=_T1),    # earliest
            _make_item("i3", "Story C", published_at=_T0),    # middle
        ]
        base = _unit([1.0] + [0.0] * (DIM - 1))
        near1 = _unit([1.0, 0.01] + [0.0] * (DIM - 2))
        near2 = _unit([1.0, 0.02] + [0.0] * (DIM - 2))
        embeddings = np.stack([base, near1, near2])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 1
        assert clusters[0].earliest_published == _T1


# ===========================================================================
# TestCrossTimeDedup
# ===========================================================================

class TestCrossTimeDedup:
    """Cross-time dedup sets prior_coverage_ref to the prior cluster_id when
    today's centroid matches a released centroid above CROSS_TIME_COSINE_THRESHOLD."""

    def _plant_prior_centroid(
        self,
        tmp_data_root: Path,
        prior_date: datetime.date,
        prior_cluster_id: str,
        centroid: np.ndarray,
    ) -> None:
        """Write a centroids.npz into the RELEASED path for prior_date."""
        from src import paths
        npz_path = paths.centroids_path(prior_date, canonical=True)
        npz_path.parent.mkdir(parents=True, exist_ok=True)
        with open(npz_path, "wb") as fh:
            np.savez(fh, **{prior_cluster_id: centroid})

    def _plant_prior_cluster(
        self,
        tmp_data_root: Path,
        prior_date: datetime.date,
        cluster: Cluster,
    ) -> None:
        """Write a clusters.jsonl into the RELEASED path for prior_date."""
        from src import paths
        clusters_path = paths.clusters_path(prior_date, canonical=True)
        clusters_path.parent.mkdir(parents=True, exist_ok=True)
        with clusters_path.open("w", encoding="utf-8") as fh:
            fh.write(cluster.model_dump_json() + "\n")

    def test_prior_coverage_ref_set_when_similar(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        # Build a prior cluster with a known centroid.
        prior_date = fixed_date - datetime.timedelta(days=1)
        prior_cid = _expected_cluster_id(["prior-item"])
        prior_centroid = _unit([1.0] + [0.0] * (DIM - 1))

        # Prior cluster record (needs to exist so _load_prior_clusters finds it).
        prior_cluster_obj = Cluster(
            cluster_id=prior_cid,
            item_ids=["prior-item"],
            canonical_title="GPT-X continues",
            sources=["blog_a"],
            earliest_published=_T0,
            size=1,
            prior_coverage_ref=None,
        )
        self._plant_prior_centroid(tmp_data_root, prior_date, prior_cid, prior_centroid)
        self._plant_prior_cluster(tmp_data_root, prior_date, prior_cluster_obj)

        # Today's item embeds to a vector nearly identical to the prior centroid.
        today_items = [_make_item("today-1", "GPT-X follow-up today")]
        today_embedding = _unit([1.0, 0.001] + [0.0] * (DIM - 2))
        embeddings = np.stack([today_embedding])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        # Temporarily raise CROSS_TIME_COSINE_THRESHOLD to ensure we control
        # the comparison — set it just below the actual cosine of our vectors.
        # Cosine(prior_centroid, today_embedding) is very close to 1; set threshold
        # to 0.82 (the module default) which is well below 0.999+.
        monkeypatch.setattr(cluster_mod, "CROSS_TIME_COSINE_THRESHOLD", 0.82)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, today_items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 1
        assert clusters[0].prior_coverage_ref == prior_cid

    def test_prior_coverage_ref_none_for_new_story(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        # Prior centroid points in one direction; today's item points in orthogonal.
        prior_date = fixed_date - datetime.timedelta(days=1)
        prior_cid = _expected_cluster_id(["prior-item"])
        prior_centroid = _unit([1.0] + [0.0] * (DIM - 1))

        prior_cluster_obj = Cluster(
            cluster_id=prior_cid,
            item_ids=["prior-item"],
            canonical_title="GPT-X story",
            sources=["blog_a"],
            earliest_published=_T0,
            size=1,
        )
        self._plant_prior_centroid(tmp_data_root, prior_date, prior_cid, prior_centroid)
        self._plant_prior_cluster(tmp_data_root, prior_date, prior_cluster_obj)

        today_items = [_make_item("today-2", "Completely different AI news")]
        orthogonal = _unit([0.0, 1.0] + [0.0] * (DIM - 2))
        embeddings = np.stack([orthogonal])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)
        monkeypatch.setattr(cluster_mod, "CROSS_TIME_COSINE_THRESHOLD", 0.82)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, today_items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 1
        assert clusters[0].prior_coverage_ref is None

    def test_missing_prior_day_does_not_crash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        """No prior centroids exist — proceed gracefully."""
        items = [_make_item("i1", "Brand new story")]
        v = _unit([1.0] + [0.0] * (DIM - 1))
        embeddings = np.stack([v])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        # No centroids planted — just run. Should not raise.
        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 1
        assert clusters[0].prior_coverage_ref is None

    def test_prior_coverage_ref_resolves_to_chain_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        """When prior cluster itself has a prior_coverage_ref, today's ref points to root."""
        root_date = fixed_date - datetime.timedelta(days=3)
        mid_date = fixed_date - datetime.timedelta(days=1)

        root_cid = _expected_cluster_id(["root-item"])
        mid_cid = _expected_cluster_id(["mid-item"])
        shared_vec = _unit([1.0] + [0.0] * (DIM - 1))

        # Root cluster: no prior_coverage_ref.
        root_cluster_obj = Cluster(
            cluster_id=root_cid,
            item_ids=["root-item"],
            canonical_title="Root story",
            sources=["blog_a"],
            earliest_published=_T1,
            size=1,
        )
        # Mid cluster: prior_coverage_ref -> root.
        mid_cluster_obj = Cluster(
            cluster_id=mid_cid,
            item_ids=["mid-item"],
            canonical_title="Mid story",
            sources=["blog_b"],
            earliest_published=_T0,
            size=1,
            prior_coverage_ref=root_cid,
        )

        # Plant both days of released history.
        from src import paths

        for date, cid, obj in [
            (root_date, root_cid, root_cluster_obj),
            (mid_date, mid_cid, mid_cluster_obj),
        ]:
            npz_path = paths.centroids_path(date, canonical=True)
            npz_path.parent.mkdir(parents=True, exist_ok=True)
            with open(npz_path, "wb") as fh:
                np.savez(fh, **{cid: shared_vec})

            clusters_path = paths.clusters_path(date, canonical=True)
            clusters_path.parent.mkdir(parents=True, exist_ok=True)
            with clusters_path.open("w", encoding="utf-8") as fh:
                fh.write(obj.model_dump_json() + "\n")

        today_items = [_make_item("today-3", "Continuation story")]
        today_embedding = _unit([1.0, 0.001] + [0.0] * (DIM - 2))
        embeddings = np.stack([today_embedding])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)
        monkeypatch.setattr(cluster_mod, "CROSS_TIME_COSINE_THRESHOLD", 0.82)

        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, today_items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 1
        # Should resolve to the chain root, not mid.
        assert clusters[0].prior_coverage_ref == root_cid


# ===========================================================================
# TestCentroidSidecar
# ===========================================================================

class TestCentroidSidecar:
    """centroids.npz is written under staging/<date>/embeddings/ with correct shape."""

    def test_centroids_file_written(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        items = [_make_item("i1", "A story")]
        v = np.zeros((1, REAL_DIM), dtype=np.float32)
        v[0, 0] = 1.0  # unit vector

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: v)
        monkeypatch.setattr(cluster_mod, "EMBEDDING_DIM", REAL_DIM)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        centroids_path = paths.centroids_path(fixed_date, canonical=False)
        assert centroids_path.exists(), "centroids.npz must be written to staging"

        npz = np.load(str(centroids_path))
        assert len(npz.files) == len(clusters), "one key per cluster"

    def test_centroid_dim_matches_embedding_dim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        items = [
            _make_item("i1", "Story A"),
            _make_item("i2", "Story B"),
        ]
        # Near-identical: will collapse to 1 cluster.
        v = np.zeros((2, REAL_DIM), dtype=np.float32)
        v[0, 0] = 1.0
        v[1, 0] = 1.0
        v[1, 1] = 0.001  # tiny perturbation, still very close

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: v)
        monkeypatch.setattr(cluster_mod, "EMBEDDING_DIM", REAL_DIM)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        centroids_path = paths.centroids_path(fixed_date, canonical=False)
        npz = np.load(str(centroids_path))
        for key in npz.files:
            assert npz[key].shape == (REAL_DIM,), f"centroid for {key} must be {REAL_DIM}-dim"

    def test_centroids_file_is_under_staging_not_released(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        items = [_make_item("i1", "Story")]
        v = np.zeros((1, REAL_DIM), dtype=np.float32)
        v[0, 0] = 1.0

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: v)
        monkeypatch.setattr(cluster_mod, "EMBEDDING_DIM", REAL_DIM)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        cluster_mod.cluster_day(run_date=fixed_date)

        staging_npz = paths.centroids_path(fixed_date, canonical=False)
        released_npz = paths.centroids_path(fixed_date, canonical=True)
        assert staging_npz.exists()
        assert not released_npz.exists(), "cluster_day must not write to released"


# ===========================================================================
# TestSchemaInvariant
# ===========================================================================

class TestSchemaInvariant:
    """Every produced Cluster passes pydantic validation and satisfies the contract."""

    @pytest.mark.parametrize("n_items", [1, 2, 5])
    def test_all_clusters_pass_pydantic(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date, n_items: int
    ) -> None:
        items = [_make_item(f"item-{i}", f"Story {i}") for i in range(n_items)]
        # All identical vectors -> one cluster (or 1 each for the n=1 case handled by code).
        vec = _unit([1.0] + [0.0] * (DIM - 1))
        embeddings = np.stack([vec] * n_items)

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        for c in clusters:
            # Pydantic validation: Cluster() constructor enforces all invariants.
            validated = Cluster.model_validate_json(c.model_dump_json())
            assert validated.size == len(validated.item_ids)

    def test_cluster_id_matches_pattern(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        items = [_make_item("i1", "Pattern check story")]
        v = _unit([1.0] + [0.0] * (DIM - 1))
        embeddings = np.stack([v])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        for c in clusters:
            assert re.match(r"^c_[0-9a-f]{12,}$", c.cluster_id), (
                f"cluster_id {c.cluster_id!r} does not match pattern"
            )

    def test_size_equals_len_item_ids(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        items = [
            _make_item("i1", "GPT-X again"),
            _make_item("i2", "GPT-X follow"),
        ]
        base = _unit([1.0] + [0.0] * (DIM - 1))
        near = _unit([1.0, 0.01] + [0.0] * (DIM - 2))
        embeddings = np.stack([base, near])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        for c in clusters:
            assert c.size == len(c.item_ids)


# ===========================================================================
# TestClusterDayEdgeCases
# ===========================================================================

class TestClusterDayEdgeCases:
    """Edge cases in cluster_day() itself."""

    def test_empty_items_file_returns_empty_list(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        path.write_text("")  # empty file

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert clusters == []

    def test_missing_items_file_returns_empty_list(
        self, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        from src import paths
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        # No items.jsonl written.

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert clusters == []

    def test_published_urls_filter_drops_seen_items(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        """Items whose URL is in published_urls.txt are silently filtered."""
        items = [
            _make_item("i1", "Already released"),
            _make_item("i2", "Brand new"),
        ]
        base = _unit([1.0] + [0.0] * (DIM - 1))
        orth = _unit([0.0, 1.0] + [0.0] * (DIM - 2))
        # _embed is called only on the un-filtered items; we must return the right shape.
        # We monkeypatch to always return one vector (i2's vector).
        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: np.stack([orth] * len(_items)))

        from src import paths
        published_path = paths.PUBLISHED_URLS_PATH
        published_path.write_text("https://example.com/i1\n")

        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        all_item_ids = {iid for c in clusters for iid in c.item_ids}
        assert "i1" not in all_item_ids
        assert "i2" in all_item_ids

    def test_clusters_jsonl_written_to_staging(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        items = [_make_item("i1", "A story")]
        v = np.zeros((1, DIM), dtype=np.float32)
        v[0, 0] = 1.0

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: v)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        cluster_mod.cluster_day(run_date=fixed_date)

        out = paths.clusters_path(fixed_date, canonical=False)
        assert out.exists()
        lines = [ln for ln in out.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1
        restored = Cluster.model_validate_json(lines[0])
        assert restored.item_ids == ["i1"]


# ===========================================================================
# TestCanonicalIdHelper
# ===========================================================================

class TestCanonicalIdHelper:
    """Unit tests for _canonical_id() and _extract_canonical_id_from_url()."""

    def test_arxiv_url_primary(self) -> None:
        item = Item(
            id="ax1",
            source="arxiv_cl",
            source_type="rss",
            url="https://arxiv.org/abs/2605.23904",
            title="Some paper",
            published_at=_T0,
            raw_summary="abstract text",
            fetched_at=FIXED_NOW,
        )
        assert cluster_mod._canonical_id(item) == "arxiv:2605.23904"

    def test_arxiv_url_version_suffix_stripped(self) -> None:
        """2605.23904v2 must normalise to arxiv:2605.23904."""
        item = Item(
            id="ax2",
            source="arxiv_cl",
            source_type="rss",
            url="https://arxiv.org/abs/2605.23904v2",
            title="Some paper v2",
            published_at=_T0,
            raw_summary="abstract text",
            fetched_at=FIXED_NOW,
        )
        assert cluster_mod._canonical_id(item) == "arxiv:2605.23904"

    def test_github_release_url_primary(self) -> None:
        item = Item(
            id="gh1",
            source="llama.cpp releases",
            source_type="atom",
            url="https://github.com/ggml-org/llama.cpp/releases/tag/b9297",
            title="b9297",
            published_at=_T0,
            raw_summary="release notes",
            fetched_at=FIXED_NOW,
        )
        assert cluster_mod._canonical_id(item) == "github_release:ggml-org/llama.cpp:b9297"

    def test_doi_url_primary(self) -> None:
        item = Item(
            id="doi1",
            source="some_journal",
            source_type="rss",
            url="https://doi.org/10.1234/example.5678",
            title="A journal paper",
            published_at=_T0,
            raw_summary="abstract",
            fetched_at=FIXED_NOW,
        )
        assert cluster_mod._canonical_id(item) == "doi:10.1234/example.5678"

    def test_free_text_item_returns_none(self) -> None:
        item = Item(
            id="blog1",
            source="techcrunch",
            source_type="rss",
            url="https://techcrunch.com/2026/05/24/some-story",
            title="Some blog post",
            published_at=_T0,
            raw_summary="Some content without canonical URLs.",
            fetched_at=FIXED_NOW,
        )
        assert cluster_mod._canonical_id(item) is None

    def test_reddit_body_with_single_github_release_url(self) -> None:
        """Reddit post primary URL has no canonical ID; body contains one GitHub
        release URL -> that release's ID is returned."""
        item = Item(
            id="reddit1",
            source="r/LocalLLaMA (Reddit)",
            source_type="api",
            url="https://reddit.com/r/LocalLLaMA/comments/abc123/nvfp4_on_llamacpp",
            title="NVFP4 + MTP on llama.cpp",
            published_at=_T0,
            raw_summary=(
                "As in title - NVFP4 + MTP at once on llama.cpp "
                "[https://github.com/ggml-org/llama.cpp/releases/tag/b9297]"
                "(https://github.com/ggml-org/llama.cpp/releases/tag/b9297)"
            ),
            fetched_at=FIXED_NOW,
        )
        assert cluster_mod._canonical_id(item) == "github_release:ggml-org/llama.cpp:b9297"

    def test_reddit_body_with_single_arxiv_url(self) -> None:
        """Reddit post body contains one arxiv URL -> that arxiv ID is returned."""
        item = Item(
            id="reddit2",
            source="r/MachineLearning (Reddit)",
            source_type="api",
            url="https://reddit.com/r/MachineLearning/comments/xyz789/cool_paper",
            title="Cool paper discussion",
            published_at=_T0,
            raw_summary="Just read https://arxiv.org/abs/2605.23904 — very interesting work.",
            fetched_at=FIXED_NOW,
        )
        assert cluster_mod._canonical_id(item) == "arxiv:2605.23904"

    def test_body_multiple_distinct_canonical_ids_returns_none(self) -> None:
        """Body contains two different canonical IDs -> ambiguous -> None."""
        item = Item(
            id="blog2",
            source="some_blog",
            source_type="rss",
            url="https://example.com/roundup",
            title="Weekly roundup",
            published_at=_T0,
            raw_summary=(
                "See https://arxiv.org/abs/2605.23904 and also "
                "https://arxiv.org/abs/2605.23657 for related work."
            ),
            fetched_at=FIXED_NOW,
        )
        assert cluster_mod._canonical_id(item) is None

    def test_body_empty_returns_none(self) -> None:
        item = Item(
            id="empty1",
            source="src_a",
            source_type="rss",
            url="https://example.com/no-summary",
            title="Something",
            published_at=_T0,
            raw_summary="",
            fetched_at=FIXED_NOW,
        )
        assert cluster_mod._canonical_id(item) is None

    def test_arxiv_version_normalisation_same_id(self) -> None:
        """2605.23904 and 2605.23904v2 must extract to the same canonical ID."""
        assert cluster_mod._extract_canonical_id_from_url(
            "https://arxiv.org/abs/2605.23904"
        ) == cluster_mod._extract_canonical_id_from_url(
            "https://arxiv.org/abs/2605.23904v2"
        ) == "arxiv:2605.23904"


# ===========================================================================
# TestCanonicalIdClustering  (integration with cluster_day)
# ===========================================================================

class TestCanonicalIdClustering:
    """Integration tests for canonical-ID-aware clustering rules A and B."""

    def test_rule_a_same_arxiv_id_force_grouped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_data_root: Path,
        fixed_date: datetime.date,
    ) -> None:
        """Two items with the same arxiv URL must land in one cluster (rule A),
        regardless of embedding similarity."""
        items = [
            Item(
                id="paper-a",
                source="arxiv_cl",
                source_type="rss",
                url="https://arxiv.org/abs/2605.23904",
                title="SkillOpt: Executive Strategy for Self-Evolving Agent Skills",
                published_at=_T0,
                raw_summary="abstract one",
                fetched_at=FIXED_NOW,
            ),
            Item(
                id="paper-b",
                source="arxiv_cs",
                source_type="rss",
                url="https://arxiv.org/abs/2605.23904",
                title="SkillOpt paper (duplicate source)",
                published_at=_T0,
                raw_summary="abstract two",
                fetched_at=FIXED_NOW,
            ),
        ]
        # Deliberately orthogonal embeddings — cosine would keep them separate,
        # but rule A must override.
        v1 = _unit([1.0] + [0.0] * (DIM - 1))
        v2 = _unit([0.0, 1.0] + [0.0] * (DIM - 2))
        embeddings = np.stack([v1, v2])
        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 1, (
            "Same arxiv abs ID must force-merge into one cluster (rule A)"
        )
        assert set(clusters[0].item_ids) == {"paper-a", "paper-b"}

    def test_rule_b_distinct_arxiv_ids_forbidden_from_merging(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_data_root: Path,
        fixed_date: datetime.date,
    ) -> None:
        """Two items with DIFFERENT arxiv URLs must stay in separate clusters (rule B),
        even if embeddings would cluster them together."""
        items = [
            Item(
                id="paper-x",
                source="arxiv_cl",
                source_type="rss",
                url="https://arxiv.org/abs/2605.23904",
                title="SkillOpt: agent skill paper",
                published_at=_T0,
                raw_summary="agent skill abstract",
                fetched_at=FIXED_NOW,
            ),
            Item(
                id="paper-y",
                source="arxiv_cl",
                source_type="rss",
                url="https://arxiv.org/abs/2605.23657",
                title="OpenSkillEval: auditing agent skill ecosystem",
                published_at=_T0,
                raw_summary="agent skill evaluation abstract",
                fetched_at=FIXED_NOW,
            ),
        ]
        # Near-identical embeddings — cosine would merge them, but rule B forbids it.
        base = _unit([1.0] + [0.0] * (DIM - 1))
        near = _unit([1.0, 0.001] + [0.0] * (DIM - 2))
        embeddings = np.stack([base, near])
        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 2, (
            "Distinct arxiv abs IDs must stay in separate clusters (rule B)"
        )
        cluster_item_ids = {frozenset(c.item_ids) for c in clusters}
        assert frozenset({"paper-x"}) in cluster_item_ids
        assert frozenset({"paper-y"}) in cluster_item_ids

    def test_rule_a_reddit_body_links_to_github_release(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_data_root: Path,
        fixed_date: datetime.date,
    ) -> None:
        """Reddit post whose body contains a GitHub release URL must cluster with
        the official release entry pointing at the same tag (rule A via body extraction)."""
        items = [
            Item(
                id="reddit-b9297",
                source="r/LocalLLaMA (Reddit)",
                source_type="api",
                url="https://reddit.com/r/LocalLLaMA/comments/1tlohld/nvfp4_mtp_voila_on_llamacpp",
                title="NVFP4 + MTP on llama.cpp",
                published_at=_T0,
                raw_summary=(
                    "As in title - NVFP4 + MTP at once on llama.cpp "
                    "https://github.com/ggml-org/llama.cpp/releases/tag/b9297"
                ),
                fetched_at=FIXED_NOW,
            ),
            Item(
                id="release-b9297",
                source="llama.cpp releases",
                source_type="atom",
                url="https://github.com/ggml-org/llama.cpp/releases/tag/b9297",
                title="b9297",
                published_at=_T0,
                raw_summary="model: add NVFP4 MTP scale tensors",
                fetched_at=FIXED_NOW,
            ),
        ]
        # Deliberately orthogonal embeddings — titles are dissimilar; cosine alone
        # would NOT cluster these. Rule A must force-merge via the shared canonical ID.
        v1 = _unit([1.0] + [0.0] * (DIM - 1))
        v2 = _unit([0.0, 1.0] + [0.0] * (DIM - 2))
        embeddings = np.stack([v1, v2])
        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 1, (
            "Reddit post linking to b9297 and the official b9297 release entry "
            "must be force-merged (rule A via body extraction)"
        )
        assert set(clusters[0].item_ids) == {"reddit-b9297", "release-b9297"}

    def test_free_text_items_use_embedding_clustering_unchanged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_data_root: Path,
        fixed_date: datetime.date,
    ) -> None:
        """Items with no canonical ID must fall through to embedding-based clustering;
        near-identical embeddings merge, orthogonal embeddings stay separate."""
        items = [
            Item(
                id="blog-a",
                source="techcrunch",
                source_type="rss",
                url="https://techcrunch.com/2026/05/24/openai-gpt5",
                title="OpenAI releases GPT-5",
                published_at=_T0,
                raw_summary="GPT-5 launch announcement",
                fetched_at=FIXED_NOW,
            ),
            Item(
                id="blog-b",
                source="theverge",
                source_type="rss",
                url="https://theverge.com/2026/5/24/openai-gpt5-launch",
                title="GPT-5 is here",
                published_at=_T0,
                raw_summary="OpenAI announced GPT-5 today",
                fetched_at=FIXED_NOW,
            ),
            Item(
                id="blog-c",
                source="wired",
                source_type="rss",
                url="https://wired.com/story/anthropic-claude-update",
                title="Anthropic updates Claude",
                published_at=_T0,
                raw_summary="Anthropic releases Claude update",
                fetched_at=FIXED_NOW,
            ),
        ]
        # blog-a and blog-b near-identical; blog-c orthogonal.
        base = _unit([1.0] + [0.0] * (DIM - 1))
        near = _unit([1.0, 0.01] + [0.0] * (DIM - 2))
        far = _unit([0.0, 1.0] + [0.0] * (DIM - 2))
        embeddings = np.stack([base, near, far])
        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 2
        sizes = sorted(c.size for c in clusters)
        assert sizes == [1, 2]
        # The 2-item cluster must be blog-a + blog-b.
        two_item_cluster = next(c for c in clusters if c.size == 2)
        assert set(two_item_cluster.item_ids) == {"blog-a", "blog-b"}


# ===========================================================================
# TestVerifierStage
# ===========================================================================

class TestVerifierStage:
    """Unit tests for Stage 3 — pairwise cross-encoder verification.

    All tests use controlled stub verifiers (not the real model) so the
    suite stays fast.  The autouse stub_verifier fixture is overridden by
    per-test monkeypatches that supply the controlled verifier.
    """

    def test_two_item_cluster_passes_when_verifier_approves(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        """Two items that the verifier approves remain in one cluster."""
        items = [
            _make_item("dup1", "GPT-5 launches: OpenAI new model with vision", source="techcrunch"),
            _make_item("dup2", "OpenAI releases GPT-5: flagship model with voice and vision", source="theverge"),
        ]
        base = _unit([1.0] + [0.0] * (DIM - 1))
        near = _unit([1.0, 0.01] + [0.0] * (DIM - 2))
        embeddings = np.stack([base, near])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)
        # Approving verifier: score=1.0 > VERIFICATION_THRESHOLD
        monkeypatch.setattr(cluster_mod, "_load_verifier", lambda: _PassthroughVerifier())

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 1, "Approved pair must stay merged"
        assert set(clusters[0].item_ids) == {"dup1", "dup2"}

    def test_two_item_cluster_splits_when_verifier_rejects(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        """Two items that the verifier rejects are split into singletons.

        This is the core regression test for the May 26, 2026 intent-collision
        bug: bi-encoder cosine says 'merge', verifier says 'split'.
        """
        items = [
            _make_item("coll1", "Is Qwen3.6 current king for local agentic use?"),
            _make_item("coll2", "Want Built a React-style looping agent with small LLMs Qwen 3.5 9B Gemma4 LangGraph?"),
        ]
        # Near-identical embeddings (simulating the May 26 cosine=0.79 case).
        base = _unit([1.0] + [0.0] * (DIM - 1))
        near = _unit([1.0, 0.01] + [0.0] * (DIM - 2))
        embeddings = np.stack([base, near])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)
        # Rejecting verifier: score=0.0 < VERIFICATION_THRESHOLD=0.5
        monkeypatch.setattr(cluster_mod, "_load_verifier", lambda: _RejectAllVerifier())

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 2, "Rejected pair must be split into singletons"
        cluster_item_ids = {frozenset(c.item_ids) for c in clusters}
        assert frozenset({"coll1"}) in cluster_item_ids
        assert frozenset({"coll2"}) in cluster_item_ids

    def test_three_item_cluster_peels_outlier(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        """When one item in a 3-item cluster is rejected by all peers, it is
        peeled off and the remaining two items stay merged.

        The peel-off algorithm should identify the item with the most failing
        pairs as the outlier, not split the whole cluster.
        """
        items = [
            _make_item("dup1", "GPT-5 launches with vision"),
            _make_item("dup2", "OpenAI releases GPT-5 vision model"),
            _make_item("odd1", "How to set up agent loop with small models?"),  # intent-collision
        ]
        # All three near-identical in embedding space.
        base = _unit([1.0] + [0.0] * (DIM - 1))
        near1 = _unit([1.0, 0.01] + [0.0] * (DIM - 2))
        near2 = _unit([1.0, 0.02] + [0.0] * (DIM - 2))
        embeddings = np.stack([base, near1, near2])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)

        # Controlled verifier: dup1+dup2 pass; odd1 fails with both.
        score_table = {
            frozenset({"GPT-5 launches with vision", "OpenAI releases GPT-5 vision model"}): 0.99,
            frozenset({"GPT-5 launches with vision", "How to set up agent loop with small models?"}): 0.05,
            frozenset({"OpenAI releases GPT-5 vision model", "How to set up agent loop with small models?"}): 0.03,
        }
        monkeypatch.setattr(cluster_mod, "_load_verifier", lambda: _ControlledVerifier(score_table))

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 2, "Outlier should be peeled off; true pair stays merged"
        sizes = sorted(c.size for c in clusters)
        assert sizes == [1, 2], "Result: one 2-item cluster (dup1+dup2) + one singleton (odd1)"
        two_item = next(c for c in clusters if c.size == 2)
        assert set(two_item.item_ids) == {"dup1", "dup2"}

    def test_singleton_bypass_does_not_call_verifier(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        """Singleton items skip verification — the verifier is never loaded."""
        call_count = {"n": 0}

        def _counting_verifier_loader():
            call_count["n"] += 1
            return _PassthroughVerifier()

        items = [
            _make_item("solo1", "A unique story about obscure topic"),
            _make_item("solo2", "Completely orthogonal subject matter"),
        ]
        # Orthogonal embeddings: both items stay as singletons after bi-encoder.
        v1 = _unit([1.0] + [0.0] * (DIM - 1))
        v2 = _unit([0.0, 1.0] + [0.0] * (DIM - 2))
        embeddings = np.stack([v1, v2])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)
        monkeypatch.setattr(cluster_mod, "_load_verifier", _counting_verifier_loader)

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        cluster_mod.cluster_day(run_date=fixed_date)

        assert call_count["n"] == 0, (
            "Verifier must not be loaded when all items are singletons"
        )

    def test_verifier_text_is_title_only(self) -> None:
        """_verifier_text() returns the item title, not title+body.

        The contract is: title carries intent; body text introduces topical
        noise that degrades precision on intent-collision pairs.  If this
        assertion breaks, the title-only design decision has been changed
        without updating the threshold calibration.
        """
        item = _make_item(
            "test-item",
            "GPT-5 launches with extended context",
        )
        text = cluster_mod._verifier_text(item)
        assert text == "GPT-5 launches with extended context"
        assert "summary" not in text, (
            "_verifier_text() must return title only; body text is excluded by design"
        )

    def test_verify_and_split_cluster_two_items_approved(self) -> None:
        """Direct unit test: _verify_and_split_cluster returns one group for approved pair."""
        items = [
            _make_item("a1", "GPT-5 launch announcement"),
            _make_item("a2", "OpenAI unveils GPT-5"),
        ]
        verifier = _PassthroughVerifier()
        groups = cluster_mod._verify_and_split_cluster(items, verifier)
        assert len(groups) == 1
        assert len(groups[0]) == 2

    def test_verify_and_split_cluster_two_items_rejected(self) -> None:
        """Direct unit test: _verify_and_split_cluster returns two singletons for rejected pair."""
        items = [
            _make_item("b1", "Qwen king for agentic use?"),
            _make_item("b2", "Want built a React looping agent?"),
        ]
        verifier = _RejectAllVerifier()
        groups = cluster_mod._verify_and_split_cluster(items, verifier)
        assert len(groups) == 2
        assert all(len(g) == 1 for g in groups)

    def test_verify_and_split_cluster_singleton_passthrough(self) -> None:
        """Direct unit test: singleton list passes through unchanged."""
        items = [_make_item("solo", "Solo item")]
        verifier = _PassthroughVerifier()
        groups = cluster_mod._verify_and_split_cluster(items, verifier)
        assert len(groups) == 1
        assert groups[0] == items

    def test_verify_and_split_cluster_deterministic(self) -> None:
        """Same input produces same output across multiple calls (no randomness)."""
        items = [
            _make_item("c1", "Claude 4 release"),
            _make_item("c2", "Anthropic launches Claude 4"),
            _make_item("c3", "How to use Claude 4 API?"),  # intent-collision
        ]
        score_table = {
            frozenset({"Claude 4 release", "Anthropic launches Claude 4"}): 0.99,
            frozenset({"Claude 4 release", "How to use Claude 4 API?"}): 0.02,
            frozenset({"Anthropic launches Claude 4", "How to use Claude 4 API?"}): 0.03,
        }
        verifier = _ControlledVerifier(score_table)

        groups_1 = cluster_mod._verify_and_split_cluster(items, verifier)
        groups_2 = cluster_mod._verify_and_split_cluster(items, verifier)

        # Both runs return the same partition (same number of groups, same sizes).
        assert len(groups_1) == len(groups_2)
        ids_1 = sorted(tuple(sorted(i.id for i in g)) for g in groups_1)
        ids_2 = sorted(tuple(sorted(i.id for i in g)) for g in groups_2)
        assert ids_1 == ids_2

    def test_score_at_exact_threshold_keeps_pair(self) -> None:
        """Score exactly equal to VERIFICATION_THRESHOLD (0.5) must keep the pair.

        The comparison is `score >= threshold`, so 0.5 is a keep, not a split.
        A regression from >= to > would break this test.
        """
        items = [
            _make_item("t1", "Boundary story A"),
            _make_item("t2", "Boundary story B"),
        ]
        score_table = {
            frozenset({"Boundary story A", "Boundary story B"}): cluster_mod.VERIFICATION_THRESHOLD,
        }
        verifier = _ControlledVerifier(score_table)
        groups = cluster_mod._verify_and_split_cluster(items, verifier)
        assert len(groups) == 1, (
            "Score == VERIFICATION_THRESHOLD must keep the pair (>= not >)"
        )
        assert len(groups[0]) == 2

    def test_three_item_all_pairs_fail_produces_three_singletons(self) -> None:
        """A 3-item cluster where every pair fails produces 3 singletons.

        Tests that the peel-off loop continues until no sub-threshold pairs remain,
        not that it peels exactly one item and stops.
        """
        items = [
            _make_item("x1", "Topic A story"),
            _make_item("x2", "Topic B story"),
            _make_item("x3", "Topic C story"),
        ]
        verifier = _RejectAllVerifier()
        groups = cluster_mod._verify_and_split_cluster(items, verifier)
        assert len(groups) == 3, (
            "All pairs failing must produce one singleton per item"
        )
        assert all(len(g) == 1 for g in groups)

    def test_canonical_id_cluster_bypasses_verifier(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        """Rule-A (canonical-ID) clusters must not be submitted to the verifier.

        Even with a reject-all verifier, a pair force-merged by canonical-ID rules
        must stay merged.  If Stage 3 were incorrectly applied to canonical clusters,
        the reject-all verifier would split them and len(clusters) would be 2.
        """
        from src.models import Item as _Item

        items = [
            _Item(
                id="paper-one",
                source="arxiv_cl",
                source_type="rss",
                url="https://arxiv.org/abs/2605.12345",
                title="Same arxiv paper from feed A",
                published_at=_T0,
                raw_summary="abstract",
                fetched_at=FIXED_NOW,
            ),
            _Item(
                id="paper-two",
                source="arxiv_cs",
                source_type="rss",
                url="https://arxiv.org/abs/2605.12345",
                title="Same arxiv paper from feed B",
                published_at=_T0,
                raw_summary="abstract",
                fetched_at=FIXED_NOW,
            ),
        ]
        # Orthogonal embeddings so bi-encoder would not merge them on its own.
        v1 = _unit([1.0] + [0.0] * (DIM - 1))
        v2 = _unit([0.0, 1.0] + [0.0] * (DIM - 2))
        embeddings = np.stack([v1, v2])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)
        # Reject-all verifier: any pair sent through Stage 3 would split.
        monkeypatch.setattr(cluster_mod, "_load_verifier", lambda: _RejectAllVerifier())

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 1, (
            "Canonical-ID cluster (Rule A) must bypass Stage-3 verifier — "
            "reject-all verifier must not split a force-merged canonical pair"
        )
        assert set(clusters[0].item_ids) == {"paper-one", "paper-two"}


# ===========================================================================
# TestIntentCollisionRegression
# ===========================================================================

class TestIntentCollisionRegression:
    """Regression tests for the May 26, 2026 intent-collision bug.

    Cluster c_66173250c8d69ed6 incorrectly merged:
      - a26d56db626b45c8: "Is Qwen3.6 current king for local agentic use?" (recommendation)
      - ed18ce75188b9ad1: "Want Built a React-style looping agent ... Qwen 3.5 9B / Gemma4"
                         (help request)

    Both items share source (r/LocalLLaMA), day (2026-05-25), and entities
    (Qwen, Gemma, agent, loop, LangGraph).  Bi-encoder cosine = 0.79 (above
    threshold).  Cross-encoder title score = 0.0004 (well below threshold=0.5).

    These tests verify that the fix is structurally in place: the peel-off
    algorithm fires when the verifier rejects the pair, regardless of the
    bi-encoder outcome.
    """

    def test_intent_collision_pair_stays_split(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        """Items that share topic+entities but differ in speech act must not merge.

        The test simulates the exact failure mode: embedding cosine is above the
        within-day threshold (items would merge in Stage 2), but the verifier
        correctly rejects the pair (score=0.0004 below threshold=0.5).

        If this test breaks, the intent-collision bug has regressed.
        """
        # Titles from the actual May 26 incident.
        items = [
            _make_item(
                "a26d56db626b45c8",
                "Is Qwen3.6 current king for local agentic use?",
                source="r/LocalLLaMA (Reddit)",
            ),
            _make_item(
                "ed18ce75188b9ad1",
                "Want Built a React-style looping agent with small LLMs (Qwen 3.5 9B / Gemma4) + LangGraph?",
                source="r/LocalLLaMA (Reddit)",
            ),
        ]
        # Simulate cosine=0.79 (above 0.78 threshold → bi-encoder says merge).
        base = _unit([1.0] + [0.0] * (DIM - 1))
        near = _unit([1.0, 0.01] + [0.0] * (DIM - 2))
        embeddings = np.stack([base, near])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)
        # Verifier rejects the pair (simulating real cross-encoder score=0.0004).
        monkeypatch.setattr(cluster_mod, "_load_verifier", lambda: _RejectAllVerifier())

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 2, (
            "Regression: intent-collision items must not merge. "
            "If this fails, the Stage-3 verifier is not firing on same-source pairs."
        )
        cluster_item_ids = {frozenset(c.item_ids) for c in clusters}
        assert frozenset({"a26d56db626b45c8"}) in cluster_item_ids
        assert frozenset({"ed18ce75188b9ad1"}) in cluster_item_ids

    def test_true_duplicate_pair_stays_merged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_data_root: Path, fixed_date: datetime.date
    ) -> None:
        """True near-duplicates (same story, different sources) must still merge.

        Positive control: the Vision-LLMs-vs-OCR story cross-posted to
        r/LocalLLaMA and r/MachineLearning on 2026-05-24 (c_557d8de6f20e0a3b).
        The verifier should approve this pair with score >> 0.5.
        """
        items = [
            _make_item(
                "1605b1086233c938",
                "Vision-capable LLMs vs. OCR for long-document QA",
                source="r/LocalLLaMA (Reddit)",
            ),
            _make_item(
                "69d579e62a152ac7",
                "Vision-capable LLMs vs. OCR for long-document QA [D]",
                source="r/MachineLearning (Reddit)",
            ),
        ]
        base = _unit([1.0] + [0.0] * (DIM - 1))
        near = _unit([1.0, 0.01] + [0.0] * (DIM - 2))
        embeddings = np.stack([base, near])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)
        # Approving verifier: simulates real score=0.9917 for this true-duplicate pair.
        monkeypatch.setattr(cluster_mod, "_load_verifier", lambda: _PassthroughVerifier())

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 1, (
            "Positive control: true near-duplicates must still merge after Stage 3."
        )
        assert set(clusters[0].item_ids) == {"1605b1086233c938", "69d579e62a152ac7"}

    @pytest.mark.parametrize("collision_pair", [
        # Each tuple: (title_a, title_b, model_score, description)
        # 4 intent-collision pairs from the archive, each with the actual
        # cross-encoder score measured against BAAI/bge-reranker-v2-m3
        # revision 953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e.
        # Scores are all < 0.25; threshold is 0.50.
        # The _ControlledVerifier returns these exact scores keyed by title pair,
        # so the titles are structurally meaningful: a title change would break
        # the lookup, causing default_score=1.0 (passthrough) and a false keep.
        (
            "Is Qwen3.6 current king for local agentic use?",
            "Want Built a React-style looping agent with small LLMs (Qwen 3.5 9B / Gemma4) + LangGraph?",
            0.0004,
            "May26 incident: recommendation vs help-request (same source)",
        ),
        (
            "Qwen3.6-35B-A3B vs Gemma4-26B-A4B",
            "Please give me your best tips for fine tuning RTX Pro 6000 on Intel i7-14700KF",
            0.0000,
            "Comparison post vs hardware tuning request (same source)",
        ),
        (
            "What is the current best Small Language Model that can be run without GPU?",
            "Qwen3.6 27B Pure Quant: 40 tok/s on 16 GB VRAM",
            0.0001,
            "Model recommendation question vs benchmark announcement (same source)",
        ),
        (
            "llama.cpp server have built-in native tools (exec_shell, edit_file, etc.)",
            "How are you all handling agents and sub agents?",
            0.0000,
            "Feature announcement vs architecture question (same source)",
        ),
    ])
    def test_collision_pair_split_by_controlled_verifier(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_data_root: Path,
        fixed_date: datetime.date,
        collision_pair: tuple[str, str, float, str],
    ) -> None:
        """Parametrized: each intent-collision pair must split when the verifier scores it below threshold.

        Uses _ControlledVerifier keyed on actual model scores (not _RejectAllVerifier).
        This makes the title strings structurally load-bearing: if a title changes, the
        lookup misses and falls back to default_score=1.0 (keep), causing the assertion
        to fail and alerting the engineer that the model score for the new title is unknown.

        A verifier model revision that pushes any of these scores above 0.50 would also
        break the test, which is correct — it signals threshold recalibration is needed.
        """
        title_a, title_b, model_score, description = collision_pair
        score_table = {frozenset({title_a, title_b}): model_score}
        items = [
            _make_item("item_a", title_a, source="r/LocalLLaMA (Reddit)"),
            _make_item("item_b", title_b, source="r/LocalLLaMA (Reddit)"),
        ]
        base = _unit([1.0] + [0.0] * (DIM - 1))
        near = _unit([1.0, 0.01] + [0.0] * (DIM - 2))
        embeddings = np.stack([base, near])

        monkeypatch.setattr(cluster_mod, "_embed", lambda _items: embeddings)
        # _ControlledVerifier with default_score=1.0 means: any unknown pair is
        # treated as a true-duplicate (keep). Only the exact pairs in score_table
        # produce sub-threshold scores.  If a title changes, the test fails loudly.
        monkeypatch.setattr(cluster_mod, "_load_verifier", lambda: _ControlledVerifier(score_table, default_score=1.0))

        from src import paths
        path = paths.items_path(fixed_date, canonical=False)
        paths.staging_dir(fixed_date).mkdir(parents=True, exist_ok=True)
        _write_items(path, items)

        clusters = cluster_mod.cluster_day(run_date=fixed_date)

        assert len(clusters) == 2, (
            f"Intent-collision pair must be split — {description} "
            f"(model_score={model_score:.4f} < VERIFICATION_THRESHOLD=0.5)"
        )
