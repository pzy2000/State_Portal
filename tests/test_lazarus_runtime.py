from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from warptest import cost
from warptest.gui_agent import PhaseResult
from warptest.lazarus_orchestrator import LazarusTrialKey
from warptest.lazarus_phase0 import AgentVisibleSource, LazarusPhase0CellContext
from warptest.lazarus_runtime import (
    LazarusAgentSRuntimeRunner,
    LazarusRuntimeExecution,
    LazarusLLMStateSynthesis,
    LazarusStateRuntimeRunner,
    LazarusSynthesizedCheckpoint,
    LuantiLazarusAgentSExecution,
    LuantiLazarusStateExecution,
    OpenRAHotkeyLazarusAgentSExecution,
    OpenRAHotkeyLazarusStateExecution,
    phase0_runtime_support_report,
    validate_lazarus_phase0_runtime_support,
)
from warptest.task_schema import resolve_task_repo
from warptest.lazarus_versions import LazarusVersionArtifact, tree_fingerprint
from warptest.luanti_state import LuantiWarmSession


def _context(
    tmp_path: Path, *, version: str, arm: str = "lazarus_state"
) -> LazarusPhase0CellContext:
    artifact_root = tmp_path / f"artifact-{version}"
    artifact_root.mkdir()
    (artifact_root / "source.txt").write_text(version, encoding="utf-8")
    metadata = tmp_path / f"artifact-{version}.git"
    metadata.mkdir()
    agent_source = tmp_path / "clean-agent-source"
    agent_source.mkdir(exist_ok=True)
    return LazarusPhase0CellContext(
        key=LazarusTrialKey(
            task_id="lz-example",
            arm=arm,
            model="gpt-5.6-terra@relay",
            seed=0,
            version=version,
        ),
        task={
            "id": "lz-example",
            "benchmark": "luanti",
            "repo": "games/luanti",
            "state_description": "Preconditions: public\nReference steps: public",
            "preconditions": "public",
            "reference_steps": "public",
            "defect": {"private_marker": "never-visible-to-synthesis"},
        },
        artifact=LazarusVersionArtifact(
            task_id="lz-example",
            version=version,
            root=artifact_root,
            git_metadata=metadata,
            source_revision="a" * 40,
            patch_sha256="b" * 64,
            tree_sha256=tree_fingerprint(artifact_root),
            changed_files=("source.txt",),
            build_identity={},
            cache_hit=True,
        ),
        agent_source=AgentVisibleSource(
            task_id="lz-example",
            root=agent_source,
            trusted_git_dir=tmp_path / "agent-source.git",
            source_revision="a" * 40,
            source_tree_sha256="c" * 64,
            sanitized_tree_sha256="c" * 64,
            forbidden_tokens=("never-visible-to-synthesis",),
            version_identity_hidden=True,
        ),
        defect_locus="at_goal",
        per_trial_p2p_ids=("p2p",),
    )


def _luanti_phase0_context(
    tmp_path: Path, *, version: str, arm: str = "lazarus_state"
) -> LazarusPhase0CellContext:
    context = _context(tmp_path, version=version, arm=arm)
    task_id = "lz-luanti-11919-vector-from-string"
    return LazarusPhase0CellContext(
        **{
            **context.__dict__,
            "key": LazarusTrialKey(
                task_id=task_id,
                arm=arm,
                model=context.key.model,
                seed=context.key.seed,
                version=version,
            ),
            "task": {
                **context.task,
                "id": task_id,
                "defect": {
                    **context.task.get("defect", {}),
                    "defect_locus": "at_goal",
                },
                "state_description": (
                    "Preconditions: public fixed-seed sand and stone marker\n"
                    "Reference steps: public coordinate import"
                ),
            },
        }
    )


def _luanti_public_generated_spec() -> dict[str, object]:
    return {
        "name": "Public fixed-seed coordinate-import marker",
        "benchmark": "luanti",
        "target": {
            "kind": "synthesized_world",
            "nodes": [
                {
                    "pos": {"x": 0, "y": 0, "z": 0},
                    "node": "basenodes:sand",
                },
                {
                    "pos": {"x": 0, "y": 1, "z": 0},
                    "node": "basenodes:stone",
                },
            ],
            "player": {
                "pos": {"x": 0, "y": 3, "z": 6},
                "hover": True,
                "look_at": {"x": 0, "y": 1, "z": 0},
            },
            "validations": [
                {"path": "node.0,0,0", "expected": "basenodes:sand"},
                {
                    "path": "node.0,1,0",
                    "expected": "basenodes:stone",
                },
            ],
        },
        "checkpoint": {
            "allowed_strategies": ["luanti_worldmod_state_validate"],
            "expected_validity": "save-restorable",
        },
        "actions": [
            {
                "type": "luanti_import_vector_string",
                "value": "(1, 2, 3.14)",
                "marker_pos": {"x": 0, "y": 1, "z": 0},
            }
        ],
        "assertions": [{"type": "no_luanti_mod_errors"}],
    }


def test_luanti_warm_session_imports_an_isolated_start_snapshot(tmp_path: Path) -> None:
    snapshot = tmp_path / "agent-s-start-snapshot"
    (snapshot / "worldmods/warptest").mkdir(parents=True)
    (snapshot / "world.mt").write_text("gameid = devtest\n", encoding="utf-8")
    (snapshot / "map.sqlite").write_bytes(b"start-world")
    (snapshot / "worldmods/warptest/init.lua").write_text(
        "return 'stale'\n", encoding="utf-8"
    )
    repo = tmp_path / "version"
    (repo / "mods/warptest").mkdir(parents=True)
    (repo / "mods/warptest/init.lua").write_text(
        "return 'current'\n", encoding="utf-8"
    )
    output = tmp_path / "output"
    output.mkdir()

    session = LuantiWarmSession(
        repo,
        output,
        cache_root=tmp_path / "cache",
        initial_world_snapshot=snapshot,
    )
    world = session._prepare_world("warm")

    assert world != snapshot
    assert (world / "map.sqlite").read_bytes() == b"start-world"
    assert (world / "worldmods/warptest/init.lua").read_text(encoding="utf-8") == (
        "return 'current'\n"
    )
    (world / "map.sqlite").write_bytes(b"mutated-clone")
    assert (snapshot / "map.sqlite").read_bytes() == b"start-world"

    with pytest.raises(ValueError, match="initial_world_snapshot"):
        LuantiWarmSession(repo, output, initial_world_snapshot=tmp_path / "missing")


class _Synthesis:
    def __init__(self) -> None:
        self.calls: list[tuple[object, Path, Path, int, str]] = []

    def synthesize(self, issue, source_root, artifact_dir, *, seed, model):
        self.calls.append((issue, source_root, artifact_dir, seed, model))
        return LazarusSynthesizedCheckpoint(
            generated_spec={"target": {"kind": "synthesized_world"}, "actions": []},
            actual_synthesis_calls=1,
            treatment_tokens=13,
            treatment_time_ms=17,
            trace={"source": "fake-synthesis"},
        )


class _Execution:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.calls: list[tuple[str, dict[str, object], str]] = []

    def execute(self, context, generated_spec, *, checkpoint_sha256):
        self.calls.append((context.key.version, generated_spec, checkpoint_sha256))
        before = self.root / f"{context.key.version}-before.png"
        after = self.root / f"{context.key.version}-after.png"
        before.write_bytes(b"before")
        after.write_bytes(b"after")
        return LazarusRuntimeExecution(
            complete=True,
            runtime_restored=True,
            before_semantic_ok=True,
            goal_checks_ok=True,
            defect_signature_ok=False,
            invariants_ok=True,
            before_screenshot=before,
            after_screenshot=after,
            runtime_time_ms=23,
            trace={"source": "fake-runtime"},
        )


