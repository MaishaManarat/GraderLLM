"""Entry point: review all submissions against the case study and rank results."""

import csv
import glob
import logging
import os
import sys
import time

import config
import evaluator
import llm_engine
import logging_config
import ranking
import truststore
truststore.inject_into_ssl()


logger = logging.getLogger(__name__)



def load_prompt() -> str:
    if not os.path.isfile(config.PROMPT_FILE):
        raise FileNotFoundError(f"Prompt file not found: {config.PROMPT_FILE}")
    with open(config.PROMPT_FILE, "r", encoding="utf-8") as f:
        return f.read()


def assessment_csv_path() -> str:
    """The CSV this run reads/writes: sharded per-shard file if SHARD_COUNT
    is set, otherwise the single canonical assessment_result.csv."""
    if config.SHARD_COUNT <= 1:
        return config.ASSESSMENT_CSV
    base, ext = os.path.splitext(config.ASSESSMENT_CSV)
    return f"{base}_shard{config.SHARD_INDEX}{ext}"


def read_rows(csv_path: str) -> list:
    if not os.path.isfile(csv_path):
        return []
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def split_scored(rows: list) -> tuple:
    """Split existing rows into (usable, stale). Usable rows have a real
    score and are kept as-is; stale rows (FAILED or blank) are dropped so
    the submission gets a fresh attempt instead of accumulating duplicate
    rows across runs."""
    usable, stale = [], []
    for row in rows:
        if row.get("total_score") not in ("", "FAILED", None):
            usable.append(row)
        else:
            stale.append(row)
    return usable, stale


def list_submissions(skip: set) -> list:
    pdfs = sorted(glob.glob(os.path.join(config.SUBMISSION_DIR, "*.pdf")))
    if config.SHARD_COUNT > 1:
        pdfs = [p for i, p in enumerate(pdfs) if i % config.SHARD_COUNT == config.SHARD_INDEX]
    pending = [p for p in pdfs if os.path.basename(p) not in skip]
    return pending[: config.NUMBER_OF_ASSIGNMENT]


def run() -> None:
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    logger.info("Loading reference files (PDPA, case study)...")
    for label, tokens in llm_engine.preload_references().items():
        logger.info("  %s loaded (~%s tokens, sent with every request)", label, f"{tokens:,}")

    logger.info("Loading prompt...")
    prompt = load_prompt()

    csv_path = assessment_csv_path()
    if config.SHARD_COUNT > 1:
        logger.info(
            "Sharding enabled: this run handles shard %d/%d, writing to %s",
            config.SHARD_INDEX, config.SHARD_COUNT, csv_path,
        )

    existing_rows = read_rows(csv_path)
    usable_rows, stale_rows = split_scored(existing_rows)
    if stale_rows:
        logger.info(
            "Dropping %d stale (FAILED/blank) row(s) from %s - those submissions "
            "will be retried fresh this run",
            len(stale_rows), csv_path,
        )

    already_done = {row.get("submission_file") for row in usable_rows}
    if already_done:
        logger.info(
            "Resuming: %d submission(s) already scored in %s, skipping them",
            len(already_done), csv_path,
        )

    submissions = list_submissions(already_done)
    logger.info(
        "Processing %d submission(s) this run (NUMBER_OF_ASSIGNMENT=%d)",
        len(submissions), config.NUMBER_OF_ASSIGNMENT,
    )

    if not submissions:
        logger.warning(
            "Nothing to process in %s/ (already scored, or folder is empty "
            "for this shard)",
            config.SUBMISSION_DIR,
        )
        if usable_rows and config.SHARD_COUNT <= 1:
            logger.info("Generating ranking from existing results...")
            ranking.generate_ranking()
            logger.info("Ranked results written to %s", config.RANKED_CSV)
        return

    # Rewrite the CSV starting from just the still-usable rows (this is what
    # drops stale FAILED rows instead of letting them pile up), then append
    # freshly computed rows as they complete, so the file is both duplicate-
    # free and checkpointed incrementally if this run gets interrupted.
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=evaluator.CSV_FIELDS)
        writer.writeheader()
        for row in usable_rows:
            writer.writerow(row)
        f.flush()

        for index, submission_path in enumerate(submissions, start=1):
            logger.info("[%d/%d] Reviewing %s", index, len(submissions), submission_path)
            start_time = time.monotonic()
            result = evaluator.review_submission(submission_path, prompt)
            elapsed = time.monotonic() - start_time
            writer.writerow(result)
            f.flush()
            logger.info(
                "[%d/%d] Done %s -> total_score=%s (%.1fs)",
                index, len(submissions), result["candidate_id"], result["total_score"], elapsed,
            )

    logger.info("Assessment results written to %s", csv_path)

    if config.SHARD_COUNT > 1:
        logger.info(
            "Sharded run complete. Once every shard has finished, run "
            "merge_shards.py once to combine them and generate the ranking."
        )
        return

    logger.info("Generating ranking...")
    ranking.generate_ranking()
    logger.info("Ranked results written to %s", config.RANKED_CSV)


if __name__ == "__main__":
    log_file = logging_config.setup_logging()
    logger.info("Logging to %s", log_file)
    try:
        run()
    except Exception:
        logger.exception("Fatal error")
        sys.exit(1)
