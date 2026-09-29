"""Offline preparation for the closed JupyterLab change 3703.

This adapter is intentionally isolated from ``jupyterlab_regression``.  It
typed-loads the already captured authority chain, materializes the captured
merged tree from the retained local mirror, and describes the future F2P/P2P
comparison.  It never installs dependencies, contacts a provider, builds the
candidate, or runs its tests.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Mapping

from .jupyterlab_fixed_source_legacy_migration_reader import (
    read_legacy_merged_revision,
)
from .jupyterlab_historical_runtime import (
    RuntimeAudit,
    audit_runtime_source,
    write_runtime_preparation,
)
from .models import CandidateKey
from .pipeline import StageDecision
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


JUPYTERLAB_3703_PROFILE_ID = "jupyterlab-3703-restart-cancellation-v1"
MATERIALIZATION_CONFIRMATION = "MATERIALIZE_JUPYTERLAB_3703_FIXED_SOURCE_V2"
_CANDIDATE = CandidateKey("jupyterlab", 3703)
_PRIMARY_ISSUE = 3696
_MIRROR = PurePosixPath("private/repository-enumeration-git/jupyterlab/mirror.git")
_FIXED_TREE = PurePosixPath(
    "private/fixed-capsules/jupyterlab/3703/captured-merged-tree"
)
_RUNTIME_ROOT = PurePosixPath(
    "private/dependencies/jupyterlab-3703/"
    "node5-yarn1.3.2-karma1.7.1-firefox57/1.0"
)
_PRODUCTION_PATHS = (
    "packages/apputils/src/clientsession.ts",
    "packages/console-extension/src/index.ts",
    "packages/mainmenu/src/kernel.ts",
    "packages/mainmenu/src/run.ts",
    "packages/notebook-extension/src/index.ts",
)
_TEST_PATH = "tests/test-apputils/src/clientsession.spec.ts"
_TEST_PATHS = (_TEST_PATH,)
_F2P_SELECTOR = (
    "@jupyterlab/apputils ClientSession #restart() "
    "should not restart if the user rejects the dialog"
)
_P2P_SELECTOR = (
    "@jupyterlab/apputils ClientSession #restart() "
    "should start the same kernel as the previously started kernel"
)
_F2P_MARKER = "should not restart if the user rejects the dialog"
_FIXED_ASSERTION = "expect(restarted).to.be(false);"
_P2P_MARKER = "should start the same kernel as the previously started kernel"
_REQUIRED_VERSIONS = {
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


def _reference(stage: str) -> ArtifactReference:
    values = {
        "source": (
            "candidate_source_packet/jupyterlab/3703/source/v2",
            "candidate_source_packet",
        ),
        "patch": (
            "candidate_test_patch/jupyterlab/3703/screening/v2",
            "candidate_test_patch",
        ),
        "atomicity": (
            "source_atomicity/jupyterlab/3703/preannotation/v2",
            "source_atomicity",
        ),
        "manifest": (
            "candidate_stage/jupyterlab/3703/test_manifest/v2",
            "candidate_stage",
        ),
        "capsule": (
            "fixed_source_capsule/jupyterlab/3703/fixed/v2",
            "fixed_source_capsule",
        ),
    }
    artifact_id, artifact_kind = values[stage]
    return ArtifactReference(artifact_id, artifact_kind, "2.0")


@dataclass(frozen=True)
class JupyterLab3703Profile:
    candidate: CandidateKey = _CANDIDATE
    profile_id: str = JUPYTERLAB_3703_PROFILE_ID
    primary_issue_number: int = _PRIMARY_ISSUE
    source_packet_reference: ArtifactReference = _reference("source")
    patch_reference: ArtifactReference = _reference("patch")
    atomicity_reference: ArtifactReference = _reference("atomicity")
    test_manifest_reference: ArtifactReference = _reference("manifest")
    fixed_capsule_reference: ArtifactReference = _reference("capsule")
    fixed_source_tree_path: str = _FIXED_TREE.as_posix()
    production_paths: tuple[str, ...] = _PRODUCTION_PATHS
    patch_test_paths: tuple[str, ...] = _TEST_PATHS
    selected_test_path: str = _TEST_PATH
    package_version: str = "0.1.5"
    package_metadata_path: str = "tests/test-apputils/package.json"
    test_harness_root: str = "tests/test-apputils"
    build_script: str = "build"
    package_manager: str = "jlpm"
    runtime_dependency_id: str = (
        "jupyterlab-3703-node5-yarn1.3.2-karma1.7.1-firefox57"
    )
    runtime_root: str = _RUNTIME_ROOT.as_posix()
    required_versions: Mapping[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.required_versions is None:
            object.__setattr__(self, "required_versions", dict(_REQUIRED_VERSIONS))
        if (
            self.candidate != _CANDIDATE
            or self.profile_id != JUPYTERLAB_3703_PROFILE_ID
            or self.primary_issue_number != _PRIMARY_ISSUE
            or self.production_paths != _PRODUCTION_PATHS
            or self.patch_test_paths != _TEST_PATHS
            or self.selected_test_path != _TEST_PATH
            or dict(self.required_versions) != _REQUIRED_VERSIONS
        ):
            raise ValueError("JupyterLab 3703 profile is invalid")


PROFILE = JupyterLab3703Profile()


@dataclass(frozen=True)
class JupyterLab3703RegressionRun:
    phase: str
    selector: str
    repetitions: int
    fixed_expected_outcome: str
    buggy_expected_outcome: str


@dataclass(frozen=True)
class JupyterLab3703Preparation:
    candidate: CandidateKey
    profile_id: str
    fixed_source_tree: Path
    production_reverse_patch: bytes
    preserved_test_paths: tuple[str, ...]
    buggy_apply_argv: tuple[str, ...]
    build_working_directory: str
    build_argv: tuple[str, ...]
    test_working_directory: str
    test_argv: tuple[str, ...]
    test_environment: Mapping[str, str]
    f2p: JupyterLab3703RegressionRun
    p2p: JupyterLab3703RegressionRun
    runtime_audit: RuntimeAudit

    def __post_init__(self) -> None:
        if (
            self.candidate != _CANDIDATE
            or self.profile_id != JUPYTERLAB_3703_PROFILE_ID
            or self.preserved_test_paths != _TEST_PATHS
            or self.buggy_apply_argv
            != ("git", "apply", "-R", "--whitespace=nowarn", "-")
            or self.f2p.repetitions != 3
            or self.p2p.repetitions != 3
            or self.f2p.fixed_expected_outcome != "pass"
            or self.f2p.buggy_expected_outcome != "failure"
            or self.p2p.fixed_expected_outcome != "pass"
            or self.p2p.buggy_expected_outcome != "pass"
        ):
            raise ValueError("JupyterLab 3703 preparation is invalid")

    @property
    def runtime_decision(self) -> StageDecision:
        if self.runtime_audit.ready:
            return StageDecision.passed(
                {
                    "profile_id": self.profile_id,
                    "dependency_id": self.runtime_audit.dependency_id,
                    "runtime_root": self.runtime_audit.runtime_root,
                }
            )
        return StageDecision.terminal(
            "infrastructure_blocked",
            {
                "profile_id": self.profile_id,
                "reason": "historical_runtime_components_unavailable",
                "dependency_id": self.runtime_audit.dependency_id,
                "runtime_root": self.runtime_audit.runtime_root,
                "missing_components": list(self.runtime_audit.missing_components),
                "mismatched_components": [
                    dict(value) for value in self.runtime_audit.mismatched_components
                ],
            },
        )


def materialize_jupyterlab_3703_fixed_capsule(
    *,
    artifact_root: str | Path,
    candidate: CandidateKey,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> StableArtifact:
    """Materialize the fixed tree from the retained mirror without executing it."""

    _require_candidate(candidate)
    if confirmation != MATERIALIZATION_CONFIRMATION:
        raise RuntimeError(
            f"explicit confirmation must be {MATERIALIZATION_CONFIRMATION!r}"
        )
    if type(authorization) is not AuthorizationRecord:
        raise ValueError("JupyterLab 3703 authorization is invalid")
    if authorization.scope != "protocol-migration":
        raise PermissionError("JupyterLab 3703 authorization scope is invalid")
    root = _ordinary_root(Path(artifact_root).absolute())
    source, patch, atomicity, manifest = _load_typed_chain(root)
    source_manifest = _load_source_manifest(root, source)
    _validate_authoritative_issue(root, source_manifest)
    raw_patch = _load_raw_patch(root, source_manifest, patch)
    _production_only_patch(raw_patch)

    mirror = _ordinary_directory(root, _MIRROR, create=False)
    merged_revision = read_legacy_merged_revision(
        root,
        raw_manifest_path=source.payload["raw_evidence_path"],
        candidate=candidate,
    )
    archive = _read_archive(mirror, merged_revision)
    files = _validated_archive_files(archive)
    required = {
        "package.json",
        "tests/package.json",
        "tests/karma.conf.js",
        "tests/webpack.config.js",
        PROFILE.package_metadata_path,
        "tests/test-apputils/karma.conf.js",
        *PROFILE.production_paths,
        *PROFILE.patch_test_paths,
    }
    if not required.issubset(files):
        raise RuntimeError("captured JupyterLab 3703 fixed tree is incomplete")
    _validate_fixed_archive(archive)

    relative = PurePosixPath(PROFILE.fixed_source_tree_path)
    parent = _ordinary_directory(root, PurePosixPath(*relative.parts[:-1]), create=True)
    destination = parent / relative.name
    created_tree = False
    if destination.exists() or destination.is_symlink():
        if not _tree_matches_archive(destination, archive):
            raise RuntimeError("JupyterLab 3703 fixed source destination conflicts")
    else:
        _materialize_archive(parent, destination, archive)
        created_tree = True

    payload = {
        "candidate": candidate.to_value(),
        "profile_id": PROFILE.profile_id,
        "source_origin": "captured_merged_revision",
        "source_tree_path": PROFILE.fixed_source_tree_path,
        "production_paths": list(PROFILE.production_paths),
        "test_paths": list(PROFILE.patch_test_paths),
        "authoritative_issue_number": PROFILE.primary_issue_number,
        "single_defect_contract": "restart_cancellation_stops_follow_up_action",
    }
    created_at = _created_after(
        source.created_at,
        patch.created_at,
        atomicity.created_at,
        manifest.created_at,
        authorization.granted_at,
    )
    try:
        write_stable_artifact(
            root,
            artifact_id=PROFILE.fixed_capsule_reference.artifact_id,
            artifact_kind=PROFILE.fixed_capsule_reference.artifact_kind,
            semantic_version=PROFILE.fixed_capsule_reference.semantic_version,
            created_at=created_at,
            inputs=(source.reference, patch.reference, atomicity.reference, manifest.reference),
            payload=payload,
            authorization=authorization,
        )
        artifact = _load(root, PROFILE.fixed_capsule_reference)
    except BaseException:
        if created_tree:
            _remove_tree(destination)
        raise
    if artifact.payload != payload or artifact.inputs != (
        source.reference,
        patch.reference,
        atomicity.reference,
        manifest.reference,
    ):
        if created_tree:
            _remove_tree(destination)
        raise RuntimeError("JupyterLab 3703 fixed capsule typed reload is invalid")
    return artifact


def describe_jupyterlab_3703_preparation(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
) -> JupyterLab3703Preparation:
    """Describe the strict reverse-patch comparison without executing it."""

    _require_candidate(candidate)
    root = _ordinary_root(Path(artifact_root).absolute())
    source, patch, atomicity, manifest = _load_typed_chain(root)
    source_manifest = _load_source_manifest(root, source)
    _validate_authoritative_issue(root, source_manifest)
    raw_patch = _load_raw_patch(root, source_manifest, patch)
    production_patch = _production_only_patch(raw_patch)
    capsule = _load(root, PROFILE.fixed_capsule_reference)
    if (
        capsule.inputs
        != (source.reference, patch.reference, atomicity.reference, manifest.reference)
        or capsule.payload.get("candidate") != candidate.to_value()
        or capsule.payload.get("source_tree_path") != PROFILE.fixed_source_tree_path
        or capsule.payload.get("production_paths") != list(PROFILE.production_paths)
        or capsule.payload.get("test_paths") != list(PROFILE.patch_test_paths)
        or capsule.payload.get("authoritative_issue_number") != _PRIMARY_ISSUE
    ):
        raise RuntimeError("JupyterLab 3703 fixed capsule binding is invalid")
    fixed_tree = _ordinary_directory(
        root, PurePosixPath(PROFILE.fixed_source_tree_path), create=False
    )
    test_text = _regular_file(fixed_tree, PurePosixPath(_TEST_PATH)).read_text(
        encoding="utf-8"
    )
    if not all(marker in test_text for marker in (_F2P_MARKER, _FIXED_ASSERTION, _P2P_MARKER)):
        raise RuntimeError("JupyterLab 3703 fixed regression test is incomplete")

    runtime_path = root.joinpath(*_RUNTIME_ROOT.parts)
    runtime_source = runtime_path if runtime_path.is_dir() and not runtime_path.is_symlink() else None
    runtime_audit = audit_runtime_source(PROFILE, runtime_source)
    runtime_bin = runtime_path / "bin"
    return JupyterLab3703Preparation(
        candidate=candidate,
        profile_id=PROFILE.profile_id,
        fixed_source_tree=fixed_tree,
        production_reverse_patch=production_patch,
        preserved_test_paths=PROFILE.patch_test_paths,
        buggy_apply_argv=("git", "apply", "-R", "--whitespace=nowarn", "-"),
        build_working_directory=".",
        build_argv=(
            str(runtime_bin / "jlpm"),
            "--cwd",
            "tests/test-apputils",
            "run",
            "build",
        ),
        test_working_directory="tests/test-apputils",
        test_argv=(
            str(runtime_bin / "node"),
            str(runtime_path / "node_modules/karma/bin/karma"),
            "start",
            "karma.conf.js",
            "--browsers=Firefox",
            "--single-run",
        ),
        test_environment={
            "HOME": str(runtime_path / "runtime-home"),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": str(runtime_bin),
            "KARMA_FILE_PATTERN": "build/clientsession.spec.js",
        },
        f2p=JupyterLab3703RegressionRun(
            phase="f2p",
            selector=_F2P_SELECTOR,
            repetitions=3,
            fixed_expected_outcome="pass",
            buggy_expected_outcome="failure",
        ),
        p2p=JupyterLab3703RegressionRun(
            phase="p2p",
            selector=_P2P_SELECTOR,
            repetitions=3,
            fixed_expected_outcome="pass",
            buggy_expected_outcome="pass",
        ),
        runtime_audit=runtime_audit,
    )


def write_jupyterlab_3703_runtime_preparation(
    *,
    artifact_root: str | Path,
    preparation: JupyterLab3703Preparation,
    authorization: AuthorizationRecord,
    created_at: str,
) -> StableArtifact:
    """Persist and typed-reload the exact local runtime readiness receipt."""

    if preparation.candidate != _CANDIDATE or preparation.profile_id != PROFILE.profile_id:
        raise ValueError("JupyterLab 3703 preparation identity is invalid")
    return write_runtime_preparation(
        artifact_root=artifact_root,
        profile=PROFILE,  # type: ignore[arg-type]
        audit=preparation.runtime_audit,
        authorization=authorization,
        created_at=created_at,
    )


def _require_candidate(candidate: CandidateKey) -> None:
    if candidate != _CANDIDATE:
        raise ValueError("only registered candidate JupyterLab 3703 has this profile")


def _load_typed_chain(
    root: Path,
) -> tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact]:
    source = _load(root, PROFILE.source_packet_reference)
    patch = _load(root, PROFILE.patch_reference)
    atomicity = _load(root, PROFILE.atomicity_reference)
    manifest = _load(root, PROFILE.test_manifest_reference)
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
    if (
        source.inputs != (enumeration,)
        or source.payload.get("candidate") != _CANDIDATE.to_value()
        or source.payload.get("complete") is not True
        or patch.inputs != (enumeration, source.reference)
        or patch.payload.get("candidate") != _CANDIDATE.to_value()
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("production_paths") != list(_PRODUCTION_PATHS)
        or patch.payload.get("test_paths") != list(_TEST_PATHS)
        or patch.payload.get("source_manifest_path")
        != source.payload.get("raw_evidence_path")
        or atomicity.inputs
        != (enumeration, source.reference, patch.reference, checkpoint)
        or atomicity.payload.get("candidate") != _CANDIDATE.to_value()
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("authoritative_issue_numbers") != [_PRIMARY_ISSUE]
        or atomicity.payload.get("primary_issue_number") != _PRIMARY_ISSUE
        or atomicity.payload.get("source_manifest_path")
        != source.payload.get("raw_evidence_path")
        or manifest.inputs != (patch.reference, atomicity.reference)
        or manifest.payload.get("candidate") != _CANDIDATE.to_value()
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("details")
        != {"executable_test_paths": [_TEST_PATH], "test_count": 1}
    ):
        raise RuntimeError("JupyterLab 3703 typed prerequisite chain is invalid")
    return source, patch, atomicity, manifest


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _load_source_manifest(root: Path, source: StableArtifact) -> dict[str, object]:
    relative = source.payload.get("raw_evidence_path")
    path = _regular_file(root, _safe_relative(relative, "source manifest"))
    value = _read_json(path, "source manifest")
    if (
        value.get("candidate") != _CANDIDATE.to_value()
        or value.get("complete") is not True
        or type(value.get("raw_paths")) is not list
        or type(value.get("patch_raw_path")) is not str
    ):
        raise RuntimeError("JupyterLab 3703 raw source manifest is invalid")
    return value


def _validate_authoritative_issue(root: Path, source_manifest: dict[str, object]) -> None:
    raw_paths = source_manifest["raw_paths"]
    assert isinstance(raw_paths, list)
    suffix = f"/issue-{_PRIMARY_ISSUE}/attempt-1/"
    paths = [value for value in raw_paths if type(value) is str and suffix in value]
    if len(paths) != 1:
        raise RuntimeError("JupyterLab 3703 authoritative issue capture is invalid")
    issue = _read_json(
        _regular_file(root, _safe_relative(paths[0], "authoritative issue")),
        "authoritative issue",
    )
    title = issue.get("title")
    body = issue.get("body")
    if (
        issue.get("number") != _PRIMARY_ISSUE
        or type(title) is not str
        or type(body) is not str
        or "Cancel" not in title
        or "Restart Kernel and Run All Cells" not in title
        or "cancel the whole request" not in body
        or "do neither Restart Kernel or Run All Cells" not in body
    ):
        raise RuntimeError("JupyterLab 3703 single-defect authority is invalid")


def _load_raw_patch(
    root: Path,
    source_manifest: dict[str, object],
    patch: StableArtifact,
) -> bytes:
    relative = source_manifest.get("patch_raw_path")
    if relative != patch.payload.get("raw_patch_path"):
        raise RuntimeError("JupyterLab 3703 raw patch binding is invalid")
    return _regular_file(root, _safe_relative(relative, "raw patch")).read_bytes()


def _production_only_patch(content: bytes) -> bytes:
    try:
        text = content.decode("utf-8", errors="surrogateescape")
    except UnicodeError as error:
        raise RuntimeError("JupyterLab 3703 raw patch is invalid") from error
    sections: list[tuple[str, list[str]]] = []
    path: str | None = None
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if path is not None:
                sections.append((path, lines))
            parts = line.rstrip("\r\n").split(" ")
            if (
                len(parts) != 4
                or not parts[2].startswith("a/")
                or not parts[3].startswith("b/")
                or parts[2][2:] != parts[3][2:]
            ):
                raise RuntimeError("JupyterLab 3703 raw patch header is invalid")
            path = parts[3][2:]
            lines = [line]
        elif path is not None:
            lines.append(line)
    if path is not None:
        sections.append((path, lines))
    paths = tuple(value for value, _lines in sections)
    if paths != (*_PRODUCTION_PATHS, *_TEST_PATHS) or len(set(paths)) != len(paths):
        raise RuntimeError("JupyterLab 3703 patch is outside the registered profile")
    test_section = "".join(
        line for value, section in sections if value == _TEST_PATH for line in section
    )
    if (
        _FIXED_ASSERTION not in test_section
        or "expect(restarted).to.be(true);" not in test_section
    ):
        raise RuntimeError("JupyterLab 3703 fixed test hunk is incomplete")
    return "".join(
        line
        for value, section in sections
        if value in _PRODUCTION_PATHS
        for line in section
    ).encode("utf-8", errors="surrogateescape")


def _read_archive(mirror: Path, revision: str) -> bytes:
    try:
        completed = subprocess.run(
            ("git", f"--git-dir={mirror}", "archive", "--format=tar", revision),
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise RuntimeError("retained JupyterLab mirror is unavailable") from error
    if completed.returncode != 0 or not completed.stdout:
        raise RuntimeError("captured JupyterLab 3703 merged revision is unavailable")
    return completed.stdout


def _validated_archive_files(content: bytes) -> frozenset[str]:
    files: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or "." in path.parts
                    or ".." in path.parts
                    or path.as_posix().rstrip("/") != member.name.rstrip("/")
                ):
                    raise RuntimeError("captured JupyterLab 3703 archive path is unsafe")
                if member.isdir():
                    continue
                if not member.isfile() or member.name in files:
                    raise RuntimeError("captured JupyterLab 3703 archive entry is unsafe")
                files.add(member.name)
    except (OSError, tarfile.TarError) as error:
        raise RuntimeError("captured JupyterLab 3703 archive is invalid") from error
    if not files:
        raise RuntimeError("captured JupyterLab 3703 archive is empty")
    return frozenset(files)


def _validate_fixed_archive(content: bytes) -> None:
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            package = _archive_json(archive, PROFILE.package_metadata_path)
            tests_package = _archive_json(archive, "tests/package.json")
            test = _archive_text(archive, _TEST_PATH)
            travis = _archive_text(archive, ".travis.yml")
            yarn = _archive_text(archive, "jupyterlab/staging/yarn.js")
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError, tarfile.TarError) as error:
        raise RuntimeError("captured JupyterLab 3703 metadata is invalid") from error
    dependencies = tests_package.get("devDependencies")
    if (
        package.get("version") != PROFILE.package_version
        or type(dependencies) is not dict
        or dependencies.get("karma") != "~1.7.1"
        or dependencies.get("karma-mocha") != "~0.2.2"
        or dependencies.get("mocha") != "~3.5.3"
        or dependencies.get("typescript") != "~2.6.2"
        or dependencies.get("webpack") != "~2.7.0"
        or "node_js:\n- '5'" not in travis
        or '"version": "1.3.2"' not in yarn
        or not all(marker in test for marker in (_F2P_MARKER, _FIXED_ASSERTION, _P2P_MARKER))
    ):
        raise RuntimeError("captured JupyterLab 3703 metadata is invalid")


def _archive_json(archive: tarfile.TarFile, name: str) -> dict[str, object]:
    return json.loads(_archive_text(archive, name), object_pairs_hook=_pairs)


def _archive_text(archive: tarfile.TarFile, name: str) -> str:
    handle = archive.extractfile(archive.getmember(name))
    if handle is None:
        raise KeyError(name)
    return handle.read().decode("utf-8")


def _materialize_archive(parent: Path, destination: Path, content: bytes) -> None:
    with tempfile.TemporaryDirectory(prefix=".jupyterlab-3703-fixed-", dir=parent) as name:
        staging = Path(name)
        try:
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
                for member in archive:
                    if member.isdir():
                        continue
                    source = archive.extractfile(member)
                    if source is None:
                        raise RuntimeError("captured JupyterLab 3703 archive is invalid")
                    target = staging.joinpath(*PurePosixPath(member.name).parts)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    descriptor = os.open(
                        target,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o700 if member.mode & 0o111 else 0o600,
                    )
                    with os.fdopen(descriptor, "wb") as output:
                        while block := source.read(1024 * 1024):
                            output.write(block)
                        output.flush()
                        os.fsync(output.fileno())
            if destination.exists() or destination.is_symlink():
                raise RuntimeError("JupyterLab 3703 fixed source already exists")
            os.replace(staging, destination)
        except BaseException:
            _remove_tree(staging)
            raise


def _tree_matches_archive(destination: Path, content: bytes) -> bool:
    try:
        info = destination.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return False
        actual: dict[str, Path] = {}
        for path in destination.rglob("*"):
            entry = path.lstat()
            if stat.S_ISLNK(entry.st_mode) or not (
                stat.S_ISDIR(entry.st_mode) or stat.S_ISREG(entry.st_mode)
            ):
                return False
            if stat.S_ISREG(entry.st_mode):
                actual[path.relative_to(destination).as_posix()] = path
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            expected = {member.name: member for member in archive if member.isfile()}
            if set(actual) != set(expected):
                return False
            for name, member in expected.items():
                source = archive.extractfile(member)
                if source is None or actual[name].read_bytes() != source.read():
                    return False
                if bool(actual[name].stat().st_mode & 0o111) != bool(member.mode & 0o111):
                    return False
    except (OSError, tarfile.TarError):
        return False
    return True


def _ordinary_root(root: Path) -> Path:
    try:
        info = root.lstat()
        resolved = root.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("artifact root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return resolved


def _ordinary_directory(root: Path, relative: PurePosixPath, *, create: bool) -> Path:
    current = root
    for part in relative.parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create:
                raise RuntimeError("registered JupyterLab 3703 directory is unavailable")
            current.mkdir(mode=0o700)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("registered JupyterLab 3703 directory is unsafe")
    return current.resolve(strict=True)


def _regular_file(root: Path, relative: PurePosixPath) -> Path:
    path = root.joinpath(*relative.parts)
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("registered JupyterLab 3703 file is missing") from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or not resolved.is_relative_to(root.resolve(strict=True))
    ):
        raise RuntimeError("registered JupyterLab 3703 file is unsafe")
    return resolved


def _safe_relative(value: object, label: str) -> PurePosixPath:
    if type(value) is not str or not value:
        raise RuntimeError(f"JupyterLab 3703 {label} path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise RuntimeError(f"JupyterLab 3703 {label} path is unsafe")
    return path


def _read_json(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeError(f"JupyterLab 3703 {label} is invalid") from error
    if type(value) is not dict:
        raise RuntimeError(f"JupyterLab 3703 {label} is invalid")
    return value


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _remove_tree(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            path.rmdir()
    root.rmdir()


def _created_after(*values: str) -> str:
    latest = max(
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00") for value in values
    )
    return (latest + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


__all__ = [
    "JUPYTERLAB_3703_PROFILE_ID",
    "MATERIALIZATION_CONFIRMATION",
    "JupyterLab3703Preparation",
    "JupyterLab3703Profile",
    "JupyterLab3703RegressionRun",
    "PROFILE",
    "describe_jupyterlab_3703_preparation",
    "materialize_jupyterlab_3703_fixed_capsule",
    "write_jupyterlab_3703_runtime_preparation",
]
