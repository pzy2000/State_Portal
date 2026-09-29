"""Fresh #11866 Gate 5--7 native epoch with a candidate-owned IrrlichtMt closure."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

from .luanti_11866_cxx17_override import load_luanti_11866_cxx17_override
from .luanti_11866_irrlicht_dependency import (
    load_luanti_11866_irrlicht_dependency,
)
from .luanti_native_executor import (
    EXECUTION_CONFIRMATION as GENERIC_EXECUTION_CONFIRMATION,
    NativeRegressionExecutionResult,
    execute_luanti_native_regression,
)
from .luanti_native_regression import (
    NativeRegressionExecutionPlan,
    plan_luanti_native_regression_execution,
)
from .models import CandidateKey
from .pipeline import StageDecision
from .stable_protocol import AuthorizationRecord


CONFIRMATION = "EXECUTE_LUANTI_11866_GATE57_NATIVE_IRRLICHT_V3"
_CANDIDATE = CandidateKey("luanti", 11866)
_EXECUTION_NAMESPACE = "irrlicht-v3"


def plan_luanti_11866_gate57_native(
    artifact_root: str | Path, *, candidate: CandidateKey
) -> NativeRegressionExecutionPlan | StageDecision:
    """Bind the registered headers and isolate build/attempt receipt IDs."""

    if candidate != _CANDIDATE:
        raise ValueError("#11866 native plan candidate is invalid")
    root = Path(artifact_root).absolute().resolve(strict=True)
    plan = plan_luanti_native_regression_execution(root, candidate=candidate)
    if isinstance(plan, StageDecision):
        return plan
    dependency = load_luanti_11866_irrlicht_dependency(root)
    override = load_luanti_11866_cxx17_override(root)
    include = root / PurePosixPath(dependency.payload["include_path"])
    launcher = root / PurePosixPath(override.payload["wrapper_path"])
    if not include.is_dir() or not (include / "irrlicht.h").is_file():
        raise RuntimeError(
            "#11866 candidate-owned IrrlichtMt include closure is unavailable"
        )
    if not launcher.is_file() or launcher.is_symlink():
        raise RuntimeError("#11866 candidate-owned C++17 launcher is unavailable")
    configure = (
        *plan.configure_argv,
        "-DCMAKE_CXX_STANDARD=17",
        "-DCMAKE_CXX_STANDARD_REQUIRED=TRUE",
        "-DCMAKE_CXX_EXTENSIONS=OFF",
        f"-DCMAKE_CXX_COMPILER_LAUNCHER={launcher}",
        f"-DIRRLICHT_INCLUDE_DIR={include}",
    )
    return replace(
        plan,
        configure_argv=configure,
        execution_namespace=_EXECUTION_NAMESPACE,
        dependency_references=(dependency.reference, override.reference),
    )


def execute_luanti_11866_gate57_native(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str = CONFIRMATION,
) -> NativeRegressionExecutionResult:
    """Execute one fresh 12-cell native epoch; no paid/formal actions."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")

    def planner(root: Path, *, candidate: CandidateKey):
        return plan_luanti_11866_gate57_native(root, candidate=candidate)

    return execute_luanti_native_regression(
        artifact_root,
        candidate=_CANDIDATE,
        authorization=authorization,
        confirmation=GENERIC_EXECUTION_CONFIRMATION,
        planner=planner,
    )


__all__ = [
    "CONFIRMATION",
    "execute_luanti_11866_gate57_native",
    "plan_luanti_11866_gate57_native",
]
