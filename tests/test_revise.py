"""Unit tests for src/revise.py -- the revision engine.

Scope. The parts that must not be wrong, in order of what they cost when
they are:

  * **The validation gate.** Everything downstream of it trusts that a
    replacement did not invent a number, drop a link, or quietly become a
    different sentence. Each rule gets a test that fails if the rule is
    deleted.
  * **Containment.** One LLM call per field, and that call sees ONLY that
    field. A containment breach means one finding can rewrite the issue.
  * **Freshness.** The engine must refuse to edit against a review of a
    superseded draft -- it would be editing sentences the reviewer never
    read.
  * **Shadow vs live.** Shadow computes and records; it must not touch
    ``issue.json``. Live applies and clears the now-invalid fact-check.

We mock exactly one boundary -- ``src.revise._call_revise_llm`` -- and
assert on the unit's own work. Per ``tests/CONVENTIONS.md``.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src import gate as gate_mod
from src import paths
from src import review as review_mod
from src import revise as revise_mod
from src.models import ReviewFinding, ReviewReport, ReviewTarget
from src.revise import (
    REVISE_PROMPT_VERSION,
    _FieldGroup,
    revise_day,
    revisions_path,
    validate_replacement,
)


DATE = _dt.date(2026, 8, 2)
STORY_A = "c_" + "a" * 12
STORY_B = "c_" + "b" * 12

_BODY_A = (
    "The release cuts inference latency by 30 percent across the fleet, and "
    "the team says the gain holds under sustained load."
)
_BODY_B = (
    "A second lab replicated the throughput result on commodity hardware "
    "without vendor tooling."
)


# ---------------------------------------------------------------------------
# Fixtures / helpers.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def render_stub():
    """Stub ``src.render.render`` for every test in this module.

    A live cycle now re-renders the HTML for the copy it edited, and
    ``tmp_data_root`` redirects ``data/`` but NOT ``docs/`` -- so an
    unstubbed live test would rewrite the repo's committed pages from a tmp
    fixture's contents. Stubbing the render boundary (rather than
    ``_rerender_html``) keeps this module's own re-render wiring under test.
    """
    with patch("src.render.render", return_value=Path("docs/rendered.html")) as stub:
        yield stub


def _issue_payload() -> dict[str, Any]:
    """Two stories in two sections, plus section intros -- enough surface to
    prove containment (an edit to one must not see or touch the other)."""
    return {
        "schema_version": 6,
        "issue_number": None,
        "revision": 0,
        "date": DATE.isoformat(),
        "pulse": {
            "schema_version": 3,
            "name": "pulse",
            "stories": [{
                "story_id": STORY_A,
                "headline": "Latency drops on the fleet",
                "summary": _BODY_A,
                "source_urls": ["https://example.com/a"],
                "prior_coverage_ref": None,
                "signal": "read",
                "verification": {
                    "schema_version": 1,
                    "story_id": STORY_A,
                    "prompt_version": "v0.4",
                    "claims": [],
                    "has_contradiction": False,
                    "has_unsupported": False,
                    "headline_flagged": False,
                },
            }],
            "intro_lead": None,
            "intro_body": None,
        },
        "sections": [{
            "schema_version": 3,
            "name": "big_picture",
            "stories": [{
                "story_id": STORY_B,
                "headline": "A second lab replicates the result",
                "summary": _BODY_B,
                "source_urls": ["https://example.com/b"],
                "prior_coverage_ref": None,
                "signal": "read",
                "verification": {
                    "schema_version": 1,
                    "story_id": STORY_B,
                    "prompt_version": "v0.4",
                    "claims": [],
                    "has_contradiction": False,
                    "has_unsupported": False,
                    "headline_flagged": False,
                },
            }],
            "intro_lead": "Trust, but verify.",
            "intro_body": "Across the day, the same shape repeats.",
        }],
        "generated_at": "2026-08-02T05:55:26Z",
        "prompt_versions": {"rank": "v0.7", "summarise": "v0.20"},
        "notes": "shape: green -- pulse: 1, big_picture: 1",
    }


def _finding(
    finding_id: str = "f001",
    *,
    story_id: str | None = STORY_A,
    section: str | None = "pulse",
    kind: str = "story",
    field: str = "summary",
    quote: str = "the gain holds under sustained load",
    instruction: str = "Close on a plain take instead of a hedge.",
    fix_kind: str = "text_edit",
    severity: str = "major",
) -> ReviewFinding:
    target = (
        ReviewTarget(kind="story", story_id=story_id, section=section, field=field)
        if kind == "story"
        else ReviewTarget(kind="section", section=section, field=field)
    )
    return ReviewFinding(
        finding_id=finding_id,
        target=target,
        criterion="closing_shape",
        severity=severity,  # type: ignore[arg-type]
        quote=quote,
        fix_kind=fix_kind,  # type: ignore[arg-type]
        instruction=instruction,
    )


def _stage(
    tmp_data_root: Path,
    findings: list[ReviewFinding],
    *,
    issue: dict[str, Any] | None = None,
    stale: bool = False,
) -> Path:
    """Write a staged issue.json + a matching review.json.

    ``stale=True`` rewrites issue.json AFTER hashing it, so the review's
    ``issue_sha256`` no longer describes the file -- exactly the state the
    freshness check exists to catch.
    """
    payload = issue if issue is not None else _issue_payload()
    target = paths.staging_dir(DATE)
    target.mkdir(parents=True, exist_ok=True)
    issue_path = target / "issue.json"
    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    issue_path.write_text(body, encoding="utf-8")
    sha = hashlib.sha256(issue_path.read_bytes()).hexdigest()

    report = ReviewReport(
        generated_at=_dt.datetime(2026, 8, 2, tzinfo=_dt.timezone.utc),
        computed_verdict="amber",
        one_line="one thing to fix",
        findings=findings,
        prompt_version="v1.0",
        thresholds_version="v1.0-2026-08-02",
        llm_model="judge-model",
        issue_sha256=sha,
    )
    review_mod._write_report_json(review_mod.review_json_path(DATE), report)

    if stale:
        payload["notes"] = "shape: amber -- edited after the review"
        issue_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return issue_path


def _stage_released(
    findings: list[ReviewFinding],
    *,
    issue: dict[str, Any] | None = None,
    restamp: bool = False,
) -> Path:
    """Write a released issue.json + a matching review.json.

    ``restamp=True`` reproduces what ``render.release_promote`` actually does
    on the way to ``data/released/``: it stamps ``issue_number`` into the
    issue AFTER the review hashed the staging bytes. The prose is identical;
    the bytes are not. That single byte difference is what made the first
    live ``/revise`` run refuse (2026-08-04), so it is reproduced here rather
    than described.
    """
    payload = issue if issue is not None else _issue_payload()
    target = paths.released_dir(DATE)
    target.mkdir(parents=True, exist_ok=True)
    issue_path = target / "issue.json"
    issue_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    sha = hashlib.sha256(issue_path.read_bytes()).hexdigest()
    report = ReviewReport(
        generated_at=_dt.datetime(2026, 8, 2, tzinfo=_dt.timezone.utc),
        computed_verdict="amber", findings=findings,
        prompt_version="v1.0", issue_sha256=sha,
    )
    review_mod._write_report_json(
        review_mod.review_json_path(DATE, canonical=True), report,
    )

    if restamp:
        payload["issue_number"] = 32
        issue_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return issue_path


def _group(
    text: str,
    *,
    field: str = "summary",
    findings: list[ReviewFinding] | None = None,
    operator: list[str] | None = None,
) -> _FieldGroup:
    target = ReviewTarget(
        kind="story", story_id=STORY_A, section="pulse", field=field,  # type: ignore[arg-type]
    ) if field in ("headline", "summary") else ReviewTarget(
        kind="section", section="big_picture", field=field,  # type: ignore[arg-type]
    )
    return _FieldGroup(
        target=target,
        text=text,
        findings=findings if findings is not None else [],
        operator_instructions=operator or [],
    )


# ---------------------------------------------------------------------------
# The validation gate.
# ---------------------------------------------------------------------------

class TestNumeralDiscipline:
    """The most dangerous edit class: a number changes while the sentence
    around it still reads fine. Nothing downstream would catch it -- the
    fact-check already ran, and the reader has no reason to doubt a
    plausible figure."""

    def test_invented_numeral_is_rejected(self) -> None:
        after = _BODY_A.replace(
            "under sustained load.", "under sustained load for 12 hours.",
        )
        assert validate_replacement(
            _group(_BODY_A, findings=[_finding()]), after,
        ) == "numeral_invented"

    def test_dropped_numeral_is_rejected(self) -> None:
        after = _BODY_A.replace("by 30 percent ", "")
        assert validate_replacement(
            _group(_BODY_A, findings=[_finding()]), after,
        ) == "numeral_dropped"

    def test_numeral_change_named_in_the_finding_is_allowed(self) -> None:
        """A finding that says "the source says 45, not 30" is licensing
        exactly that change. The licence has to be written down."""
        finding = _finding(
            quote="by 30 percent",
            instruction="The source says 45 percent, not 30 percent. Correct it.",
        )
        after = _BODY_A.replace("30 percent", "45 percent")
        assert validate_replacement(_group(_BODY_A, findings=[finding]), after) == ""

    def test_formatting_only_numeral_change_is_allowed(self) -> None:
        """A thousands separator is a formatting choice, not a factual one.
        Flagging it would train operators to ignore the check."""
        before = "The index tracked 1,200 releases across the quarter window."
        after = "The index tracked 1200 releases across the quarter window."
        assert validate_replacement(_group(before, findings=[_finding()]), after) == ""

    def test_operator_instruction_can_license_a_numeral(self) -> None:
        after = _BODY_A.replace("30 percent", "45 percent")
        group = _group(
            _BODY_A, findings=[],
            operator=["Change 30 percent to 45 percent -- the source was misread."],
        )
        assert validate_replacement(group, after) == ""


class TestSourceUrlSurvival:
    """A dropped link is a dropped attribution. The publication links out
    rather than reproducing; losing the link loses the source."""

    def test_dropped_url_is_rejected(self) -> None:
        before = "The write-up at https://example.com/paper covers the method."
        after = "The write-up covers the method in detail."
        assert validate_replacement(
            _group(before, findings=[_finding(quote="covers the method")]), after,
        ) == "url_dropped"

    def test_retained_url_passes(self) -> None:
        before = "The write-up at https://example.com/paper covers the method."
        after = "The write-up at https://example.com/paper explains the method."
        assert validate_replacement(
            _group(before, findings=[_finding(quote="covers the method")]), after,
        ) == ""


class TestEditContainment:
    """Above the edit ceiling a "revision" is a rewrite, and the story is no
    longer the one that was ranked, summarised, and fact-checked."""

    def test_wholesale_rewrite_is_rejected(self) -> None:
        after = (
            "Everything about this story is different now and none of the "
            "original wording survives the replacement at all here."
        )
        assert validate_replacement(
            _group(_BODY_A, findings=[_finding()]), after,
        ) == "edit_distance_exceeded"

    def test_targeted_edit_passes(self) -> None:
        after = _BODY_A.replace(
            "the team says the gain holds under sustained load.",
            "the gain holds under sustained load.",
        )
        assert validate_replacement(
            _group(_BODY_A, findings=[_finding()]), after,
        ) == ""

    def test_short_field_may_be_replaced_outright(self) -> None:
        """The absolute floor exists for exactly this: "Trust, but verify."
        is the single most-cited anti-pattern in EDITORIAL.md, and a pure
        percentage budget would make it unfixable."""
        group = _group(
            "Trust, but verify.", field="intro_lead",
            findings=[_finding(
                kind="section", section="big_picture", field="intro_lead",
                quote="Trust, but verify.",
                instruction="Replace this cliche with a specific pattern.",
            )],
        )
        assert validate_replacement(group, "Costs land before clarity does.") == ""


class TestDeletionDiscipline:
    """When a finding says "delete X", only X's absence proves it was done.
    A model that rewords a flagged phrase has not done what was asked."""

    def test_surviving_deletion_quote_is_rejected(self) -> None:
        finding = _finding(
            quote="the team says",
            instruction="Delete the attribution hedge 'the team says'.",
        )
        after = _BODY_A  # unchanged -- the quote is still there
        assert validate_replacement(
            _group(_BODY_A, findings=[finding]), after,
        ) == "deletion_quote_survived"

    def test_honoured_deletion_passes(self) -> None:
        finding = _finding(
            quote="the team says",
            instruction="Delete the attribution hedge 'the team says'.",
        )
        after = _BODY_A.replace("the team says the gain", "the gain")
        assert validate_replacement(
            _group(_BODY_A, findings=[finding]), after,
        ) == ""

    def test_rewording_a_deletion_target_is_rejected(self) -> None:
        """The subtle case: the model edits around the flagged phrase and
        leaves it standing. Only the quote check catches this."""
        finding = _finding(
            quote="the team says",
            instruction="Remove 'the team says' -- it is an attribution hedge.",
        )
        after = _BODY_A.replace("sustained load.", "load.")
        assert validate_replacement(
            _group(_BODY_A, findings=[finding]), after,
        ) == "deletion_quote_survived"


# ---------------------------------------------------------------------------
# Containment -- what the model is allowed to see.
# ---------------------------------------------------------------------------

class TestPromptContainment:
    """One call per field, and the call sees only that field. A breach means
    one finding can drift text nobody flagged."""

    def test_prompt_carries_only_the_target_field(
        self, tmp_data_root: Path,
    ) -> None:
        _stage(tmp_data_root, [_finding()])
        prompts: list[str] = []

        def _capture(prompt: str) -> str:
            prompts.append(prompt)
            return _BODY_A.replace("sustained load.", "load.")

        with patch("src.revise._call_revise_llm", side_effect=_capture):
            revise_day(DATE, shadow=True)

        assert len(prompts) == 1
        assert _BODY_A in prompts[0]
        assert _BODY_B not in prompts[0], (
            "containment breach: the revise prompt showed the model another "
            "story's text"
        )
        assert "Trust, but verify." not in prompts[0]

    def test_one_call_per_field_not_per_finding(
        self, tmp_data_root: Path,
    ) -> None:
        """Two findings about the same field are answered together -- two
        sequential rewrites of one field would have the second overwrite the
        first's work."""
        _stage(tmp_data_root, [
            _finding("f001", quote="the gain holds under sustained load"),
            _finding("f002", quote="cuts inference latency",
                     instruction="Name the actor before the effect."),
        ])
        calls: list[str] = []

        def _capture(prompt: str) -> str:
            calls.append(prompt)
            return _BODY_A.replace("sustained load.", "load.")

        with patch("src.revise._call_revise_llm", side_effect=_capture):
            revise_day(DATE, shadow=True)
        assert len(calls) == 1

    def test_two_fields_get_two_calls(self, tmp_data_root: Path) -> None:
        _stage(tmp_data_root, [
            _finding("f001", quote="the gain holds under sustained load"),
            _finding("f002", field="headline", quote="Latency drops on the fleet",
                     instruction="Name the artefact."),
        ])
        calls: list[str] = []

        def _capture(prompt: str) -> str:
            calls.append(prompt)
            return "Fleet latency drops after the release lands"

        with patch("src.revise._call_revise_llm", side_effect=_capture):
            revise_day(DATE, shadow=True)
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# Fix-kind routing.
# ---------------------------------------------------------------------------

