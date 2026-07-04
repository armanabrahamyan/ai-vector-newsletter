"""Unit tests for src/summarise.py -- targeted regression coverage.

Scope: the deterministic helpers in summarise.py that don't require a
live LLM. Currently:

- ``_url_dedup_key``: the Reddit-cross-post dedup contract. Two different
  subreddit URLs to the same article slug must collapse to one key.
- ``_pick_source_urls``: pins that the dedup actually drops the
  cross-post when building SummaryBlock.source_urls.

The LLM-driven paths (``_summarise_one``, ``_populate_section_intro``)
are left for the LLM Engineer's own tests + the eval harness; mocking
them here would be a tautology.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from src.models import (
    RUBRIC_WEIGHTS,
    Cluster,
    IssueSection,
    Item,
    RankedStory,
    SummaryBlock,
)


def _weighted_sum(breakdown: dict[str, int]) -> float:
    """Compute the weighted score from a breakdown using current RUBRIC_WEIGHTS.
    Tests use this instead of hardcoded multipliers so weight rebalances
    don't require fixture rewrites."""
    return sum(
        (RUBRIC_WEIGHTS[k] / 100.0) * v
        for k, v in breakdown.items()
    )
from src.summarise import (
    DEFAULT_CURRENTS_MAX_STORIES,
    DEFAULT_PER_SOURCE_PER_SECTION,
    EditorialConfig,
    PULSE_ELIGIBILITY_TRUST_FLOOR,
    _assemble_sections,
    _build_summary_prompt,
    _cluster_category,
    _compute_issue_shape,
    _pick_big_picture,
    _pick_currents,
    _pick_hands_on,
    _pick_pulse,
    _pick_source_urls,
    _pulse_eligibility,
    _reconcile_signal_with_audience_tags,
    _url_dedup_key,
)
from tests.conftest import FIXED_EARLIER, FIXED_NOW


# ===========================================================================
# _url_dedup_key -- the Reddit-cross-post dedup contract.
# ===========================================================================

class TestUrlDedupKey:
    """Reddit cross-posts of the same article live at different URLs (different
    subreddits, different comment ids) but share the same slug. The dedup
    key MUST collapse them so the rendered story doesn't show two [n] links
    pointing at the same discussion."""

    def test_two_subreddits_same_slug_collapse_to_one_key(self) -> None:
        a = "https://www.reddit.com/r/MachineLearning/comments/abc123/openai_releases_gpt_x/"
        b = "https://www.reddit.com/r/LocalLLaMA/comments/xyz789/openai_releases_gpt_x/"
        assert _url_dedup_key(a) == _url_dedup_key(b)

    def test_reddit_key_has_reddit_namespace(self) -> None:
        """Pin the format: `reddit::<slug>`. Other dedup keys (non-Reddit
        URLs) collide structurally with raw URLs only if they share strings,
        so the namespace prefix matters."""
        url = "https://www.reddit.com/r/LocalLLaMA/comments/xyz789/openai_releases_gpt_x/"
        assert _url_dedup_key(url) == "reddit::openai_releases_gpt_x"

    def test_slug_is_lowercased(self) -> None:
        """Slug case shouldn't break dedup -- pin canonicalisation."""
        a = "https://www.reddit.com/r/LocalLLaMA/comments/abc/OpenAI_GPT_X/"
        b = "https://www.reddit.com/r/LocalLLaMA/comments/abc/openai_gpt_x/"
        assert _url_dedup_key(a) == _url_dedup_key(b)

    @pytest.mark.parametrize("host_prefix", ["www.", "old.", "new.", ""])
    def test_subdomain_variants_dedup(self, host_prefix: str) -> None:
        """www / old / new / bare reddit.com variants all hit the same key."""
        url = f"https://{host_prefix}reddit.com/r/MachineLearning/comments/abc/post_slug/"
        assert _url_dedup_key(url) == "reddit::post_slug"

    def test_different_slugs_get_different_keys(self) -> None:
        """Two genuinely different Reddit posts must NOT collapse."""
        a = "https://www.reddit.com/r/MachineLearning/comments/abc/story_one/"
        b = "https://www.reddit.com/r/MachineLearning/comments/abc/story_two/"
        assert _url_dedup_key(a) != _url_dedup_key(b)

    def test_non_reddit_url_returns_raw_url(self) -> None:
        """For non-Reddit URLs the key IS the URL -- no namespacing, no
        canonicalisation."""
        url = "https://arxiv.org/abs/2401.12345"
        assert _url_dedup_key(url) == url

    def test_non_reddit_urls_not_collapsed_by_slug(self) -> None:
        """A non-Reddit URL that happens to contain a similar slug must
        not be confused with a Reddit URL."""
        url = "https://blog.example.com/openai_releases_gpt_x/"
        assert _url_dedup_key(url) == url
        assert _url_dedup_key(url) != "reddit::openai_releases_gpt_x"


# ===========================================================================
# _pick_source_urls -- end-to-end dedup at the SummaryBlock seam.
# ===========================================================================

def _item(url: str, *, source: str = "src_a", trust: int = 3,
          pub: _dt.datetime = FIXED_EARLIER) -> Item:
    return Item(
        id=f"id-{hash(url) & 0xffff:x}",
        source=source,
        source_type="rss",
        url=url,
        title="t",
        published_at=pub,
        raw_summary="",
        fetched_at=FIXED_NOW,
        trust_weight=trust,
    )


class TestPickSourceUrlsRedditDedup:
    def test_reddit_cross_posts_collapse_to_one_url(self) -> None:
        """The seam test: two Reddit items with the same slug from different
        subreddits resolve to ONE source_urls entry, not two."""
        items = [
            _item(
                "https://www.reddit.com/r/MachineLearning/comments/abc/launch_post/",
                source="reddit_ml",
            ),
            _item(
                "https://www.reddit.com/r/LocalLLaMA/comments/xyz/launch_post/",
                source="reddit_local",
            ),
        ]
        urls = _pick_source_urls(items, k=3)
        assert len(urls) == 1

    def test_higher_trust_subreddit_wins_when_cross_posted(self) -> None:
        """When Reddit cross-posts collide, the URL kept must come from the
        higher-trust subreddit (sort order makes this deterministic)."""
        low = _item(
            "https://www.reddit.com/r/LocalLLaMA/comments/aaa/post/",
            source="reddit_local", trust=2,
        )
        high = _item(
            "https://www.reddit.com/r/MachineLearning/comments/bbb/post/",
            source="reddit_ml", trust=5,
        )
        urls = _pick_source_urls([low, high], k=3)
        assert urls == [str(high.url)]

    def test_non_reddit_urls_not_affected(self) -> None:
        """The Reddit branch only fires for Reddit URLs; everything else
        flows through unchanged."""
        items = [
            _item("https://arxiv.org/abs/2401.12345", source="arxiv"),
            _item("https://openai.com/blog/post", source="openai_blog"),
            _item("https://example.com/article", source="example_blog"),
        ]
        urls = _pick_source_urls(items, k=3)
        assert len(urls) == 3


# ===========================================================================
# _pick_source_urls -- canonical-ID collapse (task #84).
#
# Background. Retrieval Engineer's tasks #80 + #83 added canonical-ID-aware
# clustering: rule A force-groups items sharing the same canonical ID
# (arxiv abs, GitHub release tag, DOI), rule B forbids items with distinct
# canonical IDs from merging via embeddings. Rule A means a cluster can
# legitimately end up with multiple source URLs that all point at the
# same paper from different feeds -- e.g. arxiv X cross-posted to HF
# Daily Papers AND linked in a Reddit thread. Both items have canonical
# ID "arxiv:<abs>". Without the second-pass collapse below, the rendered
# HTML shows two arxiv.org links side-by-side -- visually noisy and
# editorially misleading (looks like two sources, is really one).
#
# This block pins the second-pass behaviour: same canonical ID -> one URL
# (highest-trust wins), different canonical IDs -> both preserved, free-
# text URLs -> untouched, pure free-text cluster -> unchanged.
# ===========================================================================

class TestPickSourceUrlsCanonicalIdCollapse:
    def test_two_items_same_arxiv_id_collapse_to_one_url(self) -> None:
        """The headline case for #84: an arxiv paper cross-posted to HF
        Daily Papers AND linked in a Reddit thread. Both items resolve to
        ``arxiv:2605.12345``; source_urls must end with ONE entry."""
        items = [
            # Reddit thread that points at the arxiv paper (canonical URL
            # variant: the Reddit item's PRIMARY URL is the arxiv abs link
            # itself -- that's how RSS feeds that pull arxiv summaries
            # surface). In production the canonical extraction is body-aware
            # too, but at the _pick_source_urls seam we only see URLs.
            _item(
                "https://arxiv.org/abs/2605.12345",
                source="reddit_ml", trust=2,
            ),
            _item(
                "https://arxiv.org/abs/2605.12345v2",
                source="hf_daily_papers", trust=5,
            ),
        ]
        urls = _pick_source_urls(items, k=3)
        assert len(urls) == 1
        # Higher-trust source wins (hf_daily_papers, trust=5). The v2
        # suffix is fine -- the canonical ID strips the version so both
        # items match the same arxiv bucket.
        assert urls[0] == "https://arxiv.org/abs/2605.12345v2"

    def test_two_items_different_arxiv_ids_both_preserved(self) -> None:
        """Cluster rule B forbids embedding-merging of items with distinct
        canonical IDs; rule A's body-bridge case is the only way two
        DIFFERENT arxiv IDs end up in the same cluster (one item links
        the other in its body). When that happens, source_urls must keep
        BOTH -- they reference different papers."""
        items = [
            _item("https://arxiv.org/abs/2605.11111", source="arxiv", trust=5),
            _item("https://arxiv.org/abs/2605.22222", source="arxiv", trust=5),
        ]
        urls = _pick_source_urls(items, k=3)
        assert len(urls) == 2
        assert set(urls) == {
            "https://arxiv.org/abs/2605.11111",
            "https://arxiv.org/abs/2605.22222",
        }

    def test_arxiv_url_plus_free_text_blog_url_both_preserved(self) -> None:
        """A cluster mixing an arxiv item with a free-text blog post: the
        blog has canonical_id == None, so it is never collapsed against
        the arxiv URL. Both URLs survive."""
        items = [
            _item("https://arxiv.org/abs/2605.12345", source="arxiv", trust=5),
            _item("https://blog.example.com/our-take", source="example_blog", trust=4),
        ]
        urls = _pick_source_urls(items, k=3)
        assert len(urls) == 2
        assert "https://arxiv.org/abs/2605.12345" in urls
        assert "https://blog.example.com/our-take" in urls

    def test_pure_free_text_cluster_unchanged(self) -> None:
        """A cluster with zero canonical-ID URLs: the second pass is a
        no-op. All distinct URLs survive (subject to existing Reddit-slug
        and exact-URL dedup, which neither apply here)."""
        items = [
            _item("https://blog.example.com/post-one", source="blog_a", trust=5),
            _item("https://news.example.org/article", source="news_b", trust=4),
            _item("https://substack.example.net/issue", source="sub_c", trust=3),
        ]
        urls = _pick_source_urls(items, k=3)
        assert len(urls) == 3
        assert set(urls) == {
            "https://blog.example.com/post-one",
            "https://news.example.org/article",
            "https://substack.example.net/issue",
        }

    def test_github_release_cross_posts_collapse(self) -> None:
        """Same canonical-ID logic, GitHub release flavour: a release
        announcement linked from two different feeds collapses to one
        URL. Pins that the canonical-ID lookup isn't arxiv-only.

        URLs are chosen to differ as strings (trailing slash variant) so
        pass 1's exact-URL dedup is a no-op and pass 2 (canonical-ID)
        actually fires."""
        items = [
            _item(
                "https://github.com/ggerganov/llama.cpp/releases/tag/b9297",
                source="github_releases", trust=5,
            ),
            _item(
                "https://github.com/ggerganov/llama.cpp/releases/tag/b9297/",
                source="hn", trust=3,
            ),
        ]
        urls = _pick_source_urls(items, k=3)
        # Higher-trust github_releases wins; the trailing-slash HN variant
        # is dropped by the canonical-ID pass (both resolve to
        # github_release:ggerganov/llama.cpp:b9297).
        assert len(urls) == 1
        assert urls == [
            "https://github.com/ggerganov/llama.cpp/releases/tag/b9297"
        ]


# ===========================================================================
# _reconcile_signal_with_audience_tags -- FM-12 / regression #75 safety net.
#
# When the per-story summarise LLM tags a story signal="act" -- the editorial
# verdict pill defined as Big Picture territory ("vendor / contract /
# architecture decision worth making this quarter") -- but the rank LLM
# (lighter context: titles + raw_summary) missed the senior-leader angle
# and tagged hands_on-only, the cross-check augments audience_tags so the
# section router can place the story in Big Picture instead of evicting
# it to On the Radar. Anchor cluster: c_78dcc648119217a1 (2026-05-24,
# spec-driven development).
# ===========================================================================

