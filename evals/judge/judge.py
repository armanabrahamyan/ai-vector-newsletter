"""
evals/judge/judge.py -- LLM-as-judge client for AI Vector eval harness (Phase C).

Independence contract
---------------------
This module is deliberately independent of src/rank.py::_llm_call_openai_compatible.
The judge wants its own narrow client so it stays decoupled from generation-provider
quirks. Coupling eval tooling to generation tooling is the path to evaluator-evaluatee
bias at the infrastructure level, not just the model level.

Judge model selection
---------------------
Reads EVAL_JUDGE_PROVIDER (default: anthropic) and EVAL_JUDGE_MODEL
(default: claude-opus-4-7). Temperature is fixed at 0.0 for stability.

Cache layer
-----------
File-per-entry JSON under evals/judge/cache/<sha256>.json.
Cache key: SHA-256(artifact_content_json + prompt_version).
Atomic write via .tmp + rename so a crash mid-write never leaves corrupt cache.
A change to prompt_version invalidates entries for that dimension only.

Retry policy
------------
Single retry on JSON parse failure (mirrors rank.py pattern). Second failure
marks the artifact 'error' and continues -- the harness never crashes a run
over a single judge failure.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
JUDGE_DIR = Path(__file__).resolve().parent
CACHE_DIR = JUDGE_DIR / "cache"
PROMPTS_DIR = JUDGE_DIR / "prompts"
EVALS_DIR = JUDGE_DIR.parent
RUBRIC_PATH = EVALS_DIR / "voice" / "rubric.yaml"

# ---------------------------------------------------------------------------
# Env-var defaults
# ---------------------------------------------------------------------------
_DEFAULT_PROVIDER = "anthropic"
_DEFAULT_MODEL = "claude-opus-4-7"
_DEFAULT_TIMEOUT = 60.0

# Models that reject temperature at the API level (populated at runtime).
_MODELS_REJECTING_TEMPERATURE: set[str] = set()


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_key(artifact_content_json: str, prompt_version: str) -> str:
    """SHA-256 of artifact content + prompt_version string."""
    raw = artifact_content_json + "|" + prompt_version
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def _cache_read(key: str) -> dict | None:
    """Return cached result dict or None on miss."""
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        # Corrupt cache entry -- treat as miss and let it be overwritten.
        return None


def _cache_write(key: str, result: dict) -> None:
    """Atomically write result dict to cache (tmp + rename)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(key)
    # Write to temp file in same dir, then rename (atomic on POSIX).
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=CACHE_DIR,
        suffix=".tmp",
        delete=False,
    ) as tmp:
        json.dump(result, tmp, indent=2)
        tmp_path = Path(tmp.name)
    tmp_path.rename(path)


def cleanup_stale(keep_days: int = 30) -> int:
    """
    Remove cache entries older than keep_days days.

    This is a MANUAL helper -- do not call it from eval runs.
    Invoke explicitly when you want to prune the cache:

        from evals.judge.judge import cleanup_stale
        cleanup_stale(keep_days=30)

    Returns the number of entries removed.
    """
    if not CACHE_DIR.exists():
        return 0
    cutoff = time.time() - (keep_days * 86400)
    removed = 0
    for entry in CACHE_DIR.glob("*.json"):
        if entry.stat().st_mtime < cutoff:
            entry.unlink(missing_ok=True)
            removed += 1
    _LOG.info("cleanup_stale: removed %d entries older than %d days", removed, keep_days)
    return removed


# ---------------------------------------------------------------------------
# Rubric loading (runtime, not inlined into prompt YAMLs)
# ---------------------------------------------------------------------------

