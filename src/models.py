"""
src/models.py -- AI Vector data contracts (pydantic v2).

This module is the single source of truth for the **shape** of every artifact
that crosses a pipeline seam in AI Vector. The long-form rationale (why each
field exists, why each constraint is what it is, what readers may tolerate)
lives in `docs/internal/DESIGN.md`. This module is the executable counterpart.

Architect (Tech Lead) owns this file. Any change to a class below is a
contract change and requires Architect review per `docs/internal/TEAM.md`.

Archive states. `Issue.issue_number` is `None` while the issue lives in
`data/staging/YYYY-MM-DD/` (work-in-progress, freely re-runnable) and is
assigned an integer at release time, when the issue is promoted to the
canonical `data/YYYY-MM-DD/`. See DESIGN.md "Archive: staging vs canonical"
for the full state model and the release transition.

Pipeline flow (producers -> consumers; full picture in `docs/internal/TEAM.md`):

    Item            -> produced by src/fetch.py
                       consumed by src/cluster.py, evals, render (provenance)

    Cluster         -> produced by src/cluster.py
                       consumed by src/rank.py, src/summarise.py, evals

    RankedStory     -> produced by src/rank.py
                       consumed by src/summarise.py, Editor, evals, render

    SummaryBlock    -> produced by src/summarise.py (as a child of IssueSection)
                       consumed by render, Editor, Arman, future summarise.py
                                   (callbacks)

    IssueSection    -> produced by src/summarise.py (as a child of Issue)
                       consumed by render, Editor, Arman

    DigestBullet    -> produced by src/summarise.py (as a child of Issue --
                       the 30-second read, 2026-08-09 layout redesign)
                       consumed by render (skim block), review, verify, evals

    Issue           -> produced by src/summarise.py
                       consumed by Arman (ratification), render, Editor, evals

    SourceHealth    -> produced by src/fetch.py (as a child of the
                       source_health.json payload)
                       consumed by evals, render (footer), source (trust decay)

    SourceHealthReport -> produced by src/fetch.py
                          consumed by evals (per-source health summary)

    ClaimVerdict /  -> produced by src/verify.py (advisory verify stage)
    StoryVerification  consumed by render (per-story factual flag), Editor
                       loop, evals; denormalised onto SummaryBlock.verification

    VerificationReport -> produced by src/verify.py (the verify.json sidecar)
                          consumed by render, Editor, evals

    GateCheck /     -> produced by src/gate.py (the gate.json sidecar)
    GateDecision       consumed by the unattended-publish workflow, render
                       (promoted to released), evals, Arman

    ReviewTarget /  -> produced by src/review.py (the review.json sidecar;
    ReviewFinding /    review.md is rendered from it)
    ReviewReport       consumed by src/revise.py (text_edit findings only),
                       Arman, evals

    RevisionChange /-> produced by src/revise.py (revisions.jsonl -- one
    RevisionCycle      RevisionCycle per line, appended per invocation)
                       consumed by Arman, render (promoted to released),
                       evals

Schema versioning. Every persisted model carries a `schema_version: int`
field per the DESIGN.md schema changelog. Most models are v1 today; `Issue`
is v8 (v2 added `issue_number`; v3 made `issue_number` Optional to support
the staging vs canonical archive split; v4 dropped direction_note +
finance_angle and renamed sections; v5 adds `revision: int = 0` for
same-date re-releases that display as `#N.M`; v6 tracks the SummaryBlock
v3 change -- the additive `verification` field from the advisory verify
stage; v7 tracks SummaryBlock v4 -- the additive `take`; v8 adds the
optional `digest` (30-second read) and tracks IssueSection v4 -- the
additive `synthesis` replacing intro_lead/intro_body going forward -- see
"Archive: staging vs canonical", "Issue Number Registry", "The digest",
and "verify.json" in DESIGN.md). When you change a shape, bump the version
on the affected model and append a row to the changelog in DESIGN.md in
the same PR.

External vectors. Embeddings are not stored inline -- `Cluster.centroid_ref`
is a plain `str` filename pointing into `data/YYYY-MM-DD/embeddings/`. See
DESIGN.md "Embedding model" for the storage contract.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)


# ---------------------------------------------------------------------------
# Literals -- pinned exactly to DESIGN.md and config/rubric.yaml.
# ---------------------------------------------------------------------------

SourceType = Literal["rss", "atom", "api", "html"]
"""How an Item was fetched. `html` is the isolated-fallback path only."""

AudienceTag = Literal["hands_on", "big_picture", "finance", "general"]
"""Who a RankedStory / SummaryBlock is for. At least one tag is required.

Renamed in v0.8 (2026-05-24) to match section names:
  - ``builder`` -> ``hands_on``
  - ``leader``  -> ``big_picture``
"""

RankTier = Literal["big_picture", "hands_on", "currents", "cut"]
"""
Editorial slot a RankedStory belongs in. `summarise.py` reads this to assign
sections; Editor may relabel; `cut` is below threshold and excluded from the
issue (kept in ranked.jsonl for transparency / eval).

Schema v3 (2026-05-30): tier value-space expanded from {pulse, on_the_radar,
cut} to {big_picture, hands_on, on_the_radar, cut}. Pulse is NOT a stored
tier -- summarise.py picks the Pulse from the union of the two head-section
tiers (big_picture + hands_on). The expansion makes tier authoritative for
section routing; the picker gates strictly on tier instead of scavenging
audience_tags + score (which was producing empty On-the-Radar / Hands-On
sections when rank.py only ever wrote on_the_radar + cut). See
config/rubric.yaml `tier_thresholds` for the score bands that drive the
assignment in `src/rank.py::_assign_initial_tier`.

Schema v4 (Phase 2, 2026-05-30): ``on_the_radar`` renamed to ``currents``.
The old name implied "you might act on this soon" -- a maturity floor with
action-readiness implied. ``currents`` drops the action implication and
keeps the maturity-tail meaning, which is what the section is actually
doing in practice. Pydantic ``model_validator(mode="before")`` coerces
the archived ``"on_the_radar"`` value to ``"currents"`` so released v3
ranked.jsonl rows still parse.
"""

SectionName = Literal[
    "pulse",          # The Pulse -- 1 story, the most important thing today
    "big_picture",    # The Big Picture -- strategic angles
    "hands_on",       # Hands-On -- enthusiasts + builders, hands-on news
    "currents",       # Currents -- maturity-tail, early signals (Phase 2)
]
"""IssueSection.name -- the four sections of the rendered newsletter.

Renamed in schema v5 (2026-05-24):
- ``leaders`` -> ``big_picture`` (display: "The Big Picture")
- ``geeks`` -> ``hands_on`` (display: "Hands-On")
- ``notable`` -> ``on_the_radar`` (display: "On the Radar")

Renamed in schema v3 of IssueSection (Phase 2, 2026-05-30):
- ``on_the_radar`` -> ``currents`` (display: "Currents")
  Pydantic ``model_validator(mode="before")`` coerces archived
  ``"on_the_radar"`` values to ``"currents"`` so released issue.json files
  still parse.
"""

Signal = Literal["act", "try", "read", "watch", "discuss"]
"""Per-story editorial verdict shown as a small pill in the rendered HTML.

Added in v0.9 (Phase B). Optional on SummaryBlock so older issues parse
without it; missing => pill is not rendered.

  - ``act``     : a vendor / contract / architecture decision worth making
                  this quarter. The Big Picture territory.
  - ``try``     : sandbox it this week. Hands-On territory.
  - ``read``    : informational; absorb the framing.
  - ``watch``   : too thin / too early to act on. Default for On the Radar.
  - ``discuss`` : design concept worth raising at a review, not shippable.
"""


MissedReason = Literal[
    "timeout",
    "http_4xx",
    "http_5xx",
    "parse_error",
    "empty_feed",
    "disabled",
]
"""SourceHealth.missed_reason short tokens. See DESIGN.md source_health.json."""


ClaimLocation = Literal["headline", "body"]
"""Where a verified claim was drawn from. Headline errors are the most severe
-- readers trust the headline first. Mirrors `src/verify.py::_VALID_LOCATIONS`."""

ClaimVerdictValue = Literal[
    "supported",
    "unsupported",
    "contradicted",
    "unverifiable",
]
"""Per-claim factual-accuracy verdict. Mirrors `src/verify.py::_VALID_VERDICTS`.

  - ``supported``     : the source excerpt corroborates the claim.
  - ``unsupported``   : the claim is absent from the source (not asserted).
  - ``contradicted``  : the source asserts something incompatible. MUST carry
                        a non-empty ``source_span`` (see the validator on
                        ``ClaimVerdict``); the verifier downgrades a
                        contradicted-without-span to ``unsupported`` before it
                        ever reaches this model.
  - ``unverifiable``  : nothing in the source to check against (empty/absent
                        excerpt).
"""

VerificationVerdict = Literal["clean", "flagged", "unavailable"]
"""Rollup verdict on `VerificationReport`. Mirrors the review.md tri-state +
unavailable pattern.

  - ``clean``       : ran, no claim flagged (no contradicted / unsupported).
  - ``flagged``     : ran, at least one claim flagged for attention.
  - ``unavailable`` : the verify stage could not run (LLM/transport failure,
                      missing inputs). ADVISORY-soft: this is a non-blocking
                      state, identical in spirit to review.md's
                      ``verdict: unavailable``. See DESIGN.md "verify.json".
"""

GatePhase = Literal["shadow", "green_only", "green_amber"]
"""Rollout phase of the unattended-publish gate, read from the
``AIV_AUTO_PUBLISH_PHASE`` environment variable (repo variable in CI).
Unset / unrecognised => ``shadow``. Ratified 2026-08-02.

  - ``shadow``      : the gate computes and writes its decision; the
                      workflow does not act on it. Observation only.
  - ``green_only``  : auto-merge is permitted only when the editorial
                      review verdict is ``green``.
  - ``green_amber`` : auto-merge is permitted when the review verdict is
                      ``green`` or ``amber`` -- the end-state policy.

The phase narrows WHICH review verdicts may auto-merge. It never widens
anything: the hard blocks (a contradicted fact-check claim, verify not
available, a stale review) hold in every phase, ``shadow`` included.
``shadow`` evaluates against the widest accepted set (``green`` +
``amber``) so the observation period measures the end-state policy; a
narrower phase is always derivable from the recorded review verdict.
"""

GateDecisionValue = Literal["auto_merge", "hold"]
"""The gate's answer. ``auto_merge`` == every blocking check passed under
the active phase; ``hold`` == at least one blocking check failed, and
``GateDecision.hold_reasons`` says which. There is no third state: a gate
that cannot decide holds (fail closed)."""


ReviewVerdictValue = Literal[
    "green",
    "amber",
    "red",
    "unavailable",
    "unparseable",
    "not_run",
]
"""The `review.json` / `review.md` verdict vocabulary. Mirrors
``review.REVIEW_VERDICTS`` -- the two must move together.

The first three are EDITORIAL judgements. Since the structured reviewer
(review prompt v1.0) they are computed BY CODE from the finding severities
under ``config/review_thresholds.yaml``; the model authors findings, never
the verdict. The last three are code-authored machine states meaning "no
judgement was recorded": ``unavailable`` (the review could not run),
``unparseable`` (it ran but its output could not be trusted), ``not_run``
(dry run). None of the three is an editorial pass -- a consumer that
authorises anything treats ``green`` (and only ``green``) as permission.
"""

ReviewTargetKind = Literal["story", "section", "digest"]
"""What a `ReviewFinding` is about: one story (a `SummaryBlock`), one
section's editorial framing (an `IssueSection` synthesis / legacy intro),
or one bullet of the issue-level digest (`Issue.digest`).

``digest`` added 2026-08-09 with `Issue.digest` (the 30-second read):
digest bullets are reader-facing assertive prose and must be reviewable
at the same field-level precision as story text."""

ReviewTargetField = Literal[
    "headline", "summary", "take",
    "intro_lead", "intro_body", "synthesis",
    "digest_lead", "digest_sentence",
]
"""The exact field a finding points at. ``headline`` / ``summary`` / ``take``
belong to story targets; ``intro_lead`` / ``intro_body`` / ``synthesis`` to
section targets; ``digest_lead`` / ``digest_sentence`` to digest targets
(enforced by the validator on `ReviewTarget`).

``take`` added 2026-08-08 with `SummaryBlock.take` (the publication's
position line): reader-facing assertive prose must be reviewable at the
same field-level precision as the headline and body.

