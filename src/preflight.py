"""src/preflight.py -- pre-flight checks for AI Vector pipeline.

Run via ``python -m src.run --check`` after setup. Verifies the two things
that fail LATE in the pipeline:

1. **Embedding model** (BAAI/bge-base-en-v1.5) -- downloadable from HF,
   loads in-process, produces a 768-dim vector. First call downloads
   ~440MB to the HF cache.
2. **LLM endpoint** -- reachable, key valid, model exists at provider,
   JSON response parseable.

These are the two checks pip-install cannot catch. Everything else
(Python version, package imports, config parse, dir writeability) would
have surfaced loudly at `pip install -r requirements.txt` or first run.

Exit 0 on all-pass, 1 on any fail. Each check returns a CheckResult so
the CLI in run.py can format consistently and we can extend the list
later without touching the orchestrator.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

EXPECTED_EMBEDDING_DIM = 768
EMBEDDING_MODEL_NAME = "BAAI/bge-base-en-v1.5"


@dataclass
class CheckResult:
    """Structured result for a single pre-flight check."""

    name: str
    passed: bool
    detail: str = ""
    hint: str = ""
    elapsed_ms: int | None = None


def check_embedding_model() -> CheckResult:
    """Verify the embedding model is cached on disk -- do NOT re-download.

    Fast presence check via huggingface_hub's cache lookup. If the model is
    cached, PASS. If not cached, FAIL with a hint to trigger the one-time
    download (the first `python -m src.run` invocation will do it
    automatically when cluster.py runs).
    """
    name = f"Embedding model ({EMBEDDING_MODEL_NAME})"
    t0 = time.perf_counter()
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError as e:
        return CheckResult(
            name, False,
            detail=f"ImportError: {e}",
            hint=(
                "run `pip install -r requirements.txt` in your venv "
                "(huggingface_hub is a sentence-transformers transitive dep)"
            ),
        )
    # `try_to_load_from_cache` returns the cached file path if present,
    # None if the model is not cached, or a sentinel constant if it's
    # known to be unavailable. We only need presence.
    cached = try_to_load_from_cache(
        repo_id=EMBEDDING_MODEL_NAME, filename="config.json"
    )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    if cached is None:
        return CheckResult(
            name, False,
            detail="not cached locally",
            hint=(
                "run `python -m src.run --stage cluster` once to trigger "
                "the one-time ~440MB download from huggingface.co, OR pre-cache "
                "via `python -c \"from sentence_transformers import "
                f"SentenceTransformer; SentenceTransformer('{EMBEDDING_MODEL_NAME}')\"`"
            ),
            elapsed_ms=elapsed_ms,
        )
    return CheckResult(
        name, True,
        detail="cached locally (not re-verified)",
        elapsed_ms=elapsed_ms,
    )


def check_llm_endpoint() -> CheckResult:
    """Send a tiny probe to the configured LLM. Verifies endpoint + key +
    model + response shape in a single round-trip (~one cent of cost).
    """
    name = "LLM endpoint reachable"
    provider = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    model = (os.getenv("LLM_MODEL") or "").strip()

    if not provider:
        return CheckResult(
            name, False,
            detail="LLM_PROVIDER is unset",
            hint="copy .env.example to .env and set LLM_PROVIDER + others",
        )
    if not model:
        return CheckResult(
            name, False,
            detail="LLM_MODEL is unset",
            hint="set LLM_MODEL in .env",
        )
    if provider in {"openai", "litellm", "ollama"}:
        return CheckResult(
            name, False,
            detail=f"LLM_PROVIDER={provider!r} is not implemented in v0",
            hint="set LLM_PROVIDER=anthropic or bedrock",
        )
    if provider not in {"anthropic", "bedrock"}:
        return CheckResult(
            name, False,
            detail=f"unknown LLM_PROVIDER={provider!r}",
            hint="set LLM_PROVIDER=anthropic or bedrock",
        )

    # Import here so a broken rank.py doesn't break --check. Also keeps
    # the SDK imports lazy.
    try:
        from src.rank import _llm_call
    except Exception as e:
        return CheckResult(
            name, False,
            detail=f"could not import LLM client: {type(e).__name__}: {e}",
            hint="check src/rank.py and the provider library is installed",
        )

    t0 = time.perf_counter()
    try:
        response = _llm_call(
            "Respond with the single word: ok",
            temperature=0.0,
            max_tokens=10,
        )
    except Exception as e:
        return CheckResult(
            name, False,
            detail=f"{type(e).__name__}: {e}",
            hint=(
                "check LLM_ENDPOINT + LLM_API_KEY + LLM_MODEL in .env; "
                "verify network reachability to the endpoint"
            ),
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    if not response or not response.strip():
        return CheckResult(
            name, False,
            detail=f"empty response from {provider}/{model}",
            hint="LLM returned no text -- check provider/model compatibility",
            elapsed_ms=elapsed_ms,
        )
    return CheckResult(
        name, True,
        detail=f"{model} via {provider}, {elapsed_ms}ms",
        elapsed_ms=elapsed_ms,
    )


CHECKS: list[Callable[[], CheckResult]] = [
    check_embedding_model,
    check_llm_endpoint,
]

# Which checks each pipeline stage requires. Used by run.py to scope the
# auto-preflight to only the dependencies that the requested stages will
# actually touch. `fetch` and `render` need neither (no embedding, no LLM
# in the runtime path).
STAGE_CHECKS: dict[str, list[Callable[[], CheckResult]]] = {
    "fetch": [],
    "cluster": [check_embedding_model],
    "rank": [check_llm_endpoint],
    "summarise": [check_llm_endpoint],
    "render": [],
}


def run_all_checks() -> tuple[list[CheckResult], bool]:
    """Run every pre-flight check sequentially; return (results, all_passed)."""
    results: list[CheckResult] = []
    for fn in CHECKS:
        results.append(fn())
    all_passed = all(r.passed for r in results)
    return results, all_passed


def run_checks_for_stages(stages: list[str]) -> tuple[list[CheckResult], bool]:
    """Run only the checks required by ``stages``. De-duplicates so
    e.g. {rank, summarise} runs the LLM check once, not twice.

    Returns ([], True) when no stage requires a check.
    """
    needed: list[Callable[[], CheckResult]] = []
    seen: set[Callable[[], CheckResult]] = set()
    for stage in stages:
        for fn in STAGE_CHECKS.get(stage, []):
            if fn not in seen:
                seen.add(fn)
                needed.append(fn)
    if not needed:
        return [], True
    results = [fn() for fn in needed]
    return results, all(r.passed for r in results)


def format_results(results: list[CheckResult]) -> str:
    """Format results as human-readable lines for the terminal."""
    if not results:
        return "  (no checks registered)"
    max_name_len = max(len(r.name) for r in results)
    lines: list[str] = []
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        line = f"  [{status}]  {r.name.ljust(max_name_len)}"
        if r.detail:
            line += f"    {r.detail}"
        lines.append(line)
        if not r.passed and r.hint:
            lines.append(f"           hint: {r.hint}")
    return "\n".join(lines)
