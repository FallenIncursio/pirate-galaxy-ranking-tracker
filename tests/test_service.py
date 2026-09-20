import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import pg_rankings.service as service_module
from pg_rankings.capture import WindowBounds, _choose_window_match, _crop_client_image
from pg_rankings.config import (
    DiscordConfig,
    DiscordTargetConfig,
    OcrConfig,
    PathConfig,
    Rect,
    RefreshConfig,
    TrackerConfig,
)
from pg_rankings.discord_webhook import snapshot_to_dict
from pg_rankings.models import RankingEntry, RankingSnapshot
from pg_rankings.ocr import OcrResult
from pg_rankings.recovery import RecoveryOutcome, RecoveryStatus
from pg_rankings.runtime_state import RuntimeHeartbeat
from pg_rankings.service import TrackerService, snapshot_for_console
from pg_rankings.ui_refresh import RefreshError


def config(tmp_path: Path) -> TrackerConfig:
    return TrackerConfig(
        server="Example Server",
        division=1,
        top_n=1,
        capture_top_n=1,
        reward_slots=1,
        poll_seconds=300,
        consensus_delay_seconds=0,
        stale_after_failures=3,
        window_title_contains="Pirate Galaxy",
        ocr=OcrConfig(
            language="eng",
            minimum_confidence=1,
            anchor_minimum_confidence=1,
            anchor=Rect(0, 0, 0.1, 0.1),
            name_x=0.1,
            name_width=0.4,
            score_x=0.6,
            score_width=0.2,
            first_row_y=0.2,
            row_height=0.1,
            row_content_height=0.1,
        ),
        refresh=RefreshConfig(
            expected_width=800,
            expected_height=600,
            menu_icon_x=0.458,
            menu_icon_y=0.1,
            rankings_tab_x=0.3,
            rankings_tab_y=0.2,
            menu_wait_seconds=0,
            content_wait_seconds=0,
        ),
        paths=PathConfig(
            state=tmp_path / "state.json",
            history=tmp_path / "history.sqlite3",
            diagnostics=tmp_path / "diagnostics",
            log=tmp_path / "tracker.log",
        ),
        discord=DiscordConfig(
            username="Tracker",
            targets=(DiscordTargetConfig("primary", "DISCORD_WEBHOOK_URL"),),
        ),
    )


def test_console_snapshot_has_no_secret() -> None:
    snapshot = RankingSnapshot(
        server="Example Server",
        division=1,
        entries=(RankingEntry(1, "Pilot", 123, 90),),
        captured_at=datetime(2026, 8, 30, tzinfo=UTC),
        anchor_confidence=90,
    )
    rendered = snapshot_for_console(snapshot)
    assert '"Pilot"' in rendered
    assert "webhook" not in rendered.casefold()


def test_capture_function_is_injected(tmp_path: Path) -> None:
    calls = 0

    def capture(_title: str) -> tuple[Image.Image, WindowBounds]:
        nonlocal calls
        calls += 1
        return Image.new("RGB", (800, 600)), WindowBounds("Pirate Galaxy", 0, 0, 800, 600)

    service = TrackerService(
        config(tmp_path), publisher=None, capture_function=capture, sleep_function=lambda _: None
    )
    assert service.capture_function("Pirate Galaxy")[1].width == 800
    assert calls == 1


def test_service_restores_an_unresolved_failure_streak_from_runtime(tmp_path: Path) -> None:
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(
        json.dumps(
            {
                "mode": "recovery-deferred",
                "consecutive_failures": 37,
            }
        ),
        encoding="utf-8",
    )

    service = TrackerService(
        config(tmp_path),
        publisher=None,
        heartbeat=RuntimeHeartbeat(runtime_path),
    )

    assert service.consecutive_failures == 37


def test_service_does_not_restore_failures_from_a_healthy_runtime(tmp_path: Path) -> None:
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(
        json.dumps(
            {
                "mode": "tracking",
                "consecutive_failures": 37,
            }
        ),
        encoding="utf-8",
    )

    service = TrackerService(
        config(tmp_path),
        publisher=None,
        heartbeat=RuntimeHeartbeat(runtime_path),
    )

    assert service.consecutive_failures == 0


