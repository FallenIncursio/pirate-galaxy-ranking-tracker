import json
from datetime import UTC, datetime

from pg_rankings.runtime_state import RuntimeHeartbeat, read_json_object


def test_heartbeat_writes_non_secret_runtime_state(tmp_path) -> None:
    path = tmp_path / "runtime.json"
    heartbeat = RuntimeHeartbeat(
        path,
        clock=lambda: datetime(2026, 9, 1, 20, 0, tzinfo=UTC),
        process_id=42,
    )

    heartbeat.write(
        "recovering",
        consecutive_failures=3,
        error=RuntimeError("do not persist this detail"),
        detail="waiting for game window",
    )

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw == {
        "version": 1,
        "updated_at": "2026-09-01T20:00:00+00:00",
        "process_id": 42,
        "mode": "recovering",
        "consecutive_failures": 3,
        "error_type": "RuntimeError",
        "detail": "waiting for game window",
    }
    assert "do not persist" not in path.read_text(encoding="utf-8")


def test_read_json_object_returns_empty_for_missing_or_invalid_state(tmp_path) -> None:
    path = tmp_path / "runtime.json"
    assert read_json_object(path) == {}

    path.write_text("[]", encoding="utf-8")
    assert read_json_object(path) == {}

    path.write_text("not json", encoding="utf-8")
    assert read_json_object(path) == {}
