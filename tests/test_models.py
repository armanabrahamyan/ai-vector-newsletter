"""Unit tests for src/models.py — the pydantic data contracts.

Coverage: round-trip JSON serialisation, every model_validator + field_validator
invariant, and the small set of edge cases that the rest of the pipeline
trusts to fail loud rather than smuggle bad shapes downstream.
"""
from __future__ import annotations

import datetime as _dt

import pytest
from pydantic import ValidationError

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
from tests.conftest import (
    FIXED_DATE,
    FIXED_EARLIER,
    FIXED_NOW,
    UTC,
    VALID_CLUSTER_ID,
    VALID_CLUSTER_ID_2,
)


# ===========================================================================
# Item
# ===========================================================================

class TestItem:
    def test_round_trip(self, item: Item) -> None:
        """Serialised → parsed back should equal the original."""
        payload = item.model_dump_json()
        restored = Item.model_validate_json(payload)
        assert restored == item

    def test_rejects_unknown_fields(self) -> None:
        """extra='forbid' should reject extra keys (locks the contract)."""
        with pytest.raises(ValidationError):
            Item.model_validate({
                "id": "x", "source": "s", "source_type": "rss",
                "url": "https://x.test/", "title": "t",
                "published_at": FIXED_NOW.isoformat(),
                "raw_summary": "", "fetched_at": FIXED_NOW.isoformat(),
                "unknown_field": "boom",
            })

    @pytest.mark.parametrize("invalid_type", ["xml", "json", "scrape", ""])
    def test_rejects_invalid_source_type(self, invalid_type: str, item: Item) -> None:
        with pytest.raises(ValidationError):
            item.model_copy(update={"source_type": invalid_type})  # type: ignore[arg-type]
            # model_copy doesn't validate by default; force re-validation:
            Item.model_validate({**item.model_dump(mode="json"), "source_type": invalid_type})

    def test_trust_weight_range(self, item: Item) -> None:
        for ok in (1, 3, 5):
            item.model_copy(update={"trust_weight": ok})
        for bad in (0, 6, -1, 100):
            with pytest.raises(ValidationError):
                Item.model_validate({**item.model_dump(mode="json"), "trust_weight": bad})

    def test_language_pattern(self, item: Item) -> None:
        # Valid: "en", "en-US", "fr-CA"
        for ok in ("en", "en-US", "fr-CA"):
            Item.model_validate({**item.model_dump(mode="json"), "language": ok})
        # Invalid: missing region with hyphen, uppercase lang, etc.
        for bad in ("EN", "english", "en-us", "en_US"):
            with pytest.raises(ValidationError):
                Item.model_validate({**item.model_dump(mode="json"), "language": bad})

    def test_extras_string_values_only(self, item: Item) -> None:
        """`extras` is dict[str, str] -- non-string values must reject so
        JSONL parse stays cheap and unambiguous."""
        payload = item.model_dump(mode="json")
        payload["extras"] = {"points": 123}  # int value, should reject
        with pytest.raises(ValidationError):
            Item.model_validate(payload)

    def test_extras_roundtrips_when_populated(self, item: Item) -> None:
        with_extras = item.model_copy(update={"extras": {"points": "120", "comments": "33"}})
        restored = Item.model_validate_json(with_extras.model_dump_json())
        assert restored.extras == {"points": "120", "comments": "33"}


# ===========================================================================
# Cluster
# ===========================================================================

class TestCluster:
    def test_round_trip(self, cluster: Cluster) -> None:
        restored = Cluster.model_validate_json(cluster.model_dump_json())
        assert restored == cluster

    def test_size_must_match_item_ids(self) -> None:
        """The size/item_ids invariant catches a real class of bugs."""
        with pytest.raises(ValidationError, match="size"):
            Cluster(
                cluster_id=VALID_CLUSTER_ID,
                item_ids=["a", "b", "c"],
                canonical_title="t",
                sources=["s"],
                earliest_published=FIXED_NOW,
                size=5,  # mismatch
            )

    @pytest.mark.parametrize("bad_id", [
        "c_short",          # too short hex
        "c_GHIJKLMNOPQR",   # non-hex
        "x_aaaaaaaaaaaa",   # wrong prefix
        "c_aaaaaaaaaaa",    # 11 chars instead of 12+
        "",
    ])
    def test_cluster_id_pattern(self, bad_id: str) -> None:
        with pytest.raises(ValidationError):
            Cluster(
                cluster_id=bad_id,
                item_ids=["a"],
                canonical_title="t",
                sources=["s"],
                earliest_published=FIXED_NOW,
                size=1,
            )

    def test_cross_time_ref_validates_pattern(self) -> None:
        with pytest.raises(ValidationError):
            Cluster(
                cluster_id=VALID_CLUSTER_ID,
                item_ids=["a"],
                canonical_title="t",
                sources=["s"],
                earliest_published=FIXED_NOW,
                size=1,
                cross_time_ref="not-a-valid-id",
            )

    def test_cross_time_ref_accepts_valid_id(self) -> None:
        c = Cluster(
            cluster_id=VALID_CLUSTER_ID,
            item_ids=["a"],
            canonical_title="t",
            sources=["s"],
            earliest_published=FIXED_NOW,
            size=1,
            cross_time_ref=VALID_CLUSTER_ID_2,
        )
        assert c.cross_time_ref == VALID_CLUSTER_ID_2

    def test_round_trip_with_centroid_sidecar(self) -> None:
        """Round-trip when the optional embedding fields are populated --
        cluster.py will write these on real runs."""
        c = Cluster(
            cluster_id=VALID_CLUSTER_ID,
            item_ids=["a", "b"],
            canonical_title="t",
            sources=["s1", "s2"],
            earliest_published=FIXED_NOW,
            size=2,
            embedding_dim=768,
            centroid_ref="c_aaaaaaaaaaaa.npy",
        )
        restored = Cluster.model_validate_json(c.model_dump_json())
        assert restored == c
        assert restored.embedding_dim == 768
        assert restored.centroid_ref == "c_aaaaaaaaaaaa.npy"


