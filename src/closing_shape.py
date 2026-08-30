"""
src/closing_shape.py -- deterministic closing-shape detector for story bodies.

Why this exists (2026-08-29). The reviewer (src/review.py, ``closing_shape``
criterion) filed the same writer defect against almost every issue in
August: a Currents or Big Picture body ending on an instruction to the
reader ("Raise it at your next fraud-detection architecture review.")
where the contract wants a maturity signal or a strategic question. Five
prompt revisions (summarise v0.18 -> v0.22) did not make the rule bind.
Per No Token Wasted the DETECTION is code -- this module -- and only the
rewrite of the one offending sentence is an LLM call (see
``summarise._repair_closing_shape``).

The detector is deliberately conservative: it recognises the concrete
failure shapes the reviewer keeps quoting, and prefers a missed defect
(the reviewer still catches it) to a false alarm (which would spend a
rewrite call on a good close and risk making it worse).

Pure functions, no I/O, no LLM. Importable by review-side code later
without pulling in summarise.py's heavy imports.

Owner: LLM Engineer.
"""

from __future__ import annotations

import re

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
"""Sentence-boundary splitter. Splits after ., ! or ? followed by
whitespace. Abbreviations with internal stops ("9 a.m. tomorrow") can
over-split; acceptable here -- the output feeds a do-not-reuse list and a
last-sentence check, never published prose on its own."""


def extract_closing_sentence(summary: str) -> str:
    """Return the last sentence of a summary body (the close). Empty or
    whitespace-only input returns ""."""
    text = (summary or "").strip()
    if not text:
        return ""
    parts = [p.strip() for p in SENTENCE_SPLIT_RE.split(text) if p.strip()]
    return parts[-1] if parts else text


def replace_closing_sentence(summary: str, replacement: str) -> str:
    """Return ``summary`` with its last sentence swapped for
    ``replacement``. Whitespace between sentences is normalised to one
    space; a body with a single sentence is replaced whole."""
    text = (summary or "").strip()
    new = (replacement or "").strip()
    if not text:
        return new
    parts = [p.strip() for p in SENTENCE_SPLIT_RE.split(text) if p.strip()]
    if not parts:
        return new
    parts[-1] = new
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Imperative detection.
#
# A sentence "opens on an imperative" when its first word -- or the first
# word of a main clause after a leading subordinate clause ("Before your
# next review, run it"), a semicolon, or a colon ("The code is public; run
# it before you ship") -- is a base-form verb from the list below.
#
# The list is a curated set of the verbs that actually appear in the
# closes the reviewer flags, not a dictionary. Words that are also common
# sentence-opening NOUNS ("Test results show", "Budget constraints ...",
# "Plan B is ...") sit in the AMBIGUOUS set and only count when the next
# word is a typical object opener ("it", "your", "the", "this" ...).
# Second person by itself ("does yours?") is ratified voice and never a
# hit. False negatives are preferred to false positives.
# ---------------------------------------------------------------------------

IMPERATIVE_VERBS: frozenset[str] = frozenset({
    "raise", "bring", "run", "wire", "map", "treat", "test", "star", "ask",
    "check", "read", "try", "use", "build", "add", "start", "put", "take",
    "watch", "keep", "compare", "benchmark", "audit", "review", "pilot",
    "ship", "plan", "budget", "revisit", "confirm", "verify", "measure",
    "instrument", "flag", "expect", "assume", "consider", "note",
    "remember", "prepare", "look", "see", "get", "make", "do", "apply",
    "adopt", "deploy", "evaluate", "validate", "install", "diff", "upgrade",
    "swap", "pull", "clone", "rerun", "pin", "enable", "disable", "push",
    "fold", "wrap", "roll", "route", "gate", "hold", "wait", "stop", "avoid",
    "skip", "drop", "cut", "refresh", "set", "open", "log", "track", "move",
    "switch", "point",
})

AMBIGUOUS_VERBS: frozenset[str] = frozenset({
    # Also plausible as the first NOUN of a declarative sentence.
    "test", "plan", "budget", "flag", "review", "benchmark", "audit",
    "ship", "measure", "note", "pilot", "watch", "star", "map", "look",
    "see", "get", "make", "do", "use", "check", "diff", "pull", "push",
    "roll", "route", "gate", "hold", "drop", "cut", "set", "open", "log",
    "track", "move", "switch", "point", "wrap", "fold", "swap",
})

_OBJECT_OPENERS: frozenset[str] = frozenset({
    "it", "this", "that", "these", "those", "them", "the", "a", "an",
    "your", "yours", "one", "each", "every", "both", "any", "all", "its",
    "our", "whether", "how", "what", "which", "where", "if",
})

_SUBORDINATORS: frozenset[str] = frozenset({
    "before", "after", "when", "if", "once", "unless", "while", "until",
    "whenever", "where", "as",
})

_CLAUSE_SPLIT_RE = re.compile(r"\s*(?:;|:|--|—)\s*")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text)]


def _opens_on_imperative(clause: str) -> bool:
    words = _words(clause)
    if not words:
        return False
    first = words[0]
    if first not in IMPERATIVE_VERBS:
        return False
    if first in AMBIGUOUS_VERBS:
        return len(words) > 1 and words[1] in _OBJECT_OPENERS
    return True


def opens_on_imperative(sentence: str) -> bool:
    """True when ``sentence`` is (or contains as a main clause) an
    instruction to the reader, per the rules in the module comment."""
    text = (sentence or "").strip().strip('"“”\'()[]')
    if not text:
        return False
    for clause in _CLAUSE_SPLIT_RE.split(text):
        clause = clause.strip()
        if not clause:
            continue
        if _opens_on_imperative(clause):
            return True
        # Leading subordinate clause: "Before your next review, run it."
        words = _words(clause)
        if words and words[0] in _SUBORDINATORS and "," in clause:
            main = clause.split(",", 1)[1]
            if _opens_on_imperative(main):
                return True
    return False


def closing_shape_defect(section: str, body: str) -> str | None:
    """Return a short reason when the body's last sentence fails the
    section's closing-shape contract, else ``None``.

    Contract (mirrors the reviewer's ``closing_shape`` criterion):
      * pulse       -- must NOT end on a question or an instruction.
      * big_picture -- MUST end on a question (a strategic question, so the
                       last character is '?').
      * currents    -- must NOT end on an instruction (the body lands on a
                       presence-form maturity signal).
      * hands_on    -- an imperative is the contract; not checked here
                       (its recurring defect is mould repetition, which the
                       feed-forward mechanism handles, not shape).
    Unknown sections and empty bodies return ``None``.
    """
    close = extract_closing_sentence(body)
    if not close:
        return None
    ends_on_question = close.rstrip().endswith("?")
    if section == "big_picture":
        if not ends_on_question:
            return "does not end on a strategic question"
        return None
    if section == "pulse":
        if ends_on_question:
            return "ends on a question"
        if opens_on_imperative(close):
            return "ends on an instruction to the reader"
        return None
    if section == "currents":
        if opens_on_imperative(close):
            return "ends on an instruction to the reader"
        return None
    return None
