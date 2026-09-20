from __future__ import annotations

import sys

from PIL import Image

import pg_rankings.x11 as x11
from pg_rankings.capture import WindowBounds, capture_window, set_window_process_throttled


def test_display_name_prefers_explicit_tracker_display(monkeypatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("PG_X11_DISPLAY", ":97")

    assert x11.display_name() == ":97"


def test_input_requires_an_explicit_isolated_session(monkeypatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("PG_X11_ISOLATED", raising=False)

    try:
        x11.require_isolated_display()
    except x11.X11Error as error:
        assert "refusing background input" in str(error)
    else:
        raise AssertionError("host-desktop input must be rejected")


def test_linux_capture_dispatches_to_x11(monkeypatch) -> None:
    expected = (
        Image.new("RGB", (800, 600)),
        WindowBounds("Pirate Galaxy", 0, 0, 800, 600, handle=42),
    )
    monkeypatch.setattr(x11, "capture_window_x11", lambda _title: expected)
    monkeypatch.setattr(sys, "platform", "linux")

    assert capture_window("Pirate Galaxy") == expected


def test_linux_priority_throttle_is_a_safe_noop(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    assert set_window_process_throttled(42, True) is None
    assert set_window_process_throttled(42, False) is None


def test_window_origin_translates_root_to_child() -> None:
    class Coordinates:
        x = 29
        y = 71

    class Root:
        def translate_coords(self, destination, x, y):
            assert destination == "game-window"
            assert (x, y) == (0, 0)
            return Coordinates()

    class Screen:
        root = Root()

    class Connection:
        def screen(self):
            return Screen()

    assert x11._window_origin(Connection(), "game-window") == (29, 71)


def test_client_insets_parse_wine_virtual_desktop_frame(monkeypatch) -> None:
    monkeypatch.setenv("PG_X11_CLIENT_INSETS", "3,22,3,3")

    assert x11._client_insets() == (3, 22, 3, 3)
