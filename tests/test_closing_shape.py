"""
tests/test_closing_shape.py -- the code-side closing-shape check
(src/closing_shape.py) and the bounded one-sentence rewrite in
summarise (v0.23.1, 2026-08-29).

Detector cases are the sentences the reviewer actually quoted in August
release PRs, so each positive is a defect that shipped and each negative
is a close the contract calls in-voice.
"""

from __future__ import annotations

import pytest

from src.closing_shape import (
    closing_shape_defect,
    extract_closing_sentence,
    opens_on_imperative,
    replace_closing_sentence,
)


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

_BODY_PREFIX = (
    "TH-GNN pairs a hybrid text-graph encoder with a review-burst detector "
    "and reports F1 0.87 across five attack types. "
)


class TestImperativeOpener:
    @pytest.mark.parametrize("sentence", [
        "Raise it at your next fraud-detection architecture review.",
        "Bring this framing to your next quoting-strategy design review.",
        "Run your own site through journey.ora.ai before your next agent "
        "deployment.",
        # main clause after a semicolon
        "Benchmark and dataset are public; run it before shipping "
        "agent-written code to production.",
        # leading subordinate clause, then the instruction
        "Before your next eval cycle, install it and diff the scores.",
        "If these findings hold, expect the timelines to move.",
        "Treat likes as a sentiment signal, downloads as your "
        "infrastructure map, before your next model-selection decision.",
        "Map your agents' write-tier tools before requesting access.",
        # ambiguous verb + object opener
        "Test it against your own failure cases first.",
        "Wire Playwright against Kitesurf in your next browser-use build.",
    ])
    def test_positive(self, sentence: str) -> None:
        assert opens_on_imperative(sentence)

    @pytest.mark.parametrize("sentence", [
        # presence-form maturity signals
        "TH-GNN is an arXiv preprint with no production deployment "
        "reported; the F1 0.87 result covers five attack types on "
        "benchmark data only.",
        "The paper is published in Risk.net Cutting Edge and is available "
        "to practitioners now.",
        "Code is public and gains are peer-review-pending.",
        # anchored second person is ratified voice, not an instruction
        "Which team in your org owns the threat model update when the "
        "attacker is fully autonomous?",
        "The reviewer role still exists on paper; does yours?",
        # ambiguous verbs used as nouns
        "Test results show the gap widening at 3-bit quantisation.",
        "Budget constraints, not model choice, set the ceiling.",
        "Plan B is a smaller model with a tighter retrieval stack.",
        "Benchmark scores held steady while decision margins collapsed.",
        # direction statements
        "The correctness-security gap is now measured, public, and "
        "reproducible.",
        "",
    ])
    def test_negative(self, sentence: str) -> None:
        assert not opens_on_imperative(sentence)


