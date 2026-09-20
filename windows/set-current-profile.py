from __future__ import annotations

import re
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.toml"
BACKUP_PATH = PROJECT_ROOT / "config.before-1002129.toml"

# Pirate Galaxy 1002129 retains 800x600 in its serialized settings but creates
# a fixed 954x610 client. These values were measured from a verified Division 1
# capture after the September 2026 maintenance update.
REPLACEMENTS = {
    "anchor": "anchor = [0.145, 0.220, 0.220, 0.040]",
    "name_x": "name_x = 0.277",
    "name_width": "name_width = 0.420",
    "score_x": "score_x = 0.745",
    "score_width": "score_width = 0.075",
    "first_row_y": "first_row_y = 0.269",
    "row_height": "row_height = 0.05245",
    "row_content_height": "row_content_height = 0.050",
    "expected_width": "expected_width = 954",
    "expected_height": "expected_height = 610",
    "menu_icon_x": "menu_icon_x = 0.450",
    "menu_icon_y": "menu_icon_y = 0.023",
    "rankings_tab_x": "rankings_tab_x = 0.295",
    "rankings_tab_y": "rankings_tab_y = 0.108",
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
    print("PROFILE_OK client=954x610 game_version=1002129")


if __name__ == "__main__":
    main()
