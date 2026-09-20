from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class RankingEntry:
    rank: int
    name: str
    score: int
    confidence: float


@dataclass(frozen=True, slots=True)
class ScoreDelta:
    one_hour: int | None
    twenty_four_hours: int | None
    fifteen_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class ActivityBucket:
    observed_intervals: int = 0
    active_intervals: int = 0
    has_data_gap: bool = False
    uncertain_increase: bool = False


@dataclass(frozen=True, slots=True)
class RewardBoundaryEvent:
    detected_at: datetime
    entered: bool


@dataclass(frozen=True, slots=True)
class PilotWeeklyActivity:
    pilot_key: str
    buckets: tuple[ActivityBucket, ...]
    last_increase_started_at: datetime | None = None
    last_increase_detected_at: datetime | None = None
    last_increase_uncertain: bool = False
    today_increase: int | None = None
    today_increase_uncertain: bool = False
    reward_boundary_events: tuple[RewardBoundaryEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class WeeklyActivity:
    period_start: datetime
    period_end: datetime
    captured_at: datetime
    pilots: tuple[PilotWeeklyActivity, ...]
    capture_coverage_percent: int | None = None


def pilot_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def canonical_pilot_name(
    value: str,
    aliases: tuple[tuple[str, str], ...] = (),
) -> str:
    key = pilot_key(value)
    return next((target for source, target in aliases if pilot_key(source) == key), value)


def history_pilot_key(value: str) -> str:
    # Pirate Galaxy's font makes capital O and zero visually indistinguishable
    # to Tesseract (for example HammerO1/Hammer01). Treat that common
    # confusable as one historical identity after the two-read safety check.
    return pilot_key(value).replace("0", "o")


@dataclass(frozen=True, slots=True)
class RankingSnapshot:
    server: str
    division: int
    entries: tuple[RankingEntry, ...]
    captured_at: datetime
    anchor_confidence: float

    @classmethod
    def now(
        cls,
        *,
        server: str,
        division: int,
        entries: tuple[RankingEntry, ...],
        anchor_confidence: float,
    ) -> RankingSnapshot:
        return cls(
            server=server,
            division=division,
            entries=entries,
            captured_at=datetime.now(UTC),
            anchor_confidence=anchor_confidence,
        )

    @property
    def mean_confidence(self) -> float:
        if not self.entries:
            return 0.0
        return sum(entry.confidence for entry in self.entries) / len(self.entries)

    @property
    def fingerprint(self) -> tuple[tuple[str, int], ...]:
        return tuple((pilot_key(entry.name), entry.score) for entry in self.entries)
