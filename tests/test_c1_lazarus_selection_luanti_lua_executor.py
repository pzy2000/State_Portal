from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_lua_executor import (
    EXECUTION_CONFIRMATION,
    execute_luanti_lua_regression,
)
from warptest.c1_lazarus_selection.luanti_lua_regression import (
    FixedCapsuleBinding,
    LuaRegressionExecutionPlan,
    LuaRegressionRun,
    ProductionInversePlan,
    WatchdogReceipt,
    profile_for_candidate,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-test/luanti-lua-execution",
        scope="preannotation-execution",
        granted_at="2026-08-13T12:00:00Z",
        local_user_id=os.getuid(),
    )


class _Watchdog:
    def __init__(self, receipts: list[WatchdogReceipt]) -> None:
        self.receipts = receipts
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str] | None]] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> WatchdogReceipt:
        assert cwd.exists()
        self.calls.append((argv, cwd, env))
        return self.receipts.pop(0)


def _receipt(outcome: str, returncode: int | None) -> WatchdogReceipt:
    if outcome == "completed":
        summary = (
            b"1 success / 0 failures / 0 errors / 0 pending\n"
            if returncode == 0
            else b"0 successes / 1 failure / 0 errors / 0 pending\n"
        )
    else:
        summary = b""
    return WatchdogReceipt(
        outcome=outcome,  # type: ignore[arg-type]
        returncode=returncode,
        stdout=summary + b"raw\x00stdout",
        stderr=b"raw\xffstderr",
    )


def _plan(
    root: Path,
    watchdog: _Watchdog,
    *,
    number: int = 14757,
) -> LuaRegressionExecutionPlan:
    candidate = CandidateKey("luanti", number)
    profile = profile_for_candidate(candidate)
    source_tree = root / profile.fixed_source_tree_path
    production = source_tree / profile.production_paths[0]
    support = source_tree / profile.support_paths[0]
    test = source_tree / profile.test_paths[0]
    production.parent.mkdir(parents=True, exist_ok=True)
    support.parent.mkdir(parents=True, exist_ok=True)
    test.parent.mkdir(parents=True, exist_ok=True)
    production.write_text("fixed\n", encoding="utf-8")
    support.write_text("fixed support\n", encoding="utf-8")
    test.write_text("fixed test\n", encoding="utf-8")
    patch = (
        f"diff --git a/{profile.production_paths[0]} "
        f"b/{profile.production_paths[0]}\n"
        f"--- a/{profile.production_paths[0]}\n"
        f"+++ b/{profile.production_paths[0]}\n"
        "@@ -1 +1 @@\n"
        "-buggy\n"
        "+fixed\n"
    ).encode()
    runs = tuple(
        LuaRegressionRun(
            phase=phase,
            selector=f"{phase} selector",
            test_path=profile.test_paths[0],
            argv=("/registered/busted", profile.test_paths[0], f"--filter={phase}"),
            repetitions=3,
            fixed_expected_outcome="pass",
            buggy_expected_outcome=(
                profile.f2p_buggy_outcome if phase == "f2p" else "pass"
            ),
        )
        for phase in ("f2p", "p2p")
    )
    return LuaRegressionExecutionPlan(
        profile=profile,
        runtime=object(),  # type: ignore[arg-type]
        fixed_capsule=FixedCapsuleBinding(
            reference=profile.fixed_capsule_reference,
            source_tree=source_tree,
            support_paths=profile.support_paths,
            test_paths=profile.test_paths,
        ),
        source_manifest_path="raw/source.json",
        raw_patch_path="raw/patch.diff",
        production_inverse=ProductionInversePlan(
            paths=profile.production_paths,
            patch=patch,
            buggy_apply_argv=("git", "apply", "-R", "--whitespace=nowarn", "-"),
        ),
        runs=runs,  # type: ignore[arg-type]
        watchdog=watchdog,  # type: ignore[arg-type]
    )


def _successful_receipts(number: int = 14757) -> list[WatchdogReceipt]:
    f2p_buggy = "timed_out" if number == 13132 else "completed"
    f2p_buggy_code = None if number == 13132 else 1
    return [
        *[_receipt("completed", 0) for _ in range(3)],
        *[_receipt(f2p_buggy, f2p_buggy_code) for _ in range(3)],
        *[_receipt("completed", 0) for _ in range(3)],
        *[_receipt("completed", 0) for _ in range(3)],
    ]