def test_state_runtime_hides_private_task_data_and_reuses_buggy_checkpoint(
    tmp_path: Path,
) -> None:
    synthesis = _Synthesis()
    execution = _Execution(tmp_path)
    runner = LazarusStateRuntimeRunner(
        synthesis=synthesis,
        execution=execution,
        artifact_root=tmp_path / "runtime-artifacts",
    )

    buggy = runner(_context(tmp_path, version="buggy"))
    fixed = runner(_context(tmp_path, version="fixed"))

    assert len(synthesis.calls) == 1
    issue, source_root, _artifact_dir, seed, model = synthesis.calls[0]
    assert issue.source_path is None
    assert "never-visible-to-synthesis" not in issue.issue
    assert source_root.name == "clean-agent-source"
    assert seed == 0
    assert model == "gpt-5.6-terra@relay"
    assert buggy.synthesis_cache_hit is False
    assert buggy.actual_synthesis_calls == 1
    assert fixed.synthesis_cache_hit is True
    assert fixed.actual_synthesis_calls == 0
    assert fixed.checkpoint_reuse_success is True
    assert fixed.checkpoint_sha256 == buggy.checkpoint_sha256
    assert fixed.treatment_tokens == 0
    assert [version for version, _spec, _sha in execution.calls] == ["buggy", "fixed"]
    assert all("never-visible-to-synthesis" not in str(spec) for _, spec, _ in execution.calls)


def test_state_runtime_refuses_fixed_before_the_registered_buggy_checkpoint(
    tmp_path: Path,
) -> None:
    runner = LazarusStateRuntimeRunner(
        synthesis=_Synthesis(),
        execution=_Execution(tmp_path),
        artifact_root=tmp_path / "runtime-artifacts",
    )

    with pytest.raises(ValueError, match="buggy"):
        runner(_context(tmp_path, version="fixed"))


def test_state_runtime_attaches_synthesis_usage_when_execution_raises(
    tmp_path: Path,
) -> None:
    class FailingExecution:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError("state execution failed")

    runner = LazarusStateRuntimeRunner(
        synthesis=_Synthesis(),
        execution=FailingExecution(),
        artifact_root=tmp_path / "runtime-artifacts",
    )

    with pytest.raises(RuntimeError, match="execution") as raised:
        runner(_context(tmp_path, version="buggy"))

    assert raised.value.lazarus_usage == {
        "llm_calls": 1,
        "total_tokens": 13,
        "treatment_time_ms": 17,
        "usage_complete": True,
    }


def test_state_runtime_keeps_usage_when_returned_execution_fails_validation(
    tmp_path: Path,
) -> None:
    class InvalidExecution:
        def execute(self, *_args, **_kwargs):
            return object()

    runner = LazarusStateRuntimeRunner(
        synthesis=_Synthesis(),
        execution=InvalidExecution(),
        artifact_root=tmp_path / "runtime-artifacts",
    )

    with pytest.raises(TypeError, match="LazarusRuntimeExecution") as raised:
        runner(_context(tmp_path, version="buggy"))

    assert raised.value.lazarus_usage["llm_calls"] == 1
    assert raised.value.lazarus_usage["total_tokens"] == 13
    assert raised.value.lazarus_usage["treatment_time_ms"] == 17


def test_state_runtime_keeps_usage_when_checkpoint_hashing_fails(
    tmp_path: Path,
) -> None:
    class NonJsonSynthesis(_Synthesis):
        def synthesize(self, *args, **kwargs):
            result = super().synthesize(*args, **kwargs)
            return LazarusSynthesizedCheckpoint(
                generated_spec={"target": {"bad": {1, 2}}},
                actual_synthesis_calls=result.actual_synthesis_calls,
                treatment_tokens=result.treatment_tokens,
                treatment_time_ms=result.treatment_time_ms,
                trace=result.trace,
            )

    runner = LazarusStateRuntimeRunner(
        synthesis=NonJsonSynthesis(),
        execution=_Execution(tmp_path),
        artifact_root=tmp_path / "runtime-artifacts",
    )

    with pytest.raises(ValueError, match="canonical JSON") as raised:
        runner(_context(tmp_path, version="buggy"))

    assert raised.value.lazarus_usage["llm_calls"] == 1
    assert raised.value.lazarus_usage["total_tokens"] == 13


def test_state_runtime_rejects_synthesis_or_runtime_self_reported_verdicts(
    tmp_path: Path,
) -> None:
    class VerdictSynthesis(_Synthesis):
        def synthesize(self, *args, **kwargs):
            result = super().synthesize(*args, **kwargs)
            return LazarusSynthesizedCheckpoint(
                generated_spec=result.generated_spec,
                actual_synthesis_calls=1,
                treatment_tokens=1,
                treatment_time_ms=1,
                trace={"goal_success": True},
            )

    runner = LazarusStateRuntimeRunner(
        synthesis=VerdictSynthesis(),
        execution=_Execution(tmp_path),
        artifact_root=tmp_path / "runtime-artifacts",
    )

    with pytest.raises(ValueError, match="final verdict"):
        runner(_context(tmp_path, version="buggy"))


def test_agent_s_runtime_restarts_each_version_and_records_only_policy_cost(
    tmp_path: Path,
) -> None:
    class Execution:
        def __init__(self) -> None:
            self.versions: list[str] = []

        def execute(self, context):
            self.versions.append(context.key.version)
            cost.record_call(
                "gpt-5.6-terra",
                prompt_tokens=3,
                completion_tokens=4,
                total_tokens=7,
                kind="agent_s_worker",
            )
            before = tmp_path / f"agent-{context.key.version}-before.png"
            after = tmp_path / f"agent-{context.key.version}-after.png"
            before.write_bytes(b"before")
            after.write_bytes(b"after")
            return LazarusRuntimeExecution(
                complete=True,
                runtime_restored=True,
                before_semantic_ok=True,
                goal_checks_ok=context.key.version == "fixed",
                defect_signature_ok=context.key.version == "buggy",
                invariants_ok=True,
                before_screenshot=before,
                after_screenshot=after,
                runtime_time_ms=23,
                trace={"source": "fake-agent-s-runtime"},
            )

    execution = Execution()
    runner = LazarusAgentSRuntimeRunner(execution=execution)

    buggy = runner(_context(tmp_path, version="buggy", arm="lazarus_agent_s"))
    fixed = runner(_context(tmp_path, version="fixed", arm="lazarus_agent_s"))

    assert execution.versions == ["buggy", "fixed"]
    assert buggy.synthesis_cache_hit is None
    assert fixed.synthesis_cache_hit is None
    assert buggy.actual_synthesis_calls == 0
    assert fixed.actual_synthesis_calls == 0
    assert buggy.checkpoint_reuse_success is None
    assert fixed.checkpoint_reuse_success is None
    assert buggy.treatment_tokens == 7
    assert fixed.treatment_tokens == 7
    assert buggy.checkpoint_sha256 != fixed.checkpoint_sha256
    assert buggy.trace["observer"] == {
        "model": "claude-sonnet-5@relay",
        "usage": "registered_not_consumed_native_screenshot",
        "llm_calls": 0,
    }


