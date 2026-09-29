"""Real-runtime golden capture for the Luanti Phase-0 Lazarus candidate."""

from __future__ import annotations

import copy
import hashlib
import os
import platform
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .c1_sandbox import strict_clone_tree
from .lazarus_goldens import (
    LazarusGoldenFrame,
    LazarusGoldenSet,
    register_lazarus_golden_set,
)
from .lazarus_probes import (
    luanti_pr11919_probe,
    luanti_pr11919_public_view_assertion,
    luanti_pr14396_probe,
    luanti_pr14757_probe,
)
from .lazarus_versions import LazarusVersionArtifact, LazarusVersionPair
from .luanti_runtime import LuantiRuntimeAdapter


_CMAKE = "/opt/miniconda3/bin/cmake"
LUANTI_PHASE0_TASK_ID = "lz-luanti-11919-vector-from-string"
LUANTI_GOLDEN_SANDBOX_ROOT = Path("/private/tmp/warptest-lazarus-goldens")
# The client must receive the authoritative player position/orientation before
# the engine hook freezes the frame.  Earlier frames were timing-sensitive across
# version clones; the independently measured frame 1000 produces pixel-identical
# public Start frames while remaining comfortably inside the 180s probe bound.
LUANTI_GOLDEN_CAPTURE_FRAME = "1000"
_LUANTI_CAPTURE_VANTAGE = {
    "type": "luanti_set_player_pos",
    "pos": {"x": 0, "y": 3, "z": 6},
    "hover": True,
    "look_at": {"x": 0, "y": 1, "z": 0},
}
LuantiFrameRunner = Callable[..., LazarusGoldenFrame]


@dataclass(frozen=True)
class LuantiGoldenProfile:
    """Which registered task a capture may run and what its frames must show."""

    task_id: str
    label: str
    goal_probe: Callable[[], dict[str, Any]]
    defect_node: str
    start_name: str
    signature_name: str
    # An engine-level inverse patch changes the client executable, which the
    # inherited macOS bundle does not carry.  Such a profile must build its own
    # bundle per version or its frames would render the same client twice.
    requires_version_client: bool = False


PR11919_GOLDEN_PROFILE = LuantiGoldenProfile(
    task_id=LUANTI_PHASE0_TASK_ID,
    label="luanti-pr11919",
    goal_probe=luanti_pr11919_probe,
    defect_node="basenodes:desert_sand",
    start_name="Luanti PR 11919 prepared starting world",
    signature_name="Luanti PR 11919 historical outcome projection",
)
PR14757_GOLDEN_PROFILE = LuantiGoldenProfile(
    task_id="lz-luanti-14757-math-round",
    label="luanti-pr14757",
    goal_probe=luanti_pr14757_probe,
    defect_node="basenodes:desert_sand",
    start_name="Luanti PR 14757 prepared starting world",
    signature_name="Luanti PR 14757 historical outcome projection",
)
PR14396_GOLDEN_PROFILE = LuantiGoldenProfile(
    task_id="lz-luanti-14396-metadata-set-string-nil",
    label="luanti-pr14396",
    goal_probe=luanti_pr14396_probe,
    defect_node="basenodes:desert_sand",
    start_name="Luanti PR 14396 prepared starting world",
    signature_name="Luanti PR 14396 historical outcome projection",
    requires_version_client=True,
)
LUANTI_GOLDEN_PROFILES: dict[str, LuantiGoldenProfile] = {
    profile.task_id: profile
    for profile in (
        PR11919_GOLDEN_PROFILE,
        PR14757_GOLDEN_PROFILE,
        PR14396_GOLDEN_PROFILE,
    )
}


