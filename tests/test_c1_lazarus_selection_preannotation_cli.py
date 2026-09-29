from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import warptest.c1_lazarus_selection.operations as operations_module
import warptest.c1_lazarus_selection.pipeline as pipeline_module
from warptest.c1_lazarus_selection.epoch import SELECTION_EPOCH_V2
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.pipeline import StageDecision
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)
from warptest.c1_lazarus_selection.source_atomicity import (
    load_source_atomicity_batch_context,
    write_source_atomicity_artifact,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_c1_lazarus_selection_preannotation.py"
REPOSITORY_ORDER = ("luanti", "blender", "jupyterlab", "openra", "libreoffice")


def test_preannotation_cli_uses_direct_v2_screening_inputs_and_wires_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    root = tmp_path / "selection"
    candidates = _write_v2_screening_inputs(root)
    captured: dict[str, object] = {}
    operation_epochs: list[object] = []

    def manifest(*_args: object, **kwargs: object) -> StageDecision:
        operation_epochs.append(kwargs["selection_epoch"])
        return StageDecision.passed({})

    def repetitions(*_args: object, **kwargs: object) -> StageDecision:
        operation_epochs.append(kwargs["selection_epoch"])
        return StageDecision.passed({})

    def pipeline(
        _root: Path,
        *,
        candidates: tuple[CandidateKey, ...],
        operations: dict[str, object],
        selection_epoch: object,
        **_kwargs: object,
    ) -> SimpleNamespace:
        captured["candidates"] = candidates
        captured["selection_epoch"] = selection_epoch
        manifest_operation = operations["test_manifest"]
        repetitions_operation = operations["test_repetitions"]
        assert callable(manifest_operation)
        assert callable(repetitions_operation)
        manifest_operation(candidates[0])
        repetitions_operation(candidates[0])
        return SimpleNamespace(
            total=1,
            scheduled=1,
            resumed=0,
            active=0,
            eligible=0,
            terminal_counts={},
            paused_for_disk=0,
        )

    monkeypatch.setattr(module, "evaluate_test_manifest", manifest)
    monkeypatch.setattr(module, "evaluate_test_repetitions", repetitions)
    monkeypatch.setattr(module, "run_preannotation_pipeline", pipeline)

    assert (
        module.main(
            [
                "--artifact-root",
                str(root),
                "--workspace-root",
                str(tmp_path / "workspace"),
                "--authorization-id",
                "local-preannotation/cli-v2",
                "--execute",
            ]
        )
        == 0
    )

    assert captured == {
        "candidates": candidates,
        "selection_epoch": SELECTION_EPOCH_V2,
    }
    assert operation_epochs == [SELECTION_EPOCH_V2, SELECTION_EPOCH_V2]
    assert json.loads(capsys.readouterr().out) == {
        "active": 0,
        "eligible": 0,
        "paused_for_disk": 0,
        "resumed": 0,
        "scheduled": 1,
        "terminal_counts": {},
        "total": 1,
    }


def test_preannotation_cli_manifest_only_is_closed_and_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    root = tmp_path / "selection"
    _write_v2_screening_inputs(
        root,
        omit_checkpoint_for=CandidateKey("jupyterlab", 9),
    )
    captured: dict[str, object] = {}

    def pipeline(
        _root: Path,
        *,
        candidates: tuple[CandidateKey, ...],
        operations: dict[str, object],
        selection_epoch: object,
        through_stage: object,
        **_kwargs: object,
    ) -> SimpleNamespace:
        captured.update(
            candidates=candidates,
            operation_names=tuple(operations),
            selection_epoch=selection_epoch,
            through_stage=through_stage,
        )
        operation = operations["test_manifest"]
        assert callable(operation)
        operation(candidates[0])
        return SimpleNamespace(
            total=1,
            scheduled=1,
            resumed=0,
            active=1,
            eligible=0,
            terminal_counts={},
            paused_for_disk=0,
        )

    monkeypatch.setattr(
        module,
        "evaluate_test_manifest",
        lambda *_args, **_kwargs: StageDecision.passed({}),
    )
    monkeypatch.setattr(
        module,
        "evaluate_test_repetitions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("manifest-only mode called test repetitions")
        ),
    )
    monkeypatch.setattr(module, "run_preannotation_pipeline", pipeline)

    assert (
        module.main(
            [
                "--artifact-root",
                str(root),
                "--authorization-id",
                "local-preannotation/manifest-only",
                "--through-stage",
                "test_manifest",
                "--max-candidates",
                "1",
                "--execute",
            ]
        )
        == 0
    )

    assert captured == {
        "candidates": (CandidateKey("luanti", 41),),
        "operation_names": ("test_manifest",),
        "selection_epoch": SELECTION_EPOCH_V2,
        "through_stage": "test_manifest",
    }
    assert json.loads(capsys.readouterr().out) == {
        "active": 1,
        "eligible": 0,
        "paused_for_disk": 0,
        "resumed": 0,
        "scheduled": 1,
        "terminal_counts": {},
        "total": 1,
    }