class TestFixKindRouting:
    """The engine acts on ``text_edit`` and only on ``text_edit``. A finding
    that needs a re-pick or a source cannot be fixed by rewriting prose, and
    attempting it produces text that reads corrected while the problem
    stands."""

    def test_structural_finding_is_not_acted_on(
        self, tmp_data_root: Path,
    ) -> None:
        _stage(tmp_data_root, [_finding(fix_kind="structural")])
        with patch("src.revise._call_revise_llm") as call:
            report = revise_day(DATE, shadow=True)
        call.assert_not_called()
        assert report.ran is False

    def test_human_finding_is_not_acted_on(
        self, tmp_data_root: Path,
    ) -> None:
        _stage(tmp_data_root, [_finding(fix_kind="human")])
        with patch("src.revise._call_revise_llm") as call:
            revise_day(DATE, shadow=True)
        call.assert_not_called()


# ---------------------------------------------------------------------------
# Freshness.
# ---------------------------------------------------------------------------

class TestFreshness:
    """A review of a superseded draft describes text that is no longer
    there. Acting on it would edit the wrong sentences with high
    confidence."""

    def test_stale_review_refuses(self, tmp_data_root: Path) -> None:
        _stage(tmp_data_root, [_finding()], stale=True)
        with patch("src.revise._call_revise_llm") as call:
            report = revise_day(DATE, shadow=False)
        call.assert_not_called()
        assert report.ran is False
        assert "stale" in report.note

    def test_stale_review_leaves_the_issue_untouched(
        self, tmp_data_root: Path,
    ) -> None:
        issue_path = _stage(tmp_data_root, [_finding()], stale=True)
        before = issue_path.read_bytes()
        with patch("src.revise._call_revise_llm", return_value="anything"):
            revise_day(DATE, shadow=False)
        assert issue_path.read_bytes() == before

    def test_missing_review_refuses(self, tmp_data_root: Path) -> None:
        target = paths.staging_dir(DATE)
        target.mkdir(parents=True, exist_ok=True)
        (target / "issue.json").write_text(json.dumps(_issue_payload()))
        report = revise_day(DATE, shadow=True)
        assert report.ran is False
        assert "review.json" in report.note

    def test_a_refusal_still_records_the_operator_instruction(
        self, tmp_data_root: Path,
    ) -> None:
        """The instruction is what the operator asked for; a refusal that
        dropped it reads as an unprompted no-op. That is precisely how the
        first live ``/revise`` run was misdiagnosed as a lost flag when the
        engine had in fact refused a stale review."""
        _stage(tmp_data_root, [_finding()], stale=True)
        report = revise_day(
            DATE, shadow=False, instruction="apply only the major ones",
        )
        assert report.ran is False
        assert report.cycle is not None
        assert report.cycle.operator_instruction == "apply only the major ones"

    def test_a_refusal_records_the_mode_it_ran_in(
        self, tmp_data_root: Path,
    ) -> None:
        """A ``--live`` invocation that refused must not be logged as a
        shadow cycle -- the two say different things about what the operator
        asked for."""
        _stage(tmp_data_root, [_finding()], stale=True)
        report = revise_day(DATE, shadow=False)
        assert report.cycle is not None
        assert report.cycle.mode == "live"


# ---------------------------------------------------------------------------
# Freshness on the released copy.
# ---------------------------------------------------------------------------

