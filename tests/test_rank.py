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
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.models import Cluster, Item
from src.rank import (
    _FRESHNESS_INFERRED_CAP,
    _PRIOR_COVERAGE_NOVELTY_CAPS,
    _PRIOR_COVERAGE_SIGNIFICANCE_CAP,
    _ParsedScore,
    _apply_freshness_inferred_penalty,
    _apply_prior_coverage_penalty,
    _llm_call_openai_compatible,
    _lookup_prior_coverage,
    _weighted_score,
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
        # Anchor expected: 0.3*65 + 0.25*75 + 0 + 0.15*15 + 0.10*40 = 44.5 -> 44
        score_before = _weighted_score(parsed.breakdown)
        assert score_before == 44

        _apply_prior_coverage_penalty(parsed, cluster)
        # After cap: 0.3*50 + 0.25*75 + 0 + 0.15*15 + 0.10*40 = 40.0 -> 40
        score_after = _weighted_score(parsed.breakdown)
        assert score_after == 40

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
        # Before: 0.3*60 + 0.25*50 + 0.20*40 + 0.15*30 + 0.10*80
        #       = 18 + 12.5 + 8 + 4.5 + 8 = 51.0 -> 51
        score_before = _weighted_score(parsed.breakdown)
        assert score_before == 51

        _apply_freshness_inferred_penalty(parsed, cluster, items_by_id)
        # After cap: ... + 0.10*30 = 18 + 12.5 + 8 + 4.5 + 3 = 46.0 -> 46
        score_after = _weighted_score(parsed.breakdown)
        assert score_after == 46
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
        # Anchor before: 0.30*50 + 0.25*100 + 0.20*25 + 0.15*50 + 0.10*25
        #              = 15 + 25 + 5 + 7.5 + 2.5 = 55.0 -> 55
        score_before = _weighted_score(parsed.breakdown)
        assert score_before == 55

        _apply_prior_coverage_penalty(parsed, cluster)
        # After cap: 0.30*25 + 0.25*100 + 0.20*25 + 0.15*50 + 0.10*25
        #          = 7.5 + 25 + 5 + 7.5 + 2.5 = 47.5 -> 48 (banker's rounding)
        score_after = _weighted_score(parsed.breakdown)
        # Either banker's rounding (round-half-to-even -> 48) or
        # half-up (-> 48); pin to the Python 3 `round` behaviour.
        assert score_after == 48
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
