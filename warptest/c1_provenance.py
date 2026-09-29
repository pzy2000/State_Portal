"""Fail-closed content provenance for the formal C1 experiment.

The parent checkout is intentionally allowed to contain uncommitted implementation
work, so a Git commit alone cannot identify the harness.  The formal run therefore
binds every runtime Python module, the manifest/task/spec inputs, and clean pinned
game/Agent-S gitlinks into one content hash.  Recomputing this hash before every P5
invocation prevents a long run from mixing samples across code or benchmark drift.
"""
from __future__ import annotations

import configparser
import hashlib
import json
import platform
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from .c1_catalog import validate_c1_v5_catalog
from .c1_evaluation import visual_oracle
from .native_window import screen_size


FORMAL_TASK_FILE = Path("benchmarks/p5/c1_tasks.yaml")
FORMAL_MANIFEST_FILE = Path("benchmarks/p5/manifest.yaml")
FORMAL_C1_ARMS = ("c1_agent_s", "c1_agent_s_synth", "c1_state")
LEGACY_FORMAL_BENCHMARK_COUNTS = {
    "2048": 5,
    "DatingSim": 5,
    "SpaceRPG": 5,
    "luanti": 5,
}
PINNED_REPOSITORIES = ("baselines/agent-s",)


class C1EnvironmentMismatch(RuntimeError):
    """The observed machine does not match the declared formal environment."""

    def __init__(self, evidence: dict[str, Any]) -> None:
        self.evidence = evidence
        fields = ", ".join(evidence.get("mismatches") or []) or "unknown"
        super().__init__(f"Formal C1 environment differs from manifest: {fields}")


def _run_git(base: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(base), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return completed.stdout.strip()


def _formal_task_inputs(base: Path) -> tuple[list[Path], list[dict[str, Any]]]:
    task_path = (base / FORMAL_TASK_FILE).resolve()
    payload = yaml.safe_load(task_path.read_text(encoding="utf-8"))
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, list) or not all(isinstance(task, dict) for task in tasks):
        raise RuntimeError(f"Formal C1 task file has no valid tasks list: {task_path}")
    task_rows = [dict(task) for task in tasks]
    formal_v5 = (
        isinstance(payload, dict)
        and payload.get("task_schema") == "c1_v5"
    )
    if formal_v5:
        try:
            _, task_rows = validate_c1_v5_catalog(
                payload,
                source_path=task_path,
            )
        except ValueError as error:
            raise RuntimeError(str(error)) from error
    task_ids = [str(task.get("id") or "") for task in task_rows]
    manifest_path = (base / FORMAL_MANIFEST_FILE).resolve()
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise RuntimeError(f"Formal C1 manifest must be a mapping: {manifest_path}")
    expected_count = int(manifest.get("c1_task_count", 0) or 0)
    if expected_count <= 0:
        raise RuntimeError("Formal C1 manifest must declare a positive c1_task_count")
    if (
        len(task_rows) != expected_count
        or not all(task_ids)
        or len(set(task_ids)) != expected_count
    ):
        raise RuntimeError(
            "Formal C1 task file must contain exactly "
            f"{expected_count} unique task ids"
        )
    counts = Counter(str(task.get("benchmark") or "") for task in task_rows)
    declared_counts = manifest.get("c1_benchmark_task_counts")
    if declared_counts is None and expected_count == 20:
        declared_counts = LEGACY_FORMAL_BENCHMARK_COUNTS
    if not isinstance(declared_counts, dict) or not declared_counts:
        raise RuntimeError(
            "Formal C1 manifest must declare c1_benchmark_task_counts"
        )
    expected_counts = {
        str(benchmark): int(count)
        for benchmark, count in declared_counts.items()
    }
    if any(count <= 0 for count in expected_counts.values()):
        raise RuntimeError("Formal C1 benchmark task counts must be positive")
    if sum(expected_counts.values()) != expected_count:
        raise RuntimeError(
            "Formal C1 benchmark task counts do not sum to c1_task_count"
        )
    if dict(counts) != expected_counts:
        raise RuntimeError(
            "Formal C1 task distribution differs from c1_benchmark_task_counts; "
            f"got {dict(counts)}"
        )

    inputs = [task_path]
    for task in task_rows:
        raw_spec = task.get("oracle_spec")
        if not isinstance(raw_spec, str) or not raw_spec:
            raise RuntimeError(f"Formal C1 task {task.get('id')} is missing oracle_spec")
        spec = (base / raw_spec).resolve()
        try:
            spec.relative_to(base)
        except ValueError as error:
            raise RuntimeError(f"Formal C1 oracle spec escapes the repository: {raw_spec}") from error
        if not spec.is_file():
            raise RuntimeError(f"Formal C1 oracle spec does not exist: {raw_spec}")
        inputs.append(spec)
        spec_payload = yaml.safe_load(spec.read_text(encoding="utf-8"))
        if (
            formal_v5
            and isinstance(spec_payload, dict)
            and spec_payload.get("benchmark") != task.get("benchmark")
        ):
            raise RuntimeError(
                f"Formal C1 oracle spec benchmark differs from task catalog: "
                f"{raw_spec}"
            )
        visual = visual_oracle(spec_payload) if isinstance(spec_payload, dict) else None
        if not isinstance(visual, dict):
            raise RuntimeError(
                f"Formal C1 oracle spec lacks visual_oracle: {raw_spec}"
            )
        registered_goldens = {
            key: value
            for key, value in visual.items()
            if (key == "golden_image" or str(key).startswith("golden_"))
            and isinstance(value, str)
            and value.strip()
        }
        if not registered_goldens:
            raise RuntimeError(
                f"Formal C1 oracle spec has no registered visual golden: {raw_spec}"
            )
        for field, raw_golden in sorted(registered_goldens.items()):
            relative = Path(raw_golden)
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError(
                    f"Formal C1 {field} must be a contained relative path: "
                    f"{raw_spec}: {raw_golden}"
                )
            declared = spec.parent / relative
            if declared.is_symlink():
                raise RuntimeError(
                    f"Formal C1 {field} must not be a symlink: {raw_spec}: {raw_golden}"
                )
            golden = declared.resolve()
            try:
                golden.relative_to(base)
            except ValueError as error:
                raise RuntimeError(
                    f"Formal C1 visual golden escapes the repository: "
                    f"{raw_spec}: {raw_golden}"
                ) from error
            if not golden.is_file():
                raise RuntimeError(
                    f"Formal C1 visual golden does not exist: "
                    f"{raw_spec}: {raw_golden}"
                )
            inputs.append(golden)
    return inputs, task_rows


