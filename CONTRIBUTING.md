# Contributing

The repository is private while its first release is reviewed. Open an issue
before making a broad behavioral change so capture safety and compatibility can
be discussed first.

## Development setup

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
Copy-Item config.example.toml config.toml
```

Use only placeholder webhooks and synthetic ranking data in tests, screenshots,
logs, and issue reports.

## Checks

```powershell
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\python.exe -m pytest --cov=pg_rankings --cov-report=term-missing
.\.venv\Scripts\python.exe scripts\check-repository.py
```

Run `ruff format .` before committing. Never commit `config.toml`,
`secrets.env`, runtime databases, logs, Discord message state, support bundles,
diagnostic screenshots, or locally captured recovery templates.

## Pull requests

- Keep changes focused and document user-visible behavior in `CHANGELOG.md`.
- Add or update tests for changed logic.
- Preserve fail-closed behavior for unknown windows and OCR uncertainty.
- Verify recovery changes on a non-production account before deployment.
