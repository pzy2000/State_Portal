"""Static packaging blocker and recovery plan for Luanti #14757.

The current-v2 #14757 application pair exists and the typed Gate 1--7
aggregate is qualified, but the previous bounded probe stopped in macOS dyld
before the application reached ``main``.  This module deliberately performs
no process launch and no signing operation.  It binds the existing evidence,
checks the pair as ordinary files, and writes a resumable non-terminal plan
for a *fresh* clean repackage/re-sign namespace.

The plan is downstream evidence only.  It must never be interpreted as a
runtime Start, replay, semantic observation, golden, visual, or lineage
receipt, and it cannot promote a candidate to the frozen catalog.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 14757}
_V2 = "2.0"
_V3 = "3.0"
_V4 = "4.0"
_V1 = "1.0"

_GATE_AGGREGATE = ArtifactReference(
    "lua_regression_aggregate/luanti/14757/fixed-buggy/v3",
    "lua_regression_aggregate",
    _V3,
)
_GATE_TRANSITION = ArtifactReference(
    "luanti_lua_regression_transition/luanti/14757/test-repetitions/v2",
    "luanti_lua_regression_transition",
    _V2,
)
_DOWNSTREAM_STATUS = ArtifactReference(
    "candidate_downstream_status/luanti/14757/current/v2",
    "candidate_downstream_status",
    _V2,
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/14757/lz-luanti-14757-math-round/v4",
    "lazarus_task_mapping",
    _V4,
)
_PREFLIGHT = ArtifactReference(
    "downstream_preflight/luanti/14757/runtime-visual/v4",
    "downstream_preflight",
    _V4,
)
_POSTGATE_AUDIT = ArtifactReference(
    "candidate_downstream_recovery_audit/luanti/14757/runtime-visual/v1",
    "candidate_downstream_recovery_audit",
    _V1,
)
_OLD_TERMINAL = ArtifactReference(
    "candidate_sprint_terminal/luanti/14757/runtime-visual-closure/v2",
    "candidate_sprint_terminal",
    _V2,
)
_BLOCKER = ArtifactReference(
    "candidate_downstream_packaging_blocker/luanti/14757/current-v2/v1",
    "candidate_downstream_packaging_blocker",
    _V1,
)
_REPACKAGE_BLOCKER = ArtifactReference(
    "candidate_downstream_repackage_blocker/luanti/14757/repackage-v1/v1",
    "candidate_downstream_repackage_blocker",
    _V1,
)
_REPACKAGE_RECEIPT = ArtifactReference(
    "candidate_downstream_repackage_receipt/luanti/14757/repackage-v2/v1",
    "candidate_downstream_repackage_receipt",
    _V1,
)

_DO_NOT_TOUCH_PIDS = (20946, 21627, 30865, 70361, 60839)
_MISSING_POSTGATE = (
    "registered_start_and_ordered_replay",
    "hidden_oracle_and_real_semantic_observations",
    "common_before_fixed_after_and_buggy_after_goldens",
    "before_visual_equivalence_and_after_2x2_cross_judgment",
    "same_lineage_construction_receipt",
)


@dataclass(frozen=True)
class PackagingBlockerBinding:
    gate_aggregate: StableArtifact
    gate_transition: StableArtifact
    downstream_status: StableArtifact
    mapping: StableArtifact
    preflight: StableArtifact
    postgate_audit: StableArtifact
    old_terminal: StableArtifact
    fixed_bundle: Path
    buggy_bundle: Path
    fixed_binary: Path
    buggy_binary: Path
    fixed_build_log: Path
    buggy_build_log: Path
    fixed_macos_doc: Path
    buggy_macos_doc: Path


def load_luanti_14757_packaging_binding(
    artifact_root: str | Path,
) -> PackagingBlockerBinding:
    """Load and strictly validate the static current-v2 closure.

    This function only reads typed artifacts and filesystem metadata.  It
    intentionally does not call ``codesign``, ``otool``, or either app.
    """

    root = _ordinary_root(Path(artifact_root).absolute())
    gate_aggregate = _load(root, _GATE_AGGREGATE)
    gate_transition = _load(root, _GATE_TRANSITION)
    downstream_status = _load(root, _DOWNSTREAM_STATUS)
    mapping = _load(root, _MAPPING)
    preflight = _load(root, _PREFLIGHT)
    postgate_audit = _load(root, _POSTGATE_AUDIT)
    old_terminal = _load(root, _OLD_TERMINAL)

    _validate_gate_1_7(gate_aggregate, gate_transition, downstream_status)
    _validate_mapping(mapping)
    _validate_preflight(preflight)
    _validate_postgate_audit(postgate_audit)
    _validate_old_terminal(old_terminal)

    fixed_bundle = root / PurePosixPath(
        "private/luanti-14757-formal-materialization/fixed/build-app/out/minetest.app"
    )
    buggy_bundle = root / PurePosixPath(
        "private/luanti-14757-formal-materialization/buggy/build-app/out/minetest.app"
    )
    fixed_binary = fixed_bundle / "Contents/MacOS/minetest"
    buggy_binary = buggy_bundle / "Contents/MacOS/minetest"
    fixed_build_log = (
        root / "private/luanti-14757-formal-materialization/fixed/build-app.formal.log"
    )
    buggy_build_log = (
        root / "private/luanti-14757-formal-materialization/buggy/build-app.formal.log"
    )
    fixed_macos_doc = (
        root
        / "private/luanti-14757-formal-materialization/fixed/doc/compiling/macos.md"
    )
    buggy_macos_doc = (
        root
        / "private/luanti-14757-formal-materialization/buggy/doc/compiling/macos.md"
    )
    for bundle in (fixed_bundle, buggy_bundle):
        _ordinary_directory(bundle, root)
    for binary in (fixed_binary, buggy_binary):
        _ordinary_file(binary, root)
        if not os.access(binary, os.X_OK):
            raise RuntimeError(
                "#14757 materialized application binary is not executable"
            )
    for log in (fixed_build_log, buggy_build_log):
        _ordinary_file(log, root)
    for doc in (fixed_macos_doc, buggy_macos_doc):
        _ordinary_file(doc, root)
        if "codesign --force --deep -s - macos/minetest.app" not in doc.read_text(
            encoding="utf-8"
        ):
            raise RuntimeError("#14757 macOS re-sign instruction is missing")

    return PackagingBlockerBinding(
        gate_aggregate,
        gate_transition,
        downstream_status,
        mapping,
        preflight,
        postgate_audit,
        old_terminal,
        fixed_bundle,
        buggy_bundle,
        fixed_binary,
        buggy_binary,
        fixed_build_log,
        buggy_build_log,
        fixed_macos_doc,
        buggy_macos_doc,
    )


def write_luanti_14757_packaging_blocker(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist a non-terminal clean-repackage/re-sign recovery plan."""

    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    binding = load_luanti_14757_packaging_binding(root)
    audit = binding.postgate_audit.payload
    payload: dict[str, object] = {
        "candidate": dict(_CANDIDATE),
        "state": "downstream_pending",
        "outcome": "infrastructure_blocked",
        "blocker_class": "macos_dyld_uninterruptible_application_launch",
        "blocker_reason": (
            "The complete current-v2 pair was materialized, but the prior "
            "bounded probe entered macOS dyld before main.  The app bundle "
            "signature is inconsistent after install_name_tool fixups and "
            "resource installation, so no Start or replay claim is valid."
        ),
        "admission_gate_1_7_status": "qualified",
        "fixed_buggy_pair_status": "complete_launch_unverified",
        "fixed_bundle_path": str(binding.fixed_bundle.relative_to(root)),
        "buggy_bundle_path": str(binding.buggy_bundle.relative_to(root)),
        "fixed_binary_path": str(binding.fixed_binary.relative_to(root)),
        "buggy_binary_path": str(binding.buggy_binary.relative_to(root)),
        "build_log_paths": [
            str(binding.fixed_build_log.relative_to(root)),
            str(binding.buggy_build_log.relative_to(root)),
        ],
        "static_packaging_evidence": {
            "codesign_check": audit["static_bundle_checks"]["codesign_check"],
            "dyld_dependency_listing": audit["static_bundle_checks"][
                "dyld_dependency_listing"
            ],
            "observed_stack_signature": audit["observed_probe"]["stack_signature"],
            "observed_probe_outcome": audit["observed_probe"]["process_outcome"],
            "observed_probe_state": audit["observed_probe"]["process_state"],
        },
        "repackage_feasibility": {
            "status": "candidate_specific_path_available_not_executed",
            "basis": "local_macos_build_instruction_and_current_fixup_log",
            "documented_command": "codesign --force --deep -s - macos/minetest.app",
            "adapted_command": "codesign --force --deep -s - <fresh-isolated-minetest.app>",
            "documentation_paths": [
                str(binding.fixed_macos_doc.relative_to(root)),
                str(binding.buggy_macos_doc.relative_to(root)),
            ],
            "fixup_order_evidence": (
                "install_name_tool fixups emit signature invalidation warnings "
                "before final resource installation"
            ),
            "existing_build_log_has_codesign_step": False,
            "existing_pair_strict_deep_status": "failed_closed",
            "clean_copy_required": True,
            "nested_resign_required": True,
            "post_sign_static_checks_required": [
                "codesign --verify --strict --deep <fresh-isolated-minetest.app>",
                "codesign --display --verbose=4 <fresh-isolated-minetest.app>",
                "otool -L <fresh-isolated-minetest.app/Contents/MacOS/minetest>",
            ],
            "no_launch_before_static_checks": True,
            "repackage_or_resign_performed": False,
        },
        "candidate_execution_performed": False,
        "registered_replay_executed": False,
        "semantic_observations_complete": False,
        "goldens_complete": False,
        "visual_cross_matrix_complete": False,
        "same_lineage_construction_receipt_complete": False,
        "missing_required_evidence": list(_MISSING_POSTGATE),
        "old_attempts_loaded": False,
        "inherit_prior_attempts": False,
        "permanent_terminal_written": False,
        "terminal": False,
        "resumable": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "do_not_touch_pids": list(_DO_NOT_TOUCH_PIDS),
        "prohibited_actions": [
            "launch_existing_materialized_pair",
            "reuse_or_signal_any_do_not_touch_pid",
            "reuse_historical_runtime_or_probe_receipts",
        ],
        "next_action": {
            "kind": "fresh_clean_repackage_and_resign",
            "namespace": "new_candidate_specific_14757_packaging_v1",
            "steps": [
                "copy fixed and buggy bundles into a new isolated namespace",
                "repair bundle resource sealing after all install_name_tool fixups",
                "ad_hoc_resign_nested_dylibs_then_app_bundle",
                "run static strict-deep signature and dependency checks only",
                "reserve a new bounded launch namespace only after static checks pass",
                "capture Start/replay/oracle/semantic/visual/lineage receipts separately",
            ],
            "launch_performed_by_this_plan": False,
        },
        "binding": {
            "gate_aggregate": binding.gate_aggregate.reference.artifact_id,
            "gate_transition": binding.gate_transition.reference.artifact_id,
            "downstream_status": binding.downstream_status.reference.artifact_id,
            "mapping": binding.mapping.reference.artifact_id,
            "preflight": binding.preflight.reference.artifact_id,
            "postgate_audit": binding.postgate_audit.reference.artifact_id,
            "old_terminal": binding.old_terminal.reference.artifact_id,
        },
        "complete": True,
    }
    write_stable_artifact(
        root,
        artifact_id=_BLOCKER.artifact_id,
        artifact_kind=_BLOCKER.artifact_kind,
        semantic_version=_BLOCKER.semantic_version,
        created_at=_now(),
        inputs=(
            binding.gate_aggregate.reference,
            binding.gate_transition.reference,
            binding.downstream_status.reference,
            binding.mapping.reference,
            binding.preflight.reference,
            binding.postgate_audit.reference,
            binding.old_terminal.reference,
        ),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _BLOCKER)
    if (
        artifact.inputs
        != (
            binding.gate_aggregate.reference,
            binding.gate_transition.reference,
            binding.downstream_status.reference,
            binding.mapping.reference,
            binding.preflight.reference,
            binding.postgate_audit.reference,
            binding.old_terminal.reference,
        )
        or dict(artifact.payload) != payload
    ):
        raise RuntimeError("#14757 packaging blocker typed reload failed")
    return artifact


