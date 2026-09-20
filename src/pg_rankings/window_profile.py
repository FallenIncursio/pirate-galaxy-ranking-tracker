from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

from .capture import CaptureError, WindowBounds, find_client_bounds

SW_RESTORE = 9
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
GWL_STYLE = -16
GWL_EXSTYLE = -20


def corrected_outer_size(
    outer_width: int,
    outer_height: int,
    *,
    actual_client_width: int,
    actual_client_height: int,
    expected_client_width: int,
    expected_client_height: int,
) -> tuple[int, int]:
    return (
        outer_width + expected_client_width - actual_client_width,
        outer_height + expected_client_height - actual_client_height,
    )


def restore_client_size(
    title_contains: str,
    *,
    expected_width: int,
    expected_height: int,
    sleep_function=time.sleep,
) -> WindowBounds:
    if sys.platform != "win32":
        raise CaptureError("window profile restore is only supported on Windows")
    user32 = ctypes.windll.user32
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT
    user32.AdjustWindowRectExForDpi.argtypes = [
        ctypes.POINTER(wintypes.RECT),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
        wintypes.UINT,
    ]
    user32.AdjustWindowRectExForDpi.restype = wintypes.BOOL
    user32.AdjustWindowRectEx.argtypes = [
        ctypes.POINTER(wintypes.RECT),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    user32.AdjustWindowRectEx.restype = wintypes.BOOL
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    bounds = find_client_bounds(title_contains)
    user32.ShowWindow(bounds.handle, SW_RESTORE)
    sleep_function(1)
    bounds = find_client_bounds(title_contains)
    if (bounds.width, bounds.height) == (expected_width, expected_height):
        return bounds

    style = user32.GetWindowLongW(bounds.handle, GWL_STYLE)
    extended_style = user32.GetWindowLongW(bounds.handle, GWL_EXSTYLE)
    rect = wintypes.RECT(0, 0, expected_width, expected_height)
    adjusted = False
    try:
        dpi = user32.GetDpiForWindow(bounds.handle)
        adjusted = bool(
            user32.AdjustWindowRectExForDpi(
                ctypes.byref(rect),
                style,
                False,
                extended_style,
                dpi,
            )
        )
    except AttributeError:
        pass
    if not adjusted:
        adjusted = bool(
            user32.AdjustWindowRectEx(
                ctypes.byref(rect),
                style,
                False,
                extended_style,
            )
        )
    if not adjusted:
        raise CaptureError("could not calculate the Pirate Galaxy window frame")

    outer_width = rect.right - rect.left
    outer_height = rect.bottom - rect.top
    window_left = (user32.GetSystemMetrics(0) - outer_width) // 2
    if not user32.SetWindowPos(
        bounds.handle,
        0,
        window_left,
        0,
        outer_width,
        outer_height,
        SWP_NOZORDER | SWP_NOACTIVATE,
    ):
        raise CaptureError("SetWindowPos failed while restoring the game profile")
    sleep_function(1)

    restored = find_client_bounds(title_contains)
    if (restored.width, restored.height) == (expected_width, expected_height):
        return restored

    # Java/AWT can apply a client inset that differs from the Win32 style
    # estimate immediately after launch. Correct the measured residual once;
    # the final client-size check below remains authoritative.
    corrected_width, corrected_height = corrected_outer_size(
        outer_width,
        outer_height,
        actual_client_width=restored.width,
        actual_client_height=restored.height,
        expected_client_width=expected_width,
        expected_client_height=expected_height,
    )
    if corrected_width <= 0 or corrected_height <= 0:
        raise CaptureError("calculated an invalid corrective game window size")
    corrected_left = (user32.GetSystemMetrics(0) - corrected_width) // 2
    if not user32.SetWindowPos(
        restored.handle,
        0,
        corrected_left,
        0,
        corrected_width,
        corrected_height,
        SWP_NOZORDER | SWP_NOACTIVATE,
    ):
        raise CaptureError("SetWindowPos failed during measured game profile correction")
    sleep_function(1)
    restored = find_client_bounds(title_contains)
    if (restored.width, restored.height) != (expected_width, expected_height):
        raise CaptureError(
            "game client size is still "
            f"{restored.width}x{restored.height}; expected "
            f"{expected_width}x{expected_height}"
        )
    return restored
