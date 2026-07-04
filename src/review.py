"""
src/review.py -- AI Vector pre-release editorial review.

Reads the staged ``issue.json`` for a date, asks the LLM (in the Editor's
voice, drawing on ``EDITORIAL.md``) for a structured editorial verdict, and
writes a Markdown artifact to ``data/staging/<date>/review.md`` with a
machine-readable YAML frontmatter block.

The review never auto-publishes. It surfaces concerns to Arman before he
runs ``aiv release``. When the LLM call fails (timeout, auth, parse error),
we write a ``verdict: unavailable`` review.md and return normally -- a
review failure must NOT block publication.

Owner: LLM Engineer (per docs/internal/TEAM.md). This module is a NEW
*mode* of the existing Editor persona, not a new agent.

Audit tag: review-v0.1-2026-05-31.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src import paths


# ---------------------------------------------------------------------------
# Module constants.
# ---------------------------------------------------------------------------

REVIEW_PROMPT_VERSION = "v0.2"
"""Versioned prompt string written into ``review.md`` frontmatter so the
eval harness can correlate verdict movement against prompt revisions.

Bump when the prompt content (criteria, instructions, output format)
changes substantively. Audit tag: ``review-v0.2-2026-07-04``.

v0.2 (2026-07-04): Currents closing-shape wording generalised — the
calibrated stake is the turn-type; "if X, Y; if not, Z" is one grammar
for it, not the required scaffold. Keeps reviewer aligned with
summarise v0.18's close-form grammar diversification so legitimate
variety isn't false-flagged. v0.1 audit tag: ``review-v0.1-2026-05-31``."""

_REVIEW_LOOKBACK_ISSUES = 3
"""How many previously-released issues to include for drift-watch context.

The review uses these to spot recurring themes, source repetition, voice
collapse across consecutive days, and missing callbacks. Three issues is
enough to see a pattern without burning input tokens on stale history."""

_REVIEW_TIMEOUT_DEFAULT = 180.0
"""Seconds. The review prompt asks for a 3-4k-token structured Markdown
response; default-60 (matching ``rank.py``) timed out the 2026-05-29
staging call mid-generation. Bumped to 180s so a slow-but-successful
response still lands. Operators can override via ``LLM_TIMEOUT_SECONDS``."""

_LOG = logging.getLogger("ai_vector.review")


# ---------------------------------------------------------------------------
# Public return type.
# ---------------------------------------------------------------------------

