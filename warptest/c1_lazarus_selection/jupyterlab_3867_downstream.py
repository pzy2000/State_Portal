"""Candidate-specific pending execution plan for JupyterLab 3867.

This module never installs packages, builds source, starts a browser, or runs
candidate code.  It records the exact offline runtime gap and the future 3x
execution DAG without converting that downstream work into a selection gate.
"""

from __future__ import annotations

import os
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = CandidateKey("jupyterlab", 3867)
_DEPENDENCY_ID = "jupyterlab-node5-karma1.7.1-webpack2-typescript2.6.2"
_RUNTIME_ROOT = (
    "private/dependencies/jupyterlab-3867/node5-karma1.7.1-webpack2-typescript2.6.2/1.0"
)
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/jupyterlab/3867/fixed-buggy/v1",
    "candidate_materialization",
    "1.0",
)
_RUNTIME = ArtifactReference(
    f"dependency_preparation/{_DEPENDENCY_ID}/current/v1",
    "dependency_preparation",
    "1.0",
)
_PRIOR_PLAN = ArtifactReference(
    "candidate_downstream_plan/jupyterlab/3867/preannotation/v1",
    "candidate_downstream_plan",
    "1.0",
)
_REFERENCE = ArtifactReference(
    "candidate_downstream_execution_plan/jupyterlab/3867/preannotation/v2",
    "candidate_downstream_execution_plan",
    "2.0",
)
_REQUIRED_TREE_FILES = (
    "jupyterlab/staging/yarn.js",
    "yarn.lock",
    "tests/package.json",
    "tests/karma.conf.js",
    "tests/webpack.config.js",
    "tests/test-coreutils/src/url.spec.ts",
    "packages/coreutils/src/url.ts",
)
_EXTRA_PENDING = (
    "complete_offline_lockfile_tarball_closure",
    "candidate_specific_mocha_grep_karma_wrapper",
    "registered_firefox_binary_and_launcher_environment",
)


