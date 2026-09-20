# Security policy

## Reporting a vulnerability

Do not open a public issue for a credential leak or a vulnerability that could
expose a Discord webhook, local account, or runtime data. Contact the repository
owner privately through GitHub instead.

## Sensitive local files

The following files and directories must never be committed or attached to an
issue without review:

- `secrets.env` and `.env*`
- `config.toml`
- `data/`, including Discord message state and ranking history
- `logs/`, `diagnostics/`, and `support-*.zip`
- locally captured files under `assets/recovery/`

If a Discord webhook is exposed, revoke it in Discord immediately, create a new
one, update `secrets.env`, and restart the tracker task.

The application validates Discord webhook origins, suppresses mentions, writes
state atomically, and limits recovery actions to recognized screen templates.
