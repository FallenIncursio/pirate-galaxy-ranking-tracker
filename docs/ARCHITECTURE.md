# Architecture

## Trust boundary

The tracker treats every game capture as untrusted input. Discord is updated
only after the current window, client dimensions, ranking view, row ordering,
scores, confidence thresholds, and two independent reads have been validated.

```text
Interactive Windows session
  Pirate Galaxy window
    -> bounded ranking-view refresh
    -> two client-area screenshots
    -> OCR and consensus
    -> RankingSnapshot
       -> SQLite history
       -> ranking PNG
       -> weekly activity PNG
       -> Discord webhook messages

Watchdog
  -> checks worker heartbeat and last valid snapshot
  -> restarts only a stale worker

Recovery controller
  -> classifies known failure states
  -> uses only recognized local templates
  -> observes unknown states without clicking
```

## Modules

- `capture.py`: platform window discovery and client-area capture
- `ui_refresh.py`: fixed-size checks and ranking-view navigation
- `ocr.py`: crops, Tesseract reads, validation, and two-read consensus
- `history.py`: SQLite snapshots, comparison baselines, and weekly activity
- `ranking_card.py`: deterministic ranking and activity PNG rendering
- `discord_webhook.py`: persistent webhook messages and isolated target state
- `service.py`: capture/publish cycle and stale-state transitions
- `recovery.py`: restart policy, maintenance backoff, and restart budgets
- `windows_recovery.py`: recognized-screen recovery driver
- `runtime_state.py`: atomic heartbeat and diagnostic state
- `single_instance.py`: worker singleton and shared automation lease

## Persistent data

All mutable data is local and ignored by Git:

- `data/history.sqlite3`: validated Top-11 snapshots and reset markers
- `data/state.json`: Discord message IDs and last published snapshot
- `data/runtime.json`: worker heartbeat and failure state
- `data/recovery-state.json`: maintenance and restart budget state
- `data/diagnostics/`: current captures, crops, and failure evidence
- `logs/`: rotating runtime logs

Webhook URLs are loaded from the ignored `secrets.env`; configuration stores
only environment-variable names.

## Failure model

- One uncertain OCR pair is rejected without replacing Discord data.
- Repeated failures mark existing cards stale.
- Missing comparison coverage is displayed as unavailable, not zero.
- Weekly comparisons cannot cross the detected or fallback reset boundary.
- Unknown screen states never authorize clicks.
- Recovery and normal refresh share one automation lease.
- Restart attempts are persisted, rate-limited, and maintenance-aware.

## Platform boundary

History, rendering, parsing, and webhook logic are portable Python. The active
capture, input, scheduled-task, watchdog, and recovery integration targets an
interactive Windows desktop because minimized, locked, or non-interactive
sessions cannot be captured reliably.
