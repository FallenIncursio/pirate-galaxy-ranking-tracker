from PIL import Image, ImageDraw

from pg_rankings.capture import WindowBounds
from pg_rankings.config import RefreshConfig
from pg_rankings.ui_refresh import (
    RefreshError,
    ScreenPoint,
    Win32InputDriver,
    client_point,
    division_view_visible,
    gameplay_frame_visible,
    gameplay_toolbar_visible,
    rankings_window_visible,
    refresh_rankings,
)


class FakeDriver:
    def __init__(self) -> None:
        self.activated: list[int] = []
        self.clicked: list[ScreenPoint] = []
        self.background_clicked: list[ScreenPoint] = []

    def activate(self, handle: int) -> None:
        self.activated.append(handle)

    def click(self, point: ScreenPoint) -> None:
        self.clicked.append(point)

    def click_background(self, point: ScreenPoint) -> None:
        self.background_clicked.append(point)


class FakeBackgroundUser32:
    def __init__(self) -> None:
        self.messages: list[tuple[int, int, int, int]] = []
        self.mouse_events: list[tuple[int, int, int, int, int]] = []

    def SetCursorPos(self, _x: int, _y: int) -> bool:
        return False

    def WindowFromPoint(self, _point: object) -> int:
        return 99

    def GetAncestor(self, handle: int, _flags: int) -> int:
        return 42 if handle == 99 else handle

    def ScreenToClient(self, handle: int, point: object) -> bool:
        assert handle == 99
        point._obj.x = 10  # type: ignore[attr-defined]
        point._obj.y = 20  # type: ignore[attr-defined]
        return True

    def PostMessageW(self, handle: int, message: int, buttons: int, point: int) -> bool:
        self.messages.append((handle, message, buttons, point))
        return True

    def mouse_event(self, *event: int) -> None:
        self.mouse_events.append(event)


class FakeDpiVirtualizedUser32(FakeBackgroundUser32):
    def ClientToScreen(self, handle: int, point: object) -> bool:
        assert handle == 99
        point._obj.x = 100  # type: ignore[attr-defined]
        point._obj.y = 100  # type: ignore[attr-defined]
        return True

    def PhysicalToLogicalPointForPerMonitorDPI(self, handle: int, point: object) -> bool:
        assert handle == 42
        point._obj.x = round(point._obj.x / 1.25)  # type: ignore[attr-defined]
        point._obj.y = round(point._obj.y / 1.25)  # type: ignore[attr-defined]
        return True


def test_win32_click_posts_to_verified_game_window_when_cursor_is_unavailable() -> None:
    driver = object.__new__(Win32InputDriver)
    driver.user32 = FakeBackgroundUser32()
    driver._target_handle = 42
    driver._topmost_fallback = False
    driver._activation_diagnostics = ""

    driver.click(ScreenPoint(110, 220))

    packed_point = 10 | (20 << 16)
    assert driver.user32.messages == [
        (99, 0x0200, 0, packed_point),
        (99, 0x0201, 0x0001, packed_point),
        (99, 0x0202, 0, packed_point),
    ]
    assert driver.user32.mouse_events == []


def test_win32_background_click_converts_physical_point_for_dpi_unaware_game() -> None:
    driver = object.__new__(Win32InputDriver)
    driver.user32 = FakeDpiVirtualizedUser32()
    driver._target_handle = 42
    driver._topmost_fallback = False
    driver._activation_diagnostics = ""

    driver.click_background(ScreenPoint(530, 220))

    packed_point = 344 | (96 << 16)
    assert driver.user32.messages == [
        (99, 0x0200, 0, packed_point),
        (99, 0x0201, 0x0001, packed_point),
        (99, 0x0202, 0, packed_point),
    ]


def profile() -> RefreshConfig:
    return RefreshConfig(
        expected_width=960,
        expected_height=600,
        menu_icon_x=0.458,
        menu_icon_y=0.023,
        rankings_tab_x=0.285,
        rankings_tab_y=0.125,
        menu_wait_seconds=3.0,
        content_wait_seconds=8.0,
    )


def bounds(width: int = 960) -> WindowBounds:
    return WindowBounds(
        "PirateGalaxy Version: 1002128",
        left=-74,
        top=25,
        width=width,
        height=600,
        handle=123,
    )


