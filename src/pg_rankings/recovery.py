from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .capture import CaptureError
from .config import RecoveryConfig
from .ocr import OcrError
from .runtime_state import RuntimeHeartbeat, _atomic_json_write, read_json_object
from .ui_refresh import RefreshError

LOGGER = logging.getLogger(__name__)
ONE_HOUR_SECONDS = 60 * 60


class RecoveryStatus(StrEnum):
    RECOVERED = "recovered"
    SHADOW = "shadow"
    DEFERRED = "deferred"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    game_window_present: bool
    game_responding: bool
    launcher_running: bool
    detail: str


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    status: RecoveryStatus
    detail: str
    restarted: bool = False
    retry_after_seconds: int = 0
    maintenance: bool = False
    maintenance_probe: bool = False

    @property
    def recovered(self) -> bool:
        return self.status is RecoveryStatus.RECOVERED


class RecoveryDriver(Protocol):
    def observe(self) -> RuntimeObservation: ...

    def recover(
        self,
        progress: Callable[[str], None],
        *,
        allow_restart: bool,
        restart_maintenance: bool,
    ) -> RecoveryOutcome: ...


class RecoveryController:
    def __init__(
        self,
        config: RecoveryConfig,
        *,
        driver: RecoveryDriver,
        heartbeat: RuntimeHeartbeat,
        state_path: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config
        self.driver = driver
        self.heartbeat = heartbeat
        self.state_path = state_path
        self.clock = clock
        raw = read_json_object(state_path)
        self.restart_timestamps = [
            float(value)
            for value in raw.get("restart_timestamps", [])
            if isinstance(value, int | float)
        ]
        next_attempt_at = raw.get("next_attempt_at", 0)
        self.next_attempt_at = (
            float(next_attempt_at) if isinstance(next_attempt_at, int | float) else 0.0
        )
        maintenance_started_at = raw.get("maintenance_started_at")
        self.maintenance_started_at = (
            float(maintenance_started_at)
            if isinstance(maintenance_started_at, int | float)
            else None
        )
        maintenance_probe_count = raw.get("maintenance_probe_count", 0)
        self.maintenance_probe_count = (
            max(0, int(maintenance_probe_count))
            if isinstance(maintenance_probe_count, int | float)
            else 0
        )

    def _now(self) -> float:
        return self.clock().astimezone(UTC).timestamp()

    def _prune_restarts(self, now: float) -> None:
        self.restart_timestamps = [
            timestamp for timestamp in self.restart_timestamps if timestamp > now - ONE_HOUR_SECONDS
        ]

    def _save(self) -> None:
        _atomic_json_write(
            self.state_path,
            {
                "version": 2,
                "restart_timestamps": self.restart_timestamps,
                "next_attempt_at": self.next_attempt_at,
                "maintenance_started_at": self.maintenance_started_at,
                "maintenance_probe_count": self.maintenance_probe_count,
            },
        )

    def _clear_maintenance(self) -> None:
        self.maintenance_started_at = None
        self.maintenance_probe_count = 0

    def _maintenance_backoff(self) -> int:
        backoffs = self.config.maintenance_backoff_seconds
        return backoffs[min(self.maintenance_probe_count, len(backoffs) - 1)]

    def should_recover(self, error: Exception, consecutive_failures: int) -> bool:
        if isinstance(error, OcrError):
            return False
        if isinstance(error, CaptureError):
            return "no window contains title" in str(error) or (
                consecutive_failures >= self.config.refresh_failure_threshold
            )
        if isinstance(error, RefreshError):
            return consecutive_failures >= self.config.refresh_failure_threshold
        return False

    def record_success(self) -> None:
        if self.next_attempt_at or self.maintenance_started_at is not None:
            self.next_attempt_at = 0.0
            self._clear_maintenance()
            self._save()
        self.heartbeat.write("tracking")

    def handle_failure(
        self,
        error: Exception,
        *,
        consecutive_failures: int,
    ) -> RecoveryOutcome | None:
        self.heartbeat.write(
            "collection-failed",
            consecutive_failures=consecutive_failures,
            error=error,
        )
        if not self.config.enabled or not self.should_recover(error, consecutive_failures):
            return None

        now = self._now()
        if self.next_attempt_at > now:
            remaining = round(self.next_attempt_at - now)
            return RecoveryOutcome(
                RecoveryStatus.DEFERRED,
                f"recovery backoff active for another {remaining} seconds",
                retry_after_seconds=remaining,
            )

        if self.config.shadow_mode:
            observation = self.driver.observe()
            detail = (
                "shadow observation: "
                f"window={observation.game_window_present}, "
                f"responding={observation.game_responding}, "
                f"launcher={observation.launcher_running}, "
                f"detail={observation.detail}"
            )
            LOGGER.warning("recovery would start in live mode; %s", detail)
            self.heartbeat.write(
                "recovery-shadow",
                consecutive_failures=consecutive_failures,
                error=error,
                detail=observation.detail,
            )
            return RecoveryOutcome(RecoveryStatus.SHADOW, detail)

        self._prune_restarts(now)
        allow_restart = len(self.restart_timestamps) < self.config.max_restarts_per_hour
        restart_maintenance = self.maintenance_started_at is not None

        def progress(detail: str) -> None:
            LOGGER.info("recovery progress: %s", detail)
            self.heartbeat.write(
                "recovering",
                consecutive_failures=consecutive_failures,
                error=error,
                detail=detail,
            )

        progress("recovery started")
        outcome = self.driver.recover(
            progress,
            allow_restart=allow_restart,
            restart_maintenance=restart_maintenance,
        )
        if outcome.restarted:
            self.restart_timestamps.append(now)
        if outcome.maintenance:
            if self.maintenance_started_at is None:
                self.maintenance_started_at = now
            if outcome.maintenance_probe:
                self.maintenance_probe_count += 1
            maintenance_backoff = self._maintenance_backoff()
            self.next_attempt_at = now + maintenance_backoff
            LOGGER.warning(
                "maintenance recovery deferred for %d seconds after %d restart probe(s)",
                maintenance_backoff,
                self.maintenance_probe_count,
            )
        else:
            self._clear_maintenance()
        if not outcome.maintenance and outcome.retry_after_seconds > 0:
            self.next_attempt_at = now + outcome.retry_after_seconds
        elif outcome.recovered:
            self.next_attempt_at = 0.0
        self._save()
        self.heartbeat.write(
            "recovery-complete" if outcome.recovered else "recovery-deferred",
            consecutive_failures=consecutive_failures,
            error=None if outcome.recovered else error,
            detail=outcome.detail,
        )
        return outcome
