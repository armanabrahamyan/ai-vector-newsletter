"""Unit tests for src/review.py and the run.py wiring around it.

Scope. The deterministic plumbing in review: prompt assembly, prior-issue
lookup, the threshold table, the verbatim-quote filter, finding-id
assignment, the model split, review.md rendering, frontmatter parsing,
artifact write, pipeline integration, the ``--no-review`` escape hatch, the
unavailable/unparseable fallbacks, and the standalone ``aiv review`` CLI.

We mock the boundary -- ``src.review._call_review_llm`` (or, where the
env-handling seam is the thing under test, ``src.rank._llm_call``) -- and
assert on the unit's own transformations. We do NOT mock the unit under
test. Per ``tests/CONVENTIONS.md``.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from src import paths
from src import review as review_mod
from src import run as run_mod
from src.review import (
    REVIEW_PROMPT_VERSION,
    ReviewArtifact,
    ReviewThresholdError,
    _build_review_prompt,
    _build_review_prompt_parts,
    _extract_frontmatter_summary,
    _load_recent_released_issues,
    _write_review_artifact,
    compute_verdict,
    load_thresholds,
    quote_present,
    render_review_markdown,
    run_review,
)


# ---------------------------------------------------------------------------
# Test data helpers.
# ---------------------------------------------------------------------------

def _story_id(ix: int) -> str:
    """Synthetic cluster id matching the ``^c_[0-9a-f]{12,}$`` pattern."""
    return f"c_{'a' * 12}{ix:02x}"


def _make_issue_payload(
    date_str: str = "2026-05-29",
    pulse_headline: str = "Today's defining story",
    pulse_summary: str = "A clear pick that carries the day's direction.",
    big_picture_headlines: list[str] | None = None,
    hands_on_headlines: list[str] | None = None,
    currents_headlines: list[str] | None = None,
    big_picture_intro: str = "Systems beat single points.",
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal staged-issue payload that matches the fields the
    review module reads. We work from raw dicts rather than pydantic
    models because review.py walks the JSON without re-validating.
    """
    big_picture_headlines = big_picture_headlines or ["BP story one"]
    hands_on_headlines = hands_on_headlines or ["HO story one"]
    currents_headlines = currents_headlines or ["Currents story one"]

    def _story(hl: str, ix: int) -> dict[str, Any]:
        return {
            "story_id": _story_id(ix),
            "headline": hl,
            "summary": f"Summary for {hl}.",
            "source_urls": [f"https://example.com/{ix}"],
            "prior_coverage_ref": None,
            "signal": "watch",
            "verification": verification if ix == 1 else None,
        }

    return {
        "schema_version": 6,
        "issue_number": None,
        "revision": 0,
        "date": date_str,
        "pulse": {
            "schema_version": 3,
            "name": "pulse",
            "stories": [_story(pulse_headline, 0) | {"summary": pulse_summary}],
            "intro_lead": None,
            "intro_body": None,
        },
        "sections": [
            {
                "schema_version": 3,
                "name": "big_picture",
                "stories": [_story(h, i + 1) for i, h in enumerate(big_picture_headlines)],
                "intro_lead": big_picture_intro,
                "intro_body": "Across the day, the same shape repeats.",
            },
            {
                "schema_version": 3,
                "name": "hands_on",
                "stories": [_story(h, i + 10) for i, h in enumerate(hands_on_headlines)],
                "intro_lead": "Verify before you deploy.",
                "intro_body": "Pull the artefact, measure against your baseline.",
            },
            {
                "schema_version": 3,
                "name": "currents",
                "stories": [_story(h, i + 20) for i, h in enumerate(currents_headlines)],
                "intro_lead": "Benchmarks are under pressure.",
                "intro_body": "Treat early signals with scepticism.",
            },
        ],
        "generated_at": "2026-05-31T05:55:26Z",
        "prompt_versions": {"rank": "v0.6", "summarise": "v0.12", "pulse": "v0.10"},
        "notes": "shape: green -- pulse: 1, big_picture: 1, hands_on: 1, currents: 1",
    }


def _finding(
    *,
    story_id: str | None = None,
    section: str | None = "big_picture",
    kind: str = "story",
    field: str = "summary",
    criterion: str = "closing_shape",
    severity: str = "minor",
    quote: str = "Summary for BP story one.",
    fix_kind: str = "text_edit",
    instruction: str = "Close on a strategic question.",
) -> dict[str, Any]:
    """One raw finding dict as the reviewer LLM would emit it."""
    target: dict[str, Any] = {"kind": kind, "field": field}
    if kind == "story":
        target["story_id"] = story_id or _story_id(1)
    if section is not None:
        target["section"] = section
    return {
        "target": target,
        "criterion": criterion,
        "severity": severity,
        "quote": quote,
        "fix_kind": fix_kind,
        "instruction": instruction,
    }


def _fake_response(
    findings: list[dict[str, Any]] | None = None,
    summary: str = "Strong day; closing shapes hold across sections",
) -> str:
    """The reviewer's JSON response, as raw text."""
    return json.dumps({"findings": findings or [], "summary": summary})


_FAKE_LLM_RESPONSE = _fake_response()
"""The default stubbed reviewer response: no findings -> green."""


_REVIEW_MD_WITH_FRONTMATTER = """\
---
verdict: green
one_line: Strong day; closing shapes hold across sections
issue_date: 2026-05-29
issue_shape: green
---

# Editor's Review -- 2026-05-29

**Verdict**: GREEN. The shape holds and the Pulse carries the day.

## Recommendations before release
- Ratify as-is.
"""
"""A rendered review document, used by the frontmatter-reader and
artifact-write tests (which are about the Markdown surface, not the LLM)."""


# ---------------------------------------------------------------------------
# The threshold table.
# ---------------------------------------------------------------------------

class TestLoadThresholds:
    """The table is the difference between "the editor found three things"
    and "hold the issue". A malformed table must fail the stage closed, not
    fall back to a default nobody ratified -- so every rejection below is
    load-bearing."""

    def test_shipped_table_loads(self) -> None:
        """The committed config must parse. If it doesn't, every review in
        production silently degrades to ``unavailable``."""
        table = load_thresholds()
        assert table["version"]
        assert table["rules"]

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ReviewThresholdError):
            load_thresholds(tmp_path / "nope.yaml")

    def test_unknown_severity_is_rejected(self, tmp_path: Path) -> None:
        """A severity we don't recognise means a rule that would never fire.
        Silently ignoring it would remove a hold nobody noticed removing."""
        path = tmp_path / "t.yaml"
        path.write_text(
            "version: v1\ndefault_verdict: green\n"
            "rules:\n  - verdict: red\n    when:\n      catastrophic: {min: 1}\n"
        )
        with pytest.raises(ReviewThresholdError, match="unknown severity"):
            load_thresholds(path)

    def test_machine_state_as_default_verdict_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """``unavailable`` is a code-authored state meaning "no judgement
        was recorded". A table must not be able to make it an outcome."""
        path = tmp_path / "t.yaml"
        path.write_text(
            "version: v1\ndefault_verdict: unavailable\n"
            "rules:\n  - verdict: red\n    when:\n      blocking: {min: 1}\n"
        )
        with pytest.raises(ReviewThresholdError, match="default_verdict"):
            load_thresholds(path)

    def test_zero_minimum_is_rejected(self, tmp_path: Path) -> None:
        """A rule with ``min: 0`` fires on every issue ever published."""
        path = tmp_path / "t.yaml"
        path.write_text(
            "version: v1\ndefault_verdict: green\n"
            "rules:\n  - verdict: red\n    when:\n      major: {min: 0}\n"
        )
        with pytest.raises(ReviewThresholdError, match=">= 1"):
            load_thresholds(path)

    def test_missing_version_is_rejected(self, tmp_path: Path) -> None:
        """A verdict must be attributable to a specific ratified table."""
        path = tmp_path / "t.yaml"
        path.write_text(
            "default_verdict: green\n"
            "rules:\n  - verdict: red\n    when:\n      blocking: {min: 1}\n"
        )
        with pytest.raises(ReviewThresholdError, match="version"):
            load_thresholds(path)

    def test_empty_rules_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "t.yaml"
        path.write_text("version: v1\ndefault_verdict: green\nrules: []\n")
        with pytest.raises(ReviewThresholdError, match="rules"):
            load_thresholds(path)


class TestComputeVerdict:
    """The ratified policy, exercised against the SHIPPED table: any
    blocking -> red; three majors -> red; one major or one minor -> amber;
    notes only -> green."""

    @pytest.fixture
    def table(self) -> dict[str, Any]:
        return load_thresholds()

    @pytest.mark.parametrize(
        "counts,expected",
        [
            ({"blocking": 1}, "red"),
            ({"major": 3}, "red"),
            ({"major": 1}, "amber"),
            ({"minor": 1}, "amber"),
            ({"note": 9}, "green"),
            ({}, "green"),
        ],
    )
    def test_policy(
        self, table: dict[str, Any], counts: dict[str, int], expected: str
    ) -> None:
        verdict, _reason = compute_verdict(counts, table)
        assert verdict == expected

    def test_blocking_outranks_a_matching_amber_rule(
        self, table: dict[str, Any]
    ) -> None:
        """Rule ORDER is the mechanism. One blocking finding alongside five
        minors must still be red -- if the amber rule were consulted first,
        a liability finding would ship with a note."""
        verdict, _ = compute_verdict({"blocking": 1, "minor": 5}, table)
        assert verdict == "red"

    def test_notes_never_escalate(self, table: dict[str, Any]) -> None:
        """Notes are observations. Twenty of them is still a clean issue the
        editor had things to say about."""
        verdict, _ = compute_verdict({"note": 20}, table)
        assert verdict == "green"


