from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_lua_regression import (
    OFFLINE_BUSTED_DEPENDENCY_ID,
    OFFLINE_BUSTED_MODULE_VERSIONS,
    OFFLINE_BUSTED_RUNTIME_ROOT,
    FixedCapsuleBinding,
    LuaRegressionExecutionPlan,
    PythonSubprocessWatchdog,
    WatchdogReceipt,
    plan_luanti_lua_regression_execution,
    profile_for_candidate,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.pipeline import StageDecision
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


_RUNTIME_MODULES = {
    "busted": "share/lua/5.1/busted/init.lua",
    "cliargs": "share/lua/5.1/cliargs/init.lua",
    "dkjson": "share/lua/5.1/dkjson.lua",
    "luafilesystem": "lib/lua/5.1/lfs.so",
    "lua_term": "share/lua/5.1/term/init.lua",
    "luassert": "share/lua/5.1/luassert/init.lua",
    "luasystem": "share/lua/5.1/system/init.lua",
    "mediator_lua": "share/lua/5.1/mediator.lua",
    "penlight": "share/lua/5.1/pl/init.lua",
    "say": "share/lua/5.1/say/init.lua",
}


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="authorization/luanti-lua-regression-test/v1",
        scope="screening",
        granted_at="2026-08-13T12:00:00Z",
        local_user_id=os.getuid(),
    )


