from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime, time, timedelta
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from .models import (
    ActivityBucket,
    PilotWeeklyActivity,
    RankingSnapshot,
    ScoreDelta,
    WeeklyActivity,
    history_pilot_key,
)

CARD_SIZE = (900, 650)
ACTIVITY_CARD_SIZE = (1100, 750)
CARD_TIMEZONE = ZoneInfo("Europe/Berlin")
GERMAN_WEEKDAYS = ("MO", "DI", "MI", "DO", "FR", "SA", "SO")


def _font_candidates(*, bold: bool) -> tuple[Path, ...]:
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    if bold:
        names = ("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf")
        linux = (
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
            Path("/usr/share/fonts/cantarell/Cantarell-Bold.otf"),
            Path("/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono-Bold.ttf"),
        )
    else:
        names = ("segoeui.ttf", "arial.ttf")
        linux = (
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
            Path("/usr/share/fonts/cantarell/Cantarell-Regular.otf"),
            Path("/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf"),
        )
    return tuple(windows / name for name in names) + linux


@lru_cache(maxsize=32)
def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in _font_candidates(bold=bold):
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default(size=size)


def _format_score(score: int) -> str:
    return f"{score:,}".replace(",", ".")


def _format_delta(delta: int | None) -> str:
    if delta is None:
        return "—"
    return f"+{_format_score(delta)}"


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    maximum_width: int,
    preferred_size: int,
    minimum_size: int,
    bold: bool,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for size in range(preferred_size, minimum_size - 1, -2):
        font = _font(size, bold=bold)
        if draw.textlength(text, font=font) <= maximum_width:
            return font
    return _font(minimum_size, bold=bold)


def _draw_vertical_gradient(image: Image.Image, start: str, end: str) -> None:
    draw = ImageDraw.Draw(image)
    start_rgb = tuple(bytes.fromhex(start.lstrip("#")))
    end_rgb = tuple(bytes.fromhex(end.lstrip("#")))
    for y in range(image.height):
        ratio = y / max(1, image.height - 1)
        color = tuple(
            round(left + (right - left) * ratio)
            for left, right in zip(start_rgb, end_rgb, strict=True)
        )
        draw.line((0, y, image.width, y), fill=color)


def _centered_y(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, center: int
) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return round(center - (box[3] - box[1]) / 2 - box[1])


