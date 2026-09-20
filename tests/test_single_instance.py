from __future__ import annotations

import pytest

from pg_rankings.single_instance import (
    AUTOMATION_MUTEX_NAME,
    ERROR_ALREADY_EXISTS,
    MUTEX_NAME,
    WAIT_OBJECT_0,
    WAIT_TIMEOUT,
    AlreadyRunningError,
    AutomationBusyError,
    automation_lease,
    tracker_instance,
)


class FakeKernel32:
    def __init__(
        self,
        *,
        handle: int = 42,
        last_error: int = 0,
        wait_result: int = WAIT_OBJECT_0,
    ) -> None:
        self.handle = handle
        self.last_error = last_error
        self.created: list[tuple[object, bool, str]] = []
        self.closed: list[int] = []
        self.wait_result = wait_result
        self.waits: list[tuple[int, int]] = []
        self.released: list[int] = []

    def CreateMutexW(self, security: object, initial_owner: bool, name: str) -> int:
        self.created.append((security, initial_owner, name))
        return self.handle

    def GetLastError(self) -> int:
        return self.last_error

    def CloseHandle(self, handle: int) -> None:
        self.closed.append(handle)

    def WaitForSingleObject(self, handle: int, timeout_ms: int) -> int:
        self.waits.append((handle, timeout_ms))
        return self.wait_result

    def ReleaseMutex(self, handle: int) -> None:
        self.released.append(handle)


def test_tracker_instance_holds_and_releases_the_named_mutex() -> None:
    kernel32 = FakeKernel32()

    with tracker_instance(kernel32=kernel32):
        assert kernel32.created == [(None, False, MUTEX_NAME)]
        assert kernel32.closed == []

    assert kernel32.closed == [42]


def test_tracker_instance_rejects_a_second_tracker() -> None:
    kernel32 = FakeKernel32(last_error=ERROR_ALREADY_EXISTS)

    with pytest.raises(AlreadyRunningError), tracker_instance(kernel32=kernel32):
        raise AssertionError("duplicate tracker must not enter the context")

    assert kernel32.closed == [42]


def test_tracker_instance_reports_mutex_creation_failure() -> None:
    kernel32 = FakeKernel32(handle=0, last_error=5)

    with pytest.raises(OSError, match="CreateMutexW failed"), tracker_instance(kernel32=kernel32):
        raise AssertionError("failed mutex creation must not enter the context")


def test_tracker_instance_rejects_a_second_posix_lock(tmp_path) -> None:
    lock_path = tmp_path / "tracker.lock"

    with (
        tracker_instance(lock_path=lock_path),
        pytest.raises(AlreadyRunningError),
        tracker_instance(lock_path=lock_path),
    ):
        raise AssertionError("duplicate tracker must not enter the context")

    with tracker_instance(lock_path=lock_path):
        assert lock_path.read_text(encoding="utf-8").strip().isdigit()


def test_automation_lease_holds_and_releases_named_mutex() -> None:
    kernel32 = FakeKernel32()

    with automation_lease(kernel32=kernel32, timeout_ms=2500):
        assert kernel32.created == [(None, False, AUTOMATION_MUTEX_NAME)]
        assert kernel32.waits == [(42, 2500)]
        assert kernel32.released == []

    assert kernel32.released == [42]
    assert kernel32.closed == [42]


def test_automation_lease_rejects_a_busy_ui() -> None:
    kernel32 = FakeKernel32(wait_result=WAIT_TIMEOUT)

    with pytest.raises(AutomationBusyError), automation_lease(kernel32=kernel32):
        raise AssertionError("busy automation lease must not be entered")

    assert kernel32.released == []
    assert kernel32.closed == [42]
