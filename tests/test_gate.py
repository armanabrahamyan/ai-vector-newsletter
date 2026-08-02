"""Unit tests for the unattended-publish gate (`src/gate.py`).

The gate decides whether a staged issue may publish without a human. Every
test here builds a staging directory on a tmp path, writes the three input
artifacts by hand, and asserts on the resulting `GateDecision`. No network,
no LLM, no reads of the real archive.

The organising principle: **one test per hold path, plus the happy path.**
The gate fails closed, so the interesting cases are all the ways a day can
fail to earn an auto-merge -- a missing review, an unparseable verdict, a
review of a superseded issue, a contradicted claim, a verifier that could
not run. If any of those stops holding, an issue ships unattended that
should not have.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from src import gate as gate_mod
from src import paths as _paths
from src import run as run_mod
from src.models import (
    GateCheck,
    GateDecision,
    GateReviewState,
    GateVerifyState,
    Issue,
    IssueSection,
    SummaryBlock,
)
from tests.conftest import (
    FIXED_DATE,
    FIXED_NOW,
    VALID_CLUSTER_ID,
    VALID_CLUSTER_ID_2,
)


# ---------------------------------------------------------------------------
# Builders.
# ---------------------------------------------------------------------------

def _issue(*, with_contradiction: bool = False) -> Issue:
    """A minimal valid Issue: one Pulse story plus three empty sections.

    ``with_contradiction=True`` denormalises a contradicted `ClaimVerdict`
    onto the Pulse block, exercising the issue.json half of the
    contradiction scan.
    """
    verification = None
    if with_contradiction:
        verification = {
            "schema_version": 1,
            "story_id": VALID_CLUSTER_ID,
            "prompt_version": "v1",
            "claims": [
                {
                    "schema_version": 1,
                    "claim": "The model runs on-device.",
                    "verdict": "contradicted",
                    "location": "headline",
                    "summary_span": "handles invoices on-device",
                    "source_span": "the model requires a cloud endpoint",
                    "note": "source says the opposite",
                },
            ],
            "has_contradiction": True,
            "has_unsupported": False,
            "headline_flagged": True,
        }
    block = SummaryBlock(
        story_id=VALID_CLUSTER_ID,
        headline="A small open model handles invoices on-device",
        summary="A 4B-param open model extracts structured data locally.",
        source_urls=["https://example.com/post-1"],
        signal="try",
        verification=verification,
    )
    return Issue(
        date=FIXED_DATE,
        pulse=IssueSection(name="pulse", stories=[block]),
        sections=[
            IssueSection(name="big_picture", stories=[]),
            IssueSection(name="hands_on", stories=[]),
            IssueSection(name="currents", stories=[]),
        ],
        generated_at=FIXED_NOW,
        prompt_versions={"rank": "v1", "summarise": "v1"},
    )


def _write_issue(date: _dt.date, issue: Issue) -> Path:
    path = _paths.issue_path(date, canonical=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(issue.model_dump_json())
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _write_review(
    date: _dt.date,
    *,
    verdict: str = "green",
    issue_hash: str | None = "auto",
    body: str = "Nothing to flag.",
    frontmatter: bool = True,
) -> Path:
    """Write a review.md.

    ``issue_hash="auto"`` records the true hash of the current issue.json
    (a fresh review). Pass an explicit string for a stale review, or ``None``
    to omit the key entirely (a writer that does not honour the freshness
    contract).
    """
    path = gate_mod.review_md_path(date)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not frontmatter:
        path.write_text(f"# Editor's Review\n\n{body}\n", encoding="utf-8")
        return path
    lines = [
        "---",
        f"verdict: {verdict}",
        "one_line: A steady day with one real release.",
        f"issue_date: {date.isoformat()}",
        "prompt_version: v0.5",
    ]
    if issue_hash == "auto":
        issue_hash = gate_mod.issue_sha256(date)
    if issue_hash is not None:
        lines.append(f"issue_sha256: {issue_hash}")
    lines += ["---", "", "# Editor's Review", "", body, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_review_json(
    date: _dt.date,
    *,
    computed_verdict: str | None = "green",
    issue_hash: str | None = "auto",
    raw: str | None = None,
) -> Path:
    """Write a review.json (the structured `ReviewReport`, gate policy v2).

    ``raw`` writes the bytes verbatim, for the corrupt-artifact cases.
    ``computed_verdict=None`` omits the key entirely -- a writer that shipped
    a report the gate cannot read a verdict out of.
    """
    path = gate_mod.review_json_path(date)
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return path
    if issue_hash == "auto":
        issue_hash = gate_mod.issue_sha256(date)
    payload: dict = {
        "schema_version": 1,
        "issue_date": date.isoformat(),
        "prompt_version": "v0.6",
        "findings": [],
        "one_line": "A steady day with one real release.",
    }
    if computed_verdict is not None:
        payload["computed_verdict"] = computed_verdict
    if issue_hash is not None:
        payload["issue_sha256"] = issue_hash
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _write_revisions(date: _dt.date, records: list[dict | str]) -> Path:
    """Write revisions.jsonl. Plain strings are written verbatim (bad lines)."""
    path = gate_mod.revisions_path(date)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_verify(
    date: _dt.date,
    *,
    verdict: str = "clean",
    stories: list[dict] | None = None,
    raw: str | None = None,
) -> Path:
    path = _paths.verify_path(date, canonical=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return path
    payload = {
        "schema_version": 1,
        "generated_at": FIXED_NOW.isoformat(),
        "prompt_version": "v1",
        "verdict": verdict,
        "verdict_counts": {"supported": 3},
        "stories": stories if stories is not None else [],
        "note": "",
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _story_verification(
    story_id: str, *, verdict: str, location: str = "body"
) -> dict:
    """A StoryVerification-shaped mapping carrying one claim of ``verdict``."""
    claim = {
        "schema_version": 1,
        "claim": "The benchmark improved by 12 points.",
        "verdict": verdict,
        "location": location,
        "summary_span": "improved by 12 points",
        "source_span": (
            "the benchmark moved by 2 points" if verdict == "contradicted" else ""
        ),
        "note": "",
    }
    return {
        "schema_version": 1,
        "story_id": story_id,
        "prompt_version": "v1",
        "claims": [claim],
        "has_contradiction": verdict == "contradicted",
        "has_unsupported": verdict == "unsupported",
        "headline_flagged": location == "headline"
        and verdict in {"contradicted", "unsupported"},
    }


def _happy_day(date: _dt.date) -> None:
    """Write the three artifacts of a day that SHOULD auto-merge."""
    _write_issue(date, _issue())
    _write_verify(date, verdict="clean")
    _write_review(date, verdict="green")


@pytest.fixture(autouse=True)
def _clear_phase_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate reads a real environment variable. Clear it so a developer's
    shell cannot change what these tests assert."""
    monkeypatch.delenv(gate_mod.PHASE_ENV_VAR, raising=False)


def _reasons(date: _dt.date, **kwargs) -> list[str]:
    return gate_mod.decide(date, **kwargs).hold_reasons


def _check(decision, name: str):
    """Fetch one GateCheck by name; fails loudly if the check disappeared."""
    for c in decision.checks:
        if c.name == name:
            return c
    raise AssertionError(f"no check named {name!r} in {[c.name for c in decision.checks]}")


# ---------------------------------------------------------------------------
# The happy path.
# ---------------------------------------------------------------------------