# ===========================================================================
# RankedStory
# ===========================================================================

class TestRankedStory:
    def test_round_trip(self, ranked_story: RankedStory) -> None:
        restored = RankedStory.model_validate_json(ranked_story.model_dump_json())
        assert restored == ranked_story

    def test_breakdown_keys_must_match_rubric(self) -> None:
        """breakdown keys are pinned to RUBRIC_WEIGHTS keys exactly."""
        bad_breakdown = {k: 50 for k in RUBRIC_WEIGHTS}
        bad_breakdown["unknown_criterion"] = 50  # extra
        with pytest.raises(ValidationError, match="breakdown keys"):
            RankedStory(
                cluster_id=VALID_CLUSTER_ID,
                score=50, breakdown=bad_breakdown,
                audience_tags=["hands_on"], rationale="r", tier="cut",
                prompt_version="v1",
            )

    def test_breakdown_missing_key_rejected(self) -> None:
        partial = {k: 50 for k in list(RUBRIC_WEIGHTS)[:-1]}  # drop one
        with pytest.raises(ValidationError, match="breakdown keys"):
            RankedStory(
                cluster_id=VALID_CLUSTER_ID,
                score=50, breakdown=partial,
                audience_tags=["hands_on"], rationale="r", tier="cut",
                prompt_version="v1",
            )

    def test_score_must_equal_weighted_breakdown(self) -> None:
        """RUBRIC_WEIGHTS is the contract — score is recomputed, not trusted blindly."""
        breakdown = {k: 100 for k in RUBRIC_WEIGHTS}  # weighted = 100
        with pytest.raises(ValidationError, match="weighted sum"):
            RankedStory(
                cluster_id=VALID_CLUSTER_ID,
                score=50,  # wrong
                breakdown=breakdown,
                audience_tags=["hands_on"], rationale="r", tier="pulse",
                prompt_version="v1",
            )

    def test_score_accepts_exact_weighted_match(self) -> None:
        breakdown = {k: 100 for k in RUBRIC_WEIGHTS}
        rs = RankedStory(
            cluster_id=VALID_CLUSTER_ID,
            score=100, breakdown=breakdown,
            audience_tags=["hands_on"], rationale="r", tier="pulse",
            prompt_version="v1",
        )
        assert rs.score == 100

    def test_audience_tags_min_length(self) -> None:
        with pytest.raises(ValidationError):
            RankedStory(
                cluster_id=VALID_CLUSTER_ID,
                score=0, breakdown={k: 0 for k in RUBRIC_WEIGHTS},
                audience_tags=[], rationale="r", tier="cut",
                prompt_version="v1",
            )

    @pytest.mark.parametrize("bad_version", ["1", "1.0", "version-1", "v"])
    def test_prompt_version_pattern(self, bad_version: str) -> None:
        with pytest.raises(ValidationError):
            RankedStory(
                cluster_id=VALID_CLUSTER_ID,
                score=0, breakdown={k: 0 for k in RUBRIC_WEIGHTS},
                audience_tags=["hands_on"], rationale="r", tier="cut",
                prompt_version=bad_version,
            )

    @pytest.mark.parametrize("good_version", ["v1", "v1.2", "v10.20.30"])
    def test_prompt_version_accepts_valid(self, good_version: str) -> None:
        rs = RankedStory(
            cluster_id=VALID_CLUSTER_ID,
            score=0, breakdown={k: 0 for k in RUBRIC_WEIGHTS},
            audience_tags=["hands_on"], rationale="r", tier="cut",
            prompt_version=good_version,
        )
        assert rs.prompt_version == good_version


