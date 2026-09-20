from types import SimpleNamespace

from PIL import Image, ImageDraw

from pg_rankings import windows_recovery as recovery_module
from pg_rankings.capture import CaptureError, WindowBounds
from pg_rankings.recovery import RecoveryStatus, RuntimeObservation
from pg_rankings.windows_recovery import WindowsGameRecovery, find_template_center


def test_template_match_returns_center_for_an_exact_unique_button() -> None:
    image = Image.new("RGB", (200, 120), "black")
    draw = ImageDraw.Draw(image)
    draw.rectangle((70, 40, 129, 69), fill=(20, 80, 140))
    draw.line((75, 55, 124, 55), fill="white", width=3)
    template = image.crop((70, 40, 130, 70))

    center, score = find_template_center(image, template, threshold=0.9)

    assert center is not None
    assert (center.x, center.y) == (100, 55)
    assert score >= 0.99


def test_template_match_rejects_a_different_screen() -> None:
    image = Image.new("RGB", (200, 120), "black")
    template = Image.new("RGB", (60, 30), "black")
    draw = ImageDraw.Draw(template)
    draw.rectangle((2, 2, 57, 27), outline="white", width=3)
    draw.line((5, 15, 54, 15), fill=(20, 80, 140), width=3)

    center, score = find_template_center(image, template, threshold=0.9)

    assert center is None
    assert score < 0.9


def maintenance_driver(tmp_path) -> WindowsGameRecovery:
    driver = object.__new__(WindowsGameRecovery)
    driver.config = SimpleNamespace(
        window_title_contains="PirateGalaxy",
        recovery=SimpleNamespace(
            maintenance_backoff_seconds=(900, 1800, 3600),
            launch_timeout_seconds=600,
            probe_interval_seconds=5,
            template_threshold=0.88,
        ),
        refresh=SimpleNamespace(expected_width=800, expected_height=600),
        paths=SimpleNamespace(diagnostics=tmp_path),
    )
    driver.sleep_function = lambda _seconds: None
    driver.monotonic = lambda: 0.0
    driver.templates = tmp_path
    driver.last_action_at = {}
    driver.last_action_centers = {}
    driver.last_status_ocr_at = None
    driver.last_status_text = ""
    driver.last_status_signature = None
    return driver


class RecoveryInputDriver:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def activate(self, handle: int) -> None:
        self.events.append(("activate", handle))

    def click(self, point: object) -> None:
        self.events.append(("click", point))

    def click_background(self, point: object) -> None:
        self.events.append(("background", point))


def test_launcher_detection_includes_the_java_client_before_its_window_exists(
    tmp_path,
    monkeypatch,
) -> None:
    driver = maintenance_driver(tmp_path)
    results = iter(
        (
            SimpleNamespace(returncode=0, stdout="INFO: No tasks are running"),
            SimpleNamespace(returncode=0, stdout="7820\n"),
        )
    )
    monkeypatch.setattr(recovery_module.subprocess, "run", lambda *_args, **_kwargs: next(results))

    assert driver._launcher_running()


def test_known_screen_retries_hover_degraded_template_via_window_message(
    tmp_path, monkeypatch
) -> None:
    driver = maintenance_driver(tmp_path)
    driver.config.recovery.action_cooldown_seconds = 10.0
    driver.input_driver = RecoveryInputDriver()
    now = [100.0]
    driver.monotonic = lambda: now[0]
    Image.new("RGB", (20, 10), "white").save(tmp_path / "login-button.png")
    strict_calls = [0]

    def template_match(_image, _template, *, threshold):
        if threshold == 0.88:
            strict_calls[0] += 1
            if strict_calls[0] == 1:
                return recovery_module.ScreenPoint(40, 30), 0.99
            return None, 0.65
        return recovery_module.ScreenPoint(40, 30), 0.65

    monkeypatch.setattr(recovery_module, "find_template_center", template_match)
    image = Image.new("RGB", (800, 600), "black")
    bounds = WindowBounds("PirateGalaxy", 100, 200, 800, 600, handle=123)

    assert "clicked verified login" in driver._advance_known_screen(image, bounds)
    now[0] = 111.0
    assert "retried verified login" in driver._advance_known_screen(image, bounds)
    assert driver.input_driver.events == [
        ("activate", 123),
        ("click", recovery_module.ScreenPoint(140, 230)),
        ("activate", 123),
        ("background", recovery_module.ScreenPoint(140, 230)),
    ]