def capture_luanti_goldens(
    pair: LazarusVersionPair,
    *,
    profile: LuantiGoldenProfile,
    repository_root: str | Path,
    output_dir: str | Path,
    sandbox_root: str | Path = LUANTI_GOLDEN_SANDBOX_ROOT,
    frame_runner: LuantiFrameRunner | None = None,
    app_bundle_cache_root: str | Path | None = None,
) -> LazarusGoldenSet:
    """Capture the fixed/buggy Luanti Phase-0 triplet in disposable CoW clones.

    The buggy after-frame runs the hidden defect-signature projection, so a
    successful capture establishes that its visible frame really corresponds to
    the historical outcome.  The public task text and model payload never pass
    through this module.
    """

    _validate_pair(pair, profile)
    runner = frame_runner or _capture_luanti_frame
    app_bundle_cache_root = (
        Path(app_bundle_cache_root).expanduser().resolve()
        if app_bundle_cache_root is not None
        else None
    )
    sandbox = Path(sandbox_root).expanduser().resolve()
    sandbox.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f"{profile.label}-", dir=sandbox
    ) as temp_dir:
        work_dir = Path(temp_dir).resolve()
        bundles: dict[str, Path] = {}
        if profile.requires_version_client and platform.system() == "Darwin":
            bundles = _stage_version_app_bundles(
                pair, work_dir, cache_root=app_bundle_cache_root
            )

        def capture(
            artifact: LazarusVersionArtifact, stage: str, spec: dict[str, Any]
        ) -> LazarusGoldenFrame:
            bundle = bundles.get(artifact.version)
            if bundle is None:
                return runner(artifact, stage, spec, work_dir)
            return runner(artifact, stage, spec, work_dir, bundle)

        with _temporary_environment(
            "WARPTEST_LUANTI_SCREENSHOT_FRAME", LUANTI_GOLDEN_CAPTURE_FRAME
        ):
            fixed_before = capture(pair.fixed, "before", _start_probe(profile))
            buggy_before = capture(pair.buggy, "before", _start_probe(profile))
            fixed_after = capture(pair.fixed, "after", _golden_goal_probe(profile))
            buggy_after = capture(
                pair.buggy, "after", _golden_signature_probe(profile)
            )
        debug_value = os.environ.get("WARPTEST_LAZARUS_GOLDEN_DEBUG_DIR")
        if debug_value:
            debug_dir = Path(debug_value).expanduser().resolve()
            debug_dir.mkdir(parents=True, exist_ok=True)
            for name, frame in (
                ("fixed-before", fixed_before),
                ("buggy-before", buggy_before),
                ("fixed-after", fixed_after),
                ("buggy-after", buggy_after),
            ):
                shutil.copy2(frame.image, debug_dir / f"{name}.png")
                shutil.copy2(frame.report, debug_dir / f"{name}.json")
        return register_lazarus_golden_set(
            task_id=profile.task_id,
            repository_root=repository_root,
            output_dir=output_dir,
            fixed_before=fixed_before,
            buggy_before=buggy_before,
            fixed_after=fixed_after,
            buggy_after=buggy_after,
            # The Luanti server banner and join messages vary by uptime.  They
            # occupy the top of the fixed 1280x720 capture and are not part of
            # the public world state, so only the rendered world/HUD is compared.
            ignore_top_fraction=0.25,
            before_max_changed_pixel_ratio=0.01,
            after_min_changed_pixel_ratio=0.001,
        )


def capture_luanti_pr11919_goldens(
    pair: LazarusVersionPair, **inputs: Any
) -> LazarusGoldenSet:
    """Capture the PR #11919 triplet through the registered profile."""

    return capture_luanti_goldens(pair, profile=PR11919_GOLDEN_PROFILE, **inputs)


def _start_probe(profile: LuantiGoldenProfile) -> dict[str, Any]:
    probe = profile.goal_probe()
    probe["name"] = profile.start_name
    probe["actions"] = []
    # The mod-error invariant is graded on the start frame too, so the start run
    # has to record it rather than leave the gate to assume it held.
    probe["assertions"] = [
        luanti_pr11919_public_view_assertion(),
        {"type": "no_luanti_mod_errors"},
    ]
    return probe


