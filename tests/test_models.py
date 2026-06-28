"""Unit tests for src/models.py -- pydantic data contracts.

Scope: the invariants WE added in model_validators + the small set of
load-bearing field rules the rest of the pipeline trusts. We deliberately
do NOT re-assert pydantic's own enforcement of Literal / Field(ge=...) /
Field(pattern=...) / extra='forbid' / dict[str, str] -- pydantic owns those.
See tests/CONVENTIONS.md sec. 2.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.models import (
    RUBRIC_WEIGHTS,
    Cluster,
    ClaimVerdict,
    Issue,
    IssueSection,
    RankedStory,
    SourceHealth,
    SourceHealthReport,
    StoryVerification,
    SummaryBlock,
    VerificationReport,
)
from tests.conftest import (
    FIXED_EARLIER,
    FIXED_NOW,
    VALID_CLUSTER_ID,
)


# ===========================================================================
# Cluster -- size invariant is the only custom validator on this model.
# ===========================================================================

class TestCluster:
    def test_size_must_match_item_ids(self) -> None:
        """size is duplicated for fast reads; must match item_ids length.
        This is the model_validator we own; pydantic would otherwise accept
        the mismatch."""
        with pytest.raises(ValidationError, match="size"):
            Cluster(
                cluster_id=VALID_CLUSTER_ID,
                item_ids=["a", "b", "c"],
                canonical_title="t",
                sources=["s"],
                earliest_published=FIXED_NOW,
                size=5,  # mismatch
            )

    def test_prior_coverage_ref_alias_accepts_old_field_name(self) -> None:
        """Task #88 migration safety net. The v1 schema serialised the
        field as ``cross_time_ref``; the v2 rename to ``prior_coverage_ref``
        kept that old name as a pydantic validation alias so already-
        released archive files (e.g. ``data/released/2026-05-24/issue.json``)
        continue to parse.

        Without this alias, every old issue.json on disk would break the
        Issue parse path -- which is what the migration is designed to
        avoid. Pinning the contract here so a future "cleanup" of the
        alias surfaces as a test failure first, not as a broken archive.
        """
        import json

        # Serialised payload built with the OLD field name, as it appears
        # in the released archive.
        raw = json.dumps({
            "schema_version": 1,
            "cluster_id": VALID_CLUSTER_ID,
            "item_ids": ["i1"],
            "canonical_title": "t",
            "sources": ["s"],
            "earliest_published": FIXED_NOW.isoformat(),
            "size": 1,
            "cross_time_ref": "c_ffffffffffff",
        })

        cluster = Cluster.model_validate_json(raw)

        # The NEW field name is populated from the alias.
        assert cluster.prior_coverage_ref == "c_ffffffffffff"


# ===========================================================================
# RankedStory -- breakdown + weighted score invariants are ours.
# ===========================================================================

class TestRankedStory:
    def test_breakdown_keys_must_match_rubric_exactly(self) -> None:
        """Extra key in breakdown -- rejected by our model_validator."""
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
        """Missing rubric key -- rejected by our model_validator."""
        partial = {k: 50 for k in list(RUBRIC_WEIGHTS)[:-1]}  # drop one
        with pytest.raises(ValidationError, match="breakdown keys"):
            RankedStory(
                cluster_id=VALID_CLUSTER_ID,
                score=50, breakdown=partial,
                audience_tags=["hands_on"], rationale="r", tier="cut",
                prompt_version="v1",
            )

    def test_score_must_equal_weighted_breakdown(self) -> None:
        """The platonic load-bearing invariant: score is RECOMPUTED from
        breakdown * RUBRIC_WEIGHTS and rejected if the LLM lies. CONVENTIONS
        sec. 2 cites this test as the worked example."""
        breakdown = {k: 100 for k in RUBRIC_WEIGHTS}  # weighted = 100
        with pytest.raises(ValidationError, match="weighted sum"):
            RankedStory(
                cluster_id=VALID_CLUSTER_ID,
                score=50,  # wrong
                breakdown=breakdown,
                audience_tags=["hands_on"], rationale="r", tier="hands_on",
                prompt_version="v1",
            )

    def test_score_accepts_exact_weighted_match(self) -> None:
        """Pin the happy path: matching score passes. Without this, a
        validator typo could silently break everything."""
        breakdown = {k: 100 for k in RUBRIC_WEIGHTS}
        rs = RankedStory(
            cluster_id=VALID_CLUSTER_ID,
            score=100, breakdown=breakdown,
            audience_tags=["hands_on"], rationale="r", tier="hands_on",
            prompt_version="v1",
        )
        assert rs.score == 100


class TestRankedStoryScoreProperty:
    """Property test: for any breakdown of integer sub-scores, the validator
    accepts iff `score == sum(weight * sub_score) // 100` (rounding mirrors
    the source). Hand-crafted cases hit one combination; this hits many."""

    @pytest.mark.parametrize("sig,hou,bpr,fsi,fm", [
        (0, 0, 0, 0, 0),
        (100, 100, 100, 100, 100),
        (70, 80, 50, 40, 60),     # the fixture's breakdown
        (35, 35, 35, 35, 35),
        (99, 1, 50, 50, 50),
        (25, 75, 50, 25, 75),
    ])
    def test_score_invariant_holds_for_arbitrary_breakdowns(
        self, sig: int, hou: int, bpr: int, fsi: int, fm: int,
    ) -> None:
        breakdown = {
            "significance": sig,
            "hands_on_utility": hou,
            "big_picture_relevance": bpr,
            "financial_services_impact": fsi,
            "freshness_momentum": fm,
        }
        # Mirror the formula in RankedStory's validator (round-half-even).
        weighted = round(
            sum((RUBRIC_WEIGHTS[k] / 100.0) * v for k, v in breakdown.items())
        )
        # Build with the matching score; must pass.
        rs = RankedStory(
            cluster_id=VALID_CLUSTER_ID,
            score=weighted, breakdown=breakdown,
            audience_tags=["hands_on"], rationale="r", tier="cut",
            prompt_version="v1",
        )
        assert rs.score == weighted
        # Build with an off-by-one score; must fail with the weighted-sum
        # message (not the Field(ge=0, le=100) bound, so step away from the
        # edge).
        bad_score = weighted - 1 if weighted >= 1 else weighted + 1
        with pytest.raises(ValidationError, match="weighted sum"):
            RankedStory(
                cluster_id=VALID_CLUSTER_ID,
                score=bad_score, breakdown=breakdown,
                audience_tags=["hands_on"], rationale="r", tier="cut",
                prompt_version="v1",
            )


# ===========================================================================
# IssueSection -- pulse-must-have-exactly-one-story is the editorial invariant.
# ===========================================================================

class TestIssueSection:
    def test_pulse_rejects_zero_stories(self) -> None:
        """The Pulse is THE story of the day. Zero is not allowed."""
        with pytest.raises(ValidationError, match="exactly 1 story"):
            IssueSection(name="pulse", stories=[])

    def test_pulse_rejects_more_than_one_story(self, summary_block: SummaryBlock) -> None:
        """The Pulse is THE story of the day. Two is not allowed."""
        with pytest.raises(ValidationError, match="exactly 1 story"):
            IssueSection(name="pulse", stories=[summary_block, summary_block])

    @pytest.mark.parametrize("name", ["on_the_radar", "big_picture", "hands_on"])
    def test_non_pulse_sections_may_be_empty(self, name: str) -> None:
        """On a slow day, non-pulse sections may legitimately be empty.
        Pinned because the renderer relies on it."""
        IssueSection(name=name, stories=[])


# ===========================================================================
# Issue -- pulse / sections / prompt_versions custom validators.
# ===========================================================================

class TestIssue:
    def test_pulse_field_must_be_named_pulse(
        self, summary_block: SummaryBlock, issue: Issue
    ) -> None:
        """Issue.pulse must hold a section with name='pulse'. Our
        field_validator; without it, a renderer trap is possible."""
        with pytest.raises(ValidationError, match="name='pulse'"):
            Issue.model_validate({
                **issue.model_dump(mode="json"),
                "pulse": {"name": "big_picture", "stories": [summary_block.model_dump(mode="json")]},
            })

    def test_sections_must_not_contain_pulse(
        self, issue: Issue, summary_block: SummaryBlock
    ) -> None:
        """Pulse lives in its own Issue.pulse field; duplicating it in
        sections would double-render and is a known renderer trap."""
        with pytest.raises(ValidationError, match="must not contain a section with name='pulse'"):
            Issue.model_validate({
                **issue.model_dump(mode="json"),
                "sections": [{"name": "pulse", "stories": [summary_block.model_dump(mode="json")]}],
            })

    def test_prompt_versions_must_include_rank_and_summarise(self, issue: Issue) -> None:
        """Audit invariant (risk register #6): every issue records which
        rank + summarise prompt produced it. We enforce the minimum."""
        with pytest.raises(ValidationError, match="missing="):
            Issue.model_validate({
                **issue.model_dump(mode="json"),
                "prompt_versions": {"rank": "v1"},  # missing summarise
            })


# ===========================================================================
# SourceHealth -- two model_validators we own.
# ===========================================================================

class TestSourceHealth:
    def test_kept_must_be_le_in(self) -> None:
        """items_kept > items_in is structurally impossible; our validator
        catches a real class of counting bugs (off-by-one, wrong accumulator)."""
        with pytest.raises(ValidationError, match="items_kept"):
            SourceHealth(
                source="s", fired=True,
                items_in=5, items_kept=10,
                latency_ms=100,
            )

    def test_missed_reason_required_when_not_fired(self) -> None:
        """If a fetch didn't fire, the engineer must say why -- enforced by
        our model_validator. Otherwise dead feeds vanish silently."""
        with pytest.raises(ValidationError, match="missed_reason is required"):
            SourceHealth(
                source="s", fired=False,
                items_in=0, items_kept=0, latency_ms=0,
            )

    def test_fired_true_with_zero_items_is_ok(self) -> None:
        """A source can fire successfully but return no new items (already-
        seen, all old). missed_reason must NOT be set in this state."""
        sh = SourceHealth(
            source="s", fired=True,
            items_in=0, items_kept=0, latency_ms=120,
        )
        assert sh.fired is True
        assert sh.missed_reason is None


class TestSourceHealthReport:
    def test_finish_must_be_after_start(self, source_health_healthy: SourceHealth) -> None:
        """Negative wall-clock would mean clock skew; our validator rejects
        rather than letting downstream eval math go nonsensical."""
        with pytest.raises(ValidationError, match="run_finished_at"):
            SourceHealthReport(
                run_started_at=FIXED_NOW,
                run_finished_at=FIXED_EARLIER,
                sources=[source_health_healthy],
            )


# ===========================================================================
# Cross-model invariants.
# ===========================================================================

class TestRubricWeights:
    def test_weights_sum_to_100(self) -> None:
        """RUBRIC_WEIGHTS is mirrored from config/rubric.yaml; if the sum
        drifts from 100, every RankedStory.score check goes wrong."""
        assert sum(RUBRIC_WEIGHTS.values()) == 100


# ===========================================================================
# Issue.display_number -- format "#N" or "#N.M" for the rendered identifier.
# This is the public-facing identifier seen in the masthead + archive
# listing; the integer registry (issue_number) is unchanged. Added v5
# (2026-05-24, task #76).
# ===========================================================================

class TestIssueDisplayNumber:
    def _issue(self, *, issue_number, revision=0) -> Issue:
        from tests.conftest import FIXED_DATE, FIXED_NOW
        return Issue(
            issue_number=issue_number,
            revision=revision,
            date=FIXED_DATE,
            pulse=IssueSection(
                name="pulse",
                stories=[SummaryBlock(
                    story_id=VALID_CLUSTER_ID,
                    headline="H",
                    summary="A summary sentence.",
                    source_urls=["https://example.com/"],
                )],
            ),
            sections=[],
            generated_at=FIXED_NOW,
            prompt_versions={"rank": "v1", "summarise": "v1"},
        )

    def test_staging_issue_returns_none(self) -> None:
        """issue_number=None (staging) -> display_number=None so templates
        fall back to the 'Preview / staging' branch."""
        issue = self._issue(issue_number=None, revision=0)
        assert issue.display_number is None

    def test_first_release_returns_integer_string(self) -> None:
        """revision=0 (first release) -> 'N' with no decimal."""
        assert self._issue(issue_number=2).display_number == "2"
        assert self._issue(issue_number=42).display_number == "42"

    def test_revision_bump_returns_dotted_form(self) -> None:
        """revision>0 -> 'N.M'. The motivating case for task #76."""
        assert self._issue(issue_number=2, revision=1).display_number == "2.1"
        assert self._issue(issue_number=2, revision=2).display_number == "2.2"
        assert self._issue(issue_number=42, revision=7).display_number == "42.7"

    def test_revision_defaults_to_zero(self) -> None:
        """Backwards-compat: old issue.json files without the `revision`
        field load with revision=0 via the default."""
        issue = self._issue(issue_number=1)  # revision omitted
        assert issue.revision == 0
        assert issue.display_number == "1"

    def test_revision_must_be_non_negative(self) -> None:
        """Field(ge=0) -- pydantic enforces, but pin the contract."""
        with pytest.raises(ValidationError):
            self._issue(issue_number=1, revision=-1)


# ===========================================================================
# ClaimVerdict -- contradicted-requires-source_span is OUR validator.
# ===========================================================================

class TestClaimVerdict:
    """The only custom logic on ClaimVerdict is _contradicted_requires_source_span.
    That validator is the contract-level backstop: verify.py downgrades span-less
    contradictions before construction, but a hand-authored or mis-serialised
    record must not sneak through. We own this invariant; pydantic does not."""

    def _valid(self, **overrides) -> dict:
        base = dict(
            claim="The model runs on an RTX 3090",
            verdict="supported",
            location="body",
        )
        base.update(overrides)
        return base

    def test_contradicted_with_source_span_is_accepted(self) -> None:
        cv = ClaimVerdict(**self._valid(
            verdict="contradicted",
            source_span="the source says it needs a data-centre GPU",
        ))
        assert cv.verdict == "contradicted"

    def test_contradicted_without_source_span_raises(self) -> None:
        """No source_span on a contradicted verdict must be rejected.
        If this validator is removed, the test fails on the validation pass."""
        with pytest.raises(ValidationError, match="source_span"):
            ClaimVerdict(**self._valid(
                verdict="contradicted",
                source_span="",  # empty -- no contradicting quote
            ))

    def test_unsupported_without_source_span_is_accepted(self) -> None:
        """unsupported legitimately has an empty source_span -- the fact is
        absent from the source, so there is nothing to quote. Must not raise."""
        cv = ClaimVerdict(**self._valid(verdict="unsupported", source_span=""))
        assert cv.verdict == "unsupported"

    def test_unverifiable_without_source_span_is_accepted(self) -> None:
        cv = ClaimVerdict(**self._valid(verdict="unverifiable", source_span=""))
        assert cv.verdict == "unverifiable"


# ===========================================================================
# StoryVerification -- rollup-must-agree-with-claims is OUR validator.
# ===========================================================================

class TestStoryVerification:
    """The _rollups_match_claims validator owns three booleans: has_contradiction,
    has_unsupported, headline_flagged. A writer that sets any of them inconsistently
    with the claim list must be rejected here -- the renderer trusts these flags
    without re-scanning claims."""

    _PROMPT_VERSION = "v0.4"
    _STORY_ID = "c_aaaaaaaaaaaa"

    def _make_claim(self, verdict: str, location: str = "body", source_span: str = "") -> ClaimVerdict:
        return ClaimVerdict(
            claim="some claim",
            verdict=verdict,
            location=location,
            source_span=source_span if verdict == "contradicted" else "",
            # contradicted needs a source_span to clear ClaimVerdict's own validator
            **({} if verdict != "contradicted" else {"source_span": source_span or "some quote"}),
        )

    def _sv(self, claims, **overrides) -> StoryVerification:
        defaults = dict(
            story_id=self._STORY_ID,
            prompt_version=self._PROMPT_VERSION,
            claims=claims,
            has_contradiction=any(c.verdict == "contradicted" for c in claims),
            has_unsupported=any(c.verdict == "unsupported" for c in claims),
            headline_flagged=any(
                c.location == "headline" and c.verdict in {"contradicted", "unsupported"}
                for c in claims
            ),
        )
        defaults.update(overrides)
        return StoryVerification(**defaults)

    def test_consistent_rollups_accepted(self) -> None:
        claim = ClaimVerdict(
            claim="runs on RTX 3090", verdict="contradicted",
            location="headline", source_span="needs a data-centre GPU",
        )
        sv = self._sv([claim])
        assert sv.has_contradiction is True
        assert sv.headline_flagged is True

    def test_has_contradiction_disagrees_with_claims_raises(self) -> None:
        """If the claim list has no contradictions but has_contradiction=True,
        the validator must reject it."""
        claim = ClaimVerdict(claim="ok", verdict="supported", location="body")
        with pytest.raises(ValidationError, match="has_contradiction"):
            self._sv([claim], has_contradiction=True)

    def test_has_unsupported_disagrees_with_claims_raises(self) -> None:
        claim = ClaimVerdict(claim="ok", verdict="supported", location="body")
        with pytest.raises(ValidationError, match="has_unsupported"):
            self._sv([claim], has_unsupported=True)

    def test_headline_flagged_disagrees_with_claims_raises(self) -> None:
        # A body contradiction does NOT set headline_flagged; a caller claiming
        # otherwise must be rejected.
        claim = ClaimVerdict(
            claim="body contradiction", verdict="contradicted",
            location="body", source_span="evidence",
        )
        with pytest.raises(ValidationError, match="headline_flagged"):
            self._sv([claim], headline_flagged=True)

    def test_empty_claims_all_rollups_false(self) -> None:
        """An empty claim list (e.g. per-story isolation fallback) must
        produce all-False rollups -- not a flag."""
        sv = self._sv([])
        assert sv.has_contradiction is False
        assert sv.has_unsupported is False
        assert sv.headline_flagged is False


# ===========================================================================
# VerificationReport -- unavailable-has-no-stories is OUR validator.
# ===========================================================================

class TestVerificationReport:
    """_unavailable_has_no_stories: an unavailable report MUST carry an empty
    stories list. The reader branches on verdict to decide whether to show
    flags; a non-empty unavailable list would be an ambiguous mix of signals."""

    _PROMPT_VERSION = "v0.4"

    def _sv(self, story_id: str = "c_aaaaaaaaaaaa") -> StoryVerification:
        return StoryVerification(
            story_id=story_id,
            prompt_version=self._PROMPT_VERSION,
            claims=[],
            has_contradiction=False,
            has_unsupported=False,
            headline_flagged=False,
        )

    def test_clean_report_with_stories_accepted(self) -> None:
        import datetime as _dt
        report = VerificationReport(
            generated_at=_dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=_dt.timezone.utc),
            prompt_version=self._PROMPT_VERSION,
            verdict="clean",
            stories=[self._sv()],
        )
        assert report.verdict == "clean"

    def test_unavailable_with_stories_raises(self) -> None:
        import datetime as _dt
        with pytest.raises(ValidationError, match="unavailable"):
            VerificationReport(
                generated_at=_dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=_dt.timezone.utc),
                prompt_version=self._PROMPT_VERSION,
                verdict="unavailable",
                stories=[self._sv()],   # must be empty for unavailable
            )

    def test_unavailable_with_empty_stories_accepted(self) -> None:
        import datetime as _dt
        report = VerificationReport(
            generated_at=_dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=_dt.timezone.utc),
            prompt_version=self._PROMPT_VERSION,
            verdict="unavailable",
            stories=[],
        )
        assert report.verdict == "unavailable"
        assert report.stories == []
