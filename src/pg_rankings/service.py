from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from .capture import WindowBounds, capture_window, set_window_process_throttled
from .config import TrackerConfig
from .discord_webhook import DiscordPublisher, snapshot_from_dict
from .models import RankingEntry, RankingSnapshot
from .ocr import (
    KNOWN_NAME_MINIMUM_CONFIDENCE,
    OcrResult,
    extract_rankings,
    require_consensus,
)
from .recovery import RecoveryController
from .runtime_state import RuntimeHeartbeat, _atomic_json_write, read_json_object
from .single_instance import automation_lease
from .ui_refresh import refresh_rankings

LOGGER = logging.getLogger(__name__)

CaptureFunction = Callable[[str], tuple[Image.Image, WindowBounds]]
ThrottleFunction = Callable[[int, bool], None]
SleepFunction = Callable[[float], None]
RECOVERY_RUNTIME_MODES = {
    "collection-failed",
    "recovering",
    "recovery-complete",
    "recovery-deferred",
    "recovery-shadow",
}


class TrackerService:
    def __init__(
        self,
        config: TrackerConfig,
        *,
        publisher: DiscordPublisher | None,
        capture_function: CaptureFunction = capture_window,
        throttle_function: ThrottleFunction = set_window_process_throttled,
        sleep_function: SleepFunction = time.sleep,
        heartbeat: RuntimeHeartbeat | None = None,
        recovery: RecoveryController | None = None,
    ) -> None:
        self.config = config
        self.publisher = publisher
        self.capture_function = capture_function
        self.throttle_function = throttle_function
        self.sleep_function = sleep_function
        self.heartbeat = heartbeat
        self.recovery = recovery
        self.consecutive_failures = self._restored_failure_count()

    def _restored_failure_count(self) -> int:
        if self.heartbeat is None:
            return 0
        state = read_json_object(self.heartbeat.path)
        value = state.get("consecutive_failures", 0)
        if (
            state.get("mode") not in RECOVERY_RUNTIME_MODES
            or isinstance(value, bool)
            or not isinstance(value, int | float)
        ):
            return 0
        return max(0, int(value))

    def _heartbeat(
        self,
        mode: str,
        *,
        error: Exception | None = None,
        detail: str | None = None,
    ) -> None:
        if self.heartbeat is not None:
            self.heartbeat.write(
                mode,
                consecutive_failures=self.consecutive_failures,
                error=error,
                detail=detail,
            )

    def _read(self, image: Image.Image, diagnostics_directory: Path) -> OcrResult:
        return extract_rankings(
            image,
            config=self.config.ocr,
            top_n=self.config.capture_top_n,
            diagnostics_directory=diagnostics_directory,
            minimum_confidence=min(
                self.config.ocr.minimum_confidence,
                KNOWN_NAME_MINIMUM_CONFIDENCE,
            ),
        )

    def _known_entries(self) -> tuple[RankingEntry, ...]:
        if self.publisher is None or self.publisher.state.last_snapshot is None:
            return ()
        try:
            previous = snapshot_from_dict(self.publisher.state.last_snapshot)
        except (KeyError, TypeError, ValueError):
            LOGGER.warning("last published snapshot cannot be used for OCR name validation")
            return ()
        if previous.server != self.config.server or previous.division != self.config.division:
            return ()
        return previous.entries

    def _save_refresh_failure_diagnostic(self, error: Exception) -> None:
        diagnostics = self.config.paths.diagnostics
        diagnostics.mkdir(parents=True, exist_ok=True)
        report: dict[str, object] = {
            "captured_at": datetime.now(UTC).isoformat(),
            "error_type": type(error).__name__,
            "error": str(error)[:500],
        }
        try:
            image, bounds = self.capture_function(self.config.window_title_contains)
            image.save(diagnostics / "refresh-failed-latest.png")
            report["window"] = {
                "title": bounds.title,
                "width": bounds.width,
                "height": bounds.height,
            }
        except Exception as capture_error:
            report["capture_error"] = (f"{type(capture_error).__name__}: {capture_error}")[:500]
        try:
            _atomic_json_write(diagnostics / "refresh-failed-latest.json", report)
        except Exception:
            LOGGER.exception("could not save refresh failure diagnostics")

    def collect(self) -> RankingSnapshot:
        diagnostics = self.config.paths.diagnostics
        diagnostics.mkdir(parents=True, exist_ok=True)

        try:
            refresh_rankings(
                self.config.window_title_contains,
                self.config.refresh,
                sleep_function=self.sleep_function,
            )
        except Exception as error:
            self._save_refresh_failure_diagnostic(error)
            raise

        first_image, first_bounds = self.capture_function(self.config.window_title_contains)
        first_image.save(diagnostics / "latest-first.png")

        self.sleep_function(self.config.consensus_delay_seconds)
        second_image, second_bounds = self.capture_function(self.config.window_title_contains)
        if (first_bounds.width, first_bounds.height) != (
            second_bounds.width,
            second_bounds.height,
        ):
            raise RuntimeError("game window size changed between OCR reads")
        second_image.save(diagnostics / "latest.png")

        # Both source images represent the same freshly rendered table. Keep the
        # game window intact, but lower only its process priority while the two
        # images are read so Tesseract wins CPU time on the small VM.
        self.throttle_function(second_bounds.handle, True)
        try:
            first = self._read(first_image, diagnostics / "first")
            second = self._read(second_image, diagnostics / "second")
        finally:
            self.throttle_function(second_bounds.handle, False)
        agreed = require_consensus(
            first,
            second,
            minimum_confidence=self.config.ocr.minimum_confidence,
            known_entries=self._known_entries(),
        )

        snapshot = RankingSnapshot.now(
            server=self.config.server,
            division=self.config.division,
            entries=agreed.entries,
            anchor_confidence=agreed.anchor_confidence,
        )
        report = {
            "captured_at": snapshot.captured_at.isoformat(),
            "window": {
                "title": second_bounds.title,
                "width": second_bounds.width,
                "height": second_bounds.height,
            },
            "anchor": {
                "text": agreed.anchor_text,
                "confidence": agreed.anchor_confidence,
            },
            "entries": [
                {
                    "rank": entry.rank,
                    "name": entry.name,
                    "score": entry.score,
                    "confidence": entry.confidence,
                }
                for entry in snapshot.entries
            ],
        }
        (diagnostics / "latest.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return snapshot

    def run_once(self, *, publish: bool = True) -> RankingSnapshot:
        snapshot = self.collect()
        self.consecutive_failures = 0
        if publish:
            if self.publisher is None:
                raise RuntimeError("publishing requested without a Discord publisher")
            message_ids = self.publisher.publish(snapshot)
            LOGGER.info(
                "updated %d persistent Discord message(s): %s",
                len(message_ids),
                ", ".join(sorted(message_ids)),
            )
        else:
            LOGGER.info("dry run succeeded with %d rows", len(snapshot.entries))
        return snapshot

    def _publish_stale_once(self, error: Exception) -> None:
        if self.publisher is None:
            return
        raw_snapshot = self.publisher.state.last_snapshot
        if not raw_snapshot:
            return
        reason = type(error).__name__
        snapshot = snapshot_from_dict(raw_snapshot)
        message_ids = self.publisher.publish_stale(snapshot, reason=reason)
        if message_ids:
            LOGGER.warning(
                "marked %d Discord message(s) as stale: %s",
                len(message_ids),
                ", ".join(sorted(message_ids)),
            )

    def run_forever(self, *, publish: bool = True) -> None:
        if publish and self.publisher is None:
            raise RuntimeError("publishing requested without a Discord publisher")
        LOGGER.info(
            "tracker started for %s Division %d at %d-second intervals (%s)",
            self.config.server,
            self.config.division,
            self.config.poll_seconds,
            "live" if publish else "dry run",
        )
        self._heartbeat("starting")
        next_run = time.monotonic()
        while True:
            self._heartbeat("collecting")
            try:
                with automation_lease():
                    snapshot = self.collect()
            except Exception as error:
                self.consecutive_failures += 1
                LOGGER.exception(
                    "collection failed (%d consecutive failure(s))",
                    self.consecutive_failures,
                )
                recovery_outcome = None
                if self.recovery is not None:
                    try:
                        with automation_lease():
                            recovery_outcome = self.recovery.handle_failure(
                                error,
                                consecutive_failures=self.consecutive_failures,
                            )
                    except Exception:
                        LOGGER.exception("automatic game recovery failed")
                else:
                    self._heartbeat("collection-failed", error=error)
                if recovery_outcome is not None and recovery_outcome.recovered:
                    LOGGER.info("automatic game recovery succeeded: %s", recovery_outcome.detail)
                    next_run = time.monotonic()
                    continue
                if publish and self.consecutive_failures >= self.config.stale_after_failures:
                    try:
                        self._publish_stale_once(error)
                    except Exception:
                        LOGGER.exception("could not publish stale tracker state")
            else:
                self.consecutive_failures = 0
                if self.recovery is not None:
                    self.recovery.record_success()
                else:
                    self._heartbeat("tracking")
                if not publish:
                    LOGGER.info("dry run succeeded with %d rows", len(snapshot.entries))
                elif self.publisher is not None:
                    try:
                        message_ids = self.publisher.publish(snapshot)
                        LOGGER.info(
                            "updated %d persistent Discord message(s): %s",
                            len(message_ids),
                            ", ".join(sorted(message_ids)),
                        )
                    except Exception:
                        LOGGER.exception("validated ranking could not be published to Discord")
            next_run += self.config.poll_seconds
            delay = max(1.0, next_run - time.monotonic())
            self.sleep_function(delay)


def snapshot_for_console(snapshot: RankingSnapshot) -> str:
    payload = {
        "server": snapshot.server,
        "division": snapshot.division,
        "captured_at": snapshot.captured_at.astimezone(UTC).isoformat(),
        "entries": [
            {
                "rank": entry.rank,
                "name": entry.name,
                "score": entry.score,
                "confidence": round(entry.confidence, 1),
            }
            for entry in snapshot.entries
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def calibrate_capture(config: TrackerConfig) -> Path:
    image, bounds = capture_window(config.window_title_contains)
    directory = config.paths.diagnostics / f"calibration-{datetime.now(UTC):%Y%m%d-%H%M%S}"
    directory.mkdir(parents=True, exist_ok=True)
    image.save(directory / "window.png")
    try:
        result = extract_rankings(
            image,
            config=config.ocr,
            top_n=config.capture_top_n,
            diagnostics_directory=directory / "crops",
        )
        report = {
            "ok": True,
            "window": {
                "title": bounds.title,
                "width": bounds.width,
                "height": bounds.height,
            },
            "anchor": result.anchor_text,
            "entries": [
                {
                    "rank": entry.rank,
                    "name": entry.name,
                    "score": entry.score,
                    "confidence": entry.confidence,
                }
                for entry in result.entries
            ],
        }
    except Exception as error:
        report = {
            "ok": False,
            "window": {
                "title": bounds.title,
                "width": bounds.width,
                "height": bounds.height,
            },
            "error": f"{type(error).__name__}: {error}",
        }
    (directory / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return directory
