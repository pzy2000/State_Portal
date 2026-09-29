"""Gate 1--6 runner for the P2P-only C1-Lazarus track.

The strict runner executes twelve cells because it grades an F2P differential.
This track has no F2P, so it executes six: the registered repository control test
on each version, three times.  Six cells all passing prove only that both
products start and run; the defect differential is established later, from the
rendered frames.  That distinction is why this is a separate module and a
separate artifact vocabulary rather than a flag on the strict runner, whose
twelve-cell assertion is its main defence against incomplete evidence.

The runner reuses no historical attempt, performs no paid work, runs no formal
trial, and constructs no golden.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Callable, Literal, Mapping

from .luanti_native_executor import NativeProcessReceipt, run_bounded_process
from .p2p_only_profiles import (
    HOST_CHECKOUT,
    P2POnlyProfile,
    StaticGateEvidence,
    recompute_static_gates,
)
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)

CONFIRMATION = "EXECUTE_LAZARUS_P2P_ONLY_GATE16_V1"
EPOCH = "p2p-only-v1"
_VERSION = "1.0"
_TRACK = "c1_lazarus_p2p_only_v1"
_REPETITIONS = 3
_EXPECTED_CELLS = 6
_MAX_OUTPUT = 262_144
_VERSIONS = {"node": "v22.22.0", "jest": "29.5.0"}
# Luanti ships its control suite as a game mod the engine runs itself, so the
# control is a real client session rather than an external test runner.
_LUANTI_APP_BINARY = "build-app/out/luanti.app/Contents/MacOS/luanti"
_LUANTI_RESOURCES = "build-app/out/luanti.app/Contents/Resources"
_LUANTI_SHARE_ROOTS = frozenset({"builtin", "client", "fonts", "games", "textures"})
_LUANTI_RESULT = "Devtest Unit Test Results:"
_LUANTI_CONTROL_SUITE = "games/devtest/mods/unittests"
_OPENRA_TEST_DLL = "bin/OpenRA.Test.dll"
_LUANTI_CONTROL_RUNNER = (
    Path(__file__).resolve().parents[2]
    / "tools/lazarus-p2p-luanti-control/run.py"
)
_TREE_ROOT = PurePosixPath(".warptest-cache/lazarus-p2p-only/trees")
_RUNTIME_MANIFEST = ".p2p-only-runtime.json"

ProcessRunner = Callable[..., NativeProcessReceipt]


class Gate16Error(RuntimeError):
    """Raised when the six-cell control matrix cannot be established."""


@dataclass(frozen=True)
class Gate16Result:
    task_id: str
    aggregate: StableArtifact
    transition: StableArtifact
    admission: StableArtifact | None
    gate_1_6_complete: bool
    executed_cells: int


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_many(paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(_sha256_file(path).encode())
    return digest.hexdigest()


def _reference(profile: P2POnlyProfile, kind: str, suffix: str = "") -> ArtifactReference:
    tail = f"/{suffix}" if suffix else ""
    return ArtifactReference(
        f"{kind}/{profile.host}/{profile.change_number}{tail}/{EPOCH}",
        kind,
        _VERSION,
    )


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("P2P-only Gate 1--6 requires candidate-screening scope")


def _write(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: Mapping[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    body = dict(payload)
    # Every artifact this track writes carries its track name, so an audit that
    # walks a closure cannot mistake one of these for strict Gate 1--7 evidence.
    body["track"] = _TRACK
    body["gate_vocabulary"] = "gate_1_6"
    body["paid_actions_authorized"] = False
    body["formal_trials_authorized"] = False
    created = _now()
    try:
        existing = load_stable_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
        )
    except (RuntimeError, ValueError):
        existing = None
    if existing is not None:
        created = (
            datetime.fromisoformat(existing.created_at.replace("Z", "+00:00"))
            + timedelta(seconds=1)
        ).isoformat().replace("+00:00", "Z")
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=created,
        inputs=inputs,
        payload=body,
        authorization=authorization,
    )
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _clone_tree(source: Path, destination: Path) -> None:
    """Clone a checkout with APFS copy-on-write so a variant stays isolated."""

    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ("cp", "-c", "-R", str(source), str(destination)),
        capture_output=True,
        check=False,
        timeout=1800,
    )
    if result.returncode != 0:
        raise Gate16Error(
            "copy-on-write clone failed: "
            + result.stderr.decode("utf-8", "replace").strip()
        )


def _apply_inverse(tree: Path, patch: bytes) -> None:
    result = subprocess.run(
        ("git", "apply", "-R", "--whitespace=nowarn", "-"),
        cwd=tree,
        input=patch,
        capture_output=True,
        check=False,
        timeout=300,
    )
    if result.returncode != 0:
        raise Gate16Error(
            "inverse patch did not apply: "
            + result.stderr.decode("utf-8", "replace").strip()
        )


def materialize_pair(
    repository_root: Path, profile: P2POnlyProfile
) -> tuple[Path, Path, dict[str, object]]:
    """Return the ``(fixed, buggy)`` trees and what distinguishes them.

    The fixed tree is the pinned checkout, shared by every task of the same
    host because no task changes it.  The buggy tree is a private clone
    carrying the reverse of this task's repair and nothing else.
    """

    source = repository_root / HOST_CHECKOUT[profile.host]
    trees = repository_root / _TREE_ROOT
    source_head = _git_head(source)
    if source_head != profile.pinned_revision:
        raise Gate16Error(
            f"{profile.task_id} checkout {source_head} does not match pin "
            f"{profile.pinned_revision}"
        )
    if _tracked_tree_dirty(source):
        raise Gate16Error(f"{profile.task_id} source checkout has tracked changes")
    fixed = trees / f"shared-fixed-{profile.host}-{profile.pinned_revision[:12]}"
    buggy = trees / f"{profile.host}-{profile.change_number}-buggy"

    if (
        not _tree_ready(profile.host, fixed)
        or _git_head(fixed) != profile.pinned_revision
        or _tracked_tree_dirty(fixed)
    ):
        _clone_tree(source, fixed)
    if not _runtime_ready(profile, fixed):
        _prepare_variant_runtime(profile, fixed)
        _write_runtime_manifest(profile, fixed)
    # Derive the buggy tree from the already validated fixed runtime.  In
    # particular, Luanti's ignored macOS bundle is built and pin-checked once in
    # ``fixed`` rather than inherited independently from an arbitrary developer
    # checkout artifact.
    _clone_tree(fixed, buggy)
    _apply_inverse(buggy, profile.inverse_patch(repository_root))
    _prepare_variant_runtime(profile, buggy)

    production = tuple(PurePosixPath(value) for value in profile.production_paths)
    fixed_hash = _sha256_many(tuple(fixed / value for value in production))
    buggy_hash = _sha256_many(tuple(buggy / value for value in production))
    if fixed_hash == buggy_hash:
        raise Gate16Error(f"{profile.task_id} inverse did not change production source")

    control = PurePosixPath(profile.control_test_path)
    fixed_control = _sha256_file(fixed / control)
    buggy_control = _sha256_file(buggy / control)
    if fixed_control != buggy_control:
        # Gate 4's runtime half: the control test must be the same bytes on both
        # products, or the two runs are not measuring the same control.
        raise Gate16Error(f"{profile.task_id} inverse changed the control test")

    # Gate 3's runtime half: nothing outside the declared production set moved.
    differing = _differing_tracked_paths(buggy)
    if sorted(differing) != sorted(profile.production_paths):
        raise Gate16Error(
            f"{profile.task_id} inverse touched {sorted(differing)} rather than "
            f"{sorted(profile.production_paths)}"
        )

    return (
        fixed,
        buggy,
        {
            "fixed_tree": fixed.relative_to(repository_root).as_posix(),
            "buggy_tree": buggy.relative_to(repository_root).as_posix(),
            "production_source_hash": {"fixed": fixed_hash, "buggy": buggy_hash},
            "pinned_revision": profile.pinned_revision,
            "fixed_base_revision": _git_head(fixed),
            "buggy_base_revision": _git_head(buggy),
            "control_test_hash": fixed_control,
            "identical_control_test": True,
            "inverse_changed_paths": sorted(differing),
            "production_only_inverse": True,
        },
    )


def _tree_ready(host: str, tree: Path) -> bool:
    if not (tree / ".git").exists():
        return False
    if host == "jupyterlab":
        return (tree / "package.json").is_file()
    if host == "luanti":
        return (tree / _LUANTI_APP_BINARY).is_file()
    if host == "openra":
        return (tree / _OPENRA_TEST_DLL).is_file()
    return False


def _git_head(tree: Path) -> str:
    result = subprocess.run(
        ("git", "-C", str(tree), "rev-parse", "HEAD"),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _tracked_tree_dirty(tree: Path) -> bool:
    result = subprocess.run(
        ("git", "-C", str(tree), "diff", "--quiet", "HEAD", "--"),
        capture_output=True,
        check=False,
        timeout=60,
    )
    return result.returncode != 0


def _runtime_signature(profile: P2POnlyProfile) -> dict[str, object]:
    return {
        "schema": "c1-lazarus-p2p-only-runtime-cache-v2",
        "host": profile.host,
        "pinned_revision": profile.pinned_revision,
        "runtime_class": (
            "openra-incremental-dotnet"
            if profile.host == "openra"
            else "luanti-app-share-sync"
            if profile.host == "luanti"
            else "jupyterlab-source-tree"
        ),
    }


def _runtime_ready(profile: P2POnlyProfile, tree: Path) -> bool:
    path = tree / _RUNTIME_MANIFEST
    try:
        expected = {
            **_runtime_signature(profile),
            "runtime_artifacts": _runtime_artifacts(profile, tree),
        }
        return json.loads(path.read_text(encoding="utf-8")) == expected
    except (Gate16Error, OSError, json.JSONDecodeError):
        return False


def _write_runtime_manifest(profile: P2POnlyProfile, tree: Path) -> None:
    (tree / _RUNTIME_MANIFEST).write_text(
        json.dumps(
            {
                **_runtime_signature(profile),
                "runtime_artifacts": _runtime_artifacts(profile, tree),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _luanti_version_output(profile: P2POnlyProfile, tree: Path) -> str:
    binary = tree / _LUANTI_APP_BINARY
    if not binary.is_file():
        raise Gate16Error("luanti app bundle is missing from the materialized tree")
    result = subprocess.run(
        (str(binary), "--version"),
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    output = result.stdout.strip()
    first = output.splitlines()[0] if output else ""
    match = re.search(r"-([0-9a-f]{7,40})(-dirty)?(?:\s|$)", first)
    if (
        result.returncode != 0
        or match is None
        or not profile.pinned_revision.startswith(match.group(1))
        or match.group(2) is not None
    ):
        raise Gate16Error(
            f"{profile.task_id} Luanti bundle is not built from pin "
            f"{profile.pinned_revision}: {first or 'no version output'}"
        )
    return output


def _luanti_build_declares_pin(profile: P2POnlyProfile, tree: Path) -> bool:
    header = tree / "build-app/src/cmake_config_githash.h"
    try:
        text = header.read_text(encoding="utf-8")
    except OSError:
        return False
    match = re.search(r'VERSION_GITHASH\s+"[^"\n]*-([0-9a-f]{7,40})"', text)
    return match is not None and profile.pinned_revision.startswith(match.group(1))


def _sign_luanti_app(tree: Path) -> None:
    app = tree / "build-app/out/luanti.app"
    result = subprocess.run(
        ("codesign", "--force", "--deep", "--sign", "-", "--timestamp=none", str(app)),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise Gate16Error(
            "luanti app signing failed: "
            + (result.stderr or result.stdout or "unknown codesign error").strip()
        )


def _runtime_artifacts(profile: P2POnlyProfile, tree: Path) -> dict[str, object]:
    if profile.host == "luanti":
        return {
            "binary_sha256": _sha256_file(tree / _LUANTI_APP_BINARY),
            "version_output": _luanti_version_output(profile, tree),
        }
    if profile.host == "openra":
        required = (
            tree / "bin/OpenRA.Game.dll",
            tree / "bin/OpenRA.Mods.Common.dll",
            tree / _OPENRA_TEST_DLL,
        )
        missing = [path.relative_to(tree).as_posix() for path in required if not path.is_file()]
        if missing:
            raise Gate16Error(
                "openra runtime is missing required artifacts: " + ", ".join(missing)
            )
        return {
            path.relative_to(tree).as_posix(): _sha256_file(path) for path in required
        }
    return {}


def _prepare_variant_runtime(profile: P2POnlyProfile, tree: Path) -> None:
    """Make a reverted source the thing the host's binary will actually load.

    The macOS Luanti bundle reads ``builtin`` from its Resources directory, not
    from the git tree.  Copying only the share roots the inverse touched keeps
    the running product aligned with Gate 3 without a C++ rebuild.

    OpenRA loads compiled assemblies from ``bin/``.  A C# inverse therefore has
    to rebuild those assemblies; a chrome YAML inverse does not.
    """

    if profile.host == "luanti":
        try:
            _luanti_version_output(profile, tree)
        except Gate16Error:
            # The source checkout may contain an ignored developer build, or no
            # app at all.  Neither is admissible evidence.  Build a fresh bundle
            # from this pin before overlaying the versioned share roots.
            from ..lazarus_luanti_goldens import build_version_app_bundle

            if not _luanti_build_declares_pin(profile, tree):
                try:
                    build_version_app_bundle(tree)
                except RuntimeError as error:
                    raise Gate16Error(
                        f"luanti pinned app build failed: {error}"
                    ) from error
        resources = tree / _LUANTI_RESOURCES
        if not resources.is_dir():
            raise Gate16Error("luanti app bundle is missing from the materialized tree")
        for name in sorted(_LUANTI_SHARE_ROOTS):
            source = tree / name
            destination = resources / name
            if not source.is_dir():
                continue
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination)
        _sign_luanti_app(tree)
        _luanti_version_output(profile, tree)
        return
    if profile.host != "openra":
        return
    if any(path.endswith(".cs") for path in profile.production_paths):
        dotnet = _dotnet_executable()
        result = subprocess.run(
            (
                dotnet,
                "build",
                "OpenRA.Test/OpenRA.Test.csproj",
                "-c",
                "Release",
                "--nologo",
            ),
            cwd=tree,
            capture_output=True,
            check=False,
            timeout=1800,
            env={**os.environ, "DOTNET_NOLOGO": "1"},
        )
        if result.returncode != 0:
            raise Gate16Error(
                "openra rebuild after inverse failed:\n"
                + result.stdout.decode("utf-8", "replace")[-2000:]
                + result.stderr.decode("utf-8", "replace")[-2000:]
            )
        required = (
            tree / "bin/OpenRA.Game.dll",
            tree / "bin/OpenRA.Mods.Common.dll",
            tree / _OPENRA_TEST_DLL,
        )
        missing = [path.relative_to(tree).as_posix() for path in required if not path.is_file()]
        if missing:
            raise Gate16Error(
                "openra rebuild missed required artifacts: " + ", ".join(missing)
            )
    patch_openra_runtime_roll_forward(tree)


def patch_openra_runtime_roll_forward(tree: Path) -> None:
    """Let net8.0 testhost and the game host start on a newer installed runtime.

    ``DOTNET_ROLL_FORWARD`` is not enough once a ``*.runtimeconfig.json`` names
    ``Microsoft.NETCore.App`` 8.0.0 with no roll-forward policy: testhost then
    exits before any test runs.  ``bin/`` is gitignored, so this does not
    disturb Gate 3's tracked-path check.
    """

    bin_dir = tree / "bin"
    if not bin_dir.is_dir():
        return
    for path in sorted(bin_dir.glob("*.runtimeconfig.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        options = data.setdefault("runtimeOptions", {})
        if options.get("rollForward") == "LatestMajor":
            continue
        options["rollForward"] = "LatestMajor"
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _differing_tracked_paths(tree: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ("git", "diff", "--name-only", "HEAD"),
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if result.returncode != 0:
        raise Gate16Error("could not inspect the materialized buggy tree")
    return tuple(sorted(value for value in result.stdout.split() if value))


def _closed_environment(tree: Path, profile: P2POnlyProfile) -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tree / ".p2p-only-home"),
        "LANG": "C",
        "LC_ALL": "C",
        "CI": "true",
        "NODE_ENV": "test",
        "JUPYTERLAB_GALATA": "0",
    }
    if profile.host == "openra":
        # ``dotnet test`` needs its own root and a PATH that can find hostfxr.
        # Both are taken from the developer machine, not from the product tree.
        dotnet = shutil.which("dotnet")
        if dotnet is None:
            raise Gate16Error("dotnet is not available")
        env["PATH"] = str(Path(dotnet).parent) + ":/usr/bin:/bin:/usr/sbin:/sbin"
        env["DOTNET_CLI_HOME"] = str(tree / ".p2p-only-home")
        env["DOTNET_NOLOGO"] = "1"
        env["DOTNET_CLI_UI_LANGUAGE"] = "en"
        env["DOTNET_ROLL_FORWARD"] = "LatestMajor"
        root = _dotnet_root(dotnet)
        if root is not None:
            env["DOTNET_ROOT"] = root
    return env


def _receipt_payload(receipt: NativeProcessReceipt) -> dict[str, object]:
    return {
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "stdout": receipt.stdout[:_MAX_OUTPUT].decode("utf-8", "replace"),
        "stderr": receipt.stderr[:_MAX_OUTPUT].decode("utf-8", "replace"),
    }


def _dotnet_executable() -> str:
    dotnet = shutil.which("dotnet")
    if dotnet is None:
        raise Gate16Error("dotnet is not available")
    return dotnet


def _dotnet_root(dotnet: str) -> str | None:
    """Return a DOTNET_ROOT that actually contains shared frameworks."""

    resolved = Path(dotnet).resolve()
    candidates = []
    configured = os.environ.get("DOTNET_ROOT")
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        (
            resolved.parent.parent / "libexec",
            resolved.parent,
        )
    )
    for path in candidates:
        if (path / "shared" / "Microsoft.NETCore.App").is_dir():
            return str(path)
    return None


def _node_executable() -> str:
    node = shutil.which("node")
    if node is None:
        raise Gate16Error("node is not available")
    return node


def _runtime_steps(
    profile: P2POnlyProfile, tree: Path, node: str
) -> tuple[tuple[str, tuple[str, ...], str | None], ...]:
    """Return the version probes that prove one product's runtime is the pinned one."""

    if profile.host == "luanti":
        # The engine prints its version and exits, which also proves the bundle
        # this variant will be graded through actually launches.
        return (
            (
                "engine-version",
                (str(tree / _LUANTI_APP_BINARY), "--version"),
                _luanti_version_output(profile, tree),
            ),
        )
    if profile.host == "openra":
        dotnet = _dotnet_executable()
        return (
            (
                "dotnet-version",
                (dotnet, "--version"),
                None,
            ),
        )
    jest = tree / "node_modules/jest/bin/jest.js"
    return (
        ("node-version", (node, "--version"), _VERSIONS["node"]),
        ("jest-version", (node, str(jest), "--version"), _VERSIONS["jest"]),
    )


