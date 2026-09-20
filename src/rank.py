# Percentile-ranks the qualifying pool and reweights by profile to produce shortlists.
#
# Metrics are skewed (a handful of players take most of the shots), so ranking
# uses percentile position within the qualifying pool rather than z-scores, so
# a single outlier can't drag the scale the way it would with raw standard
# deviations.

from __future__ import annotations

import pandas as pd


# Adds a `<metric>_pctl` column (0-100) for each metric, ranked within `pool`.
def percentile_rank_metrics(pool: pd.DataFrame, metric_columns: list[str]) -> pd.DataFrame:
    ranked = pool.copy()
    for col in metric_columns:
        ranked[f"{col}_pctl"] = ranked[col].rank(pct=True, method="average") * 100.0
    return ranked


# Computes the composite score = sum of weight * percentile for each metric in `weights`.
def score_profile(ranked_pool: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    score = pd.Series(0.0, index=ranked_pool.index)
    for metric, weight in weights.items():
        score = score + weight * ranked_pool[f"{metric}_pctl"]
    return score


# Ranks `ranked_pool` by the profile's composite score and returns the top `max_size`
# rows (or all of them, if the qualifying pool is thinner than that; the caller is
# responsible for warning if it's thinner than the configured minimum shortlist size).
def build_shortlist(
    ranked_pool: pd.DataFrame,
    profile_name: str,
    weights: dict[str, float],
    max_size: int,
) -> pd.DataFrame:
    scored = ranked_pool.copy()
    scored["composite_score"] = score_profile(scored, weights)
    scored["profile"] = profile_name
    shortlist = scored.sort_values("composite_score", ascending=False).head(max_size)
    return shortlist.reset_index(drop=True)


# Builds a shortlist for every profile in `profiles`, keyed by profile name.
def build_all_shortlists(
    ranked_pool: pd.DataFrame,
    profiles: dict[str, dict[str, float]],
    max_size: int,
) -> dict[str, pd.DataFrame]:
    shortlists = {}
    for profile_name, weights in profiles.items():
        shortlists[profile_name] = build_shortlist(ranked_pool, profile_name, weights, max_size)
    return shortlists
