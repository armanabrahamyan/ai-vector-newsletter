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
touched. ``shadow=False`` applies the accepted changes atomically and
records ``applied`` / ``rejected``.

Applying an edit invalidates two things downstream, both handled here or
stated plainly:
  * the touched story's ``verification`` is set to ``None`` by CODE -- the
    fact-check was about text that no longer exists, and a stale clean
    verdict is worse than none;
  * the review's ``issue_sha256`` no longer matches the file, so
    ``src/gate.py`` will hold with ``hold:stale-review`` until the issue is
    re-reviewed. That is correct, not a bug: an edited issue has not been
    reviewed.

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
from pathlib import Path
from typing import Any, Optional

from src import llm_usage, paths
from src.models import (
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

REVISE_PROMPT_VERSION = "v0.1"
r"""Versioned prompt string recorded on every ``RevisionCycle`` so a bad
edit can be attributed to the prompt that produced it.

Bump on any prompt-content change. Audit tag: ``revise-v0.1-2026-08-02``.

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
    """``{field: (min_length, max_length)}`` for every revisable field.

    Falls back to the values documented in DESIGN.md when introspection
    finds nothing, and logs -- a stage that silently dropped its length
    checks would start writing fields pydantic then refuses, turning a
    clean rejection into a corrupted issue.
    """
    fallback = {
        "headline": (1, 200),
        "summary": (1, 1200),
        "intro_lead": (1, 80),
        "intro_body": (1, 400),
    }
    out: dict[str, tuple[int, int]] = {}
    for name, model in (
        ("headline", SummaryBlock),
        ("summary", SummaryBlock),
        ("intro_lead", IssueSection),
        ("intro_body", IssueSection),
    ):
        bounds = _bounds_from_model(model, name)
        if bounds is None:
            _LOG.warning(
                "revise: could not read length bounds for %s from the model; "
                "falling back to %s", name, fallback[name],
            )
            out[name] = fallback[name]
        else:
            # Intro fields are Optional in the model, so their declared
            # minimum is 0; a replacement that empties one is a deletion,
            # not a revision, so we hold every field to at least 1.
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
) -> RevisionReport:
    """Apply the review's ``text_edit`` findings to one day's staged issue.

    Flow
    ----
    1. Read ``review.json`` and the staged ``issue.json``.
    2. REFUSE unless the review's ``issue_sha256`` still matches the issue
       on disk. A review of a superseded draft describes text that is no
       longer there; acting on it would edit the wrong sentences.
    3. Group the ``text_edit`` findings by target field.
    4. One LLM call per field: current text + directives in, replacement
       text out.
    5. Validate each replacement in code. Pass -> ``proposed`` (shadow) or
       ``applied`` (live). Fail -> ``rejected``, field untouched.
    6. Write the cycle to ``revisions.jsonl``; in live mode also rewrite
       ``issue.json`` atomically, clearing ``verification`` on every story
       whose text changed.

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
        in CI carries only the released copy. Editing a released issue is a
        different act from editing a draft -- DESIGN.md's "Issue Number
        Registry -> Same-date re-release" says a corrected re-publication
        bumps ``revision`` and re-renders -- and NEITHER of those happens
        here. This parameter is the seam, not the policy: whether the
        ``/revise`` command should use it, and what else must run alongside
        it, is an Architect + ratification decision. See the hand-off note
        at module end.
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
        )

    issue_path = paths.issue_path(run_date, canonical=canonical)
    try:
        issue_bytes = issue_path.read_bytes()
        issue_payload = json.loads(issue_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _refuse(
            run_date, mode, out_path,
            f"could not read staged issue.json at {issue_path}: {exc}",
        )
    if not isinstance(issue_payload, dict):
        return _refuse(
            run_date, mode, out_path, "staged issue.json is not a JSON object",
        )

    import hashlib

    sha_before = hashlib.sha256(issue_bytes).hexdigest()
    if report.issue_sha256 != sha_before:
        return _refuse(
            run_date, mode, out_path,
            "the review is stale: it was written against issue.json "
            f"{report.issue_sha256[:12]}..., the file on disk is "
            f"{sha_before[:12]}.... Re-run `aiv review --date "
            f"{run_date.isoformat()}` before revising.",
            issue_sha256_before=sha_before,
        )

    groups = _build_groups(
        report, issue_payload, instruction, instruction_target,
    )
    if not groups:
        return _refuse(
            run_date, mode, out_path,
            "nothing to revise: no text_edit findings"
            + (
                " and the operator instruction named no target"
                if instruction else ""
            ),
            issue_sha256_before=sha_before,
        )

    changes: list[RevisionChange] = []
    for group in groups:
        changes.append(_revise_one_field(group, shadow=shadow))

    sha_after: str | None = None
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
    return _write_cycle(
        run_date, mode, out_path, changes, report,
        instruction=instruction,
        sha_before=sha_before, sha_after=sha_after, note=note,
    )


# ---------------------------------------------------------------------------
# Grouping.
# ---------------------------------------------------------------------------

def _build_groups(
    report: ReviewReport,
    issue_payload: dict[str, Any],
    instruction: str,
    instruction_target: str,
) -> list[_FieldGroup]:
    """Group the actionable findings by target field, then attach any
    operator directive.

    Only ``fix_kind == "text_edit"`` findings are collected; everything else
    in the review is somebody's judgement call, not a rewrite. A finding
    whose target no longer resolves to text is skipped (the freshness check
    makes that near-impossible, but "near" is not "never").
    """
    field_texts = index_issue_fields(issue_payload)
    groups: dict[str, _FieldGroup] = {}

    for finding in report.findings:
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
    """Parse ``story:<cluster_id>:<field>`` / ``section:<name>:<field>``.

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
            "revise: --target %r is not 'story:<id>:<field>' or "
            "'section:<name>:<field>'", selector,
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
    except Exception as exc:  # noqa: BLE001 -- a bad selector is user input
        _LOG.warning("revise: --target %r is not a valid target: %s",
                     selector, exc)
        return None
    _LOG.warning("revise: --target %r has unknown kind %r", selector, kind)
    return None


# ---------------------------------------------------------------------------
# The per-field revision.
# ---------------------------------------------------------------------------

def _revise_one_field(group: _FieldGroup, *, shadow: bool) -> RevisionChange:
    """Produce (and validate) one field's replacement text.

    One LLM call, one field. On a length failure we retry once quoting the
    exact constraint -- that is the one failure a model can fix when told
    the number. Every other rejection is final for this cycle.
    """
    recommendation = " | ".join(group.instructions)[:2000]
    field_name = group.target.field
    minimum, maximum = FIELD_BOUNDS[field_name]

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
    "intro_lead": (
        "This is a section INTRO LEAD -- a short bold phrase, a handful of "
        "words. It frames the pattern across the section's stories."
    ),
    "intro_body": (
        "This is a section INTRO BODY -- one or two sentences framing the "
        "day's pattern in this section."
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
    ``verify._rewrite_issue_with_verification`` uses, so a reader never sees
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
        else:
            section = sections.get(target.section or "")
            if section is None:
                continue
            section[target.field] = change.after

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
) -> RevisionReport:
    """Record a refusal: the engine declined to act, and why.

    A refusal is written to ``revisions.jsonl`` as an empty cycle rather
    than passed over in silence. "The engine ran and refused" and "the
    engine was never invoked" are different states, and only the artifact
    can tell them apart after the fact.
    """
    _LOG.warning("revise: %s -- %s", run_date.isoformat(), reason)
    existing = _read_cycle_lines(out_path)
    cycle = RevisionCycle(
        date=run_date,
        cycle=len(existing) + 1,
        mode=mode,  # type: ignore[arg-type]
        changes=[],
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
#     aiv revise --date 2026-08-02 --live \
#         --instruction "drop the second sentence" \
#         --target story:c_abc123def456:summary
#
# ---------------------------------------------------------------------------
# TWO OPEN QUESTIONS FOR THE ARCHITECT (surfaced by reading
# .github/workflows/revise-command.yml, which was written in parallel with
# this module). Both are contract decisions, so neither is decided here.
#
# 1. WHICH COPY DOES `/revise` EDIT?
#    `revise_day` defaults to `data/staging/<date>/`, which is what this
#    stage is for. But `data/staging/` is gitignored, so a release-PR
#    branch checked out in CI has ONLY `data/released/<date>/` -- and the
#    workflow's commit step git-adds exactly that path. As written, the
#    workflow would hit "no readable review.json" and refuse.
#    The `canonical=True` / `--released` seam exists so the wiring is a
#    one-word change, but pointing it there is not free: DESIGN.md's
#    "Same-date re-release" says a corrected re-publication bumps
#    `Issue.revision` and re-renders, and this stage does neither. Either
#    the workflow keeps the staging artifacts alive (upload/restore, or an
#    `aiv run --stages review` to rebuild them), or `/revise` becomes a
#    released-copy edit that runs alongside the revision-bump path.
#
# 2. WHERE DOES AN UNTARGETED `--instruction` APPLY?
#    The workflow passes `--instruction` with no `--target`. This module
#    attaches such a directive to the fields the review's `text_edit`
#    findings already scheduled, and refuses when there are none, because
#    an instruction with no address is an edit with no address and code
#    should not guess. If the intended `/revise` experience is "say
#    anything and it fixes the right thing", the routing needs a ratified
#    rule -- most likely a small LLM classification of the instruction to a
#    target field, which is a new judgment call and therefore a new eval.
# ---------------------------------------------------------------------------

def revise_command(
    date: Optional[str] = None,
    shadow: bool = True,
    instruction: str = "",
    target: str = "",
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

    ``--released`` revises ``data/released/<date>/`` instead of the staging
    draft. Present because staging is gitignored and a release-PR checkout
    has only the released copy -- see ``revise_day``'s docstring for why
    that is a seam awaiting a ratified policy, not a supported workflow.
    """
    import sys

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

    report = revise_day(
        run_date,
        shadow=shadow,
        instruction=instruction,
        instruction_target=target,
        canonical=released,
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
            released=args.released,
            verbose=args.verbose,
        )
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
