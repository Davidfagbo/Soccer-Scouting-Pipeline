# Renders the scouting report PDF: a short intro (brief, method, one limitations
# section) followed by each profile's top-10 shortlist, flowing across 3 pages.
#
# Ranking is against the consistency-filtered pool from main.py (players who qualified
# as an 18-23 forward with enough minutes in at least `consistency.min_seasons_qualified`
# of the tracked seasons). Per-player context here (age progression, seasons
# qualified, metric trend across seasons) comes from `season_detail`, the underlying
# per-season rows main.py built before averaging.

from __future__ import annotations

import tempfile
from pathlib import Path
from xml.sax.saxutils import escape as _esc

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from mplsoccer import Radar
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Table, TableStyle

METRIC_LABELS = {
    "xg_per90": "xG/90",
    "shots_on_target_per90": "SoT/90",
    "conversion_rate": "Conv. rate",
    "box_touches_per90": "Box touches/90",
    "xg_assisted_per90": "xG assisted/90",
    "key_passes_per90": "Key passes/90",
    "progressive_carries_per90": "Prog. carries/90",
    "dribbles_completed_per90": "Dribbles/90",
    "pressures_per90": "Pressures/90",
    "defensive_actions_per90": "Def. actions/90",
    "pass_completion_pct": "Pass completion %",
}

METRIC_GLOSSARY = (
    "<b>Glossary:</b> xG/90 is StatsBomb's own expected-goals model, not one built for this project. "
    "xG assisted/90 credits the passer when their pass is tagged as a shot assist. Key passes/90 counts "
    "passes leading to a shot; prog. carries/90 counts carries that meaningfully close the distance to "
    "goal; box touches/90 counts any touch inside the penalty box; pressures/90 and def. actions/90 cover "
    "pressing and defensive work. Pxx is a player's percentile within the qualifying pool (0 to 100), used "
    "instead of a raw z-score since shot and pass volume is skewed by a handful of heavy-usage players."
)

PROFILE_TITLES = {
    "goalscorer": "Goalscorer",
    "creative_wide": "Creative Wide Forward",
    "complete_pressing": "Complete / Pressing Forward",
}

PROFILE_BLURBS = {
    "goalscorer": "Ranked on shot quality and volume in and around the box: xG, shots on target, conversion, box touches.",
    "creative_wide": "Ranked on chance creation: xG assisted, key passes, progressive carries, and dribbling.",
    "complete_pressing": "A blend profile: finishing and creation alongside pressing and defensive work rate.",
}

_PAGE_SIZE = letter


# Builds the named paragraph styles used throughout the report.
def _build_styles() -> dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle("title", fontSize=15, leading=18, fontName="Helvetica-Bold", spaceAfter=5),
        "body": ParagraphStyle("body", fontSize=7.8, leading=9.6, spaceAfter=5),
        "profile_header": ParagraphStyle("profile_header", fontSize=13, leading=15, fontName="Helvetica-Bold"),
        "profile_blurb": ParagraphStyle("profile_blurb", fontSize=8, leading=10, textColor=colors.HexColor("#444444"), spaceAfter=3),
        "rank": ParagraphStyle("rank", fontSize=10, leading=11, fontName="Helvetica-Bold"),
        "player_meta": ParagraphStyle("player_meta", fontSize=5.9, leading=7.2, textColor=colors.HexColor("#444444")),
        "stat": ParagraphStyle("stat", fontSize=5.6, leading=6.7),
        "rationale": ParagraphStyle("rationale", fontSize=5.6, leading=6.7, textColor=colors.HexColor("#333333")),
    }


# Renders one player's percentile radar (only the metrics their profile weights) to a
# PNG in `tmp_dir` and returns its path.
def _render_radar(player_row: pd.Series, weights: dict[str, float], tmp_dir: Path) -> Path:
    metric_names = list(weights.keys())
    labels = [METRIC_LABELS.get(m, m) for m in metric_names]
    values = [float(player_row[f"{m}_pctl"]) for m in metric_names]

    radar = Radar(
        labels, [0] * len(metric_names), [100] * len(metric_names), num_rings=3, round_int=[True] * len(metric_names)
    )
    fig, ax = radar.setup_axis(figsize=(1.3, 1.3))
    radar.draw_circles(ax=ax, facecolor="#e8e8e8", edgecolor="#999999", lw=0.3)
    radar.draw_radar(
        values,
        ax=ax,
        kwargs_radar={"facecolor": "#2a6f4f", "alpha": 0.6},
        kwargs_rings={"facecolor": "#a8d5ba", "alpha": 0.25},
    )
    radar.draw_param_labels(ax=ax, fontsize=3.6)

    out_path = tmp_dir / f"radar_{player_row['player_id']}_{player_row['profile']}.png"
    fig.savefig(out_path, dpi=200, bbox_inches="tight", transparent=True)
    plt.close(fig)
    return out_path


