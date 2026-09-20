# Runs the full scouting pipeline end to end: load -> events -> scaled minutes floor ->
# age -> metrics (per season) -> consistency check across seasons -> rank -> report.

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

import yaml

from src import age, consistency, events, load, metrics, rank, report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("pipeline")


# Runs load -> flatten -> classify forwards for one season, WITHOUT applying the
# minutes floor yet (that needs a reference match count computed across all seasons
# first; see `_scaled_min_minutes` below). Returns every classified forward
# (with their team attached) alongside that season's full actions/appearances and
# its per-team match counts.
def _load_season_raw(competition_id: int, season_cfg: dict, cache_dir: Path, fwd_cfg: dict, max_matches: int | None):
    logger.info("Loading %s season (season_id=%s)", season_cfg["name"], season_cfg["season_id"])
    matches, events_by_match, lineups_by_match = load.load_season(
        competition_id, season_cfg["season_id"], cache_dir, max_matches=max_matches
    )
    actions, appearances = events.build_season_tables(events_by_match, lineups_by_match)

    players = events.classify_forwards(
        appearances, set(fwd_cfg["positions"]), fwd_cfg["min_forward_appearance_share"]
    )
    forwards = players[players["is_forward"]].copy()
    forwards = forwards.merge(events.primary_team(appearances), on="player_id", how="left")
    forwards = forwards.merge(
        events.primary_forward_position(appearances, set(fwd_cfg["positions"])), on="player_id", how="left"
    )

    team_match_counts = events.count_team_matches(matches)
    logger.info(
        "%s: %d of %d players classify as forwards",
        season_cfg["name"],
        players["is_forward"].sum(),
        len(players),
    )
    return forwards, actions, appearances, team_match_counts


# Scales `min_minutes` down for a team whose season is only partially represented in
# the dataset (see the Barcelona-only-release limitation in the README): a team with
# `team_matches` of `reference_matches` on record is held to that same proportion of
# the full minutes floor, never below `absolute_floor` and never above `min_minutes`
# itself.
def _scaled_min_minutes(team_matches: int, reference_matches: int, min_minutes: int, absolute_floor: int) -> float:
    proportional = min_minutes * (team_matches / reference_matches)
    return min(min_minutes, max(absolute_floor, proportional))