def _ranked_story(
    cluster_id: str,
    audience_tags: list[str],
) -> RankedStory:
    # Weights (per config/rubric.yaml v0.2): 30/25/20/15/10.
    # 60*.3 + 60*.25 + 60*.2 + 25*.15 + 60*.1 = 18+15+12+3.75+6 = 54.75 -> 55.
    breakdown = {
        "significance": 60,
        "hands_on_utility": 60,
        "big_picture_relevance": 60,
        "financial_services_impact": 25,
        "freshness_momentum": 60,
    }
    return RankedStory(
        cluster_id=cluster_id,
        score=55,
        breakdown=breakdown,
        audience_tags=audience_tags,  # type: ignore[arg-type]
        rationale="test",
        tier="currents",
        prompt_version="v0.2",
    )


def _summary_block(cluster_id: str, signal: str | None) -> SummaryBlock:
    return SummaryBlock(
        story_id=cluster_id,
        headline="A headline that exists",
        summary="A body that exists for the seam test.",
        source_urls=["https://example.com/x"],  # type: ignore[list-item]
        signal=signal,  # type: ignore[arg-type]
    )


class TestReconcileSignalWithAudienceTags:
    """The body-grounded signal corrects rank's lighter-context undertags."""

    def test_signal_act_adds_big_picture_when_missing(self) -> None:
        """The smoking gun: hands_on-only rank tags + signal=act => add
        big_picture. Anchor for c_78dcc648119217a1-class miscalls."""
        story = _ranked_story("c_aaaaaaaaaaaaaaaa", ["hands_on", "general"])
        block = _summary_block("c_aaaaaaaaaaaaaaaa", "act")
        _reconcile_signal_with_audience_tags([(story, block)])
        assert "big_picture" in story.audience_tags
        # Original tags preserved -- we ADD, we don't overwrite.
        assert "hands_on" in story.audience_tags
        assert "general" in story.audience_tags

    def test_signal_act_is_noop_when_already_tagged_big_picture(self) -> None:
        """Idempotent: if rank already tagged big_picture, no change."""
        story = _ranked_story(
            "c_bbbbbbbbbbbbbbbb", ["hands_on", "big_picture"],
        )
        block = _summary_block("c_bbbbbbbbbbbbbbbb", "act")
        before = list(story.audience_tags)
        _reconcile_signal_with_audience_tags([(story, block)])
        assert list(story.audience_tags) == before

    def test_non_act_signals_leave_tags_alone(self) -> None:
        """Only signal=act fires the rule. try/read/watch/discuss never
        force big_picture -- those pills don't carry the same editorial
        weight (per the signal definitions in the summarise prompt)."""
        for signal in ("try", "read", "watch", "discuss", None):
            story = _ranked_story(
                f"c_cccccccccccccc{abs(hash(str(signal))) % 100:02d}",
                ["hands_on"],
            )
            block = _summary_block(story.cluster_id, signal)
            _reconcile_signal_with_audience_tags([(story, block)])
            assert "big_picture" not in story.audience_tags, (
                f"signal={signal!r} should not force big_picture"
            )

    def test_multiple_blocks_independent(self) -> None:
        """Each block is reconciled independently; one bad apple doesn't
        bleed into another."""
        s1 = _ranked_story("c_dddddddddddddd01", ["hands_on"])
        s2 = _ranked_story("c_dddddddddddddd02", ["hands_on"])
        b1 = _summary_block(s1.cluster_id, "act")
        b2 = _summary_block(s2.cluster_id, "watch")
        _reconcile_signal_with_audience_tags([(s1, b1), (s2, b2)])
        assert "big_picture" in s1.audience_tags
        assert "big_picture" not in s2.audience_tags


# ===========================================================================
# _pick_pulse -- prior-coverage bias (task #82).
#
# A prior-coverage story (SummaryBlock.prior_coverage_ref is not null) is a
# topical recurrence of something we covered on a previous day. The Pulse
# is meant to be the day's freshest editorial anchor; leading with a
# recurrence tells the reader "we have nothing new today." The selection
# rule must prefer any FRESH story over any prior-coverage story,
# regardless of score.
#
# Anchor: 2026-05-25 -- c_2e53967d020fb800 (llama.cpp how-to follow-up,
# score 44, prior_coverage_ref set) was selected as Pulse over the fresh
# Hugging Face benchmark tracker (score 39, prior_coverage_ref None).
# ===========================================================================

def _ranked(cluster_id: str, score: int, *,
            significance: int = 60, freshness: int = 60,
            hands_on: int = 60) -> RankedStory:
    # Choose a breakdown that gives the requested score under the rubric
    # weights (30/25/20/15/10). For simplicity we set the named axes and
    # solve big_picture_relevance + financial_services_impact = constants
    # that make the math work for the chosen score. The score validator
    # in RankedStory rejects mismatched score; we recompute exactly.
    big_picture = 50
    fs = 25
    # weighted sum via current RUBRIC_WEIGHTS (auto-stays-in-sync)
    weighted = _weighted_sum({
        "significance": significance,
        "hands_on_utility": hands_on,
        "big_picture_relevance": big_picture,
        "financial_services_impact": fs,
        "freshness_momentum": freshness,
    })
    breakdown = {
        "significance": significance,
        "hands_on_utility": hands_on,
        "big_picture_relevance": big_picture,
        "financial_services_impact": fs,
        "freshness_momentum": freshness,
    }
    return RankedStory(
        cluster_id=cluster_id,
        score=round(weighted),
        breakdown=breakdown,
        audience_tags=["hands_on"],
        rationale="t",
        tier="currents",
        prompt_version="v0.1",
    )


def _block(cluster_id: str, *, prior_coverage_ref: str | None) -> SummaryBlock:
    return SummaryBlock(
        story_id=cluster_id,
        headline="A headline that exists for the seam test",
        summary="A body that exists for the seam test of pulse selection.",
        source_urls=["https://example.com/x"],  # type: ignore[list-item]
        prior_coverage_ref=prior_coverage_ref,
    )


class TestPulseSelectionPriorCoverageBias:
    """The Pulse selection rule (v0.3, #82): prefer fresh (no prior
    coverage) stories regardless of score; only fall back to a
    prior-coverage story when there are no fresh stories left."""

    def test_fresh_low_score_beats_prior_coverage_high_score(self) -> None:
        """The smoking gun. Prior-coverage story scored higher; fresh story
        must still win. Anchor: c_2e53967d020fb800 (score 44, prior
        coverage) vs. c_78dabe7884f76ef8 (score 39, fresh) on 2026-05-25."""
        # NB: blocks arrive in score-desc order (caller maintains that).
        recur = (
            _ranked("c_eeeeeeeeeeee0001", score=53, significance=80, freshness=40),
            _block("c_eeeeeeeeeeee0001", prior_coverage_ref="c_ffffffffffff0001"),
        )
        fresh = (
            _ranked("c_eeeeeeeeeeee0002", score=46, significance=60, freshness=40),
            _block("c_eeeeeeeeeeee0002", prior_coverage_ref=None),
        )
        pulse_id = _pick_pulse([recur, fresh])
        assert pulse_id == "c_eeeeeeeeeeee0002"

    def test_prior_coverage_used_when_no_fresh_survivors(self) -> None:
        """Degraded mode: every surviving story has prior coverage. Pulse
        must still get filled (Issue.pulse mandates exactly 1 block); we
        ship with a warning log rather than crash."""
        c1 = (
            _ranked("c_eeeeeeeeeeee0010", score=53, significance=80),
            _block("c_eeeeeeeeeeee0010", prior_coverage_ref="c_ffffffffffff0001"),
        )
        c2 = (
            _ranked("c_eeeeeeeeeeee0011", score=46, significance=60),
            _block("c_eeeeeeeeeeee0011", prior_coverage_ref="c_ffffffffffff0002"),
        )
        pulse_id = _pick_pulse([c1, c2])
        # Best of the prior-coverage pool -- highest score, both >= 2 signal
        # dimensions (significance 80 + hands_on 60 + freshness 60 hits 2
        # axes for c1; we pick c1 because it's first in score-desc order).
        assert pulse_id == "c_eeeeeeeeeeee0010"

    def test_fresh_pool_pulse_class_quality_bar_applies(self) -> None:
        """Within the FRESH pool, the existing >= 2 signal-dimensions quality
        bar still applies. A fresh story that hits the bar beats a fresh
        story that doesn't, even if the latter is higher-scored."""
        # Story A: fresh, higher score, hits 1 signal dim (significance only).
        a = (
            _ranked("c_eeeeeeeeeeee0020", score=51, significance=85,
                    hands_on=40, freshness=40),
            _block("c_eeeeeeeeeeee0020", prior_coverage_ref=None),
        )
        # Story B: fresh, lower score, hits 3 signal dims (>= 70 on all).
        b = (
            _ranked("c_eeeeeeeeeeee0021", score=50, significance=70,
                    hands_on=70, freshness=70),
            _block("c_eeeeeeeeeeee0021", prior_coverage_ref=None),
        )
        pulse_id = _pick_pulse([a, b])
        # B wins on the Pulse-class quality bar; both are fresh so the
        # prior-coverage rule doesn't help A here.
        assert pulse_id == "c_eeeeeeeeeeee0021"

    def test_returns_none_on_empty_input(self) -> None:
        """Defensive: empty blocks list returns None. The caller raises
        RuntimeError on that signal (see summarise()'s pulse_id is None
        branch)."""
        assert _pick_pulse([]) is None

    def test_all_fresh_picks_highest_pulse_class(self) -> None:
        """The non-degraded happy path: all stories fresh, the rule reduces
        to the pre-#82 behaviour -- top-of-list Pulse-class story wins."""
        a = (
            _ranked("c_eeeeeeeeeeee0030", score=60, significance=80,
                    hands_on=75, freshness=75),
            _block("c_eeeeeeeeeeee0030", prior_coverage_ref=None),
        )
        b = (
            _ranked("c_eeeeeeeeeeee0031", score=55, significance=70,
                    hands_on=70, freshness=70),
            _block("c_eeeeeeeeeeee0031", prior_coverage_ref=None),
        )
        pulse_id = _pick_pulse([a, b])
        assert pulse_id == "c_eeeeeeeeeeee0030"


# ===========================================================================
# _pick_pulse -- sourcing-credibility eligibility gate (v0.10 / 2026-05-26).
#
# Fixes the May 26, 2026 PII-scrubber regression: a singleton cluster with
# one trust=2 Reddit source (no repo, no canonical artefact) was promoted
# to Pulse via the score fallback path because the picker had no notion
# of *eligibility*. The eligibility gate sits in front of the existing
# fresh/recurring partition and >=2 signal-dimension Pulse-class check.
#
# Eligibility requires AT LEAST ONE of:
#   1. cluster.size > 1                (multi-source corroboration)
#   2. cluster.canonical_id is not None (verifiable artefact)
#   3. max trust_weight in cluster >= PULSE_ELIGIBILITY_TRUST_FLOOR (=3)
#
# If zero candidates pass, fall back to current behaviour with WARNING.
# ===========================================================================

def _cluster(
    cluster_id: str,
    *,
    size: int = 1,
    canonical_id: str | None = None,
    item_ids: list[str] | None = None,
    sources: list[str] | None = None,
) -> Cluster:
    """Build a minimal Cluster for eligibility tests. Defaults to a
    thin-sourced singleton (the May 26 PII pattern). Override size /
    canonical_id / item_ids / sources for the must-pass cases."""
    if item_ids is None:
        # Synthesise size distinct item ids of the right cardinality.
        item_ids = [f"item_{cluster_id[2:6]}_{i:02d}" for i in range(size)]
    if sources is None:
        sources = ["r/LocalLLaMA (Reddit)"]
    return Cluster(
        cluster_id=cluster_id,
        item_ids=item_ids,
        canonical_title="A canonical title that exists",
        sources=sources,
        earliest_published=FIXED_EARLIER,
        size=len(item_ids),
        prior_coverage_ref=None,
        canonical_id=canonical_id,
    )


def _items_by_id(
    item_ids: list[str],
    *,
    trust: int = 2,
    source: str = "r/LocalLLaMA (Reddit)",
) -> dict[str, Item]:
    """Build {item_id: Item} for the eligibility gate's trust_weight lookup.
    Each item shares the same trust + source unless tests override."""
    return {
        iid: Item(
            id=iid,
            source=source,
            source_type="rss",
            url=f"https://example.com/{iid}",  # type: ignore[arg-type]
            title=f"t-{iid}",
            published_at=FIXED_EARLIER,
            raw_summary="",
            fetched_at=FIXED_NOW,
            trust_weight=trust,
        )
        for iid in item_ids
    }