@dataclass
class ReviewArtifact:
    """Lightweight summary the pipeline / CLI uses to print the terminal
    line. The substantive artifact is the ``review.md`` file on disk; this
    structure exposes just what callers need to log a one-liner.

    ``verdict`` mirrors the frontmatter value: ``"green" | "amber" | "red"``
    on success, or ``"unavailable"`` when the LLM call failed and we wrote a
    placeholder. ``path`` is the path to ``review.md`` (always written, even
    on the unavailable branch).
    """
    date: _dt.date
    verdict: str
    one_line: str
    path: Path


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def run_review(
    date: _dt.date | None = None, dry_run: bool = False
) -> ReviewArtifact:
    """Run the editorial review for one date.

    Loads the staged ``issue.json``, gathers up to the last
    ``_REVIEW_LOOKBACK_ISSUES`` released issues, calls the LLM with the
    review prompt, and writes ``data/staging/<date>/review.md``. Returns a
    ``ReviewArtifact`` summarising the verdict.

    Failure-soft contract: any LLM-side failure (transport, timeout, parse,
    missing env vars) is logged at WARNING, written into ``review.md`` as
    ``verdict: unavailable`` with the error message in the body, and
    returned normally. The pipeline must continue.

    Parameters
    ----------
    date
        Issue date (local). Defaults to today.
    dry_run
        When True, returns a fake ReviewArtifact and writes nothing.
        Mirrors the dry-run contract on other stages.
    """
    run_date = date or _dt.date.today()
    review_path = paths.staging_dir(run_date) / "review.md"

    if dry_run:
        return ReviewArtifact(
            date=run_date,
            verdict="green",
            one_line="(dry-run: review would write to review.md)",
            path=review_path,
        )

    staged_issue_path = paths.issue_path(run_date, canonical=False)
    if not staged_issue_path.exists():
        msg = f"no staged issue.json at {staged_issue_path}"
        _LOG.warning("review: %s -- writing unavailable review.md", msg)
        return _write_unavailable(run_date, review_path, msg)

    try:
        issue_payload = json.loads(staged_issue_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"could not read staged issue.json: {exc}"
        _LOG.warning("review: %s", msg)
        return _write_unavailable(run_date, review_path, msg)

    recent_issues = _load_recent_released_issues(run_date, _REVIEW_LOOKBACK_ISSUES)

    prompt = _build_review_prompt(issue_payload, recent_issues)
    timeout = _resolve_timeout()

    try:
        raw = _call_review_llm(prompt, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 -- never fail the pipeline
        msg = f"LLM call failed: {type(exc).__name__}: {exc}"
        _LOG.warning("review: %s -- writing unavailable review.md", msg)
        return _write_unavailable(run_date, review_path, msg)

    verdict, one_line = _extract_frontmatter_summary(raw)
    # The LLM is asked to emit verdict/one_line/issue_date/issue_shape in
    # its own frontmatter; we layer ONLY the audit-trail keys on top so the
    # final block is single-authored per key. When the LLM omitted the
    # block entirely, the synthesise path falls back to including verdict
    # + one_line we extracted (otherwise the frontmatter would be missing
    # the most useful fields).
    llm_metadata = {
        "llm_model": (os.getenv("LLM_MODEL") or "").strip() or "unknown",
    }
    written = _write_review_artifact(
        run_date, raw, llm_metadata,
        fallback_verdict=verdict, fallback_one_line=one_line,
        issue_date=run_date.isoformat(),
        issue_shape=_extract_issue_shape(issue_payload),
    )
    _LOG.info(
        "review: %s verdict=%s one_line=%r -> %s",
        run_date.isoformat(), verdict, one_line, written,
    )
    return ReviewArtifact(
        date=run_date, verdict=verdict, one_line=one_line, path=written,
    )


# ---------------------------------------------------------------------------
# Prompt assembly.
# ---------------------------------------------------------------------------

_REVIEW_INSTRUCTIONS = """\
You are the AI Vector EDITOR running a pre-release review on the staged
issue below. You are NOT writing the issue; you are reading it and
producing a structured editorial verdict that Arman will read before
deciding whether to publish.

Voice: the editor's voice from EDITORIAL.md -- direct, specific, no
buzzwords, no hype. You may quote short fragments of the issue back at
yourself. Do not rewrite the issue prose unless you are surfacing a
specific concern with a specific story.

EVALUATE THE ISSUE AGAINST THESE CRITERIA
=========================================

SHAPE INTEGRITY
- Section counts vs caps: 1 Pulse / up to 4 Big Picture / up to 5 Hands-On
  / up to 8 Currents.
- The reported shape (green/amber/red) reflects the section fill rate.
  Decide whether amber/red reflects a genuinely thin tier pool (acceptable
  on slow days) or a routing failure upstream (smell that needs flagging).

PULSE PICK
- Does the Pulse story carry the day's editorial position?
- Closing shape: PLAIN TAKE (sharp declarative). No question, no
  prescription, no hedge.
- Sourcing credibility (multi-source, canonical_id present, or trust-3+).
- Freshness vs recurrence -- if it's a recurrence, is novelty earned?

BIG PICTURE (up to 4 stories)
- Voice adherence per EDITORIAL.md: named actors + first-order consequence
  framing. Lead with WHO and what changes for them.
- Closing shape: STRATEGIC QUESTION on each story. Flag any that close on
  a take, an action, or a hedge instead.
- Section intro frames the pattern across stories (leader-orienting).
- Flag any story that reads more like Hands-On than Big Picture.

HANDS-ON (up to 5 stories)
- Voice adherence: tool / repo / version / config in the headline noun
  phrase.
- Closing shape: IMPERATIVE ACTION sharpened to a specific artefact +
  trigger. Generic "test before you trust" / "be cautious" fails the
  shape.
- Section intro carries a practitioner posture.
- Flag any story that reads more like Big Picture or Currents than
  Hands-On.

CURRENTS (variable count)
- Voice adherence: conditional / hedged opening ("Early signal:";
  "If X holds:"; "Worth watching:").
- Closing shape: CALIBRATED STAKE -- two-sided, with real stakes on both
  branches. "If X, Y; if not, Z" is ONE grammar for it, not the required
  scaffold: stake-first, watch-condition, and magnitude-framed closes are
  equally valid when both branches carry real stakes. Judge the
  two-sidedness, not the surface mould.
- Section intro is MANDATORY and names the aggregate motion direction --
  flag if missing or flat.
- Flag items that belong head-tier (Big Picture / Hands-On) instead.

DRIFT WATCH (compare against previous released issues, supplied below)
- Recurring themes covered the same way without progression.
- Source repetition (same source 3+ days running on similar topic).
- Missing callbacks (today's story extends yesterday's but doesn't
  reference it).
- Voice drift -- intros collapsing into a single register across sections
  (e.g. all reading as "trust but verify").

FINANCE ANGLE
- Does the FS lens land where it appears, or feel forced?
- Any story where the FS angle was the only reason to surface it but the
  angle is weak?

OUTPUT
======

Return your review as Markdown matching EXACTLY this template. Replace
all <...> placeholders. Keep the YAML frontmatter at the very top. Every
section heading must appear even when the section has no concerns -- in
that case write "No concerns this issue." rather than omitting the
section. The frontmatter ``verdict`` must be one of: green, amber, red.

GREEN = ratify as-is, no concerns. AMBER = ratify with notes / one or two
specific concerns surfaced. RED = hold; substantive editorial problem
that warrants a re-summarise or a re-pick before publication.

```markdown
---
verdict: <green|amber|red>
one_line: <30-60 character editorial summary of the day>
issue_date: <YYYY-MM-DD>
issue_shape: <green|amber|red>
---

# Editor's Review -- <YYYY-MM-DD>

**Verdict**: <GREEN|AMBER|RED>. <One-paragraph editorial read of the day -- 2-4 sentences.>

## Shape
<One short paragraph on shape integrity -- is the reported shape appropriate given the day's news?>

## Pulse
**Pick**: "<headline>"
- Editorial fit: <assessment>
- Closing shape: <plain take / off>
- Sourcing: <credibility note>
- <Any concern, or "No concerns.">

## Big Picture
**Intro**: "<intro_lead -- exact quote from the section>"
- Distinct register: <yes/no, why>
- Closing shapes: <N of M strategic questions; flag slips>

### Stories
1. "<headline>" -- <one-line editorial read; flag voice or closing concerns>
2. ...

## Hands-On
**Intro**: "<intro_lead>"
- Distinct register: <yes/no, why>
- Closing shapes: <N of M imperative actions; flag slips>

### Stories
1. "<headline>" -- <one-line editorial read>
2. ...

## Currents
**Intro**: "<intro_lead -- mandatory>"
- Aggregate direction: <named yes/no>
- Closing shapes: <N of M calibrated stakes; flag slips>

### Stories
1. "<headline>" -- <one-line editorial read>
2. ...

## Drift watch
- <Specific observation, or "No drift concerns this issue.">

## Recommendations before release
- <Actionable, or "Ratify as-is.">

## Ratification call
**Editor recommends**: <RATIFY|RATIFY WITH NOTES|HOLD>
**Arman's call**: ___
```
"""


def _build_review_prompt(
    issue: dict[str, Any], recent_issues: list[dict[str, Any]]
) -> str:
    """Assemble the LLM review prompt.

    ``issue`` and ``recent_issues`` are raw issue.json payloads (dicts) --
    we work from the parsed JSON rather than constructing the pydantic
    ``Issue`` so we don't crash on schema-version skew between staged and
    released issues; the review is a best-effort read.
    """
    today_block = _format_issue_for_prompt(
        issue, label="STAGED ISSUE UNDER REVIEW",
    )
    if recent_issues:
        recent_blocks = "\n\n".join(
            _format_issue_for_prompt(
                ri, label=f"PRIOR RELEASED ISSUE ({ri.get('date', '?')})",
                compact=True,
            )
            for ri in recent_issues
        )
        recent_section = (
            "\n\nFor drift-watch context, here are the previous "
            f"{len(recent_issues)} released issues (compact form -- headlines "
            "and intros only):\n\n" + recent_blocks
        )
    else:
        recent_section = (
            "\n\n(No prior released issues available within the lookback "
            "window. Skip the drift-watch comparison.)"
        )
    return f"""\
{_REVIEW_INSTRUCTIONS}

{today_block}{recent_section}
"""


def _format_issue_for_prompt(
    payload: dict[str, Any], *, label: str, compact: bool = False
) -> str:
    """Render an issue payload as a prompt-friendly block.

    ``compact=True`` drops the full summary body and keeps only headlines
    + section intros -- used for the prior-issue context so we don't burn
    tokens on prose the editor only needs at the pattern level.
    """
    lines: list[str] = []
    date = payload.get("date") or "?"
    shape = _extract_issue_shape(payload)
    lines.append(f"=== {label} ===")
    lines.append(f"date: {date}")
    if shape:
        lines.append(f"shape: {shape}")
    pulse = payload.get("pulse") or {}
    pulse_stories = pulse.get("stories") or []
    lines.append("")
    lines.append("PULSE:")
    for story in pulse_stories:
        if not isinstance(story, dict):
            continue
        lines.append(f"  - headline: {story.get('headline', '')}")
        summary = story.get("summary", "")
        if not compact:
            lines.append(f"    summary: {summary}")
        srcs = story.get("source_urls") or []
        if srcs:
            lines.append(f"    sources: {len(srcs)} -- {', '.join(map(str, srcs[:3]))}")

    sections = payload.get("sections") or []
    for section in sections:
        if not isinstance(section, dict):
            continue
        name = section.get("name", "?")
        stories = section.get("stories") or []
        intro_lead = section.get("intro_lead") or ""
        intro_body = section.get("intro_body") or ""
        lines.append("")
        lines.append(f"SECTION {name} ({len(stories)} stories):")
        if intro_lead:
            lines.append(f"  intro_lead: {intro_lead}")
        if intro_body:
            lines.append(f"  intro_body: {intro_body}")
        for story in stories:
            if not isinstance(story, dict):
                continue
            lines.append(f"  - headline: {story.get('headline', '')}")
            if not compact:
                summary = story.get("summary", "")
                lines.append(f"    summary: {summary}")
                prior = story.get("prior_coverage_ref")
                if prior:
                    lines.append(f"    prior_coverage_ref: {prior}")
            srcs = story.get("source_urls") or []
            if srcs:
                lines.append(
                    f"    sources: {len(srcs)} -- {', '.join(map(str, srcs[:2]))}"
                )
    return "\n".join(lines)


def _extract_issue_shape(payload: dict[str, Any]) -> str:
    """Pull the shape token out of the staged issue's ``notes`` field, if
    present. Returns ``""`` when not found -- summarise.py writes a
    "shape: green -- pulse: 1, ..." prefix that we parse to surface in
    the review frontmatter for at-a-glance correlation."""
    notes = payload.get("notes")
    if not isinstance(notes, str):
        return ""
    notes = notes.strip()
    if notes.startswith("shape:"):
        tail = notes[len("shape:"):].strip()
        token = tail.split()[0] if tail else ""
        # Strip trailing punctuation from "green --".
        token = token.rstrip(",.;:-")
        return token
    return ""


# ---------------------------------------------------------------------------
# Recent-issue lookup.
# ---------------------------------------------------------------------------

def _load_recent_released_issues(
    today: _dt.date, n: int
) -> list[dict[str, Any]]:
    """Walk ``data/released/*/issue.json`` newest-first, returning the
    last ``n`` issues' raw payloads. Tolerates fewer than ``n`` (returns
    what's available) and malformed JSON (skips silently)."""
    out: list[dict[str, Any]] = []
    # Exclude today's own date if it has been released somehow (re-review
    # against a released issue shouldn't see itself as a prior reference).
    candidates = [d for d in paths.all_released_dates() if d < today]
    for d in sorted(candidates, reverse=True):
        if len(out) >= n:
            break
        path = paths.issue_path(d, canonical=True)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            out.append(payload)
    # Return oldest-first so the prompt reads chronologically.
    return list(reversed(out))


# ---------------------------------------------------------------------------
# LLM call -- reuses rank.py's provider routing.
# ---------------------------------------------------------------------------

def _call_review_llm(prompt: str, timeout: float) -> str:
    """Issue one LLM call and return the raw response text.

    Reuses ``rank._llm_call`` so provider routing (anthropic / bedrock /
    openai-compatible) and timeout-handling are inherited unchanged.
    Temperature is intentionally moderate (0.4) -- the editor's voice has
    texture, but we don't want wild verdict variance across same-day
    re-runs.

    Local timeout is applied via the ``LLM_TIMEOUT_SECONDS`` env var which
    ``rank._llm_call`` already reads; the ``timeout`` arg here is the
    fallback we set on the env mid-call so the existing transport honours
    it. We restore the prior value on the way out.
    """
    # rank._llm_call reads LLM_TIMEOUT_SECONDS from os.environ itself; the
    # cleanest seam is to set/restore it around the call rather than
    # duplicate provider routing here.
    from src import rank as _rank

    prior = os.environ.get("LLM_TIMEOUT_SECONDS")
    os.environ["LLM_TIMEOUT_SECONDS"] = str(timeout)
    try:
        # ~4k tokens covers a full structured review across all sections;
        # an earlier 2000-token budget truncated mid-Currents on the
        # 2026-05-29 staging issue. Headroom is cheap vs. the cost of a
        # half-written verdict.
        return _rank._llm_call(prompt, temperature=0.4, max_tokens=4000)
    finally:
        if prior is None:
            os.environ.pop("LLM_TIMEOUT_SECONDS", None)
        else:
            os.environ["LLM_TIMEOUT_SECONDS"] = prior


def _resolve_timeout() -> float:
    """Decide the per-call timeout for the review LLM request.

    Reads ``LLM_REVIEW_TIMEOUT_SECONDS`` first so operators can tune the
    review-specific budget without disturbing rank/summarise. Falls back
    to ``_REVIEW_TIMEOUT_DEFAULT`` (180s) -- which is intentionally LARGER
    than the rank/summarise default (60s) because the review prompt asks
    for a structured 3-4k-token Markdown response.

    We deliberately do NOT read ``LLM_TIMEOUT_SECONDS`` -- shared with
    rank/summarise -- because their 60s default truncates the review
    mid-generation (validated empirically on the 2026-05-29 staging
    issue).
    """
    raw = os.getenv("LLM_REVIEW_TIMEOUT_SECONDS")
    if raw is None or not raw.strip():
        return _REVIEW_TIMEOUT_DEFAULT
    try:
        return float(raw.strip())
    except ValueError:
        return _REVIEW_TIMEOUT_DEFAULT


# ---------------------------------------------------------------------------
# Frontmatter parsing + Markdown write.
# ---------------------------------------------------------------------------

_VALID_VERDICTS = {"green", "amber", "red"}


def _extract_frontmatter_summary(raw: str) -> tuple[str, str]:
    """Best-effort extraction of ``verdict`` + ``one_line`` from the LLM's
    Markdown response.

    The prompt asks for a YAML frontmatter block delimited by ``---``.
    We scan the first such block and read the two keys; on any parse
    failure we return ``("amber", "<could not parse verdict>")`` so the
    terminal line still surfaces SOMETHING and Arman opens the file.
    """
    # Strip code-fence wrappers the LLM sometimes adds.
    stripped = raw.lstrip()
    if stripped.startswith("```"):
        # drop the first fence line
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1:]
        # drop a trailing fence if any
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rsplit("```", 1)[0]
    if not stripped.startswith("---"):
        return ("amber", "<frontmatter missing>")
    # find the closing --- after the first line
    rest = stripped[3:]
    end = rest.find("\n---")
    if end == -1:
        return ("amber", "<frontmatter unclosed>")
    fm_block = rest[:end]
    verdict = "amber"
    one_line = "<one_line missing>"
    for line in fm_block.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip().strip('"').strip("'")
        if key == "verdict":
            value_lower = value.lower()
            if value_lower in _VALID_VERDICTS:
                verdict = value_lower
        elif key == "one_line":
            if value:
                one_line = value
    return (verdict, one_line)


def _write_review_artifact(
    date: _dt.date,
    markdown: str,
    llm_metadata: dict[str, Any],
    *,
    fallback_verdict: str | None = None,
    fallback_one_line: str | None = None,
    issue_date: str | None = None,
    issue_shape: str | None = None,
) -> Path:
    """Write ``data/staging/<date>/review.md``.

    The LLM is asked to produce a complete Markdown doc with frontmatter;
    we accept it as-is and append our own ``generated_at`` /
    ``prompt_version`` / ``llm_model`` keys to the frontmatter block so
    downstream tooling can parse provenance without re-LLM. When the LLM
    omits frontmatter entirely (defensive case), we synthesise a minimal
    one from the fallback values supplied so the file remains parseable.
    """
    review_path = paths.staging_dir(date) / "review.md"
    review_path.parent.mkdir(parents=True, exist_ok=True)

    enriched = _enrich_frontmatter(
        markdown, llm_metadata,
        fallback_verdict=fallback_verdict,
        fallback_one_line=fallback_one_line,
        issue_date=issue_date,
        issue_shape=issue_shape,
    )
    # Atomic write -- mirrors the rank/summarise pattern. Same-day re-runs
    # overwrite the prior review.md cleanly.
    tmp = review_path.with_suffix(review_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(enriched)
        if not enriched.endswith("\n"):
            fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, review_path)
    return review_path


def _enrich_frontmatter(
    markdown: str,
    extra: dict[str, Any],
    *,
    fallback_verdict: str | None = None,
    fallback_one_line: str | None = None,
    issue_date: str | None = None,
    issue_shape: str | None = None,
) -> str:
    """Append provenance keys to the LLM's frontmatter block.

    Always adds ``generated_at`` + ``prompt_version`` + ``llm_model`` so
    the artifact carries its own audit trail. If the LLM omitted the
    frontmatter entirely, we synthesise a minimal one with the
    ``fallback_verdict`` / ``fallback_one_line`` / ``issue_date`` /
    ``issue_shape`` we extracted upstream so the file is still
    machine-parseable.
    """
    generated_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    base_kvs: dict[str, Any] = {
        "generated_at": generated_at,
        "prompt_version": REVIEW_PROMPT_VERSION,
    }
    base_kvs.update(extra)

    stripped = markdown.lstrip()
    if stripped.startswith("```"):
        # Strip a leading code-fence the LLM sometimes adds around the
        # whole document, e.g. ```markdown\n...\n```.
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1:]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rsplit("```", 1)[0]

    if stripped.startswith("---"):
        rest = stripped[3:]
        # Drop a leading newline so we don't double up on the `---\n` line.
        if rest.startswith("\n"):
            rest = rest[1:]
        end = rest.find("\n---")
        if end != -1:
            fm = rest[:end]
            body = rest[end + len("\n---"):]
            # Append our keys to the existing block. We deliberately do
            # NOT inject verdict/one_line/issue_date/issue_shape here --
            # those came from the LLM's own frontmatter and the avoid-
            # duplicate-keys cleanliness rule wins. YAML readers tolerate
            # duplicate keys but humans don't.
            extras = "\n".join(
                f"{k}: {_yaml_safe(v)}" for k, v in base_kvs.items()
            )
            fm_combined = fm.rstrip() + "\n" + extras + "\n"
            return f"---\n{fm_combined}---{body}"
    # No frontmatter detected -- synthesise one using the fallback values.
    synth: dict[str, Any] = {}
    if fallback_verdict is not None:
        synth["verdict"] = fallback_verdict
    if fallback_one_line is not None:
        synth["one_line"] = fallback_one_line
    if issue_date is not None:
        synth["issue_date"] = issue_date
    if issue_shape:
        synth["issue_shape"] = issue_shape
    synth.update(base_kvs)
    extras = "\n".join(f"{k}: {_yaml_safe(v)}" for k, v in synth.items())
    return f"---\n{extras}\n---\n\n{markdown}"


