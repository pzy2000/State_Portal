from __future__ import annotations

import copy
from pathlib import Path

import pytest

from warptest.lazarus_calibration import (
    LazarusBackendBinding,
    build_lazarus_calibration,
    lazarus_backend_binding_from_artifact,
    lazarus_harness_hash,
    lazarus_provider_identity_sha256,
    validate_lazarus_calibration,
)
from warptest.lazarus_versions import LazarusVersionArtifact


def _bindings() -> tuple[list[LazarusBackendBinding], dict[tuple[str, str], dict]]:
    rows: list[LazarusBackendBinding] = []
    expected: dict[tuple[str, str], dict] = {}
    for task_index, benchmark in enumerate(("luanti", "openra")):
        task_id = f"lz-{benchmark}-pilot"
        for version_index, version in enumerate(("buggy", "fixed")):
            source_sha = f"{task_index + version_index + 1:064x}"
            binary_sha = f"{task_index + version_index + 11:064x}"
            row = LazarusBackendBinding(
                task_id=task_id,
                benchmark=benchmark,
                version=version,
                source_tree_sha256=source_sha,
                prebuilt_binary_sha256=binary_sha,
                capture_backend=f"{benchmark}_capture_v1",
                input_backend=f"{benchmark}_input_v1",
                canary_passed=True,
            )
            rows.append(row)
            expected[(task_id, version)] = {
                "benchmark": benchmark,
                "source_tree_sha256": source_sha,
                "prebuilt_binary_sha256": binary_sha,
                "capture_backend": f"{benchmark}_capture_v1",
                "input_backend": f"{benchmark}_input_v1",
            }
    return rows, expected


def test_lazarus_calibration_binds_every_task_version_and_global_identity() -> None:
    bindings, expected = _bindings()

    artifact = build_lazarus_calibration(
        bindings,
        manifest_sha256="a" * 64,
        harness_sha256="b" * 64,
        provider_identity_sha256="c" * 64,
        observer_model="claude-sonnet-5@relay",
        grounder_model="gpt-5.6-terra@relay",
        judge_model="qwen3-vl-flash@relay",
    )
    errors = validate_lazarus_calibration(
        artifact,
        expected_versions=expected,
        manifest_sha256="a" * 64,
        harness_sha256="b" * 64,
        provider_identity_sha256="c" * 64,
    )

    assert errors == []
    assert artifact["schema"] == "lazarus-calibration-v1"
    assert artifact["schema_version"] == 1
    assert artifact["passed"] is True
    assert len(artifact["backends"]) == 4
    assert {(row["task_id"], row["version"]) for row in artifact["backends"]} == set(
        expected
    )


def test_lazarus_calibration_fails_closed_on_missing_tampered_or_failed_binding() -> None:
    bindings, expected = _bindings()
    artifact = build_lazarus_calibration(
        bindings,
        manifest_sha256="a" * 64,
        harness_sha256="b" * 64,
        provider_identity_sha256="c" * 64,
        observer_model="claude-sonnet-5@relay",
        grounder_model="gpt-5.6-terra@relay",
        judge_model="qwen3-vl-flash@relay",
    )
    missing = copy.deepcopy(artifact)
    missing["backends"].pop()
    tampered = copy.deepcopy(artifact)
    tampered["backends"][0]["source_tree_sha256"] = "0" * 64
    failed = copy.deepcopy(artifact)
    failed["backends"][0]["canary_passed"] = False
    failed["passed"] = False

    kwargs = {
        "expected_versions": expected,
        "manifest_sha256": "a" * 64,
        "harness_sha256": "b" * 64,
        "provider_identity_sha256": "c" * 64,
    }
    assert any("missing" in error for error in validate_lazarus_calibration(missing, **kwargs))
    assert any("source_tree" in error for error in validate_lazarus_calibration(tampered, **kwargs))
    assert any("canary" in error for error in validate_lazarus_calibration(failed, **kwargs))


def test_lazarus_calibration_rejects_manifest_harness_and_provider_drift() -> None:
    bindings, expected = _bindings()
    artifact = build_lazarus_calibration(
        bindings,
        manifest_sha256="a" * 64,
        harness_sha256="b" * 64,
        provider_identity_sha256="c" * 64,
        observer_model="claude-sonnet-5@relay",
        grounder_model="gpt-5.6-terra@relay",
        judge_model="qwen3-vl-flash@relay",
    )

    errors = validate_lazarus_calibration(
        artifact,
        expected_versions=expected,
        manifest_sha256="d" * 64,
        harness_sha256="e" * 64,
        provider_identity_sha256="f" * 64,
    )

    assert any("manifest" in error for error in errors)
    assert any("harness" in error for error in errors)
    assert any("provider" in error for error in errors)


