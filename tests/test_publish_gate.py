"""Unit tests for the publish gate (task #79).

`release_promote` calls `evals.run_evals.check_integrity(date, staging=True)`
before writing the canonical issue.json. Failing integrity refuses the
release with `StagingIntegrityFailure`. `force=True` bypasses and logs a
WARNING for audit.

These tests must monkeypatch BOTH `src.paths.DATA_ROOT` AND
`evals.run_evals.DATA_DIR` to the same tmp directory — the gate reads
through the eval module's constant, not the src.paths one.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path

import pytest

from src import paths as _paths
from src.models import (
    RUBRIC_WEIGHTS,
    Cluster,
    Issue,
    IssueSection,
    Item,
    RankedStory,
    SourceHealth,
    SourceHealthReport,
    SummaryBlock,
)
from src.render import (
    StagingIntegrityFailure,
    release_promote,
)
from tests.conftest import (
    FIXED_DATE,
    FIXED_EARLIER,
    FIXED_NOW,
    VALID_CLUSTER_ID,
    VALID_CLUSTER_ID_2,
)


# ---------------------------------------------------------------------------
# Fixture wiring — also monkeypatches evals.run_evals.DATA_DIR.
# ---------------------------------------------------------------------------


@pytest.fixture
def gated_tmp(
    tmp_data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Like `tmp_data_root` + `tmp_docs`, but also redirects the eval
    module's DATA_DIR so check_integrity reads the same tmp data the
    test wrote, not the real archive."""
    # docs/
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(_paths, "DOCS_ROOT", docs)
    monkeypatch.setattr(_paths, "STAGING_HTML_DIR", docs / "staging")
    monkeypatch.setattr(_paths, "RELEASED_HTML_DIR", docs / "released")
    monkeypatch.setattr(_paths, "DOCS_INDEX", docs / "index.html")
    # eval module's DATA_DIR — load lazily so we don't trigger imports
    # the rest of the test would skip.
    from evals import run_evals as _eh
    monkeypatch.setattr(_eh, "DATA_DIR", tmp_data_root)
    return tmp_data_root


# ---------------------------------------------------------------------------
# Issue + staging builders — full enough for check_integrity to evaluate.
# ---------------------------------------------------------------------------


def _make_block(story_id: str, signal: str | None = None) -> SummaryBlock:
    return SummaryBlock(
        story_id=story_id,
        headline="A short consequence-led headline that fits the cap",
        summary=(
            "Forty-five-word summary that hits the deterministic length cap "
            "comfortably. Includes a specific number (42 percent), a trust "
            "flag (single source), and a decision-tied close: run it before "
            "you commit to a vendor on the next architecture review."
        ),
        source_urls=[f"https://example.test/{story_id}"],
        signal=signal,
    )


def _hands_on_section(n: int) -> IssueSection:
    """Hands-On section with `n` distinct stories."""
    blocks = [
        _make_block(story_id=f"c_{i:012x}", signal="try")
        for i in range(1, n + 1)
    ]
    return IssueSection(name="hands_on", stories=blocks)


def _issue_with_hands_on(n: int) -> Issue:
    return Issue(
        date=FIXED_DATE,
        pulse=IssueSection(name="pulse", stories=[_make_block(VALID_CLUSTER_ID, "try")]),
        sections=[
            IssueSection(name="big_picture", stories=[]),
            _hands_on_section(n),
            IssueSection(name="on_the_radar", stories=[]),
        ],
        generated_at=FIXED_NOW,
        prompt_versions={"rank": "v1", "summarise": "v1"},
    )


