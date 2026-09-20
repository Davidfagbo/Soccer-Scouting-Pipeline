# Fetches StatsBomb open-data matches, events, and lineups, cached locally as pickle files.
#
# Swapping the data source later (e.g. to an NCAA D1 feed) means replacing this
# module's three fetch functions with equivalents that return the same shapes:
# matches indexed by match_id, one events DataFrame per match, one lineups
# DataFrame per match. Nothing downstream (events.py onward) imports statsbombpy.

from __future__ import annotations

import concurrent.futures
import logging
from pathlib import Path

import pandas as pd
from statsbombpy import sb

logger = logging.getLogger(__name__)


# Builds the cache file path for one cached item and makes sure its parent directory exists.
def _cache_file(cache_dir: Path, *parts: str) -> Path:
    path = cache_dir.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# Returns the match list for a competition/season, cached to pickle. Pickle rather than
# parquet: StatsBomb's flattened columns (e.g. manager lists) carry irregular per-row
# Python objects that parquet's columnar schema inference rejects.
def get_matches(competition_id: int, season_id: int, cache_dir: Path) -> pd.DataFrame:
    cache_path = _cache_file(cache_dir, "matches", f"{competition_id}_{season_id}.pkl")
    if cache_path.exists():
        return pd.read_pickle(cache_path)
    matches = sb.matches(competition_id=competition_id, season_id=season_id)
    matches.to_pickle(cache_path)
    return matches


# Returns the flattened event stream for one match, cached to pickle.
def get_events(match_id: int, cache_dir: Path) -> pd.DataFrame:
    cache_path = _cache_file(cache_dir, "events", f"{match_id}.pkl")
    if cache_path.exists():
        return pd.read_pickle(cache_path)
    events = sb.events(match_id=match_id)
    events.to_pickle(cache_path)
    return events


# Returns both teams' lineups for one match as a single DataFrame, cached to pickle.
def get_lineups(match_id: int, cache_dir: Path) -> pd.DataFrame:
    cache_path = _cache_file(cache_dir, "lineups", f"{match_id}.pkl")
    if cache_path.exists():
        return pd.read_pickle(cache_path)
    lineups_by_team = sb.lineups(match_id=match_id)
    combined = pd.concat(
        [df.assign(team=team) for team, df in lineups_by_team.items()],
        ignore_index=True,
    )
    combined.to_pickle(cache_path)
    return combined


# Fetches (or reads from cache) matches plus per-match events and lineups, returning
# (matches, {match_id: events_df}, {match_id: lineups_df}). Fetches run concurrently
# since raw.githubusercontent.com happily serves parallel requests and a full 380-match
# season is otherwise slow to pull one file at a time; cached matches are read from disk
# and cost nothing. Pass `max_matches` to only fetch a prefix of the season (quick smoke
# tests).
def load_season(
    competition_id: int,
    season_id: int,
    cache_dir: Path,
    max_workers: int = 8,
    max_matches: int | None = None,
) -> tuple[pd.DataFrame, dict[int, pd.DataFrame], dict[int, pd.DataFrame]]:
    matches = get_matches(competition_id, season_id, cache_dir)
    match_ids: list[int] = matches["match_id"].tolist()
    if max_matches:
        match_ids = match_ids[:max_matches]
    logger.info("Loading %d matches for competition=%s season=%s", len(match_ids), competition_id, season_id)

    events_by_match: dict[int, pd.DataFrame] = {}
    lineups_by_match: dict[int, pd.DataFrame] = {}

    # Fetches one match's events and lineups together, or returns Nones if either fails.
    def fetch_one(match_id: int) -> tuple[int, pd.DataFrame | None, pd.DataFrame | None]:
        try:
            events = get_events(match_id, cache_dir)
            lineups = get_lineups(match_id, cache_dir)
            return match_id, events, lineups
        except Exception:
            logger.warning("Failed to fetch match_id=%s, skipping", match_id, exc_info=True)
            return match_id, None, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for i, (match_id, events, lineups) in enumerate(pool.map(fetch_one, match_ids), start=1):
            if events is not None and lineups is not None:
                events_by_match[match_id] = events
                lineups_by_match[match_id] = lineups
            if i % 50 == 0 or i == len(match_ids):
                logger.info("Fetched %d/%d matches", i, len(match_ids))

    return matches, events_by_match, lineups_by_match