def test_collect_captures_twice_then_throttles_only_while_reading(
    tmp_path: Path, monkeypatch
) -> None:
    events: list[str] = []
    captures = iter(("first", "second"))

    def capture(_title: str) -> tuple[Image.Image, WindowBounds]:
        label = next(captures)
        events.append(f"capture-{label}")
        return Image.new("RGB", (800, 600)), WindowBounds(
            "Pirate Galaxy", 0, 0, 800, 600, handle=42
        )

    monkeypatch.setattr(
        service_module,
        "refresh_rankings",
        lambda *_args, **_kwargs: events.append("refresh"),
    )
    service = TrackerService(
        config(tmp_path),
        publisher=None,
        capture_function=capture,
        throttle_function=lambda handle, enabled: events.append(f"throttle-{handle}-{enabled}"),
        sleep_function=lambda _seconds: None,
    )
    reads = iter(("first", "second"))

    def read(_image: Image.Image, _directory: Path) -> OcrResult:
        label = next(reads)
        events.append(f"read-{label}")
        return OcrResult((RankingEntry(1, "Pilot", 123, 90),), 90, "Division 1")

    monkeypatch.setattr(service, "_read", read)

    snapshot = service.collect()

    assert snapshot.entries[0].name == "Pilot"
    assert events == [
        "refresh",
        "capture-first",
        "capture-second",
        "throttle-42-True",
        "read-first",
        "read-second",
        "throttle-42-False",
    ]


def test_collect_restores_game_priority_when_ocr_fails(tmp_path: Path, monkeypatch) -> None:
    events: list[str] = []

    monkeypatch.setattr(service_module, "refresh_rankings", lambda *_args, **_kwargs: None)
    service = TrackerService(
        config(tmp_path),
        publisher=None,
        capture_function=lambda _title: (
            Image.new("RGB", (800, 600)),
            WindowBounds("Pirate Galaxy", 0, 0, 800, 600, handle=42),
        ),
        throttle_function=lambda handle, enabled: events.append(f"{handle}-{enabled}"),
        sleep_function=lambda _seconds: None,
    )
    monkeypatch.setattr(service, "_read", lambda *_args: (_ for _ in ()).throw(RuntimeError("OCR")))

    try:
        service.collect()
    except RuntimeError as error:
        assert str(error) == "OCR"
    else:
        raise AssertionError("OCR failure should escape collection")

    assert events == ["42-True", "42-False"]


def test_collect_saves_latest_screen_and_error_when_refresh_fails(
    tmp_path: Path, monkeypatch
) -> None:
    tracker_config = config(tmp_path)
    service = TrackerService(
        tracker_config,
        publisher=None,
        capture_function=lambda _title: (
            Image.new("RGB", (800, 600), "navy"),
            WindowBounds("Pirate Galaxy", 0, 0, 800, 600, handle=42),
        ),
    )
    monkeypatch.setattr(
        service_module,
        "refresh_rankings",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RefreshError("Division 1 missing")),
    )

    with pytest.raises(RefreshError, match="Division 1 missing"):
        service.collect()

    image_path = tracker_config.paths.diagnostics / "refresh-failed-latest.png"
    report_path = tracker_config.paths.diagnostics / "refresh-failed-latest.json"
    assert Image.open(image_path).size == (800, 600)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["error_type"] == "RefreshError"
    assert report["error"] == "Division 1 missing"
    assert report["window"]["width"] == 800


def test_read_uses_the_known_name_soft_confidence_floor(tmp_path: Path, monkeypatch) -> None:
    tracker_config = config(tmp_path)
    tracker_config = replace(
        tracker_config,
        ocr=replace(tracker_config.ocr, minimum_confidence=40.0),
    )
    observed: dict[str, float] = {}

    def extract(*_args, **kwargs) -> OcrResult:
        observed["minimum_confidence"] = kwargs["minimum_confidence"]
        return OcrResult((RankingEntry(1, "Pilot", 123, 90),), 90, "Division 1")

    monkeypatch.setattr(service_module, "extract_rankings", extract)
    service = TrackerService(tracker_config, publisher=None)

    service._read(Image.new("RGB", (800, 600)), tmp_path / "diagnostics")

    assert observed == {"minimum_confidence": 35.0}


def test_collect_validates_borderline_name_against_last_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    tracker_config = config(tmp_path)
    tracker_config = replace(
        tracker_config,
        ocr=replace(tracker_config.ocr, minimum_confidence=40.0),
    )
    previous = RankingSnapshot(
        server="Example Server",
        division=1,
        entries=(RankingEntry(1, "Stellar Pilot01", 49_588, 90),),
        captured_at=datetime(2026, 9, 1, 18, 0, tzinfo=UTC),
        anchor_confidence=90,
    )
    publisher = SimpleNamespace(state=SimpleNamespace(last_snapshot=snapshot_to_dict(previous)))
    monkeypatch.setattr(service_module, "refresh_rankings", lambda *_args, **_kwargs: None)
    service = TrackerService(
        tracker_config,
        publisher=publisher,  # type: ignore[arg-type]
        capture_function=lambda _title: (
            Image.new("RGB", (800, 600)),
            WindowBounds("Pirate Galaxy", 0, 0, 800, 600, handle=42),
        ),
        throttle_function=lambda *_args: None,
        sleep_function=lambda _seconds: None,
    )
    reads = iter(
        (
            OcrResult((RankingEntry(1, "Stellar PilotOl", 52_000, 72),), 90, "Division 1"),
            OcrResult((RankingEntry(1, "Stellar PilotO1", 52_000, 37),), 90, "Division 1"),
        )
    )
    monkeypatch.setattr(service, "_read", lambda *_args: next(reads))

    snapshot = service.collect()

    assert snapshot.entries == (RankingEntry(1, "Stellar Pilot01", 52_000, 72),)


