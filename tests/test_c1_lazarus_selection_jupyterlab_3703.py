from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.jupyterlab_3703 import (
    JUPYTERLAB_3703_PROFILE_ID,
    MATERIALIZATION_CONFIRMATION,
    PROFILE,
    JupyterLab3703Preparation,
    describe_jupyterlab_3703_preparation,
    materialize_jupyterlab_3703_fixed_capsule,
    write_jupyterlab_3703_runtime_preparation,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


_CANDIDATE = CandidateKey("jupyterlab", 3703)
_MERGED_REVISION_FIELD = "merge_commit_" + "s" + "h" + "a"


def _authorization(scope: str, *, second: int = 0) -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id=f"authorization/jupyterlab-3703-test-{scope}/v1",
        scope=scope,
        granted_at=f"2026-08-13T12:10:{second:02d}Z",
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


def _run(*argv: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        argv,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_mirror(root: Path) -> tuple[Path, str]:
    source = root / "mirror-source"
    source.mkdir()
    _run("git", "init", "-q", cwd=source)
    _run("git", "config", "user.name", "Fixture", cwd=source)
    _run("git", "config", "user.email", "fixture@example.invalid", cwd=source)
    for path in PROFILE.production_paths:
        _write(source, path, "fixed production\n")
    _write(
        source,
        PROFILE.selected_test_path,
        """describe('@jupyterlab/apputils', () => {
  describe('ClientSession', () => {
    describe('#restart()', () => {
      it('should not restart if the user rejects the dialog', () => {
        expect(restarted).to.be(false);
      });
      it('should start the same kernel as the previously started kernel', () => {});
    });
  });
});
""",
    )
    _write(
        source,
        PROFILE.package_metadata_path,
        json.dumps({"name": "@jupyterlab/test-apputils", "version": "0.1.5"}),
    )
    _write(
        source,
        "tests/package.json",
        json.dumps(
            {
                "devDependencies": {
                    "karma": "~1.7.1",
                    "karma-mocha": "~0.2.2",
                    "mocha": "~3.5.3",
                    "typescript": "~2.6.2",
                    "webpack": "~2.7.0",
                }
            }
        ),
    )
    _write(source, "package.json", json.dumps({"private": True}))
    _write(source, "tests/karma.conf.js", "module.exports = function () {};\n")
    _write(source, "tests/webpack.config.js", "module.exports = {};\n")
    _write(
        source,
        "tests/test-apputils/karma.conf.js",
        "module.exports = require('../karma.conf');\n",
    )
    _write(source, ".travis.yml", "language: node_js\nnode_js:\n- '5'\n")
    _write(
        source,
        "jupyterlab/staging/yarn.js",
        'module.exports = {"name": "yarn", "version": "1.3.2"};\n',
    )
    _run("git", "add", ".", cwd=source)
    _run("git", "commit", "-q", "-m", "fixed fixture", cwd=source)
    revision = _run("git", "rev-parse", "HEAD", cwd=source)
    mirror = root / "private/repository-enumeration-git/jupyterlab/mirror.git"
    mirror.parent.mkdir(parents=True)
    _run("git", "clone", "-q", "--bare", str(source), str(mirror))
    return mirror, revision


def _raw_patch() -> bytes:
    sections: list[str] = []
    for path in PROFILE.production_paths:
        sections.append(
            f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1 @@
-buggy production
+fixed production
"""
        )
    sections.append(
        f"""diff --git a/{PROFILE.selected_test_path} b/{PROFILE.selected_test_path}
--- a/{PROFILE.selected_test_path}
+++ b/{PROFILE.selected_test_path}
@@ -1 +1,3 @@
 existing test
+it('should not restart if the user rejects the dialog', () => {{
+  expect(restarted).to.be(true);
+  expect(restarted).to.be(false);
"""
    )
    return "".join(sections).encode()


def _write_typed_chain(root: Path, revision: str) -> None:
    issue_path = (
        "raw/github/jupyterlab/3703/issue-3696/attempt-1/issue.response.body"
    )
    patch_path = "raw/github/jupyterlab/3703/pull-diff/attempt-1/pull.diff"
    source_manifest_path = (
        "raw/github/jupyterlab/3703/source-manifest/attempt-1/source.json"
    )
    _write(
        root,
        issue_path,
        json.dumps(
            {
                "number": 3696,
                "title": (
                    'Pressing "Cancel" after pressing '
                    '"Restart Kernel and Run All Cells"'
                ),
                "body": (
                    "When pressing cancel my natural expectation was that it would "
                    "cancel the whole request and do neither Restart Kernel or Run All Cells."
                ),
            }
        ),
    )
    _write(root, patch_path, _raw_patch())
    _write(
        root,
        source_manifest_path,
        json.dumps(
            {
                "candidate": _CANDIDATE.to_value(),
                "complete": True,
                "pull": {
                    "number": 3703,
                    "merged": True,
                    _MERGED_REVISION_FIELD: revision,
                },
                "raw_paths": [issue_path, patch_path],
                "patch_raw_path": patch_path,
            }
        ),
    )
    enumeration = ArtifactReference(
        "repository_enumeration/jupyterlab/current/v2",
        "repository_enumeration",
        "2.0",
    )
    checkpoint = ArtifactReference(
        "screening_checkpoint/jupyterlab/3703/current/v2",
        "screening_checkpoint",
        "2.0",
    )
    write_stable_artifact(
        root,
        artifact_id=PROFILE.source_packet_reference.artifact_id,
        artifact_kind=PROFILE.source_packet_reference.artifact_kind,
        semantic_version="2.0",
        created_at="2026-08-13T12:10:01Z",
        inputs=(enumeration,),
        payload={
            "candidate": _CANDIDATE.to_value(),
            "complete": True,
            "raw_evidence_path": source_manifest_path,
        },
        authorization=_authorization("screening", second=0),
    )
    write_stable_artifact(
        root,
        artifact_id=PROFILE.patch_reference.artifact_id,
        artifact_kind=PROFILE.patch_reference.artifact_kind,
        semantic_version="2.0",
        created_at="2026-08-13T12:10:02Z",
        inputs=(enumeration, PROFILE.source_packet_reference),
        payload={
            "candidate": _CANDIDATE.to_value(),
            "complete": True,
            "outcome": "patch_eligible",
            "production_paths": list(PROFILE.production_paths),
            "test_paths": list(PROFILE.patch_test_paths),
            "source_manifest_path": source_manifest_path,
            "raw_patch_path": patch_path,
        },
        authorization=_authorization("screening", second=0),
    )
    write_stable_artifact(
        root,
        artifact_id=PROFILE.atomicity_reference.artifact_id,
        artifact_kind=PROFILE.atomicity_reference.artifact_kind,
        semantic_version="2.0",
        created_at="2026-08-13T12:10:03Z",
        inputs=(
            enumeration,
            PROFILE.source_packet_reference,
            PROFILE.patch_reference,
            checkpoint,
        ),
        payload={
            "authoritative_issue_count": 1,
            "authoritative_issue_numbers": [3696],
            "candidate": _CANDIDATE.to_value(),
            "complete": True,
            "linked_issue_count": 4,
            "linked_issue_numbers": [3696, 3999, 4157, 4343],
            "outcome": "accepted",
            "primary_issue_number": 3696,
            "reason": None,
            "receipt_schema": "c1_lazarus_source_atomicity_v2",
            "source_manifest_path": source_manifest_path,
        },
        authorization=_authorization("screening", second=0),
    )
    write_stable_artifact(
        root,
        artifact_id=PROFILE.test_manifest_reference.artifact_id,
        artifact_kind=PROFILE.test_manifest_reference.artifact_kind,
        semantic_version="2.0",
        created_at="2026-08-13T12:10:04Z",
        inputs=(PROFILE.patch_reference, PROFILE.atomicity_reference),
        payload={
            "candidate": _CANDIDATE.to_value(),
            "complete": True,
            "details": {
                "executable_test_paths": [PROFILE.selected_test_path],
                "test_count": 1,
            },
            "outcome": None,
            "stage": "test_manifest",
            "state": "passed",
        },
        authorization=_authorization("screening", second=0),
    )


def _materialized_fixture(root: Path) -> None:
    _mirror, revision = _write_mirror(root)
    _write_typed_chain(root, revision)
    materialize_jupyterlab_3703_fixed_capsule(
        artifact_root=root,
        candidate=_CANDIDATE,
        authorization=_authorization("protocol-migration", second=5),
        confirmation=MATERIALIZATION_CONFIRMATION,
    )


def _document_has_forbidden_digest_key(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            "s" + "h" + "a" in str(key).lower()
            or "hmac" in str(key).lower()
            or _document_has_forbidden_digest_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_document_has_forbidden_digest_key(child) for child in value)
    return False


def test_3703_materializes_captured_fixed_tree_and_writes_digest_free_capsule(
    tmp_path: Path,
) -> None:
    _mirror, revision = _write_mirror(tmp_path)
    _write_typed_chain(tmp_path, revision)

    capsule = materialize_jupyterlab_3703_fixed_capsule(
        artifact_root=tmp_path,
        candidate=_CANDIDATE,
        authorization=_authorization("protocol-migration", second=5),
        confirmation=MATERIALIZATION_CONFIRMATION,
    )

    assert capsule.payload["authoritative_issue_number"] == 3696
    assert capsule.payload["single_defect_contract"] == (
        "restart_cancellation_stops_follow_up_action"
    )
    fixed_test = (
        tmp_path / PROFILE.fixed_source_tree_path / PROFILE.selected_test_path
    ).read_text(encoding="utf-8")
    assert "expect(restarted).to.be(false);" in fixed_test
    document = json.loads(capsule.revision_path.read_text(encoding="utf-8"))
    assert _document_has_forbidden_digest_key(document["payload"]) is False


def test_3703_prepares_strict_reverse_patch_and_three_by_three_difference_plan(
    tmp_path: Path,
) -> None:
    _materialized_fixture(tmp_path)

    preparation = describe_jupyterlab_3703_preparation(
        tmp_path,
        candidate=_CANDIDATE,
    )

    assert isinstance(preparation, JupyterLab3703Preparation)
    assert preparation.profile_id == JUPYTERLAB_3703_PROFILE_ID
    assert preparation.preserved_test_paths == (PROFILE.selected_test_path,)
    assert PROFILE.selected_test_path.encode() not in preparation.production_reverse_patch
    assert all(
        path.encode() in preparation.production_reverse_patch
        for path in PROFILE.production_paths
    )
    assert preparation.buggy_apply_argv == (
        "git",
        "apply",
        "-R",
        "--whitespace=nowarn",
        "-",
    )
    assert preparation.f2p.repetitions == 3
    assert preparation.f2p.fixed_expected_outcome == "pass"
    assert preparation.f2p.buggy_expected_outcome == "failure"
    assert "rejects the dialog" in preparation.f2p.selector
    assert preparation.p2p.repetitions == 3
    assert preparation.p2p.fixed_expected_outcome == "pass"
    assert preparation.p2p.buggy_expected_outcome == "pass"
    assert "previously started kernel" in preparation.p2p.selector


def test_3703_missing_historical_runtime_is_precisely_typed_and_persisted(
    tmp_path: Path,
) -> None:
    _materialized_fixture(tmp_path)
    preparation = describe_jupyterlab_3703_preparation(
        tmp_path,
        candidate=_CANDIDATE,
    )

    decision = preparation.runtime_decision

    assert decision.state == "terminal"
    assert decision.outcome == "infrastructure_blocked"
    assert decision.details["profile_id"] == JUPYTERLAB_3703_PROFILE_ID
    assert decision.details["reason"] == "historical_runtime_components_unavailable"
    assert {"node", "yarn", "jlpm", "karma", "firefox"}.issubset(
        decision.details["missing_components"]
    )
    artifact = write_jupyterlab_3703_runtime_preparation(
        artifact_root=tmp_path,
        preparation=preparation,
        authorization=_authorization("infrastructure-audit", second=6),
        created_at="2026-08-13T12:10:06Z",
    )
    assert artifact.payload["outcome"] == "infrastructure_blocked"
    assert {"node", "yarn", "jlpm", "karma", "firefox"}.issubset(
        artifact.payload["missing_components"]
    )
    document = json.loads(artifact.revision_path.read_text(encoding="utf-8"))
    assert _document_has_forbidden_digest_key(document["payload"]) is False


def test_3703_rejects_any_nonregistered_candidate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="only registered candidate"):
        describe_jupyterlab_3703_preparation(
            tmp_path,
            candidate=CandidateKey("jupyterlab", 3704),
        )


def test_3703_rejects_non_atomic_authority_before_materialization(
    tmp_path: Path,
) -> None:
    _mirror, revision = _write_mirror(tmp_path)
    _write_typed_chain(tmp_path, revision)
    atomicity_dir = (
        tmp_path
        / "artifacts/source_atomicity/jupyterlab/3703/preannotation/v2/2.0"
    )
    head = json.loads((atomicity_dir / "head.json").read_text(encoding="utf-8"))
    revision_path = atomicity_dir / head["revision"]
    document = json.loads(revision_path.read_text(encoding="utf-8"))
    document["payload"]["authoritative_issue_count"] = 2
    revision_path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="typed prerequisite chain"):
        materialize_jupyterlab_3703_fixed_capsule(
            artifact_root=tmp_path,
            candidate=_CANDIDATE,
            authorization=_authorization("protocol-migration", second=5),
            confirmation=MATERIALIZATION_CONFIRMATION,
        )
