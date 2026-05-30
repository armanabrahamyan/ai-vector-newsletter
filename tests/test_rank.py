"""Unit tests for src/rank.py -- targeted regression coverage.

Scope: the OpenAI-compatible LLM transport (``_llm_call_openai_compatible``).
This is the path used for OpenAI, LiteLLM, Ollama, vLLM, Together, Groq,
and any other OpenAI-API-compatible endpoint -- a lot of surface area that
isn't otherwise exercised in the test suite.

We mock ``httpx.post`` (the external boundary) and assert on the unit's
own transformations: URL composition, header shape, payload shape, response
parsing. We do NOT mock the function under test (CONVENTIONS sec. 3).

The Anthropic + Bedrock branches use vendor SDKs whose contracts are
better covered by the vendor's own test suite + the integration eval; we
deliberately don't pin them here.

Additionally pins the deterministic post-LLM prior-coverage penalty (#81)
in ``TestPriorCoveragePenalty`` -- the rule that caps
``breakdown["significance"]`` at 50 for any cluster carrying a
``prior_coverage_ref`` so a topical recurrence of yesterday's story can't
crowd fresh items out of high-scoring slots.
"""
from __future__ import annotations

import datetime as _dt
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.models import Cluster, Item
from src.rank import (
    _DEFAULT_TIER_THRESHOLDS,
    _FRESHNESS_INFERRED_CAP,
    _PRIOR_COVERAGE_NOVELTY_CAPS,
    _PRIOR_COVERAGE_SIGNIFICANCE_CAP,
    _ParsedScore,
    _apply_freshness_inferred_penalty,
    _apply_prior_coverage_penalty,
    _assign_initial_tier,
    _build_rank_prompt,
    _llm_call_openai_compatible,
    _lookup_prior_coverage,
    _rank_one,
    _weighted_score,
    rank,
)
from tests.conftest import FIXED_EARLIER, FIXED_NOW


def _ok_response(content: str = "ranked output") -> MagicMock:
    """Minimal fake of httpx.Response shaped like an OpenAI chat completion."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock(return_value=None)
    resp.json = MagicMock(return_value={
        "choices": [{"message": {"content": content}}],
    })
    return resp


class TestUrlComposition:
    """The unit appends ``/chat/completions`` to ``LLM_ENDPOINT``. Pinned
    because every OpenAI-compatible vendor uses this exact suffix; a typo
    here means zero providers work."""

    def test_appends_chat_completions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_ENDPOINT", "https://api.openai.com/v1")
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        with patch("httpx.post", return_value=_ok_response()) as mock_post:
            _llm_call_openai_compatible(
                "prompt", model="gpt-4", temperature=0.5,
                max_tokens=100, timeout=30.0,
            )
        url = mock_post.call_args.args[0]
        assert url == "https://api.openai.com/v1/chat/completions"

    def test_strips_trailing_slash_on_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A user-supplied LLM_ENDPOINT with a trailing slash must not yield
        a double-slash URL (some proxies 404 on that)."""
        monkeypatch.setenv("LLM_ENDPOINT", "https://api.openai.com/v1/")
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        with patch("httpx.post", return_value=_ok_response()) as mock_post:
            _llm_call_openai_compatible(
                "prompt", model="gpt-4", temperature=0.5,
                max_tokens=100, timeout=30.0,
            )
        url = mock_post.call_args.args[0]
        assert "//" not in url.replace("https://", "")

    def test_raises_when_endpoint_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without LLM_ENDPOINT the call must fail loud rather than hit
        some default. There is no sane default for "OpenAI-compatible"."""
        monkeypatch.delenv("LLM_ENDPOINT", raising=False)
        with pytest.raises(RuntimeError, match="LLM_ENDPOINT"):
            _llm_call_openai_compatible(
                "prompt", model="gpt-4", temperature=0.5,
                max_tokens=100, timeout=30.0,
            )


class TestAuthHeader:
    def test_authorization_header_set_when_api_key_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Bearer token format is what every OpenAI-compatible vendor
        expects -- pin it so a refactor can't drop the prefix."""
        monkeypatch.setenv("LLM_ENDPOINT", "https://api.openai.com/v1")
        monkeypatch.setenv("LLM_API_KEY", "sk-test-1234")
        with patch("httpx.post", return_value=_ok_response()) as mock_post:
            _llm_call_openai_compatible(
                "prompt", model="gpt-4", temperature=0.5,
                max_tokens=100, timeout=30.0,
            )
        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-test-1234"

    def test_no_authorization_header_when_api_key_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Local Ollama / vLLM endpoints often have no auth. The Authorization
        header must be OMITTED rather than sent as 'Bearer ' (some servers
        reject the empty-bearer)."""
        monkeypatch.setenv("LLM_ENDPOINT", "http://localhost:11434/v1")
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        with patch("httpx.post", return_value=_ok_response()) as mock_post:
            _llm_call_openai_compatible(
                "prompt", model="llama3", temperature=0.5,
                max_tokens=100, timeout=30.0,
            )
        headers = mock_post.call_args.kwargs["headers"]
        assert "Authorization" not in headers

    def test_content_type_header_always_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_ENDPOINT", "https://api.openai.com/v1")
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        with patch("httpx.post", return_value=_ok_response()) as mock_post:
            _llm_call_openai_compatible(
                "prompt", model="gpt-4", temperature=0.5,
                max_tokens=100, timeout=30.0,
            )
        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Content-Type"] == "application/json"


class TestRequestPayload:
    def test_payload_carries_model_and_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_ENDPOINT", "https://api.openai.com/v1")
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        with patch("httpx.post", return_value=_ok_response()) as mock_post:
            _llm_call_openai_compatible(
                "rank these clusters", model="gpt-4-turbo",
                temperature=0.3, max_tokens=512, timeout=30.0,
            )
        body = mock_post.call_args.kwargs["json"]
        assert body["model"] == "gpt-4-turbo"
        assert body["messages"] == [{"role": "user", "content": "rank these clusters"}]

    def test_payload_propagates_temperature_and_max_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_ENDPOINT", "https://api.openai.com/v1")
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        with patch("httpx.post", return_value=_ok_response()) as mock_post:
            _llm_call_openai_compatible(
                "p", model="gpt-4", temperature=0.42,
                max_tokens=777, timeout=30.0,
            )
        body = mock_post.call_args.kwargs["json"]
        assert body["temperature"] == 0.42
        assert body["max_tokens"] == 777

    def test_timeout_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The caller's timeout must reach httpx -- otherwise long-running
        local models would hang the pipeline."""
        monkeypatch.setenv("LLM_ENDPOINT", "https://api.openai.com/v1")
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        with patch("httpx.post", return_value=_ok_response()) as mock_post:
            _llm_call_openai_compatible(
                "p", model="gpt-4", temperature=0.5,
                max_tokens=100, timeout=99.0,
            )
        assert mock_post.call_args.kwargs["timeout"] == 99.0


