# AI Vector — Source Research

*Research conducted: 2026-05-23. Author: Source Engineer.*
*This is the single source of truth for what AI Vector subscribes to and why.*
*`config/sources.yaml` is populated from this document. When sources change, update here first.*

---

## Methodology

Research was conducted on 2026-05-23 using a combination of:

1. **WebFetch probes** — direct URL probing for each candidate feed (RSS/Atom validity, item count, most-recent item date).
2. **WebSearch** — discovering current canonical feed URLs, third-party workarounds, and identifying finance-AI candidates.
3. **Both editorial lenses applied in sequence** — `editorial-focus` (tier filter + signal filter) first; `finance-lens` (sourcing-signal criteria) applied additionally for all finance-AI candidates.

**Tier-filter discipline applied:**
- Tier-1 (Agentic + Generative AI) sources are the core of the list.
- Tier-2 sources included only when load-bearing for practitioners.
- Tier-3-dominant sources excluded or trust-weighted down.
- "Subscribe, don't scrape" (PLAN §0.4). Third-party community-maintained GitHub-hosted RSS feeds are treated as a provisional exception — they are structured XML feeds, not live HTML scraping. Each is flagged with its dependency and fragility.

---

## Prioritized Master List

### Priority 1 — Must Have

*High-trust, high-signal, daily/weekly cadence. These are the load-bearing sources for AI Vector's daily issue.*

| # | Name | Feed URL | Type | Category | Tier expectation | Why | Frequency |
|---|------|----------|------|----------|-----------------|-----|-----------|
| 1 | OpenAI Blog | `https://openai.com/news/rss.xml` | rss | lab | tier-1 | Primary model-release and capability-advance source; dense Tier-1 on release days. Confirmed live 2026-05-23. | Burst on releases, quiet otherwise |
| 2 | Google DeepMind Blog | `https://deepmind.google/discover/blog/feed/` | rss | lab | tier-1 | Gemini, AlphaFold, safety, foundational research. Confirmed live 2026-05-23. | Weekly–monthly |
| 3 | Hugging Face Blog | `https://huggingface.co/blog/feed.xml` | rss | lab | tier-1 | 400+ entries, model releases, fine-tuning, inference optimisations, open-source tooling. Best consistent Tier-1 source. Trust weight 4. Confirmed live 2026-05-23. | Multiple per week |
| 4 | Hugging Face Daily Papers | `https://huggingface.co/api/daily_papers` | api | papers | tier-1 | Community-curated daily highlights. Higher signal-to-noise than raw arXiv. Trust weight 4. | Daily |
| 5 | Import AI | `https://importai.substack.com/feed` | rss | newsletter | tier-1 | Jack Clark's weekly; years of consistent Tier-1 signal. Safety, capability, policy with depth. Trust weight 4. Confirmed live 2026-05-23. | Weekly |
| 6 | Ahead of AI (Sebastian Raschka) | `https://magazine.sebastianraschka.com/feed` | rss | newsletter | tier-1 | Best practitioner analysis of ML/LLM papers. Deep Tier-1: training, fine-tuning, inference. 150k+ subscribers. Trust weight 4. Confirmed live 2026-05-23. | Weekly (monthly deep-dives) |
| 7 | Latent Space | `https://www.latent.space/feed` | rss | newsletter | tier-1 | Long-form interviews with practitioners. Tier-1: inference, agents, open-source. Trust weight 4. Confirmed live 2026-05-23. | 2–3× per month |
| 8 | Simon Willison's Blog | `https://simonwillison.net/atom/everything/` | atom | newsletter | tier-1 | Running LLM commentary; practical, high-frequency, signal-dense. Trust weight 4. Confirmed live 2026-05-23. | Daily (short posts) |
| 9 | Hacker News (Algolia API) | `https://hn.algolia.com/api/v1/search?query=LLM+OR+%22language+model%22+OR+%22AI+agent%22&tags=story&numericFilters=points%3E100` | api | community | tier-1-mixed | Community breaks stories same day as labs. Points threshold placeholder — LLM Engineer to calibrate. | Daily (continuous) |
| 10 | r/LocalLLaMA (Reddit) | `https://www.reddit.com/r/LocalLLaMA.json` | api | community | tier-1-mixed | Highest-signal community for open-weight deployments, quantisation, inference. Trust weight 2. | Daily (continuous) |
| 11 | The Batch (deeplearning.ai) | `https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_the_batch.xml` | rss | newsletter | tier-1-mixed | Andrew Ng's weekly; broad Tier-1-mixed coverage from practitioner lens. Feed via Olshansk/rss-feeds (hourly scrape, maintained). Confirmed active 2026-05-23 (most recent item: May 22). | Weekly |
| 12 | Anthropic News | `https://raw.githubusercontent.com/taobojlen/anthropic-rss-feed/main/anthropic_news_rss.xml` | rss | lab | tier-1 | Primary Claude/AI safety source. No official feed — this third-party feed is maintained community project (taobojlen), runs on schedule, 15 items, confirmed live 2026-05-23 (latest: May 22). Dependency risk flagged below. | Burst on releases |
| 13 | Mistral AI News | `https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_mistral.xml` | rss | lab | tier-1 | Open-weight model releases (Mistral 3, Codestral, etc.). No official feed — Olshansk/rss-feeds community project (hourly, maintained). Confirmed active 2026-05-23 (latest: April 29). | Burst on releases |
| 14 | Google Research Blog | `https://research.google/blog/rss/` | rss | lab | tier-1-mixed | Broader than DeepMind; applied ML, infrastructure, foundation models. Confirmed live 2026-05-23. | Weekly–monthly |
| 15 | TLDR AI | `https://tldr.tech/api/rss/ai` | rss | newsletter | tier-1-mixed | Daily digest; high volume, good breadth. Catches items that slip others. Trust weight 3. Confirmed live 2026-05-23. | Daily |

---

### Priority 2 — Strong Supporting

*Good signal, lower volume or slightly broader scope. Supplements P1 to ensure daily signal volume.*

| # | Name | Feed URL | Type | Category | Tier expectation | Why | Frequency |
|---|------|----------|------|----------|-----------------|-----|-----------|
| 16 | LangChain Blog | `https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_langchain.xml` *(TBD — see dead-feeds section)* | rss | lab | tier-1 | LangGraph, agentic frameworks. Confirmed active at blog.langchain.com (Webflow, no native RSS). Use Olshansk if feed available; otherwise fallback pending. | Weekly |
| 17 | LlamaIndex Blog | `https://medium.com/feed/llamaindex-blog` | rss | lab | tier-1 | RAG, agentic pipelines, document AI. Medium feed confirmed live 2026-05-23 (note: most recent item is March 2024 — blog may have migrated to llamaindex.ai/blog which has no feed). See dead-feeds section. | Weekly |
| 18 | Cohere Blog | `https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_cohere.xml` | rss | lab | tier-1 | Enterprise RAG, deployment, model releases. No official feed — Olshansk community feed, confirmed live (latest: March 16, 2026). | Monthly–burst |
| 19 | Microsoft Research Blog | `https://www.microsoft.com/en-us/research/blog/feed/` | rss | lab | load-bearing-tier-2 | Deep research on reasoning, safety, multimodal, systems. Load-bearing Tier-2. Confirmed live 2026-05-23. | Weekly |
| 20 | Microsoft AI Blog | `https://blogs.microsoft.com/ai/feed/` | rss | lab | tier-1-mixed | Azure AI, Copilot, product-side. Tier-1 on capability shifts; Tier-3 risk on marketing. Trust weight 2. Confirmed live 2026-05-23. | Weekly |
| 21 | Meta AI Blog | `https://about.fb.com/news/feed/` | rss | lab | tier-1-mixed | Llama releases, open-source drops. Corporate feed (not AI-specific). No ai.meta.com feed found. Trust weight 2. Confirmed live 2026-05-23. | Burst on releases |
| 22 | Last Week in AI | `https://lastweekin.ai/feed` | rss | newsletter | tier-1-mixed | Weekly AI summary; good breadth, reasonable editorial selection. Confirmed live 2026-05-23. | Weekly |
| 23 | Eugene Yan's Blog | `https://eugeneyan.com/rss/` | rss | newsletter | tier-1 | Practitioner ML: recommendations, LLMs, agents, evals, engineering. 210+ posts. Confirmed live 2026-05-23 (latest: May 3, 2026). Trust weight 3. | Monthly |
| 24 | Chip Huyen's Blog | `https://huyenchip.com/feed` | rss | newsletter | tier-1 | Gen AI application design, production ML, agents. Confirmed live (latest: Jan 16, 2025). Trust weight 3. Note: lower cadence in 2025–26. | Low-frequency |
| 25 | Lilian Weng's Blog (Lil'Log) | `https://lilianweng.github.io/index.xml` | rss | newsletter | tier-1 | Deep technical posts from OpenAI (now Meta) researcher. Reward hacking, reasoning, hallucinations. Confirmed live (latest: May 2025). Trust weight 4 — low cadence but always signal. | Very low frequency (high quality) |
| 26 | Jay Alammar / Language Models | `https://newsletter.languagemodels.co/feed` | rss | newsletter | tier-1 | Illustrated explainers of LLM architectures (NeurIPS 2025 visual, etc.). Feed confirmed live 2026-05-23 (redirects from jayalammar.substack.com). Trust weight 3. | Low-frequency |
| 27 | The Algorithmic Bridge | `https://www.thealgorithmicbridge.com/feed` | rss | newsletter | tier-1-mixed | Alberto Romero; AI analysis for general-technical audience. Confirmed live (latest: May 20, 2026). Trust weight 2 — watch tier mix. | Weekly |
| 28 | BAIR Blog (Berkeley AI Research) | `https://bair.berkeley.edu/blog/feed.xml` | rss | research | tier-1-mixed | Academic AI research from Berkeley; RL, vision, NLP, planning. Confirmed live (latest: May 8, 2026). Trust weight 3. | Monthly |
| 29 | EleutherAI Blog | `https://blog.eleuther.ai/index.xml` | rss | research | load-bearing-tier-2 | Open-weight AI safety research; reward hacking, alignment, pretraining. Confirmed live (latest: April 15, 2026). Trust weight 3. | Low-frequency |
| 30 | arXiv cs.CL | `http://export.arxiv.org/rss/cs.CL` | rss | papers | risk-tier-3 | CL firehose; highest per-paper relevance of arXiv feeds. Trust weight 1; heavy LLM-Engineer filtering required. Confirmed live. | Daily (50–200 papers) |
| 31 | r/MachineLearning (Reddit) | `https://www.reddit.com/r/MachineLearning.json` | api | community | load-bearing-tier-2 | Papers, research discussion. Surfaces papers before they trend. Trust weight 2. | Daily |
| 32 | MIT Technology Review AI | `https://www.technologyreview.com/feed/` | rss | news | tier-1-mixed | Regulation, safety, capability shifts with depth. Trust weight 3. Confirmed live 2026-05-23. | Daily |
| 33 | VentureBeat AI | `https://venturebeat.com/category/ai/feed/` | rss | news | tier-1-mixed | Enterprise AI, product launches. High Tier-3 risk. Trust weight 2. Confirmed live 2026-05-23. | Daily |
| 34 | Replicate Changelog | `https://replicate.com/changelog/rss` | rss | tooling | tier-1 | Model releases, MCP integrations, agent infrastructure changes. Confirmed live (latest: April 21, 2026). Trust weight 3. | Monthly–burst |
| 35 | vLLM Blog | Redirect to `https://vllm.ai/feed.xml` — returns 404; use `https://blog.vllm.ai/` (no confirmed feed). TBD. | rss | tooling | tier-1 | Inference-engine releases; directly relevant to model deployment. See tooling gap note. | Burst on releases |

