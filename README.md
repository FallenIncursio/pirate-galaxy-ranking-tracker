# Pirate Galaxy Ranking Tracker

A screenshot-based Pirate Galaxy pilot-ranking tracker for Discord. It refreshes
the already opened ranking view, validates two independent OCR captures, stores
the visible Top 11, and updates persistent ranking and activity cards.

The tracker does not use a game API, inspect process memory, enter credentials,
move the pilot, or interact with combat. Unknown layouts and uncertain OCR
results fail closed and are not published.

> The repository is private while installation, documentation, and release
> hygiene are being reviewed. Pirate Galaxy and related names and artwork belong
> to their respective owners. This project is not affiliated with or endorsed by
> Splitscreen Studios.

## Features

- Two screenshots per cycle with strict name, score, ordering, and confidence checks
- Configurable server, division, visible rows, reward slots, and poll interval
- Persistent Discord messages for a ranking card and weekly activity timeline
- Multiple independent Discord webhook targets
- Fifteen-minute, one-hour, and 24-hour score differences
- Eight-day Top-11 SQLite history and weekly reset isolation
- Missing-data and stale-state visualization instead of invented activity
- Windows scheduled-task installation, watchdog, and bounded recovery
- Normalized coordinates with a fixed client-size safety gate
- Configuration-backed exact OCR name aliases for verified recurring misreads

## How it works

```text
Pirate Galaxy window
  -> open/reselect the ranking view
  -> capture twice
  -> crop names, scores, and Division anchor
  -> Tesseract OCR + two-read consensus
  -> validated Top-11 snapshot
  -> SQLite history and delta calculation
  -> ranking/activity PNG cards
  -> edit persistent Discord webhook messages
```

Only a complete validated snapshot replaces existing Discord content. Rejected
cycles retain the last trustworthy ranking; repeated failures mark it stale.

## Requirements

- Windows 10 or Windows 11 with an interactive desktop session
- Python 3.11 or newer
- Tesseract OCR 5
- Pirate Galaxy running in a stable windowed resolution
- One or more Discord incoming webhooks

The supported production path is Windows. Older Linux/Proton experiments are
not part of this repository.

## Installation

Clone the repository into a short local path, then open an elevated PowerShell:

```powershell
git clone https://github.com/FallenIncursio/pirate-galaxy-ranking-tracker.git
cd pirate-galaxy-ranking-tracker
Set-ExecutionPolicy -Scope Process Bypass
.\windows\install-game.ps1
.\windows\install.ps1
```

The installer creates `.venv`, copies `config.example.toml` to the ignored
`config.toml`, collects the first webhook through hidden input, restricts the
secret file ACL, and optionally registers the tracker and watchdog tasks.

Start the game, sign in manually, open the desired pilot ranking once, and keep
the pilot in a stable non-combat location. Then calibrate and perform a dry run:

```powershell
.\windows\calibrate.ps1
.\windows\run-once.ps1 -DryRun
Start-ScheduledTask -TaskName "Pirate Galaxy Rankings"
```

The first non-dry run creates two Discord messages for each configured target.
Later cycles edit those messages instead of posting new ones.

## Configuration

`config.example.toml` is safe to commit. Copy it to `config.toml` and adjust the
local copy only. Important sections are:

- `[tracker]`: server, division, row counts, rewards, polling, and window title
- `[ocr]`: language, confidence thresholds, and normalized crop geometry
- `[ocr.name_aliases]`: exact, locally verified OCR corrections
- `[refresh]`: expected client size and normalized ranking-navigation points
- `[recovery]`: disabled-by-default recovery and restart limits
- `[paths]`: ignored runtime state, history, diagnostics, and logs
- `[discord]` and `[[discord.targets]]`: webhook environment-variable mapping

Webhooks belong only in `secrets.env`:

```dotenv
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/REPLACE_ME/REPLACE_ME
```

Add another target by defining a unique environment variable in both files,
then restart only the tracker task:

```powershell
.\windows\set-webhook.ps1 -Target clan -Restart
```

## Recovery templates

Recovery is disabled by default. It requires narrow, locally captured templates
under `assets/recovery/`; PNG files in that directory are intentionally ignored
by Git. See [assets/recovery/README.md](assets/recovery/README.md) before enabling
recovery. Start in shadow mode and validate every recognized action on the target
machine.

## Useful commands

```powershell
# Static configuration validation
.\.venv\Scripts\python.exe -m pg_rankings --config config.toml validate-config

# Capture and OCR without Discord
.\windows\run-once.ps1 -DryRun

# Refresh only the ranking view
.\.venv\Scripts\python.exe -m pg_rankings --config config.toml refresh-ui

# Read-only task and runtime report
.\windows\verify-runtime.ps1 -NoOpen

# Run tests and repository safety checks
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\python.exe -m pytest --cov=pg_rankings --cov-report=term-missing
.\.venv\Scripts\python.exe scripts\check-repository.py
```

Operational behavior, failure states, and acceptance checks are documented in
[docs/OPERATIONS.md](docs/OPERATIONS.md). Architecture and data boundaries are
described in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Repository safety

Never commit or upload:

- `secrets.env`, `.env*`, or a real webhook URL
- `config.toml`
- `data/`, SQLite databases, Discord state, logs, or diagnostics
- support bundles or screenshots containing player/runtime data
- locally captured game UI templates

The CI workflow checks formatting, lint, tests, package metadata, and common
credential/runtime-file leaks on Windows and Linux.

## License

The original source code is licensed under the [MIT License](LICENSE). The
license does not grant rights to third-party names, trademarks, or artwork.
