from __future__ import annotations

import contextlib
import contextvars
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

import yaml

from .models import CheckpointValidity


CORE_PUBLIC_TASKS = Path("benchmarks/core/public/tasks.yaml")
CORE_HIDDEN_ORACLES = Path("benchmarks/core/hidden/oracles.yaml")


_TASK_REPO_OVERRIDES: contextvars.ContextVar[dict[str, Path]] = (
    contextvars.ContextVar("warptest_task_repo_overrides", default={})
)


@dataclass(frozen=True)
class PublicIssue:
    """Public task input. This is the only benchmark task text visible to methods."""

    id: str
    benchmark: str
    repo: str
    issue: str
    tags: list[str] = field(default_factory=list)
    budget_ms: int = 60_000
    prompt_revision: str = ""
    random_control: dict[str, Any] = field(default_factory=dict)
    source_path: str | None = None

    @classmethod
    def from_mapping(cls, item: dict[str, Any], *, source_path: Path) -> "PublicIssue":
        return cls(
            id=_required_str(item, "id", source_path),
            benchmark=_required_str(item, "benchmark", source_path),
            repo=_required_str(item, "repo", source_path),
            issue=_required_str(item, "issue", source_path),
            tags=[str(tag) for tag in item.get("tags", [])],
            budget_ms=int(item.get("budget_ms", 60_000)),
            prompt_revision=str(item.get("prompt_revision") or "").strip(),
            random_control=(
                dict(item.get("random_control"))
                if isinstance(item.get("random_control"), dict)
                else {}
            ),
            source_path=str(source_path),
        )

    def public_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("source_path", None)
        return payload


@dataclass(frozen=True)
class HiddenOracle:
    """Private grading target. Methods must never receive this object."""

    task_id: str
    hidden_spec: str
    source_path: str | None = None

    @classmethod
    def from_mapping(cls, item: dict[str, Any], *, source_path: Path) -> "HiddenOracle":
        return cls(
            task_id=_required_str(item, "task_id", source_path),
            hidden_spec=_required_str(item, "hidden_spec", source_path),
            source_path=str(source_path),
        )


@dataclass(frozen=True)
class MethodCheckpoint:
    """Method output for the core synthesis experiment."""

    task_id: str
    benchmark: str
    generated_spec: dict[str, Any]
    source_evidence: list[dict[str, Any]]
    payload: dict[str, Any]
    validity_class: CheckpointValidity
    runtime_validation: dict[str, Any]
    inputs_read: list[str]
    hidden_files_read: list[str] = field(default_factory=list)
    generated_by: str = "warptest-heuristic-synthesizer-v1"
    # The method's own proposed executable spec before any public-task scaffolding.
    # None means the method did not return a usable spec mapping (for example, prose).
    model_generated_spec: dict[str, Any] | None = None
    # True when the executed generated_spec was canonicalized from the public-task
    # scaffold rather than taken directly from the method output.
    used_public_scaffold: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_public_issues(root: str | Path = Path.cwd(), tasks_path: str | Path = CORE_PUBLIC_TASKS) -> list[PublicIssue]:
    base = Path(root).expanduser().resolve()
    path = _resolve(base, tasks_path)
    data = _load_yaml_mapping(path)
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError(f"Public task file must contain a tasks list: {path}")
    return [PublicIssue.from_mapping(item, source_path=path) for item in tasks if isinstance(item, dict)]


def load_hidden_oracles(root: str | Path = Path.cwd(), oracle_path: str | Path = CORE_HIDDEN_ORACLES) -> dict[str, HiddenOracle]:
    base = Path(root).expanduser().resolve()
    path = _resolve(base, oracle_path)
    data = _load_yaml_mapping(path)
    oracles = data.get("oracles")
    if not isinstance(oracles, list):
        raise ValueError(f"Hidden oracle file must contain an oracles list: {path}")
    result: dict[str, HiddenOracle] = {}
    for item in oracles:
        if not isinstance(item, dict):
            continue
        oracle = HiddenOracle.from_mapping(item, source_path=path)
        result[oracle.task_id] = oracle
    return result


