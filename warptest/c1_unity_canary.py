"""No-provider Unity concurrency canary for isolated APFS project clones."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .c1_sandbox import C1SandboxManager, git_fingerprint
from .c1_unity_runtime import create_c1_unity_session


UNITY_BENCHMARKS = {"jynew", "pal3"}
UNITY_SERIAL_LEASE_ROOT = Path("/private/tmp/warptest-c1-unity-serial")
# Kept as a compatibility alias for readers that only know the historical
# process-wide boolean. New task admission always uses the benchmark-specific
# target returned by ``unity_serial_lease_target``.
UNITY_SERIAL_LEASE_TARGET = UNITY_SERIAL_LEASE_ROOT


def unity_serial_lease_target(benchmark: str) -> Path:
    """Return the machine-shared fallback lease for one Unity benchmark."""

    normalized = str(benchmark).strip().lower()
    if normalized not in UNITY_BENCHMARKS:
        raise ValueError(f"unsupported Unity serial benchmark: {benchmark}")
    return UNITY_SERIAL_LEASE_ROOT / normalized


def classify_unity_concurrency_canaries(
    canaries: Mapping[str, Mapping[str, Any]],
) -> tuple[list[str], list[str]]:
    """Return safe per-backend fallbacks and failures that must block paid work."""

    serialized: list[str] = []
    blocking: list[str] = []
    for benchmark, row in sorted(canaries.items()):
        normalized = str(benchmark).strip().lower()
        if normalized not in UNITY_BENCHMARKS:
            blocking.append(normalized)
            continue
        if row.get("passed") is True:
            continue
        if (
            normalized == "jynew"
            and row.get("editor_overlap_passed") is True
            and row.get("mutable_storage_isolated") is False
        ):
            serialized.append(normalized)
            continue
        blocking.append(normalized)
    return serialized, blocking

# An APFS project clone isolates checkout-owned Unity state, but it does not
# change Application.persistentDataPath. Jynew's Easy Save backend writes its
# archive and atomic ``.tmp`` file there, so two otherwise-independent Editor
# processes can race on the same user-level file. Keep the overlap probe as
# evidence that the Editors themselves are clone-safe, but fail the parallel
# capability check until the mutable save backend is isolated as well.
PARALLEL_MUTABLE_STORAGE_ISOLATED = {
    "jynew": False,
    "pal3": True,
}


def run_unity_concurrency_canary(
    *,
    benchmark: str,
    source_repo: str | Path,
    manager: C1SandboxManager,
    report_dir: str | Path,
    timeout_s: float = 900.0,
) -> dict[str, Any]:
    """Launch two real Editors on two clones and prove overlapping sessions."""

    if benchmark not in UNITY_BENCHMARKS:
        raise ValueError(f"unsupported Unity canary benchmark: {benchmark}")
    source = Path(source_repo).expanduser().resolve()
    out_dir = Path(report_dir).expanduser().resolve() / benchmark
    out_dir.mkdir(parents=True, exist_ok=True)
    before = git_fingerprint(source)
    deadline = time.monotonic() + timeout_s
    barrier = threading.Barrier(2)
    rows: list[dict[str, Any]] = []
    rows_lock = threading.Lock()

    def lane(lane_id: int) -> dict[str, Any]:
        row: dict[str, Any] = {
            "lane_id": lane_id,
            "status": "failed",
            "started_monotonic_ns": None,
            "ready_monotonic_ns": None,
            "completed_monotonic_ns": None,
            "pid": None,
        }
        try:
            with manager.task_repo(
                source,
                invocation_id=f"unity-canary-{benchmark}",
                lane_id=lane_id,
                task_id=f"{benchmark}-canary-{lane_id}",
                allow_source_fallback=False,
            ) as sandbox:
                row["sandbox"] = sandbox.metadata()
                if sandbox.mode != "apfs_cow_clone":
                    raise RuntimeError(
                        "Unity concurrency canary requires two successful APFS clones: "
                        + str(sandbox.clone_error)
                    )
                row["started_monotonic_ns"] = time.monotonic_ns()
                with create_c1_unity_session(
                    benchmark,
                    sandbox.repo,
                    out_dir / f"lane-{lane_id}",
                    experiment_seed=lane_id,
                ) as runtime:
                    row["pid"] = runtime.hold_pid
                    clean = runtime.clean_entry()
                    row["clean_entry"] = clean
                    if clean.get("status") != "success":
                        raise RuntimeError(f"Unity clean entry failed: {clean}")
                    row["ready_monotonic_ns"] = time.monotonic_ns()
                    # Cloning and Editor startup can differ substantially by
                    # lane. Keep the ready Editor alive within the canary's
                    # remaining budget so a slower peer can prove real overlap.
                    barrier.wait(timeout=max(0.0, deadline - time.monotonic()))
                    time.sleep(0.5)
                    row["status"] = "passed"
        except BaseException as error:  # noqa: BLE001 - evidence, not propagation
            row["error"] = f"{type(error).__name__}: {error}"
            try:
                barrier.abort()
            except Exception:
                pass
        finally:
            row["completed_monotonic_ns"] = time.monotonic_ns()
            with rows_lock:
                rows.append(row)
        return row

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(lane, lane_id) for lane_id in range(2)]
        for future in futures:
            future.result(timeout=max(0.0, deadline - time.monotonic()))
    ordered = sorted(rows, key=lambda row: int(row["lane_id"]))
    overlap_ns = 0
    if len(ordered) == 2 and all(row.get("ready_monotonic_ns") for row in ordered):
        overlap_ns = max(
            0,
            min(int(row["completed_monotonic_ns"]) for row in ordered)
            - max(int(row["ready_monotonic_ns"]) for row in ordered),
        )
    pids = [row.get("pid") for row in ordered if row.get("pid") is not None]
    after = git_fingerprint(source)
    editor_overlap_passed = bool(
        len(ordered) == 2
        and all(row.get("status") == "passed" for row in ordered)
        and len(set(pids)) == 2
        and overlap_ns > 0
        and before == after
    )
    mutable_storage_isolated = PARALLEL_MUTABLE_STORAGE_ISOLATED[benchmark]
    passed = editor_overlap_passed and mutable_storage_isolated
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "benchmark": benchmark,
        "source_repo": str(source),
        "source_fingerprint_before": before,
        "source_fingerprint_after": after,
        "source_unchanged": before == after,
        "overlap_ns": overlap_ns,
        "distinct_pids": len(set(pids)) == 2,
        "editor_overlap_passed": editor_overlap_passed,
        "mutable_storage_isolated": mutable_storage_isolated,
        "parallel_rejection_reason": (
            None
            if mutable_storage_isolated
            else "Application.persistentDataPath is shared across APFS project clones"
        ),
        "sandboxes_cleaned": all(
            not any(
                manager.run_root.glob(
                    f"unity-canary-{benchmark}/lane-{lane_id}/{benchmark}-canary-{lane_id}-*"
                )
            )
            for lane_id in range(2)
        ),
        "results": ordered,
        "passed": passed,
    }
    output = out_dir / "unity-concurrency-canary.json"
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(output)
    return {**payload, "report": str(output)}