# Builds a "0.82 -> 1.15" style trend string for one metric across a player's qualifying
# seasons, oldest first, so a scout can see trajectory rather than just an average.
def _metric_trend(player_id: int, metric: str, season_detail: pd.DataFrame) -> str:
    rows = season_detail[season_detail["player_id"] == player_id].sort_values("season_start_date")
    return " -> ".join(f"{v:.2f}" for v in rows[metric])


# Below this many total qualifying minutes (~3.3 full matches), a player's per-90 rates
# are dominated by single-match variance rather than a real pattern: e.g. one deflected
# shot in 90 minutes alone can produce a top-percentile xG/90. Flagged directly on the
# player row rather than left to a page-1 disclaimer, since this is exactly the kind of
# thing a scout could otherwise mistake for signal.
SMALL_SAMPLE_MINUTES = 300


# Builds a data-driven rationale sentence: names the two metrics that contributed most
# to this player's composite score (weight * percentile), and states how many of the
# tracked seasons they actually qualified in, so the sentence reflects both why they
# ranked where they did and how much of a track record backs the number. Prefixes a
# small-sample warning when total qualifying minutes are thin enough that the ranking
# is likely driven by variance rather than a real pattern.
def generate_rationale(player_row: pd.Series, weights: dict[str, float], seasons_total: int) -> str:
    contributions = sorted(
        ((metric, weight * player_row[f"{metric}_pctl"]) for metric, weight in weights.items()),
        key=lambda item: item[1],
        reverse=True,
    )
    top_metrics = [METRIC_LABELS.get(m, m) for m, _ in contributions[:2]]
    minutes_total = int(player_row["minutes_played_total"])
    warning = (
        f"<font color='#b3541e'><b>Small sample ({minutes_total} min), treat with caution.</b></font> "
        if minutes_total < SMALL_SAMPLE_MINUTES
        else ""
    )
    return (
        f"{warning}Qualified in {int(player_row['seasons_qualified_count'])}/{seasons_total} tracked seasons "
        f"({_esc(player_row['seasons_qualified'])}), {minutes_total} total qualifying "
        f"minutes. Ranks here chiefly on {top_metrics[0]} "
        f"(P{player_row[f'{contributions[0][0]}_pctl']:.0f}) and {top_metrics[1]} "
        f"(P{player_row[f'{contributions[1][0]}_pctl']:.0f})."
    )


# Builds one shortlisted player's full row: rank + radar, name/team/age context, the
# profile's stat block with season trend, and the rationale sentence.
def _player_row(
    rank_num: int,
    player_row: pd.Series,
    weights: dict[str, float],
    styles: dict,
    tmp_dir: Path,
    season_detail: pd.DataFrame,
    seasons_total: int,
) -> Table:
    radar_path = _render_radar(player_row, weights, tmp_dir)
    rank_block = Table(
        [[Paragraph(f"#{rank_num}", styles["rank"])], [Image(str(radar_path), width=0.62 * inch, height=0.62 * inch)]],
        colWidths=[0.7 * inch],
    )
    rank_block.setStyle(
        TableStyle(
            [
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    # Mixed-font-size Paragraphs (rather than nested sub-tables for each column) so no
    # extra table cell padding accumulates per player row. With up to 10 rows needing
    # to fit on one page, that padding was the difference between fitting in 4 pages and
    # overflowing to 7.
    meta_para = Paragraph(
        f"<font size='7.6'><b>{_esc(player_row['player_name'])}</b></font><br/>"
        f"{_esc(player_row['position'])}<br/>"
        f"{_esc(player_row['team'])} &middot; age {int(player_row['age'])}<br/>"
        f"Age by season: {player_row['age_progression']}",
        styles["player_meta"],
    )

    top_metric = next(iter(weights))
    stat_lines = [
        f"{METRIC_LABELS.get(m, m)}: {player_row[m]:.2f} (P{player_row[f'{m}_pctl']:.0f})" for m in weights
    ]
    stat_para = Paragraph(
        "<br/>".join(stat_lines)
        + f"<br/><font color='#2a6f4f'>{METRIC_LABELS.get(top_metric, top_metric)} by season: "
        f"{_metric_trend(player_row['player_id'], top_metric, season_detail)}</font>",
        styles["stat"],
    )

    rationale_para = Paragraph(generate_rationale(player_row, weights, seasons_total), styles["rationale"])

    row = Table(
        [[rank_block, meta_para, stat_para, rationale_para]],
        colWidths=[0.75 * inch, 1.85 * inch, 2.15 * inch, 2.75 * inch],
    )
    row.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#dddddd")),
            ]
        )
    )
    return row