---

### Priority 3 — Long-tail / Watchlist

*Specialized, lower trust to start, or cadence too low for daily reliance. Monitor and promote if quality holds.*

| # | Name | Feed URL | Type | Category | Notes |
|---|------|----------|------|----------|-------|
| 36 | arXiv cs.AI | `http://export.arxiv.org/rss/cs.AI` | rss | papers | Broad AI firehose. Trust weight 1. Confirmed live. |
| 37 | arXiv cs.LG | `http://export.arxiv.org/rss/cs.LG` | rss | papers | ML firehose. Trust weight 1. Confirmed live. |
| 38 | Towards Data Science | `https://towardsdatascience.com/feed` | rss | community | Community-authored; Tier-1-mixed but highly variable quality. Trust weight 2. Confirmed live (active May 22, 2026). |
| 39 | Gradient Flow (Ben Lorica) | `https://gradientflow.substack.com/feed` | rss | newsletter | Weekly data/ML/AI practitioner. Finance angle occasional. Confirmed live (latest: May 19, 2026). Trust weight 2. |
| 40 | Numerai Blog | `https://blog.numer.ai/rss/` | rss | finance-ai | Monthly ML competition / quant finance updates. Trust weight 2. Confirmed live (latest: May 5, 2026). |
| 41 | Ars Technica AI | `https://arstechnica.com/ai/feed/` | rss | news | Listed as working in 2026 RSS directories. WebFetch blocked (anti-scrape). Re-probe from pipeline environment. Trust weight 3. |
| 42 | The Verge AI | `https://www.theverge.com/ai-artificial-intelligence/rss/index.xml` | rss | news | Confirmed in 2026 RSS directories. WebFetch blocked. Re-probe from pipeline. Trust weight 2. |
| 43 | BIS Research Papers | `https://www.bis.org/doclist/bis_fsi_publs.rss` | rss | finance-ai | BIS financial research feed; AI/fintech items mixed in. RSS 1.0 format. Confirmed live (latest: May 21, 2026). Low frequency, high credibility when it hits. Trust weight 3. |
| 44 | Risk.net Cutting Edge | `http://www.risk.net/feeds/rss/category/cutting-edge` | rss | finance-ai | Peer-reviewed quant finance papers. Paywalled content but titles/abstracts free. Confirmed live (latest: May 20, 2026). Trust weight 3. Paywall flagged. |
| 45 | ML Quant Finance (Derek Snow) | `https://blog.ml-quant.com/feed` | rss | finance-ai | Weekly quant letter with finance+ML links. Confirmed live (latest: May 20, 2026). Trust weight 2. |
| 46 | LLMQuant Newsletter | `https://llmquant.substack.com/feed` | rss | finance-ai | Open-source community, AI+quant finance, 2–3× per week. Confirmed live (latest: May 22, 2026). Trust weight 2. Watch tier-mix. |
| 47 | AI in Finance (Christophe Atten) | `https://aiinfinance.substack.com/feed` | rss | finance-ai | EU AI Act, LLM deployment in FS, practical. Confirmed live (latest: May 20, 2026). Trust weight 2. |
| 48 | GARP Risk Intelligence | TBD — no feed URL confirmed; check `garp.org/rss` | rss | finance-ai | Agentic AI + SR 26-2 / model risk. Active 2025–26. No feed confirmed. |

---

### Excluded (Researched, Ruled Out)

| Source | Reason |
|--------|--------|
| **VentureBeat AI (P1)** | Kept at P2 with trust weight 2; too much Tier-3 vendor PR to be P1. Eval Engineer to watch items_kept ratio. |
| **X/Twitter** | Locked per PLAN §0.4 — API closed/expensive, scraping = ToS violation. |
| **LinkedIn** | Locked per PLAN §0.4 — same reason. |
| **QuantAI Substack** | Low publishing frequency (last confirmed post: November 2024). Not reliable for daily cadence. |
| **The Algorithmic Bridge** | Borderline — kept at P2/watchlist. Not FS-specific and leans general-public AI commentary. Watch Tier-1 density. |
| **OpenQuant Newsletter** | Jobs + events focus, not AI signal. Out of scope for editorial lens. |
| **American Banker** | `americanbanker.com/feed` returns HTML, not RSS. AI section exists but no machine-readable feed confirmed. Paywall likely. Candidate for future manual check. |
| **FT Alphaville** | Paywalled with no confirmed free RSS for the AI/tech section. Ruled out for automated pipeline — too much friction. |
| **Two Sigma Engineering Blog** | `twosigma.com/feed/` exists but is an empty RSS channel (no `<item>` elements). Confirmed dead. |
| **Domino Data Lab Blog** | `domino.ai/blog/rss.xml` and `/blog/feed` both return empty/404. No functional feed found. |
| **ValidMind Blog** | `validmind.com/blog/rss.xml` returns 404. Valuable content but no RSS endpoint found. |
| **Anyscale Blog** | Confirmed no RSS at `/blog/rss` or `/blog/feed.xml` (both return HTML). Anyscale merged into Ray/Anyscale brand; check periodically. |
| **Together AI Blog** | No RSS found. `/blog/rss` 404. Candidate if they add a feed. |
| **Stanford CRFM / HAI Blog** | `hai.stanford.edu/news/feed` returns 404. SAIL blog (`ai.stanford.edu/blog/feed.xml`) may exist — unconfirmed. Low daily volume for P1/P2. |
| **Stripe Engineering Blog** | `stripe.com/blog/feed/rss` 404. Good content but no feed; engineering-specific, not AI-primary. |
| **LMSys / LMSYS Org Blog** | No confirmed RSS feed on `lmsys.org/blog`. Publish 1–2 papers/posts per month; watchlist. |
| **JP Morgan AI Research** | No RSS confirmed on public website. Publications are quarterly/sporadic. Low daily volume. |
| **FCA / PRA publications** | FCA RSS page blocked (403). Irregular publication cadence. Tier-2 at best for regulatory updates — not daily signal source. |
| **Replicate Blog** | `replicate.com/blog/rss` valid but recent items are tutorial-like (video prompting, etc.) — Tier-3 risk. Changelog feed (P2 above) is the better signal. |

---

## Dead Feeds — Research Outcomes

All 8 sources previously marked `enabled: false` in sources.yaml were researched on 2026-05-23.

### 1. Anthropic
**Status: No official feed exists — community workaround found and verified.**

Anthropic's `/news` page has no `<link rel="alternate">` RSS tag in the HTML head. All canonical variants probed (news/rss.xml, feed.xml, rss.xml, blog/feed, research/feed) return 404. Multiple third-party GitHub projects exist to fill this gap:
- **taobojlen/anthropic-rss-feed** — hourly GitHub Action, generates `anthropic_news_rss.xml`. Confirmed live 2026-05-23; most recent item: May 22, 2026 (Project Glasswing). 15 items, build date May 23. Active.
- **Olshansk/rss-feeds** — also has `feed_anthropic_news.xml` (270 items, most recent May 19). Active.

