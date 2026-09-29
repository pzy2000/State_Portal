from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_15519_null_client_build import (
    BUILD_CONFIRMATION,
    execute_luanti_15519_null_client_build,
)
from warptest.c1_lazarus_selection.luanti_native_executor import NativeProcessReceipt
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
)
from test_c1_lazarus_selection_luanti_native_executor import _plan


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-test/luanti-15519-null-client-build",
        scope="preannotation-execution",
        granted_at="2026-08-14T04:00:00Z",
        local_user_id=os.getuid(),
    )


def _closure(root: Path) -> StableArtifact:
    destination = root / "private/dependencies/luanti-client/2.0"
    for relative in (
        "packages/freetype/include/freetype2/ft2build.h",
        "packages/freetype/lib/libfreetype.6.dylib",
        "packages/jpeg/include/jpeglib.h",
        "packages/jpeg/lib/libjpeg.8.dylib",
        "packages/png/include/libpng16/png.h",
        "packages/png/lib/libpng16.16.dylib",
        "packages/opengl/include/OpenGL/gl.h",
        "packages/opengl/lib/OpenGL.tbd",
    ):
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("registered\n", encoding="utf-8")
    reference = ArtifactReference(
        "client_dependency_closure/luanti/15519/darwin-arm64/v2",
        "client_dependency_closure",
        "2.0",
    )
    return StableArtifact(
        reference=reference,
        created_at="2026-08-14T04:00:01Z",
        inputs=(),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 15519},
            "destination_root": "private/dependencies/luanti-client/2.0",
            "state": "materialized",
            "complete": True,
        },
        authorization=_authorization(),
        revision_path=root / "unused-revision.json",
        head_path=root / "unused-head.json",
    )


class _Runner:
    def __init__(self, receipts: list[NativeProcessReceipt]) -> None:
        self.receipts = receipts
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str], int]] = []

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> NativeProcessReceipt:
        self.calls.append((argv, cwd, env, timeout_seconds))
        return self.receipts.pop(0)


def _receipt(code: int = 0, *, outcome: str = "completed") -> NativeProcessReceipt:
    return NativeProcessReceipt(
        outcome=outcome,  # type: ignore[arg-type]
        returncode=code if outcome == "completed" else None,
        stdout=b"stdout",
        stderr=b"stderr",
    )


def _execute(root: Path, runner: _Runner, run_name: str = "run-1"):
    existing = root / "private/fixed-capsules/luanti/15519/fixed-source"
    if existing.exists():
        shutil.rmtree(existing)
    toolchain = root / "private/dependencies/luanti-native/3.0"
    if toolchain.exists():
        shutil.rmtree(toolchain)
    plan = _plan(root)
    return execute_luanti_15519_null_client_build(
        root,
        run_root=root / f"private/null-client-runs/luanti/15519/{run_name}",
        authorization=_authorization(),
        confirmation=BUILD_CONFIRMATION,
        native_planner=lambda _root, *, candidate: plan,
        closure_loader=lambda _root: _closure(root),
        process_runner=runner,
    )


def test_builds_fixed_and_buggy_in_independent_trees_with_closed_dependencies(
    tmp_path: Path,
) -> None:
    runner = _Runner([_receipt() for _ in range(4)])

    result = _execute(tmp_path, runner)

    assert result.state == "built"
    assert result.executed_steps == 4
    assert result.resumed_steps == 0
    assert [call[1].name for call in runner.calls] == [
        "fixed",
        "fixed",
        "buggy",
        "buggy",
    ]
    assert [call[3] for call in runner.calls] == [600, 1800, 600, 1800]
    for index, (argv, cwd, env, _) in enumerate(runner.calls):
        variant = "fixed" if index < 2 else "buggy"
        assert cwd.name == variant
        assert env == {
            "HOME": str(
                tmp_path / f"private/null-client-runs/luanti/15519/run-1/home/{variant}"
            ),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": str(tmp_path / "private/dependencies/luanti-native/3.0/bin"),
        }
        if index % 2 == 0:
            joined = "\n".join(argv)
            assert "-DBUILD_CLIENT=TRUE" in argv
            assert "-DBUILD_SERVER=FALSE" in argv
            assert "-DFREETYPE_LIBRARY=" in joined
            assert "-DJPEG_LIBRARY=" in joined
            assert "-DPNG_LIBRARY=" in joined
            assert "-DOPENGL_INCLUDE_DIR=" in joined
            assert "-DOPENGL_gl_LIBRARY=" in joined
        else:
            assert "--target" in argv and "luanti" in argv
    reserved = tmp_path / "private/null-client-runs/luanti/15519/run-1"
    assert reserved.is_dir()
    assert not (reserved / "fixed").exists()
    assert not (reserved / "buggy").exists()
    assert result.aggregate.payload["permanent_terminal_written"] is False
    assert result.aggregate.payload["downstream_state"] == "pending"


