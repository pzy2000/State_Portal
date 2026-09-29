"""Offline preflight descriptor for JupyterLab change 3168.

This module deliberately stops before materialisation, dependency setup, or
test execution.  It only reloads the already persisted source/patch/
atomicity/manifest chain and records the missing runtime prerequisites.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = CandidateKey("jupyterlab", 3168)
_ENUMERATION = ArtifactReference(
    "repository_enumeration/jupyterlab/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/jupyterlab/3168/source/v2",
    "candidate_source_packet",
    "2.0",
)
_PATCH = ArtifactReference(
    "candidate_test_patch/jupyterlab/3168/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/jupyterlab/3168/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/jupyterlab/3168/test_manifest/v2", "candidate_stage", "2.0"
)
_PROFILE = ArtifactReference(
    "candidate_profile/jupyterlab/3168/preflight/v2", "candidate_profile", "2.0"
)
_PRODUCTION_PATHS = (
    "buildutils/src/add-sibling.ts",
    "buildutils/src/build.ts",
    "buildutils/src/clean-packages.ts",
    "buildutils/src/create-package.ts",
    "buildutils/src/ensure-repo.ts",
    "buildutils/src/make-release.ts",
    "buildutils/src/patch-release.ts",
    "buildutils/src/remove-package.ts",
    "buildutils/src/utils.ts",
    "jupyterlab/build_handler.py",
    "jupyterlab/commands.py",
    "jupyterlab/extension.py",
    "jupyterlab/index.js",
    "jupyterlab/jlpmapp.py",
    "jupyterlab/labapp.py",
    "jupyterlab/labextensions.py",
    "jupyterlab/process.py",
    "jupyterlab/process_app.py",
    "jupyterlab/selenium_check.py",
    "jupyterlab/update.py",
    "jupyterlab/webpack.config.js",
    "jupyterlab/yarn.js",
    "packages/metapackage/watch-files.js",
    "scripts/ensure-buildutils.js",
    "setup.py",
    "setupbase.py",
)
_TEST_PATH = "test/src/rendermime/rendermime.spec.ts"
_SELECTORS = (
    "rendermime/index #resolveUrl() should resolve a relative url",
    "rendermime/index #resolveUrl() should ignore urls that have a protocol",
    "rendermime/index #getDownloadUrl() should resolve an absolute server url to a download url",
)


@dataclass(frozen=True)
class JupyterLab3168Preflight:
    candidate: CandidateKey
    profile_id: str
    source: ArtifactReference
    patch: ArtifactReference
    atomicity: ArtifactReference
    manifest: ArtifactReference
    production_paths: tuple[str, ...]
    test_path: str
    selectors: tuple[str, ...]
    reverse_patch_argv: tuple[str, ...]
    state: str
    outcome: str
    blockers: tuple[str, ...]


def _load(root: Path, ref: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=ref.artifact_id,
        artifact_kind=ref.artifact_kind,
        semantic_version=ref.semantic_version,
    )


def _check_chain(
    root: Path,
) -> tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact]:
    source, patch, atomicity, manifest = (
        _load(root, ref) for ref in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    )
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != _CANDIDATE.to_value()
        or source.payload.get("complete") is not True
    ):
        raise RuntimeError(
            "3168 source prerequisite is not the current typed candidate"
        )
    if (
        patch.inputs != (_ENUMERATION, _SOURCE)
        or patch.payload.get("candidate") != _CANDIDATE.to_value()
        or patch.payload.get("outcome") != "patch_eligible"
    ):
        raise RuntimeError("3168 patch prerequisite is not patch_eligible")
    if tuple(patch.payload.get("production_paths", ())) != _PRODUCTION_PATHS or tuple(
        patch.payload.get("test_paths", ())
    ) != (
        "jupyterlab/tests/mock_packages/extension/index.js",
        "jupyterlab/tests/mock_packages/extension/package.json",
        "jupyterlab/tests/mock_packages/incompat/package.json",
        "jupyterlab/tests/mock_packages/mimeextension/package.json",
        "jupyterlab/tests/test_build_api.py",
        "jupyterlab/tests/test_jupyterlab.py",
        "jupyterlab/tests/test_settings_api.py",
        "jupyterlab/tests/utils.py",
        "packages/services/test/integration_test.py",
        "test/package.json",
        _TEST_PATH,
    ):
        raise RuntimeError("3168 patch path partition changed")
    if atomicity.inputs != (
        _ENUMERATION,
        _SOURCE,
        _PATCH,
        ArtifactReference(
            "screening_checkpoint/jupyterlab/3168/current/v2",
            "screening_checkpoint",
            "2.0",
        ),
    ):
        raise RuntimeError("3168 atomicity dependency closure changed")
    if (
        atomicity.payload.get("candidate") != _CANDIDATE.to_value()
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
    ):
        raise RuntimeError("3168 source atomicity is not accepted")
    if (
        manifest.inputs != (_PATCH, _ATOMICITY)
        or manifest.payload.get("candidate") != _CANDIDATE.to_value()
        or manifest.payload.get("state") != "passed"
    ):
        raise RuntimeError("3168 test manifest is not passed")
    if manifest.payload.get("details") != {
        "executable_test_paths": [_TEST_PATH],
        "test_count": 1,
    }:
        raise RuntimeError("3168 executable test manifest changed")
    return source, patch, atomicity, manifest


def describe_jupyterlab_3168_preflight(
    artifact_root: str | Path,
) -> JupyterLab3168Preflight:
    """Reload prerequisites and describe the strict, not-yet-runnable profile."""

    root = Path(artifact_root).resolve()
    source, patch, atomicity, manifest = _check_chain(root)
    raw_patch = root / str(patch.payload["raw_patch_path"])
    if not raw_patch.is_file() or raw_patch.is_symlink():
        raise RuntimeError("3168 raw patch evidence is unavailable")
    text = raw_patch.read_text(encoding="utf-8", errors="strict")
    changed = tuple(re.findall(r"^diff --git a/(.*?) b/.*$", text, flags=re.MULTILINE))
    if not set(_PRODUCTION_PATHS).issubset(changed) or _TEST_PATH not in changed:
        raise RuntimeError("3168 production/test patch partition is incomplete")
    blockers = (
        "fixed_source_capsule_missing",
        "runtime_dependency_not_registered",
        "f2p_p2p_not_executed",
        "replay_oracle_golden_not_executed",
    )
    return JupyterLab3168Preflight(
        candidate=_CANDIDATE,
        profile_id="jupyterlab-3168-yarn-migration-v1",
        source=_SOURCE,
        patch=_PATCH,
        atomicity=_ATOMICITY,
        manifest=_MANIFEST,
        production_paths=_PRODUCTION_PATHS,
        test_path=_TEST_PATH,
        selectors=_SELECTORS,
        reverse_patch_argv=("git", "apply", "-R", "--whitespace=nowarn", "-"),
        state="terminal",
        outcome="infrastructure_blocked",
        blockers=blockers,
    )


def write_jupyterlab_3168_preflight(*, artifact_root: str | Path) -> StableArtifact:
    """Persist and reload the blocker; this function never executes a candidate."""

    root = Path(artifact_root).resolve()
    preflight = describe_jupyterlab_3168_preflight(root)
    source = _load(root, _SOURCE)
    now = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    payload: Mapping[str, object] = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": preflight.profile_id,
        "state": preflight.state,
        "outcome": preflight.outcome,
        "source_patch_atomicity_manifest_reloaded": True,
        "production_paths": list(preflight.production_paths),
        "test_path": preflight.test_path,
        "selectors": list(preflight.selectors),
        "reverse_patch_argv": list(preflight.reverse_patch_argv),
        "f2p_repetitions": 3,
        "p2p_repetitions": 3,
        "blockers": list(preflight.blockers),
    }
    write_stable_artifact(
        root,
        artifact_id=_PROFILE.artifact_id,
        artifact_kind=_PROFILE.artifact_kind,
        semantic_version=_PROFILE.semantic_version,
        created_at=now,
        inputs=(_SOURCE, _PATCH, _ATOMICITY, _MANIFEST),
        payload=dict(payload),
        authorization=source.authorization,
    )
    artifact = _load(root, _PROFILE)
    if artifact.payload != payload or artifact.inputs != (
        _SOURCE,
        _PATCH,
        _ATOMICITY,
        _MANIFEST,
    ):
        raise RuntimeError("3168 preflight typed reload mismatch")
    return artifact


__all__ = [
    "JupyterLab3168Preflight",
    "describe_jupyterlab_3168_preflight",
    "write_jupyterlab_3168_preflight",
]