**Resolution**: Use `https://raw.githubusercontent.com/taobojlen/anthropic-rss-feed/main/anthropic_news_rss.xml` as a provisional feed. This is structured XML (not live scraping) — it is a community-maintained feed generator that runs on a schedule. **Dependency risk**: if the maintainer pauses the repo, the feed goes stale. Note this in sources.yaml and re-probe monthly. This is the "rare scraping" case per PLAN §0.4 — the scraping is done by the third-party project, not by us; we consume their XML output. For the pipeline, this is treated as a regular RSS feed. Flag as fragile.

**Architect review recommended**: Arman and Architect should decide whether to depend on a community feed vs. a dedicated AI Vector scraper for Anthropic (PLAN §0.4 — scraping requires Architect approval). For now, using taobojlen's feed is a reasonable Phase 0/1 starting point.

### 2. Mistral AI
**Status: No official feed exists — community workaround found and verified.**

`mistral.ai/rss.xml`, `/feed.xml`, `/news/rss`, `/news/feed.xml` — all 404. No feed link in page HTML.

**Resolution**: Use `https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_mistral.xml` (Olshansk/rss-feeds, hourly). Confirmed live 2026-05-23; most recent item: April 29, 2026. Same dependency-risk caveat as Anthropic above.

### 3. Cohere
**Status: No official blog RSS — community workaround found.**

`cohere.com/blog` page has no feed link in HTML. All variants (blog/rss, blog/rss.xml, blog/feed) return HTML or 404. `docs.cohere.com/changelog/rss` also 404.

**Resolution**: Use `https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_cohere.xml` (Olshansk/rss-feeds, hourly). Confirmed live 2026-05-23; most recent item: March 16, 2026 (gap of ~2 months — Cohere may be publishing less frequently, or the scraper missed items). Trust weight stays at 2 until Eval establishes a baseline. Same dependency-risk caveat.

### 4. LangChain
**Status: No canonical machine-readable RSS — Ghost blog migrated to Webflow with no feed.**

`blog.langchain.dev` → redirects 301 to `blog.langchain.com` → redirects 301 to `langchain.com/blog/rss` → returns HTML, not XML. LangChain's blog is now a Webflow site with no RSS endpoint.

**Resolution**: The Olshansk/rss-feeds project lists `feed_langchain.xml` as available; verify the URL (`https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_langchain.xml`) and probe before Phase 2. If it holds, use it. The blog is actively publishing (items from May 21–20, 2026). LangChain's changelog is at `changelog.langchain.com` but no RSS endpoint found there either.

**TODO for Phase 2**: Probe the Olshansk LangChain feed URL. If it works, enable with trust weight 3.

### 5. LlamaIndex
**Status: Medium feed is live but stale (last post: March 2024). Primary blog moved to llamaindex.ai/blog with no RSS.**

`https://medium.com/feed/llamaindex-blog` is a valid RSS feed but the most recent post is March 2024 — the blog migrated to `llamaindex.ai/blog`. That Webflow-built site has no discoverable RSS endpoint (no feed link in HTML head, no `/feed` or `/rss.xml` working).

**Resolution**: Medium feed is technically valid but stale. The better source is the primary blog at `llamaindex.ai/blog`. Check Olshansk/rss-feeds for a LlamaIndex feed; it was not listed as of research date (May 2026). **Recommended action**: open an issue in Olshansk/rss-feeds or use the Medium feed with the understanding it reflects pre-2024 content. The primary blog publishes newsletters weekly (LlamaIndex Newsletter 2026-04-21 confirmed) — this is a real gap.

**TODO**: Request a LlamaIndex feed in Olshansk/rss-feeds, or flag to Architect for a dedicated scraper (same approval path as Anthropic).

### 6. The Batch (deeplearning.ai)
**Status: No official feed — community workaround found and verified.**

`deeplearning.ai/the-batch/feed/` returns 404. No feed link in page HTML. Email-first distribution.

**Resolution**: Use `https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_the_batch.xml` (Olshansk/rss-feeds, hourly). Confirmed live 2026-05-23; most recent item: May 22, 2026 (weekly issue). Active. Same dependency-risk caveat.

### 7. Ars Technica AI
**Status: Feed URL structurally valid; confirmed in multiple 2026 RSS directories. WebFetch blocked from this research environment.**

`arstechnica.com/ai/feed/` is listed as working in daige.st (May 2026) and feedspot. WebFetch from this research environment returns a connection error (likely anti-scrape on the research IP, not on a server-side pipeline). The canonical URL `feeds.arstechnica.com/arstechnica/technology-lab` is also blocked from this environment.

**Resolution**: Enable with URL `https://arstechnica.com/ai/feed/` and trust weight 3. Re-probe from the Actions runner in Phase 2. Note: Ars Technica provides excerpt-only RSS; full articles behind paywall (acceptable for our use-case, we summarise).

### 8. The Verge AI
**Status: Feed URL confirmed active in 2026 directories. WebFetch blocked from this research environment.**

`theverge.com/ai-artificial-intelligence/rss/index.xml` is listed as active in 2026 RSS guides and tools. WebFetch blocked from research environment (same anti-scrape reason as Ars Technica).

**Resolution**: Enable with URL `https://www.theverge.com/ai-artificial-intelligence/rss/index.xml` and trust weight 2. Re-probe from pipeline environment in Phase 2. Trust weight 2 reflects Tier-3 risk on consumer-AI hype coverage.

---

## Finance-AI Sources

*Researched 2026-05-23. Evaluated against: (1) editorial-focus tier filter (must be Tier-1 or load-bearing Tier-2) AND (2) finance-lens sourcing-signal criteria (publishes at least weekly, covers Tier-1 finance territory, cites primary work not press releases).*

---

### Finance-AI P1 — Must Have

#### 1. LLMQuant Newsletter
- **URL**: `https://llmquant.substack.com/feed`
- **Type**: RSS (Substack)
- **What they publish**: AI + quantitative finance practitioner content. Recent items: "When Should an AI Hand the Wheel to Another AI?" (May 22), "How a New AI Framework Is Quietly Rewriting Portfolio Management" (May 20), "Europe's Quiet Plan to Put AI Agents Inside the Banking Vault" (May 18). Open-source community focus.
- **Frequency**: 2–3 per week.
- **Finance-lens pass**: Tier-1 territory — trading/portfolio ML, agents-in-finance, AML/banking. Cites primary work. Not press releases.
- **Tier-3 risk**: Some posts are lighter explainers aimed at beginners. Watch items_kept/items_in ratio.
- **Trust weight**: 2 (new — earn toward 4 over months).
- **Confirmed live**: 2026-05-23 (most recent: May 22, 2026).

#### 2. BIS Research Papers (FSI)
- **URL**: `https://www.bis.org/doclist/bis_fsi_publs.rss`
- **Type**: RSS 1.0 (RDF)
- **What they publish**: Bank for International Settlements financial stability papers, working papers, BIS bulletins. AI items observed: "In data we trust? How supervisors approach AI data use in financial services," "The geography of AI firms." Not AI-exclusive — economics, regulation, fintech mixed in.
- **Frequency**: Daily (varied topics; AI/fintech items maybe weekly).
- **Finance-lens pass**: Load-bearing Tier-2 territory — model risk, fintech regulation, systemic AI risk. Primary research, not recycled content. High credibility.
- **Tier-3 risk**: Most items are macroeconomics (irrelevant). LLM Engineer must filter aggressively by title/keywords.
- **Trust weight**: 3 when an AI item surfaces (credibility is high); 1 in aggregate volume terms.
- **Confirmed live**: 2026-05-23 (most recent: May 21, 2026).

#### 3. Risk.net Cutting Edge
- **URL**: `http://www.risk.net/feeds/rss/category/cutting-edge`
- **Type**: RSS 2.0
- **What they publish**: Peer-reviewed quantitative finance papers. Recent items include agentic AI governance, options pricing models, market microstructure. Free titles/abstracts; full content paywalled.
- **Frequency**: Multiple per week.
- **Finance-lens pass**: Tier-1 territory — trading ML (volatility, options), model risk governance, quantitative methods. Cites primary academic work.
- **Tier-3 risk**: Low. Peer-reviewed filter removes press releases.
- **Trust weight**: 3 (paywalled articles reduce utility — titles and abstracts only in pipeline).
- **Paywall note**: Full articles require Risk.net subscription. The pipeline gets titles and short abstracts. Sufficient for editorial awareness; the LLM Engineer should note this in summaries.
- **Confirmed live**: 2026-05-23 (latest: May 20, 2026).
- **Arman TODO**: Is a Risk.net subscription in scope to enable full-text fetch? (See TODO section.)

---

### Finance-AI P2 — Strong Supporting

#### 4. ML Quant Finance (Dr. Derek Snow)
- **URL**: `https://blog.ml-quant.com/feed`
- **Type**: RSS 2.0
- **What they publish**: Weekly "Quant Letter" — curated links on quantitative ML and finance topics. Recent: "Quant Letter: May 2026, Week-3" (May 20). 30+ subscribers base; practitioner-level.
- **Frequency**: Weekly.
- **Finance-lens pass**: Tier-1 territory — trading, portfolio ML, signal generation. Link-curation format means high signal density when the curator is on point.
- **Tier-3 risk**: Curation quality depends on single author judgment. Trust weight 2 until baseline established.
- **Trust weight**: 2.
- **Confirmed live**: 2026-05-23.

