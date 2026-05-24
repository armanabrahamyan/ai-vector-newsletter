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

from src.models import Item
from src.summarise import _pick_source_urls, _url_dedup_key
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
