"""Tests for src/cost.py -- the ``aiv cost`` spend report.

The report is pure bookkeeping over ``data/metrics_log.jsonl`` plus an
optional read of Anthropic's Analytics API. Every test here pins a
behaviour the operator relies on: which records count, how calendar
buckets fall, that unknown prices are never guessed, and that the
reconciliation request is shaped the way the API documents.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from src import cost
from src import run as run_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rec(
    date: str,
    run_at: str,
    stages: dict[str, tuple[int, int, int, int, float | None]],
    *,
    models: dict[str, dict[str, dict]] | None = None,
) -> str:
    """One metrics_log line. ``stages`` maps a stage to
    ``(input, output, cache_write, cache_read, cost_usd)``."""
    stage_snaps: dict[str, dict] = {}
    total = {f: 0 for f in cost.TOKEN_FIELDS}
    total_cost = 0.0
    unknown = False
    for name, (inp, out, cw, cr, usd) in stages.items():
        snap = {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_creation_input_tokens": cw,
            "cache_read_input_tokens": cr,
            "cost_usd": usd,
        }
        if models and name in models:
            snap["models"] = models[name]
        stage_snaps[name] = snap
        total["input_tokens"] += inp
        total["output_tokens"] += out
        total["cache_creation_input_tokens"] += cw
        total["cache_read_input_tokens"] += cr
        if usd is None:
            unknown = True
        else:
            total_cost += usd
    record = {
        "date": date,
        "run_at": run_at,
        "stages_succeeded": list(stages),
        "failed_stage": None,
        "elapsed_s": 1.0,
        "llm_usage": {
            "stages": stage_snaps,
            "total": {**total, "cost_usd": None if unknown else round(total_cost, 4)},
        },
    }
    return json.dumps(record)


def _write_log(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _load(tmp_path: Path, lines: list[str]) -> cost.LoadResult:
    return cost.load_runs(_write_log(tmp_path / "metrics_log.jsonl", lines))


RANK = {"rank": (100_000, 5_000, 0, 0, 1.00)}


# ---------------------------------------------------------------------------
# Reading the log
# ---------------------------------------------------------------------------

class TestLoadRuns:
    def test_counts_only_records_with_llm_usage_and_tokens(self, tmp_path):
        pre_tracking = json.dumps({"date": "2026-06-01", "run_at": "2026-06-01T08:00:00+00:00"})
        idle = _rec("2026-10-02", "2026-10-02T08:00:00+00:00", {})
        good = _rec("2026-10-03", "2026-10-03T08:00:00+00:00", RANK)
        loaded = _load(tmp_path, [pre_tracking, idle, "not json {", good])
        assert [r.issue_date for r in loaded.runs] == [dt.date(2026, 10, 3)]
        assert loaded.untracked_runs == 1
        assert loaded.idle_runs == 1
        assert loaded.malformed_lines == 1

    def test_missing_log_is_empty_not_an_error(self, tmp_path):
        loaded = cost.load_runs(tmp_path / "nope.jsonl")
        assert loaded.runs == [] and loaded.untracked_runs == 0

    def test_run_date_is_the_utc_day_the_issue_date_is_the_issue(self, tmp_path):
        # 23:30 in UTC-5 is already the next day in UTC -- the day Anthropic bills.
        loaded = _load(tmp_path, [_rec("2026-10-31", "2026-10-31T23:30:00-05:00", RANK)])
        run = loaded.runs[0]
        assert run.issue_date == dt.date(2026, 10, 31)
        assert run.run_date == dt.date(2026, 11, 1)
        assert run.date_for("issue") == dt.date(2026, 10, 31)
        assert run.date_for("run") == dt.date(2026, 11, 1)


# ---------------------------------------------------------------------------
# Calendar buckets
# ---------------------------------------------------------------------------

class TestBuckets:
    def test_week_starts_monday_and_is_labelled_by_iso_week(self):
        wednesday = dt.date(2026, 10, 28)
        start = cost.bucket_start(wednesday, "week")
        assert start == dt.date(2026, 10, 26)
        assert cost.bucket_end(start, "week") == dt.date(2026, 11, 1)
        assert cost.bucket_label(start, "week") == "2026-W44"

    def test_month_bucket_ends_on_the_last_day(self):
        start = cost.bucket_start(dt.date(2026, 2, 17), "month")
        assert start == dt.date(2026, 2, 1)
        assert cost.bucket_end(start, "month") == dt.date(2026, 2, 28)
        assert cost.bucket_label(start, "month") == "2026-02"

    def test_shift_month_crosses_the_year_boundary(self):
        assert cost.shift_bucket(dt.date(2026, 2, 1), "month", 3) == dt.date(2025, 11, 1)

    def test_period_bounds_cover_the_last_n_buckets_ending_today(self):
        today = dt.date(2026, 11, 8)  # a Sunday
        assert cost.period_bounds("day", 30, today) == (dt.date(2026, 10, 10), today)
        assert cost.period_bounds("week", 2, today) == (dt.date(2026, 10, 26), today)
        assert cost.period_bounds("month", 6, today) == (dt.date(2026, 6, 1), today)

    def test_unknown_granularity_is_rejected(self):
        with pytest.raises(ValueError, match="--by"):
            cost.bucket_start(dt.date(2026, 1, 1), "fortnight")


class TestResolvePeriod:
    TODAY = dt.date(2026, 11, 8)

    def test_from_alone_runs_to_today(self):
        assert cost.resolve_period(
            by="day", last=None, start="2026-10-01", end=None, today=self.TODAY,
        ) == (dt.date(2026, 10, 1), self.TODAY)

    def test_to_alone_reaches_back_the_default_number_of_buckets(self):
        start, end = cost.resolve_period(
            by="month", last=None, start=None, end="2026-10-31", today=self.TODAY,
        )
        assert (start, end) == (dt.date(2026, 5, 1), dt.date(2026, 10, 31))

    def test_last_and_explicit_dates_are_mutually_exclusive(self):
        with pytest.raises(ValueError, match="--last"):
            cost.resolve_period(by="day", last=7, start="2026-10-01", end=None, today=self.TODAY)

    def test_from_after_to_is_rejected(self):
        with pytest.raises(ValueError, match="after"):
            cost.resolve_period(
                by="day", last=None, start="2026-10-05", end="2026-10-01", today=self.TODAY,
            )

    def test_malformed_date_names_the_flag(self):
        with pytest.raises(ValueError, match="--from"):
            cost.resolve_period(by="day", last=None, start="31/10/2026", end=None, today=self.TODAY)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

class TestAggregate:
    def test_runs_on_the_same_issue_day_sum_into_one_bucket(self, tmp_path):
        loaded = _load(tmp_path, [
            _rec("2026-10-02", "2026-10-02T08:00:00+00:00",
                 {"rank": (100_000, 5_000, 1_000, 2_000, 1.00),
                  "summarise": (50_000, 2_000, 0, 0, 0.50)}),
            _rec("2026-10-02", "2026-10-02T12:00:00+00:00",
                 {"summarise": (60_000, 3_000, 0, 0, 0.60)}),
        ])
        report = cost.aggregate(
            loaded, start=dt.date(2026, 10, 1), end=dt.date(2026, 10, 31), by="day",
        )
        assert [b.label for b in report.buckets] == ["2026-10-02"]
        bucket = report.buckets[0]
        assert bucket.runs == 2
        assert bucket.tokens["input_tokens"] == 210_000
        assert bucket.tokens["cache_creation_input_tokens"] == 1_000
        assert bucket.cost_usd == pytest.approx(2.10)
        assert bucket.by_stage == {"rank": pytest.approx(1.00), "summarise": pytest.approx(1.10)}
        assert report.total.cost_usd == pytest.approx(2.10)
        assert len(report.total.days) == 1

    def test_period_is_inclusive_at_both_ends(self, tmp_path):
        loaded = _load(tmp_path, [
            _rec("2026-09-30", "2026-09-30T08:00:00+00:00", RANK),
            _rec("2026-10-01", "2026-10-01T08:00:00+00:00", RANK),
            _rec("2026-10-31", "2026-10-31T08:00:00+00:00", RANK),
            _rec("2026-11-01", "2026-11-01T08:00:00+00:00", RANK),
        ])
        report = cost.aggregate(
            loaded, start=dt.date(2026, 10, 1), end=dt.date(2026, 10, 31), by="day",
        )
        assert [b.label for b in report.buckets] == ["2026-10-01", "2026-10-31"]

    def test_week_and_month_buckets_follow_the_calendar(self, tmp_path):
        loaded = _load(tmp_path, [
            _rec("2026-09-30", "2026-09-30T08:00:00+00:00", RANK),  # Wed, W40
            _rec("2026-10-01", "2026-10-01T08:00:00+00:00", RANK),  # Thu, W40
            _rec("2026-10-05", "2026-10-05T08:00:00+00:00", RANK),  # Mon, W41
        ])
        start, end = dt.date(2026, 9, 1), dt.date(2026, 10, 31)
        weekly = cost.aggregate(loaded, start=start, end=end, by="week")
        assert [(b.label, b.runs) for b in weekly.buckets] == [("2026-W40", 2), ("2026-W41", 1)]
        assert weekly.buckets[0].start == dt.date(2026, 9, 28)
        monthly = cost.aggregate(loaded, start=start, end=end, by="month")
        assert [(b.label, b.runs) for b in monthly.buckets] == [("2026-09", 1), ("2026-10", 2)]
        assert monthly.buckets[1].end == dt.date(2026, 10, 31)

    def test_run_date_field_buckets_by_when_it_ran(self, tmp_path):
        loaded = _load(tmp_path, [_rec("2026-10-31", "2026-11-01T02:00:00+00:00", RANK)])
        by_issue = cost.aggregate(
            loaded, start=dt.date(2026, 10, 1), end=dt.date(2026, 11, 30), by="day",
        )
        by_run = cost.aggregate(
            loaded, start=dt.date(2026, 10, 1), end=dt.date(2026, 11, 30), by="day",
            date_field="run",
        )
        assert by_issue.buckets[0].label == "2026-10-31"
        assert by_run.buckets[0].label == "2026-11-01"

    def test_unknown_price_is_flagged_never_guessed(self, tmp_path):
        loaded = _load(tmp_path, [
            _rec("2026-10-02", "2026-10-02T08:00:00+00:00", {"rank": (1_000, 100, 0, 0, None)}),
            _rec("2026-10-02", "2026-10-02T09:00:00+00:00", RANK),
        ])
        report = cost.aggregate(
            loaded, start=dt.date(2026, 10, 1), end=dt.date(2026, 10, 31), by="day",
        )
        bucket = report.buckets[0]
        assert bucket.unknown_cost_runs == 1
        assert bucket.cost_usd == pytest.approx(1.00)   # only the priced run
        assert bucket.tokens["input_tokens"] == 101_000  # tokens still counted
        rendered = cost.render_report(report, log_path=Path("x.jsonl"))
        assert "$1.00?" in rendered
        assert "missing from the price table" in rendered

    def test_by_model_comes_from_stage_model_snapshots(self, tmp_path):
        models = {
            "rank": {"claude-sonnet-5-5": {
                "input_tokens": 100_000, "output_tokens": 5_000,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                "cost_usd": 0.25}},
            "summarise": {"claude-sonnet-5-5": {
                "input_tokens": 50_000, "output_tokens": 2_000,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                "cost_usd": 0.12}},
        }
        with_models = _rec(
            "2026-11-05", "2026-11-05T08:00:00+00:00",
            {"rank": (100_000, 5_000, 0, 0, 0.25), "summarise": (50_000, 2_000, 0, 0, 0.12)},
            models=models,
        )
        without_models = _rec("2026-11-04", "2026-11-04T08:00:00+00:00", RANK)
        loaded = _load(tmp_path, [without_models, with_models])
        report = cost.aggregate(
            loaded, start=dt.date(2026, 11, 1), end=dt.date(2026, 11, 30), by="month",
        )
        total = report.total
        assert list(total.by_model) == ["claude-sonnet-5-5"]
        assert total.by_model["claude-sonnet-5-5"]["input_tokens"] == 150_000
        assert total.by_model["claude-sonnet-5-5"]["cost_usd"] == pytest.approx(0.37)
        rendered = cost.render_report(report, log_path=Path("x.jsonl"))
        assert "claude-sonnet-5-5 $0.37" in rendered
        assert "before model attribution" in rendered  # the $1.00 run carries no model


# ---------------------------------------------------------------------------
# Rendering + JSON
# ---------------------------------------------------------------------------

class TestRender:
    def _report(self, tmp_path):
        loaded = _load(tmp_path, [
            _rec("2026-10-02", "2026-10-02T08:00:00+00:00",
                 {"rank": (100_000, 5_000, 0, 0, 2.00), "verify": (10_000, 500, 0, 0, 0.10)}),
            _rec("2026-10-05", "2026-10-05T08:00:00+00:00", {"rank": (100_000, 5_000, 0, 0, 1.00)}),
        ])
        return cost.aggregate(
            loaded, start=dt.date(2026, 10, 1), end=dt.date(2026, 10, 31), by="day",
        )

    def test_table_has_a_row_per_bucket_a_total_and_a_stage_split(self, tmp_path):
        text = cost.render_report(self._report(tmp_path), log_path=Path("x.jsonl"))
        assert "2026-10-02" in text and "2026-10-05" in text
        assert "TOTAL" in text and "$3.10" in text
        assert "by stage : rank $3.00 (97%) | verify $0.10 (3%)" in text
        assert "avg $1.55" in text

    def test_json_carries_buckets_totals_and_skip_counts(self, tmp_path):
        payload = self._report(tmp_path).to_json(Path("x.jsonl"))
        assert payload["by"] == "day" and payload["date_field"] == "issue"
        assert [b["bucket"] for b in payload["buckets"]] == ["2026-10-02", "2026-10-05"]
        assert payload["total"]["cost_usd"] == pytest.approx(3.10)
        assert payload["total"]["by_stage"]["rank"] == pytest.approx(3.00)
        assert payload["untracked_runs"] == 0


# ---------------------------------------------------------------------------
# Reconciliation against the Analytics API
# ---------------------------------------------------------------------------

class _FakeApi:
    """Records every request and serves canned pages keyed by starting_at."""

    def __init__(self, pages: dict[tuple[str, str | None], dict], status: int = 200):
        self.pages = pages
        self.status = status
        self.calls: list[tuple[dict, dict]] = []

    def __call__(self, url, params, headers):
        self.calls.append((dict(params), dict(headers)))
        if self.status != 200:
            return self.status, {"error": {"type": "authentication_error", "message": "invalid x-api-key"}}
        return 200, self.pages[(params["starting_at"], params.get("page"))]


def _row(day: str, cents: str) -> dict:
    return {
        "starting_at": f"{day}T00:00:00Z",
        "ending_at": f"{day}T00:00:00Z",
        "group": {"api_key_id": None},
        "cost": {"amount": cents, "list_amount": cents, "currency": "USD", "requests": 3},
    }


class TestFetchBilled:
    def test_splits_long_ranges_follows_pages_and_converts_cents(self):
        api = _FakeApi({
            ("2026-09-01T00:00:00Z", None): {
                "data": [_row("2026-09-01", "41280.000000")],
                "has_more": True, "next_page": "page_2", "data_refreshed_at": "2026-10-16T04:00:00Z",
            },
            ("2026-09-01T00:00:00Z", "page_2"): {
                "data": [_row("2026-09-15", "250.500000"), _row("2026-09-15", "49.500000")],
                "has_more": False, "next_page": None, "data_refreshed_at": "2026-10-16T04:00:00Z",
            },
            ("2026-10-02T00:00:00Z", None): {
                "data": [_row("2026-10-10", "100.000000")],
                "has_more": False, "next_page": None, "data_refreshed_at": "2026-10-16T04:00:00Z",
            },
        })
        billed = cost.fetch_billed_by_day(
            dt.date(2026, 9, 1), dt.date(2026, 10, 15),
            admin_key="sk-ant-admin-test", api_key_id="apikey_123", http_get=api,
            today=dt.date(2026, 10, 16),
        )
        assert billed.by_day == {
            dt.date(2026, 9, 1): Decimal("412.80"),
            dt.date(2026, 9, 15): Decimal("3.00"),
            dt.date(2026, 10, 10): Decimal("1.00"),
        }
        assert billed.requests == 3
        assert billed.data_refreshed_at == "2026-10-16T04:00:00Z"
        assert billed.scope == "API key apikey_123"
        first_params, first_headers = api.calls[0]
        # 45 days -> a 31-day window (Sep 1 .. Oct 1, ending_at exclusive) then the rest.
        assert first_params["ending_at"] == "2026-10-02T00:00:00Z"
        assert api.calls[2][0]["starting_at"] == "2026-10-02T00:00:00Z"
        assert api.calls[2][0]["ending_at"] == "2026-10-16T00:00:00Z"
        assert first_params["time_bucket_width"] == "1d"
        assert first_params["api_key_ids[]"] == "apikey_123"
        assert api.calls[1][0]["page"] == "page_2"
        assert first_headers["anthropic-beta"] == cost.ANALYTICS_BETA
        assert first_headers["anthropic-version"] == cost.ANTHROPIC_VERSION
        assert first_headers["x-api-key"] == "sk-ant-admin-test"

    def test_rejected_key_explains_which_key_type_is_needed(self):
        api = _FakeApi({}, status=401)
        with pytest.raises(cost.CostApiError, match="Admin API key") as exc_info:
            cost.fetch_billed_by_day(
                dt.date(2026, 10, 1), dt.date(2026, 10, 2),
                admin_key="sk-ant-api03-notadmin", http_get=api, today=dt.date(2026, 10, 3),
            )
        assert "sk-ant-api03-notadmin" not in str(exc_info.value)

    def test_start_is_clamped_to_what_the_api_can_reach(self):
        api = _FakeApi({
            ("2026-01-01T00:00:00Z", None): {"data": [], "has_more": False, "data_refreshed_at": None},
        })
        billed = cost.fetch_billed_by_day(
            dt.date(2025, 12, 1), dt.date(2026, 1, 5),
            admin_key="k", http_get=api, today=dt.date(2026, 1, 6),
        )
        assert billed.clamped_start == dt.date(2026, 1, 1)
        assert api.calls[0][0]["starting_at"] == "2026-01-01T00:00:00Z"


class TestAttachBilled:
    def test_fills_day_buckets_and_totals_the_unlogged_days(self, tmp_path):
        loaded = _load(tmp_path, [
            _rec("2026-10-02", "2026-10-02T08:00:00+00:00", {"rank": (1, 1, 0, 0, 3.00)}),
            _rec("2026-10-05", "2026-10-05T08:00:00+00:00", {"rank": (1, 1, 0, 0, 1.00)}),
        ])
        report = cost.aggregate(
            loaded, start=dt.date(2026, 10, 1), end=dt.date(2026, 10, 31), by="day",
            date_field="run",
        )
        billed = cost.BilledReport(
            by_day={
                dt.date(2026, 10, 2): Decimal("3.25"),
                dt.date(2026, 10, 5): Decimal("1.00"),
                dt.date(2026, 10, 7): Decimal("2.10"),   # judge run, no aiv run logged
                dt.date(2026, 11, 2): Decimal("9.99"),   # outside the period
            },
            data_refreshed_at="2026-11-01T00:00:00Z", scope="whole organisation", requests=1,
        )
        cost.attach_billed(report, billed)
        assert report.buckets[0].billed_usd == Decimal("3.25")
        assert report.buckets[0].delta_usd == Decimal("0.25")
        assert report.buckets[1].delta_usd == Decimal("0.00")
        assert report.total.billed_usd == Decimal("4.25")
        assert report.billed_unlogged_usd == Decimal("2.10")
        text = cost.render_report(report, log_path=Path("x.jsonl"))
        assert "billed" in text and "+$0.25" in text
        assert "no logged run: $2.10" in text
        assert cost.API_KEY_ID_ENV in text  # org-wide scope -> tells how to narrow it

    def test_only_day_buckets_can_be_reconciled(self, tmp_path):
        loaded = _load(tmp_path, [_rec("2026-10-02", "2026-10-02T08:00:00+00:00", RANK)])
        report = cost.aggregate(
            loaded, start=dt.date(2026, 10, 1), end=dt.date(2026, 10, 31), by="week",
        )
        with pytest.raises(ValueError, match="day"):
            cost.attach_billed(report, cost.BilledReport({}, None, "whole organisation", 0))


# ---------------------------------------------------------------------------
# Command body + CLI wiring
# ---------------------------------------------------------------------------

class TestRunCost:
    def test_reconcile_without_an_admin_key_fails_clearly(self, tmp_path, caplog):
        log = _write_log(tmp_path / "metrics_log.jsonl", [
            _rec("2026-10-02", "2026-10-02T08:00:00+00:00", RANK),
        ])
        code = cost.run_cost(
            by="day", start="2026-10-01", end="2026-10-31", reconcile=True,
            env={}, log_path=log, today=dt.date(2026, 11, 8),
        )
        assert code == 1
        assert cost.ADMIN_KEY_ENV in caplog.text

    def test_reconcile_needs_day_buckets(self, tmp_path, caplog):
        log = _write_log(tmp_path / "metrics_log.jsonl", [])
        code = cost.run_cost(
            by="week", reconcile=True, env={cost.ADMIN_KEY_ENV: "k"}, log_path=log,
            today=dt.date(2026, 11, 8),
        )
        assert code == 1
        assert "--by day" in caplog.text

    def test_reconcile_dates_runs_by_run_day_and_adds_billed_column(self, tmp_path, capsys):
        log = _write_log(tmp_path / "metrics_log.jsonl", [
            _rec("2026-10-31", "2026-11-01T02:00:00+00:00", {"rank": (1, 1, 0, 0, 2.00)}),
        ])
        api = _FakeApi({
            ("2026-11-01T00:00:00Z", None): {
                "data": [_row("2026-11-01", "200.000000")],
                "has_more": False, "next_page": None, "data_refreshed_at": "2026-11-03T00:00:00Z",
            },
        })
        code = cost.run_cost(
            by="day", start="2026-11-01", end="2026-11-02", reconcile=True,
            env={cost.ADMIN_KEY_ENV: "k"}, http_get=api, log_path=log, today=dt.date(2026, 11, 8),
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "2026-11-01" in out and "2026-10-31" not in out  # dated by run day
        assert "$2.00" in out and "+$0.00" in out

    def test_cli_json_reads_the_data_root_log(self, tmp_data_root):
        _write_log(tmp_data_root / "metrics_log.jsonl", [
            _rec("2026-10-02", "2026-10-02T08:00:00+00:00", RANK),
            _rec("2026-10-09", "2026-10-09T08:00:00+00:00", RANK),
        ])
        result = CliRunner().invoke(
            run_mod.app,
            ["cost", "--json", "--by", "week", "--from", "2026-10-01", "--to", "2026-10-31"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert [b["bucket"] for b in payload["buckets"]] == ["2026-W40", "2026-W41"]
        assert payload["total"]["runs"] == 2

    def test_cli_rejects_last_with_explicit_dates(self, tmp_data_root):
        result = CliRunner().invoke(
            run_mod.app, ["cost", "--last", "3", "--from", "2026-10-01"],
        )
        assert result.exit_code == 1
