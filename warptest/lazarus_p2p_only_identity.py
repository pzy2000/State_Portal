"""Scoped identities for the exploratory P2P-only experiment.

The paid runner used to bind every operational file to one broad Lazarus
harness hash.  That made a viewer edit invalidate native runtime canaries and a
Jupyter-only fix invalidate provider routing.  These identities follow the
actual dependency graph instead: schedule, provider route, grading, viewer,
materialization and each benchmark runtime are independent.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


START_PROTOCOL = "real_cold_start_v1"
PAID_RUNTIME_REVISIONS = {
    "jupyterlab": "jupyter-real-lab-paid-runtime-r3",
    "luanti": "luanti-neutral-menu-paid-runtime-r1",
    "openra": "openra-post-boot-paid-runtime-r1",
}
AGENT_SOURCE_SANITIZER_VERSION = "lazarus-agent-source-sanitizer-v1"

_RUNTIME_SYMBOLS = {
    "jupyterlab": (
        "_JUPYTER_PIXEL_TARGETS",
        "JupyterOnlineSession",
        "run_jupyter_online_transition",
        "_prepare_jupyter_document",
        "_prepare_jupyter_benchmark_extension",
        "_start_jupyterlab_server",
        "_interaction_bridge",
        "_jupyter_start_restore_adapter",
    ),
    "luanti": (
        "LuantiOnlineSession",
        "run_luanti_online_transition",
        "_luanti_public_pixel_sequence",
        "_apply_luanti_registered_transition",
        "_luanti_start_restore_adapter",
    ),
    "openra": (
        "_OPENRA_ONLINE_START_CS",
        "prepare_openra_online_tree",
        "run_openra_online_transition",
        "_openra_start_pixel_sequence",
        "_openra_public_pixel_sequence",
        "_dispatch_openra_pixel_sequence",
        "_openra_online_session",
        "_restore_openra_start",
        "_apply_openra_registered_transition",
        "_openra_start_restore_adapter",
    ),
}


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def schedule_sha256(manifest: Mapping[str, Any]) -> str:
    fields = (
        "schema_version",
        "method_protocol",
        "methods",
        "shared_synthesis",
        "start_gate",
        "cost_accounting",
        "experiment_id",
        "tasks",
        "catalog_sha256",
        "task_count",
        "expected_cells",
        "formal",
        "confirmatory",
        "paper_ready",
        "models",
        "model_request_profiles",
        "api",
        "observer_model",
        "grounder_model",
        "judge_model",
        "arms",
        "versions",
        "seeds",
        "provider_timeout_s",
        "task_timeout_s",
        "per_trial_p2p_timeout_s",
        "outer_workers",
        "start_protocol",
        "decision_budget",
        "cold_start_bindings",
        "jupyter_benchmark_extension",
        "scheduler",
        "statistics",
        "authorization",
    )
    return canonical_sha256({field: manifest.get(field) for field in fields})


def provider_profile_sha256(root: Path, manifest: Mapping[str, Any]) -> str:
    from .image_history_backoff import enabled, protocol

    payload = {
            "routes": {
                field: manifest.get(field)
                for field in (
                    "models",
                    "model_request_profiles",
                    "api",
                    "observer_model",
                    "grounder_model",
                    "judge_model",
                    "provider_timeout_s",
                )
            },
            "client_files": {
                name: file_sha256(root / "warptest" / name)
                for name in (
                    "model_request.py",
                    "providers.py",
                    "synthesis.py",
                    "agent_s_bridge.py",
                    "c1_task_deadline.py",
                    "cost.py",
                    "visual_bridge.py",
                )
            },
        }
    # Preserve the original identity when the opt-in variation is disabled.
    if enabled():
        payload["image_history_backoff"] = protocol()
        payload["image_history_backoff_files"] = {
            name: file_sha256(root / "warptest" / name)
            for name in ("image_history_backoff.py", "agent_s_worker.py")
        }
    return canonical_sha256(payload)


def grading_sha256(root: Path) -> str:
    files = {
        name: file_sha256(root / "warptest" / name)
        for name in (
            "lazarus_grading.py",
            "visual.py",
            "lazarus_trial.py",
            "lazarus_p2p_only_executor.py",
            "lazarus_upstream.py",
        )
    }
    # Rubrics are executable grading inputs, not presentation. Editing one must
    # invalidate previous calibration/readiness even if Python is unchanged.
    files["p2p_only_oracles"] = {
        str(path.relative_to(root)): file_sha256(path)
        for host in ("jupyterlab", "luanti", "openra")
        for path in sorted((root / "specs" / host / "lazarus_p2p_only").glob("lzp-*.yaml"))
    }
    return canonical_sha256(files)


def orchestrator_sha256(root: Path) -> str:
    return _ast_symbol_sha256(
        root / "warptest/lazarus_p2p_only_experiment.py",
        (
            "registered_schedule",
            "validate_p2p_only_manifest",
            "build_p2p_only_plan",
            "run_p2p_only_groups",
            "write_run_index",
            "write_run_report",
            "_load_resume_groups",
            "_validate_p2p_only_cell",
            "_materialize_profile_pair",
            "_cached_agent_source",
            "_write_pair_receipt",
        ),
    )


def treatment_runtime_sha256(root: Path) -> str:
    return canonical_sha256(
        {
            "paid_runner": _ast_symbol_sha256(
                root / "warptest/lazarus_p2p_only_paid_runtime.py",
                ("P2POnlyPaidRuntimeRunner",),
            ),
            "public_prompts": _ast_symbol_sha256(
                root / "warptest/lazarus_runtime.py",
                (
                    "_agent_s_start_instruction",
                    "_agent_s_reference_steps_prompt",
                    "_public_task_text",
                    "_assert_public_instruction",
                ),
            ),
            "policy_loop": _ast_symbol_sha256(
                root / "warptest/gui_agent.py",
                (
                    "_build_policy_client",
                    "_run_phase",
                    "_apply_action",
                    "_apply_action_batch",
                ),
            ),
            "generated_modules": {
                name: file_sha256(root / "warptest" / name)
                for name in (
                    "lazarus_p2p_only_contract.py",
                    "lazarus_p2p_only_spec.py",
                    "lazarus_p2p_only_schema.py",
                    "lazarus_p2p_only_synthesis.py",
                    "lazarus_p2p_only_recovery.py",
                    "lazarus_p2p_only_generated_runtime.py",
                    "lazarus_p2p_only_components.js",
                    "lazarus_p2p_only_native.py",
                    "synthesis.py",
                )
            },
        }
    )


def materialization_sha256(root: Path) -> str:
    return canonical_sha256(
        {
            "sanitizer_version": AGENT_SOURCE_SANITIZER_VERSION,
            "phase0_source": _ast_symbol_sha256(
                root / "warptest/lazarus_phase0.py",
                (
                    "prepare_lazarus_agent_source",
                    "_remove_git_ignored_paths",
                    "_assert_agent_source_safe",
                ),
            ),
            "version_materializer": file_sha256(
                root / "warptest/c1_lazarus_selection/p2p_only_gate16.py"
            ),
        }
    )


def runtime_sha256(root: Path, benchmark: str) -> str:
    if benchmark not in _RUNTIME_SYMBOLS:
        raise ValueError(f"unknown P2P-only runtime benchmark: {benchmark}")
    runtime = root / "warptest/lazarus_p2p_only_runtime.py"
    return canonical_sha256(
        {
            "benchmark": benchmark,
            "start_protocol": START_PROTOCOL,
            "paid_runtime_revision": PAID_RUNTIME_REVISIONS[benchmark],
            "generated_runtime": treatment_runtime_sha256(root),
            "adapter_ast": _ast_symbol_sha256(runtime, _RUNTIME_SYMBOLS[benchmark]),
        }
    )


def runtime_identities(root: Path) -> dict[str, str]:
    return {
        benchmark: runtime_sha256(root, benchmark)
        for benchmark in ("jupyterlab", "luanti", "openra")
    }


def scoped_identities(root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schedule_sha256": schedule_sha256(manifest),
        "catalog_sha256": str(manifest.get("catalog_sha256") or ""),
        "runtime_sha256": runtime_identities(root),
        "grading_sha256": grading_sha256(root),
        "orchestrator_sha256": orchestrator_sha256(root),
        "treatment_runtime_sha256": treatment_runtime_sha256(root),
        "provider_profile_sha256": provider_profile_sha256(root, manifest),
        "materialization_sha256": materialization_sha256(root),
    }


def _ast_symbol_sha256(path: Path, names: Sequence[str]) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    selected: dict[str, str] = {}
    wanted = set(names)
    for node in tree.body:
        name = getattr(node, "name", None)
        if isinstance(name, str) and name in wanted:
            selected[name] = ast.dump(
                node, annotate_fields=True, include_attributes=False
            )
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in wanted:
                    selected[target.id] = ast.dump(
                        node, annotate_fields=True, include_attributes=False
                    )
    missing = sorted(wanted - set(selected))
    if missing:
        raise ValueError(f"scoped identity symbols are missing in {path}: {missing}")
    return canonical_sha256(selected)


__all__ = [
    "AGENT_SOURCE_SANITIZER_VERSION",
    "PAID_RUNTIME_REVISIONS",
    "START_PROTOCOL",
    "canonical_sha256",
    "file_sha256",
    "grading_sha256",
    "materialization_sha256",
    "orchestrator_sha256",
    "provider_profile_sha256",
    "runtime_identities",
    "runtime_sha256",
    "schedule_sha256",
    "scoped_identities",
    "treatment_runtime_sha256",
]
