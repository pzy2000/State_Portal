from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_15519_candidate_materialization import (
    CANDIDATE_MATERIALIZATION_CONFIRMATION,
    load_luanti_15519_candidate_materialization,
    materialize_luanti_15519_persistent_client_pair,
)
from warptest.c1_lazarus_selection.luanti_native_executor import NativeProcessReceipt
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)
from test_c1_lazarus_selection_luanti_native_executor import _plan
from test_c1_lazarus_selection_luanti_15519_null_client_build import _closure


_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/15519/fixed/v2", "fixed_source_capsule", "2.0"
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-test/luanti-15519-candidate-materialization",
        scope="version-materialization",
        granted_at="2026-08-14T06:00:00Z",
        local_user_id=os.getuid(),
    )


def _seed(root: Path):
    plan = _plan(root)
    write_stable_artifact(
        root,
        artifact_id=_CAPSULE.artifact_id,
        artifact_kind=_CAPSULE.artifact_kind,
        semantic_version=_CAPSULE.semantic_version,
        created_at="2026-08-14T05:59:00Z",
        inputs=(),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 15519},
            "source_tree_path": plan.profile.fixed_source_tree_path,
        },
        authorization=AuthorizationRecord(
            authorization_id="local-test/luanti-15519-capsule",
            scope="protocol-migration",
            granted_at="2026-08-14T05:58:00Z",
            local_user_id=os.getuid(),
        ),
    )
    return plan


class _Runner:
    def __init__(self, receipts: list[NativeProcessReceipt]) -> None:
        self.receipts = receipts
        self.calls: list[tuple[tuple[str, ...], Path, int]] = []

    def __call__(self, argv, *, cwd, env, timeout_seconds):
        self.calls.append((argv, cwd, timeout_seconds))
        if argv[0].endswith("cmake") and "--build" in argv:
            binary = cwd / "bin/luanti"
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(b"fake-luanti")
            binary.chmod(0o700)
        if argv[0].endswith("cmake") and "--build" not in argv:
            (cwd / "build-luanti-15519").mkdir(exist_ok=True)
        return self.receipts.pop(0)


def _receipt(code: int = 0, *, outcome: str = "completed"):
    return NativeProcessReceipt(
        outcome=outcome,  # type: ignore[arg-type]
        returncode=code if outcome == "completed" else None,
        stdout=b"stdout",
        stderr=b"stderr",
    )


def _execute(root: Path, *, plan, execute_build=False, runner=None):
    return materialize_luanti_15519_persistent_client_pair(
        root,
        authorization=_authorization(),
        confirmation=CANDIDATE_MATERIALIZATION_CONFIRMATION,
        execute_build=execute_build,
        native_planner=lambda _root, *, candidate: plan,
        closure_loader=lambda _root: _closure(root),
        process_runner=runner or _Runner([]),
    )


def test_source_pair_is_persistent_without_starting_a_build(tmp_path: Path) -> None:
    plan = _seed(tmp_path)

    result = _execute(tmp_path, plan=plan)

    assert result.state == "pending"
    assert result.aggregate.reference == ArtifactReference(
        "candidate_materialization/luanti/15519/fixed-buggy/v1",
        "candidate_materialization",
        "1.0",
    )
    base = tmp_path / "private/materialized/luanti/15519/null-client/v2"
    fixed = base / "fixed"
    buggy = base / "buggy"
    assert fixed.is_dir() and buggy.is_dir()
    for tree in (fixed, buggy):
        assert not any(path.is_symlink() for path in tree.rglob("*"))
    production = plan.profile.production_paths[0]
    assert (fixed / production).read_bytes() != (buggy / production).read_bytes()
    assert result.aggregate.payload["build_requested"] is False
    assert result.aggregate.payload["permanent_terminal_written"] is False
    assert not list(
        (tmp_path / "artifacts/candidate_materialization_step").rglob("head.json")
    )


def test_build_persists_both_trees_and_resumes_four_typed_steps(tmp_path: Path) -> None:
    plan = _seed(tmp_path)
    first = _execute(
        tmp_path,
        plan=plan,
        execute_build=True,
        runner=_Runner([_receipt() for _ in range(4)]),
    )

    assert first.state == "built"
    assert first.executed_steps == 4
    assert first.resumed_steps == 0
    base = tmp_path / "private/materialized/luanti/15519/null-client/v2"
    for variant in ("fixed", "buggy"):
        binary = base / variant / "bin/luanti"
        assert binary.is_file() and binary.stat().st_mode & 0o100
    assert len(first.aggregate.payload["step_receipts"]) == 4
    loaded = load_luanti_15519_candidate_materialization(
        tmp_path,
        native_planner=lambda _root, *, candidate: plan,
        closure_loader=lambda _root: _closure(tmp_path),
    )
    assert loaded.payload["state"] == "built"

    second_runner = _Runner([])
    second = _execute(tmp_path, plan=plan, execute_build=True, runner=second_runner)
    assert second.state == "built"
    assert second.executed_steps == 0
    assert second.resumed_steps == 4
    assert second_runner.calls == []


def test_failed_build_is_pending_and_can_be_retried_without_terminal(
    tmp_path: Path,
) -> None:
    plan = _seed(tmp_path)
    failed = _execute(
        tmp_path,
        plan=plan,
        execute_build=True,
        runner=_Runner([_receipt(1)]),
    )
    assert failed.state == "pending"
    assert failed.aggregate.payload["reason"] == "fixed_configure_failed"
    assert failed.aggregate.payload["permanent_terminal_written"] is False
    assert not (tmp_path / "artifacts/preannotation_terminal").exists()

    retried = _execute(
        tmp_path,
        plan=plan,
        execute_build=True,
        runner=_Runner([_receipt() for _ in range(4)]),
    )
    assert retried.state == "built"
    assert retried.executed_steps == 4


def test_existing_untyped_materialization_root_is_rejected(tmp_path: Path) -> None:
    plan = _seed(tmp_path)
    base = tmp_path / "private/materialized/luanti/15519/null-client/v2"
    base.mkdir(parents=True)
    (base / "fixed").mkdir()
    (base / "buggy").mkdir()
    with pytest.raises(RuntimeError, match="without a typed receipt"):
        _execute(tmp_path, plan=plan)


def test_existing_pair_symlink_is_rejected(tmp_path: Path) -> None:
    plan = _seed(tmp_path)
    _execute(tmp_path, plan=plan)
    base = tmp_path / "private/materialized/luanti/15519/null-client/v2"
    external = tmp_path / "external"
    external.mkdir()
    shutil.rmtree(base / "buggy")
    (base / "buggy").symlink_to(external, target_is_directory=True)
    with pytest.raises(RuntimeError, match="persistent Luanti client tree"):
        load_luanti_15519_candidate_materialization(
            tmp_path,
            native_planner=lambda _root, *, candidate: plan,
            closure_loader=lambda _root: _closure(tmp_path),
        )