def test_continuous_dry_run_does_not_require_a_publisher(tmp_path: Path, monkeypatch) -> None:
    snapshot = RankingSnapshot(
        server="Example Server",
        division=1,
        entries=(RankingEntry(1, "Pilot", 123, 90),),
        captured_at=datetime(2026, 8, 30, tzinfo=UTC),
        anchor_confidence=90,
    )
    service = TrackerService(
        config(tmp_path),
        publisher=None,
        sleep_function=lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(service, "collect", lambda: snapshot)

    with pytest.raises(KeyboardInterrupt):
        service.run_forever(publish=False)


def test_continuous_live_run_requires_a_publisher(tmp_path: Path) -> None:
    service = TrackerService(config(tmp_path), publisher=None)

    with pytest.raises(RuntimeError, match="without a Discord publisher"):
        service.run_forever()


def test_continuous_tracker_retries_immediately_after_successful_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot = RankingSnapshot(
        server="Example Server",
        division=1,
        entries=(RankingEntry(1, "Pilot", 123, 90),),
        captured_at=datetime(2026, 8, 30, tzinfo=UTC),
        anchor_confidence=90,
    )

    class FakeRecovery:
        def __init__(self) -> None:
            self.failures: list[int] = []
            self.successes = 0

        def handle_failure(self, _error: Exception, *, consecutive_failures: int):
            self.failures.append(consecutive_failures)
            return RecoveryOutcome(RecoveryStatus.RECOVERED, "ready")

        def record_success(self) -> None:
            self.successes += 1

    recovery = FakeRecovery()
    service = TrackerService(
        config(tmp_path),
        publisher=None,
        recovery=recovery,  # type: ignore[arg-type]
        sleep_function=lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    collections = iter(
        (
            RuntimeError("missing game"),
            RuntimeError("rankings still missing"),
            snapshot,
        )
    )

    def collect():
        result = next(collections)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(service, "collect", collect)

    with pytest.raises(KeyboardInterrupt):
        service.run_forever(publish=False)

    assert recovery.failures == [1, 2]
    assert recovery.successes == 1


def test_crop_client_image_uses_window_relative_coordinates() -> None:
    window = Image.new("RGB", (12, 10), "black")
    for x in range(3, 11):
        for y in range(2, 8):
            window.putpixel((x, y), (20, 40, 60))
    bounds = WindowBounds(
        "Pirate Galaxy",
        left=103,
        top=202,
        width=8,
        height=6,
        window_left=100,
        window_top=200,
    )

    client = _crop_client_image(window, bounds)

    assert client.size == (8, 6)
    assert client.getpixel((0, 0)) == (20, 40, 60)


def test_crop_client_image_accepts_client_only_capture() -> None:
    client_capture = Image.new("RGB", (8, 6), (20, 40, 60))
    bounds = WindowBounds(
        "Pirate Galaxy",
        left=103,
        top=202,
        width=8,
        height=6,
        window_left=100,
        window_top=200,
    )

    client = _crop_client_image(client_capture, bounds)

    assert client.size == (8, 6)
    assert client.getpixel((0, 0)) == (20, 40, 60)


def test_window_match_prefers_minimized_game_over_other_hidden_matches() -> None:
    match = _choose_window_match(
        [
            (11, "Pirate Galaxy Launcher", False, False),
            (42, "Pirate Galaxy", False, True),
        ],
        "Pirate Galaxy",
    )

    assert match[0] == 42


def test_window_match_prefers_visible_game_window() -> None:
    match = _choose_window_match(
        [
            (11, "Pirate Galaxy", False, True),
            (42, "Pirate Galaxy", True, False),
        ],
        "Pirate Galaxy",
    )

    assert match[0] == 42


def test_window_match_prefers_responsive_duplicate_game_window() -> None:
    match = _choose_window_match(
        [
            (11, "PirateGalaxy Version: 1002129 (Not Responding)", True, False, True),
            (42, "PirateGalaxy Version: 1002129", True, False, True),
        ],
        "PirateGalaxy Version:",
    )

    assert match[0] == 42
