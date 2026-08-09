"""
evals/review_cache -- result cache for reviewer-based eval runs (Eval 9 and
any future fixture-replay eval).

STATUS: PROPOSAL (2026-08-09), pending ratification. Built after the
2026-08-09 incident: an Eval 9 run died at 118/126 reviewer calls on
exhausted API credits, and the rerun re-paid for every one of the 118
calls that had already succeeded. Fixtures are frozen files; re-buying an
identical LLM verdict on an identical fixture under an identical
configuration is pure waste.

Modelled on evals/judge/judge.py's cache (file-per-entry JSON, SHA-256
key, atomic tmp+rename writes, corrupt entry == miss), with one critical
difference in WHAT may be cached -- see "The stability carve-out" below.

Two stores, deliberately separate
---------------------------------
1. ``cache/`` -- config-keyed PRIMARY result cache.
   Key: SHA-256(canonical fixture JSON | REVIEW_PROMPT_VERSION |
   review_thresholds version | model id | temperature). Stores the parsed
   reviewer result: verdict + findings + metadata. A change to ANY key
   component invalidates naturally (new key, old entry never matches).
   Read ONLY by the calibration-only path of eval_reviewer_gate
   (rerun_n == 1). Written by every path (fresh results may always seed
   the cache; reads are what the carve-out restricts).

2. ``runs/<run_key>/`` -- run-scoped stability journal.
   run_key = SHA-256(config fingerprint | rerun_n | UTC date)[:16].
   Each completed fixture's FULL fresh verdict list is written here the
   moment its rerun set finishes, so a run killed at call N resumes --
   same config, same rerun_n, same day -- by replaying its own already-
   paid-for fresh samples and only re-buying the incomplete fixtures.
   This is the incident's fix for the default rerun_n=3 run shape.
   A journal entry embeds the fixture's full config-keyed cache key, so
   any config or fixture-content change invalidates it naturally too.

The stability carve-out (structural, not a flag)
------------------------------------------------
gate_stability measures FRESH-CALL variance: does the reviewer give the
same answer when asked again? Serving a config-cached verdict as a
stability sample would report fake perfect stability -- the exact FM-06
canary this metric exists to catch. Therefore:

- The stability sample collector in run_evals.py
  (``_fresh_reviewer_verdicts``) takes only (reviewer, payload, n) and
  has no access to this module. It cannot read the cache; there is no
  flag to forget.
- The config-keyed cache is READ only when rerun_n == 1, where no
  stability is measured (gate_stability is None there).
- The runs/ journal is not a counter-example: it replays a complete,
  genuinely-fresh sample list recorded by the same logical run (same
  config, same rerun_n, same UTC day). It never synthesises agreement
  from a single cached verdict. A deliberate fresh audit bypasses it via
  ``stability_resume=False`` (or a different rerun_n, or the next day).

Failure results are never persisted
-----------------------------------
Only verdicts in {green, amber, red} are written to either store.
``unavailable`` (transport/credit failure) and ``unparseable`` (model
output failure) are transient states, not judgments -- caching them
would freeze an outage into the eval's answer. The incident's own
failure mode (exhausted credits) must never become a cache entry.

Gitignore
---------
The judge cache is ignored via the ROOT .gitignore (lines
``evals/judge/cache/*`` + ``!evals/judge/cache/.gitkeep``). The root
file is outside the Eval Engineer's write scope, so this package matches
the effect with directory-local .gitignore files in ``cache/`` and
``runs/`` (ignore everything, keep the .gitignore itself). Entries never
reach git either way.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths (module-level so tests can monkeypatch them, mirroring judge.py)
# ---------------------------------------------------------------------------
PACKAGE_DIR = Path(__file__).resolve().parent
CACHE_DIR = PACKAGE_DIR / "cache"
RUNS_DIR = PACKAGE_DIR / "runs"

# The only verdicts that are real reviewer judgments. Everything else
# (unavailable, unparseable, or garbage) is a failure state and is never
# persisted to either store.
CACHEABLE_VERDICTS = frozenset({"green", "amber", "red"})

_GITIGNORE_BODY = "*\n!.gitignore\n"


def _ensure_store(directory: Path) -> None:
    """Create *directory* (and its ignore file) if missing."""
    directory.mkdir(parents=True, exist_ok=True)
    gi = directory / ".gitignore"
    if not gi.exists():
        gi.write_text(_GITIGNORE_BODY, encoding="utf-8")


# ---------------------------------------------------------------------------
# Keying
# ---------------------------------------------------------------------------

def canonical_payload_json(payload: dict) -> str:
    """Canonical JSON for keying: sorted keys, compact separators.

    sort_keys=True (unlike the judge cache) so a key-order-only rewrite of
    a frozen fixture file does not invalidate its entries.
    """
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def context_fingerprint(context: dict) -> str:
    """Stable string of the four config key components.

    Components (task-ratified key design): review prompt version,
    review_thresholds table version, model id, temperature. Any change to
    any component changes every key derived from it.
    """
    return "|".join(
        [
            f"prompt_version={context.get('prompt_version', '')}",
            f"thresholds_version={context.get('thresholds_version', '')}",
            f"model={context.get('model', '')}",
            f"temperature={context.get('temperature', '')}",
        ]
    )


def cache_key(payload: dict, context: dict) -> str:
    """SHA-256 over canonical fixture content + config fingerprint."""
    raw = canonical_payload_json(payload) + "|" + context_fingerprint(context)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def resolve_context() -> Optional[dict]:
    """Derive the live key components from src.review (read-only import).

    Returns None -- which DISABLES caching, never mis-keys it -- when any
    component cannot be positively resolved (src.review unimportable,
    threshold table unloadable, model unresolved because neither
    REVIEW_MODEL nor LLM_MODEL is set). Fail closed: no key, no cache.
    """
    try:
        from src import review as _review
    except ImportError:
        return None
    try:
        prompt_version = str(getattr(_review, "REVIEW_PROMPT_VERSION", "") or "")
        thresholds_version = str(_review.load_thresholds().get("version", "") or "")
        model = str(_review._resolve_review_model() or "")
        temperature = getattr(_review, "_REVIEW_TEMPERATURE", None)
    except Exception:  # noqa: BLE001 -- any resolution failure disables caching
        return None
    if not prompt_version or not thresholds_version:
        return None
    if not model or model == "unknown":
        return None
    if temperature is None:
        return None
    return {
        "prompt_version": prompt_version,
        "thresholds_version": thresholds_version,
        "model": model,
        "temperature": temperature,
    }


# ---------------------------------------------------------------------------
# Primary (config-keyed) store
# ---------------------------------------------------------------------------

def _entry_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def _atomic_write_json(path: Path, doc: dict) -> None:
    """tmp + rename in the target dir, mirroring judge.py's crash safety."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        suffix=".tmp",
        delete=False,
    ) as tmp:
        json.dump(doc, tmp, indent=2, ensure_ascii=False)
        tmp_path = Path(tmp.name)
    tmp_path.rename(path)


