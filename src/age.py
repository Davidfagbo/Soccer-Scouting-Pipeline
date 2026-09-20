# Reconciles StatsBomb player names against birthdates and filters to an age range.

# StatsBomb events carry full legal names (gotcha #3): Spanish players in
# particular are tagged with paternal+maternal surnames ("Francisco Roman
# Alarcon Suarez") that never match a football-common-name label ("Isco").
# FBref itself is not usable here: it sits behind a Cloudflare JS challenge
# that blocks plain HTTP requests, so this module resolves ages from Wikidata's
# structured player data instead (same join-by-name architecture the brief
# asked for, just a scrape-friendly source).

# Reconciliation goes through Wikidata's full-text search API, which indexes
# aliases and redirects, rather than exact label matching. Every candidate is
# verified against a football-occupation description before being trusted.
# Names that still can't be confidently resolved are logged for manual review
# rather than silently dropped.

from __future__ import annotations

import csv
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

USER_AGENT = "SoccerScoutingPipeline/1.0 (method-demonstration; contact via repo issues)"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
FOOTBALL_DESC_KEYWORDS = ("footballer", "football player", "soccer player")
MAX_RETRIES = 3


# Sends a GET request to the Wikidata API and returns the parsed JSON body, retrying with backoff on HTTP/URL errors (honoring a Retry-After header when one is given).
def _request_json(params: dict) -> dict:
    url = WIKIDATA_API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            last_error = exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            wait = float(retry_after) if retry_after else 5 * (2**attempt)
            logger.debug("Wikidata HTTP %s, backing off %.1fs", exc.code, wait)
            time.sleep(wait)
        except urllib.error.URLError as exc:
            last_error = exc
            time.sleep(2**attempt)
    raise RuntimeError(f"Wikidata request failed after {MAX_RETRIES} attempts: {last_error}")


# Returns the QID of the best football-player match for `name`, or None.
def _search_wikidata(name: str) -> str | None:
    result = _request_json(
        {
            "action": "wbsearchentities",
            "search": name,
            "language": "en",
            "type": "item",
            "limit": 5,
            "format": "json",
        }
    )
    for candidate in result.get("search", []):
        description = (candidate.get("description") or "").lower()
        if any(keyword in description for keyword in FOOTBALL_DESC_KEYWORDS):
            return candidate["id"]
    return None


# Picks the claim Wikidata itself prefers: a 'preferred'-rank claim if one exists,
# otherwise the first non-deprecated claim. Some contested-birthdate players carry
# more than one P569 statement.
def _best_birthdate_claim(claims: list[dict]) -> dict | None:
    preferred = [c for c in claims if c.get("rank") == "preferred"]
    if preferred:
        return preferred[0]
    normal = [c for c in claims if c.get("rank") != "deprecated"]
    return normal[0] if normal else None


# Batch-fetches P569 (date of birth) for up to 50 QIDs per call. Only day-precision
# claims (Wikidata precision=11) are accepted: a year- or month-only birthdate can't
# compute an accurate age, and silently defaulting the missing day to the 1st could
# misclassify a player's age band by up to ~11 months. Anything less precise is left
# unresolved so it surfaces in the unmatched-players log instead.
def _fetch_birthdates(qids: list[str]) -> dict[str, date]:
    birthdates: dict[str, date] = {}
    for i in range(0, len(qids), 50):
        batch = qids[i : i + 50]
        result = _request_json(
            {"action": "wbgetentities", "ids": "|".join(batch), "props": "claims", "format": "json"}
        )
        for qid, entity in result.get("entities", {}).items():
            claims = entity.get("claims", {}).get("P569")
            if not claims:
                continue
            claim = _best_birthdate_claim(claims)
            if claim is None:
                continue
            value = claim["mainsnak"]["datavalue"]["value"]
            if value.get("precision") != 11:
                continue
            raw = value["time"]  # e.g. '+1992-04-21T00:00:00Z'
            birthdates[qid] = date.fromisoformat(raw[1:11])
    return birthdates