def write_3867_downstream_pending_plan(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Write a new v2 pending plan while preserving every v1 artifact."""

    _require_authorization(authorization)
    root = _ordinary_directory(
        Path(artifact_root).absolute(), Path(artifact_root).absolute()
    )
    materialization = _load(root, _MATERIALIZATION)
    runtime = _load(root, _RUNTIME)
    prior = _load(root, _PRIOR_PLAN)
    fixed, buggy = _validate_inputs(root, materialization, runtime, prior)
    free_mib = shutil.disk_usage(root).free // (1024 * 1024)
    pending = sorted(
        {
            *(
                value
                for value in runtime.payload.get("missing_components", [])
                if type(value) is str and value
            ),
            *_EXTRA_PENDING,
        }
    )
    payload: dict[str, object] = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": "jupyterlab-3867-islocal-qualified-url-v1",
        "state": "downstream_pending",
        "terminal": False,
        "candidate_lifecycle": "active",
        "candidate_execution_performed": False,
        "execution_authorized": False,
        "runtime_dependency_id": _DEPENDENCY_ID,
        "runtime_root": _RUNTIME_ROOT,
        "offline_runtime_buildable": False,
        "fixed_tree_path": fixed.relative_to(root).as_posix(),
        "buggy_tree_path": buggy.relative_to(root).as_posix(),
        "captured_package_manager": {
            "name": "yarn",
            "version": "1.3.2",
            "relative_path": "jupyterlab/staging/yarn.js",
        },
        "candidate_specific_workspace_root": (
            "private/downstream-runs/jupyterlab/3867/v2"
        ),
        "pending_components": pending,
        "repetition_contract": {
            "fixed_f2p": 3,
            "buggy_f2p": 3,
            "fixed_p2p": 3,
            "buggy_p2p": 3,
        },
        "selectors": {
            "f2p": (
                "@jupyterlab/coreutils URLExt .isLocal() "
                "should test whether the url is a local url"
            ),
            "p2p": (
                "@jupyterlab/coreutils URLExt .parse() "
                "should parse a url into a URLExt object"
            ),
        },
        "future_execution_dag": [
            "typed_reload_materialization_runtime_and_plan",
            "validate_candidate_specific_runtime_tuple",
            "copy_fixed_and_buggy_to_independent_workspaces",
            "offline_frozen_install_from_registered_tarballs",
            "build_selected_coreutils_test_harness",
            "fixed_f2p_three_repetitions",
            "buggy_f2p_three_repetitions",
            "fixed_p2p_three_repetitions",
            "buggy_p2p_three_repetitions",
            "persist_and_reload_each_attempt",
            "version_replay_oracle_golden_construction_pending",
        ],
        "future_argv_templates": {
            "offline_install": [
                f"{_RUNTIME_ROOT}/bin/node",
                "jupyterlab/staging/yarn.js",
                "install",
                "--offline",
                "--frozen-lockfile",
            ],
            "build_tests": [
                f"{_RUNTIME_ROOT}/bin/node",
                "jupyterlab/staging/yarn.js",
                "run",
                "build:test",
            ],
            "selected_test": [
                f"{_RUNTIME_ROOT}/bin/node",
                "tests/node_modules/karma/bin/karma",
                "start",
                "tests/karma.conf.js",
                "--browsers",
                "Firefox",
                "--single-run",
            ],
        },
        "watchdog_seconds": {"install": 900, "build": 1800, "test": 180},
        "failure_semantics": (
            "write_typed_attempt_and_remain_downstream_pending_never_terminal"
        ),
        "disk_budget_mib": {
            "plan_write": 1,
            "runtime_closure_upper": 1536,
            "fixed_buggy_workspaces_upper": 4096,
            "attempt_outputs_upper": 512,
            "future_peak": 6144,
            "minimum_free_before_execution": 57344,
            "observed_free": free_mib,
        },
        "disk_ready_for_plan_write": free_mib >= 1,
        "disk_ready_for_future_execution": free_mib >= 57344,
        "next_action": "supply_and_register_complete_offline_historical_runtime",
        "complete": True,
    }
    write_stable_artifact(
        root,
        artifact_id=_REFERENCE.artifact_id,
        artifact_kind=_REFERENCE.artifact_kind,
        semantic_version=_REFERENCE.semantic_version,
        created_at=_now(),
        inputs=(materialization.reference, runtime.reference, prior.reference),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _REFERENCE)
    if (
        artifact.inputs
        != (
            materialization.reference,
            runtime.reference,
            prior.reference,
        )
        or artifact.payload != payload
    ):
        raise RuntimeError("JupyterLab 3867 downstream plan typed reload mismatch")
    return artifact


def _validate_inputs(
    root: Path,
    materialization: StableArtifact,
    runtime: StableArtifact,
    prior: StableArtifact,
) -> tuple[Path, Path]:
    candidate = _CANDIDATE.to_value()
    if (
        materialization.payload.get("candidate") != candidate
        or materialization.payload.get("candidate_lifecycle") != "active"
        or materialization.payload.get("complete") is not True
        or materialization.payload.get("runtime_dependency_id") != _DEPENDENCY_ID
        or materialization.payload.get("runtime_root") != _RUNTIME_ROOT
        or runtime.payload.get("dependency_id") != _DEPENDENCY_ID
        or runtime.payload.get("runtime_root") != _RUNTIME_ROOT
        or runtime.payload.get("candidate_lifecycle") != "active"
        or runtime.payload.get("outcome") != "pending"
        or runtime.payload.get("offline_runtime_buildable") is not False
        or runtime.payload.get("terminal") is not False
        or prior.payload.get("candidate") != candidate
        or prior.payload.get("candidate_lifecycle") != "active"
        or prior.payload.get("readiness_state") != "runtime_dependency_pending"
        or prior.payload.get("terminal") is not False
    ):
        raise RuntimeError("JupyterLab 3867 downstream inputs are invalid")
    trees: list[Path] = []
    for variant in ("fixed", "buggy"):
        relative = materialization.payload.get(f"{variant}_tree_path")
        expected = f"private/materialized/jupyterlab/3867/v1/{variant}"
        if relative != expected:
            raise RuntimeError("JupyterLab 3867 materialized tree identity is invalid")
        tree = _ordinary_directory(root / PurePosixPath(relative), root)
        for required in _REQUIRED_TREE_FILES:
            _ordinary_file(tree / PurePosixPath(required), tree)
        trees.append(tree)
    return trees[0], trees[1]


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _ordinary_directory(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("required directory is missing or unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("required directory is missing or unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("required file is missing or unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("required file is missing or unsafe")
    return resolved


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-planning"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("preannotation-planning authorization is required")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_3867_downstream_pending_plan"]