def _validate_runtime(
    root: Path,
    *,
    repository_root: Path,
    profile: P2POnlyProfile,
    variant: str,
    tree: Path,
    node: str,
    inputs: tuple[ArtifactReference, ...],
    receipts: list[ArtifactReference],
    runner: ProcessRunner,
    authorization: AuthorizationRecord,
) -> None:
    for step, argv, expected in _runtime_steps(profile, tree, node):
        receipt = runner(
            argv,
            cwd=tree,
            env=_closed_environment(tree, profile),
            timeout_seconds=120,
        )
        observed = receipt.stdout.decode("utf-8", "replace").strip()
        matched = (
            receipt.returncode == 0
            and (True if expected is None else observed == expected)
        )
        payload = _receipt_payload(receipt)
        payload.update(
            {
                "candidate": profile.candidate,
                "task_id": profile.task_id,
                "variant": variant,
                "step": step,
                "argv": list(argv),
                "expected_stdout": expected,
                "matched_expected": matched,
            }
        )
        reference = _reference(
            profile, "p2p_only_runtime_receipt", f"{variant}/{step}"
        )
        artifact = _write(root, reference, inputs, payload, authorization)
        receipts.append(artifact.reference)
        if receipt.outcome != "completed" or not matched:
            raise Gate16Error(
                f"{profile.task_id} {variant} {step} expected {expected!r}, "
                f"observed {observed!r}"
            )


