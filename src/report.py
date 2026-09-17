"""Render the one-page PDF scouting report.

Layout: a top band (brief + method), three side-by-side columns (one per
profile's shortlist, each player shown as a percentile radar plus key
numbers and a data-driven rationale), and a bottom limitations band. Each
column is wrapped in reportlab's KeepInFrame(mode="shrink") so a shortlist
that's a little too long to fit auto-scales down rather than overflowing
onto a second page.
"""

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
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    FrameBreak,
    Image,
    KeepInFrame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

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

PROFILE_TITLES = {
    "goalscorer": "Goalscorer",
    "creative_wide": "Creative Wide Forward",
    "complete_pressing": "Complete / Pressing Forward",
}

_PAGE_SIZE = landscape(letter)


def _render_radar(player_row: pd.Series, weights: dict[str, float], tmp_dir: Path) -> Path:
    metrics = list(weights.keys())
    labels = [METRIC_LABELS.get(m, m) for m in metrics]
    values = [float(player_row[f"{m}_pctl"]) for m in metrics]

    radar = Radar(
        labels, [0] * len(metrics), [100] * len(metrics), num_rings=3, round_int=[True] * len(metrics)
    )
    fig, ax = radar.setup_axis(figsize=(1.7, 1.7))
    radar.draw_circles(ax=ax, facecolor="#e8e8e8", edgecolor="#999999", lw=0.4)
    radar.draw_radar(
        values,
        ax=ax,
        kwargs_radar={"facecolor": "#2a6f4f", "alpha": 0.6},
        kwargs_rings={"facecolor": "#a8d5ba", "alpha": 0.25},
    )
    radar.draw_param_labels(ax=ax, fontsize=4.5)

    out_path = tmp_dir / f"radar_{player_row['player_id']}_{player_row['profile']}.png"
    fig.savefig(out_path, dpi=200, bbox_inches="tight", transparent=True)
    plt.close(fig)
    return out_path


def generate_rationale(player_row: pd.Series, weights: dict[str, float]) -> str:
    """Data-driven rationale: names the two metrics that contributed most to
    this player's composite score (weight * percentile), so the sentence
    always reflects why THIS player ranked where they did.
    """
    contributions = sorted(
        ((metric, weight * player_row[f"{metric}_pctl"]) for metric, weight in weights.items()),
        key=lambda item: item[1],
        reverse=True,
    )
    top_metrics = [METRIC_LABELS.get(m, m) for m, _ in contributions[:2]]
    age = int(player_row["age"])
    minutes = int(player_row["minutes_played"])
    return (
        f"{_esc(player_row['player_name'])} ({age}, {_esc(player_row['team'])}) ranks here chiefly on "
        f"{top_metrics[0]} (P{player_row[f'{contributions[0][0]}_pctl']:.0f}) and "
        f"{top_metrics[1]} (P{player_row[f'{contributions[1][0]}_pctl']:.0f}) across "
        f"{minutes} qualifying minutes."
    )


def _player_flowable(player_row: pd.Series, weights: dict[str, float], styles: dict, tmp_dir: Path) -> Table:
    radar_path = _render_radar(player_row, weights, tmp_dir)
    stat_lines = [
        f"{METRIC_LABELS.get(m, m)}: {player_row[m]:.2f} (P{player_row[f'{m}_pctl']:.0f})" for m in weights
    ]
    stat_para = Paragraph("<br/>".join(stat_lines), styles["stat"])
    rationale_para = Paragraph(generate_rationale(player_row, weights), styles["rationale"])
    name_para = Paragraph(
        f"<b>{_esc(player_row['player_name'])}</b>, {_esc(player_row['team'])}, age {int(player_row['age'])}",
        styles["player_name"],
    )
    text_block = Table(
        [[name_para], [stat_para], [rationale_para]],
        colWidths=[1.55 * inch],
    )
    text_block.setStyle(
        TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 1)])
    )

    row = Table(
        [[Image(str(radar_path), width=0.85 * inch, height=0.85 * inch), text_block]],
        colWidths=[0.9 * inch, 1.55 * inch],
    )
    row.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#dddddd")),
            ]
        )
    )
    return row


