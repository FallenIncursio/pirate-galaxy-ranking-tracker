from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .config import ConfigError, TrackerConfig, load_config

if TYPE_CHECKING:
    from .discord_webhook import DiscordPublisher


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Screenshot-only Pirate Galaxy Division 1 tracker")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="path to config.toml",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run every configured interval")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="keep collecting without sending anything to Discord",
    )
    once = subparsers.add_parser("once", help="capture and OCR one update")
    once.add_argument("--dry-run", action="store_true", help="do not send anything to Discord")
    subparsers.add_parser("calibrate", help="save a screenshot, crops, and OCR report")
    subparsers.add_parser("refresh-ui", help="refresh only the rankings UI")
    subparsers.add_parser("recovery-probe", help="inspect game recovery state without actions")
    subparsers.add_parser("recover", help="run one configured recovery attempt")
    subparsers.add_parser("validate-config", help="validate configuration only")
    webhook_environment = subparsers.add_parser(
        "webhook-environment",
        help="print the environment variable configured for one Discord target",
    )
    webhook_environment.add_argument("--target", required=True)
    return parser


def _configure_logging(config: TrackerConfig) -> None:
    config.paths.log.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S%z"
    )
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    rotating = logging.handlers.RotatingFileHandler(
        config.paths.log,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    rotating.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[stream, rotating])


def _publisher(config: TrackerConfig) -> DiscordPublisher:
    from .discord_webhook import DiscordPublisher

    return DiscordPublisher(
        webhook_urls=config.webhook_urls(),
        state_path=config.paths.state,
        history_path=config.paths.history,
        username=config.discord.username,
        display_top_n=config.top_n,
        reward_slots=config.reward_slots,
    )


def _runtime_components(config: TrackerConfig, *, project_root: Path):
    from .recovery import RecoveryController
    from .runtime_state import RuntimeHeartbeat

    runtime_path = config.paths.runtime or config.paths.state.with_name("runtime.json")
    recovery_state_path = config.paths.recovery_state or config.paths.state.with_name(
        "recovery-state.json"
    )
    heartbeat = RuntimeHeartbeat(runtime_path)
    recovery = None
    driver = None
    if config.recovery.enabled and sys.platform == "win32":
        from .windows_recovery import WindowsGameRecovery

        driver = WindowsGameRecovery(config, project_root=project_root)
        recovery = RecoveryController(
            config.recovery,
            driver=driver,
            heartbeat=heartbeat,
            state_path=recovery_state_path,
        )
    return heartbeat, recovery, driver


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        config = load_config(arguments.config)
        _configure_logging(config)
        if arguments.command == "webhook-environment":
            print(config.webhook_environment(arguments.target))
            return 0
        if arguments.command == "validate-config":
            print(f"Configuration valid: {config.server} Division {config.division}")
            return 0
        if arguments.command == "refresh-ui":
            from .single_instance import automation_lease
            from .ui_refresh import refresh_rankings

            with automation_lease():
                bounds = refresh_rankings(config.window_title_contains, config.refresh)
            print(f"Rankings UI refreshed in {bounds.title} ({bounds.width}x{bounds.height})")
            return 0
        if arguments.command in {"recovery-probe", "recover"}:
            if sys.platform != "win32":
                raise RuntimeError("game recovery commands require Windows")
            _heartbeat, recovery, driver = _runtime_components(
                config,
                project_root=arguments.config.resolve().parent,
            )
            if driver is None or recovery is None:
                raise RuntimeError("game recovery is disabled in config.toml")
            if arguments.command == "recovery-probe":
                observation = driver.observe()
                print(
                    json.dumps(
                        {
                            "game_window_present": observation.game_window_present,
                            "game_responding": observation.game_responding,
                            "launcher_running": observation.launcher_running,
                            "detail": observation.detail,
                            "shadow_mode": config.recovery.shadow_mode,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            from .capture import CaptureError
            from .single_instance import automation_lease

            with automation_lease():
                outcome = recovery.handle_failure(
                    CaptureError("no window contains title (manual recovery request)"),
                    consecutive_failures=config.recovery.refresh_failure_threshold,
                )
            if outcome is None:
                raise RuntimeError("manual recovery request was not handled")
            print(
                json.dumps(
                    {
                        "status": outcome.status,
                        "detail": outcome.detail,
                        "restarted": outcome.restarted,
                        "retry_after_seconds": outcome.retry_after_seconds,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if arguments.command == "calibrate":
            from .service import calibrate_capture

            directory = calibrate_capture(config)
            print(f"Calibration artifacts: {directory}")
            return 0

        if arguments.command == "once":
            from .service import TrackerService, snapshot_for_console

            dry_run = arguments.dry_run
            publisher = None if dry_run else _publisher(config)
            heartbeat, recovery, _driver = _runtime_components(
                config,
                project_root=arguments.config.resolve().parent,
            )
            service = TrackerService(
                config,
                publisher=publisher,
                heartbeat=heartbeat,
                recovery=recovery,
            )
            snapshot = service.run_once(publish=not dry_run)
            print(snapshot_for_console(snapshot))
            return 0

        from .single_instance import AlreadyRunningError, tracker_instance

        continuous_dry_run = arguments.dry_run

        def run_continuous_tracker() -> None:
            from .service import TrackerService

            publisher = None if continuous_dry_run else _publisher(config)
            heartbeat, recovery, _driver = _runtime_components(
                config,
                project_root=arguments.config.resolve().parent,
            )
            service = TrackerService(
                config,
                publisher=publisher,
                heartbeat=heartbeat,
                recovery=recovery,
            )
            service.run_forever(publish=not continuous_dry_run)

        if os.environ.get("PG_TRACKER_MUTEX_HELD") == "1":
            run_continuous_tracker()
            return 0
        try:
            with tracker_instance():
                run_continuous_tracker()
        except AlreadyRunningError as error:
            logging.getLogger(__name__).warning("tracker not started: %s", error)
        return 0
    except KeyboardInterrupt:
        return 130
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        logging.getLogger(__name__).exception("tracker stopped")
        print(f"Tracker error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