``synthesis`` added 2026-08-09 with `IssueSection.synthesis` (the 2026-08-09
layout redesign merges the intro_lead/intro_body pair into one italic
synthesis paragraph). ``intro_lead`` / ``intro_body`` REMAIN in the
vocabulary so archived `review.json` records that targeted them still
parse; the reviewer prompt emits ``synthesis`` only, going forward.

``digest_lead`` / ``digest_sentence`` added 2026-08-09 with `DigestBullet`
(bold lead + one-sentence body of a 30-second-read bullet). The tokens are
prefixed -- the underlying model fields are `DigestBullet.lead` /
`DigestBullet.sentence` -- so the flat token vocabulary stays unambiguous
next to the section-intro fields.

Naming a FIELD rather than "the story" is what makes a finding actionable
without judgement: `revise.py` looks the field up, hands the model only
that text, and writes only that text back. This is also why the rendered
story TAG (the act/try/read/discuss/watch verb in the new layout) is NOT
in this vocabulary: the tag is derived by code at render time from
section + `SummaryBlock.signal` and has no text on disk a quote could
resolve against -- findings about the verb target the stored ``signal``
via ``fix_kind="metadata"`` instead."""

ReviewSeverity = Literal["blocking", "major", "minor", "note"]
"""How much a finding matters. Feeds the code-computed verdict via the
ratifiable threshold table in ``config/review_thresholds.yaml``.

  - ``blocking`` : do not publish as-is. Reserved for reputational /
                   liability exposure and factual claims the issue cannot
                   stand behind.
  - ``major``    : a substantive editorial defect a reader would notice.
  - ``minor``    : a real improvement, not a defect.
  - ``note``     : an observation; nothing to do today.
"""

ReviewFixKind = Literal[
    "text_edit",
    "structural",
    "sourcing",
    "metadata",
    "carry_forward",
    "human",
]
"""What KIND of action would resolve a finding. This is the routing key
between the reviewer and the revision engine: `src/revise.py` acts on
``text_edit`` and ONLY on ``text_edit``.

  - ``text_edit``     : rewriting the quoted text in place fixes it.
  - ``structural``    : needs a re-pick, a re-route, or a re-order --
                        a different story or a different section.
  - ``sourcing``      : needs another source, a link, or a corroboration.
  - ``metadata``      : signal pill, audience tags, section name.
  - ``carry_forward`` : nothing to do today; remember it tomorrow.
  - ``human``         : needs Arman's judgement, not an edit.
"""

RevisionStatus = Literal["proposed", "applied", "rejected"]
"""Lifecycle of one `RevisionChange`.

  - ``proposed`` : computed and validated, not written. The shadow-run
                   status.
  - ``applied``  : written into `issue.json`. Live runs only.
  - ``rejected`` : the code-side validation gate refused the replacement.
                   Reachable in BOTH modes -- a shadow run that would have
                   refused must say so, or it is not a preview of what live
                   would do.
"""

RevisionMode = Literal["shadow", "live"]
"""Whether a `RevisionCycle` wrote to `issue.json` (``live``) or only
recorded what it would have written (``shadow``)."""


# Patterns reused across models.
_CLUSTER_ID_PATTERN = r"^c_[0-9a-f]{12,}$"
_PROMPT_VERSION_PATTERN = r"^v\d+(\.\d+)*$"
_FINDING_ID_PATTERN = r"^f\d{3,}$"


# ---------------------------------------------------------------------------
# Rubric weights -- the weights from config/rubric.yaml, mirrored here so
# RankedStory's score-consistency validator can run without YAML I/O.
#
# DESIGN.md note: rubric weights are duplicated between config/rubric.yaml
# (source of truth, owned by LLM Engineer) and this module (used only by the
# RankedStory.score validator). If the YAML changes, this constant must move
# in lockstep -- the Eval Engineer's module-integrity check should catch
# drift, but a TODO is to load these from rubric.yaml at import time in a
# future refactor.
# ---------------------------------------------------------------------------

RUBRIC_WEIGHTS: dict[str, int] = {
    "significance": 40,
    "hands_on_utility": 10,
    "big_picture_relevance": 30,
    "financial_services_impact": 15,
    "freshness_momentum": 5,
}
"""Weights in [0, 100] that sum to 100. Sourced from config/rubric.yaml.
Renamed in v0.8 (2026-05-24): builder_utility -> hands_on_utility,
leadership_relevance -> big_picture_relevance, to align with section names.
Rebalanced v0.6 (2026-05-30): significance 30 -> 40, big_picture_relevance
20 -> 30, hands_on_utility 25 -> 10, freshness_momentum 10 -> 5. Editorial
shift: prioritise field-shifting stories and senior-leader strategic
framing over practitioner-actionability and pure recency.

LEGACY role (v0.7, 2026-05-31): these weights remain as the FALLBACK
single-aggregate score used by the back-compat validator on archived
ranked.jsonl rows (schema_version <= 5) and by the legacy ``score`` field
that RankedStory continues to expose for backwards compatibility.
SECTION_WEIGHTS (below) is the new routing authority for schema_version
>= 6 rows -- the per-section weighted sums on
``RankedStory.score_by_section`` drive tier assignment, story order, and
Pulse selection."""


SECTION_WEIGHTS: dict[str, dict[str, int]] = {
    "pulse": {
        "significance": 45,
        "big_picture_relevance": 20,
        "hands_on_utility": 5,
        "financial_services_impact": 20,
        "freshness_momentum": 10,
    },
    "big_picture": {
        "significance": 35,
        "big_picture_relevance": 45,
        "hands_on_utility": 0,
        "financial_services_impact": 15,
        "freshness_momentum": 5,
    },
    "hands_on": {
        "significance": 25,
        "big_picture_relevance": 10,
        "hands_on_utility": 45,
        "financial_services_impact": 10,
        "freshness_momentum": 10,
    },
    "currents": {
        "significance": 30,
        "big_picture_relevance": 20,
        "hands_on_utility": 15,
        "financial_services_impact": 15,
        "freshness_momentum": 20,
    },
}
"""Per-section weight sets (v0.7, 2026-05-31). Mirrors
``config/rubric.yaml:section_weights``. Each inner dict sums to 100.

DESIGN.md note: like ``RUBRIC_WEIGHTS``, this constant is duplicated
between ``config/rubric.yaml`` (source of truth, owned by LLM Engineer)
and this module (used by ``RankedStory._section_scores_match_weighted_breakdowns``
+ ``rank.py`` to populate ``score_by_section`` without YAML I/O on every
cluster). If the YAML changes, this constant must move in lockstep -- the
Eval Engineer's module-integrity check should catch drift, but a TODO is
to load both weight tables from rubric.yaml at import time in a future
refactor.

