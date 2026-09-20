from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import (
    ActivityBucket,
    PilotWeeklyActivity,
    RankingSnapshot,
    RewardBoundaryEvent,
    ScoreDelta,
    WeeklyActivity,
    history_pilot_key,
)

HISTORY_TIMEZONE = ZoneInfo("Europe/Berlin")
FIFTEEN_MINUTES_SECONDS = 15 * 60
ONE_HOUR_SECONDS = 60 * 60
ONE_DAY_SECONDS = 24 * ONE_HOUR_SECONDS
DEFAULT_RETENTION_DAYS = 8
DEFAULT_TOLERANCE_SECONDS = 7 * 60 + 30
FIFTEEN_MINUTES_TOLERANCE_SECONDS = 3 * 60
COMPARISON_WINDOWS = (FIFTEEN_MINUTES_SECONDS, ONE_HOUR_SECONDS, ONE_DAY_SECONDS)
RESET_WINDOW_SECONDS = 6 * ONE_HOUR_SECONDS
RESET_LOOKBACK_SECONDS = 12 * ONE_HOUR_SECONDS
MIN_RESET_ENTRIES = 7
ACTIVITY_BUCKET_SECONDS = ONE_HOUR_SECONDS
ACTIVITY_MAX_PRECISE_GAP_SECONDS = 12 * 60
EXPECTED_CAPTURE_SECONDS = 5 * 60

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _HistoryPeriod:
    start_timestamp: int
    cycle_anchor_timestamp: int


def ranking_period_start(reference: datetime) -> datetime:
    if reference.tzinfo is None:
        raise ValueError("history reference must include a timezone")
    local = reference.astimezone(HISTORY_TIMEZONE)
    monday = local.date() - timedelta(days=local.weekday())
    return datetime.combine(monday, time.min, tzinfo=HISTORY_TIMEZONE)


def reset_cycle_anchor(reference: datetime) -> datetime:
    current_monday = ranking_period_start(reference)
    local = reference.astimezone(HISTORY_TIMEZONE)
    if local.weekday() == 6 and local.hour >= 18:
        return current_monday + timedelta(days=7)
    return current_monday


