from __future__ import annotations

import ctypes
import logging
import re
import subprocess
import sys
import time
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

import cv2
import numpy as np
import pytesseract
from PIL import Image

from .capture import CaptureError, WindowBounds, capture_window, find_client_bounds
from .config import TrackerConfig
from .ocr import configure_tesseract
from .recovery import RecoveryOutcome, RecoveryStatus, RuntimeObservation
from .ui_refresh import (
    RefreshError,
    ScreenPoint,
    Win32InputDriver,
    division_view_visible,
    gameplay_frame_visible,
    gameplay_toolbar_visible,
    rankings_window_visible,
    refresh_rankings,
)
from .window_profile import restore_client_size

LOGGER = logging.getLogger(__name__)
MAINTENANCE_WORDS = (
    "maintenance",
    "wartung",
    "server unavailable",
    "server nicht verfugbar",
    "server nicht verfügbar",
)
RESTART_REQUIRED_WORDS = ("connection failure please restart the game",)
LOGIN_RETURN_LIMIT = 4
MAX_BLOCKING_DISMISSALS = 3
POST_DIALOG_SETTLE_TIMEOUT_SECONDS = 180
READY_STABILITY_SECONDS = 30
VERIFICATION_RETRY_STABILITY_SECONDS = 5
MAX_RANKINGS_VERIFICATION_ATTEMPTS = 3
BLOCKING_ACTIONS = ("dismiss-support-energy", "dismiss-raven-event")
ACTION_TEMPLATES = (
    ("dismiss-support-energy", "support-energy-close-v1002130.png", 2.0),
    (
        "dismiss-support-energy",
        "support-energy-close-default-v1002130.png",
        2.0,
    ),
    ("dismiss-raven-event", "raven-dynamics-event-title-v1002130.png", 2.0),
    ("login", "login-button.png", 3.0),
    ("login", "login-button-v1002130.png", 3.0),
    ("server-selection", "server-selection.png", 3.0),
    ("enter-game", "enter-game-button.png", 5.0),
    ("play", "play-button.png", 5.0),
)
ACTION_CLICK_OFFSETS = {
    # The template is the unique dialog title. Its close control is fixed in
    # the same modal, 269 px right and 261 px above the title center.
    "dismiss-raven-event": ScreenPoint(269, -261),
}


def find_template_center(
    image: Image.Image,
    template: Image.Image,
    *,
    threshold: float,
) -> tuple[ScreenPoint | None, float]:
    source = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    needle = cv2.cvtColor(np.asarray(template.convert("RGB")), cv2.COLOR_RGB2GRAY)
    if needle.shape[0] > source.shape[0] or needle.shape[1] > source.shape[1]:
        return None, 0.0
    result = cv2.matchTemplate(source, needle, cv2.TM_CCOEFF_NORMED)
    _minimum, maximum, _minimum_location, maximum_location = cv2.minMaxLoc(result)
    if maximum < threshold:
        return None, float(maximum)
    return (
        ScreenPoint(
            maximum_location[0] + needle.shape[1] // 2,
            maximum_location[1] + needle.shape[0] // 2,
        ),
        float(maximum),
    )