class TestReleasedCopyFreshness:
    """``render.release_promote`` stamps ``issue_number`` into the canonical
    ``issue.json``, so its bytes can NEVER equal the staging bytes the
    reviewer hashed -- while every sentence in it is identical. A byte-hash
    freshness check on the released copy is therefore not a freshness test at
    all, it is an unconditional refusal, and that is what the first live
    ``/revise`` run hit on 2026-08-04.

    On that copy the engine establishes freshness from the evidence each
    finding carries: its verbatim quote must still be in the field it points
    at. Same rule ``src/review.py`` already uses to filter the reviewer's
    output."""

    def test_release_stamped_issue_is_still_revisable(
        self, tmp_data_root: Path,
    ) -> None:
        """The regression test for the bug. Prose unchanged, bytes changed by
        the release stamp -- the edit must go through."""
        issue_path = _stage_released([_finding()], restamp=True)
        with patch(
            "src.revise._call_revise_llm",
            return_value=_BODY_A.replace("sustained load.", "load."),
        ):
            report = revise_day(DATE, shadow=False, canonical=True)
        assert report.ran is True
        assert report.applied == 1
        payload = json.loads(issue_path.read_text(encoding="utf-8"))
        assert payload["pulse"]["stories"][0]["summary"].endswith("under load.")
        # The release stamp survives the edit: revise rewrites fields, it
        # does not re-issue the day.
        assert payload["issue_number"] == 32

    def test_quote_evidence_is_recorded_on_the_cycle(
        self, tmp_data_root: Path,
    ) -> None:
        """Which basis the engine accepted is audit information: "the hash
        matched" and "the quotes still line up" are different guarantees."""
        _stage_released([_finding()], restamp=True)
        with patch(
            "src.revise._call_revise_llm",
            return_value=_BODY_A.replace("sustained load.", "load."),
        ):
            report = revise_day(DATE, shadow=False, canonical=True)
        assert report.cycle is not None
        assert "quote evidence" in report.cycle.note

    def test_finding_whose_quote_is_gone_is_dropped(
        self, tmp_data_root: Path,
    ) -> None:
        """A finding that no longer quotes live text is about a sentence
        somebody already changed. Dropping it is the same call review.py
        makes; acting on it would edit text the reviewer never read."""
        _stage_released(
            [
                _finding("f001"),
                _finding(
                    "f002", field="headline",
                    quote="a headline that was replaced days ago",
                    instruction="Name the artefact.",
                ),
            ],
            restamp=True,
        )
        with patch(
            "src.revise._call_revise_llm",
            return_value=_BODY_A.replace("sustained load.", "load."),
        ) as call:
            report = revise_day(DATE, shadow=True, canonical=True)
        assert call.call_count == 1
        assert report.cycle is not None
        assert [c.finding_ids for c in report.cycle.changes] == [["f001"]]

    def test_all_quotes_gone_refuses(self, tmp_data_root: Path) -> None:
        """No anchored finding means no evidence the review describes this
        text at all. That is a genuinely stale review, and the engine still
        refuses it -- the quote fallback loosens the check's mechanism, not
        its standard."""
        _stage_released(
            [_finding(quote="a sentence that is not in this issue")],
            restamp=True,
        )
        with patch("src.revise._call_revise_llm") as call:
            report = revise_day(DATE, shadow=False, canonical=True)
        call.assert_not_called()
        assert report.ran is False
        assert "stale" in report.note

    def test_targeted_instruction_survives_a_stale_review(
        self, tmp_data_root: Path,
    ) -> None:
        """A ``--target`` instruction names its own field and is shown that
        field's current text, so it does not depend on the review being
        fresh. It can carry a cycle when every finding has aged out."""
        _stage_released(
            [_finding(quote="a sentence that is not in this issue")],
            restamp=True,
        )
        with patch(
            "src.revise._call_revise_llm",
            return_value="Costs land before clarity does.",
        ):
            report = revise_day(
                DATE, shadow=False, canonical=True,
                instruction="Replace the cliche.",
                instruction_target="section:big_picture:intro_lead",
            )
        assert report.applied == 1

    def test_staging_still_demands_an_exact_hash_match(
        self, tmp_data_root: Path,
    ) -> None:
        """The fallback is scoped to the released copy on purpose. In staging
        a byte mismatch means the draft was regenerated after review, and
        ``aiv review`` is one command away -- so the strict check stays."""
        _stage(tmp_data_root, [_finding()], stale=True)  # quote still present
        with patch("src.revise._call_revise_llm") as call:
            report = revise_day(DATE, shadow=False)
        call.assert_not_called()
        assert report.ran is False


# ---------------------------------------------------------------------------
# Shadow mode.
# ---------------------------------------------------------------------------

class TestShadowMode:
    """Shadow is the observation contract: compute everything, write
    nothing. A shadow run that edited the issue would be the worst possible
    surprise."""

    def test_issue_json_is_not_touched(self, tmp_data_root: Path) -> None:
        issue_path = _stage(tmp_data_root, [_finding()])
        before = issue_path.read_bytes()
        with patch(
            "src.revise._call_revise_llm",
            return_value=_BODY_A.replace("sustained load.", "load."),
        ):
            revise_day(DATE, shadow=True)
        assert issue_path.read_bytes() == before

    def test_change_is_recorded_as_proposed(self, tmp_data_root: Path) -> None:
        _stage(tmp_data_root, [_finding()])
        with patch(
            "src.revise._call_revise_llm",
            return_value=_BODY_A.replace("sustained load.", "load."),
        ):
            report = revise_day(DATE, shadow=True)
        assert report.proposed == 1
        assert report.applied == 0
        assert report.cycle is not None
        assert report.cycle.changes[0].status == "proposed"

    def test_shadow_still_reports_what_live_would_refuse(
        self, tmp_data_root: Path,
    ) -> None:
        """A preview that hid the refusals would not be a preview."""
        _stage(tmp_data_root, [_finding()])
        with patch(
            "src.revise._call_revise_llm",
            return_value=_BODY_A.replace(
                "under sustained load.", "under sustained load for 12 hours.",
            ),
        ):
            report = revise_day(DATE, shadow=True)
        assert report.rejected == 1
        assert report.cycle is not None
        assert report.cycle.changes[0].reject_reason == "numeral_invented"

    def test_revisions_jsonl_is_written(self, tmp_data_root: Path) -> None:
        _stage(tmp_data_root, [_finding()])
        with patch(
            "src.revise._call_revise_llm",
            return_value=_BODY_A.replace("sustained load.", "load."),
        ):
            revise_day(DATE, shadow=True)
        lines = revisions_path(DATE).read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["mode"] == "shadow"
        assert record["prompt_version"] == REVISE_PROMPT_VERSION


# ---------------------------------------------------------------------------
# Live mode.
# ---------------------------------------------------------------------------

class TestLiveMode:
    """Live writes. The two things that must be true afterwards: the text
    changed, and the fact-check that described the old text is gone."""

    def _apply(self, tmp_data_root: Path) -> dict[str, Any]:
        _stage(tmp_data_root, [_finding()])
        with patch(
            "src.revise._call_revise_llm",
            return_value=_BODY_A.replace("sustained load.", "load."),
        ):
            revise_day(DATE, shadow=False)
        return json.loads(
            paths.issue_path(DATE, canonical=False).read_text(encoding="utf-8")
        )

    def test_field_is_rewritten(self, tmp_data_root: Path) -> None:
        payload = self._apply(tmp_data_root)
        assert payload["pulse"]["stories"][0]["summary"].endswith("under load.")

    def test_verification_is_cleared_on_the_touched_story(
        self, tmp_data_root: Path,
    ) -> None:
        """The fact-check was performed against text that no longer exists.
        ``None`` correctly means "not verified"; a retained ``clean`` would
        assert something nobody checked."""
        payload = self._apply(tmp_data_root)
        assert payload["pulse"]["stories"][0]["verification"] is None

    def test_verification_survives_on_untouched_stories(
        self, tmp_data_root: Path,
    ) -> None:
        payload = self._apply(tmp_data_root)
        assert payload["sections"][0]["stories"][0]["verification"] is not None

    def test_rejected_replacement_leaves_the_field_untouched(
        self, tmp_data_root: Path,
    ) -> None:
        _stage(tmp_data_root, [_finding()])
        with patch(
            "src.revise._call_revise_llm",
            return_value=_BODY_A.replace(
                "under sustained load.", "under sustained load for 12 hours.",
            ),
        ):
            report = revise_day(DATE, shadow=False)
        payload = json.loads(
            paths.issue_path(DATE, canonical=False).read_text(encoding="utf-8")
        )
        assert payload["pulse"]["stories"][0]["summary"] == _BODY_A
        assert report.applied == 0
        assert report.rejected == 1

    def test_rejected_replacement_leaves_verification_intact(
        self, tmp_data_root: Path,
    ) -> None:
        """Nothing changed, so nothing about the fact-check is stale."""
        _stage(tmp_data_root, [_finding()])
        with patch(
            "src.revise._call_revise_llm",
            return_value=_BODY_A.replace(
                "under sustained load.", "under sustained load for 12 hours.",
            ),
        ):
            revise_day(DATE, shadow=False)
        payload = json.loads(
            paths.issue_path(DATE, canonical=False).read_text(encoding="utf-8")
        )
        assert payload["pulse"]["stories"][0]["verification"] is not None

    def test_section_intro_edit_applies(self, tmp_data_root: Path) -> None:
        _stage(tmp_data_root, [_finding(
            kind="section", section="big_picture", field="intro_lead",
            quote="Trust, but verify.",
            instruction="Replace this cliche with a specific pattern.",
        )])
        with patch(
            "src.revise._call_revise_llm",
            return_value="Costs land before clarity does.",
        ):
            revise_day(DATE, shadow=False)
        payload = json.loads(
            paths.issue_path(DATE, canonical=False).read_text(encoding="utf-8")
        )
        assert payload["sections"][0]["intro_lead"] == "Costs land before clarity does."

    def test_applied_issue_no_longer_matches_the_review_hash(
        self, tmp_data_root: Path,
    ) -> None:
        """Deliberate downstream consequence: an edited issue has not been
        reviewed, so ``src/gate.py`` must hold with ``hold:stale-review``
        until it is re-reviewed."""
        _stage(tmp_data_root, [_finding()])
        with patch(
            "src.revise._call_revise_llm",
            return_value=_BODY_A.replace("sustained load.", "load."),
        ):
            report = revise_day(DATE, shadow=False)
        assert report.cycle is not None
        assert report.cycle.issue_sha256_after is not None
        assert report.cycle.issue_sha256_after != report.cycle.issue_sha256_before
        current = hashlib.sha256(
            paths.issue_path(DATE, canonical=False).read_bytes()
        ).hexdigest()
        assert current == report.cycle.issue_sha256_after

    def test_over_long_replacement_is_rejected(
        self, tmp_data_root: Path,
    ) -> None:
        """Length bounds come from the pydantic model, so a replacement that
        pydantic would refuse never reaches issue.json."""
        _stage(tmp_data_root, [_finding(
            field="headline", quote="Latency drops on the fleet",
            instruction="Name the artefact in the headline.",
        )])
        with patch("src.revise._call_revise_llm", return_value="x " * 300):
            report = revise_day(DATE, shadow=False)
        assert report.rejected == 1
        payload = json.loads(
            paths.issue_path(DATE, canonical=False).read_text(encoding="utf-8")
        )
        assert payload["pulse"]["stories"][0]["headline"] == "Latency drops on the fleet"


