# GraderLLM

> **Disclaimer:** The architecture and design of this project are my own, but the code was written by AI.

> **AI should NEVER make decisions** This project was built ad hoc to help sort a very large number of submissions. The final decision must always be made by people.

An AI reviewer that reads written submissions (PDF), scores them against a rubric using an **ensemble of LLMs**, and produces a scored CSV and a ranked leaderboard. It was built for a data-privacy case study contest, but the rubric, reference text and case study are plain Markdown files, so it can be pointed at any written assessment.

The result is a shortlist with reasons, not a final verdict.

## How it works

```
submission/*.pdf
      │
pdf_reader.py ── text + tables as Markdown (+ optional figure descriptions)
      │
llm_engine.py ── request = rubric + reference text + case study + submission
      ├──► model A ─┐
      ├──► model B ─┼── average scores, measure spread, flag disagreements
      └──► model … ─┘
      │
output/assessment_result.csv ──► ranking.py ──► output/ranked_result.csv
```

## Features

- **Ensemble scoring:** several models score each submission in parallel; scores are averaged.
- **Disagreement flag:** large score gaps between models mark the row `REVIEW`.
- **Model feedback:** each model's strengths and summary are kept, tagged by model.
- **Any OpenAI-compatible provider:** Groq and Gemini built in.
- **PDF extraction:** text and tables as Markdown; optional vision descriptions of diagrams.
- **Resilient:** retries rate limits and network errors; fails fast on errors retrying can't fix.
- **Resumable:** saves after every submission; reruns skip scored ones.
- **Shardable:** split a batch across people, then merge.

## Scoring rubric

Defined in `prompt.md` and editable without code changes.

| Criterion | Points |
|---|---|
| Problem understanding | 20 |
| Privacy, AI risk & regulatory analysis | 25 |
| Recommendations & strategy | 45 |
| Communication & presentation | 10 |
| Penalties (ignoring case facts / poor structure) | up to −15 |
| Bonus: out-of-the-box thinking | up to +5 |

Total range: **0–105**.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp env.example .env              # add your API key(s)
python check_connection.py       # checks files, network, keys and models
```

Put PDFs in `submission/` named `<candidate_id>_<anything>.pdf`, then:

```bash
NUMBER_OF_ASSIGNMENT=3 python main.py    # small trial
python main.py                           # full run
```

## Project structure

| File | Role |
|---|---|
| `main.py` | Entry point: extract, score, write CSV, rank |
| `config.py` | Loads and validates `.env` settings |
| `providers.py` | OpenAI-compatible client (text + vision) |
| `llm_engine.py` | Builds requests, runs the ensemble, aggregates scores |
| `pdf_reader.py` | PDF → Markdown, optional figure descriptions |
| `evaluator.py` / `ranking.py` | Per-submission result row / leaderboard |
| `merge_shards.py` | Combines sharded results |
| `check_connection.py` / `diagnose.py` | Pre-flight and network troubleshooting |
| `prompt.md`, `PDPA.md`, `case_study.md` | Rubric and reference material sent with every request |

## Cost estimate

The cost of a run is not fixed. It depends on:

- **Number of submissions:** cost grows roughly in proportion.
- **Submission length:** the full text of each submission is sent to every model.
- **Pictures and diagrams:** with `PDF_IMAGE_HANDLING=describe`, each flagged page (up to 5 per PDF) costs one extra vision-model call.
- **Number and choice of models:** every model in `LLM_MODELS` scores every submission.
- **Retries and JSON corrections:** a failed or malformed reply can mean the request is sent again.
- **Reference files:** the rubric, PDPA text and case study (~12.5K tokens together) go with *every* request, so they are the biggest fixed cost. Trimming `PDPA.md` cuts it the most.

**Rough estimate** with the models used in this project, `gpt-oss-120b` ($0.15 / $0.60 per 1M input/output tokens) and `qwen3.8-27b` ($0.80 / $4.00), assuming ~15K input tokens (references + a typical submission) and ~1.5–2K output tokens per call:

| Submissions | Approx. cost (both models) |
|---|---|
| 1 | ~$0.02 |
| 100 | ~$2 |
| 500 | ~$11 |
| 1,000 | ~$22 |

About 85% of that is the Qwen model. Long submissions add about $0.005 per extra ~10 pages. Vision calls depend on the vision model's own pricing. These are estimates based on October 2026 prices; check your provider's current pricing and watch the usage dashboard during the first few dozen submissions.

## Configuration

Settings live in `.env` (template: `env.example`). Models used in this project:

```dotenv
LLM_MODELS=groq:openai/gpt-oss-120b,groq:qwen/qwen3.8-27b
```

| Setting | Default | Purpose |
|---|---|---|
| `LLM_MODELS` | see above | `<provider>:<model_id>` list |
| `GROQ_API_KEY` / `GEMINI_API_KEY` | — | Keys for providers in use |
| `MIN_SUCCESSFUL_MODELS` | `2` | Answers needed for a valid score |
| `MAX_TOKENS` | `2000` | Reply budget per call |
| `REASONING_EFFORT` | `low` | gpt-oss models only |
| `NUMBER_OF_ASSIGNMENT` | `500` | Max new submissions per run |
| `DISAGREEMENT_*_THRESHOLD` | `5` / `15` | Spread that triggers `REVIEW` |
| `PDF_IMAGE_HANDLING` | `off` | `describe` to read diagrams |

Each request is ~12–15K tokens, so each model's tokens-per-minute limit on your plan must be above that.

## Output

- `output/assessment_result.csv`: per-criterion scores, penalties, bonus, `total_score`, `models_used`, `disagreement_flag`, `disagreement_detail`, `strengths`, `summary`. Failed rows show `FAILED` with the reason.
- `output/ranked_result.csv`: the leaderboard, sorted by `total_score`.


## Author's comments

### On AI bias

LLM reviewers are not neutral. Biases worth knowing about when reading these scores:

- **Length and polish bias:** longer, more formally written submissions tend to score higher, even when the substance is similar.
- **Keyword matching:** submissions that echo the rubric's or the law's wording can be rewarded over ones that make the same point in plain language.
- **Shared blind spots:** models can agree with each other and still be wrong, so low disagreement does not always mean the score is right.
- **Missing visuals:** with figure handling off, diagrams and charts are invisible to the models, so visual-heavy submissions can be under-scored.
- **Language and style:** non-native English writing may be marked down under communication even when the analysis is strong.

### On the results

- **AI-written submissions scored highest.** Submissions written by AI, or with heavy AI help, were consistently ranked among the top. The total score alone should not be trusted to reward original thinking.
- **The bonus score was the most useful signal.** The *out-of-the-box thinking* bonus picked out relevant, original points of view. Sorting by `bonus_innovation` instead of `total_score` helped surface some genuinely great submissions.
- **Best at finding what's missing.** The reviewer was most reliable at spotting submissions with irrelevant content or missing key topics, which made it useful for filtering out weak submissions rather than picking the winners.