def render_ranking_card(
    snapshot: RankingSnapshot,
    *,
    reward_slots: int,
    display_top_n: int,
    evaluation_at: datetime,
    score_deltas: Mapping[str, ScoreDelta] | None = None,
    stale_reason: str | None = None,
) -> bytes:
    visible_entries = snapshot.entries[:display_top_n]
    score_deltas = score_deltas or {}
    image = Image.new("RGB", CARD_SIZE)
    _draw_vertical_gradient(image, "#0A1324", "#172B3E")
    draw = ImageDraw.Draw(image)

    gold = "#E5B93F"
    pale_gold = "#F3D77B"
    white = "#F5F7FA"
    muted = "#9FAFC0"
    line = "#304359"
    row_dark = "#111E30"
    row_light = "#16263A"
    stale = "#E05A4F"
    positive = "#62D08B"
    accent = stale if stale_reason else gold

    draw.rounded_rectangle((18, 18, 882, 632), radius=22, fill="#0D192A", outline=line, width=2)
    draw.rounded_rectangle((18, 18, 26, 632), radius=4, fill=accent)

    title_font = _font(42, bold=True)
    subtitle_font = _font(18, bold=True)
    badge_font = _font(17, bold=True)
    header_font = _font(16, bold=True)
    row_score_font = _font(24, bold=True)
    delta_font = _font(18, bold=True)
    rank_font = _font(20, bold=True)
    footer_font = _font(15, bold=True)

    draw.text((52, 38), snapshot.server.upper(), font=title_font, fill=white)
    draw.text(
        (54, 91),
        f"PILOT-RANGLISTE  ·  TOP {len(visible_entries)}  ·  VERLAUF {len(snapshot.entries)}",
        font=subtitle_font,
        fill=muted,
    )

    division_text = f"DIVISION {snapshot.division}"
    division_width = draw.textlength(division_text, font=badge_font)
    division_box = (828 - division_width, 42, 850, 78)
    draw.rounded_rectangle(division_box, radius=10, fill="#352D18", outline=gold, width=1)
    draw.text((division_box[0] + 12, 49), division_text, font=badge_font, fill=pale_gold)

    reward_text = f"GOLDENE SCHÄDEL  ·  PLÄTZE 1–{reward_slots}"
    reward_width = draw.textlength(reward_text, font=badge_font)
    draw.text((850 - reward_width, 94), reward_text, font=badge_font, fill=pale_gold)

    draw.line((50, 124, 850, 124), fill=line, width=2)
    draw.text((58, 136), "RANG", font=header_font, fill=muted)
    draw.text((132, 136), "PILOT", font=header_font, fill=muted)
    headers = (
        ("WERTUNG", 540),
        ("+15 MIN", 650),
        ("+1 STD", 744),
        ("+24 STD", 838),
    )
    for label, right in headers:
        draw.text(
            (right - draw.textlength(label, font=header_font), 136),
            label,
            font=header_font,
            fill=muted,
        )

    row_top = 164
    row_height = 54
    for index, entry in enumerate(visible_entries):
        top = row_top + index * row_height
        bottom = top + 46
        rewarded = entry.rank <= reward_slots
        fill = "#2A281D" if rewarded else (row_dark if index % 2 == 0 else row_light)
        outline = "#5B4B20" if rewarded else line
        draw.rounded_rectangle(
            (48, top, 852, bottom), radius=12, fill=fill, outline=outline, width=1
        )

        rank_center = (84, top + 23)
        rank_fill = gold if rewarded else "#263B52"
        rank_text_color = "#17140B" if rewarded else white
        draw.ellipse(
            (
                rank_center[0] - 18,
                rank_center[1] - 18,
                rank_center[0] + 18,
                rank_center[1] + 18,
            ),
            fill=rank_fill,
            outline=pale_gold if rewarded else "#496078",
            width=1,
        )
        rank_text = str(entry.rank)
        rank_x = rank_center[0] - draw.textlength(rank_text, font=rank_font) / 2
        draw.text(
            (rank_x, _centered_y(draw, rank_text, rank_font, rank_center[1])),
            rank_text,
            font=rank_font,
            fill=rank_text_color,
        )

        pilot_font = _fit_font(
            draw,
            entry.name,
            maximum_width=280,
            preferred_size=28,
            minimum_size=18,
            bold=rewarded,
        )
        pilot_y = _centered_y(draw, entry.name, pilot_font, top + 23)
        draw.text((132, pilot_y), entry.name, font=pilot_font, fill=white)

        score = _format_score(entry.score)
        score_color = pale_gold if rewarded else white
        score_x = 540 - draw.textlength(score, font=row_score_font)
        score_y = _centered_y(draw, score, row_score_font, top + 23)
        draw.text((score_x, score_y), score, font=row_score_font, fill=score_color)

        delta = score_deltas.get(history_pilot_key(entry.name), ScoreDelta(None, None))
        for value, right in (
            (delta.fifteen_minutes, 650),
            (delta.one_hour, 744),
            (delta.twenty_four_hours, 838),
        ):
            text = _format_delta(value)
            color = positive if value is not None and value > 0 else muted
            x = right - draw.textlength(text, font=delta_font)
            y = _centered_y(draw, text, delta_font, top + 23)
            draw.text((x, y), text, font=delta_font, fill=color)

    draw.line((50, 558, 850, 558), fill=line, width=1)
    evaluation_local = evaluation_at.astimezone(CARD_TIMEZONE)
    captured_local = snapshot.captured_at.astimezone(CARD_TIMEZONE)
    months = ("JAN", "FEB", "MÄR", "APR", "MAI", "JUN", "JUL", "AUG", "SEP", "OKT", "NOV", "DEZ")
    evaluation_text = (
        f"NÄCHSTE AUSWERTUNG  ·  {evaluation_local.day:02d}. "
        f"{months[evaluation_local.month - 1]} {evaluation_local.year}  ·  "
        f"{evaluation_local:%H:%M} UHR"
    )
    updated_label = "LETZTER STAND" if stale_reason else "AKTUALISIERT"
    updated_text = f"{updated_label}  ·  {captured_local:%H:%M} UHR"
    draw.text((52, 574), evaluation_text, font=footer_font, fill=white)
    draw.text(
        (848 - draw.textlength(updated_text, font=footer_font), 574),
        updated_text,
        font=footer_font,
        fill=white,
    )

    footer_y = 606
    status_text = "DATEN VERALTET" if stale_reason else "LIVE · SCREENSHOT-AUSWERTUNG"
    draw.text((52, footer_y), status_text, font=footer_font, fill=accent)
    confidence_text = f"OCR {snapshot.mean_confidence:.0f} %  ·  ALLE 5 MINUTEN"
    draw.text(
        (848 - draw.textlength(confidence_text, font=footer_font), footer_y),
        confidence_text,
        font=footer_font,
        fill=muted,
    )

    output = BytesIO()
    image.save(output, format="PNG", compress_level=6)
    return output.getvalue()


