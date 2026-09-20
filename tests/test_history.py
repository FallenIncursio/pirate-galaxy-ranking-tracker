import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pg_rankings.history import RankingHistory, ranking_period_start, reset_cycle_anchor
from pg_rankings.models import RankingEntry, RankingSnapshot, ScoreDelta, history_pilot_key


def snapshot(captured_at: datetime, rows: tuple[tuple[int, str, int], ...]) -> RankingSnapshot:
    return RankingSnapshot(
        server="Example Server",
        division=1,
        captured_at=captured_at,
        anchor_confidence=90,
        entries=tuple(RankingEntry(rank, name, score, 90) for rank, name, score in rows),
    )


def eleven_rows(score: int) -> tuple[tuple[int, str, int], ...]:
    return tuple((rank, f"Pilot {rank}", score) for rank in range(1, 12))


def test_history_key_normalizes_zero_and_letter_o() -> None:
    assert history_pilot_key("PilotO1") == history_pilot_key("Pilot01")


def test_calculates_hour_and_day_growth_across_rank_changes(tmp_path: Path) -> None:
    history = RankingHistory(tmp_path / "history.sqlite3")
    current_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    history.record_and_deltas(
        snapshot(
            current_at - timedelta(hours=24),
            ((8, "Orbit-Master", 100), (2, "Rjjan", 200)),
        )
    )
    history.record_and_deltas(
        snapshot(
            current_at - timedelta(hours=1),
            ((5, "ORBIT MASTER", 150), (8, "Rjjan", 220)),
        )
    )
    history.record_and_deltas(
        snapshot(
            current_at - timedelta(minutes=15),
            ((3, "Orbit-Master", 170), (4, "Rjjan", 225)),
        )
    )

    deltas = history.record_and_deltas(
        snapshot(current_at, ((1, "Orbit-Master", 180), (2, "Rjjan", 230)))
    )

    assert deltas[history_pilot_key("Orbit-Master")] == ScoreDelta(30, 80, 10)
    assert deltas[history_pilot_key("Rjjan")] == ScoreDelta(10, 30, 5)


def test_does_not_compare_across_monday_reset(tmp_path: Path) -> None:
    history = RankingHistory(tmp_path / "history.sqlite3")
    berlin = ZoneInfo("Europe/Berlin")
    before_reset = datetime(2026, 9, 6, 23, 30, tzinfo=berlin)
    after_reset = datetime(2026, 9, 7, 0, 30, tzinfo=berlin)
    history.record_and_deltas(snapshot(before_reset, ((1, "Pilot", 50),)))

    deltas = history.record_and_deltas(snapshot(after_reset, ((1, "Pilot", 100),)))

    assert deltas[history_pilot_key("Pilot")] == ScoreDelta(None, None)


def test_uses_nearby_five_minute_snapshot_within_tolerance(tmp_path: Path) -> None:
    history = RankingHistory(tmp_path / "history.sqlite3")
    current_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    history.record_and_deltas(
        snapshot(current_at - timedelta(hours=1, minutes=7), ((1, "Pilot", 100),))
    )

    deltas = history.record_and_deltas(snapshot(current_at, ((1, "Pilot", 140),)))

    assert deltas[history_pilot_key("Pilot")].one_hour == 40


def test_fifteen_minute_growth_uses_tighter_three_minute_tolerance(tmp_path: Path) -> None:
    current_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    accepted = RankingHistory(tmp_path / "accepted.sqlite3")
    accepted.record_and_deltas(snapshot(current_at - timedelta(minutes=18), ((1, "Pilot", 100),)))
    rejected = RankingHistory(tmp_path / "rejected.sqlite3")
    rejected.record_and_deltas(snapshot(current_at - timedelta(minutes=19), ((1, "Pilot", 100),)))

    accepted_delta = accepted.record_and_deltas(snapshot(current_at, ((1, "Pilot", 140),)))
    rejected_delta = rejected.record_and_deltas(snapshot(current_at, ((1, "Pilot", 140),)))

    assert accepted_delta[history_pilot_key("Pilot")].fifteen_minutes == 40
    assert rejected_delta[history_pilot_key("Pilot")].fifteen_minutes is None


def test_missing_or_decreasing_baseline_is_not_invented(tmp_path: Path) -> None:
    history = RankingHistory(tmp_path / "history.sqlite3")
    current_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    history.record_and_deltas(
        snapshot(current_at - timedelta(hours=1), ((1, "Other Pilot", 200), (2, "Pilot", 300)))
    )

    deltas = history.record_and_deltas(
        snapshot(current_at, ((1, "New Pilot", 250), (2, "Pilot", 290)))
    )

    assert deltas[history_pilot_key("New Pilot")].one_hour is None
    assert deltas[history_pilot_key("Pilot")].one_hour is None