def test_updated_login_template_is_recognized_without_removing_the_legacy_variant(
    tmp_path, monkeypatch
) -> None:
    driver = maintenance_driver(tmp_path)
    driver.config.recovery.action_cooldown_seconds = 10.0
    driver.input_driver = RecoveryInputDriver()
    driver.monotonic = lambda: 100.0
    legacy = Image.new("RGB", (138, 32), "black")
    ImageDraw.Draw(legacy).rectangle((0, 0, 137, 31), outline="white")
    legacy.save(tmp_path / "login-button.png")
    updated = Image.new("RGB", (240, 36), "black")
    ImageDraw.Draw(updated).rectangle((4, 4, 235, 31), outline="white", width=2)
    screen = Image.new("RGB", (800, 600), "black")
    screen.paste(updated, (280, 477))
    updated.save(tmp_path / "login-button-v1002130.png")
    bounds = WindowBounds("PirateGalaxy", 0, 0, 800, 600, handle=123)

    action = driver._advance_known_screen(screen, bounds)

    assert action is not None and "clicked verified login" in action
    assert driver.input_driver.events == [
        ("activate", 123),
        ("click", recovery_module.ScreenPoint(400, 495)),
    ]


def test_blocking_action_filter_closes_only_the_verified_support_dialog(
    tmp_path, monkeypatch
) -> None:
    driver = maintenance_driver(tmp_path)
    driver.config.recovery.action_cooldown_seconds = 10.0
    driver.input_driver = RecoveryInputDriver()
    driver.monotonic = lambda: 100.0
    support = Image.new("RGB", (23, 26), "black")
    ImageDraw.Draw(support).line((3, 3, 19, 22), fill="white", width=2)
    ImageDraw.Draw(support).line((19, 3, 3, 22), fill="white", width=2)
    screen = Image.new("RGB", (800, 600), "black")
    screen.paste(support, (517, 30))
    support.save(tmp_path / "support-energy-close-v1002130.png")
    login = Image.new("RGB", (240, 36), "black")
    ImageDraw.Draw(login).rectangle((0, 0, 239, 35), outline="white")
    login.save(tmp_path / "login-button-v1002130.png")
    bounds = WindowBounds("PirateGalaxy", 0, 0, 800, 600, handle=123)

    action = driver._advance_known_screen(
        screen,
        bounds,
        allowed_actions=frozenset({"dismiss-support-energy"}),
    )

    assert action is not None and "clicked verified dismiss-support-energy" in action
    assert driver.input_driver.events == [
        ("activate", 123),
        ("click", recovery_module.ScreenPoint(528, 43)),
    ]