class TestAutoMerge:
    def test_green_review_clean_verify_fresh_hash_auto_merges(
        self, tmp_data_root: Path
    ) -> None:
        _happy_day(FIXED_DATE)
        decision = gate_mod.decide(FIXED_DATE)

        assert decision.decision == "auto_merge"
        assert decision.hold_reasons == []
        assert decision.phase == "shadow"  # env unset -> default
        assert decision.gate_version == gate_mod.GATE_VERSION
        assert decision.review.verdict == "green"
        assert decision.review.fresh is True
        assert decision.verify.verdict == "clean"
        assert decision.verify.contradicted_story_ids == []
        # Every blocking check passed, and the artifact says so explicitly.
        assert all(c.passed for c in decision.checks if c.blocking)

    def test_amber_auto_merges_in_green_amber_phase(
        self, tmp_data_root: Path
    ) -> None:
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review(FIXED_DATE, verdict="amber")

        assert gate_mod.decide(FIXED_DATE, phase="green_amber").decision == "auto_merge"

    def test_amber_holds_in_green_only_phase(self, tmp_data_root: Path) -> None:
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review(FIXED_DATE, verdict="amber")

        decision = gate_mod.decide(FIXED_DATE, phase="green_only")
        assert decision.decision == "hold"
        assert decision.hold_reasons == [gate_mod.HOLD_REVIEW_AMBER]

    def test_flagged_verify_without_contradiction_still_auto_merges(
        self, tmp_data_root: Path
    ) -> None:
        """An unsupported claim is surfaced, not blocked -- ratified policy
        blocks on contradictions only."""
        _write_issue(FIXED_DATE, _issue())
        _write_verify(
            FIXED_DATE, verdict="flagged",
            stories=[_story_verification(VALID_CLUSTER_ID, verdict="unsupported")],
        )
        _write_review(FIXED_DATE, verdict="green")

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.decision == "auto_merge"
        assert decision.verify.unsupported_story_ids == [VALID_CLUSTER_ID]
        assert _check(decision, "verify_no_unsupported").passed is False
        assert _check(decision, "verify_no_unsupported").blocking is False


# ---------------------------------------------------------------------------
# Hold paths -- the review artifact.
# ---------------------------------------------------------------------------

class TestReviewHolds:
    def test_missing_review_holds(self, tmp_data_root: Path) -> None:
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        # No review.md at all.

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.decision == "hold"
        assert gate_mod.HOLD_REVIEW_MISSING in decision.hold_reasons
        assert decision.review.present is False
        # The dependent checks are recorded but contribute no second reason.
        assert _check(decision, "review_verdict").blocking is False
        assert decision.hold_reasons == [gate_mod.HOLD_REVIEW_MISSING]

    def test_review_without_frontmatter_holds_as_unparseable(
        self, tmp_data_root: Path
    ) -> None:
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review(FIXED_DATE, frontmatter=False)

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.decision == "hold"
        assert decision.hold_reasons == [gate_mod.HOLD_REVIEW_UNPARSEABLE]
        assert decision.review.present is True
        assert decision.review.verdict is None

    def test_unparseable_verdict_token_holds(self, tmp_data_root: Path) -> None:
        """A verdict outside the known vocabulary means the artifact and the
        gate have drifted apart -- hold, do not guess."""
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review(FIXED_DATE, verdict="mostly-fine")

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.decision == "hold"
        assert decision.hold_reasons == [gate_mod.HOLD_REVIEW_UNKNOWN]

    def test_red_review_holds(self, tmp_data_root: Path) -> None:
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review(FIXED_DATE, verdict="red")

        assert _reasons(FIXED_DATE) == [gate_mod.HOLD_REVIEW_RED]

    def test_unavailable_review_holds(self, tmp_data_root: Path) -> None:
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review(FIXED_DATE, verdict="unavailable")

        assert _reasons(FIXED_DATE) == [gate_mod.HOLD_REVIEW_UNAVAILABLE]

    def test_stale_review_holds_when_issue_changed_after_review(
        self, tmp_data_root: Path
    ) -> None:
        """The core freshness case: the editor read an issue, then the issue
        changed. A green verdict about a superseded draft is not evidence
        about the draft we are publishing."""
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review(FIXED_DATE, verdict="green")  # records the current hash

        # Now the issue changes: a second story is added post-review.
        changed = _issue()
        changed.sections[1].stories.append(
            SummaryBlock(
                story_id=VALID_CLUSTER_ID_2,
                headline="A second story lands after the review",
                summary="Added to the issue after the editor signed off.",
                source_urls=["https://example.com/post-2"],
            )
        )
        _write_issue(FIXED_DATE, changed)

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.decision == "hold"
        assert decision.hold_reasons == [gate_mod.HOLD_STALE_REVIEW]
        assert decision.review.fresh is False
        assert decision.review.issue_sha256 != decision.review.computed_issue_sha256

    def test_review_without_issue_sha256_key_holds(
        self, tmp_data_root: Path
    ) -> None:
        """Absence of the key is not a pass. We cannot confirm what the
        editor read, so we hold -- fail closed."""
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review(FIXED_DATE, verdict="green", issue_hash=None)

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.hold_reasons == [gate_mod.HOLD_STALE_REVIEW]
        assert decision.review.issue_sha256 is None
        assert decision.review.fresh is False

    def test_stale_review_holds_even_in_shadow_phase(
        self, tmp_data_root: Path
    ) -> None:
        """Hard blocks ignore the phase. Shadow computes the same hold."""
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review(FIXED_DATE, verdict="green", issue_hash="deadbeef" * 8)

        assert _reasons(FIXED_DATE, phase="shadow") == [gate_mod.HOLD_STALE_REVIEW]
        assert _reasons(FIXED_DATE, phase="green_amber") == [gate_mod.HOLD_STALE_REVIEW]

    def test_issue_sha256_literal_unknown_holds_as_stale(
        self, tmp_data_root: Path
    ) -> None:
        """``issue_sha256: unknown`` is the literal placeholder src/review.py
        writes when it never got as far as hashing the issue (see
        _write_unavailable / _enrich_frontmatter). It must not be treated as
        a wildcard hash that matches anything -- it is compared byte-for-byte
        like any other recorded hash and so holds as stale."""
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review(FIXED_DATE, verdict="green", issue_hash="unknown")

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.decision == "hold"
        assert decision.hold_reasons == [gate_mod.HOLD_STALE_REVIEW]
        assert decision.review.issue_sha256 == "unknown"
        assert decision.review.fresh is False

    def test_review_frontmatter_with_crlf_line_endings_still_parses(
        self, tmp_data_root: Path
    ) -> None:
        """review.md is written by src/review.py's own atomic writer (LF), but
        a hand-edited file (or one touched on Windows) could carry CRLF. The
        parser tolerates it via .strip(), and this pins that it keeps doing
        so -- a refactor to a stricter line-splitting scheme could silently
        turn every review into 'review_verdict unparseable'."""
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        computed = gate_mod.issue_sha256(FIXED_DATE)
        review_path = gate_mod.review_md_path(FIXED_DATE)
        review_path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "---\r\n"
            "verdict: green\r\n"
            "one_line: A steady day.\r\n"
            f"issue_sha256: {computed}\r\n"
            "---\r\n"
            "\r\n"
            "# Editor's Review\r\n"
        )
        review_path.write_bytes(content.encode("utf-8"))

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.decision == "auto_merge"
        assert decision.review.verdict == "green"
        assert decision.review.fresh is True

    def test_review_verdict_unparseable_token_holds(
        self, tmp_data_root: Path
    ) -> None:
        """review.py's own fail-closed vocabulary (REVIEW_VERDICTS) includes
        ``unparseable`` as a real, on-disk token distinct from
        ``unavailable`` -- it is written whenever the review ran but the
        LLM's own frontmatter could not be trusted. gate.py's
        KNOWN_REVIEW_VERDICTS does not separately name it, so it falls
        through to the generic unknown-verdict path -- this pins that it
        still holds, however that token is spelled inside the check."""
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review(FIXED_DATE, verdict="unparseable")

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.decision == "hold"


# ---------------------------------------------------------------------------
# Hold paths -- the verify artifact.
# ---------------------------------------------------------------------------

