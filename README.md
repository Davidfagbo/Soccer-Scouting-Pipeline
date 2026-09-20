# Soccer Scouting Pipeline

Identifies the strongest young forwards (18–23) from StatsBomb open data,
checked for consistency across three La Liga seasons (2015/16–2017/18), and
ranks them against three configurable forward profiles, producing a
multi-page PDF scouting report (top 10 per profile, budgeted at 4 pages).

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

## ⚠️ Known limitation: uneven season coverage, and how it's mitigated

This is the most important thing to understand before trusting this report's
output, so it's stated up front rather than buried.

`config.yaml`'s `seasons` list tracks three La Liga seasons (2015/16,
2016/17, 2017/18) so a player has to show up as a qualifying forward in
`consistency.min_seasons_qualified` (default 2) of them to make the report at
all: the idea being to surface players with a track record, not a one-season
fluke. **Only 2015/16 is a complete season in StatsBomb's open data.**
2016/17 and 2017/18 are **Barcelona-only releases**: StatsBomb published
every Barcelona match in those seasons, and since a match has two teams,
every *other* club's players ended up in the data too, but only for the 2
matches (home + away) their team played against Barcelona that year.

**A flat minutes floor would make this unusable.** No non-Barcelona forward
can clear a flat 600-minute floor (`playing_time.min_minutes`) on 2 matches
(180 possible minutes): the very first version of this pipeline applied the
floor that way, and it produced exactly one qualifying player across all
three profiles (Francisco Alcácer García, a Barcelona forward), because he
was essentially the only non-Barcelona-first-team player who happened to
clear the bar in more than one season.

**Fix: the minutes floor scales to how much of a player's team's season is
actually in the dataset.** `main.py` computes, per season, how many matches
each team has on record (`events.count_team_matches`), and scales
`min_minutes` down proportionally for a team with fewer matches available,
relative to a reference of 38 matches (a full La Liga season), never below
`playing_time.min_minutes_absolute_floor` (default 90 minutes, about one full
match), so a single substitute cameo still can't qualify just because a
club's season is barely represented. Concretely: a club with only 2 of 38
matches on record is held to `max(90, 600 × 2/38)` ≈ 90 minutes for that
season, not 600.

With that fix, the default config produces **5 qualifying players spanning 4
different clubs** (Barcelona, Sevilla, Athletic Club, Real Sociedad, Atlético
Madrid) rather than 1: a materially better, more representative result. It
does not fully close the gap, though: a non-Barcelona player's qualifying
seasons still rest on far fewer minutes than a full campaign, so their
percentile ranks carry more sampling noise than a player with a complete
season behind them. Read the report's own "Data limitation" section (page 1
of the PDF) for the same explanation in scout-facing language.

**This was a deliberate choice, made after checking for a better dataset and
not finding one.** Before building this, I checked whether a different free
source could provide genuine full-match-coverage data across 3 consecutive
league seasons:

- **FBref**: blocked by a Cloudflare JS challenge (see above), not
  reachable by a simple request at all.
- **Understat**: reachable, but the actual player/team stats aren't in the
  initial page HTML: Understat loads that data client-side after the page
  loads, so a plain fetch only returns a near-empty shell. Getting real data
  out would mean reverse-engineering their internal API. Even if that
  worked, Understat only exposes match-result-level stats (goals, assists,
  minutes, their own xG model), no shot locations, no pressures, no
  progressive-carry data, so it couldn't feed this pipeline's location-based
  metrics (box touches, progressive carries, pressures/90) even in principle.

No other free source with StatsBomb's granularity (event-level, pitch
coordinates) and full match coverage across 3 consecutive seasons of one
league was found. Given the choice between waiting on a dataset that doesn't
exist publicly and shipping the pipeline against what's actually available,
this project proceeds with StatsBomb's open data, mitigated as above.

**If you want a different tradeoff:** `consistency.min_seasons_qualified`
(set to 1 to drop the multi-season requirement entirely and rank each
season's qualifiers on their own merits), `playing_time.min_minutes_absolute_floor`
(raise it to demand more minutes even from thin seasons, at the cost of
excluding more players), or removing 2016/17 and 2017/18 from `seasons`
entirely for a single-season report.

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

First run fetches and caches each configured season's matches, events, and
lineups from StatsBomb's open-data repo (the full 380-match 2015/16 season
takes a few minutes; the two partial seasons are fast, ~35 matches each) and
resolves every forward's age against Wikidata once, across all seasons
(rate-limited to stay polite). Everything is cached to `data/`, so
subsequent runs are fast. Output: `output/scouting_report.pdf`.

For a quick smoke test on a handful of matches per season instead of full
seasons:

```bash
python main.py --max-matches 8
```

## Pipeline