#### 5. AI in Finance (Christophe Atten)
- **URL**: `https://aiinfinance.substack.com/feed`
- **Type**: RSS 2.0 (Substack)
- **What they publish**: LLM deployment in FS, EU AI Act compliance, practical AI governance in financial institutions. Recent items: "EU AI Act for Finance Teams: What You Actually Need to Do" (May 20), "Deploy an LLM to 1800 Employees — Here's What Actually Happened" (May 17).
- **Frequency**: 2–4 per week.
- **Finance-lens pass**: Tier-1 territory — productionising under regs, model governance, agents-in-finance. Cites primary regulatory documents.
- **Tier-3 risk**: Some posts lean toward "leadership" thought-pieces without practitioner specifics. Watch.
- **Trust weight**: 2.
- **Confirmed live**: 2026-05-23 (latest: May 20, 2026).

#### 6. Numerai Blog
- **URL**: `https://blog.numer.ai/rss/`
- **Type**: RSS 2.0
- **What they publish**: Monthly updates on the Numerai quant ML tournament, ML competition results, open-source tooling for financial ML. Recent: "Numerai Monthly: UX Improvements, Atomic Staking Progress, Agentic Adoption, CoE Updates" (May 5, 2026).
- **Frequency**: Monthly.
- **Finance-lens pass**: Tier-1/Load-bearing Tier-2 — trading ML in practice, real ML competition outcomes, signal generation. Primary source (the tournament IS the data).
- **Tier-3 risk**: Low within its domain. Monthly cadence means it's supplemental not daily.
- **Trust weight**: 2.
- **Confirmed live**: 2026-05-23.

---

### Finance-AI P3 — Watchlist

| # | Name | URL | Why watch | Tier-3 risk |
|---|------|-----|-----------|-------------|
| 47 | Gradient Flow (Ben Lorica) | `https://gradientflow.substack.com/feed` | Covers AI patterns in finance (e.g. "Emerging AI patterns in finance: what to watch in 2026"). Weekly. | Not FS-exclusive — general AI sometimes. Trust weight 2. |
| 48 | GARP Risk Intelligence | No feed confirmed | Articles on SR 26-2, agentic AI in risk management, model risk. Excellent signal IF they have a feed. | No machine-readable feed found. Must check manually. |
| 49 | American Banker AI | `americanbanker.com/feed` (returns HTML, not RSS) | Dedicated AI section (`americanbanker.com/artificial-intelligence`). Real banking-AI news. | No working RSS endpoint found. Paywalled. |
| 50 | BIS Central Bank Speeches | `https://www.bis.org/doclist/cbspeeches.rss` | Central bankers on AI risk and monetary policy + AI. Very low signal density but high credibility items. | Vast majority not AI. Noise floor extremely high. |

---

### Finance-AI: Researched But Ruled Out

| Source | Reason |
|--------|--------|
| **JP Morgan AI Research** | No RSS. Publications are quarterly/sporadic. Low daily volume. |
| **Goldman Sachs / Two Sigma engineering blogs** | Two Sigma feed exists but is empty. Goldman has no public engineering blog with RSS. |
| **Bank of England / FCA** | BoE RSS page blocked (403); FCA same. Irregular cadence. Tier-2 at best. Not daily-volume sources. |
| **Arpitrage (Arpit Gupta)** | NYU Stern AI in Finance course summaries. Academic, slow cadence. Tier-2 max. |
| **Harbourfrontquant** | Trading risk + AI. Slow cadence, unconfirmed feed. Watchlist only. |
| **FT Alphaville** | Paywalled with no confirmed free RSS for the AI/tech beat. |
| **American Banker** | Feed URL returns HTML, not RSS. Paywalled. Would need dedicated scraper (Architect approval required). |
| **Domino Data Lab Blog** | No working RSS endpoint found. |
| **ValidMind Blog** | No working RSS endpoint found (404 on `/rss.xml`). |

---

## Finance-AI deepening (2026-05-24)

*Follow-up research pass targeting 8–10 new finance-AI sources. 20+ candidates probed via WebFetch. Research by Source Engineer.*

---

### What was researched this round

Six categories were systematically probed:

1. **FS-firm engineering / research blogs**: JP Morgan, Two Sigma, Goldman Sachs, Capital One, Klarna, Stripe, Jane Street, AQR, Bridgewater.
2. **Central-bank and regulator research**: US Federal Reserve (FEDS, IFDP, Liberty Street), FRBNY, ECB Working Papers, Bank of England / Bank Underground, FCA Insight, OCC, BIS Working Papers, BIS Quarterly Review, RBA Bulletin, IMF, FSB, Federal Reserve (various regional).
3. **Practitioner writers / specialist commentary**: Net Interest (Marc Rubinstein), Quantocracy, Risk.net additional sections (technology, market-risk, credit-risk, op-risk, derivatives, structured-products), GARP Risk Intelligence, FinRegLab, The Financial Revolutionist.
4. **Academic / research centres in FS+ML**: Oxford-Man Institute, Stanford HAI, AQR research (no feed found).
5. **FS-AI industry orgs / events**: NIST AI RMF, AI for Good (ITU), Bank Policy Institute, Financial Stability Board.
6. **Other practitioner/FS sources**: Accenture Banking Blog, Fintech.Global, IMF Blogs.

**Key pattern observed**: Central-bank research is almost exclusively PDF-distributed; only ~3 of the institutions probed have live RSS feeds with recent items. Many regulator sites (BoE publications, FCA, OCC, IMF, Fed regional banks) block WebFetch with 403 or redirect to homepage. The exception is the FRBNY Liberty Street Economics blog (live, RSS 2.0) and the Fed FEDS series (live, RSS 2.0), and the Bank Underground category feed (live, confirmed AI-relevant posts).

---

### Per-candidate verdicts

#### FS-firm engineering blogs

| Candidate | Feed URL | Verdict | Reason |
|-----------|----------|---------|--------|
| JP Morgan AI Research | None confirmed | Excluded | No RSS on public website; researched previously. |
| Two Sigma Engineering | engineering.twosigma.com (ECONNREFUSED) | Excluded | Connection refused; confirmed dead from prior round. |
| Goldman Sachs engineering | None | Excluded | No public engineering blog with RSS. |
| Capital One Tech Blog | `https://medium.com/feed/capital-one-tech` | **P1 (enabled)** | Live RSS via Medium. ~60% AI/ML, FS-firm. "Insights from inaugural Capital One AI Symposium" April 2026. NLP at ICLR 2026. Trust weight 2. |
| Klarna engineering | engineering.klarna.com (SSL error) | Excluded | SSL cert error; no confirmed feed. Blog may exist but not reachable. |
| Stripe engineering | stripe.com/blog/feed/rss (404) | Excluded | Confirmed 404 — same result as prior round. Engineering content but not AI-primary. |
| Jane Street tech blog | No feed (404 on all variants) | **P2 (disabled)** | Blog exists but zero RSS endpoint found. High-value if feed appears. |
| AQR Capital | No feed (all variants 404/HTML) | **P2 (disabled)** | No RSS found despite active website. High-quality research. Arman's call on scraper. |
| Accenture Banking Blog | `https://bankingblog.accenture.com/feed` | **P1 (enabled)** | Live RSS, 10 items, ~80% AI/ML, agentic AI heavy. Vendor Tier-3 risk; trust weight 2. |

#### Central-bank and regulator research

| Candidate | Feed URL | Verdict | Reason |
|-----------|----------|---------|--------|
| FRBNY Liberty Street Economics | `https://libertystreeteconomics.newyorkfed.org/feed/` | **P1 (enabled)** | Live RSS 2.0, recent AI macroeconomics posts. Fed credibility. Trust weight 3. |
| Fed FEDS Working Papers | `https://www.federalreserve.gov/feeds/feds.xml` | **P1 (enabled)** | Live RSS 2.0, 16 items, LLM validation papers confirmed. Trust weight 3. |
| Fed FEDS Notes (newyorkfed.org/libstr) | 403 blocked | Excluded | Blocked in research environment. |
| Bank Underground (BoE AI feed) | `https://www.bankunderground.co.uk/category/artificial-intelligence/feed/` | **P1 (enabled)** | Live RSS 2.0, 5 AI items, most recent May 21 2026 (agentic commerce + payments). Exceptional signal density. Trust weight 3. |
| Bank of England publications | 403 blocked | Excluded | BoE root RSS blocked; Bank Underground AI feed covers the signal. |
| ECB Working Papers | Multiple URL variants — all 404 | Excluded | No working RSS endpoint found for ECB research. PDF-only distribution. |
| FCA publications | Redirects to blocked endpoint | Excluded | FCA news RSS redirects to HTTP but 403 block; researched previously. |
| BIS Quarterly Review | `https://www.bis.org/doclist/quarterlyreviews.rss` | **P2 (enabled, low-freq)** | Live RSS 1.0 (RDF), quarterly. AI appears when relevant. Trust weight 2. |
| BIS Working Papers | Already in list as `bis_fsi_publs.rss` | Existing | Scope check: FSI feed confirmed to cover AI papers. No new URL needed. |
| RBA Bulletin | rba.gov.au/rss variants — all 404 | Excluded | No confirmed RSS feed endpoint. |
| OCC | Redirects to homepage | Excluded | No confirmed RSS; redirect to occ.treas.gov homepage. |
| IMF publications | 403 blocked | Excluded | IMF blocks WebFetch; feed exists but inaccessible from research env. |
| FSB | `https://www.fsb.org/feed/` — live but no AI items | Excluded | Valid RSS but recent items are meeting summaries, not AI/ML content. |
| Bank Policy Institute | `https://bpi.com/feed/` — live, no AI focus | Excluded | BPI covers AML/BSA compliance; AI is peripheral, not primary. |