class TestPulseEligibilityGate:
    """The sourcing-credibility gate that sits in front of _pick_pulse."""

    def test_filters_singleton_low_trust(self, caplog) -> None:
        """The May 26 smoking gun. PII-scrubber cluster pattern: size=1,
        canonical_id=None, trust=2 Reddit source. Higher-scored but
        sourcing-thin story must be demoted in favour of an eligible
        lower-scored story. INFO log records the demotion."""
        # Ineligible top story (May 26 PII pattern): singleton, no canonical,
        # trust=2 only. Higher significance + higher score than the
        # eligible story below.
        thin_cluster = _cluster("c_eeeeeeeeeeee0040", size=1)
        thin = (
            _ranked("c_eeeeeeeeeeee0040", score=56, significance=65,
                    hands_on=72, freshness=50),
            _block("c_eeeeeeeeeeee0040", prior_coverage_ref=None),
        )
        # Eligible candidate: multi-source (size=2). Lower score.
        multi_cluster = _cluster(
            "c_eeeeeeeeeeee0041", size=2,
            sources=["github_releases", "r/LocalLLaMA (Reddit)"],
        )
        multi = (
            _ranked("c_eeeeeeeeeeee0041", score=45, significance=50,
                    hands_on=40, freshness=50),
            _block("c_eeeeeeeeeeee0041", prior_coverage_ref=None),
        )
        clusters_by_id = {
            thin_cluster.cluster_id: thin_cluster,
            multi_cluster.cluster_id: multi_cluster,
        }
        items_by_id = {
            **_items_by_id(thin_cluster.item_ids, trust=2),
            **_items_by_id(multi_cluster.item_ids, trust=2),
        }
        import logging
        with caplog.at_level(logging.INFO, logger="ai_vector.summarise"):
            pulse_id = _pick_pulse(
                [thin, multi],
                clusters_by_id=clusters_by_id,
                items_by_id=items_by_id,
            )
        assert pulse_id == "c_eeeeeeeeeeee0041"
        # The thin story was logged as filtered AND the demotion was logged.
        assert any("eligibility gate filtered" in r.message
                   for r in caplog.records)

    def test_passes_multi_source(self) -> None:
        """Pulse-eligible when size > 1 (multi-source corroboration)."""
        cluster = _cluster(
            "c_eeeeeeeeeeee0050", size=2,
            sources=["github_releases", "r/LocalLLaMA (Reddit)"],
        )
        eligible, reason = _pulse_eligibility(
            cluster, _items_by_id(cluster.item_ids, trust=2),
        )
        assert eligible is True
        assert "size=2" in reason

    def test_passes_canonical_id(self) -> None:
        """Pulse-eligible when canonical_id is set (verifiable artefact)
        even if singleton and trust=2."""
        cluster = _cluster(
            "c_eeeeeeeeeeee0060", size=1,
            canonical_id="arxiv:2605.12345",
        )
        eligible, reason = _pulse_eligibility(
            cluster, _items_by_id(cluster.item_ids, trust=2),
        )
        assert eligible is True
        assert "canonical_id=present" in reason

    def test_passes_established_source(self) -> None:
        """Pulse-eligible when max trust_weight >= floor (3), even if
        singleton with no canonical_id. Models the OpenAI/Anthropic/EU AI
        Act Newsletter case."""
        cluster = _cluster(
            "c_eeeeeeeeeeee0070", size=1,
            sources=["The EU AI Act Newsletter"],
        )
        items = _items_by_id(
            cluster.item_ids, trust=PULSE_ELIGIBILITY_TRUST_FLOOR,
            source="The EU AI Act Newsletter",
        )
        eligible, reason = _pulse_eligibility(cluster, items)
        assert eligible is True
        assert f"trust_max={PULSE_ELIGIBILITY_TRUST_FLOOR}" in reason

    def test_filters_singleton_low_trust_no_canonical(self) -> None:
        """The exact PII pattern: size=1, canonical_id=None,
        all sources at trust=2. Ineligible. Reason string carries all three
        fields for operator clarity."""
        cluster = _cluster("c_eeeeeeeeeeee0080", size=1)
        eligible, reason = _pulse_eligibility(
            cluster, _items_by_id(cluster.item_ids, trust=2),
        )
        assert eligible is False
        assert "size=1" in reason
        assert "canonical_id=none" in reason
        assert "trust_max=2" in reason

    def test_degraded_mode_fallback_when_no_eligible(self, caplog) -> None:
        """All candidates ineligible: gate falls back to unfiltered set
        with a WARNING; highest-scoring story is chosen anyway so the
        issue still ships. The warning is visible at ratification."""
        a_cluster = _cluster("c_eeeeeeeeeeee0090", size=1)
        b_cluster = _cluster("c_eeeeeeeeeeee0091", size=1)
        a = (
            _ranked("c_eeeeeeeeeeee0090", score=56, significance=65,
                    hands_on=72, freshness=50),
            _block("c_eeeeeeeeeeee0090", prior_coverage_ref=None),
        )
        b = (
            _ranked("c_eeeeeeeeeeee0091", score=45, significance=50,
                    hands_on=40, freshness=50),
            _block("c_eeeeeeeeeeee0091", prior_coverage_ref=None),
        )
        clusters_by_id = {
            a_cluster.cluster_id: a_cluster,
            b_cluster.cluster_id: b_cluster,
        }
        items_by_id = {
            **_items_by_id(a_cluster.item_ids, trust=2),
            **_items_by_id(b_cluster.item_ids, trust=2),
        }
        import logging
        with caplog.at_level(logging.WARNING, logger="ai_vector.summarise"):
            pulse_id = _pick_pulse(
                [a, b],
                clusters_by_id=clusters_by_id,
                items_by_id=items_by_id,
            )
        # Fallback runs: top-scored ineligible story is picked.
        assert pulse_id == "c_eeeeeeeeeeee0090"
        # WARNING was logged.
        assert any("PULSE ELIGIBILITY GATE FOUND NO ELIGIBLE CANDIDATES"
                   in r.message for r in caplog.records)

    def test_does_not_promote_ineligible_over_eligible(self) -> None:
        """When an ineligible story has higher significance AND score, the
        eligible story still wins. This is the core demotion rule the
        gate enforces."""
        ineligible_cluster = _cluster("c_eeeeeeeeeeee00a0", size=1)
        eligible_cluster = _cluster(
            "c_eeeeeeeeeeee00a1", size=1,
            canonical_id="github_release:org/repo:v1.0",
        )
        ineligible = (
            _ranked("c_eeeeeeeeeeee00a0", score=70, significance=95,
                    hands_on=80, freshness=70),
            _block("c_eeeeeeeeeeee00a0", prior_coverage_ref=None),
        )
        eligible = (
            _ranked("c_eeeeeeeeeeee00a1", score=40, significance=40,
                    hands_on=40, freshness=40),
            _block("c_eeeeeeeeeeee00a1", prior_coverage_ref=None),
        )
        clusters_by_id = {
            ineligible_cluster.cluster_id: ineligible_cluster,
            eligible_cluster.cluster_id: eligible_cluster,
        }
        items_by_id = {
            **_items_by_id(ineligible_cluster.item_ids, trust=2),
            **_items_by_id(eligible_cluster.item_ids, trust=2),
        }
        pulse_id = _pick_pulse(
            [ineligible, eligible],
            clusters_by_id=clusters_by_id,
            items_by_id=items_by_id,
        )
        assert pulse_id == "c_eeeeeeeeeeee00a1"

    def test_deterministic_across_runs(self) -> None:
        """Same input -> same Pulse pick. The gate is pure-deterministic
        (no LLM, no randomness); we pin that explicitly."""
        thin_cluster = _cluster("c_eeeeeeeeeeee00b0", size=1)
        multi_cluster = _cluster(
            "c_eeeeeeeeeeee00b1", size=2,
            sources=["github_releases", "r/LocalLLaMA (Reddit)"],
        )
        thin = (
            _ranked("c_eeeeeeeeeeee00b0", score=56, significance=65),
            _block("c_eeeeeeeeeeee00b0", prior_coverage_ref=None),
        )
        multi = (
            _ranked("c_eeeeeeeeeeee00b1", score=45, significance=50),
            _block("c_eeeeeeeeeeee00b1", prior_coverage_ref=None),
        )
        clusters_by_id = {
            thin_cluster.cluster_id: thin_cluster,
            multi_cluster.cluster_id: multi_cluster,
        }
        items_by_id = {
            **_items_by_id(thin_cluster.item_ids, trust=2),
            **_items_by_id(multi_cluster.item_ids, trust=2),
        }
        ids = {
            _pick_pulse(
                [thin, multi],
                clusters_by_id=clusters_by_id,
                items_by_id=items_by_id,
            )
            for _ in range(5)
        }
        assert ids == {"c_eeeeeeeeeeee00b1"}

    def test_eligibility_gate_with_missing_clusters_is_safe(self) -> None:
        """If clusters_by_id is None (back-compat path used only by
        narrow unit tests that don't exercise the gate), the gate
        degrades to the all-ineligible fallback rather than crashing.
        Existing behaviour preserved."""
        # No clusters_by_id, no items_by_id provided. The original two
        # blocks-only signature still works (eligibility check returns
        # False for every cluster, fallback kicks in).
        a = (
            _ranked("c_eeeeeeeeeeee00c0", score=60, significance=80,
                    hands_on=75, freshness=75),
            _block("c_eeeeeeeeeeee00c0", prior_coverage_ref=None),
        )
        b = (
            _ranked("c_eeeeeeeeeeee00c1", score=55, significance=70,
                    hands_on=70, freshness=70),
            _block("c_eeeeeeeeeeee00c1", prior_coverage_ref=None),
        )
        pulse_id = _pick_pulse([a, b])
        # All ineligible -> fallback -> top Pulse-class story wins.
        assert pulse_id == "c_eeeeeeeeeeee00c0"


# ===========================================================================
# Source-diversity caps (2026-05-27).
#
# Two-layer post-rank rule, fixes May 27 single-category dominance pattern
# (9 of 12 stories from papers because arxiv cs.CL alone supplied 252 of 424
# fetched items + recent rubric rebalance favoured paper-shaped content).
#
# Layer 1: per_source_per_section (default 2, baked in code).
# Layer 2: per_category_per_issue (config-driven; AI Vector caps papers=4).
#
# Degraded mode: if caps starve Hands-On below the minimum-of-3 integrity
# gate, the picker logs WARNING and fills from over-cap candidates.
# ===========================================================================

def _cluster_with_source(
    cluster_id: str, source: str, *, item_count: int = 1,
) -> Cluster:
    """Minimal Cluster keyed to a single source name for cap tests. Source-
    name is the cap key for Layer 1; cluster_id is the routing handle.
    """
    item_ids = [f"item_{cluster_id[2:6]}_{i:02d}" for i in range(item_count)]
    return Cluster(
        cluster_id=cluster_id,
        item_ids=item_ids,
        canonical_title="A canonical title",
        sources=[source],
        earliest_published=FIXED_EARLIER,
        size=item_count,
        prior_coverage_ref=None,
        canonical_id=None,
    )


def _ranked_tagged(
    cluster_id: str, *, score: int, tags: list[str], hands_on_score: int = 75,
    tier: str | None = None,
) -> RankedStory:
    """A ranked story with chosen audience tags. Score chosen against the
    rubric weights so RankedStory validates.

    Schema v3 (2026-05-30): ``tier`` defaults to one derived from ``tags``
    (big_picture / hands_on / currents) so callers asking for
    ``tags=["big_picture"]`` see a story that the picker will accept under
    the tier-pool gate. Explicit ``tier=`` overrides -- callers that want
    to test the gate REJECTING a story (e.g. wrong tier) pass it directly.
    """
    big_picture = 50
    fs = 25
    significance = 60
    freshness = 60
    weighted = _weighted_sum({
        "significance": significance,
        "hands_on_utility": hands_on_score,
        "big_picture_relevance": big_picture,
        "financial_services_impact": fs,
        "freshness_momentum": freshness,
    })
    breakdown = {
        "significance": significance,
        "hands_on_utility": hands_on_score,
        "big_picture_relevance": big_picture,
        "financial_services_impact": fs,
        "freshness_momentum": freshness,
    }
    if tier is None:
        if "big_picture" in tags:
            tier = "big_picture"
        elif "hands_on" in tags:
            tier = "hands_on"
        else:
            tier = "currents"
    return RankedStory(
        cluster_id=cluster_id,
        score=round(weighted),
        breakdown=breakdown,
        audience_tags=tags,  # type: ignore[arg-type]
        rationale="t",
        tier=tier,  # type: ignore[arg-type]
        prompt_version="v0.2",
    )


def _summary_for(cluster_id: str) -> SummaryBlock:
    return SummaryBlock(
        story_id=cluster_id,
        headline="A headline that exists for the seam test",
        summary="A body that exists for the seam test of cap logic.",
        source_urls=["https://example.com/x"],  # type: ignore[list-item]
        prior_coverage_ref=None,
    )


def _papers_cfg(
    *, per_source: int = 2, papers_cap: int = 4,
) -> EditorialConfig:
    """An EditorialConfig pinned for the AI Vector editorial intent (papers
    capped at 4 per issue, per-source-per-section default 2). Source map
    minimal: just the few names the tests reference."""
    return EditorialConfig(
        per_source_per_section=per_source,
        per_category_per_issue={"papers": papers_cap},
        source_to_category={
            "arXiv cs.CL": "papers",
            "Hugging Face Daily Papers": "papers",
            "Simon Willison's Blog": "newsletter",
            "Ars Technica AI": "news",
            "r/LocalLLaMA (Reddit)": "community",
            "OpenAI": "lab",
            "Anthropic": "lab",
        },
        source_to_trust={
            "arXiv cs.CL": 1,
            "Hugging Face Daily Papers": 4,
            "Simon Willison's Blog": 4,
            "Ars Technica AI": 3,
            "r/LocalLLaMA (Reddit)": 2,
            "OpenAI": 3,
            "Anthropic": 3,
        },
    )