def test_agent_s_runtime_writes_the_policy_call_ledger_beside_the_frames(
    tmp_path: Path,
) -> None:
    output = tmp_path / "runtime" / "output"
    (output / "phase-a").mkdir(parents=True)
    (output / "phase-a" / "actions.json").write_text("{}", encoding="utf-8")

    class Execution:
        def execute(self, context):
            cost.record_call(
                "gpt-5.6-terra",
                prompt_tokens=3,
                completion_tokens=4,
                total_tokens=7,
                kind="agent_s_worker",
                messages=[{"role": "user", "content": "press the buildings hotkey"}],
                response="click(10, 20)",
            )
            before = tmp_path / "before.png"
            before.write_bytes(b"before")
            return LazarusRuntimeExecution(
                complete=False,
                runtime_restored=True,
                before_semantic_ok=None,
                goal_checks_ok=None,
                defect_signature_ok=None,
                invariants_ok=None,
                before_screenshot=before,
                after_screenshot=None,
                runtime_time_ms=23,
                trace={"source": "fake-agent-s-runtime"},
                artifact_dir=output,
            )

    observation = LazarusAgentSRuntimeRunner(execution=Execution())(
        _context(tmp_path, version="buggy", arm="lazarus_agent_s")
    )

    trajectory = observation.trace["trajectory"]
    assert trajectory["recorded"] is True
    assert trajectory["artifact_dir"] == str(output)
    assert trajectory["phase_actions"] == ["phase-a/actions.json"]
    ledger = json.loads(
        (output / trajectory["cost_ledger"]).read_text(encoding="utf-8")
    )
    call = ledger["usage"]["calls"][0]
    assert call["kind"] == "agent_s_worker"
    assert call["messages"] == [
        {"role": "user", "content": "press the buildings hotkey"}
    ]
    assert call["response"] == "click(10, 20)"


def test_agent_s_runtime_reports_an_unrecorded_trajectory_without_an_artifact_dir(
    tmp_path: Path,
) -> None:
    class Execution:
        def execute(self, context):
            cost.record_call(
                "gpt-5.6-terra",
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
                kind="agent_s_worker",
            )
            before = tmp_path / "before.png"
            before.write_bytes(b"before")
            return LazarusRuntimeExecution(
                complete=False,
                runtime_restored=True,
                before_semantic_ok=None,
                goal_checks_ok=None,
                defect_signature_ok=None,
                invariants_ok=None,
                before_screenshot=before,
                after_screenshot=None,
                runtime_time_ms=5,
                trace={"source": "fake-agent-s-runtime"},
            )

    observation = LazarusAgentSRuntimeRunner(execution=Execution())(
        _context(tmp_path, version="buggy", arm="lazarus_agent_s")
    )

    assert observation.trace["trajectory"] == {
        "schema": "c1-lazarus-agent-s-trajectory-v1",
        "recorded": False,
        "reason": "runtime_did_not_report_an_artifact_dir",
    }


def test_agent_s_runtime_attaches_policy_usage_when_execution_raises(
    tmp_path: Path,
) -> None:
    class FailingExecution:
        def execute(self, _context):
            cost.record_call(
                "gpt-5.6-terra",
                prompt_tokens=5,
                completion_tokens=3,
                total_tokens=8,
                latency_ms=19,
                kind="agent_s_worker",
            )
            raise RuntimeError("Agent-S execution failed")

    runner = LazarusAgentSRuntimeRunner(execution=FailingExecution())

    with pytest.raises(RuntimeError, match="Agent-S") as raised:
        runner(_context(tmp_path, version="buggy", arm="lazarus_agent_s"))

    assert raised.value.lazarus_usage["llm_calls"] == 1
    assert raised.value.lazarus_usage["total_tokens"] == 8
    assert raised.value.lazarus_usage["llm_latency_ms"] == 19
    assert raised.value.lazarus_usage["usage_complete"] is True


def test_agent_s_runtime_keeps_usage_when_returned_execution_fails_validation(
    tmp_path: Path,
) -> None:
    class InvalidExecution:
        def execute(self, _context):
            cost.record_call(
                "gpt-5.6-terra",
                prompt_tokens=5,
                completion_tokens=3,
                total_tokens=8,
                latency_ms=19,
                kind="agent_s_worker",
            )
            return object()

    runner = LazarusAgentSRuntimeRunner(execution=InvalidExecution())

    with pytest.raises(TypeError, match="LazarusRuntimeExecution") as raised:
        runner(_context(tmp_path, version="buggy", arm="lazarus_agent_s"))

    assert raised.value.lazarus_usage["llm_calls"] == 1
    assert raised.value.lazarus_usage["total_tokens"] == 8
    assert raised.value.lazarus_usage["llm_latency_ms"] == 19


