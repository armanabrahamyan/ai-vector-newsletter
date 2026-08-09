"""
src/review.py -- AI Vector pre-release editorial review (structured).

Reads the staged ``issue.json`` for a date, asks the LLM (in the Editor's
voice, drawing on ``EDITORIAL.md``) for a list of structured FINDINGS,
computes a verdict from those findings in CODE, and writes two artifacts:

  * ``data/staging/<date>/review.json`` -- the ``ReviewReport``: every
    finding, the computed verdict, and the provenance needed to re-derive
    it (prompt version, threshold-table version, model, issue hash).
  * ``data/staging/<date>/review.md`` -- the same report RENDERED for a
    human, with the machine-readable YAML frontmatter Phase 1 established.
    It is generated from the report by this module, so the two artifacts
    cannot disagree.

What changed at prompt v1.0, and why
------------------------------------
The reviewer used to write prose and name its own verdict. A prose verdict
can only be read; it cannot be routed, counted, or checked. Three things
follow from the new shape:

1. **Findings, not paragraphs.** Each finding names a target FIELD
   (a story's headline/summary, a section's intro), quotes the offending
   text VERBATIM, and states what kind of fix it needs. That makes the
   review actionable: ``src/revise.py`` acts on the ``text_edit`` findings
   and only those.

2. **The verdict is computed by code** from the finding severities under
   ``config/review_thresholds.yaml`` -- a ratifiable table, outside the
   prompt. The model produces evidence; the table turns evidence into a
   decision. Anyone can re-derive the verdict from ``review.json`` without
   re-running the LLM, and a change in publishing standards is a config
   diff rather than a prompt rewrite.

3. **A deterministic misfire kill.** Any finding whose ``quote`` does not
   appear in the target field's CURRENT text is dropped before it reaches a
   reader (and recorded in ``dropped_findings``). Both documented reviewer
   misfires (2026-07-04) were complaints about text that was not in the
   issue; a substring check ends that class of error outright -- No Token
   Wasted, since no prompt engineering can make a hallucinated quote match.

Fail-closed contract (unchanged from Phase 1)
---------------------------------------------
``green | amber | red`` are editorial outcomes and are only ever reached by
computing the threshold table over real findings. ``unavailable``,
``unparseable``, and ``not_run`` are code-authored states meaning "no
judgement was recorded", and nothing here degrades a failure into a
judgement-shaped default -- a malformed threshold table yields
``unavailable``, never green. The verdict in the returned ``ReviewArtifact``,
the ``computed_verdict`` in ``review.json``, and the ``verdict:`` line in
``review.md`` are the same value written by the same code. The frontmatter
also carries ``issue_sha256``, the hash of the exact ``issue.json`` bytes
the reviewer read, so a consumer (``src/gate.py``, ``src/revise.py``) can
tell whether the issue moved after the review ran. See ``REVIEW_VERDICTS``.

The review NEVER blocks a human-ratified release. It surfaces concerns to
Arman before he runs ``aiv release``; it does gate the unattended path via
``src/gate.py``, which is a statement about the absence of a human, not
about the review.

Owner: LLM Engineer (per docs/internal/TEAM.md). This module is a NEW
*mode* of the existing Editor persona, not a new agent.

Audit tag: review-v1.0-2026-08-02.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src import paths
from src.models import (
    ReviewFinding,
    ReviewReport,
    ReviewTarget,
)


# ---------------------------------------------------------------------------
# Module constants.
# ---------------------------------------------------------------------------

REVIEW_PROMPT_VERSION = "v1.2.1"
"""Versioned prompt string written into ``review.json`` and the
``review.md`` frontmatter so the eval harness can correlate verdict
movement against prompt revisions.

Bump when the prompt content (criteria, instructions, output format)
changes substantively. Audit tag: ``review-v1.2.1-2026-08-09``.

v1.2.1 (2026-08-09): MESSAGE-STRUCTURE-ONLY change -- prompt caching.
The first-attempt prompt BYTES are identical to v1.2; the prompt is now
sent to the Anthropic API as two content blocks (static instruction
prefix with ``cache_control: ephemeral`` + day-specific variable part)
instead of one string, split at the end of ``_REVIEW_INSTRUCTIONS +
"\\n\\n"`` (see ``_build_review_prompt_parts``). The only byte-level
difference is on the JSON-parse RETRY path: corrective text used to be
PREPENDED to the whole prompt (changing byte 0 and forcing a full cache
miss); it is now APPENDED after the variable part so the cached prefix
block stays byte-identical across attempts (the rank v0.6.1 /
summarise trap). Patch-level bump so the archive records the
transition; Eval 9 calibration against v1.2 remains valid because the
model reads the same bytes.

v1.2 (2026-08-09, "digest + synthesis" -- contract R1, DESIGN.md "The
digest"): the reviewer now sees the two new redesign surfaces. Changes,
prompt-content plus the matching code surfaces:
  (a) The rendered issue gains a DIGEST block (index, lead, sentence,
      story_ids per bullet) above the Pulse, a ``synthesis:`` line per
      section (replacing intro_lead/intro_body on redesign issues; the
      legacy pair still renders on archived issues), and a ``signal:``
      line per story (the DESIGN.md tag-derivation recommendation --
      metadata findings about the verb need to see the stored input).
  (b) ``index_issue_fields`` indexes ``digest:<i>:digest_lead`` /
      ``digest:<i>:digest_sentence`` / ``section:<name>:synthesis`` so
      findings on the new fields survive the verbatim-quote check;
      ``target_key`` + ``_build_finding`` learn ``kind="digest"`` with
      the ``digest_index`` locator (ReviewTarget v3).
  (c) New criterion ``digest_shape``: a bullet whose sentence restates a
      take or whose lead echoes a synthesis = major; a lead over 6 words
      or opening on an imperative = major.
  (d) New criterion ``synthesis_shape``: a synthesis whose first
      sentence is an aphorism = minor; a synthesis on a ONE-story
      section = major flagged as a pipeline defect (summarise's n>=2
      rule makes it structurally impossible -- its presence means the
      generation pipeline broke, not the prose).
  (e) ``section_intro`` recalibrated: the section framing lives in the
      single ``synthesis`` field on redesign issues; the legacy
      intro-pair guidance applies only when the issue shows the pair.

v1.1 (2026-08-08, "the take"): the reviewer now sees each story's
``take`` (SummaryBlock.take, schema v4) and reviews its shape. Changes,
all prompt-content plus the matching code surfaces:
  (a) Each story block in the prompt carries a ``take:`` line;
      ``index_issue_fields`` indexes ``story:<id>:take`` so take findings
      pass the verbatim-quote filter (``ReviewTargetField`` gained
      ``"take"`` in the same-day models.py schema bump).
  (b) New criterion ``take_shape``: missing take on a story = major
      (quote the story's final body sentence, field "summary" -- there is
      no take text to quote); take ending '?' or opening on an imperative
      verb = major; two takes sharing a syntactic frame in one issue =
      minor, three or more = major; a take whose proposition repeats the
      section's intro_lead = minor (register collision); hedges, label
      constructions, or universalisms in a take = major. Take criteria
      are SKIPPED entirely when no story in the issue carries a take
      (legacy / pre-v0.22 issues re-reviewed from the archive).
  (c) ``closing_shape`` recalibrated for v0.22 prose: the Pulse plain
      take and the Currents calibrated stake now live in the take FIELD;
      the Pulse body ends on the day's direction and the Currents body on
      a presence-form maturity signal -- the old body-end shapes must not
      be demanded back.

v1.0 (2026-08-02): the STRUCTURED reviewer. Four changes, all
prompt-content:
  (a) Output is JSON findings, not a Markdown verdict document. Each
      finding names a target field, quotes the offending text verbatim,
      and declares a ``fix_kind`` so it can be routed. The ``summary``
      key comes AFTER ``findings`` in the requested key order so the model
      commits to evidence before it characterises the day.
  (b) The model no longer authors a verdict at all -- code computes it
      from finding severities under ``config/review_thresholds.yaml``.
  (c) New criterion ``reputational_liability`` at ``blocking`` severity:
      a named individual or firm carrying an allegation the source does
      not state; a legal/regulatory outcome asserted as settled where the
      source hedged; anything a reader could act on as investment advice.
  (d) The prompt now shows each story's ``story_id`` (so findings can
      target it) and its ``verification`` block from the advisory verify
      stage (so the editor sees the fact-check before judging the prose).
Temperature drops to 0.0: with the verdict computed downstream, run-to-run
variance in the findings buys nothing.

v0.5 (2026-07-04): trust-flag reviewer calibration after two same-day
misfires on summarise v0.20 output. Misfire 1: the review claimed
"trust flags defective ... functioning as absence inventories" on
stories whose text contained ZERO absence forms. Misfire 2: it
requested "(arXiv preprint, single research team)" -- a
default-restating flag its own gate-3 criterion bans -- on a body that
already opened with "An arXiv preprint reverse-engineers...". The
TRUST FLAGS criterion now requires QUOTING the offending flag text
verbatim from the story before any defect finding (no quote = no
finding). At v1.0 that rule generalises: EVERY finding carries a quote,
and code drops the ones that do not match.

v0.4 (2026-07-04): trust-flag gate 3 (informative vs the evidence-class
default; reader-needs study, READING_EXPERIENCE.md §3 + R-8).
v0.3 (2026-07-04): trust flags are presence-form (READING_EXPERIENCE.md R-8).
v0.2 (2026-07-04): Currents closing-shape wording generalised.
v0.1 audit tag: ``review-v0.1-2026-05-31``."""

_REVIEW_LOOKBACK_ISSUES = 3
"""How many previously-released issues to include for drift-watch context.

