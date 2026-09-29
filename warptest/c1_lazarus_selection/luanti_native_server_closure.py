"""Bounded server-only semantic closure for Luanti candidate 15519."""

from __future__ import annotations

import base64
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .luanti_native_executor import (
    NativeProcessReceipt,
    _apply_production_inverse,
    _copy_full_capsule,
    _validate_capsule,
    run_bounded_process,
)
from .luanti_native_regression import plan_luanti_native_regression_execution
from .models import CandidateKey
from .pipeline import StageDecision
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CANDIDATE = CandidateKey("luanti", 15519)
_VERSION = "1.0"
_MAX_OUTPUT = 262_144
_OBSERVER_SUFFIX = r"""
minetest.register_on_mods_loaded(function()
  minetest.after(0.1, function()
    minetest.log("action", "C1_SERVER_OBSERVER_STARTED")
    local _, NS = minetest.get_translator("c1_plural")
    local encoded = NS("apple", "apples", 122)
    local ok, translated = pcall(minetest.get_translated_string, "ru", encoded)
    if not ok then
      minetest.log("error", "C1_SERVER_OBSERVER_ERROR " .. tostring(translated))
      translated = "<error>"
    elseif translated == nil then
      translated = "<nil>"
    end
    minetest.log("action", "C1_SERVER_OBSERVER_RESULT " .. tostring(translated))
    minetest.request_shutdown("C1 server observer complete", false, 0)
  end)
end)
"""


@dataclass(frozen=True)
class ServerClosureResult:
    aggregate: StableArtifact
    semantic_qualified: bool


