"""Cooperative hard wall-clock deadlines for one formal C1 task.

The deadline is stored in a context variable so every task lane gets an isolated
budget while low-level provider clients can shorten their own request timeout to
the remaining task wall clock.  ``C1TaskTimeout`` intentionally derives directly
from ``BaseException``: benchmark adapters contain many broad ``except Exception``
blocks that translate ordinary runtime failures into task-specific rows, but a
formal wall-clock stop must unwind those adapters and close their runtimes first.
The C1 lane boundary catches and persists this exception explicitly.
"""
from __future__ import annotations

import contextvars
import math
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from .c1_budgets import DEFAULT_C1_TASK_TIMEOUT_S, MAX_C1_TASK_TIMEOUT_S


__all__ = [
    "DEFAULT_C1_TASK_TIMEOUT_S",
    "MAX_C1_TASK_TIMEOUT_S",
    "C1TaskTimeout",
    "C1TaskDeadline",
    "validate_task_timeout_s",
    "c1_task_deadline",
    "current_task_deadline",
    "raise_if_task_timed_out",
    "bounded_task_timeout_s",
    "raise_task_timeout_if_expired",
]


class C1TaskTimeout(BaseException):
    """Raised when one formal C1 task reaches its wall-clock limit."""


@dataclass(frozen=True)
class C1TaskDeadline:
    task_id: str
    timeout_s: float
    started_monotonic: float
    deadline_monotonic: float

    def remaining_s(self) -> float:
        return self.deadline_monotonic - time.monotonic()

    def timeout_error(self) -> C1TaskTimeout:
        return C1TaskTimeout(
            f"C1 task {self.task_id} exceeded its {self.timeout_s:g}s wall-clock limit"
        )


_CURRENT_DEADLINE: contextvars.ContextVar[C1TaskDeadline | None] = (
    contextvars.ContextVar("warptest_c1_task_deadline", default=None)
)


def validate_task_timeout_s(value: float) -> float:
    timeout_s = float(value)
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("task_timeout_s must be a positive finite number")
    if timeout_s > MAX_C1_TASK_TIMEOUT_S:
        raise ValueError(
            f"task_timeout_s must not exceed {MAX_C1_TASK_TIMEOUT_S:g}s, got {timeout_s:g}s"
        )
    return timeout_s


@contextmanager
def c1_task_deadline(task_id: str, timeout_s: float) -> Iterator[C1TaskDeadline]:
    timeout_s = validate_task_timeout_s(timeout_s)
    started = time.monotonic()
    deadline = C1TaskDeadline(
        task_id=str(task_id),
        timeout_s=timeout_s,
        started_monotonic=started,
        deadline_monotonic=started + timeout_s,
    )
    token = _CURRENT_DEADLINE.set(deadline)
    try:
        yield deadline
    finally:
        _CURRENT_DEADLINE.reset(token)


def current_task_deadline() -> C1TaskDeadline | None:
    return _CURRENT_DEADLINE.get()


def raise_if_task_timed_out() -> None:
    deadline = current_task_deadline()
    if deadline is not None and deadline.remaining_s() <= 0:
        raise deadline.timeout_error()


def bounded_task_timeout_s(requested_s: float | None) -> float | None:
    """Cap a blocking request by the current task's remaining wall clock."""

    deadline = current_task_deadline()
    if deadline is None:
        return requested_s
    remaining = deadline.remaining_s()
    if remaining <= 0:
        raise deadline.timeout_error()
    # Avoid a zero timeout after float/SDK rounding while never extending the
    # caller's explicit transport limit.
    remaining = max(0.001, remaining)
    return remaining if requested_s is None else min(float(requested_s), remaining)


def raise_task_timeout_if_expired(error: BaseException) -> None:
    """Translate a transport timeout at the task deadline into the formal stop."""

    deadline = current_task_deadline()
    if deadline is not None and deadline.remaining_s() <= 0:
        raise deadline.timeout_error() from error
