"""
src/gate.py -- the unattended-publish gate.

Answers exactly one question for a given issue date: **may this issue be
published without a human in the loop?** The answer is written to
``data/staging/<date>/gate.json`` as a `GateDecision`, and the CI workflow
decides what to do with it according to the rollout phase.

Nothing in this module calls an LLM. The gate is deterministic code reading
artifacts that judgment already produced -- the editor's ``review.md`` and
the verifier's ``verify.json``. Per the No Token Wasted principle, asking a
model "should we publish?" when the inputs are two verdict tokens and a hash
comparison would be paying for non-determinism we do not want anywhere near
a publish decision.

Owner: Architect. The checks encode ratified policy (2026-08-02); changing
which checks block, or adding one, is a contract change -- bump
``GATE_VERSION`` and record the diff in ``docs/internal/DESIGN.md``.

Design invariants
-----------------

1. **Fail closed.** Every state the gate cannot positively confirm is a
   hold. A missing review, an unparseable verdict, a verify report that
   could not run, an issue.json that does not validate -- all hold. The
   gate never publishes on the absence of evidence.
2. **Hard blocks ignore the phase.** A contradicted fact-check claim, an
   unavailable verifier, and a stale review hold in every phase, including
   ``shadow``. The phase only narrows which *editorial* verdicts may
   auto-merge.
3. **The decision lives in the artifact, not the exit code.** ``aiv gate``
   exits 0 whether it decided ``auto_merge`` or ``hold``, so a non-zero
   exit unambiguously means "the gate itself failed to run" -- a state the
   workflow must also treat as a hold.
4. **Advisory stays advisory.** This module reads ``review.md`` and
   ``verify.json``; it does not change their contracts. Both remain
   non-blocking for a HUMAN-ratified ``aiv release``. The gate governs the
   unattended path only.

Audit tag: gate-v1-2026-08-02.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from src import paths
from src.models import (
    GateCheck,
    GateDecision,
    GateReviewState,
    GateVerifyState,
    Issue,
)

# ---------------------------------------------------------------------------
# Policy constants.
# ---------------------------------------------------------------------------

GATE_VERSION = "v1"
"""Version of the POLICY (which checks exist and which of them block).
Written into every `GateDecision`. Bump on any change to the check set,
their blocking flags, or the hold-reason vocabulary -- and record the diff
in DESIGN.md. Distinct from `GateDecision.schema_version`, which versions
the SHAPE."""

PHASE_ENV_VAR = "AIV_AUTO_PUBLISH_PHASE"
"""Repository variable / environment variable naming the rollout phase."""

DEFAULT_PHASE = "shadow"
"""Unset or unrecognised phase resolves here: compute the decision, act on
nothing. The safe default for a control that can publish unattended."""

VALID_PHASES: tuple[str, ...] = ("shadow", "green_only", "green_amber")

ACCEPTED_REVIEW_VERDICTS: dict[str, frozenset[str]] = {
    # Shadow evaluates against the END-STATE policy so the observation
    # period measures what we are rolling towards. A narrower phase's
    # answer is always derivable from the recorded review verdict.
    "shadow": frozenset({"green", "amber"}),
    "green_only": frozenset({"green"}),
    "green_amber": frozenset({"green", "amber"}),
}
"""Which editorial verdicts may auto-merge, per phase. ``red`` and
``unavailable`` are in no phase's set; neither is any token the gate does
not recognise."""

KNOWN_REVIEW_VERDICTS: frozenset[str] = frozenset(
    {"green", "amber", "red", "unavailable"}
)
"""The review.md frontmatter vocabulary (see DESIGN.md "review.md"). A
verdict outside this set means the artifact and the gate have drifted
apart -- hold, and let a human look."""


# Hold-reason vocabulary. Stable machine tokens: workflows, dashboards and
# postmortems key on these, so treat a rename as a contract change.
HOLD_ISSUE_MISSING = "hold:issue-missing"
HOLD_ISSUE_INVALID = "hold:issue-invalid"
HOLD_REVIEW_MISSING = "hold:review-missing"
HOLD_REVIEW_UNPARSEABLE = "hold:review-unparseable"
HOLD_REVIEW_UNAVAILABLE = "hold:review-unavailable"
HOLD_REVIEW_RED = "hold:review-verdict-red"
HOLD_REVIEW_AMBER = "hold:review-verdict-amber"
HOLD_REVIEW_UNKNOWN = "hold:review-verdict-unknown"
HOLD_STALE_REVIEW = "hold:stale-review"
HOLD_VERIFY_MISSING = "hold:verify-missing"
HOLD_VERIFY_UNPARSEABLE = "hold:verify-unparseable"
HOLD_VERIFY_UNAVAILABLE = "hold:verify-unavailable"
HOLD_CONTRADICTED_CLAIM = "hold:contradicted-claim"

_NOT_EVALUATED = "not evaluated -- "
"""Detail prefix for a check whose prerequisite already failed. Such a check
is recorded as ``passed=False, blocking=False``: it did not pass, but it
contributes no hold reason because the prerequisite check already holds the
release and naming the same problem twice makes ``hold_reasons`` noisier
without making it truer."""

_LOG = logging.getLogger("ai_vector.gate")


# ---------------------------------------------------------------------------
# Paths.
#
# NOTE: these two helpers live here rather than in ``src/paths.py`` only
# because gate.py landed while paths.py was under concurrent edit. Fold them
# into paths.py alongside ``verify_path`` when convenient -- the signature is
# deliberately identical to the helpers there.
# ---------------------------------------------------------------------------

def gate_path(date: _dt.date, *, canonical: bool) -> Path:
    """Path to ``gate.json`` for the given date and archive state."""
    base = paths.released_dir(date) if canonical else paths.staging_dir(date)
    return base / "gate.json"


def review_md_path(date: _dt.date, *, canonical: bool = False) -> Path:
    """Path to ``review.md`` for the given date and archive state."""
    base = paths.released_dir(date) if canonical else paths.staging_dir(date)
    return base / "review.md"


# ---------------------------------------------------------------------------
# Freshness: the issue hash contract.
# ---------------------------------------------------------------------------

def sha256_of_file(path: Path) -> str | None:
    """SHA-256 hex digest of a file's bytes, or ``None`` if unreadable.

    Hashing the bytes on disk -- not a canonical re-serialisation of the
    parsed object -- is deliberate. The question the freshness check asks is
    "did the editor read THIS file?", and any edit at all (a hand-tweaked
    headline, a re-run of summarise, verify's denormalisation pass) changes
    the bytes. A canonicalising hash would let a semantically-identical
    rewrite pass as fresh, which is fine, and would let a whitespace-only
    hand edit pass too -- but it would also require both writers to agree on
    a canonicalisation, which is a second contract to keep in sync for no
    gain.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def issue_sha256(date: _dt.date, *, canonical: bool = False) -> str | None:
    """SHA-256 of the staged (or released) ``issue.json`` bytes.

    The reader half of the freshness contract. ``src/review.py`` writes the
    same digest into the ``issue_sha256`` frontmatter key, hashing the bytes
    in the same operation as its read -- deliberately NOT by calling this
    function, because re-reading the file to hash it would leave a window in
    which it could change between the read and the hash, which is exactly
    the staleness the check exists to catch.

    The call sites differ; the DEFINITION is what must not drift: the digest
    is over the raw bytes on disk, never a canonical re-serialisation. See
    DESIGN.md "review.md -> issue_sha256".
    """
    return sha256_of_file(paths.issue_path(date, canonical=canonical))


# ---------------------------------------------------------------------------
# Phase resolution.
# ---------------------------------------------------------------------------

def resolve_phase(explicit: str | None = None) -> tuple[str, str]:
    """Resolve the rollout phase. Returns ``(phase, note)``.

    Precedence: the ``explicit`` argument (test / CLI override), then the
    ``AIV_AUTO_PUBLISH_PHASE`` environment variable, then `DEFAULT_PHASE`.
    An unrecognised value falls back to ``shadow`` and says so in ``note``
    -- a typo'd repo variable must never silently widen the gate.
    """
    raw = explicit if explicit is not None else os.getenv(PHASE_ENV_VAR)
    if raw is None or not raw.strip():
        return DEFAULT_PHASE, ""
    value = raw.strip().lower()
    if value in VALID_PHASES:
        return value, ""
    note = (
        f"unrecognised {PHASE_ENV_VAR}={raw!r}; falling back to "
        f"{DEFAULT_PHASE!r} (valid: {', '.join(VALID_PHASES)})"
    )
    _LOG.warning("gate: %s", note)
    return DEFAULT_PHASE, note


# ---------------------------------------------------------------------------
# Artifact readers. Each returns plain data + a status token; none of them
# raise. A reader that raised would turn "the gate holds" into "the gate
# crashed", and the workflow would have to guess which happened.
# ---------------------------------------------------------------------------

def _read_frontmatter(path: Path) -> tuple[dict[str, str] | None, str]:
    """Parse the YAML frontmatter block of a Markdown file.

    Returns ``(mapping, status)`` where status is ``"ok"``, ``"missing"``
    (no such file / unreadable), or ``"unparseable"`` (no delimited
    frontmatter block, or a block with no key/value lines).

    We parse the block ourselves rather than importing review.py's private
    helper: modules talk to each other through artifacts, not internals
    (DESIGN.md "Module boundaries -> Seam rules"). The frontmatter contract
    is flat ``key: value`` scalars, documented in DESIGN.md "review.md", so
    this stays a dozen lines and no YAML dependency is needed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, "missing"

    stripped = text.lstrip()
    # Tolerate a code fence wrapped around the whole document.
    if stripped.startswith("```"):
        newline = stripped.find("\n")
        if newline != -1:
            stripped = stripped[newline + 1:]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rsplit("```", 1)[0]
    if not stripped.startswith("---"):
        return None, "unparseable"
    rest = stripped[3:]
    if rest.startswith("\n"):
        rest = rest[1:]
    end = rest.find("\n---")
    if end == -1:
        return None, "unparseable"

    mapping: dict[str, str] = {}
    for line in rest[:end].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        mapping[key.strip().lower()] = value.strip().strip('"').strip("'")
    if not mapping:
        return None, "unparseable"
    return mapping, "ok"


def _read_verify(path: Path) -> tuple[dict[str, Any] | None, str]:
    """Read ``verify.json`` as a plain mapping.

    Returns ``(payload, status)`` with status ``"ok"``, ``"missing"``, or
    ``"unparseable"``. Deliberately NOT pydantic-validated: the gate needs
    two facts out of this file (the rollup verdict and which stories carry a
    contradicted claim), and holding the whole publication because an
    unrelated field drifted would be the gate failing at its own job. JSON
    that is not an object, or an object with no recognisable ``verdict``,
    counts as unparseable and holds.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None, "missing"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, "unparseable"
    if not isinstance(payload, dict):
        return None, "unparseable"
    verdict = payload.get("verdict")
    if not isinstance(verdict, str) or not verdict.strip():
        return None, "unparseable"
    return payload, "ok"


def _claim_scan(stories: list[Any]) -> tuple[set[str], set[str], int]:
    """Scan `StoryVerification`-shaped mappings for flagged claims.

    Returns ``(contradicted_ids, unsupported_ids, story_count)``. Reads both
    the rollup booleans (``has_contradiction`` / ``has_unsupported``) and the
    per-claim verdicts, and treats EITHER as sufficient evidence. The model
    validator keeps the two consistent on anything we wrote ourselves; the
    belt-and-braces read means a hand-edited or partially-written record
    still trips the block rather than sliding through.
    """
    contradicted: set[str] = set()
    unsupported: set[str] = set()
    count = 0
    for story in stories:
        if not isinstance(story, dict):
            continue
        count += 1
        story_id = story.get("story_id")
        if not isinstance(story_id, str) or not story_id:
            story_id = "<unknown-story-id>"
        if story.get("has_contradiction") is True:
            contradicted.add(story_id)
        if story.get("has_unsupported") is True:
            unsupported.add(story_id)
        for claim in story.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            verdict = claim.get("verdict")
            if verdict == "contradicted":
                contradicted.add(story_id)
            elif verdict == "unsupported":
                unsupported.add(story_id)
    return contradicted, unsupported, count


def _issue_verification_blocks(issue: Issue) -> list[dict[str, Any]]:
    """Pull the denormalised ``SummaryBlock.verification`` records out of a
    validated `Issue`, as plain mappings for `_claim_scan`.

    The gate reads BOTH ``verify.json`` and these denormalised copies and
    takes the union. They should always agree -- verify.py writes both in the
    same pass -- but a contradiction recorded in either place is evidence
    that a contradiction was found, and this gate's job is to not publish
    over that evidence.
    """
    out: list[dict[str, Any]] = []
    sections = [issue.pulse, *issue.sections]
    for section in sections:
        for block in section.stories:
            if block.verification is not None:
                out.append(block.verification.model_dump(mode="json"))
    return out


# ---------------------------------------------------------------------------
# The decision.
# ---------------------------------------------------------------------------

def decide(date: _dt.date, *, phase: str | None = None) -> GateDecision:
    """Compute the unattended-publish decision for ``date``. Pure function.

    Reads ``data/staging/<date>/{issue.json, review.md, verify.json}``,
    evaluates the policy checks in order, and returns a `GateDecision`.
    Writes nothing -- use `write_decision` (or `run_gate`) to persist.

    The checks, in evaluation order:

    ``issue_readable`` (blocking)
        ``issue.json`` exists and validates against the `Issue` contract.
        Everything downstream is a statement about this file; if we cannot
        read it, we have nothing to publish unattended.
    ``review_present`` (blocking)
        ``review.md`` exists.
    ``review_verdict`` (blocking)
        The frontmatter verdict is in the accepted set for the active phase.
        Missing frontmatter, ``unavailable``, ``red``, and any unrecognised
        token all hold.
    ``review_freshness`` (blocking)
        The frontmatter ``issue_sha256`` matches a fresh hash of the current
        ``issue.json``. A review of a superseded draft is not evidence about
        the draft we are publishing. An absent key holds too -- we cannot
        confirm what the editor read.
    ``verify_readable`` (blocking)
        ``verify.json`` exists and parses.
    ``verify_available`` (blocking)
        The verify verdict is not ``unavailable``. Ratified 2026-08-02: an
        issue that was never fact-checked does not go out unattended.
    ``no_contradicted_claims`` (blocking)
        No story carries a ``contradicted`` claim, in ``verify.json`` or in
        the denormalised copies inside ``issue.json``. Ratified 2026-08-02:
        this blocks regardless of the editorial verdict -- a green review on
        a story the verifier says the source contradicts means the two
        judges disagree, and that is precisely when a human should look.
    ``verify_no_unsupported`` (NON-blocking)
        Informational. "The source does not assert this" is an editorial
        concern worth surfacing, not a factual error worth blocking on.
    """
    active_phase, phase_note = resolve_phase(phase)
    accepted = ACCEPTED_REVIEW_VERDICTS[active_phase]
    checks: list[GateCheck] = []

    # --- issue.json -------------------------------------------------------
    issue_file = paths.issue_path(date, canonical=False)
    issue: Issue | None = None
    if not issue_file.exists():
        checks.append(GateCheck(
            name="issue_readable", passed=False, blocking=True,
            detail=f"no staged issue.json at {issue_file}",
            hold_reason=HOLD_ISSUE_MISSING,
        ))
    else:
        try:
            issue = Issue.model_validate_json(
                issue_file.read_text(encoding="utf-8")
            )
        except Exception as exc:  # noqa: BLE001 -- any failure is a hold
            checks.append(GateCheck(
                name="issue_readable", passed=False, blocking=True,
                detail=f"issue.json did not validate: {type(exc).__name__}: {exc}",
                hold_reason=HOLD_ISSUE_INVALID,
            ))
        else:
            story_count = 1 + sum(len(s.stories) for s in issue.sections)
            checks.append(GateCheck(
                name="issue_readable", passed=True, blocking=True,
                detail=f"issue.json validates ({story_count} stories)",
            ))

    computed_hash = sha256_of_file(issue_file)

    # --- review.md --------------------------------------------------------
    review_file = review_md_path(date)
    frontmatter, review_status = _read_frontmatter(review_file)
    review_verdict: str | None = None
    recorded_hash: str | None = None
    review_prompt_version: str | None = None
    if frontmatter is not None:
        raw_verdict = frontmatter.get("verdict", "").strip().lower()
        review_verdict = raw_verdict or None
        recorded_hash = frontmatter.get("issue_sha256") or None
        review_prompt_version = frontmatter.get("prompt_version") or None

    review_present = review_status != "missing"
    checks.append(GateCheck(
        name="review_present", passed=review_present, blocking=True,
        detail=(
            f"review.md found at {review_file}" if review_present
            else f"no review.md at {review_file}"
        ),
        hold_reason=None if review_present else HOLD_REVIEW_MISSING,
    ))

    if not review_present:
        checks.append(GateCheck(
            name="review_verdict", passed=False, blocking=False,
            detail=f"{_NOT_EVALUATED}review.md is missing",
        ))
    elif review_status == "unparseable" or not review_verdict:
        checks.append(GateCheck(
            name="review_verdict", passed=False, blocking=True,
            detail="review.md carries no parseable frontmatter verdict",
            hold_reason=HOLD_REVIEW_UNPARSEABLE,
        ))
    elif review_verdict in accepted:
        checks.append(GateCheck(
            name="review_verdict", passed=True, blocking=True,
            detail=(
                f"review verdict {review_verdict!r} is accepted in phase "
                f"{active_phase!r} (accepted: {', '.join(sorted(accepted))})"
            ),
        ))
    else:
        checks.append(GateCheck(
            name="review_verdict", passed=False, blocking=True,
            detail=(
                f"review verdict {review_verdict!r} is not accepted in phase "
                f"{active_phase!r} (accepted: {', '.join(sorted(accepted))})"
            ),
            hold_reason=_verdict_hold_reason(review_verdict),
        ))

    # --- review freshness -------------------------------------------------
    fresh = bool(recorded_hash) and recorded_hash == computed_hash
    if not review_present or frontmatter is None:
        checks.append(GateCheck(
            name="review_freshness", passed=False, blocking=False,
            detail=f"{_NOT_EVALUATED}no review frontmatter to read a hash from",
        ))
    elif computed_hash is None:
        checks.append(GateCheck(
            name="review_freshness", passed=False, blocking=False,
            detail=f"{_NOT_EVALUATED}issue.json could not be hashed",
        ))
    elif recorded_hash is None:
        checks.append(GateCheck(
            name="review_freshness", passed=False, blocking=True,
            detail=(
                "review.md frontmatter carries no issue_sha256; cannot confirm "
                "the review read the issue we are about to publish"
            ),
            hold_reason=HOLD_STALE_REVIEW,
        ))
    elif not fresh:
        checks.append(GateCheck(
            name="review_freshness", passed=False, blocking=True,
            detail=(
                f"review.md was written against issue_sha256="
                f"{recorded_hash[:12]}... but issue.json now hashes to "
                f"{computed_hash[:12]}...; the issue changed after the review"
            ),
            hold_reason=HOLD_STALE_REVIEW,
        ))
    else:
        checks.append(GateCheck(
            name="review_freshness", passed=True, blocking=True,
            detail=f"review matches current issue.json ({computed_hash[:12]}...)",
        ))

    # --- verify.json ------------------------------------------------------
    verify_file = paths.verify_path(date, canonical=False)
    verify_payload, verify_status = _read_verify(verify_file)
    verify_verdict: str | None = None
    if verify_payload is not None:
        raw = verify_payload.get("verdict")
        verify_verdict = raw.strip().lower() if isinstance(raw, str) else None

    if verify_status == "missing":
        checks.append(GateCheck(
            name="verify_readable", passed=False, blocking=True,
            detail=f"no verify.json at {verify_file}",
            hold_reason=HOLD_VERIFY_MISSING,
        ))
    elif verify_status == "unparseable":
        checks.append(GateCheck(
            name="verify_readable", passed=False, blocking=True,
            detail=f"verify.json at {verify_file} is not a readable report",
            hold_reason=HOLD_VERIFY_UNPARSEABLE,
        ))
    else:
        checks.append(GateCheck(
            name="verify_readable", passed=True, blocking=True,
            detail=f"verify.json parsed (verdict={verify_verdict!r})",
        ))

    if verify_status != "ok":
        checks.append(GateCheck(
            name="verify_available", passed=False, blocking=False,
            detail=f"{_NOT_EVALUATED}verify.json could not be read",
        ))
    elif verify_verdict == "unavailable":
        checks.append(GateCheck(
            name="verify_available", passed=False, blocking=True,
            detail=(
                "verify verdict is 'unavailable' -- the factual check did not "
                "run for this issue"
            ),
            hold_reason=HOLD_VERIFY_UNAVAILABLE,
        ))
    else:
        checks.append(GateCheck(
            name="verify_available", passed=True, blocking=True,
            detail=f"verify ran (verdict={verify_verdict!r})",
        ))

    # --- contradiction scan (union of both evidence sources) --------------
    contradicted: set[str] = set()
    unsupported: set[str] = set()
    stories_verified = 0
    if verify_payload is not None:
        stories = verify_payload.get("stories")
        if isinstance(stories, list):
            c, u, stories_verified = _claim_scan(stories)
            contradicted |= c
            unsupported |= u
    if issue is not None:
        c, u, _ = _claim_scan(_issue_verification_blocks(issue))
        contradicted |= c
        unsupported |= u

    if contradicted:
        checks.append(GateCheck(
            name="no_contradicted_claims", passed=False, blocking=True,
            detail=(
                f"{len(contradicted)} story/stories carry a contradicted claim: "
                f"{', '.join(sorted(contradicted))}"
            ),
            hold_reason=HOLD_CONTRADICTED_CLAIM,
        ))
    elif verify_payload is None and issue is None:
        # Neither evidence source was readable. Recording this as a clean
        # pass would read as an all-clear in the audit trail when what
        # actually happened is that we had nothing to look at. The
        # verify_readable / issue_readable checks already hold the release.
        checks.append(GateCheck(
            name="no_contradicted_claims", passed=True, blocking=True,
            detail="no evidence available to scan (see the readability checks)",
        ))
    else:
        checks.append(GateCheck(
            name="no_contradicted_claims", passed=True, blocking=True,
            detail=f"no contradicted claims across {stories_verified} verified stories",
        ))

    checks.append(GateCheck(
        name="verify_no_unsupported", passed=not unsupported, blocking=False,
        detail=(
            f"{len(unsupported)} story/stories carry an unsupported claim "
            f"(advisory, not blocking): {', '.join(sorted(unsupported))}"
            if unsupported else "no unsupported claims"
        ),
    ))

    # --- roll up ----------------------------------------------------------
    hold_reasons: list[str] = []
    for check in checks:
        if check.passed or not check.blocking or not check.hold_reason:
            continue
        if check.hold_reason not in hold_reasons:
            hold_reasons.append(check.hold_reason)

    decision = GateDecision(
        gate_version=GATE_VERSION,
        phase=active_phase,  # type: ignore[arg-type]
        date=date,
        decision="hold" if hold_reasons else "auto_merge",
        hold_reasons=hold_reasons,
        review=GateReviewState(
            present=review_present,
            verdict=review_verdict,
            issue_sha256=recorded_hash,
            computed_issue_sha256=computed_hash,
            fresh=fresh,
            prompt_version=review_prompt_version,
            path=str(review_file),
        ),
        verify=GateVerifyState(
            present=verify_status == "ok",
            verdict=verify_verdict,
            stories_verified=stories_verified,
            contradicted_story_ids=sorted(contradicted),
            unsupported_story_ids=sorted(unsupported),
            path=str(verify_file),
        ),
        checks=checks,
        decided_at=_dt.datetime.now(_dt.timezone.utc),
        note=phase_note,
    )
    _LOG.info(
        "gate: %s decision=%s phase=%s reasons=%s",
        date.isoformat(), decision.decision, active_phase,
        ",".join(hold_reasons) or "(none)",
    )
    return decision


def _verdict_hold_reason(verdict: str) -> str:
    """Map a rejected review verdict to its stable hold token."""
    if verdict == "unavailable":
        return HOLD_REVIEW_UNAVAILABLE
    if verdict == "red":
        return HOLD_REVIEW_RED
    if verdict == "amber":
        # Only reachable in the ``green_only`` phase -- amber is accepted
        # everywhere else.
        return HOLD_REVIEW_AMBER
    if verdict in KNOWN_REVIEW_VERDICTS:
        # Defensive: a known verdict we somehow rejected without a specific
        # token. Better a generic hold than a crash inside the gate.
        return HOLD_REVIEW_UNKNOWN
    return HOLD_REVIEW_UNKNOWN


# ---------------------------------------------------------------------------
# Persistence.
# ---------------------------------------------------------------------------

def write_decision(decision: GateDecision, *, canonical: bool = False) -> Path:
    """Write ``gate.json`` atomically (.tmp + fsync + rename) and return the path.

    Same-day re-runs overwrite cleanly, like every other stage artifact.
    """
    path = gate_path(decision.date, canonical=canonical)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(decision.model_dump_json())
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return path


def run_gate(
    date: _dt.date, *, phase: str | None = None
) -> tuple[GateDecision, Path]:
    """Decide and persist in one call. The `aiv gate` command's entry point."""
    decision = decide(date, phase=phase)
    return decision, write_decision(decision)


def read_decision(date: _dt.date, *, canonical: bool = False) -> GateDecision | None:
    """Load a previously written ``gate.json``; ``None`` if absent or invalid.

    Readers tolerate a missing gate.json -- but a workflow deciding whether to
    publish must treat ``None`` as a hold, not as a pass.
    """
    path = gate_path(date, canonical=canonical)
    try:
        return GateDecision.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "GATE_VERSION",
    "PHASE_ENV_VAR",
    "DEFAULT_PHASE",
    "VALID_PHASES",
    "ACCEPTED_REVIEW_VERDICTS",
    "KNOWN_REVIEW_VERDICTS",
    "gate_path",
    "review_md_path",
    "sha256_of_file",
    "issue_sha256",
    "resolve_phase",
    "decide",
    "write_decision",
    "run_gate",
    "read_decision",
]
