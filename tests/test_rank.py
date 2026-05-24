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

Additionally pins the deterministic post-LLM continuation penalty (#81)
in ``TestContinuationPenalty`` -- the rule that caps
``breakdown["significance"]`` at 50 for any cluster carrying a
``cross_time_ref`` so a follow-up to yesterday's story can't crowd fresh
items out of high-scoring slots.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.models import Cluster
from src.rank import (
    _CONTINUATION_SIGNIFICANCE_CAP,
    _ParsedScore,
    _apply_continuation_penalty,
    _llm_call_openai_compatible,
    _weighted_score,
)
from tests.conftest import FIXED_EARLIER


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
# Continuation penalty (#81) -- post-LLM deterministic downweighting.
#
# A continuation (Cluster.cross_time_ref is not None) is a follow-up to a
# story we covered on a previous day. Allowing the LLM to score it 65+ on
# significance crowds genuinely-new stories out of high slots. The penalty
# caps breakdown["significance"] at 50 (rubric anchor 50 = "single signal-
# filter dimension hit"); the caller recomputes score via _weighted_score.
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


def _cluster(cluster_id: str, *, cross_time_ref: str | None) -> Cluster:
    return Cluster(
        cluster_id=cluster_id,
        item_ids=["i1"],
        canonical_title="t",
        sources=["src_a"],
        earliest_published=FIXED_EARLIER,
        size=1,
        cross_time_ref=cross_time_ref,
    )


class TestContinuationPenalty:
    """The deterministic post-LLM continuation penalty -- the safer alternative
    to a prompt change for this rule (see #75/#77 cliff)."""

    def test_no_change_when_cross_time_ref_is_none(self) -> None:
        """Fresh stories must not be touched. Most stories are fresh; this is
        the common path -- a bug here would be a global regression."""
        parsed = _parsed(significance=80)
        cluster = _cluster("c_" + "1" * 14, cross_time_ref=None)
        _apply_continuation_penalty(parsed, cluster)
        assert parsed.breakdown["significance"] == 80

    def test_caps_significance_when_continuation(self) -> None:
        """The smoking-gun anchor: continuation with significance=65 must
        drop to the cap. Mirrors c_2e53967d020fb800 / 2026-05-25."""
        parsed = _parsed(significance=65)
        cluster = _cluster(
            "c_" + "2" * 14, cross_time_ref="c_" + "f" * 14,
        )
        _apply_continuation_penalty(parsed, cluster)
        assert parsed.breakdown["significance"] == _CONTINUATION_SIGNIFICANCE_CAP
        assert _CONTINUATION_SIGNIFICANCE_CAP == 50

    def test_score_recomputes_correctly_after_cap(self) -> None:
        """The pydantic invariant `score == weighted_sum(breakdown)` must
        still hold after the penalty mutates breakdown. We recompute via
        the same _weighted_score helper RankedStory uses."""
        parsed = _parsed(significance=65)
        cluster = _cluster(
            "c_" + "3" * 14, cross_time_ref="c_" + "f" * 14,
        )
        # Anchor expected: 0.3*65 + 0.25*75 + 0 + 0.15*15 + 0.10*40 = 44.5 -> 44
        score_before = _weighted_score(parsed.breakdown)
        assert score_before == 44

        _apply_continuation_penalty(parsed, cluster)
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
            "c_" + "4" * 14, cross_time_ref="c_" + "f" * 14,
        )
        before = dict(parsed.breakdown)
        _apply_continuation_penalty(parsed, cluster)
        for key in (
            "hands_on_utility",
            "big_picture_relevance",
            "financial_services_impact",
            "freshness_momentum",
        ):
            assert parsed.breakdown[key] == before[key], (
                f"{key} should not be affected by the continuation penalty"
            )

    def test_noop_when_significance_already_below_cap(self) -> None:
        """If the LLM already scored significance <= 50, the rule is a no-op
        -- and importantly, no warning log fires either (the rule didn't
        need to act)."""
        parsed = _parsed(significance=40)
        cluster = _cluster(
            "c_" + "5" * 14, cross_time_ref="c_" + "f" * 14,
        )
        _apply_continuation_penalty(parsed, cluster)
        assert parsed.breakdown["significance"] == 40

    def test_noop_at_exact_cap(self) -> None:
        """Boundary: significance == cap. No mutation, no log churn."""
        parsed = _parsed(significance=_CONTINUATION_SIGNIFICANCE_CAP)
        cluster = _cluster(
            "c_" + "6" * 14, cross_time_ref="c_" + "f" * 14,
        )
        _apply_continuation_penalty(parsed, cluster)
        assert parsed.breakdown["significance"] == _CONTINUATION_SIGNIFICANCE_CAP

    def test_caps_high_significance_continuation(self) -> None:
        """A continuation the LLM rated near the top of significance still
        gets pinned to the cap, not to "5 below where it was"."""
        parsed = _parsed(significance=95)
        cluster = _cluster(
            "c_" + "7" * 14, cross_time_ref="c_" + "f" * 14,
        )
        _apply_continuation_penalty(parsed, cluster)
        assert parsed.breakdown["significance"] == _CONTINUATION_SIGNIFICANCE_CAP
