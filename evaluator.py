"""Evaluate a single submission PDF against the case study using the LLM engine."""

import logging
import os

import llm_engine
import pdf_reader

logger = logging.getLogger(__name__)

CSV_FIELDS = [
    "candidate_id",
    "submission_file",
    "total_score",
    "problem_understanding",
    "analysis_quality",
    "recommendation_quality",
    "communication_quality",
    "penalty_case_facts",
    "penalty_structure_clarity",
    "bonus_innovation",
    "models_used",
    "disagreement_flag",
    "disagreement_detail",
    "strengths",
    "summary",
]


def _candidate_id_from_filename(submission_file: str) -> str:
    """Derive candidate ID from the submission filename.

    Example:
        Ruby_ConceptPaper.pdf -> Ruby
    """
    stem = os.path.splitext(submission_file)[0]
    return stem.split("_", 1)[0].strip()


def review_submission(submission_path: str, prompt: str) -> dict:
    """Return a CSV-ready result dict for one submission."""
    submission_file = os.path.basename(submission_path)
    candidate_id = _candidate_id_from_filename(submission_file)

    logger.debug("Starting review for candidate_id=%s (%s)", candidate_id, submission_path)

    try:
        submission_text = pdf_reader.extract_text(submission_path)
        logger.debug("Extracted %d chars from %s", len(submission_text), submission_path)
    except Exception:
        logger.exception("PDF extraction failed for %s", submission_path)
        return _failed_row(candidate_id, submission_file, "PDF extraction failed")

    try:
        result = llm_engine.evaluate(prompt, submission_text)
    except Exception:
        logger.exception("LLM evaluation failed for %s", submission_path)
        return _failed_row(candidate_id, submission_file, "LLM evaluation failed")

    return {
        "candidate_id": candidate_id,
        "submission_file": submission_file,
        "total_score": result.get("total_score", ""),
        "problem_understanding": result.get("problem_understanding", ""),
        "analysis_quality": result.get("analysis_quality", ""),
        "recommendation_quality": result.get("recommendation_quality", ""),
        "communication_quality": result.get("communication_quality", ""),
        "penalty_case_facts": result.get("penalty_case_facts", ""),
        "penalty_structure_clarity": result.get("penalty_structure_clarity", ""),
        "bonus_innovation": result.get("bonus_innovation", ""),
        "models_used": result.get("models_used", ""),
        "disagreement_flag": result.get("disagreement_flag", ""),
        "disagreement_detail": result.get("disagreement_detail", ""),
        "strengths": result.get("strengths", ""),
        "summary": result.get("summary", ""),
    }


def _failed_row(candidate_id: str, submission_file: str, reason: str) -> dict:
    return {
        "candidate_id": candidate_id,
        "submission_file": submission_file,
        "total_score": "FAILED",
        "problem_understanding": "",
        "analysis_quality": "",
        "recommendation_quality": "",
        "communication_quality": "",
        "penalty_case_facts": "",
        "penalty_structure_clarity": "",
        "bonus_innovation": "",
        "models_used": "",
        "disagreement_flag": "",
        "disagreement_detail": "",
        "strengths": "",
        "summary": reason,
    }