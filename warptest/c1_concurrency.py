"""Deterministic inner-lane scheduling and auditable C1 concurrency metadata."""
from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .c1_budgets import C1_STEP_BUDGET


# Lane balancing weights the expected cost of a task by its decision-step budget,
# which is uniform, so every task carries the same weight.
_TASK_WEIGHT = C1_STEP_BUDGET
READY_SCHEDULER_POLICY = "global_resource_ready_round_robin_v2"
READY_SCHEDULER_SCHEMA = 1
INNER_SCHEDULER_SCHEMA = 2


class C1TaskDeferred(RuntimeError):
    """Internal control flow for an atomic task returned to the ready queue."""


class C1TaskDispatchCancelled(RuntimeError):
    """A claimed task was cancelled before its treatment started."""


@dataclass(frozen=True)
class LaneAssignment:
    lane_id: int
    task: Any
    manifest_ordinal: int
    dispatch_ordinal: int
    source_repo: Path
    weight: int

    def metadata(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane_id,
            "task_id": str(self.task.id),
            "benchmark": str(self.task.benchmark),
            "manifest_ordinal": self.manifest_ordinal,
            "dispatch_ordinal": self.dispatch_ordinal,
            "source_repo": str(self.source_repo),
            "weight": self.weight,
        }


@dataclass(frozen=True)
class ReadyTaskRegistration:
    task: Any
    manifest_ordinal: int
    dispatch_ordinal: int
    source_repo: Path
    weight: int
    preferred_lane_id: int | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "task_id": str(self.task.id),
            "benchmark": str(self.task.benchmark),
            "manifest_ordinal": self.manifest_ordinal,
            "dispatch_ordinal": self.dispatch_ordinal,
            "source_repo": str(self.source_repo),
            "weight": self.weight,
            "preferred_lane_id": self.preferred_lane_id,
        }


def build_ready_task_plan(
    tasks: Sequence[Any],
    *,
    repo_for_task: Callable[[Any], Path],
) -> list[ReadyTaskRegistration]:
    """Build the canonical game-interleaved priority order without fixing lanes."""

    indexed = [
        (index, task, repo_for_task(task).resolve())
        for index, task in enumerate(tasks)
    ]
    groups: OrderedDict[str, list[tuple[int, Any, Path]]] = OrderedDict()
    for item in indexed:
        groups.setdefault(str(item[2]), []).append(item)
    interleaved: list[tuple[int, Any, Path]] = []
    offset = 0
    while True:
        added = False
        for rows in groups.values():
            if offset < len(rows):
                interleaved.append(rows[offset])
                added = True
        if not added:
            break
        offset += 1
    registrations: list[ReadyTaskRegistration] = []
    for dispatch_ordinal, (manifest_ordinal, task, repo) in enumerate(interleaved):
        registrations.append(
            ReadyTaskRegistration(
                task=task,
                manifest_ordinal=manifest_ordinal,
                dispatch_ordinal=dispatch_ordinal,
                source_repo=repo,
                weight=_TASK_WEIGHT,
            )
        )
    return registrations


def retry_ready_invocation_id(
    invocation_id: str,
    registrations: Sequence[ReadyTaskRegistration],
    prior_attempt_counts: dict[str, int],
) -> str:
    """Give an in-process retry its own deterministic scheduler namespace."""

    normalized_id = str(invocation_id)
    counts = sorted(
        (str(task_id), int(count))
        for task_id, count in prior_attempt_counts.items()
    )
    if not counts:
        return normalized_id
    if any(count < 1 for _, count in counts):
        raise ValueError("C1 retry attempt counts must be positive")
    task_ids = [str(registration.task.id) for registration in registrations]
    digest_input = "\n".join(
        [
            normalized_id,
            *(f"task:{task_id}" for task_id in task_ids),
            *(f"attempt:{task_id}:{count}" for task_id, count in counts),
        ]
    )
    retry_digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:16]
    return f"{normalized_id}-retry-{retry_digest}"


