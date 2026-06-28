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