class TestResponseParsing:
    def test_extracts_content_from_first_choice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The OpenAI shape is choices[0].message.content -- pin the path."""
        monkeypatch.setenv("LLM_ENDPOINT", "https://api.openai.com/v1")
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        with patch("httpx.post", return_value=_ok_response("the response text")):
            out = _llm_call_openai_compatible(
                "p", model="gpt-4", temperature=0.5,
                max_tokens=100, timeout=30.0,
            )
        assert out == "the response text"

    def test_raises_on_empty_choices(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A response with no choices is unrecoverable -- must surface as
        an error, not return empty string and pollute downstream stages."""
        monkeypatch.setenv("LLM_ENDPOINT", "https://api.openai.com/v1")
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        empty_resp = MagicMock()
        empty_resp.raise_for_status = MagicMock(return_value=None)
        empty_resp.json = MagicMock(return_value={"choices": []})
        with patch("httpx.post", return_value=empty_resp):
            with pytest.raises(RuntimeError, match="no choices"):
                _llm_call_openai_compatible(
                    "p", model="gpt-4", temperature=0.5,
                    max_tokens=100, timeout=30.0,
                )

    def test_missing_content_returns_empty_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Some OpenAI-compat servers return a choice with no `message.content`
        on a successful but empty completion. The unit must not crash --
        the caller's JSON-retry budget handles the downstream error."""
        monkeypatch.setenv("LLM_ENDPOINT", "https://api.openai.com/v1")
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        weird_resp = MagicMock()
        weird_resp.raise_for_status = MagicMock(return_value=None)
        weird_resp.json = MagicMock(return_value={
            "choices": [{"message": {}}],  # no `content`
        })
        with patch("httpx.post", return_value=weird_resp):
            out = _llm_call_openai_compatible(
                "p", model="gpt-4", temperature=0.5,
                max_tokens=100, timeout=30.0,
            )
        assert out == ""

    def test_http_error_propagated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """raise_for_status's exception must propagate -- the unit must not
        swallow a 401 / 500 and return an empty string."""
        monkeypatch.setenv("LLM_ENDPOINT", "https://api.openai.com/v1")
        monkeypatch.setenv("LLM_API_KEY", "sk-test")

        class _HTTPError(Exception):
            pass

        bad_resp = MagicMock()
        bad_resp.raise_for_status = MagicMock(side_effect=_HTTPError("401 Unauthorized"))
        with patch("httpx.post", return_value=bad_resp):
            with pytest.raises(_HTTPError):
                _llm_call_openai_compatible(
                    "p", model="gpt-4", temperature=0.5,
                    max_tokens=100, timeout=30.0,
                )


# ===========================================================================
# Prior-coverage penalty (#81) -- post-LLM deterministic downweighting.
#
# A cluster with prior coverage (Cluster.prior_coverage_ref is not None) is
# a topical recurrence of something we covered on a previous day. Allowing
# the LLM to score it 65+ on significance crowds genuinely-new stories out
# of high slots. The penalty caps breakdown["significance"] at 50 (rubric
# anchor 50 = "single signal-filter dimension hit"); the caller recomputes
# score via _weighted_score.
#
# Anchor case: c_2e53967d020fb800 on 2026-05-25 -- llama.cpp how-to
# follow-up scored 44 with significance=65, became Pulse by default.
# Penalty drops significance 65->50, score 44->40.
# ===========================================================================

def _parsed(significance: int) -> _ParsedScore:
    return _ParsedScore(
        breakdown={
            "significance": significance,
            "hands_on_utility": 75,
            "big_picture_relevance": 0,
            "financial_services_impact": 15,
            "freshness_momentum": 40,
        },
        audience_tags=["hands_on"],
        rationale="test",
    )


def _cluster(cluster_id: str, *, prior_coverage_ref: str | None) -> Cluster:
    return Cluster(
        cluster_id=cluster_id,
        item_ids=["i1"],
        canonical_title="t",
        sources=["src_a"],
        earliest_published=FIXED_EARLIER,
        size=1,
        prior_coverage_ref=prior_coverage_ref,
    )


class TestPriorCoveragePenalty:
    """The deterministic post-LLM prior-coverage penalty -- the safer alternative
    to a prompt change for this rule (see #75/#77 cliff)."""

    def test_no_change_when_prior_coverage_ref_is_none(self) -> None:
        """Fresh stories must not be touched. Most stories are fresh; this is
        the common path -- a bug here would be a global regression."""
        parsed = _parsed(significance=80)
        cluster = _cluster("c_" + "1" * 14, prior_coverage_ref=None)
        _apply_prior_coverage_penalty(parsed, cluster)
        assert parsed.breakdown["significance"] == 80

    def test_caps_significance_when_prior_coverage(self) -> None:
        """The smoking-gun anchor: prior-coverage story with significance=65
        must drop to the cap. Mirrors c_2e53967d020fb800 / 2026-05-25."""
        parsed = _parsed(significance=65)
        cluster = _cluster(
            "c_" + "2" * 14, prior_coverage_ref="c_" + "f" * 14,
        )
        _apply_prior_coverage_penalty(parsed, cluster)
        assert parsed.breakdown["significance"] == _PRIOR_COVERAGE_SIGNIFICANCE_CAP
        assert _PRIOR_COVERAGE_SIGNIFICANCE_CAP == 50

    def test_score_recomputes_correctly_after_cap(self) -> None:
        """The pydantic invariant `score == weighted_sum(breakdown)` must
        still hold after the penalty mutates breakdown. We recompute via
        the same _weighted_score helper RankedStory uses."""
        parsed = _parsed(significance=65)
        cluster = _cluster(
            "c_" + "3" * 14, prior_coverage_ref="c_" + "f" * 14,
        )
        # Anchor expected (v0.6 weights 40/10/30/15/5):
        # 0.40*65 + 0.10*75 + 0.30*0 + 0.15*15 + 0.05*40 = 37.75 -> 38
        score_before = _weighted_score(parsed.breakdown)
        assert score_before == 38

        _apply_prior_coverage_penalty(parsed, cluster)
        # After cap: 0.40*50 + 0.10*75 + 0.30*0 + 0.15*15 + 0.05*40 = 31.75 -> 32
        score_after = _weighted_score(parsed.breakdown)
        assert score_after == 32

    def test_other_breakdown_dimensions_untouched(self) -> None:
        """The penalty must only touch significance. hands_on_utility,
        big_picture_relevance, financial_services_impact, freshness_momentum
        all stay as the LLM rated them -- the rule is about whether the
        story is NEW, not about its other merits."""
        parsed = _parsed(significance=70)
        cluster = _cluster(
            "c_" + "4" * 14, prior_coverage_ref="c_" + "f" * 14,
        )
        before = dict(parsed.breakdown)
        _apply_prior_coverage_penalty(parsed, cluster)
        for key in (
            "hands_on_utility",
            "big_picture_relevance",
            "financial_services_impact",
            "freshness_momentum",
        ):
            assert parsed.breakdown[key] == before[key], (
                f"{key} should not be affected by the prior-coverage penalty"
            )

    def test_noop_when_significance_already_below_cap(self) -> None:
        """If the LLM already scored significance <= 50, the rule is a no-op
        -- and importantly, no warning log fires either (the rule didn't
        need to act)."""
        parsed = _parsed(significance=40)
        cluster = _cluster(
            "c_" + "5" * 14, prior_coverage_ref="c_" + "f" * 14,
        )
        _apply_prior_coverage_penalty(parsed, cluster)
        assert parsed.breakdown["significance"] == 40

    def test_noop_at_exact_cap(self) -> None:
        """Boundary: significance == cap. No mutation, no log churn."""
        parsed = _parsed(significance=_PRIOR_COVERAGE_SIGNIFICANCE_CAP)
        cluster = _cluster(
            "c_" + "6" * 14, prior_coverage_ref="c_" + "f" * 14,
        )
        _apply_prior_coverage_penalty(parsed, cluster)
        assert parsed.breakdown["significance"] == _PRIOR_COVERAGE_SIGNIFICANCE_CAP

    def test_caps_high_significance_prior_coverage(self) -> None:
        """A prior-coverage story the LLM rated near the top of significance
        still gets pinned to the cap, not to "5 below where it was"."""
        parsed = _parsed(significance=95)
        cluster = _cluster(
            "c_" + "7" * 14, prior_coverage_ref="c_" + "f" * 14,
        )
        _apply_prior_coverage_penalty(parsed, cluster)
        assert parsed.breakdown["significance"] == _PRIOR_COVERAGE_SIGNIFICANCE_CAP


