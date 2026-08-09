"""Cross-module seam test -- the digest + section synthesis flow end to end
(2026-08-09, wave three of the layout redesign).

Every digest/synthesis-related test elsewhere in the suite is module-local:
summarise tests pin ``_generate_digest`` / ``_populate_section_synthesis``
output directly; verify tests hand-build a raw payload dict with a
``"digest"`` key and a ``"synthesis"`` key already in it
(``tests/test_verify_day.py::TestAuxSurfaces``); review tests call
``index_issue_fields`` on a hand-built payload; revise tests hand-build a
``_FieldGroup`` with ``target.kind == "digest"`` already set. None of them
prove that the SAME bullet a real ``summarise._generate_digest()`` call
writes is the bullet that later reaches verify's auxiliary claim
extraction, review's ``digest:<i>:digest_lead`` field index, and revise's
``digest_index`` routing back onto ``issue.json`` -- or that the SAME
synthesis a real ``summarise._populate_section_synthesis()`` call writes
survives the same chain. A key-name typo, an intermediate dict rebuild at
any module seam, or a digest_index off-by-one would pass every existing
digest/synthesis test while breaking the real pipeline. This test closes
that gap, mirroring ``tests/test_take_seam.py`` (CONVENTIONS.md #12) for
the two wave-three surfaces.

Mocked LLM boundaries only, one per stage, matching each module's own
established mock seam (CONVENTIONS.md #11):
  - ``summarise._llm_call`` -- produces the digest bullets and the
    synthesis.
  - ``verify.verify_rich`` / ``verify.verify_aux_rich`` -- the verify-stage
    mock boundaries ``tests/test_verify_day.py`` already establishes.
  - ``revise._call_revise_llm`` -- the revise-stage mock boundary
    ``tests/test_revise.py`` already establishes.
  - ``render.render`` -- revise's live path re-renders HTML; stubbed
    exactly as ``tests/test_revise.py``'s autouse fixture does, so this
    file does not also have to stand up a docs/ tree.

Everything between those boundaries -- DigestBullet/IssueSection
validation, Issue assembly, the issue.json round trip, verify's excerpt
union + primary-story attachment, review's field indexer, and revise's
digest_index / section-target routing -- runs unmocked.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src import paths
from src import review as review_mod
from src import revise as revise_mod
from src import summarise as summarise_mod
from src import verify as verify_mod
from src.models import (
    Issue,
    IssueSection,
    ReviewFinding,
    ReviewReport,
    ReviewTarget,
    SummaryBlock,
)
from src.verify import ClaimVerdict

from tests.conftest import FIXED_DATE, FIXED_NOW

_PULSE_ID = "c_aaaaaaaaaaaa"
_BP1_ID = "c_bbbbbbbbbbbb"
_BP2_ID = "c_cccccccccccc"
_HO1_ID = "c_dddddddddddd"
_HO2_ID = "c_eeeeeeeeeeee"

_PULSE_URL = "https://example.com/pulse-story"
_BP1_URL = "https://example.com/bp-one"
_BP2_URL = "https://example.com/bp-two"
_HO1_URL = "https://example.com/ho-one"
_HO2_URL = "https://example.com/ho-two"

_BP_SYNTHESIS = (
    "Governance stories spent the day moving from policy papers into "
    "running code, and the shift changes who owns compliance risk inside "
    "a bank. The pattern rewards firms that already treat audit as an "
    "engineering surface, not a quarterly report."
)

_DIGEST_LLM_PAYLOAD = {
    "bullets": [
        {"section": "pulse",
         "lead": "Regulator text, cited direct.",
         "sentence": ("The UK regulator published machine-readable rules "
                      "that map every obligation to a section of primary "
                      "legislation."),
         "story_ids": [_PULSE_ID]},
        {"section": "big_picture",
         "lead": "Runtime compliance scoring arrives.",
         "sentence": ("Two banks shipped runtime compliance scoring into "
                      "production, replacing the annual model audit with "
                      "continuous checks."),
         # Cites BOTH Big Picture stories -- exercises the cross-story
         # excerpt union (V2) inside the seam, not just the single-story
         # case.
         "story_ids": [_BP1_ID, _BP2_ID]},
        {"section": "hands_on",
         "lead": "Guardrail suites, timed properly.",
         "sentence": ("A new open harness runs guardrail suites against "
                      "any agent stack in fourteen minutes on one "
                      "machine."),
         "story_ids": [_HO1_ID]},
    ]
}


@pytest.fixture(autouse=True)
def render_stub():
    """Stub the render call revise's live path issues -- mirrors
    tests/test_revise.py's identical fixture. tmp_data_root redirects
    data/ but not docs/, and this file has no business rewriting the
    repo's committed pages."""
    with patch("src.render.render", return_value=Path("docs/rendered.html")):
        yield


def _story(story_id: str, headline: str, url: str) -> SummaryBlock:
    return SummaryBlock(
        story_id=story_id, headline=headline,
        summary=" ".join(["word"] * 40),
        source_urls=[url],  # type: ignore[list-item]
    )


