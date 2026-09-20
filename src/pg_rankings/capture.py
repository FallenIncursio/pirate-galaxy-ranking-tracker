from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass

from PIL import Image, ImageGrab


class CaptureError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WindowBounds:
    title: str
    left: int
    top: int
    width: int
    height: int
    handle: int = 0
    window_left: int = 0
    window_top: int = 0


WindowMatch = tuple[int, str, bool, bool] | tuple[int, str, bool, bool, bool]


def _choose_window_match(matches: list[WindowMatch], title_contains: str) -> WindowMatch:
    if not matches:
        raise CaptureError(f"no window contains title {title_contains!r}")

    def priority(match: WindowMatch) -> int:
        _handle, title, visible, iconic = match[:4]
        title_casefold = title.casefold()
        title_signals_hung = any(
            marker in title_casefold for marker in ("(not responding)", "(keine rückmeldung)")
        )
        responding = (match[4] if len(match) == 5 else True) and not title_signals_hung
        return (
            8 * responding
            + 4 * visible
            + 2 * iconic
            + int(title.casefold() == title_contains.casefold())
        )

    highest = max(priority(match) for match in matches)
    preferred = [match for match in matches if priority(match) == highest]
    if len(preferred) > 1:
        titles = ", ".join(match[1] for match in preferred)
        raise CaptureError(f"multiple matching windows found: {titles}")
    return preferred[0]


def _find_client_bounds_win32(title_contains: str) -> WindowBounds:
    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()

    matches: list[WindowMatch] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def enum_callback(hwnd: int, _lparam: int) -> bool:
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value
        if title_contains.casefold() in title.casefold():
            matches.append(
                (
                    hwnd,
                    title,
                    bool(user32.IsWindowVisible(hwnd)),
                    bool(user32.IsIconic(hwnd)),
                    not bool(user32.IsHungAppWindow(hwnd)),
                )
            )
        return True

    user32.EnumWindows(callback_type(enum_callback), 0)
    match = _choose_window_match(matches, title_contains)
    hwnd, title, visible, iconic = match[:4]
    if iconic:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.5)
    elif not visible:
        user32.ShowWindow(hwnd, 5)  # SW_SHOW
        time.sleep(0.5)

    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise CaptureError("GetClientRect failed")
    origin = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        raise CaptureError("ClientToScreen failed")
    window_rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(window_rect)):
        raise CaptureError("GetWindowRect failed")
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width < 640 or height < 480:
        raise CaptureError(f"game client area is unexpectedly small: {width}x{height}")
    return WindowBounds(
        title,
        origin.x,
        origin.y,
        width,
        height,
        handle=int(hwnd),
        window_left=window_rect.left,
        window_top=window_rect.top,
    )


def find_client_bounds(title_contains: str) -> WindowBounds:
    if sys.platform == "win32":
        return _find_client_bounds_win32(title_contains)
    if sys.platform.startswith("linux"):
        from .x11 import find_client_bounds_x11

        return find_client_bounds_x11(title_contains)
    raise CaptureError(f"window capture is unsupported on platform {sys.platform!r}")


def _crop_client_image(window_image: Image.Image, bounds: WindowBounds) -> Image.Image:
    # Pillow's Windows HWND capture currently returns the client area directly.
    # Keep the offset-based path for implementations that return the full window.
    if window_image.size == (bounds.width, bounds.height):
        return window_image.convert("RGB")
    left = bounds.left - bounds.window_left
    top = bounds.top - bounds.window_top
    right = left + bounds.width
    bottom = top + bounds.height
    if left < 0 or top < 0 or right > window_image.width or bottom > window_image.height:
        raise CaptureError(
            "captured window does not contain the expected client area: "
            f"window={window_image.width}x{window_image.height}, "
            f"client=({left},{top})-({right},{bottom})"
        )
    return window_image.crop((left, top, right, bottom)).convert("RGB")


def capture_window(title_contains: str) -> tuple[Image.Image, WindowBounds]:
    if sys.platform.startswith("linux"):
        from .x11 import capture_window_x11

        return capture_window_x11(title_contains)
    if sys.platform != "win32":
        raise CaptureError(f"window capture is unsupported on platform {sys.platform!r}")
    bounds = find_client_bounds(title_contains)
    try:
        window_image = ImageGrab.grab(window=bounds.handle)
    except (OSError, ValueError) as error:
        raise CaptureError(f"window capture failed: {error}") from error
    return _crop_client_image(window_image, bounds), bounds


def set_window_process_throttled(handle: int, throttled: bool) -> None:
    """Give OCR CPU priority over the game without changing its window state."""
    if sys.platform != "win32":
        # A regular Linux user can lower a process priority but cannot reliably
        # restore it. The isolated display already limits the game's refresh
        # rate, so keep this Windows-only optimization as a no-op on Linux.
        return
    if not handle:
        raise CaptureError("cannot adjust process priority without a window handle")

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetPriorityClass.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    process_id = wintypes.DWORD()
    if not user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id)):
        raise CaptureError("GetWindowThreadProcessId failed")
    process = kernel32.OpenProcess(0x0200, False, process_id.value)  # PROCESS_SET_INFORMATION
    if not process:
        raise CaptureError("OpenProcess failed while adjusting game priority")
    try:
        priority_class = 0x00000040 if throttled else 0x00000020
        if not kernel32.SetPriorityClass(process, priority_class):
            raise CaptureError("SetPriorityClass failed for the game process")
    finally:
        kernel32.CloseHandle(process)