def _golden_goal_probe(profile: LuantiGoldenProfile) -> dict[str, Any]:
    """Append the registered view reset after the real upstream action.

    The reset is capture-only: PR F2P/P2P probes retain the upstream action
    sequence unchanged.  It prevents client-side spawn orientation timing from
    pushing the landing cell outside the after-frame.
    """

    probe = profile.goal_probe()
    probe["actions"].append(copy.deepcopy(_LUANTI_CAPTURE_VANTAGE))
    return probe


def _golden_signature_probe(profile: LuantiGoldenProfile) -> dict[str, Any]:
    probe = _golden_goal_probe(profile)
    probe["name"] = profile.signature_name
    assertions = probe.get("assertions")
    if not isinstance(assertions, list) or not assertions:
        raise RuntimeError(f"{profile.task_id} probe is missing its goal assertion")
    first = assertions[0]
    if not isinstance(first, dict) or first.get("type") != "luanti_node_is":
        raise RuntimeError(f"{profile.task_id} probe no longer has a node goal")
    first["expected"] = profile.defect_node
    return probe


def _macos_app_bundle_path(root: Path) -> Path:
    return root / "build-app/out/luanti.app"


def build_version_app_bundle(clone: Path, *, timeout_s: float = 3_600.0) -> Path:
    """Build the macOS client bundle from one version's own sources.

    The bundle a materialized version inherits was produced before the pair
    existed, so both versions ship the *same* ``luanti.app`` executable.  A
    Lua-only inverse patch survives that because :func:`sync_luanti_builtin_assets`
    overlays the versioned ``builtin/`` tree, but an engine-level patch would be
    invisible: the capture would render the identical client twice.  Building the
    bundle from the clone's own sources is what makes an engine-level Lazarus
    row's frames version-specific.
    """

    root = Path(clone).expanduser().resolve()
    build_dir = root / "build-app"
    shutil.rmtree(build_dir, ignore_errors=True)
    env = LuantiRuntimeAdapter._with_homebrew_prefix(dict(os.environ))
    configure = [
        _CMAKE,
        "-S",
        str(root),
        "-B",
        str(build_dir),
        "-DCMAKE_BUILD_TYPE=Release",
        # The bundle resolves its assets from ``Contents/Resources``, so it must
        # not be an in-place build.
        "-DRUN_IN_PLACE=FALSE",
        f"-DCMAKE_INSTALL_PREFIX={build_dir / 'out'}",
        "-DBUILD_CLIENT=TRUE",
        "-DBUILD_SERVER=FALSE",
        "-DBUILD_UNITTESTS=FALSE",
        "-DENABLE_GETTEXT=TRUE",
        "-DCMAKE_FIND_FRAMEWORK=LAST",
        "-DCMAKE_CXX_FLAGS=-DLIBICONV_PLUG",
        "-DCMAKE_EXE_LINKER_FLAGS=-lbz2",
    ]
    parallel = str(os.cpu_count() or 2)
    commands = [
        configure,
        # ``install`` copies compiled locale catalogues, so they must exist.
        [_CMAKE, "--build", str(build_dir), "--target", "translations", "--parallel", parallel],
        [_CMAKE, "--build", str(build_dir), "--target", "luanti", "--parallel", parallel],
        [_CMAKE, "--install", str(build_dir)],
    ]
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(
                f"Luanti bundle build failed: {' '.join(command)}: {detail[-800:]}"
            )
    bundle = _macos_app_bundle_path(root)
    executable = bundle / "Contents/MacOS/luanti"
    if not executable.is_file():
        raise RuntimeError(f"Luanti bundle build did not produce {executable}")
    return bundle


def _stage_version_app_bundles(
    pair: LazarusVersionPair, work_dir: Path, *, cache_root: Path | None = None
) -> dict[str, Path]:
    """Build one bundle per version so every frame clone can reuse it.

    The build is keyed by the materialized version's own client sources, so a
    re-capture of an unchanged pair reuses the bundle instead of spending ten
    minutes per version rebuilding identical bytes.
    """

    staged: dict[str, Path] = {}
    for artifact in (pair.fixed, pair.buggy):
        source = artifact.root.expanduser().resolve()
        if cache_root is not None:
            staged[artifact.version] = ensure_version_app_bundle(
                source,
                task_id=artifact.task_id,
                version=artifact.version,
                cache_root=cache_root,
            )
            continue
        stage_root = work_dir / f"{artifact.version}-app"
        strict_clone_tree(source, stage_root)
        staged[artifact.version] = build_version_app_bundle(stage_root)
    return staged


