"""Registered, shell-free prebuild callback for Lazarus version materialization."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

from warptest.lazarus_versions import tree_fingerprint


def create_prebuild_callback(
    argv: Sequence[str],
    *,
    pre_commands: Sequence[Sequence[str]] = (),
    post_commands: Sequence[Sequence[str]] = (),
    artifact_paths: Sequence[str],
    timeout_s: float,
):
    command = tuple(str(value) for value in argv)
    if not command or any(not value for value in command):
        raise ValueError("prebuild command must be a non-empty argv sequence")
    normalized_pre_commands = tuple(
        _validate_command(value, label="prebuild pre-command") for value in pre_commands
    )
    normalized_post_commands = tuple(
        _validate_command(value, label="prebuild post-command")
        for value in post_commands
    )
    if timeout_s <= 0:
        raise ValueError("prebuild timeout must be positive")
    normalized_artifacts: tuple[str, ...] = tuple(
        _validate_relative_path(value) for value in artifact_paths
    )
    if len(normalized_artifacts) != len(set(normalized_artifacts)):
        raise ValueError("prebuild artifact paths must be unique")

    def build(version: str, root: Path) -> dict[str, Any]:
        base = Path(root).expanduser().resolve()
        started = time.monotonic()
        prebuild_steps: list[dict[str, Any]] = []
        postbuild_steps: list[dict[str, Any]] = []
        for index, pre_command in enumerate(normalized_pre_commands):
            prebuild_steps.append(
                _run_registered_command(
                    version=version,
                    command=pre_command,
                    root=base,
                    timeout_s=timeout_s,
                    label=f"prebuild pre-command {index}",
                )
            )
        try:
            completed = subprocess.run(
                list(command),
                cwd=base,
                capture_output=True,
                timeout=timeout_s,
                shell=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"{version} prebuild exceeded {timeout_s:g}s") from error
        stdout_sha = hashlib.sha256(completed.stdout).hexdigest()
        stderr_sha = hashlib.sha256(completed.stderr).hexdigest()
        if completed.returncode != 0:
            raise RuntimeError(
                f"{version} prebuild command exited with exit {completed.returncode}; "
                f"stdout_sha256={stdout_sha}; stderr_sha256={stderr_sha}"
            )
        for index, post_command in enumerate(normalized_post_commands):
            postbuild_steps.append(
                _run_registered_command(
                    version=version,
                    command=post_command,
                    root=base,
                    timeout_s=timeout_s,
                    label=f"prebuild post-command {index}",
                )
            )
        elapsed_ms = (time.monotonic() - started) * 1000.0
        artifacts: dict[str, dict[str, Any]] = {}
        for relative in normalized_artifacts:
            path = (base / relative).resolve()
            if base != path and base not in path.parents:
                raise RuntimeError(
                    f"prebuild artifact escapes version root: {relative}"
                )
            if path.is_file():
                artifacts[relative] = {
                    "kind": "file",
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size": path.stat().st_size,
                }
            elif path.is_dir():
                artifacts[relative] = {
                    "kind": "directory",
                    "sha256": tree_fingerprint(path),
                }
            else:
                raise RuntimeError(f"declared prebuild artifact is missing: {relative}")
        return {
            "status": "passed",
            "version": version,
            "pre_commands": [list(value) for value in normalized_pre_commands],
            "prebuild_steps": prebuild_steps,
            "post_commands": [list(value) for value in normalized_post_commands],
            "postbuild_steps": postbuild_steps,
            "command": list(command),
            "timeout_s": float(timeout_s),
            "elapsed_ms": elapsed_ms,
            "stdout_sha256": stdout_sha,
            "stderr_sha256": stderr_sha,
            "artifacts": artifacts,
        }

    fingerprint_payload = {
        "schema": "c1-lazarus-prebuild-inputs-v2",
        "pre_commands": [list(value) for value in normalized_pre_commands],
        "post_commands": [list(value) for value in normalized_post_commands],
        "argv": list(command),
        "artifact_paths": list(normalized_artifacts),
        "timeout_s": float(timeout_s),
    }
    build.lazarus_build_fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return build


def _validate_command(values: Sequence[str], *, label: str) -> tuple[str, ...]:
    command = tuple(str(value) for value in values)
    if not command or any(not value for value in command):
        raise ValueError(f"{label} must be a non-empty argv sequence")
    return command


def _run_registered_command(
    *,
    version: str,
    command: tuple[str, ...],
    root: Path,
    timeout_s: float,
    label: str,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(command),
            cwd=root,
            capture_output=True,
            timeout=timeout_s,
            shell=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"{version} {label} exceeded {timeout_s:g}s") from error
    elapsed_ms = (time.monotonic() - started) * 1000.0
    stdout_sha = hashlib.sha256(completed.stdout).hexdigest()
    stderr_sha = hashlib.sha256(completed.stderr).hexdigest()
    if completed.returncode != 0:
        raise RuntimeError(
            f"{version} {label} exited with exit {completed.returncode}; "
            f"stdout_sha256={stdout_sha}; stderr_sha256={stderr_sha}"
        )
    return {
        "command": list(command),
        "elapsed_ms": elapsed_ms,
        "stdout_sha256": stdout_sha,
        "stderr_sha256": stderr_sha,
    }


def _validate_relative_path(value: str) -> str:
    path = Path(str(value))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"prebuild artifact path escapes version root: {value!r}")
    return path.as_posix()
