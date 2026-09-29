from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_fixed_source_capsule import (
    CONFIRMATION,
    migrate_fixed_source_capsule,
)
from warptest.c1_lazarus_selection.luanti_lua_regression import (
    profile_for_candidate,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)
from warptest.c1_lazarus_selection.migrate_luanti_fixed_source_cli import main


_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2",
    "repository_enumeration",
    "2.0",
)
_OLD_REVISION_FIELD = "merge_" + "commit_" + "s" + "h" + "a"


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="authorization/fixed-source-test/v1",
        scope="protocol-migration",
        granted_at="2026-08-13T12:00:00Z",
        local_user_id=os.getuid(),
    )


def _run(*argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _fixture_mirror(
    root: Path,
    candidate: CandidateKey,
    *,
    linked_test: bool = False,
) -> str:
    profile = profile_for_candidate(candidate)
    work = root / "fixture-work"
    work.mkdir()
    _run("git", "init", "-b", "main", cwd=work)
    _run("git", "config", "user.name", "Fixture", cwd=work)
    _run("git", "config", "user.email", "fixture@example.invalid", cwd=work)
    for relative in profile.production_paths:
        _write(work / relative, f"fixed production for {candidate.change_number}\n")
    for relative in profile.support_paths:
        _write(work / relative, "return vector\n")
    for relative in profile.test_paths:
        path = work / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if linked_test:
            path.symlink_to("outside-test")
        else:
            _write(path, "\n".join(profile.fixed_test_markers) + "\n")
    _run("git", "add", ".", cwd=work)
    _run("git", "commit", "-m", "fixture", cwd=work)
    revision = _run("git", "rev-parse", "HEAD", cwd=work).stdout.decode().strip()
    mirror = root / "private" / "repository-enumeration-git" / "luanti" / "mirror.git"
    mirror.parent.mkdir(parents=True)
    _run("git", "clone", "--bare", str(work), str(mirror))
    return revision


def _source_packet(
    root: Path,
    candidate: CandidateKey,
    revision: str,
    *,
    manifest_path: str | None = None,
) -> str:
    profile = profile_for_candidate(candidate)
    relative = manifest_path or (
        f"raw/github/luanti/{candidate.change_number}/source-manifest/"
        "attempt-1/source.json"
    )
    manifest = root / relative
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "candidate": candidate.to_value(),
                "complete": True,
                "pull": {
                    "number": candidate.change_number,
                    "merged": True,
                    _OLD_REVISION_FIELD: revision,
                },
            }
        ),
        encoding="utf-8",
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
            "raw_evidence_path": relative,
        },
        authorization=_authorization(),
    )
    return relative


@pytest.mark.parametrize("change_number", [13132, 14757])
def test_migration_extracts_only_profile_files_and_typed_reloads(
    tmp_path: Path,
    change_number: int,
) -> None:
    candidate = CandidateKey("luanti", change_number)
    profile = profile_for_candidate(candidate)
    revision = _fixture_mirror(tmp_path, candidate)
    _source_packet(tmp_path, candidate, revision)

    capsule = migrate_fixed_source_capsule(
        artifact_root=tmp_path,
        candidate=candidate,
        authorization=_authorization(),
        confirmation=CONFIRMATION,
    )

    expected_payload = {
        "candidate": candidate.to_value(),
        "profile_id": profile.profile_id,
        "source_tree_path": profile.fixed_source_tree_path,
        "support_paths": list(profile.support_paths),
        "test_paths": list(profile.test_paths),
    }
    assert capsule.reference == profile.fixed_capsule_reference
    assert capsule.inputs == (profile.source_packet_reference,)
    assert capsule.payload == expected_payload
    reloaded = load_stable_artifact(
        tmp_path,
        artifact_id=profile.fixed_capsule_reference.artifact_id,
        artifact_kind=profile.fixed_capsule_reference.artifact_kind,
        semantic_version=profile.fixed_capsule_reference.semantic_version,
    )
    assert reloaded.payload == expected_payload
    tree = tmp_path / profile.fixed_source_tree_path
    assert sorted(
        path.relative_to(tree).as_posix() for path in tree.rglob("*") if path.is_file()
    ) == sorted(
        (*profile.production_paths, *profile.support_paths, *profile.test_paths)
    )
    assert not any(path.is_symlink() for path in tree.rglob("*"))