class TestDigestAndSynthesisFlowThroughTheRealPipeline:
    def test_summarise_output_reaches_verify_review_and_revise(
        self, tmp_data_root: Path,
    ) -> None:
        # --- 1. summarise: real _populate_section_synthesis + real
        # _generate_digest, LLM boundary mocked. -----------------------
        pulse_section = IssueSection(
            name="pulse", stories=[_story(_PULSE_ID, "Pulse headline", _PULSE_URL)],
        )
        bp_section = IssueSection(
            name="big_picture",
            stories=[
                _story(_BP1_ID, "BP one", _BP1_URL),
                _story(_BP2_ID, "BP two", _BP2_URL),
            ],
        )
        ho_section = IssueSection(
            name="hands_on",
            stories=[
                _story(_HO1_ID, "HO one", _HO1_URL),
                _story(_HO2_ID, "HO two", _HO2_URL),
            ],
            # Not the subject of this seam test (only Big Picture's
            # synthesis is); hardcoded so the digest floor (pulse + 2
            # eligible sections >= 3 bullets) is met without a second
            # real LLM round trip to assert on.
            synthesis=(
                "Tooling releases clustered around evaluation harnesses "
                "rather than models. The practical move is in the "
                "scaffolding this week, not the weights."
            ),
        )
        currents_section = IssueSection(name="currents", stories=[])

        def _synthesis_llm(prompt, **_kw):
            assert "big_picture" not in prompt.lower() or "BP one" in prompt
            return json.dumps({"synthesis": _BP_SYNTHESIS})

        with patch.object(summarise_mod, "_llm_call", side_effect=_synthesis_llm):
            summarise_mod._populate_section_synthesis(bp_section)
        assert bp_section.synthesis == _BP_SYNTHESIS

        with patch.object(
            summarise_mod, "_llm_call",
            return_value=json.dumps(_DIGEST_LLM_PAYLOAD),
        ) as mock_digest_llm:
            digest = summarise_mod._generate_digest(
                pulse_section, [bp_section, ho_section, currents_section],
            )
        assert digest is not None and len(digest) == 3
        # One call, no corrective retry -- if this goes to 2 the fixture
        # drifted out of spec and the rest of the test exercises retry
        # behaviour instead of the seam.
        assert mock_digest_llm.call_count == 1
        bp_bullet = digest[1]
        assert bp_bullet.story_ids == [_BP1_ID, _BP2_ID]
        assert bp_bullet.lead == "Runtime compliance scoring arrives."

        # --- 2. Issue assembly + a REAL issue.json round trip. ---------
        issue = Issue(
            date=FIXED_DATE,
            pulse=pulse_section,
            sections=[bp_section, ho_section, currents_section],
            digest=digest,
            generated_at=FIXED_NOW,
            prompt_versions={"rank": "v1", "summarise": "v1"},
        )
        staging = paths.staging_dir(FIXED_DATE)
        staging.mkdir(parents=True, exist_ok=True)
        issue_path = staging / "issue.json"
        issue_path.write_text(issue.model_dump_json(indent=2), encoding="utf-8")

        excerpts_path = paths.source_excerpts_path(FIXED_DATE, canonical=False)
        excerpts_path.parent.mkdir(parents=True, exist_ok=True)
        with excerpts_path.open("w", encoding="utf-8") as fh:
            for url, sid, excerpt in (
                (_PULSE_URL, _PULSE_ID, "PULSE-EXCERPT"),
                (_BP1_URL, _BP1_ID, "BP1-EXCERPT"),
                (_BP2_URL, _BP2_ID, "BP2-EXCERPT"),
                (_HO1_URL, _HO1_ID, "HO1-EXCERPT"),
                (_HO2_URL, _HO2_ID, "HO2-EXCERPT"),
            ):
                fh.write(json.dumps({
                    "schema_version": 1, "url": url, "excerpt": excerpt,
                    "fetched_at": FIXED_NOW.isoformat(), "story_id": sid,
                }) + "\n")

        # --- 3. verify_day(): re-parses issue.json from disk; the SAME
        # bullet text and synthesis text must reach the aux verifier,
        # scoped to the union of ITS cited stories' excerpts (V2), and
        # attach to the right primary story (V3). ------------------------
        aux_calls: list[tuple[str, str, str]] = []

        def _stub_verify_rich(headline, body, source_excerpt, **kw):
            return [ClaimVerdict(
                claim="story claim", verdict="supported", location="body",
                source_span="x",
            )]

        def _stub_verify_aux(kind, text, source_excerpt, **kw):
            aux_calls.append((kind, text, source_excerpt))
            return [ClaimVerdict(
                claim=f"{kind} claim", verdict="supported", location="body",
                source_span="x",
            )]

        with patch.object(verify_mod, "verify_rich", _stub_verify_rich), \
             patch.object(verify_mod, "verify_aux_rich", _stub_verify_aux):
            report = verify_mod.verify_day(FIXED_DATE)

        digest_calls = [c for c in aux_calls if c[0] == "digest"]
        assert len(digest_calls) == 3
        bp_digest_call = digest_calls[1]
        # The SAME text summarise wrote (lead + sentence), not a
        # reconstruction of it.
        assert bp_digest_call[1] == f"{bp_bullet.lead} {bp_bullet.sentence}"
        # V2: the union of BOTH cited stories' excerpts, citation order.
        assert bp_digest_call[2] == "BP1-EXCERPT\n\nBP2-EXCERPT"

        # Hands-On also carries a (hardcoded, not the subject here)
        # synthesis, so both sections' syntheses reach the aux verifier --
        # pick out Big Picture's call specifically.
        syn_calls = [c for c in aux_calls if c[0] == "synthesis"]
        assert len(syn_calls) == 2
        bp_syn_call = next(c for c in syn_calls if c[1] == _BP_SYNTHESIS)
        assert bp_syn_call[2] == "BP1-EXCERPT\n\nBP2-EXCERPT"

        by_id = {s.story_id: s for s in report.stories}
        bp1_verification = by_id[_BP1_ID]
        assert any(
            c.note.startswith("digest: ") for c in bp1_verification.claims
        ), "the digest verdict did not attach to the bullet's primary story"
        assert any(
            c.note.startswith("synthesis: ") for c in bp1_verification.claims
        ), "the synthesis verdict did not attach to the section's first story"

        # --- 4. review.index_issue_fields: reload issue.json exactly as
        # review.py does (raw JSON, not pydantic) -- the SAME digest lead
        # and synthesis text must be addressable by the keys revise.py
        # resolves against. ------------------------------------------------
        reloaded_payload = json.loads(issue_path.read_text(encoding="utf-8"))
        field_index = review_mod.index_issue_fields(reloaded_payload)
        assert field_index["digest:1:digest_lead"] == bp_bullet.lead
        assert (
            field_index["section:big_picture:synthesis"] == _BP_SYNTHESIS
        )

        # --- 5. revise_day(): a review.json naming both targets, LLM
        # boundary mocked, routes each replacement back onto the SAME
        # bullet index and the SAME section -- never the other's slot. ---
        sha_before = hashlib.sha256(issue_path.read_bytes()).hexdigest()
        findings = [
            ReviewFinding(
                finding_id="f001",
                target=ReviewTarget(
                    kind="digest", digest_index=1, field="digest_lead",
                ),
                criterion="closing_shape",
                severity="major",
                quote=bp_bullet.lead,
                fix_kind="text_edit",
                instruction="Tighten the lead.",
            ),
            ReviewFinding(
                finding_id="f002",
                target=ReviewTarget(
                    kind="section", section="big_picture", field="synthesis",
                ),
                criterion="closing_shape",
                severity="major",
                quote=_BP_SYNTHESIS,
                fix_kind="text_edit",
                instruction="Sharpen the framing.",
            ),
        ]
        review_report = ReviewReport(
            generated_at=FIXED_NOW,
            computed_verdict="amber",
            findings=findings,
            prompt_version="v1.0",
            issue_sha256=sha_before,
        )
        review_mod._write_report_json(
            review_mod.review_json_path(FIXED_DATE), review_report,
        )

        _NEW_LEAD = "Runtime compliance scoring lands wider."
        # A light edit -- small enough to clear revise's containment gate
        # (edit_distance_exceeded), which is the point: this proves the
        # SAME (near-identical) text round-trips, not an unbounded rewrite.
        _NEW_SYNTHESIS = (
            "Governance stories spent the day moving from policy papers "
            "into running code, and the shift changes who owns compliance "
            "risk inside a bank. The pattern rewards firms that treat "
            "audit as an engineering discipline, not a quarterly report."
        )

        def _stub_revise_llm(prompt: str) -> str:
            if bp_bullet.lead in prompt:
                return _NEW_LEAD
            if _BP_SYNTHESIS in prompt:
                return _NEW_SYNTHESIS
            raise AssertionError(f"unexpected revise prompt: {prompt[:200]!r}")

        with patch.object(
            revise_mod, "_call_revise_llm", side_effect=_stub_revise_llm,
        ):
            revision_report = revise_mod.revise_day(FIXED_DATE, shadow=False)

        assert revision_report.applied == 2, revision_report.note

        final_payload = json.loads(issue_path.read_text(encoding="utf-8"))
        # digest_index 1 (the SAME index review reported and revise
        # resolved) carries the new lead -- and no OTHER bullet moved.
        assert final_payload["digest"][1]["lead"] == _NEW_LEAD
        assert final_payload["digest"][0]["lead"] != _NEW_LEAD
        assert final_payload["digest"][2]["lead"] != _NEW_LEAD
        assert (
            final_payload["sections"][0]["synthesis"] == _NEW_SYNTHESIS
        )
        # Both edits stale the verify verdicts attached under the edited
        # text (revise's own clearing rule) -- proving the routing landed
        # on the real primary/first story, not a placeholder.
        stories_by_id = {
            s["story_id"]: s
            for s in final_payload["pulse"]["stories"]
            + final_payload["sections"][0]["stories"]
            + final_payload["sections"][1]["stories"]
        }
        assert stories_by_id[_BP1_ID]["verification"] is None
