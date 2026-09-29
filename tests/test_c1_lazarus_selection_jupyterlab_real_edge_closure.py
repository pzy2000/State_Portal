from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Mapping

import pytest
from PIL import Image, ImageDraw

from warptest.c1_lazarus_selection import jupyterlab_19227_downstream as candidate
from warptest.c1_lazarus_selection import jupyterlab_real_edge_closure as target
from warptest.c1_lazarus_selection.luanti_native_executor import NativeProcessReceipt
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


SPEC = candidate.SPEC


def _authorization(scope: str = "preannotation-execution") -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id=f"test/jupyterlab-real-edge/{scope}",
        scope=scope,
        granted_at="2026-08-15T02:30:00Z",
        local_user_id=os.getuid(),
    )


def _seed(
    root: Path,
    reference: ArtifactReference,
    payload: dict[str, object],
    *,
    inputs: tuple[ArtifactReference, ...] = (),
) -> None:
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at="2026-08-15T02:00:00Z",
        inputs=inputs,
        payload=payload,
        authorization=_authorization("infrastructure-audit"),
    )


def _fixture(root: Path) -> None:
    _seed(
        root,
        SPEC.gate57_transition,
        {
            "candidate": SPEC.candidate,
            "state": "passed",
            "strict_gate_1_7": True,
            "strict_no_timeout_unknown": True,
            "lineage_inherited": False,
            "old_attempts_loaded": False,
            "gate8_13_performed": False,
        },
    )
    _seed(
        root,
        SPEC.gate57_pool,
        {
            "candidate": SPEC.candidate,
            "state": "active_qualified",
            "strict_gate_1_7": True,
            "strict_no_timeout_unknown": True,
            "lineage_inherited": False,
            "old_attempts_loaded": False,
            "gate8_13_performed": False,
        },
        inputs=(SPEC.gate57_transition,),
    )
    for variant in ("fixed", "buggy"):
        tree = root / SPEC.trees / variant
        values = {
            **{asset: f"/* {asset} */\n" for asset in SPEC.common_assets},
            **{
                module: f"// candidate production {variant}\n"
                for module in SPEC.production_modules
            },
            "node_modules/@rspack/cli/bin/rspack.js": "// rspack\n",
            "node_modules/playwright/index.js": "// playwright\n",
        }
        for relative, content in values.items():
            path = tree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


def _frame(path: Path, order: tuple[str, ...]) -> None:
    image = Image.new("RGB", (200, 60), "white")
    draw = ImageDraw.Draw(image)
    for index, name in enumerate(order):
        draw.text((4 + index * 38, 20), name[:4], fill="black")
    image.save(path)


class _Runner:
    def __init__(self, *, fixed_stays_wrong: bool = False) -> None:
        self.calls: list[tuple[tuple[str, ...], Path, int]] = []
        self.fixed_stays_wrong = fixed_stays_wrong

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> NativeProcessReceipt:
        self.calls.append((argv, cwd, timeout_seconds))
        if "build" in argv:
            (cwd / "dist").mkdir()
            (cwd / "dist/bundle.js").write_text("// bundle\n", encoding="utf-8")
            return NativeProcessReceipt("completed", 0, b"Rspack\n", b"")
        variant = cwd.name
        correct = tuple(SPEC.expected_after["fixed"]["after_item_names"])
        defective = tuple(SPEC.expected_after["buggy"]["after_item_names"])
        after = (
            defective
            if variant == "buggy" or self.fixed_stays_wrong
            else correct
        )
        _frame(cwd / "before.png", correct)
        _frame(cwd / "after.png", after)
        _frame(cwd / "fixed-golden.png", correct)
        _frame(cwd / "buggy-golden.png", defective)
        observation = {
            "golden_correct_order": list(correct),
            "golden_defective_order": list(defective),
            "before_names": list(correct),
            "before_order": list(correct),
            "after_names": list(after),
            "after_order": list(after),
            "browser_channel": "msedge",
            "browser_version": "144.0.3719.115",
            "frames": {
                "before": "before.png",
                "after": "after.png",
                "fixed_golden": "fixed-golden.png",
                "buggy_golden": "buggy-golden.png",
            },
        }
        marker = f"{SPEC.observation_marker}=" + json.dumps(observation)
        return NativeProcessReceipt("completed", 0, marker.encode(), b"")


