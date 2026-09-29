from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_native_executor import (
    EXECUTION_CONFIRMATION,
    NativeProcessReceipt,
    completed_native_test_count,
    execute_luanti_native_regression,
    run_bounded_process,
)
from warptest.c1_lazarus_selection.luanti_native_regression import (
    FixedCapsuleBinding,
    NativeRegressionExecutionPlan,
    NativeRegressionRun,
    NativeToolchain,
    ProductionInversePlan,
    profile_for_candidate,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.stable_protocol import (
    AuthorizationRecord,
    load_stable_artifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-test/luanti-native-execution",
        scope="preannotation-execution",
        granted_at="2026-08-13T13:00:00Z",
        local_user_id=os.getuid(),
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
        if argv[0].endswith("cmake") and "--build" in argv:
            (cwd / "bin").mkdir(parents=True, exist_ok=True)
            (cwd / "bin/luantiserver").write_bytes(b"executable")
        return self.receipts.pop(0)


def _receipt(
    returncode: int,
    stdout: bytes = b"",
    *,
    outcome: str = "completed",
) -> NativeProcessReceipt:
    return NativeProcessReceipt(
        outcome=outcome,  # type: ignore[arg-type]
        returncode=returncode if outcome == "completed" else None,
        stdout=stdout,
        stderr=b"raw\xffstderr",
    )


def _module_output(*, passed: bool = True, count: int = 1) -> bytes:
    status = b"PASSED" if passed else b"FAILED"
    failed = 0 if passed else 1
    return (
        b"Unit Test Results: "
        + status
        + f"\n    {failed} / {count} failed tests.\n".encode()
    )


def _catch_output(*, passed: bool = True) -> bytes:
    if passed:
        return b"All tests passed (8 assertions in 1 test case)\n"
    return b"test cases: 1 | 1 failed\nassertions: 8 | 1 failed\n"


def _successful_receipts() -> list[NativeProcessReceipt]:
    return [
        _receipt(0),
        _receipt(0),
        _receipt(0),
        _receipt(0),
        *[_receipt(0, _catch_output()) for _ in range(3)],
        *[_receipt(1, _catch_output(passed=False)) for _ in range(3)],
        *[_receipt(0, _module_output()) for _ in range(3)],
        *[_receipt(0, _module_output()) for _ in range(3)],
    ]


def _plan(root: Path) -> NativeRegressionExecutionPlan:
    candidate = CandidateKey("luanti", 15519)
    profile = profile_for_candidate(candidate)
    source_tree = root / profile.fixed_source_tree_path
    source_tree.mkdir(parents=True)
    production = profile.production_paths[0]
    (source_tree / production).parent.mkdir(parents=True, exist_ok=True)
    (source_tree / production).write_text("fixed\n", encoding="utf-8")
    for relative in (
        *profile.production_paths[1:],
        *profile.support_paths,
        *profile.test_paths,
        *profile.p2p_test_paths,
    ):
        path = source_tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixed\n", encoding="utf-8")
    patch = "".join(
        f"diff --git a/{relative} b/{relative}\n"
        f"--- a/{relative}\n"
        f"+++ b/{relative}\n"
        "@@ -1 +1 @@\n"
        "-buggy\n"
        "+fixed\n"
        for relative in profile.production_paths
    ).encode()
    toolchain_root = root / "private/dependencies/luanti-native/3.0"
    bin_root = toolchain_root / "bin"
    bin_root.mkdir(parents=True)
    for name in ("git", "cmake", "ninja", "cc", "c++"):
        (bin_root / name).write_text("tool\n", encoding="utf-8")
    packages = {
        name: toolchain_root / "packages" / name
        for name in ("sqlite3", "zlib", "zstd", "iconv")
    }
    for path in packages.values():
        path.mkdir(parents=True)
    runs = (
        NativeRegressionRun(
            phase="f2p",
            selector=profile.f2p_selector,
            executable_path="bin/luantiserver",
            argv=(
                "bin/luantiserver",
                "--run-unittests",
                "--test-module",
                profile.f2p_selector,
            ),
            repetitions=3,
            fixed_expected_outcome="pass",
            buggy_expected_outcome="failure",
        ),
        NativeRegressionRun(
            phase="p2p",
            selector=profile.p2p_selector,
            executable_path="bin/luantiserver",
            argv=(
                "bin/luantiserver",
                "--run-unittests",
                "--test-module",
                profile.p2p_selector,
            ),
            repetitions=3,
            fixed_expected_outcome="pass",
            buggy_expected_outcome="pass",
        ),
    )
    toolchain = NativeToolchain(
        root=toolchain_root,
        git=Path("/usr/bin/git"),
        cmake=bin_root / "cmake",
        ninja=bin_root / "ninja",
        c_compiler=bin_root / "cc",
        cxx_compiler=bin_root / "c++",
        archiver=bin_root / "ar",
        ranlib=bin_root / "ranlib",
        linker=bin_root / "ld",
        symbol_tool=bin_root / "nm",
        strip_tool=bin_root / "strip",
        object_inspector=bin_root / "otool",
        install_name_tool=bin_root / "install_name_tool",
        package_roots=packages,
    )
    return NativeRegressionExecutionPlan(
        profile=profile,
        toolchain=toolchain,
        fixed_capsule=FixedCapsuleBinding(
            reference=profile.fixed_capsule_reference,
            source_tree=source_tree,
            test_paths=profile.test_paths,
            p2p_test_paths=profile.p2p_test_paths,
            support_paths=profile.support_paths,
        ),
        source_manifest_path="raw/source.json",
        raw_patch_path="raw/patch.diff",
        production_inverse=ProductionInversePlan(
            paths=profile.production_paths,
            patch=patch,
            git_executable=Path("/usr/bin/git"),
            buggy_apply_argv=(
                Path("/usr/bin/git"),
                "apply",
                "-R",
                "--whitespace=nowarn",
                "-",
            ),
        ),
        configure_argv=(toolchain.cmake, "-S", ".", "-B", "build-luanti-15519"),
        build_argv=(
            toolchain.cmake,
            "--build",
            "build-luanti-15519",
            "--target",
            "luantiserver",
            "--parallel",
            "1",
        ),
        runs=runs,
        closed_environment={
            "HOME": str(root / "private/runtime-home/luanti-native"),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": str(bin_root),
        },
    )


def test_executor_builds_fixed_and_buggy_then_persists_twelve_attempts(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    runner = _Runner(_successful_receipts())

    result = execute_luanti_native_regression(
        tmp_path,
        candidate=plan.profile.candidate,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda _root, *, candidate: plan,
        process_runner=runner,
    )

    assert result.executed_build_steps == 4
    assert result.executed_attempts == 12
    assert result.resumed_attempts == 0
    assert result.qualified is True
    assert len(runner.calls) == 16
    assert [call[1].name for call in runner.calls[:4]] == [
        "fixed",
        "fixed",
        "buggy",
        "buggy",
    ]
    assert not list((tmp_path / "private/native-regression-work").iterdir())
    attempt = load_stable_artifact(
        tmp_path,
        artifact_id="native_regression_attempt/luanti/15519/f2p/fixed/1/v3",
        artifact_kind="native_regression_attempt",
        semantic_version="3.0",
    )
    assert attempt.payload["completed_test_count"] == 1
    assert attempt.payload["observed_outcome"] == "pass"
    assert base64.b64decode(attempt.payload["stderr_base64"]) == b"raw\xffstderr"
    assert not any("sha" in key.casefold() for key in attempt.payload)


def test_executor_resumes_valid_attempts_but_rebuilds_ephemeral_trees(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    first = execute_luanti_native_regression(
        tmp_path,
        candidate=plan.profile.candidate,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda _root, *, candidate: plan,
        process_runner=_Runner(_successful_receipts()),
    )
    runner = _Runner(_successful_receipts()[:4])
    resumed = execute_luanti_native_regression(
        tmp_path,
        candidate=plan.profile.candidate,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda _root, *, candidate: plan,
        process_runner=runner,
    )

    assert first.qualified and resumed.qualified
    assert resumed.executed_build_steps == 4
    assert resumed.executed_attempts == 0
    assert resumed.resumed_attempts == 12
    assert len(runner.calls) == 4


def test_zero_or_ambiguous_selected_tests_never_qualifies(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    receipts = _successful_receipts()
    receipts[4] = _receipt(0, b"No tests ran\n")

    result = execute_luanti_native_regression(
        tmp_path,
        candidate=plan.profile.candidate,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda _root, *, candidate: plan,
        process_runner=_Runner(receipts),
    )

    assert result.qualified is False
    attempt = load_stable_artifact(
        tmp_path,
        artifact_id="native_regression_attempt/luanti/15519/f2p/fixed/1/v3",
        artifact_kind="native_regression_attempt",
        semantic_version="3.0",
    )
    assert attempt.payload["observed_outcome"] == "invalid_test_count"
    assert attempt.payload["completed_test_count"] == 0


def test_build_failure_is_persisted_and_stops_before_attempts(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    runner = _Runner([_receipt(0), _receipt(1, b"build failed\n")])

    with pytest.raises(RuntimeError, match="fixed build failed"):
        execute_luanti_native_regression(
            tmp_path,
            candidate=plan.profile.candidate,
            authorization=_authorization(),
            confirmation=EXECUTION_CONFIRMATION,
            planner=lambda _root, *, candidate: plan,
            process_runner=runner,
        )

    receipt = load_stable_artifact(
        tmp_path,
        artifact_id="native_regression_build/luanti/15519/fixed/build/v3",
        artifact_kind="native_regression_build",
        semantic_version="3.0",
    )
    assert receipt.payload["process_outcome"] == "completed"
    assert receipt.payload["returncode"] == 1
    assert len(runner.calls) == 2


def test_confirmation_and_authorization_are_required(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    with pytest.raises(RuntimeError, match="explicit confirmation"):
        execute_luanti_native_regression(
            tmp_path,
            candidate=plan.profile.candidate,
            authorization=_authorization(),
            confirmation="no",
            planner=lambda _root, *, candidate: plan,
            process_runner=_Runner([]),
        )


def test_real_process_runner_drains_both_streams_and_bounds_capture(
    tmp_path: Path,
) -> None:
    receipt = run_bounded_process(
        (
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'x'*300000); "
            "sys.stderr.buffer.write(b'y'*300000)",
        ),
        cwd=tmp_path,
        env=dict(os.environ),
        timeout_seconds=10,
    )

    assert receipt.outcome == "completed"
    assert receipt.returncode == 0
    assert len(receipt.stdout) == 262_144
    assert len(receipt.stderr) == 262_144
    assert receipt.stdout_truncated is True
    assert receipt.stderr_truncated is True


def test_mixed_module_and_catch_summaries_are_not_an_exact_selection(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    receipts = _successful_receipts()
    receipts[4] = _receipt(0, _catch_output() + _module_output())

    result = execute_luanti_native_regression(
        tmp_path,
        candidate=plan.profile.candidate,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda _root, *, candidate: plan,
        process_runner=_Runner(receipts),
    )

    assert result.qualified is False
    assert result.aggregate.payload["all_expected"] is False


def test_native_test_count_reads_exact_catch_success_from_stderr(
    tmp_path: Path,
) -> None:
    run = _plan(tmp_path).runs[0]

    assert (
        completed_native_test_count(
            run,
            "completed",
            b"",
            _catch_output(),
        )
        == 1
    )


def test_native_test_count_reads_exact_catch_failure_from_stderr(
    tmp_path: Path,
) -> None:
    run = _plan(tmp_path).runs[0]

    assert (
        completed_native_test_count(
            run,
            "completed",
            b"",
            _catch_output(passed=False),
        )
        == 1
    )


def test_native_test_count_treats_one_module_as_one_selected_unit(
    tmp_path: Path,
) -> None:
    run = _plan(tmp_path).runs[1]

    assert (
        completed_native_test_count(
            run,
            "completed",
            b"",
            _module_output(count=3),
        )
        == 1
    )


def test_native_test_count_rejects_mixed_or_duplicated_stream_summaries(
    tmp_path: Path,
) -> None:
    run = _plan(tmp_path).runs[0]

    assert (
        completed_native_test_count(
            run,
            "completed",
            _catch_output(),
            _module_output(),
        )
        == 0
    )
    assert (
        completed_native_test_count(
            run,
            "completed",
            _catch_output(),
            _catch_output(),
        )
        == 0
    )


def test_full_capsule_link_cannot_escape_registered_source_root(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("outside\n", encoding="utf-8")
    (plan.fixed_capsule.source_tree / "unsafe-link").symlink_to(outside)

    with pytest.raises(RuntimeError, match="capsule link is unsafe"):
        execute_luanti_native_regression(
            tmp_path,
            candidate=plan.profile.candidate,
            authorization=_authorization(),
            confirmation=EXECUTION_CONFIRMATION,
            planner=lambda _root, *, candidate: plan,
            process_runner=_Runner([]),
        )


def test_inverse_cannot_modify_any_nonproduction_capsule_file(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    nonproduction = plan.profile.test_paths[0]
    original = (plan.fixed_capsule.source_tree / nonproduction).read_text(
        encoding="utf-8"
    )
    extra = (
        f"diff --git a/{nonproduction} b/{nonproduction}\n"
        f"--- a/{nonproduction}\n"
        f"+++ b/{nonproduction}\n"
        "@@ -1 +1 @@\n"
        "-buggy test\n"
        f"+{original.strip()}\n"
    ).encode()
    object.__setattr__(
        plan.production_inverse,
        "patch",
        plan.production_inverse.patch + extra,
    )

    with pytest.raises(RuntimeError, match="changed non-production source"):
        execute_luanti_native_regression(
            tmp_path,
            candidate=plan.profile.candidate,
            authorization=_authorization(),
            confirmation=EXECUTION_CONFIRMATION,
            planner=lambda _root, *, candidate: plan,
            process_runner=_Runner([]),
        )
