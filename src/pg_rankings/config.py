from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    @classmethod
    def from_value(cls, value: Any, *, name: str) -> Rect:
        if not isinstance(value, list) or len(value) != 4:
            raise ConfigError(f"{name} must contain four normalized numbers")
        rect = cls(*(float(part) for part in value))
        if (
            rect.x < 0
            or rect.y < 0
            or rect.width <= 0
            or rect.height <= 0
            or rect.x + rect.width > 1
            or rect.y + rect.height > 1
        ):
            raise ConfigError(f"{name} must stay inside the normalized client area")
        return rect


@dataclass(frozen=True, slots=True)
class OcrConfig:
    language: str
    minimum_confidence: float
    anchor_minimum_confidence: float
    anchor: Rect
    name_x: float
    name_width: float
    score_x: float
    score_width: float
    first_row_y: float
    row_height: float
    row_content_height: float
    name_aliases: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class RefreshConfig:
    expected_width: int
    expected_height: int
    menu_icon_x: float
    menu_icon_y: float
    rankings_tab_x: float
    rankings_tab_y: float
    menu_wait_seconds: float
    content_wait_seconds: float


@dataclass(frozen=True, slots=True)
class RecoveryConfig:
    enabled: bool = False
    shadow_mode: bool = True
    refresh_failure_threshold: int = 3
    max_restarts_per_hour: int = 3
    launch_timeout_seconds: int = 600
    probe_interval_seconds: float = 5.0
    graceful_close_seconds: int = 15
    action_cooldown_seconds: float = 10.0
    watchdog_stale_seconds: int = 720
    client_task_name: str = "Pirate Galaxy Client"
    template_threshold: float = 0.88
    maintenance_backoff_seconds: tuple[int, ...] = (900, 1800, 3600)


@dataclass(frozen=True, slots=True)
class PathConfig:
    state: Path
    history: Path
    diagnostics: Path
    log: Path
    runtime: Path | None = None
    recovery_state: Path | None = None


@dataclass(frozen=True, slots=True)
class DiscordTargetConfig:
    id: str
    webhook_environment: str


@dataclass(frozen=True, slots=True)
class DiscordConfig:
    username: str
    targets: tuple[DiscordTargetConfig, ...]


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    server: str
    division: int
    top_n: int
    capture_top_n: int
    reward_slots: int
    poll_seconds: int
    consensus_delay_seconds: float
    stale_after_failures: int
    window_title_contains: str
    ocr: OcrConfig
    refresh: RefreshConfig
    paths: PathConfig
    discord: DiscordConfig
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)

    def webhook_urls(self) -> tuple[tuple[str, str], ...]:
        resolved: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for target in self.discord.targets:
            value = os.environ.get(target.webhook_environment, "").strip()
            if not value:
                raise ConfigError(
                    f"environment variable {target.webhook_environment} "
                    f"for Discord target {target.id} is missing"
                )
            if value in seen_urls:
                raise ConfigError("Discord targets must not resolve to the same webhook URL")
            seen_urls.add(value)
            resolved.append((target.id, value))
        return tuple(resolved)

    def webhook_environment(self, target_id: str) -> str:
        for target in self.discord.targets:
            if target.id == target_id:
                return target.webhook_environment
        available = ", ".join(target.id for target in self.discord.targets)
        raise ConfigError(f"unknown Discord target {target_id!r}; available: {available}")


def _required(table: dict[str, Any], key: str, table_name: str) -> Any:
    if key not in table:
        raise ConfigError(f"missing {table_name}.{key}")
    return table[key]


def _relative_path(base: Path, value: Any) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else (base / path).resolve()


