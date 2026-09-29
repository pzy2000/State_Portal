from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from warptest.lazarus_grading import VisualJudgement
from warptest.lazarus_mining import build_upstream_record
from warptest.lazarus_orchestrator import LazarusTrialKey
from warptest.lazarus_phase0 import (
    AgentVisibleSource,
    LazarusPhase0CellContext,
    build_lazarus_phase0_executor,
)
from warptest.lazarus_trial import LazarusRuntimeObservation, LazarusVisualEvidence
from warptest.lazarus_versions import LazarusVersionArtifact, tree_fingerprint


def _checkpoint_restore(checkpoint_sha256: str) -> dict[str, object]:
    payload = {
        "schema": "c1-lazarus-p2p-checkpoint-restore-v1",
        "schema_version": 1,
        "checkpoint_sha256": checkpoint_sha256,
        "checkpoint_kind": "semantic_state",
        "runtime_restored": True,
    }
    encoded = (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    return {
        "schema_version": 1,
        "applied": True,
        "checkpoint_sha256": checkpoint_sha256,
        "checkpoint_kind": "semantic_state",
        "runtime_restored": True,
        "path": ".warptest/lazarus/checkpoint-restore.json",
        "payload_sha256": hashlib.sha256(encoded).hexdigest(),
        "state_restore": {
            "schema_version": 1,
            "state_restore_applied": True,
            "adapter": "test-isolated-checkpoint-restorer-v1",
            "checkpoint_sha256": checkpoint_sha256,
            "checkpoint_kind": "semantic_state",
            "receipt_sha256": hashlib.sha256(encoded).hexdigest(),
        },
    }


def _context(tmp_path: Path) -> LazarusPhase0CellContext:
    root = tmp_path / "artifact"
    root.mkdir()
    (root / "source.txt").write_text("source\n", encoding="utf-8")
    upstream_test = root / "tests" / "example_test.py"
    upstream_test.parent.mkdir()
    # The buggy inverse patch can legitimately revert the regression test too;
    # the registered command executes the clean pinned copy created below.
    upstream_test.write_text(
        "# reverted with the historical fix\n", encoding="utf-8"
    )
    git_metadata = tmp_path / "metadata.git"
    git_metadata.mkdir()
    artifact = LazarusVersionArtifact(
        task_id="lz-example",
        version="buggy",
        root=root,
        git_metadata=git_metadata,
        source_revision="a" * 40,
        patch_sha256="b" * 64,
        tree_sha256=tree_fingerprint(root),
        changed_files=("source.txt",),
        build_identity={},
        cache_hit=True,
    )
    source = tmp_path / "agent-source"
    source.mkdir()
    return LazarusPhase0CellContext(
        key=LazarusTrialKey(
            task_id="lz-example",
            arm="lazarus_state",
            model="gpt-5.6-terra@relay",
            seed=0,
            version="buggy",
        ),
        task={
            "id": "lz-example",
            "repo": "games/luanti",
            "oracle_spec": "specs/example.yaml",
            "defect": {
                "defect_locus": "at_goal",
                "upstream_record": "defects/example-upstream.json",
            },
        },
        artifact=artifact,
        agent_source=AgentVisibleSource(
            task_id="lz-example",
            root=source,
            trusted_git_dir=tmp_path / "agent.git",
            source_revision="a" * 40,
            source_tree_sha256="c" * 64,
            sanitized_tree_sha256="c" * 64,
            forbidden_tokens=(),
            version_identity_hidden=True,
        ),
        defect_locus="at_goal",
        per_trial_p2p_ids=("p2p",),
        version_switch_ms=12.5,
    )


def _write_private_inputs(root: Path) -> None:
    spec_dir = root / "specs"
    spec_dir.mkdir()
    visual = spec_dir / "visual"
    visual.mkdir()
    for name in ("before.png", "fixed.png", "buggy.png"):
        (visual / name).write_bytes(name.encode("utf-8"))
    (spec_dir / "example.yaml").write_text(
        yaml.safe_dump(
            {
                "evaluation": {
                    "lazarus": {
                        "goldens": {
                            "before": "visual/before.png",
                            "fixed": {
                                "after": "visual/fixed.png",
                                "after_description": "fixed",
                                "after_rubric": {},
                            },
                            "buggy": {
                                "after": "visual/buggy.png",
                                "after_description": "buggy",
                                "after_rubric": {},
                            },
                        }
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    defects = root / "defects"
    defects.mkdir()
    upstream_test = root / "games" / "luanti" / "tests" / "example_test.py"
    upstream_test.parent.mkdir(parents=True)
    upstream_test.write_text("def test_example():\n    assert True\n", encoding="utf-8")
    upstream_test_sha256 = hashlib.sha256(upstream_test.read_bytes()).hexdigest()
    record = build_upstream_record(
        task_id="lz-example",
        benchmark="luanti",
        authority="github",
        repository="luanti-org/luanti",
        issue={"body": "A reproducible test case."},
        pull_request=None,
        comments=[],
        reviews=[],
        fix_commit="a" * 40,
        pinned_revision="b" * 40,
        touched_files=["builtin/game/falling.lua"],
        patch_sha256="c" * 64,
        tests=[
            {
                "id": "f2p",
                "kind": "fail_to_pass",
                "provenance": "repository_test",
                "command": ["manual", "run"],
                "argv": ["{python}", "-V"],
                "cwd": ".",
                "timeout_s": 10,
                "origin": "upstream",
                "upstream_test_path": "tests/example_test.py",
                "upstream_test_sha256": upstream_test_sha256,
                "source_url": "https://github.com/luanti-org/luanti/blob/master/tests/example_test.py",
            },
            {
                "id": "p2p",
                "kind": "pass_to_pass",
                "provenance": "repository_test",
                "command": ["manual", "run"],
                "argv": ["{python}", "-V"],
                "cwd": ".",
                "timeout_s": 10,
                "origin": "upstream",
                "upstream_test_path": "tests/example_test.py",
                "upstream_test_sha256": upstream_test_sha256,
                "source_url": "https://github.com/luanti-org/luanti/blob/master/tests/example_test.py",
            },
        ],
        retrieved_at="2026-08-11T00:00:00Z",
    )
    (defects / "example-upstream.json").write_text(
        json.dumps(record),
        encoding="utf-8",
    )


def test_phase0_executor_binds_registered_p2p_to_raw_runtime_evidence(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _write_private_inputs(tmp_path)
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    before.write_bytes(b"before")
    after.write_bytes(b"after")
    order: list[str] = []

    def runtime_runner(_context):
        order.append("runtime")
        return LazarusRuntimeObservation(
            complete=True,
            runtime_restored=True,
            before_semantic_ok=True,
            goal_checks_ok=False,
            defect_signature_ok=True,
            invariants_ok=True,
            before_screenshot=before,
            after_screenshot=after,
            checkpoint_sha256="d" * 64,
            synthesis_cache_hit=False,
            actual_synthesis_calls=1,
            checkpoint_reuse_success=False,
            treatment_tokens=5,
            treatment_time_ms=7,
            trace={"audit": "raw"},
        )

    def visual_runner(_context, observation, _spec, _spec_path, evaluate_after):
        order.append("visual")
        assert evaluate_after is True
        before_sha = hashlib.sha256(observation.before_screenshot.read_bytes()).hexdigest()
        after_sha = hashlib.sha256(observation.after_screenshot.read_bytes()).hexdigest()
        return LazarusVisualEvidence(
            before=VisualJudgement(True, before_sha, "e" * 64),
            fixed_after=VisualJudgement(False, after_sha, "f" * 64),
            buggy_after=VisualJudgement(True, after_sha, "0" * 64),
            audits={},
        )

    def p2p_runner(_context, definitions):
        order.append("p2p")
        assert set(definitions) == {"f2p", "p2p"}
        return {
            "schema_version": 1,
            "task_id": "lz-example",
            "version": "buggy",
            "completed": True,
            "passed": True,
            "timeout_s": 300.0,
            "duration_ms": 1.0,
            "checkpoint_restore": _checkpoint_restore("d" * 64),
            "results": [
                {"id": "p2p", "passed": True, "timed_out": False, "duration_ms": 1}
            ],
        }

    executor = build_lazarus_phase0_executor(
        tmp_path,
        runtime_runner=runtime_runner,
        visual_runner=visual_runner,
        p2p_runner=p2p_runner,
    )
    report = executor(context)

    assert order == ["runtime", "visual", "p2p"]
    assert report["complete"] is True, report
    assert report["defect_exposed"] is True
    assert report["per_trial_p2p_evidence"]["results"][0]["id"] == "p2p"
    assert report["version_switch_ms"] == 12.5


def test_phase0_executor_default_p2p_runner_applies_the_runtime_clone_restorer(
    tmp_path: Path,
) -> None:
    """The production executor cannot treat a receipt as a restored Start."""

    context = _context(tmp_path)
    _write_private_inputs(tmp_path)
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    before.write_bytes(b"before")
    after.write_bytes(b"after")
    restored: list[Path] = []

    def restorer(clone: Path, receipt_path: Path, receipt: dict[str, object]):
        assert receipt_path == clone / ".warptest/lazarus/checkpoint-restore.json"
        assert json.loads(receipt_path.read_text(encoding="utf-8"))["checkpoint_sha256"] == "d" * 64
        marker = clone / ".warptest/lazarus/runtime-start-restored"
        marker.write_text("restored\n", encoding="utf-8")
        restored.append(marker)
        return {
            "schema_version": 1,
            "state_restore_applied": True,
            "adapter": "test-runtime-clone-restorer-v1",
            "checkpoint_sha256": receipt["checkpoint_sha256"],
            "checkpoint_kind": receipt["checkpoint_kind"],
            "receipt_sha256": receipt["payload_sha256"],
        }

    def runtime_runner(_context):
        return LazarusRuntimeObservation(
            complete=True,
            runtime_restored=True,
            before_semantic_ok=True,
            goal_checks_ok=False,
            defect_signature_ok=True,
            invariants_ok=True,
            before_screenshot=before,
            after_screenshot=after,
            checkpoint_sha256="d" * 64,
            synthesis_cache_hit=False,
            actual_synthesis_calls=1,
            checkpoint_reuse_success=False,
            treatment_tokens=5,
            treatment_time_ms=7,
            trace={"audit": "raw"},
            p2p_restore_adapter=restorer,
        )

    def visual_runner(_context, observation, _spec, _spec_path, evaluate_after):
        assert evaluate_after is True
        before_sha = hashlib.sha256(observation.before_screenshot.read_bytes()).hexdigest()
        after_sha = hashlib.sha256(observation.after_screenshot.read_bytes()).hexdigest()
        return LazarusVisualEvidence(
            before=VisualJudgement(True, before_sha, "e" * 64),
            fixed_after=VisualJudgement(False, after_sha, "f" * 64),
            buggy_after=VisualJudgement(True, after_sha, "0" * 64),
            audits={},
        )

    executor = build_lazarus_phase0_executor(
        tmp_path,
        runtime_runner=runtime_runner,
        visual_runner=visual_runner,
        p2p_sandbox_root="p2p-sandbox",
    )
    report = executor(context)

    assert len(restored) == 1
    assert report["complete"] is True, report
    restore = report["per_trial_p2p_evidence"]["checkpoint_restore"]
    assert restore["state_restore"]["adapter"] == "test-runtime-clone-restorer-v1"
