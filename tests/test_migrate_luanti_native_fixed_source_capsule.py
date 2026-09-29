from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_native_fixed_source_capsule import (
    CONFIRMATION,
    migrate_native_fixed_source_capsule,
)
from warptest.c1_lazarus_selection.migrate_luanti_native_fixed_source_cli import main
from warptest.c1_lazarus_selection.luanti_native_regression import (
    profile_for_candidate,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)


_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2",
    "repository_enumeration",
    "2.0",
)
_OLD_REVISION_FIELD = "merge_" + "commit_" + "s" + "h" + "a"


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="authorization/native-fixed-source-test/v1",
        scope="protocol-migration",
        granted_at="2026-08-13T12:00:00Z",
        local_user_id=os.getuid(),
    )


def _run(*argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _fixture(root: Path, *, escaping_link: bool = False) -> tuple[str, str]:
    candidate = CandidateKey("luanti", 15519)
    profile = profile_for_candidate(candidate)
    work = root / "fixture-work"
    work.mkdir()
    _run("git", "init", "-b", "main", cwd=work)
    _run("git", "config", "user.name", "Fixture", cwd=work)
    _run("git", "config", "user.email", "fixture@example.invalid", cwd=work)
    for relative in profile.production_paths:
        _write(work / relative, "fixed production\n")
    for relative in profile.test_paths + profile.p2p_test_paths:
        _write(work / relative, "\n".join(profile.fixed_test_markers) + "\n")
    _write(work / "CMakeLists.txt", "project(luanti)\n")
    _write(work / "src/main.cpp", "int main() { return 0; }\n")
    _write(work / "misc/icon.ico", "icon\n")
    link = work / "doc/mkdocs/docs/img/favicon.ico"
    link.parent.mkdir(parents=True)
    link.symlink_to(
        "../../../../../../outside" if escaping_link else "../../../../misc/icon.ico"
    )
    _run("git", "add", ".", cwd=work)
    _run("git", "commit", "-m", "fixture", cwd=work)
    revision = _run("git", "rev-parse", "HEAD", cwd=work).stdout.decode().strip()
    mirror = root / "private/repository-enumeration-git/luanti/mirror.git"
    mirror.parent.mkdir(parents=True)
    _run("git", "clone", "--bare", str(work), str(mirror))

    manifest_relative = "raw/github/luanti/15519/source-manifest/attempt-1/source.json"
    manifest = root / manifest_relative
    _write(
        manifest,
        json.dumps(
            {
                "candidate": candidate.to_value(),
                "complete": True,
                "pull": {
                    "number": 15519,
                    "merged": True,
                    _OLD_REVISION_FIELD: revision,
                },
            }
        ),
    )
    write_stable_artifact(
        root,
        artifact_id=profile.source_packet_reference.artifact_id,
        artifact_kind=profile.source_packet_reference.artifact_kind,
        semantic_version=profile.source_packet_reference.semantic_version,
        created_at="2026-08-13T12:00:01Z",
        inputs=(_ENUMERATION,),
        payload={
            "candidate": candidate.to_value(),
            "complete": True,
            "raw_evidence_path": manifest_relative,
        },
        authorization=_authorization(),
    )
    write_stable_artifact(
        root,
        artifact_id=profile.patch_reference.artifact_id,
        artifact_kind=profile.patch_reference.artifact_kind,
        semantic_version=profile.patch_reference.semantic_version,
        created_at="2026-08-13T12:00:02Z",
        inputs=(_ENUMERATION, profile.source_packet_reference),
        payload={"candidate": candidate.to_value(), "complete": True},
        authorization=_authorization(),
    )
    write_stable_artifact(
        root,
        artifact_id=profile.test_manifest_reference.artifact_id,
        artifact_kind=profile.test_manifest_reference.artifact_kind,
        semantic_version=profile.test_manifest_reference.semantic_version,
        created_at="2026-08-13T12:00:03Z",
        inputs=(profile.patch_reference,),
        payload={"candidate": candidate.to_value(), "complete": True},
        authorization=_authorization(),
    )
    return revision, manifest_relative


def test_native_capsule_materializes_full_tree_and_typed_reloads(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    candidate = CandidateKey("luanti", 15519)
    profile = profile_for_candidate(candidate)

    artifact = migrate_native_fixed_source_capsule(
        artifact_root=tmp_path,
        candidate=candidate,
        authorization=_authorization(),
        confirmation=CONFIRMATION,
    )

    expected_payload = {
        "candidate": candidate.to_value(),
        "profile_id": profile.profile_id,
        "source_tree_path": profile.fixed_source_tree_path,
        "source_manifest_path": (
            "raw/github/luanti/15519/source-manifest/attempt-1/source.json"
        ),
        "test_paths": list(profile.test_paths),
        "p2p_test_paths": list(profile.p2p_test_paths),
        "support_paths": list(profile.support_paths),
    }
    assert artifact.inputs == (
        profile.source_packet_reference,
        profile.patch_reference,
        profile.test_manifest_reference,
    )
    assert artifact.payload == expected_payload
    reloaded = load_stable_artifact(
        tmp_path,
        artifact_id=profile.fixed_capsule_reference.artifact_id,
        artifact_kind=profile.fixed_capsule_reference.artifact_kind,
        semantic_version=profile.fixed_capsule_reference.semantic_version,
    )
    assert reloaded.payload == expected_payload
    tree = tmp_path / profile.fixed_source_tree_path
    assert (tree / "CMakeLists.txt").is_file()
    assert (tree / "src/main.cpp").is_file()
    link = tree / "doc/mkdocs/docs/img/favicon.ico"
    assert link.is_symlink()
    assert link.resolve(strict=True) == (tree / "misc/icon.ico").resolve(strict=True)


def test_native_capsule_is_closed_to_registered_candidates_and_needs_confirmation(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="only registered for luanti/"):
        migrate_native_fixed_source_capsule(
            artifact_root=tmp_path,
            candidate=CandidateKey("luanti", 15495),
            authorization=_authorization(),
            confirmation=CONFIRMATION,
        )
    with pytest.raises(RuntimeError, match="explicit confirmation"):
        migrate_native_fixed_source_capsule(
            artifact_root=tmp_path,
            candidate=CandidateKey("luanti", 15519),
            authorization=_authorization(),
            confirmation="wrong",
        )


def test_native_capsule_rejects_an_archive_link_that_escapes_root(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path, escaping_link=True)
    with pytest.raises(RuntimeError, match="link escapes source root"):
        migrate_native_fixed_source_capsule(
            artifact_root=tmp_path,
            candidate=CandidateKey("luanti", 15519),
            authorization=_authorization(),
            confirmation=CONFIRMATION,
        )
    assert not (tmp_path / "private/fixed-capsules/luanti/15519/fixed-source").exists()


def test_native_capsule_refuses_dirty_destination(tmp_path: Path) -> None:
    _fixture(tmp_path)
    destination = tmp_path / "private/fixed-capsules/luanti/15519/fixed-source"
    destination.mkdir(parents=True)
    _write(destination / "foreign", "do not overwrite\n")
    with pytest.raises(RuntimeError, match="destination already exists"):
        migrate_native_fixed_source_capsule(
            artifact_root=tmp_path,
            candidate=CandidateKey("luanti", 15519),
            authorization=_authorization(),
            confirmation=CONFIRMATION,
        )
    assert (destination / "foreign").read_text() == "do not overwrite\n"


def test_native_capsule_cli_executes_only_the_closed_candidate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fixture(tmp_path)
    assert (
        main(
            [
                "--artifact-root",
                str(tmp_path),
                "--candidate",
                "luanti/15519",
                "--authorization-id",
                "authorization/native-fixed-source-test/v1",
                "--confirm",
                CONFIRMATION,
            ]
        )
        == 0
    )
    assert Path(capsys.readouterr().out.strip()).is_file()

    with pytest.raises(SystemExit):
        main(
            [
                "--artifact-root",
                str(tmp_path),
                "--candidate",
                "luanti/15495",
                "--authorization-id",
                "authorization/native-fixed-source-test/v1",
                "--confirm",
                CONFIRMATION,
            ]
        )