class TestSourceCapPerSection:
    """Layer 1: no single section may carry more than N stories from the
    same source name."""

    def test_per_section_cap_filters_excess(self) -> None:
        """Three candidates from the same source name + cap=2: only the
        first two land in the section; the third is skipped."""
        from collections import Counter
        cfg = _papers_cfg(per_source=2)
        # All three from the same source name -- Layer 1 binds.
        ids = [f"c_aaaaaaaaaaaa00{i:02x}" for i in (1, 2, 3)]
        blocks = [
            (_ranked_tagged(cid, score=55, tags=["big_picture"]), _summary_for(cid))
            for cid in ids
        ]
        clusters = {cid: _cluster_with_source(cid, "arXiv cs.CL") for cid in ids}
        available = set(ids)
        categories_used: Counter[str] = Counter()
        picked = _pick_big_picture(
            blocks, available,
            clusters_by_id=clusters,
            cfg=cfg,
            categories_used_this_issue=categories_used,
        )
        assert picked == ids[:2]
        # And the category counter recorded 2 papers consumed by this section.
        assert categories_used["papers"] == 2

    def test_no_caps_configured_default_per_source_cap_still_applies(self) -> None:
        """Forker case: empty editorial config (cfg with default cap=2 and
        empty per_category_per_issue). Per-source cap still binds; no
        category cap fires."""
        from collections import Counter
        # The defaults a forker who hasn't created editorial.yaml gets.
        cfg = EditorialConfig(
            per_source_per_section=DEFAULT_PER_SOURCE_PER_SECTION,
            per_category_per_issue={},
            source_to_category={},
            source_to_trust={},
        )
        ids = [f"c_bbbbbbbbbbbb00{i:02x}" for i in (1, 2, 3)]
        blocks = [
            (_ranked_tagged(cid, score=55, tags=["big_picture"]), _summary_for(cid))
            for cid in ids
        ]
        clusters = {cid: _cluster_with_source(cid, "some_source") for cid in ids}
        available = set(ids)
        categories_used: Counter[str] = Counter()
        picked = _pick_big_picture(
            blocks, available,
            clusters_by_id=clusters, cfg=cfg,
            categories_used_this_issue=categories_used,
        )
        # Per-source cap (default 2) binds even without a config file.
        assert picked == ids[:2]
        # No category cap fires (unknown category, no entry in cap dict).
        # All categories ended up in "unknown".
        assert categories_used["unknown"] == 2


class TestCategoryCapPerIssue:
    """Layer 2: across the whole issue, no more than M stories of any one
    category. Counter is threaded from Pulse through every picker."""

    def test_category_cap_filters_across_sections(self) -> None:
        """Six paper candidates across big_picture + hands_on, cap=4. Only
        4 papers land in the whole issue; the remaining 2 are dropped.

        Schema v3 (2026-05-30): Hands-On's degraded-mode Pass 2 is gone,
        so the per-issue category cap is now a hard ceiling -- Hands-On
        cannot scavenge past it to hit a minimum. The shape post-condition
        surfaces the under-fill as amber instead.
        """
        from collections import Counter
        cfg = _papers_cfg(per_source=10, papers_cap=4)  # high per-source so only category binds
        # Three big_picture papers + three hands_on papers; cap=4.
        bp_ids = [f"c_cccccccccccc00{i:02x}" for i in (1, 2, 3)]
        ho_ids = [f"c_cccccccccccc01{i:02x}" for i in (1, 2, 3)]
        all_ids = bp_ids + ho_ids
        # Each from a distinct paper-source so Layer 1 doesn't bind.
        source_pool = [
            "arXiv cs.CL", "Hugging Face Daily Papers", "arXiv cs.CL",
            "Hugging Face Daily Papers", "arXiv cs.CL", "Hugging Face Daily Papers",
        ]
        clusters = {
            cid: _cluster_with_source(cid, source_pool[i])
            for i, cid in enumerate(all_ids)
        }
        bp_blocks = [
            (_ranked_tagged(cid, score=55, tags=["big_picture"]), _summary_for(cid))
            for cid in bp_ids
        ]
        ho_blocks = [
            (_ranked_tagged(cid, score=55, tags=["hands_on"], hands_on_score=80),
             _summary_for(cid))
            for cid in ho_ids
        ]
        blocks = bp_blocks + ho_blocks
        available = set(all_ids)
        categories_used: Counter[str] = Counter()
        bp_picked = _pick_big_picture(
            blocks, available, clusters_by_id=clusters, cfg=cfg,
            categories_used_this_issue=categories_used,
        )
        for cid in bp_picked:
            available.discard(cid)
        ho_picked = _pick_hands_on(
            blocks, available, clusters_by_id=clusters, cfg=cfg,
            categories_used_this_issue=categories_used,
        )
        # Big Picture takes 3 (its hard cap is 4, all 3 are eligible).
        # Hands-On then sees 1 paper slot left in the issue-wide cap and
        # accepts exactly one -- no degraded-mode scavenge under v3.
        assert len(bp_picked) == 3
        assert len(ho_picked) == 1
        # 3 (bp) + 1 (ho) = 4 papers consumed; cap was the gate.
        assert categories_used["papers"] == 4

    def test_pulse_category_counts_toward_per_category_cap(self) -> None:
        """When the Pulse is itself a paper, the per-issue category counter
        sees it before any other picker runs. Subsequent picks of papers
        come under a smaller remaining budget."""
        from collections import Counter
        cfg = _papers_cfg(per_source=10, papers_cap=4)
        # Five candidates all from papers; cap=4.
        ids = [f"c_dddddddddddd00{i:02x}" for i in (1, 2, 3, 4, 5)]
        clusters = {
            cid: _cluster_with_source(cid, "arXiv cs.CL")
            for cid in ids
        }
        blocks = [
            (_ranked_tagged(cid, score=55, tags=["big_picture"]), _summary_for(cid))
            for cid in ids
        ]
        # Simulate Pulse already accepted a paper (counter pre-incremented).
        categories_used: Counter[str] = Counter({"papers": 1})
        available = set(ids)
        picked = _pick_big_picture(
            blocks, available, clusters_by_id=clusters, cfg=cfg,
            categories_used_this_issue=categories_used,
        )
        # 4 cap - 1 already used by Pulse = 3 slots left. But per_source
        # cap=10 ensures Layer 1 doesn't bind. Layer 2 limits Big Picture
        # acceptance to 3 papers (4 - 1 already used).
        assert len(picked) == 3
        assert categories_used["papers"] == 4


class TestCapHardCeiling:
    """Schema v3 (2026-05-30): Hands-On's degraded-mode Pass 2 is GONE.
    Source-diversity caps are now a hard ceiling -- the picker does not
    scavenge past the per-issue category cap to chase a minimum. The
    under-fill is surfaced by the shape post-condition (Issue.notes), not
    masked by quietly relaxing the cap."""

    def test_caps_starve_hands_on_no_degraded_mode_no_warning(
        self, caplog,
    ) -> None:
        """Cap=4 papers; Pulse + Big Picture have already consumed 4.
        Hands-On then sees 5 paper candidates with cap exhausted. The
        picker accepts ZERO (cap is binding) and emits NO warning about
        relaxing caps. Under-fill is the shape post-condition's job."""
        from collections import Counter
        import logging as _logging
        cfg = _papers_cfg(per_source=10, papers_cap=4)
        ho_ids = [f"c_eeeeeeeeeeee20{i:02x}" for i in (1, 2, 3, 4, 5)]
        clusters = {
            cid: _cluster_with_source(cid, "arXiv cs.CL")
            for cid in ho_ids
        }
        blocks = [
            (_ranked_tagged(cid, score=55, tags=["hands_on"], hands_on_score=80),
             _summary_for(cid))
            for cid in ho_ids
        ]
        available = set(ho_ids)
        # Pre-fill the per-issue counter as if Pulse + Big Picture used 4.
        categories_used: Counter[str] = Counter({"papers": 4})
        with caplog.at_level(_logging.WARNING, logger="ai_vector.summarise"):
            picked = _pick_hands_on(
                blocks, available, clusters_by_id=clusters, cfg=cfg,
                categories_used_this_issue=categories_used,
            )
        # Cap is the hard ceiling: zero accepted.
        assert picked == []
        # No degraded-mode warning fires -- the old Pass 2 is gone.
        assert not any("SOURCE-DIVERSITY CAPS STARVED HANDS-ON" in r.message
                       for r in caplog.records)
        # Category counter is unchanged from the pre-fill (cap held).
        assert categories_used["papers"] == 4


class TestUnknownCategoryUncapped:
    """A cluster whose highest-trust source has no category in sources.yaml
    resolves to ``"unknown"`` and is treated as uncapped by Layer 2."""

    def test_unknown_category_is_uncapped(self) -> None:
        """Five candidates from a source not in source_to_category; their
        category resolves to 'unknown'. Layer 2 has no 'unknown' cap, so
        no filter fires. (Layer 1's per-source cap still binds when the
        SAME source is repeated; this test uses distinct sources to
        isolate Layer 2.)"""
        from collections import Counter
        cfg = EditorialConfig(
            per_source_per_section=10,  # high so Layer 1 doesn't bind
            per_category_per_issue={"papers": 4},  # 'unknown' not in cap dict
            source_to_category={},  # empty -> every source unknown
            source_to_trust={},
        )
        ids = [f"c_ffffffffffff10{i:02x}" for i in (1, 2, 3, 4, 5)]
        clusters = {
            cid: _cluster_with_source(cid, f"unknown_source_{i:02x}")
            for i, cid in enumerate(ids, start=1)
        }
        blocks = [
            (_ranked_tagged(cid, score=55, tags=["big_picture"]), _summary_for(cid))
            for cid in ids
        ]
        available = set(ids)
        categories_used: Counter[str] = Counter()
        picked = _pick_big_picture(
            blocks, available, clusters_by_id=clusters, cfg=cfg,
            categories_used_this_issue=categories_used,
        )
        # Hard cap on big_picture is 4 (independent of source-diversity caps).
        # Five candidates, no Layer 1 or Layer 2 firing => 4 accepted.
        assert len(picked) == 4
        # All counted as 'unknown' -- uncapped.
        assert categories_used["unknown"] == 4


class TestClusterCategoryResolution:
    """The ``_cluster_category`` helper: pick the category of the
    highest-trust source; tie-break by source name asc."""

    def test_picks_highest_trust_source_category(self) -> None:
        cfg = EditorialConfig(
            source_to_category={"low_trust": "community", "high_trust": "lab"},
            source_to_trust={"low_trust": 1, "high_trust": 5},
        )
        cluster = Cluster(
            cluster_id="c_aaaaaaaaaaaa1000",
            item_ids=["i_01", "i_02"],
            canonical_title="t",
            sources=["low_trust", "high_trust"],
            earliest_published=FIXED_EARLIER,
            size=2,
        )
        assert _cluster_category(cluster, cfg) == "lab"

    def test_tie_break_by_source_name_ascending(self) -> None:
        cfg = EditorialConfig(
            source_to_category={"zsource": "lab", "asource": "papers"},
            source_to_trust={"zsource": 3, "asource": 3},
        )
        cluster = Cluster(
            cluster_id="c_aaaaaaaaaaaa1001",
            item_ids=["i_03"],
            canonical_title="t",
            sources=["zsource", "asource"],
            earliest_published=FIXED_EARLIER,
            size=1,
        )
        # asource < zsource ascending, trust equal -> asource wins -> papers.
        assert _cluster_category(cluster, cfg) == "papers"

    def test_unknown_source_returns_unknown(self) -> None:
        cfg = EditorialConfig(
            source_to_category={"known": "papers"},
            source_to_trust={"known": 3},
        )
        cluster = Cluster(
            cluster_id="c_aaaaaaaaaaaa1002",
            item_ids=["i_04"],
            canonical_title="t",
            sources=["mystery_source"],
            earliest_published=FIXED_EARLIER,
            size=1,
        )
        assert _cluster_category(cluster, cfg) == "unknown"


class TestCapsDeterminism:
    """Pure-code deterministic guard: same input -> same output across
    repeated runs of the picker chain."""

    def test_same_input_same_output(self) -> None:
        from collections import Counter
        cfg = _papers_cfg(per_source=2, papers_cap=4)
        ids = [f"c_aaaaaaaaaaaa30{i:02x}" for i in (1, 2, 3, 4, 5, 6)]
        # Mix of paper + non-paper sources, all big_picture tagged.
        sources_cycle = [
            "arXiv cs.CL", "Hugging Face Daily Papers", "arXiv cs.CL",
            "Simon Willison's Blog", "OpenAI", "Anthropic",
        ]
        clusters = {
            cid: _cluster_with_source(cid, sources_cycle[i])
            for i, cid in enumerate(ids)
        }
        blocks = [
            (_ranked_tagged(cid, score=55, tags=["big_picture"]), _summary_for(cid))
            for cid in ids
        ]
        # Run the picker 5 times; results identical.
        outputs = []
        for _ in range(5):
            counters: Counter[str] = Counter()
            picked = _pick_big_picture(
                blocks, set(ids), clusters_by_id=clusters, cfg=cfg,
                categories_used_this_issue=counters,
            )
            outputs.append(tuple(picked))
        assert len(set(outputs)) == 1, "picker should be deterministic"


