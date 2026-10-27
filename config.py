"""Load configuration from environment variables (.env)."""

import os

from dotenv import load_dotenv

import truststore
truststore.inject_into_ssl()

load_dotenv()


def _env(*names: str, default: str = "") -> str:
    """First non-empty env var among `names` (lets old names keep working)."""
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _is_set(value) -> bool:
    return bool(value) and value.strip().lower() not in ("", "xxxx")


# ---------------------------------------------------------------------------
# LLM providers
# ---------------------------------------------------------------------------
# Every provider below speaks the OpenAI-compatible chat API, so one client
# code path serves all of them. A model is written as "<provider>:<model_id>",
# e.g. "groq:openai/gpt-oss-20b". With no recognised prefix, "groq" is assumed.
# To add another OpenAI-compatible provider, add one entry here.
PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "key_url": "https://console.groq.com/keys",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_env": "GEMINI_API_KEY",
        "key_url": "https://aistudio.google.com/apikey",
    },
}
DEFAULT_PROVIDER = "groq"


def parse_model_spec(spec: str) -> tuple:
    """'gemini:gemini-x' -> ('gemini', 'gemini-x'); 'openai/gpt-oss-20b' -> ('groq', ...)."""
    spec = spec.strip()
    prefix, sep, rest = spec.partition(":")
    if sep and prefix.lower() in PROVIDERS:
        return prefix.lower(), rest.strip()
    return DEFAULT_PROVIDER, spec


def base_url_for(provider: str) -> str:
    """Provider base URL; override with <PROVIDER>_BASE_URL (e.g. GROQ_BASE_URL)."""
    return _env(f"{provider.upper()}_BASE_URL", default=PROVIDERS[provider]["base_url"])


def api_key_for(provider: str) -> str:
    return os.getenv(PROVIDERS[provider]["key_env"], "")


# Models Groq has retired. Sending one returns "model_not_found", so we refuse
# to start instead of failing every submission. Source:
# https://console.groq.com/docs/deprecations
RETIRED_GROQ_MODELS = {
    "llama-3.1-8b-instant": ("2026-08-16", "openai/gpt-oss-20b"),
    "llama-3.3-70b-versatile": ("2026-08-16", "openai/gpt-oss-120b"),
    "qwen/qwen3-32b": ("2026-07-17", "openai/gpt-oss-120b"),
    "meta-llama/llama-4-scout-17b-16e-instruct": ("2026-07-17", "openai/gpt-oss-120b"),
    "moonshotai/kimi-k2-instruct-0905": ("2026-04-15", "openai/gpt-oss-120b"),
}


# --- Ensemble models ---
# Comma-separated "<provider>:<model_id>" list. Defaults are Groq's two
# production text models (as of the last docs check - always confirm with
# `python check_connection.py`, since Groq retires models regularly).
_DEFAULT_MODELS = "groq:openai/gpt-oss-20b,groq:groq:qwen/qwen3.8-27b"
LLM_MODELS = [
    m.strip()
    for m in _env("LLM_MODELS", "GROQ_MODELS", default=_DEFAULT_MODELS).split(",")
    if m.strip()
]

# A submission only counts as scored if at least this many models returned a
# usable JSON result.
MIN_SUCCESSFUL_MODELS = int(os.getenv("MIN_SUCCESSFUL_MODELS", "2"))

# Max number of models queried concurrently per submission.
LLM_MAX_CONCURRENT = int(_env("LLM_MAX_CONCURRENT", "GROQ_MAX_CONCURRENT", default="4"))

# Reasoning models (gpt-oss) think before answering, and those tokens are
# billed and count toward MAX_TOKENS. "low" keeps cost down and stops the
# reasoning from eating the whole budget before the JSON is written. Only
# sent to gpt-oss models. Set empty to disable.
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "low").strip()

# Per-request timeout in seconds.
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "180"))

if not LLM_MODELS:
    raise RuntimeError("LLM_MODELS is empty. Configure at least one model in .env.")


def _validate_models(specs: list, what: str) -> None:
    for spec in specs:
        provider, model_id = parse_model_spec(spec)
        if provider == "groq" and model_id in RETIRED_GROQ_MODELS:
            date, replacement = RETIRED_GROQ_MODELS[model_id]
            if what == "Vision model":
                # The suggested text replacement can't read images, so don't
                # point people at it for this setting.
                fix = (
                    "Groq currently has no production vision-capable model - point "
                    "VISION_MODEL at another provider's vision model (e.g. a Gemini "
                    "Flash-Lite model), or set PDF_IMAGE_HANDLING=off."
                )
            else:
                fix = f"Replace it in your .env with '{replacement}'."
            raise RuntimeError(
                f"{what} '{model_id}' was retired by Groq on {date} and will fail "
                f"with model_not_found. {fix} "
                f"(see https://console.groq.com/docs/deprecations)"
            )


def _validate_keys(specs: list) -> None:
    for provider in sorted({parse_model_spec(s)[0] for s in specs}):
        info = PROVIDERS[provider]
        if not _is_set(api_key_for(provider)):
            raise RuntimeError(
                f"{info['key_env']} is not set, but LLM_MODELS/VISION_MODEL uses the "
                f"'{provider}' provider. Add it to your .env file "
                f"(get one at {info['key_url']})."
            )


_validate_models(LLM_MODELS, "Model")


# Processing limit
NUMBER_OF_ASSIGNMENT = int(os.getenv("NUMBER_OF_ASSIGNMENT", "500"))

