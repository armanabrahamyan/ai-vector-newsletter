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

from src.models import Item, RankedStory, SummaryBlock
from src.summarise import (
    _pick_pulse,
    _pick_source_urls,
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
        tier="on_the_radar",
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
    # weighted sum: 0.3*sig + 0.25*hands_on + 0.2*bp + 0.15*fs + 0.1*fresh
    weighted = (
        0.30 * significance + 0.25 * hands_on + 0.20 * big_picture
        + 0.15 * fs + 0.10 * freshness
    )
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
        tier="on_the_radar",
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