def test_manifest_only_loads_each_repository_enumeration_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    root = tmp_path / "selection"
    candidates = _write_v2_screening_inputs(root)
    load_counts: dict[str, int] = {}
    worker_enumeration_loads = 0
    real_load = module.load_source_atomicity_batch_context
    real_pipeline_load = pipeline_module.load_stable_artifact
    real_operations_load = operations_module.load_stable_artifact

    def counted_load(
        artifact_root: Path,
        *,
        repository_key: str,
    ) -> object:
        load_counts[repository_key] = load_counts.get(repository_key, 0) + 1
        return real_load(
            artifact_root,
            repository_key=repository_key,
        )

    def reject_pipeline_enumeration_reload(*args: object, **kwargs: object) -> object:
        nonlocal worker_enumeration_loads
        if kwargs.get("artifact_kind") == "repository_enumeration":
            worker_enumeration_loads += 1
        return real_pipeline_load(*args, **kwargs)

    def reject_operations_enumeration_reload(*args: object, **kwargs: object) -> object:
        nonlocal worker_enumeration_loads
        if kwargs.get("artifact_kind") == "repository_enumeration":
            worker_enumeration_loads += 1
        return real_operations_load(*args, **kwargs)

    def manifest(
        _root: Path,
        candidate: CandidateKey,
        *,
        selection_epoch: object,
        batch_context: object,
    ) -> StageDecision:
        assert selection_epoch == SELECTION_EPOCH_V2
        assert batch_context.repository_key == candidate.repository_key
        assert candidate.change_number in batch_context.candidate_numbers
        return StageDecision.terminal("quality_rejected")

    monkeypatch.setattr(module, "load_source_atomicity_batch_context", counted_load)
    monkeypatch.setattr(module, "evaluate_test_manifest", manifest)
    monkeypatch.setattr(
        pipeline_module,
        "load_stable_artifact",
        reject_pipeline_enumeration_reload,
    )
    monkeypatch.setattr(
        operations_module,
        "load_stable_artifact",
        reject_operations_enumeration_reload,
    )

    assert (
        module.main(
            [
                "--artifact-root",
                str(root),
                "--authorization-id",
                "local-preannotation/manifest-context",
                "--through-stage",
                "test_manifest",
                "--execute",
            ]
        )
        == 0
    )

    assert load_counts == {repository: 1 for repository in REPOSITORY_ORDER}
    assert worker_enumeration_loads == 0
    assert len(candidates) == 3


def test_candidate_selection_skips_missing_and_rejected_source_atomicity(
    tmp_path: Path,
) -> None:
    module = _load_module()
    root = tmp_path / "selection"
    missing = CandidateKey("luanti", 41)
    rejected = CandidateKey("jupyterlab", 9)
    accepted = CandidateKey("openra", 3)
    _write_v2_screening_inputs(
        root,
        missing_atomicity_for=missing,
        rejected_atomicity_for=rejected,
    )

    candidates, contexts = module._patch_eligible_candidates(root)

    assert candidates == (accepted,)
    assert set(contexts) == set(REPOSITORY_ORDER)


