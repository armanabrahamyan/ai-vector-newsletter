r"""
src/revise.py -- AI Vector revision engine (the "revise" stage).

Takes the ``text_edit`` findings from ``review.json`` and rewrites exactly
the fields they point at. Nothing else. It is the only part of the pipeline
that edits an issue after it has been written, so its whole design is about
containment: the smallest possible unit of work, the smallest possible
prompt, and a code-side validation gate the model cannot argue with.

The three containment rules
---------------------------
1. **Only ``fix_kind == "text_edit"``.** A finding that needs a different
   story, a different source, or Arman's judgement is not something a text
   rewrite can fix, and attempting one would produce prose that reads
   corrected while the problem stands.

2. **One LLM call per target FIELD.** Findings that share a field are
   answered together; the model receives ONLY that field's current text and
   the instructions about it -- not the story, not the section, not the
   issue. A model that cannot see the rest of the issue cannot drift the
   rest of the issue.

3. **Code validates, then code writes.** The model returns replacement text
   and nothing else. Before anything is accepted, plain code checks the
   length constraint, that deletion-class quotes are actually gone, that the
   edit stayed small, that every source URL survived, and that no numeral
   was dropped or invented. A replacement that fails any check is refused
   and the field is left exactly as it was. No Token Wasted cuts both ways:
   the model does the writing, code does the checking.

Shadow vs live
--------------
``revise_day(date, shadow=True)`` computes every replacement, runs the full
validation gate, and writes ``revisions.jsonl`` with statuses ``proposed``
(would apply) and ``rejected`` (would refuse) -- ``issue.json`` is not
touched. ``shadow=False`` applies the accepted changes atomically, re-renders
the HTML for the copy it edited, and records ``applied`` / ``rejected``.

Applying an edit invalidates three things downstream, all handled here or
stated plainly:
  * the touched story's ``verification`` is set to ``None`` by CODE -- the
    fact-check was about text that no longer exists, and a stale clean
    verdict is worse than none;
  * the rendered HTML now describes text that is not in ``issue.json``, so a
    live cycle that changed the file re-renders it (``_rerender_html``) --
    the JSON and the page a reader opens are never allowed to disagree;
  * the review's ``issue_sha256`` no longer matches the file, so
    ``src/gate.py`` will hold with ``hold:stale-review`` until the issue is
    re-reviewed. That is correct, not a bug: an edited issue has not been
    reviewed.

Which findings reach the model
------------------------------
Two deterministic filters run BEFORE any LLM call, in this order:

1. **Freshness** (``_assess_freshness``). The review must still describe the
   text on disk. On the staging draft that is a byte-for-byte hash match. On
   the released copy it cannot be: ``render.release_promote`` stamps
   ``issue_number`` into the canonical ``issue.json``, so its bytes differ
   from the staging bytes the reviewer hashed while every sentence is
   identical. There, freshness is established per finding from the evidence
   the finding carries -- its verbatim ``quote`` must still appear in the
   field it points at.

2. **Severity** (``min_severity``). "Apply only the major recommendations"
   is a selection rule, not a writing instruction, so it is code: findings
   below the floor are dropped and never reach a prompt. Passing that
   sentence as ``--instruction`` instead would have paid tokens to ask a
   field-scoped model -- which cannot see the other findings -- to filter a
   list it was never shown.

Freshness answers first because it is a property of the review against the
file, which the operator's floor cannot change. Running the floor first
makes a day whose findings are all minor look like a stale review when
``--min-severity major`` is passed, and a wrong diagnosis in the refusal is
worse than no refusal message at all.

Voice
-----
The revise prompt writes reader-facing prose, so it is voice-bearing: the
voice-adherence eval (Eval 3) applies to it before any commit, alongside
Eval 9 for the reviewer that feeds it.

Owner: LLM Engineer (docs/internal/TEAM.md).

Audit tag: revise-v0.1-2026-08-02.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import os
import re
from dataclasses import dataclass, field

import typer
from pathlib import Path
from typing import Any, Optional

from src import llm_usage, paths
from src.models import (
    DigestBullet,
    IssueSection,
    ReviewFinding,
    ReviewReport,
    ReviewTarget,
    RevisionChange,
    RevisionCycle,
    SummaryBlock,
)
from src.review import (
    quote_present,
    read_report,
    review_json_path,
    index_issue_fields,
    target_key,
)


# ---------------------------------------------------------------------------
# Module constants.
# ---------------------------------------------------------------------------

REVISE_PROMPT_VERSION = "v0.2"
r"""Versioned prompt string recorded on every ``RevisionCycle`` so a bad
edit can be attributed to the prompt that produced it.

Bump on any prompt-content change. Audit tag: ``revise-v0.2-2026-08-09``.

v0.2 (2026-08-09, wave three): ``_FIELD_GUIDANCE`` gains the redesign
surfaces -- ``synthesis``, ``digest_lead``, ``digest_sentence`` -- so a
digest/synthesis finding's rewrite prompt states the budgets and the
story-anchored vs section-anchored register before the code-side spec
re-validation enforces them. STRICTLY ADDITIVE: the prompt for every
pre-existing field (headline / summary / take / intro pair) is
byte-identical to v0.1 -- the new guidance strings are reachable only
from targets that could not exist before ReviewTarget v3.

v0.1 (2026-08-02): first cut. Field-scoped rewrite prompt -- current text
plus the findings about it, replacement text out, nothing else. The
constraints the code enforces afterwards are stated IN the prompt as well:
telling the model the rule is cheaper than a retry, and a retry is cheaper
than a rejection."""

_REVISE_TEMPERATURE_DEFAULT = 0.2
"""Low. A revision is a targeted repair of prose that already exists, not a
fresh draft -- summarise runs at 0.6 because it is inventing voice, and this
stage is deliberately more conservative than the writer it corrects.
Override with ``LLM_TEMPERATURE_REVISE``."""

_REVISE_MAX_TOKENS = 1200
"""The largest field is a 1200-character summary. A generous token ceiling
still cannot produce a valid over-long field, because the length check
rejects one."""

_REVISE_TIMEOUT_DEFAULT = 90.0
"""Seconds per field-level call. Override with
``LLM_REVISE_TIMEOUT_SECONDS``."""

_LENGTH_RETRY_BUDGET = 1
"""One retry, and only for a LENGTH failure -- the single failure mode a
model can reliably fix when told the number it missed. Every other
rejection (numeral invented, URL dropped, edit too large) indicates the
model did something different from what was asked, and asking again is
paying twice for the same misunderstanding."""

_MAX_EDIT_RATIO = 0.40
"""Ceiling on token-level edit distance as a fraction of the ORIGINAL
field's token count. Above this, the "revision" is a rewrite: the story is
no longer the one that was ranked, summarised, and fact-checked."""

_MIN_EDIT_ALLOWANCE_TOKENS = 8
"""Absolute floor on the edit allowance, in tokens.

A pure ratio makes the shortest fields unrevisable. An ``intro_lead`` like
"Trust, but verify." is three tokens, so a 40% budget permits roughly one
token of change -- and replacing that exact phrase is the single
most-cited anti-pattern in EDITORIAL.md. Eight tokens is enough to replace
a short lead outright while still refusing a wholesale rewrite of a
twelve-token headline, and it is negligible against a sixty-token body,
where the ratio governs.

Calibration knob. It and ``_MAX_EDIT_RATIO`` are the two numbers to revisit
if the shadow run shows rewrites slipping through, or legitimate repairs
being refused."""

SEVERITY_RANK: dict[str, int] = {
    "note": 0, "minor": 1, "major": 2, "blocking": 3,
}
"""Order of ``ReviewSeverity``, lowest first. The only place this stage
ranks severities, so ``--min-severity major`` and any future threshold read
the same scale."""

MIN_SEVERITY_CHOICES: tuple[str, ...] = tuple(
    sorted(SEVERITY_RANK, key=lambda name: SEVERITY_RANK[name])
)
"""``("note", "minor", "major", "blocking")`` -- the accepted
``--min-severity`` values, in ascending order, for help text and validation."""

MIN_SEVERITY_DEFAULT = "note"
"""The floor that filters nothing. Default because the reviewer's severities
are advisory evidence, not a queue: an operator who wants only the serious
ones says so, and the daily loop keeps acting on everything."""

_LOG = logging.getLogger("ai_vector.revise")

_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+")
"""URLs in prose. Any URL present before the edit must survive it -- a
dropped link is a dropped attribution."""

_NUMERAL_RE = re.compile(r"\d+(?:[.,]\d+)*")
"""Numeral tokens (integers, decimals, thousands-separated). The check that
uses these is the one that catches the most dangerous class of edit: a
number that changed while the sentence around it stayed plausible."""

_DELETION_VERB_RE = re.compile(
    r"\b(delete|remove|cut|drop|strike|excise)\b", re.IGNORECASE
)
"""Instruction verbs that mean "this text should not be there afterwards".
When one appears, code checks the quote is genuinely gone -- a model that
rewords a flagged phrase instead of removing it has not done what was
asked, and only the quote's absence proves it did."""


