# Platform asks — AI Vector day-one validation

**Status:** drafted by Release Engineer for Arman to send.
**Context for Arman:** these are the three §7 blocking questions PLAN.md flags.
Pipeline build-out is gated on at least workarounds being identified. Send
these to the people who run your internal-bank GitHub (platform / infra /
DevX team).

You can send them as one email with three numbered asks, or as three
separate tickets/threads — your call based on who the right recipient is
and how that team prefers to receive requests.

---

## Ask 1 — GitHub Actions `schedule:` triggers

**Subject:** AI Vector — request: confirm GitHub Actions `schedule:` triggers available

**Context:**
I'm building a small internal repo (AI Vector) — a daily, agent-assisted AI
newsletter for personal use, with a financial-services lens. The engine runs on
GitHub Actions, writes output to the repo's `docs/` folder, and publishes via
GitHub Pages. No external infra, no external users — v0 is purely internal, with
me as the sole audience. I'd like to use a daily `schedule:` cron trigger to kick
off the run each morning (target: ~7 am Sydney time, i.e. UTC 21:00 the previous
day).

**The ask:**
Are GitHub Actions enabled on this org, and specifically, are `schedule:` (cron)
triggers permitted on workflows? A once-daily run is all we need.

**If not available — our fallback:**
We can switch to `workflow_dispatch` (manual trigger) with an external nudge —
a local cron on an approved machine that hits the dispatch API, or a manual
button press. Knowing now lets us scope the pipeline correctly rather than
discovering the constraint after building around `schedule:`.

**Time we need:** before we invest in pipeline code — this shapes the workflow
architecture from day one.

---

## Ask 2 — Outbound egress from Actions runners

**Subject:** AI Vector — request: confirm outbound network egress from Actions runners

**Context:**
Same project as Ask 1 — an internal daily-publish repo running on GitHub Actions.
During each daily run, the engine fetches data from two classes of external
endpoints before doing any local processing.

**The ask:**
Are Actions runners able to reach the following?

**(a) RSS/API endpoints** — public feed URLs from AI labs and publishers: Anthropic
blog, OpenAI blog, Google DeepMind, Hugging Face, Hacker News Algolia API, arXiv
RSS, and ~25 similar hostnames, all over HTTPS:443. Happy to supply the full
hostname list if an allowlist review is required.

**(b) LLM API endpoint** — a LiteLLM / Bedrock endpoint for the ranking and
summarisation calls the engine makes per run. I'll provide the specific endpoint
details once we know the architecture is viable here and I've confirmed which
internal LLM gateway to use.

Are there allowlist mechanics, proxy requirements, or other patterns we should
follow to reach either class of endpoint from a runner?

**If not available — our fallback:**
The fetch step can run from a different approved network — a local cron on an
approved machine that pulls the data and pushes it into the repo — with the
Action handling only the in-repo processing and publish. Less clean, but workable.
Worth knowing before the Source Engineer invests time in fetch.py.

**Time we need:** same as Ask 1 — before pipeline code is written.

---

## Ask 3 — GitHub Pages on `/docs`

**Subject:** AI Vector — request: confirm GitHub Pages publish from `/docs` is enabled

**Context:**
Same project. The pipeline writes a daily HTML file to the repo's `docs/` folder
and relies on GitHub Pages to serve it. The published page is a daily HTML issue
plus a flat archive — pure static, no server-side code. Internal-only URL is
perfectly fine; v0 has no external audience.

**The ask:**
Is GitHub Pages enabled on this org? Specifically, can a repo be configured to
serve its `docs/` folder as a Pages site (internal URL only is fine)?

**If not available — our fallback:**
Substitute publish surfaces we could explore: an internal wiki or site, sharing
the rendered HTML via an internal blob store or shared drive, or reading the
rendered file directly from the repo. None are as clean as Pages; some require
meaningful extra scope. Want to know now so we can size the substitute work if
needed.

**Time we need:** same as Ask 1.

---

## Recap for Arman

| # | Blocker if no | Workaround | Effort if workaround |
|---|---|---|---|
| 1 | No daily cron | `workflow_dispatch` + external nudge | small |
| 2 | No egress to RSS/API/LLM | Fetch from approved network → push to repo | medium — needs separate runner |
| 3 | No Pages on `/docs` | Substitute publish surface | medium-to-large depending on substitute |

**Recommendation:** send all three together as one ask if possible. They're
related — the same team likely answers them, and the answers tend to come
together. The friction cost of three separate threads is higher than one
consolidated one.
