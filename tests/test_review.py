"""Unit tests for src/review.py and the run.py wiring around it.

Scope. The deterministic plumbing in review: prompt assembly, prior-issue
lookup, frontmatter parsing, artifact write, pipeline integration, the
``--no-review`` escape hatch, the unavailable-fallback when the LLM call
fails, and the standalone ``aiv review`` CLI.

We mock the boundary -- ``src.review._call_review_llm`` -- and assert on
the unit's own transformations (file contents, verdict surfacing,
pipeline-stage list shape). We do NOT mock the unit under test. Per
``tests/CONVENTIONS.md``.
"""
from __future__ import annotations

import datetime as _dt
import json
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
    _build_review_prompt,
    _extract_frontmatter_summary,
    _load_recent_released_issues,
    _write_review_artifact,
    run_review,
)


# ---------------------------------------------------------------------------
# Test data helpers.
# ---------------------------------------------------------------------------

def _make_issue_payload(
    date_str: str = "2026-05-29",
    pulse_headline: str = "Today's defining story",
    pulse_summary: str = "A clear pick that carries the day's direction.",
    big_picture_headlines: list[str] | None = None,
    hands_on_headlines: list[str] | None = None,
    currents_headlines: list[str] | None = None,
    big_picture_intro: str = "Systems beat single points.",
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
            "story_id": f"c_{'a' * 12}{ix:02x}",
            "headline": hl,
            "summary": f"Summary for {hl}.",
            "source_urls": [f"https://example.com/{ix}"],
            "prior_coverage_ref": None,
            "signal": "watch",
        }

    return {
        "schema_version": 5,
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


_FAKE_LLM_RESPONSE = """\
---
verdict: green
one_line: Strong day; closing shapes hold across sections
issue_date: 2026-05-29
issue_shape: green
---

# Editor's Review -- 2026-05-29

**Verdict**: GREEN. The shape holds and the Pulse carries the day.

## Shape
Green is right; sections are within caps.

## Pulse
**Pick**: "Today's defining story"
- Editorial fit: lands
- Closing shape: plain take
- Sourcing: thin but adequate
- No concerns.

## Big Picture
**Intro**: "Systems beat single points."
- Distinct register: yes
- Closing shapes: 1 of 1 strategic questions

### Stories
1. "BP story one" -- in voice.

## Hands-On
**Intro**: "Verify before you deploy."
- Distinct register: yes
- Closing shapes: 1 of 1 imperative actions

### Stories
1. "HO story one" -- in voice.

## Currents
**Intro**: "Benchmarks are under pressure."
- Aggregate direction: named yes
- Closing shapes: 1 of 1 calibrated stakes

### Stories
1. "Currents story one" -- in voice.

## Drift watch
- No drift concerns this issue.

## Recommendations before release
- Ratify as-is.

## Ratification call
**Editor recommends**: RATIFY
**Arman's call**: ___
"""


# ---------------------------------------------------------------------------
# Prompt assembly.
# ---------------------------------------------------------------------------

class TestBuildReviewPrompt:
    """Pins what the prompt actually contains -- the LLM needs the staged
    issue's headlines, the Pulse pick, and any prior-issue context.
    Without these the editor can't do its job; with too much, we burn
    tokens on noise. The shape is load-bearing."""

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
# Frontmatter parsing.
# ---------------------------------------------------------------------------

class TestExtractFrontmatterSummary:
    """The terminal one-line and the downstream parsers both depend on
    pulling ``verdict`` + ``one_line`` cleanly from the LLM's response.
    If this is wrong, the pipeline prints garbage even when the LLM
    nailed the review."""

    def test_parses_valid_frontmatter(self) -> None:
        verdict, one_line = _extract_frontmatter_summary(_FAKE_LLM_RESPONSE)
        assert verdict == "green"
        assert "Strong day" in one_line

    def test_unknown_verdict_falls_back_to_amber(self) -> None:
        raw = "---\nverdict: maybe\none_line: ok\n---\n\nbody"
        verdict, _ = _extract_frontmatter_summary(raw)
        assert verdict == "amber"

    def test_missing_frontmatter_falls_back_to_amber(self) -> None:
        verdict, one_line = _extract_frontmatter_summary("just some prose")
        assert verdict == "amber"
        assert "missing" in one_line.lower() or "unclosed" in one_line.lower()

    def test_strips_code_fence_wrapper(self) -> None:
        raw = "```markdown\n" + _FAKE_LLM_RESPONSE + "\n```"
        verdict, _ = _extract_frontmatter_summary(raw)
        assert verdict == "green"


# ---------------------------------------------------------------------------
# Artifact write.
# ---------------------------------------------------------------------------

class TestWriteReviewArtifact:
    """The on-disk format is what Arman reads and what downstream tooling
    parses. If the path is wrong or the frontmatter is missing keys, the
    rest of the contract collapses."""

    def test_writes_to_correct_path(self, tmp_data_root: Path) -> None:
        date = _dt.date(2026, 5, 29)
        out = _write_review_artifact(
            date, _FAKE_LLM_RESPONSE,
            llm_metadata={
                "verdict": "green",
                "one_line": "strong",
                "issue_date": "2026-05-29",
                "issue_shape": "green",
                "llm_model": "claude-opus-4-7",
            },
        )
        expected = paths.staging_dir(date) / "review.md"
        assert out == expected
        assert expected.exists()

    def test_frontmatter_includes_provenance_keys(
        self, tmp_data_root: Path,
    ) -> None:
        date = _dt.date(2026, 5, 29)
        path = _write_review_artifact(
            date, _FAKE_LLM_RESPONSE,
            llm_metadata={
                "verdict": "green",
                "one_line": "strong",
                "issue_date": "2026-05-29",
                "issue_shape": "green",
                "llm_model": "claude-opus-4-7",
            },
        )
        content = path.read_text(encoding="utf-8")
        assert f"prompt_version: {REVIEW_PROMPT_VERSION}" in content
        assert "llm_model: claude-opus-4-7" in content
        assert "generated_at:" in content
        assert "issue_date: 2026-05-29" in content


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

    def test_happy_path_writes_review_md_and_returns_verdict(
        self, tmp_data_root: Path,
    ) -> None:
        date = _dt.date(2026, 5, 29)
        self._stage_issue(date)
        with patch("src.review._call_review_llm", return_value=_FAKE_LLM_RESPONSE):
            artifact = run_review(date=date)
        assert artifact.verdict == "green"
        assert artifact.path == paths.staging_dir(date) / "review.md"
        assert artifact.path.exists()

    def test_missing_staged_issue_writes_unavailable(
        self, tmp_data_root: Path,
    ) -> None:
        date = _dt.date(2026, 5, 29)
        # No staging dir -- the underlying path doesn't exist.
        artifact = run_review(date=date)
        assert artifact.verdict == "unavailable"
        content = artifact.path.read_text(encoding="utf-8")
        assert "verdict: unavailable" in content

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
        assert "simulated transport failure" in artifact.path.read_text(encoding="utf-8")

    def test_dry_run_writes_nothing(self, tmp_data_root: Path) -> None:
        date = _dt.date(2026, 5, 29)
        artifact = run_review(date=date, dry_run=True)
        assert not artifact.path.exists()


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
        import datetime as _dt

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
