from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from warptest.c1_lazarus_selection import jupyterlab_18938_downstream as target
from warptest.c1_lazarus_selection.luanti_native_executor import NativeProcessReceipt
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


def _authorization(scope: str = "preannotation-execution") -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id=f"test/jupyterlab-18938-downstream/{scope}",
        scope=scope,
        granted_at="2026-08-15T02:00:00Z",
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
        created_at="2026-08-15T01:00:00Z",
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
            "strict_gate_1_7": True,
            "strict_no_timeout_unknown": True,
            "lineage_inherited": False,
            "old_attempts_loaded": False,
            "gate8_13_performed": False,
        },
    )
    _seed_artifact(
        root,
        target._POOL,
        {
            "candidate": target._CANDIDATE,
            "state": "active_qualified",
            "defect_locus": "at_goal",
            "strict_gate_1_7": True,
            "strict_no_timeout_unknown": True,
            "lineage_inherited": False,
            "old_attempts_loaded": False,
            "gate8_13_performed": False,
        },
        inputs=(target._TRANSITION,),
    )
    common = {
        target._DIALOG_CSS: ".jp-Dialog { position: absolute; }\n",
        target._BASE_CSS: ".jp-Dialog-content { display: flex; }\n",
        target._UI_CSS: ".jp-Button { border: 0; }\n",
        target._VARIABLES_CSS: ":root { --jp-layout-color0: white; }\n",
        target._RSPACK: "// rspack fixture\n",
        target._PLAYWRIGHT: "// playwright fixture\n",
    }
    for variant in ("fixed", "buggy"):
        tree = root / target._TREES / variant
        values = {**common, target._DIALOG: f"// candidate production {variant}\n"}
        for relative, content in values.items():
            path = tree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


def _frame(path: Path, dialog_open: bool) -> None:
    image = Image.new("RGB", (180, 120), "white")
    if dialog_open:
        ImageDraw.Draw(image).rectangle((40, 30, 140, 90), fill="grey")
    image.save(path)


class _Runner:
    def __init__(self, *, fixed_dialog_stays: bool = False) -> None:
        self.calls: list[tuple[tuple[str, ...], Path, int]] = []
        self.fixed_dialog_stays = fixed_dialog_stays

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
        hangs = variant == "buggy" or self.fixed_dialog_stays
        _frame(cwd / "before.png", True)
        _frame(cwd / "after.png", hangs)
        _frame(cwd / "fixed-golden.png", False)
        _frame(cwd / "buggy-golden.png", True)
        observation = {
            "before_dialog_count": 1,
            "after_dialog_count": 1 if hangs else 0,
            "golden_dialog_count": 0,
            "before_settled": {"settled": None, "error": None},
            "after_settled": (
                {"settled": None, "error": None}
                if hangs
                else {"settled": "rejected", "error": target._THROWN_MESSAGE}
            ),
            "page_errors": [target._THROWN_MESSAGE] if hangs else [],
            "browser_channel": "msedge",
            "browser_version": "144.0.3719.115",
            "frames": {
                "before": "before.png",
                "after": "after.png",
                "fixed_golden": "fixed-golden.png",
                "buggy_golden": "buggy-golden.png",
            },
        }
        marker = "C1_LAZARUS_18938_EDGE_OBSERVATION=" + json.dumps(observation)
        return NativeProcessReceipt("completed", 0, marker.encode(), b"")


