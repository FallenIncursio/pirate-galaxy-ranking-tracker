from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_EXACT = {
    ".env",
    "config.toml",
    "secrets.env",
}
FORBIDDEN_PARTS = {"__pycache__", "data", "diagnostics", "logs"}
FORBIDDEN_SUFFIXES = {".db", ".log", ".pyc", ".sqlite3"}
SECRET_PATTERNS = {
    "Discord webhook": re.compile(
        rb"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{20,}"
    ),
    "GitHub token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
LOCAL_PATTERNS = {
    "local home path": re.compile(rb"/home/" + rb"arc/"),
    "private IPv4 address": re.compile(
        rb"\b(?:10\.\d+|192\.168\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+)\.\d+\b"
    ),
}
LOCAL_PATTERN_EXEMPTIONS = {PurePosixPath("windows/harden-network.ps1")}


def tracked_files() -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return tuple(ROOT / path.decode("utf-8") for path in result.stdout.split(b"\0") if path)


def repository_violations(files: tuple[Path, ...]) -> list[str]:
    violations: list[str] = []
    for path in files:
        relative = PurePosixPath(path.relative_to(ROOT).as_posix())
        lowered_parts = {part.casefold() for part in relative.parts}
        if relative.name.casefold() in FORBIDDEN_EXACT:
            violations.append(f"{relative}: local configuration or credential file is tracked")
        if lowered_parts & FORBIDDEN_PARTS:
            violations.append(f"{relative}: runtime directory is tracked")
        if relative.suffix.casefold() in FORBIDDEN_SUFFIXES:
            violations.append(f"{relative}: runtime/generated file type is tracked")
        if relative.parts[:2] == ("assets", "recovery") and relative.suffix.casefold() == ".png":
            violations.append(f"{relative}: local recovery template is tracked")
        if relative.name.startswith("support-") and relative.suffix.casefold() == ".zip":
            violations.append(f"{relative}: support bundle is tracked")

        try:
            content = path.read_bytes()
        except OSError as error:
            violations.append(f"{relative}: could not inspect file: {error}")
            continue
        if b"\0" in content:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                violations.append(f"{relative}: possible {label} detected")
        if relative not in LOCAL_PATTERN_EXEMPTIONS:
            for label, pattern in LOCAL_PATTERNS.items():
                if pattern.search(content):
                    violations.append(f"{relative}: {label} detected")
    return violations


def main() -> int:
    violations = repository_violations(tracked_files())
    if violations:
        print("Repository safety check failed:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1
    print("Repository safety check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
