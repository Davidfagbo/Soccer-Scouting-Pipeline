"""Flatten raw StatsBomb events into a tidy per-action table, and derive
per-appearance playing time and position from lineups + events.

Two outputs feed metrics.py:
  - actions: one row per action-relevant event (shot, pass, carry, dribble,
    pressure, defensive action), with the columns metrics.py aggregates.
  - appearances: one row per (match_id, player_id) with minutes played and
    the position the player started that appearance in, the basis for both
    the forward classification (gotcha #1) and the minutes denominator used
    for every per-90 rate.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

ACTION_EVENT_TYPES = {
    "Shot",
    "Pass",
    "Carry",
    "Dribble",
    "Pressure",
    "Interception",
    "Block",
    "Clearance",
    "Duel",
}

DEFENSIVE_EVENT_TYPES = {"Interception", "Block", "Clearance"}


def _parse_clock(value: str | None) -> float | None:
    """Parse a StatsBomb 'mm:ss' clock string into total minutes (float)."""
    if value is None:
        return None
    minutes, seconds = value.split(":")
    return int(minutes) + int(seconds) / 60.0


def _period_end_minutes(events: pd.DataFrame) -> dict[int, float]:
    """Map period -> the match-clock minute (in lineup 'positions' convention)
    at which that period ended, read from the 'Half End' events.
    """
    half_ends = events[events["type"] == "Half End"]
    return {int(row.period): float(row.minute) + float(row.second) / 60.0 for row in half_ends.itertuples()}


@dataclass(frozen=True)
class Appearance:
    match_id: int
    player_id: int
    player_name: str
    team: str
    minutes_played: float
    starting_position: str | None


def compute_appearances(match_id: int, lineups: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """One row per player who took the pitch in this match, with total minutes
    played and the position they started that appearance in.
    """
    period_end = _period_end_minutes(events)
    rows: list[Appearance] = []

    for row in lineups.itertuples():
        positions = row.positions
        if not positions:
            continue  # squad-listed but did not play

        total_minutes = 0.0
        for segment in positions:
            start = _parse_clock(segment["from"])
            end = _parse_clock(segment["to"])
            if end is None:
                end = period_end.get(segment["from_period"], start)
            total_minutes += max(end - start, 0.0)

        rows.append(
            Appearance(
                match_id=match_id,
                player_id=row.player_id,
                player_name=row.player_name,
                team=row.team,
                minutes_played=total_minutes,
                starting_position=positions[0]["position"],
            )
        )

    return pd.DataFrame(rows)


def flatten_actions(match_id: int, events: pd.DataFrame) -> pd.DataFrame:
    """Reduce one match's raw event stream to a tidy per-action table."""
    df = events[events["type"].isin(ACTION_EVENT_TYPES)].copy()
    df["match_id"] = match_id

    is_tackle = df["duel_type"] == "Tackle" if "duel_type" in df.columns else False
    df = df[(df["type"] != "Duel") | is_tackle].copy()

    location = df["location"] if "location" in df.columns else pd.Series([None] * len(df), index=df.index)
    df["loc_x"] = location.map(lambda loc: loc[0] if isinstance(loc, (list, tuple)) else None)
    df["loc_y"] = location.map(lambda loc: loc[1] if isinstance(loc, (list, tuple)) else None)

    keep_cols = [
        "id",
        "match_id",
        "player_id",
        "player",
        "team",
        "type",
        "loc_x",
        "loc_y",
        "duel_type",
        "shot_statsbomb_xg",
        "shot_outcome",
        "pass_shot_assist",
        "pass_goal_assist",
        "pass_assisted_shot_id",
        "pass_outcome",
        "carry_end_location",
        "dribble_outcome",
    ]
    for col in keep_cols:
        if col not in df.columns:
            df[col] = None

    df["is_defensive_action"] = df["type"].isin(DEFENSIVE_EVENT_TYPES) | is_tackle
    return df[keep_cols + ["is_defensive_action"]].rename(columns={"player": "player_name"})


def build_season_tables(
    events_by_match: dict[int, pd.DataFrame],
    lineups_by_match: dict[int, pd.DataFrame],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Concatenate per-match actions and appearances into season-level tables."""
    action_frames = []
    appearance_frames = []

    for match_id, events in events_by_match.items():
        lineups = lineups_by_match[match_id]
        action_frames.append(flatten_actions(match_id, events))
        appearance_frames.append(compute_appearances(match_id, lineups, events))

    actions = pd.concat(action_frames, ignore_index=True) if action_frames else pd.DataFrame()
    appearances = pd.concat(appearance_frames, ignore_index=True) if appearance_frames else pd.DataFrame()
    return actions, appearances


def classify_forwards(
    appearances: pd.DataFrame, forward_positions: set[str], min_share: float
) -> pd.DataFrame:
    """Aggregate appearances to one row per player with a season-level
    forward_share and is_forward flag (gotcha #1: never filter on a single
    position field; this uses the share of a player's own appearances that
    started in a forward position).
    """
    per_player = (
        appearances.assign(is_forward_appearance=appearances["starting_position"].isin(forward_positions))
        .groupby(["player_id", "player_name"], as_index=False)
        .agg(
            appearances=("match_id", "count"),
            forward_appearances=("is_forward_appearance", "sum"),
            minutes_played=("minutes_played", "sum"),
        )
    )
    per_player["forward_share"] = per_player["forward_appearances"] / per_player["appearances"]
    per_player["is_forward"] = per_player["forward_share"] >= min_share
    return per_player
