from datetime import UTC, datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from PIL import Image

from pg_rankings.models import (
    ActivityBucket,
    PilotWeeklyActivity,
    RankingEntry,
    RankingSnapshot,
    RewardBoundaryEvent,
    ScoreDelta,
    WeeklyActivity,
    history_pilot_key,
)
from pg_rankings.ranking_card import (
    ACTIVITY_CARD_SIZE,
    CARD_SIZE,
    _format_delta,
    _reset_countdown,
    render_ranking_card,
    render_weekly_activity_card,
)


def snapshot() -> RankingSnapshot:
    return RankingSnapshot(
        server="Example Server",
        division=1,
        captured_at=datetime(2026, 9, 1, 0, 1, tzinfo=UTC),
        anchor_confidence=91.0,
        entries=(
            RankingEntry(1, "Rjjan", 251_107, 90.0),
            RankingEntry(2, "Orbit-Master", 196_230, 88.0),
            RankingEntry(3, "Pilot Gamma", 194_189, 92.0),
            RankingEntry(4, "Pilot Delta", 119_733, 89.0),
            RankingEntry(5, "Pilot Epsilon", 100_105, 87.0),
            RankingEntry(6, "Ravenl999", 96_492, 86.0),
            RankingEntry(7, "Stellar PilotO1", 49_588, 85.0),
            RankingEntry(8, "Pilot Eight", 40_000, 84.0),
            RankingEntry(9, "Pilot Nine", 30_000, 83.0),
            RankingEntry(10, "Pilot Ten", 20_000, 82.0),
            RankingEntry(11, "Pilot Eleven", 10_000, 81.0),
        ),
    )


def test_renders_discord_card_as_rgb_png() -> None:
    card = render_ranking_card(
        snapshot(),
        reward_slots=4,
        display_top_n=7,
        evaluation_at=datetime(2026, 9, 7, tzinfo=ZoneInfo("Europe/Berlin")),
        score_deltas={
            history_pilot_key("Rjjan"): ScoreDelta(12_430, 86_201, 4_210),
            history_pilot_key("Orbit-Master"): ScoreDelta(0, None, 0),
        },
    )

    assert card.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(BytesIO(card)) as image:
        assert image.size == CARD_SIZE
        assert image.mode == "RGB"


def test_stale_card_has_distinct_pixels() -> None:
    evaluation_at = datetime(2026, 9, 7, tzinfo=ZoneInfo("Europe/Berlin"))
    live = render_ranking_card(
        snapshot(), reward_slots=4, display_top_n=7, evaluation_at=evaluation_at
    )
    stale = render_ranking_card(
        snapshot(),
        reward_slots=4,
        display_top_n=7,
        evaluation_at=evaluation_at,
        stale_reason="OcrError",
    )

    assert stale != live


def test_formats_history_deltas_without_new_label() -> None:
    assert _format_delta(12_430) == "+12.430"
    assert _format_delta(0) == "+0"
    assert _format_delta(None) == "—"


def weekly_activity() -> WeeklyActivity:
    current = snapshot()
    buckets = tuple(
        ActivityBucket(
            observed_intervals=8,
            active_intervals=index % 5,
            has_data_gap=index in {4, 5},
            uncertain_increase=index == 5,
        )
        for index in range(168)
    )
    return WeeklyActivity(
        period_start=datetime(2026, 8, 31, tzinfo=ZoneInfo("Europe/Berlin")),
        period_end=datetime(2026, 9, 7, tzinfo=ZoneInfo("Europe/Berlin")),
        captured_at=current.captured_at,
        pilots=tuple(
            PilotWeeklyActivity(
                pilot_key=history_pilot_key(entry.name),
                buckets=buckets,
                last_increase_started_at=current.captured_at,
                last_increase_detected_at=current.captured_at,
                today_increase=12_345,
                reward_boundary_events=(
                    RewardBoundaryEvent(
                        detected_at=current.captured_at,
                        entered=True,
                    ),
                ),
            )
            for entry in current.entries
        ),
        capture_coverage_percent=94,
    )


def test_renders_weekly_activity_as_rgb_png() -> None:
    card = render_weekly_activity_card(
        snapshot(),
        weekly_activity(),
        display_top_n=7,
        reward_slots=4,
        rendered_at=datetime(2026, 9, 1, 0, 2, tzinfo=UTC),
    )

    assert card.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(BytesIO(card)) as image:
        assert image.size == ACTIVITY_CARD_SIZE
        assert image.mode == "RGB"


def test_stale_weekly_activity_card_has_distinct_pixels() -> None:
    rendered_at = datetime(2026, 9, 1, 1, tzinfo=UTC)
    live = render_weekly_activity_card(
        snapshot(),
        weekly_activity(),
        display_top_n=7,
        reward_slots=4,
        rendered_at=rendered_at,
    )
    stale = render_weekly_activity_card(
        snapshot(),
        weekly_activity(),
        display_top_n=7,
        reward_slots=4,
        rendered_at=rendered_at,
        stale_reason="OcrError",
    )

    assert stale != live


def test_formats_reset_countdown_compactly() -> None:
    berlin = ZoneInfo("Europe/Berlin")
    reference = datetime(2026, 9, 5, 14, 32, tzinfo=berlin)

    assert _reset_countdown(datetime(2026, 9, 7, tzinfo=berlin), reference) == "1T 09H"
    assert _reset_countdown(reference, reference) == "FÄLLIG"