# --- Sharding (optional) ---
# Lets several people split one submission folder and run independently, each
# under their own API key/account. Leave SHARD_COUNT=1 to process everything
# in one run. See README "Splitting Work Across Multiple People".
SHARD_INDEX = int(os.getenv("SHARD_INDEX", "0"))
SHARD_COUNT = int(os.getenv("SHARD_COUNT", "1"))

if SHARD_COUNT < 1:
    raise RuntimeError("SHARD_COUNT must be >= 1.")
if not (0 <= SHARD_INDEX < SHARD_COUNT):
    raise RuntimeError(f"SHARD_INDEX must be between 0 and SHARD_COUNT-1 ({SHARD_COUNT - 1}).")

# Model parameters
TEMPERATURE = float(os.getenv("TEMPERATURE", "0"))
# Ceiling on the model's *reply* (reasoning + JSON), not on the prompt. Too
# low and a reasoning model can run out before writing the JSON.
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "2000"))

# Paths
SUBMISSION_DIR = "submission"
OUTPUT_DIR = "output"
MARKDOWN_DIR = os.getenv("MARKDOWN_DIR", "markdown_version")
PROMPT_FILE = "prompt.md"
LOG_DIR = "log"

# Path to the PDPA (or other authoritative regulatory source) text sent
# alongside the rubric on every request, so the model doesn't fall back on
# some other country's privacy framework.
PDPA_PATH = os.getenv("PDPA_PATH", "PDPA.md")

# Path to the case study text. Read once per run and cached in memory (exactly
# like PDPA_PATH above), then sent as the [CASE STUDY] section of every
# request. Keeping it in its own file - rather than embedded inline in
# prompt.md - means the rubric and the case facts can be edited independently,
# and the case study is never duplicated between prompt.md and a supplementary
# PDF.
CASE_STUDY_PATH = os.getenv("CASE_STUDY_PATH", "case_study.md")

# --- PDF diagram/chart handling ---
# Plain text extraction can't see flowcharts, org charts or graphs, even
# though the rubric credits diagrams. Two modes:
#   "off"      - text/tables only. Cheapest; blind to visual-only content.
#   "describe" - rasterize pages that look like they contain a diagram and
#                have a vision model write a short description that is
#                inserted into the submission text. One extra call per
#                flagged page (not per evaluator model).
# Default is "off" because it needs a vision-capable model, and Groq's
# production list currently has none (its only vision model, llama-4-scout,
# was retired 2026-07-17). See README "Diagrams, Charts, and Flowcharts".
PDF_IMAGE_HANDLING = os.getenv("PDF_IMAGE_HANDLING", "off").strip().lower()
if PDF_IMAGE_HANDLING not in ("off", "describe"):
    raise RuntimeError("PDF_IMAGE_HANDLING must be 'off' or 'describe'.")

# "<provider>:<model_id>" of a model that accepts image input. Required when
# PDF_IMAGE_HANDLING=describe.
VISION_MODEL = _env("VISION_MODEL", "GROQ_VISION_MODEL", default="")

if PDF_IMAGE_HANDLING == "describe":
    if not VISION_MODEL:
        raise RuntimeError(
            "PDF_IMAGE_HANDLING=describe needs VISION_MODEL set to a vision-capable "
            "'<provider>:<model_id>' (e.g. a Gemini Flash-Lite model). See README "
            "'Diagrams, Charts, and Flowcharts in PDFs'."
        )
    _validate_models([VISION_MODEL], "Vision model")

_validate_keys(LLM_MODELS + ([VISION_MODEL] if VISION_MODEL and PDF_IMAGE_HANDLING == "describe" else []))

# Safety cap: at most this many pages per PDF get a vision call.
PDF_IMAGE_MAX_PAGES_PER_DOC = int(os.getenv("PDF_IMAGE_MAX_PAGES_PER_DOC", "5"))

# DPI used when rasterizing a flagged page for the vision model.
PDF_IMAGE_DPI = int(os.getenv("PDF_IMAGE_DPI", "150"))

# A page with at least this many vector shapes (rects+lines+curves) is
# treated as likely holding a vector-drawn chart/flowchart (PowerPoint
# SmartArt, Excel exports) even with zero embedded raster images.
PDF_VECTOR_SHAPE_THRESHOLD = int(os.getenv("PDF_VECTOR_SHAPE_THRESHOLD", "8"))

# A page with fewer extracted characters than this AND at least one vector
# shape is treated as "mostly a diagram with little caption text".
PDF_SPARSE_TEXT_CHARS = int(os.getenv("PDF_SPARSE_TEXT_CHARS", "40"))

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()

ASSESSMENT_CSV = os.path.join(OUTPUT_DIR, "assessment_result.csv")
RANKED_CSV = os.path.join(OUTPUT_DIR, "ranked_result.csv")

LLM_MAX_RETRIES = 3

# --- Ensemble disagreement detection ---
# Flag a submission for manual review when the max-min spread across models
# exceeds these thresholds. The prompt.md rubric is out of 100 with
# per-criterion maxima 20/30/30/20, so a flat threshold is only approximate;
# tune if the REVIEW rate looks too high or too low.
DISAGREEMENT_SECTION_THRESHOLD = float(os.getenv("DISAGREEMENT_SECTION_THRESHOLD", "5.0"))
DISAGREEMENT_TOTAL_THRESHOLD = float(os.getenv("DISAGREEMENT_TOTAL_THRESHOLD", "15.0"))