def _formal_host_registry(base: Path) -> dict[str, dict[str, str]]:
    task_path = (base / FORMAL_TASK_FILE).resolve()
    payload = yaml.safe_load(task_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("task_schema") != "c1_v5":
        return {}
    try:
        hosts, _ = validate_c1_v5_catalog(payload, source_path=task_path)
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    return hosts


def _pinned_repository_snapshot(base: Path, relpath: str) -> str:
    repo = (base / relpath).resolve()
    if not repo.is_dir():
        raise RuntimeError(f"Required C1 repository is not initialized: {relpath}")
    index_row = _run_git(base, "ls-files", "-s", "--", relpath)
    fields = index_row.split()
    if len(fields) < 4 or fields[0] != "160000":
        raise RuntimeError(f"Required C1 repository is not a pinned gitlink: {relpath}")
    expected = fields[1]
    actual = _run_git(repo, "rev-parse", "HEAD")
    if actual != expected:
        raise RuntimeError(
            f"Required C1 repository HEAD differs from parent gitlink: {relpath} "
            f"({actual} != {expected})"
        )
    dirty = _run_git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if dirty:
        preview = dirty.splitlines()[0]
        raise RuntimeError(f"Required C1 repository is dirty: {relpath}: {preview}")
    return actual


def _gitmodules_urls(base: Path) -> dict[str, str]:
    path = base / ".gitmodules"
    parser = configparser.ConfigParser()
    try:
        parser.read_string(path.read_text(encoding="utf-8"))
    except (OSError, configparser.Error) as error:
        raise RuntimeError(f"Cannot parse required .gitmodules: {error}") from error
    result: dict[str, str] = {}
    for section in parser.sections():
        if not section.startswith('submodule "') or not section.endswith('"'):
            continue
        relpath = str(parser.get(section, "path", fallback="")).strip()
        url = str(parser.get(section, "url", fallback="")).strip()
        if relpath and url:
            result[relpath] = url
    return result


def c1_application_revisions(root: str | Path) -> dict[str, dict[str, str]]:
    """Validate and return the ten explicit application revisions."""

    base = Path(root).expanduser().resolve()
    hosts = _formal_host_registry(base)
    if len(hosts) != 10:
        raise RuntimeError("Formal C1 c1_v5 catalog must register ten benchmark hosts")
    gitmodules_urls = _gitmodules_urls(base)
    revisions: dict[str, dict[str, str]] = {}
    for benchmark, host in hosts.items():
        repo = host["repo"]
        declared_url = host["gitmodules_url"]
        observed_url = gitmodules_urls.get(repo)
        if observed_url != declared_url:
            raise RuntimeError(
                f"Formal C1 host {benchmark} .gitmodules URL differs from registry: "
                f"{observed_url!r} != {declared_url!r}"
            )
        actual = _pinned_repository_snapshot(base, repo)
        if actual != host["revision"]:
            raise RuntimeError(
                f"Formal C1 host {benchmark} revision differs from registry: "
                f"{actual} != {host['revision']}"
            )
        revisions[benchmark] = {
            "display_name": host["display_name"],
            "repo": repo,
            "gitmodules_url": declared_url,
            "revision": actual,
        }
    return revisions


def c1_reproducibility_contract(
    root: str | Path,
    *,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    if manifest is None:
        manifest_path = (base / FORMAL_MANIFEST_FILE).resolve()
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    raw = manifest.get("c1_reproducibility") if isinstance(manifest, dict) else None
    if not isinstance(raw, dict):
        raise RuntimeError("Formal C1 manifest must declare c1_reproducibility")
    contract = {
        "harness_revision": str(raw.get("harness_revision") or "").strip(),
        "os": str(raw.get("os") or "").strip(),
        "architecture": str(raw.get("architecture") or "").strip(),
        "physical_screen": raw.get("physical_screen"),
    }
    if (
        not contract["harness_revision"]
        or not contract["os"]
        or not contract["architecture"]
        or not isinstance(contract["physical_screen"], list)
        or len(contract["physical_screen"]) != 2
        or not all(isinstance(value, int) and value > 0 for value in contract["physical_screen"])
    ):
        raise RuntimeError("Formal C1 c1_reproducibility contract is incomplete")
    contract["physical_screen"] = list(contract["physical_screen"])
    return contract


def observe_c1_environment() -> dict[str, Any]:
    product_version = subprocess.run(
        ["sw_vers", "-productVersion"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    build_version = subprocess.run(
        ["sw_vers", "-buildVersion"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    width, height = screen_size()
    return {
        "os": f"macOS {product_version} ({build_version})",
        "architecture": platform.machine(),
        "physical_screen": [int(width), int(height)],
    }


def validate_c1_environment(
    root: str | Path,
    *,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Observe and fail closed on OS, architecture, or physical-screen drift."""

    declared = c1_reproducibility_contract(root, manifest=manifest)
    observed = observe_c1_environment()
    compared_fields = ("os", "architecture", "physical_screen")
    mismatches = [
        field for field in compared_fields if declared.get(field) != observed.get(field)
    ]
    canonical = json.dumps(
        {"declared": declared, "observed": observed},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    evidence = {
        "schema_version": 1,
        "declared": declared,
        "observed": observed,
        "environment_fingerprint": hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest(),
        "status": "mismatch" if mismatches else "matched",
        "mismatches": mismatches,
    }
    if mismatches:
        raise C1EnvironmentMismatch(evidence)
    return evidence


def c1_task_manifest_hash(
    root: str | Path,
    *,
    manifest: dict[str, Any],
    arms: list[str] | tuple[str, ...],
    task_ids: list[str] | None,
    manifest_hash: str | None = None,
) -> str:
    """Hash the full catalog bytes, host registry, and selected task rows."""

    base = Path(root).expanduser().resolve()
    manifest_digest = manifest_hash or hashlib.sha256(
        json.dumps(
            manifest,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:16]
    selected_arms = [arm for arm in arms if arm in FORMAL_C1_ARMS]
    if not selected_arms:
        identity = (
            {"manifest_hash": manifest_digest, "task_ids": task_ids}
            if task_ids
            else manifest
        )
        return hashlib.sha256(
            json.dumps(
                identity,
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:16]
    arm_cfg = manifest.get("arms", {}).get(selected_arms[0], {}) or {}
    raw_path = arm_cfg.get("tasks", str(FORMAL_TASK_FILE))
    task_path = Path(str(raw_path)).expanduser()
    if not task_path.is_absolute():
        task_path = (base / task_path).resolve()
    raw = task_path.read_bytes()
    payload = yaml.safe_load(raw)
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, list) or not all(isinstance(task, dict) for task in tasks):
        raise RuntimeError(f"C1 task file must contain mapping task rows: {task_path}")
    if task_ids:
        by_id = {str(task.get("id") or ""): task for task in tasks}
        missing = [task_id for task_id in task_ids if task_id not in by_id]
        if missing:
            raise RuntimeError("Unknown C1 task id(s): " + ", ".join(missing))
        selected_rows = [by_id[task_id] for task_id in task_ids]
    else:
        selected_rows = tasks
    try:
        task_file = task_path.relative_to(base).as_posix()
    except ValueError:
        task_file = str(task_path)
    identity = {
        "manifest_hash": manifest_digest,
        "task_file": task_file,
        "task_file_sha256": hashlib.sha256(raw).hexdigest(),
        "task_file_content": raw.decode("utf-8"),
        "benchmark_hosts": (
            payload.get("benchmark_hosts") if isinstance(payload, dict) else None
        ),
        "selected_tasks": selected_rows,
    }
    return hashlib.sha256(
        json.dumps(
            identity,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:16]


def c1_harness_hash(root: str | Path) -> str:
    """Return the complete formal-C1 content hash or fail on mutable subrepos."""

    base = Path(root).expanduser().resolve()
    task_inputs, task_rows = _formal_task_inputs(base)
    del task_rows  # Validation above is itself part of the provenance contract.
    application_revisions = _formal_host_registry(base)

    static_inputs = [
        base / ".gitmodules",
        base / "pyproject.toml",
        base / "requirements-agent-s.txt",
        base / FORMAL_MANIFEST_FILE,
        # Executed by every LibreOffice native C1 bridge command.  Java sources
        # are not covered by the Python runtime glob below.
        base / "warptest" / "LibreOfficeC1Bridge.java",
        # BrowserRuntimeAdapter injects this file through NODE_OPTIONS=--require.
        # It is executable harness code even though it is not Python.
        base / "warptest" / "node_preloads" / "no_fsevents.cjs",
    ]
    runtime_resource_inputs = []
    for resource_root in (
        base / "warptest" / "instrumentation",
        base / "warptest" / "node_preloads",
    ):
        runtime_resource_inputs.extend(
            path for path in resource_root.rglob("*") if path.is_file()
        )
    runtime_inputs = sorted(
        [
            *(
                path
                for suffix in ("*.py", "*.swift", "*.m", "*.java")
                for path in (base / "warptest").rglob(suffix)
            ),
            *runtime_resource_inputs,
        ]
    )
    paths = sorted({path.resolve() for path in [*static_inputs, *task_inputs, *runtime_inputs]})

    digest = hashlib.sha256()
    for path in paths:
        if not path.is_file():
            raise RuntimeError(f"Required C1 provenance input is missing: {path}")
        try:
            relpath = path.relative_to(base).as_posix()
        except ValueError as error:
            raise RuntimeError(f"C1 provenance input escapes the repository: {path}") from error
        digest.update(b"file\0")
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")

    for relpath in PINNED_REPOSITORIES:
        commit = _pinned_repository_snapshot(base, relpath)
        digest.update(b"gitlink\0")
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\0")
        digest.update(commit.encode("ascii"))
        digest.update(b"\0clean\0")
    if application_revisions:
        # Validate the registry against .gitmodules, parent gitlinks, child HEADs,
        # and clean worktrees, then bind the explicit ids and revisions.
        for benchmark, host in c1_application_revisions(base).items():
            digest.update(b"application\0")
            digest.update(benchmark.encode("utf-8"))
            digest.update(b"\0")
            digest.update(host["repo"].encode("utf-8"))
            digest.update(b"\0")
            digest.update(host["revision"].encode("ascii"))
            digest.update(b"\0clean\0")
    return digest.hexdigest()