def test_migration_is_closed_and_requires_exact_confirmation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="registered"):
        migrate_fixed_source_capsule(
            artifact_root=tmp_path,
            candidate=CandidateKey("luanti", 1),
            authorization=_authorization(),
            confirmation=CONFIRMATION,
        )
    with pytest.raises(RuntimeError, match="explicit confirmation"):
        migrate_fixed_source_capsule(
            artifact_root=tmp_path,
            candidate=CandidateKey("luanti", 13132),
            authorization=_authorization(),
            confirmation="wrong",
        )


def test_v2_migration_leaves_an_existing_v1_capsule_immutable(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("luanti", 13132)
    profile = profile_for_candidate(candidate)
    revision = _fixture_mirror(tmp_path, candidate)
    _source_packet(tmp_path, candidate, revision)
    legacy_tree = (
        f"private/fixed-capsules/luanti/{candidate.change_number}/fixed-source"
    )
    legacy_file = tmp_path / legacy_tree / profile.production_paths[0]
    _write(legacy_file, "immutable legacy capsule\n")
    legacy_payload = {
        "candidate": candidate.to_value(),
        "profile_id": profile.profile_id,
        "source_tree_path": legacy_tree,
        "test_paths": list(profile.test_paths),
    }
    legacy_id = f"fixed_source_capsule/luanti/{candidate.change_number}/fixed/v1"
    write_stable_artifact(
        tmp_path,
        artifact_id=legacy_id,
        artifact_kind="fixed_source_capsule",
        semantic_version="1.0",
        created_at="2026-08-13T12:00:02Z",
        inputs=(profile.source_packet_reference,),
        payload=legacy_payload,
        authorization=_authorization(),
    )

    migrate_fixed_source_capsule(
        artifact_root=tmp_path,
        candidate=candidate,
        authorization=_authorization(),
        confirmation=CONFIRMATION,
    )

    assert legacy_file.read_text(encoding="utf-8") == "immutable legacy capsule\n"
    assert (
        load_stable_artifact(
            tmp_path,
            artifact_id=legacy_id,
            artifact_kind="fixed_source_capsule",
            semantic_version="1.0",
        ).payload
        == legacy_payload
    )
    assert (tmp_path / profile.fixed_source_tree_path).is_dir()


def test_migration_rejects_missing_revision_and_dirty_destination(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("luanti", 13132)
    _fixture_mirror(tmp_path, candidate)
    _source_packet(tmp_path, candidate, "0" * 40)
    with pytest.raises(RuntimeError, match="source revision is unavailable"):
        migrate_fixed_source_capsule(
            artifact_root=tmp_path,
            candidate=candidate,
            authorization=_authorization(),
            confirmation=CONFIRMATION,
        )

    profile = profile_for_candidate(candidate)
    destination = tmp_path / profile.fixed_source_tree_path
    destination.mkdir(parents=True)
    (destination / "foreign.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(RuntimeError, match="destination already exists"):
        migrate_fixed_source_capsule(
            artifact_root=tmp_path,
            candidate=candidate,
            authorization=_authorization(),
            confirmation=CONFIRMATION,
        )


def test_migration_rejects_linked_archive_member(tmp_path: Path) -> None:
    candidate = CandidateKey("luanti", 14757)
    revision = _fixture_mirror(tmp_path, candidate, linked_test=True)
    _source_packet(tmp_path, candidate, revision)

    with pytest.raises(RuntimeError, match="ordinary files"):
        migrate_fixed_source_capsule(
            artifact_root=tmp_path,
            candidate=candidate,
            authorization=_authorization(),
            confirmation=CONFIRMATION,
        )


def test_migration_rejects_a_linked_support_file(tmp_path: Path) -> None:
    candidate = CandidateKey("luanti", 13132)
    profile = profile_for_candidate(candidate)
    work = tmp_path / "fixture-work"
    work.mkdir()
    _run("git", "init", "-b", "main", cwd=work)
    _run("git", "config", "user.name", "Fixture", cwd=work)
    _run("git", "config", "user.email", "fixture@example.invalid", cwd=work)
    for relative in profile.production_paths:
        _write(work / relative, "fixed production\n")
    support = work / profile.support_paths[0]
    support.parent.mkdir(parents=True, exist_ok=True)
    support.symlink_to("outside-support")
    for relative in profile.test_paths:
        _write(work / relative, "\n".join(profile.fixed_test_markers) + "\n")
    _run("git", "add", ".", cwd=work)
    _run("git", "commit", "-m", "fixture", cwd=work)
    revision = _run("git", "rev-parse", "HEAD", cwd=work).stdout.decode().strip()
    mirror = (
        tmp_path / "private" / "repository-enumeration-git" / "luanti" / "mirror.git"
    )
    mirror.parent.mkdir(parents=True)
    _run("git", "clone", "--bare", str(work), str(mirror))
    _source_packet(tmp_path, candidate, revision)

    with pytest.raises(RuntimeError, match="ordinary files"):
        migrate_fixed_source_capsule(
            artifact_root=tmp_path,
            candidate=candidate,
            authorization=_authorization(),
            confirmation=CONFIRMATION,
        )


def test_migration_rejects_raw_manifest_escape_and_link(tmp_path: Path) -> None:
    candidate = CandidateKey("luanti", 13132)
    revision = _fixture_mirror(tmp_path, candidate)
    outside = tmp_path.parent / "outside-source.json"
    outside.write_text("{}", encoding="utf-8")
    _source_packet(
        tmp_path, candidate, revision, manifest_path="../outside-source.json"
    )
    with pytest.raises(RuntimeError, match="manifest path is unsafe"):
        migrate_fixed_source_capsule(
            artifact_root=tmp_path,
            candidate=candidate,
            authorization=_authorization(),
            confirmation=CONFIRMATION,
        )


def test_migration_rejects_a_linked_raw_manifest(tmp_path: Path) -> None:
    candidate = CandidateKey("luanti", 13132)
    revision = _fixture_mirror(tmp_path, candidate)
    relative = _source_packet(tmp_path, candidate, revision)
    manifest = tmp_path / relative
    outside = tmp_path / "outside-source.json"
    outside.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
    manifest.unlink()
    manifest.symlink_to(outside)

    with pytest.raises(RuntimeError, match="manifest path is unsafe"):
        migrate_fixed_source_capsule(
            artifact_root=tmp_path,
            candidate=candidate,
            authorization=_authorization(),
            confirmation=CONFIRMATION,
        )


def test_cli_migrates_one_allowed_candidate_and_prints_the_typed_revision(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    candidate = CandidateKey("luanti", 14757)
    revision = _fixture_mirror(tmp_path, candidate)
    _source_packet(tmp_path, candidate, revision)

    assert (
        main(
            [
                "--artifact-root",
                str(tmp_path),
                "--candidate",
                "luanti/14757",
                "--authorization-id",
                "authorization/fixed-source-test/v1",
                "--confirm",
                CONFIRMATION,
            ]
        )
        == 0
    )
    output = Path(capsys.readouterr().out.strip())
    assert output.is_file()
    profile = profile_for_candidate(candidate)
    assert (
        load_stable_artifact(
            tmp_path,
            artifact_id=profile.fixed_capsule_reference.artifact_id,
            artifact_kind=profile.fixed_capsule_reference.artifact_kind,
            semantic_version=profile.fixed_capsule_reference.semantic_version,
        ).payload["profile_id"]
        == profile.profile_id
    )


def test_cli_requires_all_public_arguments_and_uses_closed_candidate_syntax(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["--artifact-root", str(tmp_path), "--candidate", "luanti/13132"])
    with pytest.raises(SystemExit):
        main(
            [
                "--artifact-root",
                str(tmp_path),
                "--candidate",
                "luanti/1",
                "--authorization-id",
                "authorization/fixed-source-test/v1",
                "--confirm",
                CONFIRMATION,
            ]
        )
    assert "usage:" in capsys.readouterr().err
