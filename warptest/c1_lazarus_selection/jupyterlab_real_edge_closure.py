"""Shared Gate 8--13 real-browser closure for modern JupyterLab candidates.

A candidate supplies a :class:`RealEdgeClosureSpec`: the production modules that
must differ between the variants, the shared production assets that must not, the
candidate-local harness files, and the hidden semantic expectation per variant.
The shared runner bundles each variant with the candidate-local Rspack, drives the
bundle in Microsoft Edge through Playwright, and writes the Gate 8--13 chain plus
the formal freeze admission.

It consults no historical attempt, reuses no diagnostic image, performs no paid
work and runs no formal trial.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

from PIL import Image, ImageChops, ImageStat

from .luanti_native_executor import NativeProcessReceipt, run_bounded_process
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_VERSION = "1.0"
_RSPACK = PurePosixPath("node_modules/@rspack/cli/bin/rspack.js")
_PLAYWRIGHT = PurePosixPath("node_modules/playwright/index.js")
_FRAMES = frozenset({"before", "after", "fixed_golden", "buggy_golden"})

ProcessRunner = Callable[..., NativeProcessReceipt]


@dataclass(frozen=True)
class RealEdgeClosureSpec:
    """Candidate-specific inputs for one real Microsoft Edge closure."""

    change_number: int
    epoch: str
    gate57_transition: ArtifactReference
    gate57_pool: ArtifactReference
    production_modules: tuple[str, ...]
    common_assets: tuple[str, ...]
    harness_files: Callable[[Path, Path], Mapping[str, str]] = field(repr=False)
    observation_marker: str
    registered_start: Mapping[str, object]
    replay_steps: tuple[str, ...]
    authoritative_defect: str
    expected_after: Mapping[str, Mapping[str, object]]
    semantic_state: Callable[[Mapping[str, object]], dict[str, object]] = field(
        repr=False
    )
    observation_payload: Callable[[Mapping[str, object]], dict[str, object]] = field(
        repr=False
    )
    start_is_registered: Callable[[Mapping[str, object]], bool] = field(repr=False)
    golden_frame_source: str
    actual_after_source: str
    global_cross_threshold: float
    # Some candidate surfaces repaint a few sub-pixels of caret or focus styling
    # between two semantically identical states, so the diagonal is allowed a
    # tolerance that stays orders of magnitude below the cross threshold.
    diagonal_tolerance: float = 0.0
    build_timeout_seconds: int = 900
    browser_timeout_seconds: int = 300

    def __post_init__(self) -> None:
        if (
            self.change_number <= 0
            or not self.production_modules
            or set(self.expected_after) != {"fixed", "buggy"}
            or self.expected_after["fixed"] == self.expected_after["buggy"]
            or self.global_cross_threshold <= 0
            or self.diagonal_tolerance < 0
            or self.diagonal_tolerance >= self.global_cross_threshold / 100
        ):
            raise ValueError("real Edge closure specification is invalid")

    @property
    def candidate(self) -> dict[str, object]:
        return {"repository_key": "jupyterlab", "change_number": self.change_number}

    @property
    def trees(self) -> PurePosixPath:
        return PurePosixPath(
            f"private/materialized/jupyterlab/{self.change_number}/current-v1"
        )

    @property
    def run_root(self) -> PurePosixPath:
        return PurePosixPath(
            f"private/bounded-runs/jupyterlab/{self.change_number}/{self.epoch}"
        )

    @property
    def marker_pattern(self) -> re.Pattern[bytes]:
        return re.compile(self.observation_marker.encode() + rb"=(\{.*\})")

    def reference(self, kind: str, suffix: str = "") -> ArtifactReference:
        tail = f"/{suffix}" if suffix else ""
        return ArtifactReference(
            f"{kind}/jupyterlab/{self.change_number}{tail}/{self.epoch}",
            kind,
            _VERSION,
        )

    @property
    def admission(self) -> ArtifactReference:
        return self.reference("candidate_formal_freeze_admission")


def execute_real_edge_closure(
    artifact_root: str | Path,
    *,
    spec: RealEdgeClosureSpec,
    authorization: AuthorizationRecord,
    process_runner: ProcessRunner | None = None,
) -> tuple[StableArtifact, ...]:
    """Execute fresh Gate 8--13 construction without any paid/formal trial."""

    _require_authorization(authorization)
    if type(spec) is not RealEdgeClosureSpec:
        raise ValueError("real Edge closure specification is invalid")
    root = Path(artifact_root).absolute().resolve(strict=True)
    transition = _load(root, spec.gate57_transition)
    pool = _load(root, spec.gate57_pool)
    _validate_strict_inputs(spec, transition, pool)
    trees = {variant: _tree(root, spec, variant) for variant in ("fixed", "buggy")}
    fingerprints = {
        variant: _fingerprints(spec, tree) for variant, tree in trees.items()
    }
    for asset in spec.common_assets:
        if fingerprints["fixed"][asset] != fingerprints["buggy"][asset]:
            raise RuntimeError(
                f"#{spec.change_number} common production asset drifted: {asset}"
            )
    if all(
        fingerprints["fixed"][module] == fingerprints["buggy"][module]
        for module in spec.production_modules
    ):
        raise RuntimeError(
            f"#{spec.change_number} fixed/buggy production build is not an inverse pair"
        )

    materialization = _write(
        root,
        spec.reference("candidate_gate8_materialization"),
        (transition.reference, pool.reference),
        {
            "candidate": spec.candidate,
            "state": "passed",
            "complete": True,
            "variants": {
                variant: {
                    "tree": tree.relative_to(root).as_posix(),
                    "fingerprints": fingerprints[variant],
                }
                for variant, tree in trees.items()
            },
            "production_modules": list(spec.production_modules),
            "common_production_assets": list(spec.common_assets),
            "candidate_local_direct_imports": True,
            "identical_common_assets_across_variants": True,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    runner = process_runner or run_bounded_process
    node = _node_executable()
    observations: list[StableArtifact] = []
    runtime_receipts: dict[str, dict[str, object]] = {}
    for variant, tree in trees.items():
        run = _ordinary_run_directory(root / spec.run_root / variant, root)
        for name, text in spec.harness_files(run, tree).items():
            target = run / name
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        environment = _environment(run)
        build_argv = (
            node,
            str(tree / _RSPACK),
            "build",
            "--config",
            str(run / "rspack.config.cjs"),
        )
        build = runner(
            build_argv,
            cwd=run,
            env=environment,
            timeout_seconds=spec.build_timeout_seconds,
        )
        _require_completed(build, f"#{spec.change_number} {variant} Rspack build")
        bundle = _ordinary_file(run / "dist/bundle.js", run)
        browser_argv = (node, str(run / "edge-observe.cjs"))
        browser = runner(
            browser_argv,
            cwd=run,
            env=environment,
            timeout_seconds=spec.browser_timeout_seconds,
        )
        _require_completed(
            browser, f"#{spec.change_number} {variant} Microsoft Edge observation"
        )
        data = _parse_observation(spec, browser)
        _validate_observation(spec, data, variant, run)
        runtime_receipts[variant] = {
            "rspack_argv": list(build_argv),
            "rspack_stdout_base64": base64.b64encode(build.stdout).decode("ascii"),
            "rspack_stderr_base64": base64.b64encode(build.stderr).decode("ascii"),
            "bundle_fingerprint": _sha256(bundle),
            "browser_argv": list(browser_argv),
            "browser_version": data["browser_version"],
            "browser_channel": data["browser_channel"],
        }
        observations.append(
            _write(
                root,
                spec.reference("candidate_gate11_edge_observation", variant),
                (materialization.reference,),
                {
                    "candidate": spec.candidate,
                    "variant": variant,
                    "state": "passed",
                    "complete": True,
                    **spec.observation_payload(data),
                    "semantic_state": spec.semantic_state(data),
                    "frames": {
                        key: (run / str(value)).relative_to(root).as_posix()
                        for key, value in dict(data["frames"]).items()
                    },
                    "capture_backend": "playwright_microsoft_edge_png",
                    "browser_channel": "msedge",
                    "after_dom_candidate_controlled": True,
                    "candidate_local_bundle": bundle.relative_to(root).as_posix(),
                    "old_attempts_loaded": False,
                    "paid_actions_authorized": False,
                    "formal_trials_authorized": False,
                },
                authorization,
            )
        )

    runtime = _write(
        root,
        spec.reference("candidate_gate8_runtime"),
        (materialization.reference,) + tuple(item.reference for item in observations),
        {
            "candidate": spec.candidate,
            "state": "passed",
            "complete": True,
            "bundler": "candidate_local_rspack",
            "browser": "Microsoft Edge via Playwright channel msedge",
            "variant_receipts": runtime_receipts,
            "fresh_process_per_variant": True,
            "candidate_execution_performed": True,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    by_variant = {str(item.payload["variant"]): item for item in observations}
    start_ok = all(
        spec.start_is_registered(item.payload) for item in observations
    )
    start = _write(
        root,
        spec.reference("candidate_gate9_start"),
        (runtime.reference,) + tuple(item.reference for item in observations),
        {
            "candidate": spec.candidate,
            "state": "passed" if start_ok else "blocked",
            "complete": start_ok,
            "registered_start": dict(spec.registered_start),
            "common_before_equivalent": start_ok,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    replay = _write(
        root,
        spec.reference("candidate_gate9_replay"),
        (start.reference,) + tuple(item.reference for item in observations),
        {
            "candidate": spec.candidate,
            "state": "passed" if start_ok else "blocked",
            "complete": start_ok,
            "ordered_replay": list(spec.replay_steps),
            "actual_after_source": spec.actual_after_source,
            "fresh_process_per_variant": True,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    observed = {
        variant: dict(by_variant[variant].payload["semantic_state"])
        for variant in ("fixed", "buggy")
    }
    expected = {
        variant: dict(value) for variant, value in spec.expected_after.items()
    }
    oracle_ok = start_ok and observed == expected
    oracle = _write(
        root,
        spec.reference("candidate_gate10_hidden_oracle"),
        (replay.reference,) + tuple(item.reference for item in observations),
        {
            "candidate": spec.candidate,
            "state": "passed" if oracle_ok else "blocked",
            "complete": oracle_ok,
            "hidden_until_after_candidate_execution": True,
            "authoritative_defect": spec.authoritative_defect,
            "expected_after": expected,
            "observed_after": observed,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    matrix = _write(
        root,
        spec.reference("candidate_gate12_semantic_pixel_matrix"),
        (oracle.reference,) + tuple(item.reference for item in observations),
        _matrix(root, spec, by_variant, observed, expected),
        authorization,
    )
    passed = all(
        item.payload["state"] == "passed" for item in (start, replay, oracle, matrix)
    )
    lineage = _write(
        root,
        spec.reference("candidate_gate13_same_lineage"),
        (
            transition.reference,
            pool.reference,
            materialization.reference,
            runtime.reference,
            start.reference,
            replay.reference,
            oracle.reference,
            *(item.reference for item in observations),
            matrix.reference,
        ),
        {
            "candidate": spec.candidate,
            "state": "passed" if passed else "blocked",
            "complete": passed,
            "same_lineage": passed,
            "gate57_epoch": "current-v1",
            "downstream_epoch": spec.epoch,
            "candidate_local_variant_bundles": True,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    if not passed:
        raise RuntimeError(
            f"#{spec.change_number} Gate 8--13 semantic/visual closure failed"
        )
    admission = _write(
        root,
        spec.admission,
        (lineage.reference, matrix.reference, oracle.reference),
        {
            "candidate": spec.candidate,
            "state": "admitted",
            "complete": True,
            "strict_gate_1_7": True,
            "gate8_materialization_runtime": "passed",
            "gate9_registered_start_replay": "passed",
            "gate10_hidden_oracle": "passed",
            "gate11_real_edge_observations": "passed",
            "gate12_semantic_pixel_2x2": "passed",
            "gate13_same_lineage": "passed",
            "formal_freeze_ready": True,
            "no_timeout_unknown_or_inheritance": True,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    return (
        materialization,
        runtime,
        start,
        replay,
        oracle,
        *observations,
        matrix,
        lineage,
        admission,
    )


def run_harness_observation(
    artifact_root: str | Path,
    *,
    change_number: int,
    variant: str,
    trees: PurePosixPath,
    run_root: PurePosixPath,
    required_tree_files: tuple[str, ...],
    harness_files: Callable[[Path, Path], Mapping[str, str]],
    observation_marker: str,
    build_timeout_seconds: int = 900,
    browser_timeout_seconds: int = 300,
    process_runner: ProcessRunner | None = None,
) -> dict[str, object]:
    """Build one materialized version's bundle and read its Edge observation.

    The Gate 8--13 closure and the lazarus probe share this step, so a probe never
    re-implements the harness that produced the frozen frames.  ``harness_files``
    may either return the files to write or write them itself and return nothing.
    """

    if variant not in {"fixed", "buggy"}:
        raise RuntimeError(f"#{change_number} variant must be fixed or buggy")
    root = Path(artifact_root).absolute().resolve(strict=True)
    tree = _ordinary_directory(root / trees / variant, root)
    for relative in (*required_tree_files, _RSPACK.as_posix(), _PLAYWRIGHT.as_posix()):
        _ordinary_file(tree / PurePosixPath(relative), tree)
    run = _ordinary_run_directory(root / run_root / variant, root)
    for name, text in dict(harness_files(run, tree) or {}).items():
        target = run / name
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    runner = process_runner or run_bounded_process
    node = _node_executable()
    environment = _environment(run)
    build = runner(
        (node, str(tree / _RSPACK), "build", "--config", str(run / "rspack.config.cjs")),
        cwd=run,
        env=environment,
        timeout_seconds=build_timeout_seconds,
    )
    _require_completed(build, f"#{change_number} {variant} Rspack build")
    _ordinary_file(run / "dist/bundle.js", run)
    browser = runner(
        (node, str(run / "edge-observe.cjs")),
        cwd=run,
        env=environment,
        timeout_seconds=browser_timeout_seconds,
    )
    _require_completed(browser, f"#{change_number} {variant} Microsoft Edge observation")
    pattern = re.compile(observation_marker.encode() + rb"=(\{.*\})")
    matches = [
        match
        for stream in (browser.stdout, browser.stderr)
        for match in pattern.finditer(stream)
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"#{change_number} Edge observation marker is missing or ambiguous"
        )
    data = json.loads(matches[0].group(1))
    if not isinstance(data, dict):
        raise RuntimeError(f"#{change_number} Edge observation marker is malformed")
    data["frame_paths"] = {
        key: (run / str(value)).relative_to(root).as_posix()
        for key, value in dict(data.get("frames") or {}).items()
    }
    return data


def run_variant_observation(
    artifact_root: str | Path,
    *,
    spec: RealEdgeClosureSpec,
    variant: str,
    run_root: PurePosixPath | None = None,
    process_runner: ProcessRunner | None = None,
) -> dict[str, object]:
    """Run one variant of a closure specification's harness."""

    return run_harness_observation(
        artifact_root,
        change_number=spec.change_number,
        variant=variant,
        trees=spec.trees,
        run_root=run_root or spec.run_root,
        required_tree_files=(*spec.production_modules, *spec.common_assets),
        harness_files=spec.harness_files,
        observation_marker=spec.observation_marker,
        build_timeout_seconds=spec.build_timeout_seconds,
        browser_timeout_seconds=spec.browser_timeout_seconds,
        process_runner=process_runner,
    )