def screenshot(
    *,
    division_visible: bool,
    width: int = 960,
    gray_marker: bool = False,
) -> Image.Image:
    image = Image.new("RGB", (width, 600), "black")
    center_x = round(profile().menu_icon_x * width)
    center_y = round(profile().menu_icon_y * 600)
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (center_x - 17, max(0, center_y - 14), center_x + 17, center_y + 14),
        fill=(24, 80, 130),
    )
    draw.rectangle((center_x - 5, center_y - 5, center_x + 5, center_y + 5), fill="white")
    if division_visible and width == 960:
        draw.rectangle((58, 34, 889, 55), fill=(42, 42, 42))
        fill = (42, 42, 42) if gray_marker else (255, 145, 0)
        draw.rectangle((155, 129, 787, 146), fill=fill)
        draw_division_identity(image)
    return image


def draw_division_identity(image: Image.Image) -> None:
    width, height = image.size
    draw = ImageDraw.Draw(image)
    if image.size == (800, 600):
        draw.rectangle((150, 54, 259, 76), fill=(108, 133, 164))
        draw.rectangle((705, 161, 724, 505), fill=(35, 95, 150))
        return
    draw.rectangle(
        (
            round(width * 0.239),
            round(height * 0.090),
            round(width * 0.351) - 1,
            round(height * 0.128) - 1,
        ),
        fill=(108, 133, 164),
    )
    draw.rectangle(
        (
            round(width * 0.819),
            round(height * 0.30),
            round(width * 0.84),
            round(height * 0.82),
        ),
        fill=(35, 95, 150),
    )


def hall_of_fame_screenshot() -> Image.Image:
    image = screenshot(division_visible=False)
    draw = ImageDraw.Draw(image)
    draw.rectangle((58, 34, 889, 55), fill=(42, 42, 42))
    draw.rectangle((155, 129, 787, 146), fill=(52, 52, 52))
    draw.rectangle((round(960 * 0.14), 54, round(960 * 0.238), 76), fill=(108, 133, 164))
    draw.rectangle((round(960 * 0.239), 54, round(960 * 0.351), 76), fill=(54, 70, 90))
    draw.rectangle((786, 180, 804, 492), fill=(150, 135, 37))
    return image


def captures(states: list[bool], *, width: int = 960):
    iterator = iter(states)

    def capture(_title: str) -> tuple[Image.Image, WindowBounds]:
        return screenshot(division_visible=next(iterator), width=width), bounds(width=width)

    return capture


def capture_images(images: list[Image.Image]):
    iterator = iter(images)

    def capture(_title: str) -> tuple[Image.Image, WindowBounds]:
        image = next(iterator)
        return image, bounds(width=image.width)

    return capture


def test_client_point_includes_offscreen_window_origin() -> None:
    assert client_point(bounds(), 0.458, 0.023) == ScreenPoint(366, 39)


def test_division_view_accepts_compact_800_english_layout() -> None:
    image = Image.new("RGB", (800, 600), "black")
    draw = ImageDraw.Draw(image)
    draw.rectangle((70, 115, 729, 132), fill=(52, 52, 52))
    draw.rectangle((150, 54, 259, 76), fill=(108, 133, 164))
    draw.rectangle((705, 161, 724, 505), fill=(35, 95, 150))

    assert division_view_visible(image)


def test_refresh_activates_and_uses_only_the_calibrated_points() -> None:
    driver = FakeDriver()
    sleeps: list[float] = []

    result = refresh_rankings(
        "PirateGalaxy",
        profile(),
        capture_function=capture_images(
            [
                screenshot(division_visible=False),
                hall_of_fame_screenshot(),
                screenshot(division_visible=True),
            ]
        ),
        driver=driver,
        sleep_function=sleeps.append,
    )

    assert result.handle == 123
    assert driver.activated == [123, 123]
    assert driver.clicked == [
        ScreenPoint(366, 39),
        ScreenPoint(200, 100),
    ]
    assert driver.background_clicked == []
    assert sleeps == [3.0, 8.0]


def test_refresh_accepts_the_stable_gameplay_frame_when_toolbar_icon_moves() -> None:
    frame = Image.new("RGB", (960, 600), "black")
    draw = ImageDraw.Draw(frame)
    draw.rectangle((926, 1, 959, 39), fill=(20, 70, 120))
    draw.rectangle((934, 6, 950, 30), fill=(40, 150, 20))
    driver = FakeDriver()

    result = refresh_rankings(
        "PirateGalaxy",
        profile(),
        capture_function=capture_images(
            [frame, hall_of_fame_screenshot(), screenshot(division_visible=True)]
        ),
        driver=driver,
        sleep_function=lambda _seconds: None,
    )

    assert result.handle == 123
    assert driver.clicked == [ScreenPoint(366, 39), ScreenPoint(200, 100)]
    assert driver.background_clicked == []


