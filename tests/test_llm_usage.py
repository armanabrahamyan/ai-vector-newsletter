"""
tests/test_llm_usage.py -- unit tests for src/llm_usage.py.

Covers: accumulation math, prefix-pricing lookup, unknown-model -> cost
None, stage tagging (including the "unknown" default), and reset().
Subject to test-engineer review per tests/CONVENTIONS.md.
"""

from __future__ import annotations

import pytest

from src import llm_usage


@pytest.fixture(autouse=True)
def _reset_accumulator():
    """Every test starts from a clean accumulator and leaves one behind --
    this module is process-global state, and CONVENTIONS.md's "never let a
    test bleed into another" bar applies to module singletons too."""
    llm_usage.reset()
    yield
    llm_usage.reset()


class TestRecordAccumulation:
    def test_single_call_recorded_under_active_stage(self):
        llm_usage.set_stage("rank")
        llm_usage.record("claude-sonnet-4-6", 1000, 200)
        snap = llm_usage.snapshot()
        assert snap["stages"]["rank"]["input_tokens"] == 1000
        assert snap["stages"]["rank"]["output_tokens"] == 200

    def test_multiple_calls_in_same_stage_sum(self):
        llm_usage.set_stage("summarise")
        llm_usage.record("claude-sonnet-4-6", 1000, 100)
        llm_usage.record("claude-sonnet-4-6", 2000, 300)
        snap = llm_usage.snapshot()
        assert snap["stages"]["summarise"]["input_tokens"] == 3000
        assert snap["stages"]["summarise"]["output_tokens"] == 400

    def test_calls_in_different_stages_stay_separate(self):
        llm_usage.set_stage("rank")
        llm_usage.record("claude-sonnet-4-6", 1000, 100)
        llm_usage.set_stage("verify")
        llm_usage.record("claude-sonnet-4-6", 500, 50)
        snap = llm_usage.snapshot()
        assert snap["stages"]["rank"]["input_tokens"] == 1000
        assert snap["stages"]["verify"]["input_tokens"] == 500

    def test_total_sums_across_all_stages(self):
        llm_usage.set_stage("rank")
        llm_usage.record("claude-sonnet-4-6", 1000, 100)
        llm_usage.set_stage("verify")
        llm_usage.record("claude-sonnet-4-6", 500, 50)
        snap = llm_usage.snapshot()
        assert snap["total"]["input_tokens"] == 1500
        assert snap["total"]["output_tokens"] == 150


class TestStageTagging:
    def test_record_before_set_stage_tags_unknown(self):
        llm_usage.record("claude-sonnet-4-6", 100, 10)
        snap = llm_usage.snapshot()
        assert "unknown" in snap["stages"]
        assert snap["stages"]["unknown"]["input_tokens"] == 100

    def test_set_stage_with_empty_string_falls_back_to_unknown(self):
        llm_usage.set_stage("")
        llm_usage.record("claude-sonnet-4-6", 50, 5)
        snap = llm_usage.snapshot()
        assert "unknown" in snap["stages"]


