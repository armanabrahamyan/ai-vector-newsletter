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

Verdicts are fail-closed. ``green | amber | red`` are editorial
judgements and only the model can author them; ``unavailable``,
``unparseable``, and ``not_run`` are code-authored states meaning "no
judgement was recorded", and nothing here ever degrades a failure into a
judgement-shaped default. The verdict in the returned ``ReviewArtifact``
and the ``verdict:`` line in ``review.md`` come from the same value
written by the same code, so a consumer reading either sees the same
thing. The frontmatter also carries ``issue_sha256``, the hash of the
exact ``issue.json`` bytes the reviewer read, so a consumer can tell
whether the issue changed after the review ran. See ``REVIEW_VERDICTS``.

Owner: LLM Engineer (per docs/internal/TEAM.md). This module is a NEW
*mode* of the existing Editor persona, not a new agent.

Audit tag: review-v0.1-2026-05-31.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
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

REVIEW_PROMPT_VERSION = "v0.5"
"""Versioned prompt string written into ``review.md`` frontmatter so the
eval harness can correlate verdict movement against prompt revisions.

Bump when the prompt content (criteria, instructions, output format)
changes substantively. Audit tag: ``review-v0.5-2026-07-04``.

v0.5 (2026-07-04): trust-flag reviewer calibration after two same-day
misfires on summarise v0.20 output. Misfire 1: the review claimed
"trust flags defective ... functioning as absence inventories" on
stories whose text contained ZERO absence forms. Misfire 2: it
requested "(arXiv preprint, single research team)" -- a
default-restating flag its own gate-3 criterion bans -- on a body that
already opened with "An arXiv preprint reverse-engineers...". The
TRUST FLAGS criterion now requires QUOTING the offending flag text
verbatim from the story before any defect finding (no quote = no
finding), and states that a source-class noun phrase in the body IS
the calibration -- the reviewer must never request parenthetical flags
on top of it.

v0.4 (2026-07-04): trust-flag gate 3 (informative vs the evidence-class
default; reader-needs study, READING_EXPERIENCE.md §3 + R-8). The
TRUST FLAGS criterion now flags default-restating flags ("a preprint
from a single research team"; "vendor-published benchmark" when the
body names the vendor) as noise, not virtue -- the fix is deleting the
flag, not rewording it -- and its positive examples are deviation-shaped
so the reviewer never demands a default-restating flag back. Aligns the
reviewer with summarise v0.20.

v0.3 (2026-07-04): trust flags are presence-form (Arman's direction via
Experience Designer, READING_EXPERIENCE.md R-8). New per-story
criterion: trust flags must characterise the evidence that exists;
absence-inventory ("no code yet", "no independent replication yet",
"not yet peer-reviewed") is flagged as a defect, never demanded back
as a missing virtue. Aligns the reviewer with summarise v0.19.

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
# Verdict vocabulary.
# ---------------------------------------------------------------------------

VERDICT_GREEN = "green"
VERDICT_AMBER = "amber"
VERDICT_RED = "red"
VERDICT_UNAVAILABLE = "unavailable"
VERDICT_UNPARSEABLE = "unparseable"
VERDICT_NOT_RUN = "not_run"

REVIEW_LLM_VERDICTS: frozenset[str] = frozenset(
    {VERDICT_GREEN, VERDICT_AMBER, VERDICT_RED}
)
"""The only verdict tokens the LLM is allowed to author.

Anything else in the model's frontmatter -- including the code-reserved
tokens below -- is out-of-vocabulary and yields ``unparseable``. The model
must not be able to claim a machine state it does not own."""

REVIEW_VERDICTS: frozenset[str] = REVIEW_LLM_VERDICTS | frozenset(
    {VERDICT_UNAVAILABLE, VERDICT_UNPARSEABLE, VERDICT_NOT_RUN}
)
"""Every verdict a ``ReviewArtifact`` (and the frontmatter written beside
it) may carry. The three code-authored tokens mean:

``unavailable`` -- the review could not run (no staged issue, unreadable
issue, LLM transport failure). ``unparseable`` -- the review ran but its
frontmatter could not be read strictly enough to trust a verdict.
``not_run`` -- the stage was invoked in dry-run and produced no judgement
at all.

