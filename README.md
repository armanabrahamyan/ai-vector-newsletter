# AI Vector

*Today's AI, with a heading.*

AI Vector is a daily AI newsletter with a financial-services lens. Curated, not aggregated. AI-drafted, human-ratified.

Heavy on Agentic AI and Generative AI — what shifts how readers work today, what to anticipate tomorrow, what's practical to use right now. Traditional ML appears only when load-bearing. Every story points somewhere: each carries a direction note and, where it earns one, an explicit financial-services angle.

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
| **On the Radar** | Short-form: worth watching, not yet acting on |

Each story carries a **signal pill** and a short editorial intro — both written fresh each day, ratified by Arman before publish.

---

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # fill in LLM_PROVIDER, LLM_ENDPOINT, LLM_API_KEY, LLM_MODEL
python -m src.run --check
```

The LLM defaults to `claude-sonnet-4-6` via the Anthropic API. Embeddings run locally (BAAI/bge-base-en-v1.5, cached under `~/.cache/huggingface/`). See `.env.example` for the full config reference.

---

## Daily flow

```bash
python -m src.run                           # fetch → cluster → rank → summarise → render (staging)
open docs/staging/<date>.html               # review the draft
python -m src.run --release                 # promote to released, assign issue number, rebuild index
python -m src.run --unrelease --date <date> # reverse a release if needed
```

### Granular stage control

```bash
python -m src.run --stage fetch             # one stage only
python -m src.run --stages fetch,cluster    # subset
python -m src.run --date 2026-05-23         # specific date (default: today)
python -m src.run --dry-run                 # print what would happen, write nothing
python -m src.run --skip-preflight          # skip LLM + embedding checks when iterating
python -m src.run --verbose                 # debug logging
```