#### Practitioner writers / specialist commentary

| Candidate | Feed URL | Verdict | Reason |
|-----------|----------|---------|--------|
| Net Interest (Marc Rubinstein) | `https://www.netinterest.co/feed` | **P2 (enabled)** | Live RSS, weekly. Finance + AI intersection. Not AI-primary but recurring sub-theme. Trust weight 2. Previously borderline P2; confirmed worth enabling. |
| Quantocracy | `http://feeds.feedburner.com/Quantocracy` | **P1 (enabled)** | Live RSS, weekly aggregator of quant ML links. Finance-lens Tier-1: trading ML, RL, signal generation. Trust weight 3. Surprising find — very consistent. |
| Risk.net technology | `http://www.risk.net/feeds/rss/category/technology` | Excluded | Last items: July 2025, May 2025. Too stale; AI content sparse. |
| Risk.net market-risk | `http://www.risk.net/feeds/rss/category/market-risk` | Excluded | Last items: Dec 2024, Nov 2023. Stale feed. |
| Risk.net credit-risk | `http://www.risk.net/feeds/rss/category/credit-risk` | Excluded | Last items: Mar 2026 vendor spotlights, Oct 2025. Sponsored content heavy; AI items are 2019-2021. |
| Risk.net op-risk | Category feed — empty (no items) | Excluded | Feed structure valid but zero items. |
| Risk.net derivatives | `http://www.risk.net/feeds/rss/category/derivatives` | Excluded | Items are Counterparty Radar / fund positioning data (not AI/ML). |
| Risk.net structured-products | Category feed — last items 2021–2024 | Excluded | Stale; not AI-relevant. |
| GARP Risk Intelligence | No feed found | Excluded | Email signup only; no RSS. Arman TODO from prior round still open. |
| FinRegLab | `https://finreglab.org/feed/` | Excluded | Live RSS but slow cadence; cash-flow data / small business focus, not AI-primary. |
| The Financial Revolutionist | Redirect then 404 | Excluded | thefr.com returns 404 on feed endpoint. |
| Fintech.Global main feed | `https://fintech.global/feed/` — live, mixed | Excluded | Valid RSS but funding announcements dominate; not practitioner AI/ML depth. |

#### Academic / research centres

| Candidate | Feed URL | Verdict | Reason |
|-----------|----------|---------|--------|
| Oxford-Man Institute | ECONNREFUSED | Excluded | No reachable feed. OMI has low publication cadence for newsletter use. |
| Stanford HAI | `https://hai.stanford.edu/news/rss.xml` — 404 | Excluded | Confirmed 404 (same as prior round attempt). |
| NYU / MIT / Cambridge | Not probed individually | Deferred | Academic centres typically PDF-only; low daily cadence. Out of scope for this round. |
| AQR Capital research | No feed (see FS-firm section) | Disabled | Research quality high but no feed endpoint. |

#### Industry orgs / events

| Candidate | Feed URL | Verdict | Reason |
|-----------|----------|---------|--------|
| NIST AI | `/feed` and `/rss.xml` — both 404 | Excluded | No working feed endpoint found. |
| AI for Good (ITU) | `https://aiforgood.itu.int/feed/` — live | Excluded | Valid RSS but focus is global development / robotics competitions, not FS. |
| FSB | See central-bank section | Excluded | No AI items in feed. |

---

### Verdict count

- **P1 (enabled)**: 6 — FRBNY Liberty Street Economics, Bank Underground AI, Fed FEDS Working Papers, Quantocracy, Capital One Tech Blog, Accenture Banking Blog
- **P2 (enabled, supplemental)**: 2 — Net Interest, BIS Quarterly Review
- **Disabled (no feed)**: 2 — AQR Capital Research, Jane Street Tech Blog
- **Excluded**: ~20 (stale feeds, no feeds, off-topic, blocked)

**Finance-AI total in sources.yaml after this round**: 16 entries (6 original + 8 enabled + 2 disabled)

---

### Patterns noticed

1. **Central-bank research is almost entirely PDF-distributed.** Of ~10 regulator/central-bank institutions probed, only 3 have usable RSS feeds (FRBNY Liberty Street, Fed FEDS, Bank Underground). The rest are 403-blocked, 404, or publish via email/PDF only.

2. **Risk.net additional categories are largely dead or stale.** Only "Cutting Edge" (already in list) is consistently active. All other Risk.net category feeds probed have either zero items, last items from 2021–2024, or sponsored content.

3. **Quantocracy is a sleeper find.** A well-established quant ML aggregator with 5+ years of consistent weekly publishing was not in the prior round. It provides exactly the cross-source signal the newsletter needs for quant ML stories, and it passes the finance-lens two-tier test cleanly.

4. **Bank Underground AI category feed is the highest-quality new find.** The BoE staff blog's AI-filtered feed publishes infrequently but every post is load-bearing: "Agentic commerce and payments infrastructure" (May 2026), "Could financial infrastructure govern AI agents?" (Sep 2025), "LLMs for prudential supervision" (May 2024). This is the exact finance-AI signal AI Vector is seeking.

5. **FS-firm engineering blogs mostly don't have RSS.** Stripe, Goldman, Two Sigma (dead), Klarna (SSL error), and Jane Street (no feed) all fail to provide machine-readable feeds. Only Capital One (Medium) and Accenture (WordPress) have working feeds. This is a systemic gap in the category.

---

## TODO — Gaps Arman Should Weigh In On

1. **Anthropic + Mistral + Cohere + The Batch feeds depend on Olshansk/rss-feeds (community project)**. If the maintainer stops, feeds go stale with no warning. Options: (a) accept the dependency and monitor monthly, (b) AI Vector maintains its own scrapers for these sources (requires Architect approval per PLAN §0.4). **Arman's call on risk appetite.**

2. **Risk.net subscription in scope?** The Cutting Edge feed provides titles and abstracts free. Full articles are paywalled. Subscribing unlocks full-text in the pipeline. Risk.net subscription is a few hundred USD/year. Does Arman want to pay for full-text access for the pipeline?

3. **LlamaIndex: no RSS for the primary blog (llamaindex.ai/blog)**. Medium feed is stale (2024). Options: (a) request a feed in Olshansk/rss-feeds, (b) flag for Architect approval of a dedicated scraper, (c) deprioritise LlamaIndex if LangChain covers the agentic-RAG beat adequately. **Arman's call on importance.**

4. **HN Algolia points threshold**: Current placeholder is 100. LLM Engineer should calibrate. Based on AI Vector's daily cadence and focus, 100 is probably right for broad coverage; 150+ risks missing mid-tier AI items on quiet days.

5. **Ars Technica and The Verge feeds**: marked enabled in yaml but only confirmed from RSS directories, not from a direct fetch. Phase 2 pipeline probe will confirm. If they block the Actions runner IP as well, either (a) use `feeds.arstechnica.com/arstechnica/index` (broader main feed), or (b) try a cached CDN variant.

6. **GARP Risk Intelligence**: Excellent finance-AI content but no confirmed RSS feed. If Arman values this source, someone should check `garp.org` directly or email their editorial team for a feed URL.

7. **vLLM blog**: `blog.vllm.ai/feed.xml` redirects to `vllm.ai/feed.xml` which 404s. The blog is at `blog.vllm.ai/` (Jekyll); the feed likely exists as `blog.vllm.ai/feed` or `/atom.xml`. Needs a direct probe from a non-blocked environment.

8. **arXiv firehose (cs.AI, cs.CL, cs.LG)**: Three feeds enabled with trust weight 1. These will dominate `items_in` counts but should have low `items_kept`. Recommend starting with cs.CL only (highest LLM relevance), disable cs.AI and cs.LG in the first month, then re-enable based on Eval data.

9. **Paid source policy generally**: If Arman wants the newsletter to have genuine depth on finance-AI, there is a reasonable case for a small paid-source budget (Risk.net, possibly FT Alphaville access). Should this be a standing budget item? Out of scope for Source Engineer to decide.

10. **AQR Capital Research (2026-05-24 deepening round)**: No RSS feed found. AQR publishes high-quality quantitative finance research (factor investing, ML in asset management) but distributes via website only. If Arman values this source, options: (a) email subscribe and pipe through a personal RSS bridge (e.g., Kill the Newsletter), (b) request Architect approval for a dedicated scraper. Very high content quality justifies the effort.

11. **Jane Street Tech Blog (2026-05-24 deepening round)**: No RSS feed endpoint found despite multiple attempts. Jane Street's engineering blog covers OCaml, trading systems, and inference infrastructure — Tier-1 for an FS-AI newsletter. Check `janestreet.com` HTML head tags directly in the pipeline environment (not research env) for a feed link. If still no feed, consider requesting Architect approval for a scraper, or checking Olshansk/rss-feeds periodically.

12. **Accenture Banking Blog (2026-05-24 deepening round)**: Enabled at trust weight 2 as a vendor blog. Eval Engineer should watch items_kept ratio from week 1 — if marketing-heavy posts dominate, trust weight should drop and/or disable. The current 80% AI/ML density may reflect a temporary editorial focus on agentic AI; confirm over 4 weeks of data.

13. **ECB Working Papers**: No working RSS endpoint confirmed after multiple attempts. ECB does have a research section at ecb.europa.eu but feed URLs tried all return 404. If Arman values ECB monetary policy + AI research, this would require either a direct URL discovery (check ECB press RSS pages) or a dedicated scraper.

