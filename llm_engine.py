"""LLM engine: builds the review prompt, queries several models in parallel
(any OpenAI-compatible provider - see config.PROVIDERS), and averages their
per-section scores into a single ensemble evaluation.
"""

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import config
import providers

logger = logging.getLogger(__name__)


class LLMEngineError(Exception):
    """Raised when the LLM engine cannot produce a usable response."""


# Sections the prompt is expected to score, and their known maxima per the
# current prompt.md rubric (problem_understanding /20, analysis_quality /30,
# recommendation_quality /30, communication_quality /20). Each is averaged
# across models; the maxima aren't enforced here, they're just documentation
# for anyone tuning DISAGREEMENT_SECTION_THRESHOLD in config.py.
SCORE_FIELDS = (
    "problem_understanding",
    "analysis_quality",
    "recommendation_quality",
    "communication_quality",
)

# Penalty fields from the current rubric (each 0 to -10). These are part of
# total_score = sum(SCORE_FIELDS) + sum(PENALTY_FIELDS) + sum(BONUS_FIELDS),
# per prompt.md's "Scoring Rules" section - they're averaged across models
# exactly like the score fields, and folded into total_score below.
PENALTY_FIELDS = (
    "penalty_case_facts",
    "penalty_structure_clarity",
)

# Bonus field(s) from the current rubric (0 to +5, additive - see prompt.md's
# "Bonus: Out-of-the-Box Thinking" section). Averaged and folded into
# total_score exactly like PENALTY_FIELDS, just added instead of subtracted.
# Because this is additive, total_score's real ceiling is 100 + sum of these
# maxima (currently 105), not 100.
BONUS_FIELDS = (
    "bonus_innovation",
)

# Free-text list fields the model returns per rubric ("Strengths"). These
# aren't averaged (they're not numeric) - each model's list is kept and
# combined, tagged by model, same as summary.
LIST_FIELDS = (
    "strengths",
)

# Static reference documents (PDPA text, case study) are read from disk once
# per process and cached here, keyed by their configured path. "Loaded once"
# only means the disk read happens once - the API itself is stateless, so the
# full cached text is still sent (and billed as input tokens) on every single
# model request.
_REFERENCE_CACHE: dict[str, str] = {}


def _load_reference(path_str: str, label: str) -> str:
    text = _REFERENCE_CACHE.get(path_str)
    if text is None:
        path = Path(path_str)
        if not path.exists():
            raise LLMEngineError(
                f"{label} file not found: {path.resolve()} "
                f"(set its path in .env, or create the file)"
            )
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise LLMEngineError(f"{label} file is empty: {path.resolve()}")
        _REFERENCE_CACHE[path_str] = text
    return text


def _load_pdpa_text() -> str:
    """Load and cache the authoritative regulatory text (PDPA.md by default)."""
    return _load_reference(config.PDPA_PATH, "PDPA reference")


def _load_case_study_text() -> str:
    """Load and cache the case study text (case_study.md by default)."""
    return _load_reference(config.CASE_STUDY_PATH, "Case study")


def estimate_tokens(text: str) -> int:
    """Rough token estimate (words * 1.45) - no tokenizer dependency needed,
    and consistent between logging and cost estimates."""
    return int(len(text.split()) * 1.45)


def preload_references() -> dict[str, int]:
    """Load and validate every reference file up front, before any submission
    is processed, so a missing/empty file fails fast with a clear error
    rather than mid-batch. Returns {label: approx_tokens} for logging."""
    return {
        "PDPA": estimate_tokens(_load_pdpa_text()),
        "case study": estimate_tokens(_load_case_study_text()),
    }


REQUEST_TEMPLATE = """[SYSTEM ROLE]
{prompt}

[PDPA.md - AUTHORITATIVE SOURCE]
{pdpa_text}

[CASE STUDY]
{case_study_text}

[SUBMISSION]
{submission_text}

[TASK]
Evaluate the submission.
Return score and justification as JSON only, with no extra commentary or markdown fences.
"""

JSON_CORRECTION_TEMPLATE = """Your previous response was not valid JSON.

Previous response:
{previous_response}

Return the same evaluation again, but strictly as a single valid JSON object
and nothing else - no markdown fences, no commentary.
"""


def build_request(prompt: str, submission_text: str) -> str:
    """Build the full request text. The PDPA and case study sections are
    loaded from their own files (cached after the first read) rather than
    being passed in, so every caller automatically gets the same, current
    reference text without having to thread it through."""
    return REQUEST_TEMPLATE.format(
        prompt=prompt,
        pdpa_text=_load_pdpa_text(),
        case_study_text=_load_case_study_text(),
        submission_text=submission_text,
    )