# ===========================================================================
# SummaryBlock
# ===========================================================================

class TestSummaryBlock:
    def test_round_trip(self, summary_block: SummaryBlock) -> None:
        restored = SummaryBlock.model_validate_json(summary_block.model_dump_json())
        assert restored == summary_block

    def test_source_urls_min_length(self) -> None:
        with pytest.raises(ValidationError):
            SummaryBlock(
                story_id=VALID_CLUSTER_ID,
                headline="h", summary="s", source_urls=[],
            )

    @pytest.mark.parametrize("signal", ["act", "try", "read", "watch", "discuss"])
    def test_signal_accepts_all_valid_values(self, signal: str) -> None:
        sb = SummaryBlock(
            story_id=VALID_CLUSTER_ID,
            headline="h", summary="s",
            source_urls=["https://example.com/"],
            signal=signal,  # type: ignore[arg-type]
        )
        assert sb.signal == signal

    def test_signal_optional(self) -> None:
        sb = SummaryBlock(
            story_id=VALID_CLUSTER_ID,
            headline="h", summary="s",
            source_urls=["https://example.com/"],
        )
        assert sb.signal is None

    def test_signal_rejects_unknown(self) -> None:
        with pytest.raises(ValidationError):
            SummaryBlock(
                story_id=VALID_CLUSTER_ID,
                headline="h", summary="s",
                source_urls=["https://example.com/"],
                signal="urgent",  # type: ignore[arg-type]
            )

    def test_cross_time_ref_round_trip(self) -> None:
        sb = SummaryBlock(
            story_id=VALID_CLUSTER_ID,
            headline="h", summary="s",
            source_urls=["https://example.com/"],
            cross_time_ref=VALID_CLUSTER_ID_2,
        )
        restored = SummaryBlock.model_validate_json(sb.model_dump_json())
        assert restored.cross_time_ref == VALID_CLUSTER_ID_2

    def test_headline_max_length(self) -> None:
        with pytest.raises(ValidationError):
            SummaryBlock(
                story_id=VALID_CLUSTER_ID,
                headline="x" * 201,  # cap is 200
                summary="s",
                source_urls=["https://example.com/"],
            )

    def test_summary_max_length(self) -> None:
        with pytest.raises(ValidationError):
            SummaryBlock(
                story_id=VALID_CLUSTER_ID,
                headline="h",
                summary="x" * 1201,  # cap is 1200
                source_urls=["https://example.com/"],
            )


# ===========================================================================
# IssueSection
# ===========================================================================

class TestIssueSection:
    def test_pulse_must_have_exactly_one_story(self, summary_block: SummaryBlock) -> None:
        """The pulse section is "the story of the day" — exactly one."""
        with pytest.raises(ValidationError, match="exactly 1 story"):
            IssueSection(name="pulse", stories=[])
        with pytest.raises(ValidationError, match="exactly 1 story"):
            IssueSection(name="pulse", stories=[summary_block, summary_block])

    def test_non_pulse_sections_may_be_empty(self) -> None:
        """On a slow day, on_the_radar may be empty."""
        IssueSection(name="on_the_radar", stories=[])
        IssueSection(name="big_picture", stories=[])
        IssueSection(name="hands_on", stories=[])

    def test_intro_lead_and_body_optional(self, summary_block: SummaryBlock) -> None:
        s = IssueSection(name="big_picture", stories=[summary_block])
        assert s.intro_lead is None
        assert s.intro_body is None

    def test_intro_lead_length_cap(self, summary_block: SummaryBlock) -> None:
        with pytest.raises(ValidationError):
            IssueSection(
                name="big_picture",
                stories=[summary_block],
                intro_lead="x" * 81,  # cap is 80
            )

    def test_intro_body_length_cap(self, summary_block: SummaryBlock) -> None:
        with pytest.raises(ValidationError):
            IssueSection(
                name="big_picture",
                stories=[summary_block],
                intro_body="x" * 401,  # cap is 400
            )


# ===========================================================================
# Issue
# ===========================================================================