Keys MUST be exactly ``{"pulse", "big_picture", "hands_on", "currents"}`` --
these are the four sections the rendered newsletter has. Each inner dict's
keys MUST match the criterion names in ``RUBRIC_WEIGHTS`` exactly."""


# ---------------------------------------------------------------------------
# Item -- one raw entry from one source.
# ---------------------------------------------------------------------------

class Item(BaseModel):
    """
    One raw entry from one source.

    Produced by `src/fetch.py`. Consumed by `src/cluster.py`,
    `evals.run_evals`, and Release's provenance views.

    The smallest piece of provenance the rest of the pipeline trusts: a single
    entry, exact-URL deduped within the day's fetch but NOT yet clustered
    against near-duplicates from other sources.
    """

    schema_version: int = 1
    id: Annotated[str, Field(min_length=1, max_length=256)]
    """Stable per-source id (entry guid, atom id, API row id, or url-hash)."""

    source: Annotated[str, Field(min_length=1, max_length=128)]
    """Source name from `config/sources.yaml` (e.g. "anthropic_blog")."""

    source_type: SourceType
    """How this entry was fetched: rss | atom | api | html (fallback only)."""

    url: HttpUrl
    """Canonical URL to the original story."""

    title: Annotated[str, Field(min_length=1, max_length=512)]
    """Entry title, stripped, no HTML."""

    published_at: datetime
    """UTC timestamp from the feed; falls back to `fetched_at` if missing."""

    raw_summary: Annotated[str, Field(max_length=8000)]
    """Short summary as published; HTML stripped, length-capped at 8 KB."""

    fetched_at: datetime
    """UTC timestamp when this run pulled the entry."""

    trust_weight: Annotated[int, Field(ge=1, le=5)] = 3
    """Mirrored from `sources.yaml` at fetch time for traceability."""

    language: Annotated[str, Field(pattern=r"^[a-z]{2}(-[A-Z]{2})?$")] = "en"
    """ISO 639-1 (optional region), default "en"."""

    extras: dict[str, str] = Field(default_factory=dict)
    """
    Flat, string-only per-source payloads (e.g. HN points). Kept flat on
    purpose so JSONL lines stay cheap to parse.
    """

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Cluster -- a set of Items judged to be the same story.
# ---------------------------------------------------------------------------

class Cluster(BaseModel):
    """
    A set of Items judged to be the same story (within-day, plus cross-time
    linkage via `prior_coverage_ref`).

    Produced by `src/cluster.py`. Consumed by `src/rank.py`,
    `src/summarise.py`, and evals.

    Centroid vectors are stored externally (see DESIGN.md "Embedding model").
    `centroid_ref` is the sidecar filename inside
    `data/YYYY-MM-DD/embeddings/`; pydantic intentionally keeps the vector
    itself out of the JSONL line.
    """

    schema_version: int = 2
    cluster_id: Annotated[str, Field(pattern=_CLUSTER_ID_PATTERN)]
    """"c_" + 12+ hex chars. Stable per day."""

    item_ids: Annotated[list[str], Field(min_length=1)]
    """`Item.id` values that belong to this cluster."""

    canonical_title: Annotated[str, Field(min_length=1, max_length=512)]
    """Best-title pick from members (deterministic rule, not LLM)."""

    sources: Annotated[list[str], Field(min_length=1)]
    """Distinct `Item.source` values; order = first-seen."""

    earliest_published: datetime
    """min(Item.published_at) across members; UTC."""

    size: Annotated[int, Field(ge=1)]
    """`len(item_ids)` -- duplicated for fast reads without parsing the list."""

    prior_coverage_ref: Annotated[
        str | None,
        Field(
            default=None,
            pattern=_CLUSTER_ID_PATTERN,
            alias="cross_time_ref",
            validation_alias=AliasChoices("prior_coverage_ref", "cross_time_ref"),
        ),
    ] = None
    """
    Earliest `cluster_id` in the chain when this cluster has prior coverage
    (a prior-day cluster covered the same topic); `None` if new today.
    See DESIGN.md "Cross-time dedup contract".

    Schema v2 rename (task #88): formerly `cross_time_ref`. The old name
    conflated true continuations (new info worth showing) with effective
    duplicates (same story, repeated surface); the new name just says
    "this cluster has been covered before" without implying progression.
    Pydantic alias `cross_time_ref` keeps released archive files parseable.
    """

    canonical_id: str | None = None
    """
    Stable per-story identifier when the items in the cluster carry one
    (arxiv abs ID, GitHub release tag, DOI). Populated by
    `src.cluster._apply_canonical_id_rules` at clustering time -- the same
    string used to bucket items under canonical-ID rule A. ``None`` for
    free-text clusters (most Reddit posts, blog entries, news without a
    canonical artefact link).

    Downstream readers (e.g. `summarise._pick_pulse`'s eligibility gate)
    use this as a sourcing-credibility signal: a cluster with a non-null
    `canonical_id` references a verifiable artefact and clears the
    eligibility bar without needing multi-source corroboration.

    Backwards-compatible Optional field addition (default None): older
    `clusters.jsonl` records written before this field existed still parse
    cleanly. The schema version is NOT bumped because the field is
    additive-only with a safe default; existing readers that ignore the
    field continue to work unchanged.
    """

    embedding_dim: int | None = None
    """Length of the centroid vector if stored; `None` if vectors are external."""

    centroid_ref: str | None = None
    """
    Filename inside `data/YYYY-MM-DD/embeddings/` if vectors are stored
    separately; `None` if not stored.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _size_matches_item_ids(self) -> "Cluster":
        """Invariant: `size == len(item_ids)`. DESIGN.md duplicates the field
        for fast reads -- we enforce consistency at construction time."""
        if self.size != len(self.item_ids):
            raise ValueError(
                f"Cluster.size ({self.size}) must equal len(item_ids) "
                f"({len(self.item_ids)})"
            )
        return self


# ---------------------------------------------------------------------------
# RankedStory -- a scored cluster ready to write.
# ---------------------------------------------------------------------------

class RankedStory(BaseModel):
    """
    A scored cluster ready to write.

    Produced by `src/rank.py` (one LLM pass per cluster against
    `config/rubric.yaml`). Consumed by `src/summarise.py`, the Editor,
    Release archive views, and evals.

    Order in `ranked.jsonl` is significant: sorted by `score` descending.
    Downstream readers preserve that order.
    """

    schema_version: int = 6
    cluster_id: Annotated[str, Field(pattern=_CLUSTER_ID_PATTERN)]
    """FK to Cluster.cluster_id."""

    score: Annotated[int, Field(ge=0, le=100)]
    """Legacy single-aggregate weighted score (RUBRIC_WEIGHTS sum); kept
    for backwards compatibility and audit. For schema_version >= 5 it must
    equal the breakdown-weighted sum under ``RUBRIC_WEIGHTS``. Archived
    rows (schema_version <= 4) carry scores computed under earlier weights
    and are not re-validated on parse -- the score field is trusted as-
    written for legacy data.

    Schema v0.7 (2026-05-31): no longer the routing authority. Tier
    assignment, story order, and Pulse selection now use the per-section
    weighted sums on ``score_by_section`` (computed under SECTION_WEIGHTS)
    instead. The aggregate ``score`` continues to be computed so existing
    consumers (dedup, evals, render) that read it keep working unchanged,
    and so the v5 invariant remains enforceable on existing fixtures."""

    score_by_section: dict[str, Annotated[int, Field(ge=0, le=100)]] | None = None
    """
    Schema v0.7 (2026-05-31, schema_version=6): four per-section weighted
    sums (one per section) computed from ``breakdown`` x ``SECTION_WEIGHTS``,
    integer-rounded. Keys MUST be exactly
    ``{"pulse", "big_picture", "hands_on", "currents"}``.

    This field is the routing authority going forward: ``rank.py``
    ``_assign_initial_tier`` argmaxes across the three section-tier scores
    (big_picture / hands_on / currents) with a cut-floor; ``summarise.py``
    pickers rank within each section by that section's score; the Pulse
    picker uses ``score_by_section["pulse"]`` to rank the head-tier union.

    Optional / nullable for backwards compatibility: archived ranked.jsonl
    rows (schema_version <= 5) don't carry this field and parse fine as
    ``None``. The cross-check validator below
    (``_section_scores_match_weighted_breakdowns``) runs only when
    ``schema_version >= 6`` AND the field is present; v6 rows that omit
    ``score_by_section`` are tolerated for back-compat (e.g. unit-test
    fixtures that pre-date the field) but rank.py always populates it
    on fresh writes so production data is always consistent.
    """

    breakdown: dict[str, Annotated[int, Field(ge=0, le=100)]]
    """
    Per-criterion sub-scores; keys match `config/rubric.yaml` criterion
    names. Validated against `RUBRIC_WEIGHTS` keys -- the rubric may evolve,
    but rank.py and this module must move together.
    """

    audience_tags: Annotated[list[AudienceTag], Field(min_length=1)]
    """Who this is for; e.g. ["hands_on", "finance"]."""

    rationale: Annotated[str, Field(min_length=1, max_length=1000)]
    """One-line LLM rationale for transparency and eval."""

    tier: RankTier
    """
    Editorial slot assignment. LLM Engineer picks; Editor may relabel;
    `cut` means below threshold (excluded from the issue but kept in
    ranked.jsonl).
    """

    prompt_version: Annotated[str, Field(pattern=_PROMPT_VERSION_PATTERN)]
    """
    Version of the rank prompt that produced this row (e.g. "v1.2").
    Mandatory so the eval harness can correlate score movement against
    prompt revisions (risk-register item #6).
    """

    novelty: Literal["none", "minor", "major"] | None = None
    """
    Task #89: LLM-returned novelty assessment when the cluster has prior
    coverage. ``"none"`` => effective duplicate of a previously-published
    story; ``"minor"`` => incremental update; ``"major"`` => substantive
    new info. ``None`` when the cluster is fresh (no ``prior_coverage_ref``)
    OR when the LLM did not return a usable value -- the deterministic
    cap in ``rank._apply_prior_coverage_penalty`` defaults to the existing
    50 cap in that case.

    Schema v2 (2026-05-25): added so the eval harness can see which
    novelty branch fired per cluster. Backwards-compat: older ranked.jsonl
    rows (schema_version=1) parse cleanly with ``novelty=None`` via the
    default. The field is non-mandatory by design -- a missing value is
    semantically meaningful ("the prompt didn't ask, or the LLM didn't say").

    Schema v3 (2026-05-30): no shape change to ``novelty``; the version bump
    is driven by the ``tier`` value-space expansion (see RankTier). Released
    rows with ``schema_version=2`` and tier in {on_the_radar, cut} parse
    fine -- the value-space EXPANDED, didn't shrink.

    Schema v4 (Phase 2, 2026-05-30): tier value ``on_the_radar`` renamed to
    ``currents`` (see RankTier). The ``_coerce_legacy_tier`` validator below
    transparently maps ``"on_the_radar"`` -> ``"currents"`` at parse time so
    released v3 rows continue to load.
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_tier(cls, data: Any) -> Any:
        """Phase 2 (2026-05-30) backwards-compat alias: archived
        ``ranked.jsonl`` records carry ``tier="on_the_radar"``; coerce them
        to the new ``"currents"`` value at input time so existing v3 archive
        files parse without rewrite. Only fires when the input is a mapping;
        non-mapping inputs (model copies, partial fixtures) are passed
        through untouched."""
        if isinstance(data, dict) and data.get("tier") == "on_the_radar":
            data = {**data, "tier": "currents"}
        return data

    @field_validator("breakdown")
    @classmethod
    def _breakdown_keys_match_rubric(
        cls, v: dict[str, int]
    ) -> dict[str, int]:
        """
        DESIGN.md note: rubric.yaml keys are the source of truth, but
        RankedStory needs to validate at the boundary. We require exact
        key-set match against `RUBRIC_WEIGHTS` -- if you add a criterion
        to rubric.yaml, update `RUBRIC_WEIGHTS` here in the same PR. The
        Eval Engineer's module-integrity check catches drift if either
        side moves alone.
        """
        expected = set(RUBRIC_WEIGHTS.keys())
        got = set(v.keys())
        if got != expected:
            missing = expected - got
            extra = got - expected
            raise ValueError(
                "RankedStory.breakdown keys must match rubric.yaml criteria. "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )
        return v

    @model_validator(mode="after")
    def _score_matches_weighted_breakdown(self) -> "RankedStory":
        """
        Invariant per rubric.yaml `calibration_notes`:

            score = sum_c (weight_c / 100) * breakdown[c]

        DESIGN.md note: the LLM may return a `score` field that drifts from
        the weighted sum (arithmetic noise). rank.py is expected to RECOMPUTE
        the score from `breakdown` before constructing this model -- we
        enforce the invariant rather than silently accept LLM arithmetic.
        Tolerance: integer-rounded -- expected and provided must match
        exactly after rounding the weighted sum to the nearest int.

        Schema-version gate (v0.6 rebalance, 2026-05-30): the weighted-sum
        check enforces only for schema_version >= 5. Archived ranked.jsonl
        rows written before that bump carry scores computed under earlier
        RUBRIC_WEIGHTS and would fail the invariant. Trust the score field
        as-written for legacy rows -- the dedup, eval, and render paths
        that consume archived data should not be broken by a rubric tuning.

        Schema-version gate (v0.7, 2026-05-31): the same legacy-aggregate
        invariant still runs for schema_version >= 5. The new
        ``score_by_section`` invariant lives in
        ``_section_scores_match_weighted_breakdowns`` below and gates on
        schema_version >= 6.
        """
        if self.schema_version < 5:
            return self
        expected = sum(
            (RUBRIC_WEIGHTS[name] / 100.0) * value
            for name, value in self.breakdown.items()
        )
        expected_int = round(expected)
        if self.score != expected_int:
            raise ValueError(
                f"RankedStory.score ({self.score}) must equal the weighted "
                f"sum of breakdown ({expected_int}). breakdown={self.breakdown}"
            )
        return self

    @model_validator(mode="after")
    def _section_scores_match_weighted_breakdowns(self) -> "RankedStory":
        """
        Invariant (v0.7, schema_version >= 6): each entry in
        ``score_by_section`` equals the corresponding weighted sum of
        ``breakdown`` under ``SECTION_WEIGHTS[section]``, integer-rounded.

        Mirrors the existing aggregate-score invariant in shape:
        rank.py is expected to RECOMPUTE every section score from
        ``breakdown`` x section-weights before constructing this model;
        this validator enforces consistency rather than accepting LLM /
        caller arithmetic.

        Schema-version gate: archived rows with ``schema_version <= 5``
        do not carry ``score_by_section`` (None) and skip this check; new
        rows (schema_version >= 6) must carry the full four-section dict
        with values matching the breakdown.

        Tolerance: integer-rounded -- expected and provided must match
        exactly after rounding the weighted sum to the nearest int.
        """
        if self.schema_version < 6:
            return self
        if self.score_by_section is None:
            # Permitted at v6: legacy callers (existing tests, archived
            # fixtures that pre-date this field) construct RankedStory
            # without ``score_by_section``. The cross-check is enforced
            # when the field IS present so rank.py's writes stay
            # consistent with SECTION_WEIGHTS; absence is treated as
            # "this caller hasn't migrated yet, skip the check".
            return self
        expected_keys = set(SECTION_WEIGHTS.keys())
        got_keys = set(self.score_by_section.keys())
        if got_keys != expected_keys:
            missing = expected_keys - got_keys
            extra = got_keys - expected_keys
            raise ValueError(
                "RankedStory.score_by_section keys must match SECTION_WEIGHTS "
                f"(missing={sorted(missing)} extra={sorted(extra)})"
            )
        for section_name, section_weights in SECTION_WEIGHTS.items():
            expected = sum(
                (section_weights[crit] / 100.0) * self.breakdown[crit]
                for crit in section_weights
            )
            expected_int = round(expected)
            got = self.score_by_section[section_name]
            if got != expected_int:
                raise ValueError(
                    f"RankedStory.score_by_section[{section_name!r}] ({got}) "
                    f"must equal the weighted sum of breakdown "
                    f"({expected_int}) under SECTION_WEIGHTS[{section_name!r}]. "
                    f"breakdown={self.breakdown}"
                )
        return self


# ---------------------------------------------------------------------------
# SummaryBlock -- one written story inside an IssueSection.
# ---------------------------------------------------------------------------

class SummaryBlock(BaseModel):
    """
    One written story -- the inner unit of `IssueSection.stories`.

    Produced by `src/summarise.py`. Consumed by render, Editor, Arman, and by
    future-day `summarise.py` for callbacks.

    `story_id` equals the originating `Cluster.cluster_id` -- the canonical
    handle for a story across cluster/rank/summarise/render.

    Schema v3 (2026-06-29): added the optional nullable ``verification:
    StoryVerification | None = None`` field, populated by the advisory verify
    stage. Additive + nullable -- older issue.json files (schema_version <= 2)
    parse cleanly with ``verification=None``.

    Schema v4 (2026-08-08): added the optional nullable ``take: str | None =
    None`` field -- the publication's position line, one declarative italic
    sentence rendered last in the story unit. Additive + nullable -- every
    issue.json written before this date parses cleanly with ``take=None``.

    Schema v5 (2026-08-09): added the optional nullable ``take_route:
    Literal["R1","R2","R3"] | None = None`` field -- the take's generation
    route label. Additive + nullable -- every earlier issue.json parses
    cleanly with ``take_route=None``.
    """

    schema_version: int = 5
    story_id: Annotated[str, Field(pattern=_CLUSTER_ID_PATTERN)]
    """= Cluster.cluster_id; the canonical handle for a story."""

    headline: Annotated[str, Field(min_length=1, max_length=200)]
    """Editorial headline (LLM-written; may differ from canonical_title)."""

    summary: Annotated[str, Field(min_length=1, max_length=1200)]
    """
    The story body. Link out; never reproduce full articles.

    Schema v4 (2026-05-23): direction note and finance angle are now embedded
    in the summary prose *when relevant*, not surfaced as separate fields.
    They are philosophies of the newsletter (rhythm + lens), not labels.
    """

    take: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    """
    The take (schema v4, 2026-08-08): the publication's position on the
    story -- one declarative line, rendered last in the story unit, in
    italics. Editorially 8-16 words, hard cap 18; the 200-char structural
    cap here is deliberate headroom over that editorial limit.

    Pydantic enforces STRUCTURE only (stripped, non-empty if present,
    <= 200 chars). Form constraints -- word count, declarative mood, no
    trailing question mark -- are enforced by `summarise.py`'s code-side
    validation and judged by the reviewer, not here: they are editorial
    rules that move with the voice spec, not archive-integrity rules.

    Nullable: ``None`` means either a pre-take archive issue (everything
    written before 2026-08-08) or a story whose take was cut by summarise's
    body-cap collision ladder. Absence is normal, never an error.

    Integrity requirement (ratified 2026-08-08): the take is reader-facing
    assertive prose, so it MUST be visible to both the verify stage (claims
    drawn from it are checked like body claims) and the reviewer (findings
    may target ``field="take"`` -- see `ReviewTargetField`).
    """

    take_route: Literal["R1", "R2", "R3"] | None = None
    """
    The take's generation route (schema v5, 2026-08-09): which of the three
    rhetorical routes the summarise LLM declared the take runs on --
    ``R1`` (displacement), ``R2`` (named-owner consequence), ``R3``
    (reframe). A GENERATION judgment, not a derivable property of the text
    (architect ruling, wave two: derived-tag-at-render governs tags; routes
    are generation judgments and persist). Consumed by the eval harness's
    route-distribution counter and the take-drift lint.

    Nullable: ``None`` means a pre-route archive issue, a story with no
    take, or a take whose generation response omitted the route label
    (soft-fail -- the take still ships; only the label is absent). Never
    non-None when ``take`` is None in practice, but that pairing is a
    writer convention, not a validator: a stray label on a cut take is
    audit noise, not an archive-integrity risk.
    """

    source_urls: Annotated[list[HttpUrl], Field(min_length=1)]
    """Links to original sources; render attributes attribution."""

    prior_coverage_ref: Annotated[
        str | None,
        Field(
            default=None,
            pattern=_CLUSTER_ID_PATTERN,
            alias="cross_time_ref",
            validation_alias=AliasChoices("prior_coverage_ref", "cross_time_ref"),
        ),
    ] = None
    """
    Mirrored from `Cluster.prior_coverage_ref` so renderers do not need to
    re-join. Chain root, when set.

    Schema v2 rename (task #88): formerly `cross_time_ref`. Pydantic alias
    `cross_time_ref` keeps released archive issue.json files parseable.
    """

    signal: Signal | None = None
    """Editorial verdict pill (Phase B). LLM-tagged in summarise.py.
    Optional so pre-Phase-B archive issues still parse."""

    verification: "StoryVerification | None" = None
    """
    Denormalised factual-accuracy verdict for this story (schema v3,
    2026-06-29). Written by the **advisory** verify stage (`src/verify.py`
    ``verify_day``) AFTER summarise produced the block: verify reads the
    staged ``issue.json``, runs the calibrated verifier against the exact
    source excerpt summarise used (see DESIGN.md "source_excerpts.jsonl"),
    and rewrites each block's ``verification`` in place. The authoritative
    copy of the full report is the ``verify.json`` sidecar; this field is a
    convenience denormalisation so the renderer / editor loop can show a
    per-story flag without re-joining.

    Optional / nullable for backwards compatibility: every issue.json written
    before the verify stage existed (and any issue produced with verify
    skipped, or with an ``unavailable`` verdict) parses cleanly with
    ``verification=None``. ``None`` means "not verified" -- it is NOT a
    clean bill of health. The verify stage is advisory and never blocks
    release; absence of this field must never be read as a failure.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("take", mode="before")
    @classmethod
    def _strip_take(cls, v: Any) -> Any:
        """Strip surrounding whitespace before the length constraints run,
        so a whitespace-only take fails ``min_length=1`` (non-empty if
        present) instead of sneaking through as padding. A writer that wants
        "no take" says ``None`` explicitly; an empty or blank string is a
        writer bug and is rejected at the boundary."""
        if isinstance(v, str):
            return v.strip()
        return v


# ---------------------------------------------------------------------------
# IssueSection -- one section of the rendered issue.
# ---------------------------------------------------------------------------

class IssueSection(BaseModel):
    """
    One section of the rendered newsletter.

    Produced by `src/summarise.py` (as a child of Issue). Consumed by render,
    Editor, Arman.

    Per DESIGN.md, the per-section invariants are:
      - `pulse` must contain exactly 1 SummaryBlock.
      - `currents` may be empty on a slow day.

    Schema v4 (2026-05-23): direction-note enforcement removed; direction
    lives in summary prose now ("Where it's heading" is no longer a section,
    and Pulse carries its direction in its own summary).

    Schema v3 of IssueSection (Phase 2, 2026-05-30): section name
    ``on_the_radar`` renamed to ``currents``. The ``_coerce_legacy_name``
    validator below transparently maps the archived value at parse time so
    released v2 issue.json files continue to load.

    Schema v4 (2026-08-09 layout redesign): added the optional nullable
    ``synthesis`` field -- ONE italic paragraph framing the section,
    replacing the ``intro_lead`` + ``intro_body`` pair going forward. The
    legacy pair is RETAINED (not removed) so all released issue.json files
    parse unchanged; the ``_synthesis_excludes_legacy_intros`` validator
    enforces the migration direction (a draft carries the new field or the
    old pair, never both). The renderer prefers ``synthesis`` when present
    and falls back to joining the legacy pair for archived issues.
    """

    schema_version: int = 4
    name: SectionName
    """Which section this is."""

    stories: list[SummaryBlock]
    """May be empty for "on_the_radar" on a slow day; pulse must have exactly 1."""

    intro_lead: Annotated[str, Field(max_length=80)] | None = None
    """LEGACY (superseded by ``synthesis``, 2026-08-09): bold lead phrase
    rendered before the intro body. Phase B; LLM-written per section per day
    (e.g. "Bench before you budget."). None for the pulse section, for any
    pre-Phase-B issue, and for every issue written from schema v4 on --
    summarise (>= v0.23) writes ``synthesis`` instead. Kept so released
    archive files parse unchanged."""

    intro_body: Annotated[str, Field(max_length=400)] | None = None
    """LEGACY (superseded by ``synthesis``, 2026-08-09): one or two
    sentences framing the day's pattern in this section. Same lifecycle as
    ``intro_lead`` above."""

    synthesis: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    """
    Schema v4 (2026-08-09): one italic synthesis paragraph framing the
    section -- the merge of the former intro_lead ("Bench before you
    budget.") and intro_body into a single text unit, per the ratified
    layout redesign (the section head renders name, story-count kicker,
    then this paragraph in italics).

    Pydantic enforces STRUCTURE only: stripped, non-empty if present,
    <= 500 chars (headroom over the old pair's 80 + 400 caps; editorial
    word budget lives with the voice spec, not here -- same split as
    `SummaryBlock.take`). ``None`` for the pulse section (the Pulse IS the
    framing), for every pre-redesign issue, and on a degraded day where
    intro generation failed.

    Reviewable at field-level precision: section-target findings may name
    ``field="synthesis"`` (see `ReviewTargetField`), with the same
    verbatim-quote check and `revise.py` routing as the legacy pair.
    """

    model_config = ConfigDict(extra="forbid")

    @field_validator("synthesis", mode="before")
    @classmethod
    def _strip_synthesis(cls, v: Any) -> Any:
        """Strip before the length constraints run, so a whitespace-only
        synthesis fails ``min_length=1`` instead of passing as padding.
        Mirrors `SummaryBlock._strip_take`: "no synthesis" is ``None``,
        never an empty string."""
        if isinstance(v, str):
            return v.strip()
        return v

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_name(cls, data: Any) -> Any:
        """Phase 2 (2026-05-30) backwards-compat alias: archived
        ``issue.json`` records carry ``name="on_the_radar"``; coerce them to
        the new ``"currents"`` value at input time so released v2 archive
        files parse without rewrite. Only fires when the input is a mapping;
        non-mapping inputs (model copies, partial fixtures) pass through."""
        if isinstance(data, dict) and data.get("name") == "on_the_radar":
            data = {**data, "name": "currents"}
        return data

    @model_validator(mode="after")
    def _section_invariants(self) -> "IssueSection":
        """Pulse must have exactly 1 story; other sections may be empty."""
        if self.name == "pulse" and len(self.stories) != 1:
            raise ValueError(
                f"IssueSection(name='pulse') must contain exactly 1 story; "
                f"got {len(self.stories)}"
            )
        return self

    @model_validator(mode="after")
    def _synthesis_excludes_legacy_intros(self) -> "IssueSection":
        """Invariant (schema v4, 2026-08-09): ``synthesis`` and the legacy
        ``intro_lead``/``intro_body`` pair are mutually exclusive. Archived
        issues carry the pair (synthesis is None); new drafts carry
        synthesis (pair is None). A record carrying both has two competing
        framings for one render slot -- a writer bug -- and would leave the
        renderer's "prefer synthesis" rule silently discarding text a
        reviewer may have judged. Reject at the boundary."""
        if self.synthesis is not None and (
            self.intro_lead is not None or self.intro_body is not None
        ):
            raise ValueError(
                f"IssueSection(name={self.name!r}) carries synthesis AND a "
                "legacy intro_lead/intro_body; the pair is superseded -- a "
                "v4 writer sets synthesis only."
            )
        return self


# ---------------------------------------------------------------------------
# DigestBullet -- one bullet of the 30-second read.
# ---------------------------------------------------------------------------

class DigestBullet(BaseModel):
    """One bullet of the issue-level digest ("The 30-second read").

    Produced by `src/summarise.py` (new digest prompt, 2026-08-09 layout
    redesign). Consumed by render (the skim block above The Pulse:
    ``<p><strong>{lead}.</strong> <span>{sentence}</span></p>``), the
    reviewer, the verify stage, and evals.

    The bullet is stored as TWO fields, not one string, because the two
    units render into distinct HTML elements (bold lead vs. plain
    sentence) and are separately reviewable/revisable. Splitting one
    string on "the first period" at render time would be code parsing LLM
    prose -- exactly the fragility the structured contract exists to
    avoid.

    ``story_ids`` is the provenance contract: every reader-facing
    assertion must be attributable, and the verify stage judges the
    bullet's claims against the source excerpts of exactly these stories.
    ``story_ids[0]`` is the PRIMARY story -- the one whose
    `StoryVerification` carries the bullet's claim verdicts (see
    DESIGN.md "The digest").
    """

    schema_version: int = 1
    lead: Annotated[str, Field(min_length=1, max_length=80)]
    """The bold takeaway phrase (e.g. "Agents run locally now"). Stripped,
    non-empty. 80-char structural cap mirrors the old intro_lead budget;
    the editorial word budget lives with the voice spec."""

    sentence: Annotated[str, Field(min_length=1, max_length=300)]
    """The one plain-text sentence expanding the lead. Stripped, non-empty.
    ONE sentence is an editorial rule (summarise code-side validation +
    reviewer), not a pydantic rule -- same split as `SummaryBlock.take`."""

    story_ids: Annotated[
        list[Annotated[str, Field(pattern=_CLUSTER_ID_PATTERN)]],
        Field(min_length=1),
    ]
    """The `SummaryBlock.story_id`s this bullet condenses, primary first.
    Must resolve to stories present in the same Issue (enforced by
    `Issue._digest_story_ids_resolve`) -- a digest bullet about a story
    the issue does not carry is hallucinated provenance."""

    model_config = ConfigDict(extra="forbid")

    @field_validator("lead", "sentence", mode="before")
    @classmethod
    def _strip_text(cls, v: Any) -> Any:
        """Strip before length constraints run, so whitespace-only text
        fails ``min_length=1``. Mirrors `SummaryBlock._strip_take`."""
        if isinstance(v, str):
            return v.strip()
        return v


# ---------------------------------------------------------------------------
# Issue -- the full structured issue.
# ---------------------------------------------------------------------------

class Issue(BaseModel):
    """
    The full structured issue -- the top-level artifact written to
    `data/YYYY-MM-DD/issue.json`.

    Produced by `src/summarise.py`. Consumed by Editor, Arman (ratification),
    `src/render.py`, evals, and future-day `summarise.py` (for callbacks).

    schema_version=5 per DESIGN.md changelog: v2 added `issue_number`; v3
    made it Optional (None while in staging, assigned at release time); v4
    drops `direction_note` + `finance_angle` from SummaryBlock (now embedded
    in summary prose) and the `where_heading` section, and renames `builders`
    -> `geeks`. v5 adds `revision: int = 0` so a same-date re-release
    (e.g. a prompt fix re-shipped against an already-released date) bumps
    `revision` instead of burning a new integer issue number -- the
    rendered identifier becomes `#N.M` (e.g. `#2.1`, `#2.2`) when
    revision > 0. See "Archive: staging vs canonical" and "Issue Number
    Registry" in DESIGN.md for derivation, idempotency, gap behaviour,
    revision semantics, and the release transition.

    schema_version=6 (2026-06-29): no field change on `Issue` itself; the
    bump tracks the SummaryBlock v2->v3 change (added the optional
    ``verification`` field written by the advisory verify stage), since
    SummaryBlock is a transitive child of Issue and the issue.json envelope
    now carries the new shape. The optional ``"verify"`` key may now appear
    in ``prompt_versions``. Older issue.json files (schema_version <= 5)
    parse unchanged.

    schema_version=7 (2026-08-08): no field change on `Issue` itself; the
    bump tracks the SummaryBlock v3->v4 change (added the optional
    nullable ``take`` position line), same transitive-envelope rule as v6.
    Older issue.json files (schema_version <= 6) parse unchanged with
    ``take=None`` on every block.

    schema_version=8 (2026-08-09 layout redesign): added the optional
    nullable ``digest`` field (the 30-second read -- 3-5 `DigestBullet`s)
    and, transitively, the IssueSection v3->v4 change (optional nullable
    ``synthesis`` replacing the intro_lead/intro_body pair going forward).
    The optional ``"digest"`` key may now appear in ``prompt_versions``.
    Older issue.json files (schema_version <= 7) parse unchanged with
    ``digest=None`` and ``synthesis=None`` everywhere.

    schema_version=9 (2026-08-09): no field change on `Issue` itself; the
    bump tracks the SummaryBlock v4->v5 change (added the optional
    nullable ``take_route`` generation-route label), same
    transitive-envelope rule as v6/v7. Older issue.json files
    (schema_version <= 8) parse unchanged with ``take_route=None`` on
    every block.
    """

    schema_version: int = 9
    issue_number: Annotated[int, Field(ge=1)] | None = None
    """
    Sequential, 1-indexed, monotonically increasing across RELEASED
    (canonical) issues. None while in staging; assigned at release time.
    See DESIGN.md "Archive: staging vs canonical" -- staging issues live in
    `data/staging/YYYY-MM-DD/` with `issue_number = None`; on `--release`,
    `src/run.py` computes `max(canonical issue_numbers) + 1` (or 1 if none),
    writes the assigned number into `data/released/YYYY-MM-DD/issue.json`,
    and promotes the rest of the staging artifacts. A same-date
    re-release (opt-in via `aiv release --revise`) keeps `issue_number`
    constant and bumps `revision` instead -- see DESIGN.md "Issue Number
    Registry -> Same-date re-release (revision bump)".
    """

    revision: Annotated[int, Field(ge=0)] = 0
    """
    Same-date re-release counter. 0 on first release of a date; +1 each
    time `aiv release --revise` re-promotes a staging draft over an
    already-released date. Rendered as `#{issue_number}.{revision}` when
    revision > 0 (e.g. `#2.1`), else just `#{issue_number}` (`#2`).

    Backwards-compat: older issue.json files (schema_version <= 4) without
    this field load with `revision = 0` via the field default, which
    displays exactly as before. See DESIGN.md "Issue Number Registry ->
    Same-date re-release (revision bump)" for the full state model.

    Unrelease semantics: unreleasing a date removes the whole date dir,
    so a subsequent first release of that date starts back at
    `revision = 0`. The counter does not survive a full unrelease.
    """

    @property
    def display_number(self) -> str | None:
        """Human-facing identifier: `"2"` for first release, `"2.1"` for
        the first revision, `"2.2"` for the second, etc. None while
        staging (issue_number not yet assigned). The templates render
        this; the field-level `issue_number` is the integer registry key
        that all uniqueness + sort logic continues to use unchanged."""
        if self.issue_number is None:
            return None
        if self.revision == 0:
            return f"{self.issue_number}"
        return f"{self.issue_number}.{self.revision}"

    date: date
    """Issue date (YYYY-MM-DD); matches the archive folder."""

    pulse: IssueSection
    """The Pulse -- separate field (not just sections[0]) for type-level
    guarantee that exactly 1 block exists. IssueSection validator enforces
    that pulse holds exactly 1 SummaryBlock."""

    sections: list[IssueSection]
    """
    Remaining sections in display order: big_picture, hands_on, currents.
    Pulse lives in the dedicated `pulse` field above.
    """

    digest: Annotated[list[DigestBullet], Field(min_length=3, max_length=5)] | None = None
    """
    The 30-second read (schema v8, 2026-08-09): 3-5 bullets rendered above
    The Pulse, each a bold lead + one sentence. Reader-facing assertive
    prose -- both advisory stages see it (verify: claim-extraction input
    per bullet against its ``story_ids`` excerpts; review: findings target
    ``digest_lead`` / ``digest_sentence``). See DESIGN.md "The digest".

    Present => well-formed (3-5 bullets, every bullet's story_ids resolve
    to stories in this issue). ``None`` means either a pre-redesign
    archive issue (everything written before 2026-08-09) or a day where
    the digest prompt failed / produced fewer than 3 usable bullets -- the
    graceful-degradation path is NO digest section, never a degenerate
    one. Absence is normal and never an error, exactly like
    `SummaryBlock.take`.
    """

    generated_at: datetime
    """UTC timestamp when summarise.py wrote this Issue."""

    prompt_versions: dict[str, Annotated[str, Field(pattern=_PROMPT_VERSION_PATTERN)]]
    """
    Which prompt revisions produced this issue. Required keys: "rank",
    "summarise". Optional keys: "pulse", "callback", "digest" (set when the
    digest prompt produced `Issue.digest`; absent when the digest was
    skipped or degraded to None), and "verify" (set by the advisory verify
    stage to ``verify.VERIFY_PROMPT_VERSION`` when it runs; absent when
    verify was skipped or unavailable). Supports audit and A/B (risk
    register #6).
    """

    notes: Annotated[str, Field(max_length=2000)] = ""
    """
    Optional engine-side notes (e.g. "slow day; Currents tail shortened").
    Not rendered.
    """

    model_config = ConfigDict(extra="forbid")

    @field_validator("pulse")
    @classmethod
    def _pulse_section_must_be_pulse(cls, v: IssueSection) -> IssueSection:
        """The `pulse` field must hold an IssueSection whose name == "pulse"."""
        if v.name != "pulse":
            raise ValueError(
                f"Issue.pulse must be an IssueSection with name='pulse'; "
                f"got name='{v.name}'"
            )
        return v

    @field_validator("sections")
    @classmethod
    def _sections_must_not_be_pulse(cls, v: list[IssueSection]) -> list[IssueSection]:
        """
        `sections` holds the non-pulse sections. DESIGN.md note: pulse lives
        in its own field; including it again in `sections` would be a
        renderer trap.
        """
        for section in v:
            if section.name == "pulse":
                raise ValueError(
                    "Issue.sections must not contain a section with "
                    "name='pulse'; The Pulse lives in Issue.pulse instead"
                )
        # Allowed names that may appear in `sections`.
        # DESIGN.md note: order of sections is a renderer concern, not a
        # contract concern -- we do not require a specific order here.
        return v

    @field_validator("prompt_versions")
    @classmethod
    def _required_prompt_versions(
        cls, v: dict[str, str]
    ) -> dict[str, str]:
        """
        DESIGN.md note: `prompt_versions` must record "rank" and "summarise"
        at minimum. "pulse", "callback", and "verify" are optional (callbacks
        only fire on continuation chains; pulse may be folded into summarise
        depending on prompt structure; "verify" is set only when the advisory
        verify stage runs and is NOT required because verify must never block
        and may be skipped). Enforcing the minimum protects the audit
        invariant in risk-register item #6. Extra keys are tolerated -- this
        check only asserts the required subset is present.
        """
        required = {"rank", "summarise"}
        missing = required - set(v.keys())
        if missing:
            raise ValueError(
                f"Issue.prompt_versions must include keys {sorted(required)}; "
                f"missing={sorted(missing)}"
            )
        return v

    @model_validator(mode="after")
    def _digest_story_ids_resolve(self) -> "Issue":
        """Invariant (schema v8, 2026-08-09): every digest bullet's
        ``story_ids`` resolve to stories carried by THIS issue (pulse +
        sections). A bullet citing a story the issue does not contain is
        hallucinated provenance -- the verify stage could not fetch an
        excerpt for it and the reader could not find the story it
        summarises. Reject at the boundary, like every other
        cross-field-consistency rule in this module."""
        if self.digest is None:
            return self
        known = {block.story_id for block in self.pulse.stories}
        for section in self.sections:
            known.update(block.story_id for block in section.stories)
        for index, bullet in enumerate(self.digest):
            unknown = [sid for sid in bullet.story_ids if sid not in known]
            if unknown:
                raise ValueError(
                    f"Issue.digest[{index}] cites story_ids not present in "
                    f"this issue: {unknown}. Digest provenance must resolve "
                    "to the issue's own stories."
                )
        return self


# ---------------------------------------------------------------------------
# SourceHealth -- one row per source per run.
# ---------------------------------------------------------------------------

class SourceHealth(BaseModel):
    """
    Per-source health for a single fetch run.

    Produced by `src/fetch.py` (one per configured source per run, embedded
    in `source_health.json`). Consumed by evals (module-integrity, dead-feed
    surfacing), render (optional "sources fired today" footer), and Source
    Engineer (trust-weight decay).
    """

    schema_version: int = 1
    source: Annotated[str, Field(min_length=1, max_length=128)]
    """Matches `Item.source` and `sources.yaml` name."""

    fired: bool
    """True if the fetch attempt completed (regardless of items returned)."""

    items_in: Annotated[int, Field(ge=0)]
    """Raw entries seen on the wire."""

    items_kept: Annotated[int, Field(ge=0)]
    """Entries kept after exact-URL dedup + filters."""

    latency_ms: Annotated[int, Field(ge=0)]
    """Wall-clock for this source in milliseconds."""

    last_modified: datetime | None = None
    """HTTP Last-Modified or feed-updated timestamp; UTC. `None` if absent."""

    missed_reason: MissedReason | None = None
    """
    Short token explaining a miss when `fired=False` (or a partial). `None`
    on healthy runs.
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _kept_le_in(self) -> "SourceHealth":
        """Invariant: items_kept <= items_in. A keeper can't out-count what
        we saw."""
        if self.items_kept > self.items_in:
            raise ValueError(
                f"SourceHealth.items_kept ({self.items_kept}) must be <= "
                f"items_in ({self.items_in}) for source={self.source!r}"
            )
        return self

    @model_validator(mode="after")
    def _missed_reason_consistent_with_fired(self) -> "SourceHealth":
        """
        DESIGN.md note: the spec defines `missed_reason` as "short token for
        a miss." If a source did not fire, we require a missed_reason; if
        it did fire and returned items, missed_reason should be None. We
        allow `fired=True` with `missed_reason` set for the partial case
        (e.g. parse_error on some entries) -- that case is rare; the writer
        is responsible for choosing the right shape.
        """
        if not self.fired and self.missed_reason is None:
            raise ValueError(
                f"SourceHealth.missed_reason is required when fired=False "
                f"(source={self.source!r})"
            )
        return self


# ---------------------------------------------------------------------------
# SourceHealthReport -- the top-level object written to source_health.json.
# ---------------------------------------------------------------------------

class SourceHealthReport(BaseModel):
    """
    The top-level object serialised to `data/YYYY-MM-DD/source_health.json`.

    Produced by `src/fetch.py`. Consumed by evals (module-integrity) and
    render (optional footer).

    DESIGN.md note: DESIGN.md describes the source_health.json schema as a
    single JSON object with `schema_version`, `run_started_at`,
    `run_finished_at`, and a `sources: list[SourceHealth]`. We model that
    explicitly here so the contract is one symbol, not "an envelope shape
    described in prose."
    """

    schema_version: int = 1
    run_started_at: datetime
    """UTC timestamp when the fetch run started."""

    run_finished_at: datetime
    """UTC timestamp when the fetch run finished."""

    sources: list[SourceHealth]
    """One SourceHealth per configured source. May be empty on a no-op run."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _finish_after_start(self) -> "SourceHealthReport":
        """Invariant: run_finished_at >= run_started_at."""
        if self.run_finished_at < self.run_started_at:
            raise ValueError(
                f"SourceHealthReport.run_finished_at ({self.run_finished_at}) "
                f"must be >= run_started_at ({self.run_started_at})"
            )
        return self


# ---------------------------------------------------------------------------
# ClaimVerdict / StoryVerification -- the factual-accuracy verifier output.
#
# Promoted from the `ClaimVerdict` dataclass + the NOTE-FOR-THE-ARCHITECT
# block in `src/verify.py` (verify-v0.x). The verifier produces these; the
# advisory verify stage persists them. See DESIGN.md "verify.json" and the
# `verify_day` stage contract.
# ---------------------------------------------------------------------------

class ClaimVerdict(BaseModel):
    """One atomic factual claim drawn from a story and its verifier verdict.

    Produced by `src/verify.py` (one per atomic claim a story decomposes into).
    Consumed by render (per-story factual flag), the Editor loop, and evals.

    The verifier (`verify.py`) owns the JUDGMENT; this model owns the SHAPE.
    A ``contradicted`` verdict is only meaningful with the contradicting quote
    attached, so the validator below rejects ``contradicted`` with an empty
    ``source_span`` -- mirroring `verify._enforce_contradiction_discipline`,
    which downgrades such a verdict to ``unsupported`` before construction.
    The model is the second, hard line of defence on that invariant.
    """

    schema_version: int = 1
    claim: Annotated[str, Field(min_length=1, max_length=1000)]
    """The atomic factual assertion, as a near-verbatim span of the
    headline or body."""

    verdict: ClaimVerdictValue
    """supported | unsupported | contradicted | unverifiable."""

    location: ClaimLocation
    """headline | body -- where the claim was drawn from."""

    summary_span: Annotated[str, Field(max_length=1000)] = ""
    """The exact summary (headline/body) text carrying the claim. May be
    empty when the verifier did not anchor a verbatim span."""

    source_span: Annotated[str, Field(max_length=2000)] = ""
    """The supporting OR contradicting span quoted from the source excerpt.
    Empty for ``unverifiable`` (nothing to quote) and may be empty for
    ``unsupported`` (the fact is absent by definition). REQUIRED (non-empty)
    when ``verdict == "contradicted"`` -- see the validator below."""

    note: Annotated[str, Field(max_length=1000)] = ""
    """One-line rationale -- feeds the audit trail and the eval's
    transparency promise."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _contradicted_requires_source_span(self) -> "ClaimVerdict":
        """Invariant: a ``contradicted`` verdict MUST carry a non-empty
        ``source_span``. A contradiction asserts the source says something
        incompatible -- without the quote there is no contradiction to show,
        only an unsupported claim. `verify.py` enforces the same rule
        deterministically (downgrading to ``unsupported``); this model is the
        contract-level backstop so a hand-authored or mis-serialised record
        cannot smuggle a span-less contradiction into the archive."""
        if self.verdict == "contradicted" and not self.source_span.strip():
            raise ValueError(
                "ClaimVerdict with verdict='contradicted' must carry a "
                "non-empty source_span (the contradicting quote)."
            )
        return self


class StoryVerification(BaseModel):
    """The factual-accuracy verdict for one story (one SummaryBlock).

    Produced by `src/verify.py` (`verify_day`). Stored both as a member of
    `VerificationReport.stories` (the authoritative sidecar) and denormalised
    onto `SummaryBlock.verification` (convenience for render / editor).

    The ``has_*`` / ``headline_flagged`` rollups are convenience booleans the
    renderer and editor loop read without re-scanning ``claims``. The
    validator keeps them honest so a writer cannot ship a rollup that
    disagrees with the claim list.
    """

    schema_version: int = 1
    story_id: Annotated[str, Field(pattern=_CLUSTER_ID_PATTERN)]
    """= SummaryBlock.story_id = Cluster.cluster_id; the story verified."""

    prompt_version: Annotated[str, Field(pattern=_PROMPT_VERSION_PATTERN)]
    """== `verify.VERIFY_PROMPT_VERSION` at the time of verification.
    Lets the eval harness correlate verdict movement against prompt revisions
    (risk-register item #6), exactly as rank/summarise prompt versions do."""

    claims: list[ClaimVerdict]
    """The per-claim verdicts. May be empty when the story decomposed into no
    checkable atomic claims (rare)."""

    has_contradiction: bool
    """True iff any claim has ``verdict == "contradicted"``."""

    has_unsupported: bool
    """True iff any claim has ``verdict == "unsupported"``."""

    headline_flagged: bool
    """True iff any ``location == "headline"`` claim is flagged
    (``contradicted`` or ``unsupported``). The most severe class of finding --
    readers trust the headline first."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _rollups_match_claims(self) -> "StoryVerification":
        """Invariant: the rollup booleans agree with ``claims``. A writer that
        sets a rollup inconsistent with the claim list is buggy; we reject it
        at the boundary rather than letting render/editor trust a stale flag."""
        flagged = {"contradicted", "unsupported"}
        has_contra = any(c.verdict == "contradicted" for c in self.claims)
        has_unsup = any(c.verdict == "unsupported" for c in self.claims)
        headline_flag = any(
            c.location == "headline" and c.verdict in flagged for c in self.claims
        )
        if self.has_contradiction != has_contra:
            raise ValueError(
                f"StoryVerification.has_contradiction ({self.has_contradiction}) "
                f"disagrees with claims ({has_contra})."
            )
        if self.has_unsupported != has_unsup:
            raise ValueError(
                f"StoryVerification.has_unsupported ({self.has_unsupported}) "
                f"disagrees with claims ({has_unsup})."
            )
        if self.headline_flagged != headline_flag:
            raise ValueError(
                f"StoryVerification.headline_flagged ({self.headline_flagged}) "
                f"disagrees with claims ({headline_flag})."
            )
        return self


class VerificationReport(BaseModel):
    """The top-level object serialised to `data/<date>/verify.json`.

    Produced by `src/verify.py` (`verify_day`). Consumed by render (per-story
    flags + an optional issue-level banner), the Editor loop, and evals.

    Mirrors the `SourceHealthReport` envelope pattern: a versioned top-level
    object with a generated-at timestamp and a list of per-unit records. The
    ``verdict`` field carries the advisory tri-state + ``unavailable`` so a
    reader can branch on "did verify run, and did it find anything" without
    scanning every story.

    ADVISORY guarantee: on any verifier/transport failure (or missing inputs),
    `verify_day` writes this report with ``verdict="unavailable"`` and returns
    normally. The verify stage NEVER blocks release -- identical to the
    review.md ``verdict: unavailable`` contract. See DESIGN.md "verify.json".
    """

    schema_version: int = 1
    generated_at: datetime
    """UTC timestamp when the verify stage wrote this report."""

    prompt_version: Annotated[str, Field(pattern=_PROMPT_VERSION_PATTERN)]
    """== `verify.VERIFY_PROMPT_VERSION`. On an ``unavailable`` run this still
    records the version the stage WOULD have used, for audit continuity."""

    verdict: VerificationVerdict
    """clean | flagged | unavailable -- the advisory rollup. See
    `VerificationVerdict`. ``unavailable`` means the stage could not run; an
    ``unavailable`` report carries an empty ``stories`` list."""

    verdict_counts: dict[ClaimVerdictValue, Annotated[int, Field(ge=0)]] = Field(
        default_factory=dict
    )
    """Per-verdict claim tallies across all stories (e.g.
    ``{"supported": 31, "unsupported": 2, "contradicted": 0,
    "unverifiable": 4}``). A cheap at-a-glance distribution for the eval
    harness and render banner; empty on an ``unavailable`` run."""

    stories: list[StoryVerification]
    """One StoryVerification per verified SummaryBlock (Pulse + all sections).
    Empty when ``verdict == "unavailable"`` or when the issue had no stories."""

    note: Annotated[str, Field(max_length=2000)] = ""
    """Free-text engine note. On an ``unavailable`` run this carries the
    failure reason (transport error, missing source_excerpts sidecar, etc.),
    mirroring the review.md unavailable-body convention."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _unavailable_has_no_stories(self) -> "VerificationReport":
        """Invariant: an ``unavailable`` report carries no stories and no
        verdict counts -- the stage did not run, so there is nothing to
        report. Keeps the advisory failure state unambiguous for readers."""
        if self.verdict == "unavailable" and self.stories:
            raise ValueError(
                "VerificationReport.verdict='unavailable' must carry an empty "
                f"stories list; got {len(self.stories)}."
            )
        return self


# ---------------------------------------------------------------------------
# GateCheck / GateDecision -- the unattended-publish gate output.
#
# Produced by `src/gate.py` (`decide`), persisted as the `gate.json` sidecar.
# The gate is the ONLY artifact in the pipeline that is allowed to say "do
# not publish this without a human": every other advisory stage (verify,
# review) is explicitly non-blocking. See DESIGN.md "Unattended publish".
# ---------------------------------------------------------------------------

class GateCheck(BaseModel):
    """One policy check the gate ran, and what it found.

    The gate is deterministic code reading advisory artifacts -- No Token
    Wasted: there is no LLM call anywhere in `src/gate.py`. Each check is
    recorded whether it passed or failed so `gate.json` reads as a full
    audit trail rather than a list of complaints.

    ``blocking`` is a property of the CHECK, not of the outcome: a
    non-blocking check that fails is surfaced for the operator (e.g. the
    verifier flagged an unsupported claim) but does not by itself force a
    hold. A blocking check that fails contributes its ``hold_reason`` to
    ``GateDecision.hold_reasons`` and forces ``decision="hold"``.
    """

    schema_version: int = 1
    name: Annotated[str, Field(min_length=1, max_length=64)]
    """Stable machine token for the check, e.g. ``"review_verdict"``.
    Workflows and dashboards key on this; treat renames as contract changes."""

    passed: bool
    """True when the check found what it required."""

    blocking: bool
    """True when a failure of this check forces ``decision="hold"``."""

    detail: Annotated[str, Field(max_length=1000)] = ""
    """One-line human-readable finding. Written for the operator reading the
    workflow log at 06:00, not for a parser."""

    hold_reason: Annotated[str | None, Field(max_length=64)] = None
    """The ``hold:<token>`` this check contributes when it fails while
    blocking. ``None`` on a passing check and on non-blocking checks."""

    model_config = ConfigDict(extra="forbid")

    @field_validator("detail", mode="before")
    @classmethod
    def _truncate_detail(cls, v: Any) -> Any:
        """Truncate rather than reject an over-long detail.

        ``detail`` is prose for a human reading a workflow log. Its length
        must never be the reason the gate fails to produce a decision -- a
        gate that raises instead of deciding is strictly worse than one that
        decides with an elided sentence. This is the single truncation
        mechanism; callers construct `GateCheck` without pre-slicing.
        """
        if isinstance(v, str) and len(v) > 1000:
            return v[:997] + "..."
        return v

    @model_validator(mode="after")
    def _hold_reason_consistency(self) -> "GateCheck":
        """Invariant: a failed blocking check MUST name its hold reason, and
        a passing check MUST NOT carry one. Without this, `hold_reasons`
        could silently disagree with `checks` and the artifact would stop
        being an audit trail."""
        if self.passed and self.hold_reason is not None:
            raise ValueError(
                f"GateCheck({self.name!r}) passed but carries "
                f"hold_reason={self.hold_reason!r}; passing checks contribute "
                "no hold reason."
            )
        if not self.passed and self.blocking and not self.hold_reason:
            raise ValueError(
                f"GateCheck({self.name!r}) failed as a blocking check but "
                "carries no hold_reason; a block must say why."
            )
        return self


class GateReviewState(BaseModel):
    """What the gate observed about `review.md` for the date.

    Denormalised out of the review artifact so a reader of `gate.json` can
    reconstruct the decision without re-parsing Markdown frontmatter.
    """

    schema_version: int = 1
    present: bool
    """True when `data/staging/<date>/review.md` exists and was readable."""

    verdict: Annotated[str | None, Field(max_length=32)] = None
    """The frontmatter ``verdict`` as written: ``green | amber | red |
    unavailable``, any unrecognised token verbatim, or ``None`` when the
    file was missing or carried no parseable frontmatter."""

    issue_sha256: Annotated[str | None, Field(max_length=64)] = None
    """The ``issue_sha256`` recorded in the review frontmatter -- the hash of
    the `issue.json` the editor actually read. ``None`` when the key is
    absent (a review written before the freshness contract, or by a writer
    that does not honour it)."""

    computed_issue_sha256: Annotated[str | None, Field(max_length=64)] = None
    """SHA-256 of the CURRENT staged `issue.json` bytes, recomputed by the
    gate. ``None`` when `issue.json` is missing."""

    fresh: bool = False
    """True iff ``issue_sha256`` is present and equals
    ``computed_issue_sha256``. A review of a superseded issue is not
    evidence about the issue we are about to publish."""

    prompt_version: Annotated[str | None, Field(max_length=32)] = None
    """The review prompt version from the frontmatter, for audit correlation.
    Free-form (not pattern-validated) because the gate must never fail on a
    malformed advisory artifact -- it holds instead."""

    path: Annotated[str | None, Field(max_length=512)] = None
    """Path the gate read (or would have read), for the operator's benefit."""

    model_config = ConfigDict(extra="forbid")


class GateVerifyState(BaseModel):
    """What the gate observed about `verify.json` for the date.

    The contradiction scan is the load-bearing part: ratified 2026-08-02,
    ANY contradicted claim hard-blocks unattended publish regardless of the
    editorial verdict.
    """

    schema_version: int = 1
    present: bool
    """True when `data/staging/<date>/verify.json` exists and parsed."""

    verdict: Annotated[str | None, Field(max_length=32)] = None
    """``clean | flagged | unavailable``, or ``None`` when the file was
    missing or unparseable."""

    stories_verified: Annotated[int, Field(ge=0)] = 0
    """Count of `StoryVerification` records in the report."""

    contradicted_story_ids: list[str] = Field(default_factory=list)
    """Story ids carrying at least one ``contradicted`` claim, from
    `verify.json` and from the denormalised `SummaryBlock.verification`
    copies in `issue.json` (union -- either source is sufficient evidence)."""

    unsupported_story_ids: list[str] = Field(default_factory=list)
    """Story ids carrying at least one ``unsupported`` claim. Surfaced but
    NOT blocking: "the source does not assert this" is an editorial concern,
    not a factual error."""

    path: Annotated[str | None, Field(max_length=512)] = None
    """Path the gate read (or would have read)."""

    model_config = ConfigDict(extra="forbid")


class GateDecision(BaseModel):
    """The top-level object serialised to `data/<date>/gate.json`.

    Produced by `src/gate.py` (`decide`). Consumed by the unattended-publish
    workflow (which decides whether to act on it, per `phase`), by
    `render.release_promote` (promoted to `data/released/<date>/gate.json`
    as part of the published audit trail), by evals, and by Arman.

    The decision lives in this ARTIFACT, never in a process exit code:
    `aiv gate` exits 0 whether it decided ``auto_merge`` or ``hold``, so an
    orchestration failure (non-zero exit) stays distinguishable from a
    policy hold. A workflow that cannot read `gate.json` must treat the
    absence as a hold.
    """

    schema_version: int = 1
    gate_version: Annotated[str, Field(pattern=_PROMPT_VERSION_PATTERN)]
    """Version of the POLICY that produced this decision (== `gate.GATE_VERSION`),
    e.g. ``"v1"``. Distinct from ``schema_version`` (the shape): the policy can
    tighten without the shape moving, and a shape change need not re-open the
    policy. Bump when a check is added, removed, or changes its blocking flag."""

    phase: GatePhase
    """Rollout phase in force when this decision was computed. See `GatePhase`."""

    date: date
    """Issue date this decision is about; matches the archive folder."""

    decision: GateDecisionValue
    """``auto_merge`` | ``hold``. See `GateDecisionValue`."""

    hold_reasons: list[Annotated[str, Field(max_length=64)]] = Field(
        default_factory=list
    )
    """Ordered, de-duplicated ``hold:<token>`` list -- exactly the
    ``hold_reason`` of every failed blocking check, in check order. Empty iff
    ``decision == "auto_merge"`` (enforced below)."""

    review: GateReviewState
    """What the gate saw in `review.md`."""

    verify: GateVerifyState
    """What the gate saw in `verify.json` (+ the denormalised copies in
    `issue.json`)."""

    checks: list[GateCheck]
    """Every check the gate ran, in evaluation order -- passes included."""

    decided_at: datetime
    """UTC timestamp when the gate computed this decision."""

    note: Annotated[str, Field(max_length=2000)] = ""
    """Free-text engine note (e.g. an unrecognised phase value that fell back
    to ``shadow``). Not rendered to readers."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _decision_matches_checks(self) -> "GateDecision":
        """Invariant: the headline ``decision`` and ``hold_reasons`` agree with
        ``checks``. The whole value of this artifact is that a reader can trust
        the one-word answer without re-deriving it; a writer that ships a
        disagreeing rollup is buggy and we reject it at the boundary --
        exactly as `StoryVerification` does with its rollups."""
        expected_reasons = [
            c.hold_reason for c in self.checks
            if not c.passed and c.blocking and c.hold_reason
        ]
        # De-duplicate, preserving first-seen order.
        deduped: list[str] = []
        for reason in expected_reasons:
            if reason not in deduped:
                deduped.append(reason)
        if self.hold_reasons != deduped:
            raise ValueError(
                f"GateDecision.hold_reasons {self.hold_reasons} disagrees with "
                f"the failed blocking checks {deduped}."
            )
        expected_decision = "hold" if deduped else "auto_merge"
        if self.decision != expected_decision:
            raise ValueError(
                f"GateDecision.decision ({self.decision!r}) disagrees with the "
                f"checks (expected {expected_decision!r}; failed blocking "
                f"checks: {deduped})."
            )
        return self


# ---------------------------------------------------------------------------
# ReviewFinding / ReviewReport -- the structured editorial reviewer output.
#
# Produced by `src/review.py` (`run_review`), persisted as the `review.json`
# sidecar; `review.md` is RENDERED FROM this object by the same module, so
# the human-readable artifact and the machine-readable one cannot disagree.
#
# The shape exists to make the review ACTIONABLE. A prose verdict can only
# be read; a finding that names a target field, quotes the offending text
# verbatim, and states what kind of fix it needs can be routed -- to
# `src/revise.py` (text edits), to a re-pick, or to Arman. See DESIGN.md
# "review.json".
# ---------------------------------------------------------------------------

class ReviewTarget(BaseModel):
    """What one `ReviewFinding` is about, resolved to a single editable field.

    The field-level precision is the point. "The Hands-On section reads
    flat" is an opinion nobody can act on deterministically; "the
    ``intro_lead`` of the ``hands_on`` section" is a string on disk that a
    reviewer quoted from and a reviser can rewrite.

    Schema v2 (2026-08-08): story targets may now name ``field="take"``
    (the `SummaryBlock.take` position line). Value-space widening only; v1
    records parse unchanged.

    Schema v3 (2026-08-09): section targets may now name
    ``field="synthesis"`` (the merged section paragraph), and a new
    ``kind="digest"`` targets one bullet of `Issue.digest` via the
    ``digest_index`` locator with ``field`` in {``digest_lead``,
    ``digest_sentence``}. Widening only; v1/v2 records parse unchanged
    (``digest_index`` defaults to None).
    """

    schema_version: int = 3
    kind: ReviewTargetKind
    """``story`` (a SummaryBlock), ``section`` (an IssueSection
    synthesis / legacy intro), or ``digest`` (one `DigestBullet`)."""

    story_id: Annotated[
        str | None, Field(default=None, pattern=_CLUSTER_ID_PATTERN)
    ] = None
    """= `SummaryBlock.story_id` = `Cluster.cluster_id`. REQUIRED when
    ``kind == "story"``; MUST be None otherwise (a section intro or digest
    bullet belongs to no single story -- digest provenance lives on
    `DigestBullet.story_ids`, not here)."""

    section: SectionName | None = None
    """Which section the target lives in. REQUIRED when
    ``kind == "section"``; MUST be None when ``kind == "digest"`` (the
    digest is issue-level). Optional-but-useful on a story target: it lets
    the renderer group findings by section without re-joining `issue.json`."""

    digest_index: Annotated[int, Field(ge=0)] | None = None
    """0-based index into `Issue.digest`. REQUIRED when
    ``kind == "digest"``; MUST be None otherwise. An index, not an id,
    because bullets have no stable identifier and the digest is small and
    ordered -- the same issue_sha256 freshness check that protects every
    other finding protects the index from drifting under an edit."""

    field: ReviewTargetField
    """The exact field. ``headline`` / ``summary`` / ``take`` for story
    targets; ``intro_lead`` / ``intro_body`` / ``synthesis`` for section
    targets; ``digest_lead`` / ``digest_sentence`` for digest targets."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _kind_matches_locator_and_field(self) -> "ReviewTarget":
        """Invariant: the locator fields and the field name agree with
        ``kind``. A story finding that names no story, or a section finding
        that points at ``headline``, cannot be resolved to text on disk --
        and an unresolvable finding is exactly the misfire class this whole
        shape exists to eliminate. Reject it at the boundary."""
        if self.kind == "story":
            if not self.story_id:
                raise ValueError(
                    "ReviewTarget(kind='story') requires story_id."
                )
            if self.digest_index is not None:
                raise ValueError(
                    "ReviewTarget(kind='story') must not carry a "
                    f"digest_index; got {self.digest_index!r}."
                )
            if self.field not in ("headline", "summary", "take"):
                raise ValueError(
                    f"ReviewTarget(kind='story') field must be 'headline', "
                    f"'summary', or 'take'; got {self.field!r}."
                )
        elif self.kind == "section":
            if self.story_id is not None:
                raise ValueError(
                    "ReviewTarget(kind='section') must not carry a story_id; "
                    f"got {self.story_id!r}."
                )
            if self.digest_index is not None:
                raise ValueError(
                    "ReviewTarget(kind='section') must not carry a "
                    f"digest_index; got {self.digest_index!r}."
                )
            if self.section is None:
                raise ValueError(
                    "ReviewTarget(kind='section') requires section."
                )
            if self.field not in ("intro_lead", "intro_body", "synthesis"):
                raise ValueError(
                    f"ReviewTarget(kind='section') field must be "
                    f"'intro_lead', 'intro_body', or 'synthesis'; got "
                    f"{self.field!r}."
                )
        else:  # digest
            if self.story_id is not None:
                raise ValueError(
                    "ReviewTarget(kind='digest') must not carry a story_id; "
                    f"got {self.story_id!r}. Digest provenance lives on "
                    "DigestBullet.story_ids."
                )
            if self.section is not None:
                raise ValueError(
                    "ReviewTarget(kind='digest') must not carry a section; "
                    f"got {self.section!r}. The digest is issue-level."
                )
            if self.digest_index is None:
                raise ValueError(
                    "ReviewTarget(kind='digest') requires digest_index."
                )
            if self.field not in ("digest_lead", "digest_sentence"):
                raise ValueError(
                    f"ReviewTarget(kind='digest') field must be "
                    f"'digest_lead' or 'digest_sentence'; got {self.field!r}."
                )
        return self


class ReviewFinding(BaseModel):
    """One specific editorial concern about one field of the issue.

    Produced by `src/review.py`'s reviewer LLM (as JSON), then filtered by
    code: any finding whose ``quote`` does not appear in the target field's
    CURRENT text is dropped before it reaches a reader (see
    `ReviewReport.dropped_findings`). That deterministic check is why
    ``quote`` is mandatory -- a finding that cannot point at the text it
    objects to has no evidence, and the reviewer's two documented misfire
    classes (2026-07-04) were both findings about text that was not there.
    """

    schema_version: int = 1
    finding_id: Annotated[str, Field(pattern=_FINDING_ID_PATTERN)]
    """``f001``, ``f002``, ... Assigned BY CODE in emission order, not by the
    model: `RevisionChange.finding_ids` references these, and a duplicate id
    would silently mis-attribute an edit."""

    target: ReviewTarget
    """Which field this is about."""

    criterion: Annotated[str, Field(min_length=1, max_length=64)]
    """Stable token for the editorial criterion that fired, e.g.
    ``closing_shape``, ``reputational_liability``, ``trust_flags``. The
    vocabulary the prompt publishes lives in
    ``review.REVIEW_CRITERIA``; an unrecognised token is KEPT (and logged),
    because a real concern in an unexpected bucket is still a real concern."""

    severity: ReviewSeverity
    """blocking | major | minor | note. Drives the code-computed verdict."""

    quote: Annotated[str, Field(min_length=1, max_length=1000)]
    """VERBATIM span of the target field's text that the finding is about.
    Mandatory, and verified by code against the live text. No quote, no
    finding."""

    fix_kind: ReviewFixKind
    """What kind of action resolves this. `src/revise.py` acts on
    ``text_edit`` only."""

    instruction: Annotated[str, Field(min_length=1, max_length=1000)]
    """What to do about it, in the imperative, specific enough that a
    reviser could act on it without re-reading the whole issue."""

    model_config = ConfigDict(extra="forbid")


class ReviewReport(BaseModel):
    """The top-level object serialised to `data/<date>/review.json`.

    Produced by `src/review.py` (`run_review`). Consumed by
    `src/revise.py` (the ``text_edit`` findings), by `review.md` rendering
    (same module, same object), and by evals.

    ``computed_verdict`` is authored by CODE from the finding severities
    under ``config/review_thresholds.yaml``. The model never writes a
    verdict: it produces evidence, and the ratified threshold table turns
    evidence into a decision. That separation is what makes the verdict
    auditable -- you can re-derive it from ``findings`` and the table
    without re-running the LLM.

    Schema v2 (2026-08-08): no field change on `ReviewReport` itself; the
    bump tracks the ReviewTarget v1->v2 change (story targets may now name
    ``field="take"``), since the review.json envelope now carries the
    widened vocabulary. v1 records parse unchanged.

    Schema v3 (2026-08-09): same transitive-envelope rule for the
    ReviewTarget v2->v3 change (``kind="digest"`` + ``digest_index``,
    section-target ``field="synthesis"``). v1/v2 records parse unchanged.
    """

    schema_version: int = 3
    generated_at: datetime
    """UTC timestamp when the review stage wrote this report."""

    computed_verdict: ReviewVerdictValue
    """green | amber | red (computed from findings + thresholds) or one of
    the code-authored machine states (see `ReviewVerdictValue`)."""

    one_line: Annotated[str, Field(max_length=200)] = ""
    """Short editorial summary of the day, for the terminal line and the
    `review.md` frontmatter. The model may supply it (AFTER its findings,
    by prompt key order, so it commits to evidence first); code falls back
    to a severity tally when it doesn't."""

    findings: list[ReviewFinding] = Field(default_factory=list)
    """Every finding that survived the verbatim-quote check, in emission
    order. THE input to the verdict computation."""

    dropped_findings: list[ReviewFinding] = Field(default_factory=list)
    """Findings the code dropped because their ``quote`` is not present in
    the target field's current text (or the target could not be resolved).
    Kept for the audit trail -- they are the reviewer's misfire rate, which
    is a number worth watching -- and EXCLUDED from the verdict."""

    prompt_version: Annotated[str, Field(pattern=_PROMPT_VERSION_PATTERN)]
    """== `review.REVIEW_PROMPT_VERSION` at the time of the review."""

    thresholds_version: Annotated[str, Field(max_length=64)] = ""
    """``version`` from ``config/review_thresholds.yaml`` -- the table that
    turned these severities into this verdict. Empty only on a run that
    never got as far as loading it."""

    llm_model: Annotated[str, Field(max_length=128)] = "unknown"
    """The model that produced the findings (``REVIEW_MODEL``, falling back
    to ``LLM_MODEL``). Recorded because a reviewer reading its own writer's
    output is a known leniency risk -- the audit trail must show which
    model judged."""

    issue_sha256: Annotated[str, Field(max_length=64)] = "unknown"
    """SHA-256 of the exact `issue.json` bytes the reviewer read. Same
    definition as the `review.md` frontmatter key of the same name (see
    DESIGN.md "review.md -> issue_sha256"); `src/revise.py` refuses to edit
    when it no longer matches the file on disk."""

    note: Annotated[str, Field(max_length=2000)] = ""
    """Free-text engine note -- the failure reason on an ``unavailable`` /
    ``unparseable`` run, a drop tally otherwise."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _machine_states_carry_no_findings(self) -> "ReviewReport":
        """Invariant: a code-authored machine state means NO judgement was
        recorded, so it cannot ship findings. Mirrors
        `VerificationReport._unavailable_has_no_stories`: the failure state
        must be unambiguous, or a reader can't tell "the editor found
        nothing" from "the editor never ran"."""
        if self.computed_verdict in ("unavailable", "unparseable", "not_run"):
            if self.findings:
                raise ValueError(
                    f"ReviewReport.computed_verdict="
                    f"{self.computed_verdict!r} must carry an empty findings "
                    f"list; got {len(self.findings)}."
                )
        return self

    @model_validator(mode="after")
    def _finding_ids_are_unique(self) -> "ReviewReport":
        """Invariant: finding ids are unique across findings AND dropped
        findings. `RevisionChange.finding_ids` is a reference into this
        list; a duplicate id would attribute an edit to the wrong
        concern."""
        seen: set[str] = set()
        for finding in list(self.findings) + list(self.dropped_findings):
            if finding.finding_id in seen:
                raise ValueError(
                    f"ReviewReport carries duplicate finding_id "
                    f"{finding.finding_id!r}; ids must be unique so a "
                    "RevisionChange can reference exactly one finding."
                )
            seen.add(finding.finding_id)
        return self

    def severity_counts(self) -> dict[str, int]:
        """Tally of ``findings`` by severity, every severity present with a
        zero default. The verdict computation in `src/review.py` reads
        exactly this, so the tally the artifact displays and the tally the
        verdict came from are the same code path."""
        counts = {"blocking": 0, "major": 0, "minor": 0, "note": 0}
        for finding in self.findings:
            counts[finding.severity] += 1
        return counts


# ---------------------------------------------------------------------------
# RevisionChange / RevisionCycle -- the revision engine's audit trail.
#
# Produced by `src/revise.py` (`revise_day`), persisted as
# `data/<date>/revisions.jsonl` -- ONE RevisionCycle per line, appended per
# invocation, so the file reads as the edit history of the day's draft.
#
# The engine acts only on `ReviewFinding`s with ``fix_kind == "text_edit"``,
# one LLM call per target FIELD, and every replacement passes a code-side
# validation gate before it is written. These models record what was
# proposed, what was applied, and what was refused -- and why.
# ---------------------------------------------------------------------------

class RevisionChange(BaseModel):
    """One proposed-or-applied rewrite of one field.

    ``before`` and ``after`` are the FULL field text on each side, not a
    diff: the record has to be readable a month later without the issue it
    came from, and a diff is only interpretable against a base you still
    have.
    """

    schema_version: int = 1
    target: ReviewTarget
    """The field that was rewritten. Same locator shape the findings use."""

    finding_ids: list[Annotated[str, Field(pattern=_FINDING_ID_PATTERN)]] = Field(
        default_factory=list
    )
    """The `ReviewFinding.finding_id`s this change answers. Empty ONLY for
    an operator-supplied ``--instruction`` directive, which has no finding
    behind it."""

    before: Annotated[str, Field(max_length=4000)]
    """The field text as it stood before the change."""

    after: Annotated[str, Field(max_length=4000)]
    """The replacement text. On a ``rejected`` change this is the candidate
    that FAILED validation -- kept deliberately, because the rejected text
    plus ``reject_reason`` is the evidence for tuning the revise prompt."""

    recommendation: Annotated[str, Field(max_length=2000)]
    """The instruction(s) the reviser was given, joined. What was asked."""

    rationale: Annotated[str, Field(max_length=2000)] = ""
    """Why this replacement answers the recommendation -- the model's own
    one-line account, or a code-authored line on the rejected path."""

    status: RevisionStatus
    """proposed (shadow) | applied (written to issue.json) | rejected."""

    reject_reason: Annotated[str | None, Field(max_length=200)] = None
    """Which validation rule refused the change (e.g.
    ``edit_distance_exceeded``, ``numeral_invented``, ``url_dropped``).
    REQUIRED when ``status == "rejected"``, forbidden otherwise."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _status_consistency(self) -> "RevisionChange":
        """Invariants: a rejection must say why, an acceptance must not
        carry a rejection reason, and an applied change must actually
        change something. Without the last one, "applied" would include
        no-op writes and the audit trail would overstate what the engine
        did."""
        if self.status == "rejected" and not (self.reject_reason or "").strip():
            raise ValueError(
                "RevisionChange(status='rejected') must carry a "
                "reject_reason; a refusal that does not say why cannot be "
                "acted on."
            )
        if self.status != "rejected" and self.reject_reason is not None:
            raise ValueError(
                f"RevisionChange(status={self.status!r}) must not carry a "
                f"reject_reason; got {self.reject_reason!r}."
            )
        if self.status == "applied" and self.before == self.after:
            raise ValueError(
                "RevisionChange(status='applied') must change the text; "
                "before == after is a no-op, not an application."
            )
        return self


class RevisionCycle(BaseModel):
    """One invocation of the revision engine -- one line of
    `data/<date>/revisions.jsonl`.

    Produced by `src/revise.py` (`revise_day`). Consumed by Arman (what did
    the engine touch?), by `render.release_promote` (promoted with the
    released day as part of the audit trail), and by evals.
    """

    schema_version: int = 1
    date: date
    """Issue date this cycle revised; matches the archive folder."""

    cycle: Annotated[int, Field(ge=1)]
    """1-indexed sequence number within the date -- the Nth time the engine
    ran against this draft. Derived from the existing line count, so a
    re-run never overwrites the previous cycle's record."""

    mode: RevisionMode
    """``shadow`` (computed, nothing written to issue.json) or ``live``."""

    changes: list[RevisionChange] = Field(default_factory=list)
    """One entry per target field the engine attempted, in field order.
    Empty when no ``text_edit`` finding applied."""

    operator_instruction: Annotated[str, Field(max_length=2000)] = ""
    """The free-text ``--instruction`` this cycle carried, if any. Recorded
    verbatim: an operator directive is an editorial act and belongs in the
    trail next to the findings it ran alongside."""

    generated_at: datetime
    """UTC timestamp when the cycle ran."""

    prompt_version: Annotated[str, Field(pattern=_PROMPT_VERSION_PATTERN)]
    """== `revise.REVISE_PROMPT_VERSION`."""

    review_prompt_version: Annotated[str | None, Field(max_length=32)] = None
    """The `review.json` prompt version this cycle acted on, for
    correlating a bad edit back to the review that asked for it."""

    issue_sha256_before: Annotated[str, Field(max_length=64)] = "unknown"
    """SHA-256 of the `issue.json` bytes the cycle read. Must equal the
    reviewed hash -- the engine refuses to edit against a stale review."""

    issue_sha256_after: Annotated[str | None, Field(max_length=64)] = None
    """SHA-256 of the bytes written. ``None`` on a shadow cycle and on a
    live cycle that applied nothing."""

    note: Annotated[str, Field(max_length=2000)] = ""
    """Free-text engine note -- the refusal reason when the cycle did not
    run (no review, stale review), a tally otherwise."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _mode_matches_change_statuses(self) -> "RevisionCycle":
        """Invariant: a shadow cycle applies nothing and writes nothing; a
        live cycle leaves nothing merely proposed.

        This is the contract the ``--shadow`` flag makes to the operator,
        and it is cheap to state here rather than trust three call sites to
        hold it. Shadow may carry ``rejected`` changes on purpose: a preview
        that hid the refusals would not be a preview of what live does."""
        if self.mode == "shadow":
            offenders = [c.status for c in self.changes if c.status == "applied"]
            if offenders:
                raise ValueError(
                    "RevisionCycle(mode='shadow') must not carry 'applied' "
                    f"changes; got {len(offenders)}. A shadow cycle writes "
                    "no issue.json."
                )
            if self.issue_sha256_after is not None:
                raise ValueError(
                    "RevisionCycle(mode='shadow') must not record an "
                    "issue_sha256_after; a shadow cycle writes no issue.json."
                )
        else:  # live
            offenders = [c.status for c in self.changes if c.status == "proposed"]
            if offenders:
                raise ValueError(
                    "RevisionCycle(mode='live') must resolve every change to "
                    "'applied' or 'rejected'; got "
                    f"{len(offenders)} still 'proposed'."
                )
        return self


# Resolve the forward reference on SummaryBlock.verification now that
# StoryVerification is defined.
SummaryBlock.model_rebuild()


# ---------------------------------------------------------------------------
# Public re-export surface. Anything outside this module imports from here.
# ---------------------------------------------------------------------------

__all__ = [
    # Literals
    "SourceType",
    "AudienceTag",
    "RankTier",
    "SectionName",
    "MissedReason",
    "ClaimLocation",
    "ClaimVerdictValue",
    "VerificationVerdict",
    "GatePhase",
    "GateDecisionValue",
    "ReviewVerdictValue",
    "ReviewTargetKind",
    "ReviewTargetField",
    "ReviewSeverity",
    "ReviewFixKind",
    "RevisionStatus",
    "RevisionMode",
    # Constants
    "RUBRIC_WEIGHTS",
    "SECTION_WEIGHTS",
    # Persisted models
    "Item",
    "Cluster",
    "RankedStory",
    "SummaryBlock",
    "IssueSection",
    "DigestBullet",
    "Issue",
    "SourceHealth",
    "SourceHealthReport",
    "ClaimVerdict",
    "StoryVerification",
    "VerificationReport",
    "GateCheck",
    "GateReviewState",
    "GateVerifyState",
    "GateDecision",
    "ReviewTarget",
    "ReviewFinding",
    "ReviewReport",
    "RevisionChange",
    "RevisionCycle",
]