def write_luanti_14757_repackage_receipt(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    repackage_root: str | Path,
    resign_log: str | Path,
    static_validation_log: str | Path,
) -> StableArtifact:
    """Persist a static-only receipt for a fresh, repaired app pair.

    A successful receipt closes only the copy/resource/signature/dependency
    seam.  It explicitly makes the pair eligible for a *new* downstream Start
    construction, but never claims that Start, replay, semantic observations,
    visuals, or Gate 9--13 have executed.
    """

    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    binding = load_luanti_14757_packaging_binding(root)
    target = _ordinary_directory(Path(repackage_root).absolute(), root)
    resign_log_path = _ordinary_file(Path(resign_log).absolute(), root)
    static_log_path = _ordinary_file(Path(static_validation_log).absolute(), root)

    bundles: dict[str, Path] = {}
    resource_paths: dict[str, str] = {}
    dylib_counts: dict[str, int] = {}
    for variant in ("fixed", "buggy"):
        bundle = _ordinary_directory(target / f"{variant}-minetest.app", root)
        resource_link = bundle / "Contents/Resources/client/shaders/Irrlicht"
        if not resource_link.is_symlink():
            raise RuntimeError(f"#14757 {variant} Irrlicht repair is not a symlink")
        resolved_resource = resource_link.resolve(strict=True)
        try:
            resolved_resource.relative_to(bundle)
        except ValueError as error:
            raise RuntimeError(
                f"#14757 {variant} Irrlicht repair escapes copied bundle"
            ) from error
        if not resolved_resource.is_dir():
            raise RuntimeError(f"#14757 {variant} Irrlicht repair is not a directory")
        dylibs = tuple(
            path
            for path in (bundle / "Contents/Frameworks").rglob("*.dylib")
            if path.is_file()
        )
        if not dylibs:
            raise RuntimeError(f"#14757 {variant} copied bundle has no nested dylibs")
        bundles[variant] = bundle
        resource_paths[variant] = str(resolved_resource.relative_to(root))
        dylib_counts[variant] = len(dylibs)

    plan_ref = _BLOCKER
    receipt_inputs = (
        binding.gate_aggregate.reference,
        binding.gate_transition.reference,
        binding.downstream_status.reference,
        binding.mapping.reference,
        binding.preflight.reference,
        binding.postgate_audit.reference,
        binding.old_terminal.reference,
        plan_ref,
        _REPACKAGE_BLOCKER,
    )
    payload: dict[str, object] = {
        "candidate": dict(_CANDIDATE),
        "state": "downstream_pending",
        "outcome": "static_packaging_qualified",
        "admission_gate_1_7_status": "qualified",
        "source_pair_status": "complete_launch_unverified",
        "fresh_namespace": str(target.relative_to(root)),
        "fresh_copy_status": "succeeded",
        "original_pair_modified": False,
        "inherit_prior_attempts": False,
        "old_attempts_loaded": False,
        "resource_repair": {
            "kind": "copied_bundle_internal_resource_target",
            "relative_link": "../../irr/media/Shaders",
            "resolved_paths": resource_paths,
            "outside_bundle": False,
        },
        "nested_resign": {
            "method": "ad_hoc_nested_dylibs_then_app_bundle",
            "dylib_counts": dylib_counts,
            "log_path": str(resign_log_path.relative_to(root)),
        },
        "static_checks": {
            "fixed": {
                "codesign_verify_strict_deep": {
                    "command": "codesign --verify --strict --deep --verbose=4 <fixed-minetest.app>",
                    "returncode": 0,
                },
                "codesign_display_verbose": {
                    "command": "codesign --display --verbose=4 <fixed-minetest.app>",
                    "returncode": 0,
                },
                "otool_dependency_listing": {
                    "command": "otool -L <fixed-minetest.app/Contents/MacOS/minetest>",
                    "returncode": 0,
                },
            },
            "buggy": {
                "codesign_verify_strict_deep": {
                    "command": "codesign --verify --strict --deep --verbose=4 <buggy-minetest.app>",
                    "returncode": 0,
                },
                "codesign_display_verbose": {
                    "command": "codesign --display --verbose=4 <buggy-minetest.app>",
                    "returncode": 0,
                },
                "otool_dependency_listing": {
                    "command": "otool -L <buggy-minetest.app/Contents/MacOS/minetest>",
                    "returncode": 0,
                },
            },
            "log_path": str(static_log_path.relative_to(root)),
        },
        "downstream_start_admission": {
            "status": "eligible_for_new_start_construction",
            "static_packaging_closed": True,
            "start_executed": False,
            "registered_replay_executed": False,
            "gate_9_claimed": False,
            "requires_separate_fresh_start_namespace": True,
        },
        "candidate_execution_performed": False,
        "semantic_observations_complete": False,
        "goldens_complete": False,
        "visual_cross_matrix_complete": False,
        "same_lineage_construction_receipt_complete": False,
        "missing_required_evidence": list(_MISSING_POSTGATE),
        "permanent_terminal_written": False,
        "terminal": False,
        "resumable": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "do_not_touch_pids": list(_DO_NOT_TOUCH_PIDS),
        "prohibited_actions": [
            "launch_any_repackage_v2_copy_from_this_receipt",
            "touch_or_signal_any_do_not_touch_pid",
            "reuse_historical_runtime_or_probe_receipts",
        ],
        "binding": {
            "gate_aggregate": binding.gate_aggregate.reference.artifact_id,
            "gate_transition": binding.gate_transition.reference.artifact_id,
            "downstream_status": binding.downstream_status.reference.artifact_id,
            "mapping": binding.mapping.reference.artifact_id,
            "preflight": binding.preflight.reference.artifact_id,
            "postgate_audit": binding.postgate_audit.reference.artifact_id,
            "old_terminal": binding.old_terminal.reference.artifact_id,
            "packaging_plan": plan_ref.artifact_id,
            "previous_repackage_blocker": _REPACKAGE_BLOCKER.artifact_id,
        },
        "complete": True,
    }
    write_stable_artifact(
        root,
        artifact_id=_REPACKAGE_RECEIPT.artifact_id,
        artifact_kind=_REPACKAGE_RECEIPT.artifact_kind,
        semantic_version=_REPACKAGE_RECEIPT.semantic_version,
        created_at=_now(),
        inputs=receipt_inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _REPACKAGE_RECEIPT)
    if artifact.inputs != receipt_inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#14757 repackage receipt typed reload failed")
    return artifact


