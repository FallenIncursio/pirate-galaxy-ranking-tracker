from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HEADLESS_LOG = PROJECT_ROOT / "logs" / "headless.log"


def log_headless(message: str) -> None:
    HEADLESS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with HEADLESS_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now().astimezone().isoformat()} {message}\n")


if __name__ == "__main__":
    from pg_rankings.single_instance import AlreadyRunningError, tracker_instance

    python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    worker = Path(__file__).with_name("run-worker.py")
    log_headless("hidden launcher started")
    try:
        with tracker_instance():
            environment = os.environ.copy()
            environment["PG_TRACKER_MUTEX_HELD"] = "1"
            with HEADLESS_LOG.open("a", encoding="utf-8") as handle:
                completed = subprocess.run(
                    [str(python), str(worker)],
                    cwd=PROJECT_ROOT,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    check=False,
                )
    except AlreadyRunningError as error:
        log_headless(f"hidden launcher skipped: {error}")
        raise SystemExit(0) from error
    log_headless(f"hidden worker exited with {completed.returncode}")
    raise SystemExit(completed.returncode)