def _activity_color(bucket: ActivityBucket) -> str:
    if bucket.active_intervals >= 4:
        return "#63D58D"
    if bucket.active_intervals >= 2:
        return "#43A875"
    if bucket.active_intervals == 1:
        return "#2F775D"
    if bucket.uncertain_increase:
        return "#9A7432"
    if bucket.observed_intervals:
        return "#16263A"
    return "#35404E"


def _timeline_x(value: datetime, *, start: datetime, end: datetime, left: int, right: int) -> int:
    duration = max(1.0, end.timestamp() - start.timestamp())
    ratio = (value.timestamp() - start.timestamp()) / duration
    return round(left + max(0.0, min(1.0, ratio)) * (right - left))


def _draw_activity_gap(
    draw: ImageDraw.ImageDraw,
    *,
    left: int,
    top: int,
    right: int,
    bottom: int,
    color: str,
) -> None:
    for x in range(left, right + 1, 5):
        draw.line((x, top, x, bottom), fill=color, width=1)


def _last_activity_label(activity: PilotWeeklyActivity | None) -> str:
    if activity is None or activity.last_increase_detected_at is None:
        return "—"
    local = activity.last_increase_detected_at.astimezone(CARD_TIMEZONE)
    prefix = "~ " if activity.last_increase_uncertain else ""
    return f"{prefix}{GERMAN_WEEKDAYS[local.weekday()]} {local:%H:%M}"


def _today_increase_label(activity: PilotWeeklyActivity | None) -> str:
    if activity is None or activity.today_increase is None:
        return "—"
    prefix = "~" if activity.today_increase_uncertain else ""
    return f"{prefix}{_format_delta(activity.today_increase)}"


