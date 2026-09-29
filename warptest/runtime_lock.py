"""Cross-thread and cross-process leases for mutable benchmark runtimes.

Benchmark source checkouts are intentionally shared between P5 model jobs.  Most
runtime adapters already place their data under a report directory, but several
engines still touch checkout-owned build or instrumentation state.  A lease per
resolved repository keeps those critical sections isolated without serialising
unrelated benchmark families.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import fcntl


RUNTIME_LOCK_DIR_ENV = "WARPTEST_RUNTIME_LOCK_DIR"
RUNTIME_LOCK_WAIT_TIMEOUT_ENV = "WARPTEST_RUNTIME_LOCK_WAIT_TIMEOUT_S"
DEFAULT_RUNTIME_LOCK_WAIT_TIMEOUT_S = 3.0 * 60.0 * 60.0

_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_LEASE_LOCAL = threading.local()
_LOCK_WAIT_LEDGER: contextvars.ContextVar[list[dict[str, Any]] | None] = (
    contextvars.ContextVar("warptest_runtime_lock_wait_ledger", default=None)
)


class RuntimeLockWaitTimeout(RuntimeError):
    """A backend resource queue exceeded its independent infrastructure limit."""

    infrastructure_error = True
    usage_accounting_complete = True


class RuntimeResourceBusy(RuntimeError):
    """A non-blocking lease attempt found an already-owned resource."""

    infrastructure_error = False
    usage_accounting_complete = True

    def __init__(self, resource_kind: str, resource_sha256: str) -> None:
        self.resource_kind = str(resource_kind)
        self.resource_sha256 = str(resource_sha256)
        super().__init__(f"C1 resource is busy: {self.resource_kind}")


def validate_runtime_lock_wait_timeout_s(value: float) -> float:
    timeout_s = float(value)
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("runtime_lock.wait_timeout_s must be a positive finite number")
    return timeout_s


def runtime_lock_wait_timeout_s() -> float:
    configured = os.environ.get(RUNTIME_LOCK_WAIT_TIMEOUT_ENV)
    if configured is None or not configured.strip():
        return DEFAULT_RUNTIME_LOCK_WAIT_TIMEOUT_S
    try:
        return validate_runtime_lock_wait_timeout_s(float(configured))
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{RUNTIME_LOCK_WAIT_TIMEOUT_ENV} must be a positive finite number"
        ) from error


@contextlib.contextmanager
def runtime_lock_wait_ledger() -> Iterator[list[dict[str, Any]]]:
    """Collect lock-queue evidence for one trial without crossing task lanes."""

    current = _LOCK_WAIT_LEDGER.get()
    if current is not None:
        yield current
        return
    records: list[dict[str, Any]] = []
    token = _LOCK_WAIT_LEDGER.set(records)
    try:
        yield records
    finally:
        _LOCK_WAIT_LEDGER.reset(token)


def runtime_lock_wait_payload(
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    waits = [dict(item) for item in records]
    return {
        "runtime_lock_wait_ms": sum(
            max(0, int(item.get("wait_ms") or 0)) for item in waits
        ),
        "runtime_lock_wait_timeout_s": runtime_lock_wait_timeout_s(),
        "runtime_lock_waits": waits,
    }


def _record_lock_wait(
    *,
    path: Path,
    resource_kind: str,
    wait_ms: int,
    wait_timeout_s: float,
    status: str,
) -> None:
    ledger = _LOCK_WAIT_LEDGER.get()
    if ledger is None:
        return
    ledger.append(
        {
            "resource_kind": str(resource_kind),
            "resource_sha256": hashlib.sha256(str(path).encode("utf-8")).hexdigest()[
                :16
            ],
            "wait_ms": max(0, int(wait_ms)),
            "wait_timeout_s": float(wait_timeout_s),
            "status": str(status),
        }
    )


def runtime_lock_dir() -> Path:
    """Return the process-wide lease directory without touching a checkout."""

    configured = os.environ.get(RUNTIME_LOCK_DIR_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(tempfile.gettempdir()) / "warptest-runtime-locks"


def runtime_lock_path(repo: str | Path, *, lock_dir: str | Path | None = None) -> Path:
    """Build a stable, collision-resistant lock path for one repository."""

    resolved_repo = Path(repo).expanduser().resolve()
    root = (
        Path(lock_dir).expanduser().resolve()
        if lock_dir is not None
        else runtime_lock_dir()
    )
    digest = hashlib.sha256(str(resolved_repo).encode("utf-8")).hexdigest()[:16]
    safe_name = (
        "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in resolved_repo.name)
        or "repo"
    )
    return root / f"{safe_name}-{digest}.lock"


@contextlib.contextmanager
def exclusive_file_lease(
    path: str | Path,
    *,
    resource_kind: str,
    wait_timeout_s: float | None = None,
    blocking: bool = True,
) -> Iterator[Path]:
    """Acquire one re-entrant file lease using an infrastructure-only budget."""

    lock_path = Path(path).expanduser().resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    timeout_s = validate_runtime_lock_wait_timeout_s(
        runtime_lock_wait_timeout_s() if wait_timeout_s is None else wait_timeout_s
    )
    key = (str(lock_path.parent), lock_path.name)
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())

    started = time.monotonic()
    wait_deadline = started + timeout_s
    remaining = max(0.0, wait_deadline - time.monotonic())
    acquired_thread = (
        thread_lock.acquire(blocking=False)
        if not blocking
        else thread_lock.acquire(timeout=remaining)
    )
    if not acquired_thread:
        if not blocking:
            raise RuntimeResourceBusy(
                resource_kind,
                hashlib.sha256(str(lock_path).encode("utf-8")).hexdigest()[:16],
            )
        wait_ms = int((time.monotonic() - started) * 1000)
        _record_lock_wait(
            path=lock_path,
            resource_kind=resource_kind,
            wait_ms=wait_ms,
            wait_timeout_s=timeout_s,
            status="timeout",
        )
        raise RuntimeLockWaitTimeout(
            f"runtime lock service unavailable: {resource_kind} queue exceeded "
            f"its independent {timeout_s:g}s wait limit"
        )
    try:
        pid = os.getpid()
        held_pid = getattr(_LEASE_LOCAL, "pid", None)
        if held_pid != pid:
            # A forked child must never inherit the parent's logical recursion
            # state. It has to contend on flock as a distinct process.
            _LEASE_LOCAL.pid = pid
            _LEASE_LOCAL.depths = {}
        depths: dict[tuple[str, str], int] = getattr(_LEASE_LOCAL, "depths", {})
        if key in depths:
            depths[key] += 1
            _record_lock_wait(
                path=lock_path,
                resource_kind=resource_kind,
                wait_ms=int((time.monotonic() - started) * 1000),
                wait_timeout_s=timeout_s,
                status="reentrant",
            )
            try:
                yield lock_path
            finally:
                depths[key] -= 1
            return

        with lock_path.open("a+", encoding="utf-8") as handle:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not blocking:
                        raise RuntimeResourceBusy(
                            resource_kind,
                            hashlib.sha256(
                                str(lock_path).encode("utf-8")
                            ).hexdigest()[:16],
                        )
                    remaining = wait_deadline - time.monotonic()
                    if remaining <= 0:
                        wait_ms = int((time.monotonic() - started) * 1000)
                        _record_lock_wait(
                            path=lock_path,
                            resource_kind=resource_kind,
                            wait_ms=wait_ms,
                            wait_timeout_s=timeout_s,
                            status="timeout",
                        )
                        raise RuntimeLockWaitTimeout(
                            "runtime lock service unavailable: "
                            f"{resource_kind} queue exceeded its independent "
                            f"{timeout_s:g}s wait limit"
                        )
                    threading.Event().wait(min(0.05, remaining))
            depths[key] = 1
            _record_lock_wait(
                path=lock_path,
                resource_kind=resource_kind,
                wait_ms=int((time.monotonic() - started) * 1000),
                wait_timeout_s=timeout_s,
                status="acquired",
            )
            try:
                yield lock_path
            finally:
                depths.pop(key, None)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        thread_lock.release()


@contextlib.contextmanager
def runtime_lease(
    repo: str | Path,
    *,
    lock_dir: str | Path | None = None,
    wait_timeout_s: float | None = None,
    blocking: bool = True,
) -> Iterator[Path]:
    """Exclusively lease one benchmark checkout for a runtime invocation.

    ``flock`` protects distinct P5 processes, while the in-process lock covers
    parallel model workers (where platform flock semantics alone are not enough
    to provide a thread-level critical section). Queue time is governed by a
    separate infrastructure limit and is never charged to a C1 task deadline.
    """

    path = runtime_lock_path(repo, lock_dir=lock_dir)
    with exclusive_file_lease(
        path,
        resource_kind="checkout-runtime",
        wait_timeout_s=wait_timeout_s,
        blocking=blocking,
    ):
        yield path
