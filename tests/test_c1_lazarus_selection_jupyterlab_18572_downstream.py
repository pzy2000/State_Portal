from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from warptest.c1_lazarus_selection import jupyterlab_18572_downstream as target
from warptest.c1_lazarus_selection.luanti_native_executor import NativeProcessReceipt
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


def _authorization(scope: str = "preannotation-execution") -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id=f"test/jupyterlab-18572-downstream/{scope}",
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
            "strict_gate_1_7": True,
            "strict_no_timeout_unknown": True,
            "lineage_inherited": False,
            "old_attempts_loaded": False,
            "gate8_13_performed": False,
        },
        inputs=(target._TRANSITION,),
    )
    common = {
        target._PARSER: "export const parser = { parse: value => ({ value }) };\n",
        target._CODEMIRROR_LANGUAGE: (
            "export class LRLanguage { static define(value) { return value; } }\n"
            "export class LanguageSupport { constructor(language) { this.language = language; } }\n"
        ),
        target._RSPACK: "// rspack fixture\n",
        target._PLAYWRIGHT: "// playwright fixture\n",
    }
    for variant in ("fixed", "buggy"):
        tree = root / target._TREES / variant
        values = {
            **common,
            target._LANGUAGE: f"// candidate production {variant}\n",
        }
        for relative, content in values.items():
            path = tree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


def _frame(path: Path, text: str) -> None:
    image = Image.new("RGB", (160, 90), "white")
    if text:
        ImageDraw.Draw(image).text((100, 35), text, fill="black")
    image.save(path)


class _Runner:
    def __init__(self, *, wrong_fixed_after: bool = False) -> None:
        self.calls: list[tuple[tuple[str, ...], Path, int]] = []
        self.wrong_fixed_after = wrong_fixed_after

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
            return NativeProcessReceipt("completed", 0, b"Rspack 1.7.5\n", b"")
        assert argv[1].endswith("edge-observe.cjs")
        variant = cwd.name
        after = "x" if variant == "fixed" and not self.wrong_fixed_after else ""
        _frame(cwd / "before.png", "")
        _frame(cwd / "after.png", after)
        _frame(cwd / "fixed-golden.png", "x")
        _frame(cwd / "buggy-golden.png", "")
        observation = {
            "before": "",
            "after": after,
            "browser_channel": "msedge",
            "browser_version": "144.0.3719.82",
            "after_dom_candidate_controlled": True,
            "candidate_appended_dom_nodes": variant == "fixed" and bool(after),
            "frames": {
                "before": "before.png",
                "after": "after.png",
                "fixed_golden": "fixed-golden.png",
                "buggy_golden": "buggy-golden.png",
            },
        }
        marker = "C1_LAZARUS_18572_EDGE_OBSERVATION=" + json.dumps(observation)
        return NativeProcessReceipt("completed", 0, marker.encode(), b"")


def test_downstream_executes_candidate_rspack_bundles_in_real_edge_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/node")
    runner = _Runner()

    artifacts = target.execute_jupyterlab_18572_downstream(
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

    observations = {
        item.payload["variant"]: item
        for item in artifacts
        if item.reference.artifact_kind == "candidate_gate11_edge_observation"
    }
    assert observations["fixed"].payload["after"] == "x"
    assert observations["buggy"].payload["after"] == ""
    assert (
        observations["fixed"].payload["before"]
        == observations["buggy"].payload["before"]
    )
    assert all(
        item.payload["after_dom_candidate_controlled"] for item in observations.values()
    )
    assert observations["fixed"].payload["candidate_appended_dom_nodes"] is True
    assert observations["buggy"].payload["candidate_appended_dom_nodes"] is False

    matrix = next(item for item in artifacts if item.reference == target._GATE12_MATRIX)
    assert matrix.payload["semantic_two_by_two"] == {
        "fixed": {"fixed": True, "buggy": False},
        "buggy": {"fixed": False, "buggy": True},
    }
    assert matrix.payload["common_before_pixel_difference_mean"] == 0.0
    assert matrix.payload["pixel_difference_mean_two_by_two"]["fixed"]["fixed"] == 0.0
    assert matrix.payload["pixel_difference_mean_two_by_two"]["buggy"]["buggy"] == 0.0


def test_harness_directly_imports_variant_modules_and_never_prefills_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/node")
    target.execute_jupyterlab_18572_downstream(
        tmp_path, authorization=_authorization(), process_runner=_Runner()
    )

    for variant in ("fixed", "buggy"):
        tree = (tmp_path / target._TREES / variant).resolve()
        run = tmp_path / target._RUN / variant
        entry = (run / "entry.js").read_text(encoding="utf-8")
        config = (run / "rspack.config.cjs").read_text(encoding="utf-8")
        html = (run / "index.html").read_text(encoding="utf-8")
        browser = (run / "edge-observe.cjs").read_text(encoding="utf-8")
        assert (tree / target._LANGUAGE).as_posix() in entry
        assert (tree / target._PARSER).as_posix() in entry
        assert (tree / target._CODEMIRROR_LANGUAGE).as_posix() in entry
        assert "new LanguageSupport" in entry
        assert "registry.highlight('x', language, output)" in entry
        assert "@jupyterlab/codeeditor" in config
        assert "@jupyterlab/coreutils" in config
        assert "@jupyterlab/translation" in config
        assert '<pre id="candidate-output" class="candidate-output"></pre>' in html
        assert '<pre class="code">x</pre>' in html
        assert "Not rendered yet" in html
        assert "fixed-golden" not in html and "buggy-golden" not in html
        assert "channel: 'msedge'" in browser
        assert "window.__runCandidate()" in browser


def test_only_fresh_v2_artifacts_are_loaded_into_downstream_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/node")
    artifacts = target.execute_jupyterlab_18572_downstream(
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
    # Gate 1--7 stays on its own fresh epoch; this closure only adds current-v3.
    assert {target._TRANSITION, target._POOL} == {
        reference
        for reference in reachable
        if reference.artifact_id.endswith("current-v2")
    }
    assert all(
        reference.artifact_id.endswith(("current-v2", "current-v3"))
        for reference in reachable
    )


def test_invalid_or_inherited_gate57_input_stops_before_candidate_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    transition = target._load(tmp_path, target._TRANSITION)
    # A separate root avoids rewriting an immutable stable head.
    bad_root = tmp_path / "bad"
    bad_root.mkdir()
    _fixture(bad_root)
    head = target._load(bad_root, target._TRANSITION)
    assert transition.payload == head.payload
    # Validate the guard directly with a typed artifact carrying inherited state.
    inherited = replace(head, payload={**head.payload, "lineage_inherited": True})
    with pytest.raises(RuntimeError, match="current-v2 Gate 1--7"):
        target._validate_fresh_gate57_inputs(
            inherited, target._load(bad_root, target._POOL)
        )


def test_wrong_fixed_edge_result_never_writes_formal_freeze_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path)
    monkeypatch.setattr(target, "_node_executable", lambda: "/fixture/node")

    with pytest.raises(RuntimeError, match="fixed Edge semantic observation failed"):
        target.execute_jupyterlab_18572_downstream(
            tmp_path,
            authorization=_authorization(),
            process_runner=_Runner(wrong_fixed_after=True),
        )
    with pytest.raises(RuntimeError, match="artifact directory is missing"):
        target._load(tmp_path, target._ADMISSION)