The review uses these to spot recurring themes, source repetition, voice
collapse across consecutive days, and missing callbacks. Three issues is
enough to see a pattern without burning input tokens on stale history."""

_REVIEW_TIMEOUT_DEFAULT = 180.0
"""Seconds. The review prompt asks for a multi-thousand-token structured
response; default-60 (matching ``rank.py``) timed out the 2026-05-29
staging call mid-generation. Bumped to 180s so a slow-but-successful
response still lands. Operators can override via
``LLM_REVIEW_TIMEOUT_SECONDS``."""

_REVIEW_TEMPERATURE = 0.0
"""Temperature for the review call. Zero since v1.0: the reviewer's output
is evidence (quotes + severities), not prose, and the verdict is computed
downstream. Variance across same-day re-runs would move the verdict without
moving the issue, which is exactly what the code-computed verdict exists to
prevent."""

# 4000 -> 8000 (2026-08-09): the redesign surfaces (digest + syntheses +
# takes) grew finding-dense responses past 4000, truncating JSON mid-array
# -> `unparseable` -> a spurious hold. Observed twice on live probes (cap
# hit exactly) and on 2 of 15 Eval 9 clean fixtures. A ceiling, not a
# spend: output bills only what is generated.
_REVIEW_MAX_TOKENS = 8000
"""Comfortable headroom for ~20 findings with quotes and instructions.
An earlier 2000-token budget truncated mid-output on the 2026-05-29
staging issue."""

_JSON_RETRY_BUDGET = 1
"""One corrective retry on unparseable JSON, mirroring
``rank.JSON_RETRY_BUDGET`` and the verify stage. Beyond one retry we are
paying for the same failure twice."""

_DEFAULT_THRESHOLDS_PATH = Path("config/review_thresholds.yaml")
"""The ratifiable severity -> verdict table. Parsed and validated on every
run; a malformed table fails the stage closed (``unavailable``)."""

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

REVIEW_EDITORIAL_VERDICTS: frozenset[str] = frozenset(
    {VERDICT_GREEN, VERDICT_AMBER, VERDICT_RED}
)
"""The three verdicts that represent an editorial judgement.

Since v1.0 these are reached ONLY by computing the threshold table over
findings -- no model output is trusted to name one, and the code-reserved
tokens below are not reachable from the table either (validated at load).
The model must not be able to claim a machine state it does not own, and it
no longer gets to claim an editorial one either."""

# Backwards-compatible alias: this name held the LLM-authored vocabulary
# when the model wrote its own verdict (prompt <= v0.5). It is the same set.
REVIEW_LLM_VERDICTS = REVIEW_EDITORIAL_VERDICTS

REVIEW_VERDICTS: frozenset[str] = REVIEW_EDITORIAL_VERDICTS | frozenset(
    {VERDICT_UNAVAILABLE, VERDICT_UNPARSEABLE, VERDICT_NOT_RUN}
)
"""Every verdict a ``ReviewArtifact`` (and the artifacts written beside it)
may carry. The three code-authored tokens mean:

``unavailable`` -- the review could not run (no staged issue, unreadable
issue, unreadable threshold table, LLM transport failure). ``unparseable``
-- the review ran but its output could not be read strictly enough to trust.
``not_run`` -- the stage was invoked in dry-run and produced no judgement.

None of the three is an editorial pass. A consumer that authorises anything
must treat ``green`` (and only ``green``) as permission; every other token
withholds it."""

_VALID_VERDICTS = REVIEW_EDITORIAL_VERDICTS  # legacy alias

_SEVERITIES: tuple[str, ...] = ("blocking", "major", "minor", "note")
"""Severity vocabulary, most-severe first. Mirrors
``models.ReviewSeverity``; the threshold table is validated against it."""

REVIEW_CRITERIA: frozenset[str] = frozenset({
    "shape_integrity",
    "pulse_pick",
    "voice_adherence",
    "closing_shape",
    "take_shape",
    "section_routing",
    "section_intro",
    "synthesis_shape",
    "digest_shape",
    "trust_flags",
    "factual_grounding",
    "reputational_liability",
    "drift",
    "finance_angle",
})
"""The criterion vocabulary the prompt publishes. Findings carrying an
unrecognised criterion are KEPT and logged, not dropped: a real concern
filed in an unexpected bucket is still a real concern, and silently
discarding it would trade a taxonomy nit for lost signal."""


# ---------------------------------------------------------------------------
# Artifact paths.
#
# ``review.md`` has lived at this path since Phase 1; ``review.json`` is new
# at v1.0. Both are constructed here rather than in ``src/paths.py`` because
# that module is the Architect's -- a helper there is the right end state and
# is flagged in the hand-off note at module end.
# ---------------------------------------------------------------------------

def review_md_path(date: _dt.date, *, canonical: bool = False) -> Path:
    """Path to ``review.md`` for a date. Staging by default; the released
    copy exists because the gate reads the file (DESIGN.md "review.md")."""
    base = paths.released_dir(date) if canonical else paths.staging_dir(date)
    return base / "review.md"


def review_json_path(date: _dt.date, *, canonical: bool = False) -> Path:
    """Path to ``review.json`` -- the ``ReviewReport`` sidecar (v1.0)."""
    base = paths.released_dir(date) if canonical else paths.staging_dir(date)
    return base / "review.json"


# ---------------------------------------------------------------------------
# Public return type.
# ---------------------------------------------------------------------------

@dataclass
class ReviewArtifact:
    """Lightweight summary the pipeline / CLI uses to print the terminal
    line. The substantive artifacts are ``review.json`` and ``review.md``
    on disk; this structure exposes just what callers need to log a
    one-liner.

    ``verdict`` is one of ``REVIEW_VERDICTS``. ``path`` is the path to
    ``review.md`` -- always written, except on the dry-run path where
    nothing is written at all. ``report`` is the full ``ReviewReport`` when
    one was produced (``None`` only on the dry-run path), so a caller that
    wants the findings does not have to re-read the file it just wrote.

    The verdict here, the ``computed_verdict`` in ``review.json``, and the
    ``verdict:`` line in ``review.md`` are written from the same value by
    the same code, so they cannot disagree.
    """
    date: _dt.date
    verdict: str
    one_line: str
    path: Path
    report: ReviewReport | None = None

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


class ReviewThresholdError(ValueError):
    """The severity -> verdict table could not be read or does not validate.

    Raised by ``load_thresholds`` and caught by ``run_review``, which turns
    it into ``verdict: unavailable``. Deliberately NOT recoverable with a
    built-in default: a verdict computed under a table nobody ratified is a
    verdict nobody ratified."""


# ---------------------------------------------------------------------------
# Threshold table -- parse, validate, apply. All plain code (No Token
# Wasted): turning four integers into one of three tokens is not judgment.
# ---------------------------------------------------------------------------

def load_thresholds(
    path: Path = _DEFAULT_THRESHOLDS_PATH,
) -> dict[str, Any]:
    """Load and validate ``config/review_thresholds.yaml``.

    Returns a normalised table: ``{"version": str, "rules": [{"verdict":
    str, "when": {severity: int}, "reason": str}], "default_verdict": str}``.

    Raises ``ReviewThresholdError`` on anything it cannot fully account
    for -- a missing file, a non-mapping document, an unknown severity key,
    a verdict outside the editorial vocabulary, a non-positive minimum, an
    empty rule list. We validate strictly rather than skipping unreadable
    entries because a rule we silently ignore is a hold that silently stops
    happening.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReviewThresholdError(
            f"could not read the review threshold table at {path}: {exc}"
        ) from exc
    except yaml.YAMLError as exc:
        raise ReviewThresholdError(
            f"review threshold table at {path} is not valid YAML: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise ReviewThresholdError(
            f"review threshold table at {path} must be a mapping; got "
            f"{type(raw).__name__}"
        )

    version = str(raw.get("version") or "").strip()
    if not version:
        raise ReviewThresholdError(
            f"review threshold table at {path} has no 'version'; the verdict "
            "must be attributable to a specific ratified table"
        )

    default_verdict = str(raw.get("default_verdict") or "").strip().lower()
    if default_verdict not in REVIEW_EDITORIAL_VERDICTS:
        raise ReviewThresholdError(
            f"review threshold table 'default_verdict' must be one of "
            f"{sorted(REVIEW_EDITORIAL_VERDICTS)}; got {default_verdict!r}"
        )

    rules_raw = raw.get("rules")
    if not isinstance(rules_raw, list) or not rules_raw:
        raise ReviewThresholdError(
            f"review threshold table at {path} must carry a non-empty "
            "'rules' list"
        )

    rules: list[dict[str, Any]] = []
    for index, entry in enumerate(rules_raw):
        where = f"rules[{index}]"
        if not isinstance(entry, dict):
            raise ReviewThresholdError(f"{where} must be a mapping")
        verdict = str(entry.get("verdict") or "").strip().lower()
        if verdict not in REVIEW_EDITORIAL_VERDICTS:
            raise ReviewThresholdError(
                f"{where}.verdict must be one of "
                f"{sorted(REVIEW_EDITORIAL_VERDICTS)}; got {verdict!r}"
            )
        when_raw = entry.get("when")
        if not isinstance(when_raw, dict) or not when_raw:
            raise ReviewThresholdError(
                f"{where}.when must be a non-empty mapping of severity -> "
                "{min: N}"
            )
        when: dict[str, int] = {}
        for severity, spec in when_raw.items():
            key = str(severity).strip().lower()
            if key not in _SEVERITIES:
                raise ReviewThresholdError(
                    f"{where}.when has unknown severity {severity!r}; valid: "
                    f"{list(_SEVERITIES)}"
                )
            if not isinstance(spec, dict) or "min" not in spec:
                raise ReviewThresholdError(
                    f"{where}.when.{key} must be a mapping carrying 'min'"
                )
            try:
                minimum = int(spec["min"])
            except (TypeError, ValueError) as exc:
                raise ReviewThresholdError(
                    f"{where}.when.{key}.min must be an integer; got "
                    f"{spec['min']!r}"
                ) from exc
            if minimum < 1:
                raise ReviewThresholdError(
                    f"{where}.when.{key}.min must be >= 1; got {minimum}. A "
                    "rule that fires on zero findings would fire on every "
                    "issue."
                )
            when[key] = minimum
        rules.append({
            "verdict": verdict,
            "when": when,
            "reason": str(entry.get("reason") or "").strip(),
        })

    return {
        "version": version,
        "rules": rules,
        "default_verdict": default_verdict,
    }


