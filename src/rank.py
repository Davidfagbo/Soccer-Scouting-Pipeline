"""Percentile-rank the qualifying pool and reweight by profile to produce shortlists.

Metrics are skewed (a handful of players take most of the shots), so ranking
uses percentile position within the qualifying pool rather than z-scores, so
a single outlier can't drag the scale the way it would with raw standard
deviations.
"""

from __future__ import annotations

import pandas as pd


def percentile_rank_metrics(pool: pd.DataFrame, metric_columns: list[str]) -> pd.DataFrame:
    """Add a `<metric>_pctl` column (0-100) for each metric, ranked within `pool`."""
    ranked = pool.copy()
    for col in metric_columns:
        ranked[f"{col}_pctl"] = ranked[col].rank(pct=True, method="average") * 100.0
    return ranked


def score_profile(ranked_pool: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    """Composite score = sum of weight * percentile for each metric in `weights`."""
    score = pd.Series(0.0, index=ranked_pool.index)
    for metric, weight in weights.items():
        score = score + weight * ranked_pool[f"{metric}_pctl"]
    return score


def build_shortlist(
    ranked_pool: pd.DataFrame,
    profile_name: str,
    weights: dict[str, float],
    max_size: int,
) -> pd.DataFrame:
    """Rank `ranked_pool` by the profile's composite score and return the top
    `max_size` rows (or all of them, if the qualifying pool is thinner than
    that; the caller is responsible for warning if it's thinner than the
    configured minimum shortlist size).
    """
    scored = ranked_pool.copy()
    scored["composite_score"] = score_profile(scored, weights)
    scored["profile"] = profile_name
    shortlist = scored.sort_values("composite_score", ascending=False).head(max_size)
    return shortlist.reset_index(drop=True)


def build_all_shortlists(
    ranked_pool: pd.DataFrame,
    profiles: dict[str, dict[str, float]],
    max_size: int,
) -> dict[str, pd.DataFrame]:
    shortlists = {}
    for profile_name, weights in profiles.items():
        shortlists[profile_name] = build_shortlist(ranked_pool, profile_name, weights, max_size)
    return shortlists