def test_records_all_eleven_captured_places(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    history = RankingHistory(path)
    rows = tuple((rank, f"Pilot {rank}", 1_000 - rank) for rank in range(1, 12))

    history.record_and_deltas(snapshot(datetime(2026, 9, 8, 12, tzinfo=UTC), rows))

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ranking_history").fetchone()[0] == 11


def test_reads_existing_deltas_without_recording_stale_snapshot(tmp_path: Path) -> None:
    history = RankingHistory(tmp_path / "history.sqlite3")
    current_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    history.record_and_deltas(snapshot(current_at - timedelta(hours=1), ((1, "Pilot", 100),)))
    current = snapshot(current_at, ((1, "Pilot", 150),))
    history.record_and_deltas(current)

    deltas = history.deltas(current)

    assert deltas[history_pilot_key("Pilot")].one_hour == 50


def test_history_identity_tolerates_letter_o_and_zero(tmp_path: Path) -> None:
    history = RankingHistory(tmp_path / "history.sqlite3")
    current_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    history.record_and_deltas(
        snapshot(current_at - timedelta(hours=1), ((7, "Stellar PilotO1", 100),))
    )

    deltas = history.record_and_deltas(snapshot(current_at, ((5, "Stellar Pilot01", 140),)))

    assert deltas[history_pilot_key("Stellar PilotO1")].one_hour == 40


def test_period_start_is_berlin_monday_midnight() -> None:
    result = ranking_period_start(datetime(2026, 10, 27, 12, tzinfo=UTC))

    assert result.isoformat() == "2026-10-26T00:00:00+01:00"


def test_detects_delayed_zero_reset_and_starts_history_at_that_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    history = RankingHistory(path)
    berlin = ZoneInfo("Europe/Berlin")
    history.record_and_deltas(
        snapshot(datetime(2026, 9, 6, 23, 55, tzinfo=berlin), eleven_rows(1_000))
    )
    history.record_and_deltas(
        snapshot(datetime(2026, 9, 7, 0, 5, tzinfo=berlin), eleven_rows(1_000))
    )
    reset_at = datetime(2026, 9, 7, 0, 10, tzinfo=berlin)

    reset_deltas = history.record_and_deltas(snapshot(reset_at, eleven_rows(0)))
    early_deltas = history.record_and_deltas(
        snapshot(datetime(2026, 9, 7, 0, 50, tzinfo=berlin), eleven_rows(50))
    )
    later = snapshot(datetime(2026, 9, 7, 1, 10, tzinfo=berlin), eleven_rows(100))
    later_deltas = history.record_and_deltas(later)

    assert reset_deltas[history_pilot_key("Pilot 1")].one_hour is None
    assert early_deltas[history_pilot_key("Pilot 1")].one_hour is None
    assert later_deltas[history_pilot_key("Pilot 1")] == ScoreDelta(100, None)
    with sqlite3.connect(path) as connection:
        stored_reset = connection.execute("SELECT reset_at FROM ranking_resets").fetchone()[0]
        zero_period = connection.execute(
            "SELECT DISTINCT period_start FROM ranking_history WHERE captured_at = ?",
            (int(reset_at.timestamp()),),
        ).fetchone()[0]
    assert stored_reset == int(reset_at.timestamp())
    assert zero_period == int(reset_at.timestamp())
    assert RankingHistory(path).deltas(later)[history_pilot_key("Pilot 1")].one_hour == 100


def test_sunday_evening_reset_remains_active_after_midnight(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    history = RankingHistory(path)
    berlin = ZoneInfo("Europe/Berlin")
    history.record_and_deltas(
        snapshot(datetime(2026, 9, 6, 23, 45, tzinfo=berlin), eleven_rows(500))
    )
    reset_at = datetime(2026, 9, 6, 23, 50, tzinfo=berlin)
    history.record_and_deltas(snapshot(reset_at, eleven_rows(0)))

    deltas = history.record_and_deltas(
        snapshot(datetime(2026, 9, 7, 0, 50, tzinfo=berlin), eleven_rows(75))
    )

    assert deltas[history_pilot_key("Pilot 1")].one_hour == 75
    with sqlite3.connect(path) as connection:
        cycle_anchor, stored_reset = connection.execute(
            "SELECT cycle_anchor, reset_at FROM ranking_resets"
        ).fetchone()
    assert cycle_anchor == int(datetime(2026, 9, 7, tzinfo=berlin).timestamp())
    assert stored_reset == int(reset_at.timestamp())


def test_all_zero_table_outside_reset_window_does_not_create_reset(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    history = RankingHistory(path)
    berlin = ZoneInfo("Europe/Berlin")
    history.record_and_deltas(snapshot(datetime(2026, 9, 8, 10, tzinfo=berlin), eleven_rows(500)))
    history.record_and_deltas(snapshot(datetime(2026, 9, 8, 10, 5, tzinfo=berlin), eleven_rows(0)))

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ranking_resets").fetchone()[0] == 0


def test_reset_cycle_anchor_accepts_sunday_evening_and_monday_morning() -> None:
    berlin = ZoneInfo("Europe/Berlin")
    expected = datetime(2026, 9, 7, tzinfo=berlin)

    assert reset_cycle_anchor(datetime(2026, 9, 6, 23, tzinfo=berlin)) == expected
    assert reset_cycle_anchor(datetime(2026, 9, 7, 2, tzinfo=berlin)) == expected


def test_detected_period_remains_active_during_sunday_evening_lookahead(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    history = RankingHistory(path)
    berlin = ZoneInfo("Europe/Berlin")
    history.record_and_deltas(
        snapshot(datetime(2026, 9, 13, 23, 58, tzinfo=berlin), eleven_rows(10_000))
    )
    reset_at = datetime(2026, 9, 14, 0, 3, tzinfo=berlin)
    history.record_and_deltas(snapshot(reset_at, eleven_rows(0)))
    before_lookahead = datetime(2026, 9, 20, 17, 5, tzinfo=berlin)
    history.record_and_deltas(snapshot(before_lookahead, eleven_rows(100)))
    during_lookahead = datetime(2026, 9, 20, 18, 5, tzinfo=berlin)

    deltas = history.record_and_deltas(snapshot(during_lookahead, eleven_rows(140)))
    activity = history.weekly_activity(snapshot(during_lookahead, eleven_rows(140)))

    assert deltas[history_pilot_key("Pilot 1")].one_hour == 40
    assert activity.period_start == reset_at.astimezone(UTC)
    assert activity.period_end == datetime(2026, 9, 21, tzinfo=berlin).astimezone(UTC)
    with sqlite3.connect(path) as connection:
        period_starts = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT period_start FROM ranking_history WHERE captured_at IN (?, ?)",
                (int(before_lookahead.timestamp()), int(during_lookahead.timestamp())),
            )
        }
    assert period_starts == {int(reset_at.timestamp())}


def test_sunday_evening_zero_reset_switches_to_next_cycle(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    history = RankingHistory(path)
    berlin = ZoneInfo("Europe/Berlin")
    history.record_and_deltas(
        snapshot(datetime(2026, 9, 13, 23, 58, tzinfo=berlin), eleven_rows(10_000))
    )
    previous_reset = datetime(2026, 9, 14, 0, 3, tzinfo=berlin)
    history.record_and_deltas(snapshot(previous_reset, eleven_rows(0)))
    history.record_and_deltas(
        snapshot(datetime(2026, 9, 20, 18, 5, tzinfo=berlin), eleven_rows(1_000))
    )
    next_reset = datetime(2026, 9, 20, 18, 10, tzinfo=berlin)

    deltas = history.record_and_deltas(snapshot(next_reset, eleven_rows(0)))
    activity = history.weekly_activity(snapshot(next_reset, eleven_rows(0)))

    assert deltas[history_pilot_key("Pilot 1")].one_hour is None
    assert activity.period_start == next_reset.astimezone(UTC)
    assert activity.period_end == datetime(2026, 9, 28, tzinfo=berlin).astimezone(UTC)
    with sqlite3.connect(path) as connection:
        cycle_anchor, stored_reset = connection.execute(
            "SELECT cycle_anchor, reset_at FROM ranking_resets ORDER BY reset_at DESC LIMIT 1"
        ).fetchone()
    assert cycle_anchor == int(datetime(2026, 9, 21, tzinfo=berlin).timestamp())
    assert stored_reset == int(next_reset.timestamp())


def test_weekly_activity_aggregates_valid_intervals_and_last_increase(tmp_path: Path) -> None:
    history = RankingHistory(tmp_path / "history.sqlite3")
    berlin = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 9, 7, 0, 5, tzinfo=berlin)
    current = None
    for index in range(14):
        captured_at = start + timedelta(minutes=5 * index)
        score = 100 + (20 if index >= 2 else 0) + (30 if index >= 13 else 0)
        current = snapshot(captured_at, ((1, "Pilot", score),))
        history.record_and_deltas(current)

    assert current is not None
    activity = history.weekly_activity(current)
    pilot = activity.pilots[0]

    assert len(pilot.buckets) == 168
    assert pilot.buckets[0].active_intervals == 1
    assert pilot.buckets[0].observed_intervals == 10
    assert pilot.buckets[1].active_intervals == 1
    assert pilot.buckets[1].observed_intervals == 3
    assert pilot.last_increase_detected_at == current.captured_at.astimezone(UTC)
    assert pilot.last_increase_uncertain is False
    assert pilot.today_increase == 50
    assert pilot.today_increase_uncertain is False
    assert activity.capture_coverage_percent == 93


def test_weekly_activity_marks_long_positive_gap_as_uncertain(tmp_path: Path) -> None:
    history = RankingHistory(tmp_path / "history.sqlite3")
    berlin = ZoneInfo("Europe/Berlin")
    first = snapshot(datetime(2026, 9, 7, 0, 5, tzinfo=berlin), ((1, "Pilot", 100),))
    current = snapshot(datetime(2026, 9, 7, 1, 5, tzinfo=berlin), ((1, "Pilot", 150),))
    history.record_and_deltas(first)
    history.record_and_deltas(current)

    pilot = history.weekly_activity(current).pilots[0]

    assert pilot.buckets[0].has_data_gap is True
    assert pilot.buckets[0].uncertain_increase is True
    assert pilot.buckets[1].has_data_gap is True
    assert pilot.buckets[1].uncertain_increase is True
    assert pilot.last_increase_started_at == first.captured_at.astimezone(UTC)
    assert pilot.last_increase_detected_at == current.captured_at.astimezone(UTC)
    assert pilot.last_increase_uncertain is True


def test_weekly_activity_marks_late_daily_baseline_as_uncertain(tmp_path: Path) -> None:
    history = RankingHistory(tmp_path / "history.sqlite3")
    berlin = ZoneInfo("Europe/Berlin")
    first = snapshot(datetime(2026, 9, 8, 2, tzinfo=berlin), ((1, "Pilot", 100),))
    current = snapshot(datetime(2026, 9, 8, 2, 5, tzinfo=berlin), ((1, "Pilot", 140),))
    history.record_and_deltas(first)
    history.record_and_deltas(current)

    pilot = history.weekly_activity(current).pilots[0]

    assert pilot.today_increase == 40
    assert pilot.today_increase_uncertain is True


def test_weekly_activity_confirms_reward_boundary_changes(tmp_path: Path) -> None:
    history = RankingHistory(tmp_path / "history.sqlite3")
    berlin = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 9, 7, tzinfo=berlin)
    ranks = (5, 4, 4, 5, 4, 5, 5)
    current = None
    for index, rank in enumerate(ranks):
        current = snapshot(
            start + timedelta(minutes=5 * index),
            ((rank, "Pilot", 100 + index),),
        )
        history.record_and_deltas(current)

    assert current is not None
    events = history.weekly_activity(current, reward_slots=4).pilots[0].reward_boundary_events

    assert [(event.detected_at, event.entered) for event in events] == [
        ((start + timedelta(minutes=5)).astimezone(UTC), True),
        ((start + timedelta(minutes=25)).astimezone(UTC), False),
    ]


def test_weekly_activity_omits_reward_change_across_long_gap(tmp_path: Path) -> None:
    history = RankingHistory(tmp_path / "history.sqlite3")
    berlin = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 9, 7, tzinfo=berlin)
    for offset, rank in ((0, 5), (60, 4), (65, 4)):
        current = snapshot(
            start + timedelta(minutes=offset),
            ((rank, "Pilot", 100 + offset),),
        )
        history.record_and_deltas(current)

    events = history.weekly_activity(current, reward_slots=4).pilots[0].reward_boundary_events

    assert events == ()


def test_weekly_activity_uses_actual_dst_week_duration(tmp_path: Path) -> None:
    history = RankingHistory(tmp_path / "history.sqlite3")
    berlin = ZoneInfo("Europe/Berlin")
    current = snapshot(datetime(2026, 10, 25, 12, tzinfo=berlin), ((1, "Pilot", 100),))
    history.record_and_deltas(current)

    activity = history.weekly_activity(current)

    assert len(activity.pilots[0].buckets) == 169
    assert (activity.period_end - activity.period_start).total_seconds() == 169 * 60 * 60