def build_lane_plan(
    tasks: Sequence[Any],
    *,
    workers: int,
    repo_for_task: Callable[[Any], Path],
) -> list[list[LaneAssignment]]:
    """Interleave games, then greedily balance immutable weighted lanes."""

    if workers < 1:
        raise ValueError("C1 inner workers must be at least 1")
    registrations = build_ready_task_plan(tasks, repo_for_task=repo_for_task)
    lane_count = min(workers, max(1, len(tasks)))
    lanes: list[list[LaneAssignment]] = [[] for _ in range(lane_count)]
    loads = [0] * lane_count
    for registration in registrations:
        lane_id = min(range(lane_count), key=lambda value: (loads[value], value))
        assignment = LaneAssignment(
            lane_id=lane_id,
            task=registration.task,
            manifest_ordinal=registration.manifest_ordinal,
            dispatch_ordinal=registration.dispatch_ordinal,
            source_repo=registration.source_repo,
            weight=registration.weight,
        )
        lanes[lane_id].append(assignment)
        loads[lane_id] += registration.weight
    return lanes


def _resource_metadata(resource: str) -> dict[str, str]:
    kind = str(resource).split(":", 1)[0] or "resource"
    return {
        "resource_kind": kind,
        "resource_sha256": hashlib.sha256(
            str(resource).encode("utf-8")
        ).hexdigest()[:16],
    }


@dataclass
class C1ReadyTicket:
    invocation_id: str
    outer_ordinal: int
    registration: ReadyTaskRegistration
    resources: tuple[str, ...] = ()
    state: str = "pending"
    claimed_lane_id: int | None = None
    first_busy_monotonic_ns: int | None = None
    next_retry_monotonic_ns: int = 0
    defer_count: int = 0
    admitted_monotonic_ns: int | None = None
    completed_monotonic_ns: int | None = None
    global_start_ordinal: int | None = None
    global_completion_ordinal: int | None = None
    queue_timed_out: bool = False
    blocked_resources: dict[tuple[str, str], int] = field(default_factory=dict)

    @property
    def task_id(self) -> str:
        return str(self.registration.task.id)

    @property
    def order_key(self) -> tuple[int, int, str, str]:
        return (
            int(self.outer_ordinal),
            int(self.registration.dispatch_ordinal),
            str(self.invocation_id),
            self.task_id,
        )

    def queue_wait_ms(self, *, now_ns: int | None = None) -> int:
        if self.first_busy_monotonic_ns is None:
            return 0
        terminal = (
            self.admitted_monotonic_ns
            or self.completed_monotonic_ns
            or now_ns
            or time.monotonic_ns()
        )
        return max(0, int((terminal - self.first_busy_monotonic_ns) / 1_000_000))

    def queue_payload(self) -> dict[str, Any]:
        return {
            "resource_queue": {
                "policy": READY_SCHEDULER_POLICY,
                "wait_ms": self.queue_wait_ms(),
                "defer_count": int(self.defer_count),
                "first_busy_monotonic_ns": self.first_busy_monotonic_ns,
                "admitted_monotonic_ns": self.admitted_monotonic_ns,
                "timed_out": bool(self.queue_timed_out),
                "blocked_resources": [
                    {
                        "resource_kind": kind,
                        "resource_sha256": digest,
                        "observations": count,
                    }
                    for (kind, digest), count in sorted(
                        self.blocked_resources.items()
                    )
                ],
            }
        }

    def registration_metadata(self) -> dict[str, Any]:
        return {
            **self.registration.metadata(),
            "outer_ordinal": int(self.outer_ordinal),
            "resources": [_resource_metadata(item) for item in self.resources],
        }

    def dispatch_metadata(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "dispatch_ordinal": self.registration.dispatch_ordinal,
            "lane_id": self.claimed_lane_id,
            "global_start_ordinal": self.global_start_ordinal,
            "admitted_monotonic_ns": self.admitted_monotonic_ns,
            "global_completion_ordinal": self.global_completion_ordinal,
            "completed_monotonic_ns": self.completed_monotonic_ns,
            "status": self.state,
            **self.queue_payload(),
        }