None of the three is an editorial pass. A consumer that authorises
anything must treat ``green`` (and only ``green``) as permission; every
other token withholds it."""

# Backwards-compatible alias: this name previously held the LLM vocabulary.
_VALID_VERDICTS = REVIEW_LLM_VERDICTS


# ---------------------------------------------------------------------------
# Public return type.
# ---------------------------------------------------------------------------

@dataclass
class ReviewArtifact:
    """Lightweight summary the pipeline / CLI uses to print the terminal
    line. The substantive artifact is the ``review.md`` file on disk; this
    structure exposes just what callers need to log a one-liner.

    ``verdict`` is one of ``REVIEW_VERDICTS``: ``green | amber | red`` when
    the LLM produced a readable verdict, ``unavailable`` when the review
    could not run, ``unparseable`` when it ran but the frontmatter could not
    be trusted, and ``not_run`` for dry runs. ``path`` is the path to
    ``review.md`` -- always written, except on the dry-run path where
    nothing is written at all.

    The verdict here and the ``verdict:`` line in the file at ``path`` are
    written from the same value by the same code, so they cannot disagree.
    """
    date: _dt.date
    verdict: str
    one_line: str
    path: Path

    def __post_init__(self) -> None:
        # Guards a code bug, not user input: every construction site passes
        # one of the module constants. A verdict outside the vocabulary
        # would be uninterpretable to any downstream consumer, so we fail
        # loudly rather than hand it on. run.py's advisory-stage guard keeps
        # the pipeline alive if this ever fires.
        if self.verdict not in REVIEW_VERDICTS:
            raise ValueError(
                f"review verdict {self.verdict!r} is outside the vocabulary "
                f"{sorted(REVIEW_VERDICTS)}"
            )


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

    The one exception is a filesystem failure that prevents us recording
    the unavailable state at all: that raises, because an absent
    ``review.md`` is indistinguishable from a stage that was never asked to
    run, and silence must never be readable as approval. ``run.py`` treats
    review as an advisory stage and keeps the pipeline going either way.

    Parameters
    ----------
    date
        Issue date (local). Defaults to today.
    dry_run
        When True, returns a ``not_run`` ReviewArtifact and writes nothing.
        Mirrors the dry-run contract on other stages.
    """
    run_date = date or _dt.date.today()
    review_path = paths.staging_dir(run_date) / "review.md"

    if dry_run:
        # A dry run produces no editorial judgement, so it must not return a
        # judgement-shaped verdict. ``not_run`` is deliberately outside the
        # green/amber/red vocabulary any consumer would read as a pass.
        return ReviewArtifact(
            date=run_date,
            verdict=VERDICT_NOT_RUN,
            one_line="(dry-run: review did not run; no verdict)",
            path=review_path,
        )

    staged_issue_path = paths.issue_path(run_date, canonical=False)
    if not staged_issue_path.exists():
        msg = f"no staged issue.json at {staged_issue_path}"
        _LOG.warning("review: %s -- writing unavailable review.md", msg)
        return _write_unavailable(run_date, review_path, msg)

    # Read the bytes ONCE and hash exactly what we are about to send to the
    # reviewer. Re-reading to hash would leave a window in which the file
    # changed between the read and the hash, which is precisely the
    # staleness the downstream freshness check exists to catch.
    try:
        issue_bytes = staged_issue_path.read_bytes()
    except OSError as exc:
        msg = f"could not read staged issue.json: {exc}"
        _LOG.warning("review: %s", msg)
        return _write_unavailable(run_date, review_path, msg)

    issue_sha256 = hashlib.sha256(issue_bytes).hexdigest()

    try:
        issue_payload = json.loads(issue_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = f"could not parse staged issue.json: {exc}"
        _LOG.warning("review: %s", msg)
        return _write_unavailable(
            run_date, review_path, msg, issue_sha256=issue_sha256,
        )

    if not isinstance(issue_payload, dict):
        msg = (
            "staged issue.json is not a JSON object "
            f"(got {type(issue_payload).__name__})"
        )
        _LOG.warning("review: %s", msg)
        return _write_unavailable(
            run_date, review_path, msg, issue_sha256=issue_sha256,
        )

    recent_issues = _load_recent_released_issues(run_date, _REVIEW_LOOKBACK_ISSUES)

    prompt = _build_review_prompt(issue_payload, recent_issues)
    timeout = _resolve_timeout()

    try:
        raw = _call_review_llm(prompt, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 -- never fail the pipeline
        msg = f"LLM call failed: {type(exc).__name__}: {exc}"
        _LOG.warning("review: %s -- writing unavailable review.md", msg)
        return _write_unavailable(
            run_date, review_path, msg, issue_sha256=issue_sha256,
        )

    verdict, one_line = _extract_frontmatter_summary(raw)
    # The verdict we just extracted is the ONE verdict: it goes into the
    # returned artifact AND is the value written into the file's
    # frontmatter. The LLM's own verdict line is stripped from the block and
    # preserved as ``llm_reported_verdict`` for the audit trail; its prose,
    # including the "**Verdict**: ..." line in the body, is left untouched.
    llm_metadata = {
        "llm_model": (os.getenv("LLM_MODEL") or "").strip() or "unknown",
    }
    written = _write_review_artifact(
        run_date, raw, llm_metadata,
        verdict=verdict, one_line=one_line,
        issue_date=run_date.isoformat(),
        issue_shape=_extract_issue_shape(issue_payload),
        issue_sha256=issue_sha256,
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

TRUST FLAGS (every story, all sections)
- Trust flags must be PRESENCE-FORM: they characterise the evidence that
  exists ("a second lab replicated it", "the vendor benchmarked its
  competitor's model", "scored by an ensemble of LLM judges").
  Flag absence-inventory ("no code yet", "no independent replication
  yet", "not yet peer-reviewed") as a DEFECT -- never as a missing
  virtue.
- A flag that merely restates its evidence-class default ("a preprint
  from a single research team"; "vendor-published benchmark" when the
  body already names the vendor) is NOISE, not virtue -- flag it as a
  defect; the fix is deleting the flag and letting the source-class
  name in the body carry the calibration.
- CALIBRATION -- evidence before finding. Before flagging ANY
  trust-flag defect, QUOTE the offending flag text VERBATIM from the
  story summary. No verbatim quote = no finding; do not report
  "absence inventories" against stories whose text contains zero
  absence forms.
- A source-class noun phrase in the body ("an arXiv preprint",
  "Anthropic's release notes", "a Reddit thread") IS the calibration.
  Never request a parenthetical flag on top of it -- asking for
  "(arXiv preprint, single research team)" on a body that already
  opens "An arXiv preprint reverse-engineers..." demands the exact
  default-restating noise this criterion bans.

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

_CODE_AUTHORED_KEYS: frozenset[str] = frozenset({
    "verdict",
    "one_line",
    "llm_reported_verdict",
    "issue_date",
    "issue_shape",
    "issue_sha256",
    "generated_at",
    "prompt_version",
    "llm_model",
})
"""Frontmatter keys the WRITE path owns outright.

Any line carrying one of these keys is stripped from the LLM's block
before we append our own, so each key appears exactly once and is written
by code. ``verdict`` is the load-bearing member: it is why the file and
the returned ``ReviewArtifact`` cannot disagree."""


def _strip_code_fence(text: str) -> str:
    """Remove a whole-document code fence the LLM sometimes wraps its
    Markdown in (e.g. ```markdown ... ```). Leading whitespace goes too, so
    the frontmatter delimiter can be matched at position zero."""
    stripped = text.lstrip()
    if not stripped.startswith("```"):
        return stripped
    first_newline = stripped.find("\n")
    if first_newline != -1:
        stripped = stripped[first_newline + 1:]
    if stripped.rstrip().endswith("```"):
        stripped = stripped.rsplit("```", 1)[0]
    return stripped


def _split_frontmatter(text: str) -> tuple[str | None, str, str]:
    """Split a Markdown document into (frontmatter, body, failure).

    A frontmatter block is recognised ONLY when the first line is exactly
    ``---`` and a later line is exactly ``---``. Nothing looser counts:
    ``---yaml`` is not an opener and ``----`` is not a closer, because a
    delimiter we half-recognise is a delimiter we can be fooled by.

    On success returns ``(frontmatter_text, body_text, "")``. On failure
    returns ``(None, "", reason)`` where reason is ``"missing"`` or
    ``"unclosed"``.
    """
    lines = _strip_code_fence(text).split("\n")
    if not lines or lines[0].strip() != "---":
        return (None, "", "missing")
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return ("\n".join(lines[1:idx]), "\n".join(lines[idx + 1:]), "")
    return (None, "", "unclosed")


def _parse_frontmatter_block(
    frontmatter: str,
) -> tuple[dict[str, str], set[str]] | None:
    """Parse a frontmatter block into ``{key: value}`` plus the set of keys
    that appeared more than once.

    First occurrence wins in the mapping; the duplicate set lets callers
    refuse to guess where the ambiguity actually matters. Blank lines and
    ``#`` comments are tolerated. Any other line that is not a plain
    ``key: value`` pair makes the whole block malformed and returns
    ``None`` -- we do not skip over lines we cannot account for, because a
    line we cannot parse may be the one that changes the meaning.
    """
    mapping: dict[str, str] = {}
    duplicates: set[str] = set()
    for raw_line in frontmatter.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            return None
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if not key or any(ch.isspace() for ch in key):
            return None
        value = value.strip().strip('"').strip("'")
        if key in mapping:
            duplicates.add(key)
            continue
        mapping[key] = value
    return (mapping, duplicates)


def _extract_frontmatter_summary(raw: str) -> tuple[str, str]:
    """Strictly extract ``verdict`` + ``one_line`` from the LLM's Markdown.

    Returns ``(verdict, one_line)`` where verdict is one of
    ``REVIEW_LLM_VERDICTS`` when -- and only when -- the frontmatter is
    well-formed and carries exactly one recognised verdict token. Every
    other outcome returns ``VERDICT_UNPARSEABLE`` with a diagnostic
    one_line naming the specific defect.

    There is deliberately no default verdict. The previous behaviour fell
    back to ``amber``, which turned three different failures (no
    frontmatter, unclosed frontmatter, a verdict token nobody recognised)
    into a value that reads like a real editorial judgement. Anything that
    consumes these verdicts must be able to tell "the editor said amber"
    apart from "we could not read what the editor said".

    Duplicate ``verdict:`` keys are unparseable rather than first-wins.
    Two verdict lines mean the document does not state one verdict, and
    picking either is guessing -- last-wins in particular let a
    ``red``-then-``green`` pair report green.
    """
    frontmatter, _body, failure = _split_frontmatter(raw)
    if frontmatter is None:
        return (VERDICT_UNPARSEABLE, f"<frontmatter {failure}>")

    parsed = _parse_frontmatter_block(frontmatter)
    if parsed is None:
        return (VERDICT_UNPARSEABLE, "<frontmatter malformed>")
    mapping, duplicates = parsed

    if "verdict" in duplicates:
        return (VERDICT_UNPARSEABLE, "<frontmatter has duplicate verdict keys>")

    raw_verdict = mapping.get("verdict")
    if raw_verdict is None:
        return (VERDICT_UNPARSEABLE, "<frontmatter has no verdict key>")

    verdict = raw_verdict.strip().lower()
    if verdict not in REVIEW_LLM_VERDICTS:
        return (
            VERDICT_UNPARSEABLE,
            f"<verdict token not recognised: {raw_verdict!r}>",
        )

    one_line = mapping.get("one_line") or "<one_line missing>"
    return (verdict, one_line)


def _write_review_artifact(
    date: _dt.date,
    markdown: str,
    llm_metadata: dict[str, Any],
    *,
    verdict: str,
    one_line: str,
    issue_date: str | None = None,
    issue_shape: str | None = None,
    issue_sha256: str | None = None,
) -> Path:
    """Write ``data/staging/<date>/review.md``.

    The LLM's prose is preserved verbatim in the body. The frontmatter is
    rewritten so that the machine-readable keys -- ``verdict`` above all --
    are authored by this function from the values the caller is also
    putting into the returned ``ReviewArtifact``. That is what makes the
    file and the artifact incapable of disagreeing.
    """
    review_path = paths.staging_dir(date) / "review.md"
    review_path.parent.mkdir(parents=True, exist_ok=True)

    enriched = _enrich_frontmatter(
        markdown, llm_metadata,
        verdict=verdict,
        one_line=one_line,
        issue_date=issue_date,
        issue_shape=issue_shape,
        issue_sha256=issue_sha256,
    )
    _atomic_write_text(review_path, enriched)
    return review_path


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (tmp + fsync + rename),
    ensuring a trailing newline. Mirrors the rank/summarise pattern, so
    same-day re-runs overwrite the prior file cleanly and a reader never
    sees a half-written review."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(content)
        if not content.endswith("\n"):
            fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _enrich_frontmatter(
    markdown: str,
    extra: dict[str, Any],
    *,
    verdict: str,
    one_line: str,
    issue_date: str | None = None,
    issue_shape: str | None = None,
    issue_sha256: str | None = None,
) -> str:
    """Rewrite the document's frontmatter so code owns the machine keys.

    Every key in ``_CODE_AUTHORED_KEYS`` is stripped from whatever the LLM
    produced and re-emitted from the arguments here, in a fixed order. Any
    other key the LLM invented is kept, in its original order, above ours.

    Two things are preserved rather than discarded. The LLM's own verdict
    token is re-emitted as ``llm_reported_verdict`` so a reviewer can see
    what the model said next to what we recorded -- which is the whole
    audit trail when the two differ (a verdict the strict parser rejected
    still shows up here). And the body is untouched, including the
    ``**Verdict**: GREEN.`` sentence the prompt asks for; we never edit the
    editor's prose.

    ``issue_sha256`` is the SHA-256 of the exact ``issue.json`` bytes the
    reviewer read. It is always emitted -- ``unknown`` when we never got as
    far as reading the file -- so a freshness check downstream can compare
    it against the file on disk and hold when the issue moved underneath
    the review.
    """
    generated_at = _dt.datetime.now(_dt.timezone.utc).isoformat()

    frontmatter, body, _failure = _split_frontmatter(markdown)

    kept_lines: list[str] = []
    llm_reported_verdict: str | None = None
    if frontmatter is not None:
        for raw_line in frontmatter.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            key = line.partition(":")[0].strip().lower() if ":" in line else ""
            if key == "verdict" and llm_reported_verdict is None:
                llm_reported_verdict = (
                    line.partition(":")[2].strip().strip('"').strip("'")
                )
            if key in _CODE_AUTHORED_KEYS:
                continue
            kept_lines.append(raw_line.rstrip())

    kvs: dict[str, Any] = {"verdict": verdict, "one_line": one_line}
    if llm_reported_verdict:
        kvs["llm_reported_verdict"] = llm_reported_verdict
    if issue_date is not None:
        kvs["issue_date"] = issue_date
    if issue_shape:
        kvs["issue_shape"] = issue_shape
    kvs["issue_sha256"] = issue_sha256 or "unknown"
    kvs["generated_at"] = generated_at
    kvs["prompt_version"] = REVIEW_PROMPT_VERSION
    for key, value in extra.items():
        # Never let caller metadata shadow a key we have already written.
        # The comparison is case-insensitive because a stray ``Verdict:``
        # would parse back as a duplicate of our own ``verdict:`` line.
        if key.strip().lower() in {k.lower() for k in kvs}:
            continue
        kvs[key] = value

    code_block = _render_frontmatter(kvs)

    if frontmatter is None:
        # No readable frontmatter from the LLM -- synthesise the whole block
        # and leave the model's output as the body, so nothing is lost.
        return f"---\n{code_block}\n---\n\n{_strip_code_fence(markdown)}"

    kept = "\n".join(kept_lines).strip()
    combined = f"{kept}\n{code_block}" if kept else code_block
    return f"---\n{combined}\n---\n{body}"


def _render_frontmatter(kvs: dict[str, Any]) -> str:
    """Render an ordered mapping as YAML ``key: value`` lines."""
    return "\n".join(f"{k}: {_yaml_safe(v)}" for k, v in kvs.items())


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
    date: _dt.date, path: Path, reason: str, *, issue_sha256: str | None = None,
) -> ReviewArtifact:
    """Write a placeholder review.md when the review could not run.

    The publication still ships; Arman just doesn't get a review for the
    day. The file is shaped so downstream parsers see ``verdict:
    unavailable`` in the frontmatter and act accordingly (don't print a
    misleading green).

    The write itself is guarded, and a failure is logged AND re-raised.
    That is the deliberate difference from the ordinary failure-soft path:
    if we cannot even record that the review is unavailable, the only
    trace left on disk would be an absent (or stale, from a previous run)
    ``review.md``, and either reads as a state nobody chose. An exception
    reaching the caller is visible; a missing file is not. ``run.py``
    treats review as advisory and continues regardless.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    one_line = f"unavailable: {reason}"
    kvs: dict[str, Any] = {
        "verdict": VERDICT_UNAVAILABLE,
        "one_line": one_line,
        "issue_date": date.isoformat(),
        "issue_shape": "unknown",
        "issue_sha256": issue_sha256 or "unknown",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "prompt_version": REVIEW_PROMPT_VERSION,
        "llm_model": (os.getenv("LLM_MODEL") or "").strip() or "unknown",
    }
    content = (
        "---\n"
        f"{_render_frontmatter(kvs)}\n"
        "---\n\n"
        f"# Editor's Review -- {date.isoformat()}\n\n"
        f"**Verdict**: UNAVAILABLE.\n\n"
        f"The review LLM call did not complete: {reason}.\n\n"
        "The publication can still ship; Arman has no structured editorial "
        "read for today. Re-run `aiv review --date "
        f"{date.isoformat()}` once the underlying issue is resolved.\n"
    )
    try:
        _atomic_write_text(path, content)
    except Exception:
        _LOG.exception(
            "review: could not write the unavailable review.md at %s "
            "(original reason: %s)", path, reason,
        )
        raise
    return ReviewArtifact(
        date=date, verdict=VERDICT_UNAVAILABLE,
        one_line=one_line, path=path,
    )