class RankingHistory:
    def __init__(
        self,
        path: Path,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    ) -> None:
        if retention_days < 2:
            raise ValueError("history retention must be at least two days")
        if tolerance_seconds < 0:
            raise ValueError("history tolerance cannot be negative")
        self.path = path
        self.retention_seconds = retention_days * ONE_DAY_SECONDS
        self.tolerance_seconds = tolerance_seconds

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ranking_history (
                server TEXT NOT NULL,
                division INTEGER NOT NULL,
                captured_at INTEGER NOT NULL,
                period_start INTEGER NOT NULL,
                pilot_key TEXT NOT NULL,
                pilot_name TEXT NOT NULL,
                rank INTEGER NOT NULL,
                score INTEGER NOT NULL,
                PRIMARY KEY (server, division, captured_at, pilot_key)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS ranking_history_lookup
            ON ranking_history (server, division, period_start, captured_at)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ranking_resets (
                server TEXT NOT NULL,
                division INTEGER NOT NULL,
                cycle_anchor INTEGER NOT NULL,
                reset_at INTEGER NOT NULL,
                PRIMARY KEY (server, division, cycle_anchor)
            )
            """
        )
        return connection

    def _detected_reset_at(
        self,
        connection: sqlite3.Connection,
        snapshot: RankingSnapshot,
        *,
        captured_timestamp: int,
        cycle_anchor_timestamp: int,
    ) -> int | None:
        row = connection.execute(
            """
            SELECT reset_at
            FROM ranking_resets
            WHERE server = ?
              AND division = ?
              AND cycle_anchor = ?
              AND reset_at <= ?
            """,
            (
                snapshot.server,
                snapshot.division,
                cycle_anchor_timestamp,
                captured_timestamp,
            ),
        ).fetchone()
        return None if row is None else int(row[0])

    def _period_for_read(
        self,
        connection: sqlite3.Connection,
        snapshot: RankingSnapshot,
        *,
        captured_timestamp: int,
    ) -> _HistoryPeriod:
        calendar_anchor_timestamp = int(ranking_period_start(snapshot.captured_at).timestamp())
        prospective_anchor_timestamp = int(reset_cycle_anchor(snapshot.captured_at).timestamp())
        anchors = (calendar_anchor_timestamp,)
        if prospective_anchor_timestamp != calendar_anchor_timestamp:
            anchors += (prospective_anchor_timestamp,)

        detected_periods = tuple(
            _HistoryPeriod(
                start_timestamp=reset_at,
                cycle_anchor_timestamp=anchor,
            )
            for anchor in anchors
            for reset_at in (
                self._detected_reset_at(
                    connection,
                    snapshot,
                    captured_timestamp=captured_timestamp,
                    cycle_anchor_timestamp=anchor,
                ),
            )
            if reset_at is not None
        )
        if detected_periods:
            return max(detected_periods, key=lambda period: period.start_timestamp)
        return _HistoryPeriod(
            start_timestamp=calendar_anchor_timestamp,
            cycle_anchor_timestamp=calendar_anchor_timestamp,
        )

    def _latest_scores(
        self,
        connection: sqlite3.Connection,
        snapshot: RankingSnapshot,
        *,
        captured_timestamp: int,
    ) -> tuple[int, ...]:
        row = connection.execute(
            """
            SELECT captured_at
            FROM ranking_history
            WHERE server = ?
              AND division = ?
              AND captured_at < ?
              AND captured_at >= ?
            GROUP BY captured_at
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (
                snapshot.server,
                snapshot.division,
                captured_timestamp,
                captured_timestamp - RESET_LOOKBACK_SECONDS,
            ),
        ).fetchone()
        if row is None:
            return ()
        return tuple(
            int(score)
            for (score,) in connection.execute(
                """
                SELECT score
                FROM ranking_history
                WHERE server = ? AND division = ? AND captured_at = ?
                """,
                (snapshot.server, snapshot.division, int(row[0])),
            )
        )

    def _period_start_for_record(
        self,
        connection: sqlite3.Connection,
        snapshot: RankingSnapshot,
        *,
        captured_timestamp: int,
    ) -> int:
        cycle_anchor = int(reset_cycle_anchor(snapshot.captured_at).timestamp())
        detected_reset = self._detected_reset_at(
            connection,
            snapshot,
            captured_timestamp=captured_timestamp,
            cycle_anchor_timestamp=cycle_anchor,
        )
        inside_reset_window = abs(captured_timestamp - cycle_anchor) <= RESET_WINDOW_SECONDS
        current_is_zero_table = len(snapshot.entries) >= MIN_RESET_ENTRIES and all(
            entry.score == 0 for entry in snapshot.entries
        )
        previous_scores = (
            self._latest_scores(
                connection,
                snapshot,
                captured_timestamp=captured_timestamp,
            )
            if detected_reset is None and inside_reset_window and current_is_zero_table
            else ()
        )
        if previous_scores and any(score > 0 for score in previous_scores):
            connection.execute(
                """
                INSERT OR IGNORE INTO ranking_resets (
                    server, division, cycle_anchor, reset_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    snapshot.server,
                    snapshot.division,
                    cycle_anchor,
                    captured_timestamp,
                ),
            )
            detected_reset = self._detected_reset_at(
                connection,
                snapshot,
                captured_timestamp=captured_timestamp,
                cycle_anchor_timestamp=cycle_anchor,
            )
            LOGGER.info(
                "detected ranking reset for %s Division %d at %s",
                snapshot.server,
                snapshot.division,
                snapshot.captured_at.isoformat(),
            )
        return self._period_for_read(
            connection,
            snapshot,
            captured_timestamp=captured_timestamp,
        ).start_timestamp

    def _period_start_for_read(
        self,
        connection: sqlite3.Connection,
        snapshot: RankingSnapshot,
        *,
        captured_timestamp: int,
    ) -> int:
        return self._period_for_read(
            connection,
            snapshot,
            captured_timestamp=captured_timestamp,
        ).start_timestamp

    def _baseline_scores(
        self,
        connection: sqlite3.Connection,
        snapshot: RankingSnapshot,
        *,
        target_timestamp: int,
        period_start_timestamp: int,
        tolerance_seconds: int,
    ) -> dict[str, int]:
        row = connection.execute(
            """
            SELECT captured_at
            FROM ranking_history
            WHERE server = ?
              AND division = ?
              AND period_start = ?
              AND captured_at BETWEEN ? AND ?
            GROUP BY captured_at
            ORDER BY ABS(captured_at - ?) ASC, captured_at DESC
            LIMIT 1
            """,
            (
                snapshot.server,
                snapshot.division,
                period_start_timestamp,
                target_timestamp - tolerance_seconds,
                target_timestamp + tolerance_seconds,
                target_timestamp,
            ),
        ).fetchone()
        if row is None:
            return {}
        return {
            str(key): int(score)
            for key, score in connection.execute(
                """
                SELECT pilot_key, score
                FROM ranking_history
                WHERE server = ? AND division = ? AND captured_at = ?
                """,
                (snapshot.server, snapshot.division, int(row[0])),
            )
        }

    def record_and_deltas(self, snapshot: RankingSnapshot) -> dict[str, ScoreDelta]:
        if snapshot.captured_at.tzinfo is None:
            raise ValueError("snapshot captured_at must include a timezone")
        captured_timestamp = int(snapshot.captured_at.timestamp())

        with self._connect() as connection:
            period_start_timestamp = self._period_start_for_record(
                connection,
                snapshot,
                captured_timestamp=captured_timestamp,
            )
            connection.executemany(
                """
                INSERT INTO ranking_history (
                    server, division, captured_at, period_start,
                    pilot_key, pilot_name, rank, score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (server, division, captured_at, pilot_key)
                DO UPDATE SET
                    period_start = excluded.period_start,
                    pilot_name = excluded.pilot_name,
                    rank = excluded.rank,
                    score = excluded.score
                """,
                (
                    (
                        snapshot.server,
                        snapshot.division,
                        captured_timestamp,
                        period_start_timestamp,
                        history_pilot_key(entry.name),
                        entry.name,
                        entry.rank,
                        entry.score,
                    )
                    for entry in snapshot.entries
                ),
            )
            connection.execute(
                "DELETE FROM ranking_history WHERE captured_at < ?",
                (captured_timestamp - self.retention_seconds,),
            )
            connection.execute(
                "DELETE FROM ranking_resets WHERE reset_at < ?",
                (captured_timestamp - self.retention_seconds,),
            )

            baselines: dict[int, dict[str, int]] = {}
            for seconds in COMPARISON_WINDOWS:
                target = captured_timestamp - seconds
                if target < period_start_timestamp:
                    baselines[seconds] = {}
                else:
                    baselines[seconds] = self._baseline_scores(
                        connection,
                        snapshot,
                        target_timestamp=target,
                        period_start_timestamp=period_start_timestamp,
                        tolerance_seconds=(
                            FIFTEEN_MINUTES_TOLERANCE_SECONDS
                            if seconds == FIFTEEN_MINUTES_SECONDS
                            else self.tolerance_seconds
                        ),
                    )

        deltas: dict[str, ScoreDelta] = {}
        for entry in snapshot.entries:
            key = history_pilot_key(entry.name)
            fifteen_minute_score = baselines[FIFTEEN_MINUTES_SECONDS].get(key)
            hour_score = baselines[ONE_HOUR_SECONDS].get(key)
            day_score = baselines[ONE_DAY_SECONDS].get(key)
            fifteen_minute_delta = (
                None if fifteen_minute_score is None else entry.score - fifteen_minute_score
            )
            hour_delta = None if hour_score is None else entry.score - hour_score
            day_delta = None if day_score is None else entry.score - day_score
            deltas[key] = ScoreDelta(
                one_hour=hour_delta if hour_delta is None or hour_delta >= 0 else None,
                twenty_four_hours=day_delta if day_delta is None or day_delta >= 0 else None,
                fifteen_minutes=(
                    fifteen_minute_delta
                    if fifteen_minute_delta is None or fifteen_minute_delta >= 0
                    else None
                ),
            )
        return deltas

    def deltas(self, snapshot: RankingSnapshot) -> dict[str, ScoreDelta]:
        if not self.path.exists():
            return {
                history_pilot_key(entry.name): ScoreDelta(None, None) for entry in snapshot.entries
            }
        if snapshot.captured_at.tzinfo is None:
            raise ValueError("snapshot captured_at must include a timezone")
        captured_timestamp = int(snapshot.captured_at.timestamp())
        with self._connect() as connection:
            period_start_timestamp = self._period_start_for_read(
                connection,
                snapshot,
                captured_timestamp=captured_timestamp,
            )
            baselines: dict[int, dict[str, int]] = {}
            for seconds in COMPARISON_WINDOWS:
                target = captured_timestamp - seconds
                baselines[seconds] = (
                    {}
                    if target < period_start_timestamp
                    else self._baseline_scores(
                        connection,
                        snapshot,
                        target_timestamp=target,
                        period_start_timestamp=period_start_timestamp,
                        tolerance_seconds=(
                            FIFTEEN_MINUTES_TOLERANCE_SECONDS
                            if seconds == FIFTEEN_MINUTES_SECONDS
                            else self.tolerance_seconds
                        ),
                    )
                )

        return {
            history_pilot_key(entry.name): ScoreDelta(
                fifteen_minutes=(
                    entry.score - baselines[FIFTEEN_MINUTES_SECONDS][history_pilot_key(entry.name)]
                    if history_pilot_key(entry.name) in baselines[FIFTEEN_MINUTES_SECONDS]
                    and entry.score
                    >= baselines[FIFTEEN_MINUTES_SECONDS][history_pilot_key(entry.name)]
                    else None
                ),
                one_hour=(
                    entry.score - baselines[ONE_HOUR_SECONDS][history_pilot_key(entry.name)]
                    if history_pilot_key(entry.name) in baselines[ONE_HOUR_SECONDS]
                    and entry.score >= baselines[ONE_HOUR_SECONDS][history_pilot_key(entry.name)]
                    else None
                ),
                twenty_four_hours=(
                    entry.score - baselines[ONE_DAY_SECONDS][history_pilot_key(entry.name)]
                    if history_pilot_key(entry.name) in baselines[ONE_DAY_SECONDS]
                    and entry.score >= baselines[ONE_DAY_SECONDS][history_pilot_key(entry.name)]
                    else None
                ),
            )
            for entry in snapshot.entries
        }

    def weekly_activity(
        self,
        snapshot: RankingSnapshot,
        *,
        reward_slots: int = 4,
    ) -> WeeklyActivity:
        if snapshot.captured_at.tzinfo is None:
            raise ValueError("snapshot captured_at must include a timezone")
        if reward_slots < 1:
            raise ValueError("reward slots must be positive")

        captured_timestamp = int(snapshot.captured_at.timestamp())
        with self._connect() as connection:
            period = self._period_for_read(
                connection,
                snapshot,
                captured_timestamp=captured_timestamp,
            )
            period_start_timestamp = period.start_timestamp
            period_end = datetime.fromtimestamp(
                period.cycle_anchor_timestamp,
                HISTORY_TIMEZONE,
            ) + timedelta(days=7)
            period_end_timestamp = int(period_end.timestamp())
            bucket_count = math.ceil(
                (period_end_timestamp - period_start_timestamp) / ACTIVITY_BUCKET_SECONDS
            )

            pilot_keys = tuple(history_pilot_key(entry.name) for entry in snapshot.entries)
            rows_by_pilot: dict[str, list[tuple[int, int, int]]] = {
                pilot_key: [] for pilot_key in pilot_keys
            }
            if pilot_keys:
                placeholders = ", ".join("?" for _ in pilot_keys)
                query = f"""
                    SELECT pilot_key, captured_at, rank, score
                    FROM ranking_history
                    WHERE server = ?
                      AND division = ?
                      AND period_start = ?
                      AND captured_at <= ?
                      AND pilot_key IN ({placeholders})
                    ORDER BY pilot_key, captured_at
                """
                for pilot_key, captured_at, rank, score in connection.execute(
                    query,
                    (
                        snapshot.server,
                        snapshot.division,
                        period_start_timestamp,
                        captured_timestamp,
                        *pilot_keys,
                    ),
                ):
                    rows_by_pilot[str(pilot_key)].append((int(captured_at), int(rank), int(score)))

            capture_times = tuple(
                int(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT captured_at
                    FROM ranking_history
                    WHERE server = ?
                      AND division = ?
                      AND period_start = ?
                      AND captured_at <= ?
                    ORDER BY captured_at
                    """,
                    (
                        snapshot.server,
                        snapshot.division,
                        period_start_timestamp,
                        captured_timestamp,
                    ),
                )
            )

        expected_capture_count = max(
            1,
            (captured_timestamp - period_start_timestamp) // EXPECTED_CAPTURE_SECONDS + 1,
        )
        capture_coverage_percent = min(
            100,
            round(len(capture_times) * 100 / expected_capture_count),
        )
        captured_local = snapshot.captured_at.astimezone(HISTORY_TIMEZONE)
        local_day_start = datetime.combine(
            captured_local.date(),
            time.min,
            tzinfo=HISTORY_TIMEZONE,
        )
        today_start_timestamp = max(
            period_start_timestamp,
            int(local_day_start.timestamp()),
        )

        def touched_buckets(started_at: int, ended_at: int) -> range:
            clipped_start = max(started_at, period_start_timestamp)
            clipped_end = min(ended_at, period_end_timestamp)
            if clipped_end <= clipped_start:
                return range(0)
            first = max(
                0,
                (clipped_start - period_start_timestamp) // ACTIVITY_BUCKET_SECONDS,
            )
            last = min(
                bucket_count - 1,
                (clipped_end - 1 - period_start_timestamp) // ACTIVITY_BUCKET_SECONDS,
            )
            return range(first, last + 1)

        pilot_activity: list[PilotWeeklyActivity] = []
        for pilot_key in pilot_keys:
            observed = [0] * bucket_count
            active = [0] * bucket_count
            gaps = [False] * bucket_count
            uncertain = [False] * bucket_count
            last_increase_start: int | None = None
            last_increase_end: int | None = None
            last_increase_uncertain = False

            rows = rows_by_pilot[pilot_key]
            for (previous_at, _previous_rank, previous_score), (
                current_at,
                _current_rank,
                current_score,
            ) in zip(rows, rows[1:], strict=False):
                interval_seconds = current_at - previous_at
                if interval_seconds <= 0:
                    continue
                score_change = current_score - previous_score
                if score_change < 0 or interval_seconds > ACTIVITY_MAX_PRECISE_GAP_SECONDS:
                    for bucket_index in touched_buckets(previous_at, current_at):
                        gaps[bucket_index] = True
                        if score_change > 0:
                            uncertain[bucket_index] = True
                    if score_change > 0:
                        last_increase_start = previous_at
                        last_increase_end = current_at
                        last_increase_uncertain = True
                    continue

                bucket_index = min(
                    bucket_count - 1,
                    max(
                        0,
                        (current_at - period_start_timestamp) // ACTIVITY_BUCKET_SECONDS,
                    ),
                )
                observed[bucket_index] += 1
                if score_change > 0:
                    active[bucket_index] += 1
                    last_increase_start = previous_at
                    last_increase_end = current_at
                    last_increase_uncertain = False

            today_increase: int | None = None
            today_increase_uncertain = False
            if rows:
                nearby_baselines = tuple(
                    row
                    for row in rows
                    if abs(row[0] - today_start_timestamp) <= self.tolerance_seconds
                )
                if nearby_baselines:
                    baseline = min(
                        nearby_baselines,
                        key=lambda row: (
                            abs(row[0] - today_start_timestamp),
                            -row[0],
                        ),
                    )
                else:
                    baseline = next(
                        (row for row in rows if row[0] >= today_start_timestamp),
                        None,
                    )
                    today_increase_uncertain = baseline is not None
                if baseline is not None and rows[-1][2] >= baseline[2]:
                    today_increase = rows[-1][2] - baseline[2]

            reward_boundary_events: list[RewardBoundaryEvent] = []
            if rows:
                stable_rewarded = rows[0][1] <= reward_slots
                candidate_rewarded: bool | None = None
                candidate_started_at: int | None = None
                candidate_observations = 0
                candidate_precise = False
                previous_at = rows[0][0]
                for current_at, current_rank, _current_score in rows[1:]:
                    current_rewarded = current_rank <= reward_slots
                    gap_is_precise = current_at - previous_at <= ACTIVITY_MAX_PRECISE_GAP_SECONDS
                    if current_rewarded == stable_rewarded:
                        candidate_rewarded = None
                        candidate_started_at = None
                        candidate_observations = 0
                        candidate_precise = False
                    elif candidate_rewarded != current_rewarded or not gap_is_precise:
                        candidate_rewarded = current_rewarded
                        candidate_started_at = current_at
                        candidate_observations = 1
                        candidate_precise = gap_is_precise
                    else:
                        candidate_observations += 1
                        if candidate_observations >= 2:
                            if candidate_precise and candidate_started_at is not None:
                                reward_boundary_events.append(
                                    RewardBoundaryEvent(
                                        detected_at=datetime.fromtimestamp(
                                            candidate_started_at,
                                            UTC,
                                        ),
                                        entered=current_rewarded,
                                    )
                                )
                            stable_rewarded = current_rewarded
                            candidate_rewarded = None
                            candidate_started_at = None
                            candidate_observations = 0
                            candidate_precise = False
                    previous_at = current_at

            pilot_activity.append(
                PilotWeeklyActivity(
                    pilot_key=pilot_key,
                    buckets=tuple(
                        ActivityBucket(
                            observed_intervals=observed[index],
                            active_intervals=active[index],
                            has_data_gap=gaps[index],
                            uncertain_increase=uncertain[index],
                        )
                        for index in range(bucket_count)
                    ),
                    last_increase_started_at=(
                        datetime.fromtimestamp(last_increase_start, UTC)
                        if last_increase_start is not None
                        else None
                    ),
                    last_increase_detected_at=(
                        datetime.fromtimestamp(last_increase_end, UTC)
                        if last_increase_end is not None
                        else None
                    ),
                    last_increase_uncertain=last_increase_uncertain,
                    today_increase=today_increase,
                    today_increase_uncertain=today_increase_uncertain,
                    reward_boundary_events=tuple(reward_boundary_events),
                )
            )

        return WeeklyActivity(
            period_start=datetime.fromtimestamp(period_start_timestamp, UTC),
            period_end=datetime.fromtimestamp(period_end_timestamp, UTC),
            captured_at=snapshot.captured_at,
            pilots=tuple(pilot_activity),
            capture_coverage_percent=capture_coverage_percent,
        )