class WindowsGameRecovery:
    def __init__(
        self,
        config: TrackerConfig,
        *,
        project_root: Path,
        sleep_function: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows game recovery requires Windows")
        self.config = config
        self.project_root = project_root
        self.templates = project_root / "assets" / "recovery"
        self.sleep_function = sleep_function
        self.monotonic = monotonic
        self.input_driver = Win32InputDriver()
        self.last_action_at: dict[str, float] = {}
        self.last_action_centers: dict[str, ScreenPoint] = {}
        self.last_status_ocr_at: float | None = None
        self.last_status_text = ""
        self.last_status_signature: bytes | None = None

    @staticmethod
    def _hidden_flags() -> int:
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))

    def _launcher_running(self) -> bool:
        completed = subprocess.run(
            ["tasklist.exe", "/FI", "IMAGENAME eq Launcher.exe", "/NH"],
            capture_output=True,
            text=True,
            creationflags=self._hidden_flags(),
            check=False,
        )
        if completed.returncode == 0 and "Launcher.exe" in completed.stdout:
            return True
        java_client = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-CimInstance Win32_Process | Where-Object { "
                "$_.Name -eq 'javaw.exe' -and $_.CommandLine -match "
                "'Splitscreen Studios.*Pirate Galaxy.*(Launcher|PirateGalaxy)\\.jar' "
                "} | Select-Object -First 1 -ExpandProperty ProcessId",
            ],
            capture_output=True,
            text=True,
            creationflags=self._hidden_flags(),
            check=False,
        )
        return java_client.returncode == 0 and java_client.stdout.strip().isdigit()

    @staticmethod
    def _window_responding(handle: int) -> bool:
        return not bool(ctypes.windll.user32.IsHungAppWindow(handle))

    def observe(self) -> RuntimeObservation:
        try:
            bounds = find_client_bounds(self.config.window_title_contains)
        except CaptureError:
            return RuntimeObservation(
                game_window_present=False,
                game_responding=False,
                launcher_running=self._launcher_running(),
                detail="game window is absent",
            )
        responding = self._window_responding(bounds.handle)
        return RuntimeObservation(
            game_window_present=True,
            game_responding=responding,
            launcher_running=self._launcher_running(),
            detail=(
                f"game window {bounds.width}x{bounds.height} is "
                f"{'responsive' if responding else 'hung'}"
            ),
        )

    def _start_client_task(self) -> None:
        completed = subprocess.run(
            [
                "schtasks.exe",
                "/Run",
                "/TN",
                self.config.recovery.client_task_name,
            ],
            capture_output=True,
            text=True,
            creationflags=self._hidden_flags(),
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("the Pirate Galaxy client task could not be started")

    def _close_game(self, bounds: WindowBounds) -> None:
        user32 = ctypes.windll.user32
        user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.PostMessageW(bounds.handle, 0x0010, 0, 0)  # WM_CLOSE
        deadline = self.monotonic() + self.config.recovery.graceful_close_seconds
        while self.monotonic() < deadline:
            self.sleep_function(1)
            try:
                find_client_bounds(self.config.window_title_contains)
            except CaptureError:
                return

        process_id = wintypes.DWORD()
        if not user32.GetWindowThreadProcessId(bounds.handle, ctypes.byref(process_id)):
            raise RuntimeError("could not resolve the hung game process")
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        process = kernel32.OpenProcess(0x0001, False, process_id.value)  # PROCESS_TERMINATE
        if not process:
            raise RuntimeError("could not open the hung game process")
        try:
            if not kernel32.TerminateProcess(process, 1):
                raise RuntimeError("could not terminate the hung game process")
        finally:
            kernel32.CloseHandle(process)

    def _screen_text(self, image: Image.Image) -> str:
        configure_tesseract()
        full_text = pytesseract.image_to_string(
            image, lang=self.config.ocr.language, config="--oem 3 --psm 6"
        )
        dialog = image.crop(
            (
                round(image.width * 0.20),
                round(image.height * 0.44),
                round(image.width * 0.80),
                round(image.height * 0.56),
            )
        )
        dialog = dialog.resize((dialog.width * 3, dialog.height * 3))
        dialog_text = pytesseract.image_to_string(
            dialog,
            lang=self.config.ocr.language,
            config="--oem 3 --psm 7",
        )
        return re.sub(r"[^\w]+", " ", f"{full_text} {dialog_text}".casefold()).strip()

    def _status_text(self, image: Image.Image) -> str:
        now = self.monotonic()
        sample = np.asarray(image.convert("L").resize((32, 24)))
        signature = np.packbits(sample >= sample.mean()).tobytes()
        if (
            self.last_status_ocr_at is not None
            and now - self.last_status_ocr_at < 30
            and signature == self.last_status_signature
        ):
            return self.last_status_text
        self.last_status_text = self._screen_text(image)
        self.last_status_ocr_at = now
        self.last_status_signature = signature
        return self.last_status_text

    def _clear_status_cache(self) -> None:
        self.last_status_ocr_at = None
        self.last_status_text = ""
        self.last_status_signature = None

    def _status_template_visible(self, image: Image.Image, filename: str) -> bool:
        template_path = self.templates / filename
        if not template_path.is_file():
            return False
        with Image.open(template_path) as template:
            center, _score = find_template_center(
                image,
                template,
                threshold=self.config.recovery.template_threshold,
            )
        return center is not None

    def _restart_required_visible(self, image: Image.Image) -> bool:
        if self._status_template_visible(image, "restart-required.png"):
            return True
        text = self._status_text(image)
        return any(words in text for words in RESTART_REQUIRED_WORDS)

    def _maintenance_visible(self, image: Image.Image) -> bool:
        if self._status_template_visible(image, "maintenance.png"):
            return True
        text = self._status_text(image)
        return any(word in text for word in MAINTENANCE_WORDS)

    def _action_template_visible(
        self,
        image: Image.Image,
        action: str,
        *,
        allow_degraded: bool = True,
    ) -> bool:
        prior_center = self.last_action_centers.get(action)
        for candidate_action, filename, _wait_seconds in ACTION_TEMPLATES:
            if candidate_action != action:
                continue
            template_path = self.templates / filename
            if not template_path.is_file():
                continue
            with Image.open(template_path) as template:
                center, _score = find_template_center(
                    image,
                    template,
                    threshold=self.config.recovery.template_threshold,
                )
                if center is None and allow_degraded and prior_center is not None:
                    center, _score = find_template_center(
                        image,
                        template,
                        threshold=max(
                            0.5,
                            self.config.recovery.template_threshold - 0.35,
                        ),
                    )
            if center is not None and (
                prior_center is None
                or (abs(center.x - prior_center.x) <= 4 and abs(center.y - prior_center.y) <= 4)
            ):
                return True
        return False

    def _advance_known_screen(
        self,
        image: Image.Image,
        bounds: WindowBounds,
        *,
        allowed_actions: frozenset[str] | None = None,
        allow_degraded: bool = True,
    ) -> str | None:
        now = self.monotonic()
        for action, filename, wait_seconds in ACTION_TEMPLATES:
            if allowed_actions is not None and action not in allowed_actions:
                continue
            template_path = self.templates / filename
            if not template_path.is_file():
                continue
            with Image.open(template_path) as template:
                center, score = find_template_center(
                    image,
                    template,
                    threshold=self.config.recovery.template_threshold,
                )
                degraded_repeat = False
                prior_center = self.last_action_centers.get(action)
                if center is None and allow_degraded and prior_center is not None:
                    center, score = find_template_center(
                        image,
                        template,
                        threshold=max(
                            0.5,
                            self.config.recovery.template_threshold - 0.35,
                        ),
                    )
                    degraded_repeat = center is not None and (
                        abs(center.x - prior_center.x) <= 4 and abs(center.y - prior_center.y) <= 4
                    )
                    if not degraded_repeat:
                        center = None
            if center is None:
                continue
            if (
                now - self.last_action_at.get(action, 0.0)
                < self.config.recovery.action_cooldown_seconds
            ):
                return f"waiting after {action} action"
            self.input_driver.activate(bounds.handle)
            click_offset = ACTION_CLICK_OFFSETS.get(action, ScreenPoint(0, 0))
            screen_point = ScreenPoint(
                bounds.left + center.x + click_offset.x,
                bounds.top + center.y + click_offset.y,
            )
            if degraded_repeat:
                self.input_driver.click_background(screen_point)
            else:
                self.input_driver.click(screen_point)
            if action == "server-selection":
                self.sleep_function(0.3)
                if degraded_repeat:
                    self.input_driver.click_background(screen_point)
                else:
                    self.input_driver.click(screen_point)
            self.last_action_at[action] = now
            self.last_action_centers[action] = center
            self.sleep_function(wait_seconds)
            verb = "retried" if degraded_repeat else "clicked"
            transport = " via window message" if degraded_repeat else ""
            return f"{verb} verified {action} template{transport} at confidence {score:.3f}"
        return None

    def _dismiss_blocking_dialogs(
        self,
        image: Image.Image,
        bounds: WindowBounds,
        progress: Callable[[str], None],
        *,
        stability_seconds: float = READY_STABILITY_SECONDS,
    ) -> tuple[Image.Image, WindowBounds] | None:
        """Dismiss a bounded stack of known foreground dialogs.

        Close controls never use the hover/degraded fallback. After every
        click, the exact template that authorized it must disappear before a
        different dialog may be handled. Once the stack is clear, gameplay
        must remain recognizable and dialog-free for a bounded stability
        window before rankings input is allowed.
        """
        dismissed_count = 0
        ready_seconds = 0.0
        settle_deadline: float | None = None
        waiting_reported = False
        while True:
            action = self._visible_blocking_action(image)
            if action is None and dismissed_count == 0:
                return image, bounds
            if action is not None:
                if dismissed_count >= MAX_BLOCKING_DISMISSALS:
                    self._save_unknown_screen(image)
                    return None
                result = self._advance_known_screen(
                    image,
                    bounds,
                    allowed_actions=frozenset({action}),
                    allow_degraded=False,
                )
                if result is None or result.startswith("waiting after "):
                    self._save_unknown_screen(image)
                    return None
                progress(result)
                dismissed_count += 1
                ready_seconds = 0.0
                image, bounds = capture_window(self.config.window_title_contains)
                if self._action_template_visible(
                    image,
                    action,
                    allow_degraded=False,
                ):
                    self._save_unknown_screen(image)
                    return None
                if settle_deadline is None:
                    settle_deadline = self.monotonic() + POST_DIALOG_SETTLE_TIMEOUT_SECONDS
                continue
            if not waiting_reported:
                progress(
                    "waiting for dialog-free stable gameplay after verified blocking dialog(s)"
                )
                waiting_reported = True
            if settle_deadline is not None and self.monotonic() >= settle_deadline:
                self._save_unknown_screen(image)
                return None
            self.sleep_function(self.config.recovery.probe_interval_seconds)
            try:
                image, bounds = capture_window(self.config.window_title_contains)
            except CaptureError:
                ready_seconds = 0.0
                continue
            if self._ready(image):
                ready_seconds += self.config.recovery.probe_interval_seconds
                if ready_seconds >= stability_seconds:
                    return image, bounds
            else:
                ready_seconds = 0.0

    def _visible_blocking_action(self, image: Image.Image) -> str | None:
        return next(
            (
                candidate
                for candidate in BLOCKING_ACTIONS
                if self._action_template_visible(
                    image,
                    candidate,
                    allow_degraded=False,
                )
            ),
            None,
        )

    def _save_unknown_screen(self, image: Image.Image) -> None:
        path = self.config.paths.diagnostics / "recovery-unknown.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)

    def _ready(self, image: Image.Image) -> bool:
        # The gameplay frame remains visible behind an open rankings window.
        # Once that window is present, only the verified Division 1 view is
        # ready for the tracker; Hall of Fame must not be hidden by the wrench.
        if rankings_window_visible(image):
            return division_view_visible(image)
        return gameplay_toolbar_visible(image, self.config.refresh) or gameplay_frame_visible(image)

    def _restore_division_rankings(
        self,
        image: Image.Image,
        bounds: WindowBounds,
        progress: Callable[[str], None],
    ) -> tuple[Image.Image, WindowBounds] | None:
        if not rankings_window_visible(image) or division_view_visible(image):
            return image, bounds
        progress("restoring verified Division 1 rankings from the generic rankings window")
        try:
            refresh_rankings(
                self.config.window_title_contains,
                self.config.refresh,
                driver=self.input_driver,
                sleep_function=self.sleep_function,
            )
            refreshed_image, refreshed_bounds = capture_window(self.config.window_title_contains)
        except (CaptureError, RefreshError) as error:
            LOGGER.warning("Division 1 recovery refresh failed: %s", error)
            self._save_unknown_screen(image)
            return None
        if not division_view_visible(refreshed_image):
            self._save_unknown_screen(refreshed_image)
            return None
        return refreshed_image, refreshed_bounds

    def _verify_division_rankings(
        self,
        image: Image.Image,
        bounds: WindowBounds,
        progress: Callable[[str], None],
    ) -> tuple[Image.Image, WindowBounds] | None:
        if division_view_visible(image):
            return image, bounds
        progress("verifying the Division 1 rankings view")
        try:
            refresh_rankings(
                self.config.window_title_contains,
                self.config.refresh,
                driver=self.input_driver,
                sleep_function=self.sleep_function,
            )
            verified_image, verified_bounds = capture_window(self.config.window_title_contains)
        except (CaptureError, RefreshError) as error:
            LOGGER.warning("Division 1 recovery verification failed: %s", error)
            try:
                failed_image, _failed_bounds = capture_window(self.config.window_title_contains)
            except CaptureError:
                failed_image = image
            self._save_unknown_screen(failed_image)
            return None
        if not division_view_visible(verified_image):
            self._save_unknown_screen(verified_image)
            return None
        return verified_image, verified_bounds

    def _verify_division_rankings_resilient(
        self,
        image: Image.Image,
        bounds: WindowBounds,
        progress: Callable[[str], None],
    ) -> tuple[Image.Image, WindowBounds] | None:
        """Retry rankings verification only when a known dialog interrupted it."""
        for attempt in range(1, MAX_RANKINGS_VERIFICATION_ATTEMPTS + 1):
            verified = self._verify_division_rankings(image, bounds, progress)
            if verified is not None:
                return verified
            try:
                failed_image, failed_bounds = capture_window(self.config.window_title_contains)
            except CaptureError:
                return None
            action = self._visible_blocking_action(failed_image)
            if action is None:
                return None
            if attempt >= MAX_RANKINGS_VERIFICATION_ATTEMPTS:
                self._save_unknown_screen(failed_image)
                return None
            progress(
                f"verified {action} dialog appeared during rankings verification; "
                "returning to bounded dialog recovery"
            )
            dismissed = self._dismiss_blocking_dialogs(
                failed_image,
                failed_bounds,
                progress,
                stability_seconds=VERIFICATION_RETRY_STABILITY_SECONDS,
            )
            if dismissed is None:
                return None
            image, bounds = dismissed
        return None

    def _park_pointer(self, bounds: WindowBounds) -> None:
        user32 = ctypes.windll.user32
        user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user32.SetCursorPos.restype = wintypes.BOOL
        if not user32.SetCursorPos(
            bounds.left + bounds.width - 12,
            bounds.top + bounds.height - 12,
        ):
            raise RuntimeError("could not park the pointer inside the game window")
        self.sleep_function(1)

    def _restore_ready_profile(
        self,
        image: Image.Image,
        bounds: WindowBounds,
        progress: Callable[[str], None],
    ) -> tuple[Image.Image, WindowBounds]:
        """Resize only after the game has reached a recognizable gameplay screen.

        Pirate Galaxy uses the same window title for its fixed-size startup
        splash and the actual game client. Trying to resize that splash fails
        even though the later gameplay window is resizable.
        """
        expected = (
            self.config.refresh.expected_width,
            self.config.refresh.expected_height,
        )
        if gameplay_frame_visible(image):
            self._park_pointer(bounds)
            image, bounds = capture_window(self.config.window_title_contains)
        if (bounds.width, bounds.height) == expected:
            return image, bounds
        bounds = restore_client_size(
            self.config.window_title_contains,
            expected_width=expected[0],
            expected_height=expected[1],
            sleep_function=self.sleep_function,
        )
        progress(f"restored game client to {bounds.width}x{bounds.height}")
        return capture_window(self.config.window_title_contains)

    def recover(
        self,
        progress: Callable[[str], None],
        *,
        allow_restart: bool,
        restart_maintenance: bool,
    ) -> RecoveryOutcome:
        observation = self.observe()
        restarted = False
        maintenance_probe = False
        login_attempts = 0
        if observation.game_window_present:
            bounds = find_client_bounds(self.config.window_title_contains)
            if observation.game_responding:
                image, bounds = capture_window(self.config.window_title_contains)
                restored_rankings = self._restore_division_rankings(image, bounds, progress)
                if restored_rankings is None:
                    return RecoveryOutcome(
                        RecoveryStatus.DEFERRED,
                        "rankings window is responsive but Division 1 could not be restored",
                        retry_after_seconds=300,
                    )
                image, bounds = restored_rankings
                dismissed_dialogs = self._dismiss_blocking_dialogs(image, bounds, progress)
                if dismissed_dialogs is None:
                    if not allow_restart:
                        return RecoveryOutcome(
                            RecoveryStatus.DEFERRED,
                            "verified blocking dialog did not close and restart "
                            "budget is exhausted",
                            retry_after_seconds=900,
                        )
                    progress("restarting client after a verified blocking dialog did not close")
                    self._close_game(bounds)
                    self._clear_status_cache()
                    restarted = True
                else:
                    image, bounds = dismissed_dialogs
                if not restarted and self._ready(image):
                    try:
                        image, bounds = self._restore_ready_profile(image, bounds, progress)
                    except CaptureError as error:
                        return RecoveryOutcome(
                            RecoveryStatus.DEFERRED,
                            "gameplay is ready but the window profile could not be restored: "
                            f"{error}",
                            retry_after_seconds=300,
                        )
                    if not self._ready(image):
                        return RecoveryOutcome(
                            RecoveryStatus.DEFERRED,
                            "gameplay markers disappeared while restoring the window profile",
                            retry_after_seconds=30,
                        )
                    verified_rankings = self._verify_division_rankings_resilient(
                        image,
                        bounds,
                        progress,
                    )
                    if verified_rankings is not None:
                        return RecoveryOutcome(
                            RecoveryStatus.RECOVERED,
                            "responsive Division 1 rankings are verified",
                        )
                    if not allow_restart:
                        return RecoveryOutcome(
                            RecoveryStatus.DEFERRED,
                            "game is responsive but Division 1 verification failed and "
                            "restart budget is exhausted",
                            retry_after_seconds=900,
                        )
                    progress("restarting client after Division 1 verification failed")
                    self._close_game(bounds)
                    self._clear_status_cache()
                    restarted = True
                if not restarted:
                    restart_required = self._restart_required_visible(image)
                    if restart_required:
                        if not allow_restart:
                            return RecoveryOutcome(
                                RecoveryStatus.DEFERRED,
                                "game requests a restart but restart budget is exhausted",
                                retry_after_seconds=(
                                    self.config.recovery.maintenance_backoff_seconds[-1]
                                ),
                                maintenance=True,
                            )
                        progress("restarting client after explicit connection failure")
                        self._close_game(bounds)
                        self._clear_status_cache()
                        restarted = True
                        maintenance_probe = restart_maintenance
                    elif restart_maintenance and self._action_template_visible(image, "login"):
                        if not allow_restart:
                            return RecoveryOutcome(
                                RecoveryStatus.DEFERRED,
                                "login returned during maintenance but restart budget is exhausted",
                                retry_after_seconds=(
                                    self.config.recovery.maintenance_backoff_seconds[-1]
                                ),
                                maintenance=True,
                            )
                        progress("restarting client for the login-return maintenance probe")
                        self._close_game(bounds)
                        self._clear_status_cache()
                        restarted = True
                        maintenance_probe = True
                    elif self._maintenance_visible(image):
                        if not restart_maintenance:
                            return RecoveryOutcome(
                                RecoveryStatus.DEFERRED,
                                "game reports maintenance; waiting before restart probe",
                                retry_after_seconds=(
                                    self.config.recovery.maintenance_backoff_seconds[0]
                                ),
                                maintenance=True,
                            )
                        if not allow_restart:
                            return RecoveryOutcome(
                                RecoveryStatus.DEFERRED,
                                "game reports maintenance but restart budget is exhausted",
                                retry_after_seconds=(
                                    self.config.recovery.maintenance_backoff_seconds[-1]
                                ),
                                maintenance=True,
                            )
                        progress("restarting client after confirmed maintenance backoff")
                        self._close_game(bounds)
                        self._clear_status_cache()
                        restarted = True
                        maintenance_probe = True
                    else:
                        action = self._advance_known_screen(image, bounds)
                        if action is None:
                            self._save_unknown_screen(image)
                            return RecoveryOutcome(
                                RecoveryStatus.DEFERRED,
                                "responsive but unknown game screen; no click was attempted",
                                retry_after_seconds=300,
                            )
                        progress(action)
                        if "verified login template" in action:
                            login_attempts += 1
            elif not allow_restart:
                return RecoveryOutcome(
                    RecoveryStatus.DEFERRED,
                    "game is hung but restart budget is exhausted",
                    retry_after_seconds=900,
                )
            else:
                progress("closing confirmed hung game window")
                self._close_game(bounds)
                self._clear_status_cache()
                restarted = True

        observation = self.observe()
        if not observation.game_window_present and not observation.launcher_running:
            if not allow_restart:
                return RecoveryOutcome(
                    RecoveryStatus.DEFERRED,
                    "game is absent but restart budget is exhausted",
                    retry_after_seconds=900,
                )
            progress("starting Pirate Galaxy client task")
            self._start_client_task()
            restarted = True
        elif observation.launcher_running:
            progress("launcher or updater is already running")

        deadline = self.monotonic() + self.config.recovery.launch_timeout_seconds
        last_progress = 0.0
        while self.monotonic() < deadline:
            now = self.monotonic()
            if now - last_progress >= 30:
                progress("waiting for a verified gameplay state")
                last_progress = now
            try:
                bounds = find_client_bounds(self.config.window_title_contains)
            except CaptureError:
                self.sleep_function(self.config.recovery.probe_interval_seconds)
                continue
            if not self._window_responding(bounds.handle):
                self.sleep_function(self.config.recovery.probe_interval_seconds)
                continue
            image, bounds = capture_window(self.config.window_title_contains)
            restored_rankings = self._restore_division_rankings(image, bounds, progress)
            if restored_rankings is None:
                self.sleep_function(self.config.recovery.probe_interval_seconds)
                continue
            image, bounds = restored_rankings
            dismissed_dialogs = self._dismiss_blocking_dialogs(image, bounds, progress)
            if dismissed_dialogs is None:
                return RecoveryOutcome(
                    RecoveryStatus.DEFERRED,
                    "verified blocking dialog remains after client recovery",
                    restarted=restarted,
                    retry_after_seconds=900,
                )
            image, bounds = dismissed_dialogs
            if self._ready(image):
                try:
                    image, bounds = self._restore_ready_profile(image, bounds, progress)
                except CaptureError as error:
                    return RecoveryOutcome(
                        RecoveryStatus.DEFERRED,
                        f"gameplay is ready but the window profile could not be restored: {error}",
                        restarted=restarted,
                        retry_after_seconds=300,
                    )
                if not self._ready(image):
                    self.sleep_function(self.config.recovery.probe_interval_seconds)
                    continue
                verified_rankings = self._verify_division_rankings_resilient(
                    image,
                    bounds,
                    progress,
                )
                if verified_rankings is not None:
                    return RecoveryOutcome(
                        RecoveryStatus.RECOVERED,
                        "Division 1 rankings are verified after client recovery",
                        restarted=restarted,
                    )
                return RecoveryOutcome(
                    RecoveryStatus.DEFERRED,
                    "gameplay became responsive but Division 1 could not be verified",
                    restarted=restarted,
                    retry_after_seconds=300,
                )
            if self._restart_required_visible(image):
                return RecoveryOutcome(
                    RecoveryStatus.DEFERRED,
                    "connection failure remains after client restart",
                    restarted=restarted,
                    retry_after_seconds=self.config.recovery.maintenance_backoff_seconds[0],
                    maintenance=True,
                    maintenance_probe=maintenance_probe,
                )
            if self._maintenance_visible(image):
                return RecoveryOutcome(
                    RecoveryStatus.DEFERRED,
                    "game reports maintenance",
                    restarted=restarted,
                    retry_after_seconds=self.config.recovery.maintenance_backoff_seconds[0],
                    maintenance=True,
                    maintenance_probe=maintenance_probe,
                )
            action = self._advance_known_screen(image, bounds)
            if action is not None:
                progress(action)
                if "verified login template" in action:
                    login_attempts += 1
                    if login_attempts >= LOGIN_RETURN_LIMIT:
                        return RecoveryOutcome(
                            RecoveryStatus.DEFERRED,
                            "login returned repeatedly; treating the game service as unavailable",
                            restarted=restarted,
                            maintenance=True,
                            maintenance_probe=maintenance_probe,
                        )
                elif "verified server-selection template" in action:
                    login_attempts = 0
                continue
            self._save_unknown_screen(image)
            self.sleep_function(self.config.recovery.probe_interval_seconds)

        return RecoveryOutcome(
            RecoveryStatus.FAILED,
            "game did not reach a verified gameplay state before timeout",
            restarted=restarted,
            retry_after_seconds=900,
        )
