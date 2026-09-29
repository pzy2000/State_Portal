from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from warptest.lazarus_versions import (
    LazarusVersionError,
    LazarusVersionRequest,
    materialize_lazarus_versions,
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _fixed_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "lazarus@example.invalid")
    _git(repo, "config", "user.name", "Lazarus Test")
    (repo / "behavior.txt").write_text("historical-bug\n", encoding="utf-8")
    (repo / "invariant.txt").write_text("stable\n", encoding="utf-8")
    _git(repo, "add", "behavior.txt", "invariant.txt")
    _git(repo, "commit", "-qm", "buggy parent")
    (repo / "behavior.txt").write_text("fixed-behavior\n", encoding="utf-8")
    _git(repo, "add", "behavior.txt")
    _git(repo, "commit", "-qm", "true upstream fix")
    revision = _git(repo, "rev-parse", "HEAD")
    patch = tmp_path / "revert.patch"
    patch.write_bytes(
        subprocess.run(
            ["git", "-C", str(repo), "show", "--format=", "--binary", "HEAD"],
            check=True,
            capture_output=True,
        ).stdout
    )
    return repo, patch, revision


def _request(repo: Path, patch: Path, revision: str) -> LazarusVersionRequest:
    return LazarusVersionRequest(
        task_id="lz-test-real-fix",
        source_repo=repo,
        fixed_revision=revision,
        revert_patch=patch,
        patch_sha256=hashlib.sha256(patch.read_bytes()).hexdigest(),
        declared_files=("behavior.txt",),
        build_class="none",
    )


def test_materializer_builds_exact_buggy_fixed_pair_and_hides_git(tmp_path: Path) -> None:
    repo, patch, revision = _fixed_repo(tmp_path)
    cache = tmp_path / "cache"

    pair = materialize_lazarus_versions(
        _request(repo, patch, revision), cache_root=cache
    )

    assert (pair.fixed.root / "behavior.txt").read_text() == "fixed-behavior\n"
    assert (pair.buggy.root / "behavior.txt").read_text() == "historical-bug\n"
    assert (pair.fixed.root / "invariant.txt").read_text() == "stable\n"
    assert (pair.buggy.root / "invariant.txt").read_text() == "stable\n"
    assert pair.fixed.changed_files == ()
    assert pair.buggy.changed_files == ("behavior.txt",)
    assert pair.fixed.tree_sha256 != pair.buggy.tree_sha256
    assert pair.fixed.cache_hit is False
    assert pair.buggy.cache_hit is False
    for artifact in (pair.fixed, pair.buggy):
        assert not (artifact.root / ".git").exists()
        assert artifact.git_metadata.exists()
        assert artifact.root not in artifact.git_metadata.parents
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "rev-parse", "HEAD") == revision


def test_materializer_reuses_only_fingerprint_verified_prebuilt_pair(tmp_path: Path) -> None:
    repo, patch, revision = _fixed_repo(tmp_path)
    request = _request(repo, patch, revision)
    cache = tmp_path / "cache"
    first = materialize_lazarus_versions(request, cache_root=cache)

    second = materialize_lazarus_versions(request, cache_root=cache)

    assert second.fixed.root == first.fixed.root
    assert second.buggy.root == first.buggy.root
    assert second.fixed.cache_hit is True
    assert second.buggy.cache_hit is True
    (second.buggy.root / "behavior.txt").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(LazarusVersionError, match="tree fingerprint"):
        materialize_lazarus_versions(request, cache_root=cache)


def test_materializer_rejects_cached_pair_when_prebuild_inputs_change(
    tmp_path: Path,
) -> None:
    repo, patch, revision = _fixed_repo(tmp_path)
    request = replace(_request(repo, patch, revision), build_class="incremental_dotnet")
    cache = tmp_path / "cache"

    materialize_lazarus_versions(
        request, cache_root=cache, build=_build_callback("overlay-v1")
    )

    with pytest.raises(LazarusVersionError, match="build fingerprint"):
        materialize_lazarus_versions(
            request, cache_root=cache, build=_build_callback("overlay-v2")
        )


def test_materializer_accepts_incremental_cpp_prebuilds(tmp_path: Path) -> None:
    repo, patch, revision = _fixed_repo(tmp_path)
    request = replace(_request(repo, patch, revision), build_class="incremental_cpp")
    calls: list[str] = []

    def build(version: str, root: Path) -> dict[str, str]:
        calls.append(version)
        return {"version": version, "root": str(root)}

    build.lazarus_build_fingerprint = "a" * 64

    pair = materialize_lazarus_versions(
        request,
        cache_root=tmp_path / "cache",
        build=build,
    )

    assert calls == ["fixed", "buggy"]
    assert pair.fixed.build_identity["version"] == "fixed"
    assert pair.buggy.build_identity["version"] == "buggy"


def test_materializer_rejects_patch_sha_and_non_exact_reverse_application(
    tmp_path: Path,
) -> None:
    repo, patch, revision = _fixed_repo(tmp_path)
    request = _request(repo, patch, revision)
    wrong_sha = LazarusVersionRequest(
        **{**request.__dict__, "patch_sha256": "0" * 64}
    )
    with pytest.raises(LazarusVersionError, match="patch SHA"):
        materialize_lazarus_versions(wrong_sha, cache_root=tmp_path / "wrong-sha")

    (repo / "behavior.txt").write_text("newer-fixed-context\n", encoding="utf-8")
    _git(repo, "add", "behavior.txt")
    _git(repo, "commit", "-qm", "post-fix context changed")
    newer = LazarusVersionRequest(
        **{**request.__dict__, "fixed_revision": _git(repo, "rev-parse", "HEAD")}
    )
    with pytest.raises(LazarusVersionError, match="git apply --reverse --check"):
        materialize_lazarus_versions(newer, cache_root=tmp_path / "context-mismatch")


def _build_callback(tag: str):
    def build(version: str, root: Path) -> dict[str, str]:
        return {"version": version, "tag": tag, "root": str(root)}

    build.lazarus_build_fingerprint = hashlib.sha256(tag.encode("utf-8")).hexdigest()
    return build