class TestAssembleSectionsIntegration:
    """End-to-end seam: _assemble_sections threading the EditorialConfig
    through every picker and the cap state surviving Pulse -> Big Picture
    -> Hands-On -> On the Radar."""

    def test_caps_propagate_pulse_to_subsequent_sections(self) -> None:
        """Pulse picks a paper. The per-category counter sees it before
        Big Picture runs; Big Picture's paper budget is therefore reduced.

        Schema v3 (2026-05-30): tier is the routing authority. We split
        the 7 candidates across big_picture / hands_on tiers explicitly so
        the picker chain has stories to find in each tier pool. Degraded-
        mode Pass 2 is gone; the cap is a hard ceiling and under-fill
        surfaces via the shape post-condition.
        """
        # 4 big_picture-tier + 3 hands_on-tier paper candidates. Cap=4
        # papers per issue. Top story becomes Pulse from the head-tier
        # union (eligibility gate passes via size>1 in clusters below).
        bp_ids = [f"c_aaaaaaaaaaaa40{i:02x}" for i in (1, 2, 3, 4)]
        ho_ids = [f"c_aaaaaaaaaaaa40{i:02x}" for i in (5, 6, 7)]
        ids = bp_ids + ho_ids
        # significance/hands_on_utility/freshness >= 70 so they're pulse-class.
        breakdown = {
            "significance": 80, "hands_on_utility": 80,
            "big_picture_relevance": 70, "financial_services_impact": 50,
            "freshness_momentum": 80,
        }
        weighted = round(_weighted_sum(breakdown))
        ranked = [
            RankedStory(
                cluster_id=cid, score=weighted, breakdown=breakdown,
                audience_tags=["big_picture", "hands_on"],
                rationale="t",
                tier="big_picture" if cid in bp_ids else "hands_on",
                prompt_version="v0.2",
            )
            for cid in ids
        ]
        blocks = list(zip(ranked, [_summary_for(cid) for cid in ids]))
        # All from arXiv cs.CL (papers). Source is multi-item so each
        # cluster passes Pulse eligibility via size>1.
        clusters = {
            cid: Cluster(
                cluster_id=cid,
                item_ids=[f"i_{cid[2:6]}_a", f"i_{cid[2:6]}_b"],
                canonical_title="t",
                sources=["arXiv cs.CL"],
                earliest_published=FIXED_EARLIER,
                size=2,
                canonical_id=None,
            )
            for cid in ids
        }
        items: dict[str, Item] = {}
        for cid in ids:
            cluster = clusters[cid]
            for iid in cluster.item_ids:
                items[iid] = Item(
                    id=iid, source="arXiv cs.CL", source_type="rss",
                    url=f"https://arxiv.org/abs/{iid}",  # type: ignore[arg-type]
                    title=f"t-{iid}",
                    published_at=FIXED_EARLIER, raw_summary="",
                    fetched_at=FIXED_NOW, trust_weight=1,
                )
        cfg = _papers_cfg(per_source=10, papers_cap=4)
        pulse, bp, ho, rad = _assemble_sections(
            blocks, clusters_by_id=clusters, items_by_id=items,
            editorial_config=cfg,
        )
        # Pulse always carries 1 paper. Remaining cap budget: 3 papers.
        # Pulse picks the first head-tier candidate (a big_picture). Big
        # Picture's pool then has 3 left (the other big_picture-tier
        # stories); cap=4 - 1 (pulse) = 3 slots remain -> all 3 land.
        # Hands-On's pool has 3 hands_on-tier candidates and 0 paper slots
        # under cap; degraded-mode fill is GONE -> 0 accepted.
        # Currents is empty (no currents-tier stories in the input).
        assert len(pulse.stories) == 1
        assert len(bp.stories) == 3
        assert len(ho.stories) == 0
        assert len(rad.stories) == 0


# ===========================================================================
# Phase 2 (2026-05-30) -- section taxonomy + voice + caps.
#
# - on_the_radar -> currents rename (with pydantic alias for archived data)
# - explicit hard ceiling on Currents via editorial.yaml
# - audience-only routing in head pickers (no maturity gate)
# ===========================================================================

class TestLegacyTierAlias:
    """Phase 2: archived ranked.jsonl records carrying ``tier="on_the_radar"``
    must continue to parse cleanly. The model_validator(mode="before")
    transparently coerces the legacy value to ``"currents"`` at input time."""

    def test_ranked_story_legacy_tier_value_coerces_to_currents(self) -> None:
        """A v3 archive record with ``tier="on_the_radar"`` round-trips as
        ``tier="currents"`` on the v4 model."""
        breakdown = {
            "significance": 60, "hands_on_utility": 60,
            "big_picture_relevance": 60, "financial_services_impact": 25,
            "freshness_momentum": 60,
        }
        weighted = round(_weighted_sum(breakdown))
        # Build the payload the way ranked.jsonl rows arrive: raw dict
        # mirroring the v3 schema (with the old tier value).
        legacy_payload = {
            "schema_version": 3,
            "cluster_id": "c_aaaaaaaaaaaaaaaa",
            "score": weighted,
            "breakdown": breakdown,
            "audience_tags": ["hands_on"],
            "rationale": "legacy archive row",
            "tier": "on_the_radar",
            "prompt_version": "v0.4",
        }
        rs = RankedStory.model_validate(legacy_payload)
        # Alias collapsed the legacy value to the canonical name.
        assert rs.tier == "currents"

    def test_issue_section_legacy_name_coerces_to_currents(self) -> None:
        """A v2 IssueSection record with ``name="on_the_radar"`` parses on
        the v3 model and the name is coerced to ``"currents"``."""
        legacy_payload = {
            "schema_version": 2,
            "name": "on_the_radar",
            "stories": [],
        }
        section = IssueSection.model_validate(legacy_payload)
        assert section.name == "currents"


class TestCurrentsCapEnforcement:
    """Phase 2: ``_pick_currents`` enforces a hard ceiling from
    ``cfg.currents_max_stories``. Earlier behaviour relied on the upstream
    ``CURRENTS_TIER_SUMMARISE_BUDGET`` to bound the section; that's now
    the input safety bound, this is the editorial authority."""

    def test_caps_cuts_currents_at_configured_ceiling(self) -> None:
        """Ten currents-tier candidates + cap=4 -> only 4 land."""
        from collections import Counter
        cfg = EditorialConfig(
            per_source_per_section=100,  # high so per-section never binds
            per_category_per_issue={},
            source_to_category={},
            source_to_trust={},
            currents_max_stories=4,
        )
        ids = [f"c_aaaaaaaaaaaa50{i:02x}" for i in range(10)]
        clusters = {
            cid: _cluster_with_source(cid, f"src_{i:02x}")
            for i, cid in enumerate(ids)
        }
        blocks = [
            (_ranked_tagged(cid, score=45, tags=["general"],
                            tier="currents"), _summary_for(cid))
            for cid in ids
        ]
        categories_used: Counter[str] = Counter()
        picked = _pick_currents(
            blocks, set(ids),
            clusters_by_id=clusters, cfg=cfg,
            categories_used_this_issue=categories_used,
        )
        assert len(picked) == 4
        # Score-desc input order is preserved (file order is score-desc).
        assert picked == ids[:4]

    def test_default_config_uses_default_currents_max(self) -> None:
        """When no cfg is passed, ``_pick_currents`` still bounds at the
        in-code default (8). Mirrors the fork-friendly defaults elsewhere
        in the module."""
        # 12 candidates, no cfg -> default cap (8) binds.
        ids = [f"c_bbbbbbbbbbbb50{i:02x}" for i in range(12)]
        blocks = [
            (_ranked_tagged(cid, score=45, tags=["general"],
                            tier="currents"), _summary_for(cid))
            for cid in ids
        ]
        picked = _pick_currents(blocks, set(ids))
        assert len(picked) == DEFAULT_CURRENTS_MAX_STORIES == 8


class TestHeadPickersNoMaturityGate:
    """Phase 2: head-section pickers (``_pick_big_picture``,
    ``_pick_hands_on``) route on TIER only. They must NOT impose a
    secondary maturity / freshness / signal-dimensions filter -- that
    would re-create the audience-vs-maturity conflation EDITORIAL.md
    flagged. ``_pick_pulse`` retains its eligibility gate (sourcing
    credibility) and signal-dimensions Pulse-class bar; those gates are
    Pulse-specific, not head-section maturity gates, and live in
    ``TestPulseEligibilityGate`` + ``TestPulseSelectionPriorCoverageBias``."""

    def test_big_picture_accepts_low_freshness_story(self) -> None:
        """A big_picture-tier story with low freshness still lands -- the
        head picker does not gate on freshness_momentum."""
        from collections import Counter
        cfg = EditorialConfig(
            per_source_per_section=10,
            per_category_per_issue={},
            source_to_category={},
            source_to_trust={},
        )
        # Build a story with explicitly low freshness.
        breakdown = {
            "significance": 70, "hands_on_utility": 40,
            "big_picture_relevance": 80, "financial_services_impact": 30,
            "freshness_momentum": 10,  # cold story
        }
        weighted = round(_weighted_sum(breakdown))
        story = RankedStory(
            cluster_id="c_aaaaaaaaaaaa6001",
            score=weighted,
            breakdown=breakdown,
            audience_tags=["big_picture"],
            rationale="cold but strategic",
            tier="big_picture",
            prompt_version="v0.4",
        )
        block = _summary_for(story.cluster_id)
        cluster = _cluster_with_source(story.cluster_id, "some_lab")
        picked = _pick_big_picture(
            [(story, block)],
            {story.cluster_id},
            clusters_by_id={story.cluster_id: cluster},
            cfg=cfg,
            categories_used_this_issue=Counter(),
        )
        # Audience-only: low freshness does not block.
        assert picked == [story.cluster_id]

    def test_hands_on_accepts_story_regardless_of_signal_dimensions(self) -> None:
        """A hands_on-tier story that misses the >= 2 signal-dimensions
        Pulse bar still lands in Hands-On -- that bar applies only inside
        ``_pick_pulse``."""
        from collections import Counter
        cfg = EditorialConfig(
            per_source_per_section=10,
            per_category_per_issue={},
            source_to_category={},
            source_to_trust={},
        )
        # All three "signal dimensions" (significance, hands_on_utility,
        # freshness_momentum) below 70 -- the Pulse-class bar fails.
        breakdown = {
            "significance": 60, "hands_on_utility": 60,
            "big_picture_relevance": 30, "financial_services_impact": 30,
            "freshness_momentum": 50,
        }
        weighted = round(_weighted_sum(breakdown))
        story = RankedStory(
            cluster_id="c_aaaaaaaaaaaa6002",
            score=weighted,
            breakdown=breakdown,
            audience_tags=["hands_on"],
            rationale="below the Pulse-class bar, still a Hands-On",
            tier="hands_on",
            prompt_version="v0.4",
        )
        block = _summary_for(story.cluster_id)
        cluster = _cluster_with_source(story.cluster_id, "some_repo")
        picked = _pick_hands_on(
            [(story, block)],
            {story.cluster_id},
            clusters_by_id={story.cluster_id: cluster},
            cfg=cfg,
            categories_used_this_issue=Counter(),
        )
        # Audience-only: no signal-dimension filter applied inside the
        # head picker.
        assert picked == [story.cluster_id]


# ===========================================================================
# v0.7 (2026-05-31) -- per-section weighted scores drive picker ordering.
#
# Each picker now ranks candidates within its tier pool by the section-
# specific weighted score (RankedStory.score_by_section[section]) rather
# than the legacy aggregate ``score`` / file order. The spec calls out one
# anchor case: a story strong on hands_on_utility but weak on significance
# should land in Hands-On at a top position, where the old single-
# aggregate flow would have ranked it low globally.
# ===========================================================================

def _ranked_with_section_scores(
    cluster_id: str,
    *,
    breakdown: dict[str, int],
    tier: str,
    audience_tags: list[str] | None = None,
) -> RankedStory:
    """Build a v6 RankedStory with score_by_section populated from
    breakdown x SECTION_WEIGHTS. Mirrors what rank.py persists. The
    aggregate score is recomputed under RUBRIC_WEIGHTS for the legacy
    invariant."""
    from src.models import SECTION_WEIGHTS, RUBRIC_WEIGHTS
    score = round(sum(
        (RUBRIC_WEIGHTS[k] / 100.0) * v for k, v in breakdown.items()
    ))
    score_by_section = {
        section: round(sum(
            (weights[k] / 100.0) * breakdown[k] for k in weights
        ))
        for section, weights in SECTION_WEIGHTS.items()
    }
    return RankedStory(
        cluster_id=cluster_id,
        score=score,
        score_by_section=score_by_section,
        breakdown=breakdown,
        audience_tags=audience_tags or ["hands_on"],  # type: ignore[arg-type]
        rationale="r",
        tier=tier,  # type: ignore[arg-type]
        prompt_version="v0.6",
    )


