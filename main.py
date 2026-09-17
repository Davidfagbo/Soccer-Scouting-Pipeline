"""Run the full scouting pipeline end to end: load -> events -> age -> metrics -> rank -> report."""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

import yaml

from src import age, events, load, metrics, rank, report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("pipeline")


def run(config_path: Path, max_matches: int | None = None) -> Path:
    config = yaml.safe_load(config_path.read_text())

    comp = config["competition"]
    paths = config["paths"]
    cache_dir = Path(paths["cache_dir"])

    logger.info(
        "Loading %s (competition_id=%s, season_id=%s)",
        comp["name"],
        comp["competition_id"],
        comp["season_id"],
    )
    _matches, events_by_match, lineups_by_match = load.load_season(
        comp["competition_id"], comp["season_id"], cache_dir, max_matches=max_matches
    )

    logger.info("Flattening events and computing appearances for %d matches", len(events_by_match))
    actions, appearances = events.build_season_tables(events_by_match, lineups_by_match)

    fwd_cfg = config["forward_classification"]
    players = events.classify_forwards(
        appearances, set(fwd_cfg["positions"]), fwd_cfg["min_forward_appearance_share"]
    )
    forwards = players[players["is_forward"]].copy()
    logger.info("%d of %d players classify as forwards", len(forwards), len(players))

    min_minutes = config["playing_time"]["min_minutes"]
    forwards = forwards[forwards["minutes_played"] >= min_minutes].copy()
    logger.info("%d forwards clear the %d-minute floor", len(forwards), min_minutes)

    birthdates = age.resolve_birthdates(
        sorted(forwards["player_name"].unique()),
        cache_path=Path(paths["age_lookup_cache"]),
        unmatched_log_path=Path(paths["unmatched_players_log"]),
        manual_overrides_path=Path("data/manual_age_overrides.csv"),
    )
    age_cfg = config["age"]
    season_start = date.fromisoformat(comp["season_start_date"])
    forwards_with_age = age.attach_age(forwards, birthdates, season_start)
    qualifying_pool = age.filter_age_range(forwards_with_age, age_cfg["min_age"], age_cfg["max_age"])
    logger.info(
        "%d forwards aged %d-%d after age reconciliation (%d had no resolved birthdate)",
        len(qualifying_pool),
        age_cfg["min_age"],
        age_cfg["max_age"],
        forwards_with_age["age"].isna().sum(),
    )

    # Compute metrics from the full season's actions/appearances, not a
    # pre-filtered subset: a qualifying player's key pass may assist a shot
    # taken by a teammate who doesn't themselves qualify, and that link would
    # silently drop out of xg_assisted if non-qualifying players' shots were
    # filtered out before the join. Restrict to the qualifying pool only
    # after the full metric table is built.
    metric_cfg = config["metrics"]
    all_player_metrics = metrics.compute_player_metrics(
        actions,
        appearances,
        on_target_outcomes=set(metric_cfg["on_target_outcomes"]),
        penalty_box=metric_cfg["penalty_box"],
        progressive_carry_min_distance=metric_cfg["progressive_carry_min_distance"],
    )
    qualifying_ids = set(qualifying_pool["player_id"])
    player_metrics = all_player_metrics[all_player_metrics["player_id"].isin(qualifying_ids)].copy()
    player_metrics = player_metrics.merge(
        qualifying_pool[["player_id", "age", "birth_date"]], on="player_id", how="left"
    )

    all_metric_columns = sorted({m for weights in config["profiles"].values() for m in weights})
    ranked_pool = rank.percentile_rank_metrics(player_metrics, all_metric_columns)

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
    report.render_report(shortlists, config["profiles"], config, output_path)
    logger.info("Report written to %s", output_path)
    return output_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument(
        "--max-matches", type=int, default=None, help="Limit matches fetched (for quick smoke tests)"
    )
    args = parser.parse_args()
    run(args.config, args.max_matches)
