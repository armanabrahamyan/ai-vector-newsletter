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
import re
from pathlib import Path

import pytest

from src import paths as _paths
from src import render as _render_mod
from src.models import (
    ClaimVerdict,
    DigestBullet,
    Issue,
    IssueSection,
    StoryVerification,
    SummaryBlock,
)
from src.render import (
    TEMPLATE_NAME,
    AlreadyReleased,
    NotReleased,
    _aest,
    _build_env,
    _code_spans,
    _digest_items,
    _normalise_digest,
    _read_minutes,
    _section_synthesis,
    _source_label,
    _story_tag,
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
            IssueSection(name="currents", stories=[on_radar_block]),
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

    def test_currents_section_appears(self) -> None:
        assert "Currents" in self._html


# ===========================================================================
# TestStoryTag — the meta-row verb (act / try / read / discuss / watch).
#
# The design calls it `.tag`; the data behind it is `SummaryBlock.signal`,
# whose Literal is exactly the design's five verbs. The Editor's ratified
# admissibility ruling (2026-08-09) decides which of those verbs a section
# will actually show:
#
#     tag = signal if signal in admissible(section) else default(section)
#
# with the Pulse suppressed entirely. The table below IS the ruling written
# out longhand — every section crossed with every signal value, including
# None. Change one cell of `_ADMISSIBLE_TAGS` or `_DEFAULT_TAG` and this
# test goes red, which is exactly what a ratified table deserves.
# ===========================================================================

# (section, signal) -> expected rendered tag. None means "no tag element".
_TAG_TABLE: list[tuple[str, str | None, str | None]] = [
    # The Pulse: suppressed by position. No tag, whatever the signal says.
    ("pulse", "act", None),
    ("pulse", "try", None),
    ("pulse", "read", None),
    ("pulse", "watch", None),
    ("pulse", "discuss", None),
    ("pulse", None, None),
    # The Big Picture: admissible {act, discuss}; default discuss.
    ("big_picture", "act", "act"),
    ("big_picture", "discuss", "discuss"),
    ("big_picture", "try", "discuss"),
    ("big_picture", "read", "discuss"),
    ("big_picture", "watch", "discuss"),
    ("big_picture", None, "discuss"),
    # Hands-On: admissible {try, read, discuss}; default try.
    ("hands_on", "try", "try"),
    ("hands_on", "read", "read"),
    ("hands_on", "discuss", "discuss"),
    ("hands_on", "act", "try"),
    ("hands_on", "watch", "try"),
    ("hands_on", None, "try"),
    # Currents: admissible {watch, read, discuss}; default watch.
    ("currents", "watch", "watch"),
    ("currents", "read", "read"),
    ("currents", "discuss", "discuss"),
    ("currents", "act", "watch"),
    ("currents", "try", "watch"),
    ("currents", None, "watch"),
]


def _block(signal=None, story_id: str = VALID_CLUSTER_ID) -> SummaryBlock:
    return SummaryBlock(
        story_id=story_id,
        headline="H",
        summary="S",
        source_urls=["https://example.com/"],
        signal=signal,
    )


class TestStoryTag:
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

    # --- unit: the admissibility table ------------------------------------

    @pytest.mark.parametrize(
        ("section", "signal", "expected"),
        _TAG_TABLE,
        ids=[f"{s}-{sig or 'none'}" for s, sig, _ in _TAG_TABLE],
    )
    def test_admissibility_table(
        self, section: str, signal: str | None, expected: str | None
    ) -> None:
        assert _story_tag(_block(signal), section) == expected

    def test_every_non_pulse_section_always_yields_a_verb(self) -> None:
        """No empty slots: the reader never sees a story whose neighbours
        carry a verb and it does not."""
        for section, signal, expected in _TAG_TABLE:
            if section == "pulse":
                continue
            assert expected is not None, (section, signal)

    def test_pulse_suppression_is_positional_not_block_history(self) -> None:
        """The same block object renders no tag in the Pulse and a tag in a
        section. Suppression is a property of the position, not the story."""
        block = _block("discuss")
        assert _story_tag(block, "pulse") is None
        assert _story_tag(block, "big_picture") == "discuss"

    def test_pulse_suppression_beats_an_admissible_signal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Suppression is checked BEFORE admissibility, not implied by the
        Pulse being absent from the tables.

        Without this test the suppression set is dead weight: `pulse` has no
        entry in `_ADMISSIBLE_TAGS` or `_DEFAULT_TAG`, so emptying
        `_TAG_SUPPRESSED_SECTIONS` changes nothing observable. Giving the
        Pulse a full table entry here makes the early return the only thing
        standing between the reader and a tag under the story of the day.
        """
        monkeypatch.setitem(
            _render_mod._ADMISSIBLE_TAGS, "pulse", frozenset({"act", "read"})
        )
        monkeypatch.setitem(_render_mod._DEFAULT_TAG, "pulse", "read")
        assert _story_tag(_block("act"), "pulse") is None
        assert _story_tag(_block("watch"), "pulse") is None
        assert _story_tag(_block(None), "pulse") is None

    def test_unknown_section_yields_no_tag(self) -> None:
        """A section name with no admissible set and no default cannot be
        given a meaningful verb, so it gets none rather than a guess."""
        assert _story_tag(_block("act"), "not_a_section") is None

    def test_legacy_on_the_radar_matches_currents(self) -> None:
        """Archived issues name the section `on_the_radar`. The model coerces
        it to `currents`, but a raw caller must not fall off the table."""
        for signal in ("act", "try", "read", "watch", "discuss", None):
            assert _story_tag(_block(signal), "on_the_radar") == _story_tag(
                _block(signal), "currents"
            )

    # --- rendered element -------------------------------------------------

    @pytest.mark.parametrize(
        ("signal_value", "expected"),
        [(s, e) for sec, s, e in _TAG_TABLE if sec == "hands_on"],
        ids=lambda v: str(v),
    )
    def test_tag_element_rendered_from_the_table(
        self,
        signal_value: str | None,
        expected: str,
        tmp_data_root: Path,
        tmp_docs: Path,
    ) -> None:
        html = self._render_html(signal_value, tmp_data_root, tmp_docs)
        assert f'<span class="tag">{expected}</span>' in html

    def test_tag_element_present_even_when_signal_is_none(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """The ruling's "never an empty slot" clause, at the HTML level."""
        html = self._render_html(None, tmp_data_root, tmp_docs)
        assert '<span class="tag">try</span>' in html

    def test_pulse_meta_row_carries_no_tag(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """Per the handoff design the Pulse meta row is source-only, even
        though the Pulse story carries a signal (rich_issue's is "try")."""
        assert rich_issue.pulse.stories[0].signal == "try"
        _write_staging(FIXED_DATE, rich_issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        pulse_meta = html[html.index('<h1>'):html.index('<section class="section')]
        assert '<span class="tag">' not in pulse_meta


# ===========================================================================
# TestTagStats — the override meter the drift counter will consume.
# ===========================================================================

def _issue_for_tag_stats(
    pulse_signal,
    section_signals: dict[str, list],
) -> Issue:
    """Build an Issue with the given signals. Story ids are made unique so
    the model's referential checks pass."""
    counter = iter(range(1, 1000))

    def _next_id() -> str:
        return f"c_{next(counter):012x}"

    sections = [
        IssueSection(
            name=name,
            stories=[_block(sig, story_id=_next_id()) for sig in signals],
        )
        for name, signals in section_signals.items()
    ]
    return Issue(
        date=FIXED_DATE,
        pulse=IssueSection(
            name="pulse",
            stories=[_block(pulse_signal, story_id=_next_id())],
        ),
        sections=sections,
        generated_at=FIXED_NOW,
        prompt_versions={"rank": "v1", "summarise": "v1"},
    )


class TestTagStats:
    def test_all_signals_admissible_means_zero_overrides(self) -> None:
        issue = _issue_for_tag_stats(
            "act",
            {"big_picture": ["act", "discuss"], "hands_on": ["try", "read"]},
        )
        stats = _render_mod.tag_stats(issue)
        assert stats == {
            "stories": 5,
            "suppressed": 1,
            "tagged": 4,
            "signal_replaced": 0,
            "default_filled": 0,
            "overrides": 0,
        }

    def test_inadmissible_signal_counts_as_a_replacement(self) -> None:
        # "watch" is not admissible in hands_on -> replaced by "try".
        issue = _issue_for_tag_stats("act", {"hands_on": ["watch", "try"]})
        stats = _render_mod.tag_stats(issue)
        assert stats["signal_replaced"] == 1
        assert stats["default_filled"] == 0
        assert stats["overrides"] == 1

    def test_missing_signal_counts_as_a_default_fill(self) -> None:
        issue = _issue_for_tag_stats("act", {"currents": [None, "watch"]})
        stats = _render_mod.tag_stats(issue)
        assert stats["default_filled"] == 1
        assert stats["signal_replaced"] == 0
        assert stats["overrides"] == 1

    def test_overrides_is_the_sum_of_both_causes(self) -> None:
        issue = _issue_for_tag_stats(
            "act",
            {
                "big_picture": ["try", None],      # replaced + filled
                "hands_on": ["act", "read"],       # replaced + admissible
                "currents": ["watch"],             # admissible
            },
        )
        stats = _render_mod.tag_stats(issue)
        assert stats["signal_replaced"] == 2
        assert stats["default_filled"] == 1
        assert stats["overrides"] == 3
        assert stats["tagged"] == 5
        assert stats["suppressed"] == 1
        assert stats["stories"] == 6

    def test_suppressed_pulse_is_not_counted_as_an_override(self) -> None:
        """The Pulse always has tag != signal. Counting it would add a
        constant +1 to every issue and drown the number the meter exists
        to surface."""
        issue = _issue_for_tag_stats("act", {"hands_on": ["try"]})
        stats = _render_mod.tag_stats(issue)
        assert stats["suppressed"] == 1
        assert stats["overrides"] == 0

    def test_stats_reconcile(self) -> None:
        issue = _issue_for_tag_stats(
            None,
            {"big_picture": ["read"], "hands_on": [None], "currents": ["act"]},
        )
        stats = _render_mod.tag_stats(issue)
        assert stats["tagged"] + stats["suppressed"] == stats["stories"]
        assert (
            stats["signal_replaced"] + stats["default_filled"]
            == stats["overrides"]
        )
        assert stats["overrides"] <= stats["tagged"]

    def test_render_logs_the_override_count(
        self,
        rich_issue: Issue,
        tmp_data_root: Path,
        tmp_docs: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The drift counter's future input has to actually be emitted."""
        _write_staging(FIXED_DATE, rich_issue)
        with caplog.at_level("INFO", logger="src.render"):
            render(FIXED_DATE, mode="preview")
        lines = [r.getMessage() for r in caplog.records if "tags:" in r.getMessage()]
        assert len(lines) == 1
        # rich_issue: pulse(try, suppressed), big_picture(act, admissible),
        # hands_on(None -> try, filled), currents(watch, admissible).
        assert lines[0] == (
            "tags: 3 tagged, 1 suppressed, 1 overrides "
            "(0 inadmissible signal, 1 default-filled)"
        )


# ===========================================================================
# TestEvidenceChipDropped — ratified deviation #1 from the handoff design.
# ===========================================================================

class TestEvidenceChipDropped:
    def test_no_ev_element_anywhere(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert '<span class="ev' not in html

    def test_no_ev_css_rule(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert ".ev{" not in html
        assert ".ev.l1" not in html

    def test_no_ev_palette_tokens(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """The chip is gone, so the triad tokens it fed are dead weight in a
        permanent artifact. The palette is recorded in READING_EXPERIENCE.md
        instead of being carried by every page we ship."""
        _write_staging(FIXED_DATE, rich_issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        for token in ("--ev1", "--ev2", "--ev3"):
            assert token not in html

    def test_no_ev_palette_tokens_on_the_landing_page(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_released(FIXED_DATE, rich_issue, number=1)
        render(FIXED_DATE, mode="release")
        html = _paths.DOCS_INDEX.read_text(encoding="utf-8")
        for token in ("--ev1", "--ev2", "--ev3"):
            assert token not in html


# ===========================================================================
# TestPalette — the ratified colour scheme (2026-08-09).
#
# The house rule is "never blue, navy or black as an accent". The accent
# swapped from navy oklch(0.45 0.115 256) to warm vermilion
# oklch(0.49 0.135 30), and every accented element on both surfaces reads
# the token, so these tests pin the token and then prove no blue survived.
#
# The neutrals are asserted token-by-token rather than eyeballed: they were
# supposed to already match the ratified palette, and "supposed to" is not
# evidence.
# ===========================================================================

# The ratified palette. --ink-3 is the one deliberate departure: the palette
# says 0.56, which measures 4.49:1 on --paper and so misses the 4.5:1 AA
# floor for normal text. Held at 0.55 (4.68:1).
_RATIFIED_TOKENS: dict[str, str] = {
    "--paper": "oklch(0.987 0.004 95)",
    "--paper-2": "oklch(0.965 0.006 90)",
    "--ink": "oklch(0.255 0.012 60)",
    "--ink-2": "oklch(0.46 0.012 62)",
    "--ink-3": "oklch(0.55 0.010 66)",
    "--line": "oklch(0.905 0.006 80)",
    "--line-2": "oklch(0.845 0.008 78)",
    "--accent": "oklch(0.49 0.135 30)",
}

# Any oklch() whose hue is 256 — the navy that was purged.
_NAVY_RE = re.compile(r"oklch\([0-9.]+\s+[0-9.]+\s+256\)")


class TestPalette:
    @pytest.fixture
    def issue_html(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> str:
        _write_staging(FIXED_DATE, rich_issue)
        return render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")

    @pytest.fixture
    def landing_html(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> str:
        _write_released(FIXED_DATE, rich_issue, number=1)
        render(FIXED_DATE, mode="release")
        return _paths.DOCS_INDEX.read_text(encoding="utf-8")

    # --- the accent -------------------------------------------------------

    def test_issue_accent_is_vermilion(self, issue_html: str) -> None:
        assert "--accent:oklch(0.49 0.135 30)" in issue_html

    def test_landing_accent_is_vermilion(self, landing_html: str) -> None:
        assert "--accent: oklch(0.49 0.135 30)" in landing_html

    def test_issue_hover_is_vermilion(self, issue_html: str) -> None:
        assert "a:hover{color:oklch(0.40 0.135 30)}" in issue_html

    def test_landing_hover_is_vermilion(self, landing_html: str) -> None:
        assert "a:hover { color: oklch(0.40 0.135 30); }" in landing_html

    def test_landing_accent_tint_left_the_blue_hue(self, landing_html: str) -> None:
        """--accent-2 tints row hover and search hits. Left at hue 256 it
        would have been the one blue surviving the purge."""
        assert "--accent-2: oklch(0.95 0.03 30)" in landing_html

    # --- the purge --------------------------------------------------------

    def test_no_navy_survives_on_the_issue_page(self, issue_html: str) -> None:
        assert _NAVY_RE.search(issue_html) is None

    def test_no_navy_survives_on_the_landing_page(self, landing_html: str) -> None:
        assert _NAVY_RE.search(landing_html) is None

    def test_old_vermilion_hex_is_gone(
        self, issue_html: str, landing_html: str
    ) -> None:
        """#e6452f was the previous brand orange, still on the mask-icon
        after the accent had already moved. Both must agree."""
        assert "#e6452f" not in issue_html
        assert "#e6452f" not in landing_html

    # The mask-icon colour moved OUT of this class on 2026-08-09. The mark is
    # ink, so the mask-icon is ink; see TestMark.test_mask_icon_is_ink_on_both
    # _surfaces. What remains here is the guarantee that no vermilion crept
    # back into the link tags after the accent stopped owning them.
    def test_no_accent_hex_in_link_tags(
        self, issue_html: str, landing_html: str
    ) -> None:
        for html in (issue_html, landing_html):
            for line in html.splitlines():
                if "<link" in line:
                    assert "#a83a2a" not in line
                    assert "#9f3b2e" not in line

    # --- the neutrals -----------------------------------------------------

    @pytest.mark.parametrize(
        ("token", "value"), sorted(_RATIFIED_TOKENS.items()),
    )
    def test_issue_neutral_matches_ratified_palette(
        self, issue_html: str, token: str, value: str
    ) -> None:
        assert f"{token}:{value}" in issue_html

    @pytest.mark.parametrize(
        ("token", "value"), sorted(_RATIFIED_TOKENS.items()),
    )
    def test_landing_neutral_matches_ratified_palette(
        self, landing_html: str, token: str, value: str
    ) -> None:
        assert f"{token}: {value}" in landing_html

    def test_ink_3_holds_at_the_aa_passing_lightness(
        self, issue_html: str, landing_html: str
    ) -> None:
        """Guards the deliberate departure. The palette's 0.56 measures
        4.49:1 on --paper; anyone 'correcting' this to match the palette
        drops the dateline and footer below AA."""
        assert "--ink-3:oklch(0.55 0.010 66)" in issue_html
        assert "--ink-3: oklch(0.55 0.010 66)" in landing_html
        assert "oklch(0.56 0.010 66)" not in issue_html
        assert "oklch(0.56 0.010 66)" not in landing_html

    # --- the semantic triad stays a comment, not tokens --------------------

    def test_semantic_triad_is_recorded_but_not_declared(
        self, issue_html: str
    ) -> None:
        """Sanctioned for future badge surfaces, deliberately not tokens:
        the chip they were built for is deleted."""
        for value in (
            "oklch(0.55 0.090 45)",
            "oklch(0.54 0.085 72)",
            "oklch(0.52 0.075 156)",
        ):
            assert value in issue_html
        for dead in ("--rust:", "--amber:", "--sage:", "--ev1"):
            assert dead not in issue_html


# ===========================================================================
# TestMark — "The Open Block", ratified 2026-08-09.
#
# A solid square with a leaning V cut out of it. The V is the paper showing
# through, which is why `fill-rule="evenodd"` is not decoration: drop it and
# the second subpath paints instead of subtracting, and the mark becomes a
# plain filled square with no letter in it at all.
#
# The mark is INK on every surface. The accent is a scarce signal already
# spent on the wordmark slash 14px away.
# ===========================================================================

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CANONICAL_MARK = _REPO_ROOT / "docs" / "brand" / "aiv-mark.svg"
_FAVICON = _REPO_ROOT / "docs" / "favicon.svg"

# The one path all four copies must agree on.
_MARK_PATH = (
    "M0 0L100 0L100 100L0 100ZM18.37 0L36.37 84.7L100 16.46L100 0"
    "L93.47 0L45.63 51.3L34.72 0Z"
)
_INK_HEX = "#3b352d"


def _path_d(svg_text: str) -> str:
    m = re.search(r'd="([^"]+)"', svg_text)
    assert m, "no path data found"
    return m.group(1)


def _markup_only(svg_text: str) -> str:
    """Strip XML comments. These files carry long explanatory comments that
    quote the very values the assertions below forbid (the retired #ffffff
    ground, the currentColor that cannot work in a favicon). Asserting against
    the raw text would fail on the explanation rather than the markup."""
    return re.sub(r"<!--.*?-->", "", svg_text, flags=re.S)


class TestMark:
    @pytest.fixture
    def issue_html(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> str:
        _write_staging(FIXED_DATE, rich_issue)
        return render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")

    @pytest.fixture
    def landing_html(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> str:
        _write_released(FIXED_DATE, rich_issue, number=1)
        render(FIXED_DATE, mode="release")
        return _paths.DOCS_INDEX.read_text(encoding="utf-8")

    # --- the canonical file is the source of truth ------------------------

    def test_canonical_mark_exists(self) -> None:
        assert _CANONICAL_MARK.exists(), f"missing {_CANONICAL_MARK}"

    def test_canonical_mark_carries_the_ratified_path(self) -> None:
        assert _path_d(_CANONICAL_MARK.read_text(encoding="utf-8")) == _MARK_PATH

    def test_canonical_mark_uses_currentcolor(self) -> None:
        """In-page use inherits ink from its container."""
        assert 'fill="currentColor"' in _CANONICAL_MARK.read_text(encoding="utf-8")

    def test_every_derivative_matches_the_canonical_path(self) -> None:
        """The favicon and the template partial are derivatives. If any drifts,
        the identity is quietly two marks."""
        canonical = _path_d(_CANONICAL_MARK.read_text(encoding="utf-8"))
        favicon = _path_d(_FAVICON.read_text(encoding="utf-8"))
        partial = _path_d(
            (_REPO_ROOT / "templates" / "_mark.svg.j2").read_text(encoding="utf-8")
        )
        assert favicon == canonical
        assert partial == canonical

    # --- fill-rule is load-bearing ----------------------------------------

    @pytest.mark.parametrize(
        "source",
        ["canonical", "favicon", "partial", "issue", "landing"],
    )
    def test_evenodd_survives_everywhere(
        self, source: str, issue_html: str, landing_html: str
    ) -> None:
        """Without evenodd the counterform V fills solid and the mark is a
        square. Every copy must carry it.

        Comments are stripped first, and that is the whole point: three of
        these files explain in prose that evenodd is load-bearing, so an
        assertion against the raw text passes on the explanation even after
        the attribute itself has been deleted from the path. Asserting on the
        `<path` element specifically is what makes this test able to fail.
        """
        text = {
            "canonical": _CANONICAL_MARK.read_text(encoding="utf-8"),
            "favicon": _FAVICON.read_text(encoding="utf-8"),
            "partial": (
                _REPO_ROOT / "templates" / "_mark.svg.j2"
            ).read_text(encoding="utf-8"),
            "issue": issue_html,
            "landing": landing_html,
        }[source]
        paths = re.findall(r"<path\b[^>]*>", _markup_only(text))
        assert paths, "no <path> element found"
        for path_el in paths:
            assert 'fill-rule="evenodd"' in path_el, path_el

    # --- rendered into both mastheads -------------------------------------

    def test_issue_masthead_carries_the_mark(self, issue_html: str) -> None:
        assert '<span class="mark" aria-hidden="true">' in issue_html
        assert _MARK_PATH in issue_html

    def test_landing_hero_carries_the_mark(self, landing_html: str) -> None:
        assert '<span class="mark" aria-hidden="true">' in landing_html
        assert _MARK_PATH in landing_html

    def test_mark_precedes_the_wordmark(self, issue_html: str) -> None:
        """Spec section 1: the mark sits before the wordmark text, inside
        .brand, so the lockup reads mark-then-word."""
        brand = issue_html[issue_html.index('<div class="brand">'):]
        brand = brand[: brand.index("</div>")]
        assert brand.index("</svg>") < brand.index('<span class="slash">')

    def test_mark_is_hidden_from_assistive_tech(
        self, issue_html: str, landing_html: str
    ) -> None:
        """The wordmark text beside it already says "AI Vector"; announcing
        the mark too would read the brand twice."""
        assert 'class="mark" aria-hidden="true"' in issue_html
        assert 'class="mark" aria-hidden="true"' in landing_html

    # --- sizing per the integration spec ----------------------------------

    def test_issue_mark_sized_per_spec(self, issue_html: str) -> None:
        assert (
            ".brand .mark{display:inline-block;width:22px;height:22px;"
            "margin-right:14px;vertical-align:baseline;color:var(--ink)}"
        ) in issue_html

    def test_landing_mark_sized_per_spec(self, landing_html: str) -> None:
        assert "width: 36px; height: 36px; margin-right: 23px;" in landing_html

    @pytest.mark.parametrize(
        "rule",
        [
            ".brand .mark { width: 27px; height: 27px; margin-right: 17px; }",
            ".brand .mark { width: 23px; height: 23px; margin-right: 15px; }",
        ],
    )
    def test_landing_mark_mirrors_both_breakpoints(
        self, landing_html: str, rule: str
    ) -> None:
        assert rule in landing_html

    # --- ink, never accent -------------------------------------------------

    def test_mark_takes_ink_not_accent(
        self, issue_html: str, landing_html: str
    ) -> None:
        """The accent is spent on the slash. A vermilion mark was considered
        on a real issue and declined."""
        assert ".brand .mark{" in issue_html
        mark_rule = issue_html[issue_html.index(".brand .mark{"):]
        mark_rule = mark_rule[: mark_rule.index("}")]
        assert "color:var(--ink)" in mark_rule
        assert "accent" not in mark_rule

    def test_slash_keeps_the_accent(
        self, issue_html: str, landing_html: str
    ) -> None:
        assert ".brand .slash{color:var(--accent)}" in issue_html
        assert ".brand .slash { color: var(--accent); }" in landing_html

    # --- footer unchanged per spec section 3 ------------------------------

    def test_footer_colophon_has_no_mark(self, issue_html: str) -> None:
        """Spec section 3: no change. The wordmark stays alone at 19px."""
        footer = issue_html[issue_html.index('<footer class="colophon'):]
        assert 'class="mark"' not in footer
        assert "<svg" not in footer

    # --- favicon -----------------------------------------------------------

    def test_favicon_has_no_opaque_ground(self) -> None:
        """The old file painted a cold #ffffff square against warm paper. The
        replacement is transparent so the tab strip shows through."""
        markup = _markup_only(_FAVICON.read_text(encoding="utf-8"))
        assert "<rect" not in markup
        assert "#ffffff" not in markup

    def test_favicon_uses_an_explicit_fill_not_currentcolor(self) -> None:
        """An SVG favicon renders as its own document, so currentColor has
        nothing to inherit and would resolve to black."""
        markup = _markup_only(_FAVICON.read_text(encoding="utf-8"))
        assert f"fill:{_INK_HEX}" in markup
        assert "currentColor" not in markup

    def test_favicon_carries_a_dark_scheme_query(self) -> None:
        markup = _markup_only(_FAVICON.read_text(encoding="utf-8"))
        assert "prefers-color-scheme:dark" in markup
        assert "#f4f0e7" in markup

    def test_retired_ancestor_is_gone(self) -> None:
        """The V-plus-chevron favicon and its vermilion are retired."""
        markup = _markup_only(_FAVICON.read_text(encoding="utf-8"))
        assert "#e6452f" not in markup
        assert "#a83a2a" not in markup
        assert "#1a1a1a" not in markup

    def test_mask_icon_is_ink_on_both_surfaces(
        self, issue_html: str, landing_html: str
    ) -> None:
        assert f'rel="mask-icon" href="../favicon.svg" color="{_INK_HEX}"' in issue_html
        assert f'rel="mask-icon" href="favicon.svg" color="{_INK_HEX}"' in landing_html

    def test_no_vermilion_left_in_any_link_tag(
        self, issue_html: str, landing_html: str
    ) -> None:
        for html in (issue_html, landing_html):
            for line in html.splitlines():
                if "<link" in line:
                    assert "#a83a2a" not in line
                    assert "#9f3b2e" not in line
                    assert "#e6452f" not in line


# ===========================================================================
# TestNavigation — getting out of an issue, and getting home.
#
# A reader who lands on a dated permalink from a search result or a shared
# link has, until now, had no way back to the archive: the issue page was a
# leaf. Three anchors fix that — the masthead lockup, an "All issues" link
# in the masthead meta line, and one in the colophon.
# ===========================================================================

_INDEX_HREF_FROM_ISSUE = "../index.html"


def _brand_without(env_key: str) -> dict:
    """A brand dict with one key removed, for the fork-safety branches."""
    brand = dict(_render_mod._load_brand())
    brand.pop(env_key, None)
    return brand


class TestNavigation:
    @pytest.fixture
    def issue_html(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> str:
        _write_staging(FIXED_DATE, rich_issue)
        return render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")

    @pytest.fixture
    def landing_html(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> str:
        _write_released(FIXED_DATE, rich_issue, number=1)
        render(FIXED_DATE, mode="release")
        return _paths.DOCS_INDEX.read_text(encoding="utf-8")

    # --- "All issues", twice on the issue page ----------------------------

    def test_masthead_carries_an_all_issues_link(self, issue_html: str) -> None:
        meta = issue_html[issue_html.index('<span class="mast-meta">'):]
        meta = meta[: meta.index("</span>", meta.index("</a>"))]
        assert f'<a class="navlink" href="{_INDEX_HREF_FROM_ISSUE}">' in meta
        assert "All issues" in meta

    def test_colophon_carries_an_all_issues_link(self, issue_html: str) -> None:
        footer = issue_html[issue_html.index('<footer class="colophon'):]
        assert f'<a class="navlink" href="{_INDEX_HREF_FROM_ISSUE}">all issues' in footer

    def test_issue_page_has_exactly_two_all_issues_links(
        self, issue_html: str
    ) -> None:
        """One in the masthead, one in the colophon. A third would be clutter
        and a missing one is the leaf-page problem returning."""
        assert issue_html.lower().count(">all issues &rarr;</a>") == 2

    def test_all_issues_links_are_relative_to_the_archive_dir(
        self, issue_html: str
    ) -> None:
        """Issue pages live in docs/released/, so the landing page is one
        directory up. An absolute /index.html would break the local preview
        and any fork served from a subpath."""
        assert 'href="/index.html"' not in issue_html
        assert issue_html.count(f'href="{_INDEX_HREF_FROM_ISSUE}"') == 3

    def test_navlink_styling_is_the_quiet_register(self, issue_html: str) -> None:
        """Mono, ink-2, hairline rule, accent on hover — the same signal the
        source links already use."""
        assert (
            ".navlink{font-family:var(--mono);color:var(--ink-2);"
            "text-decoration:none;border-bottom:1px solid var(--line-2);"
        ) in issue_html
        assert ".navlink:hover{color:var(--accent)" in issue_html

    # --- the lockup is the home link --------------------------------------

    def test_issue_lockup_is_wrapped_in_a_home_anchor(
        self, issue_html: str
    ) -> None:
        brand = issue_html[issue_html.index('<div class="brand">'):]
        brand = brand[: brand.index("</div>")]
        assert brand.startswith(
            f'<div class="brand"><a href="{_INDEX_HREF_FROM_ISSUE}">'
        )
        # the anchor wraps BOTH the mark and the wordmark
        assert '<span class="mark"' in brand
        assert '<span class="slash">' in brand
        assert brand.index("<a ") < brand.index('<span class="mark"')
        assert brand.index('<span class="slash">') < brand.index("</a>")

    def test_landing_lockup_is_wrapped_in_a_self_anchor(
        self, landing_html: str
    ) -> None:
        brand = landing_html[landing_html.index('<h1 class="brand">'):]
        brand = brand[: brand.index("</h1>")]
        assert '<a href="index.html">' in brand
        assert '<span class="mark"' in brand
        assert brand.index("<a ") < brand.index('<span class="mark"')
        assert brand.index('<span class="slash">') < brand.index("</a>")

    @pytest.mark.parametrize("surface", ["issue", "landing"])
    def test_lockup_anchor_is_visually_silent(
        self, issue_html: str, landing_html: str, surface: str
    ) -> None:
        """Inheriting colour is what keeps the wordmark ink and the slash
        accent; without it the global `a{color:var(--accent)}` would paint the
        whole lockup vermilion."""
        html = issue_html if surface == "issue" else landing_html
        assert "color:inherit" in html.replace(" ", "")
        assert "textdecoration:none" in html.replace(" ", "").replace("-", "")

    # --- author attribution ------------------------------------------------

    def test_issue_colophon_links_the_author(self, issue_html: str) -> None:
        footer = issue_html[issue_html.index('<footer class="colophon'):]
        assert (
            '<a class="navlink" href="https://armanabrahamyan.com">'
            "Arman Abrahamyan</a>"
        ) in footer

    def test_author_sits_between_wordmark_and_ethos(self, issue_html: str) -> None:
        footer = issue_html[issue_html.index('<footer class="colophon'):]
        assert (
            footer.index('class="brand-sm"')
            < footer.index('class="by"')
            < footer.index('class="ethos"')
        )

    def test_landing_footer_links_the_author(self, landing_html: str) -> None:
        assert (
            '<a href="https://armanabrahamyan.com">Arman Abrahamyan</a>'
            in landing_html
        )

    @pytest.mark.parametrize("template", ["issue.html.j2", "index.html.j2"])
    def test_author_degrades_to_plain_text_without_a_url(
        self, rich_issue: Issue, template: str
    ) -> None:
        """Fork safety: clearing author_url must yield plain text, never an
        anchor pointing nowhere. Rendered directly so the brand dict can be
        overridden without touching config/brand.yaml."""
        env = _build_env()
        env.globals["brand"] = _brand_without("author_url")
        if template == "issue.html.j2":
            html = env.get_template(template).render(
                issue=rich_issue,
                pulse_story=rich_issue.pulse.stories[0],
                digest=[],
                read_minutes=1,
                dup_risk_dates=[],
                show_verify_flags=False,
            )
        else:
            html = env.get_template(template).render(
                latest=None, latest_kicker="", latest_digest=[],
                archive_data=[], generated_at=FIXED_NOW,
            )
        assert "Arman Abrahamyan" in html
        assert ">Arman Abrahamyan</a>" not in html

    def test_author_url_has_a_packaged_default(self) -> None:
        """A fork that never writes config/brand.yaml still gets a working
        by-line, matching how every other brand key behaves."""
        assert (
            _render_mod._DEFAULT_BRAND["author_url"]
            == "https://armanabrahamyan.com"
        )

    def test_brand_yaml_carries_author_url(self) -> None:
        assert _render_mod._load_brand()["author_url"] == "https://armanabrahamyan.com"


# ===========================================================================
# TestHowItsMade — the standing "How it's made" page.
#
# The copy is operator-ratified and verbatim. The assertions below spot-check
# sentences from every section, which is what makes a silent reword fail
# rather than merely look different.
# ===========================================================================

# One sentence per section, chosen to be unmistakable.
_RATIFIED_SENTENCES: list[str] = [
    "Every story has a headline, a one-line take saying what it means, a "
    "summary, and a link to its source.",
    "AI Vector is open source.",
    "The code is MIT-licensed. The issue text is copyright.",
    "reads about ninety feeds each morning: AI labs, research groups, "
    "regulators, practitioner blogs, and community forums.",
    "Nothing is scraped from websites.",
    "Many feeds report the same news, so articles about the same story are "
    "grouped and the story appears once.",
    "Each group is scored against a written rubric: significance, hands-on "
    "usefulness, big-picture relevance, financial-services impact, freshness.",
    "Two checks then run.",
    "Every issue is either read and approved by a person before it goes out, "
    "or published under rules that person wrote, checks afterwards, and can "
    "switch off.",
    "An issue is held if a story makes a claim its source contradicts, or if "
    "a check did not run.",
    "The financial-services angle appears when a story has one: some days "
    "that is half the issue, some days none.",
    "No. 34 becomes No. 34.1.",
    "Factual errors are corrected on the day they are confirmed. A story "
    "that should not have run is removed.",
    "Corrections, disagreements, and sources worth adding go to",
    "who approves every issue before it publishes:",
]

_RATIFIED_HEADINGS: list[str] = [
    "Open source",
    "Where the stories come from",
    "How a story is chosen",
    "What the machine does and what a person does",
    "Corrections",
    "Contact",
]


class TestHowItsMade:
    @pytest.fixture
    def rendered(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> dict[str, str]:
        """Render a release, which is what emits the standing pages."""
        _write_released(FIXED_DATE, rich_issue, number=1)
        render(FIXED_DATE, mode="release")
        docs = _paths.DOCS_ROOT
        return {
            "how": (docs / "how-its-made.html").read_text(encoding="utf-8"),
            "about": (docs / "about.html").read_text(encoding="utf-8"),
            "index": _paths.DOCS_INDEX.read_text(encoding="utf-8"),
            "issue": _paths.released_html_path(FIXED_DATE).read_text(
                encoding="utf-8"
            ),
        }

    # --- emitted alongside the index --------------------------------------

    def test_page_is_written_on_release(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_released(FIXED_DATE, rich_issue, number=1)
        render(FIXED_DATE, mode="release")
        assert (_paths.DOCS_ROOT / "how-its-made.html").exists()

    def test_page_is_rewritten_on_unrelease(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """unrelease() rebuilds the index, so the standing pages ride along and
        cannot be left behind by a rollback."""
        _write_released(FIXED_DATE, rich_issue, number=1)
        render(FIXED_DATE, mode="release")
        target = _paths.DOCS_ROOT / "how-its-made.html"
        target.unlink()
        unrelease(FIXED_DATE)
        assert target.exists()

    def test_staging_preview_does_not_emit_it(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """A staging preview touches nothing canonical, standing pages
        included."""
        _write_staging(FIXED_DATE, rich_issue)
        render(FIXED_DATE, mode="preview")
        assert not (_paths.DOCS_ROOT / "how-its-made.html").exists()
        assert not (_paths.DOCS_ROOT / "about.html").exists()

    # --- the ratified copy, verbatim --------------------------------------

    @pytest.mark.parametrize("sentence", _RATIFIED_SENTENCES)
    def test_ratified_sentence_is_present(
        self, rendered: dict[str, str], sentence: str
    ) -> None:
        assert sentence in rendered["how"]

    @pytest.mark.parametrize("heading", _RATIFIED_HEADINGS)
    def test_heading_renders_in_the_heading_register(
        self, rendered: dict[str, str], heading: str
    ) -> None:
        """Bold lines in the ratified copy are section headings, not prose
        emphasis — they must be <h2>, not <strong>."""
        assert f"<h2>{heading}</h2>" in rendered["how"]
        assert f"<strong>{heading}</strong>" not in rendered["how"]

    def test_title_nav_label_and_h1_all_say_how_its_made(
        self, rendered: dict[str, str]
    ) -> None:
        how = rendered["how"]
        assert "<title>How it's made &mdash; AI Vector</title>" in how
        assert "<h1>How it's made</h1>" in how
        assert 'How it\'s made &rarr;</a>' in rendered["index"]

    def test_opening_paragraph_uses_the_brand_name(
        self, rendered: dict[str, str]
    ) -> None:
        assert "AI Vector publishes every weekday morning." in rendered["how"]

    # --- exactly two body anchors ------------------------------------------

    def test_body_carries_exactly_two_anchors(
        self, rendered: dict[str, str]
    ) -> None:
        """The ratified copy names two links and no others. Every other anchor
        on the page is chrome (the lockup and the nav links), so the count is
        taken over the prose paragraphs only."""
        body = re.findall(r'<p class="body">.*?</p>', rendered["how"], re.S)
        anchors = [a for p in body for a in re.findall(r"<a [^>]*href=\"([^\"]+)\"", p)]
        assert anchors == [
            "https://github.com/armanabrahamyan/ai-vector-newsletter",
            "https://armanabrahamyan.com",
        ]

    def test_named_links_render_as_their_own_text(
        self, rendered: dict[str, str]
    ) -> None:
        how = rendered["how"]
        assert (
            '<a href="https://github.com/armanabrahamyan/ai-vector-newsletter">'
            "github.com/armanabrahamyan/ai-vector-newsletter</a>"
        ) in how
        assert (
            '<a href="https://armanabrahamyan.com">armanabrahamyan.com</a>'
        ) in how

    # --- the /about redirect -----------------------------------------------

    def test_about_is_a_redirect_to_the_page(
        self, rendered: dict[str, str]
    ) -> None:
        about = rendered["about"]
        assert '<meta http-equiv="refresh" content="0; url=how-its-made.html">' in about
        assert '<link rel="canonical" href="how-its-made.html">' in about

    def test_about_offers_a_manual_link_for_no_refresh_clients(
        self, rendered: dict[str, str]
    ) -> None:
        assert '<a href="how-its-made.html">' in rendered["about"]

    def test_about_is_not_indexed(self, rendered: dict[str, str]) -> None:
        """A stub competing with the real page in search results would be a
        worse outcome than not having it."""
        assert '<meta name="robots" content="noindex">' in rendered["about"]

    def test_about_carries_no_copy_of_its_own(
        self, rendered: dict[str, str]
    ) -> None:
        """It must never become a second place the explanation lives."""
        for sentence in _RATIFIED_SENTENCES:
            assert sentence not in rendered["about"]

    # --- reachable from both surfaces --------------------------------------

    def test_index_links_to_the_page(self, rendered: dict[str, str]) -> None:
        assert '<a class="navlink" href="how-its-made.html">' in rendered["index"]

    def test_issue_colophon_links_to_the_page(
        self, rendered: dict[str, str]
    ) -> None:
        footer = rendered["issue"][rendered["issue"].index('<footer class="colophon'):]
        assert '<a class="navlink" href="../how-its-made.html">' in footer
        assert "how it's made" in footer

    def test_issue_nav_links_travel_together(
        self, rendered: dict[str, str]
    ) -> None:
        """Grouped so a narrow measure wraps them as a unit instead of
        stranding one on its own line."""
        footer = rendered["issue"][rendered["issue"].index('<footer class="colophon'):]
        nav = footer[footer.index('<span class="colo-nav">'):]
        nav = nav[: nav.index("</span>", nav.rindex("</a>"))]
        assert nav.count("<a ") == 2

    # --- it is a page of the same design ----------------------------------

    def test_page_carries_the_current_tokens(
        self, rendered: dict[str, str]
    ) -> None:
        """The reason this file is emitted rather than hand-kept: it has to
        carry the same palette as the issues, every time."""
        how = rendered["how"]
        for token, value in sorted(_RATIFIED_TOKENS.items()):
            assert f"{token}:{value}" in how

    def test_page_carries_the_mark_and_home_link(
        self, rendered: dict[str, str]
    ) -> None:
        how = rendered["how"]
        assert _MARK_PATH in how
        assert 'fill-rule="evenodd"' in how
        brand = how[how.index('<div class="brand">'):]
        brand = brand[: brand.index("</div>")]
        assert brand.startswith('<div class="brand"><a href="index.html">')

    def test_page_uses_the_shared_geometry(
        self, rendered: dict[str, str]
    ) -> None:
        how = rendered["how"]
        assert "--measure:620px" in how
        assert "max-width:812px" in how
        assert "padding:64px 96px 76px" in how


# ===========================================================================
# TestCodeSpans — literal `backticks` in generated prose become <code>.
#
# Six real released summaries carry them (all Hands-On shell fragments:
# `hf jobs run`, `-DGGML_ET=ON`, `store: hf`, ...). The filter runs AFTER
# escaping, so the safety property is the one worth pinning down hardest:
# the only markup it may introduce is the <code> element itself.
# ===========================================================================

class TestCodeSpans:
    def test_backticks_become_code_elements(self) -> None:
        assert (
            _code_spans("run `hf jobs run` today")
            == "run <code>hf jobs run</code> today"
        )

    def test_multiple_spans_in_one_string(self) -> None:
        assert (
            _code_spans("`kernels` and `kernel-builder`")
            == "<code>kernels</code> and <code>kernel-builder</code>"
        )

    def test_flag_style_fragment(self) -> None:
        """The real 2026-07-11 case: a build flag, not a word."""
        assert (
            _code_spans("build with `-DGGML_ET=ON`")
            == "build with <code>-DGGML_ET=ON</code>"
        )

    def test_unpaired_backtick_is_left_alone(self) -> None:
        assert _code_spans("a stray ` backtick") == "a stray ` backtick"

    def test_backticks_do_not_span_a_newline(self) -> None:
        assert _code_spans("a `b\nc` d") == "a `b\nc` d"

    def test_empty_span_is_not_converted(self) -> None:
        assert _code_spans("nothing `` here") == "nothing `` here"

    # --- the safety property ---------------------------------------------

    def test_markup_outside_a_span_is_escaped(self) -> None:
        assert _code_spans("<b>bold</b> & risk") == "&lt;b&gt;bold&lt;/b&gt; &amp; risk"

    def test_markup_inside_a_span_is_escaped(self) -> None:
        """The dangerous case: escaping happens BEFORE the substitution, so
        the span's contents are already inert when <code> wraps them."""
        out = _code_spans("try `<script>alert(1)</script>`")
        assert out == "try <code>&lt;script&gt;alert(1)&lt;/script&gt;</code>"
        assert "<script>" not in out

    def test_a_forged_code_element_stays_escaped(self) -> None:
        """Prose that literally contains `</code><script>` must not be able
        to break out of the element the filter just opened."""
        out = _code_spans("`a</code><script>x</script>`")
        assert "<script>" not in out
        assert "&lt;/code&gt;&lt;script&gt;" in out
        assert out.count("<code>") == 1
        assert out.count("</code>") == 1

    def test_none_renders_as_empty(self) -> None:
        assert _code_spans(None) == ""

    def test_result_is_markup_so_jinja_will_not_double_escape(self) -> None:
        from markupsafe import Markup
        assert isinstance(_code_spans("`x`"), Markup)

    # --- rendered ---------------------------------------------------------

    def test_summary_backticks_render_as_code_in_html(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        block = SummaryBlock(
            story_id="c_" + "1" * 12,
            headline="Hugging Face Jobs runs a vLLM server in one command",
            summary="A single `hf jobs run` command now starts the server.",
            source_urls=["https://example.com/hf"],
            signal="try",
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
            sections=[IssueSection(name="hands_on", stories=[block])],
            generated_at=FIXED_NOW,
            prompt_versions={"rank": "v1", "summarise": "v1"},
        )
        _write_staging(FIXED_DATE, issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert "<code>hf jobs run</code>" in html
        assert "`hf jobs run`" not in html

    def test_stylesheet_carries_a_code_rule(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert "code{font-family:var(--mono)" in html


# ===========================================================================
# TestFullReadHead — the label only earns its place opposite a digest.
# ===========================================================================

class TestFullReadHead:
    def test_head_absent_without_a_digest(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """Every real preview hits this path: no digest, so "The full read"
        named a contrast that was not on the page."""
        assert not rich_issue.digest
        _write_staging(FIXED_DATE, rich_issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert "The full read" not in html

    def test_head_present_with_a_digest(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        pulse_id = rich_issue.pulse.stories[0].story_id
        with_digest = rich_issue.model_copy(update={
            # The model requires at least three bullets.
            "digest": [
                DigestBullet(
                    lead=f"Bullet {n} lead.",
                    sentence=f"Bullet {n} sentence carrying the detail.",
                    story_ids=[pulse_id],
                )
                for n in range(1, 4)
            ]
        })
        _write_staging(FIXED_DATE, with_digest)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert "The 30-second read" in html
        assert "The full read" in html


# ===========================================================================
# TestSectionSynthesis — the italic paragraph under each section head.
# `IssueSection.synthesis` (v4) wins; the legacy intro_lead + intro_body
# pair is joined for archived issues.
# ===========================================================================

class TestSectionSynthesis:
    def _issue_with_section(self, *, story_count: int = 2, **section_kwargs) -> Issue:
        """Two stories by default: a synthesis over a single story is
        suppressed by the render guard, so one story would silently make
        every synthesis assertion below vacuous."""
        stories = [
            SummaryBlock(
                story_id=f"c_{i:012x}",
                headline=f"A headline for big picture {i}",
                summary="Summary prose here for the big picture story.",
                source_urls=[f"https://example.com/bp{i}"],
            )
            for i in range(1, story_count + 1)
        ]
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
                IssueSection(name="big_picture", stories=stories, **section_kwargs)
            ],
            generated_at=FIXED_NOW,
            prompt_versions={"rank": "v1", "summarise": "v1"},
        )

    # --- the single-story guard -------------------------------------------

    def test_single_story_section_suppresses_the_synthesis(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """A synthesis over one story is that story restated. The guard is
        render-time and unconditional, whatever generation produced."""
        _write_staging(
            FIXED_DATE,
            self._issue_with_section(
                story_count=1, synthesis="Speed is outrunning safety."
            ),
        )
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert '<p class="synthesis">' not in html
        assert "Speed is outrunning safety." not in html

    def test_single_story_legacy_intro_still_renders(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """The n=1 guard applies to the v4 synthesis field only. A legacy
        intro on a one-story section is ratified published content on every
        archive issue; the 2026-08-10 conversion must not drop it."""
        _write_staging(
            FIXED_DATE,
            self._issue_with_section(
                story_count=1, intro_lead="The threat has already arrived."
            ),
        )
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert (
            '<p class="synthesis">The threat has already arrived.</p>' in html
        )

    def test_empty_section_with_quiet_day_text_renders_it(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """A zero-story Currents carrying the quiet-day line is content,
        not an empty shell: the section head and line render, the story
        count kicker does not."""
        issue = self._issue_with_section(story_count=2)
        issue = issue.model_copy(update={"sections": issue.sections + [
            IssueSection(
                name="currents",
                stories=[],
                intro_lead="A quiet day in the undercurrents.",
                intro_body="Little cleared the bar today.",
            )
        ]})
        _write_staging(FIXED_DATE, issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        currents = html[html.index('class="section currents"'):]
        assert (
            '<p class="synthesis">A quiet day in the undercurrents. '
            'Little cleared the bar today.</p>' in currents
        )
        head = currents[:currents.index("</div>")]
        assert '<span class="kicker">' not in head

    def test_empty_section_with_v4_quiet_day_synthesis_renders_it(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """The live 2026-08-10 bug: a zero-story Currents whose quiet-day
        text is in the v4 synthesis field (not the legacy pair) rendered a
        bare heading -- the outer guard admitted the section, the inner
        guard suppressed its only content. Suppression is for exactly one
        story, never zero."""
        issue = self._issue_with_section(story_count=2)
        issue = issue.model_copy(update={"sections": issue.sections + [
            IssueSection(
                name="currents",
                stories=[],
                synthesis="Nothing surfaced for Currents today.",
            )
        ]})
        _write_staging(FIXED_DATE, issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        currents = html[html.index('class="section currents"'):]
        assert (
            '<p class="synthesis">Nothing surfaced for Currents today.</p>'
            in currents
        )

    def test_empty_section_with_no_text_stays_absent(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """The other side of the empty-section rule: nothing to say, no
        section shell."""
        issue = self._issue_with_section(story_count=2)
        issue = issue.model_copy(update={"sections": issue.sections + [
            IssueSection(name="currents", stories=[])
        ]})
        _write_staging(FIXED_DATE, issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert 'class="section currents"' not in html

    def test_two_story_section_still_renders_the_synthesis(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """The boundary, from the other side: the guard must be `> 1`, not
        a blanket suppression."""
        _write_staging(
            FIXED_DATE,
            self._issue_with_section(
                story_count=2, synthesis="Speed is outrunning safety."
            ),
        )
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert '<p class="synthesis">Speed is outrunning safety.</p>' in html

    def test_synthesis_field_renders(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(
            FIXED_DATE,
            self._issue_with_section(synthesis="Speed is outrunning safety."),
        )
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert (
            '<p class="synthesis">Speed is outrunning safety.</p>' in html
        )

    def test_legacy_intro_pair_is_joined(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """Every released archive issue carries the pair, not the field.
        Joining with one space is what makes those pages render unchanged."""
        _write_staging(
            FIXED_DATE,
            self._issue_with_section(
                intro_lead="Bold opening phrase.",
                intro_body="One or two framing sentences.",
            ),
        )
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert (
            '<p class="synthesis">Bold opening phrase. '
            'One or two framing sentences.</p>' in html
        )

    def test_lead_only_renders_without_trailing_space(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(
            FIXED_DATE, self._issue_with_section(intro_lead="Lead phrase only.")
        )
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert '<p class="synthesis">Lead phrase only.</p>' in html

    def test_element_absent_when_nothing_set(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, self._issue_with_section())
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        # The stylesheet always carries .synthesis rules; assert the element.
        assert '<p class="synthesis">' not in html

    def test_synthesis_is_html_escaped(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(
            FIXED_DATE, self._issue_with_section(synthesis="<b>bold</b> & risk")
        )
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert "<b>bold</b>" not in html
        assert "&lt;b&gt;bold&lt;/b&gt; &amp; risk" in html

    # --- unit: preference order -------------------------------------------

    def test_helper_prefers_synthesis_over_legacy_pair(self) -> None:
        section = IssueSection(
            name="big_picture",
            stories=[],
            synthesis="New paragraph.",
        )
        # model_copy bypasses the "never both" validator so the preference
        # order is observable at all.
        both = section.model_copy(
            update={"intro_lead": "Old lead.", "intro_body": "Old body."}
        )
        assert _section_synthesis(both) == "New paragraph."

    def test_helper_returns_empty_string_for_nothing(self) -> None:
        section = IssueSection(name="currents", stories=[])
        assert _section_synthesis(section) == ""


# ===========================================================================
# TestDigest — "The 30-second read". Issue.digest is 3-5 DigestBullets;
# absent on every pre-redesign archive issue, and the whole section must
# then vanish rather than leave an empty block.
# ===========================================================================

def _bullet(lead: str, sentence: str, story_id: str) -> DigestBullet:
    return DigestBullet(lead=lead, sentence=sentence, story_ids=[story_id])


def _issue_with_digest(digest) -> Issue:
    """Pulse + one hands_on story; digest bullets point at the pulse story."""
    return Issue(
        date=FIXED_DATE,
        pulse=IssueSection(
            name="pulse",
            stories=[
                SummaryBlock(
                    story_id=VALID_CLUSTER_ID,
                    headline="Pulse headline",
                    summary="Pulse summary sentence for the digest tests.",
                    source_urls=["https://example.com/pulse"],
                )
            ],
        ),
        sections=[
            IssueSection(
                name="hands_on",
                stories=[
                    SummaryBlock(
                        story_id=VALID_CLUSTER_ID_2,
                        headline="Hands-on headline",
                        summary="Story summary sentence for the digest tests.",
                        source_urls=["https://example.com/story"],
                    )
                ],
            )
        ],
        digest=digest,
        generated_at=FIXED_NOW,
        prompt_versions={"rank": "v1", "summarise": "v1"},
    )


class TestDigest:
    def _three(self) -> list[DigestBullet]:
        return [
            _bullet("Agents run locally now.", "Quantised weights land.", VALID_CLUSTER_ID),
            _bullet("Speed outruns safety.", "Controls lag behind pace.", VALID_CLUSTER_ID),
            _bullet("Verify before you adopt.", "Vendor numbers dominate.", VALID_CLUSTER_ID_2),
        ]

    def _render(self, digest) -> str:
        _write_staging(FIXED_DATE, _issue_with_digest(digest))
        return render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")

    def test_section_renders_with_kicker(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        html = self._render(self._three())
        assert (
            '<span class="kicker accent">The 30-second read</span>' in html
        )

    def test_bullet_markup_is_strong_lead_then_span_sentence(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        html = self._render(self._three())
        assert (
            "<p><strong>Agents run locally now.</strong> "
            "<span>Quantised weights land.</span></p>" in html
        )

    def test_every_bullet_renders(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        html = self._render(self._three())
        skim = html[html.index('<div class="skim">'):html.index('<div class="full-head">')]
        assert skim.count("<strong>") == 3

    def test_digest_sits_above_the_full_read(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        html = self._render(self._three())
        assert html.index('The 30-second read') < html.index('The full read')

    def test_section_omitted_entirely_when_digest_absent(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """Every issue released before 2026-08-09 has digest=None. The
        section must not render as an empty block."""
        html = self._render(None)
        assert "The 30-second read" not in html
        assert '<div class="skim">' not in html
        assert '<div class="skim-head">' not in html

    def test_digest_is_html_escaped(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        html = self._render([
            _bullet("<b>bold</b> lead", "risk & <script>x</script>", VALID_CLUSTER_ID),
            _bullet("Second.", "Second sentence.", VALID_CLUSTER_ID),
            _bullet("Third.", "Third sentence.", VALID_CLUSTER_ID),
        ])
        assert "<b>bold</b> lead" not in html
        assert "<script>x</script>" not in html
        assert "&lt;b&gt;bold&lt;/b&gt; lead" in html
        assert "risk &amp; &lt;script&gt;x&lt;/script&gt;" in html

    # --- unit: the normaliser ---------------------------------------------

    def test_normalise_accepts_model_objects(self) -> None:
        items = _normalise_digest(self._three())
        assert items[0] == {
            "lead": "Agents run locally now.",
            "sentence": "Quantised weights land.",
        }

    def test_normalise_accepts_raw_dicts(self) -> None:
        """The landing-page builder reads archive JSON, not parsed models."""
        items = _normalise_digest([{"lead": "A.", "sentence": "B.", "story_ids": ["x"]}])
        assert items == [{"lead": "A.", "sentence": "B."}]

    @pytest.mark.parametrize("raw", [None, "", 0, {}, "not a list"])
    def test_normalise_non_list_yields_empty(self, raw) -> None:
        assert _normalise_digest(raw) == []

    def test_normalise_drops_a_bullet_with_neither_field(self) -> None:
        assert _normalise_digest([{"lead": "  ", "sentence": ""}]) == []


# ===========================================================================
# TestTake — the take renders as the bold takeaway-first slot (ratified
# deviation #2 from the handoff: `.takeaway` carries our SummaryBlock.take).
#
# The absence assertions guard the 35 released archive pages: a story with
# take=None must render dek-only, with no layout hole.
# ===========================================================================

def _issue_with_takes(story_take: str | None, pulse_take: str | None) -> Issue:
    """A minimal two-story issue (pulse + one hands_on story) whose takes are
    set from the arguments. `None` means the story carries no take."""
    return Issue(
        date=FIXED_DATE,
        pulse=IssueSection(
            name="pulse",
            stories=[
                SummaryBlock(
                    story_id=VALID_CLUSTER_ID,
                    headline="Pulse headline",
                    summary="Pulse summary sentence for the take tests.",
                    source_urls=["https://example.com/pulse"],
                    take=pulse_take,
                )
            ],
        ),
        sections=[
            IssueSection(
                name="hands_on",
                stories=[
                    SummaryBlock(
                        story_id=VALID_CLUSTER_ID_2,
                        headline="Hands-on headline",
                        summary="Story summary sentence for the take tests.",
                        source_urls=["https://example.com/story"],
                        take=story_take,
                    )
                ],
            )
        ],
        generated_at=FIXED_NOW,
        prompt_versions={"rank": "v1", "summarise": "v1"},
    )


def _render_template(issue: Issue, *, show_verify_flags: bool = False) -> str:
    """Render issue.html.j2 with the exact context src.render.render() uses.
    Used only where the Issue cannot survive a validating round-trip (the
    empty-string take, which the model rejects at the boundary)."""
    env = _build_env()
    template = env.get_template(TEMPLATE_NAME)
    return template.render(
        issue=issue,
        pulse_story=issue.pulse.stories[0],
        digest=_digest_items(issue),
        read_minutes=_read_minutes(issue),
        dup_risk_dates=[],
        show_verify_flags=show_verify_flags,
    )


class TestTake:
    def _render(
        self,
        story_take: str | None,
        pulse_take: str | None,
    ) -> str:
        _write_staging(FIXED_DATE, _issue_with_takes(story_take, pulse_take))
        return render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")

    # --- rendered element -------------------------------------------------

    def test_story_take_renders_as_takeaway(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        html = self._render("The interrupt API is the point.", None)
        assert (
            '<p class="takeaway">The interrupt API is the point.</p>' in html
        )

    def test_pulse_take_renders_as_takeaway(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        html = self._render(None, "Parity, not novelty.")
        assert '<p class="takeaway">Parity, not novelty.</p>' in html

    def test_takeaway_sits_above_the_dek(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """Takeaway-first is the whole point of the slot: the so-what
        precedes the facts, in both the Pulse and every story."""
        html = self._render("Ordering matters.", "Position, then facts.")
        pulse = html[html.index("<h1>"):html.index('<section class="section')]
        assert pulse.index('class="takeaway"') < pulse.index('class="dek"')
        story = html[html.index("<h3>"):]
        assert story.index('class="takeaway"') < story.index('class="dek"')

    def test_takeaway_sits_below_the_headline(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        html = self._render("After the headline.", None)
        story = html[html.index("<h3>"):]
        assert story.index("</h3>") < story.index('class="takeaway"')

    # --- escaping: take text is LLM output ---------------------------------

    def test_story_take_is_html_escaped(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        html = self._render(
            'Watch <script>alert("bank")</script> & the desk\'s call', None
        )
        assert "<script>alert" not in html
        assert "&lt;script&gt;alert(&#34;bank&#34;)&lt;/script&gt;" in html
        assert "&amp; the desk&#39;s call" in html

    def test_pulse_take_is_html_escaped(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        html = self._render(None, "<b>bold</b> & risky")
        assert "<b>bold</b>" not in html
        assert "&lt;b&gt;bold&lt;/b&gt; &amp; risky" in html

    # --- absence: the pre-take archive must render cleanly -----------------

    def test_no_take_renders_no_takeaway(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        # The stylesheet always carries .takeaway rules; assert the element.
        assert '<p class="takeaway">' not in html

    def test_no_take_leaves_headline_adjacent_to_dek(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """No layout hole: with take=None the conditional collapses so the
        headline is followed directly by the dek, with no blank line and no
        stray indentation for the flex gap to space out."""
        _write_staging(FIXED_DATE, rich_issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert '</h3>\n    <p class="dek">' in html
        assert '</h1>\n  <p class="dek">' in html

    def test_empty_string_take_renders_no_takeaway(self) -> None:
        """Defence in depth. SummaryBlock rejects a blank take at the
        boundary (min_length=1 after strip), so this state cannot reach a
        validating load — but the template must not emit an empty bold
        line if it ever does. model_copy bypasses validation to build it."""
        issue = _issue_with_takes(None, None)
        section = issue.sections[0]
        mutated = issue.model_copy(update={
            "sections": [
                section.model_copy(update={
                    "stories": [section.stories[0].model_copy(update={"take": ""})],
                }),
            ],
        })
        html = _render_template(mutated)
        assert '<p class="takeaway">' not in html
        assert '<p class="dek">' in html


# ===========================================================================
# TestOperatorUiIsStagingOnly — the advisory verify badges and the
# duplicate-risk banner are operator UI. Guarded jointly with the
# Experience Designer: they appear in docs/staging/, never in a released
# page, and a released page therefore carries exactly the design's CSS.
# ===========================================================================

class TestOperatorUiIsStagingOnly:
    def _issue_with_verification(self) -> Issue:
        verification = StoryVerification(
            story_id=VALID_CLUSTER_ID_2,
            prompt_version="v1",
            claims=[
                ClaimVerdict(
                    claim="a claim",
                    verdict="unsupported",
                    location="headline",
                    summary_span="span",
                )
            ],
            has_contradiction=False,
            has_unsupported=True,
            headline_flagged=True,
        )
        issue = _issue_with_takes(None, None)
        section = issue.sections[0]
        return issue.model_copy(update={
            "sections": [
                section.model_copy(update={
                    "stories": [
                        section.stories[0].model_copy(
                            update={"verification": verification}
                        )
                    ],
                }),
            ],
        })

    def test_flag_renders_in_staging_preview(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, self._issue_with_verification())
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert 'class="op-flag hard"' in html
        assert "headline claim flagged" in html

    def test_flag_absent_from_released_page(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_released(FIXED_DATE, self._issue_with_verification(), number=1)
        html = render(FIXED_DATE, mode="release").read_text(encoding="utf-8")
        assert "op-flag" not in html
        assert "headline claim flagged" not in html

    def test_released_page_carries_exactly_one_style_block(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """The operator CSS lives in its own <style> block behind the
        staging flag, so a released page's CSS is the design CSS and
        nothing else. This is the load-bearing pixel-fidelity guard."""
        _write_released(FIXED_DATE, rich_issue, number=1)
        html = render(FIXED_DATE, mode="release").read_text(encoding="utf-8")
        assert html.count("<style>") == 1
        assert "op-" not in html

    def test_staging_preview_carries_the_second_style_block(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert html.count("<style>") == 2

    def test_dup_gate_banner_is_staging_only(self) -> None:
        issue = _issue_with_takes(None, None)
        env = _build_env()
        template = env.get_template(TEMPLATE_NAME)
        ctx = dict(
            issue=issue,
            pulse_story=issue.pulse.stories[0],
            digest=[],
            read_minutes=1,
        )
        staging = template.render(
            dup_risk_dates=["2026-05-23"], show_verify_flags=True, **ctx
        )
        released = template.render(
            dup_risk_dates=[], show_verify_flags=False, **ctx
        )
        assert "Duplicate risk" in staging
        assert "aiv release --date 2026-05-23" in staging
        assert "Duplicate risk" not in released


# ===========================================================================
# TestMastheadAndColophon — the two fixed chrome elements of the design.
# ===========================================================================

class TestMastheadAndColophon:
    def test_released_masthead_is_number_date_readtime(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_released(FIXED_DATE, rich_issue, number=7)
        html = render(FIXED_DATE, mode="release").read_text(encoding="utf-8")
        # The meta line gained an "All issues" link after the read time, so it
        # no longer closes here; the number/date/read-time run is what matters.
        assert (
            '<span class="mast-meta">No. 7 &middot; 24 May &middot; 1 min'
            in html
        )

    def test_staging_masthead_says_preview(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert (
            '<span class="mast-meta">Preview &middot; 24 May &middot; 1 min'
            in html
        )

    def test_revision_shows_in_masthead(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        revised = rich_issue.model_copy(update={"revision": 2})
        _write_released(FIXED_DATE, revised, number=7)
        html = render(FIXED_DATE, mode="release").read_text(encoding="utf-8")
        assert "No. 7.2 &middot;" in html

    def test_colophon_matches_the_handoff(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_released(FIXED_DATE, rich_issue, number=1)
        html = render(FIXED_DATE, mode="release").read_text(encoding="utf-8")
        assert (
            '<span class="brand-sm">AI'
            '<span class="slash" style="color:var(--accent)">/</span>Vector</span>'
            in html
        )
        assert (
            '<span class="ethos">Curated, not aggregated. '
            'AI-drafted, human-accountable.</span>' in html
        )

    def test_story_count_kicker_uses_items_for_currents(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_staging(FIXED_DATE, rich_issue)
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        assert '<span class="kicker">1 item</span>' in html
        assert '<span class="kicker">1 story</span>' in html

    def test_self_hosted_fonts_not_google_cdn(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """Repo doctrine: fonts are self-hosted woff2, never a CDN link."""
        _write_released(FIXED_DATE, rich_issue, number=1)
        html = render(FIXED_DATE, mode="release").read_text(encoding="utf-8")
        assert '<link rel="stylesheet" href="../fonts/fonts.css">' in html
        assert "fonts.googleapis.com" not in html
        assert "fonts.gstatic.com" not in html


# ===========================================================================
# TestLandingHeroDigest — the inline 30-second read in the index hero.
# ===========================================================================

class TestLandingHeroDigest:
    def test_hero_shows_digest_when_latest_has_one(
        self, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        digest = [
            _bullet("Regulation turns readable.", "The rulebook ships as data.", VALID_CLUSTER_ID),
            _bullet("Agents breach perimeters.", "Two new studies landed.", VALID_CLUSTER_ID),
            _bullet("Verify the benchmark.", "Vendor numbers dominate.", VALID_CLUSTER_ID),
        ]
        _write_released(FIXED_DATE, _issue_with_digest(digest), number=1)
        render(FIXED_DATE, mode="release")
        index = _paths.DOCS_INDEX.read_text(encoding="utf-8")
        assert "The 30-second read" in index
        assert (
            "<p><strong>Regulation turns readable.</strong> "
            "<span>The rulebook ships as data.</span></p>" in index
        )

    def test_hero_omits_skim_when_latest_has_no_digest(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_released(FIXED_DATE, rich_issue, number=1)
        render(FIXED_DATE, mode="release")
        index = _paths.DOCS_INDEX.read_text(encoding="utf-8")
        assert "The 30-second read" not in index
        assert '<div class="skim">' not in index

    def test_index_carries_recent_block_and_expand_all(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        earlier = FIXED_DATE - _dt.timedelta(days=1)
        _write_released(earlier, rich_issue.model_copy(update={"date": earlier}), number=1)
        _write_released(FIXED_DATE, rich_issue, number=2)
        render(FIXED_DATE, mode="release")
        index = _paths.DOCS_INDEX.read_text(encoding="utf-8")
        assert 'id="recent-block"' in index
        assert 'id="expand-all"' in index
        assert 'id="archive-data"' in index

    def test_index_uses_self_hosted_fonts(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        _write_released(FIXED_DATE, rich_issue, number=1)
        render(FIXED_DATE, mode="release")
        index = _paths.DOCS_INDEX.read_text(encoding="utf-8")
        assert '<link rel="stylesheet" href="fonts/fonts.css">' in index
        assert "fonts.googleapis.com" not in index


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
        assert '>example.com <span class="arw">&rarr;</span></a>' in html
        # Verify the www. prefix does NOT appear as visible link text
        # (it may appear inside href= attributes, which is correct).
        assert ">www.example.com <span" not in html


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

    def test_aest_output_appears_on_the_landing_page(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """The redesigned issue colophon carries no build timestamp, so the
        `aest` filter's only reader-visible output is the landing page's
        "Page generated" line."""
        _write_released(FIXED_DATE, rich_issue, number=1)
        render(FIXED_DATE, mode="release")
        index = _paths.DOCS_INDEX.read_text(encoding="utf-8")
        assert "Page generated " in index
        assert "AEST" in index or "AEDT" in index


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

    def test_optional_peripherals_copied_when_present(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """verify.json, review.md, gate.json, revisions.jsonl are advisory
        peripherals: promoted when the upstream stage produced them, so a
        released day carries the evidence it shipped on."""
        _write_staging(FIXED_DATE, rich_issue)
        staging = _paths.staging_dir(FIXED_DATE)
        for name, content in (
            ("verify.json", '{"verdict": "clean"}'),
            ("review.md", "---\nverdict: green\n---\n\nfine\n"),
            ("gate.json", '{"decision": "auto_merge"}'),
            ("revisions.jsonl", '{"revision": 0}\n'),
        ):
            (staging / name).write_text(content, encoding="utf-8")

        release_promote(FIXED_DATE)
        released = _paths.released_dir(FIXED_DATE)
        for name in ("verify.json", "review.md", "gate.json", "revisions.jsonl"):
            assert (released / name).exists(), name

    def test_optional_peripherals_absent_does_not_fail_release(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """None of verify.json / review.md / gate.json / revisions.jsonl are
        written by _write_staging -- absence must not fail the release, and
        none should appear in the released dir either."""
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE)
        released = _paths.released_dir(FIXED_DATE)
        for name in ("verify.json", "review.md", "gate.json", "revisions.jsonl"):
            assert not (released / name).exists(), name

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

    def test_unrelease_removes_optional_peripherals(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """An unreleased day must not keep carrying evidence (review.md,
        gate.json) for an issue that is no longer released."""
        _write_staging(FIXED_DATE, rich_issue)
        staging = _paths.staging_dir(FIXED_DATE)
        (staging / "review.md").write_text(
            "---\nverdict: green\n---\n\nfine\n", encoding="utf-8"
        )
        (staging / "gate.json").write_text(
            '{"decision": "auto_merge"}', encoding="utf-8"
        )
        release_promote(FIXED_DATE)
        released = _paths.released_dir(FIXED_DATE)
        assert (released / "review.md").exists()
        assert (released / "gate.json").exists()

        unrelease(FIXED_DATE)
        assert not (released / "review.md").exists()
        assert not (released / "gate.json").exists()

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


# ===========================================================================
# TestReleaseRevise -- same-date re-release bumps `revision`, preserves
# `issue_number`. Added v0.9 (task #76, 2026-05-24).
# ===========================================================================

class TestReleaseRevise:
    def test_first_release_has_revision_zero(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """First release of a date -> revision=0, display_number='N'."""
        _write_staging(FIXED_DATE, rich_issue)
        final = release_promote(FIXED_DATE)
        assert final.revision == 0
        assert final.display_number == "1"

    def test_revise_bumps_revision_not_issue_number(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """The core invariant: --revise on an already-released date
        preserves issue_number and bumps revision by 1."""
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE)  # #1 rev 0
        _write_staging(FIXED_DATE, rich_issue)
        revised = release_promote(FIXED_DATE, revise=True)  # #1 rev 1
        assert revised.issue_number == 1
        assert revised.revision == 1
        assert revised.display_number == "1.1"

    def test_revise_multiple_times_accumulates(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """rev 0 -> 1 -> 2 -> 3. The integer registry never moves."""
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE)
        for expected_rev in (1, 2, 3):
            _write_staging(FIXED_DATE, rich_issue)
            revised = release_promote(FIXED_DATE, revise=True)
            assert revised.issue_number == 1
            assert revised.revision == expected_rev

    def test_revise_persists_to_canonical_issue_json(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """The canonical issue.json on disk carries the bumped revision."""
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE)
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE, revise=True)
        payload = json.loads(
            _paths.issue_path(FIXED_DATE, canonical=True).read_text(encoding="utf-8")
        )
        assert payload["issue_number"] == 1
        assert payload["revision"] == 1

    def test_revise_does_not_burn_a_new_integer_for_later_dates(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """A subsequent first-release of a later date follows the prior
        integer -- the revision-bump does not consume an integer slot."""
        date_a = FIXED_DATE - _dt.timedelta(days=1)
        date_b = FIXED_DATE
        # date_a: first release -> #1
        _write_staging(date_a, rich_issue)
        release_promote(date_a)
        # date_a: revise -> #1.1 (no integer burn)
        _write_staging(date_a, rich_issue)
        release_promote(date_a, revise=True)
        # date_b: first release -> still #2 (max canonical integer +1 == 2)
        _write_staging(date_b, rich_issue)
        final_b = release_promote(date_b)
        assert final_b.issue_number == 2
        assert final_b.revision == 0

    def test_revise_on_first_release_raises_already_released(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """Default (revise=False) still raises AlreadyReleased on a
        date that already has a canonical issue.json -- the safety net
        for accidental double-fires is unchanged."""
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE)
        _write_staging(FIXED_DATE, rich_issue)
        with pytest.raises(AlreadyReleased):
            release_promote(FIXED_DATE)  # no --revise

    def test_revise_with_no_prior_release_falls_through_to_first_release(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """If --revise is passed but there's no canonical for the date,
        treat it as a first release (no existing issue_number to
        preserve). revision=0; integer assigned via max+1."""
        _write_staging(FIXED_DATE, rich_issue)
        final = release_promote(FIXED_DATE, revise=True)
        assert final.issue_number == 1
        assert final.revision == 0

    def test_unrelease_then_release_resets_revision(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """Full unrelease wipes the date dir; the next first release
        starts at revision=0 again (the counter does not survive)."""
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE)
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE, revise=True)  # rev 1
        unrelease(FIXED_DATE)
        _write_staging(FIXED_DATE, rich_issue)
        fresh = release_promote(FIXED_DATE)  # first release again
        assert fresh.revision == 0

    def test_revise_renders_dotted_number_in_html(
        self, rich_issue: Issue, tmp_data_root: Path, tmp_docs: Path
    ) -> None:
        """The released HTML masthead shows 'Issue No. N.M'."""
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE)
        _write_staging(FIXED_DATE, rich_issue)
        release_promote(FIXED_DATE, revise=True)
        html = _paths.released_html_path(FIXED_DATE).read_text(encoding="utf-8")
        assert "Issue No. 1.1" in html