class TestSectionScoreOrdering:
    """v0.7: pickers rank within tier pool by the section-specific
    weighted score, not by the aggregate score / file order."""

    def test_hands_on_pool_orders_by_hands_on_section_score(self) -> None:
        """Two hands_on-tier candidates: the one with the higher
        hands_on section score lands first, even when its aggregate
        ``score`` (RUBRIC_WEIGHTS sum) is lower. This is the spec
        anchor case -- strong on hands_on_utility but weak on
        significance lands at the TOP of Hands-On."""
        from collections import Counter
        cfg = EditorialConfig(
            per_source_per_section=10,
            per_category_per_issue={},
            source_to_category={},
            source_to_trust={},
        )
        # Story A: strong on hands_on_utility, weak on significance.
        # Aggregate score under RUBRIC_WEIGHTS (40/10/30/15/5):
        #   0.40*40 + 0.10*95 + 0.30*30 + 0.15*30 + 0.05*60 = 41
        # hands_on section score under SECTION_WEIGHTS (25/45/10/10/10):
        #   0.25*40 + 0.45*95 + 0.10*30 + 0.10*30 + 0.10*60 = 64.75 -> 65
        story_a = _ranked_with_section_scores(
            "c_aaaaaaaaaaaa7001",
            breakdown={
                "significance": 40, "hands_on_utility": 95,
                "big_picture_relevance": 30, "financial_services_impact": 30,
                "freshness_momentum": 60,
            },
            tier="hands_on",
        )
        # Story B: stronger on significance, weaker on hands_on_utility.
        # Aggregate: 0.40*75 + 0.10*55 + 0.30*55 + 0.15*30 + 0.05*40 = 58.5 -> 58 or 59
        # hands_on section: 0.25*75 + 0.45*55 + 0.10*55 + 0.10*30 + 0.10*40 = 56
        story_b = _ranked_with_section_scores(
            "c_aaaaaaaaaaaa7002",
            breakdown={
                "significance": 75, "hands_on_utility": 55,
                "big_picture_relevance": 55, "financial_services_impact": 30,
                "freshness_momentum": 40,
            },
            tier="hands_on",
        )
        # Assertions on the score model -- the v0.7 routing premise.
        assert story_a.score_by_section is not None
        assert story_b.score_by_section is not None
        assert story_a.score_by_section["hands_on"] > story_b.score_by_section["hands_on"]
        # Aggregate score goes the OTHER way (story_a < story_b).
        assert story_a.score < story_b.score

        # Now feed both to _pick_hands_on. story_a must land first --
        # the section-specific ordering, not the aggregate.
        blocks = [
            (story_b, _summary_for(story_b.cluster_id)),
            (story_a, _summary_for(story_a.cluster_id)),
        ]
        clusters_by_id = {
            story_a.cluster_id: _cluster_with_source(story_a.cluster_id, "repo_a"),
            story_b.cluster_id: _cluster_with_source(story_b.cluster_id, "repo_b"),
        }
        picked = _pick_hands_on(
            blocks, {story_a.cluster_id, story_b.cluster_id},
            clusters_by_id=clusters_by_id, cfg=cfg,
            categories_used_this_issue=Counter(),
        )
        assert picked == [story_a.cluster_id, story_b.cluster_id]

    def test_big_picture_pool_orders_by_big_picture_section_score(self) -> None:
        """Mirror of the Hands-On test but for Big Picture: the
        story with the higher big_picture section score (driven by
        big_picture_relevance + significance) lands first in the
        Big Picture pool, regardless of aggregate."""
        from collections import Counter
        cfg = EditorialConfig(
            per_source_per_section=10,
            per_category_per_issue={},
            source_to_category={},
            source_to_trust={},
        )
        # Strong on big_picture_relevance.
        story_a = _ranked_with_section_scores(
            "c_aaaaaaaaaaaa7011",
            breakdown={
                "significance": 50, "hands_on_utility": 30,
                "big_picture_relevance": 95, "financial_services_impact": 40,
                "freshness_momentum": 40,
            },
            tier="big_picture",
            audience_tags=["big_picture"],
        )
        # Strong on significance + hands_on_utility, weak on big_picture.
        story_b = _ranked_with_section_scores(
            "c_aaaaaaaaaaaa7012",
            breakdown={
                "significance": 80, "hands_on_utility": 80,
                "big_picture_relevance": 30, "financial_services_impact": 40,
                "freshness_momentum": 40,
            },
            tier="big_picture",
            audience_tags=["big_picture"],
        )
        assert story_a.score_by_section is not None
        assert story_b.score_by_section is not None
        assert story_a.score_by_section["big_picture"] > story_b.score_by_section["big_picture"]

        blocks = [
            (story_b, _summary_for(story_b.cluster_id)),
            (story_a, _summary_for(story_a.cluster_id)),
        ]
        clusters_by_id = {
            story_a.cluster_id: _cluster_with_source(story_a.cluster_id, "lab_a"),
            story_b.cluster_id: _cluster_with_source(story_b.cluster_id, "lab_b"),
        }
        picked = _pick_big_picture(
            blocks, {story_a.cluster_id, story_b.cluster_id},
            clusters_by_id=clusters_by_id, cfg=cfg,
            categories_used_this_issue=Counter(),
        )
        assert picked == [story_a.cluster_id, story_b.cluster_id]

    def test_currents_pool_orders_by_currents_section_score(self) -> None:
        """Currents now orders within its tier pool by the Currents-
        specific weighted score. A story with high freshness_momentum
        lands higher than one with high significance but low freshness."""
        from collections import Counter
        cfg = EditorialConfig(
            per_source_per_section=10,
            per_category_per_issue={},
            source_to_category={},
            source_to_trust={},
        )
        # High freshness lifts the Currents score (Currents weights
        # freshness_momentum at 20, second-most among the sections).
        story_a = _ranked_with_section_scores(
            "c_aaaaaaaaaaaa7021",
            breakdown={
                "significance": 50, "hands_on_utility": 50,
                "big_picture_relevance": 40, "financial_services_impact": 40,
                "freshness_momentum": 95,
            },
            tier="currents",
            audience_tags=["general"],
        )
        story_b = _ranked_with_section_scores(
            "c_aaaaaaaaaaaa7022",
            breakdown={
                "significance": 50, "hands_on_utility": 50,
                "big_picture_relevance": 40, "financial_services_impact": 40,
                "freshness_momentum": 20,
            },
            tier="currents",
            audience_tags=["general"],
        )
        assert story_a.score_by_section is not None
        assert story_b.score_by_section is not None
        assert story_a.score_by_section["currents"] > story_b.score_by_section["currents"]

        blocks = [
            (story_b, _summary_for(story_b.cluster_id)),
            (story_a, _summary_for(story_a.cluster_id)),
        ]
        clusters_by_id = {
            story_a.cluster_id: _cluster_with_source(story_a.cluster_id, "src_a"),
            story_b.cluster_id: _cluster_with_source(story_b.cluster_id, "src_b"),
        }
        picked = _pick_currents(
            blocks, {story_a.cluster_id, story_b.cluster_id},
            clusters_by_id=clusters_by_id, cfg=cfg,
            categories_used_this_issue=Counter(),
        )
        assert picked == [story_a.cluster_id, story_b.cluster_id]

    def test_pulse_pool_ranked_by_pulse_section_score(self) -> None:
        """Pulse picks from the union of big_picture + hands_on tier
        candidates, ranked by ``score_by_section["pulse"]``. A story
        that's strong on the Pulse weights (significance + big_picture
        + finance) wins over a higher-aggregate hands_on story."""
        # Story A: strong on significance + big_picture_relevance +
        # finance -> high pulse weighted sum, lands in big_picture tier.
        story_a = _ranked_with_section_scores(
            "c_aaaaaaaaaaaa7031",
            breakdown={
                "significance": 85, "hands_on_utility": 40,
                "big_picture_relevance": 80, "financial_services_impact": 80,
                "freshness_momentum": 60,
            },
            tier="big_picture",
            audience_tags=["big_picture", "finance"],
        )
        # Story B: hands_on tier, with a high hands_on score but lower
        # pulse score (the pulse weights are significance + bp + finance
        # heavy; hands_on_utility weight in pulse is only 5).
        story_b = _ranked_with_section_scores(
            "c_aaaaaaaaaaaa7032",
            breakdown={
                "significance": 65, "hands_on_utility": 95,
                "big_picture_relevance": 40, "financial_services_impact": 30,
                "freshness_momentum": 60,
            },
            tier="hands_on",
            audience_tags=["hands_on"],
        )
        # Verify the premise: story_a has the higher pulse score.
        assert story_a.score_by_section is not None
        assert story_b.score_by_section is not None
        assert story_a.score_by_section["pulse"] > story_b.score_by_section["pulse"]

        # Both are multi-source via cluster.size=2 -> pass the
        # eligibility gate; both fresh; both Pulse-class (>=2 of sig /
        # ho / fm >= 70 hits). Pulse is therefore picked by the
        # pulse-score-desc ordering inside the eligible Pulse-class pool.
        cluster_a = Cluster(
            cluster_id=story_a.cluster_id,
            item_ids=[f"i_{story_a.cluster_id[2:6]}_a", f"i_{story_a.cluster_id[2:6]}_b"],
            canonical_title="t",
            sources=["src_a"],
            earliest_published=FIXED_EARLIER,
            size=2,
        )
        cluster_b = Cluster(
            cluster_id=story_b.cluster_id,
            item_ids=[f"i_{story_b.cluster_id[2:6]}_a", f"i_{story_b.cluster_id[2:6]}_b"],
            canonical_title="t",
            sources=["src_b"],
            earliest_published=FIXED_EARLIER,
            size=2,
        )
        clusters_by_id = {
            cluster_a.cluster_id: cluster_a,
            cluster_b.cluster_id: cluster_b,
        }
        items_by_id: dict[str, Item] = {}
        for c in (cluster_a, cluster_b):
            for iid in c.item_ids:
                items_by_id[iid] = Item(
                    id=iid, source=c.sources[0], source_type="rss",
                    url=f"https://example.com/{iid}",  # type: ignore[arg-type]
                    title="t", published_at=FIXED_EARLIER,
                    raw_summary="", fetched_at=FIXED_NOW, trust_weight=2,
                )
        blocks = [
            (story_a, _summary_for(story_a.cluster_id)),
            (story_b, _summary_for(story_b.cluster_id)),
        ]
        pulse_id = _pick_pulse(
            blocks,
            clusters_by_id=clusters_by_id,
            items_by_id=items_by_id,
        )
        assert pulse_id == story_a.cluster_id


# ===========================================================================
# Per-section closing-shape prompt content (v0.11, 2026-05-31).
#
# The closing-shape strings are load-bearing for the editorial voice change;
# pin that they actually reach the prompt for each tier. We assert prompt-
# template containment only -- LLM-output assertions live in evals.
# ===========================================================================


class TestClosingShapePromptContent:
    """The v0.11 prompt branches per tier to inject a section-specific
    CLOSING SHAPE. Each tier must carry its frame name in the assembled
    prompt; head-tier stories must also carry the Pulse plain-take shape
    in case the picker elevates them."""

    def _prompt_for_tier(self, tier: str) -> str:
        breakdown = {
            "significance": 60,
            "hands_on_utility": 60,
            "big_picture_relevance": 60,
            "financial_services_impact": 25,
            "freshness_momentum": 60,
        }
        story = RankedStory(
            cluster_id="c_0123456789abcdef",
            score=55,
            breakdown=breakdown,
            audience_tags=["general"],  # type: ignore[arg-type]
            rationale="test",
            tier=tier,  # type: ignore[arg-type]
            prompt_version="v0.2",
        )
        cluster = Cluster(
            cluster_id=story.cluster_id,
            item_ids=["i_x"],
            canonical_title="t",
            sources=["src_a"],
            earliest_published=FIXED_EARLIER,
            size=1,
        )
        item = Item(
            id="i_x", source="src_a", source_type="rss",
            url="https://example.com/x",  # type: ignore[arg-type]
            title="t", published_at=FIXED_EARLIER,
            raw_summary="", fetched_at=FIXED_NOW, trust_weight=2,
        )
        return _build_summary_prompt(story, cluster, [item], callbacks=[])

    def test_big_picture_carries_strategic_question_closing(self) -> None:
        prompt = self._prompt_for_tier("big_picture")
        assert "CLOSING SHAPE -- STRATEGIC QUESTION" in prompt
        # Head-tier story must also carry the Pulse plain-take closing
        # because the picker may elevate it to The Pulse.
        assert "PULSE CLOSING SHAPE -- PLAIN TAKE" in prompt

    def test_hands_on_carries_imperative_action_closing(self) -> None:
        prompt = self._prompt_for_tier("hands_on")
        assert "CLOSING SHAPE -- IMPERATIVE ACTION (SHARPENED)" in prompt
        assert "PULSE CLOSING SHAPE -- PLAIN TAKE" in prompt

    def test_currents_carries_calibrated_stake_closing(self) -> None:
        prompt = self._prompt_for_tier("currents")
        assert "CLOSING SHAPE -- CALIBRATED STAKE" in prompt
        # Currents is not Pulse-eligible; the Pulse closing must NOT be
        # attached. Avoids cross-shape leakage into early-signal stories.
        assert "PULSE CLOSING SHAPE -- PLAIN TAKE" not in prompt


# ===========================================================================
# Pulse re-summarise (v0.12, 2026-05-31).
#
# After ``_pick_pulse`` chooses the head-tier winner, the summary that was
# written during the head-tier pass carries the wrong closing shape
# (Big Picture's STRATEGIC QUESTION or Hands-On's IMPERATIVE ACTION). The
# Pulse needs a PLAIN TAKE close instead. ``_maybe_resummarise_pulse``
# re-runs the per-story prompt under ``section_override="pulse"`` and
# replaces the original SummaryBlock in place. Failure (LLM error, parse
# fail) falls back to the original block + logs a WARNING.
# ===========================================================================