def execute_luanti_15519_server_closure(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> ServerClosureResult:
    _require_authorization(authorization)
    root = Path(artifact_root).resolve(strict=True)
    plan = plan_luanti_native_regression_execution(root, candidate=CANDIDATE)
    if isinstance(plan, StageDecision):
        raise RuntimeError(
            f"native server closure blocked: {plan.details.get('reason')}"
        )
    _validate_capsule(root, plan)
    fixture = root / "private/downstream-fixtures/luanti/15519/russian-po-game-v1"
    if not fixture.is_dir() or fixture.is_symlink():
        raise RuntimeError("native server fixture is unavailable")
    mapping = _load(
        root,
        ArtifactReference(
            "lazarus_task_mapping/luanti/15519/issue-15518/v2",
            "lazarus_task_mapping",
            "2.0",
        ),
    )
    work = root / "private/luanti-15519-server-closure/v1"
    work.mkdir(parents=True, exist_ok=True)
    fixed = work / "fixed"
    buggy = work / "buggy"
    if not fixed.exists():
        _copy_full_capsule(plan, fixed)
    if not buggy.exists():
        _copy_full_capsule(plan, buggy)
        _apply_production_inverse(plan, fixed, buggy)

    build_receipts: dict[str, StableArtifact] = {}
    observer_receipts: list[StableArtifact] = []
    for variant, tree in (("fixed", fixed), ("buggy", buggy)):
        last: StableArtifact | None = None
        for step, raw_argv, timeout in (
            ("configure", plan.configure_argv, 600),
            ("build", plan.build_argv, 1_800),
        ):
            reference = _step_reference(variant, step)
            previous = _try_load(root, reference)
            if previous is not None and _successful_process_payload(previous.payload):
                last = previous
                continue
            receipt = run_bounded_process(
                tuple(str(value) for value in raw_argv),
                cwd=tree,
                env=dict(plan.closed_environment),
                timeout_seconds=timeout,
            )
            last = _write_process_receipt(
                root,
                reference,
                inputs=(plan.profile.fixed_capsule_reference, mapping.reference),
                variant=variant,
                step=step,
                argv=tuple(str(value) for value in raw_argv),
                receipt=receipt,
                authorization=authorization,
            )
            if receipt.outcome != "completed" or receipt.returncode != 0:
                aggregate = _write_pending(
                    root,
                    mapping=mapping,
                    evidence=(last.reference,),
                    reason=f"{variant}_{step}_failed",
                    authorization=authorization,
                )
                return ServerClosureResult(aggregate, False)
        assert last is not None
        build_receipts[variant] = last
        executable = tree / "bin/luantiserver"
        if not executable.is_file() or executable.is_symlink():
            aggregate = _write_pending(
                root,
                mapping=mapping,
                evidence=(last.reference,),
                reason=f"{variant}_server_binary_missing",
                authorization=authorization,
            )
            return ServerClosureResult(aggregate, False)
        observer_receipts.append(
            _run_observer(
                root,
                tree=tree,
                fixture=fixture,
                variant=variant,
                build=last,
                mapping=mapping,
                authorization=authorization,
            )
        )

    qualified = all(
        item.payload["semantic_match"] is True for item in observer_receipts
    )
    reference = ArtifactReference(
        "native_server_closure/luanti/15519/fixed-buggy/v1",
        "native_server_closure",
        _VERSION,
    )
    payload = {
        "candidate": CANDIDATE.to_value(),
        "server_only": True,
        "fixed_build": "passed",
        "buggy_build": "passed",
        "fixed_observer": observer_receipts[0].payload["semantic_classification"],
        "buggy_observer": observer_receipts[1].payload["semantic_classification"],
        "semantic_qualified": qualified,
        "downstream_state": "pending",
        "missing_required_evidence": [
            "complete_fixed_buggy_visual_client_pair",
            "raw_fixed_and_buggy_application_frames",
            "visual_goldens_and_cross_matrix",
            "same_lineage_visual_construction_receipt",
        ],
        "permanent_terminal_written": False,
        "paid_actions_authorized": False,
        "complete": True,
    }
    aggregate = _write(
        root,
        reference,
        inputs=tuple(item.reference for item in observer_receipts),
        payload=payload,
        authorization=authorization,
    )
    return ServerClosureResult(aggregate, qualified)


def classify_server_observation(
    variant: str, stdout: bytes, stderr: bytes
) -> dict[str, object]:
    if variant not in {"fixed", "buggy"}:
        raise ValueError("native server observer variant is invalid")
    combined = stdout + b"\n" + stderr
    started = b"C1_SERVER_OBSERVER_STARTED" in combined
    result = b"C1_SERVER_OBSERVER_RESULT" in combined
    invalid = b"Invalid Plural-Forms line:" in combined
    nil_result = b"C1_SERVER_OBSERVER_RESULT <nil>" in combined
    call_error = b"C1_SERVER_OBSERVER_ERROR" in combined
    expected_invalid = variant == "buggy"
    semantic_match = (
        started
        and result
        and not nil_result
        and not call_error
        and invalid is expected_invalid
    )
    return {
        "startup_marker_observed": started,
        "result_marker_observed": result,
        "invalid_plural_forms_observed": invalid,
        "nil_result_observed": nil_result,
        "observer_call_error_observed": call_error,
        "expected_invalid_plural_forms": expected_invalid,
        "semantic_classification": (
            "fixed_accepts_registered_russian_plural_form"
            if semantic_match and variant == "fixed"
            else "buggy_rejects_registered_russian_plural_form"
            if semantic_match
            else "semantic_observation_incomplete"
        ),
        "semantic_match": semantic_match,
    }


def _run_observer(
    root: Path,
    *,
    tree: Path,
    fixture: Path,
    variant: str,
    build: StableArtifact,
    mapping: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    runtime = tree / "c1-server-observer"
    if runtime.exists():
        shutil.rmtree(runtime)
    # The frozen fixture is a complete historical game closure, not merely a
    # world-mod.  Register it under the tree's own ``games`` directory so the
    # server can resolve the explicit game id without depending on host data.
    game = tree / "games/c1_plural"
    if game.exists():
        shutil.rmtree(game)
    shutil.copytree(fixture, game)
    init = game / "mods/c1_plural/init.lua"
    init.write_text(
        init.read_text(encoding="utf-8") + _OBSERVER_SUFFIX, encoding="utf-8"
    )
    world = runtime / "world"
    world.mkdir(parents=True)
    (world / "world.mt").write_text(
        "gameid = c1_plural\nbackend = sqlite3\nplayer_backend = sqlite3\nauth_backend = sqlite3\n",
        encoding="utf-8",
    )
    config = runtime / "minetest.conf"
    port = 43000 + (os.getpid() % 10000)
    config.write_text(
        f"language = ru\nport = {port}\nserver_announce = false\n",
        encoding="utf-8",
    )
    argv = (
        str(tree / "bin/luantiserver"),
        "--world",
        str(world),
        "--gameid",
        "c1_plural",
        "--config",
        str(config),
        "--port",
        str(port),
        "--logfile",
        "-",
    )
    receipt = run_bounded_process(
        argv,
        cwd=tree,
        env=dict(
            os.environ, **{"HOME": str(runtime / "home"), "LANG": "C", "LC_ALL": "C"}
        ),
        timeout_seconds=20,
    )
    classification = classify_server_observation(
        variant, receipt.stdout, receipt.stderr
    )
    reference = _step_reference(variant, "observer")
    payload = _process_payload(variant, "observer", argv, receipt)
    payload.update(classification)
    payload["watchdog_seconds"] = 20
    payload["process_group_reaped"] = True
    return _write(
        root,
        reference,
        inputs=(build.reference, mapping.reference),
        payload=payload,
        authorization=authorization,
    )


def _step_reference(variant: str, step: str) -> ArtifactReference:
    return ArtifactReference(
        f"native_server_closure_step/luanti/15519/{variant}/{step}/v1",
        "native_server_closure_step",
        _VERSION,
    )


def _write_process_receipt(
    root: Path,
    reference: ArtifactReference,
    *,
    inputs: tuple[ArtifactReference, ...],
    variant: str,
    step: str,
    argv: tuple[str, ...],
    receipt: NativeProcessReceipt,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    return _write(
        root,
        reference,
        inputs=inputs,
        payload=_process_payload(variant, step, argv, receipt),
        authorization=authorization,
    )


def _process_payload(
    variant: str, step: str, argv: tuple[str, ...], receipt: NativeProcessReceipt
) -> dict[str, object]:
    stdout = receipt.stdout[:_MAX_OUTPUT]
    stderr = receipt.stderr[:_MAX_OUTPUT]
    return {
        "candidate": CANDIDATE.to_value(),
        "variant": variant,
        "step": step,
        "argv": list(argv),
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": receipt.stdout_truncated
        or len(receipt.stdout) > _MAX_OUTPUT,
        "stderr_truncated": receipt.stderr_truncated
        or len(receipt.stderr) > _MAX_OUTPUT,
        "output_encoding": "base64",
    }


def _successful_process_payload(payload: object) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("process_outcome") == "completed"
        and payload.get("returncode") == 0
    )


def _write_pending(
    root: Path,
    *,
    mapping: StableArtifact,
    evidence: tuple[ArtifactReference, ...],
    reason: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    return _write(
        root,
        ArtifactReference(
            "native_server_closure/luanti/15519/fixed-buggy/v1",
            "native_server_closure",
            _VERSION,
        ),
        inputs=(*evidence, mapping.reference),
        payload={
            "candidate": CANDIDATE.to_value(),
            "server_only": True,
            "semantic_qualified": False,
            "downstream_state": "pending",
            "reason": reason,
            "permanent_terminal_written": False,
            "paid_actions_authorized": False,
            "complete": True,
        },
        authorization=authorization,
    )


def _write(
    root: Path,
    reference: ArtifactReference,
    *,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, reference)
    if artifact.inputs != inputs or artifact.payload != payload:
        raise RuntimeError("native server closure typed reload failed")
    return artifact


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _try_load(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, reference)
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


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
    "CANDIDATE",
    "ServerClosureResult",
    "classify_server_observation",
    "execute_luanti_15519_server_closure",
]
