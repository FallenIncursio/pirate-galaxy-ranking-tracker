from __future__ import annotations

import ctypes
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Any

MUTEX_NAME = "Local\\PirateGalaxyRankingsTracker"
AUTOMATION_MUTEX_NAME = "Local\\PirateGalaxyAutomation"
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102


class AlreadyRunningError(RuntimeError):
    pass


class AutomationBusyError(RuntimeError):
    pass


def _kernel32() -> Any:
    api = ctypes.windll.kernel32
    api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    api.CreateMutexW.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.ReleaseMutex.argtypes = [wintypes.HANDLE]
    api.ReleaseMutex.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


def _default_lock_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "pirate-galaxy-rankings.lock"
    directory = Path(tempfile.gettempdir()) / f"pirate-galaxy-rankings-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory / "tracker.lock"


@contextmanager
def _posix_tracker_instance(lock_path: Path | None = None) -> Iterator[None]:
    import fcntl

    path = lock_path or _default_lock_path()
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunningError("another Pirate Galaxy tracker is already running") from exc
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()}\n".encode())
        yield
    finally:
        os.close(descriptor)


@contextmanager
def tracker_instance(
    *, kernel32: Any | None = None, lock_path: Path | None = None
) -> Iterator[None]:
    """Hold a platform-native singleton lock for the continuous tracker."""
    if sys.platform != "win32" and kernel32 is None:
        with _posix_tracker_instance(lock_path):
            yield
        return

    api = kernel32 or _kernel32()
    handle = api.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise OSError(api.GetLastError(), "CreateMutexW failed")
    if api.GetLastError() == ERROR_ALREADY_EXISTS:
        api.CloseHandle(handle)
        raise AlreadyRunningError("another Pirate Galaxy tracker is already running")

    try:
        yield
    finally:
        api.CloseHandle(handle)


@contextmanager
def automation_lease(
    *,
    kernel32: Any | None = None,
    lock_path: Path | None = None,
    timeout_ms: int = 1000,
) -> Iterator[None]:
    """Serialize all processes that can interact with the game window."""
    if sys.platform != "win32" and kernel32 is None:
        path = lock_path or (_default_lock_path().parent / "automation.lock")
        try:
            with _posix_tracker_instance(path):
                yield
        except AlreadyRunningError as error:
            raise AutomationBusyError("another game automation currently owns the UI") from error
        return

    api = kernel32 or _kernel32()
    handle = api.CreateMutexW(None, False, AUTOMATION_MUTEX_NAME)
    if not handle:
        raise OSError(api.GetLastError(), "CreateMutexW failed for automation lease")
    acquired = False
    try:
        result = api.WaitForSingleObject(handle, timeout_ms)
        if result == WAIT_TIMEOUT:
            raise AutomationBusyError("another game automation currently owns the UI")
        if result not in {WAIT_OBJECT_0, WAIT_ABANDONED}:
            raise OSError(result, "WaitForSingleObject failed for automation lease")
        acquired = True
        yield
    finally:
        if acquired:
            api.ReleaseMutex(handle)
        api.CloseHandle(handle)