def test_refresh_closes_an_existing_division_view_before_reopening() -> None:
    driver = FakeDriver()
    sleeps: list[float] = []

    refresh_rankings(
        "PirateGalaxy",
        profile(),
        capture_function=capture_images(
            [
                screenshot(division_visible=True),
                screenshot(division_visible=False),
                hall_of_fame_screenshot(),
                screenshot(division_visible=True),
            ]
        ),
        driver=driver,
        sleep_function=sleeps.append,
    )

    assert driver.clicked == [
        ScreenPoint(366, 39),
        ScreenPoint(366, 39),
        ScreenPoint(200, 100),
    ]
    assert driver.background_clicked == []
    assert sleeps == [3.0, 3.0, 8.0]


def test_refresh_retries_close_when_the_focus_click_is_consumed() -> None:
    driver = FakeDriver()

    result = refresh_rankings(
        "PirateGalaxy",
        profile(),
        capture_function=capture_images(
            [
                screenshot(division_visible=True),
                screenshot(division_visible=True),
                screenshot(division_visible=False),
                hall_of_fame_screenshot(),
                screenshot(division_visible=True),
            ]
        ),
        driver=driver,
        sleep_function=lambda _seconds: None,
    )

    assert result.handle == 123
    assert driver.clicked == [
        ScreenPoint(366, 39),
        ScreenPoint(366, 39),
        ScreenPoint(366, 39),
        ScreenPoint(200, 100),
    ]


def test_refresh_retries_the_bounded_sequence_once() -> None:
    driver = FakeDriver()
    sleeps: list[float] = []

    result = refresh_rankings(
        "PirateGalaxy",
        profile(),
        capture_function=capture_images(
            [
                screenshot(division_visible=False),
                hall_of_fame_screenshot(),
                screenshot(division_visible=False),
                hall_of_fame_screenshot(),
                screenshot(division_visible=True),
            ]
        ),
        driver=driver,
        sleep_function=sleeps.append,
    )

    assert result.handle == 123
    assert driver.activated == [123, 123, 123, 123]
    assert driver.clicked == [
        ScreenPoint(366, 39),
        ScreenPoint(200, 100),
        ScreenPoint(366, 39),
        ScreenPoint(200, 100),
    ]
    assert driver.background_clicked == []
    assert sleeps == [3.0, 8.0, 3.0, 8.0]


def test_refresh_reselects_ladder_only_after_hall_of_fame_finished_loading() -> None:
    driver = FakeDriver()
    sleeps: list[float] = []
    result = refresh_rankings(
        "PirateGalaxy",
        profile(),
        capture_function=capture_images(
            [
                hall_of_fame_screenshot(),
                screenshot(division_visible=False),
                hall_of_fame_screenshot(),
                hall_of_fame_screenshot(),
                screenshot(division_visible=True),
            ]
        ),
        driver=driver,
        sleep_function=sleeps.append,
    )

    assert result.handle == 123
    assert driver.activated == [123, 123, 123, 123]
    assert driver.clicked == [
        ScreenPoint(366, 39),
        ScreenPoint(366, 39),
        ScreenPoint(200, 100),
        ScreenPoint(200, 100),
    ]
    assert driver.background_clicked == []
    assert sleeps == [3.0, 3.0, 8.0, 8.0]


def test_refresh_still_rejects_a_missing_division_after_retry() -> None:
    driver = FakeDriver()

    try:
        refresh_rankings(
            "PirateGalaxy",
            profile(),
            capture_function=capture_images(
                [
                    screenshot(division_visible=False),
                    hall_of_fame_screenshot(),
                    screenshot(division_visible=False),
                    hall_of_fame_screenshot(),
                    screenshot(division_visible=False),
                ]
            ),
            driver=driver,
            sleep_function=lambda _seconds: None,
        )
    except RefreshError as error:
        assert "after the bounded refresh retry" in str(error)
    else:
        raise AssertionError("a failed bounded retry must not be accepted")


def test_refresh_refuses_tab_click_when_rankings_window_did_not_open() -> None:
    driver = FakeDriver()

    try:
        refresh_rankings(
            "PirateGalaxy",
            profile(),
            capture_function=capture_images(
                [
                    screenshot(division_visible=False),
                    screenshot(division_visible=False),
                    screenshot(division_visible=False),
                ]
            ),
            driver=driver,
            sleep_function=lambda _seconds: None,
        )
    except RefreshError as error:
        assert "refusing the rankings-tab click" in str(error)
    else:
        raise AssertionError("a missing rankings window must stop before the tab click")

    assert driver.clicked == [ScreenPoint(366, 39), ScreenPoint(366, 39)]
    assert driver.background_clicked == []