class TestPricingLookup:
    def test_known_model_prefix_computes_cost(self):
        llm_usage.set_stage("rank")
        # 1,000,000 input tokens @ $3.00/mtok = $3.00 exactly.
        llm_usage.record("claude-sonnet-4-6", 1_000_000, 0)
        snap = llm_usage.snapshot()
        assert snap["stages"]["rank"]["cost_usd"] == pytest.approx(3.00)

    def test_dated_model_id_resolves_via_prefix_match(self):
        llm_usage.set_stage("rank")
        llm_usage.record("claude-sonnet-4-6-20260115", 1_000_000, 0)
        snap = llm_usage.snapshot()
        assert snap["stages"]["rank"]["cost_usd"] == pytest.approx(3.00)

    def test_input_and_output_rates_both_applied(self):
        llm_usage.set_stage("summarise")
        # 500k input @ $3.00/mtok = $1.50; 200k output @ $15.00/mtok = $3.00.
        llm_usage.record("claude-sonnet-4-6", 500_000, 200_000)
        snap = llm_usage.snapshot()
        assert snap["stages"]["summarise"]["cost_usd"] == pytest.approx(4.50)

    def test_unknown_model_yields_none_cost_not_a_guess(self):
        llm_usage.set_stage("rank")
        llm_usage.record("some-future-model-nobody-priced-yet", 1000, 100)
        snap = llm_usage.snapshot()
        assert snap["stages"]["rank"]["cost_usd"] is None

    def test_unknown_model_still_reports_tokens(self):
        llm_usage.set_stage("rank")
        llm_usage.record("some-future-model-nobody-priced-yet", 1000, 100)
        snap = llm_usage.snapshot()
        assert snap["stages"]["rank"]["input_tokens"] == 1000
        assert snap["stages"]["rank"]["output_tokens"] == 100

    def test_one_unknown_model_in_stage_makes_stage_cost_none(self):
        """Mixed known + unknown models in one stage: we don't silently
        under-report by pricing only the known slice."""
        llm_usage.set_stage("rank")
        llm_usage.record("claude-sonnet-4-6", 1_000_000, 0)
        llm_usage.record("some-unpriced-model", 1_000_000, 0)
        snap = llm_usage.snapshot()
        assert snap["stages"]["rank"]["cost_usd"] is None

    def test_one_unknown_stage_makes_total_cost_none(self):
        llm_usage.set_stage("rank")
        llm_usage.record("claude-sonnet-4-6", 1_000_000, 0)
        llm_usage.set_stage("verify")
        llm_usage.record("some-unpriced-model", 1_000_000, 0)
        snap = llm_usage.snapshot()
        assert snap["stages"]["rank"]["cost_usd"] is not None
        assert snap["total"]["cost_usd"] is None


class TestCacheTokenAccounting:
    """Prompt-caching fields (2026-08-08, rank cache split). The Anthropic
    usage object splits billed input into `input_tokens` (after the last
    cache breakpoint), `cache_creation_input_tokens` (5-min writes, 1.25x
    base input) and `cache_read_input_tokens` (hits, 0.10x base input).
    Without recording all three, metrics_log.jsonl silently misreports the
    cost of every cached rank call."""

    def test_cache_fields_absent_default_to_zero(self):
        """Old call sites (bedrock/openai paths, summarise et al.) omit the
        cache kwargs -- schema stays backward-compatible, absent = 0."""
        llm_usage.set_stage("rank")
        llm_usage.record("claude-sonnet-4-6", 1000, 200)
        snap = llm_usage.snapshot()
        assert snap["stages"]["rank"]["cache_creation_input_tokens"] == 0
        assert snap["stages"]["rank"]["cache_read_input_tokens"] == 0

    def test_cache_tokens_accumulate(self):
        llm_usage.set_stage("rank")
        llm_usage.record(
            "claude-sonnet-4-6", 100, 10,
            cache_creation_input_tokens=3084, cache_read_input_tokens=0,
        )
        llm_usage.record(
            "claude-sonnet-4-6", 100, 10,
            cache_creation_input_tokens=0, cache_read_input_tokens=3084,
        )
        snap = llm_usage.snapshot()
        assert snap["stages"]["rank"]["cache_creation_input_tokens"] == 3084
        assert snap["stages"]["rank"]["cache_read_input_tokens"] == 3084
        assert snap["total"]["cache_creation_input_tokens"] == 3084
        assert snap["total"]["cache_read_input_tokens"] == 3084

    def test_cache_write_priced_at_1_25x_base_input(self):
        """1M cache-write tokens on sonnet-4-6 = $3.75 (1.25 x $3.00).
        Fetched 2026-08-08 from the Anthropic pricing table."""
        llm_usage.set_stage("rank")
        llm_usage.record(
            "claude-sonnet-4-6", 0, 0, cache_creation_input_tokens=1_000_000,
        )
        snap = llm_usage.snapshot()
        assert snap["stages"]["rank"]["cost_usd"] == pytest.approx(3.75)

    def test_cache_read_priced_at_0_10x_base_input(self):
        """1M cache-read tokens on sonnet-4-6 = $0.30 (0.10 x $3.00)."""
        llm_usage.set_stage("rank")
        llm_usage.record(
            "claude-sonnet-4-6", 0, 0, cache_read_input_tokens=1_000_000,
        )
        snap = llm_usage.snapshot()
        assert snap["stages"]["rank"]["cost_usd"] == pytest.approx(0.30)

    def test_all_four_components_summed(self):
        """A realistic cached rank call: 500 fresh input, 100 output,
        3084 read from cache. Cost = 500*3 + 100*15 + 3084*0.30 (per MTok)."""
        llm_usage.set_stage("rank")
        llm_usage.record(
            "claude-sonnet-4-6", 500, 100, cache_read_input_tokens=3084,
        )
        expected = (500 * 3.00 + 100 * 15.00 + 3084 * 0.30) / 1_000_000
        snap = llm_usage.snapshot()
        assert snap["stages"]["rank"]["cost_usd"] == pytest.approx(
            round(expected, 4)
        )

    def test_unknown_model_with_cache_tokens_still_reports_none_cost(self):
        llm_usage.set_stage("rank")
        llm_usage.record(
            "some-unpriced-model", 100, 10, cache_read_input_tokens=1000,
        )
        snap = llm_usage.snapshot()
        assert snap["stages"]["rank"]["cost_usd"] is None
        assert snap["stages"]["rank"]["cache_read_input_tokens"] == 1000