def test_blocking_dialogs_close_the_foreground_support_offer_before_the_event(
    tmp_path, monkeypatch
) -> None:
    driver = maintenance_driver(tmp_path)
    driver.config.recovery.action_cooldown_seconds = 10.0
    driver.input_driver = RecoveryInputDriver()
    driver.monotonic = lambda: 100.0
    raven_path = tmp_path / "raven-dynamics-event-title-v1002130.png"
    support_path = tmp_path / "support-energy-close-default-v1002130.png"
    foreground = Image.new("RGB", (800, 600), "black")
    event_modal = Image.new("RGB", (800, 600), "navy")
    gameplay = Image.new("RGB", (800, 600), "green")
    raven = Image.new("RGB", (180, 24), "black")
    ImageDraw.Draw(raven).rectangle((2, 2, 177, 21), outline="yellow", width=2)
    foreground.paste(raven, (155, 322))
    event_modal.paste(raven, (155, 322))
    raven.save(raven_path)
    support = Image.new("RGB", (23, 26), "black")
    ImageDraw.Draw(support).line((3, 3, 19, 22), fill="white", width=2)
    ImageDraw.Draw(support).line((19, 3, 3, 22), fill="white", width=2)
    foreground.paste(support, (517, 30))
    support.save(support_path)
    bounds = WindowBounds("PirateGalaxy", 0, 0, 800, 600, handle=123)
    captures = iter(
        (
            (event_modal, bounds),
            (gameplay, bounds),
            *((gameplay, bounds),) * 6,
        )
    )
    monkeypatch.setattr(recovery_module, "capture_window", lambda _title: next(captures))
    driver._ready = lambda image: image is gameplay
    events: list[str] = []

    result = driver._dismiss_blocking_dialogs(foreground, bounds, events.append)

    assert result == (gameplay, bounds)
    assert events == [
        "clicked verified dismiss-support-energy template at confidence 1.000",
        "clicked verified dismiss-raven-event template at confidence 1.000",
        "waiting for dialog-free stable gameplay after verified blocking dialog(s)",
    ]
    assert driver.input_driver.events == [
        ("activate", 123),
        ("click", recovery_module.ScreenPoint(528, 43)),
        ("activate", 123),
        ("click", recovery_module.ScreenPoint(514, 73)),
    ]


def test_dialog_settle_closes_a_dialog_that_returns_after_loading(tmp_path, monkeypatch) -> None:
    driver = maintenance_driver(tmp_path)
    driver.config.recovery.action_cooldown_seconds = 0.0
    driver.input_driver = RecoveryInputDriver()
    driver.monotonic = lambda: 100.0
    bounds = WindowBounds("PirateGalaxy", 0, 0, 800, 600, handle=123)
    gameplay = Image.new("RGB", (800, 600), "green")
    loading = Image.new("RGB", (800, 600), "black")
    support = Image.new("RGB", (800, 600), "navy")
    support_path = tmp_path / "support-energy-close-default-v1002130.png"
    template = Image.new("RGB", (23, 26), "black")
    ImageDraw.Draw(template).line((3, 3, 19, 22), fill="white", width=2)
    ImageDraw.Draw(template).line((19, 3, 3, 22), fill="white", width=2)
    support.paste(template, (517, 30))
    template.save(support_path)
    captures = iter(
        (
            (loading, bounds),
            (support, bounds),
            (gameplay, bounds),
            *((gameplay, bounds),) * 6,
        )
    )
    driver._ready = lambda image: image is gameplay
    monkeypatch.setattr(recovery_module, "capture_window", lambda _title: next(captures))
    events: list[str] = []

    result = driver._dismiss_blocking_dialogs(support, bounds, events.append)

    assert result == (gameplay, bounds)
    assert events == [
        "clicked verified dismiss-support-energy template at confidence 1.000",
        "waiting for dialog-free stable gameplay after verified blocking dialog(s)",
        "clicked verified dismiss-support-energy template at confidence 1.000",
    ]
    assert driver.input_driver.events == [
        ("activate", 123),
        ("click", recovery_module.ScreenPoint(528, 43)),
        ("activate", 123),
        ("click", recovery_module.ScreenPoint(528, 43)),
    ]


def test_blocking_dialog_never_uses_a_degraded_repeat_match(tmp_path, monkeypatch) -> None:
    driver = maintenance_driver(tmp_path)
    driver.input_driver = RecoveryInputDriver()
    driver.monotonic = lambda: 100.0
    driver.last_action_centers["dismiss-support-energy"] = recovery_module.ScreenPoint(528, 43)
    Image.new("RGB", (23, 26), "white").save(tmp_path / "support-energy-close-v1002130.png")
    thresholds: list[float] = []

    def template_match(_image, _template, *, threshold):
        thresholds.append(threshold)
        return None, 0.703

    monkeypatch.setattr(recovery_module, "find_template_center", template_match)
    bounds = WindowBounds("PirateGalaxy", 0, 0, 800, 600, handle=123)

    action = driver._advance_known_screen(
        Image.new("RGB", (800, 600), "black"),
        bounds,
        allowed_actions=frozenset({"dismiss-support-energy"}),
        allow_degraded=False,
    )

    assert action is None
    assert thresholds == [0.88]
    assert driver.input_driver.events == []


