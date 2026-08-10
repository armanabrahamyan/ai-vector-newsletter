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
    DigestBullet,
    Issue,
    IssueSection,
    RankedStory,
    ReviewTarget,
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
    VALID_CLUSTER_ID_2,
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


# ===========================================================================
# SummaryBlock.take -- the position line (schema v4, 2026-08-08).
# Structural rules only: stripped, non-empty if present, nullable. Form
# (word count, declarative mood) belongs to summarise.py + the reviewer.
# ===========================================================================

class TestSummaryBlockTake:
    def _block(self, **overrides: object) -> SummaryBlock:
        kwargs: dict = dict(
            story_id=VALID_CLUSTER_ID,
            headline="h",
            summary="s",
            source_urls=["https://example.com/a"],
        )
        kwargs.update(overrides)
        return SummaryBlock(**kwargs)

    def test_take_defaults_to_none(self) -> None:
        """Nullability is the backwards-compat contract: a block without a
        take (pre-take archive, or cut by the body-cap collision ladder)
        must construct and mean "no take", not fail."""
        assert self._block().take is None

    def test_take_round_trips_through_json(self) -> None:
        """The field must survive dump -> load unchanged; extra='forbid'
        means this test goes red if the field is ever removed while
        archives still carry it."""
        block = self._block(take="Position lines are speech acts, not summaries.")
        reloaded = SummaryBlock.model_validate_json(block.model_dump_json())
        assert reloaded.take == "Position lines are speech acts, not summaries."
        assert reloaded.schema_version == 5

    def test_take_is_stripped(self) -> None:
        """The before-validator strips; downstream renderers and the
        reviewer's verbatim-quote check must never see padding."""
        assert self._block(take="  edges matter  ").take == "edges matter"

    def test_whitespace_only_take_rejected(self) -> None:
        """Strip-then-min_length interplay (ours, not pydantic's default):
        a blank take is a writer bug, not an absent take -- absence is
        spelled None. Without _strip_take, '   ' would pass min_length=1."""
        with pytest.raises(ValidationError):
            self._block(take="   ")

    def test_take_route_defaults_to_none_and_round_trips(self) -> None:
        """SummaryBlock v5 (2026-08-09): the generation-route label is
        nullable (pre-route archives) and survives a JSON round trip."""
        assert self._block().take_route is None
        block = self._block(take="Edges matter now.", take_route="R2")
        reloaded = SummaryBlock.model_validate_json(block.model_dump_json())
        assert reloaded.take_route == "R2"

    def test_released_pre_take_issue_parses_with_take_none(self) -> None:
        """A real released issue.json written before the take existed
        (SummaryBlock schema_version 3) must validate under the v4 model
        with take=None on every block -- the whole point of the nullable
        contract. Mirrors the prior_coverage_ref alias test above: pin the
        archive-compat promise so a future 'cleanup' fails here first."""
        from pathlib import Path

        archive = Path("data/released/2026-07-11/issue.json")
        if not archive.exists():
            pytest.skip("data/released/2026-07-11/ not present in this environment")
        issue = Issue.model_validate_json(archive.read_text(encoding="utf-8"))
        blocks = list(issue.pulse.stories)
        for section in issue.sections:
            blocks.extend(section.stories)
        assert blocks, "released issue unexpectedly carries no stories"
        assert all(b.take is None for b in blocks)


# ===========================================================================
# ReviewTarget.field == "take" -- the reviewer must be able to point a
# finding at the position line (integrity requirement, 2026-08-08).
# ===========================================================================

class TestReviewTargetTakeField:
    def test_story_target_accepts_take(self) -> None:
        target = ReviewTarget(
            kind="story", story_id=VALID_CLUSTER_ID, field="take"
        )
        assert target.field == "take"

    def test_section_target_rejects_take(self) -> None:
        """The take is a story field; a section finding pointing at it
        cannot be resolved to text on disk and must be rejected at the
        boundary, same as a section finding pointing at 'headline'."""
        with pytest.raises(ValidationError, match="intro_lead"):
            ReviewTarget(kind="section", section="hands_on", field="take")


