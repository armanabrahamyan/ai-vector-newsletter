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