def test_shared_runner_admits_the_candidate_from_real_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/node")
    runner = _Runner()

    artifacts = target.execute_real_edge_closure(
        tmp_path, spec=SPEC, authorization=_authorization(), process_runner=runner
    )

    assert len(runner.calls) == 4
    admission = artifacts[-1]
    assert admission.reference == SPEC.admission
    assert admission.payload["state"] == "admitted"
    assert admission.payload["gate12_semantic_pixel_2x2"] == "passed"
    matrix = next(
        item
        for item in artifacts
        if item.reference.artifact_kind == "candidate_gate12_semantic_pixel_matrix"
    )
    assert matrix.payload["semantic_two_by_two"] == {
        "fixed": {"fixed": True, "buggy": False},
        "buggy": {"fixed": False, "buggy": True},
    }
    assert matrix.payload["common_before_pixel_difference_mean"] == 0.0
    observations = {
        item.payload["variant"]: item
        for item in artifacts
        if item.reference.artifact_kind == "candidate_gate11_edge_observation"
    }
    assert observations["fixed"].payload["semantic_state"] == dict(
        SPEC.expected_after["fixed"]
    )
    assert observations["buggy"].payload["semantic_state"] == dict(
        SPEC.expected_after["buggy"]
    )


def test_lineage_reaches_only_the_strict_inputs_and_this_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/node")
    artifacts = target.execute_real_edge_closure(
        tmp_path, spec=SPEC, authorization=_authorization(), process_runner=_Runner()
    )

    reachable: set[ArtifactReference] = set()
    pending = [SPEC.admission]
    while pending:
        reference = pending.pop()
        if reference in reachable:
            continue
        reachable.add(reference)
        pending.extend(target._load(tmp_path, reference).inputs)
    assert {item.reference for item in artifacts} <= reachable
    downstream = reachable - {SPEC.gate57_transition, SPEC.gate57_pool}
    assert all(SPEC.epoch in reference.artifact_id for reference in downstream)


def test_identical_production_modules_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/node")
    for variant in ("fixed", "buggy"):
        for module in SPEC.production_modules:
            (tmp_path / SPEC.trees / variant / module).write_text(
                "// identical\n", encoding="utf-8"
            )

    with pytest.raises(RuntimeError, match="not an inverse pair"):
        target.execute_real_edge_closure(
            tmp_path, spec=SPEC, authorization=_authorization(), process_runner=_Runner()
        )


def test_wrong_fixed_observation_never_writes_the_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/node")

    with pytest.raises(RuntimeError, match="fixed Edge semantic observation failed"):
        target.execute_real_edge_closure(
            tmp_path,
            spec=SPEC,
            authorization=_authorization(),
            process_runner=_Runner(fixed_stays_wrong=True),
        )
    with pytest.raises(RuntimeError, match="artifact directory is missing"):
        target._load(tmp_path, SPEC.admission)


def test_inherited_strict_input_stops_before_candidate_execution(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    transition = target._load(tmp_path, SPEC.gate57_transition)
    pool = target._load(tmp_path, SPEC.gate57_pool)

    inherited = replace(
        transition, payload={**transition.payload, "lineage_inherited": True}
    )
    with pytest.raises(RuntimeError, match="Gate 1--7 transition is invalid"):
        target._validate_strict_inputs(SPEC, inherited, pool)


def test_19227_harness_drives_real_toolbar_lifecycle(tmp_path: Path) -> None:
    run = tmp_path / "run"
    tree = tmp_path / "tree"
    run.mkdir()
    tree.mkdir()
    files: Mapping[str, str] = SPEC.harness_files(run, tree)

    entry = files["entry.js"]
    browser = files["edge-observe.cjs"]
    assert (tree / candidate._TOOLBAR).as_posix() in entry
    assert "new ReactiveToolbar()" in entry
    assert "toolbar.insertBefore('close', 'f1'" in entry
    assert "factoryItems.f1.dispose()" in entry
    assert "channel: 'msedge'" in browser
    assert "__candidateRefreshFactoryItems" in browser
    assert "fixed-golden.png" in browser and "buggy-golden.png" in browser
    assert files["index.html"].count("stylesheet") == len(SPEC.common_assets)


def test_revoked_or_wrong_scope_authorization_is_rejected(tmp_path: Path) -> None:
    _fixture(tmp_path)
    with pytest.raises(PermissionError):
        target.execute_real_edge_closure(
            tmp_path, spec=SPEC, authorization=_authorization("infrastructure-audit")
        )