def test_executor_persists_twelve_raw_attempts_and_qualified_aggregate(
    tmp_path: Path,
) -> None:
    watchdog = _Watchdog(_successful_receipts())
    plan = _plan(tmp_path, watchdog)

    result = execute_luanti_lua_regression(
        tmp_path,
        candidate=plan.profile.candidate,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda _root, *, candidate: plan,
    )

    assert result.executed_attempts == 12
    assert result.resumed_attempts == 0
    assert result.qualified is True
    assert len(watchdog.calls) == 12
    assert not list((tmp_path / "private" / "lua-regression-work").iterdir())
    attempt = load_stable_artifact(
        tmp_path,
        artifact_id="lua_regression_attempt/luanti/14757/f2p/fixed/1/v3",
        artifact_kind="lua_regression_attempt",
        semantic_version="3.0",
    )
    assert base64.b64decode(attempt.payload["stdout_base64"]) == (
        b"1 success / 0 failures / 0 errors / 0 pending\nraw\x00stdout"
    )
    assert base64.b64decode(attempt.payload["stderr_base64"]) == b"raw\xffstderr"
    assert attempt.payload["observed_outcome"] == "pass"
    assert attempt.payload["expected_outcome"] == "pass"
    assert attempt.inputs == (plan.profile.fixed_capsule_reference,)
    assert attempt.inputs[0].semantic_version == "2.0"
    assert set(attempt.payload) == {
        "candidate",
        "profile_id",
        "phase",
        "variant",
        "repetition",
        "selector",
        "test_path",
        "expected_outcome",
        "observed_outcome",
        "process_outcome",
        "returncode",
        "completed_test_count",
        "stdout_base64",
        "stderr_base64",
        "stdout_truncated",
        "stderr_truncated",
        "output_encoding",
        "matched_expected",
    }


def test_executor_resumes_each_valid_attempt_without_rerunning(tmp_path: Path) -> None:
    first_watchdog = _Watchdog(_successful_receipts())
    plan = _plan(tmp_path, first_watchdog)
    first = execute_luanti_lua_regression(
        tmp_path,
        candidate=plan.profile.candidate,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda _root, *, candidate: plan,
    )
    second_watchdog = _Watchdog([])
    resumed_plan = _plan(tmp_path, second_watchdog)

    second = execute_luanti_lua_regression(
        tmp_path,
        candidate=resumed_plan.profile.candidate,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda _root, *, candidate: resumed_plan,
    )

    assert first.qualified and second.qualified
    assert second.executed_attempts == 0
    assert second.resumed_attempts == 12
    assert second_watchdog.calls == []


def test_inconsistent_repetitions_are_not_qualified(tmp_path: Path) -> None:
    receipts = _successful_receipts()
    receipts[1] = _receipt("completed", 1)
    watchdog = _Watchdog(receipts)
    plan = _plan(tmp_path, watchdog)

    result = execute_luanti_lua_regression(
        tmp_path,
        candidate=plan.profile.candidate,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda _root, *, candidate: plan,
    )

    assert result.qualified is False
    assert result.aggregate.payload["stable_repetitions"] is False


def test_zero_selected_tests_are_persisted_but_never_qualified(
    tmp_path: Path,
) -> None:
    receipts = _successful_receipts()
    receipts[0] = WatchdogReceipt(
        outcome="completed",
        returncode=0,
        stdout=b"0 successes / 0 failures / 0 errors / 0 pending\n",
        stderr=b"",
    )
    watchdog = _Watchdog(receipts)
    plan = _plan(tmp_path, watchdog)

    result = execute_luanti_lua_regression(
        tmp_path,
        candidate=plan.profile.candidate,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda _root, *, candidate: plan,
    )

    attempt = load_stable_artifact(
        tmp_path,
        artifact_id="lua_regression_attempt/luanti/14757/f2p/fixed/1/v3",
        artifact_kind="lua_regression_attempt",
        semantic_version="3.0",
    )
    assert result.qualified is False
    assert attempt.payload["completed_test_count"] == 0
    assert attempt.payload["observed_outcome"] == "invalid_test_count"
    assert attempt.payload["matched_expected"] is False
    assert result.aggregate.payload["all_expected"] is False


def test_13132_expected_buggy_timeout_is_qualified(tmp_path: Path) -> None:
    watchdog = _Watchdog(_successful_receipts(13132))
    plan = _plan(tmp_path, watchdog, number=13132)

    result = execute_luanti_lua_regression(
        tmp_path,
        candidate=plan.profile.candidate,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda _root, *, candidate: plan,
    )

    assert result.qualified is True
    attempt = load_stable_artifact(
        tmp_path,
        artifact_id="lua_regression_attempt/luanti/13132/f2p/buggy/1/v3",
        artifact_kind="lua_regression_attempt",
        semantic_version="3.0",
    )
    assert attempt.payload["observed_outcome"] == "timed_out"
    assert attempt.payload["matched_expected"] is True