def _validate_strict_inputs(
    spec: RealEdgeClosureSpec, transition: StableArtifact, pool: StableArtifact
) -> None:
    if (
        transition.reference != spec.gate57_transition
        or transition.payload.get("candidate") != spec.candidate
        or transition.payload.get("state") != "passed"
        or transition.payload.get("strict_gate_1_7") is not True
        or transition.payload.get("strict_no_timeout_unknown") is not True
        or transition.payload.get("lineage_inherited") is not False
        or transition.payload.get("old_attempts_loaded") is not False
        or transition.payload.get("gate8_13_performed") is not False
    ):
        raise RuntimeError(
            f"#{spec.change_number} strict Gate 1--7 transition is invalid"
        )
    if (
        pool.reference != spec.gate57_pool
        or pool.inputs != (transition.reference,)
        or pool.payload.get("candidate") != spec.candidate
        or pool.payload.get("state") != "active_qualified"
        or pool.payload.get("strict_gate_1_7") is not True
        or pool.payload.get("strict_no_timeout_unknown") is not True
        or pool.payload.get("lineage_inherited") is not False
        or pool.payload.get("old_attempts_loaded") is not False
        or pool.payload.get("gate8_13_performed") is not False
    ):
        raise RuntimeError(f"#{spec.change_number} strict pool admission is invalid")


