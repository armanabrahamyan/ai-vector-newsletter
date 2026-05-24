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
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.rank import _llm_call_openai_compatible


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