# ===========================================================================
# Freshness-inferred penalty (#86) -- post-LLM deterministic downweighting.
#
# When fetch.py (task #71) detects a feed where every item shares
# `published_at == fetched_at` (the FCA News pattern -- no per-item
# pubdates), it tags each item with `extras["freshness_inferred"] = "true"`.
# rank.py's freshness-inferred penalty caps breakdown["freshness_momentum"]
# at 30 (between rubric anchors 25 = "we don't know" and 50 = "fresh angle")
# when EVERY resolved item in the cluster carries the flag. Mixed clusters
# get a pass: at least one trusted pubdate is enough to trust the signal.
# ===========================================================================

def _parsed_fm(freshness_momentum: int) -> _ParsedScore:
    """Like ``_parsed`` but parameterised on freshness_momentum -- the
    dimension this penalty actually targets."""
    return _ParsedScore(
        breakdown={
            "significance": 60,
            "hands_on_utility": 50,
            "big_picture_relevance": 40,
            "financial_services_impact": 30,
            "freshness_momentum": freshness_momentum,
        },
        audience_tags=["hands_on"],
        rationale="test",
    )


def _fresh_cluster(cluster_id: str, item_ids: list[str]) -> Cluster:
    return Cluster(
        cluster_id=cluster_id,
        item_ids=item_ids,
        canonical_title="t",
        sources=["src_a"],
        earliest_published=FIXED_EARLIER,
        size=len(item_ids),
        prior_coverage_ref=None,
    )


def _item(item_id: str, *, freshness_inferred: bool) -> Item:
    extras: dict[str, str] = {}
    if freshness_inferred:
        extras["freshness_inferred"] = "true"
    return Item(
        id=item_id,
        source="example_blog",
        source_type="rss",
        url=f"https://example.com/{item_id}",
        title="t",
        published_at=FIXED_EARLIER,
        raw_summary="raw",
        fetched_at=FIXED_NOW,
        extras=extras,
    )


class TestFreshnessInferredPenalty:
    """The deterministic post-LLM freshness-inferred penalty -- mirrors
    ``TestContinuationPenalty``. The FCA News feed is the anchor case;
    Monday onwards the penalty will fire whenever the FCA fetch lands."""

    def test_no_change_when_no_items_flagged(self) -> None:
        """All items have a real per-item pubdate (extras empty). The rule
        must not fire -- this is the common path."""
        parsed = _parsed_fm(freshness_momentum=80)
        cluster = _fresh_cluster("c_" + "a" * 14, ["i1", "i2", "i3"])
        items_by_id = {
            "i1": _item("i1", freshness_inferred=False),
            "i2": _item("i2", freshness_inferred=False),
            "i3": _item("i3", freshness_inferred=False),
        }
        before = dict(parsed.breakdown)
        score_before = _weighted_score(parsed.breakdown)
        _apply_freshness_inferred_penalty(parsed, cluster, items_by_id)
        assert parsed.breakdown == before
        assert _weighted_score(parsed.breakdown) == score_before

    def test_no_change_when_only_some_items_flagged(self) -> None:
        """Mixed cluster -- 2 of 3 items have the flag, the third has a real
        pubdate. That one trusted signal is enough; don't penalise."""
        parsed = _parsed_fm(freshness_momentum=80)
        cluster = _fresh_cluster("c_" + "b" * 14, ["i1", "i2", "i3"])
        items_by_id = {
            "i1": _item("i1", freshness_inferred=True),
            "i2": _item("i2", freshness_inferred=True),
            "i3": _item("i3", freshness_inferred=False),
        }
        before = dict(parsed.breakdown)
        _apply_freshness_inferred_penalty(parsed, cluster, items_by_id)
        assert parsed.breakdown == before

    def test_caps_when_all_items_flagged(self) -> None:
        """The smoking-gun case: every resolved item carries the flag and the
        LLM scored freshness_momentum well above the cap. Drop to 30."""
        parsed = _parsed_fm(freshness_momentum=80)
        cluster = _fresh_cluster("c_" + "c" * 14, ["i1", "i2"])
        items_by_id = {
            "i1": _item("i1", freshness_inferred=True),
            "i2": _item("i2", freshness_inferred=True),
        }
        _apply_freshness_inferred_penalty(parsed, cluster, items_by_id)
        assert parsed.breakdown["freshness_momentum"] == _FRESHNESS_INFERRED_CAP
        assert _FRESHNESS_INFERRED_CAP == 30

    def test_noop_at_exact_cap(self) -> None:
        """Boundary: freshness_momentum already at the cap -- no mutation."""
        parsed = _parsed_fm(freshness_momentum=_FRESHNESS_INFERRED_CAP)
        cluster = _fresh_cluster("c_" + "d" * 14, ["i1"])
        items_by_id = {"i1": _item("i1", freshness_inferred=True)}
        before = dict(parsed.breakdown)
        _apply_freshness_inferred_penalty(parsed, cluster, items_by_id)
        assert parsed.breakdown == before

    def test_noop_below_cap(self) -> None:
        """The LLM already scored conservatively at 20 -- the rule must not
        bump it UP to 30. The cap is a ceiling, not a floor."""
        parsed = _parsed_fm(freshness_momentum=20)
        cluster = _fresh_cluster("c_" + "e" * 14, ["i1"])
        items_by_id = {"i1": _item("i1", freshness_inferred=True)}
        _apply_freshness_inferred_penalty(parsed, cluster, items_by_id)
        assert parsed.breakdown["freshness_momentum"] == 20

    def test_score_recomputes_correctly_after_cap(self) -> None:
        """The pydantic invariant `score == weighted_sum(breakdown)` must
        still hold after the penalty mutates breakdown. Anchor on the same
        helper RankedStory uses."""
        parsed = _parsed_fm(freshness_momentum=80)
        cluster = _fresh_cluster("c_" + "f" * 14, ["i1"])
        items_by_id = {"i1": _item("i1", freshness_inferred=True)}
        # Before (v0.6 weights 40/10/30/15/5):
        # 0.40*60 + 0.10*50 + 0.30*40 + 0.15*30 + 0.05*80 = 49.5 -> 50
        score_before = _weighted_score(parsed.breakdown)
        assert score_before == 50

        _apply_freshness_inferred_penalty(parsed, cluster, items_by_id)
        # After cap: 0.40*60 + 0.10*50 + 0.30*40 + 0.15*30 + 0.05*30 = 47.0 -> 47
        score_after = _weighted_score(parsed.breakdown)
        assert score_after == 47
        # Breakdown sums must match the recomputed score exactly.
        assert _weighted_score(parsed.breakdown) == score_after


