from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.stem}-",
        suffix=".json",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        with suppress(OSError):
            os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name)


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class RuntimeHeartbeat:
    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        process_id: int | None = None,
    ) -> None:
        self.path = path
        self.clock = clock
        self.process_id = os.getpid() if process_id is None else process_id

    def write(
        self,
        mode: str,
        *,
        consecutive_failures: int = 0,
        error: Exception | None = None,
        detail: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "version": 1,
            "updated_at": self.clock().astimezone(UTC).isoformat(),
            "process_id": self.process_id,
            "mode": mode,
            "consecutive_failures": consecutive_failures,
        }
        if error is not None:
            payload["error_type"] = type(error).__name__
        if detail:
            payload["detail"] = detail[:240]
        _atomic_json_write(self.path, payload)
