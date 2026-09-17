# Soccer Scouting Pipeline

Identifies the strongest young forwards (18–23) from StatsBomb open data and
ranks them against three configurable forward profiles, producing a one-page
PDF scouting report.

This is a **method demonstration**. It is deliberately structured so the
StatsBomb loader (`src/load.py`) is the only module that knows where the
event data comes from: swapping in an NCAA D1 feed later means replacing
`load.py` (and possibly `age.py`'s source, if NCAA age data needs a different
lookup) without touching `events.py`, `metrics.py`, `rank.py`, or `report.py`.

## Why La Liga 2015/16, and why Wikidata instead of FBref

Explored three full 380-match seasons (Premier League, La Liga, Serie A, all
2015/16, the only StatsBomb open-data competitions that are complete domestic
seasons rather than single-team subsets). All three had a comparable forward
pool (138–163 players before age/minutes filtering); La Liga 2015/16 was
selected.

FBref, the age source named in the original brief, sits behind a Cloudflare
JS challenge and rejects plain HTTP requests outright (confirmed by direct
test: even a full browser User-Agent gets a "Just a moment..." interstitial,
not the stats page). Rather than add a headless-browser dependency
(Playwright + ~300MB of browser binaries) to solve a CAPTCHA, `age.py` sources
birthdates from **Wikidata**: same join-by-name architecture, same
reconciliation problem (StatsBomb records full legal names, e.g. "Luis
Alberto Suárez Díaz" rather than "Luis Suárez", that rarely match a Wikidata
page's primary label), but a source that's actually scrapeable and returns
structured data.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python main.py
```

First run fetches and caches ~380 matches' worth of events + lineups from
StatsBomb's open-data repo (several minutes) and resolves forward ages
against Wikidata (~150 names, rate-limited to stay polite, also a few
minutes). Both are cached to `data/`, so subsequent runs are fast. Output:
`output/scouting_report.pdf`.

For a quick smoke test on a handful of matches instead of the full season:

```bash
python main.py --max-matches 8
```

## Pipeline

```
load.py    → fetch matches/events/lineups from StatsBomb open data, cached locally
events.py  → flatten events to a tidy per-action table; compute minutes played and
             a forward classification per player from lineup position segments
age.py     → reconcile player names against Wikidata birthdates, filter to 18-23
metrics.py → aggregate actions to per-90 metrics for the qualifying pool (computed
             once; every profile just reweights the same table)
rank.py    → percentile-rank each metric within the qualifying pool, apply each
             profile's weights, produce a 3-5 player shortlist per profile
report.py  → render the one-page PDF (brief, method, three shortlists with
             percentile radars and data-driven rationale, limitations)
```

## Known gotchas this pipeline handles explicitly

1. **Position is per-appearance, not per-player.** A player is classified as
   a forward if a forward position was their *starting* position (the
   position they entered the pitch in, whether from kickoff or as a sub) in
   at least `forward_classification.min_forward_appearance_share` of their
   own appearances, not by filtering on a single lineup tag. See
   `events.classify_forwards`.
2. **xG is StatsBomb's own `shot_statsbomb_xg`.** No xG model is built here.
3. **Age is not in StatsBomb data and FBref can't be scraped.** `age.py`
   resolves names against Wikidata's search API (which indexes aliases, so
   "Francisco Román Alarcón Suárez" correctly resolves to Isco), verifies
   each match against a football-occupation description, and logs anything
   it can't confidently resolve to `data/unmatched_players.csv` instead of
   dropping it. To fix an unmatched player, add a row (with their real
   birth date) to `data/manual_age_overrides.csv`; it's consulted before
   Wikidata and always wins, so the fix persists across reruns.
4. **Small samples.** `playing_time.min_minutes` (default 600) keeps thin
   sample sizes out of the qualifying pool; every metric is percentile-ranked
   within that pool rather than z-scored, since shot- and pass-volume metrics
   are heavily right-skewed and a z-score would let one outlier distort the
   scale.

## Configuration

Everything that should change without touching code lives in `config.yaml`:
competition/season, age band, minutes floor, forward-position list and share
threshold, metric definitions (progressive-carry distance, penalty-box
coordinates, on-target shot outcomes), the three profile weight sets, and
shortlist size. Profile weights should sum to ~1.0 per profile for the
composite score to stay interpretable as a weighted-average percentile.

## Data directory

```
data/
  raw/                       cached StatsBomb pulls (matches, events, lineups), gitignored
  age_lookup.csv             cached Wikidata name → birthdate resolutions, gitignored
  unmatched_players.csv      names Wikidata couldn't confidently resolve, gitignored, regenerated each run
  manual_age_overrides.csv   optional, user-maintained corrections (statsbomb_name, birth_date)
```

## Limitations

- StatsBomb open data covers professional men's football. Percentile ranks
  are relative to *this* qualifying pool; they will not transfer to an NCAA
  pool without re-baselining once the loader is swapped.
- Output is shaped by team and league context as much as individual skill: a
  player in a stronger side sees more, and better, service.
- Several shortlisted players will be well under a full season of minutes;
  their percentile ranks carry more sampling noise than a 2000+ minute
  regular's.
- Age reconciliation is by name, not a shared player ID, so a mismatch is
  possible for a common name even when a QID is found. Unmatched names are
  logged for manual review rather than silently dropped, but a false-positive
  match would not be caught the same way, so spot-check `age_lookup.csv` for
  shortlisted players if the age band matters for a specific decision.