class TestPulseResummarise:
    """The v0.12 Pulse re-summarise pass. Three load-bearing properties:
    (1) the call fires exactly once per issue, on the picked Pulse story;
    (2) on success the replacement block lands in by_id under the same
        cluster_id; (3) on failure the original block stands and a
        WARNING is logged."""

    def _make_fixtures(self, cluster_id: str = "c_ffffffffffffeeee"):
        """Build the minimal cluster + items + ranked + summary needed to
        exercise ``_maybe_resummarise_pulse``. Keeps the test bodies tight
        and the inputs uniform across the three cases."""
        cluster = Cluster(
            cluster_id=cluster_id,
            item_ids=["i_p_01"],
            canonical_title="Pulse story canonical title",
            sources=["openai_blog"],
            earliest_published=FIXED_EARLIER,
            size=1,
            canonical_id="arxiv:2605.99999",
        )
        item = Item(
            id="i_p_01",
            source="openai_blog",
            source_type="rss",
            url="https://example.com/pulse-story",  # type: ignore[arg-type]
            title="Pulse item title",
            published_at=FIXED_EARLIER,
            raw_summary="A short raw summary for the fixture.",
            fetched_at=FIXED_NOW,
            trust_weight=5,
        )
        story = RankedStory(
            cluster_id=cluster_id,
            score=70,
            breakdown={
                "significance": 80,
                "hands_on_utility": 70,
                "big_picture_relevance": 70,
                "financial_services_impact": 40,
                "freshness_momentum": 80,
            },
            audience_tags=["big_picture"],  # type: ignore[arg-type]
            rationale="test",
            tier="big_picture",
            prompt_version="v0.2",
        )
        original_block = SummaryBlock(
            story_id=cluster_id,
            headline="Head-tier headline with strategic question shape",
            summary=(
                "Head-tier body written under Big Picture STRATEGIC QUESTION "
                "shape. Who owns the next decision when the agent ships "
                "unsupervised, and is that role staffed in your org today?"
            ),
            source_urls=["https://example.com/pulse-story"],  # type: ignore[list-item]
            signal="act",
        )
        return cluster, item, story, original_block

    def test_resummarise_invoked_exactly_once_on_picked_pulse(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The re-summarise helper must fire exactly once per issue, on the
        cluster_id returned by ``_pick_pulse``. Counter pinned via a
        monkeypatched ``_resummarise_as_pulse`` -- we don't want to mount
        a live LLM, and the count is the contract."""
        from src import summarise as summarise_mod

        cluster, item, story, original_block = self._make_fixtures()
        # Also build a second head-tier story so the picker has a choice
        # but the re-summarise still fires only once (on the chosen one).
        other_cluster, other_item, other_story, other_block = self._make_fixtures(
            "c_ffffffffffffeeef",
        )
        # Replacement SummaryBlock the mocked re-summarise returns. The
        # plain-take landing is the load-bearing rhetorical difference.
        replacement = SummaryBlock(
            story_id=story.cluster_id,
            headline="Pulse-shaped headline opens on a verb",
            summary=(
                "Re-summarised body landing on a PLAIN TAKE close. The day's "
                "shift named in editorial prose. That's the news today."
            ),
            source_urls=["https://example.com/pulse-story"],  # type: ignore[list-item]
            signal="act",
        )
        calls: list[str] = []

        def fake_resummarise(*, story, cluster, items, callbacks, original_block, **_kwargs):
            calls.append(cluster.cluster_id)
            return replacement

        monkeypatch.setattr(
            summarise_mod, "_resummarise_as_pulse", fake_resummarise,
        )

        by_id = {
            story.cluster_id: (story, original_block),
            other_story.cluster_id: (other_story, other_block),
        }
        clusters_by_id = {
            cluster.cluster_id: cluster,
            other_cluster.cluster_id: other_cluster,
        }
        items_by_id = {item.id: item, other_item.id: other_item}

        summarise_mod._maybe_resummarise_pulse(
            pulse_id=story.cluster_id,
            by_id=by_id,
            clusters_by_id=clusters_by_id,
            items_by_id=items_by_id,
            callbacks_by_root=None,
        )

        # Exactly once, on the picked cluster_id.
        assert calls == [story.cluster_id]

    def test_resummarise_replaces_original_block_for_picked_cluster(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """On success the new SummaryBlock replaces the original in
        ``by_id[pulse_id]``. Other entries (head-tier stories the picker
        DIDN'T elevate) must NOT be touched."""
        from src import summarise as summarise_mod

        cluster, item, story, original_block = self._make_fixtures()
        # A second story whose block must remain untouched.
        other_cluster, other_item, other_story, other_block = self._make_fixtures(
            "c_ffffffffffffeef0",
        )
        replacement = SummaryBlock(
            story_id=story.cluster_id,
            headline="Pulse plain-take headline replacement",
            summary=(
                "Replacement body that lands on a PLAIN TAKE close. Names "
                "what is true now given the day's shift. That's the take."
            ),
            source_urls=["https://example.com/pulse-story"],  # type: ignore[list-item]
            signal="act",
        )

        def fake_resummarise(*, story, cluster, items, callbacks, original_block, **_kwargs):
            return replacement

        monkeypatch.setattr(
            summarise_mod, "_resummarise_as_pulse", fake_resummarise,
        )

        by_id = {
            story.cluster_id: (story, original_block),
            other_story.cluster_id: (other_story, other_block),
        }

        summarise_mod._maybe_resummarise_pulse(
            pulse_id=story.cluster_id,
            by_id=by_id,
            clusters_by_id={
                cluster.cluster_id: cluster,
                other_cluster.cluster_id: other_cluster,
            },
            items_by_id={item.id: item, other_item.id: other_item},
            callbacks_by_root=None,
        )

        # Picked block was REPLACED.
        assert by_id[story.cluster_id][1] is replacement
        assert by_id[story.cluster_id][1].headline == replacement.headline
        # RankedStory is preserved (we only swap the SummaryBlock side).
        assert by_id[story.cluster_id][0] is story
        # Other story's block was NOT touched -- only the Pulse-elected
        # cluster is re-summarised.
        assert by_id[other_story.cluster_id][1] is other_block

    def test_resummarise_failure_preserves_original_and_logs_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog,
    ) -> None:
        """When the re-summarise LLM call raises, the original SummaryBlock
        must be preserved and a WARNING logged. The publication still
        ships -- one off-shape Pulse beats a failed issue."""
        from src import summarise as summarise_mod
        import logging as _logging

        cluster, item, story, original_block = self._make_fixtures()

        def raising_resummarise(*, story, cluster, items, callbacks, original_block, **_kwargs):
            raise RuntimeError("simulated LLM timeout")

        monkeypatch.setattr(
            summarise_mod, "_resummarise_as_pulse", raising_resummarise,
        )

        by_id = {story.cluster_id: (story, original_block)}

        with caplog.at_level(_logging.WARNING, logger="ai_vector.summarise"):
            summarise_mod._maybe_resummarise_pulse(
                pulse_id=story.cluster_id,
                by_id=by_id,
                clusters_by_id={cluster.cluster_id: cluster},
                items_by_id={item.id: item},
                callbacks_by_root=None,
            )

        # Original block preserved.
        assert by_id[story.cluster_id][1] is original_block
        # WARNING (or higher) was logged naming the cluster id.
        assert any(
            r.levelno >= _logging.WARNING and story.cluster_id in r.message
            for r in caplog.records
        ), (
            "expected a WARNING-level log naming the Pulse cluster_id "
            "after re-summarise failure; saw: "
            + repr([(r.levelname, r.message) for r in caplog.records])
        )


# ===========================================================================
# Voice diversity injection (v0.13, 2026-06-03).
#
# Two pieces of context inlined into BOTH the per-story summarise prompt
# AND the section-intro prompt so the LLM stops repeating constructions
# the editor caught on issues #8-#11 (e.g. "X outruns Y", "Verify before
# you X"). The tests cover the three loaders + the prompt rendering +
# the version bump, mocking the filesystem so we don't touch
# ``data/released/`` or the live ``EDITORIAL.md``.
# ===========================================================================

class TestVoiceDiversityVersionBump:
    """The injection is a MATERIAL prompt change; SUMMARISE_PROMPT_VERSION
    must move with it so the audit trail picks up the shift."""

    def test_summarise_prompt_version_is_v0_20(self) -> None:
        from src.summarise import SUMMARISE_PROMPT_VERSION
        assert SUMMARISE_PROMPT_VERSION == "v0.20"


# ===========================================================================
# v0.15 (2026-06-03): HEADLINE RULES + wider source-excerpt window.
#
# Triggered by the 2026-06-02 colleague.skill story where the headline
# "Capture a departing engineer's judgment as a versioned, editable file"
# dropped both the COLLEAGUE.SKILL paper name and the existing dot-skill
# repo name and read as a puzzle.  Two assertions cover the load-bearing
# bits: (a) the three new headline rules are inlined into every per-story
# prompt; (b) the source-excerpt cap was raised from 500 -> 1000 words.
# ===========================================================================

class TestHeadlineRulesInPrompt:
    """The three new HEADLINE RULES must arrive in every per-story summarise
    prompt (head-tier AND currents-tier, not Pulse-specific)."""

    def test_per_story_prompt_contains_headline_rules(self) -> None:
        from src.summarise import _build_summary_prompt
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
        prompt = _build_summary_prompt(
            story, cluster, [item], callbacks=[],
        )
        # The section title is present (one line we can grep for without
        # fragility against minor copy edits).
        assert "HEADLINE RULES" in prompt
        # Each of the three rule numerals is present, anchored by its
        # rule heading -- enough that the LLM is reading three discrete
        # instructions, not one collapsed paragraph.
        assert "1. RECOGNITION DECIDES NAMING" in prompt
        assert "2. PRESERVE DISTINCTIONS IN THE SOURCE" in prompt
        assert "3. HEADLINES LAND IN ONE BEAT" in prompt
        assert "4. NO INLINE VENUE CITATIONS IN THE BODY" in prompt


class TestSourceExcerptCapRaised:
    """The source-excerpt soft cap moved from 500 -> 1000 words in v0.15
    so the HEADLINE RULES (name the artifact; preserve distinctions) have
    the load-bearing material available."""

    def test_source_excerpt_max_words_is_1000(self) -> None:
        from src.summarise import _SOURCE_EXCERPT_MAX_WORDS
        assert _SOURCE_EXCERPT_MAX_WORDS == 1000


class TestVoiceDiversityConstants:
    """The two new module constants must be present + carry the documented
    defaults so the editor / eval engineer can grep for them without
    chasing the import."""

    def test_lookback_default_matches_editor_review_window(self) -> None:
        from src.summarise import VOICE_DIVERSITY_LOOKBACK
        assert VOICE_DIVERSITY_LOOKBACK == 5

    def test_anti_patterns_heading_matches_editorial_md_contract(self) -> None:
        from src.summarise import EDITORIAL_ANTI_PATTERNS_HEADING
        assert EDITORIAL_ANTI_PATTERNS_HEADING == (
            "## Anti-patterns the editor will flag"
        )


class TestLoadEditorialAntiPatterns:
    """Parse the catalogue section from EDITORIAL.md. Defensive on
    formatting variations, falls back to empty list on missing section /
    missing file."""

    def test_parses_bullets_under_anti_patterns_heading(
        self, tmp_path,
    ) -> None:
        from src.summarise import _load_editorial_anti_patterns
        md = tmp_path / "EDITORIAL.md"
        md.write_text(
            "# EDITORIAL.md\n"
            "\n"
            "Some prose at the top.\n"
            "\n"
            "## Anti-patterns the editor will flag\n"
            "\n"
            "Quick framing prose that should NOT be picked up.\n"
            "\n"
            '- "X outruns Y" / "X is outpacing Y" / "X is outrunning Y"\n'
            '- "Verify before you [verb]" / "Verify before you adopt"\n'
            "- Two sections sharing one thesis statement\n"
            "\n"
            "## Next section\n"
            "\n"
            "- This bullet must NOT be picked up.\n",
            encoding="utf-8",
        )
        out = _load_editorial_anti_patterns(md)
        assert out == [
            '"X outruns Y" / "X is outpacing Y" / "X is outrunning Y"',
            '"Verify before you [verb]" / "Verify before you adopt"',
            "Two sections sharing one thesis statement",
        ]

    def test_section_missing_returns_empty_and_logs_info(
        self, tmp_path, caplog,
    ) -> None:
        import logging as _logging
        from src.summarise import _load_editorial_anti_patterns
        md = tmp_path / "EDITORIAL.md"
        md.write_text(
            "# EDITORIAL.md\n\n"
            "## A different heading\n\n"
            "- This bullet belongs to no anti-pattern catalogue.\n",
            encoding="utf-8",
        )
        with caplog.at_level(_logging.INFO, logger="ai_vector.summarise"):
            out = _load_editorial_anti_patterns(md)
        assert out == []
        # INFO log (not WARNING -- the section is optional).
        assert any(
            r.levelno == _logging.INFO
            and "anti-pattern" in r.message.lower()
            for r in caplog.records
        )

    def test_missing_file_returns_empty(self, tmp_path) -> None:
        from src.summarise import _load_editorial_anti_patterns
        out = _load_editorial_anti_patterns(tmp_path / "nope.md")
        assert out == []

    def test_skips_blank_lines_and_non_bullets_inside_section(
        self, tmp_path,
    ) -> None:
        """Defensive parsing: a paragraph or stray blank line inside the
        anti-patterns section should not crash or absorb-as-bullet."""
        from src.summarise import _load_editorial_anti_patterns
        md = tmp_path / "EDITORIAL.md"
        md.write_text(
            "## Anti-patterns the editor will flag\n"
            "\n"
            "Editor's note: these are the recurring constructions.\n"
            "\n"
            "- first anti-pattern\n"
            "\n"
            "Inline reminder.\n"
            "\n"
            "- second anti-pattern\n",
            encoding="utf-8",
        )
        assert _load_editorial_anti_patterns(md) == [
            "first anti-pattern",
            "second anti-pattern",
        ]


class TestLoadRecentIntrosAndClosings:
    """The recent-issues loader walks the released archive newest-first
    and gathers each issue's section intros + first-story closings. It
    must tolerate missing days, malformed files, and the legacy
    ``on_the_radar`` -> ``currents`` rename."""

    @staticmethod
    def _write_issue(
        root, day, *,
        pulse_closing="A short Pulse take.",
        big_lead="Trust the system.",
        big_closing="Audit the architecture today.",
        hands_lead="Bench before you budget.",
        hands_closing="Clone v1 and measure.",
        currents_lead="Models are getting faster.",
        currents_closing="If it holds, ship; if not, watch.",
    ):
        """Write a minimal released issue.json fixture for one date."""
        import datetime as _dt
        import json as _json
        from src import paths as _paths
        issue_path = _paths.issue_path(day, canonical=True)
        issue_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "issue_number": 1,
            "date": day.isoformat(),
            "pulse": {
                "name": "pulse",
                "stories": [
                    {
                        "headline": "p",
                        "summary": f"Lead. Middle. {pulse_closing}",
                    }
                ],
                "intro_lead": None,
            },
            "sections": [
                {
                    "name": "big_picture",
                    "stories": [
                        {
                            "headline": "b",
                            "summary": f"Lead. Middle. {big_closing}",
                        }
                    ],
                    "intro_lead": big_lead,
                },
                {
                    "name": "hands_on",
                    "stories": [
                        {
                            "headline": "h",
                            "summary": f"Lead. Middle. {hands_closing}",
                        }
                    ],
                    "intro_lead": hands_lead,
                },
                {
                    "name": "currents",
                    "stories": [
                        {
                            "headline": "c",
                            "summary": f"Lead. Middle. {currents_closing}",
                        }
                    ],
                    "intro_lead": currents_lead,
                },
            ],
        }
        issue_path.write_text(_json.dumps(payload), encoding="utf-8")

    def test_no_prior_issues_returns_empty(
        self, tmp_data_root,
    ) -> None:
        from src.summarise import _load_recent_intros_and_closings
        today = _dt.date(2026, 6, 3)
        out = _load_recent_intros_and_closings(today)
        assert out == []

    def test_single_prior_issue_picked_up(
        self, tmp_data_root,
    ) -> None:
        from src.summarise import _load_recent_intros_and_closings
        today = _dt.date(2026, 6, 3)
        self._write_issue(tmp_data_root, _dt.date(2026, 6, 2))
        out = _load_recent_intros_and_closings(today)
        assert len(out) == 1
        past = out[0]
        assert past.issue_date == _dt.date(2026, 6, 2)
        assert past.intro_leads["big_picture"] == "Trust the system."
        assert past.intro_leads["hands_on"] == "Bench before you budget."
        assert past.intro_leads["currents"] == "Models are getting faster."
        # Pulse never has an intro_lead.
        assert "pulse" not in past.intro_leads
        # Closings populated for all four section keys.
        assert past.first_story_closings["pulse"] == "A short Pulse take"
        assert past.first_story_closings["big_picture"] == (
            "Audit the architecture today"
        )

    def test_lookback_caps_to_five_with_seven_prior_issues(
        self, tmp_data_root,
    ) -> None:
        from src.summarise import _load_recent_intros_and_closings
        today = _dt.date(2026, 6, 10)
        # Seven prior days, each with a unique big_lead so we can verify
        # the FIVE newest were taken.
        for delta in range(1, 8):
            d = today - _dt.timedelta(days=delta)
            self._write_issue(
                tmp_data_root, d,
                big_lead=f"Lead {d.isoformat()}.",
            )
        out = _load_recent_intros_and_closings(today)
        # Five newest, in newest-first order.
        assert [p.issue_date for p in out] == [
            today - _dt.timedelta(days=k) for k in range(1, 6)
        ]
        assert out[0].intro_leads["big_picture"] == (
            f"Lead {(today - _dt.timedelta(days=1)).isoformat()}."
        )

    def test_unparseable_issue_json_skipped_with_info_log(
        self, tmp_data_root, caplog,
    ) -> None:
        import logging as _logging
        from src import paths as _paths
        from src.summarise import _load_recent_intros_and_closings
        today = _dt.date(2026, 6, 3)
        # Write a malformed JSON issue file on day -1 + a valid one on -2.
        bad_day = today - _dt.timedelta(days=1)
        bad_path = _paths.issue_path(bad_day, canonical=True)
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_path.write_text("{ not valid json", encoding="utf-8")
        self._write_issue(tmp_data_root, today - _dt.timedelta(days=2))
        with caplog.at_level(_logging.INFO, logger="ai_vector.summarise"):
            out = _load_recent_intros_and_closings(today)
        # Bad file skipped; the good one still surfaced.
        assert [p.issue_date for p in out] == [today - _dt.timedelta(days=2)]
        # INFO log fired naming the unparseable file.
        assert any(
            r.levelno == _logging.INFO
            and "could not parse" in r.message
            for r in caplog.records
        )

    def test_legacy_on_the_radar_normalised_to_currents(
        self, tmp_data_root,
    ) -> None:
        """Archived issues used ``on_the_radar`` before the v0.10 rename;
        the loader must surface that lead under the ``currents`` key so
        the diversity block reads with a single section vocabulary."""
        import datetime as _dt
        import json as _json
        from src import paths as _paths
        from src.summarise import _load_recent_intros_and_closings
        today = _dt.date(2026, 6, 3)
        day = today - _dt.timedelta(days=1)
        issue_path = _paths.issue_path(day, canonical=True)
        issue_path.parent.mkdir(parents=True, exist_ok=True)
        issue_path.write_text(
            _json.dumps({
                "issue_number": 1,
                "date": day.isoformat(),
                "pulse": {"name": "pulse", "stories": []},
                "sections": [
                    {
                        "name": "on_the_radar",
                        "stories": [
                            {
                                "headline": "x",
                                "summary": "Lead. Mid. Legacy close.",
                            }
                        ],
                        "intro_lead": "Legacy radar posture.",
                    },
                ],
            }),
            encoding="utf-8",
        )
        out = _load_recent_intros_and_closings(today)
        assert out[0].intro_leads["currents"] == "Legacy radar posture."
        assert out[0].first_story_closings["currents"] == "Legacy close"


