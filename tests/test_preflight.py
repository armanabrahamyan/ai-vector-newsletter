"""Unit tests for src/preflight.py — environment + endpoint checks.

We do NOT call the real LLM. `_llm_call` is monkeypatched.
We also do NOT touch the HuggingFace cache — `check_embedding_model` is
exercised via its result format, not its disk-presence path (that depends
on developer-machine state).
"""
from __future__ import annotations

import pytest

from src import preflight
from src.preflight import (
    CheckResult,
    check_llm_endpoint,
    format_results,
    run_all_checks,
    run_checks_for_stages,
)


# `TestCheckResult::test_passes_with_defaults` cut: just asserted default
# values on a dataclass -- a definition test (CONVENTIONS sec. 2).


# ---------------------------------------------------------------------------
# check_llm_endpoint — env validation + provider routing.
# Network calls are mocked.
# ---------------------------------------------------------------------------

class TestCheckLLMEndpoint:
    def test_fails_when_provider_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.setenv("LLM_MODEL", "x")
        r = check_llm_endpoint()
        assert r.passed is False
        assert "LLM_PROVIDER" in r.detail

    def test_fails_when_model_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.delenv("LLM_MODEL", raising=False)
        r = check_llm_endpoint()
        assert r.passed is False
        assert "LLM_MODEL" in r.detail

    @pytest.mark.parametrize("provider", ["anthropic", "bedrock", "openai", "litellm", "ollama"])
    def test_supported_providers_are_accepted_for_routing(
        self, provider: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All five supported providers must pass the provider-validation
        step. We mock `_llm_call` so they don't hit the network."""
        monkeypatch.setenv("LLM_PROVIDER", provider)
        monkeypatch.setenv("LLM_MODEL", "test-model")
        # Patch the rank module's _llm_call so check_llm_endpoint's import succeeds.
        import src.rank as rank_mod
        monkeypatch.setattr(rank_mod, "_llm_call", lambda *a, **kw: "ok")
        r = check_llm_endpoint()
        assert r.passed is True, f"{provider} should pass with mocked LLM: {r.detail}"

    def test_unknown_provider_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "made-up-provider")
        monkeypatch.setenv("LLM_MODEL", "x")
        r = check_llm_endpoint()
        assert r.passed is False
        assert "unknown LLM_PROVIDER" in r.detail

    def test_propagates_llm_call_errors_as_failed_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A network/auth failure becomes a failed check, not an exception."""
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("LLM_MODEL", "x")
        import src.rank as rank_mod

        def _boom(*a, **kw):
            raise RuntimeError("simulated auth failure")
        monkeypatch.setattr(rank_mod, "_llm_call", _boom)
        r = check_llm_endpoint()
        assert r.passed is False
        assert "RuntimeError" in r.detail

    def test_empty_response_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("LLM_MODEL", "x")
        import src.rank as rank_mod
        monkeypatch.setattr(rank_mod, "_llm_call", lambda *a, **kw: "   ")
        r = check_llm_endpoint()
        assert r.passed is False
        assert "empty response" in r.detail


# ---------------------------------------------------------------------------
# run_all_checks + run_checks_for_stages — orchestration.
# ---------------------------------------------------------------------------

class TestRunAllChecks:
    def test_aggregates_pass_when_all_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(preflight, "CHECKS", [
            lambda: CheckResult("a", True),
            lambda: CheckResult("b", True),
        ])
        results, all_passed = run_all_checks()
        assert all_passed is True
        assert len(results) == 2

    def test_aggregates_fail_when_any_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(preflight, "CHECKS", [
            lambda: CheckResult("a", True),
            lambda: CheckResult("b", False, detail="broken"),
        ])
        results, all_passed = run_all_checks()
        assert all_passed is False


class TestRunChecksForStages:
    def test_returns_empty_for_stages_with_no_checks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fetch + render require no preflight."""
        results, all_passed = run_checks_for_stages(["fetch", "render"])
        assert results == []
        assert all_passed is True

    def test_dedupes_shared_checks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """rank and summarise both need the LLM check — run it ONCE."""
        call_count = {"n": 0}
        def _llm_check_stub() -> CheckResult:
            call_count["n"] += 1
            return CheckResult("llm", True)
        # Stub both stage-checks dicts to use our counting function.
        monkeypatch.setitem(preflight.STAGE_CHECKS, "rank", [_llm_check_stub])
        monkeypatch.setitem(preflight.STAGE_CHECKS, "summarise", [_llm_check_stub])
        results, _ = run_checks_for_stages(["rank", "summarise"])
        assert call_count["n"] == 1
        assert len(results) == 1

    def test_cluster_requires_embedding_check(self) -> None:
        """The cluster stage must require the embedding-model check --
        this is the wiring that prevents a fresh checkout running the
        cluster stage and OOMing on a missed model download."""
        assert preflight.check_embedding_model in preflight.STAGE_CHECKS["cluster"]

    def test_rank_requires_llm_check(self) -> None:
        """Same wiring rationale -- catches the "I forgot to set
        LLM_PROVIDER" failure before the LLM call burns 30s of latency."""
        assert preflight.check_llm_endpoint in preflight.STAGE_CHECKS["rank"]

    def test_summarise_requires_llm_check(self) -> None:
        assert preflight.check_llm_endpoint in preflight.STAGE_CHECKS["summarise"]
    # `test_fetch_requires_no_checks` and `test_render_requires_no_checks`
    # cut: `test_returns_empty_for_stages_with_no_checks` above already
    # pins that ["fetch", "render"] yields zero checks via the public
    # entry point, which is the actual contract.


# ---------------------------------------------------------------------------
# format_results — human-readable terminal output.
# ---------------------------------------------------------------------------

class TestFormatResults:
    def test_empty_results_returns_placeholder(self) -> None:
        out = format_results([])
        assert "no checks" in out

    def test_pass_marker_in_output(self) -> None:
        out = format_results([CheckResult("LLM", True, detail="ok")])
        assert "[PASS]" in out
        assert "LLM" in out
        assert "ok" in out

    def test_fail_marker_in_output(self) -> None:
        out = format_results([CheckResult("LLM", False, detail="bad", hint="fix it")])
        assert "[FAIL]" in out
        assert "fix it" in out

    def test_hint_omitted_when_passing(self) -> None:
        out = format_results([CheckResult("LLM", True, hint="should not appear")])
        assert "should not appear" not in out