def _load_rubric() -> dict:
    """Load evals/voice/rubric.yaml at runtime.

    The rubric is loaded each time a judge run starts (not per-call) so
    rubric re-anchoring doesn't require touching the prompt YAMLs. Callers
    pass the relevant rubric section into the prompt template.
    """
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required for the judge module. "
            "Install it: pip install pyyaml"
        ) from exc
    if not RUBRIC_PATH.exists():
        raise FileNotFoundError(
            f"Voice rubric not found at {RUBRIC_PATH}. "
            "Phase A must complete before Phase C runs."
        )
    with RUBRIC_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _rubric_section_text(rubric: dict, dimension: str) -> str:
    """
    Extract the rubric text for a given dimension/criterion name.

    For voice dimensions: returns anchors + description.
    For per-story criteria: returns sub_tests + description.
    Returns a compact YAML-ish text suitable for embedding in a prompt.
    """
    # Check voice_dimensions
    for vd in rubric.get("voice_dimensions", []):
        if vd.get("name") == dimension:
            lines = [f"DIMENSION: {dimension}", f"DESCRIPTION: {vd.get('description', '').strip()}"]
            anchors = vd.get("anchors", {})
            if anchors:
                lines.append("ANCHORS (0=worst, 100=best):")
                for score_key in sorted(anchors.keys(), key=int):
                    lines.append(f"  {score_key}: {anchors[score_key].strip()}")
            return "\n".join(lines)

    # Check per_story_criteria
    for pc in rubric.get("per_story_criteria", []):
        if pc.get("name") == dimension:
            lines = [f"CRITERION: {dimension}", f"DESCRIPTION: {pc.get('description', '').strip()}"]
            sub_tests = pc.get("sub_tests", {})
            if sub_tests:
                lines.append("SUB-TESTS:")
                for st_name, st_body in sub_tests.items():
                    if isinstance(st_body, dict):
                        check = st_body.get("check", "").strip()
                        lines.append(f"  {st_name}: {check}")
                        for key in ("pass_exemplar", "pass_exemplar_2", "fail_exemplar", "fail_exemplar_class"):
                            if key in st_body:
                                lines.append(f"    {key}: {st_body[key].strip()}")
                    else:
                        lines.append(f"  {st_name}: {str(st_body).strip()}")
            return "\n".join(lines)

    return f"DIMENSION/CRITERION: {dimension}\n(No rubric anchors found for this dimension.)"


# ---------------------------------------------------------------------------
# Prompt YAML loading
# ---------------------------------------------------------------------------

# Mapping from rubric dimension names to prompt file names.
# The rubric uses descriptive names (headline_quality, summary_quality, etc.)
# while the prompt files use shorter names (headline, summary, etc.).
_DIMENSION_TO_PROMPT_FILE: dict[str, str] = {
    "voice": "voice",
    "headline_quality": "headline",
    "summary_quality": "summary",
    "signal_appropriateness": "signal",
    "section_intro_quality": "intro",
}

# Max tokens per dimension. Voice judges a full issue (5 dimensions +
# per-dimension rationale) so it needs substantially more tokens.
_DIMENSION_MAX_TOKENS: dict[str, int] = {
    "voice": 1800,
    "headline_quality": 512,
    "summary_quality": 512,
    "signal_appropriateness": 512,
    "section_intro_quality": 512,
}


def _load_prompt(dimension: str) -> dict:
    """Load the prompt YAML for a given dimension name.

    Handles the mapping from rubric dimension names (headline_quality,
    summary_quality, etc.) to prompt file names (headline, summary, etc.)
    via _DIMENSION_TO_PROMPT_FILE.
    """
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for the judge module.") from exc
    file_name = _DIMENSION_TO_PROMPT_FILE.get(dimension, dimension)
    prompt_path = PROMPTS_DIR / f"{file_name}.yaml"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Judge prompt not found: {prompt_path}")
    with prompt_path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# ---------------------------------------------------------------------------
# Anthropic client call
# ---------------------------------------------------------------------------

def _call_anthropic(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str,
    timeout: float,
    max_tokens: int = 512,
) -> str:
    """Make a single Anthropic API call. Returns raw text response."""
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "anthropic SDK is required for the judge module. "
            "Install it: pip install anthropic"
        ) from exc

    api_key = os.getenv("LLM_API_KEY") or None
    base_url = os.getenv("LLM_ENDPOINT") or None

    client_kwargs: dict[str, Any] = {"timeout": timeout}
    if api_key:
        client_kwargs["api_key"] = api_key
    if base_url:
        client_kwargs["base_url"] = base_url

    client = anthropic.Anthropic(**client_kwargs)

    create_kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }

    # Temperature 0.0 for judge stability. Some Claude 4.x models reject
    # the temperature parameter -- we catch and retry without it (same
    # pattern as rank.py::_llm_call_anthropic).
    if model not in _MODELS_REJECTING_TEMPERATURE:
        create_kwargs["temperature"] = 0.0

    try:
        resp = client.messages.create(**create_kwargs)
    except anthropic.BadRequestError as exc:
        msg = str(exc).lower()
        if "temperature" in msg and ("deprecated" in msg or "not supported" in msg):
            _MODELS_REJECTING_TEMPERATURE.add(model)
            create_kwargs.pop("temperature", None)
            resp = client.messages.create(**create_kwargs)
        else:
            raise

    chunks: list[str] = []
    for block in resp.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks)


# ---------------------------------------------------------------------------
# JSON parse with single retry
# ---------------------------------------------------------------------------

def _parse_judge_json(raw: str) -> dict:
    """
    Extract a JSON object from the model's response.

    The model sometimes wraps the JSON in markdown code fences. We strip
    those before parsing. Raises ValueError on failure so the caller can
    retry once.
    """
    text = raw.strip()
    # Strip markdown code fences if present.
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove first and last fence lines.
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# Core judge call (with cache + retry)
# ---------------------------------------------------------------------------