class C1ReadyScheduler:
    """Process-wide, resource-aware admission for active C1 invocations."""

    def __init__(
        self,
        *,
        wait_timeout_s: float,
        initial_backoff_s: float = 0.05,
        maximum_backoff_s: float = 1.0,
    ) -> None:
        if wait_timeout_s <= 0:
            raise ValueError("C1 ready scheduler wait timeout must be positive")
        self.wait_timeout_s = float(wait_timeout_s)
        self.initial_backoff_s = float(initial_backoff_s)
        self.maximum_backoff_s = float(maximum_backoff_s)
        self._condition = threading.Condition()
        self._tickets: dict[str, list[C1ReadyTicket]] = {}
        self._outer_ordinals: dict[str, int] = {}
        self._worker_limits: dict[str, int] = {}
        self._cancelled: set[str] = set()
        self._dispatch_closed: dict[str, str] = {}
        self._waiting: dict[str, int] = {}
        self._held_resources: dict[str, C1ReadyTicket] = {}
        self._round_robin_after: tuple[int, str] | None = None
        self._global_started = 0
        self._global_completed = 0
        self._active = 0
        self._max_active = 0
        self._active_by_invocation: dict[str, int] = {}
        self._max_active_by_invocation: dict[str, int] = {}
        self._resume_history: list[dict[str, Any]] = []

    def restore_provenance(self, snapshot: dict[str, Any]) -> None:
        """Preserve terminal/interrupted scheduler evidence across same-root resume."""

        if (
            snapshot.get("schema_version") != READY_SCHEDULER_SCHEMA
            or snapshot.get("policy") != READY_SCHEDULER_POLICY
            or float(snapshot.get("wait_timeout_s", -1))
            != self.wait_timeout_s
        ):
            raise ValueError(
                "Cannot resume C1 with incompatible resource scheduler provenance"
            )
        with self._condition:
            if self._tickets or self._resume_history:
                raise ValueError(
                    "C1 resource scheduler provenance may only be restored once"
                )
            prior_history = snapshot.get("resume_history")
            if prior_history is not None and (
                not isinstance(prior_history, list)
                or not all(isinstance(item, dict) for item in prior_history)
            ):
                raise ValueError(
                    "Cannot resume C1 with malformed resource scheduler history"
                )
            self._resume_history.extend(
                dict(item) for item in (prior_history or [])
            )
            terminal = dict(snapshot)
            terminal.pop("resume_history", None)
            self._resume_history.append(terminal)
            self._global_started = max(
                0, int(snapshot.get("max_started_ordinal", -1)) + 1
            )
            self._global_completed = max(
                0, int(snapshot.get("max_completion_ordinal", -1)) + 1
            )
            self._max_active = max(
                0, int(snapshot.get("max_active_tasks", 0))
            )

    def register_invocation(
        self,
        invocation_id: str,
        *,
        outer_ordinal: int,
        registrations: Sequence[ReadyTaskRegistration],
        resources_for_task: Callable[[Any], Sequence[str]],
        worker_limit: int | None = None,
    ) -> "C1ReadyHandle":
        normalized_id = str(invocation_id)
        tickets = [
            C1ReadyTicket(
                invocation_id=normalized_id,
                outer_ordinal=int(outer_ordinal),
                registration=registration,
                resources=tuple(
                    sorted(
                        {
                            str(resource)
                            for resource in resources_for_task(
                                registration.task
                            )
                        }
                    )
                ),
            )
            for registration in registrations
        ]
        effective_worker_limit = (
            max(1, len(tickets)) if worker_limit is None else int(worker_limit)
        )
        if effective_worker_limit < 1:
            raise ValueError("C1 ready scheduler worker_limit must be at least 1")
        with self._condition:
            existing = self._tickets.get(normalized_id)
            if existing is not None:
                existing_metadata = [
                    ticket.registration_metadata() for ticket in existing
                ]
                new_metadata = [
                    ticket.registration_metadata() for ticket in tickets
                ]
                if (
                    self._outer_ordinals.get(normalized_id) != int(outer_ordinal)
                    or self._worker_limits.get(normalized_id)
                    != effective_worker_limit
                    or existing_metadata != new_metadata
                ):
                    raise ValueError(
                        "C1 ready scheduler invocation registration changed"
                    )
            else:
                self._tickets[normalized_id] = tickets
                self._outer_ordinals[normalized_id] = int(outer_ordinal)
                self._worker_limits[normalized_id] = effective_worker_limit
                self._active_by_invocation[normalized_id] = 0
                self._max_active_by_invocation[normalized_id] = 0
            self._condition.notify_all()
        return C1ReadyHandle(self, normalized_id)

    def _resource_fifo_blockers(self, ticket: C1ReadyTicket) -> list[str]:
        blockers: list[str] = []
        for resource in ticket.resources:
            if resource in self._held_resources:
                blockers.append(resource)
                continue
            contenders = [
                item
                for rows in self._tickets.values()
                for item in rows
                if item.state == "pending" and resource in item.resources
            ]
            if contenders and min(contenders, key=lambda item: item.order_key) is not ticket:
                blockers.append(resource)
        return blockers

    def _note_logical_busy(
        self,
        ticket: C1ReadyTicket,
        resources: Sequence[str],
        *,
        now_ns: int,
    ) -> None:
        if ticket.first_busy_monotonic_ns is None:
            ticket.first_busy_monotonic_ns = now_ns
        ticket.defer_count += 1
        for resource in resources:
            metadata = _resource_metadata(resource)
            key = (
                metadata["resource_kind"],
                metadata["resource_sha256"],
            )
            ticket.blocked_resources[key] = (
                ticket.blocked_resources.get(key, 0) + 1
            )
        delay = min(
            self.maximum_backoff_s,
            self.initial_backoff_s * (2 ** min(ticket.defer_count - 1, 8)),
        )
        ticket.next_retry_monotonic_ns = now_ns + int(delay * 1_000_000_000)

    def _ready_ticket(
        self, invocation_id: str, now_ns: int
    ) -> C1ReadyTicket | None:
        pending = [
            ticket
            for ticket in self._tickets.get(invocation_id, [])
            if ticket.state == "pending"
        ]
        for ticket in sorted(
            pending, key=lambda item: item.registration.dispatch_ordinal
        ):
            if (
                ticket.first_busy_monotonic_ns is not None
                and now_ns - ticket.first_busy_monotonic_ns
                >= int(self.wait_timeout_s * 1_000_000_000)
            ):
                ticket.queue_timed_out = True
                return ticket
            if ticket.next_retry_monotonic_ns > now_ns:
                continue
            blockers = self._resource_fifo_blockers(ticket)
            if not blockers:
                return ticket
            self._note_logical_busy(ticket, blockers, now_ns=now_ns)
        return None

    def _selected_waiting_invocation(self, now_ns: int) -> str | None:
        candidates = [
            invocation_id
            for invocation_id, count in self._waiting.items()
            if count > 0
            and invocation_id not in self._cancelled
            and invocation_id not in self._dispatch_closed
            and self._ready_ticket(invocation_id, now_ns) is not None
        ]
        if not candidates:
            return None
        ordered = sorted(
            candidates,
            key=lambda value: (self._outer_ordinals[value], value),
        )
        if self._round_robin_after is None:
            return ordered[0]
        for invocation_id in ordered:
            key = (self._outer_ordinals[invocation_id], invocation_id)
            if key > self._round_robin_after:
                return invocation_id
        return ordered[0]

    def claim(
        self,
        invocation_id: str,
        *,
        lane_id: int,
    ) -> C1ReadyTicket | None:
        with self._condition:
            self._waiting[invocation_id] = self._waiting.get(invocation_id, 0) + 1
            try:
                while True:
                    rows = self._tickets.get(invocation_id)
                    if rows is None:
                        raise ValueError(
                            "C1 ready scheduler invocation is not registered"
                        )
                    worker_limit = self._worker_limits[invocation_id]
                    if not 0 <= int(lane_id) < worker_limit:
                        raise ValueError(
                            "C1 ready scheduler lane_id exceeds worker_limit"
                        )
                    if invocation_id in self._cancelled:
                        return None
                    if invocation_id in self._dispatch_closed:
                        return None
                    if not any(
                        ticket.state in {"pending", "claimed", "running"}
                        for ticket in rows
                    ):
                        return None
                    now_ns = time.monotonic_ns()
                    selected = self._selected_waiting_invocation(now_ns)
                    if selected == invocation_id:
                        ticket = self._ready_ticket(invocation_id, now_ns)
                        if ticket is not None:
                            ticket.state = "claimed"
                            ticket.claimed_lane_id = int(lane_id)
                            if not ticket.queue_timed_out:
                                for resource in ticket.resources:
                                    self._held_resources[resource] = ticket
                            self._round_robin_after = (
                                self._outer_ordinals[invocation_id],
                                invocation_id,
                            )
                            return ticket
                    retry_ns = [
                        ticket.next_retry_monotonic_ns
                        for ticket in rows
                        if ticket.state == "pending"
                        and ticket.next_retry_monotonic_ns > now_ns
                    ]
                    timeout_s = 0.5
                    if retry_ns:
                        timeout_s = min(
                            timeout_s,
                            max(0.001, (min(retry_ns) - now_ns) / 1_000_000_000),
                        )
                    self._condition.wait(timeout=timeout_s)
            finally:
                self._waiting[invocation_id] -= 1

    def _release_resources(self, ticket: C1ReadyTicket) -> None:
        for resource in ticket.resources:
            if self._held_resources.get(resource) is ticket:
                self._held_resources.pop(resource, None)

    def defer(
        self,
        ticket: C1ReadyTicket,
        *,
        resource_kind: str,
        resource_sha256: str,
    ) -> None:
        with self._condition:
            if ticket.state != "claimed":
                raise ValueError("Only a claimed C1 ticket may be deferred")
            self._release_resources(ticket)
            now_ns = time.monotonic_ns()
            if ticket.first_busy_monotonic_ns is None:
                ticket.first_busy_monotonic_ns = now_ns
            ticket.defer_count += 1
            key = (str(resource_kind), str(resource_sha256))
            ticket.blocked_resources[key] = ticket.blocked_resources.get(key, 0) + 1
            delay = min(
                self.maximum_backoff_s,
                self.initial_backoff_s * (2 ** min(ticket.defer_count - 1, 8)),
            )
            ticket.next_retry_monotonic_ns = now_ns + int(delay * 1_000_000_000)
            ticket.claimed_lane_id = None
            if ticket.invocation_id in self._dispatch_closed:
                ticket.state = "cancelled"
            else:
                ticket.state = "pending"
            self._condition.notify_all()

    def admit(self, ticket: C1ReadyTicket, *, lane_id: int) -> None:
        with self._condition:
            if ticket.state != "claimed" or ticket.queue_timed_out:
                raise ValueError("C1 ticket cannot be admitted from its current state")
            ticket.claimed_lane_id = int(lane_id)
            ticket.admitted_monotonic_ns = time.monotonic_ns()
            ticket.global_start_ordinal = self._global_started
            self._global_started += 1
            self._active += 1
            self._max_active = max(self._max_active, self._active)
            invocation_id = ticket.invocation_id
            invocation_active = self._active_by_invocation[invocation_id] + 1
            self._active_by_invocation[invocation_id] = invocation_active
            self._max_active_by_invocation[invocation_id] = max(
                self._max_active_by_invocation[invocation_id],
                invocation_active,
            )
            ticket.state = "running"
            self._condition.notify_all()

    def complete(self, ticket: C1ReadyTicket, *, status: str = "complete") -> None:
        with self._condition:
            if ticket.state not in {"claimed", "running"}:
                raise ValueError("C1 ticket cannot complete from its current state")
            self._release_resources(ticket)
            ticket.completed_monotonic_ns = time.monotonic_ns()
            if ticket.state == "running":
                ticket.global_completion_ordinal = self._global_completed
                self._global_completed += 1
                self._active -= 1
                invocation_id = ticket.invocation_id
                self._active_by_invocation[invocation_id] -= 1
            ticket.state = str(status)
            self._condition.notify_all()

    def close_pending_dispatch(self, invocation_id: str, *, reason: str) -> None:
        """Stop new claims while allowing already-dispatched work to settle."""

        normalized_id = str(invocation_id)
        normalized_reason = str(reason).strip()
        if not normalized_reason:
            raise ValueError("C1 dispatch close reason must be non-empty")
        with self._condition:
            if normalized_id not in self._tickets:
                raise ValueError("C1 ready scheduler invocation is not registered")
            self._dispatch_closed.setdefault(normalized_id, normalized_reason)
            for ticket in self._tickets[normalized_id]:
                if ticket.state == "pending":
                    self._release_resources(ticket)
                    ticket.state = "cancelled"
            self._condition.notify_all()

    def cancel_invocation(self, invocation_id: str) -> None:
        with self._condition:
            normalized_id = str(invocation_id)
            self._cancelled.add(normalized_id)
            self._dispatch_closed.setdefault(
                normalized_id, "invocation cancelled"
            )
            for ticket in self._tickets.get(normalized_id, []):
                if ticket.state in {"pending", "claimed"}:
                    self._release_resources(ticket)
                    ticket.state = "cancelled"
            self._condition.notify_all()

    def invocation_snapshot(self, invocation_id: str) -> dict[str, Any]:
        with self._condition:
            tickets = self._tickets.get(str(invocation_id), [])
            return {
                "schema_version": INNER_SCHEDULER_SCHEMA,
                "lane_policy": READY_SCHEDULER_POLICY,
                "worker_limit": self._worker_limits.get(str(invocation_id), 1),
                "dispatch_closed": str(invocation_id) in self._dispatch_closed,
                "dispatch_close_reason": self._dispatch_closed.get(
                    str(invocation_id)
                ),
                "active_tasks": self._active_by_invocation.get(
                    str(invocation_id), 0
                ),
                "max_active_tasks": self._max_active_by_invocation.get(
                    str(invocation_id), 0
                ),
                "task_registration": [
                    ticket.registration_metadata() for ticket in tickets
                ],
                "dispatch_history": [
                    ticket.dispatch_metadata()
                    for ticket in tickets
                    if ticket.admitted_monotonic_ns is not None
                    or ticket.queue_timed_out
                    or (
                        ticket.state == "cancelled"
                        and ticket.claimed_lane_id is not None
                    )
                ],
            }

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            tickets = [
                ticket
                for invocation_id in sorted(
                    self._tickets,
                    key=lambda value: (self._outer_ordinals[value], value),
                )
                for ticket in self._tickets[invocation_id]
            ]
            return {
                "schema_version": READY_SCHEDULER_SCHEMA,
                "policy": READY_SCHEDULER_POLICY,
                "wait_timeout_s": self.wait_timeout_s,
                "initial_backoff_s": self.initial_backoff_s,
                "maximum_backoff_s": self.maximum_backoff_s,
                "registered_invocations": len(self._tickets),
                "registered_tasks": len(tickets),
                "active_tasks": self._active,
                "max_active_tasks": self._max_active,
                "max_started_ordinal": self._global_started - 1,
                "max_completion_ordinal": self._global_completed - 1,
                "resume_history": list(self._resume_history),
                "invocations": {
                    invocation_id: self.invocation_snapshot(invocation_id)
                    for invocation_id in self._tickets
                },
            }


