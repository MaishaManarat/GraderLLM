"""Rank submissions by total_score and write ranked_result.csv."""

import pandas as pd

import config


def generate_ranking(assessment_csv: str = config.ASSESSMENT_CSV, ranked_csv: str = config.RANKED_CSV) -> None:
    df = pd.read_csv(assessment_csv)

    numeric_score = pd.to_numeric(df["total_score"], errors="coerce")
    df = df.assign(_sort_score=numeric_score)

    df = df.sort_values(
        by=["_sort_score", "candidate_id"],
        ascending=[False, True],
        na_position="last",
    ).drop(columns="_sort_score")

    df.insert(0, "rank", range(1, len(df) + 1))

    ranked = df[[
        "rank",
        "candidate_id",
        "total_score",
        "problem_understanding",
        "analysis_quality",
        "recommendation_quality",
        "communication_quality",
        "penalty_case_facts",
        "penalty_structure_clarity",
        "bonus_innovation",
        "disagreement_flag",
        "models_used",
    ]]
    ranked.to_csv(ranked_csv, index=False)