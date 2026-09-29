"""Fail-closed catalog and oracle projection for C1-Lazarus."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .c1_catalog import expected_random_control
from .c1_evaluation import (
    validate_evaluation_contract,
    validate_lazarus_evaluation_contract,
)
from .lazarus_goldens import validate_lazarus_golden_set
from .lazarus_mining import (
    distinguishing_expected_values,
    validate_upstream_record,
    verify_public_text_has_no_hidden_tokens,
)
from .task_descriptions import canonical_state_description


LAZARUS_BENCHMARK_IDS = (
    "luanti",
    "blender",
    "jupyterlab",
    "openra",
    "jynew",
    "pal3",
)
# Blender reached the candidate pool but never had a row admitted, so the formal
# v1 population is drawn from three repositories.  It stays in
# ``LAZARUS_BENCHMARK_IDS`` because frozen mining and survey evidence still
# references it.
LAZARUS_FORMAL_BENCHMARK_IDS = (
    "luanti",
    "jupyterlab",
    "openra",
)
LAZARUS_VERSIONS = ("buggy", "fixed")
LAZARUS_ARMS = ("lazarus_state", "lazarus_agent_s")

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# ``lz-`` rows are graded from a fail-to-pass differential.  ``lzp-`` rows are the
# P2P-only population and carry none, so the prefix keeps the two apart in every
# report that lists a task id.
_TASK_ID = re.compile(r"^lzp?-[a-z0-9]+(?:-[a-z0-9]+)*$")
_BUILD_CLASSES = frozenset(
    {"none", "jlpm", "incremental_cpp", "incremental_dotnet", "unity"}
)
_DEFECT_LOCI = frozenset({"at_goal", "on_path"})
_DEFECT_HINT = re.compile(
    r"\b(?:bug|buggy|broken|defect|failure|fixed|fix|regression|reverted)\b",
    re.IGNORECASE,
)


def validate_lazarus_catalog(
    payload: Mapping[str, Any],
    *,
    source_path: str | Path,
) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]]]:
    """Validate the private Lazarus catalog and every referenced artifact.

    The return shape intentionally mirrors :func:`validate_c1_v5_catalog` so
    trusted loaders can adopt the new schema without changing public task
    objects.
    """

    source = Path(source_path).expanduser().resolve()
    root = _repository_root(source)
    if payload.get("schema_version") != 1 or payload.get("task_schema") != "c1_lazarus_v1":
        raise ValueError(
            f"{source}: Lazarus catalog must be c1_lazarus_v1/schema_version 1"
        )

    raw_hosts = payload.get("benchmark_hosts")
    if not isinstance(raw_hosts, Mapping):
        raise ValueError(f"{source}: c1_lazarus_v1 requires benchmark_hosts")
    formal_population = payload.get("formal_population") is True
    # The P2P-only population is a third, disjoint set.  It declares only the
    # hosts it actually draws from, because it is not sized against the formal
    # matrix and must never be aggregated with it.
    p2p_only_population = payload.get("p2p_only_population") is True
    if formal_population and p2p_only_population:
        raise ValueError(
            f"{source}: a catalog cannot be both the formal and the P2P-only "
            "population"
        )
    if p2p_only_population:
        declared = tuple(raw_hosts)
        unknown = [host for host in declared if host not in LAZARUS_FORMAL_BENCHMARK_IDS]
        if not declared or unknown:
            raise ValueError(
                f"{source}: P2P-only benchmark_hosts must be a non-empty subset of "
                f"{', '.join(LAZARUS_FORMAL_BENCHMARK_IDS)}"
            )
        if declared != tuple(
            host for host in LAZARUS_FORMAL_BENCHMARK_IDS if host in set(declared)
        ):
            raise ValueError(
                f"{source}: P2P-only benchmark_hosts must keep the registered order"
            )
    else:
        expected_hosts = (
            LAZARUS_FORMAL_BENCHMARK_IDS
            if formal_population
            else LAZARUS_BENCHMARK_IDS
        )
        if tuple(raw_hosts) != expected_hosts:
            population = "formal" if formal_population else "Phase-0"
            raise ValueError(
                f"{source}: {population} benchmark_hosts must list "
                f"{', '.join(expected_hosts)} in order"
            )
    hosts = _validate_hosts(raw_hosts, source)

    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list) or not all(
        isinstance(task, Mapping) for task in raw_tasks
    ):
        raise ValueError(f"{source}: Lazarus catalog must contain a tasks list")
    tasks = [copy.deepcopy(dict(task)) for task in raw_tasks]
    seen: set[str] = set()
    for row in tasks:
        task_id = str(row.get("id") or "").strip()
        if not _TASK_ID.fullmatch(task_id) or task_id in seen:
            raise ValueError(
                f"{source}: Lazarus task ids must be unique lz-* slugs"
            )
        seen.add(task_id)
        benchmark = str(row.get("benchmark") or "").strip()
        if benchmark not in hosts:
            raise ValueError(
                f"{source}: task {task_id} uses unknown benchmark {benchmark!r}"
            )
        if str(row.get("repo") or "").strip() != hosts[benchmark]["repo"]:
            raise ValueError(
                f"{source}: task {task_id} repo does not match benchmark_hosts"
            )
        _validate_public_contract(row, task_id, benchmark, source)
        defect, upstream = _validate_defect_contract(row, task_id, root, source)
        spec_path = _artifact_path(
            root,
            row.get("oracle_spec"),
            label=f"task {task_id} oracle_spec",
            source=source,
        )
        spec = _load_yaml_mapping(spec_path, f"task {task_id} oracle_spec")
        if str(spec.get("benchmark") or "").strip() != benchmark:
            raise ValueError(
                f"{source}: task {task_id} oracle benchmark does not match catalog"
            )
        contract_errors = validate_evaluation_contract(
            spec, spec_path, allow_lazarus=True
        )
        if contract_errors:
            raise ValueError(
                f"{source}: task {task_id} has invalid Lazarus oracle: "
                + "; ".join(contract_errors)
            )
        _validate_golden_files(spec, spec_path, source, task_id)
        _validate_test_ids(spec, upstream, source, task_id)
        _validate_public_leakage(row, spec, upstream, source, task_id)
        # Keep the copied coordinator block normalized for downstream trusted
        # objects without mutating the caller's parsed YAML.
        row["defect"] = defect
    return hosts, tasks


def materialize_lazarus_spec(
    spec: Mapping[str, Any], *, view: str
) -> dict[str, Any]:
    """Project one Lazarus verdict onto the existing semantic/visual runtime API."""

    if view not in {"goal", "defect"}:
        raise ValueError("Lazarus view must be 'goal' or 'defect'")
    errors = validate_lazarus_evaluation_contract(spec, Path("<memory>"))
    if errors:
        raise ValueError("invalid Lazarus evaluation contract: " + "; ".join(errors))

    result = copy.deepcopy(dict(spec))
    evaluation = result["evaluation"]
    lazarus = evaluation["lazarus"]
    selected_key = "goal_checks" if view == "goal" else "defect_signature"
    assertions = copy.deepcopy(
        lazarus[selected_key] + lazarus["invariant_checks"]
    )
    evaluation["semantic"]["assertions"] = copy.deepcopy(assertions)

    version = "fixed" if view == "goal" else "buggy"
    branch = lazarus["goldens"][version]
    visual = copy.deepcopy(evaluation["visual_oracle"])
    visual.update(
        {
            "golden_image": branch["after"],
            "golden_before": lazarus["goldens"]["before"],
            "golden_after": branch["after"],
            "after_description": branch["after_description"],
            "after_rubric": copy.deepcopy(branch["after_rubric"]),
        }
    )
    evaluation["visual_oracle"] = copy.deepcopy(visual)
    result["assertions"] = assertions
    result["visual_oracle"] = visual
    return result


def _validate_hosts(
    raw_hosts: Mapping[str, Any], source: Path
) -> dict[str, dict[str, str]]:
    hosts: dict[str, dict[str, str]] = {}
    repos: set[str] = set()
    revisions: set[str] = set()
    for benchmark, value in raw_hosts.items():
        if not isinstance(value, Mapping):
            raise ValueError(f"{source}: host {benchmark} must be a mapping")
        host = {
            key: str(value.get(key) or "").strip()
            for key in ("display_name", "repo", "gitmodules_url", "revision")
        }
        missing = [key for key, item in host.items() if not item]
        if missing:
            raise ValueError(
                f"{source}: host {benchmark} is missing {', '.join(missing)}"
            )
        if not _FULL_SHA.fullmatch(host["revision"]):
            raise ValueError(f"{source}: host {benchmark} revision must be a full SHA")
        if host["repo"] in repos or host["revision"] in revisions:
            raise ValueError(f"{source}: host repos and revisions must be unique")
        repos.add(host["repo"])
        revisions.add(host["revision"])
        hosts[str(benchmark)] = host
    return hosts


def _validate_public_contract(
    row: Mapping[str, Any], task_id: str, benchmark: str, source: Path
) -> None:
    values = {
        key: str(row.get(key) or "").strip()
        for key in (
            "preconditions",
            "reference_steps",
            "state_description",
            "prompt_revision",
            "horizon_class",
        )
    }
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise ValueError(f"{source}: task {task_id} is missing {', '.join(missing)}")
    if "goal_description" in row:
        raise ValueError(f"{source}: task {task_id} must not expose goal_description")
    expected_description = canonical_state_description(
        values["preconditions"], values["reference_steps"]
    )
    if values["state_description"] != expected_description:
        raise ValueError(
            f"{source}: task {task_id} state_description is not canonical"
        )
    if values["horizon_class"] not in {"short", "mid", "long"}:
        raise ValueError(f"{source}: task {task_id} has invalid horizon_class")
    if row.get("random_control") != expected_random_control(benchmark):
        raise ValueError(
            f"{source}: task {task_id} random_control must equal "
            f"{expected_random_control(benchmark)!r}"
        )
    if row.get("budget_ms") != 2_400_000:
        raise ValueError(f"{source}: task {task_id} budget_ms must be 2400000")
    tags = row.get("tags")
    if not isinstance(tags, list) or not {"c1", "lazarus", benchmark} <= {
        str(tag) for tag in tags
    }:
        raise ValueError(
            f"{source}: task {task_id} tags must include c1, lazarus, {benchmark}"
        )
    capabilities = row.get("capabilities")
    if not isinstance(capabilities, list) or not all(
        isinstance(value, str) and value.strip() for value in capabilities
    ):
        raise ValueError(f"{source}: task {task_id} capabilities must be a string list")


def _validate_defect_contract(
    row: Mapping[str, Any], task_id: str, root: Path, source: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = row.get("defect")
    if not isinstance(raw, Mapping):
        raise ValueError(f"{source}: task {task_id} requires private defect metadata")
    defect = copy.deepcopy(dict(raw))
    required = {
        "upstream",
        "fix_commit",
        "revert_patch",
        "patch_sha256",
        "build_class",
        "defect_locus",
        "gate_report",
    }
    allowed = required | {"upstream_record"}
    if set(defect) - allowed or required - set(defect):
        raise ValueError(
            f"{source}: task {task_id} defect keys do not match c1_lazarus_v1"
        )
    upstream_links = defect.get("upstream")
    if not isinstance(upstream_links, Mapping) or not upstream_links:
        raise ValueError(f"{source}: task {task_id} defect.upstream is required")
    for key, value in upstream_links.items():
        if key not in {"issue", "pr"} or not str(value).startswith("https://"):
            raise ValueError(f"{source}: task {task_id} has invalid upstream URL")
    if not _FULL_SHA.fullmatch(str(defect.get("fix_commit") or "")):
        raise ValueError(f"{source}: task {task_id} fix_commit must be a full SHA")
    if defect.get("build_class") not in _BUILD_CLASSES:
        raise ValueError(f"{source}: task {task_id} has invalid build_class")
    if defect.get("defect_locus") not in _DEFECT_LOCI:
        raise ValueError(f"{source}: task {task_id} has invalid defect_locus")

    patch_path = _artifact_path(
        root,
        defect.get("revert_patch"),
        label=f"task {task_id} revert_patch",
        source=source,
    )
    actual_patch_sha = hashlib.sha256(patch_path.read_bytes()).hexdigest()
    declared_patch_sha = str(defect.get("patch_sha256") or "")
    if not _SHA256.fullmatch(declared_patch_sha) or declared_patch_sha != actual_patch_sha:
        raise ValueError(f"{source}: task {task_id} patch_sha256 mismatch")

    upstream_value = defect.get("upstream_record")
    upstream_path = (
        _artifact_path(
            root,
            upstream_value,
            label=f"task {task_id} upstream_record",
            source=source,
        )
        if upstream_value is not None
        else patch_path.with_name("upstream.json")
    )
    if not upstream_path.is_file():
        raise ValueError(f"{source}: task {task_id} is missing upstream.json")
    upstream = _load_json_mapping(upstream_path, f"task {task_id} upstream record")
    upstream_errors = validate_upstream_record(upstream)
    if upstream_errors:
        raise ValueError(
            f"{source}: task {task_id} upstream record is invalid: "
            + "; ".join(upstream_errors)
        )

    gate_path = _artifact_path(
        root,
        defect.get("gate_report"),
        label=f"task {task_id} gate_report",
        source=source,
    )
    gate = _load_json_mapping(gate_path, f"task {task_id} gate report")
    if (
        gate.get("schema_version") != 1
        or gate.get("task_id") != task_id
        or gate.get("status") != "passed"
    ):
        raise ValueError(f"{source}: task {task_id} gate report is not passed v1")
    return defect, upstream


def _validate_golden_files(
    spec: Mapping[str, Any], spec_path: Path, source: Path, task_id: str
) -> None:
    goldens = spec["evaluation"]["lazarus"]["goldens"]
    # A registered task must show a reviewer both start frames, so the optional
    # contract key is mandatory once the task enters a catalog.
    if "buggy_before" not in goldens:
        raise ValueError(
            f"{source}: task {task_id} does not publish the buggy before golden"
        )
    paths = [
        goldens["before"],
        goldens["buggy_before"],
        goldens["fixed"]["after"],
        goldens["buggy"]["after"],
    ]
    resolved_paths: list[Path] = []
    for value in paths:
        path = (spec_path.parent / value).resolve()
        if spec_path.parent != path and spec_path.parent not in path.parents:
            raise ValueError(
                f"{source}: task {task_id} golden escapes oracle directory"
            )
        if not path.is_file():
            raise ValueError(f"{source}: task {task_id} missing golden {value}")
        resolved_paths.append(path)
    parent_dirs = {path.parent for path in resolved_paths}
    if len(parent_dirs) != 1:
        raise ValueError(
            f"{source}: task {task_id} Lazarus goldens must share one directory"
        )
    verification = validate_lazarus_golden_set(
        next(iter(parent_dirs)) / "goldens.meta.json",
        task_id=task_id,
    )
    if not verification.complete:
        raise ValueError(
            f"{source}: task {task_id} golden provenance is invalid: "
            + "; ".join(verification.failures)
        )


def _validate_test_ids(
    spec: Mapping[str, Any],
    upstream: Mapping[str, Any],
    source: Path,
    task_id: str,
) -> None:
    records = upstream.get("tests")
    if not isinstance(records, list):
        raise ValueError(f"{source}: task {task_id} upstream tests must be a list")
    known = {
        str(record.get("id"))
        for record in records
        if isinstance(record, Mapping) and record.get("id")
    }
    declared = spec["evaluation"]["lazarus"]["upstream_tests"]
    missing = {
        test_id
        for values in declared.values()
        for test_id in values
        if test_id not in known
    }
    if missing:
        raise ValueError(
            f"{source}: task {task_id} references unknown upstream tests "
            f"{sorted(missing)}"
        )


def _validate_public_leakage(
    row: Mapping[str, Any],
    spec: Mapping[str, Any],
    upstream: Mapping[str, Any],
    source: Path,
    task_id: str,
) -> None:
    public_text = "\n".join(
        str(row.get(key) or "")
        for key in ("preconditions", "reference_steps", "state_description")
    )
    if _DEFECT_HINT.search(public_text):
        raise ValueError(f"{source}: task {task_id} public text hints at a defect")
    normalized_public = _normalize(public_text)
    for literal in _hidden_expected_literals(spec):
        token = _normalize(literal)
        if len(token) >= 4 and token in normalized_public:
            raise ValueError(
                f"{source}: task {task_id} public text leaks hidden assertion "
                f"value {literal!r}"
            )
    touched = upstream.get("touched_files")
    if isinstance(touched, list):
        public_lower = public_text.lower()
        for value in touched:
            path = str(value or "").strip().lower()
            if not path:
                continue
            basename = Path(path).name
            if path in public_lower or (len(basename) >= 5 and basename in public_lower):
                raise ValueError(
                    f"{source}: task {task_id} public text leaks fix location {value!r}"
                )
    else:
        raise ValueError(f"{source}: task {task_id} upstream touched_files must be a list")
    try:
        verify_public_text_has_no_hidden_tokens(
            public_text,
            spec=spec,
            touched_files=tuple(str(value) for value in touched),
        )
    except ValueError as error:
        raise ValueError(f"{source}: task {task_id} {error}") from error


def _hidden_expected_literals(spec: Mapping[str, Any]) -> set[str]:
    # Values that goal_checks and defect_signature both expect hold in either
    # version, so they describe the registered start instead of the answer.
    spec["evaluation"]["lazarus"]  # fail closed when the extension is absent
    return distinguishing_expected_values(spec)


def _artifact_path(
    root: Path,
    value: Any,
    *,
    label: str,
    source: Path,
) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source}: {label} must be a non-empty path")
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError(f"{source}: {label} escapes repository root")
    path = (root / relative).resolve()
    if root != path and root not in path.parents:
        raise ValueError(f"{source}: {label} escapes repository root")
    if not path.is_file():
        raise ValueError(f"{source}: {label} does not exist: {path}")
    return path


def _repository_root(source: Path) -> Path:
    for parent in (source.parent, *source.parents):
        if (parent / "pyproject.toml").is_file() or (parent / ".git").exists():
            return parent.resolve()
    raise ValueError(f"{source}: cannot locate repository root")


def _load_yaml_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"{label} cannot be loaded: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _load_json_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} cannot be loaded: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())