def compute_verdict(
    counts: dict[str, int], table: dict[str, Any]
) -> tuple[str, str]:
    """Apply the threshold table to a severity tally.

    Returns ``(verdict, reason)``. Rules are evaluated IN ORDER and the
    first whose every named severity meets its minimum wins; when none
    matches, the table's ``default_verdict`` applies. Order is the whole
    mechanism -- the red rules are listed first so a blocking finding
    cannot be talked down by an amber rule that also matches.
    """
    for rule in table["rules"]:
        if all(
            counts.get(severity, 0) >= minimum
            for severity, minimum in rule["when"].items()
        ):
            condition = ", ".join(
                f"{severity} >= {minimum}"
                for severity, minimum in rule["when"].items()
            )
            return rule["verdict"], f"{condition} ({rule['reason']})".strip()
    return (
        table["default_verdict"],
        "no rule matched; findings are notes only, or there are none",
    )


# ---------------------------------------------------------------------------
# Verbatim-quote matching -- the deterministic misfire kill.
# ---------------------------------------------------------------------------

_QUOTE_TRANSLATIONS = {
    ord("‘"): "'", ord("’"): "'",   # curly single quotes
    ord("“"): '"', ord("”"): '"',   # curly double quotes
    ord("–"): "-", ord("—"): "-",   # en / em dash
    ord("…"): "...",                      # ellipsis
    ord(" "): " ",                        # non-breaking space
}


def normalise_for_quote_match(text: str) -> str:
    """Normalise text for verbatim-quote comparison.

    Unicode-normalises (NFKC), folds typographic punctuation to ASCII,
    collapses whitespace runs, and casefolds. The comparison stays a
    SUBSTRING test on the result.

    Why not byte-exact. The failure this check exists to catch is a finding
    about text that is not in the issue -- a hallucinated complaint. A model
    that re-wraps a line, straightens a curly apostrophe, or lowercases a
    sentence-initial word is still pointing at real text, and dropping that
    finding would cost real signal for no safety. A model that invented the
    sentence fails this test under any normalisation, because none of these
    transformations can conjure words that were never written.
    """
    folded = unicodedata.normalize("NFKC", text).translate(_QUOTE_TRANSLATIONS)
    return re.sub(r"\s+", " ", folded).strip().casefold()


def quote_present(quote: str, text: str) -> bool:
    """True when ``quote`` appears in ``text`` under
    ``normalise_for_quote_match``. Empty quotes are never present -- a
    finding with nothing to point at has no evidence.

    Public API: ``src/revise.py`` uses the same function for its
    deletion-class check, so "the quote is there" means one thing across
    both stages."""
    needle = normalise_for_quote_match(quote)
    if not needle:
        return False
    return needle in normalise_for_quote_match(text)


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def run_review(
    date: _dt.date | None = None, dry_run: bool = False
) -> ReviewArtifact:
    """Run the editorial review for one date.

    Loads the staged ``issue.json``, gathers up to the last
    ``_REVIEW_LOOKBACK_ISSUES`` released issues, calls the LLM with the
    review prompt, filters the returned findings against the issue text,
    computes the verdict from ``config/review_thresholds.yaml``, and writes
    ``review.json`` + ``review.md`` under ``data/staging/<date>/``.

    Failure-soft contract: any LLM-side failure (transport, timeout, parse,
    missing env vars) and any threshold-table failure is logged at WARNING,
    written as ``verdict: unavailable`` (or ``unparseable`` when the call
    succeeded but its output could not be read), and returned normally. The
    pipeline must continue.

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
    review_path = review_md_path(run_date)

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
        _LOG.warning("review: %s -- writing unavailable review", msg)
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

    # Load the threshold table BEFORE spending an LLM call: a table we
    # cannot read means we could not compute a verdict from any findings,
    # so the call would be wasted tokens on an answer we must discard.
    try:
        thresholds = load_thresholds()
    except ReviewThresholdError as exc:
        msg = f"threshold table unusable: {exc}"
        _LOG.warning("review: %s", msg)
        return _write_unavailable(
            run_date, review_path, msg, issue_sha256=issue_sha256,
        )

    recent_issues = _load_recent_released_issues(run_date, _REVIEW_LOOKBACK_ISSUES)
    prefix, variable = _build_review_prompt_parts(issue_payload, recent_issues)
    timeout = _resolve_timeout()
    model = _resolve_review_model()

    raw, parsed = "", None
    attempts = _JSON_RETRY_BUDGET + 1
    current_variable = variable
    for attempt in range(1, attempts + 1):
        try:
            raw = _call_review_llm((prefix, current_variable), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 -- never fail the pipeline
            msg = f"LLM call failed: {type(exc).__name__}: {exc}"
            _LOG.warning("review: %s -- writing unavailable review", msg)
            return _write_unavailable(
                run_date, review_path, msg, issue_sha256=issue_sha256,
                thresholds_version=thresholds["version"], llm_model=model,
            )
        parsed = _parse_findings_json(raw)
        if parsed is not None:
            break
        _LOG.warning(
            "review: findings JSON parse failed (attempt %d/%d)",
            attempt, attempts,
        )
        # v1.2.1 cache discipline: corrective text is APPENDED to the
        # variable part, never prepended to the whole prompt -- prepending
        # would change byte 0 of the cached prefix and force a full cache
        # miss plus a wasted cache write on the retry (the rank v0.6.1 /
        # summarise trap; pinned by
        # tests/test_review.py::TestPromptCacheSplit).
        current_variable = (
            variable
            + "\n\nCORRECTION -- Your previous response was not valid JSON "
            "matching the schema above. Return JSON ONLY -- no markdown "
            "fences, no prose, no commentary -- with a top-level "
            "\"findings\" array."
        )

    if parsed is None:
        return _write_unparseable(
            run_date, review_path, raw,
            issue_sha256=issue_sha256,
            thresholds_version=thresholds["version"],
            llm_model=model,
        )

    raw_findings, llm_summary = parsed
    kept, dropped, malformed = _resolve_and_filter_findings(
        raw_findings, issue_payload,
    )

    counts = {severity: 0 for severity in _SEVERITIES}
    for finding in kept:
        counts[finding.severity] += 1
    verdict, verdict_reason = compute_verdict(counts, thresholds)

    one_line = _resolve_one_line(llm_summary, counts, verdict)
    note_parts = [f"verdict rule: {verdict_reason}"]
    if dropped:
        note_parts.append(
            f"{len(dropped)} finding(s) dropped: quote not found verbatim "
            "in the target text, or criterion inapplicable to this issue"
        )
    if malformed:
        note_parts.append(f"{malformed} finding(s) dropped: malformed shape")

    report = ReviewReport(
        generated_at=_dt.datetime.now(_dt.timezone.utc),
        computed_verdict=verdict,  # type: ignore[arg-type]
        one_line=one_line,
        findings=kept,
        dropped_findings=dropped,
        prompt_version=REVIEW_PROMPT_VERSION,
        thresholds_version=thresholds["version"],
        llm_model=model,
        issue_sha256=issue_sha256,
        note=" | ".join(note_parts)[:2000],
    )

    _write_report_json(review_json_path(run_date), report)
    written = _write_review_artifact(
        run_date,
        render_review_markdown(report, run_date, issue_payload),
        {"llm_model": model},
        verdict=verdict,
        one_line=one_line,
        issue_date=run_date.isoformat(),
        issue_shape=_extract_issue_shape(issue_payload),
        issue_sha256=issue_sha256,
        extra_frontmatter=_findings_frontmatter(report, counts),
    )
    _LOG.info(
        "review: %s verdict=%s findings=%d (dropped=%d) one_line=%r -> %s",
        run_date.isoformat(), verdict, len(kept), len(dropped), one_line,
        written,
    )
    return ReviewArtifact(
        date=run_date, verdict=verdict, one_line=one_line, path=written,
        report=report,
    )


def _resolve_one_line(
    llm_summary: str, counts: dict[str, int], verdict: str
) -> str:
    """Pick the frontmatter one-liner.

    Prefers the model's own ``summary`` (it read the issue; we did not), and
    falls back to a severity tally so the line is never empty. Newlines and
    colons are stripped because this value lands in a flat YAML scalar that
    ``src/gate.py`` parses with a hand-rolled ``key: value`` split."""
    cleaned = re.sub(r"\s+", " ", (llm_summary or "")).strip().replace(":", " -")
    if cleaned:
        return cleaned[:200]
    tally = ", ".join(f"{counts[s]} {s}" for s in _SEVERITIES if counts[s])
    return f"{verdict.upper()} -- {tally or 'no findings'}"


def _findings_frontmatter(
    report: ReviewReport, counts: dict[str, int]
) -> dict[str, Any]:
    """Extra frontmatter keys carrying the finding tallies.

    Flat scalars only. ``src/gate.py`` parses this block with a hand-rolled
    ``key: value`` splitter that has no notion of nesting, so a nested
    mapping here would read back as a set of top-level keys and could
    collide with a real one."""
    return {
        "findings_total": len(report.findings),
        "findings_by_severity": " ".join(
            f"{severity}={counts[severity]}" for severity in _SEVERITIES
        ),
        "findings_dropped": len(report.dropped_findings),
        "thresholds_version": report.thresholds_version,
    }


# ---------------------------------------------------------------------------
# Prompt assembly.
# ---------------------------------------------------------------------------

_REVIEW_INSTRUCTIONS = """\
You are the AI Vector EDITOR running a pre-release review on the staged
issue below. You are NOT writing the issue and you are NOT deciding whether
it ships. You are producing EVIDENCE: a list of specific, quoted findings
that Arman (and an automated reviser) can act on.