# ===========================================================================
# Novelty detection (#89) -- novelty-aware prior-coverage cap selection.
#
# Mirrors TestPriorCoveragePenalty but parameterised on the LLM-returned
# `novelty` value. The deterministic cap now branches:
#   * novelty == "none"  -> 25  (effective duplicate; tier flips to "cut")
#   * novelty == "minor" -> 40
#   * novelty == "major" -> 50  (existing #81 behaviour)
#   * missing / invalid  -> 50  (don't punish on uncertainty)
#
# Anchor case: c_fe59351a8d336457 on 2026-05-25 -- NuExtract3 Reddit thread
# linking to HuggingFace, pure duplicate of Issue #1 Pulse. Pre-#89: passed
# at significance=50 + hands_on_utility=100, scoring 55 in Hands-On.
# Post-#89: novelty="none" -> significance=25 -> score=~40 -> tier="cut".
# ===========================================================================

def _parsed_with_novelty(
    significance: int, novelty: str | None
) -> _ParsedScore:
    """Like ``_parsed`` but parameterised on novelty -- the new variable
    this branch of the rule actually keys off."""
    return _ParsedScore(
        breakdown={
            "significance": significance,
            "hands_on_utility": 100,  # the failure-mode lever from the anchor case
            "big_picture_relevance": 25,
            "financial_services_impact": 50,
            "freshness_momentum": 25,
        },
        audience_tags=["hands_on"],
        rationale="test",
        novelty=novelty,
    )


class TestNoveltyDetection:
    """Novelty-aware cap selection in ``_apply_prior_coverage_penalty``.

    Sister suite to ``TestPriorCoveragePenalty`` -- same shape, but the cap
    is now chosen by the LLM-returned ``novelty`` field instead of the
    fixed 50 from #81.
    """

    def test_no_prior_coverage_no_novelty_no_cap(self) -> None:
        """Fresh stories must not be touched, even if the LLM somehow
        produced a novelty value -- without ``prior_coverage_ref``, the
        rule is a no-op regardless. Most stories take this path."""
        parsed = _parsed_with_novelty(significance=80, novelty=None)
        cluster = _cluster("c_" + "a" * 14, prior_coverage_ref=None)
        _apply_prior_coverage_penalty(parsed, cluster)
        assert parsed.breakdown["significance"] == 80

    def test_novelty_none_caps_significance_at_25(self) -> None:
        """The smoking-gun fix -- novelty="none" (effective duplicate)
        must collapse significance to 25, low enough that
        ``_assign_initial_tier`` flips the story to "cut" via the
        ``sig <= 25`` gate."""
        parsed = _parsed_with_novelty(significance=80, novelty="none")
        cluster = _cluster(
            "c_" + "b" * 14, prior_coverage_ref="c_" + "f" * 14,
        )
        _apply_prior_coverage_penalty(parsed, cluster)
        assert parsed.breakdown["significance"] == 25
        assert _PRIOR_COVERAGE_NOVELTY_CAPS["none"] == 25

    def test_novelty_minor_caps_at_40(self) -> None:
        """Incremental update -- intermediate cap between effective
        duplicate (25) and substantive (50)."""
        parsed = _parsed_with_novelty(significance=80, novelty="minor")
        cluster = _cluster(
            "c_" + "c" * 14, prior_coverage_ref="c_" + "f" * 14,
        )
        _apply_prior_coverage_penalty(parsed, cluster)
        assert parsed.breakdown["significance"] == 40
        assert _PRIOR_COVERAGE_NOVELTY_CAPS["minor"] == 40

    def test_novelty_major_caps_at_50(self) -> None:
        """Substantive new info -- preserves the existing #81 behaviour
        (cap at 50). The novelty branch doesn't make the LLM nicer; it
        just doesn't make it harsher."""
        parsed = _parsed_with_novelty(significance=80, novelty="major")
        cluster = _cluster(
            "c_" + "d" * 14, prior_coverage_ref="c_" + "f" * 14,
        )
        _apply_prior_coverage_penalty(parsed, cluster)
        assert parsed.breakdown["significance"] == 50
        assert _PRIOR_COVERAGE_NOVELTY_CAPS["major"] == 50
        assert _PRIOR_COVERAGE_NOVELTY_CAPS["major"] == _PRIOR_COVERAGE_SIGNIFICANCE_CAP

    def test_invalid_novelty_defaults_to_major_cap(self) -> None:
        """LLM returned an unexpected string -- fall back to the existing
        50 cap. Don't punish the cluster for an LLM glitch."""
        parsed = _parsed_with_novelty(significance=80, novelty="weird value")
        cluster = _cluster(
            "c_" + "e" * 14, prior_coverage_ref="c_" + "f" * 14,
        )
        _apply_prior_coverage_penalty(parsed, cluster)
        assert parsed.breakdown["significance"] == _PRIOR_COVERAGE_SIGNIFICANCE_CAP
        assert parsed.breakdown["significance"] == 50

    def test_missing_novelty_defaults_to_major_cap(self) -> None:
        """LLM omitted the field entirely (None on the parsed shape) --
        same default-to-50 behaviour as the invalid-value case."""
        parsed = _parsed_with_novelty(significance=80, novelty=None)
        cluster = _cluster(
            "c_" + "0" * 14, prior_coverage_ref="c_" + "f" * 14,
        )
        _apply_prior_coverage_penalty(parsed, cluster)
        assert parsed.breakdown["significance"] == _PRIOR_COVERAGE_SIGNIFICANCE_CAP

    def test_score_recomputes_after_cap(self) -> None:
        """The pydantic invariant ``score == weighted_sum(breakdown)`` must
        still hold after the novelty-driven cap mutates breakdown. Anchor
        on the 2026-05-25 NuExtract3 numbers: pre-#89 the story scored 55
        in Hands-On; post-#89 with novelty="none" it should drop to ~40
        AND tier "cut" via sig=25."""
        parsed = _parsed_with_novelty(significance=50, novelty="none")
        cluster = _cluster(
            "c_" + "1" * 14, prior_coverage_ref="c_" + "f" * 14,
        )
        # Anchor before (v0.6 weights 40/10/30/15/5):
        # 0.40*50 + 0.10*100 + 0.30*25 + 0.15*50 + 0.05*25 = 46.25 -> 46
        score_before = _weighted_score(parsed.breakdown)
        assert score_before == 46

        _apply_prior_coverage_penalty(parsed, cluster)
        # After cap: 0.40*25 + 0.10*100 + 0.30*25 + 0.15*50 + 0.05*25 = 36.25 -> 36
        score_after = _weighted_score(parsed.breakdown)
        assert score_after == 36
        # AND tier will be "cut" downstream via sig=25 floor.

    def test_lookup_prior_coverage_finds_match(self, tmp_path, monkeypatch) -> None:
        """`_lookup_prior_coverage` walks released issues and returns the
        prior headline + truncated summary excerpt on a story_id hit."""
        from src import paths as _paths
        # Build a minimal released archive: data/released/2026-05-23/issue.json
        date_dir = tmp_path / "released" / "2026-05-23"
        date_dir.mkdir(parents=True)
        prior_summary = (
            "NuExtract3 is a small image-and-text model built for extracting "
            "structured information from PDFs, invoices, tables, and "
            "screenshots, released under a permissive open licence with "
            "weights on Hugging Face -- runs locally, no cloud round-trip."
        )
        issue = {
            "pulse": {
                "name": "pulse",
                "stories": [
                    {
                        "story_id": "c_56849ea45c325178",
                        "headline": "A small open model pulls structured data from invoices",
                        "summary": prior_summary,
                        "source_urls": ["https://example.com/a"],
                    }
                ],
            },
            "sections": [],
        }
        (date_dir / "issue.json").write_text(__import__("json").dumps(issue))
        # Re-point both archive roots at our tmp tree.
        monkeypatch.setattr(_paths, "RELEASED_ROOT", tmp_path / "released")
        monkeypatch.setattr(_paths, "DATA_ROOT", tmp_path)

        out = _lookup_prior_coverage(
            "c_56849ea45c325178", today=_dt.date(2026, 5, 25),
        )
        assert out is not None
        headline, excerpt = out
        assert "small open model" in headline
        assert excerpt.startswith("NuExtract3 is a small image-and-text model")
        # Excerpt was capped + suffixed (the source is 250+ chars).
        assert excerpt.endswith("...")
        assert len(excerpt) <= 210  # 200-char cap + a few chars for "..."

    def test_lookup_prior_coverage_returns_none_when_not_found(
        self, tmp_path, monkeypatch
    ) -> None:
        """No matching story_id in the released window => None (caller
        falls back to no PRIOR COVERAGE block; only the default cap fires)."""
        from src import paths as _paths
        date_dir = tmp_path / "released" / "2026-05-23"
        date_dir.mkdir(parents=True)
        issue = {
            "pulse": {
                "name": "pulse",
                "stories": [
                    {
                        "story_id": "c_someotherstory",
                        "headline": "h",
                        "summary": "s",
                        "source_urls": ["https://example.com/a"],
                    }
                ],
            },
            "sections": [],
        }
        (date_dir / "issue.json").write_text(__import__("json").dumps(issue))
        monkeypatch.setattr(_paths, "RELEASED_ROOT", tmp_path / "released")
        monkeypatch.setattr(_paths, "DATA_ROOT", tmp_path)

        out = _lookup_prior_coverage(
            "c_56849ea45c325178", today=_dt.date(2026, 5, 25),
        )
        assert out is None


