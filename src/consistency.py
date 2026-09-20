# Combines multiple seasons' per-player qualifying metrics into one consistency-checked
# table.
#
# Each entry passed in should already be one season's fully-qualified pool (forward
# classification, minutes floor, and age band already applied for that season; see
# main.py). This module's only job is deciding which players cleared enough of those
# seasons to count as "consistent," and averaging their per-90 numbers across the
# seasons where they qualified.

from __future__ import annotations

import pandas as pd

METRIC_SUFFIXES = ("_per90", "_rate", "_pct")


# Returns the columns in `df` that look like per-90 (or rate/pct) metrics, i.e. the
# columns that should be averaged across seasons rather than carried from one row.
def _metric_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c.endswith(METRIC_SUFFIXES)]


# Concatenates one row per (player, qualifying season), counts how many seasons each
# player qualified in, and keeps only players meeting `min_seasons_qualified`. Returns
# (summary, detail): `summary` has one row per player with season-averaged metrics plus
# consistency context (seasons qualified, age progression, total minutes); `detail` has
# the underlying per-season rows, for the report's season-by-season breakdown.
def combine_seasons(season_tables: list[pd.DataFrame], min_seasons_qualified: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    detail = pd.concat(season_tables, ignore_index=True).sort_values(["player_id", "season_start_date"])

    seasons_per_player = detail.groupby("player_id")["season_name"].nunique()
    qualifying_ids = seasons_per_player[seasons_per_player >= min_seasons_qualified].index
    detail = detail[detail["player_id"].isin(qualifying_ids)].copy()

    metric_cols = _metric_columns(detail)
    averaged = detail.groupby("player_id", as_index=False)[metric_cols].mean()

    latest = detail.drop_duplicates("player_id", keep="last")[
        ["player_id", "player_name", "team", "age", "position"]
    ]

    context = detail.groupby("player_id").agg(
        seasons_qualified_count=("season_name", "nunique"),
        seasons_qualified=("season_name", lambda s: ", ".join(s)),
        age_progression=("age", lambda s: " -> ".join(str(int(v)) for v in s)),
        minutes_played_total=("minutes_played", "sum"),
    )

    summary = averaged.merge(latest, on="player_id", how="left").merge(context, on="player_id", how="left")
    return summary, detail
