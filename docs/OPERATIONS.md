# Operations

## Normal state

- Pirate Galaxy is logged in manually and remains in a stable non-combat location.
- The client is windowed at the exact dimensions configured under `[refresh]`.
- Windows display scaling and the client UI scale remain unchanged after calibration.
- `Pirate Galaxy Rankings` runs in the interactive user session.
- `Pirate Galaxy Watchdog` checks worker and snapshot freshness.
- Each Discord target contains one persistent ranking card and one activity card.
- Secrets, state, history, screenshots, and logs remain local and ignored by Git.

No scheduled task stores or types game credentials. After a reboot, an
interactive Windows session and possibly a manual game login are still required.

## Initial acceptance

Before leaving a new installation unattended:

1. Run `validate-config` successfully.
2. Keep recovery disabled and run `windows\calibrate.ps1`.
3. Confirm every captured name and score against the visible ranking.
4. Confirm the refresh sequence targets only the intended ranking controls.
5. Run `windows\run-once.ps1 -DryRun` without Discord.
6. Run one live cycle and confirm two persistent messages are created or edited.
7. Confirm the cards show the configured server, division, rows, and reward slots.
8. Confirm `data\diagnostics\latest.json` has the expected client dimensions.
9. Confirm the latest history snapshot contains `capture_top_n` rows.
10. Confirm no temporary test task or diagnostic OCR process remains.

## Lifecycle commands

```powershell
cd <repository-path>

# Read-only runtime report
.\windows\verify-runtime.ps1 -NoOpen

# Tracker task
Start-ScheduledTask -TaskName "Pirate Galaxy Rankings"
Stop-ScheduledTask -TaskName "Pirate Galaxy Rankings"
Get-ScheduledTask -TaskName "Pirate Galaxy Rankings"

# Capture without Discord
.\windows\run-once.ps1 -DryRun

# Read-only recovery observation
.\.venv\Scripts\python.exe -m pg_rankings --config .\config.toml recovery-probe

# Recent tracker events
Get-Content .\logs\tracker.log -Tail 50
```

The scheduled task uses the interactive user because desktop capture and
client-relative input are unavailable in a background service session. The
tracker itself prevents duplicate workers, and a second automation lease keeps
manual recovery and normal refresh from clicking at the same time.

## Failure behavior

| Symptom | Expected behavior | Operator action |
| --- | --- | --- |
| Windows is locked or logged out | Capture cannot continue | Restore the interactive session |
| Game window is absent | Recovery may start the configured client task | Inspect recovery state if it does not return |
| Client dimensions changed | Input stops before clicking | Restore the configured window size and recalibrate |
| Ranking view is not verified | No snapshot is published | Inspect the latest refresh diagnostic |
| One OCR pair disagrees | The cycle is rejected | Allow the next cycle to retry |
| Repeated capture failures | Existing Discord cards become stale | Repair the client/session; recovery is automatic afterward |
| A Discord message was deleted | A replacement is created on the next valid cycle | Pin the replacement if needed |
| One webhook is unavailable | Other targets continue updating | Rotate only the affected target |
| Maintenance is recognized | Persistent 15/30/60-minute backoff applies | Let the bounded probes continue |
| Screen state is unknown | No click is attempted; evidence is retained | Add a narrow template only after review |
| Weekly scores reset | A new history period begins | Let new-period comparison baselines accumulate |

Do not weaken ordering, score parsing, Division anchoring, or two-capture
consensus to hide a rejected cycle.

## Calibration

Coordinates are normalized to the captured client area, but the active profile
still requires a fixed window size. Moving the window is safe; resizing it is
not.

```powershell
Stop-ScheduledTask -TaskName "Pirate Galaxy Rankings"
.\windows\calibrate.ps1
.\windows\run-once.ps1 -DryRun
Start-ScheduledTask -TaskName "Pirate Galaxy Rankings"
```

Review the generated anchor, name, and score crops. Recalibrate whenever a game
update changes the window size, font, ranking geometry, or controls.

## Recovery rollout

Recovery is an optional second phase:

1. Keep `[recovery].enabled = false` during normal OCR calibration.
2. Capture only the smallest stable visual template that identifies an action.
3. Store local templates under `assets\recovery`; they are ignored by Git.
4. Enable recovery with `shadow_mode = true` and inspect classifications.
5. Test a complete recovery with a non-production Discord target.
6. Set `shadow_mode = false` only after each possible action is verified.

Template clicks are followed by visual confirmation. Unknown layouts, missing
templates, and low-confidence matches remain observation-only.

## Webhook changes

`secrets.env` is loaded at worker start. A webhook change therefore needs only a
tracker-task restart:

```powershell
.\windows\set-webhook.ps1 -Target primary -Restart
```

To add another channel, add a unique `[[discord.targets]]` block to local
`config.toml`, then store the corresponding URL with the same helper. Target
failures are isolated, and each target owns separate ranking/activity message
IDs.

## Support bundles

`windows\export-support-bundle.ps1` intentionally excludes `secrets.env` and
Discord state. Bundles may still contain configuration, player names, window
captures, and logs. Review them before sharing and delete them after use.

## Updates

Before deploying a new revision:

1. Confirm CI and local tests pass.
2. Back up ignored configuration and runtime data.
3. Stop only the ranking and watchdog tasks.
4. Update the source and reinstall the editable package or wheel.
5. Run `validate-config` and one dry cycle.
6. Restart the tasks and verify a real persistent-message update.

Do not run retired environments or another game session alongside the supported
Windows installation.