14. **IMF Blogs and Working Papers**: IMF blocks WebFetch (403). IMF publishes AI-relevant research (FinTech Notes, Staff Discussion Notes on AI). The IMF blog at imf.org/en/Blogs also blocked. If Arman values IMF signal, probe from the pipeline Actions runner directly — institutional feeds sometimes allow known IP ranges.

---

## Practitioner expansion (2026-05-25)

*Eight candidates probed to broaden the practitioner / evals / criticism / inference-platform tapestry. Three feeds confirmed and enabled; five had no discoverable RSS at common paths.*

### Enabled (3)

| Name | Feed URL | Format | Why |
|------|----------|--------|-----|
| Hamel Husain | `https://hamel.dev/index.xml` | RSS 2.0 | Applied ML/LLM eval practitioner — error analysis, metrics design, evaluation systems. Trust weight 2. |
| Normal Technology (Narayanan/Kapoor) | `https://www.normaltech.ai/feed` | RSS 2.0 | Rebrand of "AI Snake Oil" (aisnakeoil.com/feed now 301-redirects). Critical/accountability lens; counterweight to vendor-PR feeds. Trust weight 2. |
| Vicki Boykis | `https://vickiboykis.com/index.xml` | RSS 2.0 | Production ML, embeddings, retrieval. Note: `/feed.xml` returns 404 — canonical is `/index.xml`. Trust weight 2. |

### Probed but no discoverable feed (5)

| Name | URLs probed | Verdict |
|------|-------------|---------|
| Maxime Labonne | `/blog/index.xml`, `/blog/atom.xml`, `/blog/feed.xml` | All 404. Blog may have moved or removed the feed; needs HTML head inspection from a non-WebFetch environment. |
| METR | `/blog/feed`, `/feed`, `/blog.atom` | All 404. Active org but no public feed endpoint discoverable via common paths. |
| Modal | `/blog/feed.xml`, `/blog/rss` | All 404. Blog is active (Webflow/Framer site); no `<link rel="alternate">` visible in WebFetch's body-only excerpt. Needs view-source from browser to confirm. |
| Baseten | `/blog/rss.xml`, `/blog/feed` | All 404. Active blog ("Sub-second image generation with Flux.2", "Baseten Loops"); same situation as Modal — head tags stripped by WebFetch. |
| Fireworks AI | `/blog/feed.xml`, `/rss.xml` | All 404. Active blog (last post May 5, 2026); same situation — needs HTML head inspection. |

### Notes

- **Maxime Labonne, METR**: probably feed-less by design. Deprioritise unless a workaround surfaces.
- **Modal, Baseten, Fireworks**: marketing-CMS sites (Webflow/Framer-pattern). Pattern observed: blogs are active and obviously valuable, but feed discoverability requires `view-source` in a browser to inspect the `<link rel="alternate">` tag — WebFetch returns body-only markdown. **TODO for Arman**: 30-second browser check on each — open the blog, view-source, search for `rel="alternate"`. If a feed URL is in the head tag, paste it back and they go in as enabled.
- **Pattern for future practitioner rounds**: the Substack / GitHub-hosted-blog / Hugo / Jekyll feeds work first try. Webflow / Framer / Wix sites need browser HTML inspection — don't burn WebFetch probes guessing.

---

## Source council brainstorm (2026-05-25)

*Six-character council (Karpathy, Weng, Andreessen, Russell, Taleb, Rubinstein), each brainstorming from a distinct lens. Facilitated synthesis, ~25 URLs probed live, 16 added. Full brainstorm in `_scratch/source_council_2026-05-25.md`. Cross-mention signal (≥2 lenses) was a strong heuristic for ranking.*

### Enabled (16)

| Name | Feed URL | Section | Lens | Trust |
|------|----------|---------|------|-------|
| Bounded Regret (Jacob Steinhardt) | `https://bounded-regret.ghost.io/rss/` | newsletter | Weng | 3 |
| Bruce Schneier | `https://www.schneier.com/feed/atom/` | newsletter | Taleb | 3 |
| Krebs on Security | `https://krebsonsecurity.com/feed/` | newsletter | Taleb + Rubinstein adj | 3 |
| Andrew Gelman — Statistical Modeling | `https://statmodeling.stat.columbia.edu/feed/` | newsletter | Taleb | 2 |
| Cosma Shalizi — Three-Toed Sloth | `http://bactra.org/weblog/index.rss` | newsletter | Taleb | 3 |
| Gary Marcus — Marcus on AI | `https://garymarcus.substack.com/feed` | newsletter | Taleb | 2 |
| Artificial Fintelligence (Finbarr Timbers) | `https://www.artfintel.com/feed` | newsletter | Karpathy | 2 |
| Neel Nanda | `https://www.neelnanda.io/blog?format=rss` | research | Karpathy + Weng | 2 |
| AI Alignment Forum | `https://www.alignmentforum.org/feed.xml` | research | Weng | 2 |
| Redwood Research | `https://blog.redwoodresearch.org/feed` | research | Weng + Russell | 3 |
| Transformer Circuits Thread (Anthropic interp) | `https://transformer-circuits.pub/feed.xml` | research | Karpathy + Weng | 4 |
| AI Incident Database | `https://incidentdatabase.ai/rss.xml` | news | Taleb | 2 |
| Sequoia Capital — Perspectives | `https://www.sequoiacap.com/feed/` | news | Andreessen | 2 |
| FlashAttention releases (Tri Dao) | `https://github.com/Dao-AILab/flash-attention/releases.atom` | tooling | Karpathy | 4 |
| Vercel Changelog | `https://vercel.com/changelog/feed.xml` | tooling | Andreessen | 3 |
| Fintech Business Weekly (Jason Mikula) | `https://fintechbusinessweekly.substack.com/feed` | finance-ai | Rubinstein | 3 |

### Rejected — paywall (1)

| Name | Reason |
|------|--------|
| Sifted (European fintech) | `sifted.eu/feed` is live RSS but articles paywalled beyond standfirst. Per "no paywall" instruction for this round, excluded. (Compare to existing Risk.net Cutting Edge which is grandfathered as paywalled.) |

### Deferred — strong endorsement but no discoverable feed (~14)

Common-path probes exhausted without success. Likely require browser view-source on `<link rel="alternate">` to discover any feed URL the marketing-CMS site is hiding. **TODO for Arman**: 30-sec view-source check on each.

Priority five (strongest cross-mention or editorial fit): METR, Apollo Research, Modal Labs, a16z, Cursor Changelog.

Full list: METR, Apollo Research, Epoch AI, Modal Labs, a16z AI, Goodfire, Sasha Rush, Hazy Research, Owain Evans, Cursor Changelog, UK AI Security Institute, OECD.AI Policy Observatory, Patrick McKenzie (Bits about Money), MAS Singapore.

### Email-bridge candidates (~5, deferred for separate decision)

Kill-the-Newsletter setup required (~5 min per source, one-time):
- CAIS Newsletter (Hendrycks et al.)
- ECB Banking Supervision Newsletter
- Matt Levine — Money Stuff (Bloomberg)
- AQR Insights (already disabled in sources.yaml pending feed; this is the workaround)
- GARP Risk Intelligence (existing TODO)

### Patterns observed

1. **Cross-mention is a strong filter.** 10 candidates had ≥2 lenses endorsing them (METR, Apollo, Redwood, Epoch, Transformer Circuits, Neel Nanda, Modal, Stripe, NIST AI, CAIS). 4 of those 10 made it in (Redwood, Transformer Circuits via precedent, Neel Nanda by alternative URL). The other 6 are in the deferred list — exactly the candidates that justify the 30-sec view-source ask.
2. **The character lens that produced the most additions was Taleb** (5 of 16 — Schneier, Krebs, Gelman, Shalizi, Gary Marcus, AI Incident Database). Antifragile / skeptic / statistical-rigour sources are heavily under-represented in awesome-list aggregators; the character framing surfaced them.
3. **Karpathy contributed the highest-confidence single find** (FlashAttention releases, trust 4) but his lens has lower hit rate because most of what he wants ships through GitHub commits (noisy) rather than tagged releases (useful).
4. **Sources count** went 67 → 83 (77 enabled, 6 disabled). Adding 16 in a single round is the largest expansion since the initial 2026-05-23 round.

---

## Institutional sources round (2026-07-04)

*Follow-up to a wire-service scoping question. Reuters/AP/Bloomberg were rejected
(no usable free RSS, off-signal, wrong tool for the thin-Currents/single-source
problems). Arman then clarified the real intent: because AI Vector has a
financial-services lens, he wants credible institutional sources — named
McKinsey, KPMG, EY, BBC, NYTimes, ACM.org, "and others" — not general wires.
Hard constraint held: subscribe-only (official RSS/Atom/API), no scraping.
15 candidate URLs probed live via WebFetch.*

### Added (3)

| Name | Feed URL | Category | Trust weight | max_age_days | Why |
|------|----------|----------|---------------|---------------|-----|
| ACM Queue | `https://queue.acm.org/rss/feeds/queuecontent.xml` | research | 3 | 30 | Confirmed live, 15 items. Practitioner engineering essays under ACM's institutional banner; 4 of 5 most recent items AI-focused. Peer-edited technical writing, not thought-leadership — near-zero Tier-3 risk. Strongest institutional fit for the engineer half of the audience. |
| QuantumBlack (McKinsey AI) | `https://medium.com/feed/quantumblack` | newsletter | 3 | 30 | Confirmed live via Medium. McKinsey's AI arm; genuinely practitioner-grade ("Agent skills turn human expertise into AI advantage" Jun 4, 2026; "Generative AI workflows need engineering discipline to scale beyond the demo" May 7). Deliberately not the McKinsey Insights main feed — see rejected list below. |
| IEEE Spectrum AI | `https://spectrum.ieee.org/feeds/topic/artificial-intelligence.rss` | news | 3 | 7 | Confirmed live, 10 items through Jul 3, 2026. Engineering journalism with an infrastructure/hardware/energy angle no current source owns ("AI's Volatile Power Use Quietly Tests Grid Limits"). Sponsored-content watch flag: one recent item bylined "Melbourne Convention Bureau" — a sponsored placement inside the feed. |

