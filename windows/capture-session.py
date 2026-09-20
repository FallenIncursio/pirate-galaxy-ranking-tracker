from __future__ import annotations

import argparse
from pathlib import Path

from PIL import ImageGrab

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "session-current.png"


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture the active Windows console session")
    parser.add_argument("output", nargs="?", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    ImageGrab.grab(all_screens=True).convert("RGB").save(arguments.output)
    print(f"SESSION_CAPTURE_OK path={arguments.output}")


if __name__ == "__main__":
    main()