# ---------------------------------------------------------------------------
# The verbatim-quote filter -- the deterministic misfire kill.
# ---------------------------------------------------------------------------

class TestQuoteMatching:
    """``quote_present`` decides whether a finding has evidence. Too strict
    and we discard real complaints over a straightened apostrophe; too loose
    and a hallucinated quote passes."""

    def test_exact_span_matches(self) -> None:
        assert quote_present("beat single points", "Systems beat single points.")

    def test_rewrapped_whitespace_still_matches(self) -> None:
        assert quote_present("beat  single\npoints", "Systems beat single points.")

    def test_curly_apostrophe_matches_straight(self) -> None:
        assert quote_present("today’s pick", "The story is today's pick.")

    def test_absent_text_does_not_match(self) -> None:
        assert not quote_present(
            "no independent replication yet", "Systems beat single points."
        )

    def test_empty_quote_never_matches(self) -> None:
        """A finding with nothing to point at has no evidence."""
        assert not quote_present("", "Systems beat single points.")


class TestFindingFilter:
    """The reviewer's two documented misfire classes (2026-07-04) were both
    complaints about text that was not in the issue. These tests pin the
    check that ends that class of error."""

    def _run(
        self, tmp_data_root: Path, findings: list[dict[str, Any]]
    ) -> ReviewArtifact:
        date = _dt.date(2026, 5, 29)
        target = paths.staging_dir(date)
        target.mkdir(parents=True)
        (target / "issue.json").write_text(
            json.dumps(_make_issue_payload(date_str=date.isoformat()))
        )
        with patch(
            "src.review._call_review_llm",
            return_value=_fake_response(findings),
        ):
            return run_review(date=date)

    def test_hallucinated_quote_is_dropped(self, tmp_data_root: Path) -> None:
        artifact = self._run(tmp_data_root, [
            _finding(quote="no independent replication yet"),
        ])
        assert artifact.report is not None
        assert artifact.report.findings == []
        assert len(artifact.report.dropped_findings) == 1

    def test_dropped_finding_cannot_move_the_verdict(
        self, tmp_data_root: Path
    ) -> None:
        """The whole point: a BLOCKING finding whose quote is not in the
        issue must not hold the issue. Evidence first, then severity."""
        artifact = self._run(tmp_data_root, [
            _finding(
                severity="blocking",
                criterion="reputational_liability",
                quote="Acme Bank was found liable for fraud",
            ),
        ])
        assert artifact.verdict == "green"

    def test_real_quote_survives_the_filter(self, tmp_data_root: Path) -> None:
        artifact = self._run(tmp_data_root, [
            _finding(quote="Summary for BP story one."),
        ])
        assert artifact.report is not None
        assert len(artifact.report.findings) == 1
        assert artifact.verdict == "amber"

    def test_finding_against_an_unknown_story_is_dropped(
        self, tmp_data_root: Path
    ) -> None:
        """A target the issue does not contain cannot be checked or acted
        on -- and a review that names a story we never published is a
        review of some other issue."""
        artifact = self._run(tmp_data_root, [
            _finding(story_id="c_" + "f" * 12, quote="Summary for BP story one."),
        ])
        assert artifact.report is not None
        assert artifact.report.findings == []
        assert len(artifact.report.dropped_findings) == 1

    def test_malformed_finding_is_skipped_without_losing_the_others(
        self, tmp_data_root: Path
    ) -> None:
        """One bad entry must not cost the review its good ones."""
        artifact = self._run(tmp_data_root, [
            {"target": {"kind": "story"}, "criterion": "voice_adherence"},
            _finding(quote="Summary for BP story one."),
        ])
        assert artifact.report is not None
        assert len(artifact.report.findings) == 1

    def test_section_finding_resolves_against_the_intro(
        self, tmp_data_root: Path
    ) -> None:
        artifact = self._run(tmp_data_root, [
            _finding(
                kind="section", section="big_picture", field="intro_lead",
                quote="Systems beat single points.",
                criterion="section_intro", severity="major",
            ),
        ])
        assert artifact.report is not None
        assert len(artifact.report.findings) == 1
        assert artifact.verdict == "amber"

    def test_finding_ids_are_assigned_by_code_and_unique(
        self, tmp_data_root: Path
    ) -> None:
        """``RevisionChange.finding_ids`` references these. A model-authored
        duplicate would attribute an edit to the wrong concern."""
        artifact = self._run(tmp_data_root, [
            _finding(quote="Summary for BP story one."),
            _finding(quote="Summary for HO story one.", story_id=_story_id(10),
                     section="hands_on"),
            _finding(quote="not in the issue at all"),
        ])
        assert artifact.report is not None
        ids = [f.finding_id for f in artifact.report.findings]
        ids += [f.finding_id for f in artifact.report.dropped_findings]
        assert len(ids) == len(set(ids))
        assert all(i.startswith("f") for i in ids)


# ---------------------------------------------------------------------------
# Contract invariants on the new models.
# ---------------------------------------------------------------------------

class TestReviewContractInvariants:
    """The invariants this work added to src/models.py. Each one rejects a
    shape that would be uninterpretable downstream -- pydantic's own type
    checking would let all of them through."""

    def test_story_target_requires_a_story_id(self) -> None:
        """A story finding that names no story cannot be resolved to text on
        disk, which is exactly the misfire class the shape exists to kill."""
        from pydantic import ValidationError
        from src.models import ReviewTarget

        with pytest.raises(ValidationError, match="requires story_id"):
            ReviewTarget(kind="story", field="summary")

    def test_section_target_cannot_point_at_a_headline(self) -> None:
        from pydantic import ValidationError
        from src.models import ReviewTarget

        with pytest.raises(ValidationError, match="intro_lead"):
            ReviewTarget(kind="section", section="hands_on", field="headline")

    def test_section_target_must_not_carry_a_story_id(self) -> None:
        from pydantic import ValidationError
        from src.models import ReviewTarget

        with pytest.raises(ValidationError, match="must not carry a story_id"):
            ReviewTarget(
                kind="section", section="hands_on", field="intro_lead",
                story_id=_story_id(1),
            )

    def test_machine_state_cannot_carry_findings(self) -> None:
        """``unavailable`` means no judgement was recorded. Findings attached
        to one would let a reader believe a review happened."""
        from pydantic import ValidationError
        from src.models import ReviewFinding, ReviewReport, ReviewTarget

        finding = ReviewFinding(
            finding_id="f001",
            target=ReviewTarget(
                kind="story", story_id=_story_id(1), field="summary",
            ),
            criterion="voice_adherence", severity="minor",
            quote="text", fix_kind="text_edit", instruction="fix it",
        )
        with pytest.raises(ValidationError, match="empty findings"):
            ReviewReport(
                generated_at=_dt.datetime(2026, 5, 29, tzinfo=_dt.timezone.utc),
                computed_verdict="unavailable",
                findings=[finding],
                prompt_version="v1.0",
            )

    def test_duplicate_finding_ids_are_rejected(self) -> None:
        """``RevisionChange.finding_ids`` references these; a duplicate would
        attribute an edit to the wrong concern."""
        from pydantic import ValidationError
        from src.models import ReviewFinding, ReviewReport, ReviewTarget

        def _f() -> ReviewFinding:
            return ReviewFinding(
                finding_id="f001",
                target=ReviewTarget(
                    kind="story", story_id=_story_id(1), field="summary",
                ),
                criterion="voice_adherence", severity="minor",
                quote="text", fix_kind="text_edit", instruction="fix it",
            )

        with pytest.raises(ValidationError, match="duplicate finding_id"):
            ReviewReport(
                generated_at=_dt.datetime(2026, 5, 29, tzinfo=_dt.timezone.utc),
                computed_verdict="amber",
                findings=[_f(), _f()],
                prompt_version="v1.0",
            )


# ---------------------------------------------------------------------------
# The model split.
# ---------------------------------------------------------------------------

