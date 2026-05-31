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

    Issue           -> produced by src/summarise.py
                       consumed by Arman (ratification), render, Editor, evals

    SourceHealth    -> produced by src/fetch.py (as a child of the
                       source_health.json payload)
                       consumed by evals, render (footer), source (trust decay)

    SourceHealthReport -> produced by src/fetch.py
                          consumed by evals (per-source health summary)

Schema versioning. Every persisted model carries a `schema_version: int`
field per the DESIGN.md schema changelog. Most models are v1 today; `Issue`
is v5 (v2 added `issue_number`; v3 made `issue_number` Optional to support
the staging vs canonical archive split; v4 dropped direction_note +
finance_angle and renamed sections; v5 adds `revision: int = 0` for
same-date re-releases that display as `#N.M` -- see "Archive: staging vs
canonical" and "Issue Number Registry" in DESIGN.md). When you change a
shape, bump the version on the affected model and append a row to the
changelog in DESIGN.md in the same PR.

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


# Patterns reused across models.
_CLUSTER_ID_PATTERN = r"^c_[0-9a-f]{12,}$"
_PROMPT_VERSION_PATTERN = r"^v\d+(\.\d+)*$"


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
    """

    schema_version: int = 2
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

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


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
    """

    schema_version: int = 3
    name: SectionName
    """Which section this is."""

    stories: list[SummaryBlock]
    """May be empty for "on_the_radar" on a slow day; pulse must have exactly 1."""

    intro_lead: Annotated[str, Field(max_length=80)] | None = None
    """Bold lead phrase rendered before the intro body. Phase B; LLM-written
    per section per day (e.g. "Bench before you budget."). None for the
    pulse section and for any pre-Phase-B issues."""

    intro_body: Annotated[str, Field(max_length=400)] | None = None
    """One or two sentences (~30 words) framing the day's pattern in this
    section. Phase B; LLM-written. None for pulse / pre-Phase-B issues."""

    model_config = ConfigDict(extra="forbid")

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
    """

    schema_version: int = 5
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

    generated_at: datetime
    """UTC timestamp when summarise.py wrote this Issue."""

    prompt_versions: dict[str, Annotated[str, Field(pattern=_PROMPT_VERSION_PATTERN)]]
    """
    Which prompt revisions produced this issue. Keys: "rank", "summarise",
    "pulse", optionally "callback". Supports audit and A/B (risk register #6).
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
        at minimum. "pulse" and "callback" are optional (callbacks only fire
        on continuation chains; pulse may be folded into summarise depending
        on prompt structure). Enforcing the minimum protects the audit
        invariant in risk-register item #6.
        """
        required = {"rank", "summarise"}
        missing = required - set(v.keys())
        if missing:
            raise ValueError(
                f"Issue.prompt_versions must include keys {sorted(required)}; "
                f"missing={sorted(missing)}"
            )
        return v


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
# Public re-export surface. Anything outside this module imports from here.
# ---------------------------------------------------------------------------

__all__ = [
    # Literals
    "SourceType",
    "AudienceTag",
    "RankTier",
    "SectionName",
    "MissedReason",
    # Constants
    "RUBRIC_WEIGHTS",
    "SECTION_WEIGHTS",
    # Persisted models
    "Item",
    "Cluster",
    "RankedStory",
    "SummaryBlock",
    "IssueSection",
    "Issue",
    "SourceHealth",
    "SourceHealthReport",
]