def test_recovery_dismisses_verified_blocking_dialog_before_rankings_verification(
    tmp_path, monkeypatch
) -> None:
    driver = maintenance_driver(tmp_path)
    bounds = WindowBounds("PirateGalaxy", 0, 0, 800, 600, handle=123)
    blocked = Image.new("RGB", (800, 600), "black")
    gameplay = Image.new("RGB", (800, 600), "navy")
    division = Image.new("RGB", (800, 600), "white")
    events: list[str] = []
    driver.observe = lambda: RuntimeObservation(True, True, False, "blocked gameplay")
    driver._restore_division_rankings = lambda image, current_bounds, _progress: (
        image,
        current_bounds,
    )
    driver._dismiss_blocking_dialogs = lambda _image, _bounds, progress: (
        progress("clicked verified dismiss-support-energy template at confidence 1.000")
        or (gameplay, bounds)
    )
    driver._ready = lambda image: image is gameplay
    driver._restore_ready_profile = lambda image, current_bounds, _progress: (
        image,
        current_bounds,
    )
    driver._verify_division_rankings = lambda *_args: (division, bounds)
    monkeypatch.setattr(recovery_module, "find_client_bounds", lambda _title: bounds)
    monkeypatch.setattr(recovery_module, "capture_window", lambda _title: (blocked, bounds))

    outcome = driver.recover(
        events.append,
        allow_restart=True,
        restart_maintenance=False,
    )

    assert outcome.status is RecoveryStatus.RECOVERED
    assert events == ["clicked verified dismiss-support-energy template at confidence 1.000"]


def test_repeated_login_return_is_treated_as_service_unavailable(tmp_path, monkeypatch) -> None:
    driver = maintenance_driver(tmp_path)
    bounds = WindowBounds("PirateGalaxy", 0, 0, 800, 600, handle=123)
    image = Image.new("RGB", (800, 600), "black")
    actions = iter(
        ["clicked verified login template at confidence 0.990"] * recovery_module.LOGIN_RETURN_LIMIT
    )
    driver.observe = lambda: RuntimeObservation(True, True, False, "login")
    driver._ready = lambda _image: False
    driver._restart_required_visible = lambda _image: False
    driver._maintenance_visible = lambda _image: False
    driver._advance_known_screen = lambda *_args, **kwargs: (
        None if kwargs.get("allowed_actions") else next(actions)
    )
    driver._window_responding = lambda _handle: True
    monkeypatch.setattr(recovery_module, "find_client_bounds", lambda _title: bounds)
    monkeypatch.setattr(recovery_module, "capture_window", lambda _title: (image, bounds))
    monkeypatch.setattr(recovery_module, "rankings_window_visible", lambda _image: False)

    outcome = driver.recover(
        lambda _detail: None,
        allow_restart=True,
        restart_maintenance=False,
    )

    assert outcome.status is RecoveryStatus.DEFERRED
    assert outcome.maintenance
    assert not outcome.maintenance_probe
    assert not outcome.restarted
    assert outcome.detail == ("login returned repeatedly; treating the game service as unavailable")