def test_backend_binding_hashes_runtime_binaries_without_reusing_source_hash(
    tmp_path: Path,
) -> None:
    root = tmp_path / "fixed"
    binary = root / "bin" / "runtime.bin"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"runtime")
    artifact = LazarusVersionArtifact(
        task_id="lz-openra-pilot",
        version="fixed",
        root=root,
        git_metadata=tmp_path / "fixed.git",
        source_revision="a" * 40,
        patch_sha256="b" * 64,
        tree_sha256="c" * 64,
        changed_files=(),
        build_identity={"status": "passed"},
        cache_hit=True,
    )

    binding = lazarus_backend_binding_from_artifact(
        artifact,
        benchmark="openra",
        capture_backend="capture-v1",
        input_backend="input-v1",
        binary_paths=("bin/runtime.bin",),
        canary_passed=True,
    )

    assert binding.source_tree_sha256 == "c" * 64
    assert binding.prebuilt_binary_sha256 != binding.source_tree_sha256
    with pytest.raises(ValueError, match="escapes"):
        lazarus_backend_binding_from_artifact(
            artifact,
            benchmark="openra",
            capture_backend="capture-v1",
            input_backend="input-v1",
            binary_paths=("../outside.bin",),
            canary_passed=True,
        )


def test_provider_identity_never_includes_credential_values() -> None:
    manifest = {
        "api": {"provider": "relay"},
        "models": ["gpt-5.6-terra"],
        "model_request_profiles": {"gpt-5.6-terra": "thinking_disabled"},
        "observer_model": "claude-sonnet-5@relay",
        "grounder_model": "decision_model",
        "judge_model": "qwen3-vl-flash@relay",
        "provider_timeout_s": 900,
        "api_key": "must-not-be-hashed-as-identity",
    }

    first = lazarus_provider_identity_sha256(manifest)
    manifest["api_key"] = "different-secret"

    assert first == lazarus_provider_identity_sha256(manifest)


def test_provider_identity_binds_resolved_relay_transport_and_routing_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calibration must drift when the actual tagged-provider route changes."""

    manifest = {
        "api": {"provider": "relay"},
        "models": ["gpt-5.6-terra"],
        "model_request_profiles": {"gpt-5.6-terra": "thinking_disabled"},
        "observer_model": "claude-sonnet-5@relay",
        "grounder_model": "decision_model",
        "judge_model": "qwen3-vl-flash@relay",
        "provider_timeout_s": 900,
    }
    monkeypatch.setenv("RELAY_BASE_URL", "https://relay-a.invalid/v1")
    monkeypatch.setenv("RELAY_END_USER", "route-a")
    monkeypatch.setenv("RELAY_API_KEY", "first-secret")
    first = lazarus_provider_identity_sha256(manifest)

    monkeypatch.setenv("RELAY_API_KEY", "second-secret")
    assert lazarus_provider_identity_sha256(manifest) == first

    monkeypatch.setenv("RELAY_BASE_URL", "https://relay-b.invalid/v1")
    assert lazarus_provider_identity_sha256(manifest) != first

    monkeypatch.setenv("RELAY_BASE_URL", "https://relay-a.invalid/v1")
    monkeypatch.setenv("RELAY_END_USER", "route-b")
    assert lazarus_provider_identity_sha256(manifest) != first


def test_lazarus_harness_hash_binds_visual_runtime_and_package_dependencies(
    tmp_path: Path,
) -> None:
    """Calibration must invalidate when a live runner dependency changes."""

    package = tmp_path / "warptest"
    package.mkdir()
    for name in (
        "lazarus_trial.py",
        "lazarus_runtime.py",
        "c1_experiment.py",
        "repo_tools.py",
        "runtime.py",
        "spec_schema.py",
        "visual.py",
        "luanti_state.py",
    ):
        (package / name).write_text(f"{name}\n", encoding="utf-8")
    metadata = tmp_path / "pyproject.toml"
    metadata.write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
    lock = tmp_path / "uv.lock"
    lock.write_text("version = 1\n", encoding="utf-8")

    original = lazarus_harness_hash(tmp_path)
    visual = package / "visual.py"
    visual.write_text("visual drift\n", encoding="utf-8")
    assert lazarus_harness_hash(tmp_path) != original

    visual.write_text("visual.py\n", encoding="utf-8")
    luanti_state = package / "luanti_state.py"
    luanti_state.write_text("state drift\n", encoding="utf-8")
    assert lazarus_harness_hash(tmp_path) != original

    luanti_state.write_text("luanti_state.py\n", encoding="utf-8")
    credential_loader = package / "c1_experiment.py"
    credential_loader.write_text("credential loader drift\n", encoding="utf-8")
    assert lazarus_harness_hash(tmp_path) != original

    credential_loader.write_text("c1_experiment.py\n", encoding="utf-8")
    repo_tools = package / "repo_tools.py"
    repo_tools.write_text("sandbox drift\n", encoding="utf-8")
    assert lazarus_harness_hash(tmp_path) != original

    repo_tools.write_text("repo_tools.py\n", encoding="utf-8")
    spec_schema = package / "spec_schema.py"
    spec_schema.write_text("schema drift\n", encoding="utf-8")
    assert lazarus_harness_hash(tmp_path) != original

    spec_schema.write_text("spec_schema.py\n", encoding="utf-8")
    runtime = package / "runtime.py"
    runtime.write_text("transitive runtime drift\n", encoding="utf-8")
    assert lazarus_harness_hash(tmp_path) != original

    runtime.write_text("runtime.py\n", encoding="utf-8")
    lock.write_text("version = 2\n", encoding="utf-8")
    assert lazarus_harness_hash(tmp_path) != original
    metadata.write_text("[project]\nname = 'drifted-fixture'\n", encoding="utf-8")
    assert lazarus_harness_hash(tmp_path) != original
