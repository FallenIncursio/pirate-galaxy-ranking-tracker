from pathlib import Path

import pytest

from pg_rankings.config import ConfigError, load_config

VALID_CONFIG = """
[tracker]
server = "Example Server"
division = 1
top_n = 7
capture_top_n = 11
reward_slots = 4
poll_seconds = 300
consensus_delay_seconds = 2.0
stale_after_failures = 3
window_title_contains = "Pirate Galaxy"

[ocr]
language = "eng"
minimum_confidence = 45
anchor_minimum_confidence = 35
anchor = [0.05, 0.20, 0.30, 0.05]
name_x = 0.20
name_width = 0.50
score_x = 0.80
score_width = 0.15
first_row_y = 0.28
row_height = 0.06
row_content_height = 0.05

[ocr.name_aliases]
"Pilotss" = "Pilot98"

[refresh]
expected_width = 960
expected_height = 600
menu_icon_x = 0.458
menu_icon_y = 0.023
rankings_tab_x = 0.285
rankings_tab_y = 0.125
menu_wait_seconds = 3.0
content_wait_seconds = 5.0

[paths]
state = "data/state.json"
history = "data/history.sqlite3"
diagnostics = "data/diagnostics"
log = "logs/tracker.log"

[discord]
webhook_environment = "DISCORD_WEBHOOK_URL"
username = "Example Rankings"
"""


def write_config(tmp_path: Path, content: str = VALID_CONFIG) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_load_config_resolves_paths(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    assert config.server == "Example Server"
    assert config.top_n == 7
    assert config.capture_top_n == 11
    assert config.paths.state == tmp_path / "data" / "state.json"
    assert config.paths.history == tmp_path / "data" / "history.sqlite3"
    assert config.paths.runtime == tmp_path / "data" / "runtime.json"
    assert config.paths.recovery_state == tmp_path / "data" / "recovery-state.json"
    assert not config.recovery.enabled
    assert config.recovery.shadow_mode
    assert config.recovery.maintenance_backoff_seconds == (900, 1800, 3600)
    assert config.ocr.name_aliases == (("Pilotss", "Pilot98"),)
    assert config.discord.targets[0].id == "primary"
    assert config.discord.targets[0].webhook_environment == "DISCORD_WEBHOOK_URL"


def test_rejects_empty_ocr_name_alias(tmp_path: Path) -> None:
    path = write_config(tmp_path, VALID_CONFIG.replace('"Pilotss" = "Pilot98"', '"Pilotss" = ""'))
    with pytest.raises(ConfigError, match="must not be empty"):
        load_config(path)


def test_loads_multiple_discord_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    multi_target = VALID_CONFIG.replace(
        """webhook_environment = "DISCORD_WEBHOOK_URL"
username = "Example Rankings""".strip(),
        """username = "Example Rankings"

[[discord.targets]]
id = "primary"
webhook_environment = "DISCORD_WEBHOOK_URL"

[[discord.targets]]
id = "clan"
webhook_environment = "DISCORD_WEBHOOK_CLAN""".strip(),
    )
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/a")
    monkeypatch.setenv("DISCORD_WEBHOOK_CLAN", "https://discord.com/api/webhooks/2/b")

    config = load_config(write_config(tmp_path, multi_target))

    assert [target.id for target in config.discord.targets] == ["primary", "clan"]
    assert config.webhook_environment("clan") == "DISCORD_WEBHOOK_CLAN"
    assert config.webhook_urls() == (
        ("primary", "https://discord.com/api/webhooks/1/a"),
        ("clan", "https://discord.com/api/webhooks/2/b"),
    )


def test_rejects_duplicate_discord_target_ids(tmp_path: Path) -> None:
    multi_target = VALID_CONFIG.replace(
        """webhook_environment = "DISCORD_WEBHOOK_URL"
username = "Example Rankings""".strip(),
        """username = "Example Rankings"

[[discord.targets]]
id = "primary"
webhook_environment = "DISCORD_WEBHOOK_URL"

[[discord.targets]]
id = "primary"
webhook_environment = "DISCORD_WEBHOOK_CLAN""".strip(),
    )
    with pytest.raises(ConfigError, match="duplicate Discord target id"):
        load_config(write_config(tmp_path, multi_target))


def test_rejects_polling_faster_than_one_minute(tmp_path: Path) -> None:
    path = write_config(tmp_path, VALID_CONFIG.replace("poll_seconds = 300", "poll_seconds = 30"))
    with pytest.raises(ConfigError, match="at least 60"):
        load_config(path)


def test_rejects_rows_outside_image(tmp_path: Path) -> None:
    path = write_config(tmp_path, VALID_CONFIG.replace("first_row_y = 0.28", "first_row_y = 0.80"))
    with pytest.raises(ConfigError, match="rows exceed"):
        load_config(path)


def test_rejects_capture_count_smaller_than_display_count(tmp_path: Path) -> None:
    path = write_config(tmp_path, VALID_CONFIG.replace("capture_top_n = 11", "capture_top_n = 6"))
    with pytest.raises(ConfigError, match="capture_top_n"):
        load_config(path)


def test_defaults_capture_count_and_history_path(tmp_path: Path) -> None:
    legacy = VALID_CONFIG.replace("capture_top_n = 11\n", "").replace(
        'history = "data/history.sqlite3"\n', ""
    )

    config = load_config(write_config(tmp_path, legacy))

    assert config.capture_top_n == 11
    assert config.paths.history == tmp_path / "data" / "history.sqlite3"


def test_loads_recovery_settings(tmp_path: Path) -> None:
    configured = VALID_CONFIG.replace(
        "[ocr]",
        """[recovery]
enabled = true
shadow_mode = false
refresh_failure_threshold = 4
max_restarts_per_hour = 2
launch_timeout_seconds = 900
probe_interval_seconds = 10.0
graceful_close_seconds = 20
action_cooldown_seconds = 15.0
watchdog_stale_seconds = 900
client_task_name = "Pirate Galaxy Client"
template_threshold = 0.91
maintenance_backoff_seconds = [600, 1200, 2400]

[ocr]""",
    )

    config = load_config(write_config(tmp_path, configured))

    assert config.recovery.enabled
    assert not config.recovery.shadow_mode
    assert config.recovery.refresh_failure_threshold == 4
    assert config.recovery.max_restarts_per_hour == 2
    assert config.recovery.launch_timeout_seconds == 900
    assert config.recovery.template_threshold == 0.91
    assert config.recovery.maintenance_backoff_seconds == (600, 1200, 2400)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("refresh_failure_threshold = 1", "at least two"),
        ("max_restarts_per_hour = 0", "at least one"),
        ("launch_timeout_seconds = 30", "at least 60"),
        ("watchdog_stale_seconds = 300", "two polling intervals"),
        ("template_threshold = 0.5", "between 0.70 and 1.0"),
        ("maintenance_backoff_seconds = []", "at least one"),
        ("maintenance_backoff_seconds = [30]", "at least 60"),
        ("maintenance_backoff_seconds = [900, 600]", "shortest to longest"),
    ],
)
def test_rejects_unsafe_recovery_settings(tmp_path: Path, line: str, message: str) -> None:
    configured = VALID_CONFIG.replace("[ocr]", f"[recovery]\n{line}\n\n[ocr]")

    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tmp_path, configured))