def test_valid_typed_receipts_resume_without_reconfiguring_or_rebuilding(
    tmp_path: Path,
) -> None:
    first = _execute(tmp_path, _Runner([_receipt() for _ in range(4)]), "run-first")
    second_runner = _Runner([])

    second = _execute(tmp_path, second_runner, "run-resume")

    assert first.state == second.state == "built"
    assert second.executed_steps == 0
    assert second.resumed_steps == 4
    assert second_runner.calls == []


def test_failed_build_writes_pending_receipt_and_never_terminal(tmp_path: Path) -> None:
    runner = _Runner([_receipt(), _receipt(1)])

    result = _execute(tmp_path, runner, "run-failure")

    assert result.state == "pending"
    assert result.aggregate.payload["reason"] == "fixed_build_failed"
    assert result.aggregate.payload["permanent_terminal_written"] is False
    assert result.aggregate.payload["downstream_state"] == "pending"
    build = load_stable_artifact(
        tmp_path,
        artifact_id="null_client_build_step/luanti/15519/fixed/build/v2",
        artifact_kind="null_client_build_step",
        semantic_version="2.0",
    )
    assert build.payload["returncode"] == 1
    assert not (tmp_path / "artifacts/preannotation_terminal").exists()


def test_timeout_is_typed_pending_and_uses_exact_watchdogs(tmp_path: Path) -> None:
    runner = _Runner([_receipt(outcome="timed_out")])

    result = _execute(tmp_path, runner, "run-timeout")

    assert result.state == "pending"
    assert result.aggregate.payload["reason"] == "fixed_configure_failed"
    assert runner.calls[0][3] == 600


def test_run_root_is_exclusive_and_registered_closure_is_mandatory(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "private/null-client-runs/luanti/15519/existing"
    existing.mkdir(parents=True)
    plan = _plan(tmp_path)
    with pytest.raises(RuntimeError, match="run root already exists"):
        execute_luanti_15519_null_client_build(
            tmp_path,
            run_root=existing,
            authorization=_authorization(),
            confirmation=BUILD_CONFIRMATION,
            native_planner=lambda _root, *, candidate: plan,
            closure_loader=lambda _root: _closure(tmp_path),
            process_runner=_Runner([]),
        )

    empty = tmp_path / "empty"
    empty.mkdir()
    empty_plan = _plan(empty)
    with pytest.raises(RuntimeError, match="client dependency"):
        execute_luanti_15519_null_client_build(
            empty,
            run_root=empty / "private/null-client-runs/luanti/15519/run-1",
            authorization=_authorization(),
            confirmation=BUILD_CONFIRMATION,
            native_planner=lambda _root, *, candidate: empty_plan,
            process_runner=_Runner([]),
        )


def test_legacy_closure_without_registered_opengl_never_starts_configure(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    legacy = _closure(tmp_path)
    legacy = StableArtifact(
        reference=ArtifactReference(
            "client_dependency_closure/luanti/15519/darwin-arm64/v1",
            "client_dependency_closure",
            "1.0",
        ),
        created_at=legacy.created_at,
        inputs=legacy.inputs,
        payload={
            **legacy.payload,
            "destination_root": "private/dependencies/luanti-client/1.0",
        },
        authorization=legacy.authorization,
        revision_path=legacy.revision_path,
        head_path=legacy.head_path,
    )
    runner = _Runner([])

    with pytest.raises(RuntimeError, match="closure is invalid"):
        execute_luanti_15519_null_client_build(
            tmp_path,
            run_root=tmp_path / "private/null-client-runs/luanti/15519/legacy-closure",
            authorization=_authorization(),
            confirmation=BUILD_CONFIRMATION,
            native_planner=lambda _root, *, candidate: plan,
            closure_loader=lambda _root: legacy,
            process_runner=runner,
        )

    assert runner.calls == []