def _write(root: Path, relative: str, content: str | bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def _patch_for(candidate: CandidateKey) -> bytes:
    profile = profile_for_candidate(candidate)
    production_path = profile.production_paths[0]
    test_path = profile.test_paths[0]
    test_lines = "\n".join(f"+{marker}" for marker in profile.patch_test_markers)
    sections = [
        f"""\\
diff --git a/{production_path} b/{production_path}
--- a/{production_path}
+++ b/{production_path}
@@ -1 +1 @@
-old production behavior
+fixed production behavior
diff --git a/{test_path} b/{test_path}
--- a/{test_path}
+++ b/{test_path}
@@ -1 +1,2 @@
+describe(\"regression\", function()\n{test_lines}
"""
    ]
    if candidate.change_number == 13132:
        sections.append(
            """\\
diff --git a/doc/lua_api.txt b/doc/lua_api.txt
--- a/doc/lua_api.txt
+++ b/doc/lua_api.txt
@@ -1 +1 @@
-old docs
+new docs
"""
        )
    return "".join(sections).encode("utf-8")


def _write_source_packet(root: Path, candidate: CandidateKey) -> None:
    profile = profile_for_candidate(candidate)
    patch_path = (
        f"raw/github/luanti/{candidate.change_number}/pull-diff/attempt-1/response.body"
    )
    manifest_path = (
        f"raw/github/luanti/{candidate.change_number}/source-manifest/attempt-1/"
        "source.json"
    )
    _write(root, patch_path, _patch_for(candidate))
    _write(
        root,
        manifest_path,
        json.dumps(
            {
                "candidate": candidate.to_value(),
                "complete": True,
                "raw_paths": [patch_path],
                "patch_raw_path": patch_path,
            }
        ),
    )
    write_stable_artifact(
        root,
        artifact_id=profile.source_packet_reference.artifact_id,
        artifact_kind=profile.source_packet_reference.artifact_kind,
        semantic_version=profile.source_packet_reference.semantic_version,
        created_at="2026-08-13T12:00:01Z",
        inputs=(
            ArtifactReference(
                "repository_enumeration/luanti/current/v2",
                "repository_enumeration",
                "2.0",
            ),
        ),
        payload={
            "candidate": candidate.to_value(),
            "complete": True,
            "raw_evidence_path": manifest_path,
        },
        authorization=_authorization(),
    )


def _write_fixed_capsule(
    root: Path,
    candidate: CandidateKey,
    *,
    source_tree_path: str | None = None,
) -> FixedCapsuleBinding:
    profile = profile_for_candidate(candidate)
    source_tree_path = source_tree_path or profile.fixed_source_tree_path
    for production_path in profile.production_paths:
        _write(root, f"{source_tree_path}/{production_path}", "fixed production\n")
    for support_path in profile.support_paths:
        _write(root, f"{source_tree_path}/{support_path}", "return vector\n")
    test_text = "\n".join(profile.fixed_test_markers) + "\n"
    for test_path in profile.test_paths:
        _write(root, f"{source_tree_path}/{test_path}", test_text)
    write_stable_artifact(
        root,
        artifact_id=profile.fixed_capsule_reference.artifact_id,
        artifact_kind=profile.fixed_capsule_reference.artifact_kind,
        semantic_version=profile.fixed_capsule_reference.semantic_version,
        created_at="2026-08-13T12:00:02Z",
        inputs=(profile.source_packet_reference,),
        payload={
            "candidate": candidate.to_value(),
            "profile_id": profile.profile_id,
            "source_tree_path": source_tree_path,
            "support_paths": list(profile.support_paths),
            "test_paths": list(profile.test_paths),
        },
        authorization=_authorization(),
    )
    return FixedCapsuleBinding(
        reference=profile.fixed_capsule_reference,
        source_tree=root / source_tree_path,
        support_paths=profile.support_paths,
        test_paths=profile.test_paths,
    )


def _write_runtime(
    root: Path,
    *,
    runtime_relative: str = OFFLINE_BUSTED_RUNTIME_ROOT,
    symlink_runner: bool = False,
) -> Path:
    runtime_root = root / runtime_relative
    runner = _write(runtime_root, "bin/busted", "#!/bin/sh\n")
    _write(runtime_root, "bin/luajit", "#!/bin/sh\n")
    runner.chmod(0o700)
    (runtime_root / "bin" / "luajit").chmod(0o700)
    modules = dict(_RUNTIME_MODULES)
    closure_files = ["bin/busted", "bin/luajit", *modules.values()]
    for relative in modules.values():
        _write(runtime_root, relative, "return {}\n")
    for rock, version in OFFLINE_BUSTED_MODULE_VERSIONS.items():
        relative = f"lib/luarocks/rocks-5.1/{rock}/{version}/rock_manifest"
        _write(runtime_root, relative, "rock_manifest = {}\n")
        closure_files.append(relative)
    _write(
        runtime_root,
        "runtime.json",
        json.dumps(
            {
                "dependency_id": OFFLINE_BUSTED_DEPENDENCY_ID,
                "semantic_version": "1.0",
                "busted_version": "2.2.0-1",
                "lua_abi": "5.1",
                "runner": "bin/busted",
                "interpreter": "bin/luajit",
                "modules": modules,
                "module_versions": OFFLINE_BUSTED_MODULE_VERSIONS,
                "lua_paths": [
                    "share/lua/5.1/?.lua",
                    "share/lua/5.1/?/init.lua",
                ],
                "lua_cpaths": ["lib/lua/5.1/?.so"],
                "closure_files": sorted(closure_files),
            }
        ),
    )
    if symlink_runner:
        runner.unlink()
        runner.symlink_to(root / "outside-busted")
    return runtime_root


@pytest.mark.parametrize("change_number", [13132, 14757])
def test_lua_profiles_are_closed_to_the_two_registered_candidates(
    change_number: int,
) -> None:
    profile = profile_for_candidate(CandidateKey("luanti", change_number))

    assert profile.candidate == CandidateKey("luanti", change_number)
    assert profile.repetitions == 3
    assert profile.support_paths == ("builtin/common/vector.lua",)
    assert profile.test_paths == ("builtin/common/tests/misc_helpers_spec.lua",)
    assert profile.fixed_capsule_reference.artifact_id == (
        f"fixed_source_capsule/luanti/{change_number}/fixed/v2"
    )
    assert profile.fixed_capsule_reference.semantic_version == "2.0"
    assert profile.fixed_source_tree_path.endswith("/fixed-source-v2")
    assert all(not Path(path).is_absolute() for path in profile.test_paths)


def test_lua_profile_rejects_a_same_number_candidate_from_another_repository() -> None:
    with pytest.raises(ValueError, match="no registered Lua regression profile"):
        profile_for_candidate(CandidateKey("openra", 13132))


def test_missing_registered_busted_runtime_is_a_precise_typed_blocker(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("luanti", 13132)
    _write_runtime(tmp_path, runtime_relative="fallback-runtime")

    decision = plan_luanti_lua_regression_execution(tmp_path, candidate=candidate)

    assert isinstance(decision, StageDecision)
    assert decision.state == "terminal"
    assert decision.outcome == "infrastructure_blocked"
    assert decision.details == {
        "profile_id": "luanti-13132-empty-separator-v1",
        "reason": "offline_busted_runtime_unavailable",
        "dependency_id": OFFLINE_BUSTED_DEPENDENCY_ID,
        "semantic_version": "1.0",
        "runtime_root": OFFLINE_BUSTED_RUNTIME_ROOT,
        "missing_entries": [
            "runtime.json",
            "bin/busted",
            "bin/luajit",
            "share/lua/5.1",
            "lib/lua/5.1",
        ],
    }


def test_registered_runtime_reports_the_exact_missing_closure_entry(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("luanti", 14757)
    runtime_root = _write_runtime(tmp_path)
    (runtime_root / _RUNTIME_MODULES["say"]).unlink()

    decision = plan_luanti_lua_regression_execution(tmp_path, candidate=candidate)

    assert isinstance(decision, StageDecision)
    assert decision.outcome == "infrastructure_blocked"
    assert decision.details == {
        "profile_id": "luanti-14757-math-round-v1",
        "reason": "offline_busted_runtime_incomplete",
        "dependency_id": OFFLINE_BUSTED_DEPENDENCY_ID,
        "semantic_version": "1.0",
        "runtime_root": OFFLINE_BUSTED_RUNTIME_ROOT,
        "missing_entries": [_RUNTIME_MODULES["say"]],
    }


def test_registered_runtime_has_no_fallback_and_requires_safe_entries(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("luanti", 13132)
    _write_runtime(tmp_path, symlink_runner=True)

    decision = plan_luanti_lua_regression_execution(tmp_path, candidate=candidate)

    assert isinstance(decision, StageDecision)
    assert decision.outcome == "infrastructure_blocked"
    assert decision.details == {
        "profile_id": "luanti-13132-empty-separator-v1",
        "reason": "offline_busted_runtime_unsafe",
        "dependency_id": OFFLINE_BUSTED_DEPENDENCY_ID,
        "semantic_version": "1.0",
        "runtime_root": OFFLINE_BUSTED_RUNTIME_ROOT,
        "unsafe_entry": "bin/busted",
    }


def test_registered_runtime_rejects_a_symlinked_runtime_root(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("luanti", 13132)
    external_runtime = _write_runtime(tmp_path, runtime_relative="external-runtime")
    expected_runtime = tmp_path / OFFLINE_BUSTED_RUNTIME_ROOT
    expected_runtime.parent.mkdir(parents=True)
    expected_runtime.symlink_to(external_runtime, target_is_directory=True)

    decision = plan_luanti_lua_regression_execution(tmp_path, candidate=candidate)

    assert isinstance(decision, StageDecision)
    assert decision.outcome == "infrastructure_blocked"
    assert decision.details == {
        "profile_id": "luanti-13132-empty-separator-v1",
        "reason": "offline_busted_runtime_unsafe",
        "dependency_id": OFFLINE_BUSTED_DEPENDENCY_ID,
        "semantic_version": "1.0",
        "runtime_root": OFFLINE_BUSTED_RUNTIME_ROOT,
        "unsafe_entry": ".",
    }


@pytest.mark.parametrize("change_number", [13132, 14757])
def test_ready_plan_binds_fixed_capsule_and_only_reverses_production(
    tmp_path: Path,
    change_number: int,
) -> None:
    candidate = CandidateKey("luanti", change_number)
    profile = profile_for_candidate(candidate)
    _write_runtime(tmp_path)
    _write_source_packet(tmp_path, candidate)
    fixed_capsule = _write_fixed_capsule(tmp_path, candidate)

    plan = plan_luanti_lua_regression_execution(tmp_path, candidate=candidate)

    assert isinstance(plan, LuaRegressionExecutionPlan)
    assert plan.profile == profile
    assert plan.fixed_capsule == fixed_capsule
    assert plan.fixed_capsule.reference == profile.fixed_capsule_reference
    assert plan.fixed_capsule.support_paths == profile.support_paths
    assert plan.production_inverse.buggy_apply_argv == (
        "git",
        "apply",
        "-R",
        "--whitespace=nowarn",
        "-",
    )
    assert all(
        not argument.startswith("--lua=") for run in plan.runs for argument in run.argv
    )
    assert plan.production_inverse.paths == profile.production_paths
    assert profile.production_paths[0].encode() in plan.production_inverse.patch
    assert profile.test_paths[0].encode() not in plan.production_inverse.patch
    assert b"doc/lua_api.txt" not in plan.production_inverse.patch
    assert tuple(run.phase for run in plan.runs) == ("f2p", "p2p")
    assert tuple(run.repetitions for run in plan.runs) == (3, 3)
    assert all(run.test_path == profile.test_paths[0] for run in plan.runs)
    assert all(not Path(run.test_path).is_absolute() for run in plan.runs)
    assert tuple(run.argv[-1] for run in plan.runs) == (
        f"--name={profile.f2p_selector}",
        f"--name={profile.p2p_selector}",
    )


def test_ready_plan_never_invokes_the_watchdog_or_a_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from warptest.c1_lazarus_selection import luanti_lua_regression

    candidate = CandidateKey("luanti", 13132)
    _write_runtime(tmp_path)
    _write_source_packet(tmp_path, candidate)
    _write_fixed_capsule(tmp_path, candidate)

    def unexpected_process(*_args: object, **_kwargs: object) -> object:
        pytest.fail("planning must not execute a candidate process")

    monkeypatch.setattr(luanti_lua_regression.subprocess, "Popen", unexpected_process)

    plan = plan_luanti_lua_regression_execution(tmp_path, candidate=candidate)

    assert isinstance(plan, LuaRegressionExecutionPlan)


def test_fixed_capsule_rejects_path_escape_before_a_future_execution(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("luanti", 14757)
    _write_runtime(tmp_path)
    _write_source_packet(tmp_path, candidate)
    _write_fixed_capsule(tmp_path, candidate, source_tree_path="../outside")

    with pytest.raises(RuntimeError, match="fixed capsule source tree path is unsafe"):
        plan_luanti_lua_regression_execution(tmp_path, candidate=candidate)


def test_fixed_capsule_rejects_a_symlinked_clone_relative_test_source(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("luanti", 13132)
    profile = profile_for_candidate(candidate)
    _write_runtime(tmp_path)
    _write_source_packet(tmp_path, candidate)
    fixed_capsule = _write_fixed_capsule(tmp_path, candidate)
    test_source = fixed_capsule.source_tree / profile.test_paths[0]
    test_source.unlink()
    test_source.symlink_to(tmp_path / "outside-test-source")

    with pytest.raises(RuntimeError, match="fixed capsule source path is unsafe"):
        plan_luanti_lua_regression_execution(tmp_path, candidate=candidate)


def test_fixed_capsule_rejects_a_missing_or_linked_support_source(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("luanti", 14757)
    profile = profile_for_candidate(candidate)
    _write_runtime(tmp_path)
    _write_source_packet(tmp_path, candidate)
    fixed_capsule = _write_fixed_capsule(tmp_path, candidate)
    support_source = fixed_capsule.source_tree / profile.support_paths[0]
    support_source.unlink()

    decision = plan_luanti_lua_regression_execution(tmp_path, candidate=candidate)

    assert isinstance(decision, StageDecision)
    assert decision.outcome == "infrastructure_blocked"
    assert decision.details["reason"] == "fixed_capsule_source_unavailable"
    assert decision.details["missing_path"] == profile.support_paths[0]

    support_source.symlink_to(tmp_path / "outside-support-source")
    with pytest.raises(RuntimeError, match="fixed capsule source path is unsafe"):
        plan_luanti_lua_regression_execution(tmp_path, candidate=candidate)


def test_legacy_v1_capsule_cannot_satisfy_the_v2_planner(tmp_path: Path) -> None:
    candidate = CandidateKey("luanti", 13132)
    profile = profile_for_candidate(candidate)
    _write_runtime(tmp_path)
    _write_source_packet(tmp_path, candidate)
    legacy_tree = (
        f"private/fixed-capsules/luanti/{candidate.change_number}/fixed-source"
    )
    for relative in (*profile.production_paths, *profile.test_paths):
        _write(tmp_path, f"{legacy_tree}/{relative}", "legacy\n")
    write_stable_artifact(
        tmp_path,
        artifact_id=(f"fixed_source_capsule/luanti/{candidate.change_number}/fixed/v1"),
        artifact_kind="fixed_source_capsule",
        semantic_version="1.0",
        created_at="2026-08-13T12:00:02Z",
        inputs=(profile.source_packet_reference,),
        payload={
            "candidate": candidate.to_value(),
            "profile_id": profile.profile_id,
            "source_tree_path": legacy_tree,
            "test_paths": list(profile.test_paths),
        },
        authorization=_authorization(),
    )

    decision = plan_luanti_lua_regression_execution(tmp_path, candidate=candidate)

    assert isinstance(decision, StageDecision)
    assert decision.outcome == "infrastructure_blocked"
    assert decision.details == {
        "profile_id": profile.profile_id,
        "reason": "fixed_capsule_unavailable",
        "fixed_capsule_id": profile.fixed_capsule_reference.artifact_id,
    }


def test_watchdog_returns_a_timeout_receipt_without_running_a_real_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from warptest.c1_lazarus_selection import luanti_lua_regression

    calls: list[object] = []

    class TimedOutProcess:
        pid = 4321
        returncode: int | None = None
        timed_out = False

        def communicate(self, *, timeout: int | None = None) -> tuple[bytes, bytes]:
            calls.append(("communicate", timeout))
            if not self.timed_out:
                self.timed_out = True
                raise subprocess.TimeoutExpired(("busted",), timeout)
            self.returncode = -9
            return b"after-kill", b""

    process = TimedOutProcess()

    def fake_popen(*args: object, **kwargs: object) -> TimedOutProcess:
        calls.append(("popen", args, kwargs))
        return process

    monkeypatch.setattr(luanti_lua_regression.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        luanti_lua_regression.os,
        "killpg",
        lambda pid, signal: calls.append(("killpg", pid, signal)),
    )

    receipt = PythonSubprocessWatchdog(timeout_seconds=2).run(
        ("/registered/busted", "builtin/common/tests/misc_helpers_spec.lua"),
        cwd=tmp_path,
    )

    assert receipt == WatchdogReceipt(
        outcome="timed_out",
        returncode=-9,
        stdout=b"after-kill",
        stderr=b"",
    )
    assert calls[0][0] == "popen"
    assert ("killpg", 4321, luanti_lua_regression.signal.SIGKILL) in calls