def _validate_gate_1_7(
    aggregate: StableArtifact,
    transition: StableArtifact,
    status: StableArtifact,
) -> None:
    expected_candidate = dict(_CANDIDATE)
    aggregate_payload = aggregate.payload
    if (
        aggregate_payload.get("candidate") != expected_candidate
        or aggregate_payload.get("qualified") is not True
        or aggregate_payload.get("all_expected") is not True
        or aggregate_payload.get("attempt_count") != 12
        or aggregate_payload.get("repetitions_per_cell") != 3
        or aggregate_payload.get("stable_repetitions") is not True
        or aggregate_payload.get("cells")
        != [
            {
                "observed_outcomes": ["failure", "failure", "failure"],
                "phase": "f2p",
                "variant": "buggy",
            },
            {
                "observed_outcomes": ["pass", "pass", "pass"],
                "phase": "f2p",
                "variant": "fixed",
            },
            {
                "observed_outcomes": ["pass", "pass", "pass"],
                "phase": "p2p",
                "variant": "buggy",
            },
            {
                "observed_outcomes": ["pass", "pass", "pass"],
                "phase": "p2p",
                "variant": "fixed",
            },
        ]
    ):
        raise RuntimeError("#14757 Gate 1-7 aggregate is not qualified")
    if (
        transition.payload.get("candidate") != expected_candidate
        or transition.payload.get("qualified") is not True
        or transition.payload.get("truth_table")
        != {
            "buggy": {"f2p": False, "p2p": True},
            "fixed": {"f2p": True, "p2p": True},
        }
    ):
        raise RuntimeError("#14757 Gate 1-7 transition is not qualified")
    if (
        status.payload.get("candidate") != expected_candidate
        or status.payload.get("gate_1_7_status") != "qualified"
    ):
        raise RuntimeError("#14757 downstream status is not Gate 1-7 qualified")