class TestReviewModelSplit:
    """A reviewer running the same model as the writer is measurably softer
    on it. The env plumbing that lets them differ is worth pinning, as is
    the restore -- a leaked LLM_MODEL would silently re-point every later
    stage in the same process."""

    def test_review_model_overrides_llm_model_for_the_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_MODEL", "writer-model")
        monkeypatch.setenv("REVIEW_MODEL", "judge-model")
        seen: dict[str, Any] = {}

        def _fake(prompt: str, *, temperature: float, max_tokens: int) -> str:
            seen["model"] = os.environ["LLM_MODEL"]
            seen["temperature"] = temperature
            return "{}"

        monkeypatch.setattr("src.rank._llm_call", _fake)
        review_mod._call_review_llm("prompt", timeout=5.0)
        assert seen["model"] == "judge-model"

    def test_llm_model_is_restored_after_the_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_MODEL", "writer-model")
        monkeypatch.setenv("REVIEW_MODEL", "judge-model")
        monkeypatch.setattr(
            "src.rank._llm_call",
            lambda *a, **k: "{}",
        )
        review_mod._call_review_llm("prompt", timeout=5.0)
        assert os.environ["LLM_MODEL"] == "writer-model"

    def test_falls_back_to_llm_model_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_MODEL", "writer-model")
        monkeypatch.delenv("REVIEW_MODEL", raising=False)
        assert review_mod._resolve_review_model() == "writer-model"

    def test_temperature_is_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The verdict is computed downstream, so run-to-run variance in the
        findings buys nothing and costs verdict stability."""
        monkeypatch.setenv("LLM_MODEL", "writer-model")
        seen: dict[str, Any] = {}

        def _fake(prompt: str, *, temperature: float, max_tokens: int) -> str:
            seen["temperature"] = temperature
            return "{}"

        monkeypatch.setattr("src.rank._llm_call", _fake)
        review_mod._call_review_llm("prompt", timeout=5.0)
        assert seen["temperature"] == 0.0

    def test_recorded_model_is_the_reviewer_not_the_writer(
        self, tmp_data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_MODEL", "writer-model")
        monkeypatch.setenv("REVIEW_MODEL", "judge-model")
        date = _dt.date(2026, 5, 29)
        target = paths.staging_dir(date)
        target.mkdir(parents=True)
        (target / "issue.json").write_text(
            json.dumps(_make_issue_payload(date_str=date.isoformat()))
        )
        with patch("src.review._call_review_llm", return_value=_FAKE_LLM_RESPONSE):
            artifact = run_review(date=date)
        assert artifact.report is not None
        assert artifact.report.llm_model == "judge-model"


# ---------------------------------------------------------------------------
# Prompt assembly.
# ---------------------------------------------------------------------------

class TestBuildReviewPrompt:
    """Pins what the prompt actually contains -- the LLM needs the staged
    issue's headlines, the Pulse pick, each story's id (so findings can
    target it), the fact-check block, and any prior-issue context. Without
    these the editor can't do its job; with too much, we burn tokens on
    noise. The shape is load-bearing."""

    def test_includes_staged_headline(self) -> None:
        issue = _make_issue_payload(
            pulse_headline="Coding agents close 80% of commits",
        )
        prompt = _build_review_prompt(issue, [])
        assert "Coding agents close 80% of commits" in prompt

    def test_includes_pulse_summary(self) -> None:
        issue = _make_issue_payload(
            pulse_summary="A defining shift in coding-agent autonomy.",
        )
        prompt = _build_review_prompt(issue, [])
        assert "A defining shift in coding-agent autonomy." in prompt

    def test_includes_section_intro_lead(self) -> None:
        issue = _make_issue_payload(
            big_picture_intro="Today the regulators moved first.",
        )
        prompt = _build_review_prompt(issue, [])
        assert "Today the regulators moved first." in prompt

    def test_includes_story_ids(self) -> None:
        """A finding targets a story by id. Without the id in the prompt the
        model cannot produce a resolvable target, and every finding would be
        dropped by the filter."""
        issue = _make_issue_payload()
        prompt = _build_review_prompt(issue, [])
        assert f"story_id: {_story_id(1)}" in prompt

    def test_includes_flagged_verification_claims(self) -> None:
        """The editor judges the prose knowing what the fact-checker found.
        A contradicted claim the reviewer cannot see is a contradicted claim
        that ships."""
        issue = _make_issue_payload(verification={
            "schema_version": 1,
            "story_id": _story_id(1),
            "prompt_version": "v0.4",
            "claims": [{
                "schema_version": 1,
                "claim": "The model runs on a single H100",
                "verdict": "contradicted",
                "location": "body",
                "summary_span": "runs on a single H100",
                "source_span": "requires an eight-GPU node",
                "note": "source says otherwise",
            }],
            "has_contradiction": True,
            "has_unsupported": False,
            "headline_flagged": False,
        })
        prompt = _build_review_prompt(issue, [])
        assert "contradicted" in prompt
        assert "requires an eight-GPU node" in prompt

    def test_unverified_story_says_nothing_about_verification(self) -> None:
        """Absent verification is NOT a clean bill of health. Rendering one
        would tell the editor a check happened that did not."""
        issue = _make_issue_payload()
        prompt = _build_review_prompt(issue, [])
        assert "verification:" not in prompt

    def test_no_prior_issues_signals_skip(self) -> None:
        issue = _make_issue_payload()
        prompt = _build_review_prompt(issue, [])
        assert "Skip the drift-watch comparison" in prompt

    def test_includes_up_to_three_prior_issues(self) -> None:
        issue = _make_issue_payload(date_str="2026-05-29")
        priors = [
            _make_issue_payload(date_str="2026-05-26",
                                pulse_headline="Prior pulse A"),
            _make_issue_payload(date_str="2026-05-27",
                                pulse_headline="Prior pulse B"),
            _make_issue_payload(date_str="2026-05-28",
                                pulse_headline="Prior pulse C"),
        ]
        prompt = _build_review_prompt(issue, priors)
        assert "Prior pulse A" in prompt
        assert "Prior pulse B" in prompt
        assert "Prior pulse C" in prompt

    def test_handles_fewer_than_three_prior_issues(self) -> None:
        issue = _make_issue_payload()
        priors = [_make_issue_payload(date_str="2026-05-28",
                                      pulse_headline="Only prior")]
        prompt = _build_review_prompt(issue, priors)
        assert "Only prior" in prompt
        assert "Skip the drift-watch comparison" not in prompt

    def test_asks_for_findings_before_summary(self) -> None:
        """Key order is deliberate: the model commits to evidence before it
        characterises the day, not the other way round."""
        prompt = _build_review_prompt(_make_issue_payload(), [])
        assert prompt.index('"findings"') < prompt.index('"summary"')

    def test_names_the_reputational_liability_criterion(self) -> None:
        prompt = _build_review_prompt(_make_issue_payload(), [])
        assert "reputational_liability" in prompt
        assert "INVESTMENT ADVICE" in prompt


# ===========================================================================
# Prompt caching -- the (static_prefix, variable_part) split (v1.2.1).
#
# The review prompt is now built as (shared_prefix, variable_part) and sent
# through rank._llm_call to the Anthropic API as two content blocks, with
# `cache_control: {"type": "ephemeral"}` on the prefix. The tests below pin
# the same four invariants rank v0.6.1 and summarise pinned:
#
#   1. BYTE EQUALITY -- prefix + variable equals the pre-split v1.2
#      single-string prompt exactly, checked against a verbatim golden copy
#      of the v1.2 assembly over real released-archive issues (three days,
#      with their real prior-issue drift-watch context) plus the
#      empty-archive branch. Caching must never change what the model reads.
#   2. PREFIX STABILITY -- the prefix is exactly `_REVIEW_INSTRUCTIONS +
#      "\n\n"` for every issue and every day (it is a module literal with no
#      interpolation -- no date, no lookback content) and stays comfortably
#      above the 1,024-token cache minimum.
#   3. RETRY DISCIPLINE -- the JSON-parse retry appends its corrective text
#      AFTER the variable part. The pre-v1.2.1 code PREPENDED it to the
#      whole prompt, changing byte 0 -> guaranteed cache miss plus a wasted
#      cache write on every retry.
#   4. TRANSPORT PASS-THROUGH -- _call_review_llm hands the tuple to
#      rank._llm_call intact (the Anthropic two-block shape itself is
#      pinned in tests/test_rank.py::TestPromptCacheSplit).
# ===========================================================================

_REVIEW_ARCHIVE_DAYS = ("2026-08-04", "2026-08-05", "2026-08-08")


def _golden_v12_single_string_prompt(
    issue: dict[str, Any], recent_issues: list[dict[str, Any]]
) -> str:
    """VERBATIM copy of the v1.2 (pre-cache-split) ``_build_review_prompt``
    body -- the golden reference for the byte-equality gate. If a deliberate
    prompt change lands in review.py, this copy must be updated in the same
    PR alongside a REVIEW_PROMPT_VERSION bump; an accidental byte drift
    shows up here as a failure."""
    today_block = review_mod._format_issue_for_prompt(
        issue, label="STAGED ISSUE UNDER REVIEW",
    )
    if recent_issues:
        recent_blocks = "\n\n".join(
            review_mod._format_issue_for_prompt(
                ri, label=f"PRIOR RELEASED ISSUE ({ri.get('date', '?')})",
                compact=True,
            )
            for ri in recent_issues
        )
        recent_section = (
            "\n\nFor drift-watch context, here are the previous "
            f"{len(recent_issues)} released issues (compact form -- headlines "
            "and intros only). Do NOT quote from these; they are context, not "
            "the issue under review:\n\n" + recent_blocks
        )
    else:
        recent_section = (
            "\n\n(No prior released issues available within the lookback "
            "window. Skip the drift-watch comparison.)"
        )
    return f"""\
{review_mod._REVIEW_INSTRUCTIONS}