You do NOT return a verdict. Code computes the verdict from the severities
you assign, using a separately ratified threshold table. Assign severity
honestly on the merits of each finding; do not reason about what verdict
your set of findings will add up to.

EVERY FINDING CARRIES A VERBATIM QUOTE
======================================
The `quote` field must be an EXACT span copied from the field you are
pointing at -- the story's headline, summary, or take; the section's
synthesis (or legacy intro_lead / intro_body); or a digest bullet's lead or
sentence, as shown below. Code checks each quote against the live text and
SILENTLY DROPS any finding whose quote is not found. A finding you cannot
quote is a finding you cannot make. Do not paraphrase into the quote field,
do not quote the prompt's own labels, and do not quote text from a prior
issue.

Report only what is actually there. If a section is fine, say nothing about
it -- an empty findings list is a legitimate and common answer.

CRITERIA
========
Use these `criterion` tokens.

reputational_liability  -- THE HIGHEST BAR. Severity is always "blocking".
  Flag any of:
    * A named individual or firm carrying an allegation, wrongdoing,
      failure, or motive that the source does not state. Attaching a claim
      to a name the source did not attach it to is the most expensive error
      this publication can make.
    * A legal or regulatory outcome asserted as SETTLED where the source
      hedged: "proposed" written as "adopted", "alleged" as "found",
      "under consultation" as "in force", "sued" as "liable".
    * Anything a reader could act on as INVESTMENT ADVICE: a company,
      ticker, or security framed as a buy / sell / hold; a price, valuation,
      or share-price direction stated as expectation; "positions itself to
      outperform"; "the winner here is X". AI Vector reports on companies;
      it never advises on them.
  If a rewrite fixes it, fix_kind "text_edit". If the claim has to go
  entirely or needs a source it does not have, use "structural" or
  "sourcing".

factual_grounding  -- the VERIFICATION block below carries the advisory
  fact-check for each story. Where a claim is marked "contradicted", check
  whether the prose still asserts it: if so, flag it (major at minimum;
  blocking when the contradicted claim is in the headline or names a person
  or firm). Where a claim is "unsupported" -- the source does not assert it
  -- flag the prose that states it as bare fact. Do NOT flag "unverifiable":
  that means the excerpt did not cover the claim, which is not an error.

shape_integrity  -- section counts vs caps (1 Pulse / up to 4 Big Picture /
  up to 5 Hands-On / up to 8 Currents), and whether a thin section reflects
  a genuinely thin day (fine) or a routing failure upstream (flag it).

pulse_pick  -- does the Pulse story carry the day's editorial position? Is
  its sourcing credible? If it is a recurrence, is the novelty earned?

voice_adherence  -- per EDITORIAL.md, per section:
  * Big Picture: named actors + first-order consequence framing. Lead with
    WHO and what changes for them.
  * Hands-On: tool / repo / version / config in the headline noun phrase.
  * Currents: conditional or hedged opening ("Early signal:"; "If X holds:").
  Flag hedge accumulation, deferral ("it remains to be seen", "time will
  tell"), and adjectives doing the work of an editorial position.

closing_shape  -- the last sentence of each story's BODY (since the take
  field exists, two shapes moved out of the body -- do not demand them
  back):
  * Pulse: the body ends on the day's DIRECTION in plain editorial prose;
    the plain-take judgement lives in the take field now. Flag a body
    that ends by restating the take, or a question/prescription ending.
  * Big Picture: a STRATEGIC QUESTION (unchanged -- still the body's last
    sentence).
  * Hands-On: an IMPERATIVE ACTION sharpened to a specific artefact +
    trigger (unchanged). Generic "test before you trust" fails the shape.
  * Currents: the body ends on a PRESENCE-FORM maturity signal (what
    exists and what it is worth today); the two-sided CALIBRATED STAKE
    lives in the take field now. Judge the take's two-sidedness under
    take_shape, not here. Flag a Currents body that still carries a full
    "If X, Y; if not, Z" stake AND a stake-bearing take -- say it once.
  On a legacy issue with no takes anywhere, judge the pre-take shapes
  instead (Pulse plain-take close, Currents calibrated-stake close).

take_shape  -- the `take:` line under each story is the publication's
  position: ONE declarative sentence, 8-16 words (hard cap 18; Currents
  may run to 22 for genuine two-sidedness), present or present-perfect.
  The operational test: "It is now the case that [take]" must parse. The
  take is NOT the close -- the close stays in the body and hands the turn
  to the reader; the take states what the publication holds true.
  SKIP THIS CRITERION ENTIRELY when NO story in the issue carries a take
  (a legacy issue predating the field).
  Flag, by severity:
  * A story with NO take (while other stories have one): major. There is
    no take text to quote, so target the story's `summary` field and
    quote its FINAL SENTENCE verbatim; instruction = write the missing
    take. fix_kind "text_edit".
  * A take ending in '?' or OPENING on an imperative verb: major (quote
    the take, field `take`).
  * Hedges (may / could / potentially / appears / arguably), label
    constructions ("So what:", "Bottom line:", "This matters because"),
    or universalisms ("changes everything"): major.
  * TWO takes in the issue sharing a syntactic frame (same scaffold,
    different nouns -- e.g. both "X is now a Y problem, not a Z one"):
    minor, filed on the LATER story's take. THREE or more sharing a
    frame: major.
  * A take whose proposition repeats the section's intro_lead (register
    collision -- the reader meets the same idea twice in adjacent
    registers): minor, quoting the take.
  * A take that restates a body sentence rather than adding the position
    the body stopped short of: minor.

section_routing  -- a story whose voice and content belong in a different
  section than the one it is in. fix_kind is "structural" (code cannot fix
  a routing error by editing prose).

section_intro  -- does the section's framing text (the `synthesis:` line;
  on older issues the intro_lead/intro_body pair) frame the pattern ACROSS
  the section's stories in that section's register? The Currents framing is
  MANDATORY and must name the aggregate motion direction.

synthesis_shape  -- the `synthesis:` line under a section head is ONE
  italic paragraph, 2-3 sentences, section-anchored (it names the pattern;
  the stories carry the specifics). SKIP this criterion when the issue
  shows no synthesis lines (legacy issues carry the intro pair instead).
  Flag, by severity:
  * A synthesis whose FIRST SENTENCE is an aphorism -- a detachable
    slogan-shaped fragment ("Ship the plumbing first.") rather than a
    sentence about today's stories: minor (quote the first sentence,
    field `synthesis`).
  * A synthesis on a section showing exactly ONE story: major, fix_kind
    "structural". This is a PIPELINE DEFECT, not prose to polish -- the
    generation rule requires two or more stories before a synthesis is
    written, so its presence means the pipeline broke; say so in the
    instruction. Quote the synthesis.

digest_shape  -- the DIGEST block ("The 30-second read") above the Pulse:
  each bullet is a bold 3-6 word lead naming what happened plus ONE
  concrete sentence, STORY-ANCHORED (falsifiable against its cited
  stories). SKIP this criterion entirely when the issue has no DIGEST
  block. Target digest findings with kind "digest", the bullet's index,
  and field `digest_lead` or `digest_sentence`. Flag, by severity:
  * A bullet's sentence that RESTATES a story's take, or a lead that
    echoes a section synthesis (same proposition, same or reshuffled
    words): major. The digest compresses the STORIES; the takes and
    syntheses already own their propositions.
  * A lead over 6 words, or a lead opening on an imperative verb: major
    (quote the lead, field `digest_lead`).
  * A lead that is a question, or names an artifact a senior practitioner
    would not recognise: major.

trust_flags  -- flags must be PRESENCE-FORM: they characterise the evidence
  that EXISTS ("a second lab replicated it", "the vendor benchmarked its
  competitor's model"). Two defects to flag, both by quoting the offending
  text:
    * ABSENCE INVENTORY ("no code yet", "not yet peer-reviewed") -- a
      defect, never a missing virtue to be demanded back.
    * DEFAULT-RESTATING flags ("a preprint from a single research team";
      "vendor-published benchmark" when the body already names the vendor)
      -- noise; the fix is DELETING the flag.
  A source-class noun phrase already in the body ("an arXiv preprint",
  "Anthropic's release notes") IS the calibration. NEVER request a
  parenthetical flag on top of it.

drift  -- against the prior released issues supplied below: recurring
  themes covered the same way without progression; the same source three
  days running on similar topics; a story extending yesterday's without
  referencing it; section intros collapsing into one register. fix_kind is
  usually "carry_forward" or "text_edit".

finance_angle  -- does the financial-services lens land where it appears,
  or is it decoration? Flag any story surfaced only for a weak FS angle.

SEVERITY
========
blocking -- do not publish as-is. Reserved for reputational_liability and
            for factual claims the issue cannot stand behind.
major    -- a substantive editorial defect a reader would notice.
minor    -- a real improvement, not a defect.
note     -- an observation; nothing to do today.

Be honest and be sparing. Inflating a minor to a major to "make sure it gets
seen" corrupts the verdict for everyone downstream.

FIX KIND
========
text_edit     -- rewriting the quoted text in place fixes it. An automated
                 reviser acts on these and ONLY these, so the `instruction`
                 must be specific enough to execute without re-reading the
                 issue, and must not require facts that are not already in
                 the story.
structural    -- needs a different story, a different section, or a
                 different order.
sourcing      -- needs another source, a link, or corroboration.
metadata      -- signal pill, audience tags, section assignment.
carry_forward -- nothing to do today; remember it tomorrow.
human         -- needs Arman's judgement, not an edit.

OUTPUT
======
Return ONLY a single JSON object. No markdown fences, no commentary before
or after. `findings` comes FIRST: commit to the evidence before you
characterise the day.

{
  "findings": [
    {
      "target": {
        "kind": "story",
        "story_id": "<the story_id shown with the story; omit for section/digest targets>",
        "section": "<pulse | big_picture | hands_on | currents>",
        "digest_index": "<0-based bullet index; DIGEST targets only>",
        "field": "<headline | summary | take | synthesis | intro_lead | intro_body | digest_lead | digest_sentence>"
      },
      "criterion": "<one of the criterion tokens above>",
      "severity": "<blocking | major | minor | note>",
      "quote": "<EXACT span copied from that field's text>",
      "fix_kind": "<text_edit | structural | sourcing | metadata | carry_forward | human>",
      "instruction": "<what to do, imperative, specific>"
    }
  ],
  "summary": "<one line, 30-90 characters: the editorial read of the day>"
}

For a story target, set "kind": "story" and give the story_id exactly as
shown. For a section target, set "kind": "section", omit "story_id", and
set "field" to "synthesis" (or "intro_lead" / "intro_body" only when the
issue shows that legacy pair). For a digest target, set "kind": "digest",
omit "story_id" and "section", set "digest_index" to the bullet's index
exactly as shown in the DIGEST block, and set "field" to "digest_lead" or
"digest_sentence".
"""


def _build_review_prompt_parts(
    issue: dict[str, Any], recent_issues: list[dict[str, Any]]
) -> tuple[str, str]:
    """Assemble the LLM review prompt as ``(shared_prefix, variable_part)``.

    ``issue`` and ``recent_issues`` are raw issue.json payloads (dicts) --
    we work from the parsed JSON rather than constructing the pydantic
    ``Issue`` so we don't crash on schema-version skew between staged and
    released issues; the review is a best-effort read.

    Prompt caching (2026-08-09, v1.2.1): the concatenation
    ``shared_prefix + variable_part`` is byte-identical to the historical
    v1.2 single-string prompt -- the split changes message STRUCTURE only,
    never bytes (pinned by tests/test_review.py::TestPromptCacheSplit).
    The split point is the end of ``_REVIEW_INSTRUCTIONS + "\\n\\n"``:
    everything before it (the full instruction block -- criteria, severity
    and fix-kind vocabulary, output schema) is a module-level literal with
    no interpolation, so it is byte-identical for every call within a run
    and across days as long as this file is unchanged. Everything
    day-varying -- the staged issue, the prior-issue drift-watch context
    (which changes daily as the archive advances) -- lands in the variable
    part. ``rank._llm_call_anthropic`` marks the prefix with
    ``cache_control: {"type": "ephemeral"}`` (5-min TTL, refreshed free on
    every hit); non-Anthropic providers receive the joined string,
    byte-identical to the pre-split prompt.

    Verified 2026-08-09 with real paired calls (claude-sonnet-4-6): the
    prefix is 12,459 bytes = 3,254 billed tokens -- comfortably above the
    1,024-token cache minimum. Call 1 ``cache_creation_input_tokens=3254``,
    call 2 ``cache_read_input_tokens=3254``.
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
            "and intros only). Do NOT quote from these; they are context, not "
            "the issue under review:\n\n" + recent_blocks
        )
    else:
        recent_section = (
            "\n\n(No prior released issues available within the lookback "
            "window. Skip the drift-watch comparison.)"
        )
    # The cacheable shared prefix. MUST stay byte-identical across calls:
    # nothing day-specific may leak in before the split point.
    prefix = f"{_REVIEW_INSTRUCTIONS}\n\n"
    variable = f"{today_block}{recent_section}\n"
    return prefix, variable


def _build_review_prompt(
    issue: dict[str, Any], recent_issues: list[dict[str, Any]]
) -> str:
    """Joined single-string form of ``_build_review_prompt_parts`` -- kept
    for audit tooling and tests that want the full prompt text.
    Byte-identical to the pre-v1.2.1 single-string prompt by
    construction."""
    prefix, variable = _build_review_prompt_parts(issue, recent_issues)
    return prefix + variable


