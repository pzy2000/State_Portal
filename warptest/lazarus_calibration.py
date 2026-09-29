from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from warptest.lazarus_catalog import LAZARUS_VERSIONS
from warptest.lazarus_versions import LazarusVersionArtifact
from warptest.model_bench import DEFAULT_MODELSCOPE_BASE_URL
from warptest.providers import resolve_model


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class LazarusBackendBinding:
    task_id: str
    benchmark: str
    version: str
    source_tree_sha256: str
    prebuilt_binary_sha256: str
    capture_backend: str
    input_backend: str
    canary_passed: bool


def lazarus_backend_binding_from_artifact(
    artifact: LazarusVersionArtifact,
    *,
    benchmark: str,
    capture_backend: str,
    input_backend: str,
    binary_paths: Sequence[str | Path],
    canary_passed: bool,
) -> LazarusBackendBinding:
    """Bind one materialized version to the binaries the runtime will execute.

    A source-only backend still supplies its executable client path here.  The
    source-tree fingerprint and binary-bundle fingerprint are deliberately
    independent: Lua-only inverse patches can leave the client binary unchanged
    while their source tree differs, whereas compiled backends normally change
    both identities.
    """

    root = Path(artifact.root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Lazarus version artifact root does not exist")
    normalized_paths: list[Path] = []
    for raw_path in binary_paths:
        candidate = Path(raw_path)
        candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        if root != candidate and root not in candidate.parents:
            raise ValueError("Lazarus calibration binary path escapes version root")
        if not candidate.is_file():
            raise ValueError(f"Lazarus calibration binary is missing: {candidate}")
        normalized_paths.append(candidate)
    if not normalized_paths:
        raise ValueError("Lazarus calibration requires at least one binary path")
    return LazarusBackendBinding(
        task_id=artifact.task_id,
        benchmark=str(benchmark),
        version=artifact.version,
        source_tree_sha256=artifact.tree_sha256,
        prebuilt_binary_sha256=_binary_bundle_sha256(root, normalized_paths),
        capture_backend=str(capture_backend),
        input_backend=str(input_backend),
        canary_passed=canary_passed is True,
    )


def lazarus_harness_hash(root: str | Path) -> str:
    """Hash Lazarus runtime and registered evidence inputs, never frozen C1 proof.

    The Phase-0 catalog, its private Lazarus specs/upstream records/goldens, and
    the resolved Python dependency lock are all operational inputs to a paid
    trial.  Including only source modules would allow a post-calibration golden
    swap or dependency resolution drift to retain a stale calibration hash.
    """

    base = Path(root).expanduser().resolve()
    candidates: list[Path] = []
    runtime_root = base / "warptest"
    if runtime_root.is_dir():
        candidates.extend(
            path
            for path in runtime_root.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
            and path.name != ".DS_Store"
        )
    candidates.extend(
        base / name for name in ("pyproject.toml", "uv.lock")
    )
    candidates.extend(
        base / "scripts" / name
        for name in (
            "mine_lazarus_defects.py",
            "build_lazarus_versions.py",
            "run_lazarus_probe.py",
            "capture_lazarus_goldens.py",
            "run_lazarus_gate.py",
            "build_lazarus_calibration.py",
            "audit_lazarus_tasks.py",
            "preflight_lazarus_phase0.py",
            "check_lazarus_phase0_runtime_support.py",
            "run_lazarus_phase0.py",
            "preflight_lazarus_p2p_only.py",
            "preflight_lazarus_p2p_only_api.py",
            "run_lazarus_p2p_only_runtime_canary.py",
            "build_lazarus_p2p_only_calibration.py",
            "run_lazarus_p2p_only.py",
        )
        if (base / "scripts" / name).is_file()
    )
    candidates.extend(
        path
        for directory in (
            base / "benchmarks" / "p5" / "lazarus",
            base / "specs" / "luanti" / "lazarus",
            base / "specs" / "openra" / "lazarus",
            base / "specs" / "jupyterlab" / "lazarus_p2p_only",
            base / "specs" / "luanti" / "lazarus_p2p_only",
            base / "specs" / "openra" / "lazarus_p2p_only",
            base / "benchmarks" / "p5" / "lazarus" / "p2p_only_defects",
        )
        if directory.is_dir()
        for path in directory.rglob("*")
        if path.is_file()
    )
    candidates.extend(
        base / "benchmarks" / "p5" / name
        for name in (
            "c1_lazarus_phase0_tasks.yaml",
            "lazarus_phase0_manifest.yaml",
            "c1_lazarus_p2p_only_tasks.yaml",
            "lazarus_p2p_only_manifest.yaml",
        )
    )
    paths = sorted({path.resolve() for path in candidates if path.is_file()})
    if not paths:
        raise ValueError("Lazarus harness has no registered source inputs")
    digest = hashlib.sha256()
    for path in paths:
        try:
            relative = path.relative_to(base).as_posix()
        except ValueError as error:
            raise ValueError("Lazarus harness input escapes repository root") from error
        digest.update(b"file\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def lazarus_provider_identity_sha256(manifest: Mapping[str, Any]) -> str:
    """Freeze resolved routing/model identity without serializing credentials.

    The manifest's model strings are only half of the execution route: tagged
    Relay models resolve their endpoint and routing headers from the live
    environment.  Calibration must therefore bind that non-secret transport
    identity as well, while intentionally excluding the API-key value itself.
    """

    api = manifest.get("api")
    provider_name = (
        str(api.get("provider") or "").strip()
        if isinstance(api, Mapping)
        else ""
    )
    models = manifest.get("models")
    if not isinstance(models, list) or not models or not all(
        isinstance(model, str) and model.strip() for model in models
    ):
        raise ValueError("Lazarus provider identity requires at least one decision model")
    decision_models = tuple(str(model).strip() for model in models)
    if len(decision_models) == 1:
        # Preserve the frozen single-model identity byte-for-byte.
        decision_model = decision_models[0]
        routes = [
            _resolved_provider_route("decision", decision_model, provider_name),
            _resolved_provider_route(
                "observer",
                _required_manifest_model(manifest, "observer_model"),
                provider_name,
            ),
            _resolved_provider_route(
                "grounder",
                (
                    decision_model
                    if manifest.get("grounder_model") == "decision_model"
                    else _required_manifest_model(manifest, "grounder_model")
                ),
                provider_name,
            ),
            _resolved_provider_route(
                "judge",
                _required_manifest_model(manifest, "judge_model"),
                provider_name,
            ),
        ]
    else:
        routes = []
        for decision_model in decision_models:
            routes.append(
                _resolved_provider_route(
                    f"decision:{decision_model}", decision_model, provider_name
                )
            )
            routes.append(
                _resolved_provider_route(
                    f"grounder:{decision_model}",
                    (
                        decision_model
                        if manifest.get("grounder_model") == "decision_model"
                        else _required_manifest_model(manifest, "grounder_model")
                    ),
                    provider_name,
                )
            )
        routes.extend(
            (
                _resolved_provider_route(
                    "observer",
                    _required_manifest_model(manifest, "observer_model"),
                    provider_name,
                ),
                _resolved_provider_route(
                    "judge",
                    _required_manifest_model(manifest, "judge_model"),
                    provider_name,
                ),
            )
        )

    identity = {
        "api": manifest.get("api"),
        "models": manifest.get("models"),
        "model_request_profiles": manifest.get("model_request_profiles"),
        "observer_model": manifest.get("observer_model"),
        "grounder_model": manifest.get("grounder_model"),
        "judge_model": manifest.get("judge_model"),
        "provider_timeout_s": manifest.get("provider_timeout_s"),
        "resolved_routes": routes,
    }
    return _record_sha256(identity)


def _required_manifest_model(manifest: Mapping[str, Any], field: str) -> str:
    value = manifest.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Lazarus provider identity requires {field}")
    return value.strip()


def _resolved_provider_route(
    role: str,
    model_spec: str,
    default_provider: str,
) -> dict[str, str]:
    """Return the secret-free transport route used for one registered role."""

    effective_spec = model_spec
    if "@" not in effective_spec and default_provider:
        effective_spec = f"{effective_spec}@{default_provider}"
    try:
        clean_model, provider = resolve_model(
            effective_spec,
            default_base_url=os.environ.get(
                "MODELSCOPE_BASE_URL", DEFAULT_MODELSCOPE_BASE_URL
            ),
            default_api_key_env="MODELSCOPE_API_KEY",
        )
    except ValueError as error:
        raise ValueError(
            f"Lazarus provider identity cannot resolve {role} model route"
        ) from error
    return {
        "role": role,
        "model_spec": effective_spec,
        "resolved_model": str(clean_model),
        "provider": str(provider.name),
        "base_url": str(provider.base_url).rstrip("/"),
        "api_key_env": str(provider.api_key_env),
        "routing_headers_sha256": _routing_headers_sha256(provider.extra_headers),
    }


def _routing_headers_sha256(headers: Mapping[str, str]) -> str:
    """Hash routing-header identities while omitting credential-bearing names."""

    safe: list[tuple[str, str]] = []
    for name, value in sorted(headers.items()):
        normalized_name = str(name).strip().lower()
        if any(
            marker in normalized_name
            for marker in ("authorization", "api-key", "token", "secret", "cookie")
        ):
            continue
        safe.append(
            (
                str(name),
                hashlib.sha256(str(value).encode("utf-8")).hexdigest(),
            )
        )
    return _record_sha256({"routing_headers": safe})


def _require_sha256(name: str, value: object) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _record_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _binary_bundle_sha256(root: Path, paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda value: value.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        digest.update(b"file\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def build_lazarus_calibration(
    bindings: Sequence[LazarusBackendBinding],
    *,
    manifest_sha256: str,
    harness_sha256: str,
    provider_identity_sha256: str,
    observer_model: str,
    grounder_model: str,
    judge_model: str,
) -> dict[str, Any]:
    """Build the immutable per-task/per-version backend identity artifact."""

    global_hashes = {
        "manifest_sha256": _require_sha256("manifest_sha256", manifest_sha256),
        "harness_sha256": _require_sha256("harness_sha256", harness_sha256),
        "provider_identity_sha256": _require_sha256(
            "provider_identity_sha256", provider_identity_sha256
        ),
    }
    models = {
        "observer": str(observer_model),
        "grounder": str(grounder_model),
        "judge": str(judge_model),
    }
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for binding in sorted(bindings, key=lambda row: (row.task_id, row.version)):
        if not binding.task_id or not binding.benchmark:
            raise ValueError("backend binding task_id and benchmark must be non-empty")
        if binding.version not in LAZARUS_VERSIONS:
            raise ValueError(f"unsupported Lazarus version: {binding.version!r}")
        key = (binding.task_id, binding.version)
        if key in seen:
            raise ValueError(f"duplicate Lazarus calibration binding: {key!r}")
        seen.add(key)
        row = asdict(binding)
        row["source_tree_sha256"] = _require_sha256(
            "source_tree_sha256", binding.source_tree_sha256
        )
        row["prebuilt_binary_sha256"] = _require_sha256(
            "prebuilt_binary_sha256", binding.prebuilt_binary_sha256
        )
        if not binding.capture_backend or not binding.input_backend:
            raise ValueError("capture_backend and input_backend must be non-empty")
        row["record_sha256"] = _record_sha256(row)
        rows.append(row)

    payload: dict[str, Any] = {
        "schema": "lazarus-calibration-v1",
        "schema_version": 1,
        **global_hashes,
        "models": models,
        "backends": rows,
        "passed": bool(rows) and all(row["canary_passed"] is True for row in rows),
    }
    payload["artifact_sha256"] = _record_sha256(payload)
    return payload


def validate_lazarus_calibration(
    artifact: Mapping[str, Any],
    *,
    expected_versions: Mapping[tuple[str, str], Mapping[str, Any]],
    manifest_sha256: str,
    harness_sha256: str,
    provider_identity_sha256: str,
) -> list[str]:
    """Return every fail-closed calibration mismatch without trusting the artifact."""

    errors: list[str] = []
    if artifact.get("schema") != "lazarus-calibration-v1":
        errors.append("calibration schema is not lazarus-calibration-v1")
    if artifact.get("schema_version") != 1:
        errors.append("calibration schema_version is not 1")
    for name, expected in (
        ("manifest_sha256", manifest_sha256),
        ("harness_sha256", harness_sha256),
        ("provider_identity_sha256", provider_identity_sha256),
    ):
        if artifact.get(name) != expected:
            errors.append(f"{name} drift")

    raw_rows = artifact.get("backends")
    if not isinstance(raw_rows, list):
        errors.append("backends must be a list")
        raw_rows = []
    actual: dict[tuple[str, str], Mapping[str, Any]] = {}
    for index, value in enumerate(raw_rows):
        if not isinstance(value, Mapping):
            errors.append(f"backends[{index}] must be a mapping")
            continue
        key = (value.get("task_id"), value.get("version"))
        if not all(isinstance(item, str) and item for item in key):
            errors.append(f"backends[{index}] has an invalid task/version key")
            continue
        typed_key = (str(key[0]), str(key[1]))
        if typed_key in actual:
            errors.append(f"duplicate backend binding for {typed_key!r}")
            continue
        actual[typed_key] = value

    for key in sorted(set(expected_versions) - set(actual)):
        errors.append(f"missing backend binding for {key!r}")
    for key in sorted(set(actual) - set(expected_versions)):
        errors.append(f"unexpected backend binding for {key!r}")
    comparable_fields = (
        "benchmark",
        "source_tree_sha256",
        "prebuilt_binary_sha256",
        "capture_backend",
        "input_backend",
    )
    for key in sorted(set(expected_versions) & set(actual)):
        row = actual[key]
        expected = expected_versions[key]
        for field in comparable_fields:
            if row.get(field) != expected.get(field):
                errors.append(f"{key!r} {field} mismatch")
        if row.get("canary_passed") is not True:
            errors.append(f"{key!r} canary did not pass")
        unhashed = {k: v for k, v in row.items() if k != "record_sha256"}
        if row.get("record_sha256") != _record_sha256(unhashed):
            errors.append(f"{key!r} backend record hash mismatch")

    if artifact.get("passed") is not True:
        errors.append("calibration passed flag is not true")
    expected_artifact_hash = _record_sha256(
        {k: v for k, v in artifact.items() if k != "artifact_sha256"}
    )
    if artifact.get("artifact_sha256") != expected_artifact_hash:
        errors.append("calibration artifact hash mismatch")
    return errors
