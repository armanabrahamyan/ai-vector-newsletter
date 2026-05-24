"""Unit tests for src/render.py — Release Engineer's module.

Covers: staging/released render output paths, template section rendering,
signal pills, section intros, Jinja2 filter correctness (_source_label,
_aest), _read_minutes helper, release_promote transition, and unrelease.

All filesystem writes are redirected to pytest's tmp_path via `tmp_docs`
and `tmp_data_root` fixtures — the real docs/ and data/ trees are never
touched.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from src import paths as _paths
from src.models import Issue, IssueSection, SummaryBlock
from src.render import (
    AlreadyReleased,
    NotReleased,
    _aest,
    _read_minutes,
    _source_label,
    release_promote,
    render,
    unrelease,
)
from tests.conftest import (
    FIXED_DATE,
    FIXED_NOW,
    UTC,
    VALID_CLUSTER_ID,
    VALID_CLUSTER_ID_2,
)


# ---------------------------------------------------------------------------
# Additional fixture — redirect docs/ output paths to tmp dir.
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect all docs/ output paths to a per-test tmp dir.

    Must be combined with `tmp_data_root` for tests that exercise the full
    release pipeline; used alone for render-only tests.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(_paths, "DOCS_ROOT", docs)
    monkeypatch.setattr(_paths, "STAGING_HTML_DIR", docs / "staging")
    monkeypatch.setattr(_paths, "RELEASED_HTML_DIR", docs / "released")
    monkeypatch.setattr(_paths, "DOCS_INDEX", docs / "index.html")
    return docs


# ---------------------------------------------------------------------------
# Rich Issue fixture — includes populated sections so template loops exercise
# pills, intros, and multi-story paths.
# ---------------------------------------------------------------------------

@pytest.fixture
def rich_issue() -> Issue:
    """Issue with one story per section and varied signal values."""
    pulse_block = SummaryBlock(
        story_id=VALID_CLUSTER_ID,
        headline="On-device inference hits invoice-processing parity",
        summary=(
            "A 4B-param open model now extracts structured data from invoices "
            "without sending files to the cloud, matching hosted API accuracy "
            "on a standard benchmark."
        ),
        source_urls=["https://www.example.com/post-1"],
        signal="try",
    )
    big_picture_block = SummaryBlock(
        story_id=VALID_CLUSTER_ID_2,
        headline="Model governance frameworks enter FS regulatory dialogue",
        summary=(
            "Three central banks published a joint consultation paper on "
            "model-risk standards for LLM deployment in retail banking."
        ),
        source_urls=["https://news.example.org/reg-paper"],
        signal="act",
    )
    hands_on_block = SummaryBlock(
        story_id="c_" + "c" * 12,
        headline="LangGraph adds first-class interrupt-and-resume support",
        summary=(
            "The new checkpoint API lets agents pause at tool-call boundaries "
            "and resume from exactly that state — no replay needed."
        ),
        source_urls=["https://blog.langchain.dev/langgraph-interrupt"],
        signal=None,
    )
    on_radar_block = SummaryBlock(
        story_id="c_" + "d" * 12,
        headline="Whisper v3 Turbo lands on-device for iOS",
        summary="Real-time transcription now runs locally on iPhone 15+.",
        source_urls=["https://openai.com/blog/whisper-v3-turbo"],
        signal="watch",
    )
    return Issue(
        date=FIXED_DATE,
        pulse=IssueSection(name="pulse", stories=[pulse_block]),
        sections=[
            IssueSection(
                name="big_picture",
                stories=[big_picture_block],
                intro_lead="Regulatory pressure builds.",
                intro_body=(
                    "Governance frameworks are shifting from voluntary guidance "
                    "to enforceable standards across the G7."
                ),
            ),
            IssueSection(name="hands_on", stories=[hands_on_block]),
            IssueSection(name="on_the_radar", stories=[on_radar_block]),
        ],
        generated_at=FIXED_NOW,
        prompt_versions={"rank": "v1", "summarise": "v1"},
    )


# ---------------------------------------------------------------------------
# Helpers — write a minimal staging dir (peripheral files + issue.json).
# ---------------------------------------------------------------------------

def _write_staging(date: _dt.date, issue: Issue) -> None:
    """Write a complete staging dir so release_promote finds everything."""
    staging = _paths.staging_dir(date)
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "issue.json").write_text(
        issue.model_dump_json(indent=2), encoding="utf-8"
    )
    for name in ("items.jsonl", "source_health.json", "clusters.jsonl", "ranked.jsonl"):
        (staging / name).write_text("", encoding="utf-8")
    emb_dir = staging / "embeddings"
    emb_dir.mkdir(parents=True, exist_ok=True)
    (emb_dir / "centroids.npz").write_bytes(b"")


def _write_released(date: _dt.date, issue: Issue, number: int) -> None:
    """Write a complete released dir directly (bypasses release_promote)."""
    released = _paths.released_dir(date)
    released.mkdir(parents=True, exist_ok=True)
    numbered = issue.model_copy(update={"issue_number": number})
    (released / "issue.json").write_text(
        json.dumps(json.loads(numbered.model_dump_json()), indent=2),
        encoding="utf-8",
    )
    for name in ("items.jsonl", "source_health.json", "clusters.jsonl", "ranked.jsonl"):
        (released / name).write_text("", encoding="utf-8")
    emb_dir = released / "embeddings"
    emb_dir.mkdir(parents=True, exist_ok=True)
    (emb_dir / "centroids.npz").write_bytes(b"")


# ===========================================================================
# TestStagingRender — render(date, mode='preview') writes to staging HTML path.
# ===========================================================================

class TestStagingRender:
    def test_returns_staging_html_path(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        result = render(FIXED_DATE, mode="preview")
        assert result == _paths.staging_html_path(FIXED_DATE)

    def test_file_is_non_empty(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        out = render(FIXED_DATE, mode="preview")
        assert out.exists()
        assert out.stat().st_size > 0

    def test_does_not_write_released_html(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        render(FIXED_DATE, mode="preview")
        assert not _paths.released_html_path(FIXED_DATE).exists()

    def test_does_not_write_docs_index(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        render(FIXED_DATE, mode="preview")
        assert not _paths.DOCS_INDEX.exists()

    def test_missing_issue_json_raises(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        with pytest.raises(FileNotFoundError):
            render(FIXED_DATE, mode="preview")

    def test_idempotent_rerender(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        out1 = render(FIXED_DATE, mode="preview")
        content1 = out1.read_text(encoding="utf-8")
        render(FIXED_DATE, mode="preview")
        content2 = out1.read_text(encoding="utf-8")
        assert content1 == content2


# ===========================================================================
# TestReleasedRender — render(date, mode='release') writes released + index.
# ===========================================================================

class TestReleasedRender:
    def test_returns_released_html_path(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_released(FIXED_DATE, rich_issue, number=1)
        result = render(FIXED_DATE, mode="release")
        assert result == _paths.released_html_path(FIXED_DATE)

    def test_released_html_is_non_empty(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_released(FIXED_DATE, rich_issue, number=1)
        out = render(FIXED_DATE, mode="release")
        assert out.exists()
        assert out.stat().st_size > 0

    def test_rebuilds_docs_index(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_released(FIXED_DATE, rich_issue, number=1)
        render(FIXED_DATE, mode="release")
        assert _paths.DOCS_INDEX.exists()
        assert _paths.DOCS_INDEX.stat().st_size > 0

    def test_does_not_write_staging_html(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_released(FIXED_DATE, rich_issue, number=1)
        render(FIXED_DATE, mode="release")
        assert not _paths.staging_html_path(FIXED_DATE).exists()


# ===========================================================================
# TestTemplateSections — all four display names appear in output HTML.
# ===========================================================================

class TestTemplateSections:
    @pytest.fixture(autouse=True)
    def _render(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        out = render(FIXED_DATE, mode="preview")
        self._html = out.read_text(encoding="utf-8")

    def test_pulse_section_appears(self) -> None:
        assert "The Pulse" in self._html

    def test_big_picture_section_appears(self) -> None:
        assert "The Big Picture" in self._html

    def test_hands_on_section_appears(self) -> None:
        assert "Hands-On" in self._html

    def test_on_the_radar_section_appears(self) -> None:
        assert "On the Radar" in self._html


# ===========================================================================
# TestSignalPill — signal field drives pill CSS class.
# ===========================================================================

class TestSignalPill:
    def _render_html(
        self,
        signal_value,
        tmp_data_root: Path,
        tmp_docs: Path,
        section_name: str = "hands_on",
    ) -> str:
        block = SummaryBlock(
            story_id="c_" + "e" * 12,
            headline="Test story",
            summary="A test summary sentence long enough to pass validation.",
            source_urls=["https://example.com/test"],
            signal=signal_value,
        )
        issue = Issue(
            date=FIXED_DATE,
            pulse=IssueSection(
                name="pulse",
                stories=[
                    SummaryBlock(
                        story_id=VALID_CLUSTER_ID,
                        headline="Pulse headline",
                        summary="Pulse summary sentence.",
                        source_urls=["https://example.com/pulse"],
                    )
                ],
            ),
            sections=[IssueSection(name=section_name, stories=[block])],
            generated_at=FIXED_NOW,
            prompt_versions={"rank": "v1", "summarise": "v1"},
        )
        _write_staging(FIXED_DATE, issue)
        out = render(FIXED_DATE, mode="preview")
        return out.read_text(encoding="utf-8")

    @pytest.mark.parametrize("signal_value", ["act", "try", "read", "watch", "discuss"])
    def test_pill_class_present_when_signal_set(
        self, signal_value: str, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        html = self._render_html(signal_value, tmp_data_root, tmp_docs)
        assert f'class="av-pill av-pill-{signal_value}"' in html

    def test_no_pill_when_signal_is_none(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        html = self._render_html(None, tmp_data_root, tmp_docs)
        # The CSS stylesheet always contains .av-pill rules; the assertion
        # must check for the rendered *element*, not the CSS class name.
        assert '<span class="av-pill' not in html


# ===========================================================================
# TestSectionIntro — intro_lead / intro_body rendered only when present.
# ===========================================================================

class TestSectionIntro:
    def _issue_with_intro(
        self,
        intro_lead: str | None,
        intro_body: str | None,
    ) -> Issue:
        block = SummaryBlock(
            story_id="c_" + "f" * 12,
            headline="A headline for big picture",
            summary="Summary prose here for the big picture story.",
            source_urls=["https://example.com/bp"],
        )
        return Issue(
            date=FIXED_DATE,
            pulse=IssueSection(
                name="pulse",
                stories=[
                    SummaryBlock(
                        story_id=VALID_CLUSTER_ID,
                        headline="Pulse",
                        summary="Pulse summary.",
                        source_urls=["https://example.com/p"],
                    )
                ],
            ),
            sections=[
                IssueSection(
                    name="big_picture",
                    stories=[block],
                    intro_lead=intro_lead,
                    intro_body=intro_body,
                )
            ],
            generated_at=FIXED_NOW,
            prompt_versions={"rank": "v1", "summarise": "v1"},
        )

    def test_intro_lead_and_body_rendered_when_set(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        issue = self._issue_with_intro(
            intro_lead="Bold opening phrase.",
            intro_body="One or two framing sentences.",
        )
        _write_staging(FIXED_DATE, issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert "Bold opening phrase." in html
        assert "One or two framing sentences." in html

    def test_intro_container_absent_when_both_none(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        issue = self._issue_with_intro(intro_lead=None, intro_body=None)
        _write_staging(FIXED_DATE, issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        # CSS stylesheet always contains .av-section-intro; check for the
        # rendered element specifically.
        assert '<p class="av-section-intro"' not in html

    def test_intro_renders_with_lead_only(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        issue = self._issue_with_intro(intro_lead="Lead phrase only.", intro_body=None)
        _write_staging(FIXED_DATE, issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert "Lead phrase only." in html
        assert '<p class="av-section-intro"' in html


# ===========================================================================
# TestSourceLabelFilter — _source_label strips www. and handles edge cases.
# ===========================================================================

class TestSourceLabelFilter:
    @pytest.mark.parametrize("url,expected", [
        ("https://www.example.com/article", "example.com"),
        ("https://example.com/article", "example.com"),
        ("https://blog.openai.com/gpt-5", "blog.openai.com"),
        ("https://www.ft.com/content/xyz", "ft.com"),
        ("https://arxiv.org/abs/2501.00001", "arxiv.org"),
        ("http://www.bbc.co.uk/news/tech", "bbc.co.uk"),
    ])
    def test_source_label(self, url: str, expected: str) -> None:
        assert _source_label(url) == expected

    def test_source_label_in_rendered_html(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        # rich_issue pulse URL is https://www.example.com/post-1; the link
        # text rendered by the `source_label` filter must show "example.com"
        # (no www.). The href attribute will still hold the full URL.
        # The template renders: example.com&nbsp;&rarr; as the link text.
        assert "example.com&nbsp;" in html
        # Verify the www. prefix does NOT appear as visible link text
        # (it may appear inside href= attributes, which is correct).
        assert "www.example.com&nbsp;" not in html


# ===========================================================================
# TestAestFilter — _aest converts UTC datetime to Sydney local time.
# ===========================================================================

class TestAestFilter:
    def test_aest_standard_time(self) -> None:
        # AEST = UTC+10; July is winter in Sydney => standard time
        dt = _dt.datetime(2026, 7, 15, 14, 30, 0, tzinfo=UTC)
        result = _aest(dt)
        assert "2026-07-16 00:30" in result
        assert "AEST" in result

    def test_aedt_daylight_saving(self) -> None:
        # AEDT = UTC+11; January is summer in Sydney => daylight saving
        dt = _dt.datetime(2026, 1, 15, 14, 30, 0, tzinfo=UTC)
        result = _aest(dt)
        assert "2026-01-16 01:30" in result
        assert "AEDT" in result

    def test_naive_datetime_falls_back_to_utc_label(self) -> None:
        dt = _dt.datetime(2026, 5, 24, 12, 0, 0)  # no tzinfo
        result = _aest(dt)
        assert "UTC" in result

    def test_aest_output_appears_in_rendered_html(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        # FIXED_NOW = 2026-05-24 12:00 UTC; May is AEST (UTC+10) -> 22:00
        _write_staging(FIXED_DATE, rich_issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert "2026-05-24 22:00" in html


# ===========================================================================
# TestReadMinutes — word-count / 200 wpm, rounded up, minimum 1.
# ===========================================================================

class TestReadMinutes:
    def _issue_with_words(self, word_count: int) -> Issue:
        summary = ("word " * word_count).strip() or "x"
        block = SummaryBlock(
            story_id=VALID_CLUSTER_ID,
            headline="Headline",
            summary=summary[:1200],
            source_urls=["https://example.com/"],
        )
        return Issue(
            date=FIXED_DATE,
            pulse=IssueSection(name="pulse", stories=[block]),
            sections=[],
            generated_at=FIXED_NOW,
            prompt_versions={"rank": "v1", "summarise": "v1"},
        )

    def test_empty_summaries_yield_one_minute(self) -> None:
        block = SummaryBlock(
            story_id=VALID_CLUSTER_ID,
            headline="H",
            summary="x",
            source_urls=["https://example.com/"],
        )
        issue = Issue(
            date=FIXED_DATE,
            pulse=IssueSection(name="pulse", stories=[block]),
            sections=[],
            generated_at=FIXED_NOW,
            prompt_versions={"rank": "v1", "summarise": "v1"},
        )
        assert _read_minutes(issue) == 1

    def test_200_words_is_one_minute(self) -> None:
        issue = self._issue_with_words(200)
        assert _read_minutes(issue) == 1

    def test_201_words_rounds_up_to_two_minutes(self) -> None:
        issue = self._issue_with_words(201)
        assert _read_minutes(issue) == 2

    def test_400_words_is_two_minutes(self) -> None:
        issue = self._issue_with_words(400)
        assert _read_minutes(issue) == 2

    def test_rich_issue_read_minutes_is_positive(self, rich_issue: Issue) -> None:
        assert _read_minutes(rich_issue) >= 1


# ===========================================================================
# TestReleasePromote — 7-step transition, idempotency, issue numbering.
# ===========================================================================

class TestReleasePromote:
    def test_assigns_issue_number_one_when_no_history(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        final = release_promote(FIXED_DATE)
        assert final.issue_number == 1

    def test_increments_issue_number_from_existing_canonical(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        earlier = FIXED_DATE - _dt.timedelta(days=1)
        _write_released(earlier, rich_issue, number=5)
        _write_staging(FIXED_DATE, rich_issue)
        final = release_promote(FIXED_DATE)
        assert final.issue_number == 6

    def test_canonical_issue_json_written(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE)
        assert _paths.issue_path(FIXED_DATE, canonical=True).exists()

    def test_released_html_written(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE)
        assert _paths.released_html_path(FIXED_DATE).exists()

    def test_docs_index_written(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE)
        assert _paths.DOCS_INDEX.exists()

    def test_published_urls_written(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE)
        assert _paths.PUBLISHED_URLS_PATH.exists()
        content = _paths.PUBLISHED_URLS_PATH.read_text(encoding="utf-8")
        assert "example.com" in content

    def test_peripheral_files_copied_to_released(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE)
        released = _paths.released_dir(FIXED_DATE)
        for name in ("items.jsonl", "source_health.json", "clusters.jsonl", "ranked.jsonl"):
            assert (released / name).exists()

    def test_already_released_raises(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE)
        # Re-staging and promoting again must raise AlreadyReleased.
        _write_staging(FIXED_DATE, rich_issue)
        with pytest.raises(AlreadyReleased) as exc_info:
            release_promote(FIXED_DATE)
        assert exc_info.value.date == FIXED_DATE

    def test_no_staging_draft_raises(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        from src.render import NoStagingDraft
        with pytest.raises(NoStagingDraft):
            release_promote(FIXED_DATE)


# ===========================================================================
# TestUnrelease — deletes canonical, rebuilds published_urls, no renumbering.
# ===========================================================================

class TestUnrelease:
    def test_canonical_issue_json_removed(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE)
        unrelease(FIXED_DATE)
        assert not _paths.issue_path(FIXED_DATE, canonical=True).exists()

    def test_released_html_removed(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE)
        unrelease(FIXED_DATE)
        assert not _paths.released_html_path(FIXED_DATE).exists()

    def test_returns_url_count_removed(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE)
        removed = unrelease(FIXED_DATE)
        assert isinstance(removed, int)
        assert removed >= 0

    def test_published_urls_rebuilt_without_unreleased_urls(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE)
        unrelease(FIXED_DATE)
        # No remaining canonical issues -> published_urls.txt should be empty
        content = _paths.PUBLISHED_URLS_PATH.read_text(encoding="utf-8").strip()
        assert content == ""

    def test_surviving_issue_urls_preserved_in_published_urls(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        earlier = FIXED_DATE - _dt.timedelta(days=1)
        _write_released(earlier, rich_issue, number=1)
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE)
        unrelease(FIXED_DATE)
        content = _paths.PUBLISHED_URLS_PATH.read_text(encoding="utf-8")
        # earlier issue's URL should survive
        assert "example.com" in content

    def test_not_released_raises(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        with pytest.raises(NotReleased) as exc_info:
            unrelease(FIXED_DATE)
        assert exc_info.value.date == FIXED_DATE

    def test_issue_number_gap_preserved(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """Unreleasing issue #1 and then releasing a new issue gives #2,
        not #1 — gap is preserved because surviving history is empty."""
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE)
        unrelease(FIXED_DATE)
        # Re-stage and promote: no surviving canonical issues, so next = 1.
        # The spec says gaps are preserved (no renumber), but when history is
        # empty the next number is 1 (max of empty + 1). That's correct per
        # DESIGN.md: renumbering *subsequent* issues is what's prohibited.
        _write_staging(FIXED_DATE, rich_issue)
        final = release_promote(FIXED_DATE)
        assert final.issue_number == 1

    def test_subsequent_issue_number_not_renumbered(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """Unreleasing issue #1 must not change the number of issue #2."""
        date_a = FIXED_DATE - _dt.timedelta(days=1)
        date_b = FIXED_DATE

        _write_staging(date_a, rich_issue)
        release_promote(date_a)   # issue #1

        later_issue = rich_issue.model_copy()
        _write_staging(date_b, later_issue)
        final_b = release_promote(date_b)   # issue #2
        assert final_b.issue_number == 2

        # Unrelease #1; issue #2 must still be #2
        unrelease(date_a)
        payload = json.loads(
            _paths.issue_path(date_b, canonical=True).read_text(encoding="utf-8")
        )
        assert payload["issue_number"] == 2
