from __future__ import annotations

import ctypes
import sys
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from typing import Protocol

from PIL import Image

from .capture import WindowBounds, capture_window
from .config import RefreshConfig


class RefreshError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ScreenPoint:
    x: int
    y: int


class InputDriver(Protocol):
    def activate(self, handle: int) -> None: ...

    def click(self, point: ScreenPoint) -> None: ...


def client_point(bounds: WindowBounds, x: float, y: float) -> ScreenPoint:
    if not 0 <= x <= 1 or not 0 <= y <= 1:
        raise RefreshError("click coordinates must be normalized")
    return ScreenPoint(
        x=bounds.left + round(x * bounds.width),
        y=bounds.top + round(y * bounds.height),
    )


class Win32InputDriver:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RefreshError("UI refresh is only supported inside the Windows VM")
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32

        # ctypes otherwise assumes 32-bit integers for pointer-sized HWND values.
        # Explicit signatures are required on 64-bit Windows; without them a
        # successful foreground switch can still compare as a different handle.
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.ShowWindowAsync.restype = wintypes.BOOL
        self.user32.BringWindowToTop.argtypes = [wintypes.HWND]
        self.user32.BringWindowToTop.restype = wintypes.BOOL
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.SetActiveWindow.argtypes = [wintypes.HWND]
        self.user32.SetActiveWindow.restype = wintypes.HWND
        self.user32.SetFocus.argtypes = [wintypes.HWND]
        self.user32.SetFocus.restype = wintypes.HWND
        self.user32.AttachThreadInput.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.BOOL,
        ]
        self.user32.AttachThreadInput.restype = wintypes.BOOL
        self.user32.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self.user32.PeekMessageW.restype = wintypes.BOOL
        self.user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        self.user32.SetWindowPos.restype = wintypes.BOOL
        self.user32.WindowFromPoint.argtypes = [wintypes.POINT]
        self.user32.WindowFromPoint.restype = wintypes.HWND
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self.user32.SetCursorPos.restype = wintypes.BOOL
        self.user32.ScreenToClient.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.POINT),
        ]
        self.user32.ScreenToClient.restype = wintypes.BOOL
        self.user32.ClientToScreen.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.POINT),
        ]
        self.user32.ClientToScreen.restype = wintypes.BOOL
        try:
            self.user32.PhysicalToLogicalPointForPerMonitorDPI.argtypes = [
                wintypes.HWND,
                ctypes.POINTER(wintypes.POINT),
            ]
            self.user32.PhysicalToLogicalPointForPerMonitorDPI.restype = wintypes.BOOL
        except AttributeError:
            pass
        self.user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self.user32.PostMessageW.restype = wintypes.BOOL
        self.user32.mouse_event.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_size_t,
        ]
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._target_handle = 0
        self._topmost_fallback = False
        self._activation_diagnostics = ""

    def _post_verified_window_click(self, handle: int, point: ScreenPoint) -> None:
        client = self._window_message_client_point(handle, point)
        packed_point = (client.x & 0xFFFF) | ((client.y & 0xFFFF) << 16)
        messages = (
            (0x0200, 0),  # WM_MOUSEMOVE
            (0x0201, 0x0001),  # WM_LBUTTONDOWN, MK_LBUTTON
            (0x0202, 0),  # WM_LBUTTONUP
        )
        for message, buttons in messages:
            if not self.user32.PostMessageW(handle, message, buttons, packed_point):
                raise RefreshError("PostMessageW failed for background click")

    def _window_message_client_point(self, handle: int, point: ScreenPoint) -> wintypes.POINT:
        physical_point = wintypes.POINT(point.x, point.y)
        converter = getattr(
            self.user32,
            "PhysicalToLogicalPointForPerMonitorDPI",
            None,
        )
        if callable(converter):
            physical_origin = wintypes.POINT(0, 0)
            if not self.user32.ClientToScreen(handle, ctypes.byref(physical_origin)):
                raise RefreshError("ClientToScreen failed for background click")
            logical_point = wintypes.POINT(physical_point.x, physical_point.y)
            logical_origin = wintypes.POINT(physical_origin.x, physical_origin.y)
            if not converter(self._target_handle, ctypes.byref(logical_point)) or not converter(
                self._target_handle,
                ctypes.byref(logical_origin),
            ):
                raise RefreshError("DPI conversion failed for background click")
            return wintypes.POINT(
                logical_point.x - logical_origin.x,
                logical_point.y - logical_origin.y,
            )
        if not self.user32.ScreenToClient(handle, ctypes.byref(physical_point)):
            raise RefreshError("ScreenToClient failed for background click")
        return physical_point

    @staticmethod
    def _handle_value(handle: int | None) -> int:
        return int(handle or 0)

    def _root_handle(self, handle: int) -> int:
        if not handle:
            return 0
        root = self._handle_value(self.user32.GetAncestor(handle, 2))  # GA_ROOT
        return root or handle

    def _release_topmost(self) -> None:
        if not self._topmost_fallback or not self._target_handle:
            return
        no_move_or_size = 0x0001 | 0x0002 | 0x0040
        self.user32.SetWindowPos(
            self._target_handle, -2, 0, 0, 0, 0, no_move_or_size
        )  # HWND_NOTOPMOST
        self._topmost_fallback = False

    def _verified_window_at_point(self, point: ScreenPoint) -> int:
        window_at_point = self._handle_value(
            self.user32.WindowFromPoint(wintypes.POINT(point.x, point.y))
        )
        root_at_point = self._root_handle(window_at_point)
        if root_at_point != self._target_handle:
            raise RefreshError(
                "refusing calibrated click because Pirate Galaxy is not at the target point "
                f"(target=0x{self._target_handle:x}, point_root=0x{root_at_point:x})"
            )
        return window_at_point

    def click_background(self, point: ScreenPoint) -> None:
        if not self._target_handle:
            raise RefreshError("background click attempted before activating Pirate Galaxy")
        try:
            window_at_point = self._verified_window_at_point(point)
            self._post_verified_window_click(window_at_point, point)
        finally:
            self._release_topmost()

    def activate(self, handle: int) -> None:
        self._target_handle = handle
        self._topmost_fallback = False
        self._activation_diagnostics = ""
        # Recover from a prior worker/process that exited between the temporary
        # HWND_TOPMOST promotion and its cleanup.
        no_move_or_size = 0x0001 | 0x0002 | 0x0040
        self.user32.SetWindowPos(handle, -2, 0, 0, 0, 0, no_move_or_size)
        if self.user32.IsIconic(handle):
            self.user32.ShowWindowAsync(handle, 9)  # SW_RESTORE

        # A background Python thread may not have a Win32 message queue yet.
        # AttachThreadInput requires one for both participating threads.
        message = wintypes.MSG()
        self.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)

        last_foreground = 0
        last_set_foreground = False
        last_attached_pairs: list[tuple[int, int]] = []
        for _attempt in range(3):
            foreground = self._handle_value(self.user32.GetForegroundWindow())
            if foreground == handle:
                return

            current_thread = int(self.kernel32.GetCurrentThreadId())
            target_thread = int(self.user32.GetWindowThreadProcessId(handle, None))
            foreground_thread = (
                int(self.user32.GetWindowThreadProcessId(foreground, None)) if foreground else 0
            )
            candidate_pairs = (
                (foreground_thread, target_thread),
                (current_thread, foreground_thread),
                (current_thread, target_thread),
            )
            attached_pairs: list[tuple[int, int]] = []
            for source_thread, destination_thread in candidate_pairs:
                if (
                    source_thread
                    and destination_thread
                    and source_thread != destination_thread
                    and (source_thread, destination_thread) not in attached_pairs
                    and self.user32.AttachThreadInput(source_thread, destination_thread, True)
                ):
                    attached_pairs.append((source_thread, destination_thread))
            try:
                self.user32.ShowWindowAsync(handle, 5)  # SW_SHOW
                self.user32.BringWindowToTop(handle)
                last_set_foreground = bool(self.user32.SetForegroundWindow(handle))
                self.user32.SetActiveWindow(handle)
                self.user32.SetFocus(handle)

                # Moving briefly through the topmost band makes the z-order
                # deterministic even when Windows' foreground lock is active.
                self.user32.SetWindowPos(handle, -1, 0, 0, 0, 0, no_move_or_size)
                self.user32.SetWindowPos(handle, -2, 0, 0, 0, 0, no_move_or_size)
            finally:
                for source_thread, destination_thread in reversed(attached_pairs):
                    self.user32.AttachThreadInput(source_thread, destination_thread, False)
            time.sleep(0.35)
            last_foreground = self._handle_value(self.user32.GetForegroundWindow())
            last_attached_pairs = attached_pairs
            if last_foreground == handle:
                return

            # Fallback for Windows foreground-lock timing between task cycles.
            self.user32.keybd_event(0x12, 0, 0, 0)
            self.user32.keybd_event(0x12, 0, 0x0002, 0)
        self._activation_diagnostics = (
            f"(target=0x{handle:x}, foreground=0x{last_foreground:x}, "
            f"set_foreground={last_set_foreground}, attached={last_attached_pairs})"
        )

        # Windows may reject SetForegroundWindow for a limited scheduled task
        # even in the same interactive session. Keep the game temporarily at
        # the top of the z-order. click() verifies the actual window under the
        # calibrated point before sending input; the first click itself then
        # gives the game foreground ownership and immediately removes topmost.
        if not self.user32.SetWindowPos(handle, -1, 0, 0, 0, 0, no_move_or_size):
            raise RefreshError(
                "Pirate Galaxy could not be made safely clickable " + self._activation_diagnostics
            )
        self._topmost_fallback = True
        time.sleep(0.35)

    def click(self, point: ScreenPoint) -> None:
        if not self._target_handle:
            raise RefreshError("click attempted before activating Pirate Galaxy")
        using_topmost_fallback = self._topmost_fallback
        used_background_click = False
        foreground = 0
        try:
            cursor_positioned = bool(self.user32.SetCursorPos(point.x, point.y))
            window_at_point = self._verified_window_at_point(point)
            if cursor_positioned:
                self.user32.mouse_event(0x0002, 0, 0, 0, 0)
                time.sleep(0.05)
                self.user32.mouse_event(0x0004, 0, 0, 0, 0)
            else:
                self._post_verified_window_click(window_at_point, point)
                used_background_click = True
            if using_topmost_fallback and not used_background_click:
                time.sleep(0.2)
                foreground = self._root_handle(
                    self._handle_value(self.user32.GetForegroundWindow())
                )
        finally:
            if using_topmost_fallback:
                self._release_topmost()
        if (
            using_topmost_fallback
            and not used_background_click
            and foreground != self._target_handle
        ):
            raise RefreshError(
                "Pirate Galaxy did not receive foreground after the verified click "
                + self._activation_diagnostics
            )