def _format_issue_for_prompt(
    payload: dict[str, Any], *, label: str, compact: bool = False
) -> str:
    """Render an issue payload as a prompt-friendly block.

    Since v1.0 each story carries its ``story_id`` (findings target it) and
    its ``verification`` rollup from the advisory verify stage (the editor
    judges the prose knowing what the fact-checker found).

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

    def _story_lines(story: dict[str, Any], section_name: str) -> None:
        story_id = story.get("story_id") or ""
        if story_id and not compact:
            lines.append(f"  - story_id: {story_id}")
            lines.append(f"    section: {section_name}")
            lines.append(f"    headline: {story.get('headline', '')}")
        else:
            lines.append(f"  - headline: {story.get('headline', '')}")
        if not compact:
            lines.append(f"    summary: {story.get('summary', '')}")
            # v1.1: the take is reviewable text (take_shape criterion).
            # Shown only when present -- a legacy issue renders no take
            # lines at all, which is the signal to skip take criteria.
            take = story.get("take")
            if isinstance(take, str) and take.strip():
                lines.append(f"    take: {take.strip()}")
            # v1.2: the stored signal is the input the rendered story tag
            # is derived from (DESIGN.md tag-derivation ruling) -- shown so
            # metadata findings about the verb are grounded in what is
            # actually stored.
            signal = story.get("signal")
            if isinstance(signal, str) and signal.strip():
                lines.append(f"    signal: {signal.strip()}")
            prior = story.get("prior_coverage_ref")
            if prior:
                lines.append(f"    prior_coverage_ref: {prior}")
            verification = _format_verification(story.get("verification"))
            if verification:
                lines.append(f"    verification: {verification}")
        srcs = story.get("source_urls") or []
        if srcs:
            lines.append(
                f"    sources: {len(srcs)} -- {', '.join(map(str, srcs[:3]))}"
            )

    # v1.2 (R1): the DIGEST block renders first -- it is the first thing a
    # reader sees, so it is the first thing the reviewer sees. Indices are
    # 0-based and are what a digest finding's `digest_index` must echo.
    digest = payload.get("digest")
    if not compact and isinstance(digest, list) and digest:
        lines.append("")
        lines.append('DIGEST ("The 30-second read", rendered above the Pulse):')
        for i, bullet in enumerate(digest):
            if not isinstance(bullet, dict):
                continue
            lines.append(f"  - digest_index: {i}")
            lines.append(f"    lead: {bullet.get('lead', '')}")
            lines.append(f"    sentence: {bullet.get('sentence', '')}")
            ids = bullet.get("story_ids") or []
            lines.append(f"    story_ids: {', '.join(map(str, ids))}")

    pulse = payload.get("pulse") or {}
    pulse_stories = pulse.get("stories") or []
    lines.append("")
    lines.append("PULSE:")
    for story in pulse_stories:
        if isinstance(story, dict):
            _story_lines(story, "pulse")

    for section in payload.get("sections") or []:
        if not isinstance(section, dict):
            continue
        name = section.get("name", "?")
        stories = section.get("stories") or []
        synthesis = section.get("synthesis") or ""
        intro_lead = section.get("intro_lead") or ""
        intro_body = section.get("intro_body") or ""
        lines.append("")
        lines.append(f"SECTION {name} ({len(stories)} stories):")
        # v1.2: redesign issues carry ONE synthesis paragraph; archived
        # issues carry the legacy pair (mutually exclusive by contract).
        if synthesis:
            lines.append(f"  synthesis: {synthesis}")
        if intro_lead:
            lines.append(f"  intro_lead: {intro_lead}")
        if intro_body:
            lines.append(f"  intro_body: {intro_body}")
        for story in stories:
            if isinstance(story, dict):
                _story_lines(story, str(name))
    return "\n".join(lines)


def _format_verification(verification: Any) -> str:
    """One-line rendering of a story's denormalised ``StoryVerification``.

    Shows only what the editor can act on: the flagged claims. A story whose
    claims are all supported or unverifiable renders as "clean" -- the full
    claim list would be several hundred tokens of agreement per story, and
    agreement changes nothing about the prose.

    Returns ``""`` when the story was never verified, which is honest:
    absent verification is NOT a clean bill of health, and saying nothing is
    better than implying one.
    """
    if not isinstance(verification, dict):
        return ""
    claims = verification.get("claims")
    if not isinstance(claims, list):
        return ""
    flagged: list[str] = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        verdict = str(claim.get("verdict") or "")
        if verdict not in ("contradicted", "unsupported"):
            continue
        location = str(claim.get("location") or "body")
        text = str(claim.get("claim") or "")[:160]
        source_span = str(claim.get("source_span") or "")[:160]
        entry = f"[{verdict}/{location}] {text}"
        if source_span:
            entry += f" || source says: {source_span}"
        flagged.append(entry)
    if not flagged:
        return f"clean ({len(claims)} claims checked, none flagged)"
    return f"{len(flagged)} FLAGGED -- " + " ;; ".join(flagged)


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
# Findings parsing + the verbatim-quote filter.
# ---------------------------------------------------------------------------

def _parse_findings_json(raw: str) -> tuple[list[dict[str, Any]], str] | None:
    """Parse the reviewer's JSON into ``(raw_findings, summary)``.

    Returns ``None`` on structural failure (no JSON object, or no
    ``findings`` array) so the caller can retry once. An EMPTY findings
    array is a valid, successful parse -- "nothing to flag" is a real
    editorial answer and must not be confused with a broken response.
    """
    from src.rank import _extract_json_object

    payload = _extract_json_object(raw)
    if payload is None:
        return None
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return None
    entries = [entry for entry in findings if isinstance(entry, dict)]
    summary = str(payload.get("summary") or "").strip()
    return entries, summary


def _resolve_and_filter_findings(
    raw_findings: list[dict[str, Any]],
    issue_payload: dict[str, Any],
) -> tuple[list[ReviewFinding], list[ReviewFinding], int]:
    """Turn raw finding dicts into models, and drop the ones without
    evidence.

    Returns ``(kept, dropped, malformed_count)``:

      * ``kept`` -- findings whose target resolved to real text AND whose
        quote appears in that text.
      * ``dropped`` -- well-formed findings whose quote is NOT in the target
        text, whose target names a story/section the issue does not have,
        or (v1.1) carrying ``take_shape`` against an issue with no takes.
        Recorded for the audit trail; excluded from the verdict.
      * ``malformed_count`` -- entries that could not be built into a
        ``ReviewFinding`` at all (missing fields, bad vocabulary). Counted
        rather than kept: there is no shape to record.

    Finding ids are assigned HERE, in emission order, across both lists --
    ``RevisionChange.finding_ids`` references them, and a model-authored id
    could collide.
    """
    field_texts = index_issue_fields(issue_payload)
    kept: list[ReviewFinding] = []
    dropped: list[ReviewFinding] = []
    malformed = 0

    # v1.1: deterministic legacy guard for the take criteria. The prompt
    # says "SKIP take_shape entirely when no story carries a take", but a
    # prompt rule is a request; this is the enforcement (No Token Wasted:
    # a criterion that cannot apply is dropped by code, not by hoping the
    # model read the footnote). Measured need: the first wired Eval 9 run
    # (2026-08-08) caught the reviewer filing a take_shape major against a
    # pre-take fixture issue.
    issue_has_takes = any(
        key.endswith(":take") for key in field_texts
    )

    for index, entry in enumerate(raw_findings, start=1):
        finding_id = f"f{index:03d}"
        try:
            finding = _build_finding(finding_id, entry)
        except (ValueError, TypeError) as exc:
            malformed += 1
            _LOG.warning(
                "review: dropping malformed finding %d: %s", index, exc,
            )
            continue

        if finding.criterion not in REVIEW_CRITERIA:
            # Kept deliberately -- see REVIEW_CRITERIA. Logged so a criterion
            # the model keeps inventing shows up as a prompt-tuning signal.
            _LOG.info(
                "review: finding %s carries unpublished criterion %r",
                finding_id, finding.criterion,
            )

        if finding.criterion == "take_shape" and not issue_has_takes:
            dropped.append(finding)
            _LOG.warning(
                "review: dropping finding %s -- take_shape filed against "
                "an issue with no takes (pre-take/legacy); the criterion "
                "does not apply", finding_id,
            )
            continue

        key = target_key(finding.target)
        text = field_texts.get(key)
        if text is None:
            dropped.append(finding)
            _LOG.warning(
                "review: dropping finding %s -- target %s is not in the issue",
                finding_id, key,
            )
            continue
        if not quote_present(finding.quote, text):
            dropped.append(finding)
            _LOG.warning(
                "review: dropping finding %s (%s/%s) -- quote not found "
                "verbatim in %s: %r",
                finding_id, finding.criterion, finding.severity, key,
                finding.quote[:80],
            )
            continue
        kept.append(finding)

    return kept, dropped, malformed


def _build_finding(finding_id: str, entry: dict[str, Any]) -> ReviewFinding:
    """Construct a ``ReviewFinding`` from one raw JSON entry.

    Raises ``ValueError``/``TypeError`` (via pydantic) when the entry cannot
    be made valid -- the caller counts those as malformed. We normalise
    case and whitespace on the enumerated fields before handing them to
    pydantic so a model that writes ``"Major"`` is not thrown away over
    capitalisation; everything else is passed through as written."""
    target_raw = entry.get("target")
    if not isinstance(target_raw, dict):
        raise ValueError("finding has no target object")

    kind = str(target_raw.get("kind") or "").strip().lower()
    section = str(target_raw.get("section") or "").strip().lower() or None
    story_id = str(target_raw.get("story_id") or "").strip() or None
    field = str(target_raw.get("field") or "").strip().lower()

    # v1.2: the digest locator. Tolerant parse -- an int, or a string of
    # digits; anything else stays None and the model validator rejects the
    # digest target properly (a digest finding without an index cannot be
    # resolved to a bullet).
    digest_index_raw = target_raw.get("digest_index")
    digest_index: int | None = None
    if isinstance(digest_index_raw, bool):
        digest_index = None
    elif isinstance(digest_index_raw, int):
        digest_index = digest_index_raw
    elif isinstance(digest_index_raw, str) and digest_index_raw.strip().isdigit():
        digest_index = int(digest_index_raw.strip())

    # A section target must not carry a story_id, and the model sometimes
    # echoes one back. Clearing it here (rather than failing) keeps a
    # legitimate finding alive; the model validator still rejects the
    # genuinely incoherent combinations. Same treatment for a digest
    # target echoing a story_id or section (digest provenance lives on
    # DigestBullet.story_ids; the digest is issue-level).
    if kind == "section":
        story_id = None
    elif kind == "digest":
        story_id = None
        section = None
    elif section is not None and section not in _SECTION_DISPLAY:
        # On a STORY target the section is informative only (it groups the
        # rendered review); the story_id is what resolves the text. An
        # unrecognised token is dropped rather than allowed to fail the
        # whole finding over a label. On a SECTION target it is load-bearing
        # and is left alone, so the model validator rejects it properly.
        _LOG.info(
            "review: story finding names unknown section %r -- clearing",
            section,
        )
        section = None

    target = ReviewTarget(
        kind=kind,  # type: ignore[arg-type]
        story_id=story_id,
        section=section,  # type: ignore[arg-type]
        digest_index=digest_index if kind == "digest" else None,
        field=field,  # type: ignore[arg-type]
    )
    return ReviewFinding(
        finding_id=finding_id,
        target=target,
        criterion=str(entry.get("criterion") or "").strip().lower()[:64],
        severity=str(entry.get("severity") or "").strip().lower(),  # type: ignore[arg-type]
        quote=str(entry.get("quote") or "").strip()[:1000],
        fix_kind=str(entry.get("fix_kind") or "").strip().lower(),  # type: ignore[arg-type]
        instruction=str(entry.get("instruction") or "").strip()[:1000],
    )


def target_key(target: ReviewTarget) -> str:
    """Stable dict key for a target: ``story:<id>:<field>``,
    ``section:<name>:<field>``, or ``digest:<index>:<field>``. Used to
    join findings to live text and to group them for the revision
    engine."""
    if target.kind == "story":
        return f"story:{target.story_id}:{target.field}"
    if target.kind == "digest":
        return f"digest:{target.digest_index}:{target.field}"
    return f"section:{target.section}:{target.field}"


def index_issue_fields(issue_payload: dict[str, Any]) -> dict[str, str]:
    """Build ``{target_key: current text}`` for every quotable field in the
    issue -- every story's headline, summary, and take (when present),
    every section's synthesis or legacy intro_lead/intro_body, and every
    digest bullet's lead and sentence (keyed by 0-based index).

    Walks the raw JSON payload rather than the pydantic ``Issue`` so
    schema-version skew between a staged file and this code can never crash
    the review. Public shape shared with ``src/revise.py``, which resolves
    the same keys when it applies an edit.
    """
    out: dict[str, str] = {}

    def _add_story(story: dict[str, Any]) -> None:
        story_id = story.get("story_id")
        if not isinstance(story_id, str) or not story_id:
            return
        # "take" joined the quotable story fields at v1.1 (schema v4);
        # absent/None on legacy issues, in which case no key is emitted
        # and any take-targeting finding is dropped as unresolvable.
        for field in ("headline", "summary", "take"):
            value = story.get(field)
            if isinstance(value, str) and value:
                out[f"story:{story_id}:{field}"] = value

    def _add_section_fields(name: str, section: dict[str, Any]) -> None:
        # "synthesis" joined at v1.2 (IssueSection v4); mutually exclusive
        # with the legacy pair by contract, but the indexer stays tolerant
        # and indexes whatever is present.
        for field in ("synthesis", "intro_lead", "intro_body"):
            value = section.get(field)
            if isinstance(value, str) and value:
                out[f"section:{name}:{field}"] = value

    pulse = issue_payload.get("pulse") or {}
    if isinstance(pulse, dict):
        for story in pulse.get("stories") or []:
            if isinstance(story, dict):
                _add_story(story)
        _add_section_fields("pulse", pulse)

    for section in issue_payload.get("sections") or []:
        if not isinstance(section, dict):
            continue
        name = section.get("name")
        if isinstance(name, str) and name:
            _add_section_fields(name, section)
        for story in section.get("stories") or []:
            if isinstance(story, dict):
                _add_story(story)

    # Digest bullets (v1.2, R1): keyed by 0-based index -- bullets have no
    # id, the digest is small and ordered, and the issue_sha256 freshness
    # contract protects the index from drifting under an edit. No digest
    # (or a malformed one) simply emits no keys, so any digest-targeting
    # finding on such an issue is dropped as unresolvable.
    digest = issue_payload.get("digest")
    if isinstance(digest, list):
        for i, bullet in enumerate(digest):
            if not isinstance(bullet, dict):
                continue
            for field, model_field in (
                ("digest_lead", "lead"), ("digest_sentence", "sentence"),
            ):
                value = bullet.get(model_field)
                if isinstance(value, str) and value:
                    out[f"digest:{i}:{field}"] = value
    return out


# ---------------------------------------------------------------------------
# review.md rendering -- CODE writes this, from the report.
# ---------------------------------------------------------------------------

_SECTION_DISPLAY = {
    "pulse": "The Pulse",
    "big_picture": "The Big Picture",
    "hands_on": "Hands-On",
    "currents": "Currents",
}

_GROUP_DISPLAY = {
    **_SECTION_DISPLAY,
    "digest": "The 30-second read",
}
"""Display names for finding GROUPS in review.md. A superset of
``_SECTION_DISPLAY`` because the digest is a rendering group but NOT a
section -- ``_SECTION_DISPLAY`` doubles as the story-target section-token
whitelist in ``_build_finding``, and "digest" must not become a valid
story section there."""

_SEVERITY_MARK = {
    "blocking": "BLOCKING",
    "major": "MAJOR",
    "minor": "minor",
    "note": "note",
}


def render_review_markdown(
    report: ReviewReport,
    date: _dt.date,
    issue_payload: dict[str, Any] | None = None,
) -> str:
    """Render the human-readable review body from a ``ReviewReport``.

    The frontmatter is added by ``_write_review_artifact``; this function
    produces the body only. Findings are grouped by section (in reading
    order) so the document reads the way the issue does, and the
    Recommendations list at the end collects every actionable finding in
    severity order -- that list is what Arman reads first at 06:00.

    No LLM involvement. The reviewer produced findings; turning findings
    into Markdown is formatting, and formatting is code's job.
    """
    headlines = _headline_index(issue_payload or {})
    counts = report.severity_counts()
    lines: list[str] = []
    lines.append(f"# Editor's Review -- {date.isoformat()}")
    lines.append("")
    tally = ", ".join(
        f"{counts[severity]} {severity}" for severity in _SEVERITIES
        if counts[severity]
    )
    lines.append(
        f"**Verdict**: {report.computed_verdict.upper()} "
        f"({tally or 'no findings'}). {report.one_line}"
    )
    lines.append("")
    lines.append(
        "The verdict is computed by code from the finding severities below, "
        f"under threshold table `{report.thresholds_version}`. "
        f"{report.note}"
    )
    lines.append("")

    if not report.findings:
        lines.append("## Findings")
        lines.append("")
        lines.append("No findings this issue.")
    else:
        by_section = _group_findings_by_section(report.findings)
        # "digest" leads: the skim block renders above the Pulse, so its
        # findings read first, the way the issue does.
        for section_name in (
            "digest", "pulse", "big_picture", "hands_on", "currents", "",
        ):
            group = by_section.get(section_name)
            if not group:
                continue
            display = _GROUP_DISPLAY.get(section_name, "Unplaced")
            lines.append(f"## {display}")
            lines.append("")
            for finding in group:
                lines.extend(_render_finding(finding, headlines))
            lines.append("")

    lines.append("## Recommendations before release")
    lines.append("")
    actionable = [
        finding for finding in report.findings if finding.severity != "note"
    ]
    if not actionable:
        lines.append("- Ratify as-is.")
    else:
        order = {severity: i for i, severity in enumerate(_SEVERITIES)}
        for finding in sorted(actionable, key=lambda f: order[f.severity]):
            lines.append(
                f"- [{_SEVERITY_MARK[finding.severity]}] "
                f"({finding.finding_id}, {finding.fix_kind}) "
                f"{finding.instruction}"
            )
    lines.append("")

    if report.dropped_findings:
        lines.append("## Dropped findings (quote not found in the issue)")
        lines.append("")
        lines.append(
            "These were filtered out by the verbatim-quote check: the "
            "reviewer objected to text that is not in the issue. Recorded "
            "for calibration, excluded from the verdict."
        )
        lines.append("")
        for finding in report.dropped_findings:
            lines.append(
                f"- ({finding.finding_id}, {finding.criterion}) claimed "
                f"quote: \"{finding.quote[:160]}\""
            )
        lines.append("")

    lines.append("## Ratification call")
    lines.append("")
    lines.append(f"**Computed verdict**: {report.computed_verdict.upper()}")
    lines.append("**Arman's call**: ___")
    return "\n".join(lines)


def _render_finding(
    finding: ReviewFinding, headlines: dict[str, str]
) -> list[str]:
    """Render one finding as a Markdown block. The quote comes first: it is
    the evidence, and a reader should be able to check the claim before
    reading the claim."""
    target = finding.target
    if target.kind == "story":
        who = headlines.get(target.story_id or "", target.story_id or "?")
        locator = f'"{who}" -> {target.field}'
    elif target.kind == "digest":
        locator = (
            f"The 30-second read, bullet {(target.digest_index or 0) + 1} "
            f"-> {target.field}"
        )
    else:
        locator = f"{_SECTION_DISPLAY.get(target.section or '', target.section)} intro -> {target.field}"
    return [
        f"**[{_SEVERITY_MARK[finding.severity]}] {finding.finding_id} "
        f"-- {finding.criterion}** ({finding.fix_kind})",
        f"- Target: {locator}",
        f"- Quote: \"{finding.quote}\"",
        f"- Fix: {finding.instruction}",
        "",
    ]


def _group_findings_by_section(
    findings: list[ReviewFinding],
) -> dict[str, list[ReviewFinding]]:
    """Group findings by their target's section, preserving emission order
    within each group. Digest targets (section-less by contract) group
    under ``"digest"``; story targets that never declared a section fall
    into the ``""`` bucket, rendered last under "Unplaced"."""
    out: dict[str, list[ReviewFinding]] = {}
    for finding in findings:
        if finding.target.kind == "digest":
            key = "digest"
        else:
            key = finding.target.section or ""
        out.setdefault(key, []).append(finding)
    return out


def _headline_index(issue_payload: dict[str, Any]) -> dict[str, str]:
    """``{story_id: headline}`` so the rendered review can name a story
    instead of printing a cluster id at a human."""
    out: dict[str, str] = {}
    pulse = issue_payload.get("pulse") or {}
    sections = [pulse] if isinstance(pulse, dict) else []
    sections += [s for s in (issue_payload.get("sections") or []) if isinstance(s, dict)]
    for section in sections:
        for story in section.get("stories") or []:
            if not isinstance(story, dict):
                continue
            story_id = story.get("story_id")
            headline = story.get("headline")
            if isinstance(story_id, str) and isinstance(headline, str):
                out[story_id] = headline
    return out


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

def _resolve_review_model() -> str:
    """Which model reviews. ``REVIEW_MODEL`` when set, else ``LLM_MODEL``.

    The split exists to mitigate same-model leniency: a model grading prose
    a sibling model wrote is measurably softer on it than an independent
    one. Recommended production setting is a stronger model than the writer
    (``claude-opus-4-7``), mirroring ``EVAL_JUDGE_MODEL``. See
    ``.env.example``.
    """
    explicit = (os.getenv("REVIEW_MODEL") or "").strip()
    if explicit:
        return explicit
    return (os.getenv("LLM_MODEL") or "").strip() or "unknown"


def _call_review_llm(prompt: "str | tuple[str, str]", timeout: float) -> str:
    """Issue one LLM call and return the raw response text.

    Reuses ``rank._llm_call`` so provider routing (anthropic / bedrock /
    openai-compatible) and transport handling are inherited unchanged.
    ``prompt`` is passed through intact: since v1.2.1 the review sends the
    cache-split ``(static_prefix, variable_part)`` tuple, which the
    Anthropic branch turns into two content blocks with ``cache_control``
    on the prefix; every other provider receives the joined string,
    byte-identical to the single-string prompt. Temperature is 0.0 (see
    ``_REVIEW_TEMPERATURE``).

    ``rank._llm_call`` reads ``LLM_MODEL`` and ``LLM_TIMEOUT_SECONDS`` from
    the environment itself, so the cleanest seam for the review-specific
    model and timeout is to set and restore both around the call rather than
    duplicate provider routing here. Restoring in a ``finally`` matters:
    leaking ``REVIEW_MODEL`` into the process environment would silently
    re-point every later stage in the same run.
    """
    from src import rank as _rank

    prior_timeout = os.environ.get("LLM_TIMEOUT_SECONDS")
    prior_model = os.environ.get("LLM_MODEL")
    os.environ["LLM_TIMEOUT_SECONDS"] = str(timeout)
    model = _resolve_review_model()
    if model and model != "unknown":
        os.environ["LLM_MODEL"] = model
    try:
        return _rank._llm_call(
            prompt,
            temperature=_REVIEW_TEMPERATURE,
            max_tokens=_REVIEW_MAX_TOKENS,
        )
    finally:
        for key, prior in (
            ("LLM_TIMEOUT_SECONDS", prior_timeout),
            ("LLM_MODEL", prior_model),
        ):
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior


def _resolve_timeout() -> float:
    """Decide the per-call timeout for the review LLM request.

    Reads ``LLM_REVIEW_TIMEOUT_SECONDS`` first so operators can tune the
    review-specific budget without disturbing rank/summarise. Falls back
    to ``_REVIEW_TIMEOUT_DEFAULT`` (180s) -- which is intentionally LARGER
    than the rank/summarise default (60s) because the review prompt asks
    for a long structured response.

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
# Frontmatter parsing + artifact writes.
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
    "findings_total",
    "findings_by_severity",
    "findings_dropped",
    "thresholds_version",
})
"""Frontmatter keys the WRITE path owns outright.

Any line carrying one of these keys is stripped from the body before we
append our own, so each key appears exactly once and is written by code.
``verdict`` is the load-bearing member: it is why the file and the returned
``ReviewArtifact`` cannot disagree. At v1.0 the body is itself code-rendered
and carries no frontmatter, so this is now a defence against a future writer
rather than against the model."""