def _write_full_staging(date: _dt.date, issue: Issue) -> None:
    """Write a staging dir with valid source_health.json so the gate can run.

    The check needs realistic source health (5 sources, all fired) to land
    above the 0.80 fire-rate floor — empty files would fail differently.
    """
    staging = _paths.staging_dir(date)
    staging.mkdir(parents=True, exist_ok=True)

    # issue.json — the artifact under test
    (staging / "issue.json").write_text(
        issue.model_dump_json(indent=2), encoding="utf-8"
    )

    # source_health.json — 5 sources, all fired (rate = 1.0, > 0.80 floor)
    sources = [
        SourceHealth(
            source=f"src_{i}", fired=True, items_in=10, items_kept=8, latency_ms=200,
        )
        for i in range(5)
    ]
    report = SourceHealthReport(
        run_started_at=FIXED_EARLIER,
        run_finished_at=FIXED_NOW,
        sources=sources,
    )
    (staging / "source_health.json").write_text(
        report.model_dump_json(indent=2), encoding="utf-8"
    )

    # Build the full referential chain: issue → ranked → cluster → item.
    # check_integrity validates each hop; every story_id must trace back.
    all_story_ids: list[str] = [s.story_id for s in issue.pulse.stories]
    for section in issue.sections:
        all_story_ids.extend(s.story_id for s in section.stories)

    breakdown = {k: 50 for k in RUBRIC_WEIGHTS}  # weighted = 50
    item_lines: list[str] = []
    cluster_lines: list[str] = []
    ranked_lines: list[str] = []

    for idx, sid in enumerate(all_story_ids):
        item_id = f"i{idx:015x}"
        item = Item(
            id=item_id,
            source="example_blog",
            source_type="rss",
            url=f"https://example.test/post-{idx}",
            title="Fixture item title for publish-gate test",
            published_at=FIXED_EARLIER,
            raw_summary="raw",
            fetched_at=FIXED_NOW,
        )
        cluster = Cluster(
            cluster_id=sid,
            item_ids=[item_id],
            canonical_title="Fixture cluster title",
            sources=["example_blog"],
            earliest_published=FIXED_EARLIER,
            size=1,
        )
        ranked = RankedStory(
            cluster_id=sid,
            score=50,
            breakdown=breakdown,
            audience_tags=["hands_on"],
            rationale="Fixture rationale for publish-gate test.",
            tier="on_the_radar",
            prompt_version="v1",
        )
        item_lines.append(item.model_dump_json())
        cluster_lines.append(cluster.model_dump_json())
        ranked_lines.append(ranked.model_dump_json())

    (staging / "items.jsonl").write_text(
        "\n".join(item_lines) + ("\n" if item_lines else ""), encoding="utf-8"
    )
    (staging / "clusters.jsonl").write_text(
        "\n".join(cluster_lines) + ("\n" if cluster_lines else ""), encoding="utf-8"
    )
    (staging / "ranked.jsonl").write_text(
        "\n".join(ranked_lines) + ("\n" if ranked_lines else ""), encoding="utf-8"
    )
    (staging / "embeddings").mkdir(exist_ok=True)
    (staging / "embeddings" / "centroids.npz").write_bytes(b"")


# ===========================================================================
# Gate refuses thin staging (the load-bearing regression — Issue #2.1 case)
# ===========================================================================


class TestPublishGateRefusesThin:
    def test_refuses_when_hands_on_below_three(self, gated_tmp: Path) -> None:
        """The 2026-05-24 thin-release regression: only 1 hands_on story."""
        _write_full_staging(FIXED_DATE, _issue_with_hands_on(1))
        with pytest.raises(StagingIntegrityFailure) as exc_info:
            release_promote(FIXED_DATE)
        # The failure message must name the failing assertion so an
        # operator reading stderr knows what's wrong.
        assert any("hands_on" in f for f in exc_info.value.failures)
        # And the date must be preserved on the exception for logging.
        assert exc_info.value.date == FIXED_DATE

    def test_canonical_not_written_when_gate_fires(self, gated_tmp: Path) -> None:
        """The whole point of the gate: NO canonical write on failure."""
        _write_full_staging(FIXED_DATE, _issue_with_hands_on(1))
        canonical = _paths.released_dir(FIXED_DATE) / "issue.json"
        with pytest.raises(StagingIntegrityFailure):
            release_promote(FIXED_DATE)
        assert not canonical.exists()


# ===========================================================================
# --force bypasses the gate but logs the bypassed assertions
# ===========================================================================


class TestPublishGateForceBypass:
    def test_force_proceeds_despite_failures(self, gated_tmp: Path) -> None:
        """force=True is the audited escape hatch — release proceeds."""
        _write_full_staging(FIXED_DATE, _issue_with_hands_on(1))
        # Should NOT raise:
        result = release_promote(FIXED_DATE, force=True)
        assert result.issue_number is not None
        # Canonical was written:
        assert (_paths.released_dir(FIXED_DATE) / "issue.json").exists()

    def test_force_logs_warning_with_bypassed_assertions(
        self, gated_tmp: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Audit trail: every bypassed assertion lands in the log at WARNING."""
        _write_full_staging(FIXED_DATE, _issue_with_hands_on(1))
        with caplog.at_level(logging.WARNING):
            release_promote(FIXED_DATE, force=True)
        # The WARNING line(s) must include the word force AND the
        # failing-assertion text — operators reading audit logs need both.
        warning_text = "\n".join(r.message for r in caplog.records if r.levelno == logging.WARNING)
        assert "force" in warning_text.lower()
        assert "hands_on" in warning_text


# ===========================================================================
# Gate passes silently for healthy staging (no false positives)
# ===========================================================================


class TestPublishGatePassesHealthy:
    def test_healthy_staging_releases_cleanly(self, gated_tmp: Path) -> None:
        """A well-formed 3-hands_on issue passes the gate and releases."""
        _write_full_staging(FIXED_DATE, _issue_with_hands_on(3))
        result = release_promote(FIXED_DATE)
        assert result.issue_number == 1  # first release of a fresh date

    def test_healthy_staging_emits_no_force_warnings(
        self, gated_tmp: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No --force warnings should fire on a clean release."""
        _write_full_staging(FIXED_DATE, _issue_with_hands_on(3))
        with caplog.at_level(logging.WARNING):
            release_promote(FIXED_DATE)
        # No "force" or "BYPASSING" tokens in any WARNING message
        for record in caplog.records:
            if record.levelno >= logging.WARNING:
                assert "BYPASSING" not in record.message
                assert "--force" not in record.message