```
load.py         → fetch matches/events/lineups from StatsBomb open data, cached locally
events.py       → flatten events to a tidy per-action table; compute minutes played and
                   a forward classification per player from lineup position segments
age.py          → reconcile player names against Wikidata birthdates
metrics.py      → aggregate actions to per-90 metrics for a season's qualifying pool
                   (computed once per season; every profile just reweights the same table)
consistency.py  → combine each season's qualifying pool: keep players who qualified in
                   enough seasons, average their per-90 metrics across those seasons
rank.py         → percentile-rank each metric within the consistency-filtered pool, apply
                   each profile's weights, produce a top-10 shortlist per profile
report.py       → render the multi-page PDF (title/method/legend page, then one page per
                   profile with percentile radars, season trend, and data-driven rationale)
```

`main.py` runs the per-season stages (load → events → forward classification
→ scaled minutes floor → age filter → metrics) once for each season in
`config.yaml`, then hands all of them to `consistency.py` before ranking.

## Known gotchas this pipeline handles explicitly

1. **Position is per-appearance, not per-player.** A player is classified as
   a forward if a forward position was their *starting* position (the
   position they entered the pitch in, whether from kickoff or as a sub) in
   at least `forward_classification.min_forward_appearance_share` of their
   own appearances *in that season*, not by filtering on a single lineup
   tag. See `events.classify_forwards`.
2. **xG is StatsBomb's own `shot_statsbomb_xg`.** No xG model is built here.
3. **Age is not in StatsBomb data and FBref can't be scraped.** `age.py`
   resolves names against Wikidata's search API (which indexes aliases, so
   "Francisco Román Alarcón Suárez" correctly resolves to Isco), verifies
   each match against a football-occupation description, and logs anything
   it can't confidently resolve to `data/unmatched_players.csv` instead of
   dropping it. To fix an unmatched player, add a row (with their real
   birth date) to `data/manual_age_overrides.csv`; it's consulted before
   Wikidata and always wins, so the fix persists across reruns. A player's
   age is recomputed per season (their birthdate doesn't change, but their
   age does), so the report can show an age progression across seasons.
4. **Small samples.** `playing_time.min_minutes` (default 600) keeps thin
   sample sizes out of each season's qualifying pool; every metric is
   percentile-ranked within the consistency-filtered pool rather than
   z-scored, since shot- and pass-volume metrics are heavily right-skewed
   and a z-score would let one outlier distort the scale. That said, the
   composite score used for ranking does **not** discount for sample size,
   so a player admitted only via the scaled minutes floor (see below) can
   still rank #1 in a profile off a handful of matches. `report.py` flags
   any player under `SMALL_SAMPLE_MINUTES` (300, ~3.3 matches) with a visible
   "Small sample, treat with caution" warning on their row, but that's a
   read-the-fine-print safeguard, not a ranking adjustment: check for the
   flag before trusting a top rank at face value.
5. **Multi-season data coverage is uneven, and a flat minutes floor would
   make the consistency check useless.** See the "Known limitation" section
   above: `main.py` scales `min_minutes` per player by how much of their
   team's season is actually in the dataset (`events.count_team_matches`),
   with an absolute floor (`playing_time.min_minutes_absolute_floor`) so a
   single cameo still can't qualify.

## Configuration

Everything that should change without touching code lives in `config.yaml`:
which seasons to track, the consistency threshold, age band, minutes floor
(plus its absolute floor when scaled down for a partial season), forward-
position list and share threshold, metric definitions (progressive-carry
distance, penalty-box coordinates, on-target shot outcomes), the three
profile weight sets, and shortlist size. Profile weights should sum to ~1.0
per profile for the composite score to stay interpretable as a
weighted-average percentile.

## Data directory

```
data/
  raw/                       cached StatsBomb pulls (matches, events, lineups), gitignored
  age_lookup.csv             cached Wikidata name → birthdate resolutions, gitignored
  unmatched_players.csv      names Wikidata couldn't confidently resolve, gitignored, regenerated each run
  manual_age_overrides.csv   optional, user-maintained corrections (statsbomb_name, birth_date)
```

## Limitations

- **Season coverage across the 3 tracked years is uneven, mitigated by
  scaling the minutes floor (see the dedicated section above).** This is the
  limitation to understand before trusting the report's shortlists.
- StatsBomb open data covers professional men's football. Percentile ranks
  are relative to *this* qualifying pool; they will not transfer to an NCAA
  pool without re-baselining once the loader is swapped.
- Output is shaped by team and league context as much as individual skill: a
  player in a stronger side sees more, and better, service.
- Several shortlisted players will be well under a full season of minutes in
  any given season; their percentile ranks carry more sampling noise than a
  2000+ minute regular's.
- Age reconciliation is by name, not a shared player ID, so a mismatch is
  possible for a common name even when a QID is found. Unmatched names are
  logged for manual review rather than silently dropped, but a false-positive
  match would not be caught the same way, so spot-check `age_lookup.csv` for
  shortlisted players if the age band matters for a specific decision.