def read(key: str) -> Optional[dict]:
    """Cached primary result dict, or None on miss / corrupt entry."""
    path = _entry_path(key)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None  # corrupt entry == miss; next write overwrites it
    # Defensive: never serve a persisted failure state, whatever wrote it.
    if not isinstance(doc, dict) or doc.get("verdict") not in CACHEABLE_VERDICTS:
        return None
    return doc


def is_cacheable(result: Optional[dict]) -> bool:
    """True only for parsed reviewer results carrying a real verdict."""
    return isinstance(result, dict) and result.get("verdict") in CACHEABLE_VERDICTS


def write(key: str, result: dict) -> bool:
    """Persist a primary result. Refuses failure states. Returns True on write."""
    if not is_cacheable(result):
        return False
    _ensure_store(CACHE_DIR)
    doc = {
        "verdict": result.get("verdict"),
        "findings": list(result.get("findings") or []),
        "cached_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "context": result.get("context", {}),
    }
    _atomic_write_json(_entry_path(key), doc)
    return True


def entry_count() -> int:
    """Number of primary entries currently on disk."""
    if not CACHE_DIR.exists():
        return 0
    return sum(1 for f in CACHE_DIR.iterdir() if f.suffix == ".json")


# ---------------------------------------------------------------------------
# Run-scoped stability journal
# ---------------------------------------------------------------------------