def test_preannotation_cli_refuses_a_v1_enumeration_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    root = tmp_path / "selection"
    _write(
        root,
        artifact_id="repository_enumeration/luanti/current/v1",
        artifact_kind="repository_enumeration",
        semantic_version="1.0",
        payload={
            "repository_key": "luanti",
            "merged_change_numbers": [41],
            "authoritative_issue_link_missing": [],
            "repair_not_in_pinned": [],
            "candidates": [{"repository_key": "luanti", "change_number": 41}],
            "complete": True,
        },
    )
    monkeypatch.setattr(
        module,
        "run_preannotation_pipeline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("v1 enumeration must not reach the pipeline")
        ),
    )

    with pytest.raises(RuntimeError, match="artifact directory is missing"):
        module.main(
            [
                "--artifact-root",
                str(root),
                "--workspace-root",
                str(tmp_path / "workspace"),
                "--authorization-id",
                "local-preannotation/cli-v2",
                "--execute",
            ]
        )


def test_preannotation_cli_refuses_a_missing_v2_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    root = tmp_path / "selection"
    _write_v2_screening_inputs(root, omit_checkpoint_for=CandidateKey("luanti", 41))
    monkeypatch.setattr(
        module,
        "run_preannotation_pipeline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing v2 checkpoint must not reach pipeline")
        ),
    )

    with pytest.raises(RuntimeError, match="artifact directory is missing"):
        module.main(
            [
                "--artifact-root",
                str(root),
                "--workspace-root",
                str(tmp_path / "workspace"),
                "--authorization-id",
                "local-preannotation/cli-v2",
                "--execute",
            ]
        )