def test_luanti_agent_s_execution_keeps_private_oracle_out_of_policy_instructions(
    tmp_path: Path,
) -> None:
    context = _luanti_phase0_context(
        tmp_path, version="buggy", arm="lazarus_agent_s"
    )
    spec_path = tmp_path / "specs" / "luanti.yaml"
    spec_path.parent.mkdir()
    spec_path.write_text(
        yaml.safe_dump(
            {
                "target": {"kind": "synthesized_world"},
                "evaluation": {
                    "lazarus": {
                        "goal_checks": [
                            {
                                "type": "luanti_lazarus_state_audit",
                                "field": "marker_node",
                                "expected": "goal-private-marker",
                            }
                        ],
                        "defect_signature": [
                            {
                                "type": "luanti_lazarus_state_audit",
                                "field": "marker_node",
                                "expected": "defect-private-marker",
                            }
                        ],
                        "invariant_checks": [{"type": "no_luanti_mod_errors"}],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    context = LazarusPhase0CellContext(
        **{
            **context.__dict__,
            "task": {
                **context.task,
                "oracle_spec": str(spec_path.relative_to(tmp_path)),
                "preconditions": "PUBLIC start condition",
                "reference_steps": "PUBLIC transition once",
            },
        }
    )
    sessions: list[_LuantiSession] = []
    policy_instructions: list[str] = []
    policy_kwargs: dict[str, object] = {}

    def session_factory(repo, out_dir, **kwargs):
        session = _LuantiSession(repo, out_dir, kwargs)
        session.marker_node = "goal-private-marker"
        sessions.append(session)
        return session

    class Client:
        metadata = {
            "local_code_enabled": False,
            "worker_observation_mode": "screenshot",
        }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def policy_factory(**kwargs):
        policy_kwargs.update(kwargs)
        return Client()

    def phase_runner(*, instruction, phase_dir, **_kwargs):
        policy_instructions.append(instruction)
        phase_dir.mkdir(parents=True, exist_ok=True)
        screenshot = phase_dir / "terminal.png"
        screenshot.write_bytes(b"phase")
        return PhaseResult(False, 1, str(screenshot), actions=[{"kind": "done"}])

    execution = LuantiLazarusAgentSExecution(
        workspace=tmp_path,
        runtime_root=tmp_path / "runtime",
        session_factory=session_factory,
        asset_sync=lambda root: {"status": "synced", "root": str(root)},
        runtime_factory=lambda _session: object(),
        phase_runner=phase_runner,
        policy_factory=policy_factory,
    )
    runner = LazarusAgentSRuntimeRunner(execution=execution)

    result = runner(context)

    assert result.complete is True
    assert result.before_semantic_ok is True
    # The fake world holds the goal marker, so exactly one of the two groups can
    # hold.  Both are still evaluated against the same live state.
    assert result.goal_checks_ok is True
    assert result.defect_signature_ok is False
    assert result.invariants_ok is True
    assert result.treatment_tokens == 0
    assert len(policy_instructions) == 2
    assert all("PUBLIC" in instruction for instruction in policy_instructions)
    assert all("private-marker" not in instruction for instruction in policy_instructions)
    assert policy_kwargs["policy_kind"] == "agent_s"
    assert policy_kwargs["grounder_model"] == context.key.model
    assert sessions[0].repo != context.artifact.root
    assert not (context.artifact.root / ".runtime-mutated").exists()

    assert result.p2p_restore_adapter is not None
    p2p_clone = tmp_path / "agent-s-p2p-clone"
    p2p_clone.mkdir()
    receipt_path = p2p_clone / ".warptest/lazarus/checkpoint-restore.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_payload = {
        "schema": "c1-lazarus-p2p-checkpoint-restore-v1",
        "schema_version": 1,
        "checkpoint_sha256": result.checkpoint_sha256,
        "checkpoint_kind": "from_start_public_replay",
        "runtime_restored": True,
    }
    receipt_bytes = (
        json.dumps(receipt_payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    receipt_path.write_bytes(receipt_bytes)
    restored = result.p2p_restore_adapter(
        p2p_clone,
        receipt_path,
        {
            "checkpoint_sha256": result.checkpoint_sha256,
            "checkpoint_kind": "from_start_public_replay",
            "runtime_restored": True,
            "payload_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        },
    )
    assert restored["state_restore_applied"] is True
    assert restored["full_restore_used"] is True
    assert restored["adapter"] == "luanti-agent-s-start-snapshot-restore-v1"
    snapshot_sha256 = result.trace["execution"]["start_snapshot_sha256"]
    assert restored["start_snapshot_sha256"] == snapshot_sha256
    assert sessions[-1].kwargs["render"] is True
    assert Path(sessions[-1].kwargs["initial_world_snapshot"]).is_dir()
    assert sessions[-1].calls[0] == ("restore", [], [])
    assert sessions[-1].calls[1][0:2] == ("fast", [])
    assert sessions[-1].calls[1][2][0]["type"] == "luanti_player_view_matches"

    snapshot = Path(sessions[-1].kwargs["initial_world_snapshot"])
    (snapshot / "world.mt").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="snapshot changed"):
        result.p2p_restore_adapter(
            p2p_clone,
            receipt_path,
            {
                "checkpoint_sha256": result.checkpoint_sha256,
                "checkpoint_kind": "from_start_public_replay",
                "runtime_restored": True,
                "payload_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            },
        )

    def wrong_view_session_factory(repo, out_dir, **kwargs):
        session = _LuantiSession(repo, out_dir, kwargs)
        session.public_view_ok = False
        return session

    wrong_view_execution = LuantiLazarusAgentSExecution(
        workspace=tmp_path,
        runtime_root=tmp_path / "wrong-view-runtime",
        session_factory=wrong_view_session_factory,
        asset_sync=lambda root: {"status": "synced", "root": str(root)},
        runtime_factory=lambda _session: object(),
        phase_runner=phase_runner,
        policy_factory=policy_factory,
    )
    wrong_view = LazarusAgentSRuntimeRunner(execution=wrong_view_execution)(context)
    assert wrong_view.complete is True
    assert wrong_view.before_semantic_ok is False
    assert wrong_view.after_screenshot is None
    assert wrong_view.p2p_restore_adapter is None
    assert len(policy_instructions) == 3


def test_luanti_agent_s_is_incomplete_when_start_consumes_the_step_budget(
    tmp_path: Path,
) -> None:
    context = _luanti_phase0_context(
        tmp_path,
        version="buggy",
        arm="lazarus_agent_s",
    )
    spec_path = tmp_path / "specs" / "luanti-budget.yaml"
    spec_path.parent.mkdir()
    spec_path.write_text(
        yaml.safe_dump(
            {
                "target": {"kind": "synthesized_world"},
                "evaluation": {
                    "lazarus": {
                        "goal_checks": [
                            {
                                "type": "luanti_lazarus_state_audit",
                                "field": "marker_node",
                                "expected": "basenodes:dirt_with_grass",
                            }
                        ],
                        "defect_signature": [
                            {
                                "type": "luanti_lazarus_state_audit",
                                "field": "marker_node",
                                "expected": "basenodes:desert_sand",
                            }
                        ],
                        "invariant_checks": [{"type": "no_luanti_mod_errors"}],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    context = LazarusPhase0CellContext(
        **{
            **context.__dict__,
            "task": {
                **context.task,
                "oracle_spec": str(spec_path.relative_to(tmp_path)),
            },
        }
    )
    calls: list[str] = []

    class Client:
        metadata = {
            "local_code_enabled": False,
            "worker_observation_mode": "screenshot",
        }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def phase_runner(*, instruction, phase_dir, **_kwargs):
        calls.append(instruction)
        phase_dir.mkdir(parents=True, exist_ok=True)
        screenshot = phase_dir / "terminal.png"
        screenshot.write_bytes(b"phase")
        return PhaseResult(False, 1, str(screenshot), actions=[{"kind": "done"}])

    result = LuantiLazarusAgentSExecution(
        workspace=tmp_path,
        runtime_root=tmp_path / "runtime",
        session_factory=lambda repo, out_dir, **kwargs: _LuantiSession(
            repo,
            out_dir,
            kwargs,
        ),
        asset_sync=lambda root: {"status": "synced", "root": str(root)},
        runtime_factory=lambda _session: object(),
        phase_runner=phase_runner,
        policy_factory=lambda **_kwargs: Client(),
        max_steps=1,
    ).execute(context)

    assert result.complete is False
    assert len(calls) == 1
    assert result.trace["phase_b"] == {
        "steps": 0,
        "elapsed_ms": 0,
        "incomplete": True,
        "action_count": 0,
        "not_run_reason": "decision_budget_exhausted_at_start",
    }
    # The decided sequence is persisted beside the phase frames, while the
    # fingerprinted phase trace keeps carrying counts only.
    assert result.artifact_dir is not None
    actions = json.loads(
        (result.artifact_dir / "phase-a" / "actions.json").read_text(encoding="utf-8")
    )
    assert actions["actions"] == [{"kind": "done"}]
    assert actions["action_count"] == 1
    assert result.trace["phase_a"] == {
        "steps": 1,
        "elapsed_ms": 0,
        "incomplete": False,
        "action_count": 1,
    }


def test_phase0_runtime_accepts_the_registered_openra_h2_adapter(
    tmp_path: Path,
) -> None:
    luanti = _luanti_phase0_context(
        tmp_path,
        version="buggy",
        arm="lazarus_state",
    )
    openra = LazarusPhase0CellContext(
        **{
            **luanti.__dict__,
                "task": {
                    **luanti.task,
                    "id": "lz-openra-21453-hotkey-ready-building",
                    "benchmark": "openra",
                    "preconditions": (
                        "The Red Alert allies-08a map is open with one completed "
                        "structure in placement mode."
                    ),
                    "defect": {"defect_locus": "on_path"},
                },
            "key": LazarusTrialKey(
                task_id="lz-openra-21453-hotkey-ready-building",
                arm="lazarus_state",
                model="gpt-5.6-terra@relay",
                seed=0,
                version="buggy",
            ),
        }
    )

    validate_lazarus_phase0_runtime_support((luanti, openra))

    report = phase0_runtime_support_report((luanti, openra))
    assert report["schema"] == "c1-lazarus-phase0-runtime-support-v1"
    assert report["complete"] is True
    assert report["provider_dispatch_permitted"] is True
    assert report["task_ids"] == [
        "lz-luanti-11919-vector-from-string",
        "lz-openra-21453-hotkey-ready-building",
    ]
    assert report["failures"] == []


def test_phase0_runtime_rejects_an_unregistered_luanti_task(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, version="buggy", arm="lazarus_state")
    context = LazarusPhase0CellContext(
        **{
            **context.__dict__,
            "task": {
                **context.task,
                "defect": {"defect_locus": "at_goal"},
            },
        }
    )

    report = phase0_runtime_support_report((context,))

    assert report["complete"] is False
    assert report["provider_dispatch_permitted"] is False
    assert report["failures"] == [
        "lz-example: no verified Phase-0 runtime for luanti"
    ]


def test_phase0_runtime_rejects_a_private_openra_map_not_named_by_the_public_start(
    tmp_path: Path,
) -> None:
    context = _openra_context(tmp_path, version="buggy", arm="lazarus_state")
    context = LazarusPhase0CellContext(
        **{
            **context.__dict__,
            "task": {
                **context.task,
                "preconditions": (
                    "A Red Alert match is open with one completed structure in "
                    "placement mode."
                ),
            },
        }
    )

    with pytest.raises(
        RuntimeError,
        match="registered map is absent from the public Start",
    ):
        validate_lazarus_phase0_runtime_support((context,))


def test_openra_state_execution_restores_the_checkpoint_then_replays_the_real_cycle(
    tmp_path: Path,
) -> None:
    context = _openra_context(tmp_path, version="buggy", arm="lazarus_state")
    context = _write_openra_oracle(tmp_path, context)
    calls: list[tuple[Path, Path, str, dict[str, object]]] = []

    def cycle_runner(
        source: Path,
        output: Path,
        *,
        checkpoint_sha256: str,
        checkpoint_semantics: dict[str, object],
        timeout_s: float = 120.0,
    ):
        calls.append((source, output, checkpoint_sha256, checkpoint_semantics))
        output.mkdir(parents=True, exist_ok=True)
        before = output / "before.png"
        after = output / "after.png"
        before.write_bytes(b"before")
        after.write_bytes(b"after")
        semantics_sha256 = hashlib.sha256(
            json.dumps(
                checkpoint_semantics,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "status": "goal_not_reached",
            "before": {
                "executed": True,
                "ready": True,
                "state_restore_applied": True,
                "state_restore_spec_sha256": semantics_sha256,
            },
            "after": {
                "executed": True,
                "raw_audit": {
                    "schema_version": 1,
                    "world_tick": 2,
                    "player": "Greece",
                    "queue_type": "Building",
                    "target": "warpready",
                    "queue_item": "warpready",
                    "queue_ready": True,
                    "target_hidden": True,
                    "palette_items": 41,
                    "placement_active": False,
                    "placement_cancel_observed": True,
                    "state_restore_error": "",
                    "probe_errors": [],
                },
            },
            "before_path": str(before),
            "after_path": str(after),
        }

    execution = OpenRAHotkeyLazarusStateExecution(
        workspace=tmp_path,
        runtime_root=tmp_path / "openra-state-runtime",
        cycle_runner=cycle_runner,
    )
    result = execution.execute(
        context,
        {
            "target": {"kind": "openra_map", "mod": "ra", "map": "allies-08a"},
            "checkpoint": {"allowed_strategies": ["openra_gameplay_production_validate"]},
            "actions": [{"type": "openra_build_actor", "queue": "Building"}],
            "assertions": [],
        },
        checkpoint_sha256="d" * 64,
    )

    assert result.complete is True
    assert result.runtime_restored is True
    assert result.before_semantic_ok is True
    assert result.goal_checks_ok is False
    assert result.defect_signature_ok is True
    assert result.invariants_ok is True
    assert result.before_screenshot is not None and result.before_screenshot.is_file()
    assert result.after_screenshot is not None and result.after_screenshot.is_file()
    assert calls[0][0] != context.artifact.root
    assert calls[0][2] == "d" * 64
    assert calls[0][3] == {
        "schema": "c1-lazarus-openra-generated-start-v1",
        "target": {"kind": "openra_map", "mod": "ra", "map": "allies-08a"},
        "action": {
            "type": "openra_build_actor",
            "queue": "Building",
            "selector": "completed_building_placement",
        },
    }
    assert result.p2p_restore_adapter is not None
    p2p_clone = tmp_path / "openra-isolated-p2p-clone"
    p2p_clone.mkdir()
    receipt_path = p2p_clone / ".warptest/lazarus/checkpoint-restore.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_payload = {
        "schema": "c1-lazarus-p2p-checkpoint-restore-v1",
        "schema_version": 1,
        "checkpoint_sha256": "d" * 64,
        "checkpoint_kind": "semantic_state",
        "runtime_restored": True,
    }
    receipt_bytes = (
        json.dumps(receipt_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    receipt_path.write_bytes(receipt_bytes)
    restore = result.p2p_restore_adapter(
        p2p_clone,
        receipt_path,
        {
            "checkpoint_sha256": "d" * 64,
            "checkpoint_kind": "semantic_state",
            "runtime_restored": True,
            "payload_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        },
    )
    assert restore["state_restore_applied"] is True
    assert restore["adapter"] == "openra-hotkey-isolated-state-restore-v1"
    assert len(calls) == 2
    assert calls[1][0] == p2p_clone
    assert calls[1][3] == calls[0][3]
    assert "goal_success" not in result.trace


def test_openra_agent_s_starts_without_the_state_checkpoint_and_uses_public_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from warptest import lazarus_runtime

    context = _openra_context(tmp_path, version="fixed", arm="lazarus_agent_s")
    context = _write_openra_oracle(tmp_path, context)
    monkeypatch.setenv("WARPTEST_OPENRA_HOTKEY_STATE_PATH", "must-not-reach-agent-s")
    instructions: list[str] = []
    minimum_world_ticks: list[int | None] = []
    session = _OpenRAAgentSession()

    real_wait_for_hotkey_audit = lazarus_runtime._wait_for_hotkey_audit

    def wait_for_hotkey_audit(path, *, deadline, minimum_world_tick=None):
        minimum_world_ticks.append(minimum_world_tick)
        return real_wait_for_hotkey_audit(
            path,
            deadline=deadline,
            minimum_world_tick=minimum_world_tick,
        )

    monkeypatch.setattr(
        lazarus_runtime,
        "_wait_for_hotkey_audit",
        wait_for_hotkey_audit,
    )

    class Client:
        metadata = {
            "local_code_enabled": False,
            "worker_observation_mode": "screenshot",
        }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def phase_runner(*, runtime, instruction, phase_dir, **_kwargs):
        instructions.append(instruction)
        phase_dir.mkdir(parents=True, exist_ok=True)
        if len(instructions) == 1:
            runtime.set_placement(True)
            actions = [{"kind": "key", "key": "e"}]
        else:
            runtime.set_placement(False)
            runtime.set_placement(True)
            actions = [
                {"kind": "click", "button": "right"},
                {"kind": "key", "key": "e"},
            ]
        screenshot = phase_dir / "terminal.png"
        screenshot.write_bytes(b"phase")
        return PhaseResult(False, len(actions), str(screenshot), actions=actions)

    execution = OpenRAHotkeyLazarusAgentSExecution(
        workspace=tmp_path,
        runtime_root=tmp_path / "openra-agent-runtime",
        session_factory=lambda repo, out_dir, **_kwargs: session.bind(repo, out_dir),
        phase_runner=phase_runner,
        policy_factory=lambda **_kwargs: Client(),
    )
    result = execution.execute(context)

    assert result.complete is True
    assert result.runtime_restored is True
    assert result.before_semantic_ok is True
    assert result.goal_checks_ok is True
    assert result.defect_signature_ok is False
    assert result.invariants_ok is True
    assert len(instructions) == 2
    assert all("PUBLIC" in instruction for instruction in instructions)
    assert all("private-marker" not in instruction for instruction in instructions)
    assert minimum_world_ticks == [None, 12, 13]
    assert session.state_checkpoint_was_absent is True
    assert session.repo != context.artifact.root
    assert result.artifact_dir is not None
    phase_b_actions = json.loads(
        (result.artifact_dir / "phase-b" / "actions.json").read_text(encoding="utf-8")
    )
    assert phase_b_actions["actions"] == [
        {"kind": "click", "button": "right"},
        {"kind": "key", "key": "e"},
    ]


def test_openra_agent_s_rejects_a_start_reached_without_the_buildings_hotkey(
    tmp_path: Path,
) -> None:
    context = _write_openra_oracle(
        tmp_path,
        _openra_context(tmp_path, version="fixed", arm="lazarus_agent_s"),
    )
    session = _OpenRAAgentSession()

    class Client:
        metadata = {
            "local_code_enabled": False,
            "worker_observation_mode": "screenshot",
        }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def phase_runner(*, runtime, phase_dir, **_kwargs):
        runtime.set_placement(True)
        phase_dir.mkdir(parents=True, exist_ok=True)
        screenshot = phase_dir / "terminal.png"
        screenshot.write_bytes(b"phase")
        return PhaseResult(
            False,
            1,
            str(screenshot),
            actions=[{"kind": "click", "button": "left"}],
        )

    result = OpenRAHotkeyLazarusAgentSExecution(
        workspace=tmp_path,
        runtime_root=tmp_path / "openra-agent-runtime",
        session_factory=lambda repo, out_dir, **_kwargs: session.bind(repo, out_dir),
        phase_runner=phase_runner,
        policy_factory=lambda **_kwargs: Client(),
    ).execute(context)

    assert result.complete is False
    assert result.trace["phase_a"]["missing_public_buildings_hotkey"] is True


def test_openra_state_execution_fails_closed_for_an_unmapped_private_group(
    tmp_path: Path,
) -> None:
    """Every registered Lazarus group must be evaluated, never silently ignored."""

    context = _openra_context(tmp_path, version="fixed", arm="lazarus_state")
    context = _write_openra_oracle(tmp_path, context)
    oracle_path = tmp_path / str(context.task["oracle_spec"])
    oracle = yaml.safe_load(oracle_path.read_text(encoding="utf-8"))
    oracle["evaluation"]["lazarus"]["invariant_checks"].append(
        {"type": "private-marker"}
    )
    oracle_path.write_text(yaml.safe_dump(oracle, sort_keys=False), encoding="utf-8")

    def cycle_runner(
        source: Path,
        output: Path,
        *,
        checkpoint_sha256: str,
        checkpoint_semantics: dict[str, object],
    ):
        output.mkdir(parents=True, exist_ok=True)
        before = output / "before.png"
        after = output / "after.png"
        before.write_bytes(b"before")
        after.write_bytes(b"after")
        semantics_sha256 = hashlib.sha256(
            json.dumps(
                checkpoint_semantics,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "status": "goal_not_reached",
            "before": {
                "executed": True,
                "ready": True,
                "state_restore_applied": True,
                "state_restore_spec_sha256": semantics_sha256,
            },
            "after": {
                "executed": True,
                "raw_audit": {
                    "schema_version": 1,
                    "world_tick": 2,
                    "player": "Greece",
                    "queue_type": "Building",
                    "target": "warpready",
                    "queue_item": "warpready",
                    "queue_ready": True,
                    "target_hidden": True,
                    "palette_items": 41,
                    "placement_active": False,
                    "placement_cancel_observed": True,
                    "state_restore_error": "",
                    "probe_errors": [],
                },
            },
            "before_path": str(before),
            "after_path": str(after),
        }

    execution = OpenRAHotkeyLazarusStateExecution(
        workspace=tmp_path,
        runtime_root=tmp_path / "unmapped-group-runtime",
        cycle_runner=cycle_runner,
    )
    result = execution.execute(
        context,
        {
            "target": {"kind": "openra_map", "mod": "ra", "map": "allies-08a"},
            "checkpoint": {"allowed_strategies": ["openra_gameplay_production_validate"]},
            "actions": [{"type": "openra_build_actor", "queue": "Building"}],
            "assertions": [],
        },
        checkpoint_sha256="d" * 64,
    )

    assert result.complete is True
    assert result.invariants_ok is False


def test_luanti_state_execution_uses_model_transition_then_private_group_checks(
    tmp_path: Path,
) -> None:
    context = _luanti_phase0_context(tmp_path, version="buggy")
    spec_path = tmp_path / "specs" / "luanti.yaml"
    spec_path.parent.mkdir()
    spec_path.write_text(
        yaml.safe_dump(
            {
                "target": {"kind": "synthesized_world"},
                "evaluation": {
                    "lazarus": {
                        "goal_checks": [
                            {
                                "type": "luanti_lazarus_state_audit",
                                "field": "marker_node",
                                "expected": "basenodes:dirt_with_grass",
                            }
                        ],
                        "defect_signature": [
                            {
                                "type": "luanti_lazarus_state_audit",
                                "field": "marker_node",
                                "expected": "basenodes:desert_sand",
                            }
                        ],
                        "invariant_checks": [{"type": "no_luanti_mod_errors"}],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    context = LazarusPhase0CellContext(
        **{
            **context.__dict__,
            "task": {**context.task, "oracle_spec": str(spec_path.relative_to(tmp_path))},
        }
    )
    sessions: list[_LuantiSession] = []
    settled: list[float] = []

    def session_factory(repo, out_dir, **kwargs):
        session = _LuantiSession(repo, out_dir, kwargs)
        sessions.append(session)
        return session

    executor = LuantiLazarusStateExecution(
        workspace=tmp_path,
        runtime_root=tmp_path / "runtime",
        session_factory=session_factory,
        asset_sync=lambda root: {"status": "synced", "files": 1, "root": str(root)},
        sleep_fn=settled.append,
    )
    generated = _luanti_public_generated_spec()

    result = executor.execute(context, generated, checkpoint_sha256="d" * 64)

    assert result.complete is True
    assert result.runtime_restored is True
    assert result.before_semantic_ok is True
    assert result.goal_checks_ok is False
    assert result.defect_signature_ok is True
    assert result.invariants_ok is True
    assert result.before_screenshot is not None and result.before_screenshot.is_file()
    assert result.after_screenshot is not None and result.after_screenshot.is_file()
    assert len(sessions) == 1
    session = sessions[0]
    assert session.repo != context.artifact.root
    assert not (context.artifact.root / ".runtime-mutated").exists()
    assert session.kwargs["daylight_mode"] == "fixed_noon"
    assert session.calls[0] == ("restore", [], [])
    assert session.calls[1] == ("restore", [], [])
    assert session.specs[1]["target"] == {
        "kind": "synthesized_world",
        "player": generated["target"]["player"],
    }
    assert session.calls[2][0:2] == ("fast", [])
    assert session.calls[2][2][0]["type"] == "luanti_player_view_matches"
    assert session.calls[3] == ("fast", generated["actions"], [])
    # The published groups reach the session translated into live-world
    # assertions at the Start's registered marker, never as audit-field lookups.
    marker = {"x": 0, "y": 1, "z": 0}
    assert session.calls[4] == (
        "fast",
        [],
        [
            {
                "type": "luanti_node_is",
                "pos": marker,
                "expected": "basenodes:dirt_with_grass",
            }
        ],
    )
    assert session.calls[5] == (
        "fast",
        [],
        [
            {
                "type": "luanti_node_is",
                "pos": marker,
                "expected": "basenodes:desert_sand",
            }
        ],
    )
    assert session.calls[6] == ("fast", [], [{"type": "no_luanti_mod_errors"}])
    assert settled == [16.0, 2.0]

    assert result.p2p_restore_adapter is not None
    p2p_clone = tmp_path / "isolated-p2p-clone"
    p2p_clone.mkdir()
    receipt_path = p2p_clone / ".warptest/lazarus/checkpoint-restore.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_payload = {
        "schema": "c1-lazarus-p2p-checkpoint-restore-v1",
        "schema_version": 1,
        "checkpoint_sha256": "d" * 64,
        "checkpoint_kind": "semantic_state",
        "runtime_restored": True,
    }
    receipt_bytes = (
        json.dumps(receipt_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    receipt_path.write_bytes(receipt_bytes)
    receipt = {
        "checkpoint_sha256": "d" * 64,
        "checkpoint_kind": "semantic_state",
        "runtime_restored": True,
        "payload_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
    }
    restored = result.p2p_restore_adapter(p2p_clone, receipt_path, receipt)

    assert restored["state_restore_applied"] is True
    assert restored["adapter"] == "luanti-isolated-checkpoint-restore-v1"
    assert len(sessions) == 2
    assert sessions[1].repo == p2p_clone


def test_luanti_state_execution_rejects_a_checkpoint_that_misses_public_start(
    tmp_path: Path,
) -> None:
    context = _luanti_phase0_context(tmp_path, version="buggy")
    spec_path = tmp_path / "specs" / "luanti.yaml"
    spec_path.parent.mkdir()
    spec_path.write_text(
        yaml.safe_dump(
            {
                "target": {"kind": "synthesized_world"},
                "evaluation": {
                    "lazarus": {
                        "goal_checks": [
                            {
                                "type": "luanti_lazarus_state_audit",
                                "field": "marker_node",
                                "expected": "basenodes:dirt_with_grass",
                            }
                        ],
                        "defect_signature": [
                            {
                                "type": "luanti_lazarus_state_audit",
                                "field": "marker_node",
                                "expected": "basenodes:desert_sand",
                            }
                        ],
                        "invariant_checks": [{"type": "no_luanti_mod_errors"}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    context = LazarusPhase0CellContext(
        **{
            **context.__dict__,
            "task": {**context.task, "oracle_spec": str(spec_path.relative_to(tmp_path))},
        }
    )
    generated = _luanti_public_generated_spec()
    generated["target"]["nodes"][1]["node"] = "basenodes:water_source"
    executor = LuantiLazarusStateExecution(
        workspace=tmp_path,
        runtime_root=tmp_path / "mismatched-runtime",
        session_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("runtime must not launch")
        ),
        asset_sync=lambda _root: {"status": "synced"},
    )

    with pytest.raises(ValueError, match="nodes drift from the public Start"):
        executor.execute(context, generated, checkpoint_sha256="d" * 64)

    injected = _luanti_public_generated_spec()
    injected["target"]["fill"] = []
    with pytest.raises(ValueError, match="undeclared Start fields"):
        executor.execute(context, injected, checkpoint_sha256="d" * 64)

    wrong_view = _luanti_public_generated_spec()
    wrong_view["target"]["player"]["pos"]["z"] = 5
    with pytest.raises(ValueError, match="player view drifts"):
        executor.execute(context, wrong_view, checkpoint_sha256="d" * 64)


def test_llm_state_synthesis_reads_only_the_clean_agent_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path, version="buggy")
    captured: dict[str, object] = {}
    monkeypatch.delenv("WARPTEST_SYNTHESIS_TIMEOUT", raising=False)

    class Agent:
        def synthesize(self, issue, root, artifact_dir, **_kwargs):
            captured["issue"] = issue
            captured["repo"] = resolve_task_repo(root, issue)
            captured["artifact_dir"] = artifact_dir
            captured["synthesis_timeout"] = os.environ.get(
                "WARPTEST_SYNTHESIS_TIMEOUT"
            )
            return SimpleNamespace(
                model_generated_spec={
                    "target": {"kind": "synthesized_world"},
                    "actions": [],
                },
                hidden_files_read=[],
                used_public_scaffold=False,
                source_evidence=[],
            )

    synthesis = LazarusLLMStateSynthesis(
        workspace=tmp_path,
        agent_factory=lambda **_kwargs: Agent(),
        provider_resolver=lambda _model, **_kwargs: (
            "gpt-5.6-terra",
            SimpleNamespace(name="relay", base_url="https://example.invalid", api_key_env="KEY", extra_headers={}),
        ),
        acceptor=lambda candidate, **_kwargs: candidate,
        usage_summary=lambda: {"llm_calls": 1, "total_tokens": 19},
    )
    artifact_dir = tmp_path / "synthesis"
    artifact_dir.mkdir()

    result = synthesis.synthesize(
        issue=SimpleNamespace(
            id=context.task["id"],
            benchmark=context.task["benchmark"],
            repo=context.task["repo"],
            issue=context.task["state_description"],
            tags=context.task.get("tags", []),
            budget_ms=2_400_000,
            prompt_revision="lz-v1",
            random_control={},
            source_path=None,
        ),
        source_root=context.agent_source.root,
        artifact_dir=artifact_dir,
        seed=0,
        model="gpt-5.6-terra@relay",
    )

    assert result.actual_synthesis_calls == 1
    assert result.treatment_tokens == 19
    assert captured["issue"].source_path is None
    assert captured["repo"] == context.agent_source.root
    assert captured["artifact_dir"] == artifact_dir
    assert captured["synthesis_timeout"] == "900"
    # The trace names the directory holding the agent's prompt/response files so
    # a reader can reach the synthesis transcript from the run report alone.
    assert result.trace["artifact_dir"] == str(artifact_dir)
    ledger = json.loads(
        (artifact_dir / result.trace["cost_ledger"]).read_text(encoding="utf-8")
    )
    assert ledger["usage"] == {"llm_calls": 1, "total_tokens": 19}


def test_llm_state_synthesis_attaches_usage_when_checkpoint_finalization_fails(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, version="buggy")

    class Agent:
        def synthesize(self, *_args, **_kwargs):
            cost.record_call(
                "gpt-5.6-terra",
                prompt_tokens=11,
                completion_tokens=7,
                total_tokens=18,
                latency_ms=23,
                kind="checkpoint_synthesis",
            )
            raise RuntimeError("checkpoint finalization rejected")

    synthesis = LazarusLLMStateSynthesis(
        workspace=tmp_path,
        agent_factory=lambda **_kwargs: Agent(),
        provider_resolver=lambda _model, **_kwargs: (
            "gpt-5.6-terra",
            SimpleNamespace(
                name="relay",
                base_url="https://example.invalid",
                api_key_env="KEY",
                extra_headers={},
            ),
        ),
    )

    with pytest.raises(RuntimeError, match="finalization") as raised:
        synthesis.synthesize(
            issue=SimpleNamespace(
                id=context.task["id"],
                benchmark=context.task["benchmark"],
                repo=context.task["repo"],
                issue=context.task["state_description"],
                tags=context.task.get("tags", []),
                budget_ms=2_400_000,
                prompt_revision="lz-v1",
                random_control={},
                source_path=None,
            ),
            source_root=context.agent_source.root,
            artifact_dir=tmp_path / "failed-synthesis",
            seed=0,
            model="gpt-5.6-terra@relay",
        )

    usage = raised.value.lazarus_usage
    assert usage["llm_calls"] == 1
    assert usage["total_tokens"] == 18
    assert usage["llm_latency_ms"] == 23
    assert usage["usage_complete"] is True


def test_llm_state_synthesis_keeps_usage_when_acceptance_rejects_checkpoint(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, version="buggy")

    class Agent:
        def synthesize(self, *_args, **_kwargs):
            cost.record_call(
                "gpt-5.6-terra",
                prompt_tokens=9,
                completion_tokens=4,
                total_tokens=13,
                latency_ms=29,
                kind="checkpoint_synthesis",
            )
            return SimpleNamespace(
                model_generated_spec={"target": {"kind": "synthesized_world"}},
                hidden_files_read=[],
                used_public_scaffold=False,
            )

    synthesis = LazarusLLMStateSynthesis(
        workspace=tmp_path,
        agent_factory=lambda **_kwargs: Agent(),
        provider_resolver=lambda _model, **_kwargs: (
            "gpt-5.6-terra",
            SimpleNamespace(
                name="relay",
                base_url="https://example.invalid",
                api_key_env="KEY",
                extra_headers={},
            ),
        ),
        acceptor=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("checkpoint acceptance failed")
        ),
    )

    with pytest.raises(RuntimeError, match="acceptance") as raised:
        synthesis.synthesize(
            issue=SimpleNamespace(
                id=context.task["id"],
                benchmark=context.task["benchmark"],
                repo=context.task["repo"],
                issue=context.task["state_description"],
                tags=context.task.get("tags", []),
                budget_ms=2_400_000,
                prompt_revision="lz-v1",
                random_control={},
                source_path=None,
            ),
            source_root=context.agent_source.root,
            artifact_dir=tmp_path / "rejected-synthesis",
            seed=0,
            model="gpt-5.6-terra@relay",
        )

    assert raised.value.lazarus_usage["llm_calls"] == 1
    assert raised.value.lazarus_usage["total_tokens"] == 13
    assert raised.value.lazarus_usage["llm_latency_ms"] == 29


def _openra_context(
    tmp_path: Path, *, version: str, arm: str
) -> LazarusPhase0CellContext:
    base = _context(tmp_path, version=version, arm=arm)
    task_id = "lz-openra-21453-hotkey-ready-building"
    artifact = LazarusVersionArtifact(
        **{**base.artifact.__dict__, "task_id": task_id}
    )
    agent_source = AgentVisibleSource(
        **{**base.agent_source.__dict__, "task_id": task_id}
    )
    return LazarusPhase0CellContext(
        key=LazarusTrialKey(
            task_id=task_id,
            arm=arm,
            model="gpt-5.6-terra@relay",
            seed=0,
            version=version,
        ),
        task={
            "id": task_id,
            "benchmark": "openra",
            "repo": "games/OpenRA",
            "tags": ["c1", "lazarus", "openra"],
            "state_description": "Preconditions: PUBLIC placement start\nReference steps: PUBLIC repeat transition",
            "preconditions": "PUBLIC placement start",
            "reference_steps": "PUBLIC repeat transition",
            "defect": {
                "private_marker": "private-marker",
                "defect_locus": "on_path",
            },
        },
        artifact=artifact,
        agent_source=agent_source,
        defect_locus="on_path",
        per_trial_p2p_ids=("p2p",),
    )


def _write_openra_oracle(
    tmp_path: Path, context: LazarusPhase0CellContext
) -> LazarusPhase0CellContext:
    path = tmp_path / "specs" / "openra-hotkey.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "target": {"kind": "openra_map", "mod": "ra", "map": "allies-08a"},
                "evaluation": {
                    "lazarus": {
                        "goal_checks": [
                            {"type": "openra_lazarus_hotkey_audit", "field": "placement_active", "expected": True}
                        ],
                        "defect_signature": [
                            {"type": "openra_lazarus_hotkey_audit", "field": "placement_active", "expected": False}
                        ],
                        "invariant_checks": [
                            {"type": "openra_lazarus_hotkey_audit", "field": "ready_hidden_building", "expected": True},
                            {"type": "openra_lazarus_hotkey_audit", "field": "placement_cancel_observed", "expected": True},
                            {"type": "no_openra_gameplay_probe_errors"},
                        ],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return LazarusPhase0CellContext(
        **{
            **context.__dict__,
            "task": {
                **context.task,
                "oracle_spec": str(path.relative_to(tmp_path)),
            },
        }
    )


class _OpenRAAgentSession:
    def __init__(self) -> None:
        self.repo: Path | None = None
        self.out_dir: Path | None = None
        self._world_tick = 10
        self._placement_active = False
        self._placement_cancel_observed = False
        self.state_checkpoint_was_absent = False

    def bind(self, repo: Path, out_dir: Path) -> "_OpenRAAgentSession":
        self.repo = Path(repo)
        self.out_dir = Path(out_dir)
        return self

    def __enter__(self) -> "_OpenRAAgentSession":
        assert self.out_dir is not None
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.state_checkpoint_was_absent = (
            os.environ.get("WARPTEST_OPENRA_HOTKEY_STATE_PATH") is None
        )
        self.set_placement(False)
        return self

    def __exit__(self, *_args) -> None:
        return None

    def start(self) -> "_OpenRAAgentSession":
        return self

    def set_placement(self, value: bool) -> None:
        if self._placement_active and not value:
            self._placement_cancel_observed = True
        self._placement_active = value
        self._world_tick += 1
        audit_path = Path(os.environ["WARPTEST_OPENRA_HOTKEY_AUDIT_PATH"])
        audit_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "world_tick": self._world_tick,
                    "player": "Greece",
                    "queue_type": "Building",
                    "target": "warpready",
                    "queue_item": "warpready",
                    "queue_ready": True,
                    "target_hidden": True,
                    "palette_items": 41,
                    "placement_active": value,
                    "placement_cancel_observed": self._placement_cancel_observed,
                    "state_restore_requested": False,
                    "state_restore_applied": False,
                    "state_checkpoint_sha256": "",
                    "state_restore_error": "",
                    "probe_errors": [],
                }
            ),
            encoding="utf-8",
        )


class _Outcome:
    def __init__(self, *, status: str, checks: list[dict[str, object]]) -> None:
        self.status = status
        self.checks = checks
        self.detail = status


class _LuantiSession:
    def __init__(self, repo: Path, out_dir: Path, kwargs: dict[str, object]) -> None:
        self.repo = Path(repo)
        self.out_dir = Path(out_dir)
        self.kwargs = kwargs
        self.calls: list[tuple[str, list[object], list[object]]] = []
        self.specs: list[dict[str, object]] = []
        self.bootstrap_ms = 5
        self.public_view_ok = True
        # How many leading public-view checks report an unsettled camera.
        self.public_view_failures = 0
        # The node the replay left at the marker.  The default is the reverted
        # engine's outcome, so a goal group fails and a defect group passes.
        self.marker_node = "basenodes:desert_sand"

    def __enter__(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.repo / ".runtime-mutated").write_text("runtime", encoding="utf-8")
        return self

    def __exit__(self, *_args):
        return None

    def restore(self, spec, *, actions, assertions):
        self.specs.append(spec)
        self.calls.append(("restore", list(actions), list(assertions)))
        return _Outcome(status="success", checks=[])

    def fast_restore(self, spec, *, actions, assertions):
        self.calls.append(("fast", list(actions), list(assertions)))
        target = spec.get("target")
        if target != {"kind": "world_loaded"}:
            if (
                isinstance(target, dict)
                and target.get("kind") == "world_loaded"
                and spec.get("validations")
            ):
                view_assertions = [
                    assertion
                    for assertion in assertions
                    if assertion.get("type") == "luanti_player_view_matches"
                ]
                view_ok = not view_assertions or self.public_view_ok
                if view_assertions and self.public_view_failures > 0:
                    self.public_view_failures -= 1
                    view_ok = False
                return _Outcome(
                    status="success" if view_ok else "failure",
                    checks=[
                        {"name": "target.validate.0", "status": "success"},
                        {"name": "target.validate.1", "status": "success"},
                        *(
                            [
                                {
                                    "name": "assertion[luanti_player_view_matches]@1",
                                    "status": "success" if view_ok else "failure",
                                }
                            ]
                            if view_assertions
                            else []
                        ),
                    ],
                )
            return _Outcome(
                status="success",
                checks=[{"name": "target.validate.0", "status": "success"}],
            )
        # Model the live world rather than the group label: it holds one marker
        # node, so a node assertion passes only when it names that node.  This is
        # what separates the goal group from the defect group in a real trial.
        passed = all(
            assertion.get("expected") == self.marker_node
            for assertion in assertions
            if assertion.get("type") == "luanti_node_is"
        )
        return _Outcome(
            status="success" if passed else "failure",
            checks=[
                {
                    "name": "assertion[0]",
                    "status": "success" if passed else "failure",
                }
            ],
        )

    def screenshot(self, name):
        path = self.out_dir / name
        path.write_bytes(name.encode("utf-8"))
        return str(path)

    def snapshot_world(self, fingerprint: str) -> str:
        snapshot = self.out_dir / "snapshots" / fingerprint
        snapshot.mkdir(parents=True, exist_ok=True)
        (snapshot / "world.mt").write_text("gameid = devtest\n", encoding="utf-8")
        return str(snapshot)
