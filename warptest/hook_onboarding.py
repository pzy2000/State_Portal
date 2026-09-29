"""Orchestration for the automatic hook-onboarding pilot.

The implementation intentionally favors a small, explicit protocol over the
formal C1 provenance machinery.  Clean source revisions are identified by their
published Git commit IDs, but no additional content, prompt, manifest, patch, or
result hashes are computed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from .c1_experiment import relay_environment
from .hook_onboarding_agent import (
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    HookAgentWorkspace,
    HookCodingAgent,
    NamedCheck,
    sanitized_subprocess_env,
    safe_error_text,
)


DEFAULT_MANIFEST = Path("benchmarks/onboarding/manifest.yaml")
DEFAULT_CREDENTIALS = Path("test_model_batch.py")
ALLOWED_HOOK_KINDS = frozenset({"existing_api", "test_gated_patch", "extension"})
ALLOWED_ROUNDTRIPS = frozenset({"live", "save_reload"})
ALLOWED_RESULT_STATUSES = frozenset(
    {
        "pass",
        "partial",
        "agent_failure",
        "heldout_failure",
        "provider_failure",
        "source_blocked",
        "infrastructure_blocked",
        "reference_failed",
        "invalid_manual_intervention",
    }
)
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_FORBIDDEN_RESULT_KEY = re.compile(r"(?:^|_)(?:sha256|hash|fingerprint)(?:$|_)", re.IGNORECASE)


class OnboardingConfigError(ValueError):
    pass


class SourceMaterializationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_s: float = 300.0
    env: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, label: str) -> "CommandSpec":
        if not isinstance(value, Mapping):
            raise OnboardingConfigError(f"{label} must be a mapping")
        argv = value.get("argv")
        if not isinstance(argv, Sequence) or isinstance(argv, (str, bytes)):
            raise OnboardingConfigError(f"{label}.argv must be a list")
        normalized = tuple(str(item) for item in argv)
        if not normalized or any(not item for item in normalized):
            raise OnboardingConfigError(f"{label}.argv must contain non-empty strings")
        timeout_s = float(value.get("timeout_s") or 300.0)
        if timeout_s <= 0:
            raise OnboardingConfigError(f"{label}.timeout_s must be positive")
        raw_env = value.get("env") or {}
        if not isinstance(raw_env, Mapping):
            raise OnboardingConfigError(f"{label}.env must be a mapping")
        return cls(
            argv=normalized,
            cwd=str(value.get("cwd") or "."),
            timeout_s=timeout_s,
            env={str(key): str(item) for key, item in raw_env.items()},
        )


@dataclass(frozen=True)
class CaseSpec:
    id: str
    target: Mapping[str, Any]
    roundtrip: str
    expected: Mapping[str, Any]
    timeout_s: float = 300.0
    verifier: CommandSpec | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, label: str) -> "CaseSpec":
        if not isinstance(value, Mapping):
            raise OnboardingConfigError(f"{label} must be a mapping")
        case_id = str(value.get("id") or "").strip()
        if not case_id:
            raise OnboardingConfigError(f"{label}.id is required")
        if not _CASE_ID_RE.fullmatch(case_id):
            raise OnboardingConfigError(
                f"{label}.id must contain only letters, digits, dot, underscore, or hyphen"
            )
        target = value.get("target")
        if not isinstance(target, Mapping):
            raise OnboardingConfigError(f"{label}.target must be a mapping")
        roundtrip = str(value.get("roundtrip") or "")
        if roundtrip not in ALLOWED_ROUNDTRIPS:
            raise OnboardingConfigError(
                f"{label}.roundtrip must be one of: {', '.join(sorted(ALLOWED_ROUNDTRIPS))}"
            )
        expected = value.get("expected", target)
        if not isinstance(expected, Mapping):
            raise OnboardingConfigError(f"{label}.expected must be a mapping")
        timeout_s = float(value.get("timeout_s") or 300.0)
        if timeout_s <= 0:
            raise OnboardingConfigError(f"{label}.timeout_s must be positive")
        verifier_raw = value.get("verifier")
        verifier = (
            CommandSpec.from_mapping(verifier_raw, label=f"{label}.verifier")
            if verifier_raw is not None
            else None
        )
        return cls(
            id=case_id,
            target=dict(target),
            roundtrip=roundtrip,
            expected=dict(expected),
            timeout_s=timeout_s,
            verifier=verifier,
        )


@dataclass(frozen=True)
class AppSpec:
    id: str
    family: str
    remote: str
    clean_commit: str
    capability_brief: str
    runtime_notes: str
    prepare: tuple[CommandSpec, ...]
    checks: Mapping[str, CommandSpec]
    development_smoke: CaseSpec
    held_out: tuple[CaseSpec, CaseSpec]
    reference: CommandSpec | None
    driver_env: Mapping[str, str]
    lfs_skip_smudge: bool = False
    development_suite: tuple[CaseSpec, ...] = ()

    @property
    def development_cases(self) -> tuple[CaseSpec, ...]:
        """Return the visible acceptance suite, with pilot-v1 compatibility."""

        return self.development_suite or (self.development_smoke,)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, index: int) -> "AppSpec":
        label = f"apps[{index}]"
        if not isinstance(value, Mapping):
            raise OnboardingConfigError(f"{label} must be a mapping")
        app_id = str(value.get("id") or "").strip()
        if not app_id:
            raise OnboardingConfigError(f"{label}.id is required")
        remote = str(value.get("remote") or "").strip()
        if not remote.startswith("https://") or not remote.endswith(".git"):
            raise OnboardingConfigError(f"{label}.remote must be an HTTPS Git URL")
        clean_commit = str(value.get("clean_commit") or "").strip().lower()
        if not _COMMIT_RE.fullmatch(clean_commit):
            raise OnboardingConfigError(f"{label}.clean_commit must be a 40-character Git commit ID")
        capability = str(value.get("capability_brief") or "").strip()
        if not capability:
            raise OnboardingConfigError(f"{label}.capability_brief is required")
        prepare_raw = value.get("prepare") or []
        if not isinstance(prepare_raw, list):
            raise OnboardingConfigError(f"{label}.prepare must be a list")
        prepare = tuple(
            CommandSpec.from_mapping(item, label=f"{label}.prepare[{offset}]")
            for offset, item in enumerate(prepare_raw)
        )
        checks_raw = value.get("checks") or {}
        if not isinstance(checks_raw, Mapping):
            raise OnboardingConfigError(f"{label}.checks must be a mapping")
        checks = {
            str(name): CommandSpec.from_mapping(item, label=f"{label}.checks.{name}")
            for name, item in checks_raw.items()
        }
        development_suite_raw = value.get("development_suite")
        if development_suite_raw is not None:
            if not isinstance(development_suite_raw, list) or not 3 <= len(development_suite_raw) <= 5:
                raise OnboardingConfigError(
                    f"{label}.development_suite must contain between three and five cases"
                )
            development_suite = tuple(
                CaseSpec.from_mapping(item, label=f"{label}.development_suite[{offset}]")
                for offset, item in enumerate(development_suite_raw)
            )
            development = development_suite[0]
        else:
            development = CaseSpec.from_mapping(
                value.get("development_smoke") or {}, label=f"{label}.development_smoke"
            )
            development_suite = ()
        held_raw = value.get("held_out")
        if not isinstance(held_raw, list) or len(held_raw) != 2:
            raise OnboardingConfigError(f"{label}.held_out must contain exactly two cases")
        held = tuple(
            CaseSpec.from_mapping(item, label=f"{label}.held_out[{offset}]")
            for offset, item in enumerate(held_raw)
        )
        case_ids = [case.id for case in (*development_suite, *held)] if development_suite else [development.id, *[case.id for case in held]]
        if len(case_ids) != len(set(case_ids)):
            raise OnboardingConfigError(f"{label} development and held-out case ids must be unique")
        development_identities = {
            json.dumps(
                {"target": case.target, "roundtrip": case.roundtrip},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for case in (development_suite or (development,))
        }
        held_identities = {
            json.dumps(
                {"target": case.target, "roundtrip": case.roundtrip},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for case in held
        }
        if development_identities & held_identities:
            raise OnboardingConfigError(f"{label} development cases must not duplicate held-out cases")
        reference_raw = value.get("reference")
        reference = (
            CommandSpec.from_mapping(reference_raw, label=f"{label}.reference")
            if reference_raw is not None
            else None
        )
        driver_env_raw = value.get("driver_env") or {}
        if not isinstance(driver_env_raw, Mapping):
            raise OnboardingConfigError(f"{label}.driver_env must be a mapping")
        return cls(
            id=app_id,
            family=str(value.get("family") or app_id),
            remote=remote,
            clean_commit=clean_commit,
            capability_brief=capability,
            runtime_notes=str(value.get("runtime_notes") or ""),
            prepare=prepare,
            checks=checks,
            development_smoke=development,
            held_out=(held[0], held[1]),
            reference=reference,
            driver_env={str(key): str(item) for key, item in driver_env_raw.items()},
            lfs_skip_smudge=bool(value.get("lfs_skip_smudge", False)),
            development_suite=development_suite,
        )


@dataclass(frozen=True)
class OnboardingManifest:
    schema_version: int
    pilot_id: str
    model: str
    reasoning_effort: str
    prompt_path: Path
    apps: tuple[AppSpec, ...]
    require_agent_self_tests: bool = False
    self_test_min_cases: int = 5
    self_test_max_cases: int = 10
    self_test_timeout_s: float = 1_800.0
    agent_max_rounds: int = 40
    agent_max_tool_calls: int = 100
    agent_wall_time_s: float = 3_600.0


def _load_manifest_mapping(source: Path) -> Mapping[str, Any]:
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise OnboardingConfigError(f"cannot load onboarding manifest {source}: {error}") from error
    if not isinstance(raw, Mapping):
        raise OnboardingConfigError("onboarding manifest must be a mapping")
    base_value = raw.get("base_manifest")
    if base_value is None:
        return raw
    base_path = (source.parent / str(base_value)).resolve()
    base = dict(_load_manifest_mapping(base_path))
    base_apps = base.get("apps")
    overrides = raw.get("app_overrides")
    if not isinstance(base_apps, list) or not isinstance(overrides, Mapping):
        raise OnboardingConfigError("overlay manifest requires base apps and app_overrides")
    base_ids = [str(app.get("id") or "") for app in base_apps if isinstance(app, Mapping)]
    unknown = sorted(set(str(key) for key in overrides) - set(base_ids))
    if unknown:
        raise OnboardingConfigError("overlay manifest has unknown app overrides: " + ", ".join(unknown))
    missing = sorted(set(base_ids) - set(str(key) for key in overrides))
    if missing:
        raise OnboardingConfigError("overlay manifest must override every app: " + ", ".join(missing))
    merged_apps: list[dict[str, Any]] = []
    for app in base_apps:
        if not isinstance(app, Mapping):
            raise OnboardingConfigError("base manifest app must be a mapping")
        app_id = str(app.get("id") or "")
        override = overrides.get(app_id)
        if not isinstance(override, Mapping):
            raise OnboardingConfigError(f"app_overrides.{app_id} must be a mapping")
        merged = dict(app)
        merged.update(override)
        merged_apps.append(merged)
    for key in (
        "schema_version",
        "pilot_id",
        "model",
        "reasoning_effort",
        "prompt",
        "agent_self_tests",
        "agent_limits",
    ):
        if key in raw:
            base[key] = raw[key]
    base["apps"] = merged_apps
    return base


def load_manifest(path: str | Path = DEFAULT_MANIFEST) -> OnboardingManifest:
    source = Path(path).expanduser().resolve()
    raw = _load_manifest_mapping(source)
    schema_version = int(raw.get("schema_version") or 0)
    if schema_version != 1:
        raise OnboardingConfigError("onboarding manifest schema_version must be 1")
    apps_raw = raw.get("apps")
    if not isinstance(apps_raw, list):
        raise OnboardingConfigError("onboarding manifest apps must be a list")
    apps = tuple(AppSpec.from_mapping(item, index=index) for index, item in enumerate(apps_raw))
    if len(apps) != 10:
        raise OnboardingConfigError(
            f"onboarding manifest must register exactly ten apps; found {len(apps)}"
        )
    ids = [app.id for app in apps]
    if len(ids) != len(set(ids)):
        raise OnboardingConfigError("onboarding manifest app ids must be unique")
    if "build_darwin_lite" in ids:
        raise OnboardingConfigError("build_darwin_lite is a build artifact, not an onboarding app")
    prompt_value = str(raw.get("prompt") or "prompt.md")
    prompt_path = (source.parent / prompt_value).resolve()
    if not prompt_path.is_file():
        raise OnboardingConfigError(f"onboarding prompt does not exist: {prompt_path}")
    self_tests_raw = raw.get("agent_self_tests") or {}
    if not isinstance(self_tests_raw, Mapping):
        raise OnboardingConfigError("agent_self_tests must be a mapping")
    require_self_tests = bool(self_tests_raw.get("required", False))
    min_cases = int(self_tests_raw.get("min_cases") or 5)
    max_cases = int(self_tests_raw.get("max_cases") or 10)
    self_test_timeout_s = float(self_tests_raw.get("timeout_s") or 1_800.0)
    if min_cases < 1 or max_cases < min_cases or max_cases > 20:
        raise OnboardingConfigError("agent_self_tests case bounds must satisfy 1 <= min <= max <= 20")
    if self_test_timeout_s <= 0:
        raise OnboardingConfigError("agent_self_tests.timeout_s must be positive")
    limits_raw = raw.get("agent_limits") or {}
    if not isinstance(limits_raw, Mapping):
        raise OnboardingConfigError("agent_limits must be a mapping")
    agent_max_rounds = int(limits_raw.get("max_rounds") or 40)
    agent_max_tool_calls = int(limits_raw.get("max_tool_calls") or 100)
    agent_wall_time_s = float(limits_raw.get("wall_time_s") or 3_600.0)
    if agent_max_rounds <= 0 or agent_max_tool_calls <= 0 or agent_wall_time_s <= 0:
        raise OnboardingConfigError("agent_limits values must be positive")
    return OnboardingManifest(
        schema_version=schema_version,
        pilot_id=str(raw.get("pilot_id") or "pilot-v1"),
        model=str(raw.get("model") or DEFAULT_MODEL),
        reasoning_effort=str(raw.get("reasoning_effort") or DEFAULT_REASONING_EFFORT),
        prompt_path=prompt_path,
        apps=apps,
        require_agent_self_tests=require_self_tests,
        self_test_min_cases=min_cases,
        self_test_max_cases=max_cases,
        self_test_timeout_s=self_test_timeout_s,
        agent_max_rounds=agent_max_rounds,
        agent_max_tool_calls=agent_max_tool_calls,
        agent_wall_time_s=agent_wall_time_s,
    )


def materialize_clean_source(app: AppSpec, destination: str | Path, log_path: str | Path) -> Path:
    repo = Path(destination).expanduser().resolve()
    log = Path(log_path).expanduser().resolve()
    log.parent.mkdir(parents=True, exist_ok=True)
    if repo.exists():
        raise SourceMaterializationError(f"destination already exists: {repo}")
    repo.mkdir(parents=True)
    commands = [
        ["git", "init"],
        ["git", "remote", "add", "origin", app.remote],
        ["git", "fetch", "--depth", "1", "origin", app.clean_commit],
        ["git", "checkout", "--detach", "FETCH_HEAD"],
        ["git", "remote", "remove", "origin"],
    ]
    output: list[str] = []
    try:
        for command in commands:
            started = time.perf_counter()
            source_env = {"GIT_TERMINAL_PROMPT": "0"}
            if app.lfs_skip_smudge:
                source_env["GIT_LFS_SKIP_SMUDGE"] = "1"
            completed = subprocess.run(
                command,
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=1_800,
                env=sanitized_subprocess_env(source_env),
            )
            output.extend(
                [
                    f"$ {' '.join(command)}",
                    completed.stdout,
                    completed.stderr,
                    f"exit={completed.returncode} elapsed_ms={int((time.perf_counter() - started) * 1000)}",
                ]
            )
            if completed.returncode != 0:
                raise SourceMaterializationError(
                    f"source command failed for {app.id}: {' '.join(command)}: "
                    f"{(completed.stderr or completed.stdout).strip()}"
                )
        head = _git_output(repo, ["rev-parse", "HEAD"]).strip().lower()
        if head != app.clean_commit:
            raise SourceMaterializationError(
                f"materialized {app.id} at {head}, expected {app.clean_commit}"
            )
        if _git_output(repo, ["remote"]).strip():
            raise SourceMaterializationError(f"materialized {app.id} still has a Git remote")
        return repo
    finally:
        log.write_text("\n".join(output), encoding="utf-8")


def build_agent_prompts(
    manifest: OnboardingManifest,
    app: AppSpec,
    workspace: HookAgentWorkspace,
) -> tuple[str, str]:
    base = manifest.prompt_path.read_text(encoding="utf-8")
    check_names = {*app.checks.keys(), "development_smoke"}
    if len(app.development_cases) > 1:
        check_names.add("development_suite")
    require_self_tests = bool(getattr(manifest, "require_agent_self_tests", False))
    if require_self_tests:
        check_names.add("self_tests")
    user_payload = {
        "pilot_id": manifest.pilot_id,
        "app_id": app.id,
        "family": app.family,
        "capability_brief": app.capability_brief,
        "runtime_notes": app.runtime_notes,
        "available_checks": sorted(check_names),
        "development_suite": [
            {
                "id": case.id,
                **case_request(case, artifact_dir="<provided by check>"),
                "expected": dict(case.expected),
            }
            for case in app.development_cases
        ],
    }
    if require_self_tests:
        user_payload["agent_self_tests"] = {
            "required": True,
            "path": "warptest_onboarding/self_tests/cases.yaml",
            "min_cases": int(getattr(manifest, "self_test_min_cases", 5)),
            "max_cases": int(getattr(manifest, "self_test_max_cases", 10)),
            "required_roundtrips": sorted(ALLOWED_ROUNDTRIPS),
            "must_not_duplicate_development_suite": True,
        }
    user_prompt = "\n".join(
        [
            "Application-specific onboarding brief:",
            yaml.safe_dump(user_payload, sort_keys=False, allow_unicode=True),
            "Clean repository map:",
            workspace.repo_map(),
            "Implement the reusable hook and driver now. Do not stop at a recommendation.",
        ]
    )
    return base, user_prompt


def case_request(case: CaseSpec, *, artifact_dir: str | Path) -> dict[str, Any]:
    return {
        "target": dict(case.target),
        "roundtrip": case.roundtrip,
        "artifact_dir": str(artifact_dir),
    }


def case_definition(case: CaseSpec) -> dict[str, Any]:
    """Serialize a public benchmark case without exposing hidden metadata."""

    return {
        "id": case.id,
        "target": dict(case.target),
        "roundtrip": case.roundtrip,
        "expected": dict(case.expected),
        "timeout_s": case.timeout_s,
    }


def discover_playwright_chromium() -> str | None:
    """Return an installed Playwright Chromium executable without downloading."""

    candidates = sorted(
        Path.home().glob(
            "Library/Caches/ms-playwright/chromium_headless_shell-*/"
            "chrome-headless-shell-mac-arm64/chrome-headless-shell"
        ),
        reverse=True,
    )
    candidates.extend(
        [
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ]
    )
    return str(next((path.resolve() for path in candidates if path.is_file()), "")) or None


def validate_contract(repo: str | Path, expected_app_id: str) -> tuple[dict[str, Any] | None, list[str]]:
    root = Path(repo).expanduser().resolve()
    path = root / "warptest_onboarding" / "contract.yaml"
    errors: list[str] = []
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        return None, [f"cannot load contract.yaml: {error}"]
    if not isinstance(value, dict):
        return None, ["contract.yaml must contain one mapping"]
    if value.get("schema_version") != 1:
        errors.append("contract.schema_version must be 1")
    if value.get("app_id") != expected_app_id:
        errors.append(f"contract.app_id must equal {expected_app_id!r}")
    if value.get("hook_kind") not in ALLOWED_HOOK_KINDS:
        errors.append("contract.hook_kind is invalid")
    gate = value.get("gate")
    if not isinstance(gate, dict):
        errors.append("contract.gate must be a mapping")
    elif gate.get("normal_mode_absent") not in {True, False, None}:
        errors.append("contract.gate.normal_mode_absent must be true, false, or null")
    if value.get("driver") != "warptest_onboarding/driver.py":
        errors.append("contract.driver must be warptest_onboarding/driver.py")
    driver = root / "warptest_onboarding" / "driver.py"
    if not driver.is_file():
        errors.append("warptest_onboarding/driver.py is missing")
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, list) or not {"write", "read", "save_reload"}.issubset(
        {str(item) for item in capabilities or []}
    ):
        errors.append("contract.capabilities must include write, read, and save_reload")
    product_files = value.get("product_files_changed")
    if not isinstance(product_files, list) or any(not isinstance(item, str) for item in product_files or []):
        errors.append("contract.product_files_changed must be a list of strings")
    forbidden = find_forbidden_result_keys(value)
    if forbidden:
        errors.append("contract contains forbidden hash/fingerprint keys: " + ", ".join(forbidden))
    return value, errors


def run_driver_case(
    repo: str | Path,
    case: CaseSpec,
    report_dir: str | Path,
    *,
    driver_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    root = Path(repo).expanduser().resolve()
    out = Path(report_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    artifact_dir = out / f"{case.id}-artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    request_path = out / f"{case.id}-request.json"
    report_path = out / f"{case.id}.json"
    stdout_path = out / f"{case.id}-stdout.log"
    stderr_path = out / f"{case.id}-stderr.log"
    request_path.write_text(
        json.dumps(case_request(case, artifact_dir=artifact_dir), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    driver = root / "warptest_onboarding" / "driver.py"
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(driver),
                "--request",
                str(request_path),
                "--report",
                str(report_path),
            ],
            cwd=root,
            env=sanitized_subprocess_env(driver_env),
            capture_output=True,
            text=True,
            timeout=case.timeout_s,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        stdout_path.write_text(completed.stdout or "", encoding="utf-8")
        stderr_path.write_text(completed.stderr or "", encoding="utf-8")
    except subprocess.TimeoutExpired as error:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        stdout_path.write_text(_text_output(error.stdout), encoding="utf-8")
        stderr_path.write_text(_text_output(error.stderr), encoding="utf-8")
        return {
            "id": case.id,
            "status": "failure",
            "stage": "driver_timeout",
            "elapsed_ms": elapsed_ms,
            "errors": [f"driver timed out after {case.timeout_s:g}s"],
            "request_path": str(request_path),
            "report_path": str(report_path),
        }
    except OSError as error:
        return {
            "id": case.id,
            "status": "failure",
            "stage": "driver_launch",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "errors": [safe_error_text(error)],
            "request_path": str(request_path),
            "report_path": str(report_path),
        }

    errors: list[str] = []
    payload: dict[str, Any] | None = None
    if completed.returncode != 0:
        errors.append(f"driver exit code was {completed.returncode}")
    try:
        parsed = json.loads(report_path.read_text(encoding="utf-8"))
        payload = parsed if isinstance(parsed, dict) else None
        if payload is None:
            errors.append("driver report must be a JSON object")
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"cannot load driver report: {error}")
    if payload is not None:
        if payload.get("status") != "success":
            errors.append("driver report status was not success")
        if payload.get("app_started") is not True:
            errors.append("driver report app_started was not true")
        if payload.get("roundtrip") != case.roundtrip:
            errors.append("driver report roundtrip did not match request")
        observed = payload.get("observed")
        if not isinstance(observed, Mapping):
            errors.append("driver report observed must be a mapping")
        else:
            errors.extend(projection_mismatches(observed, case.expected))
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list):
            errors.append("driver report artifacts must be a list")
        elif case.roundtrip == "save_reload" and not artifacts:
            errors.append("save_reload report must include at least one application artifact")
        elif isinstance(artifacts, list):
            for index, raw_artifact in enumerate(artifacts):
                if not isinstance(raw_artifact, str) or not raw_artifact:
                    errors.append(f"driver artifact[{index}] must be a non-empty path")
                    continue
                candidate = Path(raw_artifact).expanduser().resolve()
                if candidate != artifact_dir and artifact_dir not in candidate.parents:
                    errors.append(f"driver artifact[{index}] is outside artifact_dir")
                elif candidate.is_symlink() or not candidate.is_file():
                    errors.append(f"driver artifact[{index}] is not a regular file")
                elif candidate.stat().st_size <= 0:
                    errors.append(f"driver artifact[{index}] is empty")
        forbidden = find_forbidden_result_keys(payload)
        if forbidden:
            errors.append("driver report contains forbidden hash/fingerprint keys: " + ", ".join(forbidden))
    verifier = None
    if not errors and case.verifier is not None:
        verifier = run_command(
            case.verifier,
            repo=root,
            report_dir=out,
            placeholders={
                "request": str(request_path),
                "case_report": str(report_path),
                "artifact_dir": str(artifact_dir),
            },
            log_name=f"{case.id}-verifier.log",
        )
        if verifier["exit_code"] != 0:
            errors.append("independent verifier failed")
    return {
        "id": case.id,
        "status": "success" if not errors else "failure",
        "stage": "complete" if not errors else "driver_validation",
        "elapsed_ms": elapsed_ms,
        "exit_code": completed.returncode,
        "errors": errors,
        "request_path": str(request_path),
        "report_path": str(report_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "payload": payload,
        "verifier": verifier,
    }


def projection_mismatches(actual: Any, expected: Any, path: str = "observed") -> list[str]:
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return [f"{path} expected a mapping"]
        errors: list[str] = []
        for key, value in expected.items():
            child = f"{path}.{key}"
            if key not in actual:
                errors.append(f"{child} is missing")
            else:
                errors.extend(projection_mismatches(actual[key], value, child))
        return errors
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path} expected a list"]
        if len(actual) != len(expected):
            return [f"{path} length {len(actual)} != {len(expected)}"]
        errors: list[str] = []
        for index, value in enumerate(expected):
            errors.extend(projection_mismatches(actual[index], value, f"{path}[{index}]"))
        return errors
    return [] if actual == expected else [f"{path} expected {expected!r}, got {actual!r}"]


def find_forbidden_result_keys(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            text = str(key)
            child = f"{path}.{text}" if path else text
            if _FORBIDDEN_RESULT_KEY.search(text):
                found.append(child)
            found.extend(find_forbidden_result_keys(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(find_forbidden_result_keys(item, f"{path}[{index}]"))
    return found


def run_command(
    command: CommandSpec,
    *,
    repo: Path,
    report_dir: Path,
    placeholders: Mapping[str, str] | None = None,
    log_name: str,
) -> dict[str, Any]:
    replacements = {
        "repo": str(repo),
        "report_dir": str(report_dir),
        "python": sys.executable,
        "harness_root": str(Path(__file__).resolve().parents[1]),
        **dict(placeholders or {}),
    }
    argv = [expand_placeholders(item, replacements) for item in command.argv]
    cwd_text = expand_placeholders(command.cwd, replacements)
    cwd_path = Path(cwd_text)
    cwd = cwd_path if cwd_path.is_absolute() else repo / cwd_path
    cwd = cwd.resolve()
    if cwd != repo and repo not in cwd.parents:
        raise OnboardingConfigError(f"command cwd escapes repository: {command.cwd}")
    env = {
        key: expand_placeholders(value, replacements)
        for key, value in command.env.items()
    }
    log_path = report_dir / log_name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=sanitized_subprocess_env(env),
            capture_output=True,
            text=True,
            timeout=command.timeout_s,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        output = (completed.stdout or "") + "\n[stderr]\n" + (completed.stderr or "")
        log_path.write_text(output, encoding="utf-8")
        return {
            "exit_code": completed.returncode,
            "elapsed_ms": elapsed_ms,
            "log_path": str(log_path),
        }
    except subprocess.TimeoutExpired as error:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        output = _text_output(error.stdout) + "\n[stderr]\n" + _text_output(error.stderr)
        log_path.write_text(output, encoding="utf-8")
        return {
            "exit_code": None,
            "elapsed_ms": elapsed_ms,
            "log_path": str(log_path),
            "error": f"timed out after {command.timeout_s:g}s",
        }
    except OSError as error:
        return {
            "exit_code": None,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "log_path": str(log_path),
            "error": safe_error_text(error),
        }


def expand_placeholders(value: str, replacements: Mapping[str, str]) -> str:
    result = value
    for name, replacement in replacements.items():
        result = result.replace("{" + name + "}", replacement)
    unresolved = re.findall(r"\{[A-Za-z_][A-Za-z0-9_]*\}", result)
    if unresolved:
        raise OnboardingConfigError("unresolved command placeholders: " + ", ".join(unresolved))
    return result


def write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    forbidden = find_forbidden_result_keys(value)
    if forbidden:
        raise ValueError("result contains forbidden hash/fingerprint keys: " + ", ".join(forbidden))
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def run_provider_preflight(
    credentials: str | Path,
    *,
    model: str,
    reasoning_effort: str,
    report_dir: str | Path,
) -> dict[str, Any]:
    import requests
    from openai import OpenAI

    out = Path(report_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with relay_environment(credentials) as provider:
        client = OpenAI(
            api_key=os.environ["RELAY_API_KEY"],
            base_url=provider.base_url.rstrip("/") + "/",
            timeout=900,
            max_retries=0,
        )
        models_response = requests.get(
            provider.base_url.rstrip("/") + "/models",
            headers={"Authorization": "Bearer " + os.environ["RELAY_API_KEY"]},
            timeout=30,
        )
        models_response.raise_for_status()
        models_payload = models_response.json()
        model_items = (
            models_payload
            if isinstance(models_payload, list)
            else models_payload.get("data", [])
            if isinstance(models_payload, dict)
            else []
        )
        ids = [
            str(item.get("id") or item.get("name") or "")
            for item in model_items
            if isinstance(item, dict)
        ]
        if model not in ids:
            raise RuntimeError(f"provider model list does not contain {model}")
        tool = {
            "type": "function",
            "name": "list_dir",
            "description": "Return a synthetic directory listing for connectivity preflight.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            "strict": True,
        }
        first = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": "Call list_dir exactly once, then summarize its result."},
                {"role": "user", "content": "Inspect the synthetic repository root."},
            ],
            tools=[tool],
            reasoning={"effort": reasoning_effort},
            max_output_tokens=256,
        )
        calls = [item for item in getattr(first, "output", []) if getattr(item, "type", None) == "function_call"]
        if len(calls) != 1 or getattr(calls[0], "name", None) != "list_dir":
            raise RuntimeError("provider preflight did not return the expected list_dir function call")
        second = client.responses.create(
            model=model,
            previous_response_id=first.id,
            input=[
                {
                    "type": "function_call_output",
                    "call_id": calls[0].call_id,
                    "output": "README.md\nsrc\ntests",
                }
            ],
            tools=[tool],
            reasoning={"effort": reasoning_effort},
            max_output_tokens=256,
        )
        if not str(getattr(second, "output_text", "") or "").strip():
            raise RuntimeError("provider preflight continuation returned no public text")
        result = {
            "status": "pass",
            "model": model,
            "reasoning_effort": reasoning_effort,
            "provider_base_url": provider.base_url,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "responses": [
                _preflight_response(first),
                _preflight_response(second),
            ],
        }
    write_json(out / "provider-preflight.json", result)
    return result


def _preflight_response(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    return {
        "response_id": getattr(response, "id", None),
        "status": getattr(response, "status", None),
        "provider_model": getattr(response, "model", None),
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def run_hook_onboarding(
    *,
    manifest_path: str | Path = DEFAULT_MANIFEST,
    credentials: str | Path = DEFAULT_CREDENTIALS,
    model: str | None = None,
    reasoning_effort: str | None = None,
    app_ids: Sequence[str] | None = None,
    report_dir: str | Path | None = None,
    reference_only: bool = False,
    skip_reference: bool = False,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    selected_model = model or manifest.model
    selected_effort = reasoning_effort or manifest.reasoning_effort
    root_report = (
        Path(report_dir).expanduser().resolve()
        if report_dir is not None
        else Path("reports/warptest")
        / ("onboarding-pilot-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    )
    root_report.mkdir(parents=True, exist_ok=True)
    selected = list(manifest.apps)
    if app_ids:
        requested = list(dict.fromkeys(app_ids))
        known = {app.id for app in selected}
        unknown = [item for item in requested if item not in known]
        if unknown:
            raise OnboardingConfigError("unknown onboarding app ids: " + ", ".join(unknown))
        selected = [app for app in selected if app.id in requested]
    results: list[dict[str, Any]] = []
    for app in selected:
        app_report = root_report / app.id
        app_report.mkdir(parents=True, exist_ok=True)
        reference_result = None
        if not skip_reference:
            reference_result = _run_reference_app(app, app_report / "reference")
            write_json(app_report / "reference.json", reference_result)
        if reference_only:
            results.append(
                {
                    "app_id": app.id,
                    "status": (
                        "pass" if reference_result and reference_result.get("status") == "pass" else "reference_failed"
                    ),
                    "reference": reference_result,
                }
            )
            continue
        if reference_result is not None and reference_result.get("status") != "pass":
            results.append(
                {
                    "app_id": app.id,
                    "status": "infrastructure_blocked",
                    "reference": reference_result,
                }
            )
            continue
        results.append(
            _run_agent_app(
                manifest,
                app,
                app_report,
                credentials=credentials,
                model=selected_model,
                reasoning_effort=selected_effort,
                reference=reference_result,
            )
        )
    summary = {
        "schema_version": 1,
        "pilot_id": manifest.pilot_id,
        "model": selected_model,
        "reasoning_effort": selected_effort,
        "reference_only": reference_only,
        "apps_requested": [app.id for app in selected],
        "results": results,
        "counts": dict(sorted(_status_counts(results).items())),
    }
    write_json(root_report / "summary.json", summary)
    (root_report / "summary.md").write_text(render_summary_markdown(summary), encoding="utf-8")
    return {**summary, "report_dir": str(root_report)}


def _run_reference_app(app: AppSpec, report_dir: Path) -> dict[str, Any]:
    report_dir.mkdir(parents=True, exist_ok=True)
    if app.reference is None:
        return {"status": "failure", "error": "reference command is not registered"}
    scratch = Path(tempfile.mkdtemp(prefix=f"warptest-onboarding-reference-{app.id}-"))
    try:
        repo = materialize_clean_source(app, scratch / "repo", report_dir / "source.log")
        write_json(
            report_dir / "source.json",
            {"app_id": app.id, "remote": app.remote, "clean_commit": app.clean_commit},
        )
        prepare = _run_prepare(app, repo, report_dir)
        if any(item.get("exit_code") != 0 for item in prepare):
            return {"status": "failure", "stage": "prepare", "prepare": prepare}
        commit_prepared_baseline(repo)
        result = run_command(
            app.reference,
            repo=repo,
            report_dir=report_dir,
            placeholders={},
            log_name="reference.log",
        )
        return {
            "status": "pass" if result.get("exit_code") == 0 else "failure",
            "stage": "complete" if result.get("exit_code") == 0 else "reference",
            "prepare": prepare,
            "command": result,
        }
    except SourceMaterializationError as error:
        return {"status": "failure", "stage": "source", "error": safe_error_text(error)}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _run_agent_app(
    manifest: OnboardingManifest,
    app: AppSpec,
    report_dir: Path,
    *,
    credentials: str | Path,
    model: str,
    reasoning_effort: str,
    reference: Mapping[str, Any] | None,
) -> dict[str, Any]:
    scratch = Path(tempfile.mkdtemp(prefix=f"warptest-onboarding-agent-{app.id}-"))
    try:
        repo = materialize_clean_source(app, scratch / "repo", report_dir / "source.log")
        write_json(
            report_dir / "source.json",
            {"app_id": app.id, "remote": app.remote, "clean_commit": app.clean_commit},
        )
        prepare = _run_prepare(app, repo, report_dir)
        if any(item.get("exit_code") != 0 for item in prepare):
            return {
                "app_id": app.id,
                "status": "infrastructure_blocked",
                "stage": "prepare",
                "prepare": prepare,
                "reference": reference,
            }
        commit_prepared_baseline(repo)
        development_request = report_dir / "development-smoke-request.json"
        development_report = report_dir / "development-smoke.json"
        development_artifacts = report_dir / "development-smoke-artifacts"
        development_artifacts.mkdir(parents=True, exist_ok=True)
        development_request.write_text(
            json.dumps(
                case_request(app.development_smoke, artifact_dir=development_artifacts),
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        development_suite_path = report_dir / "development-suite-input.json"
        write_json(
            development_suite_path,
            {
                "schema_version": 1,
                "app_id": app.id,
                "cases": [case_definition(case) for case in app.development_cases],
            },
        )
        replacements = {
            "repo": str(repo),
            "report_dir": str(report_dir),
            "python": sys.executable,
            "harness_root": str(Path(__file__).resolve().parents[1]),
            "development_request": str(development_request),
            "development_report": str(development_report),
            "development_artifact_dir": str(development_artifacts),
        }
        expanded_driver_env = {
            key: expand_placeholders(value, replacements)
            for key, value in app.driver_env.items()
        }
        if app.family.startswith("browser_"):
            browser_executable = discover_playwright_chromium()
            if browser_executable is not None:
                expanded_driver_env.setdefault(
                    "WARPTEST_PLAYWRIGHT_CHROMIUM_EXECUTABLE",
                    browser_executable,
                )
        checks = {
            name: NamedCheck(
                argv=tuple(expand_placeholders(item, replacements) for item in check.argv),
                cwd=expand_placeholders(check.cwd, replacements),
                timeout_s=check.timeout_s,
                env={key: expand_placeholders(value, replacements) for key, value in check.env.items()},
            )
            for name, check in app.checks.items()
        }
        checks["development_smoke"] = NamedCheck(
            argv=(
                sys.executable,
                "warptest_onboarding/driver.py",
                "--request",
                str(development_request),
                "--report",
                str(development_report),
            ),
            timeout_s=app.development_smoke.timeout_s,
            env=expanded_driver_env,
        )
        harness_root = Path(__file__).resolve().parents[1]
        if len(app.development_cases) > 1:
            checks["development_suite"] = NamedCheck(
                argv=(
                    sys.executable,
                    str(harness_root / "scripts" / "run_hook_onboarding_suite.py"),
                    "--mode",
                    "development",
                    "--repo",
                    str(repo),
                    "--app",
                    app.id,
                    "--suite",
                    str(development_suite_path),
                    "--report-dir",
                    str(report_dir / "agent-development-suite-check"),
                ),
                timeout_s=min(
                    1_800.0,
                    sum(case.timeout_s for case in app.development_cases) + 60.0,
                ),
                env=expanded_driver_env,
            )
        if manifest.require_agent_self_tests:
            checks["self_tests"] = NamedCheck(
                argv=(
                    sys.executable,
                    str(harness_root / "scripts" / "run_hook_onboarding_suite.py"),
                    "--mode",
                    "self-tests",
                    "--repo",
                    str(repo),
                    "--app",
                    app.id,
                    "--suite",
                    str(development_suite_path),
                    "--report-dir",
                    str(report_dir / "agent-self-tests-check"),
                    "--min-cases",
                    str(manifest.self_test_min_cases),
                    "--max-cases",
                    str(manifest.self_test_max_cases),
                ),
                timeout_s=manifest.self_test_timeout_s,
                env=expanded_driver_env,
            )
        workspace = HookAgentWorkspace(repo, report_dir, checks=checks)
        system_prompt, user_prompt = build_agent_prompts(manifest, app, workspace)
        (report_dir / "prompt.txt").write_text(
            system_prompt + "\n\n" + user_prompt,
            encoding="utf-8",
        )
        from openai import OpenAI

        with relay_environment(credentials) as provider:
            client = OpenAI(
                api_key=os.environ["RELAY_API_KEY"],
                base_url=provider.base_url.rstrip("/") + "/",
                timeout=900,
                max_retries=0,
            )
            agent = HookCodingAgent(
                client,
                workspace,
                model=model,
                reasoning_effort=reasoning_effort,
                max_rounds=manifest.agent_max_rounds,
                max_tool_calls=manifest.agent_max_tool_calls,
                wall_time_s=manifest.agent_wall_time_s,
            )
            agent_result = agent.run(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                transcript_path=report_dir / "public-transcript.jsonl",
            )
        (report_dir / "final.txt").write_text(agent_result.final_text + "\n", encoding="utf-8")
        agent_payload = agent_result.to_dict()
        agent_payload["check_attempts"] = dict(sorted(workspace.check_attempts.items()))
        agent_payload["current_check_passes"] = sorted(
            name for name in checks if workspace.check_is_current(name)
        )
        write_json(report_dir / "agent-result.json", agent_payload)
        _save_generated_patch(repo, report_dir / "generated.patch")
        contract, contract_errors = validate_contract(repo, app.id)
        if contract is not None:
            shutil.copy2(repo / "warptest_onboarding" / "contract.yaml", report_dir / "contract.yaml")
        if agent_result.status != "success" or contract_errors:
            return {
                "app_id": app.id,
                "status": "provider_failure" if agent_result.status == "provider_failure" else "agent_failure",
                "stage": "agent" if agent_result.status != "success" else "contract",
                "agent": agent_payload,
                "contract_errors": contract_errors,
                "prepare": prepare,
                "reference": reference,
            }
        required_checks = {"build"} & set(checks)
        if manifest.require_agent_self_tests:
            required_checks.update({"self_tests", "development_suite"})
        stale_or_missing = sorted(
            name for name in required_checks if not workspace.check_is_current(name)
        )
        if stale_or_missing:
            return {
                "app_id": app.id,
                "status": "agent_failure",
                "stage": "required_checks",
                "agent": agent_payload,
                "required_check_errors": [
                    "required checks were not passed after the final source edit: "
                    + ", ".join(stale_or_missing)
                ],
                "prepare": prepare,
                "reference": reference,
            }
        source_state = _git_output(
            repo, ["status", "--porcelain", "--untracked-files=all"]
        )
        from .hook_onboarding_suites import (
            SELF_TEST_PATH,
            load_generated_self_tests,
            run_case_suite,
        )

        self_tests: dict[str, Any] | None = None
        if manifest.require_agent_self_tests:
            try:
                generated_cases, self_test_metadata = load_generated_self_tests(
                    repo,
                    expected_app_id=app.id,
                    development_cases=app.development_cases,
                    min_cases=manifest.self_test_min_cases,
                    max_cases=manifest.self_test_max_cases,
                )
            except OnboardingConfigError as error:
                return {
                    "app_id": app.id,
                    "status": "agent_failure",
                    "stage": "self_tests_validation",
                    "agent": agent_payload,
                    "self_test_errors": [safe_error_text(error)],
                    "prepare": prepare,
                    "reference": reference,
                }
            self_test_copy = report_dir / "agent-self-tests-cases.yaml"
            shutil.copy2(repo / SELF_TEST_PATH, self_test_copy)
            self_tests = run_case_suite(
                repo,
                generated_cases,
                report_dir / "agent-self-tests-final",
                driver_env=expanded_driver_env,
            )
            self_tests["metadata"] = self_test_metadata
            self_tests["cases_path"] = str(self_test_copy)
            write_json(report_dir / "agent-self-tests.json", self_tests)
            if _git_output(
                repo, ["status", "--porcelain", "--untracked-files=all"]
            ) != source_state:
                return {
                    "app_id": app.id,
                    "status": "invalid_manual_intervention",
                    "stage": "self_tests_mutated_source",
                    "agent": agent_payload,
                    "agent_self_tests": self_tests,
                    "reference": reference,
                }

        development_suite = run_case_suite(
            repo,
            app.development_cases,
            report_dir / "development-suite-final",
            driver_env=expanded_driver_env,
        )
        write_json(report_dir / "development-suite.json", development_suite)
        development = development_suite["results"][0]
        before_held = _git_output(
            repo, ["status", "--porcelain", "--untracked-files=all"]
        )
        if before_held != source_state:
            return {
                "app_id": app.id,
                "status": "invalid_manual_intervention",
                "stage": "development_smoke_mutated_source",
                "agent": agent_payload,
                "development_smoke": development,
                "development_suite": development_suite,
                "agent_self_tests": self_tests,
                "reference": reference,
            }
        held_results: list[dict[str, Any]] = []
        for case in app.held_out:
            held_results.append(
                run_driver_case(repo, case, report_dir, driver_env=expanded_driver_env)
            )
            if _git_output(
                repo, ["status", "--porcelain", "--untracked-files=all"]
            ) != before_held:
                return {
                    "app_id": app.id,
                    "status": "invalid_manual_intervention",
                    "stage": f"{case.id}_mutated_source",
                    "agent": agent_payload,
                    "development_smoke": development,
                    "development_suite": development_suite,
                    "agent_self_tests": self_tests,
                    "held_out": held_results,
                    "reference": reference,
                }
        held_passes = sum(item.get("status") == "success" for item in held_results)
        gate = contract.get("gate") if isinstance(contract, dict) else {}
        gate_value = gate.get("normal_mode_absent") if isinstance(gate, dict) else None
        gate_status = "not_applicable" if gate_value is None else ("pass" if gate_value is True else "fail")
        self_tests_pass = self_tests is None or self_tests.get("status") == "pass"
        development_pass = development_suite.get("status") == "pass"
        if not self_tests_pass or not development_pass:
            status = "agent_failure"
        elif held_passes == 2 and gate_status != "fail":
            status = "pass"
        elif held_passes == 0:
            status = "heldout_failure"
        else:
            status = "partial"
        return {
            "app_id": app.id,
            "status": status,
            "stage": "complete",
            "onboarding_success": self_tests_pass and development_pass,
            "self_test_passes": self_tests.get("passes") if self_tests is not None else None,
            "self_test_cases": self_tests.get("case_count") if self_tests is not None else None,
            "development_passes": development_suite.get("passes"),
            "development_cases": development_suite.get("case_count"),
            "heldout_passes": held_passes,
            "reusable_hook_success": held_passes == 2,
            "hook_kind": contract.get("hook_kind") if isinstance(contract, dict) else None,
            "gate_status": gate_status,
            "human_interventions": 0,
            "agent": agent_payload,
            "agent_self_tests": self_tests,
            "development_smoke": development,
            "development_suite": development_suite,
            "held_out": held_results,
            "prepare": prepare,
            "reference": reference,
            "diff": _diff_metrics(repo),
        }
    except SourceMaterializationError as error:
        return {
            "app_id": app.id,
            "status": "source_blocked",
            "stage": "source",
            "error": safe_error_text(error),
            "reference": reference,
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _run_prepare(app: AppSpec, repo: Path, report_dir: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, command in enumerate(app.prepare):
        results.append(
            run_command(
                command,
                repo=repo,
                report_dir=report_dir,
                placeholders={},
                log_name=f"prepare-{index}.log",
            )
        )
    return results


def commit_prepared_baseline(repo: Path) -> bool:
    """Make orchestrator-owned dependency preparation invisible to agent diffs."""

    if not _git_output(repo, ["status", "--porcelain"]).strip():
        return False
    subprocess.run(
        ["git", "config", "user.email", "onboarding-infrastructure@example.invalid"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    subprocess.run(
        ["git", "config", "user.name", "Onboarding Infrastructure"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    subprocess.run(
        ["git", "add", "-A"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    subprocess.run(
        ["git", "commit", "-m", "Prepare onboarding runtime dependencies"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return True


def _save_generated_patch(repo: Path, path: Path) -> None:
    tracked = _git_output(repo, ["diff", "--no-ext-diff"])
    untracked = [
        rel
        for rel in _git_output(
            repo, ["ls-files", "--others", "--exclude-standard"]
        ).splitlines()
        if not _generated_noise(rel)
    ]
    blocks = [tracked.rstrip()]
    for rel in untracked:
        candidate = repo / rel
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        blocks.extend(
            [
                f"\n--- /dev/null\n+++ b/{rel}",
                "@@ new file @@",
                text.rstrip(),
            ]
        )
    path.write_text("\n".join(block for block in blocks if block) + "\n", encoding="utf-8")


def _diff_metrics(repo: Path) -> dict[str, Any]:
    lines = _git_output(repo, ["diff", "--numstat"]).splitlines()
    added = 0
    deleted = 0
    tracked_files = 0
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        tracked_files += 1
        if parts[0].isdigit():
            added += int(parts[0])
        if parts[1].isdigit():
            deleted += int(parts[1])
    untracked = [
        rel
        for rel in _git_output(
            repo, ["ls-files", "--others", "--exclude-standard"]
        ).splitlines()
        if not _generated_noise(rel)
    ]
    for rel in untracked:
        path = repo / rel
        if path.is_file():
            try:
                added += len(path.read_text(encoding="utf-8").splitlines())
            except (OSError, UnicodeDecodeError):
                pass
    return {
        "tracked_files_changed": tracked_files,
        "untracked_files_added": len(untracked),
        "lines_added": added,
        "lines_deleted": deleted,
    }


def _generated_noise(relative: str) -> bool:
    path = Path(relative)
    return bool(
        "__pycache__" in path.parts
        or "Library" in path.parts
        or "node_modules" in path.parts
        or path.suffix in {".pyc", ".pyo"}
    )


def _status_counts(results: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        status = str(result.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def render_summary_markdown(summary: Mapping[str, Any]) -> str:
    lines = [
        "# Hook Onboarding Pilot",
        "",
        "> This report records executed outcomes only. Planned cases are not counted as completed runs.",
        "",
        f"- Pilot: `{summary.get('pilot_id')}`",
        f"- Model: `{summary.get('model')}`",
        f"- Reasoning effort: `{summary.get('reasoning_effort')}`",
        f"- Reference only: `{str(bool(summary.get('reference_only'))).lower()}`",
        "",
        "| App | Status | Self-tests | Development | Held-out | Reusable | Gate |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for result in summary.get("results") or []:
        lines.append(
            "| {app} | {status} | {self_pass}/{self_total} | {dev_pass}/{dev_total} | "
            "{held}/2 | {reuse} | {gate} |".format(
                app=result.get("app_id", ""),
                status=result.get("status", ""),
                self_pass=result.get("self_test_passes") if result.get("self_test_passes") is not None else "-",
                self_total=result.get("self_test_cases") if result.get("self_test_cases") is not None else "-",
                dev_pass=result.get("development_passes") if result.get("development_passes") is not None else "-",
                dev_total=result.get("development_cases") if result.get("development_cases") is not None else "-",
                held=result.get("heldout_passes", "-"),
                reuse=result.get("reusable_hook_success", "-"),
                gate=result.get("gate_status", "-"),
            )
        )
    return "\n".join(lines) + "\n"


def _git_output(repo: Path, args: Sequence[str]) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return completed.stdout


def _text_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the automatic hook-onboarding pilot")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--credentials", default=str(DEFAULT_CREDENTIALS))
    parser.add_argument("--model", default=None)
    parser.add_argument("--reasoning-effort", default=None)
    parser.add_argument("--app", action="append", dest="apps")
    parser.add_argument("--report-dir", default=None)
    parser.add_argument("--reference-only", action="store_true")
    parser.add_argument("--skip-reference", action="store_true")
    parser.add_argument("--provider-preflight", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.reference_only and args.skip_reference:
        raise SystemExit("--reference-only and --skip-reference cannot be combined")
    manifest = load_manifest(args.manifest)
    selected_model = args.model or manifest.model
    selected_effort = args.reasoning_effort or manifest.reasoning_effort
    if args.provider_preflight:
        output = Path(args.report_dir or "reports/warptest/onboarding-provider-preflight")
        result = run_provider_preflight(
            args.credentials,
            model=selected_model,
            reasoning_effort=selected_effort,
            report_dir=output,
        )
    else:
        result = run_hook_onboarding(
            manifest_path=args.manifest,
            credentials=args.credentials,
            model=selected_model,
            reasoning_effort=selected_effort,
            app_ids=args.apps,
            report_dir=args.report_dir,
            reference_only=args.reference_only,
            skip_reference=args.skip_reference,
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))


__all__ = [
    "ALLOWED_RESULT_STATUSES",
    "AppSpec",
    "CaseSpec",
    "CommandSpec",
    "OnboardingConfigError",
    "OnboardingManifest",
    "SourceMaterializationError",
    "build_agent_prompts",
    "case_request",
    "find_forbidden_result_keys",
    "load_manifest",
    "materialize_clean_source",
    "projection_mismatches",
    "run_driver_case",
    "run_hook_onboarding",
    "run_provider_preflight",
    "validate_contract",
    "write_json",
]


if __name__ == "__main__":
    main()
