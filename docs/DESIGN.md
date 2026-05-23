# AI Vector — Design (DESIGN.md)

*"All of it, sorted for you."* — Author: **Arman**.
Project plan: [`../PLAN.md`](../PLAN.md). Team agreements: [`./TEAM.md`](./TEAM.md).

This file is the living technical design. It is the source of truth for every
contract, every seam, and every module's responsibility. If something here
disagrees with `PLAN.md`, `PLAN.md` wins — open a PR to fix this file.

Architect owns this document. Any change to a pydantic shape, an archive
schema, or a module's public interface is a contract change, requires
Architect review, and is reflected here in the same PR (or an earlier one).

---

## Engine vs. agents (read this paragraph first)

**The team builds the engine. The engine runs daily without Claude Code
sub-agents in the runtime loop.** The pipeline is Python code plus LLM API
calls (LiteLLM/Bedrock) inside `rank.py` and `summarise.py`. Once v0.1 ships,
GitHub Actions triggers `src/run.py` on a cron; the modules below produce
`data/YYYY-MM-DD/issue.json` and a previewable HTML, and Release commits
`docs/`. The **Editor agent is the one optional second-reader** Arman may
invoke between engine output and his ratification — it is his tool, not a
hard daily dependency. New contributors must understand this before reading
the module map below: the agents in `.claude/agents/` exist to *build and
maintain* the engine, not to run inside it.

---

## Data contracts (pydantic v2 shapes)