# ---------------------------------------------------------------------------
# The take field (2026-08-08).
#
# SummaryBlock.take (schema v4) joined FIELD_BOUNDS / _FIELD_GUIDANCE the
# same day it landed on the model. `_revise_one_field` does
# `FIELD_BOUNDS[field_name]` with no `.get()` fallback -- a field missing
# from that table is not a silent no-op, it is a KeyError that takes the
# whole revise cycle down. Nothing else in the suite exercised a
# field="take" finding through a real cycle before this gap was closed.
# ---------------------------------------------------------------------------

_TAKE_A = "Local-first inference is the procurement default now."


def _issue_payload_with_take() -> dict[str, Any]:
    payload = _issue_payload()
    payload["pulse"]["stories"][0]["take"] = _TAKE_A
    return payload


class TestTakeFieldRevision:
    def test_take_is_in_the_bounds_table(self) -> None:
        """Cheapest possible guard: fails at collection-adjacent speed,
        before the KeyError a missing entry would cause mid-cycle."""
        from src.revise import FIELD_BOUNDS
        assert "take" in FIELD_BOUNDS

    def test_take_finding_is_applied_without_keyerror(
        self, tmp_data_root: Path,
    ) -> None:
        """The seam: a field="take" finding reaches a live, applied
        revision -- FIELD_BOUNDS lookup, guidance lookup, and the
        validation gate all resolve cleanly for the new field."""
        _stage(tmp_data_root, [_finding(
            field="take", quote=_TAKE_A,
            instruction="Recast without the consultant frame.",
        )], issue=_issue_payload_with_take())
        with patch(
            "src.revise._call_revise_llm",
            return_value="Pipeable agent output is now a vendor contract.",
        ):
            report = revise_day(DATE, shadow=False)
        assert report.applied == 1
        payload = json.loads(
            paths.issue_path(DATE, canonical=False).read_text(encoding="utf-8")
        )
        assert payload["pulse"]["stories"][0]["take"] == (
            "Pipeable agent output is now a vendor contract."
        )

    def test_over_bound_take_replacement_is_rejected(
        self, tmp_data_root: Path,
    ) -> None:
        """Pins the 200-char structural cap FIELD_BOUNDS reads off
        SummaryBlock.take -- the same bound pydantic itself enforces at
        the SummaryBlock boundary, caught here BEFORE the write."""
        _stage(tmp_data_root, [_finding(
            field="take", quote=_TAKE_A,
            instruction="Expand on the reasoning.",
        )], issue=_issue_payload_with_take())
        with patch("src.revise._call_revise_llm", return_value="x " * 150):
            report = revise_day(DATE, shadow=False)
        assert report.rejected == 1
        payload = json.loads(
            paths.issue_path(DATE, canonical=False).read_text(encoding="utf-8")
        )
        assert payload["pulse"]["stories"][0]["take"] == _TAKE_A


# ---------------------------------------------------------------------------
# The operator instruction.
# ---------------------------------------------------------------------------