def evaluate(
    prompt: str,
    submission_text: str,
    models: list[str] | None = None,
) -> dict:
    """Query every configured model and return an averaged ensemble result.

    Each model is asked in parallel. Models that fail (call error, invalid
    JSON after correction, etc.) are dropped from the average. The ensemble
    fails only if fewer than config.MIN_SUCCESSFUL_MODELS succeed.
    """
    models = models or config.LLM_MODELS
    if not models:
        raise LLMEngineError("No models configured (see config.LLM_MODELS).")

    request_text = build_request(prompt, submission_text)
    _log_request_size_once(request_text)
    logger.debug(
        "Ensemble request: %d model(s) %s, %d chars",
        len(models), models, len(request_text),
    )

    per_model: dict[str, dict] = {}
    failures: dict[str, str] = {}

    max_workers = max(1, min(config.LLM_MAX_CONCURRENT, len(models)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_model = {
            pool.submit(_evaluate_with_model, request_text, model_id): model_id
            for model_id in models
        }
        for future in as_completed(future_to_model):
            model_id = future_to_model[future]
            try:
                result = future.result()
                per_model[model_id] = result
                logger.debug("Model %s succeeded: %s", model_id, result)
            except Exception as exc:
                failures[model_id] = str(exc)
                logger.warning("Model %s failed: %s", model_id, exc)

    if len(per_model) < config.MIN_SUCCESSFUL_MODELS:
        raise LLMEngineError(
            f"Only {len(per_model)}/{len(models)} models succeeded "
            f"(need >= {config.MIN_SUCCESSFUL_MODELS}). Failures: {failures}"
        )

    aggregated = _aggregate(per_model, requested=models, failures=failures)
    logger.debug("Ensemble aggregated result: %s", aggregated)
    return aggregated


def _evaluate_with_model(request_text: str, model_id: str) -> dict:
    """Run one model with retry + JSON-correction, return parsed dict.

    Failures are sorted into two kinds:
      * hopeless (retired model, request bigger than the plan's per-minute
        token limit, bad key, empty reply): fail immediately with an
        explanation - retrying just wastes time and quota;
      * transient (429 rate limit, 5xx, connection/timeouts): back off and
        retry, honouring the server's retry-after header when present.
    """
    last_error = None
    for attempt in range(1, config.LLM_MAX_RETRIES + 1):
        try:
            raw_response = _call_model(request_text, model_id)
            logger.debug("[%s] raw response (attempt %d):\n%s", model_id, attempt, raw_response)
            return _parse_json_response(raw_response, request_text, model_id)
        except LLMEngineError:
            raise
        except Exception as exc:
            last_error = exc
            hopeless = _explain_non_retryable(exc, request_text)
            if hopeless:
                raise LLMEngineError(f"[{model_id}] {hopeless}") from exc

            wait_s = _retry_after_seconds(exc)
            if wait_s is None:
                wait_s = (2 ** attempt) if _is_connection_error(exc) else 1.0
            logger.warning(
                "[%s] attempt %s/%s failed: %s (%s). %s",
                model_id, attempt, config.LLM_MAX_RETRIES,
                exc, _diagnose(exc),
                f"Waiting {wait_s:.1f}s before retry." if attempt < config.LLM_MAX_RETRIES else "Giving up.",
            )
            if attempt < config.LLM_MAX_RETRIES:
                time.sleep(wait_s)

    raise LLMEngineError(
        f"[{model_id}] failed after {config.LLM_MAX_RETRIES} attempts: {last_error} "
        f"({_diagnose(last_error)})"
    )


def _status_code(exc: Exception):
    code = getattr(exc, "status_code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    return code


def _explain_non_retryable(exc: Exception, request_text: str) -> str | None:
    """If retrying cannot help, return a plain-English explanation; else None."""
    if isinstance(exc, providers.NonRetryableError):
        return str(exc)

    message = str(exc)
    lowered = message.lower()
    status = _status_code(exc)
    est_tokens = estimate_tokens(request_text)

    if "model_not_found" in lowered or "does not exist or you do not have access" in lowered:
        return (
            "model not available: it was retired/renamed by the provider, or your account "
            "has no access to it. Run `python check_connection.py` to list the models your "
            "key can actually use, then fix LLM_MODELS in .env. "
            f"(provider said: {message})"
        )
    if "request too large" in lowered or status == 413:
        return (
            f"this request (~{est_tokens:,} tokens) is larger than the model's per-minute "
            "token limit on your current plan, so it can never succeed - retrying or "
            "waiting will not help. Move to a plan with a higher tokens-per-minute limit "
            "(on Groq: the Developer plan), use a different model/provider, or shrink "
            f"the prompt (PDPA.md + prompt.md are most of it). (provider said: {message})"
        )
    if status in (401, 403):
        return f"authentication/permission problem (HTTP {status}) - check the API key. ({message})"
    if status in (400, 404, 422):
        return f"the provider rejected the request as invalid (HTTP {status}): {message}"
    return None


def _is_connection_error(exc: Exception) -> bool:
    """True for errors that never reached the provider at all (DNS failure,
    TLS handshake failure, timeout, connection refused) as opposed to an HTTP
    error response the provider actually sent back."""
    type_name = type(exc).__name__
    return (
        "Connect" in type_name
        or "Timeout" in type_name
        or (getattr(exc, "response", None) is None and getattr(exc, "status_code", None) is None)
    )


def _diagnose(exc: Exception | None) -> str:
    """Best-effort hint about *why* a call failed. The SDK's own message for
    connection-level failures is often just "Connection error." with the real
    cause buried in exc.__cause__."""
    if exc is None:
        return "no error captured"
    cause = exc.__cause__ or exc.__context__
    if cause is None:
        return f"{type(exc).__name__}, no underlying cause captured"

    cause_str = str(cause)
    cause_type = type(cause).__name__
    hint = ""
    lowered = cause_str.lower()
    if "ssl" in lowered or "certificate" in lowered:
        hint = " - looks like a TLS/certificate problem (outdated certifi/OpenSSL, or a proxy intercepting HTTPS)"
    elif "name or service not known" in lowered or "nodename" in lowered or "getaddrinfo" in lowered:
        hint = " - DNS resolution failed (no internet, or DNS is blocked/misconfigured)"
    elif "timed out" in lowered or "timeout" in lowered:
        hint = " - the request timed out (slow network, or a firewall silently dropping the connection)"
    elif "refused" in lowered:
        hint = " - connection was refused (a proxy or firewall is actively blocking this)"
    elif "proxy" in lowered:
        hint = " - looks like a proxy configuration issue"
    return f"{cause_type}: {cause_str}{hint}"


_size_logged = False


def _log_request_size_once(request_text: str) -> None:
    """Log the approximate prompt size once per run - it is the number that
    decides whether a plan's tokens-per-minute limit can ever work."""
    global _size_logged
    if not _size_logged:
        _size_logged = True
        logger.info(
            "Each model request is ~%s tokens (rough estimate). Your plan's "
            "per-minute token limit for each model must exceed this.",
            f"{estimate_tokens(request_text):,}",
        )


def _retry_after_seconds(exc: Exception) -> float | None:
    """Extract a retry-after wait (seconds) from a 429 rate-limit error.
    Returns None if the exception isn't a 429."""
    response = getattr(exc, "response", None)
    if _status_code(exc) != 429:
        return None

    headers = getattr(response, "headers", None) if response is not None else None
    if headers:
        header_value = headers.get("retry-after")
        if header_value:
            try:
                return max(float(header_value), 0.0)
            except ValueError:
                pass

    return 10.0  # conservative fallback when no header is present


def _parse_json_response(raw_response: str, request_text: str, model_id: str) -> dict:
    parsed = _try_parse_json(raw_response)
    if parsed is not None:
        return parsed

    correction_prompt = JSON_CORRECTION_TEMPLATE.format(previous_response=raw_response)
    corrected = _call_model(request_text + "\n\n" + correction_prompt, model_id)
    parsed = _try_parse_json(corrected)
    if parsed is not None:
        return parsed

    raise LLMEngineError(f"[{model_id}] response was not valid JSON after correction")


def _try_parse_json(text: str):
    text = text.strip()
    fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                return None
        return None


def _call_model(request_text: str, model_id: str) -> str:
    """Call one model ("<provider>:<id>") through the shared client layer."""
    return providers.chat_text(model_id, request_text)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _aggregate(
    per_model: dict[str, dict],
    requested: list[str],
    failures: dict[str, str],
) -> dict:
    """Average each score/penalty/bonus field across models, then sum them
    into total_score per prompt.md's rule:

        total_score = problem_understanding + analysis_quality
                    + recommendation_quality + communication_quality
                    + penalty_case_facts + penalty_structure_clarity
                    + bonus_innovation

    Also computes the max-min spread per score field (not per penalty/bonus
    field) and flags the row if any spread exceeds the configured
    thresholds.
    """
    aggregated: dict = {}
    spreads: dict[str, float] = {}

    for field in SCORE_FIELDS:
        values = [
            v for v in (_coerce_number(r.get(field)) for r in per_model.values())
            if v is not None
        ]
        if values:
            aggregated[field] = round(sum(values) / len(values), 2)
            spreads[field] = round(max(values) - min(values), 2)
        else:
            aggregated[field] = ""
            spreads[field] = None

    for field in PENALTY_FIELDS + BONUS_FIELDS:
        values = [
            v for v in (_coerce_number(r.get(field)) for r in per_model.values())
            if v is not None
        ]
        # Missing/unparsed penalty/bonus values default to 0 (no adjustment)
        # rather than "" - these fields are expected to be present and
        # numeric, and treating a missing one as 0 keeps total_score
        # computable even if a model omits it.
        aggregated[field] = round(sum(values) / len(values), 2) if values else 0.0

    numeric_scores = [v for v in (aggregated[f] for f in SCORE_FIELDS) if isinstance(v, (int, float))]
    numeric_adjustments = [aggregated[f] for f in PENALTY_FIELDS + BONUS_FIELDS]
    aggregated["total_score"] = (
        round(sum(numeric_scores) + sum(numeric_adjustments), 2) if numeric_scores else ""
    )

    total_spread = _spread([
        sum(v for v in (_coerce_number(r.get(f)) for f in SCORE_FIELDS) if v is not None)
        + sum(_coerce_number(r.get(f)) or 0.0 for f in PENALTY_FIELDS + BONUS_FIELDS)
        for r in per_model.values()
    ])

    aggregated["models_used"] = f"{len(per_model)}/{len(requested)}"
    aggregated["per_model_scores"] = per_model

    # --- Disagreement detection ---
    flag, reasons = _disagreement_flag(spreads, total_spread, len(per_model))
    aggregated["disagreement_flag"] = flag
    aggregated["disagreement_detail"] = "; ".join(reasons) if reasons else ""

    # Combine per-model summaries and list fields (strengths),
    # plus a note on any failures.
    aggregated["summary"] = _combine_text_field(per_model, "summary")
    for field in LIST_FIELDS:
        aggregated[field] = _combine_list_field(per_model, field)

    if failures:
        failure_note = "Failed: " + ", ".join(f"{m} ({e})" for m, e in failures.items())
        aggregated["summary"] = (
            f"{aggregated['summary']} | {failure_note}" if aggregated["summary"] else failure_note
        )

    return aggregated


def _combine_text_field(per_model: dict[str, dict], field: str) -> str:
    parts = []
    for model_id, result in per_model.items():
        text = str(result.get(field, "")).strip()
        if text:
            parts.append(f"[{model_id}] {text}")
    return " | ".join(parts)


def _combine_list_field(per_model: dict[str, dict], field: str) -> str:
    """Combine a per-model list field (e.g. strengths) into one
    string: '[model_id] item1; item2 | [model_id] item1; item2'."""
    parts = []
    for model_id, result in per_model.items():
        items = result.get(field)
        if isinstance(items, list) and items:
            joined = "; ".join(str(i).strip() for i in items if str(i).strip())
            if joined:
                parts.append(f"[{model_id}] {joined}")
        elif isinstance(items, str) and items.strip():
            parts.append(f"[{model_id}] {items.strip()}")
    return " | ".join(parts)


def _spread(values: list[float]) -> float | None:
    """max - min, or None if fewer than 2 values."""
    if len(values) < 2:
        return None
    return round(max(values) - min(values), 2)


def _disagreement_flag(
    section_spreads: dict[str, float | None],
    total_spread: float | None,
    n_models: int,
) -> tuple[str, list[str]]:
    """Return (flag, reasons) where flag is 'REVIEW' or ''."""
    # Can't measure disagreement with a single model.
    if n_models < 2:
        return "", []

    reasons: list[str] = []

    for field, spread in section_spreads.items():
        if spread is not None and spread > config.DISAGREEMENT_SECTION_THRESHOLD:
            reasons.append(f"{field} spread={spread}")

    if total_spread is not None and total_spread > config.DISAGREEMENT_TOTAL_THRESHOLD:
        reasons.append(f"total spread={total_spread}")

    return ("REVIEW" if reasons else ""), reasons


def _coerce_number(value):
    """Return a float for numeric input (or numeric strings), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None
