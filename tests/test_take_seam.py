"""Cross-module seam test — the take flows end-to-end (2026-08-08).

Every take-related test elsewhere in the suite is module-local: summarise
tests pin `_summarise_one`'s output, render tests build an `Issue` directly
with `take=...` already set, review/verify tests hand-build a raw payload
dict with a `"take"` key already in it. None of them prove that the SAME
string a real `summarise()` call produces is the string that later shows up
in rendered HTML, in review's quotable-field index, and in verify's
claim-extraction input. That is a distinct failure mode from any single
module's own bug: a key-name typo or an intermediate dict rebuild at any of
the module seams (Issue assembly, `model_dump_json` / `model_validate_json`,
the raw-payload reload used by review/verify) would pass every existing
take test while breaking the real pipeline. This test closes that gap.

Mocked LLM boundaries only, matching each module's own established mock
seam (`tests/CONVENTIONS.md` #11, `tests/test_verify_day.py`'s docstring):
  - `summarise._llm_call` — produces the take.
  - `verify.verify_rich` — the verify-stage mock boundary already used by
    `tests/test_verify_day.py`; we assert on the ``take`` keyword it is
    CALLED with, not on its return value (see CONVENTIONS #3 "don't test
    mocks").
Everything between those two boundaries — SummaryBlock validation, Issue
assembly, the issue.json round trip, the real Jinja2 template, and
review's field indexer — runs unmocked.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from src import paths as _paths
from src import review as review_mod
from src import summarise as summarise_mod
from src import verify as verify_mod
from src.models import Cluster, Issue, IssueSection, Item, RankedStory
from src.render import render
from src.verify import ClaimVerdict

from tests.conftest import FIXED_DATE, FIXED_EARLIER, FIXED_NOW

TAKE_TEXT = "Local-first inference is the procurement default now."


def _make_story_cluster_item() -> tuple[RankedStory, Cluster, Item]:
    """A minimal valid (RankedStory, Cluster, Item) triple for a hands_on
    tier story. Deliberately NOT imported from test_summarise.py: importing
    a `Test*`-named class from another test module makes pytest collect it
    a second time under this file's node IDs (double-counts the suite).
    Kept intentionally small -- this file's only job is the cross-module
    wiring, not story-construction coverage (that belongs to test_summarise.py)."""
    breakdown = {
        "significance": 60, "hands_on_utility": 60,
        "big_picture_relevance": 60, "financial_services_impact": 25,
        "freshness_momentum": 60,
    }
    story = RankedStory(
        cluster_id="c_0123456789abcdef",
        score=55, breakdown=breakdown,
        audience_tags=["general"],  # type: ignore[arg-type]
        rationale="t", tier="hands_on", prompt_version="v0.2",
    )
    cluster = Cluster(
        cluster_id=story.cluster_id, item_ids=["i_x"],
        canonical_title="t", sources=["src_a"],
        earliest_published=FIXED_EARLIER, size=1,
    )
    item = Item(
        id="i_x", source="src_a", source_type="rss",
        url="https://example.com/x",  # type: ignore[arg-type]
        title="t", published_at=FIXED_EARLIER, raw_summary="",
        fetched_at=FIXED_NOW, trust_weight=2,
    )
    return story, cluster, item


@pytest.fixture
def tmp_docs(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Redirect docs/ output, mirroring test_render.py's fixture of the
    same name (kept local so this file has no cross-file fixture coupling
    beyond the shared conftest)."""
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(_paths, "DOCS_ROOT", docs)
    monkeypatch.setattr(_paths, "STAGING_HTML_DIR", docs / "staging")
    monkeypatch.setattr(_paths, "RELEASED_HTML_DIR", docs / "released")
    monkeypatch.setattr(_paths, "DOCS_INDEX", docs / "index.html")
    return docs


class TestTakeFlowsThroughTheRealPipeline:
    def test_summarise_output_reaches_render_review_and_verify(
        self, tmp_data_root, tmp_docs, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # --- 1. summarise: a real _summarise_one call, LLM boundary mocked.
        story, cluster, item = _make_story_cluster_item()
        raw_llm_response = json.dumps({
            "headline": "Local inference gear reaches parity with hosted APIs",
            "summary": " ".join(["word"] * 40),
            "take": TAKE_TEXT,
            "signal": "try",
        })
        monkeypatch.setattr(
            summarise_mod, "_fetch_source_excerpt", lambda url: "",
        )
        with patch.object(
            summarise_mod, "_llm_call", return_value=raw_llm_response,
        ) as mock_llm:
            block = summarise_mod._summarise_one(
                story=story, cluster=cluster, items=[item], callbacks=[],
            )
        assert block is not None
        assert block.take == TAKE_TEXT
        # Sanity on the mock itself: one call, no corrective retry needed
        # (draft was within every cap) -- if this ever goes to 2, the fixture
        # drifted out of spec and the rest of the test is exercising retry
        # behaviour instead of the seam.
        assert mock_llm.call_count == 1

        # --- 2. Issue assembly + a REAL issue.json round trip.
        issue = Issue(
            date=FIXED_DATE,
            pulse=IssueSection(name="pulse", stories=[block]),
            sections=[
                IssueSection(name="big_picture", stories=[]),
                IssueSection(name="hands_on", stories=[]),
                IssueSection(name="currents", stories=[]),
            ],
            generated_at=FIXED_NOW,
            prompt_versions={"rank": "v1", "summarise": "v1"},
        )
        staging = _paths.staging_dir(FIXED_DATE)
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "issue.json").write_text(
            issue.model_dump_json(indent=2), encoding="utf-8"
        )

        # --- 3. render(): re-parses issue.json from disk; the take must
        # reach the rendered HTML through the real template.
        html = render(FIXED_DATE, mode="preview").read_text(encoding="utf-8")
        # The 2026-08-09 layout redesign renders the take in the design's
        # bold takeaway-first slot (`.takeaway`), above the body (`.dek`).
        assert f'<p class="takeaway">{TAKE_TEXT}</p>' in html

        # --- 4. review.index_issue_fields: the SAME take text, reloaded
        # from disk as review.py actually reads it (raw JSON, not pydantic).
        reloaded_payload = json.loads(
            (staging / "issue.json").read_text(encoding="utf-8")
        )
        field_index = review_mod.index_issue_fields(reloaded_payload)
        assert (
            field_index[f"story:{block.story_id}:take"] == TAKE_TEXT
        )

        # --- 5. verify._verify_one_story: the SAME take text reaches the
        # claim-extraction input (asserted on the call args, not the mock's
        # return value -- CONVENTIONS #3).
        captured: dict[str, str] = {}

        def _stub_verify_rich(headline, body, source_excerpt, *,
                              take: str = "", **kw):
            captured["take"] = take
            return [ClaimVerdict(
                claim="c", verdict="unverifiable", location="body",
            )]

        monkeypatch.setattr(verify_mod, "verify_rich", _stub_verify_rich)
        pulse_block_payload = reloaded_payload["pulse"]["stories"][0]
        verify_mod._verify_one_story(pulse_block_payload, {})
        assert captured["take"] == TAKE_TEXT