def test_due_login_return_maintenance_probe_restarts_before_retry(tmp_path, monkeypatch) -> None:
    driver = maintenance_driver(tmp_path)
    bounds = WindowBounds("PirateGalaxy", 0, 0, 800, 600, handle=123)
    login = Image.new("RGB", (800, 600), "black")
    maintenance = Image.new("RGB", (800, 600), "navy")
    observations = iter(
        (
            RuntimeObservation(True, True, False, "login"),
            RuntimeObservation(False, False, False, "closed"),
        )
    )
    captures = iter(((login, bounds), (maintenance, bounds)))
    events: list[str] = []
    driver.observe = lambda: next(observations)
    driver._ready = lambda _image: False
    driver._restart_required_visible = lambda _image: False
    driver._action_template_visible = lambda _image, action, **_kwargs: action == "login"
    driver._maintenance_visible = lambda image: image is maintenance
    driver._close_game = lambda _bounds: events.append("closed")
    driver._start_client_task = lambda: events.append("started")
    driver._window_responding = lambda _handle: True
    monkeypatch.setattr(recovery_module, "find_client_bounds", lambda _title: bounds)
    monkeypatch.setattr(recovery_module, "capture_window", lambda _title: next(captures))
    monkeypatch.setattr(recovery_module, "rankings_window_visible", lambda _image: False)

    outcome = driver.recover(
        events.append,
        allow_restart=True,
        restart_maintenance=True,
    )

    assert outcome.status is RecoveryStatus.DEFERRED
    assert outcome.maintenance
    assert outcome.maintenance_probe
    assert outcome.restarted
    assert events == [
        "restarting client for the login-return maintenance probe",
        "closed",
        "starting Pirate Galaxy client task",
        "started",
    ]


def test_first_maintenance_screen_waits_without_restarting(tmp_path, monkeypatch) -> None:
    driver = maintenance_driver(tmp_path)
    bounds = WindowBounds("PirateGalaxy", 0, 0, 800, 600, handle=123)
    image = Image.new("RGB", (800, 600), "black")
    driver.observe = lambda: RuntimeObservation(True, True, False, "maintenance")
    driver._ready = lambda _image: False
    driver._restart_required_visible = lambda _image: False
    driver._maintenance_visible = lambda _image: True
    driver._close_game = lambda _bounds: (_ for _ in ()).throw(
        AssertionError("first maintenance detection must not close the game")
    )
    monkeypatch.setattr(recovery_module, "find_client_bounds", lambda _title: bounds)
    monkeypatch.setattr(recovery_module, "capture_window", lambda _title: (image, bounds))

    outcome = driver.recover(
        lambda _detail: None,
        allow_restart=True,
        restart_maintenance=False,
    )

    assert outcome.status is RecoveryStatus.DEFERRED
    assert outcome.maintenance
    assert not outcome.restarted
    assert not outcome.maintenance_probe
    assert outcome.retry_after_seconds == 900


def test_maintenance_ocr_recognizes_the_german_status(tmp_path) -> None:
    driver = maintenance_driver(tmp_path)
    driver._screen_text = lambda _image: "server wegen wartung nicht verfugbar"

    assert driver._maintenance_visible(Image.new("RGB", (800, 600), "black"))


def test_restart_required_template_recognizes_the_exact_dialog(tmp_path) -> None:
    driver = maintenance_driver(tmp_path)
    image = Image.new("RGB", (800, 600), "black")
    draw = ImageDraw.Draw(image)
    draw.rectangle((205, 210, 594, 324), fill=(31, 31, 31))
    draw.rectangle((215, 220, 238, 245), fill=(210, 20, 20))
    draw.line((260, 295, 540, 295), fill="white", width=2)
    image.crop((205, 210, 595, 325)).save(tmp_path / "restart-required.png")

    assert driver._restart_required_visible(image)


def test_explicit_connection_failure_restarts_immediately_then_enters_backoff(
    tmp_path,
    monkeypatch,
) -> None:
    driver = maintenance_driver(tmp_path)
    bounds = WindowBounds("PirateGalaxy", 0, 0, 800, 600, handle=123)
    connection_failure = Image.new("RGB", (800, 600), "black")
    observations = iter(
        (
            RuntimeObservation(True, True, False, "connection failure"),
            RuntimeObservation(False, False, False, "closed"),
        )
    )
    events: list[str] = []
    driver.observe = lambda: next(observations)
    driver._ready = lambda _image: False
    driver._restart_required_visible = lambda _image: True
    driver._maintenance_visible = lambda _image: False
    driver._close_game = lambda _bounds: events.append("closed")
    driver._start_client_task = lambda: events.append("started")
    driver._window_responding = lambda _handle: True
    monkeypatch.setattr(recovery_module, "find_client_bounds", lambda _title: bounds)
    monkeypatch.setattr(
        recovery_module,
        "capture_window",
        lambda _title: (connection_failure, bounds),
    )

    outcome = driver.recover(
        events.append,
        allow_restart=True,
        restart_maintenance=False,
    )

    assert outcome.status is RecoveryStatus.DEFERRED
    assert outcome.detail == "connection failure remains after client restart"
    assert outcome.restarted
    assert outcome.maintenance
    assert not outcome.maintenance_probe
    assert events == [
        "restarting client after explicit connection failure",
        "closed",
        "starting Pirate Galaxy client task",
        "started",
    ]