def _matrix(
    root: Path,
    spec: RealEdgeClosureSpec,
    observations: dict[str, StableArtifact],
    observed: dict[str, dict[str, object]],
    expected: dict[str, dict[str, object]],
) -> dict[str, object]:
    paths: dict[str, dict[str, Path]] = {}
    for variant, artifact in observations.items():
        paths[variant] = {
            key: _ordinary_file(root / PurePosixPath(str(value)), root)
            for key, value in dict(artifact.payload["frames"]).items()
        }
    before_difference = _pixel_difference(
        paths["fixed"]["before"], paths["buggy"]["before"]
    )
    pixel = {
        variant: {
            golden: _pixel_difference(
                paths[variant]["after"], paths[variant][f"{golden}_golden"]
            )
            for golden in ("fixed", "buggy")
        }
        for variant in ("fixed", "buggy")
    }
    sizes = {
        variant: _image_size(paths[variant]["after"]) for variant in ("fixed", "buggy")
    }
    if len(set(sizes.values())) != 1:
        raise RuntimeError(f"#{spec.change_number} Edge frame dimensions drifted")
    semantic = {
        variant: {
            golden: observed[variant] == expected[golden]
            for golden in ("fixed", "buggy")
        }
        for variant in ("fixed", "buggy")
    }
    threshold = spec.global_cross_threshold
    tolerance = spec.diagonal_tolerance
    passed = (
        before_difference <= tolerance
        and semantic
        == {
            "fixed": {"fixed": True, "buggy": False},
            "buggy": {"fixed": False, "buggy": True},
        }
        and pixel["fixed"]["fixed"] <= tolerance
        and pixel["fixed"]["buggy"] > threshold
        and pixel["buggy"]["fixed"] > threshold
        and pixel["buggy"]["buggy"] <= tolerance
    )
    return {
        "candidate": spec.candidate,
        "state": "passed" if passed else "blocked",
        "complete": passed,
        "common_before_pixel_difference_mean": before_difference,
        "common_before_equivalent": before_difference <= tolerance,
        "semantic_two_by_two": semantic,
        "pixel_difference_mean_two_by_two": pixel,
        "cross_rejection_thresholds": {
            "global_mean_gt": threshold,
            "diagonal_mean_le": tolerance,
        },
        "diagonal_matches_and_cross_rejects": passed,
        "synthetic_observation_frames": False,
        "golden_frame_source": spec.golden_frame_source,
        "actual_after_source": spec.actual_after_source,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }


def _tree(root: Path, spec: RealEdgeClosureSpec, variant: str) -> Path:
    tree = _ordinary_directory(root / spec.trees / variant, root)
    relatives = (
        *spec.production_modules,
        *spec.common_assets,
        _RSPACK.as_posix(),
        _PLAYWRIGHT.as_posix(),
    )
    for relative in relatives:
        _ordinary_file(tree / PurePosixPath(relative), tree)
    return tree


def _fingerprints(spec: RealEdgeClosureSpec, tree: Path) -> dict[str, str]:
    relatives = (
        *spec.production_modules,
        *spec.common_assets,
        _RSPACK.as_posix(),
        _PLAYWRIGHT.as_posix(),
    )
    return {
        relative: _sha256(tree / PurePosixPath(relative)) for relative in relatives
    }


def _parse_observation(
    spec: RealEdgeClosureSpec, receipt: NativeProcessReceipt
) -> dict[str, object]:
    pattern = spec.marker_pattern
    matches = [
        match
        for stream in (receipt.stdout, receipt.stderr)
        for match in pattern.finditer(stream)
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"#{spec.change_number} Edge observation marker is missing or ambiguous"
        )
    value = json.loads(matches[0].group(1))
    if not isinstance(value, dict):
        raise RuntimeError(f"#{spec.change_number} Edge observation marker is malformed")
    return value