# ---------------------------------------------------------------------------
# Field length constraints -- derived from the pydantic models so they can
# never drift from the contract the write will be validated against.
# ---------------------------------------------------------------------------

def _bounds_from_model(model: Any, field_name: str) -> tuple[int, int] | None:
    """Read ``(min_length, max_length)`` for one field off a pydantic model.

    Introspection rather than a hardcoded table: if the Architect widens
    ``SummaryBlock.summary``, this stage must widen with it in the same
    instant, and a copied constant would not.

    Reads the JSON schema rather than the field's raw metadata because the
    intro fields are Optional -- ``Annotated[str, Field(max_length=80)] |
    None`` -- and their length constraint lives inside the union branch,
    where field metadata does not reach it. The schema resolves the union
    for us, so one code path covers both shapes.

    Returns ``None`` when the field declares no maximum length, so the
    caller can fall back loudly.
    """
    try:
        schema = model.model_json_schema()
        node = schema["properties"][field_name]
    except (AttributeError, KeyError, TypeError):
        return None

    def _walk(entry: Any) -> tuple[int, int] | None:
        if not isinstance(entry, dict):
            return None
        if "maxLength" in entry:
            return int(entry.get("minLength", 0)), int(entry["maxLength"])
        for branch in entry.get("anyOf") or entry.get("oneOf") or []:
            found = _walk(branch)
            if found is not None:
                return found
        return None

    return _walk(node)


def _field_bounds() -> dict[str, tuple[int, int]]:
    """``{field token: (min_length, max_length)}`` for every revisable
    field.

    Keyed by the ``ReviewTargetField`` TOKEN, which is not always the model
    field name: ``digest_lead``/``digest_sentence`` are the prefixed review
    tokens for ``DigestBullet.lead``/``.sentence`` (DESIGN.md "The digest",
    R2 -- the token != model-field-name mapping is deliberate, so a digest
    finding can never be mistaken for a section one).

    Falls back to the values documented in DESIGN.md when introspection
    finds nothing, and logs -- a stage that silently dropped its length
    checks would start writing fields pydantic then refuses, turning a
    clean rejection into a corrupted issue.
    """
    fallback = {
        "headline": (1, 200),
        "summary": (1, 1200),
        "take": (1, 200),
        "intro_lead": (1, 80),
        "intro_body": (1, 400),
        "synthesis": (1, 500),
        "digest_lead": (1, 80),
        "digest_sentence": (1, 300),
    }
    out: dict[str, tuple[int, int]] = {}
    for name, model, model_field in (
        ("headline", SummaryBlock, "headline"),
        ("summary", SummaryBlock, "summary"),
        ("take", SummaryBlock, "take"),
        ("intro_lead", IssueSection, "intro_lead"),
        ("intro_body", IssueSection, "intro_body"),
        ("synthesis", IssueSection, "synthesis"),
        ("digest_lead", DigestBullet, "lead"),
        ("digest_sentence", DigestBullet, "sentence"),
    ):
        bounds = _bounds_from_model(model, model_field)
        if bounds is None:
            _LOG.warning(
                "revise: could not read length bounds for %s from the model; "
                "falling back to %s", name, fallback[name],
            )
            out[name] = fallback[name]
        else:
            # Optional fields declare minimum 0; a replacement that empties
            # one is a deletion, not a revision, so we hold every field to
            # at least 1.
            out[name] = (max(1, bounds[0]), bounds[1])
    return out


FIELD_BOUNDS: dict[str, tuple[int, int]] = _field_bounds()


# ---------------------------------------------------------------------------
# Artifact path.
# ---------------------------------------------------------------------------

def revisions_path(date: _dt.date, *, canonical: bool = False) -> Path:
    """Path to ``revisions.jsonl`` -- one ``RevisionCycle`` per line,
    appended per invocation. Already listed in ``render.release_promote``'s
    optional-peripheral set, so a revised day carries its edit history into
    the released archive."""
    base = paths.released_dir(date) if canonical else paths.staging_dir(date)
    return base / "revisions.jsonl"


# ---------------------------------------------------------------------------
# Public return type.
# ---------------------------------------------------------------------------

@dataclass
class RevisionReport:
    """What one ``revise_day`` invocation did.

    The lightweight caller-facing summary, mirroring ``ReviewArtifact``'s
    role for the review stage: the substantive artifact is the
    ``RevisionCycle`` line in ``revisions.jsonl``, and ``cycle`` carries it
    so a caller does not have to re-read the file it just wrote.

    ``ran`` is False when the engine REFUSED to act (no review, stale
    review, nothing to do). A refusal is not a failure -- but it is also not
    a revision, and the two must not read the same.
    """
    date: _dt.date
    mode: str
    ran: bool
    applied: int
    proposed: int
    rejected: int
    path: Path
    note: str = ""
    cycle: RevisionCycle | None = None

    @property
    def one_line(self) -> str:
        """Terminal-line summary."""
        if not self.ran:
            return f"no revision -- {self.note}"
        if self.mode == "shadow":
            return (
                f"shadow -- {self.proposed} proposed, {self.rejected} rejected"
            )
        return f"live -- {self.applied} applied, {self.rejected} rejected"


@dataclass
class _FieldGroup:
    """One target field and every directive aimed at it -- the unit of work.

    Grouping by field is what bounds the blast radius: one call, one field's
    text in, one field's text out.
    """
    target: ReviewTarget
    text: str
    findings: list[ReviewFinding] = field(default_factory=list)
    operator_instructions: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return target_key(self.target)

    @property
    def instructions(self) -> list[str]:
        """Every directive for this field, findings first (they carry the
        quote that anchors them), operator directives last."""
        return [f.instruction for f in self.findings] + list(
            self.operator_instructions
        )


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def revise_day(
    run_date: _dt.date,
    *,
    shadow: bool,
    instruction: str = "",
    instruction_target: str = "",
    canonical: bool = False,
    min_severity: str = MIN_SEVERITY_DEFAULT,
) -> RevisionReport:
    """Apply the review's ``text_edit`` findings to one day's staged issue.

    Flow
    ----
    1. Read ``review.json`` and the staged ``issue.json``.
    2. REFUSE unless the review still describes the text on disk -- a byte
       hash match on the staging draft, surviving finding quotes on the
       released copy (see ``_assess_freshness``). A review of a superseded
       draft describes text that is no longer there; acting on it would edit
       the wrong sentences.
    3. Drop every finding below ``min_severity``, then group the surviving
       ``text_edit`` findings by target field. Both filters are plain code
       and both run before the first token is spent; freshness goes first so
       a refusal names the cause the operator can act on.
    4. One LLM call per field: current text + directives in, replacement
       text out.
    5. Validate each replacement in code. Pass -> ``proposed`` (shadow) or
       ``applied`` (live). Fail -> ``rejected``, field untouched.
    6. Write the cycle to ``revisions.jsonl``; in live mode also rewrite
       ``issue.json`` atomically (clearing ``verification`` on every story
       whose text changed) and re-render that copy's HTML.

    Failure-soft: this stage never raises into a caller. Every refusal and
    every error becomes a ``RevisionReport`` with ``ran=False`` and a reason
    in ``note``.

    Parameters
    ----------
    run_date
        The issue date to revise (the staging date dir).
    shadow
        True computes and records without touching ``issue.json``.
    instruction
        Optional operator directive (the ``/revise`` PR command). Treated as
        one additional ``text_edit``-class directive under the same
        containment as a finding.
    instruction_target
        Where the operator directive applies, as ``story:<cluster_id>:<field>``
        or ``section:<name>:<field>``. When omitted, the directive is added
        to every field the findings already scheduled -- and is a no-op when
        the findings scheduled none, because a directive with no target is
        an edit with no address.
    canonical
        Which archive copy to revise. ``False`` (the default) is the staging
        draft, which is what the stage is FOR: an issue that has not been
        published yet.

        ``True`` targets ``data/released/<date>/`` and exists because
        ``data/staging/`` is gitignored, so a release-PR branch checked out
        in CI carries only the released copy. That is the copy the
        ``/revise`` PR command edits, and a live cycle there re-renders
        ``docs/released/<date>.html`` and ``docs/index.html`` so the page
        never disagrees with the JSON.

        What it deliberately does NOT do is bump ``Issue.revision``.
        DESIGN.md's "Issue Number Registry -> Same-date re-release" governs
        a corrected RE-publication -- an issue readers have already seen.
        ``/revise`` runs only on an open release PR (the workflow's
        ``state == 'open'`` guard), so the issue is still a draft that
        happens to live in the released folder, and stamping revision 1 on
        something never published would misdescribe the archive. If
        ``/revise`` is ever opened to merged PRs, the revision bump becomes
        mandatory and belongs with the Architect.
    min_severity
        Floor on ``ReviewFinding.severity``: one of ``note`` (the default,
        which filters nothing), ``minor``, ``major``, ``blocking``. Applied
        by code before any prompt is built. An unrecognised value is treated
        as the default and logged, because a typo'd floor must not silently
        drop every finding.
    """
    llm_usage.set_stage("revise")
    mode = "shadow" if shadow else "live"
    out_path = revisions_path(run_date, canonical=canonical)

    report = read_report(run_date, canonical=canonical)
    if report is None:
        return _refuse(
            run_date, mode, out_path,
            "no readable review.json at "
            f"{review_json_path(run_date, canonical=canonical)}; the "
            "revision engine acts only on a recorded review",
            instruction=instruction,
        )

    issue_path = paths.issue_path(run_date, canonical=canonical)
    try:
        issue_bytes = issue_path.read_bytes()
        issue_payload = json.loads(issue_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _refuse(
            run_date, mode, out_path,
            f"could not read staged issue.json at {issue_path}: {exc}",
            instruction=instruction,
        )
    if not isinstance(issue_payload, dict):
        return _refuse(
            run_date, mode, out_path, "staged issue.json is not a JSON object",
            instruction=instruction,
        )

    import hashlib

    sha_before = hashlib.sha256(issue_bytes).hexdigest()

    # --- filter 1: freshness. Does the review describe THIS file? --------
    freshness = _assess_freshness(
        report, report.findings, issue_payload, sha_before,
        run_date=run_date, canonical=canonical,
        has_targeted_instruction=bool(
            instruction.strip()
            and _parse_target_selector(instruction_target) is not None
        ),
    )
    if not freshness.ok:
        return _refuse(
            run_date, mode, out_path, freshness.reason,
            issue_sha256_before=sha_before, instruction=instruction,
        )

    # --- filter 2: severity. Of the valid findings, which were asked for?
    floor = normalise_min_severity(min_severity)
    findings, _ = filter_findings_by_severity(freshness.findings, floor)
    # Counted over text_edit findings only: a structural finding below the
    # floor was never going to be rewritten, and reporting it as a cost of
    # the floor would overstate what the operator's choice actually excluded.
    dropped_by_severity = _count_text_edits(freshness.findings) - (
        _count_text_edits(findings)
    )

    groups = _build_groups(
        findings, issue_payload, instruction, instruction_target,
    )
    if not groups:
        return _refuse(
            run_date, mode, out_path,
            _nothing_to_revise_reason(
                instruction, floor, dropped_by_severity,
            ),
            issue_sha256_before=sha_before, instruction=instruction,
        )

    changes: list[RevisionChange] = []
    for group in groups:
        changes.append(
            _revise_one_field(group, shadow=shadow, issue_payload=issue_payload)
        )

    sha_after: str | None = None
    render_note = ""
    applied = [c for c in changes if c.status == "applied"]
    if not shadow and applied:
        try:
            sha_after = _apply_changes(issue_path, issue_payload, applied)
        except Exception as exc:  # noqa: BLE001 -- never crash a caller
            _LOG.exception("revise: failed to write the revised issue.json")
            # The write failed, so nothing was applied. Downgrade every
            # accepted change to a rejection rather than record an edit that
            # is not on disk -- the artifact must describe the filesystem.
            changes = [
                _reject(c, "issue_write_failed") if c.status == "applied" else c
                for c in changes
            ]
            applied = []
            return _write_cycle(
                run_date, mode, out_path, changes, report,
                instruction=instruction,
                sha_before=sha_before, sha_after=None,
                note=f"issue.json write failed: {exc}",
            )
        # The JSON on disk has moved; the HTML rendered from it has not.
        # Re-rendering is code, it is cheap, and the /revise workflow commits
        # exactly these paths -- but a template failure must not turn an
        # applied edit into a crash, so the outcome lands in the note.
        render_note = _rerender_html(run_date, canonical=canonical)

    counts = {
        "applied": sum(1 for c in changes if c.status == "applied"),
        "proposed": sum(1 for c in changes if c.status == "proposed"),
        "rejected": sum(1 for c in changes if c.status == "rejected"),
    }
    note = (
        f"{len(groups)} field(s) targeted; "
        f"{counts['applied']} applied, {counts['proposed']} proposed, "
        f"{counts['rejected']} rejected"
    )
    for extra in (
        _severity_note(floor, dropped_by_severity), freshness.note, render_note,
    ):
        if extra:
            note += f"; {extra}"
    return _write_cycle(
        run_date, mode, out_path, changes, report,
        instruction=instruction,
        sha_before=sha_before, sha_after=sha_after, note=note,
    )


def _count_text_edits(findings: list[ReviewFinding]) -> int:
    """How many of these findings this stage could act on at all."""
    return sum(1 for f in findings if f.fix_kind == "text_edit")


def _severity_note(floor: str, dropped: int) -> str:
    """Audit line for the severity pre-filter. Silent when it dropped
    nothing -- a note that always fires is a note nobody reads."""
    if not dropped:
        return ""
    return f"{dropped} text_edit finding(s) below min_severity={floor} not sent"


def _nothing_to_revise_reason(
    instruction: str, floor: str, dropped_by_severity: int,
) -> str:
    """Why there was no work, in the operator's terms.

    "No text_edit findings" and "the severity floor removed all of them" look
    identical in the counts and mean opposite things: the first says the
    review found nothing to rewrite, the second says the operator asked for
    less than the review offered.
    """
    if dropped_by_severity:
        return (
            f"nothing to revise at min_severity={floor}: all "
            f"{dropped_by_severity} actionable finding(s) in this review are "
            "below the floor"
        )
    return "nothing to revise: no text_edit findings" + (
        " and the operator instruction named no target" if instruction else ""
    )


# ---------------------------------------------------------------------------
# Filter 1: severity. Deterministic, and deliberately so.
# ---------------------------------------------------------------------------

def normalise_min_severity(value: str | None) -> str:
    """Return a valid severity floor for ``value``.

    An unrecognised token falls back to ``MIN_SEVERITY_DEFAULT`` with a
    warning rather than raising: this is a failure-soft stage, and the safe
    reading of a typo is "the operator meant everything", not "drop the
    reviewer's entire report".
    """
    token = (value or "").strip().lower()
    if not token:
        return MIN_SEVERITY_DEFAULT
    if token not in SEVERITY_RANK:
        _LOG.warning(
            "revise: unknown min_severity %r (expected one of %s); using %r",
            value, ", ".join(MIN_SEVERITY_CHOICES), MIN_SEVERITY_DEFAULT,
        )
        return MIN_SEVERITY_DEFAULT
    return token


def filter_findings_by_severity(
    findings: list[ReviewFinding], min_severity: str,
) -> tuple[list[ReviewFinding], int]:
    """Keep the findings at or above ``min_severity``; return them and the
    number dropped.

    This is the code answer to "apply only the major recommendations". The
    LLM never sees a dropped finding, so the filter costs nothing and cannot
    be argued with -- which is the whole reason it is not a prompt.

    A finding carrying a severity this scale does not know is KEPT, and
    logged. The model contract makes that unreachable today; if it ever
    becomes reachable, an unrecognised severity is a reason to look, not a
    reason to silently discard editorial evidence.
    """
    floor = SEVERITY_RANK[normalise_min_severity(min_severity)]
    kept: list[ReviewFinding] = []
    dropped = 0
    for finding in findings:
        rank = SEVERITY_RANK.get(str(finding.severity))
        if rank is None:
            _LOG.warning(
                "revise: finding %s has severity %r, which is not on the "
                "known scale; keeping it", finding.finding_id, finding.severity,
            )
            kept.append(finding)
            continue
        if rank >= floor:
            kept.append(finding)
        else:
            dropped += 1
    if dropped:
        _LOG.info(
            "revise: severity filter dropped %d finding(s) below %s",
            dropped, normalise_min_severity(min_severity),
        )
    return kept, dropped


# ---------------------------------------------------------------------------
# Filter 2: freshness. Does the review still describe the text on disk?
# ---------------------------------------------------------------------------

@dataclass
class _Freshness:
    """The freshness verdict for one invocation.

    ``findings`` is the subset still anchored in the text, which is what the
    caller should act on -- on the released copy that can be narrower than
    what came in.
    """
    ok: bool
    basis: str
    findings: list[ReviewFinding] = field(default_factory=list)
    reason: str = ""
    note: str = ""


def _assess_freshness(
    report: ReviewReport,
    findings: list[ReviewFinding],
    issue_payload: dict[str, Any],
    sha_before: str,
    *,
    run_date: _dt.date,
    canonical: bool,
    has_targeted_instruction: bool,
) -> _Freshness:
    """Decide whether this review may be acted on, and with which findings.

    Two bases, and the second exists because the first is unavailable by
    construction on the released copy:

    ``sha_match``
        The review's ``issue_sha256`` equals a hash of the bytes on disk.
        Exact, cheap, and the only accepted basis for the staging draft --
        where a mismatch means the draft was regenerated after review and
        ``aiv review`` is one command away.

    ``quote_evidence``
        Every ``text_edit`` finding whose verbatim ``quote`` still appears in
        the field it points at is fresh; the rest are dropped. Accepted only
        on the released copy, because ``render.release_promote`` stamps
        ``issue_number`` into the canonical ``issue.json`` -- so its bytes
        can NEVER equal the staging bytes the reviewer hashed, while every
        sentence in it is identical. A byte check there is not a freshness
        test, it is an unconditional refusal, and that is exactly what the
        first live ``/revise`` run hit (2026-08-04).

        Per-finding quote presence is the same evidence rule ``src/review.py``
        already applies when it filters the reviewer's output, so "the
        finding points at text that is really there" means one thing across
        both stages.

    A targeted operator instruction (``--target``) is self-anchoring: it
    names its own field and the model is shown that field's current text, so
    it does not need the review to be fresh. It can therefore carry a cycle
    on its own when every finding has aged out.
    """
    if report.issue_sha256 == sha_before:
        return _Freshness(ok=True, basis="sha_match", findings=findings)

    stale_line = (
        "the review is stale: it was written against issue.json "
        f"{report.issue_sha256[:12]}..., the file on disk is "
        f"{sha_before[:12]}...."
    )
    if not canonical:
        return _Freshness(
            ok=False, basis="",
            reason=(
                f"{stale_line} Re-run `aiv review --date "
                f"{run_date.isoformat()}` before revising."
            ),
        )

    field_texts = index_issue_fields(issue_payload)
    text_edits = [f for f in findings if f.fix_kind == "text_edit"]
    anchored = [
        f for f in text_edits
        if quote_present(f.quote, field_texts.get(target_key(f.target), ""))
    ]
    aged_out = len(text_edits) - len(anchored)

    if not anchored and not has_targeted_instruction:
        return _Freshness(
            ok=False, basis="",
            reason=(
                f"{stale_line} None of the review's {len(text_edits)} "
                "text_edit finding(s) still quote text present in the "
                "released copy, so there is no anchored edit to make. "
                f"Re-run `aiv review --date {run_date.isoformat()} "
                "--released` before revising."
            ),
        )

    note = (
        f"released copy: freshness by quote evidence, {len(anchored)} of "
        f"{len(text_edits)} text_edit finding(s) still anchored"
    )
    if aged_out:
        _LOG.warning(
            "revise: %d finding(s) quote text no longer in the released "
            "copy; dropped", aged_out,
        )
    # Non-text_edit findings are carried through untouched: _build_groups
    # discards them anyway, and quote-checking a finding this stage will
    # never act on would be work with no consumer.
    keep_ids = {f.finding_id for f in anchored}
    return _Freshness(
        ok=True, basis="quote_evidence",
        findings=[
            f for f in findings
            if f.fix_kind != "text_edit" or f.finding_id in keep_ids
        ],
        note=note,
    )


# ---------------------------------------------------------------------------
# Grouping.
# ---------------------------------------------------------------------------

def _build_groups(
    findings: list[ReviewFinding],
    issue_payload: dict[str, Any],
    instruction: str,
    instruction_target: str,
) -> list[_FieldGroup]:
    """Group the actionable findings by target field, then attach any
    operator directive.

    Takes an explicit finding list rather than the whole report because the
    severity and freshness filters have already run: this function groups
    what survived, and cannot accidentally reach back past a filter to the
    full report.

    Only ``fix_kind == "text_edit"`` findings are collected; everything else
    in the review is somebody's judgement call, not a rewrite. A finding
    whose target no longer resolves to text is skipped (the freshness check
    makes that near-impossible, but "near" is not "never").
    """
    field_texts = index_issue_fields(issue_payload)
    groups: dict[str, _FieldGroup] = {}

    for finding in findings:
        if finding.fix_kind != "text_edit":
            continue
        key = target_key(finding.target)
        text = field_texts.get(key)
        if text is None:
            _LOG.warning(
                "revise: skipping finding %s -- target %s not found in the "
                "issue", finding.finding_id, key,
            )
            continue
        if key not in groups:
            groups[key] = _FieldGroup(target=finding.target, text=text)
        groups[key].findings.append(finding)

    if instruction.strip():
        explicit = _parse_target_selector(instruction_target)
        if explicit is not None:
            key = target_key(explicit)
            text = field_texts.get(key)
            if text is None:
                _LOG.warning(
                    "revise: operator target %s is not a field in the issue; "
                    "instruction not applied", key,
                )
            else:
                if key not in groups:
                    groups[key] = _FieldGroup(target=explicit, text=text)
                groups[key].operator_instructions.append(instruction.strip())
        elif groups:
            # No explicit target: the directive rides along with the fields
            # already being revised. It cannot open a new field on its own,
            # because an instruction with no address is not something code
            # should guess the address for.
            for group in groups.values():
                group.operator_instructions.append(instruction.strip())
        else:
            _LOG.warning(
                "revise: operator instruction supplied with no --target and "
                "no text_edit findings to attach it to; ignored",
            )

    # Deterministic order so two runs over the same review produce the same
    # revisions.jsonl ordering.
    return [groups[key] for key in sorted(groups)]


def _parse_target_selector(selector: str) -> ReviewTarget | None:
    """Parse ``story:<cluster_id>:<field>`` / ``section:<name>:<field>`` /
    ``digest:<index>:<field>``.

    Returns ``None`` for an empty or unparseable selector -- the caller
    treats that as "no explicit target", never as an error, because a
    typo'd selector must not stop the findings from being acted on.
    """
    raw = (selector or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) != 3:
        _LOG.warning(
            "revise: --target %r is not 'story:<id>:<field>', "
            "'section:<name>:<field>', or 'digest:<index>:<field>'", selector,
        )
        return None
    kind, locator, field_name = (p.strip() for p in parts)
    try:
        if kind == "story":
            return ReviewTarget(
                kind="story", story_id=locator, field=field_name,  # type: ignore[arg-type]
            )
        if kind == "section":
            return ReviewTarget(
                kind="section", section=locator, field=field_name,  # type: ignore[arg-type]
            )
        if kind == "digest":
            if not locator.isdigit():
                _LOG.warning(
                    "revise: --target %r digest index must be a 0-based "
                    "integer", selector,
                )
                return None
            return ReviewTarget(
                kind="digest", digest_index=int(locator),
                field=field_name,  # type: ignore[arg-type]
            )
    except Exception as exc:  # noqa: BLE001 -- a bad selector is user input
        _LOG.warning("revise: --target %r is not a valid target: %s",
                     selector, exc)
        return None
    _LOG.warning("revise: --target %r has unknown kind %r", selector, kind)
    return None


# ---------------------------------------------------------------------------
# The per-field revision.
# ---------------------------------------------------------------------------

def _revise_one_field(
    group: _FieldGroup,
    *,
    shadow: bool,
    issue_payload: dict[str, Any] | None = None,
) -> RevisionChange:
    """Produce (and validate) one field's replacement text.

    One LLM call, one field. On a length failure we retry once quoting the
    exact constraint -- that is the one failure a model can fix when told
    the number. Every other rejection is final for this cycle.

    ``issue_payload`` (wave three) feeds the spec re-validation for the
    context-dependent fields: a revised digest lead/sentence re-runs
    ``summarise._digest_violations`` (delta against the unrevised digest)
    and a revised synthesis re-runs ``summarise._synthesis_violations``,
    because the generic gate cannot see the word budgets and deconfliction
    rules those surfaces were generated under.
    """
    recommendation = " | ".join(group.instructions)[:2000]
    field_name = group.target.field
    bounds = FIELD_BOUNDS.get(field_name)
    if bounds is None:
        # The KeyError class the take integration taught us: the review
        # vocabulary can gain a field before this stage learns it (or a
        # future skew can reintroduce the gap). A field this stage cannot
        # bound is a field it must refuse, not crash on -- revise_day has
        # no try around this call, so a raise here would break the
        # failure-soft contract of the whole stage.
        _LOG.error(
            "revise: no FIELD_BOUNDS entry for field %r (target %s) -- "
            "refusing the edit; FIELD_BOUNDS must learn the field first",
            field_name, group.key,
        )
        return _rejected_change(
            group, "", recommendation, "unknown_field_bounds",
        )
    minimum, maximum = bounds

    candidate = ""
    failure = ""
    for attempt in range(1, _LENGTH_RETRY_BUDGET + 2):
        prompt = _build_revise_prompt(group, retry_reason=failure)
        try:
            raw = _call_revise_llm(prompt)
        except Exception as exc:  # noqa: BLE001 -- one field must not kill the run
            _LOG.exception("revise: LLM call failed for %s", group.key)
            return _rejected_change(
                group, "", recommendation,
                f"llm_call_failed: {type(exc).__name__}",
            )
        candidate = _clean_replacement(raw, field_name)
        if not candidate:
            failure = "the response was empty"
            continue
        if minimum <= len(candidate) <= maximum:
            break
        failure = (
            f"the replacement was {len(candidate)} characters; this field "
            f"must be between {minimum} and {maximum} characters"
        )
        _LOG.warning(
            "revise: %s replacement failed the length constraint "
            "(attempt %d): %s", group.key, attempt, failure,
        )
    else:
        return _rejected_change(
            group, candidate, recommendation, "length_constraint",
        )

    reason = validate_replacement(group, candidate)
    if reason:
        _LOG.warning(
            "revise: rejecting replacement for %s -- %s", group.key, reason,
        )
        return _rejected_change(group, candidate, recommendation, reason)

    spec_reason = _spec_violation_reason(group, candidate, issue_payload)
    if spec_reason:
        _LOG.warning(
            "revise: rejecting replacement for %s -- %s",
            group.key, spec_reason,
        )
        return _rejected_change(group, candidate, recommendation, spec_reason)

    if candidate == group.text:
        return _rejected_change(group, candidate, recommendation, "no_change")

    return RevisionChange(
        target=group.target,
        finding_ids=[f.finding_id for f in group.findings],
        before=group.text,
        after=candidate,
        recommendation=recommendation,
        rationale=_describe_change(group.text, candidate),
        status="proposed" if shadow else "applied",
    )


def _rejected_change(
    group: _FieldGroup, candidate: str, recommendation: str, reason: str,
) -> RevisionChange:
    """Build the rejection record. ``after`` keeps the refused candidate on
    purpose: the rejected text plus the reason is the evidence that tunes
    the prompt, and throwing it away would leave only a count."""
    return RevisionChange(
        target=group.target,
        finding_ids=[f.finding_id for f in group.findings],
        before=group.text,
        after=candidate[:4000],
        recommendation=recommendation,
        rationale=f"refused by the code-side validation gate: {reason}",
        status="rejected",
        reject_reason=reason[:200],
    )


def _reject(change: RevisionChange, reason: str) -> RevisionChange:
    """Downgrade an accepted change to a rejection (used when the issue
    write itself failed after the changes were validated)."""
    return RevisionChange(
        target=change.target,
        finding_ids=list(change.finding_ids),
        before=change.before,
        after=change.after,
        recommendation=change.recommendation,
        rationale=change.rationale,
        status="rejected",
        reject_reason=reason[:200],
    )


def _describe_change(before: str, after: str) -> str:
    """Code-authored one-line account of what moved. The model returns only
    replacement text (no rationale field to parse, no second failure mode),
    so the audit line is computed from the two strings we have."""
    before_tokens = _tokenise(before)
    after_tokens = _tokenise(after)
    distance = _token_edit_distance(before_tokens, after_tokens)
    return (
        f"replaced {len(before)} chars ({len(before_tokens)} tokens) with "
        f"{len(after)} chars ({len(after_tokens)} tokens); token edit "
        f"distance {distance}"
    )


# ---------------------------------------------------------------------------
# The validation gate -- plain code, no LLM. Every rule here exists because
# an unconstrained rewrite can silently change what the issue asserts.
# ---------------------------------------------------------------------------

def validate_replacement(group: _FieldGroup, candidate: str) -> str:
    """Check a replacement against every containment rule.

    Returns ``""`` when the replacement is acceptable, or a short machine
    token naming the rule that refused it. Tokens are stable: they land in
    ``RevisionChange.reject_reason`` and are what a dashboard counts.

    The rules, and what each one is protecting:

    ``deletion_quote_survived``
        A finding said "delete X" and X is still there. A model that
        rephrases flagged text instead of removing it has not done what was
        asked, and only the quote's absence proves it did.
    ``edit_distance_exceeded``
        The edit is larger than ``_MAX_EDIT_RATIO`` of the original (with an
        absolute floor of ``_MIN_EDIT_ALLOWANCE_TOKENS`` so short fields
        stay revisable). Beyond that it is a rewrite, and the story is no
        longer the one that was ranked, summarised, and fact-checked.
    ``url_dropped``
        A source URL that was in the text is gone. A dropped link is a
        dropped attribution.
    ``numeral_dropped`` / ``numeral_invented``
        A number vanished, or a number appeared that was in neither the
        original nor any finding. This is the most dangerous edit class,
        because the sentence around a changed number still reads fine.
    """
    before, after = group.text, candidate

    # --- deletion-class quotes must be gone ------------------------------
    for finding in group.findings:
        if not _DELETION_VERB_RE.search(finding.instruction):
            continue
        if quote_present(finding.quote, after):
            return "deletion_quote_survived"

    # --- containment: the edit must stay small ---------------------------
    before_tokens = _tokenise(before)
    after_tokens = _tokenise(after)
    distance = _token_edit_distance(before_tokens, after_tokens)
    allowance = max(
        _MIN_EDIT_ALLOWANCE_TOKENS,
        math.floor(_MAX_EDIT_RATIO * len(before_tokens)),
    )
    if distance > allowance:
        return "edit_distance_exceeded"

    # --- every pre-existing URL survives ---------------------------------
    for url in _URL_RE.findall(before):
        if url not in after:
            return "url_dropped"

    # --- numerals: none dropped, none invented ---------------------------
    named = _numerals_named_in_directives(group)
    before_numerals = _numeral_set(before)
    after_numerals = _numeral_set(after)
    if (before_numerals - after_numerals) - named:
        return "numeral_dropped"
    if (after_numerals - before_numerals) - named:
        return "numeral_invented"

    return ""


def _spec_violation_reason(
    group: _FieldGroup,
    candidate: str,
    issue_payload: dict[str, Any] | None,
) -> str:
    """Context-dependent spec re-validation for the redesign surfaces (R2).

    The generic gate checks containment; it cannot check the ratified
    digest budgets (lead 3-6 words, one sentence of 14-22, the 100-word
    total, take/synthesis deconfliction) or the synthesis word/sentence
    budgets, because those are properties of the WHOLE issue, not the one
    field. So a revised ``digest_lead``/``digest_sentence`` re-runs
    ``summarise._digest_violations`` and rejects on any violation the
    unrevised digest did not already carry (delta, so a pre-existing
    violation elsewhere cannot make a bullet permanently unrevisable), and
    a revised ``synthesis`` re-runs ``summarise._synthesis_violations``
    outright (its checks are text-local).

    Returns ``""`` when acceptable, or a stable reject token. Failure-soft:
    an exception inside the check is logged and returns ``""`` -- a broken
    advisory spec check must not take the revision engine down with it.
    """
    field_name = group.target.field
    if field_name not in ("synthesis", "digest_lead", "digest_sentence"):
        return ""
    if not isinstance(issue_payload, dict):
        # No issue context (should not happen on the revise_day path) --
        # the generic gate has already run; do not invent a rejection.
        _LOG.warning(
            "revise: no issue payload for the %s spec check -- skipped",
            field_name,
        )
        return ""
    try:
        if field_name == "synthesis":
            return _synthesis_spec_reason(group, candidate, issue_payload)
        return _digest_spec_reason(group, candidate, issue_payload)
    except Exception:  # noqa: BLE001 -- advisory check, never a crash
        _LOG.exception(
            "revise: spec re-validation for %s raised -- check skipped",
            group.key,
        )
        return ""


def _synthesis_spec_reason(
    group: _FieldGroup, candidate: str, issue_payload: dict[str, Any]
) -> str:
    """Re-run summarise's synthesis checks on the replacement text."""
    from src.summarise import _synthesis_violations  # lazy: heavy module

    section_name = group.target.section or ""
    quiet_day = False
    for section in issue_payload.get("sections") or []:
        if isinstance(section, dict) and section.get("name") == section_name:
            # The quiet-day Currents framing runs under a relaxed floor
            # (summarise's quiet_day parameter); mirror that here or a
            # legitimate quiet-day rewrite could never pass.
            quiet_day = (
                section_name == "currents" and not (section.get("stories") or [])
            )
            break
    violations = _synthesis_violations(candidate, quiet_day=quiet_day)
    if violations:
        _LOG.warning(
            "revise: revised %s synthesis violates the synthesis spec: %s",
            section_name, "; ".join(violations),
        )
        return "synthesis_spec_violation"
    return ""


def _digest_spec_reason(
    group: _FieldGroup, candidate: str, issue_payload: dict[str, Any]
) -> str:
    """Re-run summarise's digest checks on the digest with the replacement
    applied, rejecting only NEW violations (delta vs. the stored digest)."""
    import copy as _copy

    from src.summarise import _digest_violations  # lazy: heavy module

    digest = issue_payload.get("digest")
    idx = group.target.digest_index
    if (
        not isinstance(digest, list)
        or idx is None
        or not 0 <= idx < len(digest)
        or not isinstance(digest[idx], dict)
    ):
        return "digest_target_missing"

    context = _digest_check_context(issue_payload)

    def _violations(bullets_source: list[Any]) -> list[str]:
        bullets = []
        for bullet in bullets_source:
            if not isinstance(bullet, dict):
                continue
            story_ids = [
                str(s) for s in (bullet.get("story_ids") or [])
                if isinstance(s, str)
            ]
            bullets.append({
                # The stored DigestBullet has no section field (it was an
                # LLM-payload key); derive it from the primary story so
                # the structural checks still run.
                "section": context["section_of"].get(
                    story_ids[0] if story_ids else "", ""
                ),
                "lead": str(bullet.get("lead") or ""),
                "sentence": str(bullet.get("sentence") or ""),
                "story_ids": story_ids,
            })
        eligible = []
        for entry in bullets[1:]:
            if not eligible or eligible[-1] != entry["section"]:
                eligible.append(entry["section"])
        return _digest_violations(
            bullets,
            pulse_id=context["pulse_id"],
            eligible_names=eligible,
            allowed_ids=context["allowed_ids"],
            takes=context["takes"],
            syntheses=context["syntheses"],
        )

    before = _violations(digest)
    revised = _copy.deepcopy(digest)
    model_field = "lead" if group.target.field == "digest_lead" else "sentence"
    revised[idx][model_field] = candidate
    after = _violations(revised)

    new = [v for v in after if v not in before]
    if new:
        _LOG.warning(
            "revise: revised digest bullet %d violates the digest spec: %s",
            idx + 1, "; ".join(new),
        )
        return "digest_spec_violation"
    return ""


def _digest_check_context(issue_payload: dict[str, Any]) -> dict[str, Any]:
    """Derive the ``_digest_violations`` context from a raw issue payload:
    the pulse story id, each story's section, per-section allowed ids, the
    takes, and the syntheses. Mirrors what summarise assembled at
    generation time, reconstructed from what the issue persists."""
    section_of: dict[str, str] = {}
    allowed_ids: dict[str, list[str]] = {}
    takes: list[str] = []
    syntheses: dict[str, str | None] = {}

    def _walk(name: str, container: dict[str, Any]) -> list[str]:
        ids: list[str] = []
        for story in container.get("stories") or []:
            if not isinstance(story, dict):
                continue
            sid = story.get("story_id")
            if isinstance(sid, str) and sid:
                ids.append(sid)
                section_of[sid] = name
            take = story.get("take")
            if isinstance(take, str) and take.strip():
                takes.append(take.strip())
        return ids

    pulse = issue_payload.get("pulse") or {}
    pulse_ids = _walk("pulse", pulse) if isinstance(pulse, dict) else []
    allowed_ids["pulse"] = pulse_ids

    for section in issue_payload.get("sections") or []:
        if not isinstance(section, dict):
            continue
        name = str(section.get("name") or "")
        if not name:
            continue
        allowed_ids[name] = _walk(name, section)
        synthesis = section.get("synthesis")
        syntheses[name] = (
            synthesis.strip()
            if isinstance(synthesis, str) and synthesis.strip() else None
        )

    return {
        "pulse_id": pulse_ids[0] if pulse_ids else "",
        "section_of": section_of,
        "allowed_ids": allowed_ids,
        "takes": takes,
        "syntheses": syntheses,
    }


def _numerals_named_in_directives(group: _FieldGroup) -> set[str]:
    """Numerals a finding or operator directive explicitly mentions.

    A finding that says "the source says 30x, not 10x" is licensing exactly
    that change; a finding that says nothing about numbers licenses none.
    This is what lets a correction through while still refusing an invented
    figure -- the licence has to be written down."""
    named: set[str] = set()
    for finding in group.findings:
        named |= _numeral_set(finding.instruction)
        named |= _numeral_set(finding.quote)
    for instruction in group.operator_instructions:
        named |= _numeral_set(instruction)
    return named


def _numeral_set(text: str) -> set[str]:
    """Normalised numeral tokens in ``text``.

    Thousands separators and trailing punctuation are stripped so "1,200"
    and "1200" compare equal -- a formatting change is not a factual one,
    and flagging it would train operators to ignore the check."""
    out: set[str] = set()
    for match in _NUMERAL_RE.findall(text or ""):
        token = match.replace(",", "").rstrip(".")
        if token:
            out.add(token)
    return out


def _tokenise(text: str) -> list[str]:
    """Whitespace tokens, lowercased. Edit distance is measured on tokens
    rather than characters because a token is the unit a reader notices: a
    changed word matters, a changed comma does not."""
    return (text or "").lower().split()


def _token_edit_distance(a: list[str], b: list[str]) -> int:
    """Levenshtein distance over token lists.

    Straight dynamic programming: fields top out at a few hundred tokens,
    so the quadratic cost is microseconds and the readability is worth more
    than an optimisation nobody will need."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, token_a in enumerate(a, start=1):
        current = [i]
        for j, token_b in enumerate(b, start=1):
            current.append(min(
                previous[j] + 1,          # deletion
                current[j - 1] + 1,       # insertion
                previous[j - 1] + (token_a != token_b),  # substitution
            ))
        previous = current
    return previous[-1]


# ---------------------------------------------------------------------------
# Prompt assembly + LLM call.
# ---------------------------------------------------------------------------

_FIELD_GUIDANCE = {
    "headline": (
        "This is a story HEADLINE. Keep it a noun phrase that names the "
        "artefact or the actor. Names appear only when a reader would "
        "recognise them; an unknown name belongs in the body, and the "
        "headline describes the thing instead."
    ),
    "summary": (
        "This is a story BODY. It must keep its closing shape: the Pulse "
        "closes on a plain take, Big Picture on a strategic question, "
        "Hands-On on an imperative action with a specific artefact and "
        "trigger, Currents on a two-sided calibrated stake. If the closing "
        "sentence is not what the instruction is about, leave it as it is."
    ),
    "take": (
        "This is the story's TAKE -- one declarative sentence stating the "
        "publication's position, 8-16 words (hard cap 18). Never a question, "
        "never an imperative, no hedges, no labels like 'Bottom line:'. It "
        "must assert a consequence, not restate the body or instruct the "
        "reader."
    ),
    "intro_lead": (
        "This is a section INTRO LEAD -- a short bold phrase, a handful of "
        "words. It frames the pattern across the section's stories."
    ),
    "intro_body": (
        "This is a section INTRO BODY -- one or two sentences framing the "
        "day's pattern in this section."
    ),
    "synthesis": (
        "This is a section SYNTHESIS -- one italic paragraph of two or "
        "three sentences framing the pattern across the section's stories. "
        "Section-anchored: it names what the stories add up to; the "
        "stories carry the specifics. The first sentence must be about "
        "today's stories, never a detachable aphorism."
    ),
    "digest_lead": (
        "This is a digest bullet's bold LEAD in \"The 30-second read\" -- "
        "3-6 words ending in a full stop, NAMING what the bullet is about. "
        "Never an imperative, never a question, no artifact names a senior "
        "practitioner would not recognise."
    ),
    "digest_sentence": (
        "This is a digest bullet's SENTENCE in \"The 30-second read\" -- "
        "exactly ONE sentence of 14-22 words (up to 26 only as a "
        "semicolon-list of at most 3 clauses), story-anchored and "
        "concrete: the number, the actor, the mechanism. It compresses "
        "the story afresh; it must not paraphrase the story's take."
    ),
}


def _build_revise_prompt(group: _FieldGroup, *, retry_reason: str = "") -> str:
    """Assemble the field-scoped revision prompt.

    The model sees the field's current text and the directives about it --
    nothing else. It does not see the story's other fields, the section, the
    rest of the issue, or the sources. That is the containment rule made
    literal: text the model cannot see is text the model cannot drift.

    The code-side constraints are restated here because telling the model the
    rule up front is cheaper than a retry, and a retry is cheaper than a
    rejection that leaves the finding unaddressed.
    """
    minimum, maximum = FIELD_BOUNDS[group.target.field]
    directives = "\n".join(
        f"{i}. {text}" for i, text in enumerate(group.instructions, start=1)
    )
    quotes = "\n".join(
        f'   - "{f.quote}"  ({f.criterion}, {f.severity})'
        for f in group.findings
    ) or "   (none -- operator instruction only)"

    retry_block = ""
    if retry_reason:
        retry_block = (
            "\nYOUR PREVIOUS ATTEMPT WAS REJECTED: "
            f"{retry_reason}. Try again, respecting the length limit "
            "exactly.\n"
        )

    return f"""\
You are revising ONE field of an AI Vector story. You can see only that
field. Do not add facts, names, numbers, or claims that are not already in
the text below -- you cannot check them from here, and anything you invent
will be rejected by an automated check before it reaches a reader.

{_FIELD_GUIDANCE.get(group.target.field, "")}

CURRENT TEXT:
{group.text}

THE EDITOR FLAGGED THESE SPANS:
{quotes}

WHAT TO CHANGE:
{directives}

RULES (an automated check enforces every one of these; a violation means
your revision is discarded and the original text ships unchanged):
- Change ONLY what the instructions ask for. Leave every other sentence
  exactly as it is, word for word.
- Keep every number that is already in the text, unless an instruction
  explicitly says to change it. Do not introduce any number that is not
  already in the text or named in an instruction.
- Keep every URL that is already in the text.
- If an instruction says to delete or remove a span, that span must not
  appear in any form in your output.
- The result must be between {minimum} and {maximum} characters.
- Keep the voice: direct, specific, no hype, no hedge accumulation, no
  "it remains to be seen", no "time will tell".
{retry_block}
Return ONLY the revised text. No preamble, no explanation, no quotation
marks around it, no markdown fences.
"""


def _clean_replacement(raw: str, field_name: str) -> str:
    """Normalise the model's response into field text.

    Strips markdown fences, a leading label the model sometimes adds
    ("Revised text:"), and wrapping quotation marks. Collapses whitespace
    runs to single spaces: every revisable field is a single paragraph in
    this schema, and a stray newline would render as one.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    text = re.sub(
        r"^(revised|replacement|new)\s+(text|headline|summary|version)\s*:\s*",
        "", text, flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def _call_revise_llm(prompt: str) -> str:
    """Issue one revision call.

    Reuses ``rank._llm_call`` for provider routing, the same way review and
    verify do. The writer model (``LLM_MODEL``) handles this deliberately:
    the reviewer is deliberately a different, stronger model because judging
    wants independence, but revising is writing, and writing wants the voice
    the rest of the issue was written in.
    """
    from src import rank as _rank

    temperature = float(
        os.getenv("LLM_TEMPERATURE_REVISE", str(_REVISE_TEMPERATURE_DEFAULT))
    )
    timeout = os.getenv("LLM_REVISE_TIMEOUT_SECONDS") or ""
    prior_timeout = os.environ.get("LLM_TIMEOUT_SECONDS")
    os.environ["LLM_TIMEOUT_SECONDS"] = (
        timeout.strip() or str(_REVISE_TIMEOUT_DEFAULT)
    )
    try:
        return _rank._llm_call(
            prompt, temperature=temperature, max_tokens=_REVISE_MAX_TOKENS,
        )
    finally:
        if prior_timeout is None:
            os.environ.pop("LLM_TIMEOUT_SECONDS", None)
        else:
            os.environ["LLM_TIMEOUT_SECONDS"] = prior_timeout


# ---------------------------------------------------------------------------
# Applying changes to issue.json.
# ---------------------------------------------------------------------------

def _apply_changes(
    issue_path: Path,
    issue_payload: dict[str, Any],
    changes: list[RevisionChange],
) -> str:
    """Write the accepted changes into ``issue.json`` and return the new
    SHA-256.

    Mutates the payload we already parsed (no second read), clears
    ``verification`` on every story whose text changed, and rewrites the
    file atomically -- the same ``.tmp`` + fsync + rename pattern
    ``verify._write_issue_payload`` uses, so a reader never sees
    a half-written issue.

    Clearing ``verification`` is code's call, not the model's: the
    fact-check was performed against text that no longer exists, and
    ``None`` correctly means "not verified" while a retained ``clean``
    would assert something nobody checked.
    """
    stories = _index_story_blocks(issue_payload)
    sections = _index_section_blocks(issue_payload)
    touched_stories: set[str] = set()

    for change in changes:
        target = change.target
        if target.kind == "story":
            block = stories.get(target.story_id or "")
            if block is None:
                continue
            block[target.field] = change.after
            block["verification"] = None
            touched_stories.add(target.story_id or "")
        elif target.kind == "digest":
            # Route via digest_index (R2): the review token maps onto the
            # model field (digest_lead -> lead, digest_sentence -> sentence).
            digest = issue_payload.get("digest")
            idx = target.digest_index
            if (
                not isinstance(digest, list)
                or idx is None
                or not 0 <= idx < len(digest)
                or not isinstance(digest[idx], dict)
            ):
                _LOG.warning(
                    "revise: digest bullet %s not found while applying -- "
                    "change skipped", idx,
                )
                continue
            model_field = (
                "lead" if target.field == "digest_lead" else "sentence"
            )
            digest[idx][model_field] = change.after
            # The bullet's verify verdicts live on its PRIMARY story's
            # verification (digest contract V3); they were about text that
            # no longer exists, so the same clearing rule applies.
            primary_ids = digest[idx].get("story_ids") or []
            primary = primary_ids[0] if primary_ids else ""
            block = stories.get(str(primary))
            if block is not None:
                block["verification"] = None
                touched_stories.add(str(primary))
        else:
            section = sections.get(target.section or "")
            if section is None:
                continue
            section[target.field] = change.after
            if target.field == "synthesis":
                # Synthesis verdicts attach to the section's FIRST story
                # (verify v0.7); an edited synthesis stales them exactly
                # as an edited body stales its own.
                for story in section.get("stories") or []:
                    if isinstance(story, dict):
                        sid = story.get("story_id")
                        story["verification"] = None
                        if isinstance(sid, str) and sid:
                            touched_stories.add(sid)
                        break

    _LOG.info(
        "revise: applied %d change(s); cleared verification on %d story(ies)",
        len(changes), len(touched_stories),
    )

    issue_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = issue_path.with_suffix(issue_path.suffix + ".tmp")
    payload_bytes = (
        json.dumps(issue_payload, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    with tmp.open("wb") as fh:
        fh.write(payload_bytes)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, issue_path)

    import hashlib

    return hashlib.sha256(payload_bytes).hexdigest()


def _rerender_html(run_date: _dt.date, *, canonical: bool) -> str:
    """Re-render the HTML for the copy that was just edited.

    Returns a short note for the cycle log: empty when the render succeeded,
    a reason when it did not.

    Why this belongs here. The edit is only half-done while ``issue.json``
    says one thing and the page a reader opens says another, and no caller
    closes that gap on the released copy -- the ``/revise`` workflow commits
    ``docs/released/<date>.html`` and ``docs/index.html`` and expects this
    stage to have written them. Rendering is pure code over the file we just
    wrote (No Token Wasted: no LLM anywhere near it).

    Failure-soft, and the asymmetry is deliberate: the JSON write already
    succeeded, so raising here would report a failed cycle whose edits are on
    disk. A stale page is visible and re-renderable in one command; a
    misreported cycle is neither.
    """
    from src import render as render_mod

    mode = "release" if canonical else "preview"
    try:
        out = render_mod.render(run_date, mode=mode)
    except Exception as exc:  # noqa: BLE001 -- the edit is written; the page can wait
        _LOG.exception(
            "revise: %s render failed after applying the edits", mode,
        )
        return f"{mode} render FAILED ({type(exc).__name__}: {exc})"
    _LOG.info("revise: re-rendered %s after applying the edits", out)
    return f"re-rendered {out}"


def _index_story_blocks(issue_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``{story_id: the mutable story dict}`` across Pulse and all sections."""
    out: dict[str, dict[str, Any]] = {}
    pulse = issue_payload.get("pulse") or {}
    containers = [pulse] if isinstance(pulse, dict) else []
    containers += [
        s for s in (issue_payload.get("sections") or []) if isinstance(s, dict)
    ]
    for container in containers:
        for story in container.get("stories") or []:
            if not isinstance(story, dict):
                continue
            story_id = story.get("story_id")
            if isinstance(story_id, str) and story_id:
                out[story_id] = story
    return out


def _index_section_blocks(
    issue_payload: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """``{section name: the mutable section dict}``, Pulse included."""
    out: dict[str, dict[str, Any]] = {}
    pulse = issue_payload.get("pulse") or {}
    if isinstance(pulse, dict):
        out["pulse"] = pulse
    for section in issue_payload.get("sections") or []:
        if not isinstance(section, dict):
            continue
        name = section.get("name")
        if isinstance(name, str) and name:
            out[name] = section
    return out


# ---------------------------------------------------------------------------
# revisions.jsonl.
# ---------------------------------------------------------------------------

def _write_cycle(
    run_date: _dt.date,
    mode: str,
    out_path: Path,
    changes: list[RevisionChange],
    report: ReviewReport | None,
    *,
    instruction: str,
    sha_before: str,
    sha_after: str | None,
    note: str,
) -> RevisionReport:
    """Append one ``RevisionCycle`` to ``revisions.jsonl`` and build the
    caller-facing report.

    The file is an append-only log of what the engine did to this date's
    draft, so a cycle NEVER overwrites a previous one -- the cycle number is
    derived from the existing line count. The write is atomic (read all,
    rewrite via ``.tmp``), which is cheap at this size and means a crash
    mid-write cannot truncate the history.
    """
    existing = _read_cycle_lines(out_path)
    cycle = RevisionCycle(
        date=run_date,
        cycle=len(existing) + 1,
        mode=mode,  # type: ignore[arg-type]
        changes=changes,
        operator_instruction=instruction.strip()[:2000],
        generated_at=_dt.datetime.now(_dt.timezone.utc),
        prompt_version=REVISE_PROMPT_VERSION,
        review_prompt_version=report.prompt_version if report else None,
        issue_sha256_before=sha_before,
        issue_sha256_after=sha_after,
        note=note[:2000],
    )
    try:
        _append_cycle(out_path, existing, cycle)
    except Exception:  # noqa: BLE001 -- the edit is done; the log is best-effort
        _LOG.exception("revise: could not write %s", out_path)

    return RevisionReport(
        date=run_date,
        mode=mode,
        ran=True,
        applied=sum(1 for c in changes if c.status == "applied"),
        proposed=sum(1 for c in changes if c.status == "proposed"),
        rejected=sum(1 for c in changes if c.status == "rejected"),
        path=out_path,
        note=note,
        cycle=cycle,
    )


def _read_cycle_lines(path: Path) -> list[str]:
    """Existing revisions.jsonl lines (non-empty), or ``[]``.

    Unparseable lines are KEPT verbatim rather than dropped: this file is an
    audit log, and a reader that cannot understand a line still needs to see
    that something was there."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()]


def _append_cycle(
    path: Path, existing: list[str], cycle: RevisionCycle
) -> None:
    """Atomically rewrite revisions.jsonl with one more line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        json.loads(cycle.model_dump_json()), ensure_ascii=False,
    )
    content = "\n".join(existing + [payload]) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _refuse(
    run_date: _dt.date,
    mode: str,
    out_path: Path,
    reason: str,
    *,
    issue_sha256_before: str = "unknown",
    instruction: str = "",
) -> RevisionReport:
    """Record a refusal: the engine declined to act, and why.

    A refusal is written to ``revisions.jsonl`` as an empty cycle rather
    than passed over in silence. "The engine ran and refused" and "the
    engine was never invoked" are different states, and only the artifact
    can tell them apart after the fact.

    The operator instruction is recorded on a refusal too. A cycle that
    dropped it read as an unprompted no-op, which is what made the first
    live ``/revise`` run (2026-08-04) look like the flag had been lost when
    the engine had in fact refused a stale review.
    """
    _LOG.warning("revise: %s -- %s", run_date.isoformat(), reason)
    existing = _read_cycle_lines(out_path)
    cycle = RevisionCycle(
        date=run_date,
        cycle=len(existing) + 1,
        mode=mode,  # type: ignore[arg-type]
        changes=[],
        operator_instruction=instruction.strip()[:2000],
        generated_at=_dt.datetime.now(_dt.timezone.utc),
        prompt_version=REVISE_PROMPT_VERSION,
        issue_sha256_before=issue_sha256_before,
        issue_sha256_after=None,
        note=f"refused: {reason}"[:2000],
    )
    try:
        _append_cycle(out_path, existing, cycle)
    except Exception:  # noqa: BLE001
        _LOG.exception("revise: could not record the refusal in %s", out_path)
    return RevisionReport(
        date=run_date, mode=mode, ran=False, applied=0, proposed=0,
        rejected=0, path=out_path, note=reason, cycle=cycle,
    )


# ---------------------------------------------------------------------------
# CLI -- a typer-compatible command the Architect registers on `aiv`.
#
# REGISTRATION (src/run.py, alongside the other @app.command() functions --
# run.py is not this stage's file, so the line is stated rather than added):
#
#     from src.revise import revise_command
#     app.command(name="revise")(revise_command)
#
# Usage once registered:
#     aiv revise --date 2026-08-02 --shadow
#     aiv revise --date 2026-08-02 --live
#     aiv revise --date 2026-08-02 --live --min-severity major
#     aiv revise --date 2026-08-02 --live \
#         --instruction "drop the second sentence" \
#         --target story:c_abc123def456:summary
#
# ---------------------------------------------------------------------------
# SETTLED, AND WHAT IS STILL OPEN (the first two notes here were open
# questions until the first live `/revise` run on 2026-08-04 answered one of
# them the hard way).
#
# 1. WHICH COPY DOES `/revise` EDIT? -- SETTLED: the released copy.
#    `data/staging/` is gitignored, so a release-PR checkout in CI has ONLY
#    `data/released/<date>/`, and the workflow git-adds exactly that path.
#    `--released` therefore edits the canonical copy, and a live cycle there
#    re-renders `docs/released/<date>.html` + `docs/index.html` so the
#    committed page matches the committed JSON.
#    It does NOT bump `Issue.revision`. DESIGN.md's "Same-date re-release"
#    governs re-publishing an issue readers have seen; `/revise` runs only
#    on an OPEN release PR, where the issue is still a draft. Opening
#    `/revise` to merged PRs would make the revision bump mandatory -- an
#    Architect decision, not this stage's.
#
# 2. WHY THE RELEASED COPY NEEDS ITS OWN FRESHNESS BASIS.
#    `release_promote` stamps `issue_number` into the canonical issue.json,
#    so its bytes can never equal the staging bytes the reviewer hashed even
#    when every sentence is identical. A byte-hash freshness check there
#    refuses 100% of the time -- which is what the first live `/revise` run
#    did. `_assess_freshness` falls back to per-finding quote evidence on
#    the canonical copy only; staging keeps the exact hash check.
#
# 3. WHERE DOES AN UNTARGETED `--instruction` APPLY? -- STILL OPEN.
#    The workflow passes `--instruction` with no `--target`. This module
#    attaches such a directive to the fields the review's `text_edit`
#    findings already scheduled, and refuses when there are none, because
#    an instruction with no address is an edit with no address and code
#    should not guess. If the intended `/revise` experience is "say
#    anything and it fixes the right thing", the routing needs a ratified
#    rule -- most likely a small LLM classification of the instruction to a
#    target field, which is a new judgment call and therefore a new eval.
#
#    One large class of untargeted instruction is now answered without any
#    of that: "apply only the major recommendations" is a SELECTION rule,
#    and selection is `--min-severity major`. The workflow can parse that
#    class of comment into the flag deterministically.
# ---------------------------------------------------------------------------

def revise_command(
    date: Optional[str] = None,
    shadow: bool = typer.Option(
        True,
        "--shadow/--live",
        help="--shadow proposes only (default); --live applies accepted edits.",
    ),
    instruction: str = "",
    target: str = "",
    min_severity: str = typer.Option(
        MIN_SEVERITY_DEFAULT,
        "--min-severity",
        help=(
            "Only act on findings at or above this severity "
            "[note|minor|major|blocking]. Default note (acts on all)."
        ),
    ),
    released: bool = False,
    verbose: bool = False,
) -> None:
    """Apply the review's text-edit findings to a staged issue.

    Reads ``data/staging/<date>/review.json`` and rewrites only the fields
    its ``text_edit`` findings point at, one LLM call per field, with a
    code-side validation gate on every replacement.

    ``--shadow`` (the default) computes and records what it WOULD do in
    ``revisions.jsonl`` without touching ``issue.json``. ``--live`` applies
    the accepted changes and clears the fact-check verdict on every story it
    edited -- which means the issue must be re-reviewed before the
    unattended gate will pass it.

    ``--instruction`` supplies one extra directive (the ``/revise`` PR
    command), under the same containment as a finding. Pair it with
    ``--target story:<cluster_id>:<field>`` or ``--target
    section:<name>:<field>`` to say where it applies; without a target it
    rides along with the fields the findings already scheduled.

    ``--min-severity major`` acts only on the findings the reviewer rated
    major or blocking. It is a code filter applied before any prompt is
    built, which is why "apply only the major recommendations" belongs here
    and not in ``--instruction``: the field-scoped model cannot see the other
    findings, so it could not honour that sentence even if it tried.

    ``--released`` revises ``data/released/<date>/`` instead of the staging
    draft, and re-renders that day's released HTML plus the landing index.
    Present because staging is gitignored and a release-PR checkout has only
    the released copy.
    """
    import sys

    # Direct python calls (tests, other modules) bypass typer, so the
    # typer.Option sentinel arrives as the literal default -- normalise it.
    if isinstance(shadow, typer.models.OptionInfo):
        shadow = bool(shadow.default)
    if isinstance(min_severity, typer.models.OptionInfo):
        min_severity = str(min_severity.default)

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:  # dev convenience, not a runtime dependency
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    if date:
        try:
            run_date = _dt.date.fromisoformat(date.strip())
        except ValueError:
            print(f"--date must be YYYY-MM-DD; got {date!r}")
            sys.exit(2)
    else:
        run_date = _dt.date.today()

    # Strict at the CLI boundary, failure-soft inside the engine. A typo'd
    # floor means the operator asked for less than they get -- an unattended
    # `--live` run would then rewrite fields they meant to leave alone, so
    # the command stops rather than guesses.
    floor = (min_severity or "").strip().lower() or MIN_SEVERITY_DEFAULT
    if floor not in SEVERITY_RANK:
        print(
            f"--min-severity must be one of "
            f"{'|'.join(MIN_SEVERITY_CHOICES)}; got {min_severity!r}"
        )
        sys.exit(2)

    report = revise_day(
        run_date,
        shadow=shadow,
        instruction=instruction,
        instruction_target=target,
        canonical=released,
        min_severity=floor,
    )
    print(f"revise: {report.one_line} -> {report.path}")
    if report.cycle:
        for change in report.cycle.changes:
            marker = {
                "applied": "APPLIED ", "proposed": "PROPOSED",
                "rejected": "REJECTED",
            }[change.status]
            suffix = f" ({change.reject_reason})" if change.reject_reason else ""
            print(f"  [{marker}] {target_key(change.target)}{suffix}")
    sys.exit(0)


def _cli() -> int:
    """``python -m src.revise --date YYYY-MM-DD [--live]`` for manual runs
    before the command is registered on ``aiv``."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m src.revise")
    parser.add_argument("--date", default="", help="YYYY-MM-DD (default today)")
    parser.add_argument("--live", action="store_true",
                        help="Apply the changes (default is shadow).")
    parser.add_argument("--instruction", default="",
                        help="Extra operator directive.")
    parser.add_argument("--target", default="",
                        help="story:<cluster_id>:<field> | section:<name>:<field>")
    parser.add_argument("--min-severity", dest="min_severity",
                        default=MIN_SEVERITY_DEFAULT,
                        choices=list(MIN_SEVERITY_CHOICES),
                        help="Only act on findings at or above this severity.")
    parser.add_argument("--released", action="store_true",
                        help="Revise data/released/<date>/ instead of staging.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    try:
        revise_command(
            date=args.date or None,
            shadow=not args.live,
            instruction=args.instruction,
            target=args.target,
            min_severity=args.min_severity,
            released=args.released,
            verbose=args.verbose,
        )
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
