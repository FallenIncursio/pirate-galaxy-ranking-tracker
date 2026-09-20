from __future__ import annotations

import os
import traceback
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RECOVERY_LOG = PROJECT_ROOT / "logs" / "manual-recovery.log"


def log(message: str) -> None:
    RECOVERY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RECOVERY_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now().astimezone().isoformat()} {message}\n")


def load_environment() -> None:
    secrets_path = PROJECT_ROOT / "secrets.env"
    if not secrets_path.is_file():
        raise RuntimeError("secrets.env is missing")
    for raw_line in secrets_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()


if __name__ == "__main__":
    try:
        load_environment()
        from pg_rankings.cli import main

        log("manual recovery started")
        exit_code = main(["--config", str(PROJECT_ROOT / "config.toml"), "recover"])
        log(f"manual recovery exited with {exit_code}")
        raise SystemExit(exit_code)
    except Exception:
        log("manual recovery failed\n" + traceback.format_exc())
        raise