def test_confirmation_is_required_before_planning_or_execution(tmp_path: Path) -> None:
    called = False

    def planner(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        return object()

    with pytest.raises(RuntimeError, match="explicit confirmation"):
        execute_luanti_lua_regression(
            tmp_path,
            candidate=CandidateKey("luanti", 14757),
            authorization=_authorization(),
            confirmation="wrong",
            planner=planner,  # type: ignore[arg-type]
        )
    assert called is False


def test_symlink_in_fixed_capsule_fails_closed(tmp_path: Path) -> None:
    watchdog = _Watchdog(_successful_receipts())
    plan = _plan(tmp_path, watchdog)
    production = plan.fixed_capsule.source_tree / plan.profile.production_paths[0]
    production.unlink()
    production.symlink_to(tmp_path / "outside")

    with pytest.raises(RuntimeError, match="fixed capsule path is unsafe"):
        execute_luanti_lua_regression(
            tmp_path,
            candidate=plan.profile.candidate,
            authorization=_authorization(),
            confirmation=EXECUTION_CONFIRMATION,
            planner=lambda _root, *, candidate: plan,
        )
    assert watchdog.calls == []


def test_inverse_that_changes_fixed_test_fails_closed(tmp_path: Path) -> None:
    watchdog = _Watchdog(_successful_receipts())
    plan = _plan(tmp_path, watchdog)
    test_path = plan.profile.test_paths[0]
    malicious = ProductionInversePlan(
        paths=plan.profile.production_paths,
        patch=(
            f"diff --git a/{test_path} b/{test_path}\n"
            f"--- a/{test_path}\n"
            f"+++ b/{test_path}\n"
            "@@ -1 +1 @@\n"
            "-changed test\n"
            "+fixed test\n"
        ).encode(),
        buggy_apply_argv=("git", "apply", "-R", "--whitespace=nowarn", "-"),
    )
    altered_plan = LuaRegressionExecutionPlan(
        profile=plan.profile,
        runtime=plan.runtime,
        fixed_capsule=plan.fixed_capsule,
        source_manifest_path=plan.source_manifest_path,
        raw_patch_path=plan.raw_patch_path,
        production_inverse=malicious,
        runs=plan.runs,
        watchdog=watchdog,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="changed fixed capsule support or tests"):
        execute_luanti_lua_regression(
            tmp_path,
            candidate=plan.profile.candidate,
            authorization=_authorization(),
            confirmation=EXECUTION_CONFIRMATION,
            planner=lambda _root, *, candidate: altered_plan,
        )
    assert watchdog.calls == []


def test_executor_copies_support_to_both_variants_without_mutating_it(
    tmp_path: Path,
) -> None:
    snapshots: list[tuple[bytes, bytes, bytes]] = []

    class InspectingWatchdog(_Watchdog):
        def run(
            self,
            argv: tuple[str, ...],
            *,
            cwd: Path,
            env: dict[str, str] | None = None,
        ) -> WatchdogReceipt:
            profile = profile_for_candidate(CandidateKey("luanti", 14757))
            snapshots.append(
                (
                    (cwd / profile.production_paths[0]).read_bytes(),
                    (cwd / profile.support_paths[0]).read_bytes(),
                    (cwd / profile.test_paths[0]).read_bytes(),
                )
            )
            return super().run(argv, cwd=cwd, env=env)

    watchdog = InspectingWatchdog(_successful_receipts())
    plan = _plan(tmp_path, watchdog)

    execute_luanti_lua_regression(
        tmp_path,
        candidate=plan.profile.candidate,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda _root, *, candidate: plan,
    )

    assert len(snapshots) == 12
    assert {support for _, support, _ in snapshots} == {b"fixed support\n"}
    assert {test for _, _, test in snapshots} == {b"fixed test\n"}
    assert {production for production, _, _ in snapshots} == {
        b"fixed\n",
        b"buggy\n",
    }


def test_parent_git_repository_cannot_capture_the_disposable_inverse(
    tmp_path: Path,
) -> None:
    subprocess.run(
        ("git", "init", "--quiet", str(tmp_path)),
        check=True,
        capture_output=True,
    )
    artifact_root = tmp_path / "artifact-root"
    artifact_root.mkdir()
    watchdog = _Watchdog(_successful_receipts())
    plan = _plan(artifact_root, watchdog)

    result = execute_luanti_lua_regression(
        artifact_root,
        candidate=plan.profile.candidate,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda _root, *, candidate: plan,
    )

    assert result.qualified is True
    assert len(watchdog.calls) == 12


def test_inverse_that_changes_fixed_support_fails_closed(tmp_path: Path) -> None:
    watchdog = _Watchdog(_successful_receipts())
    plan = _plan(tmp_path, watchdog)
    support_path = plan.profile.support_paths[0]
    malicious = ProductionInversePlan(
        paths=plan.profile.production_paths,
        patch=(
            f"diff --git a/{support_path} b/{support_path}\n"
            f"--- a/{support_path}\n"
            f"+++ b/{support_path}\n"
            "@@ -1 +1 @@\n"
            "-buggy support\n"
            "+fixed support\n"
        ).encode(),
        buggy_apply_argv=("git", "apply", "-R", "--whitespace=nowarn", "-"),
    )
    altered_plan = LuaRegressionExecutionPlan(
        profile=plan.profile,
        runtime=plan.runtime,
        fixed_capsule=plan.fixed_capsule,
        source_manifest_path=plan.source_manifest_path,
        raw_patch_path=plan.raw_patch_path,
        production_inverse=malicious,
        runs=plan.runs,
        watchdog=watchdog,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="changed fixed capsule support or tests"):
        execute_luanti_lua_regression(
            tmp_path,
            candidate=plan.profile.candidate,
            authorization=_authorization(),
            confirmation=EXECUTION_CONFIRMATION,
            planner=lambda _root, *, candidate: altered_plan,
        )
    assert watchdog.calls == []


def test_launch_failure_is_persisted_but_not_qualified(tmp_path: Path) -> None:
    receipts = _successful_receipts()
    receipts[0] = _receipt("launch_failed", None)
    watchdog = _Watchdog(receipts)
    plan = _plan(tmp_path, watchdog)

    result = execute_luanti_lua_regression(
        tmp_path,
        candidate=plan.profile.candidate,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda _root, *, candidate: plan,
    )

    assert result.qualified is False
    assert result.aggregate.payload["all_expected"] is False


def test_crash_resume_reloads_completed_attempts(tmp_path: Path) -> None:
    receipts = _successful_receipts()

    class CrashingWatchdog(_Watchdog):
        def run(
            self,
            argv: tuple[str, ...],
            *,
            cwd: Path,
            env: dict[str, str] | None = None,
        ) -> WatchdogReceipt:
            if len(self.calls) == 4:
                raise RuntimeError("simulated crash")
            return super().run(argv, cwd=cwd, env=env)

    crashing = CrashingWatchdog(receipts)
    plan = _plan(tmp_path, crashing)
    with pytest.raises(RuntimeError, match="simulated crash"):
        execute_luanti_lua_regression(
            tmp_path,
            candidate=plan.profile.candidate,
            authorization=_authorization(),
            confirmation=EXECUTION_CONFIRMATION,
            planner=lambda _root, *, candidate: plan,
        )

    retry = _Watchdog(_successful_receipts()[4:])
    retry_plan = _plan(tmp_path, retry)
    result = execute_luanti_lua_regression(
        tmp_path,
        candidate=retry_plan.profile.candidate,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda _root, *, candidate: retry_plan,
    )
    assert result.resumed_attempts == 4
    assert result.executed_attempts == 8
    assert result.qualified is True


def test_resume_rejects_attempt_not_bound_to_the_v2_capsule(tmp_path: Path) -> None:
    first_watchdog = _Watchdog(_successful_receipts())
    plan = _plan(tmp_path, first_watchdog)
    execute_luanti_lua_regression(
        tmp_path,
        candidate=plan.profile.candidate,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda _root, *, candidate: plan,
    )
    attempt = load_stable_artifact(
        tmp_path,
        artifact_id="lua_regression_attempt/luanti/14757/f2p/fixed/1/v3",
        artifact_kind="lua_regression_attempt",
        semantic_version="3.0",
    )
    write_stable_artifact(
        tmp_path,
        artifact_id=attempt.reference.artifact_id,
        artifact_kind=attempt.reference.artifact_kind,
        semantic_version=attempt.reference.semantic_version,
        created_at="2099-01-01T00:00:00Z",
        inputs=(
            ArtifactReference(
                "fixed_source_capsule/luanti/99999/fixed/v2",
                "fixed_source_capsule",
                "2.0",
            ),
        ),
        payload=dict(attempt.payload),
        authorization=_authorization(),
    )
    retry = _Watchdog([])
    retry_plan = _plan(tmp_path, retry)

    with pytest.raises(RuntimeError, match="attempt dependency is invalid"):
        execute_luanti_lua_regression(
            tmp_path,
            candidate=retry_plan.profile.candidate,
            authorization=_authorization(),
            confirmation=EXECUTION_CONFIRMATION,
            planner=lambda _root, *, candidate: retry_plan,
        )
    assert retry.calls == []


def test_resume_rejects_incoherent_process_result(tmp_path: Path) -> None:
    first_watchdog = _Watchdog(_successful_receipts())
    plan = _plan(tmp_path, first_watchdog)
    execute_luanti_lua_regression(
        tmp_path,
        candidate=plan.profile.candidate,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda _root, *, candidate: plan,
    )
    attempt = load_stable_artifact(
        tmp_path,
        artifact_id="lua_regression_attempt/luanti/14757/f2p/fixed/1/v3",
        artifact_kind="lua_regression_attempt",
        semantic_version="3.0",
    )
    payload = dict(attempt.payload)
    payload["returncode"] = None
    write_stable_artifact(
        tmp_path,
        artifact_id=attempt.reference.artifact_id,
        artifact_kind=attempt.reference.artifact_kind,
        semantic_version=attempt.reference.semantic_version,
        created_at="2099-01-01T00:00:00Z",
        inputs=attempt.inputs,
        payload=payload,
        authorization=_authorization(),
    )
    retry = _Watchdog([])
    retry_plan = _plan(tmp_path, retry)

    with pytest.raises(RuntimeError, match="attempt process result is invalid"):
        execute_luanti_lua_regression(
            tmp_path,
            candidate=retry_plan.profile.candidate,
            authorization=_authorization(),
            confirmation=EXECUTION_CONFIRMATION,
            planner=lambda _root, *, candidate: retry_plan,
        )
    assert retry.calls == []


def test_attempt_output_is_bounded_before_persistence(tmp_path: Path) -> None:
    receipts = _successful_receipts()
    receipts[0] = WatchdogReceipt(
        outcome="completed",
        returncode=0,
        stdout=b"x" * 300_000,
        stderr=b"y" * 300_001,
    )
    watchdog = _Watchdog(receipts)
    plan = _plan(tmp_path, watchdog)

    execute_luanti_lua_regression(
        tmp_path,
        candidate=plan.profile.candidate,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda _root, *, candidate: plan,
    )
    attempt = load_stable_artifact(
        tmp_path,
        artifact_id="lua_regression_attempt/luanti/14757/f2p/fixed/1/v3",
        artifact_kind="lua_regression_attempt",
        semantic_version="3.0",
    )

    assert len(base64.b64decode(attempt.payload["stdout_base64"])) == 262_144
    assert len(base64.b64decode(attempt.payload["stderr_base64"])) == 262_144
    assert attempt.payload["stdout_truncated"] is True
    assert attempt.payload["stderr_truncated"] is True


def test_resume_rejects_output_larger_than_the_persistence_bound(
    tmp_path: Path,
) -> None:
    first_watchdog = _Watchdog(_successful_receipts())
    plan = _plan(tmp_path, first_watchdog)
    execute_luanti_lua_regression(
        tmp_path,
        candidate=plan.profile.candidate,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda _root, *, candidate: plan,
    )
    attempt = load_stable_artifact(
        tmp_path,
        artifact_id="lua_regression_attempt/luanti/14757/f2p/fixed/1/v3",
        artifact_kind="lua_regression_attempt",
        semantic_version="3.0",
    )
    payload = dict(attempt.payload)
    payload["stdout_base64"] = base64.b64encode(b"x" * 262_145).decode("ascii")
    payload["stdout_truncated"] = True
    write_stable_artifact(
        tmp_path,
        artifact_id=attempt.reference.artifact_id,
        artifact_kind=attempt.reference.artifact_kind,
        semantic_version=attempt.reference.semantic_version,
        created_at="2099-01-01T00:00:00Z",
        inputs=attempt.inputs,
        payload=payload,
        authorization=_authorization(),
    )
    retry = _Watchdog([])
    retry_plan = _plan(tmp_path, retry)

    with pytest.raises(RuntimeError, match="attempt output is invalid"):
        execute_luanti_lua_regression(
            tmp_path,
            candidate=retry_plan.profile.candidate,
            authorization=_authorization(),
            confirmation=EXECUTION_CONFIRMATION,
            planner=lambda _root, *, candidate: retry_plan,
        )
    assert retry.calls == []
