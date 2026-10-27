"""Merge per-shard assessment CSVs (produced when SHARD_COUNT > 1, e.g. by
several colleagues each running their own slice under their own Groq key)
into the single canonical output/assessment_result.csv, then regenerate
output/ranked_result.csv.

Usage (after every shard's run has finished):
    python merge_shards.py
"""

import csv
import glob
import logging
import os

import config
import evaluator
import logging_config
import ranking

logger = logging.getLogger(__name__)


def _read_rows(path: str) -> list:
    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def merge() -> None:
    base, ext = os.path.splitext(config.ASSESSMENT_CSV)
    shard_files = sorted(glob.glob(f"{base}_shard*{ext}"))
    if not shard_files:
        logger.warning("No shard files found matching %s_shard*%s", base, ext)
        return

    logger.info("Merging %d shard file(s): %s", len(shard_files), shard_files)

    # If a submission_file somehow appears in more than one shard (e.g. a
    # shard was re-run with a different SHARD_COUNT), prefer a real score
    # over a FAILED/blank one rather than just taking the last one seen.
    by_submission: dict = {}
    for shard_file in shard_files:
        for row in _read_rows(shard_file):
            key = row.get("submission_file")
            existing = by_submission.get(key)
            if existing is None or existing.get("total_score") in ("", "FAILED", None):
                by_submission[key] = row

    with open(config.ASSESSMENT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=evaluator.CSV_FIELDS)
        writer.writeheader()
        for row in by_submission.values():
            writer.writerow(row)

    logger.info(
        "Merged into %s (%d submission(s))",
        config.ASSESSMENT_CSV, len(by_submission),
    )

    ranking.generate_ranking()
    logger.info("Ranked results written to %s", config.RANKED_CSV)


if __name__ == "__main__":
    logging_config.setup_logging()
    merge()