**Trust weight rationale**: all three start at 3 — Arman's call, informed by the
1–4 scale. ACM was floated at 4 but deliberately started at 3, level with the
central-bank sources (FRBNY, Fed FEDS, Bank Underground), so a brand-new source
doesn't outrank primary labs before any observed `items_kept` history. Can earn
4 later through the normal trust-weight discipline.

**Pulse-eligibility consequence**: trust weight 3 meets the Pulse-eligibility
floor, so single-source stories from all three sources are Pulse-eligible from
day one. Arman accepted this explicitly to observe real ranking behaviour
rather than wait out a probation period. Eval Engineer should watch
consulting/institutional-origin share of ranked issues (QuantumBlack + ACM
Queue + IEEE Spectrum AI + the existing Accenture Banking Blog) — if
institutional thought-leadership starts displacing lab/tooling items from top
sections or from Pulse specifically, that's the signal to floor trust weights,
not a reason to tune the ranker around it.

### Rejected (with reason)

| Candidate | Feed(s) probed | Reason |
|-----------|-----------------|--------|
| McKinsey Insights (main feed) | `https://www.mckinsey.com/insights/rss` | Live RSS 2.0, ~60 items, but Tier-3-dominant marketing content ("From campaigns to continuous growth: AI capabilities shaping marketing", "From anxiety to advantage: A marketing organization that thrives with AI"). QuantumBlack (added above) is the practitioner-grade alternative from the same institution. |
| KPMG | `kpmg.com/us/en/insights.rss` (404), `kpmg.com/xx/en/home/insights.rss.xml` (404) | No feed at any probed variant. Subscribe-only rules it out. |
| EY | `ey.com/en_gl/newsroom/rss` (404) | No feed found. |
| PwC | `pwc.com/gx/en/rss.xml` (403) | No feed found. |
| Deloitte | `www2.deloitte.com/us/en/insights/rss.html` (302 → `www.deloitte.com/us/en/insights/rss.html`, 404) | Old RSS directory redirects to a dead page — RSS removed in a site migration. |
| BBC (Technology + AI topic feeds) | `feeds.bbci.co.uk/news/technology/rss.xml`, `feeds.bbci.co.uk/news/topics/ce1qrvleleqt/rss.xml` | Both live RSS 2.0. Quality journalism but general-audience framing (AI abuse-material warnings, Netflix AI-voice backlash) — redundant with the existing, already-crowded news tier (Ars Technica, MIT TR, The Verge, VentureBeat) and adds mainstream reach, not practitioner depth. |
| NYTimes | `rss.nytimes.com/services/xml/rss/nyt/Technology.xml`, `www.nytimes.com/svc/...` | Both blocked from this research environment (nytimes.com domains fully unreachable via WebFetch here). Even setting the probe failure aside: hard paywall limits the pipeline to headline + one-line summary, and NYT's ToS/robots.txt posture toward AI crawlers is explicitly hostile — a poor fit for the excerpt-fetch model even at the RSS layer. Recommend skip; if Arman wants it anyway, needs a runner-side probe plus an explicit ToS note in the source entry. |
| Knowledge at Wharton | `https://knowledge.wharton.upenn.edu/feed/` | Live RSS 2.0, but general business-school feed — healthcare marketing, World Cup analytics; AI/finance content ~40% at best. Noise floor too high. |
| The Gradient | `https://thegradient.pub/rss/` | Live but near-dormant — most recent item Feb 2026, prior item Jun 2025. Watchlist only. |

### Deferred — re-probe from the Actions runner (403 in this environment)

Same anti-scrape pattern seen elsewhere in this file (Ars Technica, The Verge,
Transformer Circuits Thread) — likely IP-level blocking of the research
environment, not server-side.

| Name | URL | Notes |
|------|-----|-------|
| Communications of the ACM | `https://cacm.acm.org/feed/` | 403 here; ACM Queue (added) already covers the practitioner-essay signal. Worth a runner probe — natural trust-2/3 add if live. |
| ACM TechNews | `https://technews.acm.org/rss.cfm` | 403 here. Lower priority — TechNews aggregates mainstream press, likely duplicates the existing news tier. |
| NBER working papers | `https://back.nber.org/rss/new.xml` | 403 here. Would be another economics firehose alongside Fed FEDS (already in config, aggressively filtered) — marginal even if it probes clean. |

### Pattern noticed

Consulting/institutional sources split cleanly into two groups: those that
distribute a *practitioner arm* separately from their marketing arm
(McKinsey/QuantumBlack, ACM/ACM Queue) clear the strong-signal filter; the
marketing-arm main feeds (McKinsey Insights) and the Big Four (no feeds at
all — RSS is not part of their lead-generation funnel) do not. This class does
not solve the thin-Currents/single-source-day problem from the original
wire-service question — these are slow-cadence credibility sources, not
volume sources.

### Addendum: IEEE-family follow-up probe (2026-07-04)

Arman asked whether other IEEE feeds — "IEEE data science and others" — were
worth adding alongside IEEE Spectrum AI. Widened the probe to the rest of the
IEEE family: other Spectrum topic feeds, the whole-site feed, IEEE Computer
Society magazines, and an IEEE Xplore journal feed as a spot-check.

| Candidate | Feed URL | Verdict | Reason |
|-----------|----------|---------|--------|
| IEEE Spectrum Computing | `https://spectrum.ieee.org/feeds/topic/computing.rss` | **Added — trust 2** | Live RSS 2.0, distinct coverage from the AI topic feed (bank-chief-scientist piece, AI-coding/"vibecoded malware" story, quantum policy). One article confirmed cross-tagged into both this feed and IEEE Spectrum AI — expected IEEE Spectrum behaviour, handled by exact-URL dedup, not a bug. Started one notch below IEEE Spectrum AI (trust 2 vs 3) since it's broader/noisier and unproven. |
| IEEE Spectrum Robotics | `https://spectrum.ieee.org/feeds/topic/robotics.rss` | Skipped | Live, but content is hardware/video-roundup coverage ("Video Friday" series, humanoid-marathon robots) — weak fit for the Agentic+GenAI-heavy lens. |
| IEEE Spectrum whole-site feed | `https://spectrum.ieee.org/rss/fulltext` | Skipped | Live, but a strict superset firehose — carries zero-signal career-development content ("Why Public Speaking Skills Are Worth Investing In," IEEE museum history) on top of what the topic feeds already deliver. Topic-scoped is the right granularity; the site feed adds noise only. |
| IEEE Spectrum data-science / machine-learning topic slugs | `.../feeds/topic/data-science.rss`, `.../feeds/topic/machine-learning.rss` | Not found | Both 404 — IEEE Spectrum doesn't segment that granularly. `computing.rss` is the closest existing bucket. |
| IEEE Computer Society magazines (Software, Computer, Intelligent Systems) | `computer.org/rss/software-rss.xml`, `computer.org/csdl/rss/magazine/so`, `computer.org/rss-feeds` | Skipped — no feeds exist | All probes returned 404 or a bare error page with no feed links anywhere in site navigation. Distribution is CSDL-gated (paywalled digital library), not RSS-first — same pattern as the Big Four. |
| IEEE Xplore journal feeds (e.g. TPAMI) | `ieeexplore.ieee.org/rss/TOC34.XML` | Skipped | Returned HTTP 418 ("I'm a Teapot") — deliberate anti-bot block. Confirms the prior: academic table-of-contents firehose, heavily redundant with the arXiv cs.CL/cs.AI/cs.LG feeds already in the config at trust weight 1. Not worth pursuing further. |

**Net result**: one feed added (IEEE Spectrum Computing, trust 2), not three —
the IEEE door widened by exactly the amount the signal justified.

---

## Discovery round (2026-07-04): directory mining

Different method from every prior round. Instead of evaluating individually-named
candidates, this round mined feed *directories* systematically — the motivation
being that ACM Queue and IEEE Spectrum only got added because Arman happened to
name them; the goal was to find the channel that surfaces such feeds proactively.

### Directories mined

1. **kilimchoi/engineering-blogs** (`engineering_blogs.opml`, not just the README —
   the OPML has the live feed URLs the README table omits). Searched two slices:
   AI-lab/tooling companies, and financial-services engineering blogs by name
   (JPMorgan, Goldman Sachs, Bloomberg, Two Sigma, Jane Street, Man Group, Stripe,
   Ramp, Nubank, Monzo, Wise, Robinhood, Coinbase, Block/Square, Plaid, Adyen).
2. **plenaryapp/awesome-rss-feeds** — README Directory + Recommended Sources
   (Programming, Startups, Tech sections). No dedicated AI section; general
   consumer-tech snapshot circa 2020.
3. **tuan3w/awesome-tech-rss** — Machine Learning and Engineering-blogs sections.
4. **Feedspot** public directory pages — AI RSS feeds, Fintech RSS feeds.

### Yield ranking (for the next quarterly re-mine)

