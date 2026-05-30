# AI Vector Newsletter

[![Live](https://img.shields.io/badge/live-e6452f)](https://armanabrahamyan.github.io/ai-vector-newsletter/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

*Today's AI, with a heading.*

AI Vector is a daily AI newsletter with a financial-services lens. Curated, not aggregated. AI-drafted, human released.

Heavy on Agentic AI and Generative AI — what shifts how readers work today, what to anticipate tomorrow, what's practical to use right now. Traditional ML appears only when load-bearing. Every story points somewhere: each carries a direction note and, where it earns one, an explicit financial-services angle.

> **Built to fork.** The architecture is domain-agnostic. Swap `config/sources.yaml`, retune `config/rubric.yaml`, edit the voice in the summarise prompt, and you have *your* daily newsletter — for AI in healthcare, fintech security, climate tech, internal eng updates, anything. The pipeline (fetch → cluster → rank → summarise → render), the staging/released archive model, the templates, and the agent team are all reusable as-is.

**Author:** Arman Abrahamyan  
**Live:** https://armanabrahamyan.github.io/ai-vector-newsletter/

---

## The publication

Four sections per issue:

| Section | What it is |
|---|---|
| **The Pulse** | Story of the day — the one thing that matters |
| **The Big Picture** | Strategic context for leaders |
| **Hands-On** | Practitioners — what to build or try |
| **Currents** | Worth watching: the field is moving here, no action yet |

Each story carries a **signal pill** and a short editorial intro — both written fresh each day, ratified by a human before publish.

---

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .                 # installs the aiv command + all dependencies
cp .env.example .env             # fill in LLM_PROVIDER, LLM_ENDPOINT, LLM_API_KEY, LLM_MODEL
aiv check                        # pre-flight: embedding model cached + LLM reachable
```

The LLM defaults to `claude-sonnet-4-6` via the Anthropic API. Embeddings run locally (BAAI/bge-base-en-v1.5, cached under `~/.cache/huggingface/`). See `.env.example` for the full config reference.

---

## Daily flow

```bash
aiv run                          # fetch → cluster → rank → summarise → render (staging)
open docs/staging/<date>.html    # review the draft
aiv release                      # promote to released, assign issue number, rebuild index
aiv unrelease --date <date>      # reverse a release if needed
```

### Granular stage control

```bash
aiv run --stage fetch            # one stage only
aiv run --stages fetch,cluster   # subset
aiv run --date 2026-05-23        # specific date (default: today)
aiv run --dry-run                # print what would happen, write nothing
aiv run --skip-preflight         # skip LLM + embedding checks when iterating
aiv run --verbose                # debug logging
aiv --help                       # full command reference
```

There's more — see the **[Operator's Handbook](docs/HANDBOOK.md)** for re-run scenarios, debugging, what's safe to edit by hand, eval gating, and the publish-gate behaviour.

---

**Code** under the [MIT License](LICENSE) — fork it, build your own newsletter, no permission needed.
**Issue content** (the published newsletter text, headlines, summaries) is © Arman Abrahamyan — all rights reserved.