def _yaml_safe(value: Any) -> str:
    """Render a value as a YAML scalar. Strings get quoted only when they
    contain reserved characters; simple tokens (green / amber / a model
    id) stay unquoted for readability."""
    if isinstance(value, str):
        if any(ch in value for ch in (":", "#", "\n", "\"", "'")) or value.strip() != value:
            return '"' + value.replace('"', '\\"') + '"'
        return value
    return str(value)


def _write_unavailable(
    date: _dt.date, path: Path, reason: str
) -> ReviewArtifact:
    """Write a placeholder review.md when the LLM call could not run.

    The publication still ships; Arman just doesn't get a review for the
    day. The file is shaped so downstream parsers see ``verdict:
    unavailable`` in the frontmatter and act accordingly (don't print a
    misleading green).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    generated_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    one_line = f"unavailable: {reason}"
    content = (
        "---\n"
        f"verdict: unavailable\n"
        f"one_line: {_yaml_safe(one_line)}\n"
        f"generated_at: {generated_at}\n"
        f"prompt_version: {REVIEW_PROMPT_VERSION}\n"
        f"llm_model: {_yaml_safe((os.getenv('LLM_MODEL') or '').strip() or 'unknown')}\n"
        f"issue_date: {date.isoformat()}\n"
        "issue_shape: unknown\n"
        "---\n\n"
        f"# Editor's Review -- {date.isoformat()}\n\n"
        f"**Verdict**: UNAVAILABLE.\n\n"
        f"The review LLM call did not complete: {reason}.\n\n"
        "The publication can still ship; Arman has no structured editorial "
        "read for today. Re-run `aiv review --date "
        f"{date.isoformat()}` once the underlying issue is resolved.\n"
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return ReviewArtifact(
        date=date, verdict="unavailable",
        one_line=one_line, path=path,
    )