class TestVerifyHolds:
    def test_verify_unavailable_holds(self, tmp_data_root: Path) -> None:
        """Ratified 2026-08-02: an issue that was never fact-checked does not
        go out unattended, however green the editorial verdict."""
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="unavailable")
        _write_review(FIXED_DATE, verdict="green")

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.decision == "hold"
        assert decision.hold_reasons == [gate_mod.HOLD_VERIFY_UNAVAILABLE]
        assert decision.verify.verdict == "unavailable"

    def test_missing_verify_holds(self, tmp_data_root: Path) -> None:
        """No verify.json is the same information state as unavailable: no
        factual check happened."""
        _write_issue(FIXED_DATE, _issue())
        _write_review(FIXED_DATE, verdict="green")

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.hold_reasons == [gate_mod.HOLD_VERIFY_MISSING]
        assert decision.verify.present is False
        assert _check(decision, "verify_available").blocking is False

    def test_unparseable_verify_holds(self, tmp_data_root: Path) -> None:
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, raw="{ not json at all")
        _write_review(FIXED_DATE, verdict="green")

        assert _reasons(FIXED_DATE) == [gate_mod.HOLD_VERIFY_UNPARSEABLE]

    def test_verify_unavailable_holds_in_every_phase(
        self, tmp_data_root: Path
    ) -> None:
        """The second ratified 2026-08-02 hard block: this must not narrow to
        just the default phase. Mirrors test_contradicted_claim_holds_in_every_phase."""
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="unavailable")
        _write_review(FIXED_DATE, verdict="green")

        for phase in gate_mod.VALID_PHASES:
            assert _reasons(FIXED_DATE, phase=phase) == [
                gate_mod.HOLD_VERIFY_UNAVAILABLE
            ], f"phase {phase} failed to block an unavailable verify"

    def test_missing_verify_holds_in_every_phase(self, tmp_data_root: Path) -> None:
        _write_issue(FIXED_DATE, _issue())
        _write_review(FIXED_DATE, verdict="green")

        for phase in gate_mod.VALID_PHASES:
            assert _reasons(FIXED_DATE, phase=phase) == [
                gate_mod.HOLD_VERIFY_MISSING
            ], f"phase {phase} failed to block a missing verify"

    def test_contradicted_claim_holds_even_when_rollup_boolean_disagrees(
        self, tmp_data_root: Path,
    ) -> None:
        """`_read_verify` deliberately does NOT pydantic-validate verify.json
        (see its docstring), so a hand-edited or partially-written record can
        carry ``has_contradiction: false`` next to a claim the verifier itself
        marked ``contradicted``. `_claim_scan` reads both the rollup and the
        per-claim verdicts and treats either as sufficient evidence -- this
        pins that the per-claim read is not decorative."""
        _write_issue(FIXED_DATE, _issue())
        story = _story_verification(VALID_CLUSTER_ID, verdict="contradicted")
        story["has_contradiction"] = False  # inconsistent rollup
        _write_verify(FIXED_DATE, verdict="flagged", stories=[story])
        _write_review(FIXED_DATE, verdict="green")

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.decision == "hold"
        assert decision.hold_reasons == [gate_mod.HOLD_CONTRADICTED_CLAIM]
        assert decision.verify.contradicted_story_ids == [VALID_CLUSTER_ID]

    def test_contradicted_claim_in_verify_json_holds(
        self, tmp_data_root: Path
    ) -> None:
        """Ratified 2026-08-02: a contradiction hard-blocks regardless of the
        editorial verdict. A green review over a contradicted claim means the
        two judges disagree, which is exactly when a human should look."""
        _write_issue(FIXED_DATE, _issue())
        _write_verify(
            FIXED_DATE, verdict="flagged",
            stories=[_story_verification(VALID_CLUSTER_ID, verdict="contradicted")],
        )
        _write_review(FIXED_DATE, verdict="green")

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.decision == "hold"
        assert decision.hold_reasons == [gate_mod.HOLD_CONTRADICTED_CLAIM]
        assert decision.verify.contradicted_story_ids == [VALID_CLUSTER_ID]

    def test_contradicted_claim_denormalised_on_issue_holds(
        self, tmp_data_root: Path
    ) -> None:
        """The scan reads both evidence sources. A contradiction recorded on
        the issue block alone still blocks, even with a clean verify.json."""
        _write_issue(FIXED_DATE, _issue(with_contradiction=True))
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review(FIXED_DATE, verdict="green")

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.hold_reasons == [gate_mod.HOLD_CONTRADICTED_CLAIM]
        assert decision.verify.contradicted_story_ids == [VALID_CLUSTER_ID]

    def test_contradicted_claim_holds_in_every_phase(
        self, tmp_data_root: Path
    ) -> None:
        _write_issue(FIXED_DATE, _issue())
        _write_verify(
            FIXED_DATE, verdict="flagged",
            stories=[_story_verification(VALID_CLUSTER_ID, verdict="contradicted")],
        )
        _write_review(FIXED_DATE, verdict="green")

        for phase in gate_mod.VALID_PHASES:
            assert _reasons(FIXED_DATE, phase=phase) == [
                gate_mod.HOLD_CONTRADICTED_CLAIM
            ], f"phase {phase} failed to block a contradiction"


# ---------------------------------------------------------------------------
# Hold paths -- the issue artifact.
# ---------------------------------------------------------------------------

class TestIssueHolds:
    def test_missing_issue_holds(self, tmp_data_root: Path) -> None:
        _paths.staging_dir(FIXED_DATE).mkdir(parents=True, exist_ok=True)
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review(FIXED_DATE, verdict="green", issue_hash="ab" * 32)

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.decision == "hold"
        assert gate_mod.HOLD_ISSUE_MISSING in decision.hold_reasons
        assert decision.review.computed_issue_sha256 is None

    def test_invalid_issue_json_holds(self, tmp_data_root: Path) -> None:
        path = _paths.issue_path(FIXED_DATE, canonical=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"date": "2026-05-24", "pulse": null}', encoding="utf-8")
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review(FIXED_DATE, verdict="green")

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.decision == "hold"
        assert gate_mod.HOLD_ISSUE_INVALID in decision.hold_reasons

    def test_empty_staging_holds_with_every_missing_artifact_named(
        self, tmp_data_root: Path
    ) -> None:
        """The nothing-ran case. Each missing input names itself once."""
        decision = gate_mod.decide(FIXED_DATE)
        assert decision.decision == "hold"
        assert decision.hold_reasons == [
            gate_mod.HOLD_ISSUE_MISSING,
            gate_mod.HOLD_REVIEW_MISSING,
            gate_mod.HOLD_VERIFY_MISSING,
        ]


# ---------------------------------------------------------------------------
# Phase resolution.
# ---------------------------------------------------------------------------