def _reset_countdown(period_end: datetime, reference: datetime) -> str:
    remaining_seconds = max(0, int(period_end.timestamp() - reference.timestamp()))
    if remaining_seconds == 0:
        return "FÄLLIG"
    days, remainder = divmod(remaining_seconds, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes = remainder // 60
    if days:
        return f"{days}T {hours:02d}H"
    if hours:
        return f"{hours}H {minutes:02d}M"
    return f"{minutes}M"


def render_weekly_activity_card(
    snapshot: RankingSnapshot,
    activity: WeeklyActivity,
    *,
    display_top_n: int,
    reward_slots: int,
    rendered_at: datetime,
    stale_reason: str | None = None,
) -> bytes:
    if rendered_at.tzinfo is None:
        raise ValueError("activity-card rendered_at must include a timezone")

    visible_entries = snapshot.entries[:display_top_n]
    activity_by_key = {pilot.pilot_key: pilot for pilot in activity.pilots}
    image = Image.new("RGB", ACTIVITY_CARD_SIZE)
    _draw_vertical_gradient(image, "#091423", "#162B3D")
    draw = ImageDraw.Draw(image)

    gold = "#E5B93F"
    pale_gold = "#F3D77B"
    white = "#F5F7FA"
    muted = "#9FAFC0"
    line = "#304359"
    row_dark = "#101D2D"
    row_light = "#142438"
    stale = "#E05A4F"
    accent = stale if stale_reason else gold
    timeline_left = 250
    timeline_right = 900
    rows_top = 180
    row_height = 58
    rows_bottom = rows_top + max(0, len(visible_entries) - 1) * row_height + 50

    draw.rounded_rectangle((18, 18, 1082, 732), radius=22, fill="#0D192A", outline=line, width=2)
    draw.rounded_rectangle((18, 18, 26, 732), radius=4, fill=accent)

    title_font = _font(38, bold=True)
    subtitle_font = _font(17, bold=True)
    badge_font = _font(16, bold=True)
    header_font = _font(14, bold=True)
    rank_font = _font(18, bold=True)
    pilot_font_size = 22
    today_font_size = 18
    last_font = _font(13, bold=True)
    now_font = _font(12, bold=True)
    legend_font = _font(13, bold=True)
    summary_font = _font(15, bold=True)
    footer_font = _font(14, bold=True)

    draw.text((52, 35), "KW-AKTIVITÄT", font=title_font, fill=white)
    draw.text(
        (54, 82),
        f"{snapshot.server.upper()}  ·  DIVISION {snapshot.division}  ·  AKTUELLE WERTUNGSWOCHE",
        font=subtitle_font,
        fill=muted,
    )

    period_start_local = activity.period_start.astimezone(CARD_TIMEZONE)
    period_end_local = activity.period_end.astimezone(CARD_TIMEZONE)
    period_label = f"{period_start_local:%d.%m.} – {period_end_local:%d.%m.}"
    period_width = draw.textlength(period_label, font=badge_font)
    period_box = (1048 - period_width - 24, 42, 1050, 78)
    draw.rounded_rectangle(period_box, radius=10, fill="#352D18", outline=gold, width=1)
    draw.text((period_box[0] + 12, 49), period_label, font=badge_font, fill=pale_gold)

    draw.line((50, 118, 1050, 118), fill=line, width=2)
    draw.text((54, 132), "PILOT", font=header_font, fill=muted)
    last_header = "HEUTE  ·  LETZTER +"
    draw.text(
        (1048 - draw.textlength(last_header, font=header_font), 132),
        last_header,
        font=header_font,
        fill=muted,
    )

    axis_start = activity.period_start.astimezone(CARD_TIMEZONE)
    axis_end = activity.period_end.astimezone(CARD_TIMEZONE)
    cursor = datetime.combine(axis_start.date(), time.min, tzinfo=CARD_TIMEZONE)
    while cursor < axis_end:
        next_day = cursor + timedelta(days=1)
        segment_start = max(cursor, axis_start)
        segment_end = min(next_day, axis_end)
        if segment_end > segment_start:
            left = _timeline_x(
                segment_start,
                start=axis_start,
                end=axis_end,
                left=timeline_left,
                right=timeline_right,
            )
            right = _timeline_x(
                segment_end,
                start=axis_start,
                end=axis_end,
                left=timeline_left,
                right=timeline_right,
            )
            label = f"{GERMAN_WEEKDAYS[cursor.weekday()]} {cursor:%d.%m.}"
            label_x = (left + right - draw.textlength(label, font=header_font)) / 2
            draw.text((label_x, 132), label, font=header_font, fill=muted)

        cursor = next_day

    captured_local = activity.captured_at.astimezone(CARD_TIMEZONE)
    reference_local = rendered_at.astimezone(CARD_TIMEZONE)
    current_local = min(max(reference_local, axis_start), axis_end)
    captured_x = _timeline_x(
        captured_local,
        start=axis_start,
        end=axis_end,
        left=timeline_left,
        right=timeline_right,
    )
    current_x = _timeline_x(
        current_local,
        start=axis_start,
        end=axis_end,
        left=timeline_left,
        right=timeline_right,
    )

    marker_label = f"{'STAND' if stale_reason else 'JETZT'} {current_local:%H:%M}"
    marker_width = round(draw.textlength(marker_label, font=now_font)) + 16
    marker_left = max(
        timeline_left,
        min(current_x - marker_width // 2, timeline_right - marker_width),
    )
    draw.rounded_rectangle(
        (marker_left, 151, marker_left + marker_width, 173),
        radius=7,
        fill="#352D18" if not stale_reason else "#3C2628",
        outline=accent,
        width=1,
    )
    draw.text((marker_left + 8, 154), marker_label, font=now_font, fill=pale_gold)

    active_today = 0
    for index, entry in enumerate(visible_entries):
        top = rows_top + index * row_height
        bottom = top + 50
        rewarded = entry.rank <= reward_slots
        fill = "#27261C" if rewarded else (row_dark if index % 2 == 0 else row_light)
        outline = "#5B4B20" if rewarded else line
        draw.rounded_rectangle((48, top, 1052, bottom), radius=10, fill=fill, outline=outline)

        rank_center = (72, top + 25)
        rank_fill = gold if rewarded else "#263B52"
        rank_text_color = "#17140B" if rewarded else white
        draw.ellipse(
            (rank_center[0] - 16, top + 9, rank_center[0] + 16, top + 41),
            fill=rank_fill,
            outline=pale_gold if rewarded else "#496078",
        )
        rank_text = str(entry.rank)
        rank_x = rank_center[0] - draw.textlength(rank_text, font=rank_font) / 2
        draw.text(
            (rank_x, _centered_y(draw, rank_text, rank_font, rank_center[1])),
            rank_text,
            font=rank_font,
            fill=rank_text_color,
        )

        pilot_font = _fit_font(
            draw,
            entry.name,
            maximum_width=138,
            preferred_size=pilot_font_size,
            minimum_size=15,
            bold=rewarded,
        )
        draw.text(
            (100, _centered_y(draw, entry.name, pilot_font, top + 25)),
            entry.name,
            font=pilot_font,
            fill=white,
        )

        pilot_activity = activity_by_key.get(history_pilot_key(entry.name))
        buckets = pilot_activity.buckets if pilot_activity is not None else ()
        bucket_count = max(1, len(buckets))
        lane_top = top + 12
        lane_bottom = bottom - 12
        for bucket_index, bucket in enumerate(buckets):
            left = timeline_left + round(
                bucket_index * (timeline_right - timeline_left) / bucket_count
            )
            right = timeline_left + round(
                (bucket_index + 1) * (timeline_right - timeline_left) / bucket_count
            )
            fill = _activity_color(bucket)
            draw.rectangle((left, lane_top, max(left, right - 1), lane_bottom), fill=fill)
            if bucket.uncertain_increase and bucket.active_intervals == 0:
                _draw_activity_gap(
                    draw,
                    left=left,
                    top=lane_top,
                    right=max(left, right - 1),
                    bottom=lane_bottom,
                    color="#E2B85D",
                )
            elif bucket.has_data_gap and bucket.observed_intervals == 0:
                _draw_activity_gap(
                    draw,
                    left=left,
                    top=lane_top,
                    right=max(left, right - 1),
                    bottom=lane_bottom,
                    color="#7D8996",
                )
            elif bucket.has_data_gap:
                draw.line((left, lane_top, max(left, right - 1), lane_top), fill="#7D8996")

        if current_x < timeline_right:
            draw.rectangle(
                (current_x, lane_top, timeline_right, lane_bottom),
                fill="#091421",
            )
        if stale_reason and captured_x < current_x:
            draw.rectangle((captured_x, lane_top, current_x, lane_bottom), fill="#3C4652")
            _draw_activity_gap(
                draw,
                left=captured_x,
                top=lane_top,
                right=current_x,
                bottom=lane_bottom,
                color="#7D8996",
            )

        if pilot_activity is not None:
            for event in pilot_activity.reward_boundary_events:
                event_x = _timeline_x(
                    event.detected_at.astimezone(CARD_TIMEZONE),
                    start=axis_start,
                    end=axis_end,
                    left=timeline_left,
                    right=timeline_right,
                )
                draw.line((event_x, lane_top, event_x, lane_bottom), fill=gold, width=2)
                if event.entered:
                    draw.polygon(
                        (
                            (event_x, top + 2),
                            (event_x - 5, top + 9),
                            (event_x + 5, top + 9),
                        ),
                        fill=pale_gold,
                    )
                else:
                    draw.polygon(
                        (
                            (event_x - 5, bottom - 9),
                            (event_x + 5, bottom - 9),
                            (event_x, bottom - 2),
                        ),
                        fill=pale_gold,
                    )

        today_label = _today_increase_label(pilot_activity)
        today_font = _fit_font(
            draw,
            today_label,
            maximum_width=128,
            preferred_size=today_font_size,
            minimum_size=14,
            bold=True,
        )
        today_color = (
            "#62D08B"
            if pilot_activity is not None
            and pilot_activity.today_increase is not None
            and pilot_activity.today_increase > 0
            else muted
        )
        if pilot_activity is not None and (pilot_activity.today_increase or 0) > 0:
            active_today += 1
        draw.text(
            (1048 - draw.textlength(today_label, font=today_font), top + 5),
            today_label,
            font=today_font,
            fill=today_color,
        )

        last_label = _last_activity_label(pilot_activity)
        draw.text(
            (1048 - draw.textlength(last_label, font=last_font), top + 31),
            last_label,
            font=last_font,
            fill=pale_gold
            if pilot_activity and pilot_activity.last_increase_detected_at
            else muted,
        )

    cursor_day = datetime.combine(axis_start.date(), time.min, tzinfo=CARD_TIMEZONE)
    while cursor_day < axis_end:
        if axis_start < cursor_day:
            day_x = _timeline_x(
                cursor_day,
                start=axis_start,
                end=axis_end,
                left=timeline_left,
                right=timeline_right,
            )
            draw.line((day_x, rows_top, day_x, rows_bottom), fill="#496078", width=2)
        noon = cursor_day + timedelta(hours=12)
        if axis_start < noon < axis_end:
            noon_x = _timeline_x(
                noon,
                start=axis_start,
                end=axis_end,
                left=timeline_left,
                right=timeline_right,
            )
            for y in range(rows_top + 3, rows_bottom, 8):
                draw.line((noon_x, y, noon_x, min(y + 3, rows_bottom)), fill=line, width=1)
        cursor_day += timedelta(days=1)

    draw.line((current_x, 173, current_x, rows_bottom), fill=accent, width=2)
    draw.polygon(
        ((current_x - 5, 174), (current_x + 5, 174), (current_x, 180)),
        fill=accent,
    )

    draw.line((50, 598, 1050, 598), fill=line, width=1)
    legend_y = 616
    legend_items = (
        ("#2F775D", "1 INTERVALL"),
        ("#43A875", "2–3"),
        ("#63D58D", "4+"),
        ("#16263A", "KEIN ANSTIEG"),
        ("#35404E", "KEINE DATEN"),
        ("#9A7432", "~ DATENLÜCKE"),
    )
    legend_x = 52
    for color, label in legend_items:
        draw.rounded_rectangle(
            (legend_x, legend_y, legend_x + 14, legend_y + 14), radius=3, fill=color
        )
        draw.text((legend_x + 20, legend_y - 2), label, font=legend_font, fill=muted)
        legend_x += round(draw.textlength(label, font=legend_font)) + 38

    draw.polygon(
        ((legend_x + 5, legend_y), (legend_x, legend_y + 7), (legend_x + 10, legend_y + 7)),
        fill=pale_gold,
    )
    draw.polygon(
        (
            (legend_x + 14, legend_y + 7),
            (legend_x + 24, legend_y + 7),
            (legend_x + 19, legend_y + 14),
        ),
        fill=pale_gold,
    )
    draw.text((legend_x + 31, legend_y - 2), "TOP-4-WECHSEL", font=legend_font, fill=pale_gold)

    summary_y = 651
    coverage = (
        f"{activity.capture_coverage_percent} %"
        if activity.capture_coverage_percent is not None
        else "—"
    )
    active_summary = f"HEUTE  {active_today}/{len(visible_entries)} MIT ANSTIEG"
    coverage_summary = f"ABDECKUNG  {coverage}"
    reset_summary = f"RESET IN  {_reset_countdown(activity.period_end, rendered_at)}"
    draw.text((52, summary_y), active_summary, font=summary_font, fill=white)
    coverage_x = 550 - draw.textlength(coverage_summary, font=summary_font) / 2
    draw.text((coverage_x, summary_y), coverage_summary, font=summary_font, fill=muted)
    draw.text(
        (1048 - draw.textlength(reset_summary, font=summary_font), summary_y),
        reset_summary,
        font=summary_font,
        fill=pale_gold,
    )

    footer_y = 705
    status_text = "DATEN VERALTET" if stale_reason else "KW-ANSTIEG AUS 5-MINUTEN-SNAPSHOTS"
    draw.text((52, footer_y), status_text, font=footer_font, fill=accent)
    updated_label = "LETZTER STAND" if stale_reason else "AKTUALISIERT"
    updated_text = f"{updated_label}  ·  {captured_local:%d.%m. %H:%M}  ·  EUROPE/BERLIN"
    draw.text(
        (1048 - draw.textlength(updated_text, font=footer_font), footer_y),
        updated_text,
        font=footer_font,
        fill=muted,
    )

    output = BytesIO()
    image.save(output, format="PNG", compress_level=6)
    return output.getvalue()