def _validate_observation(
    spec: RealEdgeClosureSpec, data: dict[str, object], variant: str, run: Path
) -> None:
    frames = data.get("frames")
    if (
        data.get("browser_channel") != "msedge"
        or not isinstance(data.get("browser_version"), str)
        or not data["browser_version"]
        or not isinstance(frames, dict)
        or set(frames) != set(_FRAMES)
    ):
        raise RuntimeError(
            f"#{spec.change_number} {variant} Edge observation envelope is invalid"
        )
    if spec.semantic_state(data) != dict(spec.expected_after[variant]):
        raise RuntimeError(
            f"#{spec.change_number} {variant} Edge semantic observation failed"
        )
    for name in frames.values():
        if not isinstance(name, str):
            raise RuntimeError(f"#{spec.change_number} Edge frame path is malformed")
        _ordinary_file(run / PurePosixPath(name), run)


def _require_completed(receipt: NativeProcessReceipt, label: str) -> None:
    if receipt.outcome != "completed" or receipt.returncode != 0:
        raise RuntimeError(f"{label} failed: {receipt.outcome}/{receipt.returncode}")


def _environment(run: Path) -> dict[str, str]:
    home = run / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    return {**os.environ, "HOME": str(home), "LANG": "C", "LC_ALL": "C", "CI": "1"}


