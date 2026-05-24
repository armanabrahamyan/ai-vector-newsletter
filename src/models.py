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
is v3 (v2 added `issue_number`; v3 made `issue_number` Optional to support
the staging vs canonical archive split -- see "Archive: staging vs
canonical" and "Issue Number Registry" in DESIGN.md). When you change a
shape, bump the version on the affected model and append a row to the
changelog in DESIGN.md in the same PR.

External vectors. Embeddings are not stored inline -- `Cluster.centroid_ref`
is a plain `str` filename pointing into `data/YYYY-MM-DD/embeddings/`. See
DESIGN.md "Embedding model" for the storage contract.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import (
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

RankTier = Literal["pulse", "on_the_radar", "cut"]
"""
Editorial slot a RankedStory belongs in. `summarise.py` reads this to assign
sections; Editor may relabel; `cut` is below threshold and excluded from the
issue (kept in ranked.jsonl for transparency / eval).
"""

SectionName = Literal[
    "pulse",          # The Pulse -- 1 story, the most important thing today
    "big_picture",    # The Big Picture -- strategic angles
    "hands_on",       # Hands-On -- enthusiasts + builders, hands-on news
    "on_the_radar",   # On the Radar -- terse linked list
]
"""IssueSection.name -- the four sections of the rendered newsletter.

Renamed in schema v5 (2026-05-24):
- ``leaders`` -> ``big_picture`` (display: "The Big Picture")
- ``geeks`` -> ``hands_on`` (display: "Hands-On")
- ``notable`` -> ``on_the_radar`` (display: "On the Radar")
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
    "significance": 30,
    "hands_on_utility": 25,
    "big_picture_relevance": 20,
    "financial_services_impact": 15,
    "freshness_momentum": 10,
}
"""Weights in [0, 100] that sum to 100. Sourced from config/rubric.yaml.
Renamed in v0.8 (2026-05-24): builder_utility -> hands_on_utility,
leadership_relevance -> big_picture_relevance, to align with section names."""


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
    continuation linkage via `cross_time_ref`).

    Produced by `src/cluster.py`. Consumed by `src/rank.py`,
    `src/summarise.py`, and evals.

    Centroid vectors are stored externally (see DESIGN.md "Embedding model").
    `centroid_ref` is the sidecar filename inside
    `data/YYYY-MM-DD/embeddings/`; pydantic intentionally keeps the vector
    itself out of the JSONL line.
    """

    schema_version: int = 1
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

    cross_time_ref: Annotated[str, Field(pattern=_CLUSTER_ID_PATTERN)] | None = None
    """
    Earliest `cluster_id` in the continuation chain when this cluster is a
    continuation of a prior-day cluster; `None` if new today. See DESIGN.md
    "Cross-time dedup contract".
    """

    embedding_dim: int | None = None
    """Length of the centroid vector if stored; `None` if vectors are external."""

    centroid_ref: str | None = None
    """
    Filename inside `data/YYYY-MM-DD/embeddings/` if vectors are stored
    separately; `None` if not stored.
    """

    model_config = ConfigDict(extra="forbid")

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

    schema_version: int = 1
    cluster_id: Annotated[str, Field(pattern=_CLUSTER_ID_PATTERN)]
    """FK to Cluster.cluster_id."""

    score: Annotated[int, Field(ge=0, le=100)]
    """Final weighted score (rubric sum); must equal the breakdown-weighted sum."""

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

    model_config = ConfigDict(extra="forbid")

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
        """
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

    schema_version: int = 1
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

    cross_time_ref: Annotated[str, Field(pattern=_CLUSTER_ID_PATTERN)] | None = None
    """
    Mirrored from `Cluster.cross_time_ref` so renderers do not need to
    re-join. Continuation chain root, when set.
    """

    signal: Signal | None = None
    """Editorial verdict pill (Phase B). LLM-tagged in summarise.py.
    Optional so pre-Phase-B archive issues still parse."""

    model_config = ConfigDict(extra="forbid")


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
      - `on_the_radar` may be empty on a slow day.

    Schema v4 (2026-05-23): direction-note enforcement removed; direction
    lives in summary prose now ("Where it's heading" is no longer a section,
    and Pulse carries its direction in its own summary).
    """

    schema_version: int = 2
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

    schema_version=4 per DESIGN.md changelog: v2 added `issue_number`; v3
    made it Optional (None while in staging, assigned at release time); v4
    drops `direction_note` + `finance_angle` from SummaryBlock (now embedded
    in summary prose) and the `where_heading` section, and renames `builders`
    -> `geeks`. See "Archive: staging vs canonical" and "Issue Number
    Registry" in DESIGN.md for derivation, idempotency, gap behaviour, and
    the release transition.
    """

    schema_version: int = 4
    issue_number: Annotated[int, Field(ge=1)] | None = None
    """
    Sequential, 1-indexed, monotonically increasing across RELEASED
    (canonical) issues. None while in staging; assigned at release time.
    See DESIGN.md "Archive: staging vs canonical" -- staging issues live in
    `data/staging/YYYY-MM-DD/` with `issue_number = None`; on `--release`,
    `src/run.py` computes `max(canonical issue_numbers) + 1` (or 1 if none),
    writes the assigned number into `data/YYYY-MM-DD/issue.json`, and
    promotes the rest of the staging artifacts. Idempotent re-release on a
    date that is already canonical is a no-op.
    """

    date: date
    """Issue date (YYYY-MM-DD); matches the archive folder."""

    pulse: IssueSection
    """The Pulse -- separate field (not just sections[0]) for type-level
    guarantee that exactly 1 block exists. IssueSection validator enforces
    that pulse holds exactly 1 SummaryBlock."""

    sections: list[IssueSection]
    """
    Remaining sections in display order: leaders, geeks, notable. Pulse
    lives in the dedicated `pulse` field above.
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
    Optional engine-side notes (e.g. "slow day; On the Radar tail shortened").
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