def default_input_driver() -> InputDriver:
    if sys.platform == "win32":
        return Win32InputDriver()
    if sys.platform.startswith("linux"):
        from .x11 import X11InputDriver

        return X11InputDriver()
    raise RefreshError(f"UI refresh is unsupported on platform {sys.platform!r}")


CaptureFunction = Callable[[str], tuple[Image.Image, WindowBounds]]
SleepFunction = Callable[[float], None]


def division_view_visible(image: Image.Image) -> bool:
    if image.size == (960, 600):
        marker = (155, 129, 788, 147)
    elif image.size == (800, 600):
        marker = (70, 115, 730, 133)
    elif image.size == (954, 610):
        marker = (146, 109, 807, 127)
    else:
        return False
    pixels = image.convert("RGB").crop(marker).get_flattened_data()
    orange = sum(
        1
        for red, green, blue in pixels
        if red >= 180 and 70 <= green <= 190 and blue <= 90 and red >= green + 60
    )
    neutral = sum(
        1
        for red, green, blue in pixels
        if 20 <= (red + green + blue) // 3 <= 110
        and max(red, green, blue) - min(red, green, blue) <= 12
    )
    total = (marker[2] - marker[0]) * (marker[3] - marker[1])
    width, height = image.size
    if image.size == (800, 600):
        # The compact English tab is shorter than the 960/954 layouts. Keep
        # the marker inside its selected blue background instead of sampling
        # the neutral area to its right.
        ladder_marker = (150, 54, 260, 77)
    else:
        ladder_marker = (
            round(width * 0.239),
            round(height * 0.090),
            round(width * 0.351),
            round(height * 0.128),
        )
    ladder_pixels = image.convert("RGB").crop(ladder_marker).get_flattened_data()
    ladder_selected = sum(
        1
        for red, green, blue in ladder_pixels
        if 75 <= red <= 145
        and 90 <= green <= 170
        and 110 <= blue <= 200
        and blue >= red + 25
        and blue >= green + 12
    )
    ladder_total = (ladder_marker[2] - ladder_marker[0]) * (ladder_marker[3] - ladder_marker[1])
    if image.size == (800, 600):
        score_icon_marker = (700, 161, 730, 506)
    else:
        score_icon_marker = (
            round(width * 0.813),
            round(height * 0.269),
            round(width * 0.846),
            round(height * 0.844),
        )
    score_icon_pixels = image.convert("RGB").crop(score_icon_marker).get_flattened_data()
    blue_score_icons = sum(
        1
        for red, green, blue in score_icon_pixels
        if blue >= 90 and blue >= red + 20 and blue >= green + 10
    )
    score_icon_total = (score_icon_marker[2] - score_icon_marker[0]) * (
        score_icon_marker[3] - score_icon_marker[1]
    )
    # The countdown is a progress bar: early in the week only a short orange
    # section remains and the rest consists of several neutral gray shades.
    # The Hall-of-Fame page also contains a neutral strip at this position.
    # Require the selected ladder tab and the blue Division score icons as
    # independent markers so that page can never pass as the live table.
    return (
        (orange + neutral) / total >= 0.80
        and ladder_selected / ladder_total >= 0.35
        and blue_score_icons / score_icon_total >= 0.01
    )