All models live in `src/models.py` (one module — Architect owns the file).
Every record that lands in `data/YYYY-MM-DD/` carries a `schema_version: int`
field. Shape changes bump the version and record the diff in the
[changelog](#schema-changelog) at the bottom of this document.

### `Item` — one raw entry from one source

`Item` is the unit produced by `src/fetch.py`. It is the smallest piece of
provenance the rest of the pipeline trusts: a single entry from a single
source, exact-URL deduped already within the day's fetch but **not** yet
clustered against near-duplicates from other sources.

```python
from __future__ import annotations
from datetime import datetime
from typing import Annotated, Literal, Optional
from pydantic import BaseModel, Field, HttpUrl

SourceType = Literal["rss", "atom", "api", "html"]  # html = isolated fallback only


class Item(BaseModel):
    schema_version: int = 1                                                 # bump on shape change
    id: Annotated[str, Field(min_length=1, max_length=256)]                 # stable per-source id (entry guid or url-hash)
    source: Annotated[str, Field(min_length=1, max_length=128)]             # source name from sources.yaml (e.g. "anthropic_blog")
    source_type: SourceType                                                 # how it was fetched
    url: HttpUrl                                                            # canonical URL to the original story
    title: Annotated[str, Field(min_length=1, max_length=512)]              # entry title, stripped, no HTML
    published_at: datetime                                                  # UTC timestamp from the feed; falls back to fetched_at if missing
    raw_summary: Annotated[str, Field(max_length=8000)]                     # short summary as published; HTML stripped, length-capped
    fetched_at: datetime                                                    # UTC timestamp when this run pulled it
    trust_weight: Annotated[int, Field(ge=1, le=5)] = 3                     # mirrored from sources.yaml at fetch time (for traceability)
    language: Annotated[str, Field(pattern=r"^[a-z]{2}(-[A-Z]{2})?$")] = "en"  # ISO 639-1 (optional region); default "en"
    extras: dict[str, str] = Field(default_factory=dict)                    # source-specific small payloads (e.g. HN points); strings only
```

**Notes on choices.** `id` is whatever the source provides as a stable
identifier (RSS `<guid>`, Atom `<id>`, API row id), or a stable hash of the
URL if none — `src/fetch.py` is responsible for that hashing and must be
deterministic. `url` is `HttpUrl` so pydantic rejects junk at the boundary.
`raw_summary` is capped at 8 KB to keep `items.jsonl` small; the LLM never
sees the long form. `extras` is a flat `dict[str, str]` on purpose — no
nested shape — so the JSONL line stays cheap to parse and the schema doesn't
quietly grow per-source tentacles.

### `Cluster` — a set of Items judged to be the same story

`Cluster` is the unit produced by `src/cluster.py`. The Retrieval Engineer
embeds title+summary, groups near-duplicates within the day, then runs
cross-time dedup against the last 14 days of `clusters.jsonl` (see
[Cross-time dedup contract](#cross-time-dedup-contract) below).

```python
from __future__ import annotations
from datetime import datetime
from typing import Annotated, Optional
from pydantic import BaseModel, Field


class Cluster(BaseModel):
    schema_version: int = 1                                                 # bump on shape change
    cluster_id: Annotated[str, Field(pattern=r"^c_[0-9a-f]{12,}$")]         # "c_" + 12+ hex chars; stable per day
    item_ids: Annotated[list[str], Field(min_length=1)]                     # Item.id values that belong to this cluster
    canonical_title: Annotated[str, Field(min_length=1, max_length=512)]    # best-title pick from members (deterministic rule, not LLM)
    sources: Annotated[list[str], Field(min_length=1)]                      # distinct Item.source values; order = first-seen
    earliest_published: datetime                                            # min(Item.published_at) across members; UTC
    size: Annotated[int, Field(ge=1)]                                       # len(item_ids); duplicated for fast read without parsing the list
    cross_time_ref: Optional[Annotated[str, Field(pattern=r"^c_[0-9a-f]{12,}$")]] = None
                                                                            # earliest cluster_id in the continuation chain (set when this is a continuation of a prior-day cluster); None = new today
    embedding_dim: Optional[int] = None                                     # length of the centroid vector if stored; None if vectors are external
    centroid_ref: Optional[str] = None                                      # filename inside data/YYYY-MM-DD/embeddings/ if vectors are stored separately; None if not stored
```

**Notes on choices.** The embedding centroid is **not** stored inline in
`clusters.jsonl`. Vectors can be hundreds of floats per cluster; inlining
them bloats the JSONL and makes diffs unreadable. Retrieval Engineer writes
vectors to `data/YYYY-MM-DD/embeddings/centroids.npz` (or similar) and sets
`embedding_dim` + `centroid_ref` for traceability. Cross-time dedup reads
the last 14 days of those sidecars. (This is a recommendation; if the
embedding-model choice forces inline storage, Retrieval may revisit — see
decision log.)

`cross_time_ref` is the single field the LLM Engineer keys callbacks off:
when set, today's cluster is part of an ongoing story chain, and
`summarise.py` should consider a "last week we flagged X" framing.

### `RankedStory` — a scored cluster ready to write

`RankedStory` is the unit produced by `src/rank.py` — one LLM pass per
cluster against `config/rubric.yaml`. Order in `ranked.jsonl` is
**significant**: sorted by `score` descending. Downstream readers
(`summarise.py`, Editor, archive views) preserve that order.

```python
from __future__ import annotations
from typing import Annotated, Literal
from pydantic import BaseModel, Field

AudienceTag = Literal["builder", "leader", "finance", "general"]


class RankedStory(BaseModel):
    schema_version: int = 1                                                 # bump on shape change
    cluster_id: Annotated[str, Field(pattern=r"^c_[0-9a-f]{12,}$")]         # FK to Cluster
    score: Annotated[int, Field(ge=0, le=100)]                              # final weighted score (rubric sum)
    breakdown: dict[str, Annotated[int, Field(ge=0, le=100)]]               # per-criterion sub-scores; keys match rubric.yaml criterion names
    audience_tags: Annotated[list[AudienceTag], Field(min_length=1)]        # who this is for; e.g. ["builder", "finance"]
    rationale: Annotated[str, Field(min_length=1, max_length=1000)]         # one-line LLM rationale for transparency and eval
    tier: Literal["pulse", "where_heading", "builders", "leaders", "notable", "cut"]
                                                                            # editorial slot assignment (LLM Engineer picks; Editor may relabel; "cut" = below threshold)
    prompt_version: Annotated[str, Field(pattern=r"^v\d+(\.\d+)*$")]        # version of the rank prompt that produced this (e.g. "v1.2"); supports A/B + audit
```

**Notes on choices.** `breakdown` keys are not pinned in the model — they
follow `config/rubric.yaml`. Eval Engineer's harness validates that the keys
match the rubric at runtime; that lets the rubric evolve without a pydantic
churn each time. `tier` is the bridge between rank and summarise: `rank.py`
assigns it, `summarise.py` reads it to pick the section, Editor can override
with a label. `prompt_version` is mandatory so the eval harness can correlate
score movement against prompt revisions (risk-register item #6).

### `IssueSection` — one section of the rendered issue

`IssueSection` is the structural unit of the published newsletter. Sections
follow `PLAN.md §4`: **The Pulse**, **Where it's heading**, **For builders**,
**For leaders**, **Also notable**. Each section holds a list of summary
blocks ready for the Jinja2 template.

```python
from __future__ import annotations
from typing import Annotated, Literal, Optional
from pydantic import BaseModel, Field, HttpUrl

SectionName = Literal[
    "pulse",            # The Pulse — 1 story, 2–3 sentences
    "where_heading",    # Where it's heading — 2–3 trend reads
    "builders",         # For builders — practical for engineers / DS
    "leaders",          # For leaders — strategic + FS implications
    "notable",          # Also notable — terse linked list
]


class SummaryBlock(BaseModel):
    schema_version: int = 1                                                 # bump on shape change
    story_id: Annotated[str, Field(pattern=r"^c_[0-9a-f]{12,}$")]           # = Cluster.cluster_id (the canonical handle for a story)
    headline: Annotated[str, Field(min_length=1, max_length=200)]           # editorial headline (LLM-written, may differ from canonical_title)
    summary: Annotated[str, Field(min_length=1, max_length=1200)]           # the story body — link out, never reproduce full article
    direction_note: Annotated[str, Field(max_length=400)] = ""              # "where this points" — required for pulse/where_heading; "" allowed elsewhere
    finance_angle: Optional[Annotated[str, Field(max_length=400)]] = None   # FS lens, when the story earns one (see finance-lens skill)
    source_urls: Annotated[list[HttpUrl], Field(min_length=1)]              # links to original sources; render attributes attribution
    cross_time_ref: Optional[Annotated[str, Field(pattern=r"^c_[0-9a-f]{12,}$")]] = None
                                                                            # mirrored from Cluster.cross_time_ref so renderers don't need to re-join


class IssueSection(BaseModel):
    schema_version: int = 1                                                 # bump on shape change
    name: SectionName                                                       # which section this is
    stories: list[SummaryBlock]                                             # may be empty for "notable" on a slow day; pulse must have exactly 1
```

**Notes on choices.** `SummaryBlock` separates `headline` (what reads in the
issue) from the cluster's `canonical_title` (what came from the feeds). The
LLM is free to write a sharper headline; provenance lives in `source_urls`
and (transitively) in `clusters.jsonl`. `direction_note` is *required* for
pulse and where-heading sections (the editorial DNA — "a vector has
direction"); the renderer or Editor enforces, not pydantic, because the
constraint is per-section, not per-block.

### `Issue` — the full structured issue

`Issue` is the top-level artifact written to `data/YYYY-MM-DD/issue.json` by
`src/summarise.py`. It is the unit Arman ratifies and Release renders.

```python
from __future__ import annotations
from datetime import date, datetime
from typing import Annotated, Optional
from pydantic import BaseModel, Field


class Issue(BaseModel):
    schema_version: int = 3                                                 # bump on shape change; v3 makes issue_number Optional (staging vs canonical)
    issue_number: Optional[Annotated[int, Field(ge=1)]] = None              # None in staging; assigned at release time (max canonical + 1). See Archive: staging vs canonical
    date: date                                                              # the issue date (YYYY-MM-DD); matches the archive folder
    pulse: IssueSection                                                     # The Pulse — exactly 1 SummaryBlock
    sections: list[IssueSection]                                            # remaining sections in display order: where_heading, builders, leaders, notable
    generated_at: datetime                                                  # UTC timestamp when summarise.py wrote this
    prompt_versions: dict[str, Annotated[str, Field(pattern=r"^v\d+(\.\d+)*$")]]
                                                                            # which prompt revisions produced this issue; keys: "rank", "summarise", "pulse", optionally "callback"
    notes: Annotated[str, Field(max_length=2000)] = ""                      # optional engine-side notes (e.g. "slow day; notable tail shortened"); not rendered
```

**Notes on choices.** `pulse` is a separate field, not just the first
`IssueSection` in `sections`, because The Pulse is editorially load-bearing
and we want type-level guarantees it exists with exactly one block. The
`prompt_versions` dict supports audit (which prompts generated this?) and
A/B (Eval Engineer can correlate score movement against prompt revisions —
risk register item #6). `issue_number` gives every *released* issue a
stable, human-friendly identifier ("issue #42") independent of date —
useful for callbacks, archive UX, and reader-facing references. The number
is **Optional** because every issue starts life in `data/staging/<date>/`
with `issue_number = None` and only earns a number when `--release`
promotes it to `data/<date>/`. The derivation rule, the staging/release
transition, and edge cases are pinned in the [Issue Number
Registry](#issue-number-registry) and [Archive: staging vs
canonical](#archive-staging-vs-canonical).

### Issue Number Registry

Issues are numbered sequentially from 1. The number is **derived at
release time** by `src/run.py --release` (not stored in a separate counter
file), so the canonical archive on disk is the single source of truth.

**When the number is assigned.** Not at summarise time. While an issue
lives in `data/staging/<date>/issue.json`, `issue_number` is `None`
(pydantic `Optional[int]`, per the v3 schema). The number is assigned
exactly once, when Arman runs `python -m src.run --release` and the
release path promotes staging to canonical. See [Archive: staging vs
canonical](#archive-staging-vs-canonical) for the full transition.

**Derivation rule.** At release time, `src/run.py --release`:

1. Scans `data/*/issue.json` in **date order** (lexicographic on the
   `YYYY-MM-DD` directory name). **Canonical only** -- the scan
   excludes `data/staging/` entirely; staging artifacts are invisible
   to numbering.
2. Reads each canonical `issue.json` and extracts `issue_number`
   (ignoring any record whose `issue_number` is `None`, which should not
   happen in canonical but the reader is defensive).
3. Computes `next_number = max(issue_numbers) + 1`. If no prior canonical
   `issue.json` exists in the archive, `next_number = 1`.
4. Reads `data/staging/<date>/issue.json`, sets `issue_number = next_number`
   on the in-memory model, and atomically writes the updated `Issue` to
   `data/<date>/issue.json` (the canonical location) as the LAST step of
   the release sequence -- see the release transition below.

**Idempotency on re-release.** If `data/<date>/issue.json` already
exists (the date has already been released), `--release` is a **no-op**:
it logs a clear message ("`<date>` already released as issue #N -- to
re-release, delete `data/<date>/issue.json` first") and exits cleanly
without rewriting any canonical files and without re-appending URLs to
`published_urls.txt`. This preserves the invariant "issue #N is a stable
handle for one specific released issue."

**Idempotency on same-day staging re-runs.** Re-running the engine
against the same date overwrites `data/staging/<date>/` files atomically.
Because staging `issue_number` is always `None`, there is no number to
preserve across re-runs -- the number does not exist yet.

**Skip behaviour.** `issue_number` counts **released issues, not
calendar days.** If a day's run is skipped (no `--release` was ever
issued -- e.g. per TEAM.md's daily-draft-loop cutoff, yesterday's
canonical issue stays live and today only produced a staging draft that
was never promoted), the sequence does **not** advance for that day. So
a sequence may legitimately read `…, 41, 42, 43, …` with arbitrary
calendar gaps between consecutive numbers. The mapping `issue_number →
date` is many-to-zero-or-one; the mapping `date → issue_number` is
partial.

**Empty archive.** If `data/` has no canonical `issue.json` files
(first-ever release, or canonical archive wiped), the first released
issue is `issue_number = 1`. Staging contents are not consulted.

**Gap recovery.** If the canonical archive is partially missing -- e.g.
a directory was deleted or never synced -- and the surviving issues are
numbered `1, 2, 3, 7, 8`, the next release's `next_number` is `9` (max
+ 1). The missing 4–6 are **not** back-filled; the gap is preserved as
evidence that issues 4–6 existed but their artifacts are gone. Do not
renumber to close the gap: external references ("see issue #7") must
keep pointing at the same content. If a missing artifact is later
recovered, drop it back into its original `data/YYYY-MM-DD/` directory
with its original `issue_number` intact.

**What to do if archive history is missing entirely.** Treat as the
empty-archive case: next release is `issue_number = 1`. The engine does
not invent issues it has no evidence of having released.

**Validation.** Eval Engineer's module-integrity check verifies, across
the **canonical** archive (`data/<date>/issue.json`, excluding
`data/staging/`), that `issue_number` values are unique per artifact
(no two canonical `issue.json` files share a number) and that the
date-ordered sequence is strictly increasing (later dates never have
lower numbers than earlier dates). Gaps are allowed; reversals are not.
Staging `issue.json` is permitted to carry `issue_number = None` and
the check tolerates that explicitly (it is the expected staging shape).

---

## Archive schema (`data/YYYY-MM-DD/`)

Locked for v0: **JSON-per-day, no SQLite.** Files below are the contract.
Every writer adheres to the **atomic write rule** ([below](#atomic-writes)).
Every reader tolerates missing days, missing files, and missing sidecars.

> **Two archive states.** Every file documented below lives in **two**
> parallel locations with the same shape: `data/staging/YYYY-MM-DD/`
> (work-in-progress, written on every engine run, freely re-runnable,
> invisible to history) and `data/YYYY-MM-DD/` (canonical, written only
> by `--release`, immutable once present). The shape is identical; only
> the path differs and `Issue.issue_number` is `None` in staging vs. an
> integer in canonical. See [Archive: staging vs
> canonical](#archive-staging-vs-canonical) for the state model, the
> release transition, and the read-path rules (cross-time dedup,
> callbacks, and `published_urls.txt` all read **canonical only**).

### `items.jsonl` — Source Engineer writes

- **Writer:** `src/fetch.py`.
- **Schema:** one `Item` per line, JSON-encoded. Each record carries
  `schema_version`.
- **Atomicity:** `items.jsonl.tmp` is written line-by-line; on success,
  fsync then rename to `items.jsonl`. Partial files never become the
  canonical name.
- **Read contract:** consumers (`cluster.py`, `evals.run_evals`, Release
  provenance views) read the whole file, ignore records whose
  `schema_version` they don't understand, and **must not crash on missing
  files** — an empty fetch day yields no `items.jsonl`, and the day is
  treated as zero items.

### `source_health.json` — Source Engineer writes

- **Writer:** `src/fetch.py`.
- **Schema:** single JSON object. Top-level fields:
  - `schema_version: int`
  - `run_started_at: datetime`
  - `run_finished_at: datetime`
  - `sources: list[SourceHealth]` where `SourceHealth` is:
    ```
    {
      source: str,                  # matches Item.source
      fired: bool,                  # True if the fetch attempt completed
      items_in: int,                # raw entries seen
      items_kept: int,              # after exact-URL dedup + filters
      latency_ms: int,              # wall-clock for this source
      last_modified: Optional[datetime],  # HTTP Last-Modified or feed updated; UTC
      missed_reason: Optional[str]  # short token: "timeout", "http_4xx", "http_5xx", "parse_error", "empty_feed", "disabled"
    }
    ```
- **Atomicity:** write to `.tmp`, fsync, rename.
- **Read contract:** Eval Engineer's module-integrity check uses `fired` +
  `missed_reason` to surface dead sources (risk #4). Release uses it to
  decide whether to include a "sources fired today" footer. Source Engineer
  uses the trailing window to decay trust weights.

### `clusters.jsonl` — Retrieval Engineer writes

- **Writer:** `src/cluster.py`.
- **Schema:** one `Cluster` per line, JSON-encoded. Each record carries
  `schema_version`.
- **Sidecar (optional):** `data/YYYY-MM-DD/embeddings/centroids.npz` (or
  similar) holds centroid vectors keyed by `cluster_id`. Retrieval chooses
  the format; `Cluster.centroid_ref` records the filename.
- **Atomicity:** `.tmp` + fsync + rename for both the JSONL and the sidecar.
- **Cross-time dedup:** when `cross_time_ref` is set on a record,
  downstream readers know this cluster is a **continuation** of a chain
  whose earliest member is the referenced `cluster_id`. LLM Engineer reads
  the last 14 days of `clusters.jsonl` (+ corresponding `issue.json`
  appearances) to generate callbacks ("last week we flagged X").
- **Read contract:** consumers iterate; if a record fails to parse,
  Retrieval Engineer's writer is buggy and Eval Engineer surfaces it — a
  reader does not silently skip.

### `ranked.jsonl` — LLM Engineer writes

- **Writer:** `src/rank.py`.
- **Schema:** one `RankedStory` per line.
- **Order is significant** — sorted by `score` descending. Downstream
  readers preserve this order.
- **Atomicity:** `.tmp` + fsync + rename.
- **Read contract:** `summarise.py` reads top-N (configurable; current
  default 8–12 — see decision log) and assigns them to sections via
  `RankedStory.tier`. Editor reads the full file (including `cut` tier) to
  flag what was dropped. Eval reads to compute ranking Spearman.

### `issue.json` — LLM Engineer writes

- **Writer:** `src/summarise.py` (writes to `data/staging/<date>/`);
  `src/run.py --release` (promotes to `data/<date>/`, the canonical
  location, and assigns `issue_number`).
- **Schema:** a single `Issue` object as JSON (not JSONL). In staging,
  `Issue.issue_number` is `None`; in canonical, it is an integer assigned
  at release time. See [Archive: staging vs
  canonical](#archive-staging-vs-canonical) and the [Issue Number
  Registry](#issue-number-registry).
- **Atomicity:** `.tmp` + fsync + rename. In the release path the
  canonical `issue.json` is written **LAST**, after every other staging
  artifact has been copied into `data/<date>/`, so a partial release
  never looks complete to readers.
- **Read contract:** Editor reads the staging copy to label and propose;
  Arman reads the staging preview to ratify; `render.py` reads (staging
  for preview, canonical for ship); Eval reads **canonical only** for
  voice + drift; future-day `summarise.py` reads the last 14 days of
  **canonical** `issue.json` for callbacks. **The released (canonical)
  `issue.json` is the labelled corpus** — over months, the most valuable
  artifact in the repo. Staging issues are draft material, not corpus.

### `data/published_urls.txt` — Release Engineer writes (cumulative, not per-day)

- **Path:** `data/published_urls.txt`. **Note: this file lives at the
  `data/` root, not under a `YYYY-MM-DD/` directory, and not under
  `data/staging/`.** It is the cumulative archive of URLs that have
  appeared in a **released (canonical)** issue.
- **Writer:** `src/render.py` (Release Engineer), invoked **only**
  through `src/run.py --release`. Staging runs never write to this
  file. The append happens as part of the release transition, after
  the canonical `issue.json` is in place.
- **Schema:** plain text. One URL per line. UTF-8, LF line endings.
  Append-only in effect (duplicates skipped); the writer rewrites the
  whole file via `.tmp` + fsync + rename for atomicity.
- **Readers:** `src/cluster.py` (pre-cluster URL filter) and
  `src/rank.py` (post-rank guard for clusters whose every member URL
  is already released). See the
  [Cross-issue article-level dedup](#cross-issue-article-level-dedup)
  section for the contract this file implements. Both readers see only
  released URLs -- a story Arman drafted in staging and never released
  remains eligible to appear in a future release.
- **Why at the root, not per-day?** It is **cumulative across all
  releases**, not a per-day artifact. Putting it under
  `data/YYYY-MM-DD/` would imply it is owned by one day's run, which
  it is not.

### Atomic writes

Every writer in the pipeline implements the same pattern:

1. Open `data/YYYY-MM-DD/<name>.tmp` for write.
2. Write contents; for JSONL, line-by-line.
3. `fsync` the file descriptor.
4. `os.replace` (atomic rename) to `data/YYYY-MM-DD/<name>`.
5. Optionally `fsync` the parent directory.

This protects against half-written archives (risk register #7). A crash
mid-write leaves a `.tmp` file behind — readers ignore it; a follow-up run
overwrites it. **Readers must always read the final name, never `.tmp`.**

Every reader tolerates:

- **Missing day directory** — a stage that didn't run yesterday yields
  zero records; today's pipeline proceeds.
- **Missing single file** — the corresponding stage didn't run; the reader
  treats it as empty and logs a structured warning.
- **Unknown `schema_version`** — readers must skip records whose
  `schema_version` is higher than they understand and log a structured
  warning. They must continue to read records whose version is lower if a
  backward-compat path exists (recorded per-version in the
  [changelog](#schema-changelog)).

Every artifact carries a `schema_version: int`. Shape changes bump it and
record the diff at the bottom of this document. The Eval Engineer's
module-integrity check schema-validates every archive write.

---

## Archive: staging vs canonical

The archive has **two states**: *staging* (work-in-progress) and
*canonical* (released, immutable). Every engine run writes to staging.
Arman promotes staging to canonical with `python -m src.run --release`.
This separation lets Arman iterate freely on a day's issue -- re-running
the pipeline, comparing prompt revisions, trying alternative Pulse picks
-- with **zero consequence** for history, future-day dedup, callbacks,
or the eval corpus.

### The two states

**Staging (default — work in progress).**

- **Path:** `data/staging/YYYY-MM-DD/` with the same five files documented
  in [Archive schema](#archive-schema-datayyyy-mm-dd) above:
  `items.jsonl`, `source_health.json`, `clusters.jsonl`, `ranked.jsonl`,
  `issue.json`, plus the `embeddings/centroids.npz` sidecar.
- **Writer:** every `python -m src.run` invocation, by default. Each
  pipeline stage writes its staging artifact via the same atomic-write
  pattern documented above.
- **`issue.json` shape:** `Issue.issue_number` is `None`. No number has
  been assigned yet -- the issue is not yet part of history.
- **Idempotency:** same-day re-runs overwrite the staging files
  atomically. Identical to the pre-staging same-day re-run behaviour,
  just under the staging path.
- **Preview render:** `render.py` reads from `data/staging/<date>/` and
  writes `docs/preview/<date>.html`. Preview is regenerated on every
  staging render.
- **Read invisibility:** **nothing else reads staging.** Cross-time
  dedup in `cluster.py`, the callback lookback in `summarise.py`, the
  pre-cluster URL filter in `cluster.py`, the post-rank URL guard in
  `rank.py`, and the eval harness in `evals/run_evals.py` all read
  canonical only. Staging is a private workspace; what happens in
  staging stays in staging.

**Canonical (released — part of the record).**

- **Path:** `data/YYYY-MM-DD/` (the same layout as before this refactor;
  no change to existing canonical paths).
- **Writer:** `python -m src.run --release` only. No other code path
  writes to canonical.
- **`issue.json` shape:** `Issue.issue_number` is an integer assigned at
  release time (see [Issue Number Registry](#issue-number-registry)).
- **Immutability:** once present, a canonical `<date>/issue.json` is
  not rewritten by any normal pipeline operation. Re-running `--release`
  on the same date is a no-op (see "Idempotency of release" below).
- **Read role:** the canonical archive is the corpus -- cross-time
  dedup, callbacks, eval baselines, drift detection, voice baselines,
  and the published archive UX all derive from it.

### The release transition

`python -m src.run --release` performs the following sequence for the
target date (default: today):

1. **Pre-flight: check for already-released.** If
   `data/<date>/issue.json` already exists, log a clear message and
   exit cleanly (no-op). See "Idempotency of release" below.
2. **Read staging.** Load `data/staging/<date>/issue.json`. Validate
   against the `Issue` model (must parse; `issue_number` is expected to
   be `None`).
3. **Assign the issue number.** Scan the **canonical** archive
   (`data/*/issue.json`, **excluding** `data/staging/`) for existing
   `issue_number` values. Compute `next_number = max(existing) + 1`,
   or `1` if no canonical history exists. Apply on the in-memory
   `Issue`.
4. **Copy peripheral artifacts first.** For each of `items.jsonl`,
   `clusters.jsonl`, `ranked.jsonl`, `source_health.json`, and the
   `embeddings/` sidecar directory, copy the file from
   `data/staging/<date>/` to `data/<date>/` using the standard atomic
   pattern (write to `<name>.tmp` in the destination, fsync, rename).
   Order among these is not load-bearing; do them in the order listed
   for log readability.
5. **Write canonical `issue.json` LAST.** With `issue_number` now set
   on the in-memory `Issue`, serialise and atomically write to
   `data/<date>/issue.json`. **This is the load-bearing ordering: a
   partial release that crashes after step 4 but before step 5 leaves
   the date without a canonical `issue.json`, so readers (`cluster.py`,
   `summarise.py`, the next `--release`) correctly treat the date as
   "not yet released" and ignore the half-copied peripheral files.**
   The presence of `data/<date>/issue.json` is the single signal that
   says "this date is released."
6. **Render the canonical issue.** Run `render.py` against
   `data/<date>/issue.json` (not the staging copy). Write
   `docs/index.html` (latest) and `docs/archive/<date>.html`.
7. **Append source URLs to `data/published_urls.txt`.** Union the
   in-issue URLs with the existing file (idempotent: any URL already
   present is skipped). Atomic write of the whole file.

Conceptually this is one atomic transaction. In practice it is many
file operations. The release sequence is **multi-file**, but the
canonical `issue.json` is the single commit marker -- writing it last
makes the transition observably atomic from every reader's point of
view: either `data/<date>/issue.json` exists (release succeeded) or it
does not (treat the date as un-released, regardless of any peripheral
copies that may exist).

### Read rules (staging is invisible to history)

| Reader | What it reads | What it ignores |
|---|---|---|
| `cluster.py` cross-time dedup (last 14 days of `clusters.jsonl` + centroid sidecars) | `data/<date>/` for each prior date | `data/staging/` entirely |
| `cluster.py` pre-cluster URL filter | `data/published_urls.txt` (released URLs only) | n/a |
| `rank.py` post-rank URL guard | `data/published_urls.txt` (released URLs only) | n/a |
| `summarise.py` callback lookback (last 14 days of `issue.json` + `ranked.jsonl`) | `data/<date>/` for each prior date | `data/staging/` entirely |
| `evals/run_evals.py` (all eval dimensions: integrity, drift, voice, dedup, ranking) | `data/<date>/` for each archive date | `data/staging/` entirely |
| `render.py` preview mode | `data/staging/<date>/` | n/a |
| `render.py` ship mode (invoked by `--release` only) | `data/<date>/` (the canonical copy that step 5 just wrote) | n/a |

The rule of thumb: **drafts Arman discards must not influence anything
downstream.** That covers cross-time dedup (a draft cluster never
"continues" a future story), URL exclusion (a story cut in staging can
appear in a future release), callbacks (the LLM doesn't reference an
issue that never went out), and eval baselines (drift is measured
against what was released, not against what was tried).

### `--release` as a CLI surface

```
python -m src.run --release [--date YYYY-MM-DD]
```

- Default `--date` is today (UTC, matching the rest of the pipeline).
- `--release` does not run the engine -- it only promotes an existing
  `data/staging/<date>/` to canonical. The expected workflow is: run
  the full pipeline (writes staging), review `docs/preview/<date>.html`,
  then re-run with `--release` to ship.
- `--release` replaces the prior `--publish` flag. There is no
  separate "render with publish=True" path; release IS the render-and-
  ship-and-append-URLs path.
- `--release` requires a `data/staging/<date>/issue.json` to exist. If
  it doesn't, log an error ("nothing to release for `<date>`: run the
  engine first to produce a staging draft") and exit non-zero.

### Idempotency of release

If `data/<date>/issue.json` already exists at the start of a
`--release` invocation, the run is a **no-op**: log

```
release: <date> is already canonical as issue #N. To re-release this
date, delete data/<date>/issue.json first (manual operation,
documented in DESIGN.md "Recovery: re-releasing a date").
```

and exit `0`. No files in `data/<date>/` are rewritten. No URLs are
appended to `published_urls.txt`. No HTML is re-rendered. This makes
`--release` safe to run twice by accident (Arman fat-fingers the
command, or a CI step re-fires).

### Recovery: re-releasing a date

Re-release is a manual, deliberate operation. It is not a CLI flag; it
is a sequence the Architect documents here so anyone who needs it can
follow it without inventing a new path:

1. Delete `data/<date>/issue.json` (the commit marker).
2. Optionally delete the peripheral canonical files
   (`data/<date>/items.jsonl`, `clusters.jsonl`, `ranked.jsonl`,
   `source_health.json`, `embeddings/`) if you also want them
   re-copied. Leaving them in place is harmless -- step 4 of the
   release transition will overwrite them atomically.
3. Optionally re-run the engine (`python -m src.run`) if the staging
   contents need to be regenerated.
4. Run `python -m src.run --release [--date <date>]`. The release
   sequence proceeds normally; the issue gets a new `issue_number`
   (`max canonical + 1`), which **may differ from the deleted one** --
   if any later dates have already been released, the deleted number
   becomes a permanent gap in the sequence (which is fine, per [Issue
   Number Registry](#issue-number-registry) gap rules; do not back-fill).
5. Remove the now-stale entry from `data/published_urls.txt` only if
   the re-release substantively changes the URL set -- otherwise the
   union-on-append behaviour leaves the file consistent.

This is the only documented path to mutate canonical. There is no
`--force-release` flag and there is no programmatic re-release; the
manual delete is the gate, and `git log` on the deletion is the audit
trail.

### Implications for evaluation

`evals/run_evals.py` reads canonical artifacts only -- `data/<date>/`
for each archive date and `data/published_urls.txt`. Staging is not
eval material:

- Drafts that Arman discarded must not influence drift baselines
  (median story count, audience-tag mix, voice score, summary
  length). Drift is measured against what readers actually saw.
- The labelled corpus (every ratified `issue.json` is implicit
  labelled data, per the existing "ratified `issue.json` is labelled
  data" rule) only counts canonical, by definition.
- Module-integrity checks run against canonical days. A staging day
  that fails module integrity is not a regression -- it's an
  in-progress draft. (Architect's call: a future enhancement may add
  an optional `--include-staging` flag to the eval harness for
  pre-release sanity checks; not in scope here.)

The eval harness's existing `--against real --dataset <YYYY-MM-DD>`
mode is unaffected: the dataset path resolves to `data/<YYYY-MM-DD>/`,
which is now explicitly the canonical location.

---

## Module boundaries & seams

One row per module. The owner agent's PRs touch that module; everyone else
reviews via the contract. Public function signatures are the entry points —
internal helpers are private to the module.

| Module | Owner agent | Reads from | Writes to | Public function signature |
|---|---|---|---|---|
| `src/fetch.py` | Source Engineer | `config/sources.yaml` | `data/YYYY-MM-DD/items.jsonl`, `data/YYYY-MM-DD/source_health.json` | `def fetch_day(run_date: date, config_path: Path = Path("config/sources.yaml"), out_dir: Path = Path("data")) -> tuple[list[Item], list[SourceHealth]]` |
| `src/cluster.py` | Retrieval Engineer | `data/YYYY-MM-DD/items.jsonl`, `data/(last 14 days)/clusters.jsonl` (+ embedding sidecars) | `data/YYYY-MM-DD/clusters.jsonl`, `data/YYYY-MM-DD/embeddings/centroids.npz` | `def cluster_day(run_date: date, data_dir: Path = Path("data"), lookback_days: int = 14) -> list[Cluster]` |
| `src/rank.py` | LLM Engineer | `data/YYYY-MM-DD/clusters.jsonl`, `config/rubric.yaml` | `data/YYYY-MM-DD/ranked.jsonl` | `def rank_day(run_date: date, rubric_path: Path = Path("config/rubric.yaml"), data_dir: Path = Path("data")) -> list[RankedStory]` |
| `src/summarise.py` | LLM Engineer | `data/YYYY-MM-DD/ranked.jsonl`, `data/YYYY-MM-DD/clusters.jsonl`, `data/YYYY-MM-DD/items.jsonl`, `data/(last 14 days)/issue.json`, `data/(last 14 days)/ranked.jsonl` | `data/YYYY-MM-DD/issue.json` | `def summarise_day(run_date: date, data_dir: Path = Path("data"), lookback_days: int = 14) -> Issue` |
| `src/render.py` | Release Engineer | `data/YYYY-MM-DD/issue.json`, `templates/issue.html.j2` | `docs/index.html`, `docs/archive/YYYY-MM-DD.html` | `def render_issue(issue: Issue, templates_dir: Path = Path("templates"), docs_dir: Path = Path("docs")) -> None` |
| `src/run.py` | Architect (orchestration shell; module owners maintain their stages) | All of the above, transitively | All of the above, transitively | `def main(run_date: date \| None = None, skip: set[str] = frozenset()) -> int` (CLI: `python -m src.run [--date YYYY-MM-DD] [--skip fetch,cluster,...]`; returns process exit code) |

**Seam rules.**

- Each module is **idempotent on the same day** — re-running overwrites the
  same files atomically. `run.py` is safe to re-execute.
- No module imports another module's internals. The contract is the file
  artifact on disk plus the public function signature above. (`run.py` may
  import the public functions to chain them in-process for local dev; CI
  may also run them as separate subprocesses.)
- No LLM calls in `fetch.py`, `cluster.py` (embeddings yes; LLM judgment
  no), `render.py`, or `run.py`. LLM lives in `rank.py` and `summarise.py`.
- Logging shape is shared (Architect cross-cutting concern): one
  structured JSON line per significant event, fields `{ts, level, module,
  event, ...}`. `run.py` decides the destination (stderr for CI;
  configurable for local).

---

## Cross-time dedup contract

The Retrieval Engineer's responsibility — and the LLM Engineer's read
contract on top of it — for not re-reporting the same story across days.

### Setting `Cluster.cross_time_ref`

1. After producing today's clusters, `cluster.py` loads the centroid
   sidecars for the last 14 days of `clusters.jsonl`.
2. For each today-cluster, it computes cosine similarity against all
   recent centroids.
3. If the highest match is **above the configured threshold** (default
   target ~0.85; Retrieval Engineer tunes against Eval fixtures) **and**
   the matched cluster is still "active" (matched within the last ~7 days
   or has a chain that is), the today-cluster is considered a
   **continuation**.
4. `cross_time_ref` is set to the `cluster_id` of the **earliest** cluster
   in the continuation chain — not the immediately previous day, but the
   root. This makes chains stable to read: "this story = chain rooted at
   `c_abc…`".
5. If no match clears the threshold, `cross_time_ref` remains `None` — the
   story is **new today**.

Threshold and active-window numbers are Retrieval Engineer's call (consult
Eval); recorded in `docs/DESIGN.md` once tuned.

### Read contract for LLM Engineer (callbacks)

When `summarise.py` writes a `SummaryBlock` for a cluster whose
`cross_time_ref` is set, it:

1. Loads the chain — read the last 14 days of `clusters.jsonl`, follow the
   chain back via `cross_time_ref`.
2. Loads which past `issue.json` files featured any member of the chain
   (the cluster_id appears as a `SummaryBlock.story_id`).
3. Considers a **callback framing** in the summary — *"Last Tuesday we
   flagged the Cohere distillation story; today's update is…"* — if the
   chain has prior published coverage.
4. Mirrors `cross_time_ref` onto the `SummaryBlock` for renderers (so the
   template can decorate continuation stories without re-joining).

Editor flags missed-callback opportunities in voice labels; Eval Engineer
includes "callback coverage on continuation chains" in its drift metrics
over time.

---

## Cross-issue article-level dedup

`Cluster.cross_time_ref` handles **story-level** continuations (the same
story develops over days; the LLM Engineer uses the ref to write
callbacks). It does **not** prevent a specific URL from re-appearing —
two clusters on different days may contain overlapping items, and a
slow-burn story may surface the same write-up again later.

This section adds a stricter, URL-level guarantee on top of `cross_time_ref`:

> **Contract — released-URL exclusion.** Once a specific article URL has
> appeared in a *released (canonical)* `issue.json`, it must not appear
> in any future released issue. Ever. The window is forever; there is
> no decay. **Staging runs do not contribute to this index**: a URL
> that appears in a `data/staging/<date>/issue.json` Arman never
> released remains eligible for a future release.

The contract is enforced by a single derived index plus two read points
in the pipeline.

### `data/published_urls.txt` — the exclusion index

- **Path:** `data/published_urls.txt` (at the `data/` root, **not**
  under a date directory, and **not** under `data/staging/` — it is
  the cumulative archive of all **released** URLs, not a per-day or
  per-staging-draft artifact).
- **Format:** plain text, one URL per line, UTF-8, LF-terminated.
  Append-only.
- **Writer:** `src/render.py` (Release Engineer), invoked **only by**
  `python -m src.run --release`. After the canonical `issue.json`
  has been written, render extracts every URL from the issue and
  appends any not-already-present URL to `published_urls.txt`.
  Specifically: the union of `Issue.pulse.stories[*].source_urls`
  and, for every section in `Issue.sections`,
  `IssueSection.stories[*].source_urls`. **Staging runs never touch
  this file** -- a staging preview render is read-only with respect
  to canonical state.
- **Atomicity:** the file is updated via the same `.tmp` + fsync +
  rename pattern as the rest of the archive. The whole file is
  rewritten on each release — small enough that this is acceptable
  and gives us a clean atomic update.
- **Readers:** `src/cluster.py` and `src/rank.py` at the start of
  each daily run. Both readers see the file as the set of all
  **released** URLs; a draft URL Arman has not released is not in
  this set and is eligible for clustering / ranking.

### Enforcement points

1. **Pre-cluster filter (`src/cluster.py`).** Before clustering,
   `cluster.py` loads `data/published_urls.txt` into a set and drops any
   `Item` whose `url` is in the set. This is item-level pre-dedup
   against the historical archive — published items never even reach
   the clusterer.
2. **Post-rank guard (`src/rank.py`).** As a belt-and-braces check,
   `rank.py` cross-references each surviving cluster against the same
   set. If **every** member `Item.url` in a cluster is in
   `published_urls.txt`, the cluster is dropped from the ranked output.
   (Edge case: a cluster that survived because at least one item slipped
   past pre-cluster filtering — e.g. a URL variant that normalises to a
   previously-published URL only after canonicalisation. The post-rank
   guard catches the "all members previously seen" case explicitly.)

### Rationale — *once released, never re-release*

The contract is strict on purpose. The continuation case (same story
develops over days) is **already** handled by `Cluster.cross_time_ref`:
when a story develops, the **new article covering it is a new URL** that
has not been released, so it surfaces normally. The LLM Engineer uses
`cross_time_ref` to write a callback ("Last week we flagged X; today's
update is…") that references the prior issue. The reader gets the
update without us recycling the exact same link.

If a URL has already been released, by definition we have already paid
the editorial bandwidth on it. Re-running it adds nothing for the reader
and erodes trust in the publication ("you sent me this on Tuesday").

### What counts as "released"

A URL is in `published_urls.txt` **only after**:

1. The engine has produced `data/staging/<date>/issue.json`,
2. Arman has run `python -m src.run --release`, and
3. The release transition has reached step 7 (append URLs) -- which
   only runs after step 5 (the canonical `issue.json` is in place).

**Staging drafts that Arman never releases** — stories present in
`data/staging/<date>/issue.json` but never promoted to canonical — do
**not** add their URLs to the exclusion index. This means a story
Arman cut from a staging draft (or simply never released that day) can
still appear in a future release, which is correct: a staging draft is
not an editorial commitment, only a release is.

If `--release` crashes between step 5 (canonical `issue.json` written)
and step 7 (URL append), the next `--release` invocation hits the
idempotency no-op (step 1 sees the canonical `issue.json` and exits).
Recovery is the documented manual path: delete `data/<date>/issue.json`
and re-release. As a safer fallback, the URL-append step is itself
idempotent (union with the existing file), so a future code path that
re-runs only the append against an already-canonical issue would
converge cleanly -- though that path is not exposed as a CLI flag in
v0.

### Interaction with `Cluster.cross_time_ref`

These two mechanisms are complementary, not redundant:

| Concern | Mechanism | Window | Granularity | Source of truth |
|---|---|---|---|---|
| Same **story** appearing twice as if new | `Cluster.cross_time_ref` + LLM callbacks | Last 14 days (active chain) | Cluster (story) | Canonical `data/<date>/clusters.jsonl` + `issue.json` |
| Same **article URL** appearing twice | `data/published_urls.txt` | Forever | Item (URL) | Canonical (staging is invisible) |

A story that runs Monday and gets a substantive follow-up Friday will:
the Friday cluster sets `cross_time_ref` to Monday's cluster id; the
Friday `SummaryBlock` contains the *new* Friday article's URL (not
Monday's); the LLM writes a callback referencing Monday's issue
number. Both mechanisms fire and the reader gets the right experience.

---

## LLM endpoint configuration

The LLM endpoint is **pluggable via `.env`**, so Arman can swap providers
(Anthropic direct, Bedrock, LiteLLM gateway, OpenAI, local Ollama, etc.)
**without code changes**. Module code reads provider/endpoint/key/model
from environment variables and branches on `LLM_PROVIDER` to pick the
right client library. No provider is hard-coded.

This section is consumed by **`src/rank.py`** and **`src/summarise.py`**
(the only modules that call an LLM in v0).

### Required env vars

| Variable | Purpose | Example |
|---|---|---|
| `LLM_PROVIDER` | Provider/protocol id; code branches on this to select the client library. One of `anthropic`, `bedrock`, `openai`, `litellm`, `ollama`. Default: `anthropic`. | `bedrock` |
| `LLM_ENDPOINT` | Base URL of the LLM API. | `https://api.anthropic.com` / `https://bedrock-runtime.us-east-1.amazonaws.com` / `http://localhost:11434` |
| `LLM_API_KEY` | Authentication key/token. **Secret.** Never logged, never committed. For providers that use signed requests (e.g. AWS SigV4 for Bedrock), this slot may hold the appropriate credential bundle or be empty if the client uses ambient AWS credentials. | `sk-ant-…` |
| `LLM_MODEL` | Model identifier as the provider expects it. | `claude-opus-4-7` / `anthropic.claude-3-5-sonnet-20241022-v2:0` / `llama3.1:70b` |

### Optional env vars

| Variable | Default | Purpose |
|---|---|---|
| `LLM_TIMEOUT_SECONDS` | `60` | Per-call wall-clock timeout. |
| `LLM_TEMPERATURE_RANK` | `0.2` | Temperature for `rank.py` calls. Low, for stability of scores across re-runs. |
| `LLM_TEMPERATURE_SUMMARISE` | `0.6` | Temperature for `summarise.py` calls. Higher, to give the voice texture room. |

### Embeddings are NOT covered here

> **Important.** The embedding model is **not** an LLM endpoint. Per the
> Retrieval Engineer's recommendation, embeddings are produced by a
> **local HuggingFace model loaded in-process** in `src/cluster.py`.
> There are **no env vars** for embeddings — no `EMBEDDING_ENDPOINT`,
> no `EMBEDDING_API_KEY`. The model lives on disk (or is downloaded
> once into the HuggingFace cache) and runs locally. This is
> documented separately by Retrieval; called out here so readers do
> not conflate the two configuration surfaces.

If, later, embeddings move to a hosted endpoint (e.g. Bedrock-native
embeddings), this section will grow a parallel `EMBEDDING_*` block. It
does not have one in v0.

### `.env` file conventions

- A `.env` file lives at the repo root and is consumed at local-dev
  time only. It contains real secrets and is excluded from version
  control via `.gitignore` (entry: `.env`).
- A `.env.example` is checked into the repo root as a template. It
  contains placeholder values (e.g. `LLM_ENDPOINT=https://...`) and
  documents every variable above with a short comment. **It must
  never contain real secrets.**
- In CI (GitHub Actions, per Release Engineer's `daily.yml`), env
  vars are injected as **repo secrets**, not via `.env`. The
  workflow's `env:` block maps repo secrets onto the same variable
  names the modules read.

### Loading pattern

- `src/run.py` calls `dotenv.load_dotenv()` (from the `python-dotenv`
  package) **once, at the start of orchestration**, before importing
  or invoking any pipeline stage. This populates `os.environ` from a
  local `.env` if present; in CI the variables are already in the
  process env and `load_dotenv()` is a no-op.
- All downstream modules read configuration via `os.environ` (or
  `os.getenv` with the defaults above). They do **not** call
  `load_dotenv` themselves and they do **not** take API keys as
  function arguments. This keeps the seam clean: one load point,
  one source of truth.
- Modules construct their LLM client from `LLM_PROVIDER` + the
  matching subset of env vars. A small `src/llm_client.py` helper
  (LLM Engineer's surface, not Architect's) is the right place to
  centralise that branch logic; both `rank.py` and `summarise.py`
  import it.

### Logging discipline

`LLM_API_KEY` is treated as a secret throughout. Structured logs may
record `LLM_PROVIDER`, `LLM_MODEL`, `LLM_ENDPOINT` (for audit and
postmortems) but must **never** log `LLM_API_KEY` — not at debug
level, not in error messages, not in tracebacks. Eval Engineer's
module-integrity check includes a grep for accidental key leakage in
log fixtures.

---

## Embedding model

### Recommendation (v0)

**`BAAI/bge-base-en-v1.5`** — the best clustering score among sub-200M-param models on MTEB (45.77), MIT-licensed, 512-token context, no special prefixes or `trust_remote_code` required, loads cleanly via `sentence-transformers`, and runs adequately on CPU for our daily volume.

### Why this one

- **Clustering performance.** MTEB clustering score 45.77 (11-task average, v-measure) — materially better than the MiniLM baseline (~38.8) and roughly equivalent to GTE-base (46.2) and BGE-large (46.08). The ~7-point gap over MiniLM is real for our use case: AI news titles share a lot of surface vocabulary ("model", "agent", "launch"), and stronger embeddings are the difference between `"GPT-5 launches"` and `"Anthropic releases Claude"` landing in the same cluster vs. separate ones.
- **Right-sized for CPU.** 110M params / 768-dim. fp32 on-disk weight ≈ 440MB; runs at roughly 60–120 sentences/sec on a modern CPU core at batch-32 (well within the daily cron budget at 200–1 000 items). GPU if available brings this to thousands/sec.
- **512-token context.** Covers our 30–300 token title+summary inputs comfortably, including the ~600-token outliers. `all-mpnet-base-v2` (the "bigger MiniLM") is eliminated here — its effective max is 128 tokens, which would silently truncate most of our inputs and corrupt cluster quality.
- **No fuss at load time.** `SentenceTransformer("BAAI/bge-base-en-v1.5")` — one line, no `trust_remote_code=True`, no task-instruction prefix needed for clustering (v1.5 dropped the mandatory query prefix from v1). Deterministic, reproducible across runs.
- **MIT license.** Unambiguous for a bank context. No usage restrictions, no "research-only" clauses, no attribution requirements beyond notice.
- **Ages well.** BGE v1.5 is the stable, widely-deployed generation of Beijing Academy of AI's general-purpose embedder — not an experimental release. If BAAI ships a materially better successor in the same weight class, migration is one model-id swap; the pipeline contract doesn't change.

### How it runs

- Loaded in-process via `sentence-transformers` (Apache 2.0):
  ```python
  from sentence_transformers import SentenceTransformer
  model = SentenceTransformer("BAAI/bge-base-en-v1.5")
  ```
- First run downloads the model from HuggingFace into the local cache (~440MB fp32 weights); cached afterwards. On GitHub Actions, add the HF cache dir (`~/.cache/huggingface/`) to the Actions cache key to avoid re-downloading on each cron run.
- Input construction — deterministic, whitespace-safe:
  ```python
  text = f"{item.title}. {item.raw_summary or ''}".strip()
  ```
  The period after the title gives the tokeniser a clean sentence boundary. If `raw_summary` is empty, the title alone is embedded; no special-casing needed.
- Returns a float32 vector of **dim 768**.
- Distance: **cosine similarity** (vectors are L2-normalised by `encode(..., normalize_embeddings=True)`; dot product then equals cosine similarity, which is faster to batch-compute).

### Storage

- Centroids written to `data/YYYY-MM-DD/embeddings/centroids.npz` (per `Cluster.centroid_ref` — see Cluster contract above); keyed by `cluster_id`.
- Item-level embeddings are **not** persisted. Re-embedding 200–1 000 items takes a few seconds on CPU; storing them would add ~2.5MB/day at 500 items (768-dim fp32) and make the archive harder to diff. Cross-time dedup reads only the centroid sidecars from the last 14 days — not item vectors.
- Exception trigger: if profiling reveals that re-embedding the last-14-days items for cross-time dedup is materially slow in practice (unlikely at this scale but possible if item counts grow to 5 000+/day), Retrieval Engineer may revisit persisting item-level embeddings. Document the decision in DESIGN.md at that point.

### Thresholds (initial targets — tune against `evals/fixtures/`)

| Context | Threshold | Rationale |
|---|---|---|
| Same-day clustering | ~0.82 cosine | Tighter than cross-time; same-day items are often near-verbatim across feeds |
| Cross-time dedup | ~0.85 cosine | Higher bar to avoid false continuations; a story must be clearly the same, not just topically similar |

These are starting points. The Eval Engineer's harness against `evals/labels.yaml` is the gate; Retrieval Engineer tunes both thresholds until dedup precision/recall hit the target. Record final tuned values here once locked.

### Alternatives considered

| Model | Clustering MTEB | Params | Dim | Max seq | License | Why not |
|---|---|---|---|---|---|---|
| `sentence-transformers/all-MiniLM-L6-v2` | ~38.8 | 22M | 384 | 256 wp | Apache 2.0 | Baseline floor. 7-point clustering gap vs. BGE-base is significant for jargon-dense AI text. Fine for speed-critical edge deployments; not the right call here. |
| `sentence-transformers/all-mpnet-base-v2` | ~43 | 110M | 768 | **128 tokens** | Apache 2.0 | **Eliminated.** 128-token effective max silently truncates our 30–300 token inputs. A model that truncates most of its inputs is a reliability hazard regardless of benchmark score. |
| `BAAI/bge-small-en-v1.5` | 43.82 | 33M | 384 | 512 | MIT | 2-point clustering gap vs. BGE-base for a 3x speedup. Worth revisiting if Actions CPU budget becomes a bottleneck at 5 000+ items/day; not the right default now. |
| `thenlper/gte-base` | 46.2 | 110M | 768 | 512 | MIT | Essentially tied with BGE-base (46.2 vs. 45.77 — within noise). BGE-base chosen for its larger deployment footprint, more community support, and cleaner sentence-transformers integration. GTE-base is an equally valid swap. |
| `nomic-ai/nomic-embed-text-v1.5` | ~43.9 | 137M | 768 (Matryoshka) | **8192** | Apache 2.0 | Strong long-context story, but requires `trust_remote_code=True` (until transformers v5.5 / sentence-transformers v5.3) and a task prefix (`"clustering: "`) for best results — two sources of operational friction. Clustering score (43.9 from Nomic Embed paper Table 4) is below BGE-base. The 8192-token context is wasted on our 30–300 token inputs. Excellent choice if long-document embedding becomes a requirement later. |
| `mixedbread-ai/mxbai-embed-large-v1` | 46.71 | 335M | 1024 | 512 | Apache 2.0 | Highest verified clustering score in this bracket, but 3x the params of BGE-base (~1.2GB fp32) with no meaningful gain for our use case (+0.94 over BGE-base). The memory and CPU overhead isn't justified at this scale. |
| `BAAI/bge-large-en-v1.5` | 46.08 | 335M | 1024 | 512 | MIT | Same calculus as mxbai-large: 3x the params, +0.31 clustering gain over BGE-base. Not worth the overhead. |
| `intfloat/e5-base-v2` | ~44.2 | 110M | 768 | 512 | MIT | Requires `"query: "` / `"passage: "` prefixes — operational friction, easy to forget in a cluster re-run. BGE-base v1.5 dropped the mandatory prefix and matches or exceeds E5-base on clustering. |
| `Snowflake/snowflake-arctic-embed-m` | N/A (retrieval-optimised) | 110M | 768 | 512 | Apache 2.0 | Optimised for retrieval (NDCG@10 = 54.90), not clustering. No published MTEB clustering score. Not the right tool for the job. |

### Open question — Arman's call

**Is local model download acceptable in the bank context?**

The recommended approach downloads `BAAI/bge-base-en-v1.5` (~440MB) from `huggingface.co` at first run and caches it in `~/.cache/huggingface/`. Two scenarios where this breaks:

1. **Outbound egress to HuggingFace is blocked** on Actions runners (or the dev machine is air-gapped). Mitigation: pre-bake the model weights into the repo's CI cache, into a Docker base image, or into an internal model registry, and point `SENTENCE_TRANSFORMERS_HOME` / `HF_HOME` at the local copy. The pipeline code doesn't change.
2. **Model download at runtime is a compliance concern** (some banks treat any "downloading executable weights from the internet" as equivalent to an unapproved software installation). Mitigation: same as above — ship the weights as an artifact in your approved software delivery process, mount them read-only in the container.

If either scenario applies, the fix is an ops/packaging change, not a model change. Record the decision in this document.

### Phase 1 work

- Implement embed step in `src/cluster.py`:
  - Load `BAAI/bge-base-en-v1.5` once per process, not once per call.
  - Embed `f"{item.title}. {item.raw_summary or ''}".strip()` in batches (batch size 64 is a good default; tune against memory).
  - Same-day agglomerative clustering with cosine threshold ~0.82 (tune against `evals/fixtures/`).
  - Cross-time dedup: load centroid `.npz` sidecars for the last 14 days, compute cosine similarity against today's cluster centroids, set `cross_time_ref` when similarity exceeds ~0.85.
- Write centroid sidecars to `data/YYYY-MM-DD/embeddings/centroids.npz`.
- Tune both thresholds against `evals/labels.yaml` — the Eval Engineer's harness gates this work.
- Document final tuned thresholds in the table above once stable.

---

## Decision log

Each row is one of PLAN §8's open questions plus an Architect recommendation
and a space for Arman's decision. Decisions get logged here when made.

| # | Question | Status | Architect recommendation | Arman's decision |
|---|---|---|---|---|
| 1 | **Language / stack** — Python (feedparser, httpx, pydantic v2, jinja2)? | **Open — strong rec** | **Python.** Locked per PLAN §10. pydantic v2 for the contracts (better perf + `Annotated`/`Field` ergonomics), `feedparser` for RSS/Atom, `httpx` for APIs (HN Algolia, HF Daily Papers), `jinja2` for templates, `numpy` for centroid math, `pyyaml` for configs. Lock the Python version in `pyproject.toml` (recommend 3.11+ — `tomllib`, modern type syntax). |  |
| 2 | **Embeddings model** — which provider available via LiteLLM/Bedrock? | **Open — blocking on platform** | Depends on what Arman's LiteLLM/Bedrock exposes. Architect's preference order: (a) a Bedrock-native embeddings model already available on-prem (lowest egress risk); (b) any solid general-purpose embedder via LiteLLM (Voyage, Cohere embed, OpenAI text-embedding-3 family). Retrieval Engineer decides exact model once the menu is known; threshold (~0.85 cosine) is calibrated *after* model is chosen. **This is one of the §7 day-one questions in disguise.** |  |
| 3 | **Stories per issue** | **Open — rec** | **8–12 ranked stories** distributed across Pulse (1), Where it's heading (2–3), For builders (2–4), For leaders (1–3), plus an **Also notable tail of ≤5** terse links. Slow days: shrink, don't pad. Eval Engineer watches drift (tier mix) over months. |  |
| 4 | **Archive UX** | **Open — rec** | **Flat dated HTML first** (`docs/archive/YYYY-MM-DD.html`). Add an indexed archive page (`docs/archive/index.html`) once we have ~30+ issues — Release Engineer ships it as a small follow-up, not in v0.1. Don't over-engineer the front door before the corpus exists. |  |
| 5 | **Email distribution** | **Out of scope v0** | Confirmed out of scope per PLAN §8. Re-open when the publication has earned a steady reader base on Pages. |  |
| 6 | **Finance-AI sources** (specific feeds for the FS lens) | **Open — Source Engineer's TODO** | Source Engineer owns the candidate list. Architect's request: at minimum 3–5 feeds covering (a) trading/markets ML, (b) fraud/AML/KYC ML, (c) model-risk + governance updates (regulator outputs where they publish feeds), (d) agents-in-finance product news. Trust-weight starts at 2; earns 4–5 over months per `finance-lens.md`. |  |

**For Arman to focus on first**, in order of how blocking they are:

1. **#2 embeddings via LiteLLM/Bedrock** — Retrieval Engineer can't pick a
   threshold without a model. This pairs naturally with the §7 day-one
   asks the Release Engineer is drafting.
2. **#6 finance-AI sources** — Source Engineer needs your input on which
   feeds you actually trust in this space; Architect cannot pick these for
   you.
3. The rest (#1, #3, #4) — Architect's recommendations are defaults the
   team will build against unless Arman says otherwise.

---

## Schema changelog

Bump a record's `schema_version` when its shape changes. Log the diff here.

| Date | Model / artifact | Old version | New version | Diff | Migration |
|---|---|---|---|---|---|
| 2026-05-23 | All models & archive files (Item, Cluster, RankedStory, IssueSection, SummaryBlock, Issue, source_health.json) | — | v1 | Initial schema. | n/a |
| 2026-05-23 | `Issue` | v1 | v2 | Added `issue_number: int` (1-indexed, monotonically increasing, sequential across **published** issues — see [Issue Number Registry](#issue-number-registry)). Derivation rule: `src/run.py` scans `data/*/issue.json` and assigns `max(existing) + 1`; idempotent on same-day re-runs; gaps in the sequence are preserved (not back-filled). | Existing archive: none (no prior `issue.json` files exist in `data/` yet at v0). If applied retroactively to an existing corpus, the migration script would walk `data/YYYY-MM-DD/issue.json` in date order and assign `issue_number = 1, 2, 3, …`. v1 readers tolerate v2 records by ignoring the unknown `issue_number` field (pydantic default) and continuing; per the read-contract rule, they may log a structured warning but must not crash. v2 readers handling v1 records (none in practice) would treat `issue_number` as absent — but since v1 was never shipped to disk, this case will not arise. |
| 2026-05-23 | `data/published_urls.txt` (new derived archive file) | — | n/a (not a versioned schema; plain text, one URL per line) | New file. Cumulative URL exclusion index. Written by `src/render.py` after ratify+ship; read by `src/cluster.py` and `src/rank.py`. See [Cross-issue article-level dedup](#cross-issue-article-level-dedup). | n/a — first introduction. Missing-file tolerance: readers treat a missing `data/published_urls.txt` as an empty set (first-ever run, or fresh checkout). |
| 2026-05-23 | `Issue` | v2 | v3 | Made `issue_number` **Optional** (`int \| None`, default `None`). Introduces the [Archive: staging vs canonical](#archive-staging-vs-canonical) split: every engine run writes to `data/staging/<date>/` with `issue_number = None`; `python -m src.run --release` promotes staging to canonical (`data/<date>/`) and assigns the number at that moment (`max(canonical issue_numbers) + 1`). Numbering is now a release-time operation, not a summarise-time one. Cross-time dedup, callbacks, and `data/published_urls.txt` all read canonical only -- staging is invisible to history. | Existing archive: none in canonical yet at v0, so no on-disk migration required. v2 readers handling v3 records reject `null` `issue_number` (pydantic would refuse `None` for a required `int`); since no v2 issues exist in canonical and staging is a fresh path, no v2 reader will encounter a v3 staging record. v3 readers handling v2 records accept the integer transparently (Optional permits the integer case). The `Issue` validator no longer enforces `issue_number >= 1` as a required invariant; the `ge=1` constraint applies only when `issue_number is not None`. |
| 2026-05-23 | Archive layout (paths, not schema) | flat `data/<date>/` | split `data/<date>/` (canonical) + `data/staging/<date>/` (working) | New parallel write path under `data/staging/`. Same five files + embeddings sidecar, same atomic-write rules, same shape. Default engine write target is now staging; canonical is written only by `--release`. See [Archive: staging vs canonical](#archive-staging-vs-canonical). | n/a — first introduction. Round B (a follow-up refactor PR) updates `src/fetch.py`, `src/cluster.py`, `src/rank.py`, `src/summarise.py`, `src/render.py`, `src/run.py` to write to staging by default and to expose `--release`. Until Round B lands, the on-disk layout still matches the pre-staging behaviour; this contract specifies the target state. |