class TestPhaseResolution:
    def test_unset_env_defaults_to_shadow(self) -> None:
        assert gate_mod.resolve_phase() == ("shadow", "")

    def test_env_var_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(gate_mod.PHASE_ENV_VAR, "green_only")
        assert gate_mod.resolve_phase()[0] == "green_only"

    def test_explicit_argument_overrides_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(gate_mod.PHASE_ENV_VAR, "green_amber")
        assert gate_mod.resolve_phase("green_only")[0] == "green_only"

    def test_unrecognised_phase_falls_back_to_shadow_with_a_note(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A typo'd repo variable must never silently widen the gate."""
        monkeypatch.setenv(gate_mod.PHASE_ENV_VAR, "green-only")
        phase, note = gate_mod.resolve_phase()
        assert phase == "shadow"
        assert "green-only" in note

    def test_decide_reads_the_env_var(
        self, tmp_data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(gate_mod.PHASE_ENV_VAR, "green_only")
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review(FIXED_DATE, verdict="amber")

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.phase == "green_only"
        assert decision.decision == "hold"


# ---------------------------------------------------------------------------
# Persistence.
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_write_decision_round_trips(self, tmp_data_root: Path) -> None:
        _happy_day(FIXED_DATE)
        decision, path = gate_mod.run_gate(FIXED_DATE)

        assert path == gate_mod.gate_path(FIXED_DATE, canonical=False)
        assert path.exists()
        reloaded = gate_mod.read_decision(FIXED_DATE)
        assert reloaded is not None
        assert reloaded.decision == decision.decision
        assert reloaded.hold_reasons == decision.hold_reasons
        assert reloaded.gate_version == gate_mod.GATE_VERSION

    def test_write_leaves_no_tmp_file_behind(self, tmp_data_root: Path) -> None:
        _happy_day(FIXED_DATE)
        gate_mod.run_gate(FIXED_DATE)
        leftovers = list(_paths.staging_dir(FIXED_DATE).glob("*.tmp"))
        assert leftovers == []

    def test_rerun_overwrites_cleanly(self, tmp_data_root: Path) -> None:
        """Same-day idempotency: a re-run after fixing the problem replaces
        the hold with an auto_merge."""
        _write_issue(FIXED_DATE, _issue())
        _write_review(FIXED_DATE, verdict="green")
        first, _ = gate_mod.run_gate(FIXED_DATE)
        assert first.decision == "hold"

        _write_verify(FIXED_DATE, verdict="clean")
        second, _ = gate_mod.run_gate(FIXED_DATE)
        assert second.decision == "auto_merge"
        assert gate_mod.read_decision(FIXED_DATE).decision == "auto_merge"

    def test_read_decision_is_none_when_absent(self, tmp_data_root: Path) -> None:
        assert gate_mod.read_decision(FIXED_DATE) is None


# ---------------------------------------------------------------------------
# Contract invariants on the gate models.
# ---------------------------------------------------------------------------

class TestGateContracts:
    def test_over_long_detail_truncates_rather_than_raising(self) -> None:
        """A gate that raises instead of deciding is strictly worse than one
        that decides with an elided sentence."""
        check = GateCheck(
            name="issue_readable", passed=True, blocking=True,
            detail="x" * 5000,
        )
        assert len(check.detail) == 1000
        assert check.detail.endswith("...")

    def test_failed_blocking_check_must_name_its_reason(self) -> None:
        with pytest.raises(ValidationError, match="carries no hold_reason"):
            GateCheck(
                name="review_verdict", passed=False, blocking=True,
                detail="something went wrong",
            )

    def test_passing_check_may_not_carry_a_hold_reason(self) -> None:
        with pytest.raises(ValidationError, match="contribute no hold reason"):
            GateCheck(
                name="review_verdict", passed=True, blocking=True,
                detail="fine", hold_reason=gate_mod.HOLD_REVIEW_RED,
            )

    def test_decision_must_agree_with_checks(self) -> None:
        """The one-word answer is only worth having if a reader can trust it
        without re-deriving it."""
        failing = GateCheck(
            name="review_present", passed=False, blocking=True,
            detail="no review.md", hold_reason=gate_mod.HOLD_REVIEW_MISSING,
        )
        with pytest.raises(ValidationError, match="disagrees with the checks"):
            GateDecision(
                gate_version="v1", phase="shadow", date=FIXED_DATE,
                decision="auto_merge",
                hold_reasons=[gate_mod.HOLD_REVIEW_MISSING],
                review=GateReviewState(present=False),
                verify=GateVerifyState(present=False),
                checks=[failing],
                decided_at=FIXED_NOW,
            )

    def test_hold_reasons_must_match_the_failed_checks(self) -> None:
        failing = GateCheck(
            name="review_present", passed=False, blocking=True,
            detail="no review.md", hold_reason=gate_mod.HOLD_REVIEW_MISSING,
        )
        with pytest.raises(ValidationError, match="disagrees with"):
            GateDecision(
                gate_version="v1", phase="shadow", date=FIXED_DATE,
                decision="hold", hold_reasons=[],
                review=GateReviewState(present=False),
                verify=GateVerifyState(present=False),
                checks=[failing],
                decided_at=FIXED_NOW,
            )


# ---------------------------------------------------------------------------
# The freshness hash contract -- shared with src/review.py.
# ---------------------------------------------------------------------------

class TestIssueHash:
    def test_hash_is_stable_across_calls(self, tmp_data_root: Path) -> None:
        _write_issue(FIXED_DATE, _issue())
        assert gate_mod.issue_sha256(FIXED_DATE) == gate_mod.issue_sha256(FIXED_DATE)

    def test_hash_changes_when_the_issue_changes(self, tmp_data_root: Path) -> None:
        _write_issue(FIXED_DATE, _issue())
        before = gate_mod.issue_sha256(FIXED_DATE)
        _write_issue(FIXED_DATE, _issue(with_contradiction=True))
        assert gate_mod.issue_sha256(FIXED_DATE) != before

    def test_hash_is_none_when_the_issue_is_missing(
        self, tmp_data_root: Path
    ) -> None:
        assert gate_mod.issue_sha256(FIXED_DATE) is None


# ---------------------------------------------------------------------------
# The `aiv gate` CLI command (src/run.py).
#
# `decide` / `run_gate` are exercised directly above; these tests are the
# only coverage of the Typer wiring around them -- in particular the
# "exit code is always 0" contract the workflow depends on to tell "the
# gate held" apart from "the gate crashed".
# ---------------------------------------------------------------------------

class TestGateCli:
    def test_exits_zero_on_auto_merge(self, tmp_data_root: Path) -> None:
        _happy_day(FIXED_DATE)
        runner = CliRunner()
        result = runner.invoke(
            run_mod.app, ["gate", "--date", FIXED_DATE.isoformat()],
        )
        assert result.exit_code == 0
        assert "AUTO_MERGE" in result.stdout
        assert gate_mod.gate_path(FIXED_DATE, canonical=False).exists()

    def test_exits_zero_on_hold(self, tmp_data_root: Path) -> None:
        """The whole point of 'decision lives in the artifact': a hold is
        NOT a process failure, so the exit code must stay 0."""
        _write_issue(FIXED_DATE, _issue())
        _write_review(FIXED_DATE, verdict="green")
        # No verify.json -> holds.
        runner = CliRunner()
        result = runner.invoke(
            run_mod.app, ["gate", "--date", FIXED_DATE.isoformat()],
        )
        assert result.exit_code == 0
        assert "HOLD" in result.stdout
        assert gate_mod.HOLD_VERIFY_MISSING in result.stdout

    def test_dry_run_does_not_write_gate_json(self, tmp_data_root: Path) -> None:
        _happy_day(FIXED_DATE)
        runner = CliRunner()
        result = runner.invoke(
            run_mod.app,
            ["gate", "--date", FIXED_DATE.isoformat(), "--dry-run"],
        )
        assert result.exit_code == 0
        assert not gate_mod.gate_path(FIXED_DATE, canonical=False).exists()

    def test_phase_flag_overrides_env(self, tmp_data_root: Path) -> None:
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review(FIXED_DATE, verdict="amber")
        runner = CliRunner()
        result = runner.invoke(
            run_mod.app,
            ["gate", "--date", FIXED_DATE.isoformat(), "--phase", "green_only"],
        )
        assert result.exit_code == 0
        assert "HOLD" in result.stdout
        decision = gate_mod.read_decision(FIXED_DATE)
        assert decision.phase == "green_only"


# ---------------------------------------------------------------------------
# Gate policy v2 -- review.json as the primary review artifact.
#
# The whole point of the change: the structured reviewer computes a verdict
# and review.md is rendered from it, so the gate should read the computed
# value rather than re-parsing prose. The tests that matter are the
# precedence rules -- which artifact wins, and what happens when the primary
# one is broken.
# ---------------------------------------------------------------------------

class TestReviewJsonPrimary:
    def test_review_json_alone_is_enough_to_auto_merge(
        self, tmp_data_root: Path
    ) -> None:
        """No review.md at all. Under v1 this held as review-missing; under
        v2 the structured artifact is a complete review on its own."""
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review_json(FIXED_DATE, computed_verdict="green")

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.decision == "auto_merge"
        assert decision.review.verdict == "green"
        assert decision.review.fresh is True
        assert decision.review.path.endswith("review.json")

    def test_review_json_wins_over_review_md(self, tmp_data_root: Path) -> None:
        """When both exist the structured file decides. review.md is rendered
        FROM review.json, so trusting the Markdown over its own source would
        mean trusting a copy over the original."""
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review(FIXED_DATE, verdict="green")
        _write_review_json(FIXED_DATE, computed_verdict="red")

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.decision == "hold"
        assert decision.hold_reasons == [gate_mod.HOLD_REVIEW_RED]

    def test_review_md_still_works_when_review_json_is_absent(
        self, tmp_data_root: Path
    ) -> None:
        """Backward compatibility with every day archived under policy v1."""
        _happy_day(FIXED_DATE)  # writes review.md only

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.decision == "auto_merge"
        assert decision.review.path.endswith("review.md")

    def test_corrupt_review_json_holds_without_falling_back(
        self, tmp_data_root: Path
    ) -> None:
        """The load-bearing precedence rule. A green review.md sitting next
        to an unreadable review.json must NOT rescue the release: the two
        could say different things, and picking the readable one means
        picking the convenient one."""
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review(FIXED_DATE, verdict="green")
        _write_review_json(FIXED_DATE, raw="{not json at all")

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.decision == "hold"
        assert decision.hold_reasons == [gate_mod.HOLD_REVIEW_UNPARSEABLE]
        assert decision.review.present is True
        assert decision.review.verdict is None

    def test_review_json_without_computed_verdict_holds(
        self, tmp_data_root: Path
    ) -> None:
        """``computed_verdict`` is the one accepted key. A report that does
        not carry it is unreadable, not a permission to guess."""
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review_json(FIXED_DATE, computed_verdict=None)

        assert _reasons(FIXED_DATE) == [gate_mod.HOLD_REVIEW_UNPARSEABLE]

    def test_stale_review_json_holds(self, tmp_data_root: Path) -> None:
        """The freshness contract is artifact-agnostic: it is about the hash,
        not about the file format. This is also the case that catches a
        revision loop that rewrote issue.json and failed to re-review it."""
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review_json(FIXED_DATE, computed_verdict="green", issue_hash="ab" * 32)

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.hold_reasons == [gate_mod.HOLD_STALE_REVIEW]
        assert decision.review.fresh is False

    def test_review_json_without_issue_sha256_holds(
        self, tmp_data_root: Path
    ) -> None:
        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        _write_review_json(FIXED_DATE, computed_verdict="green", issue_hash=None)

        assert _reasons(FIXED_DATE) == [gate_mod.HOLD_STALE_REVIEW]

    def test_read_review_state_reports_which_artifact_answered(
        self, tmp_data_root: Path
    ) -> None:
        _write_issue(FIXED_DATE, _issue())
        assert gate_mod.read_review_state(FIXED_DATE).source is None

        _write_review(FIXED_DATE, verdict="amber")
        assert gate_mod.read_review_state(FIXED_DATE).source == "review.md"

        _write_review_json(FIXED_DATE, computed_verdict="green")
        state = gate_mod.read_review_state(FIXED_DATE)
        assert state.source == "review.json"
        assert state.verdict == "green"
        assert state.prompt_version == "v0.6"

    def test_gate_version_is_v2(self, tmp_data_root: Path) -> None:
        """Policy version is written into every decision so a hold recorded
        months ago stays attributable to the policy that produced it."""
        _happy_day(FIXED_DATE)
        assert gate_mod.decide(FIXED_DATE).gate_version == "v2"


# ---------------------------------------------------------------------------
# Gate policy v2 -- the informational revision check.
# ---------------------------------------------------------------------------

class TestRevisionStatusCheck:
    def test_absent_log_reads_as_no_revision(self, tmp_data_root: Path) -> None:
        _happy_day(FIXED_DATE)
        check = _check(gate_mod.decide(FIXED_DATE), "revision_status")
        assert check.blocking is False
        assert check.passed is True
        assert "no revision loop" in check.detail

    def test_counts_cycles_and_change_statuses(self, tmp_data_root: Path) -> None:
        """One RevisionCycle per line, each carrying nested changes. The
        cycle COUNT comes from the line's `cycle` field, the applied and
        rejected counts from the statuses inside it."""
        _happy_day(FIXED_DATE)
        _write_revisions(FIXED_DATE, [
            {"cycle": 1, "mode": "live", "changes": [
                {"status": "applied"}, {"status": "rejected"},
            ]},
            {"cycle": 2, "mode": "live", "changes": [{"status": "applied"}]},
        ])
        check = _check(gate_mod.decide(FIXED_DATE), "revision_status")
        assert "2 revision cycle(s)" in check.detail
        assert "2 applied / 1 rejected" in check.detail

    def test_shadow_proposals_are_reported_separately_from_applied(
        self, tmp_data_root: Path
    ) -> None:
        """A shadow cycle changed nothing. Folding its proposals into
        'applied' would tell the operator the engine edited an issue it
        never touched."""
        _happy_day(FIXED_DATE)
        _write_revisions(FIXED_DATE, [
            {"cycle": 1, "mode": "shadow", "changes": [
                {"status": "proposed"}, {"status": "proposed"},
            ]},
        ])
        detail = _check(gate_mod.decide(FIXED_DATE), "revision_status").detail
        assert "0 applied / 0 rejected" in detail
        assert "2 proposed (shadow)" in detail

    def test_revisions_never_block_the_decision(self, tmp_data_root: Path) -> None:
        """A busy revision loop is not evidence against publishing. Whatever
        it changed was re-reviewed, and that re-review is the blocking
        evidence -- counting it twice would just make the gate timid."""
        _happy_day(FIXED_DATE)
        _write_revisions(FIXED_DATE, [
            {"cycle": 1, "mode": "live",
             "changes": [{"status": "applied"} for _ in range(12)]},
        ])
        assert gate_mod.decide(FIXED_DATE).decision == "auto_merge"

    def test_unreadable_lines_are_surfaced_but_still_do_not_block(
        self, tmp_data_root: Path
    ) -> None:
        _happy_day(FIXED_DATE)
        _write_revisions(FIXED_DATE, [
            {"cycle": 1, "mode": "live", "changes": [{"status": "applied"}]},
            "{ truncated mid-write",
        ])
        decision = gate_mod.decide(FIXED_DATE)
        check = _check(decision, "revision_status")
        assert check.passed is False
        assert check.blocking is False
        assert "1 unreadable line(s)" in check.detail
        assert decision.decision == "auto_merge"

    def test_a_line_without_a_cycle_number_still_counts_as_a_cycle(
        self, tmp_data_root: Path
    ) -> None:
        """Reporting "0 cycles" next to a non-empty log would read as "the
        loop never ran", which is the one thing the log proves false."""
        _happy_day(FIXED_DATE)
        _write_revisions(FIXED_DATE, [{"changes": [{"status": "applied"}]}])
        assert "1 revision cycle(s)" in _check(
            gate_mod.decide(FIXED_DATE), "revision_status"
        ).detail


# ---------------------------------------------------------------------------
# The revision loop (src/run.py).
#
# These tests are about ORCHESTRATION and nothing else: how many times the
# loop runs, when it stops, and what happens when a piece of it explodes.
# `revise_day`, `verify_day`, render and review are all mocked -- what
# `src/revise.py` decides to change is the LLM Engineer's contract and is
# tested there. What run.py owns is the termination condition, and a
# termination condition is exactly the kind of thing that must be pinned:
# a loop that stops one cycle late costs money every morning, and one that
# never stops costs the issue.
# ---------------------------------------------------------------------------

def _change(status: str, story_id: str | None = VALID_CLUSTER_ID) -> dict:
    """A `RevisionChange`-shaped mapping with only the fields the loop reads.

    ``story_id=None`` models a section-intro edit, which has no story behind
    it -- the case that must NOT trigger a re-verify.
    """
    target = (
        {"kind": "story", "story_id": story_id, "field": "headline"}
        if story_id is not None
        else {"kind": "section", "story_id": None, "section": "hands_on",
              "field": "intro_lead"}
    )
    return {"status": status, "target": target}


class _FakeRevisionCycle:
    """Stand-in for `RevisionCycle` -- one line of revisions.jsonl."""

    def __init__(
        self,
        applied: int = 1,
        rejected: int = 0,
        proposed: int = 0,
        touched: list[str] | None = None,
    ) -> None:
        ids = touched if touched is not None else [VALID_CLUSTER_ID]
        self.changes = (
            [_change("applied", ids[i % len(ids)] if ids else None)
             for i in range(applied)]
            + [_change("rejected", VALID_CLUSTER_ID_2) for _ in range(rejected)]
            + [_change("proposed", VALID_CLUSTER_ID) for _ in range(proposed)]
        )


class _FakeRevisionReport:
    """Stand-in for what `revise_day` actually returns: a summary carrying
    the substantive cycle on ``.cycle``. The loop must read through it."""

    def __init__(self, ran: bool = True, note: str = "", **cycle_kwargs) -> None:
        self.cycle = _FakeRevisionCycle(**cycle_kwargs)
        self.ran = ran
        self.note = note
        self.applied = sum(1 for c in self.cycle.changes if c["status"] == "applied")
        self.rejected = sum(1 for c in self.cycle.changes if c["status"] == "rejected")
        self.proposed = sum(1 for c in self.cycle.changes if c["status"] == "proposed")


@pytest.fixture
def loop_spy(monkeypatch: pytest.MonkeyPatch):
    """Patch every seam the live loop reaches through and record the calls.

    Returns a dict of call logs plus knobs for the scripted responses:
      ``reports``  -- the RevisionReport returned by each successive cycle
      ``verdicts`` -- the verdict sequence `_current_review_verdict` returns,
                      starting with the pre-loop read
    """
    calls: dict[str, list] = {
        "revise": [], "verify": [], "render": [], "review": [],
    }
    state: dict[str, list] = {"reports": [], "verdicts": []}

    def fake_revise(run_date, *, shadow):
        calls["revise"].append(shadow)
        reports = state["reports"]
        idx = min(len(calls["revise"]) - 1, len(reports) - 1)
        return reports[idx] if reports else _FakeRevisionCycle()

    def fake_reverify(run_date, story_ids):
        calls["verify"].append(list(story_ids))
        return "clean"

    def fake_render(run_date):
        calls["render"].append(run_date)
        return "preview -> /tmp/x.html"

    def fake_review(run_date):
        calls["review"].append(run_date)
        return "GREEN -- reads well"

    def fake_verdict(run_date):
        verdicts = state["verdicts"]
        idx = len(calls["review"])
        if idx < len(verdicts):
            return verdicts[idx]
        return verdicts[-1] if verdicts else None

    monkeypatch.setattr(run_mod, "_revise_once", fake_revise)
    monkeypatch.setattr(run_mod, "_reverify_stories", fake_reverify)
    monkeypatch.setattr(run_mod, "_run_render_preview", fake_render)
    monkeypatch.setattr(run_mod, "_run_review", fake_review)
    monkeypatch.setattr(run_mod, "_current_review_verdict", fake_verdict)
    monkeypatch.delenv(run_mod.REVISE_MODE_ENV_VAR, raising=False)
    return {"calls": calls, "state": state}


class TestReviseModeResolution:
    def test_unset_env_defaults_to_shadow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A control that can rewrite the issue unattended defaults to
        proposing, exactly like the publish gate defaults to shadow."""
        monkeypatch.delenv(run_mod.REVISE_MODE_ENV_VAR, raising=False)
        assert run_mod.resolve_revise_mode() == ("shadow", "")

    def test_env_selects_live_case_insensitively(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(run_mod.REVISE_MODE_ENV_VAR, "LIVE")
        assert run_mod.resolve_revise_mode() == ("live", "")

    def test_unrecognised_value_falls_back_to_shadow_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A typo'd variable must never silently license the loop to edit."""
        monkeypatch.setenv(run_mod.REVISE_MODE_ENV_VAR, "yes-please")
        mode, note = run_mod.resolve_revise_mode()
        assert mode == "shadow"
        assert "yes-please" in note

    def test_explicit_argument_beats_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(run_mod.REVISE_MODE_ENV_VAR, "live")
        assert run_mod.resolve_revise_mode("off") == ("off", "")

    def test_verdict_rank_puts_unreadable_below_red(self) -> None:
        """The improvement test leans entirely on this ordering: if an
        unreadable review outranked red, a cycle that destroyed the review
        would look like progress and the loop would keep going."""
        assert run_mod._verdict_rank("green") > run_mod._verdict_rank("amber")
        assert run_mod._verdict_rank("amber") > run_mod._verdict_rank("red")
        assert run_mod._verdict_rank("red") > run_mod._verdict_rank(None)
        assert run_mod._verdict_rank("unavailable") < run_mod._verdict_rank("red")
        assert run_mod._verdict_rank("who-knows") < run_mod._verdict_rank("red")


class TestReviseShadowMode:
    def test_shadow_calls_the_reviser_once_and_changes_nothing(
        self, loop_spy
    ) -> None:
        """Shadow is a proposal pass: one call, and none of the three stages
        that would bake a proposal into the issue."""
        loop_spy["state"]["reports"] = [_FakeRevisionCycle(applied=0, rejected=1, proposed=3)]

        outcome = run_mod._maybe_revise(FIXED_DATE, mode="shadow")

        assert loop_spy["calls"]["revise"] == [True]  # shadow=True
        assert loop_spy["calls"]["verify"] == []
        assert loop_spy["calls"]["render"] == []
        assert loop_spy["calls"]["review"] == []
        assert outcome.summary.startswith("shadow -- 3 proposed / 1 rejected")
        assert outcome.review_summary is None

    def test_shadow_is_the_default_when_no_mode_is_given(self, loop_spy) -> None:
        run_mod._maybe_revise(FIXED_DATE)
        assert loop_spy["calls"]["revise"] == [True]

    def test_off_calls_nothing_at_all(self, loop_spy) -> None:
        """The kill switch has to be a real one: mode=off spends no tokens,
        which is the difference between 'disabled' and 'disabled but still
        billing'."""
        outcome = run_mod._maybe_revise(FIXED_DATE, mode="off")
        assert loop_spy["calls"]["revise"] == []
        assert outcome.summary is None


class TestReviseLiveLoop:
    def test_each_cycle_runs_revise_verify_render_review_in_order(
        self, loop_spy
    ) -> None:
        loop_spy["state"]["reports"] = [_FakeRevisionCycle(applied=1)]
        loop_spy["state"]["verdicts"] = ["red", "amber", "green"]

        run_mod._maybe_revise(FIXED_DATE, mode="live")

        calls = loop_spy["calls"]
        assert calls["revise"] == [False, False]  # shadow=False -- real edits
        assert len(calls["verify"]) == len(calls["render"]) == len(calls["review"]) == 2

    def test_stops_at_two_cycles_even_when_still_improving(
        self, loop_spy
    ) -> None:
        """The cap is the backstop against a reviser that always finds one
        more thing to fix. Improvement at every step must not buy a third
        cycle."""
        loop_spy["state"]["reports"] = [_FakeRevisionCycle(applied=1)]
        loop_spy["state"]["verdicts"] = ["red", "amber", "green", "green"]

        outcome = run_mod._maybe_revise(FIXED_DATE, mode="live")

        assert len(loop_spy["calls"]["revise"]) == run_mod.MAX_REVISION_CYCLES == 2
        assert "2 cycle(s)" in outcome.summary

    def test_stops_early_when_the_verdict_does_not_improve(
        self, loop_spy
    ) -> None:
        """amber -> amber is not progress. Paying for a second cycle on the
        strength of a first one that changed nothing measurable is how a
        loop quietly doubles the nightly bill."""
        loop_spy["state"]["reports"] = [_FakeRevisionCycle(applied=2)]
        loop_spy["state"]["verdicts"] = ["amber", "amber"]

        outcome = run_mod._maybe_revise(FIXED_DATE, mode="live")

        assert len(loop_spy["calls"]["revise"]) == 1
        assert "did not improve" in outcome.summary
        assert "amber -> amber" in outcome.summary

    def test_stops_early_when_the_verdict_gets_worse(self, loop_spy) -> None:
        """The reviser can degrade an issue. It gets exactly one chance to."""
        loop_spy["state"]["reports"] = [_FakeRevisionCycle(applied=1)]
        loop_spy["state"]["verdicts"] = ["green", "red"]

        outcome = run_mod._maybe_revise(FIXED_DATE, mode="live")

        assert len(loop_spy["calls"]["revise"]) == 1
        assert "green -> red" in outcome.summary

    def test_stops_when_the_verdict_reaches_green(self, loop_spy) -> None:
        """Green is the ceiling. A second cycle cannot improve on it, so
        running one would be spending tokens to confirm what we know."""
        loop_spy["state"]["reports"] = [_FakeRevisionCycle(applied=1)]
        loop_spy["state"]["verdicts"] = ["amber", "green"]

        outcome = run_mod._maybe_revise(FIXED_DATE, mode="live")

        assert len(loop_spy["calls"]["revise"]) == 1
        assert "ceiling" in outcome.summary

    def test_stops_immediately_when_the_reviser_applies_nothing(
        self, loop_spy
    ) -> None:
        """Nothing on disk changed, so re-verify, render and review would all
        reproduce byte-identical outputs at full LLM price."""
        loop_spy["state"]["reports"] = [_FakeRevisionCycle(applied=0, rejected=4)]
        loop_spy["state"]["verdicts"] = ["amber"]

        outcome = run_mod._maybe_revise(FIXED_DATE, mode="live")

        assert loop_spy["calls"]["revise"] == [False]
        assert loop_spy["calls"]["verify"] == []
        assert loop_spy["calls"]["review"] == []
        assert "applied nothing" in outcome.summary

    def test_reverify_is_scoped_to_the_stories_the_reviser_touched(
        self, loop_spy
    ) -> None:
        """Re-verifying the whole issue after a one-headline edit is the
        No Token Wasted violation this scoping exists to avoid."""
        loop_spy["state"]["reports"] = [
            _FakeRevisionCycle(applied=1, touched=[VALID_CLUSTER_ID_2])
        ]
        loop_spy["state"]["verdicts"] = ["amber", "amber"]

        run_mod._maybe_revise(FIXED_DATE, mode="live")

        assert loop_spy["calls"]["verify"] == [[VALID_CLUSTER_ID_2]]

    def test_a_section_only_edit_reverifies_nothing(self, loop_spy) -> None:
        """A rewritten section intro carries no factual claim tied to a
        source excerpt -- verify reads story headlines and summaries only.
        Empty means "we checked, nothing verifiable moved"."""
        loop_spy["state"]["reports"] = [_FakeRevisionCycle(applied=1, touched=[])]
        loop_spy["state"]["verdicts"] = ["amber", "amber"]

        run_mod._maybe_revise(FIXED_DATE, mode="live")

        assert loop_spy["calls"]["verify"] == [[]]
        # The cycle still re-renders and re-reviews: the issue text changed.
        assert len(loop_spy["calls"]["render"]) == 1

    def test_an_unreadable_change_list_reverifies_everything(
        self, loop_spy
    ) -> None:
        """"I cannot tell what changed" and "nothing changed" are different
        states, and only one of them is safe to skip."""
        class NoChangeList:
            changes = "not a list"

        loop_spy["state"]["reports"] = [NoChangeList()]
        loop_spy["state"]["verdicts"] = ["amber", "amber"]

        assert run_mod._revision_touched_story_ids(NoChangeList()) is None

    def test_the_post_loop_verdict_is_the_one_reported(self, loop_spy) -> None:
        loop_spy["state"]["reports"] = [_FakeRevisionCycle(applied=1)]
        loop_spy["state"]["verdicts"] = ["red", "green"]

        outcome = run_mod._maybe_revise(FIXED_DATE, mode="live")

        assert "red -> green" in outcome.summary
        assert outcome.review_summary == "GREEN -- reads well"


class TestReviseFailureContainment:
    def test_a_raising_reviser_never_kills_the_run(
        self, loop_spy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An improvement that can destroy the day's issue is not one."""
        def boom(run_date, *, shadow):
            raise RuntimeError("the reviser fell over")

        monkeypatch.setattr(run_mod, "_revise_once", boom)
        outcome = run_mod._maybe_revise(FIXED_DATE, mode="live")

        assert outcome.summary.startswith("[advisory-guard] RuntimeError")
        assert outcome.review_summary is None

    def test_a_missing_revise_module_is_contained(
        self, loop_spy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Before src/revise.py lands -- and after any future rename -- the
        pipeline must still finish and leave a publishable draft."""
        def missing(run_date, *, shadow):
            raise ImportError("No module named 'src.revise'")

        monkeypatch.setattr(run_mod, "_revise_once", missing)
        assert "[advisory-guard]" in run_mod._maybe_revise(FIXED_DATE).summary

    def test_a_mid_cycle_failure_keeps_the_earlier_cycles(
        self, loop_spy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A render that fails on cycle 2 does not un-run cycle 1. The issue
        on disk is the revised one with a review that may not match it -- and
        the gate's freshness check is what holds that, not this loop."""
        loop_spy["state"]["reports"] = [_FakeRevisionCycle(applied=1)]
        loop_spy["state"]["verdicts"] = ["red", "amber", "green"]
        calls = loop_spy["calls"]

        def flaky_render(run_date):
            calls["render"].append(run_date)
            if len(calls["render"]) == 2:
                raise OSError("disk full")
            return "preview"

        monkeypatch.setattr(run_mod, "_run_render_preview", flaky_render)
        outcome = run_mod._maybe_revise(FIXED_DATE, mode="live")

        assert "[advisory-guard] OSError" in outcome.summary
        assert len(calls["revise"]) == 2
        assert len(calls["review"]) == 1  # cycle 2 died before its review

    def test_a_shape_change_in_the_cycle_degrades_to_stopping(
        self, loop_spy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`RevisionCycle` is the LLM Engineer's shape. If it renames
        ``changes``, the loop must read zero and stop -- not raise."""
        class Unfamiliar:
            note = "a report with none of the fields we know"

        monkeypatch.setattr(
            run_mod, "_revise_once", lambda run_date, *, shadow: Unfamiliar()
        )
        loop_spy["state"]["verdicts"] = ["amber"]

        outcome = run_mod._maybe_revise(FIXED_DATE, mode="live")

        assert "applied nothing" in outcome.summary
        assert loop_spy["calls"]["review"] == []


class TestRevisePipelineWiring:
    """Where the loop sits in `aiv run`, mocked down to the stage dispatcher."""

    @pytest.fixture(autouse=True)
    def _stub_stages(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(run_mod, "_run_stage", lambda name, d: (True, "ok"))
        recorded: list[str | None] = []

        def fake_revise(run_date, *, mode=None):
            recorded.append(mode)
            return run_mod.RevisionOutcome("live -- stub")

        monkeypatch.setattr(run_mod, "_maybe_revise", fake_revise)
        return recorded

    def test_the_loop_runs_after_a_pipeline_that_reviewed(
        self, tmp_data_root: Path, _stub_stages
    ) -> None:
        code = run_mod._run_pipeline(
            FIXED_DATE, ["render", "review"], dry_run=False, skip_preflight=True,
        )
        assert code == 0
        assert _stub_stages == [None]

    def test_the_loop_is_skipped_when_review_did_not_run(
        self, tmp_data_root: Path, _stub_stages
    ) -> None:
        """No review means no findings, and no findings means nothing to act
        on -- calling the reviser anyway would be paying for a prompt with no
        input."""
        run_mod._run_pipeline(
            FIXED_DATE, ["fetch", "cluster"], dry_run=False, skip_preflight=True,
        )
        assert _stub_stages == []

    def test_no_revise_flag_forces_mode_off(
        self, tmp_data_root: Path, _stub_stages
    ) -> None:
        run_mod._run_pipeline(
            FIXED_DATE, ["render", "review"], dry_run=False,
            skip_preflight=True, revise_mode="off",
        )
        assert _stub_stages == ["off"]

    def test_a_failed_stage_stops_before_the_loop(
        self, tmp_data_root: Path, _stub_stages, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reviser must never be handed a draft from a broken run."""
        monkeypatch.setattr(
            run_mod, "_run_stage",
            lambda name, d: (False, "boom") if name == "render" else (True, "ok"),
        )
        code = run_mod._run_pipeline(
            FIXED_DATE, ["render", "review"], dry_run=False, skip_preflight=True,
        )
        assert code == 1
        assert _stub_stages == []


class TestRevisionContractAgainstTheRealModel:
    """The seam tests above use hand-built mappings so they stay readable.

    These two pin the same readers against the ACTUAL `RevisionCycle` model,
    which is what `src/revise.py` writes. Mock-shaped tests that drift from
    the real shape are how a green suite ships a loop that stops on cycle
    one every single night.
    """

    @staticmethod
    def _real_cycle(cycle: int = 1, mode: str = "live"):
        from src.models import ReviewTarget, RevisionChange, RevisionCycle

        applied_status = "proposed" if mode == "shadow" else "applied"
        return RevisionCycle(
            date=FIXED_DATE,
            cycle=cycle,
            mode=mode,  # type: ignore[arg-type]
            changes=[
                RevisionChange(
                    target=ReviewTarget(
                        kind="story", story_id=VALID_CLUSTER_ID, field="headline",
                    ),
                    before="A small open model handles invoices on-device",
                    after="A 4B open model reads invoices without the cloud",
                    recommendation="Name the size in the headline.",
                    status=applied_status,  # type: ignore[arg-type]
                ),
                RevisionChange(
                    target=ReviewTarget(
                        kind="section", section="hands_on", field="intro_lead",
                    ),
                    before="Three things worth trying today.",
                    after="Three things worth trying before lunch.",
                    recommendation="Sharpen the lead.",
                    status="rejected",
                    reject_reason="edit_distance_exceeded",
                ),
            ],
            generated_at=FIXED_NOW,
            prompt_version="v1",
        )

    def test_the_loop_reads_a_real_revision_cycle(self) -> None:
        cycle = self._real_cycle()
        tally = run_mod._revision_tally(cycle)
        assert (tally.applied, tally.rejected, tally.proposed) == (1, 1, 0)
        # The rejected section change contributes nothing: only APPLIED
        # story edits change text the verifier reads.
        assert run_mod._revision_touched_story_ids(cycle) == [VALID_CLUSTER_ID]

    def test_the_loop_reads_a_real_shadow_cycle_as_proposals(self) -> None:
        tally = run_mod._revision_tally(self._real_cycle(mode="shadow"))
        assert (tally.applied, tally.proposed) == (0, 1)

    def test_the_gate_reads_a_real_revisions_jsonl(
        self, tmp_data_root: Path
    ) -> None:
        _happy_day(FIXED_DATE)
        path = gate_mod.revisions_path(FIXED_DATE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(
                self._real_cycle(cycle=n).model_dump_json() for n in (1, 2)
            ) + "\n",
            encoding="utf-8",
        )

        state = gate_mod.read_revision_state(FIXED_DATE)
        assert (state.cycles, state.applied, state.rejected) == (2, 2, 2)
        assert state.bad_lines == 0
        assert gate_mod.decide(FIXED_DATE).decision == "auto_merge"

    def test_the_gate_reads_a_real_review_report(self, tmp_data_root: Path) -> None:
        """review.json is written by `src/review.py` from a `ReviewReport`.
        The gate must read the model's own serialisation, not just the
        hand-built fixture above."""
        from src.models import ReviewReport

        _write_issue(FIXED_DATE, _issue())
        _write_verify(FIXED_DATE, verdict="clean")
        report = ReviewReport(
            generated_at=FIXED_NOW,
            computed_verdict="green",
            one_line="A steady day with one real release.",
            prompt_version="v0.6",
            issue_sha256=gate_mod.issue_sha256(FIXED_DATE) or "unknown",
        )
        path = gate_mod.review_json_path(FIXED_DATE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")

        decision = gate_mod.decide(FIXED_DATE)
        assert decision.decision == "auto_merge"
        assert decision.review.verdict == "green"
        assert decision.review.fresh is True
        assert decision.review.prompt_version == "v0.6"


class TestLoopReadsTheRealRevisionReport:
    """`revise_day` returns a `RevisionReport`, not a bare `RevisionCycle`.

    The loop reads its counters and, for the re-verify scope, reads THROUGH
    it to `.cycle.changes`. If that indirection breaks, `_revision_tally`
    silently reads zero and every live run stops after one cycle without
    ever re-reviewing -- a failure that looks exactly like "the reviser had
    nothing to do" in the log.
    """

    def test_counters_come_off_the_report(self, loop_spy) -> None:
        report = _FakeRevisionReport(applied=2, rejected=1)
        tally = run_mod._revision_tally(report)
        assert (tally.applied, tally.rejected) == (2, 1)

    def test_touched_stories_are_read_through_to_the_cycle(self) -> None:
        report = _FakeRevisionReport(applied=1, touched=[VALID_CLUSTER_ID_2])
        assert run_mod._revision_touched_story_ids(report) == [VALID_CLUSTER_ID_2]

    def test_a_live_loop_runs_a_full_cycle_on_the_real_shape(
        self, loop_spy
    ) -> None:
        """The end-to-end version of the two above: with the wrapper shape,
        cycle one must actually re-verify, re-render and re-review."""
        loop_spy["state"]["reports"] = [
            _FakeRevisionReport(applied=1, touched=[VALID_CLUSTER_ID_2])
        ]
        loop_spy["state"]["verdicts"] = ["red", "green"]

        outcome = run_mod._maybe_revise(FIXED_DATE, mode="live")

        assert loop_spy["calls"]["verify"] == [[VALID_CLUSTER_ID_2]]
        assert len(loop_spy["calls"]["review"]) == 1
        assert "red -> green" in outcome.summary

    def test_a_refusal_reads_as_a_decline_not_an_empty_edit(
        self, loop_spy
    ) -> None:
        """The engine refuses when there is no review, or a stale one. That
        is not the same as trying and finding nothing, and the summary the
        operator reads at 06:00 must not conflate them."""
        loop_spy["state"]["reports"] = [
            _FakeRevisionReport(ran=False, note="stale review", applied=0)
        ]
        loop_spy["state"]["verdicts"] = ["amber"]

        outcome = run_mod._maybe_revise(FIXED_DATE, mode="live")

        assert "declined: stale review" in outcome.summary
        assert loop_spy["calls"]["review"] == []

    def test_the_real_report_class_round_trips(self, loop_spy) -> None:
        """Built from the actual dataclass in src/revise.py, so a field
        rename over there fails here rather than in production."""
        from src.revise import RevisionReport

        report = RevisionReport(
            date=FIXED_DATE, mode="live", ran=True,
            applied=2, proposed=0, rejected=1,
            path=Path("data/staging/x/revisions.jsonl"),
        )
        tally = run_mod._revision_tally(report)
        assert (tally.applied, tally.rejected, tally.proposed) == (2, 1, 0)
        # No cycle attached -> the scope is unknown -> re-verify everything.
        assert run_mod._revision_touched_story_ids(report) is None