def test_ready_game_defers_when_the_window_profile_cannot_be_restored(
    tmp_path,
    monkeypatch,
) -> None:
    driver = maintenance_driver(tmp_path)
    bounds = WindowBounds("PirateGalaxy", 0, 0, 954, 610, handle=123)
    image = Image.new("RGB", (954, 610), "black")
    driver.observe = lambda: RuntimeObservation(True, True, False, "ready")
    driver._ready = lambda _image: True
    driver._restore_ready_profile = lambda *_args: (_ for _ in ()).throw(
        CaptureError("fixed post-update size")
    )
    monkeypatch.setattr(recovery_module, "find_client_bounds", lambda _title: bounds)
    monkeypatch.setattr(recovery_module, "capture_window", lambda _title: (image, bounds))

    outcome = driver.recover(
        lambda _detail: None,
        allow_restart=True,
        restart_maintenance=False,
    )

    assert outcome.status is RecoveryStatus.DEFERRED
    assert "fixed post-update size" in outcome.detail
    assert outcome.retry_after_seconds == 300


def test_generic_rankings_window_is_not_ready_even_with_gameplay_frame(
    tmp_path,
    monkeypatch,
) -> None:
    driver = maintenance_driver(tmp_path)
    image = Image.new("RGB", (800, 600), "black")
    monkeypatch.setattr(recovery_module, "rankings_window_visible", lambda _image: True)
    monkeypatch.setattr(recovery_module, "division_view_visible", lambda _image: False)
    monkeypatch.setattr(recovery_module, "gameplay_frame_visible", lambda _image: True)

    assert driver._ready(image) is False


def test_recovery_restores_and_verifies_division_from_generic_rankings(
    tmp_path,
    monkeypatch,
) -> None:
    driver = maintenance_driver(tmp_path)
    driver.input_driver = object()
    bounds = WindowBounds("PirateGalaxy", 0, 0, 800, 600, handle=123)
    generic = Image.new("RGB", (800, 600), "black")
    division = Image.new("RGB", (800, 600), "white")
    images = iter(((generic, bounds), (division, bounds)))
    events: list[str] = []
    refreshes: list[str] = []
    driver.observe = lambda: RuntimeObservation(True, True, False, "rankings")
    driver._restore_ready_profile = lambda image, current_bounds, _progress: (
        image,
        current_bounds,
    )
    monkeypatch.setattr(recovery_module, "find_client_bounds", lambda _title: bounds)
    monkeypatch.setattr(recovery_module, "capture_window", lambda _title: next(images))
    monkeypatch.setattr(
        recovery_module,
        "rankings_window_visible",
        lambda image: image is generic or image is division,
    )
    monkeypatch.setattr(recovery_module, "division_view_visible", lambda image: image is division)
    monkeypatch.setattr(
        recovery_module,
        "refresh_rankings",
        lambda title, _config, **_kwargs: refreshes.append(title) or bounds,
    )

    outcome = driver.recover(
        events.append,
        allow_restart=True,
        restart_maintenance=False,
    )

    assert outcome.status is RecoveryStatus.RECOVERED
    assert outcome.detail == "responsive Division 1 rankings are verified"
    assert refreshes == ["PirateGalaxy"]
    assert events == ["restoring verified Division 1 rankings from the generic rankings window"]


