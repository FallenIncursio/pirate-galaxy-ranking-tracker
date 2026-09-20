from __future__ import annotations

import re
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.toml"
BACKUP_PATH = PROJECT_ROOT / "config.before-800.toml"

REPLACEMENTS = {
    "anchor": "anchor = [0.090, 0.228, 0.200, 0.043]",
    "name_x": "name_x = 0.225",
    "name_width": "name_width = 0.550",
    # Keep the blue row-action icon and the left table border out of the OCR
    # crop. Both introduce digit-like strokes at the native 800x600 layout.
    "score_x": "score_x = 0.795",
    "score_width": "score_width = 0.085",
    "first_row_y": "first_row_y = 0.280",
    "row_height": "row_height = 0.050",
    "row_content_height": "row_content_height = 0.050",
    "expected_width": "expected_width = 800",
    "expected_height": "expected_height = 600",
    "menu_icon_x": "menu_icon_x = 0.450",
    "menu_icon_y": "menu_icon_y = 0.023",
    "rankings_tab_x": "rankings_tab_x = 0.285",
    "rankings_tab_y": "rankings_tab_y = 0.125",
}


def main() -> None:
    if not CONFIG_PATH.is_file():
        raise RuntimeError(f"configuration not found: {CONFIG_PATH}")
    if not BACKUP_PATH.exists():
        shutil.copy2(CONFIG_PATH, BACKUP_PATH)

    content = CONFIG_PATH.read_text(encoding="utf-8")
    for key, replacement in REPLACEMENTS.items():
        pattern = rf"(?m)^{re.escape(key)}\s*=.*$"
        content, count = re.subn(pattern, replacement, content)
        if count != 1:
            raise RuntimeError(f"expected exactly one {key!r} setting, found {count}")
    CONFIG_PATH.write_text(content, encoding="utf-8")
    print("PROFILE_OK client=800x600 backup=config.before-800.toml")


if __name__ == "__main__":
    main()
