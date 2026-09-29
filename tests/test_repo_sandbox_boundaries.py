from __future__ import annotations

import os
from pathlib import Path

import pytest

from warptest.repo_tools import RepoSandbox


def test_read_list_grep_and_map_reject_absolute_traversal_and_symlinks(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    external = tmp_path / "external"
    repo.mkdir()
    external.mkdir()
    (repo / "normal.ts").write_text(
        "export const PUBLIC_SENTINEL = true;\n", encoding="utf-8"
    )
    (external / "secret.ts").write_text(
        "export const PRIVATE_SENTINEL = true;\n", encoding="utf-8"
    )
    os.symlink(external / "secret.ts", repo / "linked-secret.ts")
    os.symlink(external, repo / "linked-directory")

    for noise in (
        "node_modules",
        ".git",
        "build",
        "dist",
        "coverage",
        "__pycache__",
        ".pytest_cache",
    ):
        directory = repo / noise
        directory.mkdir()
        (directory / "noise.ts").write_text(
            "export const PRIVATE_SENTINEL = true;\n", encoding="utf-8"
        )

    sandbox = RepoSandbox(repo)
    denied = [
        sandbox.read_file(str(repo / "normal.ts")),
        sandbox.read_file("nested/../normal.ts"),
        sandbox.read_file("linked-secret.ts"),
        sandbox.read_file("linked-directory/secret.ts"),
        sandbox.list_dir(str(repo)),
        sandbox.list_dir("linked-directory"),
    ]
    secret_hits = sandbox.grep("PRIVATE_SENTINEL")
    repo_map = sandbox.build_repo_map()
    root_listing = sandbox.list_dir(".")

    assert all(result.startswith("ERROR:") for result in denied)
    assert secret_hits.startswith("No matches for pattern")
    assert "linked-secret" not in repo_map
    assert "linked-directory" not in repo_map
    assert "linked-secret" not in root_listing
    assert "linked-directory" not in root_listing
    assert "PRIVATE_SENTINEL" not in repo_map
    assert sandbox.reads == []

    public = sandbox.read_file("normal.ts")
    assert public.startswith("FILE normal.ts")
    assert sandbox.reads == ["normal.ts"]


def test_source_suffix_expansion_is_opt_in_for_non_web_repositories(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "vector.lua").write_text(
        "function vector.from_string(value) return value end\n",
        encoding="utf-8",
    )
    built = repo / "build-app" / "out"
    built.mkdir(parents=True)
    (built / "vector.lua").write_text(
        "function vector.from_string(value) return 'stale build' end\n",
        encoding="utf-8",
    )

    default = RepoSandbox(repo)
    assert default.grep("from_string").startswith("No matches for pattern")
    assert default.reads == []

    expanded = RepoSandbox(
        repo,
        text_suffixes={".lua"},
        ignored_dirs={"build-app"},
    )
    assert "vector.lua:1" in expanded.grep("from_string")
    assert "build-app" not in expanded.grep("from_string")
    assert expanded.reads == ["vector.lua"]


def test_parent_repo_hides_all_grader_roots_but_game_scripts_remain_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "benchmark"
    (root / "benchmarks/core/public").mkdir(parents=True)
    (root / "benchmarks/core/public/tasks.yaml").write_text(
        "tasks: []\n", encoding="utf-8"
    )
    (root / "games/game/scripts").mkdir(parents=True)
    (root / "games/game/scripts/dev.js").write_text(
        "export const GAME_SCRIPT_SENTINEL = true;\n", encoding="utf-8"
    )
    (root / "games/game/src").mkdir()
    (root / "games/game/src/main.ts").write_text(
        "export const GAME_SOURCE_SENTINEL = true;\n", encoding="utf-8"
    )

    private_files = {
        "specs/private.yaml": "ORACLE_SENTINEL: spec\n",
        "scripts/private.py": "ORACLE_SENTINEL = 'script'\n",
        "benchmarks/custom/hidden/private.yaml": "ORACLE_SENTINEL: hidden\n",
        "benchmarks/state_loop/private.yaml": "ORACLE_SENTINEL: state-loop\n",
        ".warptest-cache/private.yaml": "ORACLE_SENTINEL: cache\n",
    }
    for relative, content in private_files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    parent = RepoSandbox(root)
    for relative in private_files:
        assert parent.read_file(relative).startswith("ERROR:")
        assert relative not in parent.reads
    assert parent.grep("ORACLE_SENTINEL").startswith("No matches for pattern")
    parent_map = parent.build_repo_map(max_depth=5)
    assert "private.yaml" not in parent_map
    assert "ORACLE_SENTINEL" not in parent_map
    assert "specs/" not in parent_map
    assert "  scripts/" not in parent_map.splitlines()
    assert "hidden/" not in parent_map
    assert "state_loop/" not in parent_map
    assert ".warptest-cache" not in parent_map

    game = RepoSandbox(root / "games/game")
    script = game.read_file("scripts/dev.js")
    source_hits = game.grep("GAME_SOURCE_SENTINEL")
    game_map = game.build_repo_map()
    assert script.startswith("FILE scripts/dev.js")
    assert "GAME_SCRIPT_SENTINEL" in script
    assert "src/main.ts" in source_hits
    assert "scripts/" in game_map
    assert game.reads == ["scripts/dev.js", "src/main.ts"]


@pytest.mark.parametrize(
    "path",
    [
        "benchmarks/alpha/hidden",
        "benchmarks/alpha/nested/hidden",
        "benchmarks/segments/hidden",
        "benchmarks/core/hidden",
    ],
)
def test_arbitrary_benchmark_hidden_directories_are_private(
    tmp_path: Path,
    path: str,
) -> None:
    root = tmp_path / "benchmark"
    (root / "benchmarks/core/public").mkdir(parents=True)
    (root / "benchmarks/core/public/tasks.yaml").write_text(
        "tasks: []\n", encoding="utf-8"
    )
    (root / "games").mkdir()
    hidden = root / path
    hidden.mkdir(parents=True, exist_ok=True)
    (hidden / "oracle.yaml").write_text("secret: true\n", encoding="utf-8")

    sandbox = RepoSandbox(root)
    assert sandbox.list_dir(path).startswith("ERROR:")
    assert sandbox.read_file(f"{path}/oracle.yaml").startswith("ERROR:")
    assert sandbox.reads == []