# ===========================================================================
# audience_tags validation retry (v0.5, 2026-05-26)
#
# The runtime log on 2026-05-26 surfaced cluster c_fb359151221d4e62 lost to
# `audience_tags=[] -> pydantic ValidationError`. The existing skip-on-
# validation-failure path was correct safety-net behaviour; the v0.5 change
# extends the JSON-parse retry budget (which already existed) to ALSO retry
# on pydantic ValidationError, with a corrective nudge quoting the specific
# error. The retry budget is shared (single attempt across parse/validate
# failures) so the failure modes can't compound into more LLM calls.
#
# The three tests below pin: (1) successful retry, (2) skip on both fail,
# (3) the prompt actually carries the "use general if no other tag fits"
# guidance so the LLM has a clean fallback (a prompt regression test).
# ===========================================================================

def _rank_one_test_cluster() -> Cluster:
    """A fresh cluster (no prior coverage, no special flags) so the
    deterministic post-LLM penalties don't fire and we isolate the
    audience_tags retry path."""
    return Cluster(
        cluster_id="c_fb359151221d4e62",  # the runtime-log anchor cluster
        item_ids=["i1"],
        canonical_title="A small open model handles invoices on-device",
        sources=["example_blog"],
        earliest_published=FIXED_EARLIER,
        size=1,
        prior_coverage_ref=None,
    )


def _rank_one_items_by_id() -> dict[str, Item]:
    return {
        "i1": Item(
            id="i1",
            source="example_blog",
            source_type="rss",
            url="https://example.com/i1",
            title="t",
            published_at=FIXED_EARLIER,
            raw_summary="raw",
            fetched_at=FIXED_NOW,
        ),
    }


_VALID_PAYLOAD_TEMPLATE = (
    '{{'
    '"cluster_id": "c_fb359151221d4e62",'
    '"score": 63,'
    '"breakdown": {{'
    '"significance": 70,'
    '"hands_on_utility": 80,'
    '"big_picture_relevance": 50,'
    '"financial_services_impact": 40,'
    '"freshness_momentum": 60'
    '}},'
    '"audience_tags": {tags},'
    '"rationale": "Practical for FS doc workflows; on-device deployment."'
    '}}'
)


def _payload_with_tags(tags_json: str) -> str:
    """Build a JSON LLM-output payload with the given ``audience_tags`` JSON
    fragment (e.g. ``"[]"`` for the bug-repro case, ``'["hands_on"]'`` for a
    valid follow-up)."""
    return _VALID_PAYLOAD_TEMPLATE.format(tags=tags_json)