# ===========================================================================
# IssueSection.synthesis -- the merged section paragraph (schema v4,
# 2026-08-09 layout redesign). Structural rules + the migration-direction
# exclusivity invariant we own.
# ===========================================================================

class TestIssueSectionSynthesis:
    def test_synthesis_alone_accepted(self) -> None:
        section = IssueSection(
            name="hands_on", stories=[],
            synthesis="Verify before you adopt. The pattern is trust.",
        )
        assert section.synthesis == "Verify before you adopt. The pattern is trust."
        assert section.schema_version == 4

    def test_legacy_intro_pair_alone_still_accepted(self) -> None:
        """Every released issue with a Phase-B intro must keep parsing --
        the legacy pair is retained, not removed."""
        section = IssueSection(
            name="hands_on", stories=[],
            intro_lead="Bench before you budget.",
            intro_body="Two tools, one caveat.",
        )
        assert section.synthesis is None

    @pytest.mark.parametrize("legacy", [
        {"intro_lead": "Lead."},
        {"intro_body": "Body."},
    ])
    def test_synthesis_with_legacy_intro_rejected(self, legacy: dict) -> None:
        """Migration-direction invariant (ours): synthesis and the legacy
        pair are two competing framings for one render slot; a record
        carrying both is a writer bug. Without _synthesis_excludes_legacy_intros
        pydantic would accept it and the renderer would silently discard
        reviewed text."""
        with pytest.raises(ValidationError, match="superseded"):
            IssueSection(name="hands_on", stories=[], synthesis="S.", **legacy)

    def test_synthesis_is_stripped_and_blank_rejected(self) -> None:
        """Strip-then-min_length interplay (ours): absence is spelled None,
        never ''. Without _strip_synthesis, '   ' would pass min_length=1."""
        assert IssueSection(
            name="currents", stories=[], synthesis="  edges  "
        ).synthesis == "edges"
        with pytest.raises(ValidationError):
            IssueSection(name="currents", stories=[], synthesis="   ")


# ===========================================================================
# DigestBullet + Issue.digest -- the 30-second read (Issue schema v8,
# 2026-08-09). Structural rules + the provenance-resolution invariant.
# ===========================================================================

class TestDigestBullet:
    def test_lead_and_sentence_are_stripped_blank_rejected(self) -> None:
        bullet = DigestBullet(
            lead="  Agents run locally now  ",
            sentence="  The privacy calculus just shifted.  ",
            story_ids=[VALID_CLUSTER_ID],
        )
        assert bullet.lead == "Agents run locally now"
        assert bullet.sentence == "The privacy calculus just shifted."
        with pytest.raises(ValidationError):
            DigestBullet(lead="   ", sentence="s", story_ids=[VALID_CLUSTER_ID])

    def test_story_ids_must_be_non_empty(self) -> None:
        """Provenance is the contract: a bullet that cites no story cannot
        be verified against any excerpt."""
        with pytest.raises(ValidationError):
            DigestBullet(lead="l", sentence="s", story_ids=[])


