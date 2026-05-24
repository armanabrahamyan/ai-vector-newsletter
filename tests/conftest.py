"""Shared pytest fixtures for the AI Vector test suite.

Conventions:
- Use frozen UTC datetimes — never `datetime.now()` in tests.
- Keep fixture data minimal and obviously synthetic (cluster IDs like
  c_aaaa..., URLs like https://example.com/...).
- Each fixture builds the smallest valid instance; tests override fields
  as needed for the case they're exercising.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from src.models import (
    Cluster,
    Issue,
    IssueSection,
    Item,
    RankedStory,
    SourceHealth,
    SourceHealthReport,
    SummaryBlock,
)


# ---------------------------------------------------------------------------
# Time constants — fixed so test output stays stable.
# ---------------------------------------------------------------------------

UTC = _dt.timezone.utc
FIXED_NOW = _dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)
FIXED_EARLIER = _dt.datetime(2026, 5, 24, 11, 0, 0, tzinfo=UTC)
FIXED_DATE = _dt.date(2026, 5, 24)

VALID_CLUSTER_ID = "c_" + "a" * 12
VALID_CLUSTER_ID_2 = "c_" + "b" * 12


# ---------------------------------------------------------------------------
# Model fixtures.
# ---------------------------------------------------------------------------

@pytest.fixture
def fixed_now() -> _dt.datetime:
    return FIXED_NOW


@pytest.fixture
def fixed_date() -> _dt.date:
    return FIXED_DATE


@pytest.fixture
def item() -> Item:
    return Item(
        id="entry-001",
        source="example_blog",
        source_type="rss",
        url="https://example.com/post-1",
        title="A small open model handles invoices on-device",
        published_at=FIXED_EARLIER,
        raw_summary="A 4B-param model extracts structured data without sending files to the cloud.",
        fetched_at=FIXED_NOW,
    )


@pytest.fixture
def make_item():
    """Factory fixture for downstream tests (cluster, rank) that need
    many Item instances with varied id/source/url. Defaults to a valid
    Item; override any field via kwargs.
    """
    def _make(**overrides) -> Item:
        defaults = dict(
            id="entry-001",
            source="example_blog",
            source_type="rss",
            url="https://example.com/post-1",
            title="A small open model handles invoices on-device",
            published_at=FIXED_EARLIER,
            raw_summary="raw",
            fetched_at=FIXED_NOW,
        )
        defaults.update(overrides)
        return Item(**defaults)
    return _make


@pytest.fixture
def cluster() -> Cluster:
    return Cluster(
        cluster_id=VALID_CLUSTER_ID,
        item_ids=["entry-001", "entry-002"],
        canonical_title="A small open model handles invoices on-device",
        sources=["example_blog", "reddit"],
        earliest_published=FIXED_EARLIER,
        size=2,
    )


@pytest.fixture
def ranked_story() -> RankedStory:
    breakdown = {
        "significance": 70,
        "hands_on_utility": 80,
        "big_picture_relevance": 50,
        "financial_services_impact": 40,
        "freshness_momentum": 60,
    }
    # Weighted: 0.30*70 + 0.25*80 + 0.20*50 + 0.15*40 + 0.10*60 = 63
    return RankedStory(
        cluster_id=VALID_CLUSTER_ID,
        score=63,
        breakdown=breakdown,
        audience_tags=["hands_on"],
        rationale="Practical, on-device, useful immediately for FS doc workflows.",
        tier="pulse",
        prompt_version="v1",
    )


@pytest.fixture
def summary_block() -> SummaryBlock:
    return SummaryBlock(
        story_id=VALID_CLUSTER_ID,
        headline="A small open model handles invoices on-device",
        summary="A 4B-param open model now extracts structured data from invoices without leaving the device.",
        source_urls=["https://example.com/post-1"],
        signal="try",
    )


@pytest.fixture
def pulse_section(summary_block: SummaryBlock) -> IssueSection:
    return IssueSection(name="pulse", stories=[summary_block])


@pytest.fixture
def issue(pulse_section: IssueSection) -> Issue:
    return Issue(
        date=FIXED_DATE,
        pulse=pulse_section,
        sections=[
            IssueSection(name="big_picture", stories=[]),
            IssueSection(name="hands_on", stories=[]),
            IssueSection(name="on_the_radar", stories=[]),
        ],
        generated_at=FIXED_NOW,
        prompt_versions={"rank": "v1", "summarise": "v1"},
    )


@pytest.fixture
def source_health_healthy() -> SourceHealth:
    return SourceHealth(
        source="example_blog",
        fired=True,
        items_in=10,
        items_kept=8,
        latency_ms=420,
    )


@pytest.fixture
def source_health_report(source_health_healthy: SourceHealth) -> SourceHealthReport:
    return SourceHealthReport(
        run_started_at=FIXED_EARLIER,
        run_finished_at=FIXED_NOW,
        sources=[source_health_healthy],
    )


# ---------------------------------------------------------------------------
# Path fixtures.
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override `paths.DATA_ROOT` to point at a per-test tmp dir.

    Tests that touch the filesystem should use this instead of the real
    `data/` tree so they don't pollute the repo or read released state.
    """
    from src import paths as _paths

    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(_paths, "DATA_ROOT", root)
    monkeypatch.setattr(_paths, "STAGING_ROOT", root / "staging")
    monkeypatch.setattr(_paths, "RELEASED_ROOT", root / "released")
    monkeypatch.setattr(_paths, "PUBLISHED_URLS_PATH", root / "published_urls.txt")
    return root