class TestRenderVoiceDiversityBlock:
    """The rendered prompt segment must group constructions by section,
    quote them so the LLM can match exact phrasing, and include the
    'do not repeat' instruction + the anti-pattern list when supplied."""

    def _past_issues(self):
        import datetime as _dt
        from src.summarise import _PastIssueVoice
        return [
            _PastIssueVoice(
                issue_date=_dt.date(2026, 6, 2),
                intro_leads={
                    "big_picture": "Speed is outrunning safety.",
                    "hands_on": "Verify before you adopt.",
                    "currents": "Capability races ahead of control.",
                },
                first_story_closings={
                    "pulse": "On-device agentic AI is no longer a research promise",
                    "big_picture": "When agent commit volume doubles again, which part",
                    "hands_on": "Try the pattern via the public Hermes Agent walkthrough",
                    "currents": "ask vendors whether their models handle non-stationary",
                },
            ),
            _PastIssueVoice(
                issue_date=_dt.date(2026, 6, 1),
                intro_leads={
                    "big_picture": "Capability outruns control.",
                    "hands_on": "Verify before you deploy.",
                },
                first_story_closings={
                    "pulse": "Single-source reporting from a Future of Life newsletter",
                    "big_picture": "If your agents mix client documents with external calls",
                    "hands_on": "Clone the Nano variant against your simulation pipeline",
                },
            ),
        ]

    def test_renders_recent_constructions_grouped_by_section(self) -> None:
        from src.summarise import _render_voice_diversity_block
        out = _render_voice_diversity_block(self._past_issues(), [])
        # Section headers present.
        assert "[pulse]" in out
        assert "[big_picture]" in out
        assert "[hands_on]" in out
        assert "[currents]" in out
        # Specific constructions quoted (matches the editor's flagged set).
        assert "'Speed is outrunning safety.'" in out
        assert "'Verify before you adopt.'" in out
        assert "'Capability outruns control.'" in out
        assert "'Verify before you deploy.'" in out
        # Dates are tagged so the LLM sees recency.
        assert "2026-06-02 lead" in out
        assert "2026-06-01 lead" in out
        # Do-not-repeat instruction present.
        assert "do not repeat" in out.lower()
        assert "thesis statement" in out.lower()

    def test_renders_anti_patterns_when_supplied(self) -> None:
        from src.summarise import _render_voice_diversity_block
        anti = [
            '"X outruns Y" / "X is outpacing Y"',
            '"Verify before you [verb]"',
        ]
        out = _render_voice_diversity_block([], anti)
        assert "ANTI-PATTERNS" in out
        assert '"X outruns Y" / "X is outpacing Y"' in out
        assert '"Verify before you [verb]"' in out

    def test_empty_inputs_render_empty_block(self) -> None:
        from src.summarise import _render_voice_diversity_block
        assert _render_voice_diversity_block([], []) == ""


class TestVoiceDiversityBlockReachesPromptBuilders:
    """The block must arrive in BOTH the per-story summarise prompt and
    the section-intro prompt so neither writing surface drifts back into
    the recent constructions."""

    def test_per_story_prompt_contains_block(self) -> None:
        from src.summarise import _build_summary_prompt
        breakdown = {
            "significance": 60, "hands_on_utility": 60,
            "big_picture_relevance": 60, "financial_services_impact": 25,
            "freshness_momentum": 60,
        }
        story = RankedStory(
            cluster_id="c_0123456789abcdef",
            score=55, breakdown=breakdown,
            audience_tags=["general"],  # type: ignore[arg-type]
            rationale="t", tier="big_picture", prompt_version="v0.2",
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
        block = (
            "VOICE DIVERSITY -- the editor will flag repeats from recent issues\n"
            "RECENTLY USED CONSTRUCTIONS -- do not repeat:\n"
            "  [big_picture]\n"
            "    - 2026-06-02 lead: 'Speed is outrunning safety.'\n"
        )
        prompt = _build_summary_prompt(
            story, cluster, [item], callbacks=[],
            voice_diversity_block=block,
        )
        assert "VOICE DIVERSITY" in prompt
        assert "Speed is outrunning safety." in prompt

    def test_per_story_prompt_omits_block_when_empty(self) -> None:
        from src.summarise import _build_summary_prompt
        breakdown = {
            "significance": 60, "hands_on_utility": 60,
            "big_picture_relevance": 60, "financial_services_impact": 25,
            "freshness_momentum": 60,
        }
        story = RankedStory(
            cluster_id="c_0123456789abcdef",
            score=55, breakdown=breakdown,
            audience_tags=["general"],  # type: ignore[arg-type]
            rationale="t", tier="big_picture", prompt_version="v0.2",
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
        prompt = _build_summary_prompt(
            story, cluster, [item], callbacks=[],
            voice_diversity_block="",
        )
        assert "VOICE DIVERSITY" not in prompt

    def test_section_intro_prompt_includes_block(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_populate_section_intro`` builds its own prompt; it must
        thread the diversity block in too. Mock the LLM call and capture
        the prompt it was handed."""
        from src import summarise as summarise_mod
        section = IssueSection(
            name="big_picture",
            stories=[
                SummaryBlock(
                    story_id="c_0123456789abcdef",
                    headline="h1",
                    summary="A body that satisfies the validator threshold "
                            "for word count and lands cleanly somewhere.",
                    source_urls=["https://example.com/x"],  # type: ignore[list-item]
                )
            ],
        )
        captured: dict[str, str] = {}

        def fake_llm_call(prompt, **_kwargs):
            captured["prompt"] = prompt
            return '{"lead": "Fresh lead.", "body": "Body with twenty plus words for the test framing across these one stories."}'

        monkeypatch.setattr(summarise_mod, "_llm_call", fake_llm_call)
        block = (
            "VOICE DIVERSITY -- the editor will flag repeats from recent issues\n"
            "RECENTLY USED CONSTRUCTIONS -- do not repeat:\n"
            "  [big_picture]\n"
            "    - 2026-06-02 lead: 'Speed is outrunning safety.'\n"
        )
        summarise_mod._populate_section_intro(section, block)
        assert "VOICE DIVERSITY" in captured["prompt"]
        assert "Speed is outrunning safety." in captured["prompt"]
