from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType

import pytest

from warptest.c1_lazarus_selection.enumeration import (
    EnumerationRecomputeResult,
    RepositoryEnumeration,
    load_repository_enumeration,
    write_repository_enumeration,
)
from warptest.c1_lazarus_selection.epoch import SELECTION_EPOCH_V2
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.stable_protocol import AuthorizationRecord


def test_commit_cli_writes_v2_and_resumes_exact_existing_head(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    root = tmp_path / "selection"
    baseline, result = _baseline_and_result()
    _write_v1(root, baseline)
    calls: list[dict[str, object]] = []

    def recompute(
        received: RepositoryEnumeration,
        *,
        capture_root: str | Path,
        mirror_path: str | Path,
        pinned_ref: str,
        expected_repository_name: str,
    ) -> EnumerationRecomputeResult:
        calls.append(
            {
                "baseline": received,
                "capture_root": capture_root,
                "mirror_path": mirror_path,
                "pinned_ref": pinned_ref,
                "expected_repository_name": expected_repository_name,
            }
        )
        return result

    monkeypatch.setattr(
        module, "recompute_repository_enumeration_from_github_captures", recompute
    )
    arguments = _arguments(root)

    assert module.main(arguments) == 0
    first = json.loads(capsys.readouterr().out)
    loaded = load_repository_enumeration(
        root, repository_key="example", selection_epoch=SELECTION_EPOCH_V2
    )
    assert first["enumeration_action"] == "written"
    assert first["audit_count"] == 2
    assert first["terminal_count"] == 1
    assert loaded == result.recomputed
    assert load_repository_enumeration(root, repository_key="example") == baseline
    first_created_at = _v2_created_at(root)
    first_audit_created_at = _artifact_created_at(
        root, "enumeration_audit/example/1/current/v2"
    )
    first_terminal_created_at = _artifact_created_at(
        root, "enumeration_terminal/example/2/not-in-pinned/v2"
    )

    assert module.main(arguments) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["enumeration_action"] == "resumed"
    assert second["audit_action"] == "resumed"
    assert _v2_created_at(root) == first_created_at
    assert (
        _artifact_created_at(root, "enumeration_audit/example/1/current/v2")
        == first_audit_created_at
    )
    assert (
        _artifact_created_at(root, "enumeration_terminal/example/2/not-in-pinned/v2")
        == first_terminal_created_at
    )
    assert calls == [
        {
            "baseline": baseline,
            "capture_root": tmp_path_placeholder(root, "captures"),
            "mirror_path": tmp_path_placeholder(root, "mirror.git"),
            "pinned_ref": "refs/remotes/origin/main",
            "expected_repository_name": "example/project",
        },
        {
            "baseline": baseline,
            "capture_root": tmp_path_placeholder(root, "captures"),
            "mirror_path": tmp_path_placeholder(root, "mirror.git"),
            "pinned_ref": "refs/remotes/origin/main",
            "expected_repository_name": "example/project",
        },
    ]


def test_commit_cli_refuses_divergent_v2_head_without_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    root = tmp_path / "selection"
    baseline, result = _baseline_and_result()
    _write_v1(root, baseline)
    divergent = RepositoryEnumeration(
        repository_key="example",
        merged_change_numbers=(1, 2),
        authoritative_issue_link_missing=(1,),
        repair_not_in_pinned=(2,),
        candidates=(),
    )
    write_repository_enumeration(
        root,
        divergent,
        authorization=_authorization(
            "local-enumeration/divergent", "2099-01-01T00:00:00Z"
        ),
        selection_epoch=SELECTION_EPOCH_V2,
    )
    before = _v2_created_at(root)
    monkeypatch.setattr(
        module,
        "recompute_repository_enumeration_from_github_captures",
        lambda *_args, **_kwargs: result,
    )

    with pytest.raises(RuntimeError, match="diverges from the offline recomputation"):
        module.main(_arguments(root))

    assert _v2_created_at(root) == before
    assert (
        load_repository_enumeration(
            root, repository_key="example", selection_epoch=SELECTION_EPOCH_V2
        )
        == divergent
    )


def test_commit_cli_dispatches_blender_to_gitea_recompute(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    root = tmp_path / "selection"
    baseline, result = _baseline_and_result(repository_key="blender")
    _write_v1(root, baseline)
    calls: list[dict[str, object]] = []

    def gitea_recompute(
        received: RepositoryEnumeration,
        *,
        capture_root: str | Path,
        mirror_path: str | Path,
        pinned_ref: str,
        expected_repository_name: str,
        expected_api_base: str,
    ) -> EnumerationRecomputeResult:
        calls.append(
            {
                "baseline": received,
                "capture_root": capture_root,
                "mirror_path": mirror_path,
                "pinned_ref": pinned_ref,
                "expected_repository_name": expected_repository_name,
                "expected_api_base": expected_api_base,
            }
        )
        return result

    monkeypatch.setattr(
        module, "recompute_repository_enumeration_from_gitea_captures", gitea_recompute
    )
    monkeypatch.setattr(
        module,
        "recompute_repository_enumeration_from_github_captures",
        lambda *_args, **_kwargs: pytest.fail("Blender must not use GitHub recompute"),
    )

    assert (
        module.main(
            _arguments(
                root,
                repository_key="blender",
                expected_repository_name="blender/blender",
                expected_api_base="https://projects.blender.org/api/v1",
            )
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["repository_key"] == "blender"
    assert output["enumeration_action"] == "written"
    assert calls == [
        {
            "baseline": baseline,
            "capture_root": tmp_path_placeholder(root, "captures"),
            "mirror_path": tmp_path_placeholder(root, "mirror.git"),
            "pinned_ref": "refs/remotes/origin/main",
            "expected_repository_name": "blender/blender",
            "expected_api_base": "https://projects.blender.org/api/v1",
        }
    ]


def test_commit_cli_rejects_blender_without_canonical_gitea_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    root = tmp_path / "selection"
    baseline, _ = _baseline_and_result(repository_key="blender")
    _write_v1(root, baseline)
    monkeypatch.setattr(
        module,
        "recompute_repository_enumeration_from_gitea_captures",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid Blender authority must not replay"
        ),
    )

    with pytest.raises(RuntimeError, match="--expected-api-base"):
        module.main(
            _arguments(
                root,
                repository_key="blender",
                expected_repository_name="blender/blender",
            )
        )
    with pytest.raises(RuntimeError, match="explicit mirror ref"):
        module.main(
            _arguments(
                root,
                repository_key="blender",
                expected_repository_name="blender/blender",
                expected_api_base="https://projects.blender.org/api/v1",
                pinned_ref="HEAD",
            )
        )


def test_commit_cli_requires_explicit_confirmation(tmp_path: Path) -> None:
    module = _load_module()

    with pytest.raises(SystemExit) as error:
        module.main(
            [
                "--artifact-root",
                str(tmp_path / "selection"),
                "--repository",
                "example",
                "--capture-root",
                str(tmp_path / "captures"),
                "--mirror",
                str(tmp_path / "mirror.git"),
                "--pinned-ref",
                "refs/remotes/origin/main",
                "--expected-repository-name",
                "example/project",
                "--authorization-id",
                "local-enumeration/commit",
            ]
        )

    assert error.value.code == 2
    with pytest.raises(RuntimeError, match="artifact directory is missing"):
        load_repository_enumeration(
            tmp_path / "selection",
            repository_key="example",
            selection_epoch=SELECTION_EPOCH_V2,
        )


def _load_module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "commit_c1_lazarus_enumeration_v2.py"
    spec = importlib.util.spec_from_file_location("c1_v2_enum_commit_cli", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("commit CLI module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _baseline_and_result(
    *, repository_key: str = "example"
) -> tuple[RepositoryEnumeration, EnumerationRecomputeResult]:
    baseline = RepositoryEnumeration(
        repository_key=repository_key,
        merged_change_numbers=(1, 2),
        authoritative_issue_link_missing=(),
        repair_not_in_pinned=(1, 2),
        candidates=(),
    )
    recomputed = RepositoryEnumeration(
        repository_key=repository_key,
        merged_change_numbers=(1, 2),
        authoritative_issue_link_missing=(),
        repair_not_in_pinned=(2,),
        candidates=(CandidateKey(repository_key, 1),),
    )
    return baseline, EnumerationRecomputeResult(
        baseline=baseline,
        recomputed=recomputed,
        capture_file_count=1,
        promoted_to_candidate_numbers=(1,),
        reclassified_to_issue_link_missing_numbers=(),
        genuine_nonancestor_numbers=(2,),
        blocked_capture_structural_numbers=(),
        blocked_merge_capture_missing_numbers=(),
        blocked_merge_object_missing_numbers=(),
        blocked_relationship_unverified_numbers=(),
        blocked_ancestry_unavailable_numbers=(),
    )


def _write_v1(root: Path, enumeration: RepositoryEnumeration) -> None:
    write_repository_enumeration(
        root,
        enumeration,
        authorization=_authorization(
            "local-enumeration/baseline", "2026-08-13T00:00:00Z"
        ),
    )


def _authorization(identifier: str, granted_at: str) -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id=identifier,
        scope="repository-enumeration",
        granted_at=granted_at,
        local_user_id=os.getuid(),
    )


def _arguments(
    root: Path,
    *,
    repository_key: str = "example",
    expected_repository_name: str = "example/project",
    expected_api_base: str | None = None,
    pinned_ref: str = "refs/remotes/origin/main",
) -> list[str]:
    arguments = [
        "--artifact-root",
        str(root),
        "--repository",
        repository_key,
        "--capture-root",
        str(tmp_path_placeholder(root, "captures")),
        "--mirror",
        str(tmp_path_placeholder(root, "mirror.git")),
        "--pinned-ref",
        pinned_ref,
        "--expected-repository-name",
        expected_repository_name,
        "--authorization-id",
        "local-enumeration/commit",
        "--confirm",
    ]
    if expected_api_base is not None:
        arguments.extend(("--expected-api-base", expected_api_base))
    return arguments


def tmp_path_placeholder(root: Path, name: str) -> Path:
    return root.parent / name


def _v2_created_at(root: Path) -> str:
    artifact = load_repository_enumeration(
        root, repository_key="example", selection_epoch=SELECTION_EPOCH_V2
    )
    assert artifact.repository_key == "example"
    return _artifact_created_at(root, "repository_enumeration/example/current/v2")


def _artifact_created_at(root: Path, artifact_id: str) -> str:
    head = root / "artifacts"
    for segment in artifact_id.split("/"):
        head /= segment
    head = head / "2.0" / "head.json"
    return json.loads(head.read_text(encoding="utf-8"))["created_at"]