class TestAudienceTagsRetry:
    """v0.5 retry-on-ValidationError. Mirrors the runtime log:
    audience_tags=[] -> pydantic rejects -> retry with nudge -> success
    (or skip after second failure)."""

    def test_retry_on_validation_error_succeeds_on_second_attempt(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First LLM response returns audience_tags=[]; pydantic rejects;
        retry with corrective nudge returns audience_tags=["hands_on"];
        ``_rank_one`` returns the RankedStory built from the second attempt.
        The validation-error log line must fire (operator-visibility)."""
        # Force LLM_TEMPERATURE_RANK to a known value so _rank_one doesn't
        # read an unset env var; also no other env required because the
        # patch targets _llm_call directly.
        monkeypatch.setenv("LLM_TEMPERATURE_RANK", "0.2")

        responses = [
            _payload_with_tags("[]"),               # bug-repro: empty list
            _payload_with_tags('["hands_on"]'),     # fixed on retry
        ]
        with patch("src.rank._llm_call", side_effect=responses) as mock_llm:
            with caplog.at_level("WARNING", logger="ai_vector.rank"):
                story = _rank_one(
                    cluster=_rank_one_test_cluster(),
                    items_by_id=_rank_one_items_by_id(),
                    rubric_block="(rubric)",
                    trust_weights={},
                )

        assert story is not None, (
            "retry should have built RankedStory from the second response"
        )
        assert story.cluster_id == "c_fb359151221d4e62"
        assert list(story.audience_tags) == ["hands_on"]
        # Both LLM calls were made (initial + 1 retry).
        assert mock_llm.call_count == 2
        # The retry prompt must quote the specific validation error back to
        # the LLM (signal the operator sees in logs too).
        assert any(
            "pydantic validation failed" in rec.getMessage()
            for rec in caplog.records
        ), "validation-error log line should fire on the first attempt"
        # And the corrective nudge text reaches the LLM call on attempt 2.
        retry_prompt = mock_llm.call_args_list[1].args[0]
        assert "Your prior response failed validation" in retry_prompt
        assert "audience_tags" in retry_prompt

    def test_retry_on_validation_error_then_skip_when_both_fail(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both attempts return audience_tags=[]. ``_rank_one`` returns None;
        the cluster is skipped. The existing safety net stays intact -- this
        test pins it explicitly so a future refactor can't break the
        skip-on-second-failure behaviour."""
        monkeypatch.setenv("LLM_TEMPERATURE_RANK", "0.2")

        responses = [
            _payload_with_tags("[]"),
            _payload_with_tags("[]"),
        ]
        with patch("src.rank._llm_call", side_effect=responses) as mock_llm:
            with caplog.at_level("WARNING", logger="ai_vector.rank"):
                story = _rank_one(
                    cluster=_rank_one_test_cluster(),
                    items_by_id=_rank_one_items_by_id(),
                    rubric_block="(rubric)",
                    trust_weights={},
                )

        assert story is None, (
            "two consecutive validation failures must produce a skip "
            "(safety net intact)"
        )
        # Both attempts ran (initial + 1 retry); no third attempt.
        assert mock_llm.call_count == 2
        # The validation-error log must have fired for BOTH attempts so the
        # operator sees the full story.
        validation_logs = [
            rec for rec in caplog.records
            if "pydantic validation failed" in rec.getMessage()
        ]
        assert len(validation_logs) == 2, (
            f"expected 2 validation-error log lines, got {len(validation_logs)}"
        )

    def test_audience_tags_general_fallback_in_prompt(self) -> None:
        """The rank prompt must explicitly tell the LLM to fall back to
        "general" when no other tag fits, and that the list must never be
        empty. Pins the v0.5 prompt change so it can't silently regress."""
        prompt = _build_rank_prompt(
            cluster=_rank_one_test_cluster(),
            items_by_id=_rank_one_items_by_id(),
            rubric_block="(rubric)",
            trust_weights={},
        )
        # The exact wording matters (cheap to maintain, expensive to lose):
        # the LLM needs an unambiguous fallback path so empty-list never
        # becomes its default escape hatch. Collapse whitespace before the
        # substring check so a wrap-line refactor doesn't break the test
        # (the rendered prompt wraps "must never be / empty").
        flat = " ".join(prompt.split())
        assert "must never be empty" in flat
        assert 'use "general"' in flat
        # And all four allowed tag values must be quoted in the AUDIENCE
        # TAGS block so the LLM can copy-paste rather than invent variants.
        for tag in ("hands_on", "big_picture", "finance", "general"):
            assert f'"{tag}"' in prompt


# ===========================================================================
# Parallel ranking -- the per-cluster LLM calls are fully independent, so
# `rank()` fans them out across a ThreadPoolExecutor (workers configurable
# via LLM_CONCURRENCY). The tests below pin: (1) sort-by-score-desc is
# preserved regardless of future-completion order, (2) per-call skip
# semantics survive parallelisation, (3) unexpected exceptions are caught
# per-future and logged at ERROR level, (4) the env-var concurrency knob is
# respected, (5) the implementation is actually parallel (wall-clock check).
# ===========================================================================

def _write_clusters_jsonl(
    *,
    tmp_data_root: Path,
    run_date: _dt.date,
    n: int,
) -> tuple[list[str], list[str]]:
    """Set up `data/staging/<date>/clusters.jsonl` + `items.jsonl` with `n`
    minimal clusters (one item each). Returns (cluster_ids, item_ids) in
    submission order.

    Used by the parallel-rank tests: the actual cluster content doesn't
    matter because `_rank_one` is mocked -- we just need real on-disk
    records so `rank()` reaches the parallel block.
    """
    from src import paths as _paths

    staging = _paths.staging_dir(run_date)
    staging.mkdir(parents=True, exist_ok=True)

    cluster_ids: list[str] = []
    item_ids: list[str] = []
    cluster_lines: list[str] = []
    item_lines: list[str] = []
    for idx in range(n):
        # cluster_id must match the `c_<12+ hex>` pattern -- pad with zeros.
        cid = f"c_{idx:016x}"
        iid = f"i_{idx}"
        cluster_ids.append(cid)
        item_ids.append(iid)
        cluster = Cluster(
            cluster_id=cid,
            item_ids=[iid],
            canonical_title=f"Cluster {idx}",
            sources=["example_blog"],
            earliest_published=FIXED_EARLIER,
            size=1,
        )
        item = Item(
            id=iid,
            source="example_blog",
            source_type="rss",
            url=f"https://example.com/{idx}",
            title=f"Title {idx}",
            published_at=FIXED_EARLIER,
            raw_summary=f"summary {idx}",
            fetched_at=FIXED_NOW,
        )
        cluster_lines.append(cluster.model_dump_json())
        item_lines.append(item.model_dump_json())

    (staging / "clusters.jsonl").write_text(
        "\n".join(cluster_lines) + "\n", encoding="utf-8"
    )
    (staging / "items.jsonl").write_text(
        "\n".join(item_lines) + "\n", encoding="utf-8"
    )
    return cluster_ids, item_ids


def _make_ranked_story(cluster_id: str, score: int):
    """Build a real `RankedStory` whose `score` matches `_weighted_score` of
    its breakdown -- pydantic's invariant requires this. The breakdown
    weights are: sig 30, ho 25, bp 20, fs 15, fm 10. Setting every field to
    `score` makes the weighted sum equal `score` exactly."""
    from src.models import RankedStory as _RS

    breakdown = {
        "significance": score,
        "hands_on_utility": score,
        "big_picture_relevance": score,
        "financial_services_impact": score,
        "freshness_momentum": score,
    }
    return _RS(
        cluster_id=cluster_id,
        score=score,
        breakdown=breakdown,
        audience_tags=["hands_on"],
        rationale="parallel-rank test fixture",
        tier="currents",
        prompt_version="v1",
    )


class TestParallelRank:
    """End-to-end-ish tests for the parallel rank() entry point.

    `_rank_one` is mocked at the `src.rank._rank_one` symbol; this is the
    public seam between rank() and the per-cluster LLM machinery. The
    rest of rank() -- IO, clamps, sort, atomic write -- runs unmocked so
    we exercise the actual ThreadPoolExecutor path."""

    def _run_with_mocked_rank_one(
        self,
        *,
        tmp_data_root: Path,
        n_clusters: int,
        side_effect,
        monkeypatch: pytest.MonkeyPatch,
        concurrency: str = "8",
    ) -> tuple[list, list[str]]:
        """Helper: write n clusters, mock `_rank_one`, run `rank()`, return
        the (ranked, cluster_ids) tuple. `side_effect` is the
        MagicMock side-effect for `_rank_one` (a callable mapping
        kwargs->RankedStory|None|raise)."""
        run_date = FIXED_NOW.date()
        cluster_ids, _ = _write_clusters_jsonl(
            tmp_data_root=tmp_data_root, run_date=run_date, n=n_clusters,
        )
        monkeypatch.setenv("LLM_CONCURRENCY", concurrency)
        # LLM_MODEL is asserted by _llm_call but we mock above it; still set
        # it so any unmocked codepath fails loud instead of silently.
        monkeypatch.setenv("LLM_MODEL", "test-model")

        with patch("src.rank._rank_one", side_effect=side_effect) as mock:
            ranked = rank(run_date)
        return ranked, cluster_ids, mock

    def test_parallel_rank_preserves_order(
        self,
        tmp_data_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even when futures complete in arbitrary order, the final ranked
        list is sorted by score desc. We assign scrambled scores
        (40, 95, 60, 80, 25) and assert the output is [95, 80, 60, 40, 25]."""
        scrambled_scores = [40, 95, 60, 80, 25]
        n = len(scrambled_scores)

        def fake_rank_one(cluster, items_by_id, rubric_block,
                          trust_weights, *, tier_thresholds=None, today=None):
            # Map cluster_id back to its index via the deterministic id pattern.
            idx = int(cluster.cluster_id.split("_", 1)[1], 16)
            return _make_ranked_story(cluster.cluster_id, scrambled_scores[idx])

        ranked, _cluster_ids, _mock = self._run_with_mocked_rank_one(
            tmp_data_root=tmp_data_root,
            n_clusters=n,
            side_effect=fake_rank_one,
            monkeypatch=monkeypatch,
        )

        assert [r.score for r in ranked] == sorted(
            scrambled_scores, reverse=True
        )
        # And the on-disk file mirrors the in-memory order (the atomic write
        # iterates the sorted list).
        from src import paths as _paths
        ranked_path = _paths.ranked_path(FIXED_NOW.date(), canonical=False)
        on_disk_scores = [
            json.loads(line)["score"]
            for line in ranked_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert on_disk_scores == sorted(scrambled_scores, reverse=True)

    def test_parallel_rank_skips_failed_calls(
        self,
        tmp_data_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`_rank_one` returning `None` for one cluster must not abort the
        batch -- the other clusters still land in ranked.jsonl. Mirrors
        the existing skip-on-error semantics."""
        n = 5

        def fake_rank_one(cluster, items_by_id, rubric_block,
                          trust_weights, *, tier_thresholds=None, today=None):
            idx = int(cluster.cluster_id.split("_", 1)[1], 16)
            if idx == 2:  # the middle cluster fails to parse / validate
                return None
            return _make_ranked_story(cluster.cluster_id, 50 + idx)

        ranked, cluster_ids, _mock = self._run_with_mocked_rank_one(
            tmp_data_root=tmp_data_root,
            n_clusters=n,
            side_effect=fake_rank_one,
            monkeypatch=monkeypatch,
        )

        assert len(ranked) == n - 1
        returned_ids = {r.cluster_id for r in ranked}
        # The "None" cluster is absent; the others are all present.
        assert cluster_ids[2] not in returned_ids
        for idx in (0, 1, 3, 4):
            assert cluster_ids[idx] in returned_ids

    def test_parallel_rank_propagates_unexpected_exceptions_to_skip(
        self,
        tmp_data_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """`_rank_one` raising an unexpected exception (e.g. thread-level
        failure beyond its own try/except) must be caught at the future
        boundary, logged at ERROR, and the other clusters still ship."""
        n = 4

        def fake_rank_one(cluster, items_by_id, rubric_block,
                          trust_weights, *, tier_thresholds=None, today=None):
            idx = int(cluster.cluster_id.split("_", 1)[1], 16)
            if idx == 1:
                raise RuntimeError("simulated thread panic")
            return _make_ranked_story(cluster.cluster_id, 50 + idx)

        with caplog.at_level("ERROR", logger="ai_vector.rank"):
            ranked, cluster_ids, _mock = self._run_with_mocked_rank_one(
                tmp_data_root=tmp_data_root,
                n_clusters=n,
                side_effect=fake_rank_one,
                monkeypatch=monkeypatch,
            )

        # The exception cluster is skipped; the three survivors land.
        assert len(ranked) == n - 1
        assert cluster_ids[1] not in {r.cluster_id for r in ranked}
        # And the log line names the failed cluster_id so an operator can
        # correlate against the input clusters.jsonl.
        error_logs = [
            rec for rec in caplog.records
            if rec.levelname == "ERROR" and cluster_ids[1] in rec.getMessage()
        ]
        assert error_logs, (
            "expected an ERROR log line naming the failed cluster_id"
        )

    def test_llm_concurrency_env_var_respected(
        self,
        tmp_data_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Setting `LLM_CONCURRENCY=2` must pass `max_workers=2` to
        ThreadPoolExecutor. Spies on the executor constructor so the
        assertion is direct -- no inference from observed concurrency."""

        from src import rank as _rank_module

        captured: dict[str, Any] = {}
        real_pool_cls = _rank_module.ThreadPoolExecutor

        def spy_pool(*args, **kwargs):
            captured["max_workers"] = kwargs.get("max_workers")
            return real_pool_cls(*args, **kwargs)

        def fake_rank_one(cluster, items_by_id, rubric_block,
                          trust_weights, *, tier_thresholds=None, today=None):
            idx = int(cluster.cluster_id.split("_", 1)[1], 16)
            return _make_ranked_story(cluster.cluster_id, 50 + idx)

        run_date = FIXED_NOW.date()
        _write_clusters_jsonl(
            tmp_data_root=tmp_data_root, run_date=run_date, n=3,
        )
        monkeypatch.setenv("LLM_CONCURRENCY", "2")
        monkeypatch.setenv("LLM_MODEL", "test-model")

        with patch.object(_rank_module, "ThreadPoolExecutor", spy_pool):
            with patch("src.rank._rank_one", side_effect=fake_rank_one):
                rank(run_date)

        assert captured["max_workers"] == 2

    def test_parallel_rank_actually_parallel(
        self,
        tmp_data_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sleep-based wall-clock check. With 10 clusters x 0.1s per call and
        concurrency=5, total wall clock should be ~0.2s -- definitely under
        the sequential 1.0s. We assert at least 3x speedup vs the upper
        sequential bound so the test stays robust on a loaded CI runner."""
        n_clusters = 10
        per_call_sleep = 0.1
        concurrency = 5
        sequential_upper_bound = n_clusters * per_call_sleep  # ~1.0s

        def fake_rank_one(cluster, items_by_id, rubric_block,
                          trust_weights, *, tier_thresholds=None, today=None):
            time.sleep(per_call_sleep)
            idx = int(cluster.cluster_id.split("_", 1)[1], 16)
            return _make_ranked_story(cluster.cluster_id, 50 + idx)

        run_date = FIXED_NOW.date()
        _write_clusters_jsonl(
            tmp_data_root=tmp_data_root, run_date=run_date, n=n_clusters,
        )
        monkeypatch.setenv("LLM_CONCURRENCY", str(concurrency))
        monkeypatch.setenv("LLM_MODEL", "test-model")

        with patch("src.rank._rank_one", side_effect=fake_rank_one):
            t0 = time.monotonic()
            ranked = rank(run_date)
            elapsed = time.monotonic() - t0

        assert len(ranked) == n_clusters
        # Sanity: with concurrency=5 and 10 calls x 0.1s, ideal is ~0.2s.
        # Allow generous headroom for CI jitter -- 3x faster than sequential
        # is the contract.
        assert elapsed * 3 < sequential_upper_bound, (
            f"parallel rank took {elapsed:.3f}s; expected < "
            f"{sequential_upper_bound/3:.3f}s (3x speedup vs sequential "
            f"upper bound {sequential_upper_bound:.3f}s)"
        )



# ===========================================================================
# _assign_initial_tier -- schema v3 (2026-05-30) tier routing.
#
# Tier is now the AUTHORITY for summarise.py's section routing -- rank.py
# writes the full editorial slot here (no scavenging downstream). The four
# outcomes (Phase 2 rename, 2026-05-30: on_the_radar -> currents):
#   cut             -- below thresholds.cut.max_score OR sig <= max_significance
#   currents        -- in middle band OR promoted but no head-section tag
#   big_picture     -- promoted AND big_picture tag (with tiebreak vs hands_on)
#   hands_on        -- promoted AND hands_on tag (with tiebreak vs big_picture)
# ===========================================================================

class TestAssignInitialTier:
    """Schema v3 (2026-05-30): tier-as-authority routing in rank.py."""

    def _base_breakdown(
        self,
        *,
        significance: int = 70,
        hands_on_utility: int = 70,
        big_picture_relevance: int = 70,
    ) -> dict[str, int]:
        """A breakdown the tier function reads. Other dimensions don't
        affect routing; we set them to a neutral 50."""
        return {
            "significance": significance,
            "hands_on_utility": hands_on_utility,
            "big_picture_relevance": big_picture_relevance,
            "financial_services_impact": 50,
            "freshness_momentum": 50,
        }

    def test_cut_when_score_below_cut_max_score(self) -> None:
        """score < cut.max_score -> cut, regardless of other dimensions."""
        tier = _assign_initial_tier(
            score=30,
            breakdown=self._base_breakdown(significance=70),
            audience_tags=["hands_on", "big_picture"],
            thresholds=_DEFAULT_TIER_THRESHOLDS,
        )
        assert tier == "cut"

    def test_cut_when_significance_at_or_below_cut_max(self) -> None:
        """significance <= cut.max_significance is the Tier-3 trapdoor --
        the editorial-focus skill's pre-filter rule. A vendor announcement
        scoring high on hands_on_utility but flat on significance gets
        cut even if the weighted score clears the floor."""
        tier = _assign_initial_tier(
            score=80,  # high enough that the score gate alone wouldn't cut
            breakdown=self._base_breakdown(significance=25),
            audience_tags=["hands_on"],
            thresholds=_DEFAULT_TIER_THRESHOLDS,
        )
        assert tier == "cut"

    def test_currents_when_between_cut_and_promote(self) -> None:
        """Score in the middle band -> currents regardless of tags.

        v0.4 (2026-05-30): promote_to_section.min_score is 55, so 45 is
        the middle-band test point (40 <= score < 55). Phase 2
        (2026-05-30): tier value renamed on_the_radar -> currents.
        """
        tier = _assign_initial_tier(
            score=45,
            breakdown=self._base_breakdown(significance=60),
            audience_tags=["hands_on", "big_picture"],
            thresholds=_DEFAULT_TIER_THRESHOLDS,
        )
        assert tier == "currents"

    def test_neither_head_tag_routes_by_subscore_to_hands_on(self) -> None:
        """v0.4 (2026-05-30) NEITHER-branch fix: promoted with only
        general / finance tags -> route by sub-score, not currents.

        hands_on_utility > big_picture_relevance -> hands_on. Anchor case:
        a finance-tagged practitioner story that clears the promote floor
        but the LLM didn't apply the hands_on tag explicitly.
        """
        tier = _assign_initial_tier(
            score=70,
            breakdown=self._base_breakdown(
                significance=70, hands_on_utility=80, big_picture_relevance=50,
            ),
            audience_tags=["finance", "general"],
            thresholds=_DEFAULT_TIER_THRESHOLDS,
        )
        assert tier == "hands_on"

    def test_neither_head_tag_routes_by_subscore_to_big_picture(self) -> None:
        """v0.4 (2026-05-30) NEITHER-branch fix: bp > ho -> big_picture.
        Anchor case: c_491e0b408f3bab95-style regulatory finance content
        promoted at 60+ with strong big_picture_relevance.
        """
        tier = _assign_initial_tier(
            score=65,
            breakdown=self._base_breakdown(
                significance=65, hands_on_utility=30, big_picture_relevance=75,
            ),
            audience_tags=["finance"],
            thresholds=_DEFAULT_TIER_THRESHOLDS,
        )
        assert tier == "big_picture"

    def test_neither_head_tag_subscore_tie_goes_to_big_picture(self) -> None:
        """v0.4 NEITHER-branch fix: ho == bp -> big_picture (same tiebreak
        rule as the BOTH branch). Keeps tiebreak symmetry between the two
        branches so the more strategic surface wins ties everywhere."""
        tier = _assign_initial_tier(
            score=60,
            breakdown=self._base_breakdown(
                significance=60, hands_on_utility=60, big_picture_relevance=60,
            ),
            audience_tags=["general"],
            thresholds=_DEFAULT_TIER_THRESHOLDS,
        )
        assert tier == "big_picture"

    def test_big_picture_when_promoted_and_only_big_picture_tag(self) -> None:
        """XOR routing -- big_picture tag only -> big_picture tier."""
        tier = _assign_initial_tier(
            score=75,
            breakdown=self._base_breakdown(significance=70),
            audience_tags=["big_picture", "finance"],
            thresholds=_DEFAULT_TIER_THRESHOLDS,
        )
        assert tier == "big_picture"

    def test_hands_on_when_promoted_and_only_hands_on_tag(self) -> None:
        """XOR routing -- hands_on tag only -> hands_on tier."""
        tier = _assign_initial_tier(
            score=75,
            breakdown=self._base_breakdown(significance=70),
            audience_tags=["hands_on", "general"],
            thresholds=_DEFAULT_TIER_THRESHOLDS,
        )
        assert tier == "hands_on"

    def test_both_tags_tiebreak_hands_on_when_ho_strictly_greater(self) -> None:
        """BOTH head-section tags -> tiebreak on breakdown. ho > bp -> hands_on."""
        tier = _assign_initial_tier(
            score=75,
            breakdown=self._base_breakdown(
                significance=70, hands_on_utility=85, big_picture_relevance=60,
            ),
            audience_tags=["hands_on", "big_picture"],
            thresholds=_DEFAULT_TIER_THRESHOLDS,
        )
        assert tier == "hands_on"

    def test_both_tags_tiebreak_big_picture_when_bp_strictly_greater(self) -> None:
        """BOTH head-section tags -> tiebreak on breakdown. bp > ho -> big_picture."""
        tier = _assign_initial_tier(
            score=75,
            breakdown=self._base_breakdown(
                significance=70, hands_on_utility=60, big_picture_relevance=85,
            ),
            audience_tags=["hands_on", "big_picture"],
            thresholds=_DEFAULT_TIER_THRESHOLDS,
        )
        assert tier == "big_picture"

    def test_both_tags_tie_goes_to_big_picture(self) -> None:
        """BOTH head-section tags AND ho == bp -> big_picture (the more
        strategic surface; spec: 'ties go to big_picture')."""
        tier = _assign_initial_tier(
            score=75,
            breakdown=self._base_breakdown(
                significance=70, hands_on_utility=75, big_picture_relevance=75,
            ),
            audience_tags=["hands_on", "big_picture"],
            thresholds=_DEFAULT_TIER_THRESHOLDS,
        )
        assert tier == "big_picture"

    def test_cut_trumps_promote_when_significance_floors(self) -> None:
        """A story with very high weighted score but significance <= 25
        (Tier-3 vendor fluff) must cut before the promote routing fires."""
        tier = _assign_initial_tier(
            score=90,
            breakdown=self._base_breakdown(
                significance=25, hands_on_utility=100, big_picture_relevance=100,
            ),
            audience_tags=["hands_on", "big_picture"],
            thresholds=_DEFAULT_TIER_THRESHOLDS,
        )
        assert tier == "cut"
