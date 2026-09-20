import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path


def watchdog_module():
    path = Path(__file__).parents[1] / "windows" / "watchdog.py"
    spec = importlib.util.spec_from_file_location("pg_windows_watchdog", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_watchdog_keeps_a_fresh_worker() -> None:
    module = watchdog_module()
    now = datetime(2026, 9, 1, 20, tzinfo=UTC)
    state = {"updated_at": (now - timedelta(minutes=5)).isoformat()}

    assert module.heartbeat_age_seconds(state, now=now) == 300
    assert not module.should_restart_tracker(state, now=now, stale_seconds=720)


def test_watchdog_restarts_for_stale_missing_or_invalid_heartbeat() -> None:
    module = watchdog_module()
    now = datetime(2026, 9, 1, 20, tzinfo=UTC)
    stale = {"updated_at": (now - timedelta(minutes=13)).isoformat()}

    assert module.should_restart_tracker(stale, now=now, stale_seconds=720)
    assert module.should_restart_tracker({}, now=now, stale_seconds=720)
    assert module.should_restart_tracker({"updated_at": "invalid"}, now=now, stale_seconds=720)


def test_watchdog_reports_the_age_of_the_last_valid_ranking_snapshot() -> None:
    module = watchdog_module()
    now = datetime(2026, 9, 1, 20, tzinfo=UTC)
    state = {
        "last_snapshot": {
            "captured_at": (now - timedelta(minutes=17)).isoformat(),
        }
    }

    assert module.snapshot_age_seconds(state, now=now) == 17 * 60
    assert module.snapshot_age_seconds({}, now=now) is None
    assert module.snapshot_age_seconds({"last_snapshot": []}, now=now) is None
    assert (
        module.snapshot_age_seconds(
            {"last_snapshot": {"captured_at": "invalid"}},
            now=now,
        )
        is None
    )