class TestClosingShapeDefect:
    @pytest.mark.parametrize("close", [
        "Raise it at your next fraud-detection architecture review.",
        "Bring this framing to your next quoting-strategy design review.",
        "Run your own site through journey.ora.ai before your next agent "
        "deployment.",
    ])
    def test_currents_instruction_is_a_defect(self, close: str) -> None:
        assert closing_shape_defect("currents", _BODY_PREFIX + close)

    def test_currents_maturity_signal_passes(self) -> None:
        body = _BODY_PREFIX + (
            "TH-GNN is an arXiv preprint with no production deployment "
            "reported; the F1 figure covers benchmark data only."
        )
        assert closing_shape_defect("currents", body) is None

    @pytest.mark.parametrize("close", [
        "Benchmark and dataset are public; run it before shipping "
        "agent-written code to production.",
        "Treat likes as a sentiment signal, downloads as your "
        "infrastructure map, before your next model-selection decision.",
        "Who owns the containment failure when the agent reaches the "
        "live internet?",
    ])
    def test_pulse_question_or_instruction_is_a_defect(self, close: str) -> None:
        assert closing_shape_defect("pulse", _BODY_PREFIX + close)

    def test_pulse_direction_statement_passes(self) -> None:
        body = _BODY_PREFIX + (
            "The correctness-security gap is now measured, public, and "
            "reproducible."
        )
        assert closing_shape_defect("pulse", body) is None

    @pytest.mark.parametrize("close", [
        "If these findings hold, recursive self-improvement timelines "
        "need revisiting.",
        "Map your agents' write-tier tools before requesting access.",
        "When your eval environment permits internet access, what "
        "containment failure looks like is now documented.",
    ])
    def test_big_picture_without_question_is_a_defect(self, close: str) -> None:
        assert closing_shape_defect("big_picture", _BODY_PREFIX + close)

    def test_big_picture_anchored_question_passes(self) -> None:
        body = _BODY_PREFIX + (
            "Which team in your org owns the threat model update when the "
            "attacker is fully autonomous and parallel?"
        )
        assert closing_shape_defect("big_picture", body) is None

    def test_hands_on_is_not_checked(self) -> None:
        """The imperative IS the Hands-On contract; a question there is
        left to the reviewer rather than guessed at here."""
        assert closing_shape_defect(
            "hands_on", _BODY_PREFIX + "Is this ready for your stack?",
        ) is None

    def test_empty_body_and_unknown_section_are_silent(self) -> None:
        assert closing_shape_defect("currents", "") is None
        assert closing_shape_defect("mystery", _BODY_PREFIX + "Run it.") is None


class TestSentenceHelpers:
    def test_replace_swaps_only_the_last_sentence(self) -> None:
        body = "First fact. Second fact! Run it before you ship."
        out = replace_closing_sentence(body, "The harness is public today.")
        assert out == "First fact. Second fact! The harness is public today."
        assert extract_closing_sentence(out) == "The harness is public today."

    def test_replace_on_single_sentence_body(self) -> None:
        assert replace_closing_sentence("Run it.", "It exists.") == "It exists."


# ---------------------------------------------------------------------------
# The bounded rewrite inside summarise
# ---------------------------------------------------------------------------

def _draft(summary: str, take: str | None = "Fake review detection now has a public benchmark."):
    from src.summarise import _SummaryDraft
    return _SummaryDraft(
        headline="A hybrid detector catches AI-generated fake reviews",
        summary=summary, signal="watch", take=take,
    )


_OFF_SHAPE_BODY = _BODY_PREFIX + (
    "Raise it at your next fraud-detection architecture review."
)
_GOOD_CLOSE = (
    "TH-GNN is an arXiv preprint with no production deployment reported."
)


