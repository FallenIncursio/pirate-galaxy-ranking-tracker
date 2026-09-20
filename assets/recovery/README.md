# Local recovery templates

PNG files in this directory are machine- and client-version-specific and are
ignored by Git. Recovery remains disabled until the required templates have
been captured and verified locally.

Supported filenames:

- `login-button.png`
- `login-button-v<client-version>.png`
- `server-selection.png`
- `enter-game-button.png`
- `play-button.png`
- `restart-required.png`
- `raven-dynamics-event-title-v<client-version>.png`
- `support-energy-close-v<client-version>.png`
- `support-energy-close-default-v<client-version>.png`

Use the smallest stable crop that uniquely identifies the intended control.
Do not use an entire screen or dialog as a clickable template. Validate new
templates in `[recovery].shadow_mode = true` before allowing actions.

These files may contain third-party artwork and runtime/player information.
Keep them local and review them before sharing a support bundle.