class TestIssueDigest:
    def _bullet(self, story_id: str = VALID_CLUSTER_ID) -> dict:
        return {
            "lead": "Agents run locally now",
            "sentence": "The privacy calculus just shifted.",
            "story_ids": [story_id],
        }

    def test_digest_defaults_to_none(self, issue: Issue) -> None:
        """Nullability is the backwards-compat contract: every archived
        issue (and any digest-degraded day) means "no digest section",
        not a failure."""
        assert issue.digest is None

    def test_digest_with_resolvable_story_ids_accepted(self, issue: Issue) -> None:
        payload = issue.model_dump(mode="json")
        payload["digest"] = [self._bullet(), self._bullet(), self._bullet()]
        loaded = Issue.model_validate(payload)
        assert loaded.digest is not None and len(loaded.digest) == 3
        assert loaded.schema_version == 9

    def test_digest_with_unknown_story_id_rejected(self, issue: Issue) -> None:
        """The provenance invariant we own: a bullet citing a story the
        issue does not carry is hallucinated provenance. Without
        _digest_story_ids_resolve, pydantic would accept any well-formed
        cluster id."""
        payload = issue.model_dump(mode="json")
        payload["digest"] = [
            self._bullet(), self._bullet(),
            self._bullet(story_id=VALID_CLUSTER_ID_2),  # not in the issue
        ]
        with pytest.raises(ValidationError, match="not present in"):
            Issue.model_validate(payload)

    @pytest.mark.parametrize("count", [1, 2, 6])
    def test_digest_out_of_band_bullet_count_rejected(
        self, issue: Issue, count: int
    ) -> None:
        """Present => well-formed (3-5 bullets). The degradation path for
        a thin day is digest=None, never a degenerate 1-bullet skim."""
        payload = issue.model_dump(mode="json")
        payload["digest"] = [self._bullet() for _ in range(count)]
        with pytest.raises(ValidationError):
            Issue.model_validate(payload)

    _FIRST_DIGEST_DATE = "2026-08-10"
    """Issue #35, the first published under the v9 digest/synthesis schema.
    Everything before it is the pre-redesign archive."""

    def test_all_released_issues_parse_with_digest_and_synthesis_none(self) -> None:
        """THE migration promise, proven by execution: every issue.json in
        the released archive validates under the current model, and every
        PRE-REDESIGN issue (before 2026-08-10 / #35) carries digest=None
        and synthesis=None on every section. Pins the archive-compat
        contract the same way the pre-take parse test does, but across the
        whole corpus -- a future field rename or a tightened validator that
        breaks history fails here first. Issues from #35 onward may carry
        either shape; parsing is the only claim made about them."""
        from pathlib import Path

        released = sorted(Path("data/released").glob("*/issue.json"))
        if not released:
            pytest.skip("data/released/ not present in this environment")
        for archive in released:
            issue = Issue.model_validate_json(
                archive.read_text(encoding="utf-8")
            )
            if archive.parent.name >= self._FIRST_DIGEST_DATE:
                continue
            assert issue.digest is None, archive
            for section in issue.sections:
                assert section.synthesis is None, archive


# ===========================================================================
# ReviewTarget kind="digest" + field="synthesis" -- the reviewer must be
# able to point findings at the new reader-facing prose (schema v3,
# 2026-08-09), with the same locator discipline as story/section targets.
# ===========================================================================

class TestReviewTargetDigestAndSynthesis:
    def test_section_target_accepts_synthesis(self) -> None:
        target = ReviewTarget(
            kind="section", section="hands_on", field="synthesis"
        )
        assert target.field == "synthesis"

    def test_digest_target_accepts_both_digest_fields(self) -> None:
        for field in ("digest_lead", "digest_sentence"):
            target = ReviewTarget(kind="digest", digest_index=0, field=field)
            assert target.digest_index == 0

    def test_digest_target_requires_digest_index(self) -> None:
        """A digest finding without an index cannot be resolved to a bullet
        on disk -- the unresolvable-finding misfire class."""
        with pytest.raises(ValidationError, match="digest_index"):
            ReviewTarget(kind="digest", field="digest_lead")

    def test_digest_target_rejects_story_and_section_locators(self) -> None:
        """Digest provenance lives on DigestBullet.story_ids; the digest is
        issue-level. Stray locators would let a reader mis-group findings."""
        with pytest.raises(ValidationError, match="story_id"):
            ReviewTarget(
                kind="digest", digest_index=0, field="digest_lead",
                story_id=VALID_CLUSTER_ID,
            )
        with pytest.raises(ValidationError, match="section"):
            ReviewTarget(
                kind="digest", digest_index=0, field="digest_lead",
                section="hands_on",
            )

    def test_digest_target_rejects_story_field(self) -> None:
        with pytest.raises(ValidationError, match="digest_lead"):
            ReviewTarget(kind="digest", digest_index=0, field="headline")

    def test_story_and_section_targets_reject_digest_locator_and_fields(self) -> None:
        """The widening must not leak sideways: a story target carrying a
        digest_index (or a section target naming digest_lead) is
        unresolvable and rejected."""
        with pytest.raises(ValidationError, match="digest_index"):
            ReviewTarget(
                kind="story", story_id=VALID_CLUSTER_ID, field="headline",
                digest_index=0,
            )
        with pytest.raises(ValidationError, match="synthesis"):
            ReviewTarget(kind="section", section="hands_on", field="digest_lead")