# Loads a two-column {name -> birth_date} CSV (used for both the resolution cache and
# the manual-overrides file), skipping rows with no birth_date filled in.
def _load_name_date_csv(path: Path, name_col: str) -> dict[str, date]:
    if not path.exists():
        return {}
    out: dict[str, date] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("birth_date"):
                out[row[name_col]] = date.fromisoformat(row["birth_date"])
    return out


# Merges newly-resolved entries into the existing cache file and rewrites it sorted by name.
def _save_cache(entries: dict[str, date], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {**_load_name_date_csv(path, "player_name"), **entries}
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["player_name", "birth_date"])
        for name, birth_date in sorted(merged.items()):
            writer.writerow([name, birth_date.isoformat()])


# Writes the names that couldn't be resolved to a CSV for manual review, with an
# empty birth_date column ready to be filled in and moved to the manual-overrides file.
def _log_unmatched(names: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["statsbomb_name", "birth_date"])
        for name in sorted(names):
            writer.writerow([name, ""])
    logger.warning(
        "%d players could not be matched to a birthdate; logged to %s. "
        "Fill in birth_date there (or in the manual-overrides file) and rerun.",
        len(names),
        path,
    )


# Resolves StatsBomb player names to birthdates. Manual overrides (if present) always
# win, then the local cache, then a live Wikidata lookup. Returns columns
# [player_name, birth_date, source]. Unresolved names are written to
# `unmatched_log_path`, not dropped.
def resolve_birthdates(
    player_names: list[str],
    cache_path: Path,
    unmatched_log_path: Path,
    manual_overrides_path: Path | None = None,
    request_pause_seconds: float = 0.5,
) -> pd.DataFrame:
    manual = _load_name_date_csv(manual_overrides_path, "statsbomb_name") if manual_overrides_path else {}
    cache = _load_name_date_csv(cache_path, "player_name")

    resolved: dict[str, tuple[date, str]] = {}
    unresolved: list[str] = []
    for name in player_names:
        if name in manual:
            resolved[name] = (manual[name], "manual_override")
        elif name in cache:
            resolved[name] = (cache[name], "wikidata_cached")
        else:
            unresolved.append(name)

    if unresolved:
        logger.info("Resolving %d player names against Wikidata", len(unresolved))
        qid_by_name: dict[str, str] = {}
        for name in unresolved:
            qid = _search_wikidata(name)
            if qid:
                qid_by_name[name] = qid
            time.sleep(request_pause_seconds)

        birthdates = _fetch_birthdates(sorted(set(qid_by_name.values())))
        newly_resolved = {}
        for name, qid in qid_by_name.items():
            if qid in birthdates:
                resolved[name] = (birthdates[qid], "wikidata")
                newly_resolved[name] = birthdates[qid]
        _save_cache(newly_resolved, cache_path)

    still_unmatched = [name for name in player_names if name not in resolved]
    if still_unmatched:
        _log_unmatched(still_unmatched, unmatched_log_path)

    return pd.DataFrame(
        [{"player_name": name, "birth_date": bd, "source": src} for name, (bd, src) in resolved.items()],
        columns=["player_name", "birth_date", "source"],
    )


# Computes age in whole years as of `reference_date`, given a birth date.
def _age_on(birth_date: date, reference_date: date) -> int:
    years = reference_date.year - birth_date.year
    if (reference_date.month, reference_date.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years


# Joins `birthdates` onto `players` (on player_name) and computes age as of
# `reference_date`. Rows with no resolved birthdate keep age=NaN, so callers can
# distinguish "too old/young" from "unresolved" rather than dropping either silently.
def attach_age(players: pd.DataFrame, birthdates: pd.DataFrame, reference_date: date) -> pd.DataFrame:
    merged = players.merge(birthdates[["player_name", "birth_date", "source"]], on="player_name", how="left")
    merged["age"] = merged["birth_date"].map(lambda bd: _age_on(bd, reference_date) if pd.notna(bd) else None)
    return merged


# Keeps only players with a resolved age inside [min_age, max_age].
def filter_age_range(players: pd.DataFrame, min_age: int, max_age: int) -> pd.DataFrame:
    return players[players["age"].notna() & players["age"].between(min_age, max_age)].copy()