1. **kilimchoi/engineering-blogs OPML — best yield.** This is where the two
   highest-value finds came from: the correct live URL for Jane Street
   (`blogs.janestreet.com/feed.xml`, fixing a stale disabled entry) and the correct
   live URL for Stripe (`stripe.com/blog/feed.rss` — the earlier `/feed/rss` variant
   404'd and had been rejected). Re-mine this one quarterly; grep the FS-firm names
   first, then the AI-tooling names.
2. **Feedspot fintech directory — medium yield.** One strong lead (Bank Automation
   News); the other ~75 entries are vendor marketing, funding-announcement blogs,
   or newsletter-farm content not worth individual probing.
3. **tuan3w/awesome-tech-rss — low-medium yield.** Surfaced ML@CMU; otherwise a
   2021-vintage snapshot (Distill, Magenta, old Google AI Blog URLs — mostly dead
   or superseded by entries already in this config).
4. **plenaryapp/awesome-rss-feeds — low yield.** No AI-specific section at all;
   indirect leads only (InfoQ, GitHub Blog — both of which paid off via their
   *category-scoped* AI feeds, not the general feeds listed in the directory).
   Not worth a dedicated quarterly pull; the Programming/Tech sections are
   consumer/general-audience.
5. **Feedspot AI directory — skip.** Content-farm tier (AI Weekly, AIwire,
   insideAI News, Unite.AI, AI ASPIRANT) — none cleared editorial-focus on
   inspection. Do not re-mine.

**Recommendation**: make kilimchoi/engineering-blogs a standing quarterly ritual
(FS-firm names first, then AI-tooling names); treat the others as occasional,
lower-priority pulls.

### Adds (9) — all URL-probed live 2026-07-04

| Source | Feed URL | Category | Trust | Rationale |
|--------|----------|----------|-------|-----------|
| Ramp Engineering | `https://engineering.ramp.com/feed.xml` | finance-ai | 2 | Fintech engineering blog writing directly about agentic AI in production risk operations ("Agentic Risk Operations", Jun 30, 2026). Passes both lenses outright. Best single find of the round. |
| Stripe Blog | `https://stripe.com/blog/feed.rss` | finance-ai | 2 | Corrects a prior-round 404 rejection (wrong URL variant probed). AI-in-payments signal ("What Link data tells us about AI spending", "Stripe Projects adds new agent integrations") mixed with vertical-marketing posts — watch items_kept. |
| Building Nubank | `https://building.nubank.com.br/feed/` | finance-ai | 2 | Large digital bank's engineering blog; "How Nubank uses causality, machine learning and Python to support credit limit increase decisions" is exactly the ML-in-a-bank-at-scale gap. Mixed with non-AI infra posts. |
| GitHub Blog — AI & ML | `https://github.blog/ai-and-ml/feed/` | tooling | 3 | Category-scoped (not the GitHub Blog firehose). Primary-vendor agent-tooling signal (Copilot agentic harness evals) not otherwise carried. Trust 3 = same institutional trade as ACM Queue/IEEE Spectrum AI — Pulse-eligible from day one, deliberately. |
| NVIDIA Technical Blog | `https://developer.nvidia.com/blog/feed` | tooling | 2 | High-volume vendor blog; genuine Tier-1 (agentic RL, inference, hardware security) diluted by vendor-tutorial/event volume. Eval to watch items_kept ratio. |
| InfoQ AI/ML & Data Eng | `https://feed.infoq.com/ai-ml-data-eng/` | news | 2 | Category-scoped practitioner engineering journalism; meaningful data-eng-only (no AI angle) content mixed in. |
| Databricks Blog | `https://www.databricks.com/feed` | tooling | 2 | Enterprise AI platform, heavy FS customer base. Explicit Eval watch: same consulting-origin/thought-leadership risk pattern as QuantumBlack/Accenture — floor further if it displaces technical content. |
| ML@CMU Blog | `https://blog.ml.cmu.edu/feed/` | research | 2 | Academic practitioner blog, BAIR-analogous; monthly-ish cadence, strong on eval/benchmarking methodology. |
| Bank Automation News | `https://bankautomationnews.com/feed/` | finance-ai | 2 | Trade press specifically on AI/automation inside banks — strong content fit. **Not independently confirmed live**: 403 from the research sandbox on both curl and WebFetch, matching the exact bot-block pattern already documented for Ars Technica AI and The Verge AI (both of which work from the production Actions runner). Enabled on that precedent as a bet, not a confirmed probe — watch `source_health.json` closely; disable if `fired` stays false for several days running rather than assuming it's a sandbox artifact indefinitely. |

### Fix (1)

| Source | Change | Reason |
|--------|--------|--------|
| Jane Street Tech Blog | URL swapped from `janestreet.com/tech-blog/` (404, disabled) to `blogs.janestreet.com/feed.xml` (confirmed live); `enabled: false → true`; `trust_weight: 3 → 2`; `tier_expectation: tier-1 → tier-1-mixed` | The 2026-05-24 round only probed canonical `janestreet.com/tech-blog/*` variants; the working feed lives on the Ghost backend. On inspection, content is mostly OCaml/systems/formal-methods with low AI density — the original trust weight of 3 was set aspirationally before the feed was confirmed live and its actual content reviewed. Downgraded to reflect the real tier mix; Eval Engineer to expect a high items_kept drop rate. |

### Skipped

| Candidate | Reason |
|-----------|--------|
| Finextra headlines | `https://www.finextra.com/rss/headlines.aspx` — live, daily, but a general fintech news firehose (banking layoffs, tokenisation policy, payments-industry business news). AI is a sub-theme, not the beat. Passed on; heavy filter load for thin AI yield. |

### Dead ends (from the FS-firm engineering sweep)

| Candidate | Finding |
|-----------|---------|
| Bloomberg engineering (Tech at Bloomberg) | `techatbloomberg.com/feed/` returns valid XML but frozen at Feb 2022 (last item). The live site 302-redirects to `bloomberg.com/company/values/tech-at-bloomberg/` — a blocked domain, and there is no evidence of an active engineering-blog feed distinct from the newswire. No live Bloomberg engineering feed exists. |
| Two Sigma | `twosigma.com/feed/` re-confirmed as an empty RSS channel (valid XML, zero `<item>` elements) — same dead result as the prior round. |
| PayPal | Medium feed (`medium.com/feed/paypal-tech`) is live but its most recent entry announces the blog moved to `developer.paypal.com/community/blog` — which serves HTML with no discoverable feed. Frustrating: "PayPal Releases Agentic Toolkit to Accelerate Commerce" is exactly our signal, but genuinely no machine-readable feed exists post-migration. Candidate for a future community-feed request (Olshansk-style), not scraping. |
| Coinbase | `coinbase.com/blog/rss.xml` returns 403 (bot-block); no alternate feed endpoint found. |
| Plaid | `plaid.com/blog/feed/` 404; no discoverable feed. |
| Monzo | `monzo.com/blog/rss` 404; no discoverable feed. |
| Wise (Transferwise) | `medium.com/feed/wise-engineering` live but stale — most recent item Feb 2025. Not daily-signal viable. |
| Robinhood | `medium.com/feed/robinhood-engineering` live but stale — most recent item Jun 2023. Migrated off Medium with no successor feed found. |
| Adyen | `medium.com/feed/adyen` live, most recent Sep 2025, but content is non-AI (database partitioning, API tooling) — no AI angle observed across recent items. |
| Square | `developer.squareup.com/blog/rss.xml` live but sporadic (gaps of months); `corner.squareup.com/blog/rss.xml` returns non-XML. Not pursued further. |
| Wealthfront | `eng.wealthfront.com/feed/` live, active, but zero AI content in recent items (FIX trading infra, MariaDB, Android build tooling) — engineering-quality but off-lens. |
| Man Group | No feed endpoint found (`man.com/rss/insights.xml` 404); no further variant probed. |

### Fetch hardening ladder (scoping note only — no code changes)

For sources that 403 in production (not just in this research sandbox), propose a
graduated response, in order, before disabling:

1. **Re-probe from the real Actions runner first.** Several apparent 403s in this
   file (Ars Technica, The Verge, Transformer Circuits Thread, and now Bank
   Automation News in this round) are IP-level blocks on the research sandbox, not
   server-side blocks — they may fetch cleanly from the production runner's IP.
   Confirm before assuming a feed is dead.
2. **Proper feed-reader User-Agent + conditional GET in `src/fetch.py`.** Some
   403s are basic bot-detection keyed off a missing/generic User-Agent string or
   the absence of `If-Modified-Since`/`If-None-Match` conditional-GET headers.
   Sending a real feed-reader UA and honouring 304s is squarely within
   subscribe-don't-scrape — it's still fetching the same official RSS/Atom
   endpoint, just politely. No Architect approval needed for this step.
3. **`curl_cffi` browser-TLS impersonation, per-source, official-feeds-only, as a
   last resort.** For a feed confirmed to be an *official* public RSS/Atom
   endpoint (not paywalled content, not HTML scraping) that still 403s after step
   2 — likely TLS/JA3 fingerprinting rather than UA-string bot detection —
   browser-TLS impersonation is a judgment call, not a default. This is a
   per-source exception requiring Arman's explicit sign-off before enabling,
   logged in that source's `notes:` field with the date and reasoning. It must
   never be used to reach paywalled content or to scrape non-feed HTML — that
   remains a hard no per PLAN §0.4/§6, Architect-gated. Flagging as a future
   option, not proposing it for any current source.