def _positive_integer_sequence(value: Any, *, name: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{name} must contain at least one positive number of seconds")
    converted = tuple(int(part) for part in value)
    if any(part < 60 for part in converted):
        raise ConfigError(f"{name} values must be at least 60 seconds")
    if any(later < earlier for earlier, later in zip(converted, converted[1:], strict=False)):
        raise ConfigError(f"{name} values must be ordered from shortest to longest")
    return converted


def _name_aliases(value: Any) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise ConfigError("ocr.name_aliases must be a table")

    aliases: list[tuple[str, str]] = []
    seen_sources: set[str] = set()
    for raw_source, raw_target in value.items():
        source = str(raw_source).strip()
        target = str(raw_target).strip()
        source_key = source.casefold()
        if not source or not target:
            raise ConfigError("ocr.name_aliases entries must not be empty")
        if source_key in seen_sources:
            raise ConfigError(f"duplicate OCR name alias {source!r}")
        seen_sources.add(source_key)
        aliases.append((source, target))
    return tuple(aliases)


def _discord_targets(discord: dict[str, Any]) -> tuple[DiscordTargetConfig, ...]:
    legacy_environment = discord.get("webhook_environment")
    raw_targets = discord.get("targets")
    if legacy_environment is not None and raw_targets is not None:
        raise ConfigError(
            "discord.webhook_environment and discord.targets cannot be configured together"
        )
    if raw_targets is None:
        raw_targets = [
            {
                "id": "primary",
                "webhook_environment": _required(discord, "webhook_environment", "discord"),
            }
        ]
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ConfigError("discord.targets must contain at least one target")

    targets: list[DiscordTargetConfig] = []
    seen_ids: set[str] = set()
    seen_environments: set[str] = set()
    for index, raw_target in enumerate(raw_targets):
        table_name = f"discord.targets[{index}]"
        if not isinstance(raw_target, dict):
            raise ConfigError(f"{table_name} must be a table")
        target_id = str(_required(raw_target, "id", table_name)).strip()
        environment = str(_required(raw_target, "webhook_environment", table_name)).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", target_id):
            raise ConfigError(
                f"{table_name}.id must use 1-64 letters, digits, hyphens, or underscores"
            )
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", environment):
            raise ConfigError(f"{table_name}.webhook_environment is not a valid variable name")
        if target_id in seen_ids:
            raise ConfigError(f"duplicate Discord target id {target_id!r}")
        if environment in seen_environments:
            raise ConfigError(f"duplicate Discord webhook environment {environment!r}")
        seen_ids.add(target_id)
        seen_environments.add(environment)
        targets.append(DiscordTargetConfig(target_id, environment))
    return tuple(targets)


def load_config(path: str | Path) -> TrackerConfig:
    config_path = Path(path).resolve()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    tracker = raw.get("tracker", {})
    ocr = raw.get("ocr", {})
    refresh = raw.get("refresh", {})
    recovery = raw.get("recovery", {})
    paths = raw.get("paths", {})
    discord = raw.get("discord", {})

    top_n = int(_required(tracker, "top_n", "tracker"))
    capture_top_n = int(tracker.get("capture_top_n", 11))
    reward_slots = int(_required(tracker, "reward_slots", "tracker"))
    if not 1 <= top_n <= 11:
        raise ConfigError("tracker.top_n must be between 1 and 11")
    if not top_n <= capture_top_n <= 11:
        raise ConfigError("tracker.capture_top_n must be between tracker.top_n and 11")
    if not 0 <= reward_slots <= top_n:
        raise ConfigError("tracker.reward_slots must be between zero and tracker.top_n")

    poll_seconds = int(_required(tracker, "poll_seconds", "tracker"))
    if poll_seconds < 60:
        raise ConfigError("tracker.poll_seconds must be at least 60")

    numeric_fields = {
        name: float(_required(ocr, name, "ocr"))
        for name in (
            "name_x",
            "name_width",
            "score_x",
            "score_width",
            "first_row_y",
            "row_height",
            "row_content_height",
        )
    }
    for name, value in numeric_fields.items():
        if not 0 <= value <= 1:
            raise ConfigError(f"ocr.{name} must be normalized between zero and one")
    if numeric_fields["name_x"] + numeric_fields["name_width"] > 1:
        raise ConfigError("OCR name column exceeds the client area")
    if numeric_fields["score_x"] + numeric_fields["score_width"] > 1:
        raise ConfigError("OCR score column exceeds the client area")
    if (
        numeric_fields["first_row_y"]
        + (capture_top_n - 1) * numeric_fields["row_height"]
        + numeric_fields["row_content_height"]
        > 1
    ):
        raise ConfigError("configured OCR rows exceed the client area")

    base = config_path.parent
    config = TrackerConfig(
        server=str(_required(tracker, "server", "tracker")).strip(),
        division=int(_required(tracker, "division", "tracker")),
        top_n=top_n,
        capture_top_n=capture_top_n,
        reward_slots=reward_slots,
        poll_seconds=poll_seconds,
        consensus_delay_seconds=float(_required(tracker, "consensus_delay_seconds", "tracker")),
        stale_after_failures=int(_required(tracker, "stale_after_failures", "tracker")),
        window_title_contains=str(_required(tracker, "window_title_contains", "tracker")).strip(),
        ocr=OcrConfig(
            language=str(_required(ocr, "language", "ocr")),
            minimum_confidence=float(_required(ocr, "minimum_confidence", "ocr")),
            anchor_minimum_confidence=float(_required(ocr, "anchor_minimum_confidence", "ocr")),
            anchor=Rect.from_value(_required(ocr, "anchor", "ocr"), name="ocr.anchor"),
            name_aliases=_name_aliases(ocr.get("name_aliases")),
            **numeric_fields,
        ),
        refresh=RefreshConfig(
            expected_width=int(_required(refresh, "expected_width", "refresh")),
            expected_height=int(_required(refresh, "expected_height", "refresh")),
            menu_icon_x=float(_required(refresh, "menu_icon_x", "refresh")),
            menu_icon_y=float(_required(refresh, "menu_icon_y", "refresh")),
            rankings_tab_x=float(_required(refresh, "rankings_tab_x", "refresh")),
            rankings_tab_y=float(_required(refresh, "rankings_tab_y", "refresh")),
            menu_wait_seconds=float(_required(refresh, "menu_wait_seconds", "refresh")),
            content_wait_seconds=float(_required(refresh, "content_wait_seconds", "refresh")),
        ),
        recovery=RecoveryConfig(
            enabled=bool(recovery.get("enabled", False)),
            shadow_mode=bool(recovery.get("shadow_mode", True)),
            refresh_failure_threshold=int(recovery.get("refresh_failure_threshold", 3)),
            max_restarts_per_hour=int(recovery.get("max_restarts_per_hour", 3)),
            launch_timeout_seconds=int(recovery.get("launch_timeout_seconds", 600)),
            probe_interval_seconds=float(recovery.get("probe_interval_seconds", 5.0)),
            graceful_close_seconds=int(recovery.get("graceful_close_seconds", 15)),
            action_cooldown_seconds=float(recovery.get("action_cooldown_seconds", 10.0)),
            watchdog_stale_seconds=int(recovery.get("watchdog_stale_seconds", 720)),
            client_task_name=str(recovery.get("client_task_name", "Pirate Galaxy Client")).strip(),
            template_threshold=float(recovery.get("template_threshold", 0.88)),
            maintenance_backoff_seconds=_positive_integer_sequence(
                recovery.get("maintenance_backoff_seconds", [900, 1800, 3600]),
                name="recovery.maintenance_backoff_seconds",
            ),
        ),
        paths=PathConfig(
            state=_relative_path(base, _required(paths, "state", "paths")),
            history=_relative_path(base, paths.get("history", "data/history.sqlite3")),
            diagnostics=_relative_path(base, _required(paths, "diagnostics", "paths")),
            log=_relative_path(base, _required(paths, "log", "paths")),
            runtime=_relative_path(base, paths.get("runtime", "data/runtime.json")),
            recovery_state=_relative_path(
                base, paths.get("recovery_state", "data/recovery-state.json")
            ),
        ),
        discord=DiscordConfig(
            username=str(_required(discord, "username", "discord")),
            targets=_discord_targets(discord),
        ),
    )
    if not config.server or not config.window_title_contains:
        raise ConfigError("server and window_title_contains must not be empty")
    if config.consensus_delay_seconds < 0:
        raise ConfigError("consensus_delay_seconds cannot be negative")
    if config.stale_after_failures < 1:
        raise ConfigError("stale_after_failures must be at least one")
    if config.refresh.expected_width < 640 or config.refresh.expected_height < 480:
        raise ConfigError("refresh expected client size is unexpectedly small")
    for name in ("menu_icon_x", "menu_icon_y", "rankings_tab_x", "rankings_tab_y"):
        value = getattr(config.refresh, name)
        if not 0 <= value <= 1:
            raise ConfigError(f"refresh.{name} must be normalized between zero and one")
    if config.refresh.menu_wait_seconds < 0 or config.refresh.content_wait_seconds < 0:
        raise ConfigError("refresh wait times cannot be negative")
    if config.recovery.refresh_failure_threshold < 2:
        raise ConfigError("recovery.refresh_failure_threshold must be at least two")
    if config.recovery.max_restarts_per_hour < 1:
        raise ConfigError("recovery.max_restarts_per_hour must be at least one")
    if config.recovery.launch_timeout_seconds < 60:
        raise ConfigError("recovery.launch_timeout_seconds must be at least 60")
    if not 1 <= config.recovery.probe_interval_seconds <= 60:
        raise ConfigError("recovery.probe_interval_seconds must be between 1 and 60")
    if config.recovery.graceful_close_seconds < 1:
        raise ConfigError("recovery.graceful_close_seconds must be at least one")
    if config.recovery.action_cooldown_seconds < 1:
        raise ConfigError("recovery.action_cooldown_seconds must be at least one")
    if config.recovery.watchdog_stale_seconds < 2 * config.poll_seconds:
        raise ConfigError("recovery.watchdog_stale_seconds must cover two polling intervals")
    if not config.recovery.client_task_name:
        raise ConfigError("recovery.client_task_name must not be empty")
    if not 0.70 <= config.recovery.template_threshold <= 1.0:
        raise ConfigError("recovery.template_threshold must be between 0.70 and 1.0")
    return config
