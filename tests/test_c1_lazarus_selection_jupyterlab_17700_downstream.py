from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from warptest.c1_lazarus_selection import jupyterlab_17700_downstream as target
from warptest.c1_lazarus_selection.luanti_native_executor import NativeProcessReceipt
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


_RIGHT_BOX = {"x": 100.0, "y": 60.0, "width": 50.0, "height": 20.0}
_FOCUS_X = {"line-column": 102, "notifications": 140}


def _authorization(scope: str = "preannotation-execution") -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id=f"test/jupyterlab-17700-downstream/{scope}",
        scope=scope,
        granted_at="2026-08-15T01:00:00Z",
        local_user_id=os.getuid(),
    )


def _seed_artifact(
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
        created_at="2026-08-15T00:00:00Z",
        inputs=inputs,
        payload=payload,
        authorization=_authorization("infrastructure-audit"),
    )


def _fixture(root: Path) -> None:
    _seed_artifact(
        root,
        target._TRANSITION,
        {
            "candidate": target._CANDIDATE,
            "state": "passed",
            "outcome": "qualified",
            "complete": True,
            "terminal": False,
            "gate8_13_performed": False,
            "old_attempts_loaded": False,
            "gate_status": {
                "source_authority": "passed",
                "single_bug_mapping": "passed",
                "strict_production_inverse": "passed",
                "test_api_compatibility": "passed",
                "fixed_f2p_3x": "passed",
                "buggy_f2p_3x_failure": "passed",
                "fixed_buggy_p2p_3x": "passed",
            },
        },
    )
    _seed_artifact(
        root,
        target._POOL,
        {
            "candidate": target._CANDIDATE,
            "state": "admitted",
            "defect_locus": "at_goal",
            "strict_gate_1_7": True,
            "no_timeout_unknown_or_inheritance": True,
            "old_attempts_loaded": False,
            "gate8_to_13": "pending_post_admission",
        },
        inputs=(target._TRANSITION,),
    )
    common = {
        target._BASE_CSS: ".jp-StatusBar-Right { flex-direction: row-reverse; }\n",
        target._VARIABLES_CSS: ":root { --jp-statusbar-height: 24px; }\n",
        target._LUMINO_WIDGETS: '{"name": "@lumino/widgets"}\n',
        target._RSPACK: "// rspack fixture\n",
        target._PLAYWRIGHT: "// playwright fixture\n",
    }
    for variant in ("fixed", "buggy"):
        tree = root / target._TREES / variant
        values = {**common, target._STATUSBAR: f"// candidate production {variant}\n"}
        for relative, content in values.items():
            path = tree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


def _frame(path: Path, focused: str | None) -> None:
    image = Image.new("RGB", (160, 90), "white")
    if focused is not None:
        left = _FOCUS_X[focused]
        ImageDraw.Draw(image).rectangle((left, 62, left + 10, 72), fill="black")
    image.save(path)


class _Runner:
    def __init__(self, *, wrong_fixed_order: bool = False) -> None:
        self.calls: list[tuple[tuple[str, ...], Path, int]] = []
        self.wrong_fixed_order = wrong_fixed_order

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> NativeProcessReceipt:
        self.calls.append((argv, cwd, timeout_seconds))
        assert env["CI"] == "1"
        if "build" in argv:
            assert argv[1].endswith("node_modules/@rspack/cli/bin/rspack.js")
            (cwd / "dist").mkdir()
            (cwd / "dist/bundle.js").write_text(
                f"// candidate-local {cwd.name} bundle\n", encoding="utf-8"
            )
            return NativeProcessReceipt("completed", 0, b"Rspack 2.0.2\n", b"")
        assert argv[1].endswith("edge-observe.cjs")
        variant = cwd.name
        order = list(target._EXPECTED_TAB_ORDER[variant])
        if variant == "fixed" and self.wrong_fixed_order:
            order.reverse()
        _frame(cwd / "before.png", None)
        _frame(cwd / "after.png", order[0])
        _frame(cwd / "fixed-golden.png", target._VISUAL_ORDER[0])
        _frame(cwd / "buggy-golden.png", target._VISUAL_ORDER[-1])
        observation = {
            "dom_order": order,
            "visual_order": list(target._VISUAL_ORDER),
            "right_group_box": dict(_RIGHT_BOX),
            "before_focused_item": None,
            "tab_sequence": order,
            "browser_channel": "msedge",
            "browser_version": "144.0.3719.115",
            "frames": {
                "before": "before.png",
                "after": "after.png",
                "fixed_golden": "fixed-golden.png",
                "buggy_golden": "buggy-golden.png",
            },
        }
        marker = "C1_LAZARUS_17700_EDGE_OBSERVATION=" + json.dumps(observation)
        return NativeProcessReceipt("completed", 0, marker.encode(), b"")


def test_downstream_executes_candidate_rspack_bundles_in_real_edge_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/node")
    runner = _Runner()

    artifacts = target.execute_jupyterlab_17700_downstream(
        tmp_path,
        authorization=_authorization(),
        process_runner=runner,
    )

    assert len(runner.calls) == 4
    assert [call[1].name for call in runner.calls] == [
        "fixed",
        "fixed",
        "buggy",
        "buggy",
    ]
    admission = artifacts[-1]
    assert admission.reference == target._ADMISSION
    assert admission.payload["state"] == "admitted"
    assert admission.payload["gate8_materialization_runtime"] == "passed"
    assert admission.payload["gate11_real_edge_observations"] == "passed"
    assert admission.payload["gate12_semantic_pixel_2x2"] == "passed"
    assert admission.payload["gate13_same_lineage"] == "passed"
    assert admission.payload["paid_actions_authorized"] is False
    assert admission.payload["formal_trials_authorized"] is False