def _strip_code_fence(text: str) -> str:
    """Remove a whole-document code fence a writer may have wrapped its
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
    """Strictly extract ``verdict`` + ``one_line`` from a review document.

    Returns ``(verdict, one_line)`` where verdict is one of
    ``REVIEW_EDITORIAL_VERDICTS`` when -- and only when -- the frontmatter
    is well-formed and carries exactly one recognised verdict token. Every
    other outcome returns ``VERDICT_UNPARSEABLE`` with a diagnostic
    one_line naming the specific defect.

    This is the READER half of the ``review.md`` contract. Since v1.0 the
    write path no longer parses model output with it (the model returns
    JSON), but the strictness still earns its keep two ways: it is how the
    round-trip test proves a written file reads back as the verdict we
    intended, and it is the reference implementation for any consumer
    parsing the frontmatter.

    There is deliberately no default verdict. An earlier version fell back
    to ``amber``, which turned three different failures (no frontmatter,
    unclosed frontmatter, an unrecognised token) into a value that reads
    like a real editorial judgement.

    Duplicate ``verdict:`` keys are unparseable rather than first-wins. Two
    verdict lines mean the document does not state one verdict, and picking
    either is guessing -- last-wins in particular let a ``red``-then-
    ``green`` pair report green.
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
    if verdict not in REVIEW_EDITORIAL_VERDICTS:
        return (
            VERDICT_UNPARSEABLE,
            f"<verdict token not recognised: {raw_verdict!r}>",
        )

    one_line = mapping.get("one_line") or "<one_line missing>"
    return (verdict, one_line)