class TestRedesignFieldSurfaces:
    """Wave three (2026-08-09): FIELD_BOUNDS / guidance / routing learn the
    redesign fields (R2), plus the KeyError-class regression the take
    integration taught -- a review-vocabulary field this stage cannot
    bound must be refused, never crash the failure-soft engine."""

    def test_field_bounds_cover_the_entire_review_vocabulary(self) -> None:
        """The drift guard that would have caught the take gap: every
        ReviewTargetField token has bounds, so the vocabulary can never
        again outrun the reviser."""
        from typing import get_args

        from src.models import ReviewTargetField

        for token in get_args(ReviewTargetField):
            assert token in revise_mod.FIELD_BOUNDS, (
                f"ReviewTargetField {token!r} has no FIELD_BOUNDS entry"
            )

    def test_redesign_bounds_match_the_models(self) -> None:
        """The token != model-field mapping (digest_lead -> DigestBullet.lead)
        must introspect the right field's caps."""
        assert revise_mod.FIELD_BOUNDS["synthesis"] == (1, 500)
        assert revise_mod.FIELD_BOUNDS["digest_lead"] == (1, 80)
        assert revise_mod.FIELD_BOUNDS["digest_sentence"] == (1, 300)

    def test_redesign_fields_have_prompt_guidance(self) -> None:
        for token in ("synthesis", "digest_lead", "digest_sentence"):
            assert token in revise_mod._FIELD_GUIDANCE

    def test_unknown_field_bounds_rejects_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The KeyError regression: with a bounds entry missing, the field
        is refused BEFORE any LLM call -- revise_day has no try around
        _revise_one_field, so a raise here would break failure-soft.
        (Mutation evidence: reverting the .get() guard to
        FIELD_BOUNDS[field_name] turns this test into a KeyError.)"""
        monkeypatch.delitem(revise_mod.FIELD_BOUNDS, "digest_lead")

        def _no_call(prompt):  # pragma: no cover -- the assertion IS the test
            raise AssertionError("LLM called for an unboundable field")

        monkeypatch.setattr(revise_mod, "_call_revise_llm", _no_call)
        group = _FieldGroup(
            target=ReviewTarget(
                kind="digest", digest_index=0, field="digest_lead",
            ),
            text="Fleet latency drops today.",
        )
        change = revise_mod._revise_one_field(group, shadow=True)
        assert change.status == "rejected"
        assert change.reject_reason == "unknown_field_bounds"

    def test_selector_parses_digest_targets(self) -> None:
        target = revise_mod._parse_target_selector("digest:2:digest_sentence")
        assert target is not None
        assert target.kind == "digest"
        assert target.digest_index == 2
        assert target.field == "digest_sentence"

    def test_selector_rejects_non_integer_digest_index(self) -> None:
        assert revise_mod._parse_target_selector(
            "digest:first:digest_lead"
        ) is None


# ---------------------------------------------------------------------------
# Digest + synthesis revision -- routing and spec re-validation (R2).
# ---------------------------------------------------------------------------

_STORY_C = "c_" + "c" * 12

_D_LEAD_1 = "Pulse story leads today."
_D_LEAD_2 = "Replication lands elsewhere too."
_D_LEAD_3 = "Benchmark harness ships broadly."
_D_SENT_1 = (
    "The fleet release cuts latency by thirty percent and the team says "
    "the gain holds."
)
_D_SENT_2 = (
    "A second lab replicated the throughput result on commodity hardware "
    "without any vendor tooling involved."
)
_D_SENT_3 = (
    "The new toolkit ships with a benchmark harness that runs the full "
    "suite in minutes."
)


def _issue_payload_with_digest() -> dict[str, Any]:
    """The two-story payload plus a hands_on story and a spec-clean
    3-bullet digest (bullet 1 = pulse, then big_picture, hands_on)."""
    payload = _issue_payload()
    payload["sections"].append({
        "schema_version": 3,
        "name": "hands_on",
        "stories": [{
            "story_id": _STORY_C,
            "headline": "A benchmark harness for agent stacks",
            "summary": "The toolkit runs the full suite in minutes.",
            "source_urls": ["https://example.com/c"],
            "prior_coverage_ref": None,
            "signal": "try",
            "verification": None,
        }],
        "intro_lead": None,
        "intro_body": None,
    })
    payload["digest"] = [
        {"lead": _D_LEAD_1, "sentence": _D_SENT_1, "story_ids": [STORY_A]},
        {"lead": _D_LEAD_2, "sentence": _D_SENT_2, "story_ids": [STORY_B]},
        {"lead": _D_LEAD_3, "sentence": _D_SENT_3, "story_ids": [_STORY_C]},
    ]
    return payload


def _digest_finding(
    finding_id: str = "f001", *, index: int = 0,
    field: str = "digest_lead", quote: str = _D_LEAD_1,
) -> ReviewFinding:
    return ReviewFinding(
        finding_id=finding_id,
        target=ReviewTarget(
            kind="digest", digest_index=index, field=field,  # type: ignore[arg-type]
        ),
        criterion="digest_shape",
        severity="major",
        quote=quote,
        fix_kind="text_edit",
        instruction="Recast the lead as a naming.",
    )


class TestDigestRevision:
    def test_live_cycle_routes_digest_edit_by_index(
        self, tmp_data_root: Path,
    ) -> None:
        """R2 routing: digest_lead resolves to Issue.digest[i].lead, the
        edit lands on exactly that bullet, and the primary story's
        verification is cleared (the bullet's verdicts live there, V3)."""
        payload = _issue_payload_with_digest()
        issue_path = _stage(tmp_data_root, [_digest_finding()], issue=payload)
        replacement = "Fleet latency falls fast."
        with patch.object(
            revise_mod, "_call_revise_llm", return_value=replacement,
        ):
            report = revise_day(DATE, shadow=False)

        assert report.applied == 1
        on_disk = json.loads(issue_path.read_text(encoding="utf-8"))
        assert on_disk["digest"][0]["lead"] == replacement
        assert on_disk["digest"][0]["sentence"] == _D_SENT_1  # untouched
        assert on_disk["digest"][1]["lead"] == _D_LEAD_2      # untouched
        # The primary story's verification is stale -> cleared.
        assert on_disk["pulse"]["stories"][0]["verification"] is None

    def test_shadow_cycle_leaves_the_digest_untouched(
        self, tmp_data_root: Path,
    ) -> None:
        payload = _issue_payload_with_digest()
        issue_path = _stage(tmp_data_root, [_digest_finding()], issue=payload)
        with patch.object(
            revise_mod, "_call_revise_llm",
            return_value="Fleet latency falls fast.",
        ):
            report = revise_day(DATE, shadow=True)
        assert report.proposed == 1
        on_disk = json.loads(issue_path.read_text(encoding="utf-8"))
        assert on_disk["digest"][0]["lead"] == _D_LEAD_1

    def test_spec_violating_digest_replacement_is_rejected(
        self, tmp_data_root: Path,
    ) -> None:
        """The R2 re-validation: a replacement the generic gate would
        accept but the digest spec forbids (a question, over the lead
        word budget) is refused via summarise._digest_violations."""
        payload = _issue_payload_with_digest()
        _stage(tmp_data_root, [_digest_finding()], issue=payload)
        with patch.object(
            revise_mod, "_call_revise_llm",
            return_value="Do agents win everywhere all at once today?",
        ):
            report = revise_day(DATE, shadow=False)
        assert report.applied == 0 and report.rejected == 1
        assert report.cycle is not None
        assert report.cycle.changes[0].reject_reason == (
            "digest_spec_violation"
        )

    def test_preexisting_violation_elsewhere_does_not_block_the_edit(
        self,
    ) -> None:
        """Delta discipline: the spec check rejects NEW violations only,
        so a digest already off-spec in another bullet stays revisable."""
        payload = _issue_payload_with_digest()
        # Bullet 3's sentence blows the word cap -- a pre-existing defect.
        payload["digest"][2]["sentence"] = " ".join(["word"] * 30)
        group = _FieldGroup(
            target=ReviewTarget(
                kind="digest", digest_index=0, field="digest_lead",
            ),
            text=_D_LEAD_1,
        )
        reason = revise_mod._digest_spec_reason(
            group, "Fleet latency falls fast.", payload,
        )
        assert reason == ""

    def test_missing_bullet_index_yields_digest_target_missing(self) -> None:
        payload = _issue_payload_with_digest()
        group = _FieldGroup(
            target=ReviewTarget(
                kind="digest", digest_index=9, field="digest_lead",
            ),
            text=_D_LEAD_1,
        )
        reason = revise_mod._digest_spec_reason(
            group, "Fleet latency falls fast.", payload,
        )
        assert reason == "digest_target_missing"


class TestSynthesisRevision:
    _GOOD_SYNTHESIS = (
        "Production deployments crossed a real threshold this week across "
        "two separate firms. The pattern is consistent enough that risk "
        "teams should treat agent rollouts as standard change management "
        "rather than as experiments."
    )

    def _payload_with_synthesis(self) -> dict[str, Any]:
        payload = _issue_payload()
        payload["sections"][0]["synthesis"] = self._GOOD_SYNTHESIS
        payload["sections"][0]["intro_lead"] = None
        payload["sections"][0]["intro_body"] = None
        return payload

    def _synthesis_group(self, text: str) -> _FieldGroup:
        return _FieldGroup(
            target=ReviewTarget(
                kind="section", section="big_picture", field="synthesis",
            ),
            text=text,
        )

    def test_valid_synthesis_replacement_passes_the_spec_check(self) -> None:
        reason = revise_mod._synthesis_spec_reason(
            self._synthesis_group(self._GOOD_SYNTHESIS),
            self._GOOD_SYNTHESIS.replace("real threshold", "hard threshold"),
            self._payload_with_synthesis(),
        )
        assert reason == ""

    def test_aphoristic_or_underweight_synthesis_is_rejected(self) -> None:
        reason = revise_mod._synthesis_spec_reason(
            self._synthesis_group(self._GOOD_SYNTHESIS),
            "Costs precede clarity.",  # aphorism, under every floor
            self._payload_with_synthesis(),
        )
        assert reason == "synthesis_spec_violation"

    def test_quiet_day_currents_relaxes_the_floor(self) -> None:
        payload = self._payload_with_synthesis()
        payload["sections"].append({
            "schema_version": 3, "name": "currents", "stories": [],
            "intro_lead": None, "intro_body": None,
            "synthesis": "A quiet day on the currents front today.",
        })
        group = _FieldGroup(
            target=ReviewTarget(
                kind="section", section="currents", field="synthesis",
            ),
            text="A quiet day on the currents front today.",
        )
        # 9 words -- fails the standard 28-word floor, passes quiet-day.
        reason = revise_mod._synthesis_spec_reason(
            group, "A quieter day still on the currents front.", payload,
        )
        assert reason == ""

    def test_live_synthesis_edit_writes_the_section_field(
        self, tmp_data_root: Path,
    ) -> None:
        payload = self._payload_with_synthesis()
        finding = ReviewFinding(
            finding_id="f001",
            target=ReviewTarget(
                kind="section", section="big_picture", field="synthesis",
            ),
            criterion="synthesis_shape",
            severity="minor",
            quote="Production deployments crossed a real threshold",
            fix_kind="text_edit",
            instruction="Name the two firms' sectors instead of counting.",
        )
        issue_path = _stage(tmp_data_root, [finding], issue=payload)
        replacement = self._GOOD_SYNTHESIS.replace(
            "two separate firms", "two regulated firms",
        )
        with patch.object(
            revise_mod, "_call_revise_llm", return_value=replacement,
        ):
            report = revise_day(DATE, shadow=False)
        assert report.applied == 1
        on_disk = json.loads(issue_path.read_text(encoding="utf-8"))
        assert on_disk["sections"][0]["synthesis"] == replacement
        # Synthesis verdicts attach to the section's first story -> its
        # verification is now stale and must be cleared.
        assert on_disk["sections"][0]["stories"][0]["verification"] is None


class TestOperatorInstruction:
    """The ``/revise`` PR command. One extra directive under exactly the
    same containment as a finding -- including the validation gate."""

    def test_targeted_instruction_opens_a_group(
        self, tmp_data_root: Path,
    ) -> None:
        _stage(tmp_data_root, [])  # no findings at all
        with patch(
            "src.revise._call_revise_llm",
            return_value="Costs land before clarity does.",
        ):
            report = revise_day(
                DATE, shadow=False,
                instruction="Replace the cliche.",
                instruction_target="section:big_picture:intro_lead",
            )
        assert report.applied == 1
        payload = json.loads(
            paths.issue_path(DATE, canonical=False).read_text(encoding="utf-8")
        )
        assert payload["sections"][0]["intro_lead"] == "Costs land before clarity does."

    def test_instruction_without_a_target_or_findings_is_refused(
        self, tmp_data_root: Path,
    ) -> None:
        """An instruction with no address is an edit with no address. Code
        must not guess which field the operator meant."""
        _stage(tmp_data_root, [])
        with patch("src.revise._call_revise_llm") as call:
            report = revise_day(
                DATE, shadow=True, instruction="Tighten the prose.",
            )
        call.assert_not_called()
        assert report.ran is False

    def test_instruction_is_recorded_on_the_cycle(
        self, tmp_data_root: Path,
    ) -> None:
        """An operator directive is an editorial act and belongs in the
        trail next to the findings it ran alongside."""
        _stage(tmp_data_root, [_finding()])
        with patch(
            "src.revise._call_revise_llm",
            return_value=_BODY_A.replace("sustained load.", "load."),
        ):
            report = revise_day(
                DATE, shadow=True, instruction="Also tighten the close.",
            )
        assert report.cycle is not None
        assert report.cycle.operator_instruction == "Also tighten the close."

    def test_unparseable_target_does_not_block_the_findings(
        self, tmp_data_root: Path,
    ) -> None:
        """A typo'd selector must not stop the reviewed findings from being
        acted on."""
        _stage(tmp_data_root, [_finding()])
        with patch(
            "src.revise._call_revise_llm",
            return_value=_BODY_A.replace("sustained load.", "load."),
        ):
            report = revise_day(
                DATE, shadow=True, instruction="Tighten it.",
                instruction_target="nonsense",
            )
        assert report.proposed == 1


# ---------------------------------------------------------------------------
# The archive-copy seam.
# ---------------------------------------------------------------------------

class TestCanonicalSeam:
    """``canonical=True`` points every read and write at
    ``data/released/<date>/``. It exists because staging is gitignored, so a
    release-PR checkout has only the released copy -- which is what the
    ``/revise`` PR command edits."""

    def test_released_copy_is_revised(self, tmp_data_root: Path) -> None:
        """No release stamp here: the plain seam, where the hash still
        matches. ``TestReleasedCopyFreshness`` covers the stamped copy the
        real archive actually holds."""
        issue_path = _stage_released([_finding()])
        with patch(
            "src.revise._call_revise_llm",
            return_value=_BODY_A.replace("sustained load.", "load."),
        ):
            report = revise_day(DATE, shadow=False, canonical=True)
        assert report.applied == 1
        payload = json.loads(issue_path.read_text(encoding="utf-8"))
        assert payload["pulse"]["stories"][0]["summary"].endswith("under load.")

    def test_staging_default_does_not_see_the_released_copy(
        self, tmp_data_root: Path,
    ) -> None:
        """The default must stay staging-only: silently falling back to the
        released archive would let a draft-stage command edit a published
        issue."""
        _stage_released([_finding()])
        with patch("src.revise._call_revise_llm") as call:
            report = revise_day(DATE, shadow=True)
        call.assert_not_called()
        assert report.ran is False


# ---------------------------------------------------------------------------
# The severity pre-filter.
# ---------------------------------------------------------------------------

class TestSeverityFilter:
    """"Apply only the major recommendations" is a selection rule, and
    selection is code. The filter runs before a prompt exists, so a dropped
    finding costs nothing and cannot be talked past -- which is the whole
    reason it is not a sentence in the prompt. A field-scoped model cannot
    see the other findings and so could not honour that instruction anyway."""

    def test_floor_keeps_at_and_above(self) -> None:
        findings = [
            _finding("f001", severity="note"),
            _finding("f002", severity="minor"),
            _finding("f003", severity="major"),
            _finding("f004", severity="blocking"),
        ]
        kept, dropped = revise_mod.filter_findings_by_severity(findings, "major")
        assert [f.finding_id for f in kept] == ["f003", "f004"]
        assert dropped == 2

    def test_default_floor_filters_nothing(self) -> None:
        findings = [_finding("f001", severity="note")]
        kept, dropped = revise_mod.filter_findings_by_severity(
            findings, revise_mod.MIN_SEVERITY_DEFAULT,
        )
        assert kept == findings
        assert dropped == 0

    def test_unknown_floor_falls_back_to_keeping_everything(self) -> None:
        """Failure-soft inside the engine: the safe reading of a typo is
        "the operator meant everything", not "discard the whole review"."""
        findings = [_finding("f001", severity="note")]
        kept, dropped = revise_mod.filter_findings_by_severity(findings, "urgent")
        assert kept == findings
        assert dropped == 0

    def test_filtered_finding_never_reaches_the_llm(
        self, tmp_data_root: Path,
    ) -> None:
        """The point of a pre-filter: no tokens are spent on a finding the
        operator excluded."""
        _stage(tmp_data_root, [
            _finding("f001", severity="major"),
            _finding(
                "f002", field="headline", severity="minor",
                quote="Latency drops on the fleet",
                instruction="Name the artefact in the headline.",
            ),
        ])
        with patch(
            "src.revise._call_revise_llm",
            return_value=_BODY_A.replace("sustained load.", "load."),
        ) as call:
            report = revise_day(DATE, shadow=True, min_severity="major")
        assert call.call_count == 1
        assert "Name the artefact" not in call.call_args[0][0]
        assert report.cycle is not None
        assert [c.finding_ids for c in report.cycle.changes] == [["f001"]]

    def test_everything_filtered_out_refuses_without_calling_the_llm(
        self, tmp_data_root: Path,
    ) -> None:
        _stage(tmp_data_root, [_finding(severity="minor")])
        with patch("src.revise._call_revise_llm") as call:
            report = revise_day(DATE, shadow=False, min_severity="blocking")
        call.assert_not_called()
        assert report.ran is False
        assert "min_severity=blocking" in report.note

    def test_the_refusal_distinguishes_filtered_from_empty(
        self, tmp_data_root: Path,
    ) -> None:
        """"The review found nothing to rewrite" and "the floor removed
        everything it found" produce identical counts and mean opposite
        things. The operator has to be able to tell which one happened."""
        _stage(tmp_data_root, [])
        report = revise_day(DATE, shadow=True, min_severity="blocking")
        assert "no text_edit findings" in report.note

    def test_the_floor_is_not_blamed_for_findings_it_could_not_fix(
        self, tmp_data_root: Path,
    ) -> None:
        """A structural finding below the floor was never going to be
        rewritten, so counting it as a cost of the floor would overstate
        what the operator's choice excluded."""
        _stage(tmp_data_root, [
            _finding("f001", severity="major"),
            _finding("f002", severity="minor", fix_kind="structural"),
        ])
        with patch(
            "src.revise._call_revise_llm",
            return_value=_BODY_A.replace("sustained load.", "load."),
        ):
            report = revise_day(DATE, shadow=True, min_severity="major")
        assert report.cycle is not None
        assert "below min_severity" not in report.cycle.note

    def test_dropped_count_is_recorded_on_the_cycle(
        self, tmp_data_root: Path,
    ) -> None:
        _stage(tmp_data_root, [
            _finding("f001", severity="major"),
            _finding(
                "f002", field="headline", severity="note",
                quote="Latency drops on the fleet",
                instruction="Name the artefact in the headline.",
            ),
        ])
        with patch(
            "src.revise._call_revise_llm",
            return_value=_BODY_A.replace("sustained load.", "load."),
        ):
            report = revise_day(DATE, shadow=True, min_severity="major")
        assert report.cycle is not None
        assert "1 text_edit finding(s) below min_severity=major" in report.cycle.note

    def test_a_severity_floor_does_not_read_as_a_stale_review(
        self, tmp_data_root: Path,
    ) -> None:
        """Order matters for the diagnosis. Freshness is a property of the
        review against the file and the operator's floor cannot change it,
        so freshness is decided first -- otherwise a day whose findings are
        all minor reports "stale review" under ``--min-severity major`` and
        sends the operator to re-run a review that was never the problem.
        (2026-08-04 had exactly that shape: four minor text_edit findings.)"""
        _stage_released([_finding(severity="minor")], restamp=True)
        with patch("src.revise._call_revise_llm") as call:
            report = revise_day(
                DATE, shadow=False, canonical=True, min_severity="major",
            )
        call.assert_not_called()
        assert report.ran is False
        assert "min_severity=major" in report.note
        assert "stale" not in report.note

    def test_operator_instruction_is_not_severity_filtered(
        self, tmp_data_root: Path,
    ) -> None:
        """A directive the operator typed has no severity and is not the
        reviewer's opinion. The floor selects among findings; it must not
        discard the thing the operator explicitly asked for."""
        _stage(tmp_data_root, [_finding(severity="note")])
        with patch(
            "src.revise._call_revise_llm",
            return_value="Costs land before clarity does.",
        ):
            report = revise_day(
                DATE, shadow=True, min_severity="blocking",
                instruction="Replace the cliche.",
                instruction_target="section:big_picture:intro_lead",
            )
        assert report.proposed == 1


# ---------------------------------------------------------------------------
# Re-rendering after a live edit.
# ---------------------------------------------------------------------------

class TestReRender:
    """A live cycle that changed ``issue.json`` and left the HTML alone has
    only half-applied the edit: the page a reader opens would still carry the
    sentence the editor asked to remove. The ``/revise`` workflow commits
    ``docs/released/<date>.html`` and ``docs/index.html`` and depends on this
    stage having written them."""

    _GOOD_REPLACEMENT = _BODY_A.replace("sustained load.", "load.")
    _REJECTED_REPLACEMENT = _BODY_A.replace(
        "under sustained load.", "under sustained load for 12 hours.",
    )

    def test_released_edit_renders_in_release_mode(
        self, tmp_data_root: Path, render_stub: Any,
    ) -> None:
        """``mode="release"`` is what writes docs/released/<date>.html AND
        refreshes docs/index.html -- the two paths the workflow commits."""
        _stage_released([_finding()], restamp=True)
        with patch(
            "src.revise._call_revise_llm", return_value=self._GOOD_REPLACEMENT,
        ):
            report = revise_day(DATE, shadow=False, canonical=True)
        assert report.applied == 1
        render_stub.assert_called_once_with(DATE, mode="release")

    def test_staging_edit_renders_the_preview(
        self, tmp_data_root: Path, render_stub: Any,
    ) -> None:
        _stage(tmp_data_root, [_finding()])
        with patch(
            "src.revise._call_revise_llm", return_value=self._GOOD_REPLACEMENT,
        ):
            revise_day(DATE, shadow=False)
        render_stub.assert_called_once_with(DATE, mode="preview")

    def test_shadow_never_renders(
        self, tmp_data_root: Path, render_stub: Any,
    ) -> None:
        """Shadow writes nothing, so there is nothing for the page to catch
        up with."""
        _stage(tmp_data_root, [_finding()])
        with patch(
            "src.revise._call_revise_llm", return_value=self._GOOD_REPLACEMENT,
        ):
            revise_day(DATE, shadow=True)
        render_stub.assert_not_called()

    def test_a_cycle_that_applied_nothing_does_not_render(
        self, tmp_data_root: Path, render_stub: Any,
    ) -> None:
        _stage(tmp_data_root, [_finding()])
        with patch(
            "src.revise._call_revise_llm",
            return_value=self._REJECTED_REPLACEMENT,
        ):
            report = revise_day(DATE, shadow=False)
        assert report.rejected == 1
        render_stub.assert_not_called()

    def test_render_failure_does_not_undo_the_edit(
        self, tmp_data_root: Path, render_stub: Any,
    ) -> None:
        """The JSON write already succeeded. Reporting a failed cycle whose
        edits are on disk would be a lie; a stale page is visible and
        re-renderable in one command."""
        render_stub.side_effect = RuntimeError("template blew up")
        issue_path = _stage_released([_finding()], restamp=True)
        with patch(
            "src.revise._call_revise_llm", return_value=self._GOOD_REPLACEMENT,
        ):
            report = revise_day(DATE, shadow=False, canonical=True)
        assert report.applied == 1
        payload = json.loads(issue_path.read_text(encoding="utf-8"))
        assert payload["pulse"]["stories"][0]["summary"].endswith("under load.")
        assert report.cycle is not None
        assert "render FAILED" in report.cycle.note


# ---------------------------------------------------------------------------
# The cycle log.
# ---------------------------------------------------------------------------

class TestCycleLog:
    """``revisions.jsonl`` is an append-only history of what the engine did
    to this draft. A cycle that overwrote its predecessor would lose the
    record of the edit that actually shipped."""

    def test_second_run_appends_rather_than_overwrites(
        self, tmp_data_root: Path,
    ) -> None:
        _stage(tmp_data_root, [_finding()])
        with patch(
            "src.revise._call_revise_llm",
            return_value=_BODY_A.replace("sustained load.", "load."),
        ):
            revise_day(DATE, shadow=True)
            revise_day(DATE, shadow=True)
        lines = revisions_path(DATE).read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert [json.loads(line)["cycle"] for line in lines] == [1, 2]

    def test_refusal_is_recorded_too(self, tmp_data_root: Path) -> None:
        """"The engine ran and refused" and "the engine was never invoked"
        are different states; only the artifact can tell them apart later."""
        _stage(tmp_data_root, [_finding()], stale=True)
        revise_day(DATE, shadow=True)
        lines = revisions_path(DATE).read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert "refused" in json.loads(lines[0])["note"]


# ---------------------------------------------------------------------------
# Response cleaning.
# ---------------------------------------------------------------------------

class TestCleanReplacement:
    """Models wrap their output. Every wrapper we fail to strip becomes
    literal text in a published issue."""

    def test_strips_code_fence(self) -> None:
        cleaned = revise_mod._clean_replacement(
            "```\nThe revised sentence.\n```", "summary",
        )
        assert cleaned == "The revised sentence."

    def test_strips_leading_label(self) -> None:
        cleaned = revise_mod._clean_replacement(
            "Revised text: The revised sentence.", "summary",
        )
        assert cleaned == "The revised sentence."

    def test_strips_wrapping_quotes(self) -> None:
        cleaned = revise_mod._clean_replacement(
            '"The revised sentence."', "summary",
        )
        assert cleaned == "The revised sentence."

    def test_collapses_newlines(self) -> None:
        """Every revisable field is a single paragraph in this schema; a
        stray newline would render as one."""
        cleaned = revise_mod._clean_replacement(
            "First half\nsecond half.", "summary",
        )
        assert cleaned == "First half second half."


# ---------------------------------------------------------------------------
# Contract invariants on the revision models.
# ---------------------------------------------------------------------------

class TestRevisionContractInvariants:
    """Shapes that would make ``revisions.jsonl`` lie about what happened.
    Pydantic's type checking permits every one of them."""

    def _change(self, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "target": ReviewTarget(
                kind="story", story_id=STORY_A, field="summary",
            ),
            "before": "before text",
            "after": "after text",
            "recommendation": "do the thing",
            "status": "applied",
        }
        base.update(overrides)
        return base

    def test_rejection_must_say_why(self) -> None:
        from pydantic import ValidationError
        from src.models import RevisionChange

        with pytest.raises(ValidationError, match="reject_reason"):
            RevisionChange(**self._change(status="rejected"))

    def test_acceptance_must_not_carry_a_rejection_reason(self) -> None:
        from pydantic import ValidationError
        from src.models import RevisionChange

        with pytest.raises(ValidationError, match="must not carry"):
            RevisionChange(**self._change(reject_reason="numeral_invented"))

    def test_applied_change_must_actually_change_something(self) -> None:
        """Otherwise "applied" would include no-op writes and the trail
        would overstate what the engine did."""
        from pydantic import ValidationError
        from src.models import RevisionChange

        with pytest.raises(ValidationError, match="no-op"):
            RevisionChange(**self._change(after="before text"))

    def test_shadow_cycle_may_not_carry_an_applied_change(self) -> None:
        from pydantic import ValidationError
        from src.models import RevisionChange, RevisionCycle

        with pytest.raises(ValidationError, match="must not carry 'applied'"):
            RevisionCycle(
                date=DATE, cycle=1, mode="shadow",
                changes=[RevisionChange(**self._change())],
                generated_at=_dt.datetime(2026, 8, 2, tzinfo=_dt.timezone.utc),
                prompt_version=REVISE_PROMPT_VERSION,
            )

    def test_shadow_cycle_may_not_record_a_written_hash(self) -> None:
        from pydantic import ValidationError
        from src.models import RevisionCycle

        with pytest.raises(ValidationError, match="issue_sha256_after"):
            RevisionCycle(
                date=DATE, cycle=1, mode="shadow", changes=[],
                generated_at=_dt.datetime(2026, 8, 2, tzinfo=_dt.timezone.utc),
                prompt_version=REVISE_PROMPT_VERSION,
                issue_sha256_after="a" * 64,
            )

    def test_live_cycle_may_not_leave_a_change_merely_proposed(self) -> None:
        from pydantic import ValidationError
        from src.models import RevisionChange, RevisionCycle

        with pytest.raises(ValidationError, match="resolve every change"):
            RevisionCycle(
                date=DATE, cycle=1, mode="live",
                changes=[RevisionChange(**self._change(status="proposed"))],
                generated_at=_dt.datetime(2026, 8, 2, tzinfo=_dt.timezone.utc),
                prompt_version=REVISE_PROMPT_VERSION,
            )


# ---------------------------------------------------------------------------
# Edit distance.
# ---------------------------------------------------------------------------

class TestTokenEditDistance:
    """The containment measure itself. If it is wrong, every containment
    decision built on it is wrong."""

    @pytest.mark.parametrize(
        "before,after,expected",
        [
            ("a b c", "a b c", 0),
            ("a b c", "a b d", 1),
            ("a b c", "a b", 1),
            ("a b", "a b c", 1),
            ("", "a b", 2),
            ("a b", "", 2),
        ],
    )
    def test_distance(self, before: str, after: str, expected: int) -> None:
        assert revise_mod._token_edit_distance(
            before.split(), after.split(),
        ) == expected


# ---------------------------------------------------------------------------
# Cross-module seam: review -> revise -> re-review, for real.
# ---------------------------------------------------------------------------

class TestReviewReviseReReviewSeam:
    """Every other test in this file (and in ``test_review.py``) checks one
    stage against an artifact built BY HAND to look like the other stage's
    output. That is the right grain for a unit suite, but none of it proves
    the stages actually compose: that ``revise_day`` can read a
    ``review.json`` that ``run_review`` really wrote, and that a second
    ``run_review`` reads the ``issue.json`` that ``revise_day`` really wrote.

    This test runs all three calls back to back with real file I/O, mocking
    only the two LLM boundaries (``_call_review_llm``, ``_call_revise_llm``).
    If a field rename on either side of the seam breaks the handoff, this is
    the test that catches it -- the per-module tests above cannot, because
    each of them holds the OTHER stage's contract fixed by hand.
    """

    def test_review_flags_revise_fixes_review_clears(
        self, tmp_data_root: Path,
    ) -> None:
        issue_path = paths.staging_dir(DATE) / "issue.json"
        issue_path.parent.mkdir(parents=True, exist_ok=True)
        issue_path.write_text(
            json.dumps(_issue_payload(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        # --- 1. Real review: one mocked LLM call flags the hedge in the
        # Pulse story's closing sentence. ---------------------------------
        finding_json = {
            "target": {
                "kind": "story", "story_id": STORY_A, "field": "summary",
            },
            "criterion": "closing_shape",
            "severity": "major",
            "quote": "the team says",
            "fix_kind": "text_edit",
            "instruction": "Delete the attribution hedge 'the team says'.",
        }
        review_response = json.dumps({
            "findings": [finding_json],
            "summary": "One hedge to tighten before this ships.",
        })
        with patch("src.review._call_review_llm", return_value=review_response):
            first = review_mod.run_review(DATE)

        assert first.verdict == "amber"  # one major finding, real thresholds table
        assert first.report is not None
        first_sha = first.report.issue_sha256

        # --- 2. Real revise: reads the review.json run_review just wrote,
        # not a hand-built stand-in. One mocked LLM call applies the fix. ---
        fixed_body = _BODY_A.replace("the team says the gain", "the gain")
        with patch("src.revise._call_revise_llm", return_value=fixed_body):
            revision = revise_mod.revise_day(DATE, shadow=False)

        assert revision.applied == 1, revision.note
        assert revision.rejected == 0
        assert revision.cycle is not None
        assert revision.cycle.issue_sha256_before == first_sha, (
            "revise_day read a different issue than review.json was written "
            "against -- the review -> revise handoff is broken"
        )

        payload = json.loads(issue_path.read_text(encoding="utf-8"))
        assert payload["pulse"]["stories"][0]["summary"] == fixed_body
        assert payload["pulse"]["stories"][0]["verification"] is None

        # Between revise and the second review, the gate must see the
        # recorded review as stale: it describes text that revise just
        # replaced.
        stale_state = gate_mod.read_review_state(DATE)
        assert stale_state.issue_sha256 == first_sha
        assert stale_state.issue_sha256 != gate_mod.issue_sha256(DATE)

        # --- 3. Real re-review of the file revise actually wrote. The hedge
        # is gone, so a reviewer with nothing left to flag returns clean. ---
        clean_response = json.dumps({"findings": [], "summary": "Clean now."})
        with patch("src.review._call_review_llm", return_value=clean_response):
            second = review_mod.run_review(DATE)

        assert second.verdict == "green"
        assert second.report is not None
        assert second.report.issue_sha256 == revision.cycle.issue_sha256_after, (
            "the second review did not read the issue.json revise actually "
            "wrote -- the revise -> re-review handoff is broken"
        )
        assert second.report.issue_sha256 != first_sha

        # The gate no longer holds on staleness: the fresh review's hash
        # matches the file on disk again.
        fresh_state = gate_mod.read_review_state(DATE)
        assert fresh_state.issue_sha256 == gate_mod.issue_sha256(DATE)


# ---------------------------------------------------------------------------
# The CLI entry point.
# ---------------------------------------------------------------------------
#
# `revise_command` is the seam `.github/workflows/revise-command.yml` calls
# (`aiv revise --date ... --live --released --instruction ...`) and is not
# yet registered on `run.py`'s typer app -- see the hand-off note at the end
# of src/revise.py. Before these tests, nothing proved `--released` or
# `--target` actually reached `revise_day`, or that a bad `--date` fails
# loudly instead of silently defaulting to today.
#
# We mock `revise_day` -- the CLI's only dependency -- and assert on the
# CLI's own work: argument marshaling, the exit code, and the printed
# summary. `revise_day` itself is exercised everywhere else in this file.
# ---------------------------------------------------------------------------

class TestReviseCommandArguments:
    """Does each CLI flag reach `revise_day` as the flag it names?"""

    @staticmethod
    def _spy():
        captured: dict[str, Any] = {}

        def _fake(
            run_date, *, shadow, instruction, instruction_target, canonical,
            min_severity=revise_mod.MIN_SEVERITY_DEFAULT,
        ):
            captured.update(
                run_date=run_date, shadow=shadow, instruction=instruction,
                instruction_target=instruction_target, canonical=canonical,
                min_severity=min_severity,
            )
            return revise_mod.RevisionReport(
                date=run_date, mode="shadow" if shadow else "live", ran=True,
                applied=0, proposed=0, rejected=0,
                path=Path("/tmp/revisions.jsonl"),
            )

        return captured, _fake

    def test_released_flag_sets_canonical_true(self) -> None:
        captured, fake = self._spy()
        with patch.object(revise_mod, "revise_day", side_effect=fake):
            with pytest.raises(SystemExit):
                revise_mod.revise_command(date=DATE.isoformat(), released=True)
        assert captured["canonical"] is True

    def test_default_leaves_canonical_false(self) -> None:
        """The staging draft is what this stage is for; the released copy
        is the exception, not the default, per revise_day's own docstring."""
        captured, fake = self._spy()
        with patch.object(revise_mod, "revise_day", side_effect=fake):
            with pytest.raises(SystemExit):
                revise_mod.revise_command(date=DATE.isoformat())
        assert captured["canonical"] is False

    def test_target_reaches_revise_day_as_instruction_target(self) -> None:
        captured, fake = self._spy()
        with patch.object(revise_mod, "revise_day", side_effect=fake):
            with pytest.raises(SystemExit):
                revise_mod.revise_command(
                    date=DATE.isoformat(),
                    target="story:c_abcabcabcabc:summary",
                )
        assert captured["instruction_target"] == "story:c_abcabcabcabc:summary"

    def test_shadow_true_by_default(self) -> None:
        captured, fake = self._spy()
        with patch.object(revise_mod, "revise_day", side_effect=fake):
            with pytest.raises(SystemExit):
                revise_mod.revise_command(date=DATE.isoformat())
        assert captured["shadow"] is True

    def test_shadow_false_reaches_revise_day_live(self) -> None:
        captured, fake = self._spy()
        with patch.object(revise_mod, "revise_day", side_effect=fake):
            with pytest.raises(SystemExit):
                revise_mod.revise_command(date=DATE.isoformat(), shadow=False)
        assert captured["shadow"] is False

    def test_explicit_date_is_parsed(self) -> None:
        captured, fake = self._spy()
        with patch.object(revise_mod, "revise_day", side_effect=fake):
            with pytest.raises(SystemExit):
                revise_mod.revise_command(date="2026-01-15")
        assert captured["run_date"] == _dt.date(2026, 1, 15)

    def test_min_severity_reaches_revise_day(self) -> None:
        captured, fake = self._spy()
        with patch.object(revise_mod, "revise_day", side_effect=fake):
            with pytest.raises(SystemExit):
                revise_mod.revise_command(
                    date=DATE.isoformat(), min_severity="major",
                )
        assert captured["min_severity"] == "major"

    def test_min_severity_defaults_to_filtering_nothing(self) -> None:
        captured, fake = self._spy()
        with patch.object(revise_mod, "revise_day", side_effect=fake):
            with pytest.raises(SystemExit):
                revise_mod.revise_command(date=DATE.isoformat())
        assert captured["min_severity"] == revise_mod.MIN_SEVERITY_DEFAULT

    def test_the_workflow_invocation_marshals_every_flag(self) -> None:
        """The exact argv `.github/workflows/revise-command.yml` sends. It is
        pinned as one case because the flags only matter together: the first
        live run passed all four and the engine acted on none of them."""
        captured, fake = self._spy()
        from typer.testing import CliRunner

        from src.run import app

        with patch.object(revise_mod, "revise_day", side_effect=fake):
            result = CliRunner().invoke(app, [
                "revise", "--date", DATE.isoformat(), "--live", "--released",
                "--min-severity", "major",
                "--instruction", "tighten the close",
            ])
        assert result.exit_code == 0
        assert captured == {
            "run_date": DATE, "shadow": False, "canonical": True,
            "min_severity": "major", "instruction": "tighten the close",
            "instruction_target": "",
        }


class TestReviseCommandDateValidation:
    def test_invalid_date_exits_with_usage_error(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A typo'd --date must fail loudly, not silently fall back to
        today and revise the wrong day's draft."""
        with patch.object(revise_mod, "revise_day") as fake:
            with pytest.raises(SystemExit) as exc:
                revise_mod.revise_command(date="not-a-date")
        fake.assert_not_called()
        assert exc.value.code == 2
        assert "YYYY-MM-DD" in capsys.readouterr().out

    def test_invalid_min_severity_exits_with_usage_error(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Strict at the CLI boundary even though the engine is failure-soft:
        a typo'd floor means the operator gets MORE edits than they asked
        for, and an unattended --live run would rewrite fields they meant to
        leave alone."""
        with patch.object(revise_mod, "revise_day") as fake:
            with pytest.raises(SystemExit) as exc:
                revise_mod.revise_command(
                    date=DATE.isoformat(), min_severity="urgent",
                )
        fake.assert_not_called()
        assert exc.value.code == 2
        assert "note|minor|major|blocking" in capsys.readouterr().out


class TestReviseCommandOutput:
    """The printed summary is what an operator (or the /revise workflow's
    PR-comment reply) reads -- it must name every change and why a rejected
    one was refused."""

    def test_reports_applied_and_rejected_changes(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        from src.models import RevisionChange, RevisionCycle

        cycle = RevisionCycle(
            date=DATE, cycle=1, mode="live",
            changes=[
                RevisionChange(
                    target=ReviewTarget(
                        kind="story", story_id=STORY_A, field="headline",
                    ),
                    before="old headline", after="new headline",
                    recommendation="tighten it", status="applied",
                ),
                RevisionChange(
                    target=ReviewTarget(
                        kind="section", section="big_picture", field="intro_lead",
                    ),
                    before="Trust, but verify.", after="Trust, but verify.",
                    recommendation="cut the cliche", status="rejected",
                    reject_reason="edit_distance_exceeded",
                ),
            ],
            generated_at=_dt.datetime(2026, 8, 2, tzinfo=_dt.timezone.utc),
            prompt_version=REVISE_PROMPT_VERSION,
        )
        report = revise_mod.RevisionReport(
            date=DATE, mode="live", ran=True, applied=1, proposed=0,
            rejected=1, path=Path("/tmp/revisions.jsonl"), cycle=cycle,
        )
        with patch.object(revise_mod, "revise_day", return_value=report):
            with pytest.raises(SystemExit) as exc:
                revise_mod.revise_command(date=DATE.isoformat(), shadow=False)
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert f"[APPLIED ] story:{STORY_A}:headline" in out
        assert (
            "[REJECTED] section:big_picture:intro_lead (edit_distance_exceeded)"
            in out
        )
