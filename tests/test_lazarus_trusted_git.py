from __future__ import annotations

import os
import subprocess
from pathlib import Path

from warptest.luanti_runtime import luanti_client_source_identity


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _source_tree(tmp_path: Path) -> Path:
    repo = tmp_path / "luanti"
    (repo / "src/client").mkdir(parents=True)
    (repo / "irr/src").mkdir(parents=True)
    (repo / "CMakeLists.txt").write_text("project(luanti)\n", encoding="utf-8")
    (repo / "src/client/game.cpp").write_text("int main() {}\n", encoding="utf-8")
    (repo / "irr/src/CIrrDeviceSDL.cpp").write_text("// input\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.name", "Lazarus Test")
    _git(repo, "config", "user.email", "lazarus@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "source")
    return repo


def test_luanti_client_identity_uses_trusted_git_without_exposing_dot_git(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _source_tree(tmp_path)
    before = luanti_client_source_identity(repo)
    trusted = tmp_path / "trusted.git"
    os.replace(repo / ".git", trusted)

    assert luanti_client_source_identity(repo) != before
    monkeypatch.setenv("WARPTEST_LUANTI_TRUSTED_GIT_DIR", str(trusted))
    assert luanti_client_source_identity(repo) == before

    (repo / "src/client/game.cpp").write_text("int main() { return 1; }\n", encoding="utf-8")
    assert luanti_client_source_identity(repo) != before