def resolve_task_repo(root: str | Path, task: PublicIssue) -> Path:
    resolved = _resolve(Path(root).expanduser().resolve(), task.repo)
    return _TASK_REPO_OVERRIDES.get().get(str(resolved), resolved)


@contextlib.contextmanager
def task_repo_override(
    source_repo: str | Path, target_repo: str | Path
) -> Iterator[Path]:
    """Override one resolved task checkout in the current execution context.

    Context variables are copied per thread/task context, so concurrent C1 lanes
    can resolve the same public task to different APFS clones without mutating
    the shared task object or changing trusted oracle paths.
    """

    source = Path(source_repo).expanduser().resolve()
    target = Path(target_repo).expanduser().resolve()
    overrides = dict(_TASK_REPO_OVERRIDES.get())
    overrides[str(source)] = target
    token = _TASK_REPO_OVERRIDES.set(overrides)
    try:
        yield target
    finally:
        _TASK_REPO_OVERRIDES.reset(token)


def resolve_hidden_spec(root: str | Path, oracle: HiddenOracle) -> Path:
    return _resolve(Path(root).expanduser().resolve(), oracle.hidden_spec)


def validate_public_tasks_do_not_reference_hidden(root: str | Path = Path.cwd(), tasks_path: str | Path = CORE_PUBLIC_TASKS) -> list[str]:
    base = Path(root).expanduser().resolve()
    path = _resolve(base, tasks_path)
    data = _load_yaml_mapping(path)
    errors: list[str] = []
    _scan_for_hidden_reference(data, path, errors)
    return errors


def validate_method_checkpoint(
    checkpoint: MethodCheckpoint | dict[str, Any],
    root: str | Path = Path.cwd(),
    *,
    allow_empty_source_evidence: bool = False,
) -> list[str]:
    data = checkpoint.to_dict() if isinstance(checkpoint, MethodCheckpoint) else checkpoint
    errors: list[str] = []
    for key in ["task_id", "benchmark", "generated_spec", "source_evidence", "payload", "validity_class", "runtime_validation", "inputs_read"]:
        if key not in data:
            errors.append(f"method checkpoint missing {key}")
    if not isinstance(data.get("generated_spec"), dict):
        errors.append("method checkpoint generated_spec must be a mapping")
    if data.get("validity_class") not in {"replay-reachable", "save-restorable", "injection-only", "unknown"}:
        errors.append("method checkpoint validity_class is invalid")
    if not isinstance(data.get("source_evidence"), list) or (
        not data.get("source_evidence") and not allow_empty_source_evidence
    ):
        errors.append("method checkpoint source_evidence must be a non-empty list")
    if not isinstance(data.get("payload"), dict) or not data.get("payload", {}).get("kind"):
        errors.append("method checkpoint payload.kind is required")
    runtime_validation = data.get("runtime_validation")
    if not isinstance(runtime_validation, dict) or not runtime_validation.get("status"):
        errors.append("method checkpoint runtime_validation.status is required")
    if data.get("hidden_files_read"):
        errors.append("method checkpoint must not read hidden target/oracle files")
    hidden_root = Path(root).expanduser().resolve() / "benchmarks" / "core" / "hidden"
    for value in data.get("inputs_read", []):
        try:
            path = Path(value).expanduser().resolve()
        except Exception:
            continue
        if path == hidden_root or hidden_root in path.parents:
            errors.append(f"method checkpoint input leaks hidden path: {value}")
    return errors


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"YAML file must be a mapping: {path}")
    return data


def _required_str(item: dict[str, Any], key: str, source_path: Path) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{source_path}: {key} must be a non-empty string")
    return value


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (root / path).resolve()


def _scan_for_hidden_reference(value: Any, path: Path, errors: list[str]) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _scan_for_hidden_reference(item, path, errors)
    elif isinstance(value, list):
        for item in value:
            _scan_for_hidden_reference(item, path, errors)
    elif isinstance(value, str):
        lowered = value.lower()
        if "benchmarks/core/hidden" in lowered or "hidden target" in lowered or "hidden oracle" in lowered or "hidden_spec" in lowered:
            errors.append(f"Public task file references hidden grading data: {path}: {value}")