def _validate_mapping(mapping: StableArtifact) -> None:
    payload = mapping.payload
    if (
        payload.get("candidate") != _CANDIDATE
        or payload.get("complete") is not True
        or payload.get("candidate_execution_performed") is not False
        or payload.get("registered_start") is None
        or payload.get("ordered_replay") is None
        or payload.get("semantic_oracle") is None
        or payload.get("golden_contract") is None
    ):
        raise RuntimeError("#14757 registered downstream mapping is invalid")


def _validate_preflight(preflight: StableArtifact) -> None:
    payload = preflight.payload
    if (
        payload.get("candidate") != _CANDIDATE
        or payload.get("application_pair_status") != "complete_launch_unverified"
        or payload.get("missing_application_versions") != []
        or payload.get("candidate_execution_performed") is not False
        or payload.get("permanent_terminal_written") is not False
    ):
        raise RuntimeError("#14757 application pair preflight is invalid")


def _validate_postgate_audit(audit: StableArtifact) -> None:
    payload = audit.payload
    observed = payload.get("observed_probe")
    static = payload.get("static_bundle_checks")
    if (
        payload.get("candidate") != _CANDIDATE
        or payload.get("admission_gate_1_7_status") != "qualified"
        or payload.get("state") != "downstream_pending"
        or payload.get("outcome") != "infrastructure_blocked"
        or payload.get("resumable") is not True
        or payload.get("terminal") is not False
        or payload.get("permanent_terminal_written") is not False
        or payload.get("inherit_prior_attempts") is not False
        or type(observed) is not dict
        or observed.get("process_outcome") != "timed_out"
        or observed.get("process_state") != "UE"
        or type(static) is not dict
        or "codesign_check" not in static
    ):
        raise RuntimeError("#14757 post-Gate-7 audit is invalid")
    pids = payload.get("do_not_touch_pids")
    if pids != list(_DO_NOT_TOUCH_PIDS):
        raise RuntimeError("#14757 protected UE PID set changed")


def _validate_old_terminal(terminal: StableArtifact) -> None:
    payload = terminal.payload
    if (
        payload.get("candidate") != _CANDIDATE
        or payload.get("admissible") is not False
        or payload.get("strict_freeze_eligible") is not False
        or payload.get("legacy_conclusions_reused") is not False
    ):
        raise RuntimeError("#14757 historical runtime terminal is invalid")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _ordinary_root(path: Path) -> Path:
    info = path.lstat()
    resolved = path.resolve(strict=True)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return resolved


def _ordinary_directory(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("required directory is missing or unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("required directory is missing or unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("required file is missing or unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("required file is missing or unsafe")
    return resolved


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("preannotation execution authorization is required")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "PackagingBlockerBinding",
    "load_luanti_14757_packaging_binding",
    "write_luanti_14757_packaging_blocker",
    "write_luanti_14757_repackage_receipt",
]