def test_gameplay_verification_opens_and_requires_division_rankings(
    tmp_path,
    monkeypatch,
) -> None:
    driver = maintenance_driver(tmp_path)
    driver.input_driver = object()
    bounds = WindowBounds("PirateGalaxy", 0, 0, 800, 600, handle=123)
    gameplay = Image.new("RGB", (800, 600), "black")
    division = Image.new("RGB", (800, 600), "white")
    events: list[str] = []
    refreshes: list[str] = []
    monkeypatch.setattr(
        recovery_module,
        "division_view_visible",
        lambda image: image is division,
    )
    monkeypatch.setattr(
        recovery_module,
        "refresh_rankings",
        lambda title, _config, **_kwargs: refreshes.append(title) or bounds,
    )
    monkeypatch.setattr(
        recovery_module,
        "capture_window",
        lambda _title: (division, bounds),
    )

    result = driver._verify_division_rankings(gameplay, bounds, events.append)

    assert result == (division, bounds)
    assert refreshes == ["PirateGalaxy"]
    assert events == ["verifying the Division 1 rankings view"]


def test_gameplay_verification_saves_failed_refresh_screen(
    tmp_path,
    monkeypatch,
) -> None:
    driver = maintenance_driver(tmp_path)
    driver.input_driver = object()
    bounds = WindowBounds("PirateGalaxy", 0, 0, 800, 600, handle=123)
    gameplay = Image.new("RGB", (800, 600), "black")
    failed = Image.new("RGB", (800, 600), "red")
    captures = iter(((failed, bounds),))
    monkeypatch.setattr(recovery_module, "division_view_visible", lambda _image: False)
    monkeypatch.setattr(
        recovery_module,
        "refresh_rankings",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            recovery_module.RefreshError("Division 1 missing")
        ),
    )
    monkeypatch.setattr(recovery_module, "capture_window", lambda _title: next(captures))

    result = driver._verify_division_rankings(gameplay, bounds, lambda _detail: None)

    assert result is None
    assert Image.open(tmp_path / "recovery-unknown.png").getpixel((0, 0)) == (255, 0, 0)


def test_resilient_verification_recovers_a_dialog_that_appears_during_refresh(
    tmp_path,
    monkeypatch,
) -> None:
    driver = maintenance_driver(tmp_path)
    bounds = WindowBounds("PirateGalaxy", 0, 0, 800, 600, handle=123)
    gameplay = Image.new("RGB", (800, 600), "green")
    blocked = Image.new("RGB", (800, 600), "navy")
    division = Image.new("RGB", (800, 600), "white")
    verification_results = iter((None, (division, bounds)))
    events: list[str] = []
    retry_stability: list[float] = []
    driver._verify_division_rankings = lambda *_args: next(verification_results)
    driver._visible_blocking_action = lambda image: (
        "dismiss-support-energy" if image is blocked else None
    )

    def dismiss(_image, current_bounds, progress, *, stability_seconds):
        retry_stability.append(stability_seconds)
        progress("closed returning support dialog")
        return gameplay, current_bounds

    driver._dismiss_blocking_dialogs = dismiss
    monkeypatch.setattr(
        recovery_module,
        "capture_window",
        lambda _title: (blocked, bounds),
    )

    result = driver._verify_division_rankings_resilient(gameplay, bounds, events.append)

    assert result == (division, bounds)
    assert events == [
        "verified dismiss-support-energy dialog appeared during rankings verification; "
        "returning to bounded dialog recovery",
        "closed returning support dialog",
    ]
    assert retry_stability == [recovery_module.VERIFICATION_RETRY_STABILITY_SECONDS]