class TestIssue:
    def test_round_trip(self, issue: Issue) -> None:
        restored = Issue.model_validate_json(issue.model_dump_json())
        assert restored == issue

    def test_issue_number_none_in_staging(self, issue: Issue) -> None:
        """Staging issues have issue_number=None; release assigns it."""
        assert issue.issue_number is None

    def test_pulse_field_must_be_named_pulse(self, summary_block: SummaryBlock, issue: Issue) -> None:
        with pytest.raises(ValidationError, match="name='pulse'"):
            issue.model_copy(update={
                "pulse": IssueSection(name="big_picture", stories=[summary_block])
            })
            # model_copy bypasses validation; force re-validation:
            Issue.model_validate({
                **issue.model_dump(mode="json"),
                "pulse": {"name": "big_picture", "stories": [summary_block.model_dump(mode="json")]},
            })

    def test_sections_must_not_contain_pulse(self, issue: Issue, summary_block: SummaryBlock) -> None:
        """Pulse lives in its own field; including it in sections is a renderer trap."""
        with pytest.raises(ValidationError, match="must not contain a section with name='pulse'"):
            Issue.model_validate({
                **issue.model_dump(mode="json"),
                "sections": [{"name": "pulse", "stories": [summary_block.model_dump(mode="json")]}],
            })

    def test_prompt_versions_must_include_rank_and_summarise(self, issue: Issue) -> None:
        with pytest.raises(ValidationError, match="missing="):
            Issue.model_validate({
                **issue.model_dump(mode="json"),
                "prompt_versions": {"rank": "v1"},  # missing summarise
            })

    def test_issue_number_must_be_positive(self, issue: Issue) -> None:
        with pytest.raises(ValidationError):
            Issue.model_validate({**issue.model_dump(mode="json"), "issue_number": 0})

    def test_notes_default_empty_and_capped(self, issue: Issue) -> None:
        assert issue.notes == ""
        with pytest.raises(ValidationError):
            Issue.model_validate({**issue.model_dump(mode="json"), "notes": "x" * 2001})

    def test_round_trip_with_issue_number_assigned(self, issue: Issue) -> None:
        """After release, Issue carries an int issue_number -- must round-trip."""
        released = issue.model_copy(update={"issue_number": 42})
        restored = Issue.model_validate_json(released.model_dump_json())
        assert restored.issue_number == 42
        assert restored == released


# ===========================================================================
# SourceHealth + SourceHealthReport
# ===========================================================================

class TestSourceHealth:
    def test_kept_must_be_le_in(self) -> None:
        with pytest.raises(ValidationError, match="items_kept"):
            SourceHealth(
                source="s", fired=True,
                items_in=5, items_kept=10,  # impossible
                latency_ms=100,
            )

    def test_missed_reason_required_when_not_fired(self) -> None:
        with pytest.raises(ValidationError, match="missed_reason is required"):
            SourceHealth(
                source="s", fired=False,
                items_in=0, items_kept=0, latency_ms=0,
            )

    def test_fired_true_with_zero_items_is_ok(self) -> None:
        """A source can fire successfully but return no new items."""
        sh = SourceHealth(
            source="s", fired=True,
            items_in=0, items_kept=0, latency_ms=120,
        )
        assert sh.fired is True
        assert sh.missed_reason is None

    @pytest.mark.parametrize("reason", [
        "timeout", "http_4xx", "http_5xx", "parse_error", "empty_feed", "disabled",
    ])
    def test_missed_reason_accepts_all_valid_tokens(self, reason: str) -> None:
        sh = SourceHealth(
            source="s", fired=False,
            items_in=0, items_kept=0, latency_ms=0,
            missed_reason=reason,  # type: ignore[arg-type]
        )
        assert sh.missed_reason == reason

    def test_missed_reason_rejects_unknown_token(self) -> None:
        with pytest.raises(ValidationError):
            SourceHealth(
                source="s", fired=False,
                items_in=0, items_kept=0, latency_ms=0,
                missed_reason="rate_limited",  # type: ignore[arg-type]
            )

    def test_round_trip(self, source_health_healthy: SourceHealth) -> None:
        restored = SourceHealth.model_validate_json(
            source_health_healthy.model_dump_json()
        )
        assert restored == source_health_healthy


class TestSourceHealthReport:
    def test_finish_must_be_after_start(self, source_health_healthy: SourceHealth) -> None:
        with pytest.raises(ValidationError, match="run_finished_at"):
            SourceHealthReport(
                run_started_at=FIXED_NOW,
                run_finished_at=FIXED_EARLIER,  # earlier than start
                sources=[source_health_healthy],
            )

    def test_empty_sources_allowed(self) -> None:
        """A no-op run is a valid (if useless) report."""
        SourceHealthReport(
            run_started_at=FIXED_EARLIER,
            run_finished_at=FIXED_NOW,
            sources=[],
        )

    def test_round_trip(self, source_health_report: SourceHealthReport) -> None:
        restored = SourceHealthReport.model_validate_json(
            source_health_report.model_dump_json()
        )
        assert restored == source_health_report


# ===========================================================================
# Cross-model invariants
# ===========================================================================

class TestRubricWeights:
    def test_weights_sum_to_100(self) -> None:
        """The rubric weights are a probability distribution over criteria."""
        assert sum(RUBRIC_WEIGHTS.values()) == 100
