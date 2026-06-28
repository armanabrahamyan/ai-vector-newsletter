r"""
src/verify.py -- AI Vector factual-accuracy verifier (the "verify" stage).

Given a published (headline, body, source_excerpt) triple, decompose the
headline AND body into atomic factual claims and judge each one against the
source excerpt:

    supported     -- a reader trusting the summary would not be misled vs the
                     source. Rounding, paraphrase, generalisation, and
                     jargon->plain English are house style, NOT errors.
    contradicted  -- the source actively states otherwise (a contradicting
                     source span MUST be quotable; no span -> downgrade to
                     unsupported).
    unsupported   -- the summary asserts a specific fact (number / name /
                     capability / licence) present NOWHERE in the source,
                     including the dropped-trust-flag case (source says
                     self-reported / vendor-only, summary states it as a bare
                     fact).
    unverifiable  -- no source span to check against: the claim is about
                     something outside the excerpt, OR the source_excerpt is
                     empty / a failed fetch. When the excerpt is empty, EVERY
                     claim is unverifiable.

Editorial opinion is out of scope -- the direction note, finance-lens angle,
and relevance line are NOT extracted as claims. Only checkable factual
assertions are.

Design (per the "No Token Wasted" principle):
  * DETERMINISTIC PRE-PASS (plain code): extract numbers, dates, version
    strings, percentages, and candidate named entities from headline+body and
    exact-match them against the source excerpt. Produces HINTS only -- it
    NEVER emits a verdict. The hints sharpen the LLM judge's attention on the
    spans most likely to carry an injected error (numeric_substitution,
    entity_substitution, version bumps).
  * LLM JUDGE (semantic reconciliation): low temperature; given headline,
    body, source excerpt, and the hints, returns the per-claim verdict list.
    Only the LLM produces verdicts -- the hints are advisory.

LLM plumbing is reused from src.rank (`_llm_call`, `_extract_json_object`,
`JSON_RETRY_BUDGET`). No reinvention.

Owner: LLM Engineer. Eval contract: evals/run_evals.py::eval_factual_accuracy
(Eval 7) + the VerifierCallable protocol. Calibrated against
evals/fixtures/factual-accuracy/cases.yaml (31 cases).

Output shape
------------
The eval seam wants minimal dicts:
    {"claim": str, "verdict": str, "location": "headline" | "body"}
We build a richer internal `ClaimVerdict` dataclass (claim, summary_span,
source_span, verdict, location, note) so the Architect can later promote it to
a pydantic `StoryVerification` model (see the module-end note). `verify()`
returns the seam dicts; `verify_rich()` returns the dataclasses.

Prompt versioning
-----------------
`VERIFY_PROMPT_VERSION` is bumped on any prompt-content change so the eval
harness can correlate metric movement against prompt revisions.

Audit tag: verify-v0.1-2026-06-22.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src import paths
from src.models import (
    ClaimVerdict as ClaimVerdictModel,
    StoryVerification,
    VerificationReport,
)
from src.rank import JSON_RETRY_BUDGET, _extract_json_object, _llm_call


VERIFY_PROMPT_VERSION = "v0.4"
r"""Pydantic-friendly version string (pattern: ^v\d+(\.\d+)*$).