def rankings_window_visible(image: Image.Image) -> bool:
    """Detect the rankings window independently of its changing countdown bar."""
    if image.size == (960, 600):
        marker = (58, 34, 890, 56)
    elif image.size == (800, 600):
        marker = (48, 34, 742, 56)
    elif image.size == (954, 610):
        marker = (126, 20, 829, 43)
    else:
        return False
    pixels = image.convert("RGB").crop(marker).get_flattened_data()
    neutral_dark = sum(
        1
        for red, green, blue in pixels
        if 20 <= (red + green + blue) // 3 <= 65
        and max(red, green, blue) - min(red, green, blue) <= 12
    )
    total = (marker[2] - marker[0]) * (marker[3] - marker[1])
    return neutral_dark / total >= 0.75


def gameplay_toolbar_visible(image: Image.Image, config: RefreshConfig) -> bool:
    """Recognize the calibrated toolbar before allowing a gameplay click."""
    center_x = round(config.menu_icon_x * image.width)
    center_y = round(config.menu_icon_y * image.height)
    half_width = max(14, round(image.width * 0.018))
    half_height = max(12, round(image.height * 0.023))
    marker = (
        max(0, center_x - half_width),
        max(0, center_y - half_height),
        min(image.width, center_x + half_width + 1),
        min(image.height, center_y + half_height + 1),
    )
    pixels = image.convert("RGB").crop(marker).get_flattened_data()
    blue = sum(
        1 for red, green, blue in pixels if blue >= 65 and blue >= red + 15 and blue >= green + 5
    )
    bright = sum(1 for red, green, blue in pixels if min(red, green, blue) >= 140)
    total = (marker[2] - marker[0]) * (marker[3] - marker[1])
    return blue / total >= 0.25 and bright / total >= 0.04