def _build_styles() -> dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle("title", fontSize=15, leading=17, fontName="Helvetica-Bold"),
        "brief": ParagraphStyle("brief", fontSize=7.5, leading=9.5),
        "column_header": ParagraphStyle(
            "column_header", fontSize=9.5, leading=11, fontName="Helvetica-Bold", spaceAfter=2
        ),
        "player_name": ParagraphStyle("player_name", fontSize=7, leading=8.5),
        "stat": ParagraphStyle("stat", fontSize=5.6, leading=7),
        "rationale": ParagraphStyle(
            "rationale", fontSize=5.6, leading=7, textColor=colors.HexColor("#444444")
        ),
        "limitations": ParagraphStyle("limitations", fontSize=6.3, leading=8),
    }


def render_report(
    shortlists: dict[str, pd.DataFrame],
    profiles: dict[str, dict[str, float]],
    config: dict,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    styles = _build_styles()
    page_w, page_h = _PAGE_SIZE
    margin = 0.35 * inch

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        top_h = 0.95 * inch
        bottom_h = 0.55 * inch
        col_gap = 0.15 * inch
        col_w = (page_w - 2 * margin - 2 * col_gap) / 3
        mid_h = page_h - top_h - bottom_h - 2 * margin

        top_frame = Frame(
            margin, page_h - margin - top_h, page_w - 2 * margin, top_h, id="top", showBoundary=0
        )
        bottom_frame = Frame(margin, margin, page_w - 2 * margin, bottom_h, id="bottom", showBoundary=0)
        col_frames = [
            Frame(
                margin + i * (col_w + col_gap),
                margin + bottom_h,
                col_w,
                mid_h,
                id=f"col{i}",
                showBoundary=0,
                leftPadding=2,
                rightPadding=2,
            )
            for i in range(3)
        ]

        doc = BaseDocTemplate(str(output_path), pagesize=_PAGE_SIZE)
        doc.addPageTemplates([PageTemplate(id="report", frames=[top_frame] + col_frames + [bottom_frame])])

        comp = config["competition"]
        age_cfg = config["age"]
        min_minutes = config["playing_time"]["min_minutes"]
        brief = (
            f"<b>Young Forward Scouting Report: {_esc(comp['name'])}</b><br/>"
            f"<b>Brief:</b> identify the strongest forwards aged {age_cfg['min_age']}-{age_cfg['max_age']} "
            f"against three profiles. <b>Method:</b> StatsBomb open-data events flattened to per-action rows; "
            f"a player is classed a forward if a forward position was their <i>starting</i> position in "
            f"≥{int(config['forward_classification']['min_forward_appearance_share']*100)}% of appearances; "
            f"ages resolved via Wikidata and reconciled by name (StatsBomb records full legal names, which rarely "
            f"match a common football name directly); qualifying pool requires ≥{min_minutes} minutes; every "
            f"metric is converted to a percentile within that qualifying pool (not a raw z-score, since shot- and "
            f"pass-volume metrics are heavily right-skewed) and profiles are just different weightings of the same "
            f"percentile table."
        )
        top_story = [Paragraph(brief, styles["brief"])]

        col_story_lists: list[list] = []
        for profile_name, weights in profiles.items():
            shortlist = shortlists[profile_name]
            story = [Paragraph(PROFILE_TITLES.get(profile_name, profile_name), styles["column_header"])]
            for _, player_row in shortlist.iterrows():
                story.append(_player_flowable(player_row, weights, styles, tmp_dir))
                story.append(Spacer(1, 2))
            col_story_lists.append(story)

        limitations = (
            "<b>Limitations:</b> StatsBomb open data covers professional men's football, not NCAA competition, so "
            "metrics here will not transfer directly to a college talent pool without re-baselining the percentile "
            "ranks against that pool. Output reflects team and league context as much as individual skill (a "
            "player in a stronger side sees more high-quality chances and better-quality service). Several "
            "shortlisted players are ranked on well under a full season of minutes; percentile ranks for those "
            "players carry more sampling noise than for a 2000+ minute regular. Ages are joined from Wikidata by "
            "name reconciliation, not a shared player ID, so unresolved names are logged, not silently dropped, "
            "but a reconciliation error would misclassify one player's age band."
        )
        bottom_story = [Paragraph(limitations, styles["limitations"])]

        story = [
            KeepInFrame(top_frame._width, top_frame._height, top_story, mode="shrink"),
            FrameBreak(),
        ]
        for i, col_story in enumerate(col_story_lists):
            story.append(KeepInFrame(col_frames[i]._width, col_frames[i]._height, col_story, mode="shrink"))
            story.append(FrameBreak())
        story.append(KeepInFrame(bottom_frame._width, bottom_frame._height, bottom_story, mode="shrink"))

        doc.build(story)