def _write_report_json(path: Path, report: ReviewReport) -> Path:
    """Atomic write of ``review.json`` (.tmp + fsync + rename). Mirrors
    ``verify._write_report`` so both sidecars behave identically under a
    same-day re-run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.loads(report.model_dump_json())
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def read_report(date: _dt.date, *, canonical: bool = False) -> ReviewReport | None:
    """Read ``review.json`` for a date, or ``None`` when it is missing or
    unreadable.

    The reader half of the review -> revise hand-off. Returns ``None``
    rather than raising: ``src/revise.py`` treats an absent or unparseable
    report as "there is nothing ratified to act on", which is a refusal, not
    a crash.
    """
    path = review_json_path(date, canonical=canonical)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _LOG.warning("review: could not read %s: %s", path, exc)
        return None
    try:
        return ReviewReport.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 -- a bad report is not a crash
        _LOG.warning("review: %s does not validate as a ReviewReport: %s",
                     path, exc)
        return None


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
    extra_frontmatter: dict[str, Any] | None = None,
) -> Path:
    """Write ``data/staging/<date>/review.md``.

    The body is preserved verbatim. The frontmatter is written so that the
    machine-readable keys -- ``verdict`` above all -- are authored by this
    function from the values the caller is also putting into the returned
    ``ReviewArtifact``. That is what makes the file and the artifact
    incapable of disagreeing.
    """
    review_path = review_md_path(date)
    review_path.parent.mkdir(parents=True, exist_ok=True)

    enriched = _enrich_frontmatter(
        markdown, llm_metadata,
        verdict=verdict,
        one_line=one_line,
        issue_date=issue_date,
        issue_shape=issue_shape,
        issue_sha256=issue_sha256,
        extra_frontmatter=extra_frontmatter,
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
    extra_frontmatter: dict[str, Any] | None = None,
) -> str:
    """Attach the code-authored frontmatter block to a review body.

    Every key in ``_CODE_AUTHORED_KEYS`` is stripped from whatever the body
    carried and re-emitted from the arguments here, in a fixed order. Any
    other key found is kept, in its original order, above ours. A stray
    ``verdict:`` line in the body is preserved as ``llm_reported_verdict``
    for the audit trail -- at v1.0 no writer produces one, but recording a
    disagreement is strictly better than erasing it.

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
    for key, value in (extra_frontmatter or {}).items():
        if key.strip().lower() in {k.lower() for k in kvs}:
            continue
        kvs[key] = value
    for key, value in extra.items():
        # Never let caller metadata shadow a key we have already written.
        # The comparison is case-insensitive because a stray ``Verdict:``
        # would parse back as a duplicate of our own ``verdict:`` line.
        if key.strip().lower() in {k.lower() for k in kvs}:
            continue
        kvs[key] = value

    code_block = _render_frontmatter(kvs)

    if frontmatter is None:
        # No frontmatter in the body -- synthesise the whole block and leave
        # the body untouched, so nothing is lost.
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


def _write_machine_state(
    date: _dt.date,
    path: Path,
    verdict: str,
    reason: str,
    *,
    issue_sha256: str | None = None,
    thresholds_version: str = "",
    llm_model: str = "",
    body_extra: str = "",
) -> ReviewArtifact:
    """Write the ``review.json`` + ``review.md`` pair for a code-authored
    machine state (``unavailable`` / ``unparseable``) and return the
    artifact.

    The publication still ships; Arman just doesn't get a usable review for
    the day. Both files are shaped so downstream parsers see the machine
    state and act accordingly (never print a misleading green).

    The Markdown write is guarded, and a failure is logged AND re-raised.
    That is the deliberate difference from the ordinary failure-soft path:
    if we cannot even record that the review is unavailable, the only trace
    left on disk would be an absent (or stale, from a previous run)
    ``review.md``, and either reads as a state nobody chose. An exception
    reaching the caller is visible; a missing file is not. ``run.py`` treats
    review as advisory and continues regardless.

    The ``review.json`` write is best-effort by contrast: it is the newer,
    secondary artifact, and failing the stage over it would trade a working
    fallback for a crash.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    one_line = f"{verdict}: {reason}"[:200].replace(":", " -", 1)
    model = llm_model or _resolve_review_model()

    report = ReviewReport(
        generated_at=_dt.datetime.now(_dt.timezone.utc),
        computed_verdict=verdict,  # type: ignore[arg-type]
        one_line=one_line,
        findings=[],
        dropped_findings=[],
        prompt_version=REVIEW_PROMPT_VERSION,
        thresholds_version=thresholds_version,
        llm_model=model,
        issue_sha256=issue_sha256 or "unknown",
        note=f"{verdict}: {reason}"[:2000],
    )
    try:
        _write_report_json(review_json_path(date), report)
    except Exception:  # noqa: BLE001 -- the Markdown artifact is the contract
        _LOG.exception(
            "review: could not write the %s review.json for %s",
            verdict, date.isoformat(),
        )

    kvs: dict[str, Any] = {
        "verdict": verdict,
        "one_line": one_line,
        "issue_date": date.isoformat(),
        "issue_shape": "unknown",
        "issue_sha256": issue_sha256 or "unknown",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "prompt_version": REVIEW_PROMPT_VERSION,
        "findings_total": 0,
        "findings_by_severity": " ".join(f"{s}=0" for s in _SEVERITIES),
        "findings_dropped": 0,
        "thresholds_version": thresholds_version,
        "llm_model": model,
    }
    content = (
        "---\n"
        f"{_render_frontmatter(kvs)}\n"
        "---\n\n"
        f"# Editor's Review -- {date.isoformat()}\n\n"
        f"**Verdict**: {verdict.upper()}.\n\n"
        f"The review did not produce a usable editorial read: {reason}.\n\n"
        f"{body_extra}"
        "No findings were recorded, which is NOT the same as no concerns. "
        "The publication can still ship; Arman has no structured editorial "
        "read for today. Re-run `aiv review --date "
        f"{date.isoformat()}` once the underlying issue is resolved.\n"
    )
    try:
        _atomic_write_text(path, content)
    except Exception:
        _LOG.exception(
            "review: could not write the %s review.md at %s "
            "(original reason: %s)", verdict, path, reason,
        )
        raise
    return ReviewArtifact(
        date=date, verdict=verdict, one_line=one_line, path=path,
        report=report,
    )


def _write_unavailable(
    date: _dt.date, path: Path, reason: str, *,
    issue_sha256: str | None = None,
    thresholds_version: str = "",
    llm_model: str = "",
) -> ReviewArtifact:
    """Record that the review COULD NOT RUN (missing issue, unreadable
    threshold table, LLM transport failure)."""
    return _write_machine_state(
        date, path, VERDICT_UNAVAILABLE, reason,
        issue_sha256=issue_sha256,
        thresholds_version=thresholds_version,
        llm_model=llm_model,
    )


def _write_unparseable(
    date: _dt.date, path: Path, raw: str, *,
    issue_sha256: str | None = None,
    thresholds_version: str = "",
    llm_model: str = "",
) -> ReviewArtifact:
    """Record that the review RAN but its output could not be read.

    The raw response head is kept in the body: when a reviewer starts
    returning prose instead of JSON, the first 800 characters are what tell
    you whether the prompt drifted or the model did.
    """
    excerpt = (raw or "").strip()[:800]
    body_extra = (
        "The model responded, but the response could not be read as findings "
        "JSON after a corrective retry. First 800 characters of the last "
        f"response:\n\n```\n{excerpt}\n```\n\n"
    )
    return _write_machine_state(
        date, path, VERDICT_UNPARSEABLE,
        "the reviewer's response was not valid findings JSON",
        issue_sha256=issue_sha256,
        thresholds_version=thresholds_version,
        llm_model=llm_model,
        body_extra=body_extra,
    )


# ---------------------------------------------------------------------------
# HAND-OFF NOTES
# ---------------------------------------------------------------------------
# Architect:
#   * ``review.json`` is a new staging artifact. It should join
#     ``_OPTIONAL_PERIPHERAL_FILES`` in ``render.release_promote`` next to
#     ``review.md`` -- the released record should carry the findings the
#     verdict was computed from, not just the verdict.
#   * ``review_md_path`` / ``review_json_path`` live here for the same
#     reason ``gate.review_md_path`` does (``src/paths.py`` is yours). Both
#     belong in ``paths.py`` when you next touch it.
#   * DESIGN.md needs: the ``review.json`` artifact section, the
#     ``ReviewFinding`` / ``ReviewReport`` / ``RevisionChange`` /
#     ``RevisionCycle`` contract rows, and a schema-changelog entry.
#
# Eval Engineer:
#   * The verdict is now a pure function of ``findings`` + the threshold
#     table: ``review.compute_verdict(counts, load_thresholds())``. Eval 9
#     can score finding precision (does the quote exist? is the severity
#     right?) independently of verdict accuracy.
#   * ``ReviewReport.dropped_findings`` is the reviewer's measured misfire
#     rate. It was previously invisible.
# ---------------------------------------------------------------------------
