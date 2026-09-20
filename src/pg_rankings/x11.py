from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

from PIL import Image, ImageGrab

from .capture import CaptureError, WindowBounds, _choose_window_match


class X11Error(RuntimeError):
    pass


def display_name() -> str:
    value = os.environ.get("PG_X11_DISPLAY") or os.environ.get("DISPLAY")
    if not value:
        raise X11Error("no X11 display is configured; set PG_X11_DISPLAY or DISPLAY")
    return value


def require_isolated_display() -> str:
    if os.environ.get("PG_X11_ISOLATED") != "1":
        raise X11Error("refusing background input outside the isolated Pirate Galaxy X11 session")
    return display_name()


def _dependencies() -> tuple[Any, Any, Any, Any]:
    try:
        from Xlib import X, display, error, protocol
        from Xlib.ext import xtest
    except ImportError as exc:
        raise X11Error(
            "the Linux X11 backend requires the 'linux' project extra (python-xlib)"
        ) from exc
    return X, display, error, (protocol, xtest)


def _decode_property(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").rstrip("\x00")
    if hasattr(value, "tobytes"):
        return value.tobytes().decode("utf-8", errors="replace").rstrip("\x00")
    return str(value or "")


def _window_title(window: Any, connection: Any) -> str:
    net_wm_name = connection.intern_atom("_NET_WM_NAME")
    utf8_string = connection.intern_atom("UTF8_STRING")
    try:
        prop = window.get_full_property(net_wm_name, utf8_string)
        if prop is not None:
            title = _decode_property(prop.value)
            if title:
                return title
    except Exception:
        pass
    try:
        return _decode_property(window.get_wm_name())
    except Exception:
        return ""


def _walk_windows(window: Any) -> Iterator[Any]:
    try:
        children = window.query_tree().children
    except Exception:
        return
    for child in children:
        yield child
        yield from _walk_windows(child)


def _find_window(connection: Any, title_contains: str) -> tuple[Any, str]:
    X, _display, _error, _extensions = _dependencies()
    root = connection.screen().root
    matches: list[tuple[int, str, bool, bool]] = []
    windows: dict[int, Any] = {}
    for window in _walk_windows(root):
        title = _window_title(window, connection)
        if not title or title_contains.casefold() not in title.casefold():
            continue
        try:
            visible = window.get_attributes().map_state == X.IsViewable
        except Exception:
            visible = False
        handle = int(window.id)
        windows[handle] = window
        matches.append((handle, title, visible, False))
    handle, title, _visible, _iconic = _choose_window_match(matches, title_contains)
    return windows[handle], title


def _window_origin(connection: Any, window: Any) -> tuple[int, int]:
    # python-xlib exposes TranslateCoordinates in the opposite-looking order:
    # calling it on the root with the child as destination returns the child's
    # origin in root coordinates. Calling it on the child produces the inverse
    # offset, which would shift screenshots and calibrated clicks off-screen.
    origin = connection.screen().root.translate_coords(window, 0, 0)
    return int(origin.x), int(origin.y)


def _client_insets() -> tuple[int, int, int, int]:
    raw = os.environ.get("PG_X11_CLIENT_INSETS", "0,0,0,0")
    try:
        values = tuple(int(part.strip()) for part in raw.split(","))
    except ValueError as exc:
        raise CaptureError("PG_X11_CLIENT_INSETS must contain four non-negative integers") from exc
    if len(values) != 4 or any(value < 0 for value in values):
        raise CaptureError("PG_X11_CLIENT_INSETS must contain four non-negative integers")
    return values


def find_client_bounds_x11(title_contains: str) -> WindowBounds:
    _X, display, error, _extensions = _dependencies()
    name = display_name()
    try:
        connection = display.Display(name)
    except error.DisplayConnectionError as exc:
        raise CaptureError(f"could not connect to X11 display {name!r}: {exc}") from exc
    try:
        window, title = _find_window(connection, title_contains)
        geometry = window.get_geometry()
        left, top = _window_origin(connection, window)
        window_left = left
        window_top = top
        left_inset, top_inset, right_inset, bottom_inset = _client_insets()
        left += left_inset
        top += top_inset
        width = int(geometry.width) - left_inset - right_inset
        height = int(geometry.height) - top_inset - bottom_inset
        if width < 640 or height < 480:
            raise CaptureError(f"game client area is unexpectedly small: {width}x{height}")
        return WindowBounds(
            title=title,
            left=left,
            top=top,
            width=width,
            height=height,
            handle=int(window.id),
            window_left=window_left,
            window_top=window_top,
        )
    finally:
        connection.close()


def capture_window_x11(title_contains: str) -> tuple[Image.Image, WindowBounds]:
    bounds = find_client_bounds_x11(title_contains)
    bbox = (
        bounds.left,
        bounds.top,
        bounds.left + bounds.width,
        bounds.top + bounds.height,
    )
    try:
        image = ImageGrab.grab(bbox=bbox, xdisplay=display_name())
    except (OSError, ValueError) as exc:
        raise CaptureError(f"X11 window capture failed: {exc}") from exc
    return image.convert("RGB"), bounds


class X11InputDriver:
    def __init__(self) -> None:
        X, display, error, extensions = _dependencies()
        self.X = X
        self.protocol, self.xtest = extensions
        self.display_name = require_isolated_display()
        try:
            self.connection = display.Display(self.display_name)
        except error.DisplayConnectionError as exc:
            raise X11Error(
                f"could not connect to isolated X11 display {self.display_name!r}: {exc}"
            ) from exc
        self.root = self.connection.screen().root
        self._target_handle = 0

    def _window(self, handle: int) -> Any:
        return self.connection.create_resource_object("window", handle)

    def activate(self, handle: int) -> None:
        window = self._window(handle)
        try:
            attributes = window.get_attributes()
            if attributes.map_state != self.X.IsViewable:
                window.map()
            window.configure(stack_mode=self.X.Above)
            window.set_input_focus(self.X.RevertToParent, self.X.CurrentTime)

            active_window = self.connection.intern_atom("_NET_ACTIVE_WINDOW")
            event = self.protocol.event.ClientMessage(
                window=window,
                client_type=active_window,
                data=(32, [1, self.X.CurrentTime, 0, 0, 0]),
            )
            self.root.send_event(
                event,
                event_mask=self.X.SubstructureRedirectMask | self.X.SubstructureNotifyMask,
            )
            self.connection.sync()
        except Exception as exc:
            raise X11Error(f"could not activate X11 game window 0x{handle:x}: {exc}") from exc
        self._target_handle = handle

    def _deepest_window_at_pointer(self) -> Any:
        window = self.root
        while True:
            pointer = window.query_pointer()
            child = pointer.child
            if not child:
                return window
            window = child

    def _is_target_or_descendant(self, window: Any) -> bool:
        while window:
            if int(window.id) == self._target_handle:
                return True
            if int(window.id) == int(self.root.id):
                return False
            try:
                window = window.query_tree().parent
            except Exception:
                return False
        return False

    def click(self, point: Any) -> None:
        if not self._target_handle:
            raise X11Error("click attempted before activating Pirate Galaxy")
        try:
            self.xtest.fake_input(
                self.connection,
                self.X.MotionNotify,
                x=int(point.x),
                y=int(point.y),
            )
            self.connection.sync()
            window_at_pointer = self._deepest_window_at_pointer()
            if not self._is_target_or_descendant(window_at_pointer):
                raise X11Error(
                    "refusing calibrated click because Pirate Galaxy is not at the target point "
                    f"(target=0x{self._target_handle:x}, "
                    f"point_window=0x{int(window_at_pointer.id):x})"
                )
            self.xtest.fake_input(self.connection, self.X.ButtonPress, 1)
            self.xtest.fake_input(self.connection, self.X.ButtonRelease, 1)
            self.connection.sync()
        except X11Error:
            raise
        except Exception as exc:
            raise X11Error(f"could not send isolated X11 click: {exc}") from exc
