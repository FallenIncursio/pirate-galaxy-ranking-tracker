from datetime import UTC, datetime, timedelta

from pg_rankings.capture import CaptureError
from pg_rankings.config import RecoveryConfig
from pg_rankings.ocr import OcrError
from pg_rankings.recovery import (
    RecoveryController,
    RecoveryOutcome,
    RecoveryStatus,
    RuntimeObservation,
)
from pg_rankings.runtime_state import RuntimeHeartbeat, read_json_object
from pg_rankings.ui_refresh import RefreshError


class FakeDriver:
    def __init__(self, outcome: RecoveryOutcome | None = None) -> None:
        self.outcome = outcome or RecoveryOutcome(
            RecoveryStatus.RECOVERED,
            "ready",
            restarted=True,
        )
        self.observations = 0
        self.recoveries: list[bool] = []
        self.maintenance_restarts: list[bool] = []

    def observe(self) -> RuntimeObservation:
        self.observations += 1
        return RuntimeObservation(False, False, True, "launcher update")

    def recover(
        self,
        progress,
        *,
        allow_restart: bool,
        restart_maintenance: bool,
    ) -> RecoveryOutcome:
        self.recoveries.append(allow_restart)
        self.maintenance_restarts.append(restart_maintenance)
        progress("driver called")
        return self.outcome


def controller(tmp_path, *, config: RecoveryConfig, driver: FakeDriver, now: datetime):
    def clock():
        return now

    return RecoveryController(
        config,
        driver=driver,
        heartbeat=RuntimeHeartbeat(tmp_path / "runtime.json", clock=clock, process_id=42),
        state_path=tmp_path / "recovery.json",
        clock=clock,
    )


def test_shadow_mode_observes_missing_window_without_mutation(tmp_path) -> None:
    driver = FakeDriver()
    recovery = controller(
        tmp_path,
        config=RecoveryConfig(enabled=True, shadow_mode=True),
        driver=driver,
        now=datetime(2026, 9, 1, 20, tzinfo=UTC),
    )

    outcome = recovery.handle_failure(
        CaptureError("no window contains title 'PirateGalaxy'"),
        consecutive_failures=1,
    )

    assert outcome is not None and outcome.status is RecoveryStatus.SHADOW
    assert driver.observations == 1
    assert driver.recoveries == []
    assert read_json_object(tmp_path / "runtime.json")["mode"] == "recovery-shadow"


def test_ocr_error_never_restarts_the_game(tmp_path) -> None:
    driver = FakeDriver()
    recovery = controller(
        tmp_path,
        config=RecoveryConfig(enabled=True, shadow_mode=False),
        driver=driver,
        now=datetime(2026, 9, 1, 20, tzinfo=UTC),
    )

    assert recovery.handle_failure(OcrError("bad OCR"), consecutive_failures=20) is None
    assert driver.recoveries == []


def test_refresh_failure_requires_the_configured_streak(tmp_path) -> None:
    driver = FakeDriver()
    recovery = controller(
        tmp_path,
        config=RecoveryConfig(
            enabled=True,
            shadow_mode=False,
            refresh_failure_threshold=3,
        ),
        driver=driver,
        now=datetime(2026, 9, 1, 20, tzinfo=UTC),
    )

    assert recovery.handle_failure(RefreshError("missing"), consecutive_failures=2) is None
    outcome = recovery.handle_failure(RefreshError("missing"), consecutive_failures=3)

    assert outcome is not None and outcome.recovered
    assert driver.recoveries == [True]


def test_restart_budget_is_persisted_and_passed_to_driver(tmp_path) -> None:
    now = datetime(2026, 9, 1, 20, tzinfo=UTC)
    first_driver = FakeDriver()
    first = controller(
        tmp_path,
        config=RecoveryConfig(
            enabled=True,
            shadow_mode=False,
            max_restarts_per_hour=1,
        ),
        driver=first_driver,
        now=now,
    )
    first.handle_failure(
        CaptureError("no window contains title 'PirateGalaxy'"),
        consecutive_failures=1,
    )

    second_driver = FakeDriver(
        RecoveryOutcome(
            RecoveryStatus.DEFERRED,
            "restart budget exhausted",
            retry_after_seconds=900,
        )
    )
    second = controller(
        tmp_path,
        config=RecoveryConfig(
            enabled=True,
            shadow_mode=False,
            max_restarts_per_hour=1,
        ),
        driver=second_driver,
        now=now + timedelta(minutes=5),
    )
    outcome = second.handle_failure(
        CaptureError("no window contains title 'PirateGalaxy'"),
        consecutive_failures=1,
    )

    assert outcome is not None and outcome.status is RecoveryStatus.DEFERRED
    assert second_driver.recoveries == [False]
    state = read_json_object(tmp_path / "recovery.json")
    assert len(state["restart_timestamps"]) == 1
    assert state["next_attempt_at"] > now.timestamp()


