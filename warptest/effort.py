from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


PROFILE_CLASSES = {
    "Game2048Profile": "2048",
    "SpaceRPGProfile": "SpaceRPG",
    "DatingSimProfile": "DatingSim",
    "OpenRAProfile": "OpenRA",
    "JupyterLabProfile": "JupyterLab",
    "LibreOfficeProfile": "LibreOffice",
    "BlenderProfile": "Blender",
    "JynewProfile": "jynew",
    "Pal3Profile": "pal3",
    "LuantiProfile": "luanti",
}

REUSABLE_CORE_FILES = [
    "warptest/adapters.py",
    "warptest/cli.py",
    "warptest/discovery.py",
    "warptest/models.py",
    "warptest/blender_driver.py",
    "warptest/blender_runtime.py",
    "warptest/jupyterlab_runtime.py",
    "warptest/libreoffice_runtime.py",
    "warptest/jynew_runtime.py",
    "warptest/luanti_runtime.py",
    "warptest/openra_runtime.py",
    "warptest/pal3_runtime.py",
    "warptest/paper_tables.py",
    "warptest/reports.py",
    "warptest/runner.py",
    "warptest/runtime.py",
    "warptest/spec_schema.py",
    "warptest/strategies.py",
    "warptest/visual.py",
    "warptest/visual_experiment.py",
]

BENCHMARK_SPEC_DIRS = {
    "2048": "specs/2048",
    "SpaceRPG": "specs/spacerpg",
    "DatingSim": "specs/love",
    "OpenRA": "specs/openra",
    "JupyterLab": "specs/jupyterlab",
    "LibreOffice": "specs/libreoffice",
    "Blender": "specs/blender",
    "jynew": "specs/jynew",
    "pal3": "specs/pal3",
    "luanti": "specs/luanti",
}

BENCHMARK_REPOS = {
    "2048": "games/2048",
    "SpaceRPG": "games/SpaceRPG",
    "DatingSim": "games/DatingSim",
    "OpenRA": "games/OpenRA",
    "JupyterLab": "games/jupyterlab",
    "LibreOffice": "games/LibreOffice",
    "Blender": "games/blender",
    "jynew": "games/jynew",
    "pal3": "games/Pal3.Unity",
    "luanti": "games/luanti",
}

TEXT_SUFFIXES = {
    ".cjs",
    ".cs",
    ".css",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".py",
    ".scss",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}

MAX_TEXT_FILE_BYTES = 1_000_000
SKIP_DIRS = {
    ".git",
    ".github",
    "node_modules",
    "lib",
    "dist",
    "build",
    "build-app",
    "build-debug",
    "build-release",
    "out",
    "coverage",
    "reports",
    "public/assets",
    "pet-runs",
    "tests",
    "test",
    "__tests__",
    "e2e",
    "docs",
    "doc",
    "Library",
    "Temp",
    "Logs",
    "UserSettings",
    "Plugins",
    "obj",
    "bin",
    "Gen",
    "Generated",
}
INSTRUMENTATION_MARKERS = ("warptest", "__warptest", "__warptest_", "__spacerpg_e2e__", "spacerpg-e2e", "warptestcheckpoint")


def build_effort_report(root: str | Path = Path.cwd()) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    profile_stats = _profile_class_stats(base / "warptest/profiles.py")
    game_profiles = {benchmark: profile_stats.get(class_name, {"loc": 0}) for class_name, benchmark in PROFILE_CLASSES.items()}
    reusable_files = {path: _source_loc(base / path) for path in REUSABLE_CORE_FILES if (base / path).exists()}
    spec_stats = _spec_stats(base)
    instrumentation = {benchmark: _instrumentation_stats(base / repo) for benchmark, repo in BENCHMARK_REPOS.items()}
    reusable_total = sum(reusable_files.values()) + profile_stats.get("BaseProfile", {}).get("loc", 0)
    profile_total = sum(item["loc"] for item in game_profiles.values())
    spec_total = sum(item["loc"] for item in spec_stats.values())
    return {
        "schema_version": 1,
        "root": str(base),
        "method": {
            "loc": "Non-empty, non-comment source/config lines.",
            "profile_loc": "AST class-body spans in warptest/profiles.py.",
            "instrumentation": "Text lines in benchmark repos containing warptest/e2e hook markers.",
        },
        "loc": {
            "reusable_core_and_adapters": reusable_total,
            "reusable_files": reusable_files,
            "base_profile_helpers": profile_stats.get("BaseProfile", {"loc": 0}),
            "game_profiles_total": profile_total,
            "game_profiles": game_profiles,
            "specs_total": spec_total,
            "specs": spec_stats,
        },
        "instrumentation": instrumentation,
        "ratios": {
            "game_profile_to_reusable_loc": profile_total / reusable_total if reusable_total else None,
            "spec_to_profile_loc": spec_total / profile_total if profile_total else None,
        },
    }


def _profile_class_stats(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    tree = ast.parse(text)
    stats: dict[str, dict[str, Any]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.end_lineno is not None:
            selected = lines[node.lineno - 1 : node.end_lineno]
            stats[node.name] = {
                "loc": _source_loc_from_lines(selected),
                "start_line": node.lineno,
                "end_line": node.end_lineno,
            }
    return stats


def _spec_stats(root: Path) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for benchmark, folder in BENCHMARK_SPEC_DIRS.items():
        spec_paths = sorted((root / folder).glob("*.yaml"))
        stats[benchmark] = {
            "count": len(spec_paths),
            "loc": sum(_source_loc(path) for path in spec_paths),
            "files": [str(path.relative_to(root)) for path in spec_paths],
        }
    return stats


def _instrumentation_stats(repo: Path) -> dict[str, Any]:
    files: dict[str, int] = {}
    marker_lines = 0
    if not repo.exists():
        return {"marker_lines": 0, "files": files}
    for path in _iter_text_files(repo):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        count = sum(1 for line in lines if _has_marker(line))
        if count:
            files[str(path.relative_to(repo))] = count
            marker_lines += count
    return {"marker_lines": marker_lines, "files": files}


def _iter_text_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(str(relative).startswith(skip) or skip in relative.parts for skip in SKIP_DIRS):
            continue
        if path.suffix in TEXT_SUFFIXES and path.stat().st_size <= MAX_TEXT_FILE_BYTES:
            paths.append(path)
    return paths


def _has_marker(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in INSTRUMENTATION_MARKERS)


def _source_loc(path: Path) -> int:
    if path.stat().st_size > MAX_TEXT_FILE_BYTES:
        return 0
    return _source_loc_from_lines(path.read_text(encoding="utf-8").splitlines())


def _source_loc_from_lines(lines: list[str]) -> int:
    return sum(1 for line in lines if line.strip() and not line.lstrip().startswith("#"))