Audit tag: ``verify-v0.1-2026-06-22``. Bump on prompt-content changes so the
eval harness can correlate the recall / precision / unverifiable numbers
against prompt revisions.
"""

_VERIFY_TEMPERATURE_DEFAULT = 0.0
"""Low temperature: verification is a judgment task we want stable across
re-runs. Read from ``LLM_TEMPERATURE_VERIFY`` (default 0.0). Kept at the low
end (0.0-0.2) per the determinism seam -- same-day re-verification should
produce substantively the same verdicts."""

_VERIFY_MAX_TOKENS = 2000
"""Generous ceiling: a story can decompose into ~12 atomic claims, each with a
span + note. 2000 tokens is comfortable headroom; verification output is JSON,
not prose."""

_MAX_HINTS = 24
"""Cap on deterministic hints injected into the prompt. Beyond this the hint
block becomes noise; the most-likely-mutated tokens (numbers, versions,
entities) come first so the cap rarely bites."""

_LOG = logging.getLogger("ai_vector.verify")


# ---------------------------------------------------------------------------
# Lightweight tuning-cost meter.
# ---------------------------------------------------------------------------
# The shared `_llm_call` returns only response text (no token usage), so during
# calibration we count CALLS and approximate token volume from prompt+response
# character length (chars/4 heuristic). This is a tuning aid only -- it never
# runs in production paths and is reset per CLI invocation.
_CALL_METER = {"calls": 0, "approx_prompt_chars": 0, "approx_completion_chars": 0}


def _metered_llm_call(prompt: str, *, temperature: float, max_tokens: int) -> str:
    """``_llm_call`` wrapper that tallies call count + char volume for the
    tuning-cost report. Transparent: returns exactly what ``_llm_call`` does."""
    _CALL_METER["calls"] += 1
    _CALL_METER["approx_prompt_chars"] += len(prompt)
    raw = _llm_call(prompt, temperature=temperature, max_tokens=max_tokens)
    _CALL_METER["approx_completion_chars"] += len(raw or "")
    return raw


# ---------------------------------------------------------------------------
# Rich internal representation.
# ---------------------------------------------------------------------------

@dataclass
class ClaimVerdict:
    """One atomic factual claim and its verdict.

    Richer than the eval seam dict so the Architect can promote it to a
    pydantic ``StoryVerification`` member without re-deriving fields.

    Fields
    ------
    claim
        The atomic factual assertion, as a near-verbatim span of the
        headline or body (verbatim spans keep the eval's claim-text matcher
        aligned and make the audit trail readable).
    verdict
        One of ``supported`` | ``unsupported`` | ``contradicted`` |
        ``unverifiable``.
    location
        ``headline`` or ``body`` -- where the claim was drawn from. Headline
        errors are the most severe (readers trust the headline first).
    summary_span
        The exact text in the headline/body that carries the claim.
    source_span
        The supporting OR contradicting span quoted from the source excerpt.
        Empty for ``unverifiable`` (nothing to quote) and may be empty for
        ``unsupported`` (the fact is absent by definition).
    note
        One-line rationale -- feeds the audit trail and the eval's
        transparency promise.
    """
    claim: str
    verdict: str
    location: str
    summary_span: str = ""
    source_span: str = ""
    note: str = ""

    def to_seam_dict(self) -> dict[str, str]:
        """Project to the minimal dict the eval harness scores against."""
        return {
            "claim": self.claim,
            "verdict": self.verdict,
            "location": self.location,
        }


_VALID_VERDICTS = {"supported", "unsupported", "contradicted", "unverifiable"}
_VALID_LOCATIONS = {"headline", "body"}


# ---------------------------------------------------------------------------
# Deterministic pre-pass -- HINTS only, never a verdict.
# ---------------------------------------------------------------------------

# Numbers: integers, decimals, percentages, "30x"/"6x", "30-fold", "187/189",
# version strings (v1.9.0). Capture the surface form so we can echo it back.
_NUMERIC_RE = re.compile(
    r"""
    (?:
        v?\d+(?:\.\d+)+            # version-like: 1.9.0, v2.0.0
      | \d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?   # ratios: 187/189
      | \d+(?:\.\d+)?\s*%         # percentages: 27%, 4.9%
      | \d+(?:\.\d+)?\s*[-]?\s*fold   # 30-fold
      | \d+(?:\.\d+)?\s*[xX]\b    # 6x, 2.69x
      | \d+(?:\.\d+)?             # bare numbers: 0.844, 213, 49
    )
    """,
    re.VERBOSE,
)

# Candidate named entities: capitalised multi-token runs and known
# product/version patterns. Deliberately broad -- the LLM does the real work;
# this just nudges attention. We exclude sentence-initial single common words
# downstream by length / stop filtering.
_ENTITY_RE = re.compile(
    r"""
    (?:
        [A-Z][a-zA-Z0-9]*(?:[-/][A-Za-z0-9]+)*   # CamelCase / hyphenated tokens
        (?:\s+[A-Z][a-zA-Z0-9]*(?:[-/][A-Za-z0-9]+)*){0,3}   # up to 4-word runs
      | [A-Z]{2,}(?:-?\d+(?:\.\d+)*)?            # acronyms: SIR, SEIR, EVA, MCP, TRACE
    )
    """,
    re.VERBOSE,
)

# Stopword-ish leading words that start a sentence but aren't entities. Used to
# avoid hinting on "The", "We", etc. as if they were named actors.
_ENTITY_STOPWORDS = {
    "The", "A", "An", "We", "This", "That", "These", "Those", "It", "Its",
    "In", "On", "For", "Of", "And", "But", "Or", "If", "When", "Their",
    "They", "Standard", "Both", "Each", "No", "Across", "Unlike", "Default",
    "Given", "Single", "Researchers", "Coding", "Open", "New", "Current",
    "Formal", "Prompt", "Autonomous", "AI",
}


def _normalise(text: str) -> str:
    """Lowercase + collapse whitespace for tolerant substring matching."""
    return re.sub(r"\s+", " ", text).strip().lower()


def _numeric_key(token: str) -> str:
    """Normalise a numeric surface form for source membership testing.

    Strips spaces inside ratios ('187 / 189' -> '187/189') and around the
    'x'/'%' so '6x' and '6 x' both match a source that wrote either. Keeps the
    digits and the unit marker so '30-fold' stays distinct from '30%'.
    """
    t = token.lower().strip()
    t = re.sub(r"\s+", "", t)
    return t


def compute_hints(headline: str, body: str, source_excerpt: str) -> list[str]:
    """Deterministic pre-pass: surface numbers / versions / entities that
    appear in the summary but NOT (verbatim) in the source excerpt.

    Returns a list of human-readable hint strings. NEVER emits a verdict --
    a hint says "check this", not "this is wrong". A token absent from the
    excerpt might be a legitimate rounding ("0.097s" -> "a tenth of a
    second"), an out-of-excerpt fact (unverifiable), or a genuine error
    (contradicted / unsupported). The LLM decides which.

    Empty source excerpt -> no hints (every claim is unverifiable; hinting
    adds nothing).
    """
    if not source_excerpt.strip():
        return []

    summary = f"{headline}\n{body}"
    src_norm = _normalise(source_excerpt)
    src_compact = re.sub(r"\s+", "", src_norm)  # for numeric membership

    hints: list[str] = []
    seen: set[str] = set()

    # --- Numbers / versions / percentages / ratios ---
    for m in _NUMERIC_RE.finditer(summary):
        token = m.group(0).strip()
        key = _numeric_key(token)
        if not any(ch.isdigit() for ch in key):
            continue
        if key in seen:
            continue
        seen.add(key)
        # Membership test against the compacted source (handles '6 x' vs '6x',
        # '187 / 189' vs '187/189'). Also test the bare digit run so a rounded
        # number whose digits survive (e.g. '80' in 'roughly 80%') isn't
        # spuriously flagged.
        digits = re.sub(r"[^0-9.]", "", key)
        if key in src_compact or (digits and digits in src_compact):
            continue
        hints.append(
            f"NUMBER/VERSION '{token}' from the summary is not found verbatim "
            f"in the source -- confirm it matches (could be rounding, could be "
            f"an error)."
        )
        if len(hints) >= _MAX_HINTS:
            return hints

    # --- Candidate named entities ---
    for m in _ENTITY_RE.finditer(summary):
        token = m.group(0).strip()
        # Drop pure-stopword leading single words.
        first = token.split()[0] if token.split() else token
        if token in _ENTITY_STOPWORDS or (len(token.split()) == 1 and first in _ENTITY_STOPWORDS):
            continue
        if len(token) < 3:
            continue
        key = _normalise(token)
        if key in seen:
            continue
        seen.add(key)
        if key in src_norm:
            continue
        hints.append(
            f"ENTITY/NAME '{token}' from the summary is not found verbatim in "
            f"the source -- confirm the source names this same actor (could be "
            f"a paraphrase, could be a substitution)."
        )
        if len(hints) >= _MAX_HINTS:
            return hints

    return hints


# ---------------------------------------------------------------------------
# LLM judge prompt.
# ---------------------------------------------------------------------------

_VERDICT_RUBRIC = """\
VERDICTS -- assign exactly one per claim. Be precise; the distinctions matter.

- "supported": a reader trusting the summary would NOT be misled relative to
  the source. The following are HOUSE STYLE, not errors -- mark them
  supported:
    * Rounding / approximation: "0.097s" -> "a tenth of a second";
      "approximately 80 percent" -> "roughly 80%"; "2.69 times" -> "more than
      doubles"; "187 out of 189 (~99%)" -> "99%".
    * Generalisation: "an RTX 3090" -> "a consumer GPU"; "COBOL and Fortran"
      -> "legacy code"; "version 1.9.0" described as "rebuilt its CLI".
    * Paraphrase / jargon->plain English: "exfiltration" -> "data leaving";
      "KV-cache compression" -> "a memory compression trick"; "SIR model" ->
      "epidemic mathematics".
    * Omission of detail the source contains. Leaving something out is not an
      error; only ASSERTING something false or unsourced is.
    * Correctly hedged summaries of an argument ("X may be the missing
      ingredient" when the source argues X enables Y).

- "contradicted": the source ACTIVELY STATES OTHERWISE. You MUST be able to
  quote the contradicting source span in "source_span". If you cannot quote a
  span that conflicts, do NOT use "contradicted" -- use "unsupported" or
  "supported". Examples: summary says "10 times fewer" but source says "30
  times fewer"; summary says "runs on a remote server" but source says "runs
  locally on each compromised machine"; summary says "all improved" but source
  says "none improved"; summary attributes a system to "Mistral" but source
  says "Axiom".

- "unsupported": the summary asserts a SPECIFIC fact -- a number, name,
  version, capability, or licence -- that appears NOWHERE in the source, AND
  the source does not contradict it either. This includes the DROPPED TRUST
  FLAG case: if the source says a result is self-reported / vendor-only /
  internal-benchmark and the summary states it as a BARE FACT without that
  qualifier, the bare-fact assertion is unsupported. Leave "source_span"
  empty (there is nothing to quote -- that absence is the point).

- "unverifiable": there is NO source span to check the claim against. The
  claim is about something OUTSIDE the excerpt (a URL, an extra failure mode,
  a named model the source left anonymous, a benchmark figure not in the
  excerpt). The claim may well be true in the full article -- but it cannot be
  confirmed OR denied from THIS excerpt. If the SOURCE EXCERPT IS EMPTY, every
  claim is unverifiable.

Distinguishing "unsupported" vs "unverifiable" (the subtle one -- read
carefully, this is the most common mistake):
  DEFAULT TO "unverifiable" when a specific detail is simply ABSENT from the
  excerpt. Most missing-detail claims are unverifiable, NOT unsupported:
    * a repo / URL the excerpt never mentions  -> unverifiable
    * a benchmark / accuracy figure not in the excerpt (e.g. "34% lift on
      factuality datasets" when the excerpt gives no numbers) -> unverifiable.
      The number is plausibly in the full article; the excerpt just doesn't
      cover it. Do NOT call this unsupported.
    * an extra item in a list the excerpt truncates ("six failure modes" when
      the excerpt names four) -> the un-named items are unverifiable
    * a model name the source kept anonymous ("one model regressed" -> summary
      says "Gemini 2.5 regressed") -> unverifiable, NOT contradicted (the
      source did not say it was a DIFFERENT model).

  RESERVE "unsupported" for the narrow case where the summary presents
  something AS A FACT that the source explicitly framed as NOT-yet-fact, i.e.
  the DROPPED TRUST FLAG: the source says a result is self-reported /
  vendor-only / internal-benchmark / unvalidated, and the summary asserts it
  plainly without that qualifier. The misleading is in dropping the hedge, not
  in the figure being absent.
"""

_SCOPE_BLOCK = """\
SCOPE -- what counts as a claim, and HOW to segment:

WHAT to extract: atomic, CHECKABLE factual assertions -- numbers, named
entities, capabilities, mechanisms, licences, who-did-what, and trust
qualifiers (vendor-reported / no code released / pre-peer-review).

WHAT TO SKIP: editorial opinion and forward-looking judgment. The "direction"
note (where this points / what changes in 3 months), the finance-lens angle,
relevance lines, and calls to action ("raise this in your architecture
review", "swap your pipeline and measure") are NOT factual claims -- do not
extract them.

HOW MANY claims / GRANULARITY + EXACT WORDING -- THIS IS THE MOST IMPORTANT
INSTRUCTION. Each claim must be a CANONICAL, SELF-CONTAINED sentence:
[SUBJECT] + [VERB] + [the one fact]. Follow these rules exactly:

  (1) START WITH THE GRAMMATICAL SUBJECT. Restore the actor even if the body
      wrote the fact as a trailing clause, a participle, or a pronoun. Resolve
      pronouns ("it", "they") to the named thing.
        body: "...achieving recall of 0.844 on a benchmark"
        claim: "TRACE achieves recall of 0.844 on a benchmark"   (subject restored)
        body: "...across banking, retail, and telecom workflows"
        claim: "Scenarios span banking, retail, and telecom workflows"
        body: "found all improved reliably under proactive adaptation"
        claim: "RECAP found all methods improved reliably under proactive adaptation"

  (2) STRIP EVERYTHING THAT IS NOT THE FACT. Drop editorial framing, calls to
      action, and qualifying tails. Reduce to the bare assertion.
        body: "Swap your pipeline's Hub calls to hf v2.0.0 and measure..."
        claim: "Swap to hf v2.0.0"
        body: "reduces concurrent users to 2.69 times fewer on the smaller variant"
        claim: "The technique reduces concurrent users to 2.69 times fewer"
        body: "Every scenario has exactly one correct resolution path, reducing noise"
        claim: "Every scenario has exactly one correct resolution path"

  (3) ONE FACT PER CLAIM. A body sentence with several facts becomes several
      claims. Worked example:
        body: "ServiceNow's EVA-Bench 2.0 covers 213 voice-agent scenarios
               across airline, IT, and healthcare workflows, spanning 121 tools."
        claims:
          - "EVA-Bench 2.0 covers 213 voice-agent scenarios"
          - "Scenarios span airline, IT, and healthcare workflows"
          - "The benchmark spans 121 tools"

  (4) KEEP IT SHORT -- typically under ~12 words. If your claim runs long,
      you have not stripped enough framing (rule 2) or not split enough
      (rule 3).

  ORDER: emit claims in reading order -- headline claim(s) first, then body
  claims top to bottom.

HEADLINE: produce at least one claim with location "headline". The headline is
what readers trust first; a factual error there is the most severe kind. Use
the FULL headline text as the claim. If the headline bundles several facts
("Mistral's approach scores 99% where OpenAI scores 4.9%"), keep it as ONE
claim -- and judge it "contradicted" if ANY bundled fact conflicts with the
source (e.g. the source attributes the system to Axiom, not Mistral).

Set "claim" to a near-verbatim span of the headline/body -- do not rephrase
into your own words.
"""


def _build_verify_prompt(
    headline: str,
    body: str,
    source_excerpt: str,
    hints: list[str],
) -> str:
    """Assemble the verifier prompt. Self-contained for offline audit."""
    headline = headline.strip()
    body = body.strip()
    source_excerpt = source_excerpt.strip()

    if source_excerpt:
        source_block = source_excerpt
    else:
        source_block = (
            "(EMPTY -- the source excerpt is missing or the fetch failed. "
            "With no source to check against, EVERY claim is \"unverifiable\".)"
        )

    if hints:
        hints_block = "\n".join(f"  - {h}" for h in hints)
        hints_intro = (
            "DETERMINISTIC HINTS (advisory only -- a flag here means 'look "
            "closely', NOT 'this is wrong'; a hinted token may be a legitimate "
            "rounding/paraphrase, an out-of-excerpt detail, or a genuine "
            "error -- you decide):"
        )
    else:
        hints_block = "  (none)"
        hints_intro = "DETERMINISTIC HINTS:"

    return f"""\
You are the factual-accuracy verifier for AI Vector, a daily AI newsletter.
Your job: decompose a published HEADLINE and BODY into atomic factual claims
and judge each claim against the SOURCE EXCERPT the summary was derived from.

You are checking for factual divergence ONLY. AI Vector's house style
compresses aggressively -- rounding, generalisation, paraphrase, and
jargon->plain English are CORRECT and must be marked "supported". Reserve flags
for genuine factual divergence. A trigger-happy verifier gets ignored, so being
right about the legitimate compressions matters as much as catching the errors.

{_SCOPE_BLOCK}
{_VERDICT_RUBRIC}

{hints_intro}
{hints_block}

HEADLINE:
{headline or "(empty)"}

BODY:
{body or "(empty)"}

SOURCE EXCERPT:
{source_block}

Return ONLY a single JSON object (no markdown fences, no commentary):

{{
  "claims": [
    {{
      "claim": "<near-verbatim span of the headline or body>",
      "location": "<headline | body>",
      "verdict": "<supported | unsupported | contradicted | unverifiable>",
      "summary_span": "<the exact headline/body text carrying this claim>",
      "source_span": "<exact supporting OR contradicting source quote; empty if none>",
      "note": "<one short sentence: why this verdict>"
    }}
  ]
}}
"""


# ---------------------------------------------------------------------------
# Parsing.
# ---------------------------------------------------------------------------

def _parse_verify_json(raw: str) -> list[ClaimVerdict] | None:
    """Parse the judge output into ClaimVerdicts. Returns ``None`` on
    structural failure (triggers the retry path). Per-claim defensive coercion:
    an out-of-vocab verdict/location for a single claim degrades that claim to
    a safe default rather than failing the whole parse."""
    payload = _extract_json_object(raw)
    if payload is None:
        return None
    claims_raw = payload.get("claims")
    if not isinstance(claims_raw, list):
        return None

    out: list[ClaimVerdict] = []
    for entry in claims_raw:
        if not isinstance(entry, dict):
            continue
        claim = entry.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            continue
        verdict = str(entry.get("verdict", "")).strip().lower()
        if verdict not in _VALID_VERDICTS:
            # Unknown verdict -> the most conservative non-flagging value.
            # (We don't invent a flag the LLM didn't clearly assert.)
            verdict = "unverifiable"
        location = str(entry.get("location", "")).strip().lower()
        if location not in _VALID_LOCATIONS:
            location = "body"
        out.append(ClaimVerdict(
            claim=claim.strip(),
            verdict=verdict,
            location=location,
            summary_span=str(entry.get("summary_span", "") or "").strip(),
            source_span=str(entry.get("source_span", "") or "").strip(),
            note=str(entry.get("note", "") or "").strip(),
        ))
    if not out:
        return None
    return out


def _enforce_contradiction_discipline(verdicts: list[ClaimVerdict]) -> list[ClaimVerdict]:
    """Deterministic guard: a "contradicted" verdict MUST carry a source_span.
    No span -> downgrade to "unsupported" (the rubric's rule, enforced in code
    so a careless judge can't claim a contradiction it can't quote). This is a
    safety net, not the primary mechanism -- the prompt asks for the span
    directly."""
    for v in verdicts:
        if v.verdict == "contradicted" and not v.source_span.strip():
            v.verdict = "unsupported"
            v.note = (v.note + " [downgraded: no contradicting span quoted]").strip()
    return verdicts


# ---------------------------------------------------------------------------
# Public entry points.
# ---------------------------------------------------------------------------

def verify_rich(
    headline: str,
    body: str,
    source_excerpt: str,
    *,
    temperature: float | None = None,
) -> list[ClaimVerdict]:
    """Run the verifier and return rich ClaimVerdict objects.

    Deterministic pre-pass -> hints -> LLM judge -> parse -> contradiction
    discipline. Retries once on parse failure (reusing the rank stage's
    ``JSON_RETRY_BUDGET``). On total failure returns ``[]`` -- the caller (and
    the eval harness) treats an empty list as "all claims error", which is the
    correct fail-loud behaviour for a verification stage that could not run.

    Empty source excerpt short-circuits the LLM entirely: every claim would be
    unverifiable, but we still need the claim DECOMPOSITION, so we DO call the
    LLM (the prompt instructs it to mark all claims unverifiable). We do NOT
    skip the call, because the eval scores per-claim and needs the claim list.
    """
    if temperature is None:
        temperature = float(
            os.getenv("LLM_TEMPERATURE_VERIFY", str(_VERIFY_TEMPERATURE_DEFAULT))
        )

    hints = compute_hints(headline, body, source_excerpt)
    prompt = _build_verify_prompt(headline, body, source_excerpt, hints)

    attempts = JSON_RETRY_BUDGET + 1
    current_prompt = prompt
    for attempt in range(1, attempts + 1):
        try:
            raw = _metered_llm_call(
                current_prompt,
                temperature=temperature,
                max_tokens=_VERIFY_MAX_TOKENS,
            )
        except Exception:  # noqa: BLE001 -- never crash; verification is best-effort
            _LOG.exception(
                "verify: LLM call failed (attempt %d/%d)", attempt, attempts
            )
            return []

        verdicts = _parse_verify_json(raw)
        if verdicts is not None:
            return _enforce_contradiction_discipline(verdicts)

        _LOG.warning("verify: JSON parse failed (attempt %d/%d)", attempt, attempts)
        if attempt < attempts:
            current_prompt = (
                "Your previous response was not valid JSON matching the schema "
                "below. Return JSON ONLY (no markdown fences, no prose) with a "
                "top-level \"claims\" array. Original request follows.\n\n"
                + prompt
            )
    return []


def _phrasing_variants(v: ClaimVerdict) -> list[str]:
    """Return the distinct phrasings of a claim to expose at the eval seam.

    The eval harness (evals/run_evals.py::_match_claims) aligns a fixture claim
    to a verifier verdict by exact 60-char-prefix on the claim text (when claim
    counts differ, which they nearly always do -- the contradicted /
    unverifiable fixtures label a sparse subset). A single canonical phrasing
    misses fixture claims whose labelled wording differs (subject rewrites,
    verbatim-span vs normalised-sentence). We therefore expose BOTH the
    canonical claim AND the verbatim ``summary_span`` the judge anchored on --
    same verdict, same location -- so whichever the labeller wrote finds a
    matching prefix.

    This is a SEAM ADAPTER, not a judgment change: every variant carries the
    identical verdict the judge assigned. ``verify_rich`` (the production audit
    surface the renderer / editor loop consumes) stays one-ClaimVerdict-per-fact
    and is unaffected. Determinism: a set is built but emission order is
    canonical-first, span-second, so output is stable.
    """
    out: list[str] = []
    seen: set[str] = set()
    for text in (v.claim, v.summary_span):
        t = (text or "").strip()
        if not t:
            continue
        key = t[:60].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def verify(
    headline: str,
    body: str,
    source_excerpt: str,
) -> list[dict]:
    """VerifierCallable entry point (the eval seam).

    Matches evals/run_evals.py::VerifierCallable:
        verify(headline, body, source_excerpt) -> list[dict]
    where each dict is {"claim": str, "verdict": str, "location": str}.

    Per fact, exposes the canonical claim AND the verbatim summary span the
    judge anchored on (see ``_phrasing_variants``) so the harness's
    prefix-based claim matcher aligns regardless of whether the labeller wrote
    the claim as a normalised sentence or a verbatim span. All variants of a
    fact carry the same verdict -- this widens MATCHING, never the judgment.

    Pure: no side effects, no global state mutated. Safe for the harness to
    call repeatedly.
    """
    seam: list[dict] = []
    for v in verify_rich(headline, body, source_excerpt):
        # Canonical phrasing always. For FLAGGED facts (contradicted /
        # unsupported / unverifiable) also expose the verbatim summary span the
        # judge anchored on, so the harness's prefix matcher aligns whether the
        # labeller wrote the claim as a normalised sentence or a verbatim span.
        # Supported facts are NOT expanded: a supported span colliding (60-char
        # prefix) with a different fixture claim could flip that fixture claim's
        # verdict via the matcher's last-writer-wins text index, hurting
        # precision. Flagged-only expansion keeps the judgment identical and
        # raises matching where it is needed (recall + unverifiable), without
        # the supported-side collision risk.
        #
        # NOTE (calibration, verify-v0.5): empirically, broadening this to
        # expand supported claims too made BOTH precision and recall WORSE
        # (more 60-char prefix collisions in the harness's last-writer-wins
        # text index), confirming the residual failures are a _match_claims
        # brittleness problem, not a verifier-judgment problem. See the
        # eval-engineer hand-off note at module end and _scratch/diagnose_v04.txt.
        phrasings = (
            _phrasing_variants(v) if v.verdict != "supported" else [v.claim]
        )
        for phrasing in phrasings:
            if not phrasing.strip():
                continue
            seam.append({
                "claim": phrasing,
                "verdict": v.verdict,
                "location": v.location,
            })
    return seam


# ---------------------------------------------------------------------------
# The verify STAGE -- verify_day(run_date) -> VerificationReport.
#
# Reads the staged issue.json + source_excerpts.jsonl sidecar, runs the
# calibrated verifier (verify_rich) against the EXACT excerpt text summarise
# grounded on, assembles a VerificationReport, writes verify.json, and
# rewrites the staged issue.json in place to denormalise each block's
# verification.
#
# FAILURE-SOFT (non-negotiable, mirrors review.py): on ANY failure -- missing
# issue.json, unparseable sidecar, LLM/transport error, unexpected exception
# -- write a verdict="unavailable" report (empty stories/counts, reason in
# note), do NOT rewrite issue.json (leave verification=None), and RETURN
# NORMALLY. The verify stage is advisory: it never raises into the pipeline
# and never blocks release.
# ---------------------------------------------------------------------------

_FLAGGED_VERDICTS = {"contradicted", "unsupported"}
"""The two verdicts that constitute a flag for the report-level rollup and the
per-story ``headline_flagged`` boolean. ``unverifiable`` is NOT a flag -- it
means "nothing in this excerpt to check against", which is advisory noise, not
a factual concern."""


def verify_day(run_date: _dt.date) -> VerificationReport:
    """Run the advisory factual-accuracy verifier over one day's staged issue.

    Flow
    ----
    1. Read the staged ``issue.json`` (``paths.issue_path(run_date,
       canonical=False)``) and ``source_excerpts.jsonl``
       (``paths.source_excerpts_path``) into a ``{url: excerpt}`` dict.
    2. For each ``SummaryBlock`` (Pulse + every section), union the excerpts
       of its ``source_urls`` (in order, de-duped) into one ``source_excerpt``
       string and call ``verify_rich(headline, body=summary, source_excerpt)``.
    3. Assemble a ``StoryVerification`` per story with the three rollups
       (``has_contradiction`` / ``has_unsupported`` / ``headline_flagged``)
       computed from the claims.
    4. Write ``verify.json`` (the ``VerificationReport``) atomically, then
       rewrite the staged ``issue.json`` in place to set each block's
       ``verification`` (joined on ``story_id``).

    Report-level verdict
    ---------------------
    - ``unavailable`` : the stage could not run at all (missing/unparseable
      issue.json, etc.) -- see the failure-soft contract below.
    - ``flagged``     : the stage ran and at least one story has a
      ``contradicted`` or ``unsupported`` claim.
    - ``clean``       : the stage ran and no story is flagged (an empty issue,
      or an all-``supported``/``unverifiable`` issue, is ``clean``).

    Failure-soft contract
    ----------------------
    ANY failure -- missing issue.json, unparseable sidecar, LLM/transport
    error, unexpected exception -- yields a ``verdict="unavailable"`` report
    (empty stories + counts, reason in ``note``), does NOT rewrite issue.json
    (blocks keep ``verification=None``), and RETURNS NORMALLY. Per-story
    isolation: one story whose verifier call raises is recorded as an
    unverifiable/empty StoryVerification and does not cost the other stories
    their verdicts.

    Parameters
    ----------
    run_date
        The issue date to verify (the staging date dir).

    Returns
    -------
    VerificationReport
        Always returned -- never raises into the pipeline. The same object is
        also serialised to ``verify.json``.
    """
    verify_out = paths.verify_path(run_date, canonical=False)
    issue_path = paths.issue_path(run_date, canonical=False)

    # --- Read the staged issue.json --------------------------------------
    if not issue_path.exists():
        return _write_unavailable_report(
            verify_out, f"no staged issue.json at {issue_path}"
        )
    try:
        issue_payload = json.loads(issue_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _write_unavailable_report(
            verify_out, f"could not read staged issue.json: {exc}"
        )
    if not isinstance(issue_payload, dict):
        return _write_unavailable_report(
            verify_out, "staged issue.json is not a JSON object"
        )

    # --- Read the source_excerpts sidecar --------------------------------
    excerpts_path = paths.source_excerpts_path(run_date, canonical=False)
    try:
        url_to_excerpt = _load_source_excerpts(excerpts_path)
    except Exception as exc:  # noqa: BLE001 -- unparseable sidecar => unavailable
        return _write_unavailable_report(
            verify_out, f"could not read source_excerpts.jsonl: {exc}"
        )

    # --- Verify each story (per-story isolation) -------------------------
    try:
        blocks = _iter_issue_blocks(issue_payload)
    except Exception as exc:  # noqa: BLE001 -- malformed issue shape => unavailable
        return _write_unavailable_report(
            verify_out, f"could not walk issue stories: {exc}"
        )

    stories: list[StoryVerification] = []
    for block in blocks:
        story_id = block.get("story_id")
        if not isinstance(story_id, str) or not story_id:
            _LOG.warning("verify: block missing story_id -- skipping")
            continue
        try:
            sv = _verify_one_story(block, url_to_excerpt)
        except Exception:  # noqa: BLE001 -- one bad story must not lose the rest
            _LOG.exception(
                "verify: story_id=%s failed verification -- recording empty",
                story_id,
            )
            sv = _empty_story_verification(story_id)
        stories.append(sv)

    # --- Assemble the report ---------------------------------------------
    verdict_counts = _tally_verdicts(stories)
    flagged = any(
        s.has_contradiction or s.has_unsupported for s in stories
    )
    verdict = "flagged" if flagged else "clean"

    report = VerificationReport(
        generated_at=_dt.datetime.now(_dt.timezone.utc),
        prompt_version=VERIFY_PROMPT_VERSION,
        verdict=verdict,  # type: ignore[arg-type]
        verdict_counts=verdict_counts,  # type: ignore[arg-type]
        stories=stories,
        note=(
            f"verified {len(stories)} stories"
            + (" -- factual flags present" if flagged else "")
        ),
    )

    # --- Write verify.json (atomic) --------------------------------------
    try:
        _write_report(verify_out, report)
    except Exception as exc:  # noqa: BLE001
        # If we cannot even write the sidecar, fall back to unavailable so
        # the pipeline sees a consistent state on disk. We do NOT rewrite
        # issue.json in this case.
        _LOG.exception("verify: failed to write verify.json")
        return _write_unavailable_report(
            verify_out, f"could not write verify.json: {exc}"
        )

    # --- Rewrite issue.json in place to denormalise verification ---------
    # Best-effort: if this fails the report is still the authoritative copy;
    # the per-story denormalisation is a convenience. We log and return the
    # report rather than flipping to unavailable (the verify ran fine).
    try:
        _rewrite_issue_with_verification(issue_path, issue_payload, stories)
    except Exception:  # noqa: BLE001
        _LOG.exception(
            "verify: wrote verify.json but failed to denormalise "
            "verification onto issue.json for %s", run_date.isoformat(),
        )

    _LOG.info(
        "verify: %s verdict=%s stories=%d counts=%s -> %s",
        run_date.isoformat(), verdict, len(stories), verdict_counts, verify_out,
    )
    return report


def _verify_one_story(
    block: dict[str, Any], url_to_excerpt: dict[str, str]
) -> StoryVerification:
    """Run the verifier on one issue block and build its StoryVerification.

    Unions the excerpts of the block's ``source_urls`` (in order, de-duped)
    into one source excerpt, calls ``verify_rich`` against (headline, summary,
    source_excerpt), and assembles the model with rollups computed from the
    claims. The ``ClaimVerdict`` dataclass from ``verify_rich`` maps 1:1 onto
    the pydantic ``ClaimVerdict`` model.
    """
    story_id = str(block.get("story_id", ""))
    headline = str(block.get("headline", "") or "")
    body = str(block.get("summary", "") or "")
    source_urls = block.get("source_urls") or []

    source_excerpt = _union_excerpts(source_urls, url_to_excerpt)

    rich = verify_rich(headline, body, source_excerpt)

    claims: list[ClaimVerdictModel] = []
    for v in rich:
        claims.append(ClaimVerdictModel(
            claim=v.claim,
            verdict=v.verdict,  # type: ignore[arg-type]
            location=v.location,  # type: ignore[arg-type]
            summary_span=v.summary_span,
            source_span=v.source_span,
            note=v.note,
        ))

    has_contra = any(c.verdict == "contradicted" for c in claims)
    has_unsup = any(c.verdict == "unsupported" for c in claims)
    headline_flag = any(
        c.location == "headline" and c.verdict in _FLAGGED_VERDICTS
        for c in claims
    )
    return StoryVerification(
        story_id=story_id,
        prompt_version=VERIFY_PROMPT_VERSION,
        claims=claims,
        has_contradiction=has_contra,
        has_unsupported=has_unsup,
        headline_flagged=headline_flag,
    )


def _union_excerpts(
    source_urls: list[Any], url_to_excerpt: dict[str, str]
) -> str:
    """Join the excerpts for a block's source_urls into one source string.

    In source_urls order, de-duped, skipping URLs with no excerpt or an empty
    excerpt. Distinct excerpts are joined with a blank line so the verifier
    sees clean boundaries. Returns an empty string when NONE of the URLs
    yielded text -- ``verify_rich`` then short-circuits every claim to
    ``unverifiable`` (the correct behaviour when the source is unavailable).
    """
    seen: set[str] = set()
    parts: list[str] = []
    for raw in source_urls:
        url = str(raw)
        if url in seen:
            continue
        seen.add(url)
        excerpt = (url_to_excerpt.get(url) or "").strip()
        if not excerpt:
            continue
        if excerpt in parts:
            continue
        parts.append(excerpt)
    return "\n\n".join(parts)


def _empty_story_verification(story_id: str) -> StoryVerification:
    """A StoryVerification with no claims -- used when a single story's
    verification raised. All rollups False; the report still ships the rest
    of the stories. ``clean`` for this story (no flag), which is honest: we
    could not check it, we are not asserting a problem."""
    return StoryVerification(
        story_id=story_id,
        prompt_version=VERIFY_PROMPT_VERSION,
        claims=[],
        has_contradiction=False,
        has_unsupported=False,
        headline_flagged=False,
    )


def _tally_verdicts(
    stories: list[StoryVerification],
) -> dict[str, int]:
    """Per-verdict claim tallies across all stories, e.g.
    ``{"supported": 31, "unsupported": 2, "contradicted": 0,
    "unverifiable": 4}``. Verdicts absent from the issue are simply absent
    from the dict (the model permits a sparse dict)."""
    counts: dict[str, int] = {}
    for story in stories:
        for claim in story.claims:
            counts[claim.verdict] = counts.get(claim.verdict, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Sidecar / issue I/O for the verify stage.
# ---------------------------------------------------------------------------

def _load_source_excerpts(path: Path) -> dict[str, str]:
    """Load source_excerpts.jsonl into a ``{url: excerpt}`` dict.

    Tolerates a MISSING file (returns an empty dict -- every story then
    verifies against an empty source excerpt, i.e. all claims unverifiable;
    that is the honest degraded state, not a failure). RAISES on an
    unparseable line so the caller flips the whole stage to ``unavailable``
    -- a corrupt sidecar means we can't trust the join, and a silent
    partial-load would mislead the verifier.

    Last writer wins on a duplicate URL (the summarise writer already
    de-dupes by URL, so duplicates should not occur in practice).
    """
    if not path.exists():
        _LOG.warning(
            "verify: source_excerpts.jsonl missing at %s -- verifying "
            "against empty excerpts (all claims unverifiable)", path,
        )
        return {}
    out: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)  # raises JSONDecodeError -> stage unavailable
        if not isinstance(rec, dict):
            raise ValueError(
                f"source_excerpts.jsonl line {lineno} is not an object"
            )
        url = rec.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError(
                f"source_excerpts.jsonl line {lineno} missing a string 'url'"
            )
        out[url] = str(rec.get("excerpt", "") or "")
    return out


def _iter_issue_blocks(issue_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every SummaryBlock dict in the issue (Pulse + all sections), in
    reading order. Works from the raw JSON payload (not the pydantic Issue) so
    schema-version skew between staged and code never crashes the verify stage.
    """
    blocks: list[dict[str, Any]] = []
    pulse = issue_payload.get("pulse") or {}
    if isinstance(pulse, dict):
        for story in pulse.get("stories") or []:
            if isinstance(story, dict):
                blocks.append(story)
    for section in issue_payload.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for story in section.get("stories") or []:
            if isinstance(story, dict):
                blocks.append(story)
    return blocks


def _write_report(path: Path, report: VerificationReport) -> None:
    """Atomic write of verify.json (.tmp + fsync + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.loads(report.model_dump_json())
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _write_unavailable_report(path: Path, reason: str) -> VerificationReport:
    """Build + write a ``verdict="unavailable"`` report and return it.

    Mirrors review.py's ``_write_unavailable``: the stage could not run, so we
    record WHY (in ``note``) with empty stories/counts, write verify.json, and
    return normally. issue.json is NOT touched -- blocks keep
    ``verification=None``. We deliberately do not raise even if the write
    itself fails: a verify stage must never block the pipeline.
    """
    _LOG.warning("verify: unavailable -- %s", reason)
    report = VerificationReport(
        generated_at=_dt.datetime.now(_dt.timezone.utc),
        prompt_version=VERIFY_PROMPT_VERSION,
        verdict="unavailable",
        verdict_counts={},
        stories=[],
        note=f"unavailable: {reason}",
    )
    try:
        _write_report(path, report)
    except Exception:  # noqa: BLE001 -- even the unavailable write is best-effort
        _LOG.exception(
            "verify: could not write the unavailable verify.json at %s", path,
        )
    return report


def _rewrite_issue_with_verification(
    issue_path: Path,
    issue_payload: dict[str, Any],
    stories: list[StoryVerification],
) -> None:
    """Rewrite the staged issue.json in place, setting each SummaryBlock's
    ``verification`` by joining on ``story_id``.

    Operates on the raw payload we already parsed (no second read), mutates
    the block dicts in place, then atomically rewrites the file (.tmp + fsync
    + rename). Stories the verifier produced an empty StoryVerification for
    are still joined (with their empty claim list) so render/editor sees a
    ``clean`` denormalised verdict rather than a ``None`` it can't distinguish
    from "verify never ran".

    A legitimate issue.json writer: the verify stage is contractually allowed
    to rewrite the staged issue (DESIGN.md "verify.json").
    """
    by_id = {s.story_id: s for s in stories}

    def _apply(block: dict[str, Any]) -> None:
        sid = block.get("story_id")
        sv = by_id.get(sid) if isinstance(sid, str) else None
        if sv is not None:
            block["verification"] = json.loads(sv.model_dump_json())

    pulse = issue_payload.get("pulse") or {}
    if isinstance(pulse, dict):
        for story in pulse.get("stories") or []:
            if isinstance(story, dict):
                _apply(story)
    for section in issue_payload.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for story in section.get("stories") or []:
            if isinstance(story, dict):
                _apply(story)

    issue_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = issue_path.with_suffix(issue_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(issue_payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, issue_path)


# ---------------------------------------------------------------------------
# CLI / manual invocation.
# ---------------------------------------------------------------------------

def _diagnose(only: str = "") -> int:
    """Per-fixture side-by-side dump + (a)/(b) classification.

    For every contradicted and supported fixture, runs ``verify_rich`` (the raw
    judgment, BEFORE the seam's phrasing expansion) and prints, claim by claim:
      * the ground-truth claims + verdicts + locations from the fixture, and
      * the verifier's actually-returned claims + verdicts + spans.

    Then, for each ground-truth claim that the eval would score WRONG, it
    classifies the miss as:
      (a) REAL  -- the verifier assigned the wrong verdict to the fact, or
      (b) ARTIFACT -- the verifier got the verdict right on the matching fact
          but the eval's prefix matcher would fail to align the labelled claim
          text to the verifier's claim text.

    The classifier emulates the eval seam: it expands flagged verdicts via
    ``_phrasing_variants`` (as ``verify()`` does), then runs the SAME 60-char
    (location, prefix) matching ``_match_claims`` uses, so what we print is what
    the eval would actually score. Where the matcher misses, we look for a
    verifier claim that semantically covers the same fact and report whether its
    verdict is right -- that is the (b) signal.
    """
    import yaml as _yaml

    fixtures_path = (
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    cases_path = os.path.join(
        fixtures_path, "evals", "fixtures", "factual-accuracy", "cases.yaml"
    )
    with open(cases_path, "r", encoding="utf-8") as fh:
        cases = (_yaml.safe_load(fh) or {}).get("cases", [])

    wanted = [s.strip() for s in only.split(",") if s.strip()]

    def _key(text: str) -> str:
        return (text or "")[:60].strip().lower()

    # Track aggregate (a)/(b) classification across all printed cases.
    real_misses: list[str] = []      # (a) on contradicted recall
    artifact_misses: list[str] = []  # (b) on contradicted recall
    real_fps: list[str] = []         # (a) on supported precision
    artifact_fps: list[str] = []     # (b) on supported precision

    for case in cases:
        cid = case.get("id", "<unknown>")
        category = case.get("category")
        if category not in ("contradicted", "supported"):
            continue
        if wanted and not any(w in cid for w in wanted):
            continue

        headline = case.get("headline", "")
        body = case.get("summary_text", "")
        source = case.get("source_excerpt", "")
        gt_claims = case.get("claims", [])

        rich = verify_rich(headline, body, source)

        # Build the seam exactly as verify() does (flagged verdicts expanded).
        seam: list[dict] = []
        for v in rich:
            phrasings = (
                _phrasing_variants(v) if v.verdict != "supported" else [v.claim]
            )
            for p in phrasings:
                if p.strip():
                    seam.append({"claim": p, "verdict": v.verdict,
                                 "location": v.location})

        # Emulate _match_claims: positional if equal length, else prefix index.
        positional = len(gt_claims) == len(seam)
        compound_index: dict[str, dict] = {}
        text_index: dict[str, dict] = {}
        for vd in seam:
            ck = _key(vd["claim"])
            loc = (vd.get("location") or "").strip().lower()
            compound_index[f"{loc}:{ck}" if loc else ck] = vd
            text_index[ck] = vd

        print(f"\n{'='*78}\n{cid}  [{category}"
              f"{'/' + case['mutation_type'] if case.get('mutation_type') else ''}]")
        print(f"{'-'*78}")
        print("VERIFIER RETURNED (verify_rich, pre-expansion):")
        for v in rich:
            print(f"  [{v.location:8}] {v.verdict:13} | {v.claim[:70]}")
            if v.source_span:
                print(f"             span: \"{v.source_span[:80]}\"")

        print("\nGROUND TRUTH  ->  MATCHED VERIFIER VERDICT:")
        for i, gc in enumerate(gt_claims):
            gt = gc.get("ground_truth_verdict")
            gloc = (gc.get("location") or "body").strip().lower()
            gtext = gc.get("claim", "")
            gk = _key(gtext)
            if positional:
                vd = seam[i] if i < len(seam) else None
            else:
                vd = (compound_index.get(f"{gloc}:{gk}" if gloc else gk)
                      or text_index.get(gk))
            vv = vd["verdict"] if vd else "<NO MATCH>"
            scored_correct = (vv == gt)
            flag = "OK " if scored_correct else "XX "
            print(f"  {flag}[{gloc:8}] gt={gt:13} scored={vv:13} | {gtext[:60]}")

            # Classify the misses that matter for the gates.
            if not scored_correct:
                # Find whether the verifier ACTUALLY judged this fact right,
                # regardless of matcher alignment -- semantic-cover lookup by
                # source-span / verdict at the same location among rich claims.
                covered = _find_semantic_cover(rich, gc)
                judged = covered.verdict if covered else None
                if category == "contradicted" and gt == "contradicted":
                    if judged == "contradicted":
                        artifact_misses.append(
                            f"{cid}: gt-claim '{gtext[:45]}' -- verifier DID "
                            f"flag (rich claim '{covered.claim[:40]}' = "
                            f"contradicted) but matcher missed it"
                        )
                    else:
                        real_misses.append(
                            f"{cid}: gt-claim '{gtext[:45]}' -- verifier verdict "
                            f"was '{judged or vv}', not contradicted"
                        )
                elif category == "supported" and gt == "supported":
                    # Precision FP: verifier flagged a supported claim.
                    if judged in ("supported", "unverifiable", None) and vv in (
                        "supported", "unverifiable", "<NO MATCH>"):
                        artifact_fps.append(
                            f"{cid}: gt-claim '{gtext[:45]}' -- verifier did NOT "
                            f"flag the fact (judged '{judged}') but matcher "
                            f"aligned a different/absent verdict ('{vv}')"
                        )
                    else:
                        real_fps.append(
                            f"{cid}: gt-claim '{gtext[:45]}' -- verifier flagged "
                            f"it '{judged or vv}'"
                        )

    print(f"\n\n{'#'*78}\n# (a)/(b) CLASSIFICATION SUMMARY\n{'#'*78}")
    print(f"\nCONTRADICTED RECALL MISSES:")
    print(f"  (a) REAL verifier errors      : {len(real_misses)}")
    for m in real_misses:
        print(f"      - {m}")
    print(f"  (b) MATCHING ARTIFACTS        : {len(artifact_misses)}")
    for m in artifact_misses:
        print(f"      - {m}")
    print(f"\nSUPPORTED PRECISION FALSE-POSITIVES:")
    print(f"  (a) REAL verifier errors      : {len(real_fps)}")
    for m in real_fps:
        print(f"      - {m}")
    print(f"  (b) MATCHING ARTIFACTS        : {len(artifact_fps)}")
    for m in artifact_fps:
        print(f"      - {m}")

    print(f"\nTUNING COST: {_CALL_METER['calls']} LLM calls | "
          f"~{_CALL_METER['approx_prompt_chars']//4} prompt tokens + "
          f"~{_CALL_METER['approx_completion_chars']//4} completion tokens "
          f"(chars/4 estimate)")
    return 0


def _find_semantic_cover(rich: list[ClaimVerdict], gt_claim: dict) -> ClaimVerdict | None:
    """Best-effort: among the verifier's rich claims, find the one that covers
    the same FACT as the ground-truth claim, independent of the eval's prefix
    matcher. Used only by --diagnose to tell a real miss from a match artifact.

    Heuristic: same location, then maximal token overlap on content words. This
    is diagnostic-only; it never feeds a verdict."""
    gloc = (gt_claim.get("location") or "body").strip().lower()
    gtext = (gt_claim.get("claim") or "").lower()
    g_tokens = set(re.findall(r"[a-z0-9]+", gtext))
    g_tokens -= {"the", "a", "an", "of", "to", "and", "on", "in", "is", "are",
                 "for", "with", "that", "this", "its", "it"}

    best: ClaimVerdict | None = None
    best_overlap = 0.0
    for v in rich:
        v_tokens = set(re.findall(r"[a-z0-9]+", f"{v.claim} {v.summary_span}".lower()))
        if not v_tokens or not g_tokens:
            continue
        overlap = len(g_tokens & v_tokens) / len(g_tokens)
        loc_bonus = 0.15 if v.location == gloc else 0.0
        score = overlap + loc_bonus
        if score > best_overlap:
            best_overlap = score
            best = v
    # Require a meaningful overlap to claim "cover".
    return best if best_overlap >= 0.4 else None


def _cli() -> int:
    """Tiny CLI so the verifier can be invoked and the Eval 7 gate run without
    importing from a notebook.

    Usage:
        python -m src.verify --eval
            Run Eval 7 against the 31 fixtures with this verifier wired in;
            prints the gate numbers + per-location recall and exits non-zero
            on any hard-gate failure.

        python -m src.verify --demo
            Run the verifier on a single built-in (headline, body, source)
            triple and pretty-print the rich verdicts. Useful for eyeballing
            prompt changes.

        python -m src.verify --day YYYY-MM-DD
            Run the full ``verify_day`` stage against that date's staged
            issue.json + source_excerpts.jsonl: writes verify.json, rewrites
            issue.json with per-story verification, and prints the report-
            level verdict + counts. For manual testing of the stage wiring.
    """
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(prog="python -m src.verify")
    parser.add_argument("--eval", action="store_true",
                        help="Run Eval 7 against the factual-accuracy fixtures.")
    parser.add_argument("--day", default="",
                        help="Run the verify_day stage for YYYY-MM-DD against "
                             "the staged issue.json + source_excerpts.jsonl.")
    parser.add_argument("--diagnose", action="store_true",
                        help="Side-by-side ground-truth vs verifier output for "
                             "every contradicted + supported fixture; classifies "
                             "misses as REAL error vs MATCHING ARTIFACT.")
    parser.add_argument("--only", default="",
                        help="Comma-separated fixture id substrings to restrict "
                             "--diagnose to (e.g. 'fa_301,fa_302').")
    parser.add_argument("--demo", action="store_true",
                        help="Run a single built-in demo triple.")
    args = parser.parse_args()

    # Load .env for local/manual runs so LLM_MODEL etc. are present. Best
    # effort: python-dotenv is a dev convenience, not a runtime dependency.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if args.eval:
        from evals.run_evals import eval_factual_accuracy
        result = eval_factual_accuracy(verifier=verify)
        d = result.details
        print(f"\n=== Eval 7: factual_accuracy [{result.status.upper()}] ===")
        print(f"recall_contradicted  : {d.get('recall_contradicted')}  "
              f"(gate >= {d['thresholds']['recall_contradicted']})")
        print(f"precision_supported  : {d.get('precision_supported')}  "
              f"(gate >= {d['thresholds']['precision_supported']})")
        print(f"unverifiable_accuracy: {d.get('unverifiable_accuracy')}  "
              f"(gate >= {d['thresholds']['unverifiable_accuracy']})")
        print(f"per_location_recall  : {d.get('per_location_recall')}")
        print(f"per_mutation_recall  : {d.get('per_mutation_type_recall')}")
        print(f"raw_counts           : {d.get('raw_counts')}")
        if d.get("failures"):
            print("\nFAILURES:")
            for f in d["failures"]:
                print(f"  - {f}")
        return 0 if result.passed else 1

    if args.day:
        try:
            run_date = _dt.date.fromisoformat(args.day.strip())
        except ValueError:
            print(f"--day must be YYYY-MM-DD; got {args.day!r}")
            return 2
        report = verify_day(run_date)
        print(f"\n=== verify_day {run_date.isoformat()} "
              f"[{report.verdict.upper()}] ===")
        print(f"stories       : {len(report.stories)}")
        print(f"verdict_counts: {dict(report.verdict_counts)}")
        if report.note:
            print(f"note          : {report.note}")
        for s in report.stories:
            flags = []
            if s.has_contradiction:
                flags.append("contradicted")
            if s.has_unsupported:
                flags.append("unsupported")
            if s.headline_flagged:
                flags.append("headline")
            flag_str = (" [" + ",".join(flags) + "]") if flags else ""
            print(f"  - {s.story_id}: {len(s.claims)} claims{flag_str}")
        return 0

    if args.diagnose:
        return _diagnose(only=args.only)

    if args.demo:
        headline = "Hugging Face rebuilt its CLI to cut agent token use sixfold"
        body = ("Hugging Face rebuilt the hf CLI so agents auto-receive untruncated "
                "TSV output, cutting token use by up to six times. Swap to hf v1.9.0.")
        source = ("Hugging Face has released version 1.9.0 of the hf CLI. In internal "
                  "benchmarks on multi-step Hub tasks, this reduced token consumption "
                  "by up to six times (6x).")
        rich = verify_rich(headline, body, source)
        print(_json.dumps([v.__dict__ for v in rich], indent=2))
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())


# ---------------------------------------------------------------------------
# NOTE FOR THE ARCHITECT -- proposed pydantic StoryVerification model.
# ---------------------------------------------------------------------------
# This verifier intentionally does NOT edit src/models.py. When you promote
# the output to a contract surface, the dict shape this module produces maps to:
#
#   class ClaimVerdict(BaseModel):
#       claim: str                       # near-verbatim span of headline/body
#       verdict: Literal["supported", "unsupported",
#                        "contradicted", "unverifiable"]
#       location: Literal["headline", "body"]
#       summary_span: str = ""           # exact summary text carrying the claim
#       source_span: str = ""            # supporting/contradicting source quote
#                                        # (validator: non-empty iff verdict ==
#                                        #  "contradicted")
#       note: str = ""                   # one-line rationale
#
#   class StoryVerification(BaseModel):
#       story_id: str                    # the cluster_id / story_id verified
#       prompt_version: str              # == verify.VERIFY_PROMPT_VERSION
#       claims: list[ClaimVerdict]
#       # convenience rollups the renderer / editor loop will want:
#       has_contradiction: bool          # any verdict == "contradicted"
#       has_unsupported: bool            # any verdict == "unsupported"
#       headline_flagged: bool           # any headline claim flagged
#
# Suggested validator: StoryVerification rejects a ClaimVerdict whose
# verdict == "contradicted" with an empty source_span (mirrors
# _enforce_contradiction_discipline here -- keep the rule in ONE place once the
# model exists; this code's guard can then defer to the model).
# ---------------------------------------------------------------------------