class TestRepairClosingShape:
    def test_off_shape_close_is_rewritten_and_spliced(self, monkeypatch) -> None:
        from src import summarise as summarise_mod
        seen: dict[str, str] = {}

        def fake_llm(prompt, **_kw):
            seen["prompt"] = prompt
            return f'"{_GOOD_CLOSE}"\n'

        monkeypatch.setattr(summarise_mod, "_llm_call", fake_llm)
        out = summarise_mod._repair_closing_shape(
            _draft(_OFF_SHAPE_BODY), "currents", "c_x", 0.6,
        )
        assert out.summary == _BODY_PREFIX + _GOOD_CLOSE
        # Only the close changed; the rest of the draft is intact.
        assert out.take == _draft(_OFF_SHAPE_BODY).take
        # The prompt hands the model the contract, the offender, the take.
        assert "PRESENCE-FORM MATURITY SIGNAL" in seen["prompt"]
        assert "Raise it at your next" in seen["prompt"]
        assert "do NOT restate it" in seen["prompt"]

    def test_in_shape_close_costs_no_call(self, monkeypatch) -> None:
        from src import summarise as summarise_mod

        def fail_llm(*_a, **_k):  # pragma: no cover
            raise AssertionError("no rewrite call expected for a good close")

        monkeypatch.setattr(summarise_mod, "_llm_call", fail_llm)
        draft = _draft(_BODY_PREFIX + _GOOD_CLOSE)
        assert summarise_mod._repair_closing_shape(
            draft, "currents", "c_x", 0.6,
        ) is draft

    def test_two_misses_keep_the_original_body(self, monkeypatch) -> None:
        """Bounded: two off-shape rewrites and the ORIGINAL ships (the
        reviewer flags it), never a third call."""
        from src import summarise as summarise_mod
        calls: list[str] = []

        def still_bad(prompt, **_kw):
            calls.append(prompt)
            return "Bring it to your next design review."

        monkeypatch.setattr(summarise_mod, "_llm_call", still_bad)
        draft = _draft(_OFF_SHAPE_BODY)
        out = summarise_mod._repair_closing_shape(draft, "currents", "c_x", 0.6)
        assert out.summary == _OFF_SHAPE_BODY
        assert len(calls) == 2

    def test_rewrite_that_breaks_the_word_cap_is_rejected(self, monkeypatch) -> None:
        from src import summarise as summarise_mod
        long_body = " ".join(["word"] * 55) + " Run it before you ship."
        monkeypatch.setattr(
            summarise_mod, "_llm_call",
            lambda *_a, **_k: " ".join(["signal"] * 30) + " exists today.",
        )
        out = summarise_mod._repair_closing_shape(
            _draft(long_body), "currents", "c_x", 0.6,
        )
        assert out.summary == long_body

    def test_multi_line_reply_is_not_spliced(self, monkeypatch) -> None:
        from src import summarise as summarise_mod
        monkeypatch.setattr(
            summarise_mod, "_llm_call",
            lambda *_a, **_k: "Here is a rewrite:\n" + _GOOD_CLOSE,
        )
        out = summarise_mod._repair_closing_shape(
            _draft(_OFF_SHAPE_BODY), "currents", "c_x", 0.6,
        )
        assert out.summary == _OFF_SHAPE_BODY

    def test_kill_switch_skips_the_pass(self, monkeypatch) -> None:
        from src import summarise as summarise_mod
        monkeypatch.setenv("AIV_CLOSING_SHAPE_REPAIR", "0")

        def fail_llm(*_a, **_k):  # pragma: no cover
            raise AssertionError("repair disabled; no call expected")

        monkeypatch.setattr(summarise_mod, "_llm_call", fail_llm)
        draft = _draft(_OFF_SHAPE_BODY)
        assert summarise_mod._repair_closing_shape(
            draft, "currents", "c_x", 0.6,
        ) is draft

    def test_big_picture_repair_lands_on_a_question(self, monkeypatch) -> None:
        from src import summarise as summarise_mod
        body = _BODY_PREFIX + (
            "Map your agents' write-tier tools before requesting access."
        )
        monkeypatch.setattr(
            summarise_mod, "_llm_call",
            lambda *_a, **_k: (
                "When your next agent touches write-tier tools, does your "
                "governance layer intercept at the call level?"
            ),
        )
        out = summarise_mod._repair_closing_shape(
            _draft(body), "big_picture", "c_x", 0.6,
        )
        assert out.summary.endswith("at the call level?")
        assert closing_shape_defect("big_picture", out.summary) is None


class TestRepairWiredIntoSummariseOne:
    """The repair runs on the accepted draft before the block is built,
    so the feed-forward close and the persisted summary both carry the
    repaired sentence."""

    def test_currents_story_ships_repaired_close(self, monkeypatch) -> None:
        from src import summarise as summarise_mod
        from tests.test_summarise import TestPriorClosesInPrompt
        story, cluster, item = (
            TestPriorClosesInPrompt._make_story_cluster_item("currents")
        )
        monkeypatch.setattr(
            summarise_mod, "_call_and_parse_summary",
            lambda *_a, **_k: _draft(_OFF_SHAPE_BODY),
        )
        monkeypatch.setattr(summarise_mod, "_fetch_source_excerpt", lambda url: "")
        monkeypatch.setattr(
            summarise_mod, "_llm_call", lambda *_a, **_k: _GOOD_CLOSE,
        )
        block = summarise_mod._summarise_one(
            story=story, cluster=cluster, items=[item], callbacks=[],
        )
        assert block is not None
        assert block.summary.endswith(_GOOD_CLOSE)
