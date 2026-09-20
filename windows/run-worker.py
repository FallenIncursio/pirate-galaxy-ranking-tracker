from __future__ import annotations

import ctypes
import os
import traceback
from ctypes import wintypes
from datetime import datetime
from importlib import import_module
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_LOG = PROJECT_ROOT / "logs" / "bootstrap.log"
IDLE_PRIORITY_CLASS = 0x00000040
NORMAL_PRIORITY_CLASS = 0x00000020
PROCESS_SET_INFORMATION = 0x0200


def log_bootstrap(message: str) -> None:
    BOOTSTRAP_LOG.parent.mkdir(parents=True, exist_ok=True)
    with BOOTSTRAP_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now().astimezone().isoformat()} {message}\n")


def load_runtime_environment() -> None:
    secrets_path = PROJECT_ROOT / "secrets.env"
    if not secrets_path.is_file():
        raise RuntimeError("secrets.env is missing; run windows\\install.ps1 first")
    for raw_line in secrets_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()

    candidates = (
        Path(os.environ.get("PROGRAMFILES", "")) / "Tesseract-OCR" / "tesseract.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Tesseract-OCR" / "tesseract.exe",
    )
    tesseract = next((candidate for candidate in candidates if candidate.is_file()), None)
    if tesseract is None:
        raise RuntimeError("Tesseract was not found; run windows\\install.ps1 again")
    os.environ["TESSERACT_CMD"] = str(tesseract)


def game_process_ids() -> tuple[int, ...]:
    user32 = ctypes.windll.user32
    process_ids: set[int] = set()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd: int, _lparam: int) -> bool:
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if "pirategalaxy version:" not in buffer.value.casefold():
            return True
        process_id = wintypes.DWORD()
        if user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id)):
            process_ids.add(process_id.value)
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return tuple(sorted(process_ids))


def set_process_priority(process_ids: tuple[int, ...], priority_class: int) -> int:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetPriorityClass.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    updated = 0
    for process_id in process_ids:
        process = kernel32.OpenProcess(PROCESS_SET_INFORMATION, False, process_id)
        if not process:
            continue
        try:
            updated += int(bool(kernel32.SetPriorityClass(process, priority_class)))
        finally:
            kernel32.CloseHandle(process)
    return updated


if __name__ == "__main__":
    try:
        log_bootstrap("headless worker started")
        load_runtime_environment()
        log_bootstrap("runtime environment loaded")
        game_ids = game_process_ids()
        lowered = set_process_priority(game_ids, IDLE_PRIORITY_CLASS)
        log_bootstrap(f"lowered game priority for bootstrap: {lowered} process(es)")
        try:
            for module_name in (
                "pg_rankings.config",
                "pg_rankings.discord_webhook",
                "pg_rankings.capture",
                "pg_rankings.ocr",
                "pg_rankings.service",
                "pg_rankings.cli",
            ):
                log_bootstrap(f"importing {module_name}")
                import_module(module_name)
                log_bootstrap(f"imported {module_name}")
        finally:
            restored = set_process_priority(game_ids, NORMAL_PRIORITY_CLASS)
            log_bootstrap(f"restored game priority after bootstrap: {restored} process(es)")
        from pg_rankings.cli import main

        log_bootstrap("tracker main starting")
        exit_code = main(["--config", str(PROJECT_ROOT / "config.toml"), "run"])
        log_bootstrap(f"tracker main exited with {exit_code}")
        raise SystemExit(exit_code)
    except Exception:
        log_bootstrap("bootstrap failed\n" + traceback.format_exc())
        raise