@dataclass(frozen=True)
class _Cell:
    variant: Literal["fixed", "buggy"]
    repetition: int


def _luanti_control_invocation(
    tree: Path, cell: _Cell
) -> tuple[tuple[str, ...], Path]:
    """Return the fresh-session wrapper invocation for one Luanti control cell."""

    if not _LUANTI_CONTROL_RUNNER.is_file():
        raise Gate16Error("Luanti P2P-only control runner is missing")
    argv = (
        str(_LUANTI_CONTROL_RUNNER),
        "--source-root",
        str(tree),
        "--timeout-seconds",
        "120",
        "--attempt-id",
        f"{cell.variant}-{cell.repetition}",
    )
    return argv, tree


def _classify_luanti_control(receipt: NativeProcessReceipt) -> tuple[str, dict[str, object]]:
    if receipt.outcome == "timed_out":
        return "timed_out", {}
    if receipt.outcome == "launch_failed":
        return "launch_failed", {}
    text = receipt.stdout.decode("utf-8", "replace")
    try:
        detail = json.loads(text.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return "invalid_test_count", {}
    if not isinstance(detail, dict) or detail.get("schema") != "c1-lazarus-p2p-only-luanti-control-v1":
        return "invalid_test_count", {}
    verdict = str(detail.get("verdict") or "")
    failed = int(detail.get("num_failed_tests") or 0)
    total = int(detail.get("num_total_tests") or 0)
    if total <= 0:
        return "invalid_test_count", detail
    if receipt.returncode != 0 or verdict != "PASSED" or failed:
        return "failure", detail
    return "pass", detail


def _openra_control_invocation(
    profile: P2POnlyProfile, tree: Path
) -> tuple[tuple[str, ...], Path]:
    """Return the argv and working directory for one OpenRA NUnit control run.

    The filter is the control file's stem, so a chrome repair that borrows
    ``MiniYamlTest`` cannot silently widen to the whole ``OpenRA.Test`` suite.
    """

    control = PurePosixPath(profile.control_test_path)
    argv = (
        _dotnet_executable(),
        "test",
        str(tree / _OPENRA_TEST_DLL),
        "--test-adapter-path=.",
        f"--filter=FullyQualifiedName~{control.stem}",
        "--nologo",
    )
    return argv, tree


def _classify_openra_control(
    receipt: NativeProcessReceipt,
) -> tuple[str, dict[str, object]]:
    if receipt.outcome == "timed_out":
        return "timed_out", {}
    if receipt.outcome == "launch_failed":
        return "launch_failed", {}
    text = "\n".join(
        (
            receipt.stdout.decode("utf-8", "replace"),
            receipt.stderr.decode("utf-8", "replace"),
        )
    )
    passed = 0
    failed = 0
    total = 0
    for line in text.splitlines():
        stripped = line.strip()
        failed_match = re.search(r"Failed:\s*(\d+)", stripped)
        passed_match = re.search(r"Passed:\s*(\d+)", stripped)
        total_match = re.search(r"Total:\s*(\d+)", stripped)
        if failed_match and passed_match and total_match:
            failed = int(failed_match.group(1))
            passed = int(passed_match.group(1))
            total = int(total_match.group(1))
            break
    detail = {
        "num_passed_tests": passed,
        "num_failed_tests": failed,
        "num_total_tests": total,
        "returncode": receipt.returncode,
        "suite": _OPENRA_TEST_DLL,
    }
    if total <= 0:
        return "invalid_test_count", detail
    if receipt.returncode != 0 or failed:
        return "failure", detail
    if passed != total:
        return "invalid_test_count", detail
    return "pass", detail


def _run_control_cell(
    root: Path,
    *,
    profile: P2POnlyProfile,
    cell: _Cell,
    tree: Path,
    node: str,
    inputs: tuple[ArtifactReference, ...],
    runner: ProcessRunner,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if profile.host == "luanti":
        argv, cwd = _luanti_control_invocation(tree, cell)
        receipt = runner(
            argv,
            cwd=cwd,
            env=_closed_environment(tree, profile),
            timeout_seconds=135,
        )
        observed, detail = _classify_luanti_control(receipt)
        payload = _receipt_payload(receipt)
        payload.update(
            {
                "candidate": profile.candidate,
                "task_id": profile.task_id,
                "phase": "p2p",
                "variant": cell.variant,
                "repetition": cell.repetition,
                "argv": list(argv),
                "control_test_path": profile.control_test_path,
                "expected_outcome": "pass",
                "observed_outcome": observed,
                "matched_expected": observed == "pass",
                "control_summary": detail,
            }
        )
        reference = _reference(
            profile,
            "p2p_only_control_attempt",
            f"{cell.variant}/{cell.repetition}",
        )
        return _write(root, reference, inputs, payload, authorization)

    if profile.host == "openra":
        argv, cwd = _openra_control_invocation(profile, tree)
        receipt = runner(
            argv,
            cwd=cwd,
            env=_closed_environment(tree, profile),
            timeout_seconds=900,
        )
        observed, detail = _classify_openra_control(receipt)
        payload = _receipt_payload(receipt)
        payload.update(
            {
                "candidate": profile.candidate,
                "task_id": profile.task_id,
                "phase": "p2p",
                "variant": cell.variant,
                "repetition": cell.repetition,
                "argv": list(argv),
                "control_test_path": profile.control_test_path,
                "expected_outcome": "pass",
                "observed_outcome": observed,
                "matched_expected": observed == "pass",
                "control_summary": detail,
            }
        )
        reference = _reference(
            profile,
            "p2p_only_control_attempt",
            f"{cell.variant}/{cell.repetition}",
        )
        return _write(root, reference, inputs, payload, authorization)

    control = PurePosixPath(profile.control_test_path)
    # Jest resolves its config from the package it is invoked in, which is the
    # control test's own package.  That is not always the repaired package: a
    # package that ships no test suite borrows a control from elsewhere.
    control_package = PurePosixPath(*control.parts[:2])
    package = tree / control_package
    # The selector is the control file's path relative to its package, so the run
    # cannot silently widen to the package's whole suite.
    relative = control.relative_to(control_package)
    argv = (
        node,
        str(tree / "node_modules/jest/bin/jest.js"),
        "--ci",
        "--runInBand",
        "--no-cache",
        "--json",
        f"--testPathPattern={relative.as_posix().replace('.', chr(92) + '.')}",
    )
    receipt = runner(
        argv,
        cwd=package,
        env=_closed_environment(tree, profile),
        timeout_seconds=900,
    )
    observed, detail = _classify_jest(receipt)
    payload = _receipt_payload(receipt)
    payload.update(
        {
            "candidate": profile.candidate,
            "task_id": profile.task_id,
            "phase": "p2p",
            "variant": cell.variant,
            "repetition": cell.repetition,
            "argv": list(argv),
            "control_test_path": profile.control_test_path,
            # Both versions must pass: this cell is a control, never a differential.
            "expected_outcome": "pass",
            "observed_outcome": observed,
            "matched_expected": observed == "pass",
            "jest_summary": detail,
        }
    )
    reference = _reference(
        profile,
        "p2p_only_control_attempt",
        f"{cell.variant}/{cell.repetition}",
    )
    return _write(root, reference, inputs, payload, authorization)


def _classify_jest(receipt: NativeProcessReceipt) -> tuple[str, dict[str, object]]:
    if receipt.outcome == "timed_out":
        return "timed_out", {}
    if receipt.outcome == "launch_failed":
        return "launch_failed", {}
    text = receipt.stdout.decode("utf-8", "replace")
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return "invalid_jest_result", {}
    try:
        report = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return "invalid_jest_result", {}
    total = int(report.get("numTotalTests") or 0)
    passed = int(report.get("numPassedTests") or 0)
    failed = int(report.get("numFailedTests") or 0)
    suites = int(report.get("numTotalTestSuites") or 0)
    detail = {
        "num_total_test_suites": suites,
        "num_total_tests": total,
        "num_passed_tests": passed,
        "num_failed_tests": failed,
        "success": bool(report.get("success")),
    }
    if suites != 1 or total <= 0:
        # A zero-test run is a failure, not a pass: it would let a broken
        # selector look like a healthy control.
        return "invalid_test_count", detail
    if failed or not report.get("success"):
        return "failure", detail
    if passed != total:
        return "invalid_test_count", detail
    return "pass", detail


def execute_gate_1_6(
    artifact_root: str | Path,
    *,
    repository_root: str | Path,
    profile: P2POnlyProfile,
    authorization: AuthorizationRecord,
    confirmation: str = CONFIRMATION,
    process_runner: ProcessRunner | None = None,
    keep_trees: bool = False,
) -> Gate16Result:
    """Recompute Gate 1--5, materialize the pair and run the six control cells."""

    _require_authorization(authorization)
    if confirmation != CONFIRMATION:
        raise Gate16Error(f"explicit confirmation must be {CONFIRMATION!r}")
    root = Path(artifact_root).absolute().resolve(strict=True)
    repo = Path(repository_root).absolute().resolve(strict=True)
    runner = process_runner or run_bounded_process

    static = recompute_static_gates(repo, profile)
    if not static.passed:
        raise Gate16Error(
            f"{profile.task_id} static Gate 1--5 did not pass: {static.to_value()}"
        )
    static_artifact = _write(
        root,
        _reference(profile, "p2p_only_static_profile"),
        (),
        {
            "candidate": profile.candidate,
            "task_id": profile.task_id,
            "state": "recomputed",
            "outcome": "p2p_only_static_gates_recomputed",
            "defect_locus": profile.defect_locus,
            "control_test_path": profile.control_test_path,
            "repetitions_per_cell": _REPETITIONS,
            "expected_cell_count": _EXPECTED_CELLS,
            **static.to_value(),
        },
        authorization,
    )

    fixed, buggy, pair = materialize_pair(repo, profile)
    pair_artifact = _write(
        root,
        _reference(profile, "p2p_only_pair_materialization"),
        (static_artifact.reference,),
        {
            "candidate": profile.candidate,
            "task_id": profile.task_id,
            "state": "materialized",
            "outcome": "p2p_only_pair_materialized",
            **pair,
        },
        authorization,
    )

    chain = (static_artifact.reference, pair_artifact.reference)
    node = "" if profile.host != "jupyterlab" else _node_executable()
    receipts: list[ArtifactReference] = []
    attempts: list[StableArtifact] = []
    try:
        for variant, tree in (("fixed", fixed), ("buggy", buggy)):
            _validate_runtime(
                root,
                repository_root=repo,
                profile=profile,
                variant=variant,
                tree=tree,
                node=node,
                inputs=chain,
                receipts=receipts,
                runner=runner,
                authorization=authorization,
            )
        for variant, tree in (("fixed", fixed), ("buggy", buggy)):
            for repetition in range(1, _REPETITIONS + 1):
                attempts.append(
                    _run_control_cell(
                        root,
                        profile=profile,
                        cell=_Cell(variant, repetition),
                        tree=tree,
                        node=node,
                        inputs=chain + tuple(receipts),
                        runner=runner,
                        authorization=authorization,
                    )
                )
    finally:
        if not keep_trees and buggy.is_dir():
            shutil.rmtree(buggy, ignore_errors=True)

    aggregate = _write_aggregate(
        root, profile, static, chain, tuple(receipts), attempts, authorization
    )
    transition = _write_transition(root, profile, aggregate, authorization)
    complete = transition.payload.get("strict_gate_1_6") is True
    admission = (
        _write_admission(root, profile, transition, authorization) if complete else None
    )
    return Gate16Result(
        task_id=profile.task_id,
        aggregate=aggregate,
        transition=transition,
        admission=admission,
        gate_1_6_complete=complete,
        executed_cells=len(attempts),
    )


def _write_aggregate(
    root: Path,
    profile: P2POnlyProfile,
    static: StaticGateEvidence,
    chain: tuple[ArtifactReference, ...],
    receipts: tuple[ArtifactReference, ...],
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if len(attempts) != _EXPECTED_CELLS:
        raise Gate16Error(
            f"{profile.task_id} control coverage is incomplete: {len(attempts)} cells"
        )
    groups: dict[str, list[StableArtifact]] = {}
    for attempt in attempts:
        groups.setdefault(str(attempt.payload["variant"]), []).append(attempt)
    if set(groups) != {"fixed", "buggy"} or any(
        len(value) != _REPETITIONS for value in groups.values()
    ):
        raise Gate16Error(f"{profile.task_id} control repetition coverage is incomplete")

    stable = all(
        len({item.payload["observed_outcome"] for item in group}) == 1
        for group in groups.values()
    )
    all_expected = all(item.payload["matched_expected"] is True for item in attempts)
    infrastructure_values = {
        "timed_out",
        "launch_failed",
        "invalid_test_count",
        "invalid_jest_result",
        "invalid_openra_result",
    }
    infrastructure = any(
        item.payload["observed_outcome"] in infrastructure_values
        or item.payload["process_outcome"] != "completed"
        for item in attempts
    )
    complete = stable and all_expected and not infrastructure
    outcome = (
        "p2p_only_control_matrix_complete"
        if complete
        else "p2p_only_control_matrix_infrastructure_blocked"
        if infrastructure
        else "p2p_only_control_matrix_failed"
    )
    return _write(
        root,
        _reference(profile, "p2p_only_gate16_aggregate"),
        chain + receipts + tuple(item.reference for item in attempts),
        {
            "candidate": profile.candidate,
            "task_id": profile.task_id,
            "state": "complete" if complete else "blocked",
            "outcome": outcome,
            "gate_1_6_complete": complete,
            "terminal": outcome == "p2p_only_control_matrix_failed",
            "resumable": infrastructure,
            "static_gate_evidence": static.to_value(),
            "runtime_versions": dict(_VERSIONS),
            "attempt_count": len(attempts),
            "expected_cell_count": _EXPECTED_CELLS,
            "repetitions_per_cell": _REPETITIONS,
            "stable_repetitions": stable,
            "all_expected": all_expected,
            "no_timeout_unknown_or_invalid": not infrastructure,
            "old_attempts_loaded": False,
            "inherited_attempt_count": 0,
            "cells": [
                {
                    "phase": "p2p",
                    "variant": variant,
                    "observed_outcomes": [
                        item.payload["observed_outcome"] for item in groups[variant]
                    ],
                    "expected_outcome": "pass",
                }
                for variant in ("fixed", "buggy")
            ],
            "candidate_execution_performed": True,
            "defect_differential_established": False,
            "defect_evidence_pending": "rendered_frame_differential",
        },
        authorization,
    )


def _write_transition(
    root: Path,
    profile: P2POnlyProfile,
    aggregate: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    complete = aggregate.payload.get("gate_1_6_complete") is True
    static = aggregate.payload["static_gate_evidence"]
    assert isinstance(static, Mapping)
    return _write(
        root,
        _reference(profile, "p2p_only_gate16_transition"),
        (aggregate.reference,),
        {
            "candidate": profile.candidate,
            "task_id": profile.task_id,
            "state": "passed" if complete else "blocked",
            "outcome": (
                "p2p_only_gate16_passed" if complete else "p2p_only_gate16_blocked"
            ),
            "complete": complete,
            "strict_gate_1_6": complete,
            "attempt_count": aggregate.payload["attempt_count"],
            "gate_1_6_status": {
                "source_authority": static["gate_1_source_authority"],
                "single_defect_mapping": static["gate_2_single_defect_mapping"],
                "strict_inverse_patch": static["gate_3_strict_inverse_patch"],
                "test_api_compatibility": static["gate_4_test_api_compatibility"],
                "f2p_absence_confirmed": static["gate_5_f2p_absence"],
                "fixed_buggy_p2p_3x": "passed" if complete else "blocked",
                # The strict track's two F2P names have no object here.  They are
                # published as inapplicable so a reader cannot read their absence
                # as an omission, nor their value as a differential.
                "fixed_f2p_3x": "not_applicable_no_f2p",
                "buggy_f2p_3x": "not_applicable_no_f2p",
            },
            "old_attempts_loaded": False,
            "candidate_execution_performed": True,
            "defect_differential_established": False,
        },
        authorization,
    )


def _write_admission(
    root: Path,
    profile: P2POnlyProfile,
    transition: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if transition.payload.get("strict_gate_1_6") is not True:
        raise Gate16Error(f"{profile.task_id} Gate 1--6 did not pass")
    return _write(
        root,
        _reference(profile, "p2p_only_track_admission"),
        (transition.reference,),
        {
            "candidate": profile.candidate,
            "task_id": profile.task_id,
            "state": "admitted_to_p2p_only_pool",
            "outcome": "p2p_only_track_admission_recorded",
            "complete": True,
            "defect_locus": profile.defect_locus,
            "strict_gate_1_6": True,
            "catalog_admission_ready": False,
            # Six passing control cells do not show a defect.  Admission here
            # authorizes frame capture, nothing downstream of it.
            "defect_differential_established": False,
            "authorizes": "rendered_frame_capture",
        },
        authorization,
    )


__all__ = [
    "CONFIRMATION",
    "EPOCH",
    "Gate16Error",
    "Gate16Result",
    "execute_gate_1_6",
    "materialize_pair",
    "patch_openra_runtime_roll_forward",
]