def _node_executable() -> str:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is unavailable")
    return str(Path(node).resolve(strict=True))


def _pixel_difference(left_path: Path, right_path: Path) -> float:
    with (
        Image.open(left_path).convert("RGB") as left,
        Image.open(right_path).convert("RGB") as right,
    ):
        if left.size != right.size:
            return float("inf")
        return float(sum(ImageStat.Stat(ImageChops.difference(left, right)).mean) / 3)


def _image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


def _ordinary_run_directory(path: Path, root: Path) -> Path:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    return _ordinary_directory(path, root)


def _ordinary_directory(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError(f"required directory is missing or unsafe: {path}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"required directory is missing or unsafe: {path}")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError(f"required file is missing or unsafe: {path}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"required file is missing or unsafe: {path}")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_authorization(value: AuthorizationRecord) -> None:
    if (
        type(value) is not AuthorizationRecord
        or value.scope != "preannotation-execution"
        or value.revoked
        or value.local_user_id != os.getuid()
    ):
        raise PermissionError("active preannotation-execution authorization is required")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _write(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    try:
        existing = _load(root, reference)
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"artifact conflict: {reference.artifact_id}")
        return existing
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
    return _load(root, reference)


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "RealEdgeClosureSpec",
    "execute_real_edge_closure",
    "run_harness_observation",
    "run_variant_observation",
]