def ensure_version_app_bundle(
    source: str | Path, *, task_id: str, version: str, cache_root: str | Path
) -> Path:
    """Return a bundle built from one version's own sources, building if needed.

    On macOS every client run resolves the app bundle rather than ``bin/luanti``,
    so any replay that must tell two versions apart needs this, not only the
    golden capture.
    """

    root = Path(source).expanduser().resolve()
    cached = (
        Path(cache_root).expanduser().resolve()
        / task_id
        / f"{version}-{_client_source_fingerprint(root)}"
    )
    bundle = _macos_app_bundle_path(cached)
    if (bundle / "Contents/MacOS/luanti").is_file():
        return bundle
    shutil.rmtree(cached, ignore_errors=True)
    cached.parent.mkdir(parents=True, exist_ok=True)
    strict_clone_tree(root, cached)
    return build_version_app_bundle(cached)


def _client_source_fingerprint(root: Path) -> str:
    """Fingerprint the engine sources the bundle would be compiled from."""

    digest = hashlib.sha256()
    for relative in sorted(
        path.relative_to(root).as_posix()
        for path in (root / "src").rglob("*")
        if path.is_file()
    ):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256((root / relative).read_bytes()).digest())
    return digest.hexdigest()[:16]


def _capture_luanti_frame(
    artifact: LazarusVersionArtifact,
    stage: str,
    spec: dict[str, Any],
    work_dir: Path,
    app_bundle: Path | None = None,
) -> LazarusGoldenFrame:
    if stage not in {"before", "after"}:
        raise ValueError("Luanti golden stage must be before or after")
    trial_root = work_dir / f"{artifact.version}-{stage}-trial"
    source = artifact.root.expanduser().resolve()
    strict_clone_tree(source, trial_root)
    if app_bundle is not None:
        destination = _macos_app_bundle_path(trial_root)
        shutil.rmtree(destination, ignore_errors=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        strict_clone_tree(app_bundle, destination)
    output = work_dir / f"{artifact.version}-{stage}-output"
    output.mkdir(parents=True, exist_ok=False)
    spec_path = output / f"{artifact.version}-{stage}.yaml"
    with _temporary_environment(
        "WARPTEST_LUANTI_TRUSTED_GIT_DIR", str(artifact.git_metadata)
    ):
        with LuantiRuntimeAdapter(
            trial_root,
            output,
            headless=True,
            visual_capture=True,
            sync_builtin_from_repo=True,
            daylight_mode="fixed_noon",
            experiment_seed=0,
        ) as runtime:
            result = runtime.run_checkpoint(copy.deepcopy(spec), spec_path)
            screenshot = Path(runtime.screenshot(f"{artifact.version}-{stage}.png"))
    report_value = result.get("report_path")
    report = Path(str(report_value or "")).expanduser().resolve()
    if not report.is_file():
        raise RuntimeError(
            f"Luanti {artifact.version} {stage} capture has no runtime report"
        )
    return LazarusGoldenFrame(
        image=screenshot,
        report=report,
        expected_semantics_ok=result.get("status") == "success",
    )


def _validate_pair(pair: LazarusVersionPair, profile: LuantiGoldenProfile) -> None:
    if pair.fixed.task_id != profile.task_id or pair.buggy.task_id != profile.task_id:
        raise ValueError("Luanti golden capture requires the registered Phase-0 task")
    if pair.fixed.version != "fixed" or pair.buggy.version != "buggy":
        raise ValueError("Luanti golden capture requires fixed and buggy artifacts")
    if pair.fixed.patch_sha256 != pair.buggy.patch_sha256:
        raise ValueError("Luanti golden capture requires one matched inverse patch")


@contextmanager
def _temporary_environment(name: str, value: str) -> Iterator[None]:
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous
