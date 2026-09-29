from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.jupyterlab_regression import (
    JupyterLabRegressionExecutionPlan,
    plan_jupyterlab_regression_execution,
    profile_for_candidate,
)
from warptest.c1_lazarus_selection.jupyterlab_fixed_source_capsule import (
    CONFIRMATION as CAPSULE_CONFIRMATION,
    materialize_jupyterlab_fixed_source,
)
from warptest.c1_lazarus_selection.jupyterlab_historical_runtime import (
    CONFIRMATION as RUNTIME_CONFIRMATION,
    audit_runtime_source,
    package_historical_runtime,
    write_runtime_preparation,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.pipeline import StageDecision
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="authorization/jupyterlab-regression-test/v1",
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


def _patch(candidate: CandidateKey) -> bytes:
    profile = profile_for_candidate(candidate)
    sections = []
    for path in profile.production_paths:
        sections.append(
            f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1 @@
-buggy production
+fixed production
"""
        )
    for path in profile.patch_test_paths:
        sections.append(
            f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1,2 @@
 existing test
+{profile.patch_test_marker}
"""
        )
    return "".join(sections).encode()


def _write_chain(root: Path, candidate: CandidateKey) -> None:
    profile = profile_for_candidate(candidate)
    source_manifest = (
        f"raw/github/jupyterlab/{candidate.change_number}/source-manifest/"
        "attempt-1/source.json"
    )
    patch_path = (
        f"raw/github/jupyterlab/{candidate.change_number}/pull-diff/"
        "attempt-1/response.body"
    )
    _write(root, patch_path, _patch(candidate))
    _write(
        root,
        source_manifest,
        json.dumps(
            {
                "candidate": candidate.to_value(),
                "complete": True,
                "patch_raw_path": patch_path,
                "raw_paths": [patch_path],
                "attachment_paths": [],
            }
        ),
    )
    enumeration = ArtifactReference(
        "repository_enumeration/jupyterlab/current/v2",
        "repository_enumeration",
        "2.0",
    )
    write_stable_artifact(
        root,
        artifact_id=profile.source_packet_reference.artifact_id,
        artifact_kind=profile.source_packet_reference.artifact_kind,
        semantic_version="2.0",
        created_at="2026-08-13T12:00:01Z",
        inputs=(enumeration,),
        payload={
            "candidate": candidate.to_value(),
            "complete": True,
            "raw_evidence_path": source_manifest,
        },
        authorization=_authorization(),
    )
    write_stable_artifact(
        root,
        artifact_id=profile.patch_reference.artifact_id,
        artifact_kind=profile.patch_reference.artifact_kind,
        semantic_version="2.0",
        created_at="2026-08-13T12:00:02Z",
        inputs=(enumeration, profile.source_packet_reference),
        payload={
            "candidate": candidate.to_value(),
            "complete": True,
            "outcome": "patch_eligible",
            "production_paths": list(profile.production_paths),
            "test_paths": list(profile.patch_test_paths),
            "source_manifest_path": source_manifest,
            "raw_patch_path": patch_path,
        },
        authorization=_authorization(),
    )
    write_stable_artifact(
        root,
        artifact_id=profile.atomicity_reference.artifact_id,
        artifact_kind=profile.atomicity_reference.artifact_kind,
        semantic_version="2.0",
        created_at="2026-08-13T12:00:03Z",
        inputs=(
            enumeration,
            profile.source_packet_reference,
            profile.patch_reference,
            ArtifactReference(
                f"screening_checkpoint/jupyterlab/{candidate.change_number}/current/v2",
                "screening_checkpoint",
                "2.0",
            ),
        ),
        payload={
            "authoritative_issue_count": 1,
            "authoritative_issue_numbers": [profile.primary_issue_number],
            "candidate": candidate.to_value(),
            "complete": True,
            "linked_issue_count": len(profile.linked_issue_numbers),
            "linked_issue_numbers": list(profile.linked_issue_numbers),
            "outcome": "accepted",
            "primary_issue_number": profile.primary_issue_number,
            "reason": None,
            "receipt_schema": "c1_lazarus_source_atomicity_v2",
            "source_manifest_path": source_manifest,
        },
        authorization=_authorization(),
    )
    write_stable_artifact(
        root,
        artifact_id=profile.test_manifest_reference.artifact_id,
        artifact_kind=profile.test_manifest_reference.artifact_kind,
        semantic_version="2.0",
        created_at="2026-08-13T12:00:04Z",
        inputs=(profile.patch_reference, profile.atomicity_reference),
        payload={
            "candidate": candidate.to_value(),
            "complete": True,
            "details": {
                "executable_test_paths": [profile.selected_test_path],
                "test_count": 1,
            },
            "outcome": None,
            "stage": "test_manifest",
            "state": "passed",
        },
        authorization=_authorization(),
    )


def _write_capsule(root: Path, candidate: CandidateKey) -> None:
    profile = profile_for_candidate(candidate)
    for path in (*profile.production_paths, *profile.patch_test_paths):
        marker = profile.fixed_test_marker if path == profile.selected_test_path else ""
        _write(root, f"{profile.fixed_source_tree_path}/{path}", marker + "\n")
    write_stable_artifact(
        root,
        artifact_id=profile.fixed_capsule_reference.artifact_id,
        artifact_kind=profile.fixed_capsule_reference.artifact_kind,
        semantic_version="2.0",
        created_at="2026-08-13T12:00:05Z",
        inputs=(
            profile.source_packet_reference,
            profile.patch_reference,
            profile.atomicity_reference,
            profile.test_manifest_reference,
        ),
        payload={
            "candidate": candidate.to_value(),
            "profile_id": profile.profile_id,
            "source_origin": "captured_merged_revision",
            "source_tree_path": profile.fixed_source_tree_path,
            "production_paths": list(profile.production_paths),
            "test_paths": list(profile.patch_test_paths),
        },
        authorization=_authorization(),
    )


def _write_runtime(root: Path, candidate: CandidateKey) -> None:
    profile = profile_for_candidate(candidate)
    runtime = root / profile.runtime_root
    command_paths = {
        "git": "bin/git",
        "node": "bin/node",
        "npm": "bin/npm",
        "karma": "node_modules/karma/bin/karma",
        "webpack": "node_modules/webpack/bin/webpack.js",
        "typescript": "node_modules/typescript/bin/tsc",
        "firefox": "bin/firefox",
    }
    if profile.package_manager == "jlpm":
        command_paths.update({"yarn": "bin/yarn", "jlpm": "bin/jlpm"})
    for relative in command_paths.values():
        _write(runtime, relative, "registered executable\n").chmod(0o700)
    package_versions = {
        "npm": profile.required_versions["npm"],
        "karma": profile.required_versions["karma"],
        "mocha": profile.required_versions["mocha"],
        "karma-mocha": profile.required_versions["karma_mocha"],
        "webpack": profile.required_versions["webpack"],
        "typescript": profile.required_versions["typescript"],
    }
    package_paths = []
    for name, version in package_versions.items():
        relative = f"node_modules/{name}/package.json"
        _write(runtime, relative, json.dumps({"name": name, "version": version}))
        package_paths.append(relative)
    closure = sorted((*command_paths.values(), *package_paths))
    _write(
        runtime,
        "runtime.json",
        json.dumps(
            {
                "dependency_id": profile.runtime_dependency_id,
                "semantic_version": "1.0",
                "commands": command_paths,
                "versions": dict(profile.required_versions),
                "closure_files": closure,
            }
        ),
    )


@pytest.mark.parametrize("number", [444, 574, 611, 1250])
def test_profiles_bind_only_issue_related_tests(number: int) -> None:
    profile = profile_for_candidate(CandidateKey("jupyterlab", number))

    assert profile.selected_test_path in profile.patch_test_paths
    assert profile.f2p_selector.startswith(profile.mocha_suite_prefix)
    assert profile.p2p_selector.startswith(profile.mocha_suite_prefix)
    assert profile.f2p_selector != profile.p2p_selector
    assert profile.package_manager == "npm"
    assert profile.test_framework == "karma-mocha"
    assert profile.yarn_version is None
    assert profile.jest_version is None
    assert profile.required_versions["node"] == "5.12.0"
    assert profile.required_versions["mocha"] == "2.5.3"


def test_planner_fails_closed_when_registered_runtime_is_missing(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("jupyterlab", 611)
    _write_chain(tmp_path, candidate)

    result = plan_jupyterlab_regression_execution(tmp_path, candidate=candidate)

    assert result == StageDecision.terminal(
        "infrastructure_blocked",
        {
            "profile_id": profile_for_candidate(candidate).profile_id,
            "reason": "registered_runtime_unavailable",
            "runtime_root": profile_for_candidate(candidate).runtime_root,
        },
    )


def test_complete_closed_profile_preserves_fixed_tests_in_buggy_tree(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("jupyterlab", 1250)
    profile = profile_for_candidate(candidate)
    _write_chain(tmp_path, candidate)
    _write_runtime(tmp_path, candidate)
    _write_capsule(tmp_path, candidate)

    result = plan_jupyterlab_regression_execution(tmp_path, candidate=candidate)

    assert isinstance(result, JupyterLabRegressionExecutionPlan)
    assert result.fixed_capsule.source_origin == "captured_merged_revision"
    assert result.production_inverse.paths == profile.production_paths
    assert profile.selected_test_path.encode() not in result.production_inverse.patch
    assert all(
        path.encode() in result.production_inverse.patch
        for path in profile.production_paths
    )
    assert result.f2p.selector == profile.f2p_selector
    assert result.p2p.selector == profile.p2p_selector
    assert result.f2p.repetitions == result.p2p.repetitions == 3
    assert result.f2p.buggy_expected_outcome == "failure"
    assert result.p2p.buggy_expected_outcome == "pass"
    assert result.build_argv[0] == result.runtime.node
    assert result.test_argv[0] == result.runtime.node
    assert "--grep" in result.mocha_client_args


def test_2437_closed_plan_inverts_only_production_and_keeps_test(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("jupyterlab", 2437)
    profile = profile_for_candidate(candidate)
    _write_chain(tmp_path, candidate)
    _write_runtime(tmp_path, candidate)
    _write_capsule(tmp_path, candidate)

    result = plan_jupyterlab_regression_execution(tmp_path, candidate=candidate)

    assert isinstance(result, JupyterLabRegressionExecutionPlan)
    assert result.production_inverse.paths == profile.production_paths
    assert profile.selected_test_path.encode() not in result.production_inverse.patch
    assert profile.patch_test_marker.encode() not in result.production_inverse.patch
    assert all(
        path.encode() in result.production_inverse.patch
        for path in profile.production_paths
    )
    assert result.f2p.selector == profile.f2p_selector
    assert result.p2p.selector == profile.p2p_selector
    assert result.f2p.repetitions == result.p2p.repetitions == 3
    assert result.f2p.buggy_expected_outcome == "failure"
    assert result.p2p.buggy_expected_outcome == "pass"


def test_atomicity_rejection_is_not_masked_by_missing_runtime(tmp_path: Path) -> None:
    candidate = CandidateKey("jupyterlab", 574)
    profile = profile_for_candidate(candidate)
    _write_chain(tmp_path, candidate)
    head = (
        tmp_path
        / "artifacts"
        / profile.atomicity_reference.artifact_id
        / "2.0"
        / "head.json"
    )
    revision = json.loads(head.read_text())["revision"]
    artifact = head.parent / revision
    document = json.loads(artifact.read_text())
    document["payload"]["outcome"] = "rejected"
    artifact.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RuntimeError, match="source atomicity is invalid"):
        plan_jupyterlab_regression_execution(tmp_path, candidate=candidate)


def test_runtime_version_mismatch_fails_closed(tmp_path: Path) -> None:
    candidate = CandidateKey("jupyterlab", 444)
    profile = profile_for_candidate(candidate)
    _write_chain(tmp_path, candidate)
    _write_runtime(tmp_path, candidate)
    manifest = tmp_path / profile.runtime_root / "runtime.json"
    value = json.loads(manifest.read_text())
    value["versions"]["node"] = "22.22.0"
    manifest.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(RuntimeError, match="runtime manifest is invalid"):
        plan_jupyterlab_regression_execution(tmp_path, candidate=candidate)


def test_runtime_symlink_is_rejected(tmp_path: Path) -> None:
    candidate = CandidateKey("jupyterlab", 444)
    profile = profile_for_candidate(candidate)
    _write_chain(tmp_path, candidate)
    _write_runtime(tmp_path, candidate)
    node = tmp_path / profile.runtime_root / "bin/node"
    node.unlink()
    node.symlink_to("npm")

    with pytest.raises(RuntimeError, match="runtime command is unsafe"):
        plan_jupyterlab_regression_execution(tmp_path, candidate=candidate)


def test_unknown_candidate_is_rejected() -> None:
    with pytest.raises(ValueError, match="no registered JupyterLab regression profile"):
        profile_for_candidate(CandidateKey("jupyterlab", 9999))


def _run(*argv: str, cwd: Path | None = None) -> None:
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True)


def _write_captured_mirror(root: Path, candidate: CandidateKey) -> None:
    profile = profile_for_candidate(candidate)
    work = root / "mirror-source"
    work.mkdir()
    _run("git", "init", "-q", cwd=work)
    _run("git", "config", "user.name", "C1 Test", cwd=work)
    _run("git", "config", "user.email", "c1@example.invalid", cwd=work)
    _run("git", "commit", "--allow-empty", "-qm", "captured base", cwd=work)
    for relative in dict.fromkeys(
        (
            "package.json",
            profile.package_metadata_path,
            f"{profile.test_harness_root}/package.json",
            f"{profile.test_harness_root}/karma.conf.js",
            (
                f"{profile.test_harness_root}/webpack.config.js"
                if candidate.change_number in {2437, 3662}
                else f"{profile.test_harness_root}/webpack.conf.js"
            ),
            *profile.production_paths,
            *profile.patch_test_paths,
        )
    ):
        content = "captured extra file\n"
        if relative in {
            profile.package_metadata_path,
            f"{profile.test_harness_root}/package.json",
        }:
            dependency_names = (
                "karma",
                "karma-mocha",
                "mocha",
                "typescript",
                "webpack",
            )
            content = json.dumps(
                {
                    "version": (
                        profile.package_version
                        if relative == profile.package_metadata_path
                        else "test-root"
                    ),
                    "scripts": {profile.build_script: "tsc && webpack"},
                    "dependencies": (
                        {"mocha": "registered"}
                        if candidate.change_number == 2437
                        else {}
                    ),
                    "devDependencies": {
                        name: "registered"
                        for name in dependency_names
                        if candidate.change_number != 2437 or name != "mocha"
                    },
                }
            )
        elif relative == profile.selected_test_path:
            content = profile.fixed_test_marker + "\n"
        _write(work, relative, content)
    _write(work, "README.md", "whole captured tree\n")
    _run("git", "add", ".", cwd=work)
    _run("git", "commit", "-qm", "captured candidate revision", cwd=work)
    head_revision = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    captured_revision = head_revision
    if profile.captured_merged_revision_required:
        _run("git", "branch", "candidate-head", cwd=work)
        _run("git", "checkout", "-q", "-b", "merged-base", "HEAD^", cwd=work)
        _write(work, "MERGED_BASE_ONLY.txt", "merged base contribution\n")
        _run("git", "add", ".", cwd=work)
        _run("git", "commit", "-qm", "captured base contribution", cwd=work)
        _run("git", "merge", "-q", "--no-ff", "candidate-head", "-m", "merge", cwd=work)
        captured_revision = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=work,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        source = next(
            (root / "raw/github/jupyterlab" / str(candidate.change_number)).glob(
                "source-manifest/attempt-1/*.json"
            )
        )
        manifest = json.loads(source.read_text(encoding="utf-8"))
        manifest["pull"] = {
            "number": candidate.change_number,
            "merged": True,
            "merge_commit_" + "s" + "h" + "a": captured_revision,
        }
        source.write_text(json.dumps(manifest), encoding="utf-8")
    mirror = root / "private/repository-enumeration-git/jupyterlab/mirror.git"
    mirror.parent.mkdir(parents=True)
    _run("git", "clone", "-q", "--bare", str(work), str(mirror))
    _run(
        "git",
        f"--git-dir={mirror}",
        "update-ref",
        f"refs/c1/pull/{candidate.change_number}/head",
        head_revision,
    )


def test_fixed_capsule_materializes_the_complete_captured_tree(tmp_path: Path) -> None:
    candidate = CandidateKey("jupyterlab", 611)
    _write_chain(tmp_path, candidate)
    _write_captured_mirror(tmp_path, candidate)
    authorization = AuthorizationRecord(
        authorization_id="authorization/jupyterlab-capsule-test/v1",
        scope="protocol-migration",
        granted_at="2026-08-13T12:01:00Z",
        local_user_id=os.getuid(),
    )

    artifact = materialize_jupyterlab_fixed_source(
        artifact_root=tmp_path,
        candidate=candidate,
        authorization=authorization,
        confirmation=CAPSULE_CONFIRMATION,
    )

    tree = tmp_path / profile_for_candidate(candidate).fixed_source_tree_path
    assert (tree / "README.md").read_text() == "whole captured tree\n"
    assert artifact.payload["source_origin"] == "captured_merged_revision"
    assert artifact.payload["candidate"] == candidate.to_value()
    assert datetime.fromisoformat(
        artifact.created_at.replace("Z", "+00:00")
    ) > datetime.fromisoformat(authorization.granted_at.replace("Z", "+00:00"))
    resumed = materialize_jupyterlab_fixed_source(
        artifact_root=tmp_path,
        candidate=candidate,
        authorization=authorization,
        confirmation=CAPSULE_CONFIRMATION,
    )
    assert resumed.payload == artifact.payload
    planned = plan_jupyterlab_regression_execution(tmp_path, candidate=candidate)
    assert isinstance(planned, StageDecision)
    assert planned.details["reason"] == "registered_runtime_unavailable"


def test_2752_profile_is_a_direct_multiline_source_regression() -> None:
    profile = profile_for_candidate(CandidateKey("jupyterlab", 2752))

    assert profile.primary_issue_number == 2751
    assert profile.production_paths == ("packages/cells/src/model.ts",)
    assert profile.patch_test_paths == ("test/src/cells/model.spec.ts",)
    assert profile.package_metadata_path == "test/package.json"
    assert profile.package_version == "0.9.0"
    assert profile.f2p_selector == (
        "cells/model CellModel #constructor() "
        "should accept a base cell argument with a multiline source"
    )
    assert profile.p2p_selector == (
        "cells/model CellModel #constructor() should create a cell model"
    )
    assert profile.captured_merged_revision_required is True
    assert profile.required_versions == {
        "git": "2.51.0",
        "node": "5.12.0",
        "npm": "3.8.6",
        "karma": "1.7.0",
        "mocha": "3.5.0",
        "karma_mocha": "0.2.2",
        "webpack": "2.7.0",
        "typescript": "2.4.2",
        "firefox": "54.0",
    }


def test_3662_profile_is_the_file_editor_dirty_title_regression() -> None:
    profile = profile_for_candidate(CandidateKey("jupyterlab", 3662))

    assert profile.primary_issue_number == 3652
    assert profile.production_paths == ("packages/fileeditor/src/widget.ts",)
    assert profile.patch_test_paths == ("tests/test-fileeditor/src/widget.spec.ts",)
    assert profile.package_metadata_path == "tests/test-fileeditor/package.json"
    assert profile.package_version == "0.1.2"
    assert profile.f2p_selector == (
        "fileeditorcodewrapper FileEditor #constructor() "
        "should add the dirty class when the model is dirty"
    )
    assert profile.p2p_selector == (
        "fileeditorcodewrapper FileEditor #constructor() "
        "should update the editor text when the model changes"
    )
    assert profile.captured_merged_revision_required is True
    assert profile.required_versions == {
        "git": "2.51.0",
        "node": "5.12.0",
        "npm": "3.8.6",
        "karma": "1.7.1",
        "mocha": "3.5.3",
        "karma_mocha": "0.2.2",
        "webpack": "2.7.0",
        "typescript": "2.6.2",
        "firefox": "57.0",
    }


def test_2437_profile_is_the_closed_slideshow_metadata_regression() -> None:
    profile = profile_for_candidate(CandidateKey("jupyterlab", 2437))

    assert profile.primary_issue_number == 2436
    assert profile.production_paths == ("packages/notebook/src/celltools.ts",)
    assert profile.patch_test_paths == ("test/src/notebook/celltools.spec.ts",)
    assert profile.f2p_selector == (
        "notebook/celltools CellTools.createSlideShowSelector() "
        "should create a slide show selector"
    )
    assert profile.p2p_selector == (
        "notebook/celltools CellTools.createNBConvertSelector() "
        "should create a raw mimetype selector"
    )
    assert profile.package_metadata_path == "test/package.json"
    assert profile.package_version == "0.6.1"
    assert profile.authority_attachment_closure_required is True
    assert profile.captured_merged_revision_required is True
    assert profile.single_defect_contract_proven is True


def test_3310_profile_binds_only_issue_3309_and_a_direct_existing_api_test() -> None:
    profile = profile_for_candidate(CandidateKey("jupyterlab", 3310))

    assert profile.primary_issue_number == 3309
    assert profile.linked_issue_numbers == (3303, 3309)
    assert profile.production_paths == ("packages/application/src/shell.ts",)
    assert profile.patch_test_paths == ("test/src/application/shell.spec.ts",)
    assert profile.patch_test_marker == "widget.parent = null;"
    assert profile.f2p_selector == (
        "ApplicationShell #currentWidget should be the current widget in the shell main area"
    )
    assert profile.p2p_selector == (
        "ApplicationShell #isEmpty() should test whether the main area is empty"
    )
    assert profile.package_manager == "jlpm"
    assert profile.yarn_version == "1.3.2"
    assert profile.required_versions == {
        "git": "2.51.0",
        "node": "5.12.0",
        "npm": "3.8.6",
        "karma": "1.7.1",
        "mocha": "3.5.3",
        "karma_mocha": "0.2.2",
        "webpack": "2.7.0",
        "typescript": "2.6.2",
        "firefox": "57.0",
        "yarn": "1.3.2",
        "jlpm": "1.3.2",
    }
    assert profile.captured_merged_revision_required is True


def test_3310_closed_plan_uses_jlpm_and_inverts_only_production(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("jupyterlab", 3310)
    profile = profile_for_candidate(candidate)
    _write_chain(tmp_path, candidate)
    _write_runtime(tmp_path, candidate)
    _write_capsule(tmp_path, candidate)

    result = plan_jupyterlab_regression_execution(tmp_path, candidate=candidate)

    assert isinstance(result, JupyterLabRegressionExecutionPlan)
    assert result.build_argv == (result.runtime.jlpm, "run", "build:test")
    assert result.production_inverse.paths == profile.production_paths
    assert profile.selected_test_path.encode() not in result.production_inverse.patch
    assert profile.patch_test_marker.encode() not in result.production_inverse.patch
    assert result.f2p.repetitions == result.p2p.repetitions == 3


def test_2752_capsule_uses_the_captured_merge_not_the_candidate_head(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("jupyterlab", 2752)
    _write_chain(tmp_path, candidate)
    _write_captured_mirror(tmp_path, candidate)
    authorization = AuthorizationRecord(
        authorization_id="authorization/jupyterlab-2752-capsule-test/v1",
        scope="protocol-migration",
        granted_at="2026-08-13T12:01:00Z",
        local_user_id=os.getuid(),
    )

    artifact = materialize_jupyterlab_fixed_source(
        artifact_root=tmp_path,
        candidate=candidate,
        authorization=authorization,
        confirmation=CAPSULE_CONFIRMATION,
    )

    tree = tmp_path / profile_for_candidate(candidate).fixed_source_tree_path
    assert (tree / "MERGED_BASE_ONLY.txt").read_text() == "merged base contribution\n"
    assert artifact.payload["source_origin"] == "captured_merged_revision"


def test_3662_capsule_accepts_the_tests_harness_root(tmp_path: Path) -> None:
    candidate = CandidateKey("jupyterlab", 3662)
    _write_chain(tmp_path, candidate)
    _write_captured_mirror(tmp_path, candidate)
    authorization = AuthorizationRecord(
        authorization_id="authorization/jupyterlab-3662-capsule-test/v1",
        scope="protocol-migration",
        granted_at="2026-08-13T12:01:00Z",
        local_user_id=os.getuid(),
    )

    artifact = materialize_jupyterlab_fixed_source(
        artifact_root=tmp_path,
        candidate=candidate,
        authorization=authorization,
        confirmation=CAPSULE_CONFIRMATION,
    )

    tree = tmp_path / profile_for_candidate(candidate).fixed_source_tree_path
    assert (tree / "MERGED_BASE_ONLY.txt").read_text() == ("merged base contribution\n")
    assert (tree / "tests/karma.conf.js").is_file()
    assert (tree / "tests/webpack.config.js").is_file()
    assert artifact.payload["source_origin"] == "captured_merged_revision"


def test_2437_capsule_accepts_its_captured_webpack_config_name(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("jupyterlab", 2437)
    _write_chain(tmp_path, candidate)
    _write_captured_mirror(tmp_path, candidate)
    authorization = AuthorizationRecord(
        authorization_id="authorization/jupyterlab-2437-capsule-test/v1",
        scope="protocol-migration",
        granted_at="2026-08-13T12:01:00Z",
        local_user_id=os.getuid(),
    )

    artifact = materialize_jupyterlab_fixed_source(
        artifact_root=tmp_path,
        candidate=candidate,
        authorization=authorization,
        confirmation=CAPSULE_CONFIRMATION,
    )

    tree = tmp_path / profile_for_candidate(candidate).fixed_source_tree_path
    assert (tree / "test/webpack.config.js").is_file()
    assert not (tree / "test/webpack.conf.js").exists()
    assert artifact.payload["source_origin"] == "captured_merged_revision"


def test_3310_capsule_uses_the_captured_merge_not_the_candidate_head(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("jupyterlab", 3310)
    _write_chain(tmp_path, candidate)
    _write_captured_mirror(tmp_path, candidate)
    authorization = AuthorizationRecord(
        authorization_id="authorization/jupyterlab-3310-capsule-test/v1",
        scope="protocol-migration",
        granted_at="2026-08-13T12:01:00Z",
        local_user_id=os.getuid(),
    )

    artifact = materialize_jupyterlab_fixed_source(
        artifact_root=tmp_path,
        candidate=candidate,
        authorization=authorization,
        confirmation=CAPSULE_CONFIRMATION,
    )

    tree = tmp_path / profile_for_candidate(candidate).fixed_source_tree_path
    assert (tree / "MERGED_BASE_ONLY.txt").read_text() == "merged base contribution\n"
    assert artifact.payload["source_origin"] == "captured_merged_revision"


def test_fixed_capsule_requires_explicit_migration_confirmation(tmp_path: Path) -> None:
    candidate = CandidateKey("jupyterlab", 574)
    authorization = AuthorizationRecord(
        authorization_id="authorization/jupyterlab-capsule-test/v1",
        scope="protocol-migration",
        granted_at="2026-08-13T12:01:00Z",
        local_user_id=os.getuid(),
    )

    with pytest.raises(RuntimeError, match="explicit confirmation"):
        materialize_jupyterlab_fixed_source(
            artifact_root=tmp_path,
            candidate=candidate,
            authorization=authorization,
            confirmation="not-authorized",
        )


def _runtime_source(root: Path, candidate: CandidateKey) -> Path:
    profile = profile_for_candidate(candidate)
    source = root / "historical-runtime-source"
    node = """#!/bin/sh
case "$1" in
  --version) echo v5.12.0 ;;
  */bin/npm) echo 3.8.6 ;;
  */karma/bin/karma) echo 0.13.22 ;;
  */webpack/bin/webpack.js) echo 1.15.0 ;;
  */typescript/bin/tsc) echo 1.8.10 ;;
  *) exit 2 ;;
esac
"""
    scripts = {
        "bin/git": "#!/bin/sh\necho git version 2.51.0\n",
        "bin/node": node,
        "bin/npm": "registered npm cli\n",
        "bin/firefox": "#!/bin/sh\necho Mozilla Firefox 48.0\n",
        "node_modules/karma/bin/karma": "registered karma cli\n",
        "node_modules/webpack/bin/webpack.js": "registered webpack cli\n",
        "node_modules/typescript/bin/tsc": "registered TypeScript cli\n",
    }
    for relative, content in scripts.items():
        _write(source, relative, content).chmod(0o700)
    packages = {
        "npm": "npm",
        "karma": "karma",
        "mocha": "mocha",
        "karma_mocha": "karma-mocha",
        "webpack": "webpack",
        "typescript": "typescript",
    }
    for component, directory in packages.items():
        _write(
            source,
            f"node_modules/{directory}/package.json",
            json.dumps(
                {"name": directory, "version": profile.required_versions[component]}
            ),
        )
    return source


def test_runtime_packager_copies_the_full_exact_local_closure(tmp_path: Path) -> None:
    candidate = CandidateKey("jupyterlab", 444)
    profile = profile_for_candidate(candidate)
    artifact_root = tmp_path / "artifact-root"
    artifact_root.mkdir()
    source = _runtime_source(tmp_path, candidate)

    audit = audit_runtime_source(profile, source)
    destination = package_historical_runtime(
        artifact_root=artifact_root,
        profile=profile,
        source_tree=source,
        confirmation=RUNTIME_CONFIRMATION,
    )

    assert audit.ready
    manifest = json.loads((destination / "runtime.json").read_text())
    assert "node_modules/mocha/package.json" in manifest["closure_files"]
    assert set(manifest["closure_files"]) == {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() and path.name != "runtime.json"
    }


def test_missing_runtime_components_are_persisted_as_a_typed_blocker(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("jupyterlab", 1250)
    profile = profile_for_candidate(candidate)
    _write_chain(tmp_path, candidate)
    audit = audit_runtime_source(profile, None)
    authorization = AuthorizationRecord(
        authorization_id="authorization/jupyterlab-runtime-audit-test/v1",
        scope="infrastructure-audit",
        granted_at="2026-08-13T12:02:00Z",
        local_user_id=os.getuid(),
    )

    artifact = write_runtime_preparation(
        artifact_root=tmp_path,
        profile=profile,
        audit=audit,
        authorization=authorization,
        created_at="2026-08-13T12:02:01Z",
    )

    assert artifact.payload["outcome"] == "infrastructure_blocked"
    assert artifact.payload["reason"] == "historical_runtime_components_unavailable"
    assert artifact.payload["missing_components"] == [
        "git",
        "node",
        "npm",
        "karma",
        "mocha",
        "karma_mocha",
        "webpack",
        "typescript",
        "firefox",
    ]
    assert audit.decision().outcome == "infrastructure_blocked"
    planned = plan_jupyterlab_regression_execution(tmp_path, candidate=candidate)
    assert isinstance(planned, StageDecision)
    assert planned.details["reason"] == "historical_runtime_components_unavailable"
    assert planned.details["runtime_preparation_id"].startswith(
        "dependency_preparation/"
    )


def test_3310_runtime_blocker_names_node_yarn_jlpm_karma_and_firefox(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("jupyterlab", 3310)
    profile = profile_for_candidate(candidate)
    _write_chain(tmp_path, candidate)
    audit = audit_runtime_source(profile, None)
    authorization = AuthorizationRecord(
        authorization_id="authorization/jupyterlab-3310-runtime-audit/v1",
        scope="infrastructure-audit",
        granted_at="2026-08-13T12:02:00Z",
        local_user_id=os.getuid(),
    )

    artifact = write_runtime_preparation(
        artifact_root=tmp_path,
        profile=profile,
        audit=audit,
        authorization=authorization,
        created_at="2026-08-13T12:02:01Z",
    )

    missing = artifact.payload["missing_components"]
    assert missing == [
        "git",
        "node",
        "npm",
        "karma",
        "mocha",
        "karma_mocha",
        "webpack",
        "typescript",
        "firefox",
        "yarn",
        "jlpm",
    ]
    planned = plan_jupyterlab_regression_execution(tmp_path, candidate=candidate)
    assert isinstance(planned, StageDecision)
    assert planned.details["reason"] == "historical_runtime_components_unavailable"
    assert planned.details["missing_components"] == missing


def test_runtime_packager_rejects_a_symlinked_source_closure(tmp_path: Path) -> None:
    candidate = CandidateKey("jupyterlab", 444)
    profile = profile_for_candidate(candidate)
    artifact_root = tmp_path / "artifact-root"
    artifact_root.mkdir()
    source = _runtime_source(tmp_path, candidate)
    outside = _write(tmp_path, "outside.js", "outside\n")
    package = source / "node_modules/mocha/package.json"
    package.unlink()
    package.symlink_to(outside)

    with pytest.raises(RuntimeError, match="runtime source is not exact"):
        package_historical_runtime(
            artifact_root=artifact_root,
            profile=profile,
            source_tree=source,
            confirmation=RUNTIME_CONFIRMATION,
        )