def test_preannotation_cli_refuses_a_dangling_v2_patch_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    root = tmp_path / "selection"
    _write_v2_screening_inputs(root, dangling_patch_for=CandidateKey("luanti", 41))
    monkeypatch.setattr(
        module,
        "run_preannotation_pipeline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dangling v2 screening checkpoint must not reach pipeline")
        ),
    )

    with pytest.raises(RuntimeError, match="patch source prerequisite"):
        module.main(
            [
                "--artifact-root",
                str(root),
                "--workspace-root",
                str(tmp_path / "workspace"),
                "--authorization-id",
                "local-preannotation/cli-v2",
                "--execute",
            ]
        )


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("c1_preannotation_cli", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("pre-annotation CLI module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_v2_screening_inputs(
    root: Path,
    *,
    dangling_patch_for: CandidateKey | None = None,
    omit_checkpoint_for: CandidateKey | None = None,
    missing_atomicity_for: CandidateKey | None = None,
    rejected_atomicity_for: CandidateKey | None = None,
) -> tuple[CandidateKey, ...]:
    candidates = (
        CandidateKey("luanti", 41),
        CandidateKey("jupyterlab", 9),
        CandidateKey("openra", 3),
    )
    for repository_key in REPOSITORY_ORDER:
        repository_candidates = tuple(
            candidate
            for candidate in candidates
            if candidate.repository_key == repository_key
        )
        enumeration = _write(
            root,
            artifact_id=f"repository_enumeration/{repository_key}/current/v2",
            artifact_kind="repository_enumeration",
            payload={
                "repository_key": repository_key,
                "merged_change_numbers": (
                    [candidate.change_number for candidate in repository_candidates]
                ),
                "authoritative_issue_link_missing": [],
                "repair_not_in_pinned": [],
                "infrastructure_blocked": [],
                "candidates": [
                    candidate.to_value() for candidate in repository_candidates
                ],
                "complete": True,
            },
        )
        for candidate in repository_candidates:
            source_manifest_path = (
                f"raw/source/{candidate.repository_key}/{candidate.change_number}.json"
            )
            raw_patch_path = (
                f"raw/patch/{candidate.repository_key}/{candidate.change_number}.diff"
            )
            raw_patch = root / raw_patch_path
            raw_patch.parent.mkdir(parents=True, exist_ok=True)
            raw_patch.write_bytes(
                b"""\
diff --git a/src/example.py b/src/example.py
--- a/src/example.py
+++ b/src/example.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
diff --git a/tests/test_example.py b/tests/test_example.py
--- a/tests/test_example.py
+++ b/tests/test_example.py
@@ -1 +1,2 @@
 def test_value():
+    assert VALUE == 2
"""
            )
            source_manifest = root / source_manifest_path
            source_manifest.parent.mkdir(parents=True, exist_ok=True)
            issue_number = candidate.change_number + 100_000
            repository_names = {
                "luanti": "luanti-org/luanti",
                "jupyterlab": "jupyterlab/jupyterlab",
                "openra": "OpenRA/OpenRA",
            }
            repository_name = repository_names[candidate.repository_key]
            source_manifest.write_text(
                json.dumps(
                    {
                        "candidate": candidate.to_value(),
                        "provider": "github",
                        "authority": {
                            "provider": "github",
                            "api_base": "https://api.github.com",
                            "repository": repository_name,
                        },
                        "pull": {
                            "number": candidate.change_number,
                            "merged": True,
                            "merged_at": "2026-08-01T00:00:00Z",
                            "title": "Repair regression",
                            "body": f"Fixes #{issue_number}.",
                        },
                        "relationships": [],
                        "issue_numbers": [issue_number],
                        "issues": [
                            {
                                "number": issue_number,
                                "issue": {
                                    "number": issue_number,
                                    "title": "Regression",
                                    "body": "Authoritative defect report.",
                                    "created_at": "2026-07-01T00:00:00Z",
                                },
                                "comments": [],
                                "history": [],
                            }
                        ],
                        "raw_paths": [raw_patch_path],
                        "request_receipt_paths": [],
                        "patch_raw_path": raw_patch_path,
                        "attachment_paths": [],
                        "complete": True,
                    }
                ),
                encoding="utf-8",
            )
            source = _write(
                root,
                artifact_id=(
                    f"candidate_source_packet/{candidate.repository_key}/"
                    f"{candidate.change_number}/source/v2"
                ),
                artifact_kind="candidate_source_packet",
                inputs=(enumeration.reference,),
                payload={
                    "candidate": candidate.to_value(),
                    "raw_evidence_path": source_manifest_path,
                    "complete": True,
                },
            )
            patch = _write(
                root,
                artifact_id=(
                    f"candidate_test_patch/{candidate.repository_key}/"
                    f"{candidate.change_number}/screening/v2"
                ),
                artifact_kind="candidate_test_patch",
                inputs=(
                    (enumeration.reference,)
                    if candidate == dangling_patch_for
                    else (enumeration.reference, source.reference)
                ),
                payload={
                    "candidate": candidate.to_value(),
                    "outcome": "patch_eligible",
                    "production_paths": ["src/example.py"],
                    "test_paths": ["tests/test_example.py"],
                    "source_manifest_path": source_manifest_path,
                    "raw_patch_path": raw_patch_path,
                    "complete": True,
                },
            )
            if candidate != omit_checkpoint_for:
                _write(
                    root,
                    artifact_id=(
                        f"screening_checkpoint/{candidate.repository_key}/"
                        f"{candidate.change_number}/current/v2"
                    ),
                    artifact_kind="screening_checkpoint",
                    inputs=(enumeration.reference, patch.reference),
                    payload={
                        "candidate": candidate.to_value(),
                        "state": "complete",
                        "outcome": "patch_eligible",
                        "stage_artifact_id": patch.reference.artifact_id,
                    },
                )
                if candidate == rejected_atomicity_for:
                    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
                    manifest["pull"]["body"] = "No authoritative closing relationship."
                    source_manifest.write_text(json.dumps(manifest), encoding="utf-8")
                if candidate not in {dangling_patch_for, missing_atomicity_for}:
                    context = load_source_atomicity_batch_context(
                        root,
                        repository_key=candidate.repository_key,
                    )
                    write_source_atomicity_artifact(
                        batch_context=context,
                        candidate=candidate,
                        created_at="2026-08-13T00:00:00Z",
                        authorization=_authorization(),
                    )
    return candidates


def _write(
    root: Path,
    *,
    artifact_id: str,
    artifact_kind: str,
    payload: dict[str, object],
    inputs: tuple[ArtifactReference, ...] = (),
    semantic_version: str = "2.0",
) -> StableArtifact:
    write_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        semantic_version=semantic_version,
        created_at="2026-08-13T00:00:00Z",
        inputs=inputs,
        payload=payload,
        authorization=_authorization(),
    )
    return load_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        semantic_version=semantic_version,
    )


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-preannotation/cli-test",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )
