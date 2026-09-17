"""Aggregate per-action rows into per-90 player metrics.

The full metric set is computed once per player here; rank.py reweights the
same table per profile rather than recomputing anything. All rate metrics use
`minutes_played` from events.compute_appearances (i.e. actual pitch time
reconstructed from lineup position segments and half-end clocks), not
appearance counts, so per-90 rates aren't distorted by unused-sub minutes.
"""

from __future__ import annotations

import pandas as pd

ON_TARGET_DEFAULT = {"Goal", "Saved", "Saved to Post"}


def _in_box(df: pd.DataFrame, box: dict[str, float]) -> pd.Series:
    return df["loc_x"].between(box["x_min"], box["x_max"]) & df["loc_y"].between(box["y_min"], box["y_max"])


def _per90(count: pd.Series, minutes: pd.Series) -> pd.Series:
    return (count / minutes.replace(0, pd.NA)) * 90.0


def compute_player_metrics(
    actions: pd.DataFrame,
    appearances: pd.DataFrame,
    on_target_outcomes: set[str] = ON_TARGET_DEFAULT,
    penalty_box: dict[str, float] | None = None,
    progressive_carry_min_distance: float = 5.0,
) -> pd.DataFrame:
    """Return one row per player_id with minutes, raw counts, and per-90 rates
    for every metric referenced by any profile in config.yaml.
    """
    penalty_box = penalty_box or {"x_min": 102.0, "x_max": 120.0, "y_min": 18.0, "y_max": 62.0}

    minutes = appearances.groupby(["player_id", "player_name"], as_index=False).agg(
        minutes_played=("minutes_played", "sum"), appearances=("match_id", "count")
    )
    # a player can appear for one team in a single-season pull; take the most
    # frequent team label as their season team for display purposes.
    team = (
        appearances.groupby(["player_id", "team"])
        .size()
        .reset_index(name="n")
        .sort_values("n", ascending=False)
        .drop_duplicates("player_id")[["player_id", "team"]]
    )
    minutes = minutes.merge(team, on="player_id", how="left")

    shots = actions[actions["type"] == "Shot"].copy()
    shots["is_goal"] = shots["shot_outcome"] == "Goal"
    shots["is_on_target"] = shots["shot_outcome"].isin(on_target_outcomes)
    shot_agg = shots.groupby("player_id").agg(
        shots=("id", "count"),
        goals=("is_goal", "sum"),
        shots_on_target=("is_on_target", "sum"),
        xg=("shot_statsbomb_xg", "sum"),
    )

    box_touch_types = {"Pass", "Carry", "Dribble", "Shot"}
    touches = actions[actions["type"].isin(box_touch_types)].copy()
    touches["in_box"] = _in_box(touches, penalty_box)
    box_touches = touches[touches["in_box"]].groupby("player_id").size().rename("box_touches")

    passes = actions[actions["type"] == "Pass"].copy()
    passes["is_complete"] = passes["pass_outcome"].isna()
    pass_agg = passes.groupby("player_id").agg(
        passes_attempted=("id", "count"),
        passes_completed=("is_complete", "sum"),
        key_passes=("pass_shot_assist", lambda s: s.fillna(False).astype(bool).sum()),
    )

    key_pass_events = passes[passes["pass_shot_assist"].fillna(False).astype(bool)]
    shot_xg_by_id = shots.set_index("id")["shot_statsbomb_xg"]
    xg_assisted = (
        key_pass_events.assign(assisted_xg=key_pass_events["pass_assisted_shot_id"].map(shot_xg_by_id))
        .groupby("player_id")["assisted_xg"]
        .sum()
        .rename("xg_assisted")
    )

    carries = actions[actions["type"] == "Carry"].copy()
    carries["end_x"] = carries["carry_end_location"].map(
        lambda loc: loc[0] if isinstance(loc, (list, tuple)) else None
    )
    carries["end_y"] = carries["carry_end_location"].map(
        lambda loc: loc[1] if isinstance(loc, (list, tuple)) else None
    )
    goal = (120.0, 40.0)
    carries["start_dist"] = ((carries["loc_x"] - goal[0]) ** 2 + (carries["loc_y"] - goal[1]) ** 2) ** 0.5
    carries["end_dist"] = ((carries["end_x"] - goal[0]) ** 2 + (carries["end_y"] - goal[1]) ** 2) ** 0.5
    carries["is_progressive"] = (
        carries["start_dist"] - carries["end_dist"]
    ) >= progressive_carry_min_distance
    progressive_carries = carries.groupby("player_id")["is_progressive"].sum().rename("progressive_carries")

    dribbles = actions[actions["type"] == "Dribble"].copy()
    dribbles["is_complete"] = dribbles["dribble_outcome"] == "Complete"
    dribbles_completed = dribbles.groupby("player_id")["is_complete"].sum().rename("dribbles_completed")

    pressures = actions[actions["type"] == "Pressure"].groupby("player_id").size().rename("pressures")
    defensive_actions = (
        actions[actions["is_defensive_action"]].groupby("player_id").size().rename("defensive_actions")
    )

    metrics = minutes.set_index("player_id")
    for series in (
        shot_agg["shots"],
        shot_agg["goals"],
        shot_agg["shots_on_target"],
        shot_agg["xg"],
        box_touches,
        pass_agg["passes_attempted"],
        pass_agg["passes_completed"],
        pass_agg["key_passes"],
        xg_assisted,
        progressive_carries,
        dribbles_completed,
        pressures,
        defensive_actions,
    ):
        metrics = metrics.join(series, how="left")
    metrics = metrics.fillna(0.0)

    metrics["xg_per90"] = _per90(metrics["xg"], metrics["minutes_played"])
    metrics["shots_per90"] = _per90(metrics["shots"], metrics["minutes_played"])
    metrics["shots_on_target_per90"] = _per90(metrics["shots_on_target"], metrics["minutes_played"])
    metrics["conversion_rate"] = metrics["goals"] / metrics["shots"].replace(0, pd.NA)
    metrics["box_touches_per90"] = _per90(metrics["box_touches"], metrics["minutes_played"])
    metrics["xg_assisted_per90"] = _per90(metrics["xg_assisted"], metrics["minutes_played"])
    metrics["key_passes_per90"] = _per90(metrics["key_passes"], metrics["minutes_played"])
    metrics["progressive_carries_per90"] = _per90(metrics["progressive_carries"], metrics["minutes_played"])
    metrics["dribbles_completed_per90"] = _per90(metrics["dribbles_completed"], metrics["minutes_played"])
    metrics["pressures_per90"] = _per90(metrics["pressures"], metrics["minutes_played"])
    metrics["defensive_actions_per90"] = _per90(metrics["defensive_actions"], metrics["minutes_played"])
    metrics["pass_completion_pct"] = (
        100.0 * metrics["passes_completed"] / metrics["passes_attempted"].replace(0, pd.NA)
    )

    metrics = metrics.fillna(0.0)
    return metrics.reset_index()
