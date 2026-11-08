r"""
src/cost.py -- LLM spend reporting for ``aiv cost``.

What it answers
---------------
"What did the newsletter cost to produce -- per day, per week, per month --
and does that match what Anthropic billed?"

Where the numbers come from
---------------------------
Every LLM call in the pipeline funnels through ``src.rank._llm_call``, which
records the token counts the API returns (the ``usage`` block on each
response) into ``src.llm_usage``; ``aiv run`` writes that accumulator to
``data/metrics_log.jsonl`` as one record per run. This module reads those
records back. The ``cost_usd`` values are the ones stored at run time, not
recomputed, so a later price-table change never rewrites history.

Two kinds of "real":

* ``cost_usd`` in the log = API-reported tokens x published list price. The
  Messages API never returns dollars; Anthropic bills from the same token
  counts, so this is exact to the cent when the price table in
  ``src/llm_usage.py`` is current and the call ran at the standard tier.
* ``--reconcile`` = what Anthropic actually billed, per UTC day, from the
  Usage and Cost Analytics API. It needs an Admin API key (the Admin API is
  not available on individual accounts), it covers every call made with the
  organisation's keys (the eval judge, Claude Code sessions, ...), and it
  lands up to 24 h late. Set ``AIV_ANTHROPIC_API_KEY_ID`` to the
  ``apikey_...`` id of the key the pipeline uses to narrow it to this
  newsletter.

No LLM calls, no judgment -- counters, dates and a table ("No Token Wasted").

Owner: Release Engineer (observability surface, docs/internal/TEAM.md).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

from src import paths

_LOG = logging.getLogger("ai_vector.cost")

GRANULARITIES: tuple[str, ...] = ("day", "week", "month")
"""Bucket sizes ``--by`` accepts."""

DEFAULT_LAST: dict[str, int] = {"day": 30, "week": 12, "month": 6}
"""How many buckets ``aiv cost`` reports when neither --last nor --from is given."""

DATE_FIELDS: tuple[str, ...] = ("issue", "run")
"""``issue`` dates a run by the issue it built; ``run`` by when it ran (UTC)."""

TOKEN_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

# ---------------------------------------------------------------------------
# Usage and Cost Analytics API (Admin API, beta). Called over raw HTTP: the
# installed anthropic SDK has no binding for it. Request and response shapes
# per platform.claude.com/docs/en/manage-claude/usage-cost-analytics-api
# (read 2026-11-08): daily UTC buckets, amounts as decimal strings in
# cents, at most 31 days per request, cursor pagination via next_page.
# ---------------------------------------------------------------------------
ANALYTICS_COST_URL = "https://api.anthropic.com/v1/organizations/analytics/cost"
ANTHROPIC_VERSION = "2026-10-21"
ANALYTICS_BETA = "analytics-usage-cost-2026-08-24"
ADMIN_KEY_ENV = "ANTHROPIC_ADMIN_KEY"
API_KEY_ID_ENV = "AIV_ANTHROPIC_API_KEY_ID"
_MAX_RANGE_DAYS = 31
_EARLIEST_ANALYTICS_DATE = _dt.date(2026, 1, 1)
_LOOKBACK_LIMIT_DAYS = 364
_USER_AGENT = "ai-vector/aiv-cost (https://aivector.day)"


# ---------------------------------------------------------------------------
# Reading the log
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunRecord:
    """One ``aiv run`` that made at least one LLM call."""

    issue_date: _dt.date
    run_at: _dt.datetime
    stages: dict[str, dict[str, Any]]
    total: dict[str, Any]

    @property
    def run_date(self) -> _dt.date:
        """Calendar day the run happened, in UTC (Anthropic bills by UTC day)."""
        return self.run_at.astimezone(_dt.timezone.utc).date()

    @property
    def cost_usd(self) -> float | None:
        """Stored run cost; None when a model was missing from the price table."""
        return self.total.get("cost_usd")

    def date_for(self, date_field: str) -> _dt.date:
        return self.run_date if date_field == "run" else self.issue_date


@dataclass
class LoadResult:
    runs: list[RunRecord]
    untracked_runs: int = 0
    """Records written before usage tracking existed (no ``llm_usage`` key)."""
    idle_runs: int = 0
    """Records with usage tracking but zero tokens (e.g. a render-only run)."""
    malformed_lines: int = 0


def metrics_log_path() -> Path:
    return paths.DATA_ROOT / "metrics_log.jsonl"


def load_runs(path: Path | None = None) -> LoadResult:
    """Parse ``metrics_log.jsonl`` into run records. Never raises on a bad
    line -- it is counted and skipped, the way the log's other readers do."""
    log_path = path if path is not None else metrics_log_path()
    result = LoadResult(runs=[])
    if not log_path.exists():
        return result
    with log_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                issue_date = _dt.date.fromisoformat(rec["date"])
                run_at = _dt.datetime.fromisoformat(rec["run_at"])
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                result.malformed_lines += 1
                continue
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=_dt.timezone.utc)
            usage = rec.get("llm_usage")
            if not isinstance(usage, dict):
                result.untracked_runs += 1
                continue
            total = usage.get("total") or {}
            if sum(int(total.get(f, 0) or 0) for f in TOKEN_FIELDS) == 0:
                result.idle_runs += 1
                continue
            result.runs.append(RunRecord(
                issue_date=issue_date,
                run_at=run_at,
                stages=usage.get("stages") or {},
                total=total,
            ))
    return result