def test_tab_order_inverts_while_rendered_order_stays_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/node")

    artifacts = target.execute_jupyterlab_17700_downstream(
        tmp_path, authorization=_authorization(), process_runner=_Runner()
    )

    observations = {
        item.payload["variant"]: item
        for item in artifacts
        if item.reference.artifact_kind == "candidate_gate11_edge_observation"
    }
    assert observations["fixed"].payload["first_tab_stop"] == "line-column"
    assert observations["buggy"].payload["first_tab_stop"] == "notifications"
    assert (
        observations["fixed"].payload["visual_order"]
        == observations["buggy"].payload["visual_order"]
        == list(target._VISUAL_ORDER)
    )
    assert (
        observations["fixed"].payload["dom_order"]
        != observations["buggy"].payload["dom_order"]
    )
    assert all(
        item.payload["before_focused_item"] is None for item in observations.values()
    )
    assert all(
        item.payload["real_keyboard_tab_presses"] for item in observations.values()
    )

    matrix = next(item for item in artifacts if item.reference == target._GATE12_MATRIX)
    assert matrix.payload["semantic_two_by_two"] == {
        "fixed": {"fixed": True, "buggy": False},
        "buggy": {"fixed": False, "buggy": True},
    }
    assert matrix.payload["common_before_pixel_difference_mean"] == 0.0
    assert matrix.payload["pixel_difference_mean_two_by_two"]["fixed"]["fixed"] == 0.0
    assert matrix.payload["pixel_difference_mean_two_by_two"]["buggy"]["buggy"] == 0.0
    roi = matrix.payload["right_group_roi_difference_mean_two_by_two"]
    assert roi["fixed"]["buggy"] > target._ROI_CROSS_THRESHOLD
    assert roi["buggy"]["fixed"] > target._ROI_CROSS_THRESHOLD


def test_harness_uses_candidate_production_module_and_style(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/node")
    target.execute_jupyterlab_17700_downstream(
        tmp_path, authorization=_authorization(), process_runner=_Runner()
    )

    for variant in ("fixed", "buggy"):
        tree = (tmp_path / target._TREES / variant).resolve()
        run = tmp_path / target._RUN / variant
        entry = (run / "entry.js").read_text(encoding="utf-8")
        config = (run / "rspack.config.cjs").read_text(encoding="utf-8")
        html = (run / "index.html").read_text(encoding="utf-8")
        browser = (run / "edge-observe.cjs").read_text(encoding="utf-8")
        assert (tree / target._STATUSBAR).as_posix() in entry
        assert "new StatusBar()" in entry
        assert "statusBar.registerStatusItem" in entry
        assert "align: 'right'" in entry
        assert (tree / "node_modules").as_posix() in config
        assert (tree / target._BASE_CSS).as_uri() in html
        assert (tree / target._VARIABLES_CSS).as_uri() in html
        assert "line-column" not in html and "Bell" not in html
        assert "channel: 'msedge'" in browser
        assert "page.keyboard.press('Tab')" in browser


def test_only_strict_gate57_artifacts_are_loaded_into_downstream_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/node")
    artifacts = target.execute_jupyterlab_17700_downstream(
        tmp_path, authorization=_authorization(), process_runner=_Runner()
    )

    references = {item.reference for item in artifacts}
    pending = [target._ADMISSION]
    reachable: set[ArtifactReference] = set()
    while pending:
        reference = pending.pop()
        if reference in reachable:
            continue
        reachable.add(reference)
        pending.extend(target._load(tmp_path, reference).inputs)
    assert target._TRANSITION in reachable
    assert target._POOL in reachable
    assert references <= reachable
    downstream = reachable - {target._TRANSITION, target._POOL}
    assert all(target._EPOCH in reference.artifact_id for reference in downstream)


def test_invalid_or_inherited_gate57_input_stops_before_candidate_execution(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    transition = target._load(tmp_path, target._TRANSITION)
    pool = target._load(tmp_path, target._POOL)

    inherited = replace(
        transition, payload={**transition.payload, "gate8_13_performed": True}
    )
    with pytest.raises(RuntimeError, match="Gate 1--7 transition is invalid"):
        target._validate_strict_gate57_inputs(inherited, pool)
    unadmitted = replace(pool, payload={**pool.payload, "strict_gate_1_7": False})
    with pytest.raises(RuntimeError, match="pool admission is invalid"):
        target._validate_strict_gate57_inputs(transition, unadmitted)


def test_wrong_fixed_tab_order_never_writes_formal_freeze_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/node")

    with pytest.raises(RuntimeError, match="fixed Edge semantic observation failed"):
        target.execute_jupyterlab_17700_downstream(
            tmp_path,
            authorization=_authorization(),
            process_runner=_Runner(wrong_fixed_order=True),
        )
    with pytest.raises(RuntimeError, match="artifact directory is missing"):
        target._load(tmp_path, target._ADMISSION)


def test_revoked_or_wrong_scope_authorization_is_rejected(tmp_path: Path) -> None:
    _fixture(tmp_path)
    with pytest.raises(PermissionError):
        target.execute_jupyterlab_17700_downstream(
            tmp_path, authorization=_authorization("infrastructure-audit")
        )
