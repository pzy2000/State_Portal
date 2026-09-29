"""Historical-suite selector adapter for fresh Luanti #11866 Gate 5--7 cells."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from .luanti_11866_gate57_native_v4 import plan_luanti_11866_gate57_native_v4
from .luanti_native_executor import (
    EXECUTION_CONFIRMATION as GENERIC_EXECUTION_CONFIRMATION,
    NativeProcessReceipt,
    NativeRegressionExecutionResult,
    execute_luanti_native_regression,
    run_bounded_process,
)
from .luanti_native_regression import NativeRegressionExecutionPlan
from .models import CandidateKey
from .pipeline import StageDecision
from .stable_protocol import ArtifactReference, AuthorizationRecord


CONFIRMATION = "EXECUTE_LUANTI_11866_GATE57_NATIVE_IRRLICHT_V5"
_CANDIDATE = CandidateKey("luanti", 11866)
_EXECUTION_NAMESPACE = "irrlicht-v5"
_V4_FIXED_BUILD = ArtifactReference(
    "native_regression_build/luanti/11866/fixed/build/irrlicht-v4",
    "native_regression_build",
    "3.0",
)
_V4_BUGGY_BUILD = ArtifactReference(
    "native_regression_build/luanti/11866/buggy/build/irrlicht-v4",
    "native_regression_build",
    "3.0",
)
_SELECTOR_BLOCKER = ArtifactReference(
    "candidate_gate57_11866_selector_blocker/luanti/11866/native/v1",
    "candidate_gate57_11866_selector_blocker",
    "1.0",
)
_SELECTOR_ADAPTER = ArtifactReference(
    "candidate_gate57_11866_selector_adapter/luanti/11866/native/v1",
    "candidate_gate57_11866_selector_adapter",
    "1.0",
)
_LEGACY_MODULE_SUMMARY = re.compile(
    rb"(?m)^======== Module ([^\r\n ]+) (passed|failed) "
    rb"\((\d+) failures / (\d+) tests\) - \d+ms\r?$"
)


def plan_luanti_11866_gate57_native_v5(
    artifact_root: str | Path, *, candidate: CandidateKey
) -> NativeRegressionExecutionPlan | StageDecision:
    """Use the v4 source/dependency closure without v4 test attempts."""

    plan = plan_luanti_11866_gate57_native_v4(artifact_root, candidate=candidate)
    if isinstance(plan, StageDecision):
        return plan
    if candidate != _CANDIDATE:
        raise ValueError("#11866 native-v5 candidate is invalid")
    return replace(
        plan,
        execution_namespace=_EXECUTION_NAMESPACE,
        dependency_references=(
            *plan.dependency_references,
            _SELECTOR_BLOCKER,
            _SELECTOR_ADAPTER,
            _V4_FIXED_BUILD,
            _V4_BUGGY_BUILD,
        ),
    )


def execute_luanti_11866_gate57_native_v5(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str = CONFIRMATION,
) -> NativeRegressionExecutionResult:
    """Run a new build/test epoch; each cell requires one named raw summary."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")

    def planner(root: Path, *, candidate: CandidateKey):
        return plan_luanti_11866_gate57_native_v5(root, candidate=candidate)

    def process_runner(
        argv: tuple[str, ...], *, cwd: Path, env: dict[str, str], timeout_seconds: int
    ) -> NativeProcessReceipt:
        if len(argv) != 4 or argv[1:3] != ("--run-unittests", "--test-module"):
            return run_bounded_process(argv, cwd=cwd, env=env, timeout_seconds=timeout_seconds)
        selector = argv[3]
        raw = run_bounded_process(
            (argv[0], "--run-unittests"), cwd=cwd, env=env, timeout_seconds=timeout_seconds
        )
        if raw.outcome != "completed" or raw.stdout_truncated or raw.stderr_truncated:
            return raw
        records = [
            match
            for stream in (raw.stdout, raw.stderr)
            for match in _LEGACY_MODULE_SUMMARY.finditer(stream)
            if match.group(1).decode("utf-8", errors="strict") == selector
        ]
        if len(records) != 1 or int(records[0].group(4)) <= 0:
            return raw
        failed = int(records[0].group(3))
        summary = (
            b"\nUnit Test Results: "
            + (b"FAILED" if failed else b"PASSED")
            + b"\n    "
            + str(1 if failed else 0).encode("ascii")
            + b" / 1 failed tests.\n"
        )
        return NativeProcessReceipt(
            "completed",
            1 if failed else 0,
            raw.stdout,
            raw.stderr + summary,
            False,
            False,
        )

    return execute_luanti_native_regression(
        artifact_root,
        candidate=_CANDIDATE,
        authorization=authorization,
        confirmation=GENERIC_EXECUTION_CONFIRMATION,
        planner=planner,
        process_runner=process_runner,
    )


__all__ = [
    "CONFIRMATION",
    "execute_luanti_11866_gate57_native_v5",
    "plan_luanti_11866_gate57_native_v5",
]