def judge_artifact(
    dimension: str,
    artifact_type: str,
    artifact_content: dict,
    *,
    rubric: dict | None = None,
    verbose: bool = False,
) -> dict:
    """
    Judge a single artifact on a given dimension.

    Args:
        dimension: One of voice | headline | summary | signal | intro.
        artifact_type: Human-readable label used in the prompt template
            (e.g. "story headline+summary+signal", "section intro",
            "full issue").
        artifact_content: The artifact data dict; serialised as JSON for
            the prompt and as the cache key basis.
        rubric: Pre-loaded rubric dict (load once per run, pass in).
            Loaded from disk if not provided.
        verbose: Log cache hit/miss per artifact.

    Returns:
        dict with keys: score, rationale, anchor_matched, prompt_version,
        judge_model, timestamp, cache_hit, dimension.
        On error (both retries failed): adds error key with message, sets
        score to "error".
    """
    if rubric is None:
        rubric = _load_rubric()

    # Load prompt YAML for this dimension.
    prompt_data = _load_prompt(dimension)
    prompt_version = prompt_data.get("prompt_version", "v1")
    system_prompt = prompt_data.get("system_prompt", "").strip()
    user_prompt_template = prompt_data.get("user_prompt_template", "").strip()

    # Extract rubric anchors for this dimension at runtime.
    rubric_anchors = _rubric_section_text(rubric, dimension)

    # Serialise artifact as compact JSON for the cache key and prompt.
    artifact_json = json.dumps(artifact_content, ensure_ascii=False, separators=(",", ":"))

    # Compute cache key.
    key = _cache_key(artifact_json, prompt_version)

    # Cache hit?
    cached = _cache_read(key)
    if cached is not None:
        if verbose:
            _LOG.debug("judge cache HIT: dimension=%s key=%s", dimension, key[:12])
        result = dict(cached)
        result["cache_hit"] = True
        return result

    if verbose:
        _LOG.debug("judge cache MISS: dimension=%s key=%s", dimension, key[:12])

    # Build prompt.
    user_prompt = user_prompt_template.format(
        artifact_type=artifact_type,
        artifact_json=artifact_json,
        rubric_anchors_for_this_dimension=rubric_anchors,
    )

    model = os.getenv("EVAL_JUDGE_MODEL", _DEFAULT_MODEL).strip()
    timeout = float(os.getenv("EVAL_JUDGE_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT)))
    max_tokens = _DIMENSION_MAX_TOKENS.get(dimension, 512)

    # Single retry on JSON parse failure.
    last_error: str | None = None
    for attempt in range(2):
        try:
            raw = _call_anthropic(
                system_prompt, user_prompt,
                model=model, timeout=timeout, max_tokens=max_tokens,
            )
            parsed = _parse_judge_json(raw)
            # Validate expected fields are present.
            score = parsed.get("score", "")
            if score not in ("pass", "borderline", "fail", "not_applicable"):
                raise ValueError(
                    f"Invalid score value {score!r}; expected pass/borderline/fail/not_applicable"
                )
            result: dict[str, Any] = {
                "score": score,
                "rationale": parsed.get("rationale", ""),
                "anchor_matched": parsed.get("anchor_matched", ""),
                "prompt_version": prompt_version,
                "judge_model": model,
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "dimension": dimension,
            }
            # Cache the successful result.
            _cache_write(key, result)
            result["cache_hit"] = False
            return result
        except (ValueError, json.JSONDecodeError, KeyError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            _LOG.warning(
                "judge parse failure (attempt %d/2): dimension=%s error=%s",
                attempt + 1, dimension, last_error,
            )
            if attempt == 0:
                # Brief pause before retry.
                time.sleep(0.5)
        except Exception as exc:  # noqa: BLE001
            # Non-JSON errors (network, auth, etc.) -- don't retry.
            last_error = f"{type(exc).__name__}: {exc}"
            _LOG.error(
                "judge call failed: dimension=%s error=%s", dimension, last_error
            )
            break

    # Both retries exhausted -- return error record (do not crash the run).
    _LOG.error(
        "judge returning error for dimension=%s after 2 attempts: %s",
        dimension, last_error,
    )
    return {
        "score": "error",
        "rationale": f"Judge failed after 2 attempts: {last_error}",
        "anchor_matched": "",
        "prompt_version": prompt_version,
        "judge_model": model,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dimension": dimension,
        "error": last_error,
        "cache_hit": False,
    }


# ---------------------------------------------------------------------------
# Count helpers (for reporting)
# ---------------------------------------------------------------------------

def cache_entry_count() -> int:
    """Return the number of entries currently in the cache directory."""
    if not CACHE_DIR.exists():
        return 0
    return sum(1 for f in CACHE_DIR.iterdir() if f.suffix == ".json" and f.name != ".gitkeep")