# Builds the short intro block: brief, a few method notes, one limitations paragraph,
# and a compact glossary. Kept deliberately tight (a few sentences per part, not a full
# writeup) since the detailed reasoning for these choices lives in the README, and this
# report needs to leave most of its 3 pages for the actual shortlists.
def _title_page_story(config: dict, season_detail: pd.DataFrame, styles: dict) -> list:
    comp = config["competition"]
    age_cfg = config["age"]
    seasons = config["seasons"]
    min_minutes = config["playing_time"]["min_minutes"]

    season_names = ", ".join(s["name"] for s in seasons)
    total_players = season_detail["player_id"].nunique()

    return [
        Paragraph(f"Young Forward Scouting Report: {_esc(comp['name'])}, {_esc(season_names)}", styles["title"]),
        Paragraph(
            f"<b>Methodology:</b> forwards aged {age_cfg['min_age']} to {age_cfg['max_age']} across "
            f"{len(seasons)} La Liga seasons ({season_names}), ranked against three role profiles. A player "
            f"counts as a forward if that was their starting position in most of their appearances, and needs "
            f"to qualify in at least one tracked season to appear here. Ages come from Wikidata, matched by "
            f"name, since FBref blocks scraping. The {min_minutes}-minute floor is scaled down per team for a "
            f"season only partially covered in the data (as low as "
            f"{config['playing_time']['min_minutes_absolute_floor']} minutes), so thin seasons stay usable "
            f"without letting a single cameo through. Every stat is a percentile within the {total_players}-player "
            f"qualifying pool rather than a raw number, since shot and pass volume is skewed by a handful of "
            f"heavy-usage players.",
            styles["body"],
        ),
        Paragraph(
            f"<b>Limitations:</b> {seasons[1]['name']} and {seasons[2]['name']} only cover Barcelona's matches "
            f"plus each opponent's two games against them, so non-Barcelona results in those seasons rest on a "
            f"much smaller sample; anyone under 300 total minutes is flagged below. These numbers reflect team "
            f"and league strength as much as individual skill and won't transfer to an NCAA pool without "
            f"re-baselining. Age matching is by name, not a shared ID, so a same-name mix-up is possible; "
            f"unmatched names are logged, not dropped.",
            styles["body"],
        ),
        Paragraph(METRIC_GLOSSARY, styles["body"]),
    ]


# Builds the full multi-page PDF and writes it to `output_path`.
def render_report(
    shortlists: dict[str, pd.DataFrame],
    profiles: dict[str, dict[str, float]],
    config: dict,
    season_detail: pd.DataFrame,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    styles = _build_styles()
    seasons_total = len(config["seasons"])

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        doc = SimpleDocTemplate(
            str(output_path),
            pagesize=_PAGE_SIZE,
            leftMargin=0.5 * inch,
            rightMargin=0.5 * inch,
            topMargin=0.5 * inch,
            bottomMargin=0.5 * inch,
        )

        story = _title_page_story(config, season_detail, styles)

        # No forced page breaks between profiles: letting the shortlists flow
        # naturally is what keeps this to 3 pages instead of 4. Each profile's
        # header stays glued to its first player row (KeepTogether) so a header
        # never ends up alone at the bottom of a page with its list on the next.
        for profile_name, weights in profiles.items():
            header = [
                Paragraph(PROFILE_TITLES.get(profile_name, profile_name), styles["profile_header"]),
                Paragraph(PROFILE_BLURBS.get(profile_name, ""), styles["profile_blurb"]),
            ]
            rows = [
                _player_row(rank_num, player_row, weights, styles, tmp_dir, season_detail, seasons_total)
                for rank_num, (_, player_row) in enumerate(shortlists[profile_name].iterrows(), start=1)
            ]
            first_row = rows[:1]
            story.append(KeepTogether(header + first_row))
            story.extend(rows[1:])

        doc.build(story)
