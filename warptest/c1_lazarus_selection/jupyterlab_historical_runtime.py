"""Offline packager and typed readiness evidence for historical JupyterLab."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

from .jupyterlab_regression import JupyterLabRegressionProfile
from .pipeline import StageDecision
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "PACKAGE_JUPYTERLAB_HISTORICAL_RUNTIME_1.0"
_BASE_COMPONENTS = (
    "git",
    "node",
    "npm",
    "karma",
    "mocha",
    "karma_mocha",
    "webpack",
    "typescript",
    "firefox",
)
_BASE_COMMANDS = {
    "git": "bin/git",
    "node": "bin/node",
    "npm": "bin/npm",
    "karma": "node_modules/karma/bin/karma",
    "webpack": "node_modules/webpack/bin/webpack.js",
    "typescript": "node_modules/typescript/bin/tsc",
    "firefox": "bin/firefox",
}
_PACKAGE_METADATA = {
    "npm": "node_modules/npm/package.json",
    "karma": "node_modules/karma/package.json",
    "mocha": "node_modules/mocha/package.json",
    "karma_mocha": "node_modules/karma-mocha/package.json",
    "webpack": "node_modules/webpack/package.json",
    "typescript": "node_modules/typescript/package.json",
}
_NUMBER = re.compile(r"(?:^|[^0-9])v?([0-9]+\.[0-9]+(?:\.[0-9]+)?)(?:$|[^0-9])")


@dataclass(frozen=True)
class RuntimeAudit:
    dependency_id: str
    runtime_root: str
    required_versions: Mapping[str, str]
    observed_versions: Mapping[str, str]
    missing_components: tuple[str, ...]
    mismatched_components: tuple[Mapping[str, str], ...]

    @property
    def ready(self) -> bool:
        return not self.missing_components and not self.mismatched_components

    def decision(self) -> StageDecision:
        if self.ready:
            return StageDecision.passed(
                {
                    "dependency_id": self.dependency_id,
                    "runtime_root": self.runtime_root,
                }
            )
        return StageDecision.terminal(
            "infrastructure_blocked",
            {
                "reason": "historical_runtime_components_unavailable",
                "dependency_id": self.dependency_id,
                "runtime_root": self.runtime_root,
                "missing_components": list(self.missing_components),
                "mismatched_components": [
                    dict(value) for value in self.mismatched_components
                ],
            },
        )


def runtime_preparation_reference(
    profile: JupyterLabRegressionProfile,
) -> ArtifactReference:
    return ArtifactReference(
        f"dependency_preparation/{profile.runtime_dependency_id}/current/v1",
        "dependency_preparation",
        "1.0",
    )


def audit_runtime_source(
    profile: JupyterLabRegressionProfile,
    source_tree: str | Path | None,
) -> RuntimeAudit:
    """Probe only local runtime inputs; never install or execute candidate code."""

    required = dict(profile.required_versions)
    components = _components(profile)
    command_manifest = _commands(profile)
    if source_tree is None:
        return RuntimeAudit(
            dependency_id=profile.runtime_dependency_id,
            runtime_root=profile.runtime_root,
            required_versions=required,
            observed_versions={},
            missing_components=components,
            mismatched_components=(),
        )
    try:
        source = _ordinary_directory(Path(source_tree).absolute(), "runtime source")
    except RuntimeError:
        return RuntimeAudit(
            dependency_id=profile.runtime_dependency_id,
            runtime_root=profile.runtime_root,
            required_versions=required,
            observed_versions={},
            missing_components=components,
            mismatched_components=(),
        )

    missing: list[str] = []
    observed: dict[str, str] = {}
    commands: dict[str, Path] = {}
    for component, relative in command_manifest.items():
        path = source / relative
        if not _regular_executable(path, source):
            missing.append(component)
        else:
            commands[component] = path
    for component, relative in _PACKAGE_METADATA.items():
        path = source / relative
        if not _regular_file(path, source):
            if component not in missing:
                missing.append(component)
            continue
        version = _package_version(path)
        if version is None:
            if component not in missing:
                missing.append(component)
        else:
            observed[component] = version

    probes = {
        "git": (commands.get("git"), "--version"),
        "node": (commands.get("node"), "--version"),
        "firefox": (commands.get("firefox"), "--version"),
        "yarn": (commands.get("yarn"), "--version"),
        "jlpm": (commands.get("jlpm"), "--version"),
    }
    for component, argv in probes.items():
        executable = argv[0]
        if executable is None:
            continue
        version = _probe_version((executable, argv[1]))
        if version is None:
            if component not in missing:
                missing.append(component)
        else:
            observed[component] = version
    node = commands.get("node")
    for component in ("npm", "karma", "webpack", "typescript"):
        command = commands.get(component)
        if node is None or command is None:
            continue
        version = _probe_version((node, command, "--version"))
        if version is None:
            if component not in missing:
                missing.append(component)
        else:
            observed[component] = version

    mismatched = tuple(
        {
            "component": component,
            "expected": required[component],
            "observed": observed[component],
        }
        for component in components
        if component in observed and observed[component] != required[component]
    )
    return RuntimeAudit(
        dependency_id=profile.runtime_dependency_id,
        runtime_root=profile.runtime_root,
        required_versions=required,
        observed_versions=observed,
        missing_components=tuple(
            component for component in components if component in missing
        ),
        mismatched_components=mismatched,
    )


def audit_local_host(profile: JupyterLabRegressionProfile) -> RuntimeAudit:
    """Report useful host mismatches without treating them as package inputs."""

    required = dict(profile.required_versions)
    components = _components(profile)
    observed: dict[str, str] = {}
    probes: dict[str, tuple[Path | str, ...]] = {
        "git": (Path("/usr/bin/git"), "--version"),
        "node": (Path("/opt/homebrew/bin/node"), "--version"),
        "npm": (
            Path("/opt/homebrew/bin/node"),
            Path("/opt/homebrew/lib/node_modules/npm/bin/npm-cli.js"),
            "--version",
        ),
        "firefox": (
            Path("/Applications/Firefox.app/Contents/MacOS/firefox"),
            "--version",
        ),
    }
    for component, argv in probes.items():
        executable_paths = tuple(
            value for value in argv[:-1] if isinstance(value, Path)
        )
        if not executable_paths or any(not path.exists() for path in executable_paths):
            continue
        version = _probe_version(argv)
        if version is not None:
            observed[component] = version
    missing = tuple(component for component in components if component not in observed)
    mismatched = tuple(
        {
            "component": component,
            "expected": required[component],
            "observed": observed[component],
        }
        for component in components
        if component in observed and observed[component] != required[component]
    )
    return RuntimeAudit(
        dependency_id=profile.runtime_dependency_id,
        runtime_root=profile.runtime_root,
        required_versions=required,
        observed_versions=observed,
        missing_components=missing,
        mismatched_components=mismatched,
    )


def write_runtime_preparation(
    *,
    artifact_root: str | Path,
    profile: JupyterLabRegressionProfile,
    audit: RuntimeAudit,
    authorization: AuthorizationRecord,
    created_at: str,
) -> StableArtifact:
    """Persist and typed-reload one readiness/blocker receipt."""

    if audit.dependency_id != profile.runtime_dependency_id:
        raise ValueError("JupyterLab runtime audit identity is invalid")
    if type(authorization) is not AuthorizationRecord:
        raise ValueError("JupyterLab runtime authorization is invalid")
    if authorization.scope != "infrastructure-audit":
        raise PermissionError("JupyterLab runtime authorization scope is invalid")
    reference = runtime_preparation_reference(profile)
    payload = {
        "complete": True,
        "outcome": "ready" if audit.ready else "infrastructure_blocked",
        "reason": None if audit.ready else "historical_runtime_components_unavailable",
        "dependency_id": audit.dependency_id,
        "runtime_root": audit.runtime_root,
        "required_versions": dict(audit.required_versions),
        "observed_versions": dict(audit.observed_versions),
        "missing_components": list(audit.missing_components),
        "mismatched_components": [dict(value) for value in audit.mismatched_components],
    }
    write_stable_artifact(
        artifact_root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=created_at,
        inputs=(),
        payload=payload,
        authorization=authorization,
    )
    artifact = load_stable_artifact(
        artifact_root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )
    if artifact.inputs or artifact.payload != payload:
        raise RuntimeError("JupyterLab runtime preparation typed reload is invalid")
    return artifact


def package_historical_runtime(
    *,
    artifact_root: str | Path,
    profile: JupyterLabRegressionProfile,
    source_tree: str | Path,
    confirmation: str,
) -> Path:
    """Copy a fully audited historical runtime closure without network access."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    root = _ordinary_directory(Path(artifact_root).absolute(), "artifact root")
    source = _ordinary_directory(Path(source_tree).absolute(), "runtime source")
    if root == source or root.is_relative_to(source) or source.is_relative_to(root):
        raise RuntimeError("runtime source and artifact root must be separate")
    audit = audit_runtime_source(profile, source)
    if not audit.ready:
        raise RuntimeError("historical JupyterLab runtime source is not exact")
    files = _closure_files(source)
    command_manifest = _commands(profile)
    required_files = set((*command_manifest.values(), *_PACKAGE_METADATA.values()))
    if not required_files.issubset(files):
        raise RuntimeError("historical JupyterLab runtime closure is incomplete")

    relative = PurePosixPath(profile.runtime_root)
    parent = _ordinary_parent(root, PurePosixPath(*relative.parts[:-1]))
    destination = parent / relative.name
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("registered JupyterLab runtime already exists")
    staging = Path(tempfile.mkdtemp(prefix=".jupyterlab-runtime-", dir=parent))
    try:
        for relative_name in files:
            source_file = source.joinpath(*PurePosixPath(relative_name).parts)
            target = staging.joinpath(*PurePosixPath(relative_name).parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source_file, target, follow_symlinks=False)
            target.chmod(stat.S_IMODE(source_file.lstat().st_mode))
        manifest = {
            "dependency_id": profile.runtime_dependency_id,
            "semantic_version": "1.0",
            "commands": command_manifest,
            "versions": dict(profile.required_versions),
            "closure_files": list(files),
        }
        (staging / "runtime.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, destination)
    finally:
        if staging.exists():
            _remove_tree(staging)
    return destination


def _components(profile: JupyterLabRegressionProfile) -> tuple[str, ...]:
    if profile.package_manager == "jlpm":
        return (*_BASE_COMPONENTS, "yarn", "jlpm")
    return _BASE_COMPONENTS


def _commands(profile: JupyterLabRegressionProfile) -> dict[str, str]:
    commands = dict(_BASE_COMMANDS)
    if profile.package_manager == "jlpm":
        commands.update({"yarn": "bin/yarn", "jlpm": "bin/jlpm"})
    return commands


def _probe_version(argv: tuple[Path | str, ...]) -> str | None:
    try:
        completed = subprocess.run(
            tuple(str(value) for value in argv),
            check=False,
            capture_output=True,
            timeout=15,
            env={"HOME": "/var/empty", "LANG": "C", "LC_ALL": "C", "PATH": ""},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
    match = _NUMBER.search(output)
    return match.group(1) if completed.returncode == 0 and match else None


def _package_version(path: Path) -> str | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError):
        return None
    version = value.get("version") if type(value) is dict else None
    return version if type(version) is str and _NUMBER.fullmatch(version) else None


def _closure_files(root: Path) -> tuple[str, ...]:
    files: list[str] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for entry in sorted(directory.iterdir(), key=lambda item: item.name):
            info = entry.lstat()
            relative = entry.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                raise RuntimeError(f"runtime source contains a symlink: {relative}")
            if stat.S_ISDIR(info.st_mode):
                pending.append(entry)
            elif stat.S_ISREG(info.st_mode):
                if relative == "runtime.json":
                    raise RuntimeError(
                        "runtime source must not contain a generated manifest"
                    )
                files.append(relative)
            else:
                raise RuntimeError(
                    f"runtime source contains an unsafe entry: {relative}"
                )
    return tuple(sorted(files))


def _regular_file(path: Path, root: Path) -> bool:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and resolved.is_relative_to(root.resolve(strict=True))
    )


def _regular_executable(path: Path, root: Path) -> bool:
    return _regular_file(path, root) and os.access(path, os.X_OK)


def _ordinary_directory(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError(f"{label} is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"{label} is unsafe")
    return resolved


def _ordinary_parent(root: Path, relative: PurePosixPath) -> Path:
    current = root
    for part in relative.parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("runtime destination parent is unsafe")
    return current.resolve(strict=True)


def _remove_tree(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            path.rmdir()
    root.rmdir()


__all__ = [
    "CONFIRMATION",
    "RuntimeAudit",
    "audit_local_host",
    "audit_runtime_source",
    "package_historical_runtime",
    "runtime_preparation_reference",
    "write_runtime_preparation",
]