def run_key(context: dict, rerun_n: int, run_date: Optional[str] = None) -> str:
    """Identity of one logical stability run: config + rerun_n + UTC date.

    Same-day crash + rerun resolves to the same key (auto-resume, the
    incident's fix). Tomorrow's run, a different rerun_n, or any config
    change resolves to a different key (fresh measurement).
    """
    if run_date is None:
        run_date = datetime.now(timezone.utc).date().isoformat()
    raw = context_fingerprint(context) + f"|rerun_n={rerun_n}|{run_date}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _run_entry_path(rk: str, case_id: str) -> Path:
    return RUNS_DIR / rk / f"{case_id}.json"


def run_entry_read(rk: str, case_id: str, *, expected_fixture_key: str) -> Optional[dict]:
    """A completed fixture's journal record for this run, or None.

    Validates the embedded fixture_key (content + config) and that every
    recorded verdict is a real judgment -- a mismatch or a failure verdict
    means the entry is ignored and the fixture runs fresh.
    """
    path = _run_entry_path(rk, case_id)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(doc, dict):
        return None
    if doc.get("fixture_key") != expected_fixture_key:
        return None
    verdicts = doc.get("verdicts")
    if not isinstance(verdicts, list) or not verdicts:
        return None
    if any(v not in CACHEABLE_VERDICTS for v in verdicts):
        return None
    return doc


def run_entry_write(
    rk: str,
    case_id: str,
    *,
    fixture_key: str,
    verdicts: list[str],
    primary_findings: list[dict],
) -> bool:
    """Journal a COMPLETED fixture (all rerun verdicts are real judgments).

    Called per-fixture the moment its rerun set finishes -- this is the
    partial-run resilience: a crash later in the run cannot lose it.
    Refuses incomplete/failed sets. Returns True on write.
    """
    if not verdicts or any(v not in CACHEABLE_VERDICTS for v in verdicts):
        return False
    _ensure_store(RUNS_DIR / rk)
    doc = {
        "fixture_key": fixture_key,
        "verdicts": list(verdicts),
        "primary_findings": list(primary_findings or []),
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    _atomic_write_json(_run_entry_path(rk, case_id), doc)
    return True


# ---------------------------------------------------------------------------
# Manual maintenance (mirrors judge.cleanup_stale -- never called by runs)
# ---------------------------------------------------------------------------

def cleanup_stale(keep_days: int = 30) -> int:
    """Remove primary entries and whole run journals older than keep_days.

    MANUAL helper -- do not call from eval runs. Returns entries removed.
    """
    cutoff = time.time() - (keep_days * 86400)
    removed = 0
    if CACHE_DIR.exists():
        for entry in CACHE_DIR.glob("*.json"):
            if entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
                removed += 1
    if RUNS_DIR.exists():
        for run_dir in RUNS_DIR.iterdir():
            if not run_dir.is_dir():
                continue
            for entry in run_dir.glob("*.json"):
                if entry.stat().st_mtime < cutoff:
                    entry.unlink(missing_ok=True)
                    removed += 1
            # Drop the dir once only the .gitignore remains.
            leftovers = [p for p in run_dir.iterdir() if p.name != ".gitignore"]
            if not leftovers:
                (run_dir / ".gitignore").unlink(missing_ok=True)
                run_dir.rmdir()
    _LOG.info("cleanup_stale: removed %d entries older than %d days", removed, keep_days)
    return removed