def test_refresh_retries_menu_icon_when_the_focus_click_is_consumed() -> None:
    driver = FakeDriver()
    sleeps: list[float] = []

    result = refresh_rankings(
        "PirateGalaxy",
        profile(),
        capture_function=capture_images(
            [
                screenshot(division_visible=False),
                screenshot(division_visible=False),
                hall_of_fame_screenshot(),
                screenshot(division_visible=True),
            ]
        ),
        driver=driver,
        sleep_function=sleeps.append,
    )

    assert result.handle == 123
    assert driver.clicked == [
        ScreenPoint(366, 39),
        ScreenPoint(366, 39),
        ScreenPoint(200, 100),
    ]
    assert sleeps == [3.0, 3.0, 8.0]


def test_refresh_rejects_a_different_client_size_before_input() -> None:
    driver = FakeDriver()

    try:
        refresh_rankings(
            "PirateGalaxy",
            profile(),
            capture_function=captures([False], width=800),
            driver=driver,
            sleep_function=lambda _seconds: None,
        )
    except RefreshError as error:
        assert "does not match" in str(error)
    else:
        raise AssertionError("mismatched resolution should have been rejected")

    assert driver.activated == []
    assert driver.clicked == []


def test_division_marker_accepts_active_and_reset_countdown_colors() -> None:
    assert division_view_visible(screenshot(division_visible=True))
    assert division_view_visible(screenshot(division_visible=True, gray_marker=True))
    assert not division_view_visible(screenshot(division_visible=False))


def test_division_marker_accepts_partially_filled_weekly_countdown() -> None:
    image = Image.new("RGB", (800, 600), "black")
    draw = ImageDraw.Draw(image)
    draw.rectangle((70, 115, 729, 132), fill=(52, 52, 52))
    draw.rectangle((70, 115, 253, 132), fill=(255, 145, 0))
    draw_division_identity(image)

    assert division_view_visible(image)


def test_division_marker_accepts_800px_window_profile() -> None:
    image = Image.new("RGB", (800, 600), "black")
    ImageDraw.Draw(image).rectangle((70, 115, 729, 132), fill=(255, 145, 0))
    draw_division_identity(image)

    assert division_view_visible(image)


def test_division_marker_accepts_post_update_window_profile() -> None:
    image = Image.new("RGB", (954, 610), "black")
    ImageDraw.Draw(image).rectangle((146, 109, 806, 126), fill=(255, 145, 0))
    draw_division_identity(image)

    assert division_view_visible(image)


def test_hall_of_fame_reset_view_is_not_accepted_as_division() -> None:
    image = hall_of_fame_screenshot()

    assert rankings_window_visible(image)
    assert not division_view_visible(image)


def test_rankings_window_marker_is_independent_of_division_countdown() -> None:
    image = Image.new("RGB", (800, 600), "black")
    ImageDraw.Draw(image).rectangle((48, 34, 741, 55), fill=(42, 42, 42))

    assert rankings_window_visible(image)
    assert not division_view_visible(image)
    assert not rankings_window_visible(Image.new("RGB", (800, 600), "black"))


def test_rankings_window_marker_accepts_post_update_window_profile() -> None:
    image = Image.new("RGB", (954, 610), "black")
    ImageDraw.Draw(image).rectangle((126, 20, 828, 42), fill=(42, 42, 42))

    assert rankings_window_visible(image)


def test_gameplay_frame_accepts_the_green_wrench_and_blue_frame() -> None:
    image = Image.new("RGB", (800, 600), "black")
    draw = ImageDraw.Draw(image)
    draw.rectangle((772, 1, 799, 39), fill=(20, 70, 120))
    draw.rectangle((778, 6, 792, 30), fill=(40, 150, 20))

    assert gameplay_frame_visible(image)


def test_gameplay_frame_rejects_login_and_loading_screens() -> None:
    image = Image.new("RGB", (800, 600), "black")
    ImageDraw.Draw(image).ellipse((772, 1, 799, 39), fill=(80, 100, 105))

    assert not gameplay_frame_visible(image)


def test_toolbar_marker_rejects_unknown_screen_before_clicking() -> None:
    driver = FakeDriver()

    try:
        refresh_rankings(
            "PirateGalaxy",
            profile(),
            capture_function=lambda _title: (Image.new("RGB", (960, 600), "black"), bounds()),
            driver=driver,
            sleep_function=lambda _seconds: None,
        )
    except RefreshError as error:
        assert "unknown screen" in str(error)
    else:
        raise AssertionError("unknown game screens must not receive clicks")

    assert driver.activated == []
    assert driver.clicked == []


def test_gameplay_toolbar_marker_accepts_calibrated_icon_region() -> None:
    assert gameplay_toolbar_visible(screenshot(division_visible=False), profile())
    assert not gameplay_toolbar_visible(Image.new("RGB", (960, 600), "black"), profile())