# ---------------------------------------------------------------------------
# Calendar buckets
# ---------------------------------------------------------------------------

def bucket_start(day: _dt.date, by: str) -> _dt.date:
    """First day of the bucket ``day`` falls in. Weeks start on Monday."""
    if by == "day":
        return day
    if by == "week":
        return day - _dt.timedelta(days=day.weekday())
    if by == "month":
        return day.replace(day=1)
    raise ValueError(f"--by must be one of {', '.join(GRANULARITIES)} (got {by!r})")


def bucket_end(start: _dt.date, by: str) -> _dt.date:
    """Last day (inclusive) of the bucket that starts on ``start``."""
    if by == "day":
        return start
    if by == "week":
        return start + _dt.timedelta(days=6)
    if by == "month":
        next_month = (start.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
        return next_month - _dt.timedelta(days=1)
    raise ValueError(f"--by must be one of {', '.join(GRANULARITIES)} (got {by!r})")


def bucket_label(start: _dt.date, by: str) -> str:
    if by == "day":
        return start.isoformat()
    if by == "week":
        iso_year, iso_week, _ = start.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if by == "month":
        return start.strftime("%Y-%m")
    raise ValueError(f"--by must be one of {', '.join(GRANULARITIES)} (got {by!r})")


def shift_bucket(start: _dt.date, by: str, back: int) -> _dt.date:
    """Start of the bucket ``back`` buckets before the one starting at ``start``."""
    if back < 0:
        raise ValueError("back must be >= 0")
    if by == "day":
        return start - _dt.timedelta(days=back)
    if by == "week":
        return start - _dt.timedelta(weeks=back)
    if by == "month":
        months = start.year * 12 + (start.month - 1) - back
        return _dt.date(months // 12, months % 12 + 1, 1)
    raise ValueError(f"--by must be one of {', '.join(GRANULARITIES)} (got {by!r})")


def period_bounds(by: str, last: int, today: _dt.date) -> tuple[_dt.date, _dt.date]:
    """Inclusive date range covering the last ``last`` buckets, ending today."""
    if last < 1:
        raise ValueError("--last must be at least 1")
    return shift_bucket(bucket_start(today, by), by, last - 1), today


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _empty_tokens() -> dict[str, int]:
    return {f: 0 for f in TOKEN_FIELDS}


@dataclass
class Bucket:
    label: str
    start: _dt.date
    end: _dt.date
    runs: int = 0
    days: set[_dt.date] = field(default_factory=set)
    tokens: dict[str, int] = field(default_factory=_empty_tokens)
    cost_usd: float = 0.0
    unknown_cost_runs: int = 0
    by_stage: dict[str, float] = field(default_factory=dict)
    by_model: dict[str, dict[str, Any]] = field(default_factory=dict)
    billed_usd: Decimal | None = None

    def add(self, run: RunRecord, day: _dt.date) -> None:
        self.runs += 1
        self.days.add(day)
        for f in TOKEN_FIELDS:
            self.tokens[f] += int(run.total.get(f, 0) or 0)
        if run.cost_usd is None:
            self.unknown_cost_runs += 1
        else:
            self.cost_usd += float(run.cost_usd)
        for stage, snap in run.stages.items():
            stage_cost = snap.get("cost_usd")
            if stage_cost is not None:
                self.by_stage[stage] = self.by_stage.get(stage, 0.0) + float(stage_cost)
            for model, m in (snap.get("models") or {}).items():
                entry = self.by_model.setdefault(
                    model, {**_empty_tokens(), "cost_usd": 0.0, "cost_known": True}
                )
                for f in TOKEN_FIELDS:
                    entry[f] += int(m.get(f, 0) or 0)
                if m.get("cost_usd") is None:
                    entry["cost_known"] = False
                else:
                    entry["cost_usd"] += float(m["cost_usd"])

    @property
    def delta_usd(self) -> Decimal | None:
        """Billed minus computed; None until reconciled."""
        if self.billed_usd is None:
            return None
        return self.billed_usd - Decimal(str(round(self.cost_usd, 4)))

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "bucket": self.label,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "runs": self.runs,
            "days_with_runs": len(self.days),
            **self.tokens,
            "cost_usd": round(self.cost_usd, 4),
            "unknown_cost_runs": self.unknown_cost_runs,
            "by_stage": {k: round(v, 4) for k, v in sorted(self.by_stage.items())},
            "by_model": {
                k: {**{f: v[f] for f in TOKEN_FIELDS},
                    "cost_usd": round(v["cost_usd"], 4) if v["cost_known"] else None}
                for k, v in sorted(self.by_model.items())
            },
        }
        if self.billed_usd is not None:
            out["billed_usd"] = float(self.billed_usd)
            out["delta_usd"] = float(self.delta_usd)  # type: ignore[arg-type]
        return out


@dataclass
class BilledReport:
    """Anthropic's billed cost per UTC day, from the Analytics API."""

    by_day: dict[_dt.date, Decimal]
    data_refreshed_at: str | None
    scope: str
    requests: int
    clamped_start: _dt.date | None = None


@dataclass
class CostReport:
    by: str
    date_field: str
    start: _dt.date
    end: _dt.date
    buckets: list[Bucket]
    total: Bucket
    untracked_runs: int
    idle_runs: int
    malformed_lines: int
    billed: BilledReport | None = None
    billed_unlogged_usd: Decimal | None = None
    """Billed on days inside the period that have no logged run (judge,
    Claude Code, ad-hoc calls) -- only meaningful when ``by == "day"``."""

    def to_json(self, log_path: Path | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": str(log_path or metrics_log_path()),
            "by": self.by,
            "date_field": self.date_field,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "buckets": [b.to_json() for b in self.buckets],
            "total": self.total.to_json(),
            "untracked_runs": self.untracked_runs,
            "idle_runs": self.idle_runs,
            "malformed_lines": self.malformed_lines,
        }
        if self.billed is not None:
            out["billed"] = {
                "scope": self.billed.scope,
                "data_refreshed_at": self.billed.data_refreshed_at,
                "total_usd": float(sum(self.billed.by_day.values(), Decimal(0))),
                "unlogged_days_usd": (
                    float(self.billed_unlogged_usd)
                    if self.billed_unlogged_usd is not None else None
                ),
                "clamped_start": (
                    self.billed.clamped_start.isoformat()
                    if self.billed.clamped_start else None
                ),
            }
        return out


def aggregate(
    load: LoadResult,
    *,
    start: _dt.date,
    end: _dt.date,
    by: str,
    date_field: str = "issue",
) -> CostReport:
    """Bucket the runs dated inside ``[start, end]`` (inclusive)."""
    if by not in GRANULARITIES:
        raise ValueError(f"--by must be one of {', '.join(GRANULARITIES)} (got {by!r})")
    if date_field not in DATE_FIELDS:
        raise ValueError(f"date_field must be one of {DATE_FIELDS} (got {date_field!r})")
    if start > end:
        raise ValueError(f"period start {start} is after its end {end}")

    buckets: dict[_dt.date, Bucket] = {}
    total = Bucket(label="TOTAL", start=start, end=end)
    for run in load.runs:
        day = run.date_for(date_field)
        if day < start or day > end:
            continue
        b_start = bucket_start(day, by)
        bucket = buckets.get(b_start)
        if bucket is None:
            bucket = Bucket(
                label=bucket_label(b_start, by),
                start=b_start,
                end=bucket_end(b_start, by),
            )
            buckets[b_start] = bucket
        bucket.add(run, day)
        total.add(run, day)
    return CostReport(
        by=by,
        date_field=date_field,
        start=start,
        end=end,
        buckets=[buckets[k] for k in sorted(buckets)],
        total=total,
        untracked_runs=load.untracked_runs,
        idle_runs=load.idle_runs,
        malformed_lines=load.malformed_lines,
    )


# ---------------------------------------------------------------------------
# Reconciliation against Anthropic's bill
# ---------------------------------------------------------------------------

class CostApiError(RuntimeError):
    """The Analytics API refused or failed a request. Never carries the key."""


HttpGet = Callable[[str, dict[str, Any], dict[str, str]], tuple[int, Any]]
"""``(url, params, headers) -> (status_code, decoded_json_body)``."""


def _default_http_get(url: str, params: dict[str, Any], headers: dict[str, str]) -> tuple[int, Any]:
    import httpx

    resp = httpx.get(url, params=params, headers=headers, timeout=30.0)
    try:
        body: Any = resp.json()
    except ValueError:
        body = {"raw": resp.text[:500]}
    return resp.status_code, body


def _windows(start: _dt.date, end: _dt.date) -> list[tuple[_dt.date, _dt.date]]:
    """Split an inclusive date range into chunks of at most 31 days."""
    out: list[tuple[_dt.date, _dt.date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + _dt.timedelta(days=_MAX_RANGE_DAYS - 1))
        out.append((cursor, chunk_end))
        cursor = chunk_end + _dt.timedelta(days=1)
    return out


def _describe_api_error(status: int, body: Any) -> str:
    message = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or "")
        elif "raw" in body:
            message = str(body["raw"])
    hint = ""
    if status in (401, 403):
        hint = (
            " The Analytics API needs an Admin API key (sk-ant-admin...) from an "
            "organisation account; a workspace key or a key on an individual "
            "account is refused."
        )
    return f"Anthropic Analytics API returned HTTP {status}.{hint} {message}".strip()


def fetch_billed_by_day(
    start: _dt.date,
    end: _dt.date,
    *,
    admin_key: str,
    api_key_id: str | None = None,
    http_get: HttpGet | None = None,
    today: _dt.date | None = None,
) -> BilledReport:
    """Billed USD per UTC day for ``[start, end]`` from the Analytics API.

    Amounts are the post-discount, pre-credit cost (the ``amount`` field),
    converted from cents. With ``api_key_id`` the query is narrowed to
    that key; without it the figure is the whole organisation's.
    """
    get = http_get or _default_http_get
    today = today or _dt.datetime.now(_dt.timezone.utc).date()
    floor = max(_EARLIEST_ANALYTICS_DATE, today - _dt.timedelta(days=_LOOKBACK_LIMIT_DAYS))
    clamped: _dt.date | None = None
    if start < floor:
        clamped = floor
        start = floor
    if start > end:
        return BilledReport({}, None, _scope_label(api_key_id), 0, clamped)

    headers = {
        "x-api-key": admin_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "anthropic-beta": ANALYTICS_BETA,
        "user-agent": _USER_AGENT,
    }
    by_day: dict[_dt.date, Decimal] = {}
    refreshed: str | None = None
    requests = 0
    for w_start, w_end in _windows(start, end):
        page: str | None = None
        while True:
            params: dict[str, Any] = {
                "starting_at": f"{w_start.isoformat()}T00:00:00Z",
                "ending_at": f"{(w_end + _dt.timedelta(days=1)).isoformat()}T00:00:00Z",
                "time_bucket_width": "1d",
                "order_by": "time",
                "order": "asc",
            }
            if api_key_id:
                params["api_key_ids[]"] = api_key_id
            if page:
                params["page"] = page
            status, body = get(ANALYTICS_COST_URL, params, headers)
            requests += 1
            if status != 200:
                raise CostApiError(_describe_api_error(status, body))
            if not isinstance(body, dict):
                raise CostApiError("Anthropic Analytics API returned a non-object body")
            for row in body.get("data") or []:
                try:
                    day = _dt.date.fromisoformat(str(row["starting_at"])[:10])
                    cents = Decimal(str(row["cost"]["amount"]))
                except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                    raise CostApiError(f"unexpected row shape in Analytics API response: {exc}")
                by_day[day] = by_day.get(day, Decimal(0)) + cents / Decimal(100)
            refreshed = body.get("data_refreshed_at") or refreshed
            next_page = body.get("next_page")
            if body.get("has_more") and next_page:
                page = str(next_page)
                continue
            break
    return BilledReport(by_day, refreshed, _scope_label(api_key_id), requests, clamped)


def _scope_label(api_key_id: str | None) -> str:
    return f"API key {api_key_id}" if api_key_id else "whole organisation"


def attach_billed(report: CostReport, billed: BilledReport) -> None:
    """Fill each day bucket's ``billed_usd`` and the unlogged-days total."""
    if report.by != "day":
        raise ValueError("reconciliation is day by day; aggregate with by='day'")
    logged_days = {b.start for b in report.buckets}
    for bucket in report.buckets:
        bucket.billed_usd = billed.by_day.get(bucket.start, Decimal(0))
    report.total.billed_usd = sum(
        (b.billed_usd for b in report.buckets if b.billed_usd is not None), Decimal(0)
    )
    report.billed_unlogged_usd = sum(
        (amt for day, amt in billed.by_day.items()
         if report.start <= day <= report.end and day not in logged_days),
        Decimal(0),
    )
    report.billed = billed


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _fmt_usd(x: float | Decimal | None, *, unknown: bool = False) -> str:
    if x is None:
        return "-"
    s = f"${float(x):,.2f}"
    return s + "?" if unknown else s


def _fmt_delta(x: Decimal | None) -> str:
    if x is None:
        return "-"
    sign = "+" if x >= 0 else "-"
    return f"{sign}${abs(float(x)):,.2f}"


def render_report(report: CostReport, *, log_path: Path | None = None) -> str:
    lines: list[str] = []
    dated = "issue day" if report.date_field == "issue" else "run day (UTC)"
    lines.append("AI Vector -- LLM COST")
    lines.append(f"source : {log_path or metrics_log_path()} "
                 "(API-reported tokens x price table at run time)")
    lines.append(f"period : {report.start} .. {report.end}, by {report.by}, dated by {dated}")
    t = report.total
    lines.append(f"runs   : {t.runs} across {len(t.days)} days")
    if report.untracked_runs or report.idle_runs or report.malformed_lines:
        bits = []
        if report.untracked_runs:
            bits.append(f"{report.untracked_runs} before usage tracking (2026-07-04)")
        if report.idle_runs:
            bits.append(f"{report.idle_runs} with no LLM calls")
        if report.malformed_lines:
            bits.append(f"{report.malformed_lines} unreadable lines")
        lines.append(f"skipped: {', '.join(bits)} (whole log, not just this period)")
    lines.append("")

    if not report.buckets:
        lines.append("No runs with LLM usage in this period.")
        return "\n".join(lines)

    reconciled = report.billed is not None
    label_w = max(10, max(len(b.label) for b in report.buckets), len("TOTAL"))
    header = (f"{'bucket':<{label_w}} {'runs':>5} {'input':>8} {'output':>8} "
              f"{'cache-w':>8} {'cache-r':>8} {'cost':>10}")
    if reconciled:
        header += f" {'billed':>10} {'delta':>9}"
    lines.append(header)
    lines.append("-" * len(header))

    def row(b: Bucket) -> str:
        s = (f"{b.label:<{label_w}} {b.runs:>5} "
             f"{_fmt_tokens(b.tokens['input_tokens']):>8} "
             f"{_fmt_tokens(b.tokens['output_tokens']):>8} "
             f"{_fmt_tokens(b.tokens['cache_creation_input_tokens']):>8} "
             f"{_fmt_tokens(b.tokens['cache_read_input_tokens']):>8} "
             f"{_fmt_usd(b.cost_usd, unknown=b.unknown_cost_runs > 0):>10}")
        if reconciled:
            s += f" {_fmt_usd(b.billed_usd):>10} {_fmt_delta(b.delta_usd):>9}"
        return s

    for b in report.buckets:
        lines.append(row(b))
    lines.append("-" * len(header))
    lines.append(row(t))

    day_costs = [b.cost_usd for b in report.buckets] if report.by == "day" else None
    if report.by == "day" and day_costs:
        lines.append(
            f"per day with a run: avg {_fmt_usd(sum(day_costs) / len(day_costs))}, "
            f"min {_fmt_usd(min(day_costs))}, max {_fmt_usd(max(day_costs))}"
        )
    elif len(t.days) > 0:
        lines.append(
            f"per day with a run: avg {_fmt_usd(t.cost_usd / len(t.days))} "
            f"over {len(t.days)} days"
        )
    if t.unknown_cost_runs:
        lines.append(
            f"? = {t.unknown_cost_runs} run(s) used a model missing from the price "
            "table in src/llm_usage.py; their tokens are counted, their cost is not."
        )
    lines.append("")

    if t.by_stage:
        parts = []
        for stage, usd in sorted(t.by_stage.items(), key=lambda kv: -kv[1]):
            share = (usd / t.cost_usd * 100) if t.cost_usd else 0.0
            parts.append(f"{stage} {_fmt_usd(usd)} ({share:.0f}%)")
        lines.append("by stage : " + " | ".join(parts))
    if t.by_model:
        parts = []
        for model, m in sorted(t.by_model.items(), key=lambda kv: -(kv[1]['cost_usd'])):
            parts.append(f"{model} {_fmt_usd(m['cost_usd'], unknown=not m['cost_known'])}")
        note = ""
        model_runs_cost = sum(m["cost_usd"] for m in t.by_model.values())
        if t.cost_usd and model_runs_cost < t.cost_usd * 0.999:
            note = f" | {_fmt_usd(t.cost_usd - model_runs_cost)} from runs logged before model attribution"
        lines.append("by model : " + " | ".join(parts) + note)
    elif t.runs:
        lines.append("by model : not recorded for these runs (model attribution "
                     "in the log starts with runs made after this command landed)")

    if reconciled and report.billed is not None:
        lines.append("")
        lines.append(f"Anthropic bill: {report.billed.scope}, "
                     f"total {_fmt_usd(t.billed_usd)} vs computed {_fmt_usd(t.cost_usd)} "
                     f"(delta {_fmt_delta(t.delta_usd)})")
        if report.billed_unlogged_usd is not None and report.billed_unlogged_usd > 0:
            lines.append(f"  billed on days with no logged run: "
                         f"{_fmt_usd(report.billed_unlogged_usd)} (eval judge, Claude Code, "
                         "ad-hoc calls -- not in metrics_log.jsonl)")
        if report.billed.scope == "whole organisation":
            lines.append(f"  set {API_KEY_ID_ENV}=apikey_... in .env to narrow the bill "
                         "to the key the pipeline uses")
        if report.billed.data_refreshed_at:
            lines.append(f"  data refreshed at {report.billed.data_refreshed_at}; later "
                         "usage is missing or partial, and a month settles ~7 days after it ends")
        if report.billed.clamped_start:
            lines.append(f"  the API only reaches back to {report.billed.clamped_start}; "
                         "earlier days show $0.00 billed")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command body (``aiv cost`` in src/run.py is a thin wrapper around this)
# ---------------------------------------------------------------------------

def _parse_date(value: str, flag: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{flag} must be YYYY-MM-DD (got {value!r}): {exc}") from exc


def resolve_period(
    *,
    by: str,
    last: int | None,
    start: str | None,
    end: str | None,
    today: _dt.date,
) -> tuple[_dt.date, _dt.date]:
    """Turn the CLI's period flags into an inclusive date range.

    ``--from``/``--to`` win over ``--last``; a lone ``--from`` runs to today,
    a lone ``--to`` reaches back the default number of buckets.
    """
    if by not in GRANULARITIES:
        raise ValueError(f"--by must be one of {', '.join(GRANULARITIES)} (got {by!r})")
    if start is not None or end is not None:
        if last is not None:
            raise ValueError("--last cannot be combined with --from/--to")
        end_date = _parse_date(end, "--to") if end is not None else today
        if start is not None:
            start_date = _parse_date(start, "--from")
        else:
            start_date = shift_bucket(bucket_start(end_date, by), by, DEFAULT_LAST[by] - 1)
        if start_date > end_date:
            raise ValueError(f"--from {start_date} is after --to {end_date}")
        return start_date, end_date
    return period_bounds(by, last if last is not None else DEFAULT_LAST[by], today)


def run_cost(
    *,
    by: str = "day",
    last: int | None = None,
    start: str | None = None,
    end: str | None = None,
    by_run_date: bool = False,
    reconcile: bool = False,
    json_out: bool = False,
    today: _dt.date | None = None,
    env: Mapping[str, str] | None = None,
    http_get: HttpGet | None = None,
    log_path: Path | None = None,
) -> int:
    """Build and print the report. Returns the process exit code."""
    env = os.environ if env is None else env
    today = today or _dt.date.today()
    try:
        period_start, period_end = resolve_period(
            by=by, last=last, start=start, end=end, today=today,
        )
        if reconcile and by != "day":
            raise ValueError("--reconcile compares day by day against the bill; use --by day")
        date_field = "run" if (by_run_date or reconcile) else "issue"
        path = log_path if log_path is not None else metrics_log_path()
        report = aggregate(
            load_runs(path), start=period_start, end=period_end, by=by, date_field=date_field,
        )
        if reconcile:
            admin_key = (env.get(ADMIN_KEY_ENV) or "").strip()
            if not admin_key:
                raise ValueError(
                    f"--reconcile needs {ADMIN_KEY_ENV} in .env (an Admin API key, "
                    "sk-ant-admin..., created by an organisation admin in the Claude Console)"
                )
            billed = fetch_billed_by_day(
                period_start, period_end,
                admin_key=admin_key,
                api_key_id=(env.get(API_KEY_ID_ENV) or "").strip() or None,
                http_get=http_get,
            )
            attach_billed(report, billed)
    except (ValueError, CostApiError) as exc:
        _LOG.error("cost: %s", exc)
        return 1

    if json_out:
        print(json.dumps(report.to_json(path), indent=2, sort_keys=False))
    else:
        if reconcile and not by_run_date:
            print("note: --reconcile dates runs by when they ran (UTC) to line up with the bill")
        print(render_report(report, log_path=path))
    return 0