class TestReset:
    def test_reset_clears_accumulated_usage(self):
        llm_usage.set_stage("rank")
        llm_usage.record("claude-sonnet-4-6", 1000, 100)
        llm_usage.reset()
        snap = llm_usage.snapshot()
        assert snap["stages"] == {}
        assert snap["total"]["input_tokens"] == 0

    def test_reset_clears_the_active_stage_tag(self):
        llm_usage.set_stage("rank")
        llm_usage.reset()
        llm_usage.record("claude-sonnet-4-6", 100, 10)
        snap = llm_usage.snapshot()
        assert "unknown" in snap["stages"]
        assert "rank" not in snap["stages"]


class TestFormatSummaryLine:
    def test_no_usage_returns_none(self):
        snap = llm_usage.snapshot()
        assert llm_usage.format_summary_line(snap) is None

    def test_line_includes_each_stage_and_total(self):
        llm_usage.set_stage("rank")
        llm_usage.record("claude-sonnet-4-6", 25_100, 3_200)
        llm_usage.set_stage("summarise")
        llm_usage.record("claude-sonnet-4-6", 88_000, 12_000)
        line = llm_usage.format_summary_line(stage_order=("rank", "summarise"))
        assert line is not None
        assert line.startswith("LLM usage: ")
        assert "rank" in line
        assert "summarise" in line
        assert "TOTAL" in line

    def test_stage_order_controls_left_to_right_ordering(self):
        llm_usage.set_stage("verify")
        llm_usage.record("claude-sonnet-4-6", 100, 10)
        llm_usage.set_stage("rank")
        llm_usage.record("claude-sonnet-4-6", 100, 10)
        line = llm_usage.format_summary_line(
            stage_order=("rank", "summarise", "verify", "review")
        )
        assert line.index("rank") < line.index("verify")

    def test_unknown_model_shows_cost_unknown_not_a_number(self):
        llm_usage.set_stage("rank")
        llm_usage.record("some-unpriced-model", 1000, 100)
        line = llm_usage.format_summary_line(stage_order=("rank",))
        assert "cost unknown" in line
