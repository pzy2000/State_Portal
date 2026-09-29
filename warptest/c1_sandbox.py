"""Strict APFS copy-on-write task sandboxes for concurrent C1 trials."""
from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import os
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from .c1_task_deadline import (
    bounded_task_timeout_s,
    raise_task_timeout_if_expired,
)
from .runtime_lock import runtime_lease
from .task_schema import task_repo_override


C1_SANDBOX_ROOT = Path("/private/tmp/warptest-c1-sandboxes")
C1_MIN_FREE_BYTES = 50 * 1024**3
_OWNER_FILE = ".warptest-c1-sandbox-owner.json"


class C1SandboxError(RuntimeError):
    """Raised when the C1 sandbox preflight itself is unsafe."""


class StrictCloneError(C1SandboxError):
    """Raised when APFS refuses a forced copy-on-write clone."""


@dataclass(frozen=True)
class SandboxResult:
    repo: Path
    source_repo: Path
    mode: str
    root: Path | None
    clone_error: str | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "source_repo": str(self.source_repo),
            "sandbox_repo": str(self.repo) if self.root is not None else None,
            "clone_error": self.clone_error,
        }


def _filesystem_type(path: Path) -> str:
    device_query = subprocess.run(
        ["/bin/df", str(path)], capture_output=True, text=True, timeout=15, check=True
    )
    rows = [line.split() for line in device_query.stdout.splitlines()[1:] if line.split()]
    if not rows:
        raise C1SandboxError(f"df did not report a device for {path}")
    device = rows[-1][0]
    completed = subprocess.run(
        ["/usr/sbin/diskutil", "info", device],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0:
        raise C1SandboxError(
            f"cannot inspect sandbox filesystem for {path}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    for line in completed.stdout.splitlines():
        if "File System Personality:" in line:
            return line.split(":", 1)[1].strip().lower()
    raise C1SandboxError(f"diskutil did not report a filesystem for {path}")


def _important_available_bytes(path: Path) -> int | None:
    """Return macOS capacity available for important usage, including purgeable."""

    script = r'''
import Foundation
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let values = try url.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
if let value = values.volumeAvailableCapacityForImportantUsage {
    print(value)
} else {
    exit(2)
}
'''
    try:
        completed = subprocess.run(
            ["/usr/bin/swift", "-e", script, str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        value = int(completed.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return None
    return value if value >= 0 else None


def preflight_c1_sandboxes(
    repos: Sequence[str | Path],
    *,
    sandbox_root: str | Path = C1_SANDBOX_ROOT,
    min_free_bytes: int = C1_MIN_FREE_BYTES,
) -> dict[str, Any]:
    """Fail before provider calls unless strict APFS cloning is possible."""

    root = Path(sandbox_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    root_device = root.stat().st_dev
    if _filesystem_type(root) != "apfs":
        raise C1SandboxError(f"C1 sandbox root is not APFS: {root}")
    resolved_repos = [Path(repo).expanduser().resolve() for repo in repos]
    wrong_device = [str(repo) for repo in resolved_repos if repo.stat().st_dev != root_device]
    if wrong_device:
        raise C1SandboxError(
            "C1 repositories must share the sandbox APFS device: "
            + ", ".join(wrong_device)
        )
    ordinary_free_bytes = int(shutil.disk_usage(root).free)
    important_free_bytes = _important_available_bytes(root)
    eligible_free_bytes = (
        important_free_bytes
        if important_free_bytes is not None
        else ordinary_free_bytes
    )
    if eligible_free_bytes < int(min_free_bytes):
        raise C1SandboxError(
            f"C1 requires at least {int(min_free_bytes)} free bytes on {root}; "
            f"found {eligible_free_bytes} available for important usage"
        )
    return {
        "schema_version": 1,
        "sandbox_root": str(root),
        "filesystem": "apfs",
        "device": root_device,
        "ordinary_free_bytes": ordinary_free_bytes,
        "important_usage_free_bytes": important_free_bytes,
        "eligible_free_bytes": eligible_free_bytes,
        "capacity_policy": "macos_important_usage_v1",
        "minimum_free_bytes": int(min_free_bytes),
        "repositories": [str(repo) for repo in resolved_repos],
    }


def _clonefile_library() -> ctypes.CDLL:
    library = ctypes.CDLL(None, use_errno=True)
    library.clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    library.clonefile.restype = ctypes.c_int
    return library


def strict_clone_tree(source: str | Path, destination: str | Path) -> dict[str, Any]:
    """Recursively clone a directory with no byte-copy fallback."""

    src = Path(source).expanduser().resolve()
    dst = Path(destination).expanduser().resolve()
    if dst.exists():
        raise StrictCloneError(f"clone destination already exists: {dst}")
    if src.stat().st_dev != dst.parent.stat().st_dev:
        raise StrictCloneError(f"clone crosses filesystems: {src} -> {dst}")
    library = _clonefile_library()
    rc = library.clonefile(os.fsencode(src), os.fsencode(dst), 0)
    if rc != 0:
        code = ctypes.get_errno()
        raise StrictCloneError(
            f"clonefile failed: [{errno.errorcode.get(code, code)}] "
            f"{os.strerror(code)}"
        )
    return {"was_cloned": True, "copied_bytes": 0}


def git_fingerprint(repo: str | Path) -> dict[str, str] | None:
    target = Path(repo).expanduser().resolve()
    try:
        head = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=bounded_task_timeout_s(30.0),
        )
    except subprocess.TimeoutExpired as error:
        raise_task_timeout_if_expired(error)
        raise
    if head.returncode != 0:
        return None
    try:
        status = subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "status",
                "--porcelain=v2",
                "--untracked-files=no",
            ],
            capture_output=True,
            text=True,
            timeout=bounded_task_timeout_s(300.0),
            check=True,
        )
    except subprocess.TimeoutExpired as error:
        raise_task_timeout_if_expired(error)
        raise
    return {"head": head.stdout.strip(), "status": status.stdout}


def _safe_remove(path: Path, owned_root: Path, owner: dict[str, Any]) -> None:
    resolved = path.resolve()
    root = owned_root.resolve()
    if resolved == root or root not in resolved.parents:
        raise C1SandboxError(f"refusing unsafe sandbox cleanup: {resolved}")
    marker = resolved / _OWNER_FILE
    if resolved.exists():
        try:
            recorded = json.loads(marker.read_text(encoding="utf-8"))
        except Exception as error:
            raise C1SandboxError(
                f"refusing unowned sandbox cleanup {resolved}: {error}"
            ) from error
        if recorded != owner:
            raise C1SandboxError(f"sandbox ownership marker mismatch: {resolved}")

        def ignore_disappeared_child(
            _function: Any,
            child_path: str,
            error_info: tuple[type[BaseException], BaseException, Any],
        ) -> None:
            error = error_info[1]
            if isinstance(error, FileNotFoundError):
                return
            raise C1SandboxError(
                f"failed to clean owned sandbox child {child_path}: {error}"
            ) from error

        # Unity creates and removes transient siblings such as ``Temp 2`` while
        # shutting down. Python <=3.12 can surface that harmless child race as a
        # fatal FileNotFoundError from rmtree; Python 3.13 ignores the same race.
        # The owner marker and containment checks above still fail closed, and
        # every error other than an already-disappeared child remains fatal.
        shutil.rmtree(resolved, onerror=ignore_disappeared_child)
        if resolved.exists():
            raise C1SandboxError(f"owned sandbox cleanup left residuals: {resolved}")


class C1SandboxManager:
    """Create and immediately reclaim one strict CoW checkout per task."""

    def __init__(
        self,
        *,
        run_id: str,
        sandbox_root: str | Path = C1_SANDBOX_ROOT,
    ) -> None:
        self.run_id = str(run_id)
        self.sandbox_root = Path(sandbox_root).expanduser().resolve()
        self.run_root = self.sandbox_root / self.run_id
        self._guard = threading.Lock()

    def cleanup_stale(self) -> int:
        """Remove only marker-owned leftovers belonging to this exact run id."""

        if not self.run_root.exists():
            return 0
        removed = 0
        with self._guard:
            markers = list(self.run_root.rglob(_OWNER_FILE))
            for marker in markers:
                task_root = marker.parent
                try:
                    owner = json.loads(marker.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if owner.get("run_id") != self.run_id:
                    continue
                _safe_remove(task_root, self.run_root, owner)
                removed += 1
        return removed

    @contextlib.contextmanager
    def task_repo(
        self,
        source_repo: str | Path,
        *,
        invocation_id: str,
        lane_id: int,
        task_id: str,
        allow_source_fallback: bool = True,
        blocking: bool = True,
    ) -> Iterator[SandboxResult]:
        source = Path(source_repo).expanduser().resolve()
        nonce = uuid.uuid4().hex
        task_root = (
            self.run_root
            / str(invocation_id)
            / f"lane-{int(lane_id)}"
            / f"{task_id}-{nonce}"
        )
        destination = task_root / "repo"
        owner = {
            "schema_version": 1,
            "run_id": self.run_id,
            "nonce": nonce,
            "source_repo": str(source),
        }
        clone_error: str | None = None
        try:
            task_root.parent.mkdir(parents=True, exist_ok=True)
            task_root.mkdir()
            (task_root / _OWNER_FILE).write_text(
                json.dumps(owner, sort_keys=True) + "\n", encoding="utf-8"
            )
            source_lease = (
                runtime_lease(source)
                if blocking
                else runtime_lease(source, blocking=False)
            )
            with source_lease:
                source_fingerprint = git_fingerprint(source)
                strict_clone_tree(source, destination)
                if destination.stat().st_dev != source.stat().st_dev:
                    raise StrictCloneError("cloned checkout changed filesystem device")
                if source_fingerprint != git_fingerprint(destination):
                    raise StrictCloneError("cloned checkout Git fingerprint mismatch")
            result = SandboxResult(
                repo=destination,
                source_repo=source,
                mode="apfs_cow_clone",
                root=task_root,
            )
        except Exception as error:
            clone_error = f"{type(error).__name__}: {error}"
            if task_root.exists():
                _safe_remove(task_root, self.run_root, owner)
            result = SandboxResult(
                repo=source,
                source_repo=source,
                mode="repo_serial_fallback",
                root=None,
                clone_error=clone_error,
            )
        if result.root is None and not allow_source_fallback:
            raise StrictCloneError(
                "source-checkout fallback is forbidden for this mutable runtime: "
                + str(result.clone_error)
            )
        try:
            if result.root is None:
                # A failed clone must serialize the *entire* task runtime on the
                # mutable source checkout, not merely the clone attempt above.
                fallback_lease = (
                    runtime_lease(source)
                    if blocking
                    else runtime_lease(source, blocking=False)
                )
                with fallback_lease:
                    yield result
            else:
                with task_repo_override(source, destination):
                    yield result
        finally:
            if result.root is not None and result.root.exists():
                _safe_remove(result.root, self.run_root, owner)