def gameplay_frame_visible(image: Image.Image) -> bool:
    """Recognize the stable green wrench in the live gameplay frame.

    Unlike the configured menu icon, this marker remains visible while a
    first-login tooltip temporarily covers the centered toolbar.
    """
    marker = (
        round(image.width * 0.9625),
        0,
        image.width,
        min(40, round(image.height * 0.067)),
    )
    pixels = image.convert("RGB").crop(marker).get_flattened_data()
    green = sum(
        1
        for red, green, blue in pixels
        if green >= 100 and green >= red + 20 and green >= blue + 10
    )
    blue = sum(
        1 for red, green, blue in pixels if blue >= 70 and blue >= red + 15 and blue >= green - 20
    )
    total = (marker[2] - marker[0]) * (marker[3] - marker[1])
    return green / total >= 0.05 and blue / total >= 0.08


def refresh_rankings(
    title_contains: str,
    config: RefreshConfig,
    *,
    capture_function: CaptureFunction = capture_window,
    driver: InputDriver | None = None,
    sleep_function: SleepFunction = time.sleep,
) -> WindowBounds:
    before_image, bounds = capture_function(title_contains)
    expected = (config.expected_width, config.expected_height)
    if (bounds.width, bounds.height) != expected:
        raise RefreshError(
            "game client size does not match the calibrated refresh profile: "
            f"{bounds.width}x{bounds.height} instead of {expected[0]}x{expected[1]}"
        )
    if not bounds.handle:
        raise RefreshError("game window handle is missing")

    input_driver = driver or default_input_driver()

    def click_point(point: ScreenPoint, *, background: bool = False) -> None:
        background_click = getattr(input_driver, "click_background", None)
        if background and callable(background_click):
            background_click(point)
        else:
            input_driver.click(point)

    def open_and_select_rankings(
        image: Image.Image,
        current_bounds: WindowBounds,
    ) -> None:
        rankings_visible = rankings_window_visible(image)
        if not rankings_visible and not (
            gameplay_toolbar_visible(image, config) or gameplay_frame_visible(image)
        ):
            raise RefreshError(
                "gameplay toolbar is not visible; refusing rankings clicks on an unknown screen"
            )
        input_driver.activate(current_bounds.handle)
        if rankings_visible:
            for _attempt in range(2):
                click_point(
                    client_point(current_bounds, config.menu_icon_x, config.menu_icon_y),
                )
                sleep_function(config.menu_wait_seconds)
                image, current_bounds = capture_function(title_contains)
                if (current_bounds.width, current_bounds.height) != expected:
                    raise RefreshError("game client size changed while closing rankings")
                if not rankings_window_visible(image):
                    break
            if rankings_window_visible(image):
                raise RefreshError("rankings window did not close after two verified menu clicks")
            input_driver.activate(current_bounds.handle)
        menu_image = image
        menu_bounds = current_bounds
        for _attempt in range(2):
            click_point(
                client_point(menu_bounds, config.menu_icon_x, config.menu_icon_y),
            )
            sleep_function(config.menu_wait_seconds)
            menu_image, menu_bounds = capture_function(title_contains)
            if (menu_bounds.width, menu_bounds.height) != expected:
                raise RefreshError("game client size changed while opening rankings")
            if rankings_window_visible(menu_image):
                break
        if not rankings_window_visible(menu_image):
            raise RefreshError(
                "rankings window did not open after two verified menu clicks; "
                "refusing the rankings-tab click"
            )
        input_driver.activate(menu_bounds.handle)
        click_point(
            client_point(menu_bounds, config.rankings_tab_x, config.rankings_tab_y),
        )
        sleep_function(config.content_wait_seconds)

    def reselect_rankings_tab(current_bounds: WindowBounds) -> None:
        input_driver.activate(current_bounds.handle)
        click_point(
            client_point(current_bounds, config.rankings_tab_x, config.rankings_tab_y),
        )
        sleep_function(config.content_wait_seconds)

    open_and_select_rankings(before_image, bounds)

    refreshed_image, refreshed_bounds = capture_function(title_contains)
    if (refreshed_bounds.width, refreshed_bounds.height) != expected:
        raise RefreshError("game client size changed during rankings refresh")
    if division_view_visible(refreshed_image):
        return refreshed_bounds

    # Directly re-select the ladder tab only after the first view finished
    # loading. Around the weekly reset Pirate Galaxy can open Hall of Fame and
    # ignore a second click issued during its transition.
    if rankings_window_visible(refreshed_image):
        reselect_rankings_tab(refreshed_bounds)
        reselected_image, reselected_bounds = capture_function(title_contains)
        if (reselected_bounds.width, reselected_bounds.height) != expected:
            raise RefreshError("game client size changed during rankings tab retry")
        if division_view_visible(reselected_image):
            return reselected_bounds
        refreshed_image, refreshed_bounds = reselected_image, reselected_bounds

    # Pirate Galaxy occasionally ignores one of the otherwise verified clicks
    # while it is rendering. Repeat the bounded close/open/tab sequence once;
    # every click is still constrained to the calibrated game window.
    open_and_select_rankings(refreshed_image, refreshed_bounds)
    retry_image, retry_bounds = capture_function(title_contains)
    if (retry_bounds.width, retry_bounds.height) != expected:
        raise RefreshError("game client size changed during rankings refresh retry")
    if not division_view_visible(retry_image):
        raise RefreshError("Division 1 rankings did not appear after the bounded refresh retry")
    return retry_bounds
