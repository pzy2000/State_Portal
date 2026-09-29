"""Pre-deadline resource acquisition for one formal C1 trial."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Collection, Iterator

from .c1_unity_canary import UNITY_BENCHMARKS, unity_serial_lease_target
from .native_input import native_runtime_lock
from .runtime_lock import runtime_lease


# These formal C1 runtimes use PID/session-scoped background input transports.
# Their benchmark names must never imply Quartz focus or a process-wide lock.
BACKGROUND_INPUT_BENCHMARKS = {
    "luanti",
    "minetest",
    "openra",
    "jynew",
    "pal3",
}

LEGACY_NATIVE_INPUT_LOCK_TARGET = Path("/tmp/warptest-c1-luanti.lock")
UNITY_LICENSE_LOCK_TARGET = Path("/tmp/warptest-c1-unity-license.lock")


@dataclass(frozen=True)
class C1TaskResourceConfig:
    """One source of truth for scheduler labels and execution-time leases."""

    benchmark: str
    logical_resources: tuple[str, ...]
    unity_lease_targets: tuple[Path, ...]
    native_input_lock_target: Path | None


def normalize_unity_serialized_benchmarks(
    *,
    unity_serialized: bool,
    unity_serialized_benchmarks: Collection[str] | None,
) -> frozenset[str]:
    if unity_serialized_benchmarks is None:
        # Old reports and direct callers only supplied one boolean. Preserve
        # that interpretation at the API edge without using it in new runs.
        return frozenset(UNITY_BENCHMARKS if unity_serialized else ())
    normalized = frozenset(
        str(item).strip().lower() for item in unity_serialized_benchmarks
    )
    unknown = normalized - UNITY_BENCHMARKS
    if unknown:
        raise ValueError(
            "unknown serialized Unity benchmarks: " + ", ".join(sorted(unknown))
        )
    return normalized


def canonical_unity_serialized_benchmarks(
    *,
    unity_serialized: bool,
    unity_serialized_benchmarks: Collection[str] | None,
) -> tuple[str, ...]:
    """Return the deterministic API-edge representation used in reports."""

    return tuple(
        sorted(
            normalize_unity_serialized_benchmarks(
                unity_serialized=unity_serialized,
                unity_serialized_benchmarks=unity_serialized_benchmarks,
            )
        )
    )


def c1_task_resource_config(
    benchmark: str,
    *,
    unity_serialized: bool = False,
    unity_serialized_benchmarks: Collection[str] | None = None,
    foreground_required: bool = False,
) -> C1TaskResourceConfig:
    """Describe the machine-shared resources actually used by one task.

    Background Luanti, OpenRA and Unity adapters deliberately contribute no
    native-input resource. ``foreground_required`` is an explicit compatibility
    opt-in for the remaining Quartz/desktop-input paths.
    """

    normalized = str(benchmark).strip().lower()
    serialized = normalize_unity_serialized_benchmarks(
        unity_serialized=unity_serialized,
        unity_serialized_benchmarks=unity_serialized_benchmarks,
    )
    unity_targets = (
        (UNITY_LICENSE_LOCK_TARGET,)
        if normalized in UNITY_BENCHMARKS
        else ()
    ) + (
        (unity_serial_lease_target(normalized),)
        if normalized in serialized
        else ()
    )
    native_target = LEGACY_NATIVE_INPUT_LOCK_TARGET if foreground_required else None
    logical = [f"unity-serial:{target}" for target in unity_targets]
    if native_target is not None:
        logical.append(f"native-backend:{native_target}")
    return C1TaskResourceConfig(
        benchmark=normalized,
        logical_resources=tuple(logical),
        unity_lease_targets=unity_targets,
        native_input_lock_target=native_target,
    )


@contextlib.contextmanager
def c1_task_resource_locks(
    repo: str | Path,
    benchmark: str,
    *,
    unity_serialized: bool = False,
    unity_serialized_benchmarks: Collection[str] | None = None,
    foreground_required: bool = False,
    blocking: bool = True,
) -> Iterator[None]:
    """Acquire every backend lease before the 1200-second task clock starts."""

    config = c1_task_resource_config(
        benchmark,
        unity_serialized=unity_serialized,
        unity_serialized_benchmarks=unity_serialized_benchmarks,
        foreground_required=foreground_required,
    )
    with contextlib.ExitStack() as stack:
        for target in config.unity_lease_targets:
            stack.enter_context(
                runtime_lease(target)
                if blocking
                else runtime_lease(target, blocking=False)
            )
        stack.enter_context(
            runtime_lease(repo)
            if blocking
            else runtime_lease(repo, blocking=False)
        )
        if config.native_input_lock_target is not None:
            stack.enter_context(
                native_runtime_lock(config.native_input_lock_target)
                if blocking
                else native_runtime_lock(
                    config.native_input_lock_target,
                    blocking=False,
                )
            )
        yield


def c1_task_logical_resources(
    benchmark: str,
    *,
    unity_serialized: bool = False,
    unity_serialized_benchmarks: Collection[str] | None = None,
    foreground_required: bool = False,
) -> tuple[str, ...]:
    """Return predictable machine-shared resources used for ready scheduling."""

    return c1_task_resource_config(
        benchmark,
        unity_serialized=unity_serialized,
        unity_serialized_benchmarks=unity_serialized_benchmarks,
        foreground_required=foreground_required,
    ).logical_resources
