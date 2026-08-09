"""Smoke tests for the advisory verify stage (``src.verify.verify_day``).

These pin the STAGE behaviour -- the read/write/join flow and the failure-soft
contract -- not the verifier's judgment (which is owned by Eval 7 against the
factual-accuracy fixtures). The LLM boundary (``verify_rich``) is mocked so we
exercise the I/O and rollup logic the stage itself owns.

Subject to test-engineer review before commit (per tests/CONVENTIONS.md).
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from src import paths, verify
from src.models import Issue, IssueSection, SummaryBlock
from src.verify import ClaimVerdict  # the verify_rich dataclass

from tests.conftest import FIXED_DATE, FIXED_NOW


# ---------------------------------------------------------------------------
# Fixtures: a minimal valid staged issue + its source_excerpts sidecar.
# ---------------------------------------------------------------------------

_PULSE_ID = "c_aaaaaaaaaaaa"
_BP_ID = "c_bbbbbbbbbbbb"
_PULSE_URL = "https://example.com/pulse-story"
_BP_URL = "https://example.com/big-picture-story"


def _make_staged_issue() -> Issue:
    """A two-story staged issue (Pulse + one Big Picture). No verification."""
    pulse = IssueSection(
        name="pulse",
        stories=[SummaryBlock(
            story_id=_PULSE_ID,
            headline="A new model runs on a single consumer GPU",
            summary="The release runs locally on an RTX 3090 with no cloud "
                    "dependency, which matters for on-prem deployments.",
            source_urls=[_PULSE_URL],  # type: ignore[list-item]
        )],
    )
    big_picture = IssueSection(
        name="big_picture",
        stories=[SummaryBlock(
            story_id=_BP_ID,
            headline="A bank ships an agent into production",
            summary="The rollout covers fraud triage across retail workflows.",
            source_urls=[_BP_URL],  # type: ignore[list-item]
        )],
    )
    return Issue(
        issue_number=None,
        date=FIXED_DATE,
        pulse=pulse,
        sections=[
            big_picture,
            IssueSection(name="hands_on", stories=[]),
            IssueSection(name="currents", stories=[]),
        ],
        generated_at=FIXED_NOW,
        prompt_versions={"rank": "v1.0", "summarise": "v0.16"},
    )


def _write_staged_issue(issue: Issue) -> Path:
    path = paths.issue_path(issue.date, canonical=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(issue.model_dump_json(indent=2), encoding="utf-8")
    return path


def _write_excerpts(date: _dt.date, records: list[dict]) -> Path:
    path = paths.source_excerpts_path(date, canonical=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return path


# ---------------------------------------------------------------------------
# Happy path: stage runs, writes verify.json, denormalises onto issue.json.
# ---------------------------------------------------------------------------

class TestVerifyDayHappyPath:
    def _run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Seed a valid issue + sidecar and run verify_day with a stub
        verifier that flags the Pulse and clears the Big Picture."""
        issue = _make_staged_issue()
        _write_staged_issue(issue)
        _write_excerpts(FIXED_DATE, [
            {"schema_version": 1, "url": _PULSE_URL,
             "excerpt": "The model needs a data-centre GPU, not a 3090.",
             "fetched_at": FIXED_NOW.isoformat(), "story_id": _PULSE_ID},
            {"schema_version": 1, "url": _BP_URL,
             "excerpt": "The bank deployed the agent for fraud triage.",
             "fetched_at": FIXED_NOW.isoformat(), "story_id": _BP_ID},
        ])

        def _stub_verify_rich(headline, body, source_excerpt, **kw):
            if "3090" in body:
                # Pulse: contradicted headline claim (carries a source span).
                return [ClaimVerdict(
                    claim="runs on an RTX 3090",
                    verdict="contradicted",
                    location="body",
                    summary_span="runs locally on an RTX 3090",
                    source_span="needs a data-centre GPU, not a 3090",
                    note="source says it needs a data-centre GPU",
                )]
            return [ClaimVerdict(
                claim="agent deployed for fraud triage",
                verdict="supported",
                location="body",
                source_span="deployed the agent for fraud triage",
            )]

        monkeypatch.setattr(verify, "verify_rich", _stub_verify_rich)
        verify.verify_day(FIXED_DATE)

    def test_writes_verify_json_with_flagged_verdict(
        self, tmp_data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._run(monkeypatch)
        report = json.loads(
            paths.verify_path(FIXED_DATE, canonical=False).read_text("utf-8")
        )
        assert report["verdict"] == "flagged"

    def test_verdict_counts_tally_across_stories(
        self, tmp_data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._run(monkeypatch)
        report = json.loads(
            paths.verify_path(FIXED_DATE, canonical=False).read_text("utf-8")
        )
        assert report["verdict_counts"]["contradicted"] == 1
        assert report["verdict_counts"]["supported"] == 1

    def test_issue_json_denormalises_verification(
        self, tmp_data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._run(monkeypatch)
        issue = Issue.model_validate_json(
            paths.issue_path(FIXED_DATE, canonical=False).read_text("utf-8")
        )
        pulse_block = issue.pulse.stories[0]
        assert pulse_block.verification is not None
        assert pulse_block.verification.has_contradiction is True


# ---------------------------------------------------------------------------
# Failure-soft: missing sidecar / missing issue must not raise.
# ---------------------------------------------------------------------------

class TestVerifyDayFailureSoft:
    def test_missing_issue_yields_unavailable_without_raising(
        self, tmp_data_root: Path
    ) -> None:
        # No issue.json written at all.
        report = verify.verify_day(FIXED_DATE)
        assert report.verdict == "unavailable"
        assert report.stories == []

    def test_missing_sidecar_does_not_raise_and_issue_stays_unverified(
        self, tmp_data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing source_excerpts.jsonl is a degraded-but-runnable state:
        the stage runs against empty excerpts (every claim unverifiable) and
        does NOT raise. The contract phrasing in the task is satisfied either
        way -- this test pins 'no raise' and a usable report."""
        _write_staged_issue(_make_staged_issue())
        # Sidecar intentionally absent.

        def _stub_verify_rich(headline, body, source_excerpt, **kw):
            # Empty excerpt -> the real verifier marks all claims unverifiable.
            assert source_excerpt == ""
            return [ClaimVerdict(
                claim="some claim", verdict="unverifiable", location="body",
            )]

        monkeypatch.setattr(verify, "verify_rich", _stub_verify_rich)
        report = verify.verify_day(FIXED_DATE)
        # Ran (not unavailable), no flags from unverifiable claims.
        assert report.verdict == "clean"
        assert paths.verify_path(FIXED_DATE, canonical=False).exists()

    def test_unparseable_sidecar_yields_unavailable(
        self, tmp_data_root: Path
    ) -> None:
        _write_staged_issue(_make_staged_issue())
        path = paths.source_excerpts_path(FIXED_DATE, canonical=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json\n", encoding="utf-8")
        report = verify.verify_day(FIXED_DATE)
        assert report.verdict == "unavailable"

    def test_one_failing_story_does_not_lose_the_others(
        self, tmp_data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-story isolation: a verifier that raises on one story still
        produces verdicts for the rest."""
        _write_staged_issue(_make_staged_issue())
        _write_excerpts(FIXED_DATE, [
            {"schema_version": 1, "url": _PULSE_URL, "excerpt": "x",
             "fetched_at": FIXED_NOW.isoformat(), "story_id": _PULSE_ID},
            {"schema_version": 1, "url": _BP_URL, "excerpt": "y",
             "fetched_at": FIXED_NOW.isoformat(), "story_id": _BP_ID},
        ])

        def _stub_verify_rich(headline, body, source_excerpt, **kw):
            if "3090" in body:
                raise RuntimeError("verifier blew up on the pulse")
            return [ClaimVerdict(
                claim="ok", verdict="supported", location="body",
            )]

        monkeypatch.setattr(verify, "verify_rich", _stub_verify_rich)
        report = verify.verify_day(FIXED_DATE)
        # Both stories present; the failed one carries an empty claim list.
        by_id = {s.story_id: s for s in report.stories}
        assert by_id[_PULSE_ID].claims == []
        assert len(by_id[_BP_ID].claims) == 1


# ---------------------------------------------------------------------------
# v0.6 (2026-08-08): take adjudication.
#
# Contract under test (DESIGN.md "The take"): (1) the no-take prompt path
# is UNCHANGED (Eval 7's fixtures carry no takes, so its calibration must
# be attributable to judge behaviour, not prompt drift); (2) a story's
# take is ALWAYS part of the claim-extraction input (integrity
# requirement); (3) take-drawn claims persist as location="body" -- the
# ClaimLocation vocabulary deliberately stays two-valued (a take value
# was considered and deferred).
# ---------------------------------------------------------------------------

class TestTakeAdjudication:
    def test_no_take_prompt_carries_no_take_material(self) -> None:
        """take="" must not perturb the prompt: no TAKE scope block, no
        TAKE input block, and the two-value location vocabulary. (Full
        byte-identity vs the committed v0.4 rendering was proven by
        executing both builders side by side on 2026-08-08 -- this pins
        the conditionality so a refactor cannot make the blocks
        unconditional without going red.)"""
        prompt_default = verify._build_verify_prompt("H", "B", "S", [])
        prompt_empty = verify._build_verify_prompt("H", "B", "S", [], take="")
        assert prompt_default == prompt_empty
        assert "TAKE" not in prompt_default.replace(
            "MISTAKE", ""  # defensive: no incidental substring
        )
        assert '"location": "<headline | body>"' in prompt_default

    def test_take_prompt_adds_scope_input_and_location(self) -> None:
        take = "Breaks every parser downstream."
        prompt = verify._build_verify_prompt("H", "B", "S", [], take=take)
        assert "THE TAKE -- how to adjudicate it" in prompt
        assert f"TAKE (the publication's position line):\n{take}" in prompt
        assert '"location": "<headline | body | take>"' in prompt
        assert 'AT LEAST ONE claim with location "take"' in prompt

    def test_compute_hints_scans_take_tokens(self) -> None:
        hints = verify.compute_hints(
            "Headline", "Body with no numbers.",
            "The source discusses parsers.",
            take="Throughput drops 37% under the new default.",
        )
        assert any("37" in h for h in hints)
        # Without the take, the number is invisible to the pre-pass.
        assert not any(
            "37" in h
            for h in verify.compute_hints(
                "Headline", "Body with no numbers.",
                "The source discusses parsers.",
            )
        )

    def test_parser_accepts_take_location(self) -> None:
        raw = json.dumps({"claims": [{
            "claim": "Breaks every parser downstream",
            "location": "take",
            "verdict": "unsupported",
        }]})
        parsed = verify._parse_verify_json(raw)
        assert parsed is not None
        assert parsed[0].location == "take"

    def test_verify_day_always_passes_take_to_the_judge(
        self, tmp_data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DESIGN.md integrity requirement: the take is part of the
        claim-extraction input whenever present -- with the ClaimLocation
        enum at its current two values (the deliberate, deferred state),
        NOT only under some future widening."""
        issue = _make_staged_issue()
        payload = json.loads(issue.model_dump_json())
        # Denormalised raw edit: give the Pulse story a take. (Written
        # raw so the test also passes before/after SummaryBlock.take --
        # verify_day reads the raw payload, never the pydantic model.)
        payload["pulse"]["stories"][0]["take"] = (
            "Local-first inference is the procurement default now."
        )
        path = paths.issue_path(FIXED_DATE, canonical=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        _write_excerpts(FIXED_DATE, [])

        seen: dict[str, str] = {}

        def _stub_verify_rich(headline, body, source_excerpt, *, take="",
                              **kw):
            seen[headline] = take
            return [ClaimVerdict(
                claim="c", verdict="unverifiable", location="body",
            )]

        monkeypatch.setattr(verify, "verify_rich", _stub_verify_rich)
        assert verify._MODEL_SUPPORTS_TAKE_LOCATION is False  # current enum
        verify.verify_day(FIXED_DATE)
        assert seen["A new model runs on a single consumer GPU"] == (
            "Local-first inference is the procurement default now."
        )
        # The take-less Big Picture story passes take="".
        assert seen["A bank ships an agent into production"] == ""

    def test_aux_surfaces_never_called_on_a_digestless_synthesisless_issue(
        self, tmp_data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Eval 7 calibration guard, v0.7 edition: on a day with no
        digest and no syntheses, the ONLY prompt path exercised is the
        per-story one (which is byte-identical to v0.6/v0.4 -- pinned by
        TestTakeAdjudication above). Would go red if aux verification ever
        leaked into the story path."""
        _write_staged_issue(_make_staged_issue())
        _write_excerpts(FIXED_DATE, [])
        monkeypatch.setattr(
            verify, "verify_rich",
            lambda *a, **kw: [ClaimVerdict(
                claim="c", verdict="supported", location="body",
            )],
        )

        def _boom(*a, **kw):  # pragma: no cover -- the assertion IS the test
            raise AssertionError("verify_aux_rich called with no aux surfaces")

        monkeypatch.setattr(verify, "verify_aux_rich", _boom)
        report = verify.verify_day(FIXED_DATE)
        assert report.verdict == "clean"

    def test_take_location_persists_as_body_per_location_contract(
        self, tmp_data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DESIGN.md location contract: the judge's location="take"
        attribution persists as location="body" (the enum deliberately
        stays two-valued). Also covers the defensive case of a judge
        hallucinating the location on a no-take story."""
        issue = _make_staged_issue()
        _write_staged_issue(issue)
        _write_excerpts(FIXED_DATE, [])

        def _stub_verify_rich(headline, body, source_excerpt, **kw):
            return [ClaimVerdict(
                claim="c", verdict="supported", location="take",
            )]

        monkeypatch.setattr(verify, "verify_rich", _stub_verify_rich)
        monkeypatch.setattr(verify, "_MODEL_SUPPORTS_TAKE_LOCATION", False)
        report = verify.verify_day(FIXED_DATE)
        assert len(report.stories) == 2
        for story in report.stories:
            assert story.claims, "claims lost to a location coercion gap"
            assert story.claims[0].location == "body"


# ---------------------------------------------------------------------------
# v0.7 (2026-08-09): digest + synthesis adjudication.
#
# Contracts under test (DESIGN.md "The digest", V1-V4 + the synthesis
# convention): every bullet's full text is claim-extraction input (V1),
# judged against the union of ITS story_ids' excerpts (V2), persisted onto
# the PRIMARY story's StoryVerification with location="body" and a
# "digest: " note prefix (V3), and never silently skipped (V4). Each
# section synthesis is judged against that section's stories' excerpts and
# attaches to the section's first story with a "synthesis: " prefix. The
# post-verify digest bar surfaces in the report note (advisory).
# ---------------------------------------------------------------------------

_LEAD = "Local agents arrive."
_SENTENCE = (
    "A consumer-GPU model and a bank's fraud-triage agent both shipped to "
    "production this week."
)


def _make_issue_with_digest_and_synthesis() -> dict:
    """Raw payload: the two-story issue plus a 3-bullet digest (min the
    Issue model allows) and a Big Picture synthesis. Built raw because
    verify_day reads the raw payload, never the pydantic model."""
    issue = _make_staged_issue()
    payload = json.loads(issue.model_dump_json())
    payload["sections"][0]["synthesis"] = (
        "Production agents stopped being demos today. Two deployments "
        "crossed the line in the same week."
    )
    bullet = {
        "schema_version": 1,
        "lead": _LEAD,
        "sentence": _SENTENCE,
        # PRIMARY is the Big Picture story; the Pulse story widens the
        # excerpt scope (the cross-story case V2 exists for).
        "story_ids": [_BP_ID, _PULSE_ID],
    }
    filler = {
        "schema_version": 1,
        "lead": "Pulse story recap.",
        "sentence": "The pulse story in one line for the skim reader.",
        "story_ids": [_PULSE_ID],
    }
    payload["digest"] = [filler, bullet, dict(filler)]
    return payload


def _write_payload(payload: dict) -> Path:
    path = paths.issue_path(FIXED_DATE, canonical=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _standard_excerpts() -> None:
    _write_excerpts(FIXED_DATE, [
        {"schema_version": 1, "url": _PULSE_URL, "excerpt": "PULSE-EXCERPT",
         "fetched_at": FIXED_NOW.isoformat(), "story_id": _PULSE_ID},
        {"schema_version": 1, "url": _BP_URL, "excerpt": "BP-EXCERPT",
         "fetched_at": FIXED_NOW.isoformat(), "story_id": _BP_ID},
    ])


def _supported_story_stub(headline, body, source_excerpt, **kw):
    return [ClaimVerdict(
        claim="story claim", verdict="supported", location="body",
        source_span="x",
    )]


class TestAuxSurfaces:
    def test_bullet_text_and_scoped_union_reach_the_aux_verifier(
        self, tmp_data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """V1 + V2: the full bullet text (lead + sentence) is the input,
        and the excerpt is the union of the CITED stories' excerpts in
        citation order -- primary first, never some other story's."""
        _write_payload(_make_issue_with_digest_and_synthesis())
        _standard_excerpts()
        monkeypatch.setattr(verify, "verify_rich", _supported_story_stub)

        calls: list[tuple[str, str, str]] = []

        def _stub_aux(kind, text, source_excerpt, **kw):
            calls.append((kind, text, source_excerpt))
            return [ClaimVerdict(
                claim="aux claim", verdict="supported", location="body",
                source_span="x",
            )]

        monkeypatch.setattr(verify, "verify_aux_rich", _stub_aux)
        verify.verify_day(FIXED_DATE)

        digest_calls = [c for c in calls if c[0] == "digest"]
        assert len(digest_calls) == 3
        cross_story = digest_calls[1]
        assert cross_story[1] == f"{_LEAD} {_SENTENCE}"
        # Primary (BP) excerpt first, then the pulse's -- citation order.
        assert cross_story[2] == "BP-EXCERPT\n\nPULSE-EXCERPT"
        # The single-story bullets see ONLY their story's excerpt.
        assert digest_calls[0][2] == "PULSE-EXCERPT"

    def test_digest_claims_attach_to_primary_story_as_body_with_prefix(
        self, tmp_data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """V3: verdicts land on story_ids[0]'s StoryVerification, with
        location="body", a "digest: " note prefix, and a summary_span
        quoting the bullet -- and the rollups fold them in (the gate's
        contradicted-claim hard block rides on has_contradiction)."""
        _write_payload(_make_issue_with_digest_and_synthesis())
        _standard_excerpts()
        monkeypatch.setattr(verify, "verify_rich", _supported_story_stub)

        def _stub_aux(kind, text, source_excerpt, **kw):
            if kind == "synthesis":
                return [ClaimVerdict(
                    claim="syn claim", verdict="supported", location="body",
                    source_span="x",
                )]
            return [ClaimVerdict(
                claim="both shipped to production this week",
                verdict="contradicted", location="body",
                source_span="only one shipped",
                note="source says one",
            )]

        monkeypatch.setattr(verify, "verify_aux_rich", _stub_aux)
        report = verify.verify_day(FIXED_DATE)

        by_id = {s.story_id: s for s in report.stories}
        bp = by_id[_BP_ID]
        digest_claims = [
            c for c in bp.claims if c.note.startswith("digest: ")
        ]
        assert digest_claims, "digest verdicts did not reach the primary story"
        assert all(c.location == "body" for c in digest_claims)
        assert bp.has_contradiction is True
        assert report.verdict == "flagged"
        # headline_flagged stays a HEADLINE property -- digest claims are
        # body-located by contract and must not flip it.
        assert bp.headline_flagged is False

    def test_failed_aux_call_yields_one_unverifiable_never_a_silent_skip(
        self, tmp_data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """V4, failure edition: verify_aux_rich returning [] (total LLM
        failure) still leaves one code-authored unverifiable claim
        covering the bullet on the audit trail."""
        _write_payload(_make_issue_with_digest_and_synthesis())
        _standard_excerpts()
        monkeypatch.setattr(verify, "verify_rich", _supported_story_stub)
        monkeypatch.setattr(verify, "verify_aux_rich", lambda *a, **kw: [])
        report = verify.verify_day(FIXED_DATE)

        by_id = {s.story_id: s for s in report.stories}
        marks = [
            c for c in by_id[_BP_ID].claims
            if c.note.startswith("digest: ") and c.verdict == "unverifiable"
        ]
        assert marks and marks[0].summary_span  # quotes the bullet

    def test_synthesis_scoped_to_section_and_attached_to_first_story(
        self, tmp_data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_payload(_make_issue_with_digest_and_synthesis())
        _standard_excerpts()
        monkeypatch.setattr(verify, "verify_rich", _supported_story_stub)

        calls: list[tuple[str, str, str]] = []

        def _stub_aux(kind, text, source_excerpt, **kw):
            calls.append((kind, text, source_excerpt))
            return [ClaimVerdict(
                claim="two deployments crossed the line",
                verdict="unsupported", location="body",
            )]

        monkeypatch.setattr(verify, "verify_aux_rich", _stub_aux)
        report = verify.verify_day(FIXED_DATE)

        syn_calls = [c for c in calls if c[0] == "synthesis"]
        assert len(syn_calls) == 1
        assert syn_calls[0][1].startswith("Production agents stopped")
        # Scope: the Big Picture section's stories' excerpts only.
        assert syn_calls[0][2] == "BP-EXCERPT"
        by_id = {s.story_id: s for s in report.stories}
        assert any(
            c.note.startswith("synthesis: ")
            for c in by_id[_BP_ID].claims
        )
        assert not any(
            c.note.startswith("synthesis: ")
            for c in by_id[_PULSE_ID].claims
        )

    def test_digest_bar_violation_surfaces_in_the_report_note(
        self, tmp_data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ratified post-verify bar: a bullet carrying a claim the
        verifier marked unverifiable/contradicted is surfaced in the
        verify report note (advisory -- the digest itself is untouched)."""
        _write_payload(_make_issue_with_digest_and_synthesis())
        _standard_excerpts()
        monkeypatch.setattr(verify, "verify_rich", _supported_story_stub)

        def _stub_aux(kind, text, source_excerpt, **kw):
            if kind == "digest":
                # Near-verbatim bullet text, flagged -> bar violation.
                return [ClaimVerdict(
                    claim=text, verdict="unverifiable", location="body",
                )]
            return [ClaimVerdict(
                claim="syn", verdict="supported", location="body",
                source_span="x",
            )]

        monkeypatch.setattr(verify, "verify_aux_rich", _stub_aux)
        report = verify.verify_day(FIXED_DATE)
        assert "digest-bar:" in report.note
        # Advisory: the staged digest is NOT nulled or edited.
        on_disk = json.loads(
            paths.issue_path(FIXED_DATE, canonical=False).read_text("utf-8")
        )
        assert len(on_disk["digest"]) == 3

    def test_clean_aux_verdicts_leave_the_note_bar_free(
        self, tmp_data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_payload(_make_issue_with_digest_and_synthesis())
        _standard_excerpts()
        monkeypatch.setattr(verify, "verify_rich", _supported_story_stub)
        monkeypatch.setattr(
            verify, "verify_aux_rich",
            lambda *a, **kw: [ClaimVerdict(
                claim="fresh words entirely", verdict="supported",
                location="body", source_span="x",
            )],
        )
        report = verify.verify_day(FIXED_DATE)
        assert "digest-bar:" not in report.note


class TestAuxPromptAndParse:
    def test_aux_prompt_pins_body_location_and_no_silent_skip_rule(self) -> None:
        prompt = verify._build_aux_verify_prompt(
            "digest", "Lead. One sentence.", "SOURCE", [],
        )
        assert '"location": "body"' in prompt
        assert "Produce AT LEAST ONE claim" in prompt
        assert "Never return zero claims" in prompt
        # The calibrated per-story scope/take blocks must NOT leak in.
        assert "HEADLINE:" not in prompt
        assert "THE TAKE" not in prompt

    def test_aux_rich_coerces_location_to_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """V3 at the judge boundary: whatever location the model claims,
        aux verdicts persist as body."""
        raw = json.dumps({"claims": [{
            "claim": "c", "location": "headline", "verdict": "supported",
            "source_span": "s",
        }]})
        monkeypatch.setattr(
            verify, "_metered_llm_call", lambda *a, **kw: raw,
        )
        out = verify.verify_aux_rich("digest", "text", "source")
        assert out and out[0].location == "body"

    def test_aux_rich_enforces_contradiction_discipline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = json.dumps({"claims": [{
            "claim": "c", "location": "body", "verdict": "contradicted",
            "source_span": "",
        }]})
        monkeypatch.setattr(
            verify, "_metered_llm_call", lambda *a, **kw: raw,
        )
        out = verify.verify_aux_rich("synthesis", "text", "source")
        assert out and out[0].verdict == "unsupported"
