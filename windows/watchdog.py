from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pg_rankings.config import load_config
from pg_rankings.runtime_state import read_json_object

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRACKER_TASK_NAME = "Pirate Galaxy Rankings"


def _timestamp_age_seconds(raw: Any, *, now: datetime) -> float | None:
    if not isinstance(raw, str):
        return None
    try:
        updated_at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if updated_at.tzinfo is None:
        return None
    return max(0.0, (now.astimezone(UTC) - updated_at.astimezone(UTC)).total_seconds())


def heartbeat_age_seconds(state: dict[str, Any], *, now: datetime) -> float | None:
    return _timestamp_age_seconds(state.get("updated_at"), now=now)


def snapshot_age_seconds(state: dict[str, Any], *, now: datetime) -> float | None:
    snapshot = state.get("last_snapshot")
    if not isinstance(snapshot, dict):
        return None
    return _timestamp_age_seconds(snapshot.get("captured_at"), now=now)


def should_restart_tracker(
    state: dict[str, Any],
    *,
    now: datetime,
    stale_seconds: int,
) -> bool:
    age = heartbeat_age_seconds(state, now=now)
    return age is None or age > stale_seconds


def _run_task_command(*arguments: str) -> None:
    completed = subprocess.run(
        ["schtasks.exe", *arguments],
        capture_output=True,
        text=True,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"schtasks {' '.join(arguments[:1])} failed")


def main() -> int:
    log_path = PROJECT_ROOT / "logs" / "watchdog.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load_config(PROJECT_ROOT / "config.toml")
    if not config.recovery.enabled:
        logging.info("watchdog skipped because recovery is disabled")
        return 0
    runtime_path = config.paths.runtime or config.paths.state.with_name("runtime.json")
    runtime_state = read_json_object(runtime_path)
    now = datetime.now(UTC)
    heartbeat_age = heartbeat_age_seconds(runtime_state, now=now)
    if not should_restart_tracker(
        runtime_state,
        now=now,
        stale_seconds=config.recovery.watchdog_stale_seconds,
    ):
        publisher_state = read_json_object(config.paths.state)
        snapshot_age = snapshot_age_seconds(publisher_state, now=now)
        if snapshot_age is None:
            logging.warning(
                "tracker heartbeat healthy but no valid ranking snapshot is recorded "
                "mode=%s failures=%s",
                runtime_state.get("mode", "unknown"),
                runtime_state.get("consecutive_failures", "unknown"),
            )
        elif snapshot_age > config.recovery.watchdog_stale_seconds:
            logging.warning(
                "tracker heartbeat healthy but ranking snapshot stale age_seconds=%.1f "
                "mode=%s failures=%s",
                snapshot_age,
                runtime_state.get("mode", "unknown"),
                runtime_state.get("consecutive_failures", "unknown"),
            )
        else:
            logging.info(
                "tracker healthy heartbeat_age_seconds=%.1f snapshot_age_seconds=%.1f",
                heartbeat_age,
                snapshot_age,
            )
        return 0

    logging.warning(
        "tracker heartbeat stale age_seconds=%s; restarting task",
        heartbeat_age,
    )
    try:
        _run_task_command("/End", "/TN", TRACKER_TASK_NAME)
    except RuntimeError:
        logging.info("tracker task was not running while heartbeat was stale")
    _run_task_command("/Run", "/TN", TRACKER_TASK_NAME)
    logging.info("tracker task restart requested")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