def test_resilient_verification_bounds_reappearing_dialog_retries(
    tmp_path,
    monkeypatch,
) -> None:
    driver = maintenance_driver(tmp_path)
    bounds = WindowBounds("PirateGalaxy", 0, 0, 800, 600, handle=123)
    gameplay = Image.new("RGB", (800, 600), "green")
    blocked = Image.new("RGB", (800, 600), "navy")
    verification_attempts: list[int] = []
    dismissals: list[int] = []
    driver._verify_division_rankings = lambda *_args: verification_attempts.append(1)
    driver._visible_blocking_action = lambda _image: "dismiss-support-energy"
    driver._dismiss_blocking_dialogs = lambda _image, current_bounds, _progress, **_kwargs: (
        dismissals.append(1) or (gameplay, current_bounds)
    )
    monkeypatch.setattr(
        recovery_module,
        "capture_window",
        lambda _title: (blocked, bounds),
    )

    result = driver._verify_division_rankings_resilient(
        gameplay,
        bounds,
        lambda _detail: None,
    )

    assert result is None
    assert len(verification_attempts) == recovery_module.MAX_RANKINGS_VERIFICATION_ATTEMPTS
    assert len(dismissals) == recovery_module.MAX_RANKINGS_VERIFICATION_ATTEMPTS - 1


def test_responsive_game_restarts_when_division_verification_fails(
    tmp_path,
    monkeypatch,
) -> None:
    driver = maintenance_driver(tmp_path)
    bounds = WindowBounds("PirateGalaxy", 0, 0, 800, 600, handle=123)
    image = Image.new("RGB", (800, 600), "black")
    observations = iter(
        (
            RuntimeObservation(True, True, False, "gameplay"),
            RuntimeObservation(False, False, False, "closed"),
        )
    )
    ready = iter((True, True, False))
    events: list[str] = []
    driver.observe = lambda: next(observations)
    driver._ready = lambda _image: next(ready)
    driver._restore_ready_profile = lambda current_image, current_bounds, _progress: (
        current_image,
        current_bounds,
    )
    driver._verify_division_rankings = lambda *_args: None
    driver._restart_required_visible = lambda _image: True
    driver._maintenance_visible = lambda _image: False
    driver._close_game = lambda _bounds: events.append("closed")
    driver._start_client_task = lambda: events.append("started")
    driver._window_responding = lambda _handle: True
    monkeypatch.setattr(recovery_module, "find_client_bounds", lambda _title: bounds)
    monkeypatch.setattr(recovery_module, "capture_window", lambda _title: (image, bounds))

    outcome = driver.recover(
        events.append,
        allow_restart=True,
        restart_maintenance=False,
    )

    assert outcome.status is RecoveryStatus.DEFERRED
    assert outcome.restarted
    assert outcome.maintenance
    assert events == [
        "restarting client after Division 1 verification failed",
        "closed",
        "starting Pirate Galaxy client task",
        "started",
    ]


def test_due_maintenance_probe_restarts_and_escalates_if_maintenance_remains(
    tmp_path,
    monkeypatch,
) -> None:
    driver = maintenance_driver(tmp_path)
    bounds = WindowBounds("PirateGalaxy", 0, 0, 800, 600, handle=123)
    maintenance = Image.new("RGB", (800, 600), "black")
    observations = iter(
        (
            RuntimeObservation(True, True, False, "maintenance"),
            RuntimeObservation(False, False, False, "closed"),
        )
    )
    events: list[str] = []
    driver.observe = lambda: next(observations)
    driver._ready = lambda _image: False
    driver._restart_required_visible = lambda _image: False
    driver._maintenance_visible = lambda _image: True
    driver._close_game = lambda _bounds: events.append("closed")
    driver._start_client_task = lambda: events.append("started")
    driver._window_responding = lambda _handle: True
    monkeypatch.setattr(recovery_module, "find_client_bounds", lambda _title: bounds)
    monkeypatch.setattr(
        recovery_module,
        "capture_window",
        lambda _title: (maintenance, bounds),
    )

    outcome = driver.recover(
        events.append,
        allow_restart=True,
        restart_maintenance=True,
    )

    assert outcome.status is RecoveryStatus.DEFERRED
    assert outcome.maintenance
    assert outcome.restarted
    assert outcome.maintenance_probe
    assert events == [
        "restarting client after confirmed maintenance backoff",
        "closed",
        "starting Pirate Galaxy client task",
        "started",
    ]