# Runs the pipeline for one config file, optionally capped to the first `max_matches`
# matches per season for a quick smoke test, and returns the path to the rendered PDF.
def run(config_path: Path, max_matches: int | None = None) -> Path:
    config = yaml.safe_load(config_path.read_text())

    comp = config["competition"]
    paths = config["paths"]
    cache_dir = Path(paths["cache_dir"])
    fwd_cfg = config["forward_classification"]
    playing_time_cfg = config["playing_time"]
    min_minutes = playing_time_cfg["min_minutes"]
    absolute_floor = playing_time_cfg["min_minutes_absolute_floor"]
    metric_cfg = config["metrics"]

    # Stage 1: pull every season's raw forward pool (forward-classified, not yet
    # minutes-filtered) and each season's per-team match coverage. The reference for
    # "a full season" is the most matches any team has on record across everything
    # loaded: in this dataset that's a 2015/2016 team's full 38-match campaign.
    season_raw = []
    all_team_match_counts: list[int] = []
    for season_cfg in config["seasons"]:
        forwards, actions, appearances, team_match_counts = _load_season_raw(
            comp["competition_id"], season_cfg, cache_dir, fwd_cfg, max_matches
        )
        season_raw.append((season_cfg, forwards, actions, appearances, team_match_counts))
        all_team_match_counts.extend(team_match_counts.values())
    reference_matches = max(all_team_match_counts)

    # Stage 2: apply the scaled minutes floor per season, then resolve every
    # surviving player's name against Wikidata in one batch rather than once per season.
    season_pools = []
    all_forward_names: set[str] = set()
    for season_cfg, forwards, actions, appearances, team_match_counts in season_raw:
        forwards = forwards.copy()
        forwards["effective_min_minutes"] = forwards["team"].map(team_match_counts).apply(
            lambda team_matches: _scaled_min_minutes(team_matches, reference_matches, min_minutes, absolute_floor)
        )
        forwards = forwards[forwards["minutes_played"] >= forwards["effective_min_minutes"]].copy()
        logger.info(
            "%s: %d forwards clear their team's scaled minutes floor (reference=%d matches)",
            season_cfg["name"],
            len(forwards),
            reference_matches,
        )
        season_pools.append((season_cfg, forwards, actions, appearances))
        all_forward_names |= set(forwards["player_name"].unique())

    birthdates = age.resolve_birthdates(
        sorted(all_forward_names),
        cache_path=Path(paths["age_lookup_cache"]),
        unmatched_log_path=Path(paths["unmatched_players_log"]),
        manual_overrides_path=Path("data/manual_age_overrides.csv"),
    )
    age_cfg = config["age"]

    # Stage 3: per season, attach that season's own age (a player's age changes between
    # seasons, even though their birthdate doesn't), filter to the age band, then compute
    # the full metric table from that season's full actions/appearances, not a
    # pre-filtered subset, since a qualifying player's key pass may assist a shot taken
    # by a teammate who doesn't themselves qualify, and that link would silently drop
    # out of xg_assisted otherwise, before restricting to that season's qualifiers.
    season_metric_tables = []
    for season_cfg, forwards, actions, appearances in season_pools:
        season_start = date.fromisoformat(season_cfg["season_start_date"])
        forwards_with_age = age.attach_age(forwards, birthdates, season_start)
        qualifying = age.filter_age_range(forwards_with_age, age_cfg["min_age"], age_cfg["max_age"])

        full_metrics = metrics.compute_player_metrics(
            actions,
            appearances,
            on_target_outcomes=set(metric_cfg["on_target_outcomes"]),
            penalty_box=metric_cfg["penalty_box"],
            progressive_carry_min_distance=metric_cfg["progressive_carry_min_distance"],
        )
        qualifying_ids = set(qualifying["player_id"])
        season_metrics = full_metrics[full_metrics["player_id"].isin(qualifying_ids)].copy()
        season_metrics = season_metrics.merge(
            qualifying[["player_id", "age", "birth_date", "position"]], on="player_id", how="left"
        )
        season_metrics["season_name"] = season_cfg["name"]
        season_metrics["season_start_date"] = season_cfg["season_start_date"]
        logger.info(
            "%s: %d forwards aged %d-%d after age reconciliation",
            season_cfg["name"],
            len(season_metrics),
            age_cfg["min_age"],
            age_cfg["max_age"],
        )
        season_metric_tables.append(season_metrics)

    # Stage 4: keep players who qualified in enough seasons to call "consistent", and
    # average their per-90 metrics across those seasons.
    consistency_cfg = config["consistency"]
    summary, season_detail = consistency.combine_seasons(
        season_metric_tables, consistency_cfg["min_seasons_qualified"]
    )
    logger.info(
        "%d players qualified in >=%d of %d tracked seasons",
        len(summary),
        consistency_cfg["min_seasons_qualified"],
        len(config["seasons"]),
    )

    # Stage 5: percentile-rank the consistency-filtered, season-averaged pool and build
    # each profile's shortlist.
    all_metric_columns = sorted({m for weights in config["profiles"].values() for m in weights})
    ranked_pool = rank.percentile_rank_metrics(summary, all_metric_columns)

    shortlist_cfg = config["shortlist"]
    shortlists = rank.build_all_shortlists(ranked_pool, config["profiles"], shortlist_cfg["max_size"])
    for profile_name, shortlist in shortlists.items():
        if len(shortlist) < shortlist_cfg["min_size"]:
            logger.warning(
                "Profile '%s' shortlist has only %d players (min_size=%d), qualifying pool may be too thin",
                profile_name,
                len(shortlist),
                shortlist_cfg["min_size"],
            )

    output_path = Path(paths["output_dir"]) / "scouting_report.pdf"
    report.render_report(shortlists, config["profiles"], config, season_detail, output_path)
    logger.info("Report written to %s", output_path)
    return output_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the full scouting pipeline end to end.")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument(
        "--max-matches", type=int, default=None, help="Limit matches fetched per season (for quick smoke tests)"
    )
    args = parser.parse_args()
    run(args.config, args.max_matches)