def test_success_clears_recovery_backoff(tmp_path) -> None:
    now = datetime(2026, 9, 1, 20, tzinfo=UTC)
    driver = FakeDriver(
        RecoveryOutcome(
            RecoveryStatus.DEFERRED,
            "maintenance",
            retry_after_seconds=900,
            maintenance=True,
        )
    )
    recovery = controller(
        tmp_path,
        config=RecoveryConfig(enabled=True, shadow_mode=False),
        driver=driver,
        now=now,
    )
    recovery.handle_failure(
        CaptureError("no window contains title 'PirateGalaxy'"),
        consecutive_failures=1,
    )

    recovery.record_success()

    assert read_json_object(tmp_path / "recovery.json")["next_attempt_at"] == 0.0
    assert read_json_object(tmp_path / "recovery.json")["maintenance_started_at"] is None
    assert read_json_object(tmp_path / "recovery.json")["maintenance_probe_count"] == 0
    assert read_json_object(tmp_path / "runtime.json")["mode"] == "tracking"


def test_maintenance_waits_then_uses_escalating_restart_probes(tmp_path) -> None:
    start = datetime(2026, 9, 1, 20, tzinfo=UTC)
    config = RecoveryConfig(
        enabled=True,
        shadow_mode=False,
        maintenance_backoff_seconds=(900, 1800, 3600),
    )
    first_driver = FakeDriver(
        RecoveryOutcome(
            RecoveryStatus.DEFERRED,
            "maintenance",
            maintenance=True,
        )
    )
    first = controller(tmp_path, config=config, driver=first_driver, now=start)

    first.handle_failure(
        CaptureError("no window contains title 'PirateGalaxy'"),
        consecutive_failures=1,
    )

    state = read_json_object(tmp_path / "recovery.json")
    assert first_driver.maintenance_restarts == [False]
    assert state["maintenance_started_at"] == start.timestamp()
    assert state["maintenance_probe_count"] == 0
    assert state["next_attempt_at"] == start.timestamp() + 900

    backoff_driver = FakeDriver()
    during_backoff = controller(
        tmp_path,
        config=config,
        driver=backoff_driver,
        now=start + timedelta(minutes=5),
    )
    deferred = during_backoff.handle_failure(
        CaptureError("no window contains title 'PirateGalaxy'"),
        consecutive_failures=2,
    )
    assert deferred is not None and deferred.status is RecoveryStatus.DEFERRED
    assert backoff_driver.recoveries == []

    second_driver = FakeDriver(
        RecoveryOutcome(
            RecoveryStatus.DEFERRED,
            "maintenance after restart probe",
            restarted=True,
            maintenance=True,
            maintenance_probe=True,
        )
    )
    second = controller(
        tmp_path,
        config=config,
        driver=second_driver,
        now=start + timedelta(minutes=15),
    )
    second.handle_failure(
        CaptureError("no window contains title 'PirateGalaxy'"),
        consecutive_failures=3,
    )

    state = read_json_object(tmp_path / "recovery.json")
    assert second_driver.maintenance_restarts == [True]
    assert state["maintenance_probe_count"] == 1
    assert state["next_attempt_at"] == (start + timedelta(minutes=45)).timestamp()

    third_driver = FakeDriver(
        RecoveryOutcome(
            RecoveryStatus.DEFERRED,
            "maintenance after second restart probe",
            restarted=True,
            maintenance=True,
            maintenance_probe=True,
        )
    )
    third = controller(
        tmp_path,
        config=config,
        driver=third_driver,
        now=start + timedelta(minutes=45),
    )
    third.handle_failure(
        CaptureError("no window contains title 'PirateGalaxy'"),
        consecutive_failures=4,
    )

    state = read_json_object(tmp_path / "recovery.json")
    assert state["maintenance_probe_count"] == 2
    assert state["next_attempt_at"] == (start + timedelta(minutes=105)).timestamp()

    fourth_driver = FakeDriver(
        RecoveryOutcome(
            RecoveryStatus.DEFERRED,
            "maintenance after third restart probe",
            restarted=True,
            maintenance=True,
            maintenance_probe=True,
        )
    )
    fourth = controller(
        tmp_path,
        config=config,
        driver=fourth_driver,
        now=start + timedelta(minutes=105),
    )
    fourth.handle_failure(
        CaptureError("no window contains title 'PirateGalaxy'"),
        consecutive_failures=5,
    )

    state = read_json_object(tmp_path / "recovery.json")
    assert state["maintenance_probe_count"] == 3
    assert state["next_attempt_at"] == (start + timedelta(minutes=165)).timestamp()


def test_general_client_start_that_discovers_maintenance_uses_initial_wait(tmp_path) -> None:
    now = datetime(2026, 9, 1, 20, tzinfo=UTC)
    driver = FakeDriver(
        RecoveryOutcome(
            RecoveryStatus.DEFERRED,
            "client started and found maintenance",
            restarted=True,
            maintenance=True,
            maintenance_probe=False,
        )
    )
    recovery = controller(
        tmp_path,
        config=RecoveryConfig(enabled=True, shadow_mode=False),
        driver=driver,
        now=now,
    )

    recovery.handle_failure(
        CaptureError("no window contains title 'PirateGalaxy'"),
        consecutive_failures=1,
    )

    state = read_json_object(tmp_path / "recovery.json")
    assert state["maintenance_probe_count"] == 0
    assert state["next_attempt_at"] == now.timestamp() + 900
    assert len(state["restart_timestamps"]) == 1