def test_downstream_admits_the_candidate_from_real_edge_dialog_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/node")
    runner = _Runner()

    artifacts = target.execute_jupyterlab_18938_downstream(
        tmp_path, authorization=_authorization(), process_runner=runner
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
    assert admission.payload["gate10_hidden_oracle"] == "passed"
    assert admission.payload["gate11_real_edge_observations"] == "passed"
    assert admission.payload["gate12_semantic_pixel_2x2"] == "passed"
    assert admission.payload["paid_actions_authorized"] is False


def test_hang_is_observed_as_a_surviving_dialog_and_unsettled_promise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/node")

    artifacts = target.execute_jupyterlab_18938_downstream(
        tmp_path, authorization=_authorization(), process_runner=_Runner()
    )

    observations = {
        item.payload["variant"]: item
        for item in artifacts
        if item.reference.artifact_kind == "candidate_gate11_edge_observation"
    }
    assert observations["fixed"].payload["after_dialog_count"] == 0
    assert observations["fixed"].payload["after_settled"] == "rejected"
    assert observations["fixed"].payload["after_error"] == target._THROWN_MESSAGE
    assert observations["fixed"].payload["uncaught_page_errors"] == []
    assert observations["buggy"].payload["after_dialog_count"] == 1
    assert observations["buggy"].payload["after_settled"] is None
    assert observations["buggy"].payload["uncaught_page_errors"] == [
        target._THROWN_MESSAGE
    ]
    assert all(
        item.payload["before_dialog_count"] == 1
        and item.payload["before_settled"] is None
        and item.payload["golden_dialog_count_after_cancel"] == 0
        for item in observations.values()
    )

    matrix = next(item for item in artifacts if item.reference == target._GATE12_MATRIX)
    assert matrix.payload["semantic_two_by_two"] == {
        "fixed": {"fixed": True, "buggy": False},
        "buggy": {"fixed": False, "buggy": True},
    }
    assert matrix.payload["common_before_pixel_difference_mean"] == 0.0
    pixel = matrix.payload["pixel_difference_mean_two_by_two"]
    assert pixel["fixed"]["fixed"] == 0.0 and pixel["buggy"]["buggy"] == 0.0
    assert pixel["fixed"]["buggy"] > target._GLOBAL_CROSS_THRESHOLD


def test_harness_uses_candidate_production_dialog_and_real_clicks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/node")
    target.execute_jupyterlab_18938_downstream(
        tmp_path, authorization=_authorization(), process_runner=_Runner()
    )

    for variant in ("fixed", "buggy"):
        tree = (tmp_path / target._TREES / variant).resolve()
        run = tmp_path / target._RUN / variant
        entry = (run / "entry.js").read_text(encoding="utf-8")
        html = (run / "index.html").read_text(encoding="utf-8")
        browser = (run / "edge-observe.cjs").read_text(encoding="utf-8")
        assert (tree / target._DIALOG).as_posix() in entry
        assert "showDialog(" in entry
        assert "ThrowingValueWidget" in entry
        assert target._THROWN_MESSAGE in entry
        assert (tree / target._DIALOG_CSS).as_uri() in html
        assert (tree / target._VARIABLES_CSS).as_uri() in html
        assert "channel: 'msedge'" in browser
        assert "getByRole('button', { name: 'Ok' }).click" in browser
        assert "getByRole('button', { name: 'Cancel' }).click" in browser
        assert ".remove()" not in browser


def test_only_fresh_strict_artifacts_are_loaded_into_downstream_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/node")
    artifacts = target.execute_jupyterlab_18938_downstream(
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
        transition, payload={**transition.payload, "lineage_inherited": True}
    )
    with pytest.raises(RuntimeError, match="Gate 1--7 transition is invalid"):
        target._validate_strict_gate57_inputs(inherited, pool)
    unqualified = replace(pool, payload={**pool.payload, "state": "blocked"})
    with pytest.raises(RuntimeError, match="pool admission is invalid"):
        target._validate_strict_gate57_inputs(transition, unqualified)


def test_fixed_variant_that_still_hangs_never_writes_formal_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/node")

    with pytest.raises(RuntimeError, match="fixed Edge semantic observation failed"):
        target.execute_jupyterlab_18938_downstream(
            tmp_path,
            authorization=_authorization(),
            process_runner=_Runner(fixed_dialog_stays=True),
        )
    with pytest.raises(RuntimeError, match="artifact directory is missing"):
        target._load(tmp_path, target._ADMISSION)


def test_revoked_or_wrong_scope_authorization_is_rejected(tmp_path: Path) -> None:
    _fixture(tmp_path)
    with pytest.raises(PermissionError):
        target.execute_jupyterlab_18938_downstream(
            tmp_path, authorization=_authorization("infrastructure-audit")
        )