{today_block}{recent_section}
"""


def _load_review_archive_day(
    day: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load a real released day's issue.json plus its real drift-watch
    context (the prior <= 3 released issues, exactly as run_review would
    gather them). Fails loud if the tracked archive is missing -- the
    byte-equality gate must not silently skip."""
    date = _dt.date.fromisoformat(day)
    path = paths.issue_path(date, canonical=True)
    assert path.exists(), (
        f"released archive day {day} missing at {path} -- the byte-equality "
        "gate needs real archive data (data/released/ is tracked in git)"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    recent = _load_recent_released_issues(
        date, review_mod._REVIEW_LOOKBACK_ISSUES,
    )
    assert recent, f"no prior released issues found before {day}"
    return payload, recent


class TestPromptCacheSplit:
    def test_concatenated_parts_equal_v12_single_string_over_real_archive(
        self,
    ) -> None:
        """prefix + variable must equal the pre-split single-string prompt
        BYTE FOR BYTE, for three real released days reviewed against their
        real prior-issue context. This is the core caching-safety
        invariant: the split may change message structure only, never the
        bytes the model reads."""
        checked = 0
        for day in _REVIEW_ARCHIVE_DAYS:
            payload, recent = _load_review_archive_day(day)
            prefix, variable = _build_review_prompt_parts(payload, recent)
            golden = _golden_v12_single_string_prompt(payload, recent)
            assert prefix + variable == golden, f"byte drift for {day}"
            # The kept joined form must stay in lock-step with the parts.
            assert _build_review_prompt(payload, recent) == golden
            checked += 1
        assert checked == len(_REVIEW_ARCHIVE_DAYS)

    def test_empty_archive_branch_is_byte_identical_too(self) -> None:
        """The no-prior-issues branch ("Skip the drift-watch comparison")
        is day-varying content and must land in the VARIABLE part, joined
        byte-identically."""
        issue = _make_issue_payload()
        prefix, variable = _build_review_prompt_parts(issue, [])
        golden = _golden_v12_single_string_prompt(issue, [])
        assert prefix + variable == golden
        assert "Skip the drift-watch comparison" in variable
        assert "Skip the drift-watch comparison" not in prefix

    def test_prefix_is_the_instructions_block_and_nothing_else(self) -> None:
        """One module -> exactly one prefix: `_REVIEW_INSTRUCTIONS + "\\n\\n"`,
        byte-identical across issues and days (the instructions are a module
        literal with no interpolation -- no run date, no lookback content).
        Any day-varying byte leaking in would produce a distinct prefix per
        day and zero cache hits."""
        prefixes: set[str] = set()
        for day in _REVIEW_ARCHIVE_DAYS:
            payload, recent = _load_review_archive_day(day)
            prefix, _ = _build_review_prompt_parts(payload, recent)
            prefixes.add(prefix)
        prefix_no_recent, _ = _build_review_prompt_parts(
            _make_issue_payload(), [],
        )
        prefixes.add(prefix_no_recent)
        assert len(prefixes) == 1, (
            f"expected one shared prefix, got {len(prefixes)} distinct"
        )
        the_prefix = next(iter(prefixes))
        assert the_prefix == review_mod._REVIEW_INSTRUCTIONS + "\n\n"
        # Measured 2026-08-09 with real paired calls (claude-sonnet-4-6):
        # the prefix is 12,459 bytes = 3,254 billed tokens (call 1
        # cache_creation_input_tokens=3254, call 2
        # cache_read_input_tokens=3254) -- comfortably above the
        # 1,024-token cache minimum. Guard a byte floor well above the
        # minimum so an instructions trim can't silently drop the prefix
        # below cacheability.
        assert len(the_prefix.encode("utf-8")) > 9000

    def test_variable_part_starts_with_the_staged_issue_block(self) -> None:
        """The variable part must start with the byte right after the
        boundary (the staged-issue label), not repeat any prefix content."""
        payload, recent = _load_review_archive_day(_REVIEW_ARCHIVE_DAYS[0])
        _, variable = _build_review_prompt_parts(payload, recent)
        assert variable.startswith("=== STAGED ISSUE UNDER REVIEW ===")
        assert review_mod._REVIEW_INSTRUCTIONS not in variable

    def test_json_retry_prefix_block_byte_identical_to_first_attempt(
        self, tmp_data_root: Path,
    ) -> None:
        """THE TRAP (pre-v1.2.1): the JSON-parse retry PREPENDED corrective
        text to the whole prompt, changing byte 0 -> guaranteed cache miss
        plus a wasted cache write. Pin the fix: the retry's prefix block is
        byte-identical to the first attempt's, and the corrective text is
        appended after the variable part."""
        date = _dt.date(2026, 5, 29)
        target = paths.staging_dir(date)
        target.mkdir(parents=True)
        (target / "issue.json").write_text(
            json.dumps(_make_issue_payload()), encoding="utf-8",
        )
        responses = ["not json at all", "still not json"]
        with patch(
            "src.review._call_review_llm", side_effect=responses,
        ) as mock_llm:
            artifact = run_review(date=date)
        assert artifact.verdict == "unparseable"
        assert mock_llm.call_count == 2
        first = mock_llm.call_args_list[0].args[0]
        second = mock_llm.call_args_list[1].args[0]
        assert isinstance(first, tuple) and isinstance(second, tuple)
        assert second[0] == first[0], (
            "retry prefix must be byte-identical or the cache never hits"
        )
        assert second[1].startswith(first[1]), (
            "corrective text must be APPENDED after the variable part"
        )
        assert "was not valid JSON" in second[1]
        assert "was not valid JSON" not in first[1]

    def test_call_review_llm_passes_the_tuple_through_intact(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_call_review_llm must hand the (prefix, variable) tuple to
        rank._llm_call unchanged -- joining it here would silently disable
        caching while every byte still looked right downstream."""
        monkeypatch.setenv("LLM_MODEL", "claude-test")
        monkeypatch.delenv("REVIEW_MODEL", raising=False)
        with patch("src.rank._llm_call", return_value="{}") as mock_llm:
            review_mod._call_review_llm(("PREFIX", "VARIABLE"), timeout=5.0)
        assert mock_llm.call_args.args[0] == ("PREFIX", "VARIABLE")


# ---------------------------------------------------------------------------
# Prior-issue lookup.
# ---------------------------------------------------------------------------

class TestLoadRecentReleasedIssues:
    """The drift watch depends on reading the right N issues -- not too
    many, not the current day, tolerating gaps. Pinned because a silent
    bug here means the LLM thinks every day is fresh history."""

    def test_returns_n_most_recent(
        self, tmp_data_root: Path,
    ) -> None:
        for date_str in ("2026-05-25", "2026-05-26", "2026-05-27", "2026-05-28"):
            d = _dt.date.fromisoformat(date_str)
            target = paths.released_dir(d)
            target.mkdir(parents=True)
            (target / "issue.json").write_text(
                json.dumps(_make_issue_payload(date_str=date_str))
            )
        out = _load_recent_released_issues(
            _dt.date(2026, 5, 29), n=3,
        )
        assert len(out) == 3
        # Returned oldest-first.
        assert [p["date"] for p in out] == ["2026-05-26", "2026-05-27", "2026-05-28"]

    def test_excludes_today(self, tmp_data_root: Path) -> None:
        for date_str in ("2026-05-28", "2026-05-29"):
            d = _dt.date.fromisoformat(date_str)
            target = paths.released_dir(d)
            target.mkdir(parents=True)
            (target / "issue.json").write_text(
                json.dumps(_make_issue_payload(date_str=date_str))
            )
        out = _load_recent_released_issues(_dt.date(2026, 5, 29), n=3)
        dates = [p["date"] for p in out]
        assert "2026-05-29" not in dates
        assert "2026-05-28" in dates

    def test_returns_fewer_when_archive_is_thin(
        self, tmp_data_root: Path,
    ) -> None:
        d = _dt.date(2026, 5, 28)
        target = paths.released_dir(d)
        target.mkdir(parents=True)
        (target / "issue.json").write_text(
            json.dumps(_make_issue_payload(date_str="2026-05-28"))
        )
        out = _load_recent_released_issues(_dt.date(2026, 5, 29), n=3)
        assert len(out) == 1


# ---------------------------------------------------------------------------
# review.md rendering -- code writes it, from the report.
# ---------------------------------------------------------------------------

class TestRenderReviewMarkdown:
    """The rendered document is what Arman reads at 06:00. It must carry the
    evidence next to the complaint and collect the actionable items in one
    place."""

    def _report(self, findings: list[dict[str, Any]]) -> Any:
        from src.review import _resolve_and_filter_findings
        from src.models import ReviewReport

        issue = _make_issue_payload()
        kept, dropped, _ = _resolve_and_filter_findings(findings, issue)
        return ReviewReport(
            generated_at=_dt.datetime(2026, 5, 29, tzinfo=_dt.timezone.utc),
            computed_verdict="amber",
            one_line="one major, one note",
            findings=kept,
            dropped_findings=dropped,
            prompt_version=REVIEW_PROMPT_VERSION,
            thresholds_version="v1.0-test",
        ), issue

    def test_quote_appears_next_to_the_finding(self) -> None:
        report, issue = self._report([
            _finding(quote="Summary for BP story one.", severity="major"),
        ])
        md = render_review_markdown(report, _dt.date(2026, 5, 29), issue)
        assert "Summary for BP story one." in md

    def test_findings_are_grouped_under_their_section(self) -> None:
        report, issue = self._report([
            _finding(quote="Summary for BP story one.", severity="major"),
            _finding(quote="Summary for HO story one.", story_id=_story_id(10),
                     section="hands_on", severity="minor"),
        ])
        md = render_review_markdown(report, _dt.date(2026, 5, 29), issue)
        assert "## The Big Picture" in md
        assert "## Hands-On" in md

    def test_recommendations_exclude_notes(self) -> None:
        """A note is an observation. Listing it under "before release" would
        train the reader to skim the list that exists to be acted on."""
        report, issue = self._report([
            _finding(quote="Summary for BP story one.", severity="note",
                     instruction="Worth watching the pattern tomorrow."),
        ])
        md = render_review_markdown(report, _dt.date(2026, 5, 29), issue)
        recommendations = md.split("## Recommendations before release")[1]
        assert "Ratify as-is." in recommendations
        assert "Worth watching the pattern tomorrow." not in recommendations

    def test_recommendations_list_actionable_findings(self) -> None:
        report, issue = self._report([
            _finding(quote="Summary for BP story one.", severity="major",
                     instruction="Close on a strategic question."),
        ])
        md = render_review_markdown(report, _dt.date(2026, 5, 29), issue)
        recommendations = md.split("## Recommendations before release")[1]
        assert "Close on a strategic question." in recommendations

    def test_dropped_findings_are_surfaced_separately(self) -> None:
        """The reviewer's misfire rate is a number worth watching, and it
        was invisible before v1.0."""
        report, issue = self._report([
            _finding(quote="text that is not in the issue"),
        ])
        md = render_review_markdown(report, _dt.date(2026, 5, 29), issue)
        assert "Dropped findings" in md
        assert "text that is not in the issue" in md

    def test_names_the_story_not_the_cluster_id(self) -> None:
        report, issue = self._report([
            _finding(quote="Summary for BP story one.", severity="major"),
        ])
        md = render_review_markdown(report, _dt.date(2026, 5, 29), issue)
        assert "BP story one" in md


# ---------------------------------------------------------------------------
# Frontmatter parsing.
# ---------------------------------------------------------------------------

class TestExtractFrontmatterSummary:
    """The terminal one-line and the downstream parsers (``src/gate.py``)
    both depend on pulling ``verdict`` + ``one_line`` cleanly out of
    review.md.

    The parser is strict on purpose: anything it cannot read with
    certainty returns ``unparseable``, never a verdict that looks like a
    judgement. A consumer must be able to tell "the editor said amber"
    apart from "we could not read what the editor said"."""

    def test_parses_valid_frontmatter(self) -> None:
        verdict, one_line = _extract_frontmatter_summary(
            _REVIEW_MD_WITH_FRONTMATTER
        )
        assert verdict == "green"
        assert "Strong day" in one_line

    def test_unknown_verdict_is_unparseable(self) -> None:
        raw = "---\nverdict: maybe\none_line: ok\n---\n\nbody"
        verdict, one_line = _extract_frontmatter_summary(raw)
        assert verdict == "unparseable"
        assert "maybe" in one_line

    def test_code_reserved_verdict_in_the_block_is_unparseable(self) -> None:
        # Only the three editorial tokens are readable as a judgement.
        for token in ("unavailable", "not_run", "unparseable"):
            raw = f"---\nverdict: {token}\none_line: ok\n---\n\nbody"
            verdict, _ = _extract_frontmatter_summary(raw)
            assert verdict == "unparseable", token

    def test_missing_frontmatter_is_unparseable(self) -> None:
        verdict, one_line = _extract_frontmatter_summary("just some prose")
        assert verdict == "unparseable"
        assert "missing" in one_line.lower()

    def test_unclosed_frontmatter_is_unparseable(self) -> None:
        raw = "---\nverdict: green\none_line: ok\n\n# Editor's Review\n"
        verdict, one_line = _extract_frontmatter_summary(raw)
        assert verdict == "unparseable"
        assert "unclosed" in one_line.lower()

    def test_missing_verdict_key_is_unparseable(self) -> None:
        raw = "---\none_line: ok\nissue_date: 2026-05-29\n---\n\nbody"
        verdict, _ = _extract_frontmatter_summary(raw)
        assert verdict == "unparseable"

    def test_duplicate_verdict_keys_are_unparseable(self) -> None:
        # The hazard this pins: last-wins parsing turned red-then-green into
        # green. Two verdict lines mean the document states no verdict.
        raw = "---\nverdict: red\nverdict: green\none_line: ok\n---\n\nbody"
        verdict, _ = _extract_frontmatter_summary(raw)
        assert verdict == "unparseable"

    def test_malformed_line_in_block_is_unparseable(self) -> None:
        raw = "---\nverdict: green\nthis line has no key\n---\n\nbody"
        verdict, _ = _extract_frontmatter_summary(raw)
        assert verdict == "unparseable"

    def test_loose_delimiters_do_not_count_as_frontmatter(self) -> None:
        raw = "---yaml\nverdict: green\n---\n\nbody"
        verdict, _ = _extract_frontmatter_summary(raw)
        assert verdict == "unparseable"

    def test_strips_code_fence_wrapper(self) -> None:
        raw = "```markdown\n" + _REVIEW_MD_WITH_FRONTMATTER + "\n```"
        verdict, _ = _extract_frontmatter_summary(raw)
        assert verdict == "green"


# ---------------------------------------------------------------------------
# Artifact write.
# ---------------------------------------------------------------------------

class TestWriteReviewArtifact:
    """The on-disk format is what Arman reads and what ``src/gate.py``
    parses. If the path is wrong or the frontmatter is missing keys, the
    rest of the contract collapses."""

    def test_writes_to_correct_path(self, tmp_data_root: Path) -> None:
        date = _dt.date(2026, 5, 29)
        out = _write_review_artifact(
            date, _REVIEW_MD_WITH_FRONTMATTER,
            llm_metadata={"llm_model": "claude-opus-4-7"},
            verdict="green",
            one_line="strong",
            issue_date="2026-05-29",
            issue_shape="green",
            issue_sha256="a" * 64,
        )
        expected = paths.staging_dir(date) / "review.md"
        assert out == expected
        assert expected.exists()

    def test_frontmatter_includes_provenance_keys(
        self, tmp_data_root: Path,
    ) -> None:
        date = _dt.date(2026, 5, 29)
        path = _write_review_artifact(
            date, _REVIEW_MD_WITH_FRONTMATTER,
            llm_metadata={"llm_model": "claude-opus-4-7"},
            verdict="green",
            one_line="strong",
            issue_date="2026-05-29",
            issue_shape="green",
            issue_sha256="a" * 64,
        )
        content = path.read_text(encoding="utf-8")
        assert f"prompt_version: {REVIEW_PROMPT_VERSION}" in content
        assert "llm_model: claude-opus-4-7" in content
        assert "generated_at:" in content
        assert "issue_date: 2026-05-29" in content
        assert f"issue_sha256: {'a' * 64}" in content

    def test_code_authored_verdict_replaces_any_in_the_body(
        self, tmp_data_root: Path,
    ) -> None:
        """The file must state the verdict the caller decided on, not one
        found in the body -- otherwise a strict-parse rejection would be
        recorded on disk as a clean green."""
        date = _dt.date(2026, 5, 29)
        path = _write_review_artifact(
            date, _REVIEW_MD_WITH_FRONTMATTER,  # its frontmatter says green
            llm_metadata={"llm_model": "claude-opus-4-7"},
            verdict="unparseable",
            one_line="<verdict token not recognised>",
            issue_date="2026-05-29",
        )
        content = path.read_text(encoding="utf-8")
        frontmatter = content.split("---", 2)[1]
        fm_lines = [ln.strip() for ln in frontmatter.splitlines()]
        assert "verdict: unparseable" in fm_lines
        assert "verdict: green" not in fm_lines
        # The disagreement survives for the audit trail.
        assert "llm_reported_verdict: green" in frontmatter

    def test_frontmatter_has_exactly_one_verdict_key(
        self, tmp_data_root: Path,
    ) -> None:
        date = _dt.date(2026, 5, 29)
        path = _write_review_artifact(
            date, _REVIEW_MD_WITH_FRONTMATTER,
            llm_metadata={"llm_model": "claude-opus-4-7"},
            verdict="amber",
            one_line="notes",
            issue_date="2026-05-29",
        )
        frontmatter = path.read_text(encoding="utf-8").split("---", 2)[1]
        verdict_lines = [
            ln for ln in frontmatter.splitlines()
            if ln.strip().startswith("verdict:")
        ]
        assert verdict_lines == ["verdict: amber"]

    def test_written_file_reparses_to_the_same_verdict(
        self, tmp_data_root: Path,
    ) -> None:
        """Round-trip: whatever we write must read back through the strict
        parser as the same verdict, for every editorial token."""
        date = _dt.date(2026, 5, 29)
        for token in ("green", "amber", "red"):
            path = _write_review_artifact(
                date, _REVIEW_MD_WITH_FRONTMATTER,
                llm_metadata={"llm_model": "claude-opus-4-7"},
                verdict=token,
                one_line="round trip",
                issue_date="2026-05-29",
            )
            reparsed, _ = _extract_frontmatter_summary(
                path.read_text(encoding="utf-8")
            )
            assert reparsed == token

    def test_body_without_frontmatter_gets_one_synthesised(
        self, tmp_data_root: Path,
    ) -> None:
        date = _dt.date(2026, 5, 29)
        path = _write_review_artifact(
            date, "# Editor's Review\n\nNo frontmatter at all.\n",
            llm_metadata={"llm_model": "claude-opus-4-7"},
            verdict="unparseable",
            one_line="<frontmatter missing>",
            issue_date="2026-05-29",
        )
        content = path.read_text(encoding="utf-8")
        assert content.startswith("---\nverdict: unparseable\n")
        assert "No frontmatter at all." in content

    def test_frontmatter_stays_flat_scalars(
        self, tmp_data_root: Path,
    ) -> None:
        """``src/gate.py`` parses this block with a hand-rolled ``key:
        value`` splitter that has no notion of nesting. A nested mapping
        would read back as stray top-level keys."""
        date = _dt.date(2026, 5, 29)
        path = _write_review_artifact(
            date, _REVIEW_MD_WITH_FRONTMATTER,
            llm_metadata={"llm_model": "m"},
            verdict="green", one_line="ok", issue_date="2026-05-29",
            extra_frontmatter={"findings_by_severity": "blocking=0 major=1"},
        )
        frontmatter = path.read_text(encoding="utf-8").split("---", 2)[1]
        for line in frontmatter.strip().splitlines():
            assert not line.startswith(" "), f"nested frontmatter line: {line!r}"


# ---------------------------------------------------------------------------
# run_review integration -- LLM mocked at the boundary.
# ---------------------------------------------------------------------------

class TestRunReview:
    """End-to-end review with the LLM transport stubbed. The mocked call
    is the only boundary we patch; everything else is the real unit."""

    def _stage_issue(self, date: _dt.date) -> None:
        target = paths.staging_dir(date)
        target.mkdir(parents=True)
        (target / "issue.json").write_text(
            json.dumps(_make_issue_payload(date_str=date.isoformat()))
        )

    def test_happy_path_writes_both_artifacts(
        self, tmp_data_root: Path,
    ) -> None:
        date = _dt.date(2026, 5, 29)
        self._stage_issue(date)
        with patch("src.review._call_review_llm", return_value=_FAKE_LLM_RESPONSE):
            artifact = run_review(date=date)
        assert artifact.verdict == "green"
        assert artifact.path == paths.staging_dir(date) / "review.md"
        assert artifact.path.exists()
        assert (paths.staging_dir(date) / "review.json").exists()

    def test_review_json_reparses_as_a_report(
        self, tmp_data_root: Path,
    ) -> None:
        """``src/revise.py`` reads this file. If it does not validate, the
        revision engine refuses to act on a review that was actually fine."""
        date = _dt.date(2026, 5, 29)
        self._stage_issue(date)
        with patch(
            "src.review._call_review_llm",
            return_value=_fake_response([
                _finding(quote="Summary for BP story one.")
            ]),
        ):
            run_review(date=date)
        report = review_mod.read_report(date)
        assert report is not None
        assert len(report.findings) == 1

    def test_verdict_agrees_across_artifact_json_and_markdown(
        self, tmp_data_root: Path,
    ) -> None:
        """Three surfaces, one value. A consumer reading any of them must
        see the same thing."""
        date = _dt.date(2026, 5, 29)
        self._stage_issue(date)
        with patch(
            "src.review._call_review_llm",
            return_value=_fake_response([
                _finding(quote="Summary for BP story one.", severity="blocking",
                         criterion="reputational_liability"),
            ]),
        ):
            artifact = run_review(date=date)
        on_disk, _ = _extract_frontmatter_summary(
            artifact.path.read_text(encoding="utf-8")
        )
        report = review_mod.read_report(date)
        assert report is not None
        assert artifact.verdict == "red"
        assert on_disk == "red"
        assert report.computed_verdict == "red"

    def test_missing_staged_issue_writes_unavailable(
        self, tmp_data_root: Path,
    ) -> None:
        date = _dt.date(2026, 5, 29)
        # No staging dir -- the underlying path doesn't exist.
        artifact = run_review(date=date)
        assert artifact.verdict == "unavailable"
        content = artifact.path.read_text(encoding="utf-8")
        assert "verdict: unavailable" in content
        # No issue.json was ever read, so no hash could be computed. The
        # literal "unknown" placeholder is what src/gate.py's freshness check
        # relies on comparing byte-for-byte against a real hash (and never
        # matching) -- see test_issue_sha256_literal_unknown_holds_as_stale
        # in tests/test_gate.py.
        assert "issue_sha256: unknown" in content

    def test_malformed_threshold_table_writes_unavailable(
        self, tmp_data_root: Path,
    ) -> None:
        """Fail closed. A verdict computed under a table we could not read
        is not a verdict -- and must never degrade to green."""
        date = _dt.date(2026, 5, 29)
        self._stage_issue(date)
        with patch(
            "src.review.load_thresholds",
            side_effect=ReviewThresholdError("bad table"),
        ):
            artifact = run_review(date=date)
        assert artifact.verdict == "unavailable"
        assert "bad table" in artifact.path.read_text(encoding="utf-8")

    def test_malformed_threshold_table_spends_no_llm_call(
        self, tmp_data_root: Path,
    ) -> None:
        """No Token Wasted: the answer would be discarded, so don't buy it."""
        date = _dt.date(2026, 5, 29)
        self._stage_issue(date)
        with patch(
            "src.review.load_thresholds",
            side_effect=ReviewThresholdError("bad table"),
        ), patch("src.review._call_review_llm") as call:
            run_review(date=date)
        call.assert_not_called()

    def test_llm_failure_writes_unavailable_without_raising(
        self, tmp_data_root: Path,
    ) -> None:
        date = _dt.date(2026, 5, 29)
        self._stage_issue(date)

        def _boom(*_args: Any, **_kwargs: Any) -> str:
            raise RuntimeError("simulated transport failure")

        with patch("src.review._call_review_llm", side_effect=_boom):
            artifact = run_review(date=date)
        assert artifact.verdict == "unavailable"
        assert "simulated transport failure" in artifact.path.read_text(
            encoding="utf-8"
        )

    def test_dry_run_writes_nothing(self, tmp_data_root: Path) -> None:
        date = _dt.date(2026, 5, 29)
        artifact = run_review(date=date, dry_run=True)
        assert not artifact.path.exists()

    def test_dry_run_verdict_is_not_run(self, tmp_data_root: Path) -> None:
        """A dry run makes no editorial judgement, so it must not return a
        judgement-shaped verdict that a consumer could read as a pass."""
        date = _dt.date(2026, 5, 29)
        artifact = run_review(date=date, dry_run=True)
        assert artifact.verdict == "not_run"
        assert artifact.verdict != "green"

    def test_verdict_vocabulary_is_enforced(self) -> None:
        with pytest.raises(ValueError):
            ReviewArtifact(
                date=_dt.date(2026, 5, 29), verdict="fine",
                one_line="x", path=Path("review.md"),
            )

    def test_frontmatter_carries_hash_of_the_issue_bytes_read(
        self, tmp_data_root: Path,
    ) -> None:
        """The hash must be of the exact bytes the reviewer was shown, so a
        downstream freshness check can tell whether issue.json moved after
        the review ran."""
        import hashlib

        date = _dt.date(2026, 5, 29)
        self._stage_issue(date)
        expected = hashlib.sha256(
            paths.issue_path(date, canonical=False).read_bytes()
        ).hexdigest()
        with patch("src.review._call_review_llm", return_value=_FAKE_LLM_RESPONSE):
            artifact = run_review(date=date)
        assert f"issue_sha256: {expected}" in artifact.path.read_text(
            encoding="utf-8"
        )

    def test_unparseable_llm_output_yields_unparseable_verdict(
        self, tmp_data_root: Path,
    ) -> None:
        date = _dt.date(2026, 5, 29)
        self._stage_issue(date)
        with patch(
            "src.review._call_review_llm",
            return_value="I could not complete the review today.",
        ):
            artifact = run_review(date=date)
        assert artifact.verdict == "unparseable"
        assert "verdict: unparseable" in artifact.path.read_text(encoding="utf-8")

    def test_unparseable_output_is_retried_once(
        self, tmp_data_root: Path,
    ) -> None:
        """One corrective retry, then stop. A model that returns prose twice
        will not return JSON on the third ask."""
        date = _dt.date(2026, 5, 29)
        self._stage_issue(date)
        responses = ["not json at all", _FAKE_LLM_RESPONSE]
        with patch(
            "src.review._call_review_llm",
            side_effect=responses,
        ):
            artifact = run_review(date=date)
        assert artifact.verdict == "green"

    def test_empty_findings_list_is_a_successful_green(
        self, tmp_data_root: Path,
    ) -> None:
        """"Nothing to flag" is a real editorial answer and must not be
        confused with a broken response."""
        date = _dt.date(2026, 5, 29)
        self._stage_issue(date)
        with patch(
            "src.review._call_review_llm",
            return_value='{"findings": [], "summary": "clean day"}',
        ):
            artifact = run_review(date=date)
        assert artifact.verdict == "green"

    def test_failed_unavailable_write_raises(
        self, tmp_data_root: Path,
    ) -> None:
        """A review.md we could not write must not vanish silently: an
        absent file is indistinguishable from a stage nobody ran."""
        date = _dt.date(2026, 5, 29)
        with patch(
            "src.review._atomic_write_text",
            side_effect=OSError("disk full"),
        ):
            with pytest.raises(OSError):
                run_review(date=date)  # no staged issue -> unavailable path

    def test_non_object_issue_json_writes_unavailable(
        self, tmp_data_root: Path,
    ) -> None:
        date = _dt.date(2026, 5, 29)
        target = paths.staging_dir(date)
        target.mkdir(parents=True)
        (target / "issue.json").write_text("[1, 2, 3]")
        artifact = run_review(date=date)
        assert artifact.verdict == "unavailable"


# ---------------------------------------------------------------------------
# Pipeline integration -- src/run.py wiring.
# ---------------------------------------------------------------------------

class TestPipelineIntegration:
    """The integration contract: review auto-fires when render runs, the
    ``--no-review`` escape hatch suppresses it, and ``--stages review``
    runs review standalone. The wiring matters because the spec promises
    these specific shapes."""

    def test_render_subset_appends_review(self) -> None:
        resolved = run_mod._resolve_stages(None, "render")
        assert resolved == ["render", "review"]

    def test_summarise_render_subset_appends_review(self) -> None:
        # verify auto-fires after summarise, so the full resolved list is
        # summarise -> verify -> render -> review.
        resolved = run_mod._resolve_stages(None, "summarise,render")
        assert resolved == ["summarise", "verify", "render", "review"]

    def test_summarise_only_does_not_append_review(self) -> None:
        # summarise auto-fires verify; render is not in the subset so review
        # is NOT appended.
        resolved = run_mod._resolve_stages(None, "summarise")
        assert resolved == ["summarise", "verify"]

    def test_no_review_flag_strips_review_from_full_run(self) -> None:
        resolved = run_mod._resolve_stages(None, None, no_review=True)
        assert "review" not in resolved
        # Other stages still run in order (verify fires after summarise).
        assert resolved == ["fetch", "cluster", "rank", "summarise", "verify", "render"]

    def test_no_review_flag_strips_review_from_render_subset(self) -> None:
        resolved = run_mod._resolve_stages(None, "render", no_review=True)
        assert resolved == ["render"]

    def test_explicit_stages_review_runs_standalone(self) -> None:
        resolved = run_mod._resolve_stages(None, "review")
        assert resolved == ["review"]

    def test_full_default_run_includes_review_at_tail(self) -> None:
        resolved = run_mod._resolve_stages(None, None)
        assert resolved[-1] == "review"


# ---------------------------------------------------------------------------
# verify auto-fire and --no-verify wiring (added when verify stage shipped).
# ---------------------------------------------------------------------------

class TestResolveStagesVerify:
    """Pin the auto-fire-after-summarise contract for verify and the
    --no-verify escape hatch. These are the cases most likely to silently
    regress if the STAGE_ORDER or _resolve_stages logic changes."""

    def test_summarise_alone_auto_fires_verify(self) -> None:
        resolved = run_mod._resolve_stages(None, "summarise")
        assert resolved == ["summarise", "verify"]

    def test_verify_inserted_immediately_after_summarise(self) -> None:
        # With render also in the subset, verify must sit between summarise
        # and render, not at the tail.
        resolved = run_mod._resolve_stages(None, "summarise,render")
        idx_s = resolved.index("summarise")
        idx_v = resolved.index("verify")
        idx_r = resolved.index("render")
        assert idx_s < idx_v < idx_r

    def test_no_verify_strips_verify_from_summarise_subset(self) -> None:
        resolved = run_mod._resolve_stages(None, "summarise", no_verify=True)
        assert resolved == ["summarise"]
        assert "verify" not in resolved

    def test_no_verify_strips_verify_from_full_run(self) -> None:
        resolved = run_mod._resolve_stages(None, None, no_verify=True)
        assert "verify" not in resolved

    def test_render_only_does_not_pull_in_verify(self) -> None:
        # verify is only auto-fired when summarise runs; render alone must not
        # add it.
        resolved = run_mod._resolve_stages(None, "render")
        assert "verify" not in resolved

    def test_explicit_stage_verify_runs_standalone(self) -> None:
        resolved = run_mod._resolve_stages(None, "verify")
        assert resolved == ["verify"]

    def test_explicit_stages_verify_not_duplicated(self) -> None:
        # When the caller names verify explicitly alongside summarise, it must
        # not appear twice.
        resolved = run_mod._resolve_stages(None, "summarise,verify")
        assert resolved.count("verify") == 1


# ---------------------------------------------------------------------------
# _run_stage advisory guard -- verify must never halt the pipeline.
# ---------------------------------------------------------------------------

class TestAdvisoryGuardVerify:
    """Pin the _ADVISORY_STAGES belt-and-suspenders guard at the dispatch
    level: an unexpected exception raised by _run_verify must return
    (True, ...) so the pipeline continues, regardless of what the inner
    module does. This is the second defensive layer on top of verify_day's
    own failure-soft contract."""

    def test_unexpected_raise_in_verify_returns_ok_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_date: _dt.date) -> str:
            raise RuntimeError("completely unexpected crash")

        monkeypatch.setitem(run_mod._STAGE_HANDLERS, "verify", _boom)
        ok, message = run_mod._run_stage("verify", _dt.date(2026, 5, 24))
        assert ok is True, (
            "An unexpected raise in the verify handler must not halt the "
            "pipeline -- _ADVISORY_STAGES guard must catch it and return ok=True"
        )
        assert "RuntimeError" in message or "advisory-guard" in message


# ---------------------------------------------------------------------------
# CLI -- the standalone `aiv review` subcommand.
# ---------------------------------------------------------------------------

class TestAivReviewCli:
    """The standalone command surface: ``aiv review --date YYYY-MM-DD``.
    Pinned because the help text + flag shape are the operator's contract
    and would silently rot otherwise."""

    def test_aiv_review_runs_against_existing_staged_issue(
        self, tmp_data_root: Path,
    ) -> None:
        date = _dt.date(2026, 5, 29)
        target = paths.staging_dir(date)
        target.mkdir(parents=True)
        (target / "issue.json").write_text(
            json.dumps(_make_issue_payload(date_str=date.isoformat()))
        )
        runner = CliRunner()
        with patch("src.review._call_review_llm", return_value=_FAKE_LLM_RESPONSE):
            result = runner.invoke(
                run_mod.app, ["review", "--date", "2026-05-29"],
            )
        assert result.exit_code == 0
        assert "GREEN" in result.stdout
        assert (target / "review.md").exists()

    def test_aiv_review_dry_run_writes_nothing(
        self, tmp_data_root: Path,
    ) -> None:
        date = _dt.date(2026, 5, 29)
        runner = CliRunner()
        result = runner.invoke(
            run_mod.app,
            ["review", "--date", "2026-05-29", "--dry-run"],
        )
        assert result.exit_code == 0
        assert not (paths.staging_dir(date) / "review.md").exists()


# ---------------------------------------------------------------------------
# v1.1 (2026-08-08): the take enters the review surface.
#
# Contract under test: the take is quotable text (indexed per story), the
# reviewer SEES it, the prompt teaches the take_shape criterion, and a
# take-targeting finding passes/fails the verbatim-quote filter exactly
# like headline/summary findings do.
# ---------------------------------------------------------------------------

def _payload_with_takes() -> dict[str, Any]:
    payload = _make_issue_payload()
    payload["pulse"]["stories"][0]["take"] = (
        "Local-first inference is the procurement default now."
    )
    payload["sections"][0]["stories"][0]["take"] = (
        "Model-risk sign-off now covers agent plans, not just outputs."
    )
    return payload


class TestTakeInReviewSurface:
    def test_take_indexed_as_quotable_field(self) -> None:
        from src.review import index_issue_fields
        idx = index_issue_fields(_payload_with_takes())
        assert idx[f"story:{_story_id(0)}:take"] == (
            "Local-first inference is the procurement default now."
        )

    def test_legacy_issue_indexes_no_take_keys(self) -> None:
        from src.review import index_issue_fields
        idx = index_issue_fields(_make_issue_payload())
        assert not any(key.endswith(":take") for key in idx)

    def test_prompt_shows_take_lines_only_when_present(self) -> None:
        from src.review import _format_issue_for_prompt
        with_takes = _format_issue_for_prompt(
            _payload_with_takes(), label="X",
        )
        assert (
            "take: Local-first inference is the procurement default now."
            in with_takes
        )
        legacy = _format_issue_for_prompt(_make_issue_payload(), label="X")
        assert "take:" not in legacy

    def test_prompt_teaches_take_shape_criterion(self) -> None:
        from src.review import _build_review_prompt
        prompt = _build_review_prompt(_payload_with_takes(), [])
        assert "take_shape" in prompt
        assert "It is now the case that" in prompt
        assert "SKIP THIS CRITERION ENTIRELY" in prompt
        assert (
            '"field": "<headline | summary | take | synthesis | intro_lead'
            ' | intro_body | digest_lead | digest_sentence>"' in prompt
        )

    def test_take_shape_in_published_criteria(self) -> None:
        from src.review import REVIEW_CRITERIA
        assert "take_shape" in REVIEW_CRITERIA

    def test_take_finding_with_real_quote_is_kept(self) -> None:
        from src.review import _resolve_and_filter_findings
        finding = {
            "target": {"kind": "story", "story_id": _story_id(0),
                       "section": "pulse", "field": "take"},
            "criterion": "take_shape",
            "severity": "major",
            "quote": "Local-first inference is the procurement default now.",
            "fix_kind": "text_edit",
            "instruction": "Recast without the consultant frame.",
        }
        kept, dropped, malformed = _resolve_and_filter_findings(
            [finding], _payload_with_takes(),
        )
        assert len(kept) == 1 and not dropped and malformed == 0
        assert kept[0].target.field == "take"

    def test_take_finding_with_invented_quote_is_dropped(self) -> None:
        from src.review import _resolve_and_filter_findings
        finding = {
            "target": {"kind": "story", "story_id": _story_id(0),
                       "section": "pulse", "field": "take"},
            "criterion": "take_shape",
            "severity": "major",
            "quote": "A sentence that is not in the take.",
            "fix_kind": "text_edit",
            "instruction": "Recast it.",
        }
        kept, dropped, malformed = _resolve_and_filter_findings(
            [finding], _payload_with_takes(),
        )
        assert not kept and len(dropped) == 1 and malformed == 0

    def test_take_finding_on_legacy_issue_is_dropped_as_unresolvable(
        self,
    ) -> None:
        """No take on the story -> no story:<id>:take key -> the finding
        cannot resolve to text on disk and lands in dropped_findings."""
        from src.review import _resolve_and_filter_findings
        finding = {
            "target": {"kind": "story", "story_id": _story_id(0),
                       "section": "pulse", "field": "take"},
            "criterion": "take_shape",
            "severity": "major",
            "quote": "Anything at all.",
            "fix_kind": "text_edit",
            "instruction": "Write a take.",
        }
        kept, dropped, _malformed = _resolve_and_filter_findings(
            [finding], _make_issue_payload(),
        )
        assert not kept and len(dropped) == 1

    def test_review_prompt_version_is_v1_2_1(self) -> None:
        """The take surfaces (v1.1) plus the digest/synthesis surfaces
        (v1.2) are prompt content; the version must attribute them. v1.2.1
        is the message-structure-only cache split -- first-attempt prompt
        bytes identical to v1.2 (pinned by TestPromptCacheSplit)."""
        from src.review import REVIEW_PROMPT_VERSION
        assert REVIEW_PROMPT_VERSION == "v1.2.1"

    def test_take_shape_on_takeless_issue_dropped_even_with_valid_quote(
        self,
    ) -> None:
        """The deterministic legacy guard (v1.1): a take_shape finding
        against an issue with NO takes is dropped even when its quote
        resolves against another field -- the criterion cannot apply.
        Guards the defect the first wired Eval 9 run measured
        (2026-08-08): the reviewer filed a take_shape major against a
        pre-take fixture, quoting the story's headline, and the quote
        filter alone kept it."""
        from src.review import _resolve_and_filter_findings
        payload = _make_issue_payload()
        finding = {
            "target": {"kind": "story", "story_id": _story_id(0),
                       "section": "pulse", "field": "headline"},
            "criterion": "take_shape",
            "severity": "major",
            "quote": "Today's defining story",  # real headline text
            "fix_kind": "text_edit",
            "instruction": "Write the missing take.",
        }
        kept, dropped, malformed = _resolve_and_filter_findings(
            [finding], payload,
        )
        assert not kept and len(dropped) == 1 and malformed == 0

    def test_take_shape_kept_when_issue_carries_takes(self) -> None:
        """Mutation partner for the legacy guard: on a take-bearing issue
        the same criterion survives (the guard must key on take presence,
        not blanket-drop the criterion)."""
        from src.review import _resolve_and_filter_findings
        finding = {
            "target": {"kind": "story", "story_id": _story_id(0),
                       "section": "pulse", "field": "take"},
            "criterion": "take_shape",
            "severity": "minor",
            "quote": "Local-first inference is the procurement default now.",
            "fix_kind": "text_edit",
            "instruction": "Vary the frame.",
        }
        kept, dropped, malformed = _resolve_and_filter_findings(
            [finding], _payload_with_takes(),
        )
        assert len(kept) == 1 and not dropped and malformed == 0


# ---------------------------------------------------------------------------
# v1.2 (2026-08-09): the digest + synthesis in the review surface (R1).
#
# Contract under test (DESIGN.md "The digest", R1): the reviewer's rendered
# issue includes a DIGEST block with indices and per-section synthesis
# lines; digest_lead / digest_sentence (located by digest_index) and
# synthesis are indexed quotable fields, so findings on them survive the
# verbatim-quote check exactly like every other field.
# ---------------------------------------------------------------------------

_DIGEST_LEAD_TEXT = "Local agents arrive."
_DIGEST_SENTENCE_TEXT = (
    "A consumer-GPU model and a fraud-triage agent both shipped this week."
)
_SYNTHESIS_TEXT = (
    "Production agents stopped being demos today. Two deployments crossed "
    "the line in the same week."
)


def _payload_with_digest_and_synthesis() -> dict[str, Any]:
    payload = _payload_with_takes()
    # Redesign issues carry synthesis INSTEAD of the legacy pair.
    payload["sections"][0]["synthesis"] = _SYNTHESIS_TEXT
    payload["sections"][0]["intro_lead"] = None
    payload["sections"][0]["intro_body"] = None
    payload["digest"] = [
        {"lead": _DIGEST_LEAD_TEXT, "sentence": _DIGEST_SENTENCE_TEXT,
         "story_ids": [_story_id(0)]},
        {"lead": "Second bullet here.", "sentence": "Another sentence.",
         "story_ids": [_story_id(1)]},
    ]
    return payload


def _digest_finding(
    index: int | str = 0,
    field: str = "digest_lead",
    quote: str = _DIGEST_LEAD_TEXT,
    **overrides: Any,
) -> dict[str, Any]:
    return {
        "target": {"kind": "digest", "digest_index": index, "field": field},
        "criterion": "digest_shape",
        "severity": "major",
        "quote": quote,
        "fix_kind": "text_edit",
        "instruction": "Recast the lead as a naming, not an action.",
        **overrides,
    }


class TestDigestAndSynthesisInReviewSurface:
    def test_digest_and_synthesis_indexed_as_quotable_fields(self) -> None:
        from src.review import index_issue_fields
        idx = index_issue_fields(_payload_with_digest_and_synthesis())
        assert idx["digest:0:digest_lead"] == _DIGEST_LEAD_TEXT
        assert idx["digest:0:digest_sentence"] == _DIGEST_SENTENCE_TEXT
        assert idx["digest:1:digest_lead"] == "Second bullet here."
        assert idx["section:big_picture:synthesis"] == _SYNTHESIS_TEXT

    def test_digestless_issue_indexes_no_digest_keys(self) -> None:
        from src.review import index_issue_fields
        idx = index_issue_fields(_make_issue_payload())
        assert not any(key.startswith("digest:") for key in idx)
        assert not any(key.endswith(":synthesis") for key in idx)

    def test_target_key_routes_digest_by_index(self) -> None:
        from src.models import ReviewTarget
        from src.review import target_key
        target = ReviewTarget(
            kind="digest", digest_index=2, field="digest_sentence",
        )
        assert target_key(target) == "digest:2:digest_sentence"

    def test_prompt_renders_digest_block_with_indices_and_synthesis(
        self,
    ) -> None:
        from src.review import _format_issue_for_prompt
        rendered = _format_issue_for_prompt(
            _payload_with_digest_and_synthesis(), label="X",
        )
        assert 'DIGEST ("The 30-second read"' in rendered
        assert "digest_index: 0" in rendered
        assert f"lead: {_DIGEST_LEAD_TEXT}" in rendered
        assert f"synthesis: {_SYNTHESIS_TEXT}" in rendered
        # The tag-derivation recommendation: the stored signal is shown.
        assert "signal: watch" in rendered
        # A digest-less issue renders no DIGEST block.
        legacy = _format_issue_for_prompt(_make_issue_payload(), label="X")
        assert "DIGEST" not in legacy

    def test_prompt_teaches_digest_and_synthesis_criteria(self) -> None:
        from src.review import REVIEW_CRITERIA, _build_review_prompt
        assert "digest_shape" in REVIEW_CRITERIA
        assert "synthesis_shape" in REVIEW_CRITERIA
        prompt = _build_review_prompt(
            _payload_with_digest_and_synthesis(), [],
        )
        assert "digest_shape" in prompt
        assert "synthesis_shape" in prompt
        assert "PIPELINE DEFECT" in prompt          # the n=1 rule
        assert "A lead over 6 words" in prompt      # the lead budget rule

    def test_digest_finding_with_real_quote_is_kept(self) -> None:
        from src.review import _resolve_and_filter_findings
        kept, dropped, malformed = _resolve_and_filter_findings(
            [_digest_finding()], _payload_with_digest_and_synthesis(),
        )
        assert len(kept) == 1 and not dropped and malformed == 0
        target = kept[0].target
        assert target.kind == "digest"
        assert target.digest_index == 0
        assert target.story_id is None and target.section is None

    def test_digest_index_parses_from_a_string(self) -> None:
        """Models sometimes stringify integers; a quoted index must not
        cost the finding."""
        from src.review import _resolve_and_filter_findings
        kept, dropped, malformed = _resolve_and_filter_findings(
            [_digest_finding(index="0")],
            _payload_with_digest_and_synthesis(),
        )
        assert len(kept) == 1 and malformed == 0
        assert kept[0].target.digest_index == 0

    def test_digest_finding_with_wrong_index_is_dropped(self) -> None:
        """The quote is real text, but of bullet 0 -- an index pointing at
        bullet 1 must not resolve against it (index precision is the whole
        point of the locator)."""
        from src.review import _resolve_and_filter_findings
        kept, dropped, _malformed = _resolve_and_filter_findings(
            [_digest_finding(index=1)],  # quote belongs to bullet 0
            _payload_with_digest_and_synthesis(),
        )
        assert not kept and len(dropped) == 1

    def test_digest_finding_on_digestless_issue_is_dropped(self) -> None:
        from src.review import _resolve_and_filter_findings
        kept, dropped, _malformed = _resolve_and_filter_findings(
            [_digest_finding()], _payload_with_takes(),
        )
        assert not kept and len(dropped) == 1

    def test_digest_finding_without_index_is_malformed(self) -> None:
        """ReviewTarget requires digest_index for digest targets; a
        finding without one cannot be resolved to a bullet and is counted
        malformed, not guessed at."""
        from src.review import _resolve_and_filter_findings
        finding = _digest_finding()
        del finding["target"]["digest_index"]
        kept, dropped, malformed = _resolve_and_filter_findings(
            [finding], _payload_with_digest_and_synthesis(),
        )
        assert not kept and not dropped and malformed == 1

    def test_synthesis_finding_with_real_quote_is_kept(self) -> None:
        from src.review import _resolve_and_filter_findings
        finding = {
            "target": {"kind": "section", "section": "big_picture",
                       "field": "synthesis"},
            "criterion": "synthesis_shape",
            "severity": "minor",
            "quote": "Production agents stopped being demos today.",
            "fix_kind": "text_edit",
            "instruction": "Anchor the first sentence in today's stories.",
        }
        kept, dropped, malformed = _resolve_and_filter_findings(
            [finding], _payload_with_digest_and_synthesis(),
        )
        assert len(kept) == 1 and not dropped and malformed == 0
        assert kept[0].target.field == "synthesis"

    def test_review_md_groups_digest_findings_first(self) -> None:
        from src.models import ReviewFinding, ReviewReport, ReviewTarget
        from src.review import render_review_markdown
        import datetime as dt

        finding = ReviewFinding(
            finding_id="f001",
            target=ReviewTarget(
                kind="digest", digest_index=0, field="digest_lead",
            ),
            criterion="digest_shape",
            severity="major",
            quote=_DIGEST_LEAD_TEXT,
            fix_kind="text_edit",
            instruction="Recast as a naming.",
        )
        report = ReviewReport(
            generated_at=dt.datetime(2026, 8, 9, tzinfo=dt.timezone.utc),
            computed_verdict="amber",
            findings=[finding],
            prompt_version="v1.2",
        )
        markdown = render_review_markdown(
            report, dt.date(2026, 8, 9), _payload_with_digest_and_synthesis(),
        )
        assert "## The 30-second read" in markdown
        assert "bullet 1 -> digest_lead" in markdown
