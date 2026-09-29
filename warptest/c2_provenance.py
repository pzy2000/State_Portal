"""Content provenance for the formal C2 memory-transfer comparison.

C2 deliberately runs several models concurrently against the same benchmark
checkouts.  A Git commit alone is not enough to identify a run because the
parent checkout may contain intentional, uncommitted harness changes.  This
module binds the complete evaluator/runtime source, C2 task/oracle inputs, and
the pinned game revisions to one digest.  It is intentionally independent of
runtime output files, so normal temporary game artifacts do not invalidate a
resume.
"""
from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import yaml


C2_MANIFEST_FILE = Path("benchmarks/p5/manifest.yaml")
C2_PUBLIC_TASK_FILE = Path("benchmarks/core/public/tasks.yaml")
C2_SPLIT_FILE = Path("benchmarks/core/splits/evo_phase3.yaml")
# The only hidden-grading file the C2 runtime loads (task_schema.CORE_HIDDEN_ORACLES).
# ``benchmarks/core/hidden/openra-oracles.yaml`` and the sibling
# ``core/public/tasks_explore.yaml`` file are inputs for unrelated CLI paths
# (explore-execute grading) and are never read by the C2 evaluator, so they are
# intentionally excluded here.
C2_HIDDEN_ORACLE_FILE = Path("benchmarks/core/hidden/oracles.yaml")

# Top-level manifest fields the C2 orchestrator path reads (see
# P5Orchestrator.run_c2_memory_baseline and its __init__). Arm sections other
# than c2_memory_baseline itself (c1_*, c3_fuzz, pareto, ...) are deliberately
# excluded so editing them mid-run cannot invalidate a running C2 harness hash.
_C2_MANIFEST_FIELDS = ("models", "seeds", "api", "retry", "model_request_profiles")

# C2 executes this closure directly.  C1 GUI/state-loop sources and P5
# aggregation are deliberately not roots: their provenance is separately
# attached to C1, and hashing them here made a concurrent, unrelated C1 edit
# invalidate an otherwise untouched C2 experiment.
_C2_RUNTIME_ROOTS = (
    "c2_provenance.py",
    "core_experiment.py",
    "evo.py",
    "memory_backends.py",
    "runner.py",
    "synthesis.py",
    "task_schema.py",
)
_C2_EXCLUDED_RUNTIME_MODULES = {
    "c1_calibration.py",
    "c1_experiment.py",
    "c1_provenance.py",
    "c1_public_semantics.py",
    "fuzz_llm.py",
    "fuzzing.py",
    "spacerpg_fuzz_profile.py",
    "spacerpg_gui_baseline.py",
    "gui.py",
    "gui_agent.py",
    "jynew_fuzz.py",
    "openra_fuzz.py",
    "p5_aggregate.py",
    "p5_scale.py",
    "p5_stats.py",
    "paper_tables.py",
    "state_loop.py",
    "visual_experiment.py",
}