@dataclass(frozen=True)
class C1ReadyHandle:
    scheduler: C1ReadyScheduler
    invocation_id: str

    def claim(self, *, lane_id: int) -> C1ReadyTicket | None:
        return self.scheduler.claim(self.invocation_id, lane_id=lane_id)

    def defer(
        self,
        ticket: C1ReadyTicket,
        *,
        resource_kind: str,
        resource_sha256: str,
    ) -> None:
        self.scheduler.defer(
            ticket,
            resource_kind=resource_kind,
            resource_sha256=resource_sha256,
        )

    def admit(self, ticket: C1ReadyTicket, *, lane_id: int) -> None:
        self.scheduler.admit(ticket, lane_id=lane_id)

    def complete(self, ticket: C1ReadyTicket, *, status: str = "complete") -> None:
        self.scheduler.complete(ticket, status=status)

    def cancel(self) -> None:
        self.scheduler.cancel_invocation(self.invocation_id)

    def close_pending_dispatch(self, *, reason: str) -> None:
        self.scheduler.close_pending_dispatch(
            self.invocation_id,
            reason=reason,
        )

    def snapshot(self) -> dict[str, Any]:
        return self.scheduler.invocation_snapshot(self.invocation_id)


class ConcurrencyTracker:
    """Thread-safe ordinals and overlap counters for one invocation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = 0
        self._completed = 0
        self._active = 0
        self._max_active = 0

    def start(self) -> dict[str, Any]:
        with self._lock:
            ordinal = self._started
            self._started += 1
            self._active += 1
            self._max_active = max(self._max_active, self._active)
        return {
            "start_ordinal": ordinal,
            "started_monotonic_ns": time.monotonic_ns(),
        }

    def finish(self) -> dict[str, Any]:
        finished = time.monotonic_ns()
        with self._lock:
            ordinal = self._completed
            self._completed += 1
            self._active -= 1
        return {
            "completion_ordinal": ordinal,
            "completed_monotonic_ns": finished,
        }

    @property
    def max_active(self) -> int:
        with self._lock:
            return self._max_active


class C1GlobalStop:
    """One first-writer-wins stop reason shared across outer invocations."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None

    def request(self, reason: str) -> str:
        normalized = str(reason).strip()
        if not normalized:
            raise ValueError("C1 global stop reason must be non-empty")
        with self._lock:
            if self._reason is None:
                self._reason = normalized
            self._event.set()
            return self._reason

    def is_set(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason


class C1ReadyInvocationLifecycle:
    """Shared ready-lane state transitions for one GUI or State invocation."""

    def __init__(
        self,
        *,
        component: str,
        handle: C1ReadyHandle,
        requested_workers: int,
        fallback_workers: int,
        atomic_defer: bool,
        serialized_unity_benchmarks: Sequence[str],
        global_stop_event: C1GlobalStop | threading.Event | None,
    ) -> None:
        self.component = str(component)
        self.handle = handle
        self.requested_workers = int(requested_workers)
        self.atomic_defer = bool(atomic_defer)
        self.serialized_unity_benchmarks = tuple(serialized_unity_benchmarks)
        self.global_stop_event = global_stop_event
        self.tracker = ConcurrencyTracker()
        self._start_stop_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._cancellation_reason: str | None = None

        snapshot = handle.snapshot()
        self.effective_workers = int(
            snapshot.get("worker_limit", int(fallback_workers))
        )
        if not 1 <= self.effective_workers <= self.requested_workers:
            raise ValueError(
                f"C1 {self.component} ready worker limit is invalid"
            )

    def lane_ids(
        self, initial_ticket: C1ReadyTicket | None
    ) -> tuple[int | None, list[int]]:
        """Validate an atomic ticket or return every local worker lane."""

        if initial_ticket is None:
            return None, list(range(self.effective_workers))
        lane_id = initial_ticket.claimed_lane_id
        if (
            not isinstance(lane_id, int)
            or isinstance(lane_id, bool)
            or not 0 <= lane_id < self.effective_workers
        ):
            raise ValueError(
                f"C1 {self.component} atomic ticket lane is invalid"
            )
        return lane_id, [lane_id]

    def metadata(self) -> dict[str, Any]:
        payload = self.handle.snapshot()
        payload.update(
            requested_inner_workers=self.requested_workers,
            effective_inner_workers=self.effective_workers,
            max_active_tasks=max(
                self.tracker.max_active,
                int(payload.get("max_active_tasks", 0) or 0),
            ),
            cancelled=self._stop_event.is_set(),
            cancellation_reason=self._cancellation_reason,
            unity_serialized=bool(self.serialized_unity_benchmarks),
            unity_serialized_benchmarks=list(
                self.serialized_unity_benchmarks
            ),
        )
        return payload

    def admit(
        self, ticket: C1ReadyTicket, *, lane_id: int
    ) -> dict[str, Any] | None:
        """Atomically start tracking a ticket unless its invocation stopped."""

        with self._start_stop_lock:
            if self._stop_event.is_set():
                return None
            if (
                self.global_stop_event is not None
                and self.global_stop_event.is_set()
            ):
                self._cancellation_reason = "global C1 stop requested"
                self._stop_event.set()
                return None
            timing = self.tracker.start()
            self.handle.admit(ticket, lane_id=lane_id)
            return timing

    def stop(self, reason: str) -> None:
        """Publish the first stop reason before another lane starts work."""

        with self._start_stop_lock:
            if not self._stop_event.is_set():
                self._cancellation_reason = str(reason)
            self._stop_event.set()
        if self.atomic_defer:
            self.handle.close_pending_dispatch(reason=reason)
        else:
            self.handle.cancel()

    def publish_global_unknown_stop(self) -> None:
        if self.global_stop_event is None:
            return
        request = getattr(self.global_stop_event, "request", None)
        if callable(request):
            request("permanently unknown provider usage")
        else:
            self.global_stop_event.set()

    def complete_task(self, ticket: C1ReadyTicket) -> dict[str, Any]:
        completion = self.tracker.finish()
        self.handle.complete(ticket)
        return completion

    def complete_claimed(self, ticket: C1ReadyTicket, *, status: str) -> None:
        self.handle.complete(ticket, status=status)

    def fail_task(self, ticket: C1ReadyTicket, *, admitted: bool) -> None:
        if admitted:
            self.tracker.finish()
        self.handle.complete(ticket, status="error")

    def defer_task(
        self,
        ticket: C1ReadyTicket,
        *,
        resource_kind: str,
        resource_sha256: str,
    ) -> bool:
        self.handle.defer(
            ticket,
            resource_kind=resource_kind,
            resource_sha256=resource_sha256,
        )
        return self.atomic_defer

    def cancel_task(self, ticket: C1ReadyTicket, *, task_id: str) -> bool:
        """Settle a cancelled claim; return false for atomic caller propagation."""

        if self.atomic_defer:
            return False
        # Another lane may have stopped the invocation after this lane claimed
        # a ticket but before it admitted the task. cancel_invocation() already
        # settles that claimed ticket, so completing it again would turn a
        # normal sibling cancellation into a ValueError at the outer layer.
        if ticket.state != "cancelled":
            self.handle.complete(ticket, status="cancelled")
        self.stop(
            self._cancellation_reason or f"task {task_id} dispatch cancelled"
        )
        return True


def scheduler_metadata(
    lanes: Sequence[Sequence[LaneAssignment]],
    *,
    requested_workers: int,
    tracker: ConcurrencyTracker | None = None,
    cancelled: bool = False,
    cancellation_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "requested_inner_workers": int(requested_workers),
        "effective_inner_workers": len(lanes),
        "lane_policy": "game_interleaved_weighted_v1",
        "lanes": [[item.metadata() for item in lane] for lane in lanes],
        "max_active_tasks": tracker.max_active if tracker is not None else 0,
        "cancelled": bool(cancelled),
        "cancellation_reason": cancellation_reason,
    }
