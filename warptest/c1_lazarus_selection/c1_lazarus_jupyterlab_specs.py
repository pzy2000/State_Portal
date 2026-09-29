"""Emit executable JupyterLab lazarus task specs for the frozen catalog rows.

Each spec states the same contract the Luanti and OpenRA lazarus specs state: the
public start, the observation surface, the visual oracle, and the lazarus block
whose ``goal_checks`` and ``defect_signature`` are resolved by a real probe.  The
checks are projected from the registered expectation that the frozen Gate 10 oracle
already carries, so a spec cannot claim an outcome the closure did not observe.

A task is only written as executable when :mod:`warptest.lazarus_jupyterlab_probe`
can re-run its replay; otherwise the spec records why the probe binding is absent
instead of pretending the checks can run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..lazarus_jupyterlab_probe import (
    PROBE_ADAPTER,
    PROBE_ERROR_CHECK,
    PROBE_STRATEGY,
    JupyterLabProbeBinding,
    evaluate_jupyterlab_lazarus_audit,
    evaluate_jupyterlab_lazarus_checks,
    jupyterlab_lazarus_defect_signature,
    jupyterlab_lazarus_goal_checks,
    probe_bindings,
    run_jupyterlab_lazarus_probe,
)
from ..spec_schema import ensure_valid_spec_schema
from .c1_lazarus_golden_publication import _TASK_IDS
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_TIMEOUT_MS = 900_000
_TAGS = ("c1", "lazarus", "jupyterlab", "production")
_UNBOUND_REASON: dict[int, str] = {}
_SPECS = ArtifactReference(
    "c1_lazarus_jupyterlab_task_specs/lazarus/v2-roster/current-v1",
    "c1_lazarus_jupyterlab_task_specs",
    "1.0",
)


@dataclass(frozen=True)
class WrittenSpec:
    task_id: str
    change_number: int
    path: Path
    executable: bool
    reason: str | None


def write_jupyterlab_task_specs(
    artifact_root: str | Path,
    *,
    repository_root: str | Path,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Write one spec per frozen JupyterLab row and validate every one of them."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("an active infrastructure-audit authorization is required")
    root = Path(artifact_root).absolute().resolve(strict=True)
    repo = Path(repository_root).absolute().resolve(strict=True)
    bindings = probe_bindings()
    written: list[WrittenSpec] = []
    for (repository_key, change_number), task_id in sorted(
        _TASK_IDS.items(), key=lambda item: item[0][1]
    ):
        if repository_key != "jupyterlab":
            continue
        binding = bindings.get(task_id)
        manifest_path = repo / "specs/jupyterlab/lazarus" / f"{task_id}.goldens.yaml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        document = _spec_document(
            task_id=task_id,
            change_number=change_number,
            manifest=manifest,
            binding=binding,
        )
        path = repo / "specs/jupyterlab/lazarus" / f"{task_id}.yaml"
        path.write_text(
            yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        ensure_valid_spec_schema(document, path, allow_lazarus=True)
        written.append(
            WrittenSpec(
                task_id=task_id,
                change_number=change_number,
                path=path,
                executable=binding is not None,
                reason=None if binding is not None else _UNBOUND_REASON.get(
                    change_number, "no offline probe binding is registered"
                ),
            )
        )
    payload = {
        "state": "passed",
        "complete": True,
        "spec_count": len(written),
        "executable_spec_count": sum(1 for item in written if item.executable),
        "probe_adapter": PROBE_ADAPTER,
        "checkpoint_strategy": PROBE_STRATEGY,
        "specs": [
            {
                "task_id": item.task_id,
                "change_number": item.change_number,
                "path": str(item.path.relative_to(repo)),
                "probe_executable": item.executable,
                "unbound_reason": item.reason,
            }
            for item in written
        ],
        "schema_validated": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    write_stable_artifact(
        root,
        artifact_id=_SPECS.artifact_id,
        artifact_kind=_SPECS.artifact_kind,
        semantic_version=_SPECS.semantic_version,
        created_at=_now(),
        inputs=(),
        payload=payload,
        authorization=authorization,
    )
    return load_stable_artifact(
        root,
        artifact_id=_SPECS.artifact_id,
        artifact_kind=_SPECS.artifact_kind,
        semantic_version=_SPECS.semantic_version,
    )


def verify_jupyterlab_task_specs(
    artifact_root: str | Path,
    *,
    repository_root: str | Path,
    authorization: AuthorizationRecord,
    task_ids: tuple[str, ...] | None = None,
) -> StableArtifact:
    """Execute every bound spec's checks against a fresh probe of both versions.

    The verdicts are read from the written spec files, so this proves the specs
    themselves are executable rather than re-deriving the expectation in Python.
    """

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError(
            "active preannotation-execution authorization is required"
        )
    root = Path(artifact_root).absolute().resolve(strict=True)
    repo = Path(repository_root).absolute().resolve(strict=True)
    bindings = probe_bindings()
    results: list[dict[str, Any]] = []
    for task_id, binding in sorted(bindings.items()):
        if task_ids is not None and task_id not in task_ids:
            continue
        path = repo / "specs/jupyterlab/lazarus" / f"{task_id}.yaml"
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        lazarus = spec["evaluation"]["lazarus"]
        row: dict[str, Any] = {
            "task_id": task_id,
            "change_number": binding.change_number,
            "spec_path": str(path.relative_to(repo)),
            "variants": {},
        }
        for variant in ("fixed", "buggy"):
            audit = run_jupyterlab_lazarus_probe(
                root, task_id=task_id, variant=variant, bindings=bindings
            )
            verdict = evaluate_jupyterlab_lazarus_audit(audit)
            goal_ok, goal_failures = evaluate_jupyterlab_lazarus_checks(
                lazarus["goal_checks"], audit
            )
            defect_ok, defect_failures = evaluate_jupyterlab_lazarus_checks(
                lazarus["defect_signature"], audit
            )
            invariant_ok, invariant_failures = evaluate_jupyterlab_lazarus_checks(
                lazarus["invariant_checks"], audit
            )
            row["variants"][variant] = {
                "audit_complete": verdict.complete,
                "audit_failures": list(verdict.failures),
                "goal_checks_ok": goal_ok,
                "goal_failures": list(goal_failures),
                "defect_signature_ok": defect_ok,
                "defect_failures": list(defect_failures),
                "invariant_checks_ok": invariant_ok,
                "invariant_failures": list(invariant_failures),
                "browser_version": audit.get("browser_version"),
                "frames": dict(audit.get("frames") or {}),
                "probe_errors": list(audit.get("probe_errors") or []),
            }
        fixed = row["variants"]["fixed"]
        buggy = row["variants"]["buggy"]
        row["separates_goal_from_defect"] = bool(
            fixed["audit_complete"]
            and buggy["audit_complete"]
            and fixed["goal_checks_ok"]
            and not fixed["defect_signature_ok"]
            and buggy["defect_signature_ok"]
            and not buggy["goal_checks_ok"]
            and fixed["invariant_checks_ok"]
            and buggy["invariant_checks_ok"]
        )
        results.append(row)
    passed = bool(results) and all(row["separates_goal_from_defect"] for row in results)
    reference = ArtifactReference(
        "c1_lazarus_jupyterlab_spec_verification/lazarus/v2-roster/current-v1",
        "c1_lazarus_jupyterlab_spec_verification",
        "1.0",
    )
    payload = {
        "state": "passed" if passed else "blocked",
        "complete": True,
        "verified_task_count": len(results),
        "all_specs_separate_goal_from_defect": passed,
        "probe_adapter": PROBE_ADAPTER,
        "provider_calls_performed": False,
        "tasks": results,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=(),
        payload=payload,
        authorization=authorization,
    )
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _spec_document(
    *,
    task_id: str,
    change_number: int,
    manifest: Mapping[str, Any],
    binding: JupyterLabProbeBinding | None,
) -> dict[str, Any]:
    """Assemble one spec from the frozen manifest and the registered replay.

    The visual oracle and the golden rubrics are copied from the task's goldens
    manifest so a spec and its manifest cannot describe different expectations.
    """

    evaluation = manifest.get("evaluation") or {}
    visual_oracle = evaluation.get("visual_oracle")
    manifest_lazarus = evaluation.get("lazarus") or {}
    goldens = manifest_lazarus.get("goldens")
    if not isinstance(visual_oracle, Mapping) or not isinstance(goldens, Mapping):
        raise ValueError(f"{task_id} manifest carries no oracle or goldens")
    candidate = manifest.get("candidate") or {}
    goal_checks = (
        jupyterlab_lazarus_goal_checks(binding)
        if binding is not None
        else [{"type": PROBE_ERROR_CHECK}]
    )
    defect_signature = (
        jupyterlab_lazarus_defect_signature(binding)
        if binding is not None
        else [{"type": PROBE_ERROR_CHECK}]
    )
    probe: dict[str, Any] = {
        "task_id": task_id,
        "change_number": change_number,
        "issue_number": candidate.get("issue_number"),
        "adapter": PROBE_ADAPTER,
        "registered_replay": list(binding.replay_steps) if binding is not None else [],
        "probe_executable": binding is not None,
    }
    if binding is None:
        probe["unbound_reason"] = _UNBOUND_REASON.get(
            change_number, "no offline probe binding is registered"
        )
    return {
        "name": f"C1-Lazarus JupyterLab {task_id.removeprefix('lz-jupyterlab-')}",
        "benchmark": "jupyterlab",
        "timeout_ms": _TIMEOUT_MS,
        "tags": list(_TAGS),
        "setup": {"jupyterlab_lazarus_probe": probe},
        "target": {
            "kind": "jupyterlab_lazarus_candidate_bundle",
            "change_number": change_number,
            "variants": ["fixed", "buggy"],
            "bundler": "candidate_local_rspack",
            "browser": "microsoft_edge_via_playwright",
        },
        "checkpoint": {
            "allowed_strategies": [PROBE_STRATEGY],
            "expected_validity": "replay-reachable",
        },
        "actions": [],
        "evaluation": {
            "schema_version": 1,
            "semantic": {
                "assertions": [],
                "surfaces": {
                    surface: {
                        "adapter": PROBE_ADAPTER,
                        "start": {"validate_target": True, "checks": []},
                        "goal": {"use_assertions": True, "checks": []},
                    }
                    for surface in ("gui", "state")
                },
            },
            "visual_oracle": _plain(visual_oracle),
            "lazarus": {
                "schema_version": 1,
                "goal_checks": goal_checks,
                "defect_signature": defect_signature,
                "invariant_checks": [{"type": PROBE_ERROR_CHECK}],
                "upstream_tests": _upstream_tests(manifest_lazarus),
                "goldens": _plain(goldens),
            },
        },
    }


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _upstream_tests(manifest_lazarus: Mapping[str, Any]) -> dict[str, list[str]]:
    """Reuse the manifest's upstream ids and derive the per-trial P2P subset."""

    upstream = manifest_lazarus.get("upstream_tests") or {}
    fail = [str(value) for value in (upstream.get("fail_to_pass") or [])]
    passing = [str(value) for value in (upstream.get("pass_to_pass") or [])]
    return {
        "fail_to_pass": fail,
        "pass_to_pass": passing,
        "per_trial_p2p": passing,
    }


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "WrittenSpec",
    "verify_jupyterlab_task_specs",
    "write_jupyterlab_task_specs",
]