def _run_git(base: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(base), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return completed.stdout.strip()


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Required C2 provenance input is not a mapping: {path}")
    return data


def _selected_repositories(base: Path) -> tuple[list[Path], list[str]]:
    """Validate the C2 split and return its public task file and game repos."""

    task_path = (base / C2_PUBLIC_TASK_FILE).resolve()
    split_path = (base / C2_SPLIT_FILE).resolve()
    task_payload = _load_yaml(task_path)
    split_payload = _load_yaml(split_path)
    tasks = task_payload.get("tasks")
    train = split_payload.get("train_task_ids")
    heldout = split_payload.get("heldout_task_ids")
    if not isinstance(tasks, list) or not all(isinstance(task, dict) for task in tasks):
        raise RuntimeError(f"C2 public task file has no valid tasks list: {task_path}")
    if not isinstance(train, list) or not isinstance(heldout, list):
        raise RuntimeError(f"C2 split has no valid train/held-out lists: {split_path}")
    selected_ids = [str(item) for item in [*train, *heldout]]
    if not selected_ids or len(selected_ids) != len(set(selected_ids)):
        raise RuntimeError("C2 split must contain non-overlapping, unique task ids")
    task_by_id = {
        str(task.get("id")): task
        for task in tasks
        if isinstance(task.get("id"), str) and task.get("id")
    }
    if set(selected_ids) != set(task_by_id):
        missing = sorted(set(selected_ids) - set(task_by_id))
        extra = sorted(set(task_by_id) - set(selected_ids))
        raise RuntimeError(
            "C2 split must cover exactly the registered public task universe "
            f"(missing={missing[:3]}, extra={extra[:3]})"
        )
    repositories: set[str] = set()
    for task_id in selected_ids:
        repo = task_by_id[task_id].get("repo")
        if not isinstance(repo, str) or not repo:
            raise RuntimeError(f"C2 task {task_id} has no repository path")
        repo_path = (base / repo).resolve()
        try:
            repo_path.relative_to(base)
        except ValueError as error:
            raise RuntimeError(f"C2 task {task_id} repository escapes workspace: {repo}") from error
        if not repo_path.is_dir():
            raise RuntimeError(f"C2 task {task_id} repository is unavailable: {repo}")
        repositories.add(repo_path.relative_to(base).as_posix())
    return [task_path, split_path], sorted(repositories)


def _pinned_repository_snapshot(base: Path, relpath: str) -> str:
    """Return a repository revision only when it matches the parent gitlink."""

    repo = (base / relpath).resolve()
    index_row = _run_git(base, "ls-files", "-s", "--", relpath)
    fields = index_row.split()
    if len(fields) < 4 or fields[0] != "160000":
        raise RuntimeError(f"Required C2 repository is not a pinned gitlink: {relpath}")
    expected = fields[1]
    actual = _run_git(repo, "rev-parse", "HEAD")
    if actual != expected:
        raise RuntimeError(
            f"Required C2 repository HEAD differs from parent gitlink: {relpath} "
            f"({actual} != {expected})"
        )
    return actual


def validate_c2_repositories_clean(root: str | Path) -> None:
    """Fail before a formal run if any C2 game checkout has local source drift."""

    base = Path(root).expanduser().resolve()
    _, repositories = _selected_repositories(base)
    for relpath in repositories:
        _pinned_repository_snapshot(base, relpath)
        dirty = _run_git(
            (base / relpath).resolve(),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        if dirty:
            raise RuntimeError(f"Required C2 repository is dirty: {relpath}: {dirty.splitlines()[0]}")


def _relative_import_targets(module_path: Path, package_root: Path) -> list[Path]:
    """Return local modules statically imported by one C2 runtime module."""

    try:
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    except SyntaxError as error:
        raise RuntimeError(f"Cannot parse C2 runtime module: {module_path}") from error
    targets: set[Path] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level < 1:
            continue
        relative_dir = module_path.parent
        for _ in range(node.level - 1):
            relative_dir = relative_dir.parent
        module_name = node.module.replace(".", "/") if node.module else ""
        candidates: list[Path] = []
        if module_name:
            candidates.append(relative_dir / f"{module_name}.py")
        else:
            candidates.extend(relative_dir / f"{alias.name.replace('.', '/')}.py" for alias in node.names)
        for candidate in candidates:
            try:
                candidate.resolve().relative_to(package_root.resolve())
            except ValueError:
                continue
            if candidate.is_file() and candidate.name not in _C2_EXCLUDED_RUNTIME_MODULES:
                targets.add(candidate.resolve())
    return sorted(targets)


def c2_runtime_input_paths(root: str | Path) -> list[Path]:
    """Return the C2 runtime's local-import closure, excluding C1-only code.

    The orchestrator invocation context records the scheduling configuration;
    this digest instead protects the C2 executor, memory backends, task/runtime
    evaluator, and every local module they import.  It therefore detects a C2
    semantic change while remaining stable under unrelated C1 development.
    """

    base = Path(root).expanduser().resolve()
    package_root = base / "warptest"
    pending = [package_root / name for name in _C2_RUNTIME_ROOTS]
    paths: set[Path] = set()
    while pending:
        module = pending.pop()
        resolved = module.resolve()
        if resolved in paths:
            continue
        if not resolved.is_file():
            raise RuntimeError(f"Required C2 runtime module is missing: {module}")
        if resolved.name in _C2_EXCLUDED_RUNTIME_MODULES:
            continue
        paths.add(resolved)
        pending.extend(_relative_import_targets(resolved, package_root))
    return sorted(paths)


def c2_manifest_fingerprint(manifest: dict[str, Any]) -> str:
    """Return a stable digest of only the manifest fields the C2 arm consumes.

    C2 shares one manifest file with the C1/C3 arms.  Hashing the whole
    document made an unrelated arm edit (for example c3_fuzz mutation tuning,
    or a C1 step-budget change) invalidate an in-progress C2 run.  This
    fingerprint isolates the ``arms.c2_memory_baseline`` config plus the
    top-level fields the C2 orchestrator path actually reads.
    """

    if not isinstance(manifest, dict):
        raise RuntimeError("C2 manifest fingerprint requires a mapping")
    arms = manifest.get("arms")
    relevant: dict[str, Any] = {
        "arm": (arms.get("c2_memory_baseline", {}) if isinstance(arms, dict) else {}),
        **{field: manifest.get(field) for field in _C2_MANIFEST_FIELDS},
    }
    blob = json.dumps(relevant, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def c2_harness_hash(root: str | Path) -> str:
    """Return a stable digest of all formal-C2 harness and task inputs."""

    base = Path(root).expanduser().resolve()
    split_inputs, repositories = _selected_repositories(base)
    static_inputs = [
        base / ".gitmodules",
        base / "pyproject.toml",
        base / C2_HIDDEN_ORACLE_FILE,
        *split_inputs,
    ]
    runtime_inputs = c2_runtime_input_paths(base)
    paths = sorted({path.resolve() for path in [*static_inputs, *runtime_inputs]})

    digest = hashlib.sha256()
    for path in paths:
        if not path.is_file():
            raise RuntimeError(f"Required C2 provenance input is missing: {path}")
        try:
            relpath = path.relative_to(base).as_posix()
        except ValueError as error:
            raise RuntimeError(f"C2 provenance input escapes workspace: {path}") from error
        digest.update(b"file\0")
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")

    manifest_payload = _load_yaml((base / C2_MANIFEST_FILE).resolve())
    digest.update(b"manifest-fingerprint\0")
    digest.update(c2_manifest_fingerprint(manifest_payload).encode("ascii"))
    digest.update(b"\0")

    for relpath in repositories:
        commit = _pinned_repository_snapshot(base, relpath)
        digest.update(b"gitlink\0")
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\0")
        digest.update(commit.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()
