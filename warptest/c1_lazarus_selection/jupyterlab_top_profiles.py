"""Candidate-level preparation for the strongest JupyterLab C1 candidates.

The 10-hour freeze sprint uses a two-level protocol.  This module owns the
candidate-level part of that protocol for a small, deliberately closed set of
JupyterLab changes.  It reloads the current source, patch, atomicity and test
manifest artifacts, performs a static production-only inverse-patch check, and
records a preparation receipt.  It never contacts GitHub, installs a runtime,
builds a package, starts a browser, or runs candidate code.

Runtime, repetition, replay, oracle and visual work are represented as
``downstream_pending``.  Missing historical runtime evidence therefore does
not turn a source/patch-qualified candidate into a rejection.
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_ENUMERATION = ArtifactReference(
    "repository_enumeration/jupyterlab/current/v2", "repository_enumeration", "2.0"
)
_AUTHORITY = "github:jupyterlab/jupyterlab"
_MIRROR = PurePosixPath("private/repository-enumeration-git/jupyterlab/mirror.git")


def _safe_relative(value: object, label: str) -> PurePosixPath:
    if type(value) is not str or not value:
        raise RuntimeError(f"{label} is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or "." in path.parts
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise RuntimeError(f"{label} is unsafe")
    return path


@dataclass(frozen=True)
class JupyterLabTopProfile:
    """Stable candidate-specific metadata, independent of runtime assets."""

    candidate: CandidateKey
    primary_issue_number: int
    profile_id: str
    production_paths: tuple[str, ...]
    patch_test_paths: tuple[str, ...]
    selected_test_path: str
    selected_test_marker: str
    f2p_selector: str
    p2p_selector: str
    package_version: str
    package_metadata_path: str
    test_harness_root: str
    karma_config_path: str
    webpack_config_path: str
    fixed_source_tree_path: str
    runtime_dependency_id: str
    runtime_root: str
    package_manager: str
    tier: str = "at_goal_candidate"

    def __post_init__(self) -> None:
        if self.candidate.repository_key != "jupyterlab":
            raise ValueError("JupyterLab top profile has the wrong repository")
        if not self.profile_id or self.primary_issue_number < 1:
            raise ValueError("JupyterLab top profile identity is invalid")
        paths = self.production_paths + self.patch_test_paths
        if not self.production_paths or not self.patch_test_paths:
            raise ValueError("JupyterLab top profile paths are empty")
        if len(set(paths)) != len(paths):
            raise ValueError("JupyterLab top profile paths overlap")
        if self.selected_test_path not in self.patch_test_paths:
            raise ValueError("JupyterLab selected test is not in the patch")
        for value in (
            self.package_metadata_path,
            self.test_harness_root,
            self.karma_config_path,
            self.webpack_config_path,
            self.fixed_source_tree_path,
            self.runtime_root,
            *paths,
        ):
            _safe_relative(value, "JupyterLab top profile path")
        if self.package_manager not in {"npm", "jlpm"}:
            raise ValueError("JupyterLab package manager is invalid")

    @property
    def source_reference(self) -> ArtifactReference:
        return _reference(self.candidate.change_number, "source")

    @property
    def patch_reference(self) -> ArtifactReference:
        return _reference(self.candidate.change_number, "patch")

    @property
    def atomicity_reference(self) -> ArtifactReference:
        return _reference(self.candidate.change_number, "atomicity")

    @property
    def manifest_reference(self) -> ArtifactReference:
        return _reference(self.candidate.change_number, "manifest")

    @property
    def capsule_reference(self) -> ArtifactReference:
        return _reference(self.candidate.change_number, "capsule")

    @property
    def profile_reference(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_profile/jupyterlab/{self.candidate.change_number}/preflight/v2",
            "candidate_profile",
            "2.0",
        )

    @property
    def preparation_reference(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_preparation/jupyterlab/{self.candidate.change_number}/preannotation/v2",
            "candidate_preparation",
            "2.0",
        )


def _profile(
    number: int,
    *,
    issue: int,
    profile_id: str,
    production_paths: tuple[str, ...],
    patch_test_paths: tuple[str, ...],
    selected_test_path: str,
    selected_test_marker: str,
    f2p_selector: str,
    p2p_selector: str,
    package_version: str,
    package_metadata_path: str,
    test_harness_root: str,
    karma_config_path: str,
    webpack_config_path: str,
    runtime_dependency_id: str,
    runtime_root: str,
    package_manager: str = "npm",
    tier: str = "at_goal_candidate",
) -> JupyterLabTopProfile:
    return JupyterLabTopProfile(
        candidate=CandidateKey("jupyterlab", number),
        primary_issue_number=issue,
        profile_id=profile_id,
        production_paths=production_paths,
        patch_test_paths=patch_test_paths,
        selected_test_path=selected_test_path,
        selected_test_marker=selected_test_marker,
        f2p_selector=f2p_selector,
        p2p_selector=p2p_selector,
        package_version=package_version,
        package_metadata_path=package_metadata_path,
        test_harness_root=test_harness_root,
        karma_config_path=karma_config_path,
        webpack_config_path=webpack_config_path,
        fixed_source_tree_path=(
            f"private/fixed-capsules/jupyterlab/{number}/captured-merged-tree"
        ),
        runtime_dependency_id=runtime_dependency_id,
        runtime_root=runtime_root,
        package_manager=package_manager,
        tier=tier,
    )


_PROFILES: dict[int, JupyterLabTopProfile] = {
    3867: _profile(
        3867,
        issue=3862,
        profile_id="jupyterlab-3867-islocal-qualified-url-v1",
        production_paths=("packages/coreutils/src/url.ts",),
        patch_test_paths=("tests/test-coreutils/src/url.spec.ts",),
        selected_test_path="tests/test-coreutils/src/url.spec.ts",
        selected_test_marker="should test whether the url is a local url",
        f2p_selector=(
            "@jupyterlab/coreutils URLExt .isLocal() "
            "should test whether the url is a local url"
        ),
        p2p_selector=(
            "@jupyterlab/coreutils URLExt .parse() "
            "should parse a url into a URLExt object"
        ),
        package_version="0.2.4",
        package_metadata_path="tests/test-coreutils/package.json",
        test_harness_root="tests",
        karma_config_path="tests/karma.conf.js",
        webpack_config_path="tests/webpack.config.js",
        runtime_dependency_id="jupyterlab-node5-karma1.7.1-webpack2-typescript2.6.2",
        runtime_root=(
            "private/dependencies/jupyterlab-3867/"
            "node5-karma1.7.1-webpack2-typescript2.6.2/1.0"
        ),
    ),
    1846: _profile(
        1846,
        issue=1834,
        profile_id="jupyterlab-1846-gfm-latex-delimiter-v2",
        production_paths=("src/renderers/latex.ts",),
        patch_test_paths=("test/src/renderers/latex.spec.ts",),
        selected_test_path="test/src/renderers/latex.spec.ts",
        selected_test_marker=r"should handle `\\\\\(` delimiters for math",
        f2p_selector="jupyter-ui removeMath() should handle `\\(` delimiters in GFM",
        p2p_selector="jupyter-ui removeMath() should handle code spans",
        package_version="0.18.0-0",
        package_metadata_path="package.json",
        test_harness_root="test",
        karma_config_path="test/karma.conf.js",
        webpack_config_path="test/webpack.config.js",
        runtime_dependency_id="jupyterlab-node5-karma0.13-webpack1-typescript2.1.6",
        runtime_root=(
            "private/dependencies/jupyterlab-1846/"
            "node5-karma0.13-webpack1-typescript2.1.6/1.0"
        ),
        tier="source_closure_pending",
    ),
    2752: _profile(
        2752,
        issue=2751,
        profile_id="jupyterlab-2752-multiline-source-v2",
        production_paths=("packages/cells/src/model.ts",),
        patch_test_paths=("test/src/cells/model.spec.ts",),
        selected_test_path="test/src/cells/model.spec.ts",
        selected_test_marker="should accept a base cell argument with a multiline source",
        f2p_selector=(
            "cells/model CellModel #constructor() "
            "should accept a base cell argument with a multiline source"
        ),
        p2p_selector="cells/model CellModel #constructor() should create a cell model",
        package_version="0.9.0",
        package_metadata_path="test/package.json",
        test_harness_root="test",
        karma_config_path="test/karma.conf.js",
        webpack_config_path="test/webpack.config.js",
        runtime_dependency_id="jupyterlab-node5-karma1.7-webpack2-typescript2.4.2",
        runtime_root=(
            "private/dependencies/jupyterlab-2752/"
            "node5-karma1.7-webpack2-typescript2.4.2/1.0"
        ),
    ),
    2437: _profile(
        2437,
        issue=2436,
        profile_id="jupyterlab-2437-slideshow-metadata-shape-v2",
        production_paths=("packages/notebook/src/celltools.ts",),
        patch_test_paths=("test/src/notebook/celltools.spec.ts",),
        selected_test_path="test/src/notebook/celltools.spec.ts",
        selected_test_marker=(
            "expect(metadata.get('slideshow')).to.eql({ 'slide_type': 'slide' });"
        ),
        f2p_selector=(
            "notebook/celltools CellTools.createSlideShowSelector() "
            "should create a slide show selector"
        ),
        p2p_selector=(
            "notebook/celltools CellTools.createNBConvertSelector() "
            "should create a raw mimetype selector"
        ),
        package_version="0.6.1",
        package_metadata_path="test/package.json",
        test_harness_root="test",
        karma_config_path="test/karma.conf.js",
        webpack_config_path="test/webpack.config.js",
        runtime_dependency_id="jupyterlab-node5-karma1.7-webpack2-typescript2.2.1",
        runtime_root=(
            "private/dependencies/jupyterlab-2437/"
            "node5-karma1.7-webpack2-typescript2.2.1/1.0"
        ),
    ),
    4796: _profile(
        4796,
        issue=4795,
        profile_id="jupyterlab-4796-single-line-csv-v1",
        production_paths=("packages/csvviewer/src/model.ts",),
        patch_test_paths=(
            "tests/test-csvviewer/src/model.spec.ts",
            "tests/test-csvviewer/src/parse-noquotes.spec.ts",
            "tests/test-csvviewer/src/parse.spec.ts",
        ),
        selected_test_path="tests/test-csvviewer/src/model.spec.ts",
        selected_test_marker="handles having only a header",
        f2p_selector="csvviewer/model DSVModel handles having only a header",
        p2p_selector="csvviewer/model DSVModel handles not having a header",
        package_version="0.3.2",
        package_metadata_path="tests/test-csvviewer/package.json",
        test_harness_root="tests",
        karma_config_path="tests/karma.conf.js",
        webpack_config_path="tests/webpack.config.js",
        runtime_dependency_id="jupyterlab-node5-karma1.7-webpack4-typescript2.9.1",
        runtime_root=(
            "private/dependencies/jupyterlab-4796/"
            "node5-karma1.7-webpack4-typescript2.9.1/1.0"
        ),
        tier="source_closure_pending",
    ),
    3662: _profile(
        3662,
        issue=3652,
        profile_id="jupyterlab-3662-file-editor-dirty-title-v2",
        production_paths=("packages/fileeditor/src/widget.ts",),
        patch_test_paths=("tests/test-fileeditor/src/widget.spec.ts",),
        selected_test_path="tests/test-fileeditor/src/widget.spec.ts",
        selected_test_marker="should add the dirty class when the model is dirty",
        f2p_selector=(
            "fileeditorcodewrapper FileEditor #constructor() "
            "should add the dirty class when the model is dirty"
        ),
        p2p_selector=(
            "fileeditorcodewrapper FileEditor #constructor() "
            "should update the editor text when the model changes"
        ),
        package_version="0.1.2",
        package_metadata_path="tests/test-fileeditor/package.json",
        test_harness_root="tests",
        karma_config_path="tests/karma.conf.js",
        webpack_config_path="tests/webpack.config.js",
        runtime_dependency_id="jupyterlab-node5-karma1-webpack2-typescript2.6.2",
        runtime_root=(
            "private/dependencies/jupyterlab-3662/"
            "node5-karma1-webpack2-typescript2.6.2/1.0"
        ),
        tier="replacement_candidate",
    ),
    3663: _profile(
        3663,
        issue=3653,
        profile_id="jupyterlab-3663-console-banner-lifecycle-v2",
        production_paths=("packages/console/src/widget.ts",),
        patch_test_paths=("tests/test-console/src/widget.spec.ts",),
        selected_test_path="tests/test-console/src/widget.spec.ts",
        selected_test_marker="expect(serialized).to.have.length(1);",
        f2p_selector=(
            "console/widget CodeConsole #serialize() "
            "should serialize the contents of a console"
        ),
        p2p_selector=(
            "console/widget CodeConsole #insertLinebreak() "
            "should insert a line break into the prompt"
        ),
        package_version="0.1.5",
        package_metadata_path="tests/test-console/package.json",
        test_harness_root="tests",
        karma_config_path="tests/karma.conf.js",
        webpack_config_path="tests/webpack.config.js",
        runtime_dependency_id=("jupyterlab-node5-karma1.7.1-webpack2-typescript2.6.2"),
        runtime_root=(
            "private/dependencies/jupyterlab-3663/"
            "node5-karma1.7.1-webpack2-typescript2.6.2/1.0"
        ),
        tier="replacement_candidate",
    ),
    574: _profile(
        574,
        issue=564,
        profile_id="jupyterlab-574-paste-below-v2",
        production_paths=("src/notebook/notebook/actions.ts",),
        patch_test_paths=("test/src/notebook/notebook/actions.spec.ts",),
        selected_test_path="test/src/notebook/notebook/actions.spec.ts",
        selected_test_marker="expect(widget.activeCellIndex).to.be(3);",
        f2p_selector=(
            "notebook/notebook/actions NotebookActions #paste() "
            "should paste cells from a clipboard"
        ),
        p2p_selector=(
            "notebook/notebook/actions NotebookActions #paste() "
            "should be a no-op if there is no model"
        ),
        package_version="0.1.3",
        package_metadata_path="package.json",
        test_harness_root="test",
        karma_config_path="test/karma.conf.js",
        webpack_config_path="test/webpack.conf.js",
        runtime_dependency_id="jupyterlab-node5-karma-mocha-typescript-1.8.10",
        runtime_root=(
            "private/dependencies/jupyterlab-574/"
            "node5-karma-mocha-typescript-1.8.10/1.0"
        ),
        tier="replacement_candidate",
    ),
    285: _profile(
        285,
        issue=266,
        profile_id="jupyterlab-285-kernel-startup-selection-v1",
        production_paths=(
            "src/notebook/notebook/panel.ts",
            "src/notebook/notebook/widgetfactory.ts",
        ),
        patch_test_paths=("test/src/notebook/notebook/panel.spec.ts",),
        selected_test_path="test/src/notebook/notebook/panel.spec.ts",
        selected_test_marker="panel.context.changeKernel({ name: 'shell' });",
        f2p_selector=(
            "notebook/notebook/panel NotebookPanel #kernel "
            "should be the current kernel used by the panel"
        ),
        p2p_selector=(
            "notebook/notebook/panel NotebookPanel #kernel "
            "should be read-only"
        ),
        package_version="0.0.10",
        package_metadata_path="package.json",
        test_harness_root="test",
        karma_config_path="test/karma.conf.js",
        webpack_config_path="test/webpack.conf.js",
        runtime_dependency_id=(
            "jupyterlab-285-node5-karma0.13-webpack1-typescript1.8.0"
        ),
        runtime_root=(
            "private/dependencies/jupyterlab-285/"
            "node5-karma0.13-webpack1-typescript1.8.0/1.0"
        ),
        tier="replacement_candidate",
    ),
    738: _profile(
        738,
        issue=652,
        profile_id="jupyterlab-738-empty-notebook-cell-race-v1",
        production_paths=("src/notebook/notebook/model.ts",),
        patch_test_paths=(
            "test/src/notebook/notebook/actions.spec.ts",
            "test/src/notebook/notebook/model.spec.ts",
            "test/src/notebook/notebook/widget.spec.ts",
        ),
        selected_test_path="test/src/notebook/notebook/model.spec.ts",
        selected_test_marker=(
            "it('should add a new code cell when cells are cleared', (done) => {"
        ),
        f2p_selector=(
            "notebook/notebook/model NotebookModel #constructor() "
            "should add a new code cell when cells are cleared"
        ),
        p2p_selector=(
            "notebook/notebook/model NotebookModel #constructor() "
            "should create a notebook model"
        ),
        package_version="0.2.0",
        package_metadata_path="package.json",
        test_harness_root="test",
        karma_config_path="test/karma.conf.js",
        webpack_config_path="test/webpack.conf.js",
        runtime_dependency_id=(
            "jupyterlab-738-node5-karma0.13-webpack1-typescript1.8.0"
        ),
        runtime_root=(
            "private/dependencies/jupyterlab-738/"
            "node5-karma0.13-webpack1-typescript1.8.0/1.0"
        ),
        tier="replacement_candidate",
    ),
    2469: _profile(
        2469,
        issue=2444,
        profile_id="jupyterlab-2469-vdom-null-model-v1",
        production_paths=("packages/apputils/src/vdom.ts",),
        patch_test_paths=("test/src/apputils/vdom.spec.ts",),
        selected_test_path="test/src/apputils/vdom.spec.ts",
        selected_test_marker="should work with a null model",
        f2p_selector=(
            "@jupyterlab/domutils VDomRenderer #noModel() should work with a null model"
        ),
        p2p_selector=(
            "@jupyterlab/domutils VDomRenderer #render() "
            "should render the contents after a model change"
        ),
        package_version="0.7.0",
        package_metadata_path="test/package.json",
        test_harness_root="test",
        karma_config_path="test/karma.conf.js",
        webpack_config_path="test/webpack.config.js",
        runtime_dependency_id="jupyterlab-node5-karma1.7-webpack2-typescript2.2.1",
        runtime_root=(
            "private/dependencies/jupyterlab-2469/"
            "node5-karma1.7-webpack2-typescript2.2.1/1.0"
        ),
        tier="replacement_candidate",
    ),
}


@dataclass(frozen=True)
class TopCandidateChain:
    profile: JupyterLabTopProfile
    source: StableArtifact
    patch: StableArtifact
    atomicity: StableArtifact
    manifest: StableArtifact
    raw_patch: bytes
    source_manifest: Mapping[str, Any]
    patch_paths: tuple[str, ...]


@dataclass(frozen=True)
class TopPreparation:
    profile: JupyterLabTopProfile
    chain: TopCandidateChain
    inverse_patch: bytes
    api_compatibility_status: str
    capsule_status: str
    runtime_status: str
    downstream_status: str


def profile_for_top_candidate(candidate: CandidateKey) -> JupyterLabTopProfile:
    if type(candidate) is not CandidateKey or candidate.repository_key != "jupyterlab":
        raise ValueError("JupyterLab top candidate is invalid")
    try:
        return _PROFILES[candidate.change_number]
    except KeyError as error:
        raise ValueError(
            "candidate has no registered JupyterLab top profile"
        ) from error


def profile_for_replacement_candidate(candidate: CandidateKey) -> JupyterLabTopProfile:
    """Return a gate-1--4-qualified replacement, not a frozen roster entry."""

    profile = profile_for_top_candidate(candidate)
    if profile.tier != "replacement_candidate":
        raise ValueError("candidate is not in the JupyterLab replacement pool")
    return profile


def top_profiles() -> tuple[JupyterLabTopProfile, ...]:
    return tuple(
        _PROFILES[number]
        for number in sorted(_PROFILES)
        if _PROFILES[number].tier == "at_goal_candidate"
    )


def replacement_profiles() -> tuple[JupyterLabTopProfile, ...]:
    return tuple(
        _PROFILES[number]
        for number in sorted(_PROFILES)
        if _PROFILES[number].tier == "replacement_candidate"
    )


def pending_profiles() -> tuple[JupyterLabTopProfile, ...]:
    return tuple(
        _PROFILES[number]
        for number in sorted(_PROFILES)
        if _PROFILES[number].tier == "source_closure_pending"
    )


def _reference(number: int, stage: str) -> ArtifactReference:
    values = {
        "source": (
            f"candidate_source_packet/jupyterlab/{number}/source/v2",
            "candidate_source_packet",
        ),
        "patch": (
            f"candidate_test_patch/jupyterlab/{number}/screening/v2",
            "candidate_test_patch",
        ),
        "atomicity": (
            f"source_atomicity/jupyterlab/{number}/preannotation/v2",
            "source_atomicity",
        ),
        "manifest": (
            f"candidate_stage/jupyterlab/{number}/test_manifest/v2",
            "candidate_stage",
        ),
        "capsule": (
            f"fixed_source_capsule/jupyterlab/{number}/fixed/v2",
            "fixed_source_capsule",
        ),
    }
    artifact_id, artifact_kind = values[stage]
    return ArtifactReference(artifact_id, artifact_kind, "2.0")


def _regular_file(root: Path, relative: str, label: str) -> Path:
    path = root.joinpath(*_safe_relative(relative, label).parts)
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise RuntimeError(f"{label} is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"{label} is unsafe")
    return path


def _validate_attachment_closure(
    root: Path, source_manifest: Mapping[str, Any]
) -> None:
    """Require captured image/archive evidence when authority references it."""

    attachments = source_manifest.get("attachment_paths", [])
    raw_paths = source_manifest.get("raw_paths", [])
    if type(attachments) is not list or any(
        type(value) is not str for value in attachments
    ):
        raise RuntimeError("JupyterLab top attachment inventory is invalid")
    if type(raw_paths) is not list or any(
        type(value) is not str for value in raw_paths
    ):
        raise RuntimeError("JupyterLab top raw path inventory is invalid")
    for relative in attachments:
        _regular_file(root, relative, "source attachment")
    referenced: set[str] = set()
    for relative in raw_paths:
        raw = _regular_file(root, relative, "source raw response")
        text = raw.read_text(encoding="utf-8", errors="surrogateescape")
        referenced.update(
            url.rstrip(".,\\'\\\"")
            for url in re.findall(r"https?://[^\s\"<>\)]+", text)
            if any(
                host in url
                for host in (
                    "cloud.githubusercontent.com",
                    "user-images.githubusercontent.com",
                    "postimg.cc",
                    "imgur.com",
                )
            )
        )
    if referenced and not attachments:
        raise RuntimeError(
            "JupyterLab top authority attachment closure is incomplete: "
            + ", ".join(sorted(referenced))
        )


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _parse_patch_sections(content: bytes) -> tuple[tuple[str, bytes], ...]:
    if not content:
        raise RuntimeError("JupyterLab top raw patch is empty")
    sections: list[tuple[str, bytes]] = []
    current_path: str | None = None
    current: list[str] = []
    for line in content.decode("utf-8", errors="surrogateescape").splitlines(
        keepends=True
    ):
        if line.startswith("diff --git "):
            if current_path is not None:
                sections.append(
                    (current_path, "".join(current).encode("utf-8", "surrogateescape"))
                )
            try:
                parts = shlex.split(line.rstrip("\r\n"))
            except ValueError as error:
                raise RuntimeError(
                    "JupyterLab top raw patch header is invalid"
                ) from error
            if (
                len(parts) != 4
                or parts[:2] != ["diff", "--git"]
                or not parts[2].startswith("a/")
                or not parts[3].startswith("b/")
                or parts[2][2:] != parts[3][2:]
            ):
                raise RuntimeError("JupyterLab top raw patch header is invalid")
            current_path = parts[2][2:]
            _safe_relative(current_path, "JupyterLab top raw patch path")
            current = [line]
        elif current_path is not None:
            current.append(line)
    if current_path is not None:
        sections.append(
            (current_path, "".join(current).encode("utf-8", "surrogateescape"))
        )
    if not sections or len({path for path, _ in sections}) != len(sections):
        raise RuntimeError("JupyterLab top raw patch has duplicate or missing sections")
    return tuple(sections)


def load_top_candidate_chain(
    artifact_root: str | Path, candidate: CandidateKey
) -> TopCandidateChain:
    """Reload the current typed 1--4 chain and prove its source closure."""

    root = Path(artifact_root).absolute()
    profile = profile_for_top_candidate(candidate)
    source, patch, atomicity, manifest = (
        _load(root, reference)
        for reference in (
            profile.source_reference,
            profile.patch_reference,
            profile.atomicity_reference,
            profile.manifest_reference,
        )
    )
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != candidate.to_value()
        or source.payload.get("complete") is not True
        or type(source.payload.get("raw_evidence_path")) is not str
    ):
        raise RuntimeError("JupyterLab top source packet is invalid")
    source_manifest_path = source.payload["raw_evidence_path"]
    source_manifest_path = _safe_relative(
        source_manifest_path, "JupyterLab top source manifest"
    ).as_posix()
    source_manifest_path_obj = _regular_file(
        root, source_manifest_path, "source manifest"
    )
    try:
        source_manifest = json.loads(
            source_manifest_path_obj.read_text(encoding="utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("JupyterLab top source manifest is invalid") from error
    if (
        type(source_manifest) is not dict
        or source_manifest.get("candidate") != candidate.to_value()
        or source_manifest.get("complete") is not True
        or source_manifest.get("provider") != "github"
        or source_manifest.get("issue_numbers") != [profile.primary_issue_number]
    ):
        raise RuntimeError("JupyterLab top authority closure is incomplete")
    authority = source_manifest.get("authority")
    if (
        type(authority) is not dict
        or authority.get("provider") != "github"
        or authority.get("repository") != "jupyterlab/jupyterlab"
    ):
        raise RuntimeError("JupyterLab top authority identity is invalid")
    pull = source_manifest.get("pull")
    if type(pull) is not dict or pull.get("number") != candidate.change_number:
        raise RuntimeError("JupyterLab top pull source is invalid")
    if pull.get("merged") is not True:
        raise RuntimeError("JupyterLab top pull is not merged")
    _validate_attachment_closure(root, source_manifest)
    if (
        patch.inputs != (_ENUMERATION, source.reference)
        or patch.payload.get("candidate") != candidate.to_value()
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("production_paths") != list(profile.production_paths)
        or patch.payload.get("test_paths") != list(profile.patch_test_paths)
        or patch.payload.get("source_manifest_path")
        != source.payload["raw_evidence_path"]
        or type(patch.payload.get("raw_patch_path")) is not str
    ):
        raise RuntimeError("JupyterLab top patch artifact is invalid")
    raw_patch_path = _safe_relative(
        patch.payload["raw_patch_path"], "JupyterLab top raw patch"
    ).as_posix()
    raw_patch = _regular_file(root, raw_patch_path, "raw patch").read_bytes()
    if source_manifest.get("patch_raw_path") != patch.payload["raw_patch_path"]:
        raise RuntimeError("JupyterLab top source/patch path binding is invalid")
    if (
        atomicity.inputs
        != (
            _ENUMERATION,
            source.reference,
            patch.reference,
            ArtifactReference(
                f"screening_checkpoint/jupyterlab/{candidate.change_number}/current/v2",
                "screening_checkpoint",
                "2.0",
            ),
        )
        or atomicity.payload.get("candidate") != candidate.to_value()
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("primary_issue_number") != profile.primary_issue_number
        or atomicity.payload.get("authoritative_issue_numbers")
        != [profile.primary_issue_number]
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("source_manifest_path")
        != source.payload["raw_evidence_path"]
    ):
        raise RuntimeError("JupyterLab top source atomicity is invalid")
    details = manifest.payload.get("details")
    if (
        manifest.inputs != (patch.reference, atomicity.reference)
        or manifest.payload.get("candidate") != candidate.to_value()
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("outcome") is not None
        or type(details) is not dict
        or details.get("executable_test_paths") != list(profile.patch_test_paths)
        or details.get("test_count") != len(profile.patch_test_paths)
    ):
        raise RuntimeError("JupyterLab top test manifest is invalid")
    sections = _parse_patch_sections(raw_patch)
    expected = profile.production_paths + profile.patch_test_paths
    paths = tuple(path for path, _ in sections)
    if set(paths) != set(expected) or len(paths) != len(expected):
        raise RuntimeError("JupyterLab top patch path partition is invalid")
    selected = dict(sections)[profile.selected_test_path]
    if profile.selected_test_marker.encode("utf-8") not in selected:
        raise RuntimeError("JupyterLab top selected regression test is unbound")
    return TopCandidateChain(
        profile=profile,
        source=source,
        patch=patch,
        atomicity=atomicity,
        manifest=manifest,
        raw_patch=raw_patch,
        source_manifest=source_manifest,
        patch_paths=paths,
    )


def _production_inverse(chain: TopCandidateChain) -> bytes:
    by_path = dict(_parse_patch_sections(chain.raw_patch))
    return b"".join(by_path[path] for path in chain.profile.production_paths)


def prepare_top_candidate(
    artifact_root: str | Path, candidate: CandidateKey
) -> TopPreparation:
    """Build an offline preparation descriptor; no candidate code is run."""

    root = Path(artifact_root).absolute()
    chain = load_top_candidate_chain(root, candidate)
    inverse = _production_inverse(chain)
    for path in chain.profile.patch_test_paths:
        if path.encode("utf-8") in inverse:
            raise RuntimeError("JupyterLab top inverse patch touches a test")
    capsule_dir = root.joinpath(
        *_safe_relative(chain.profile.fixed_source_tree_path, "fixed source tree").parts
    )
    capsule_status = (
        "available"
        if capsule_dir.is_dir() and not capsule_dir.is_symlink()
        else "pending"
    )
    runtime_status = (
        "registered" if (root / chain.profile.runtime_root).is_dir() else "pending"
    )
    return TopPreparation(
        profile=chain.profile,
        chain=chain,
        inverse_patch=inverse,
        api_compatibility_status="passed_static",
        capsule_status=capsule_status,
        runtime_status=runtime_status,
        downstream_status="downstream_pending",
    )


def _created_after(*values: str) -> str:
    parsed = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values]
    value = max(parsed).astimezone(timezone.utc).isoformat(timespec="microseconds")
    return value.replace("+00:00", "Z")


def _authorization_value(authorization: AuthorizationRecord) -> dict[str, object]:
    return {
        "authorization_id": authorization.authorization_id,
        "scope": authorization.scope,
        "granted_at": authorization.granted_at,
        "local_user_id": authorization.local_user_id,
        "revoked": authorization.revoked,
    }


def write_top_profile(
    artifact_root: str | Path,
    candidate: CandidateKey,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist the source/patch-qualified profile and typed-reload it."""

    if authorization.scope != "candidate-screening":
        raise PermissionError("JupyterLab top profile authorization scope is invalid")
    prep = prepare_top_candidate(artifact_root, candidate)
    profile = prep.profile
    payload: dict[str, object] = {
        "candidate": candidate.to_value(),
        "profile_id": profile.profile_id,
        "tier": profile.tier,
        "source_patch_atomicity_manifest_reloaded": True,
        "authority": _AUTHORITY,
        "primary_issue_number": profile.primary_issue_number,
        "production_paths": list(profile.production_paths),
        "test_paths": list(profile.patch_test_paths),
        "selected_test_path": profile.selected_test_path,
        "selected_test_marker": profile.selected_test_marker,
        "reverse_patch_argv": ["git", "apply", "-R", "--whitespace=nowarn", "-"],
        "strict_gate_status": {
            "source_closure": "passed",
            "single_defect_mapping": "passed",
            "production_only_patch": "passed",
            "production_inverse_static": "passed",
            "fixed_test_api_compatibility": prep.api_compatibility_status,
            "fixed_f2p_3x": "downstream_pending",
            "buggy_f2p_3x": "downstream_pending",
            "fixed_buggy_p2p_3x": "downstream_pending",
        },
        "fixed_capsule_reference": profile.capsule_reference.artifact_id,
        "fixed_capsule_status": prep.capsule_status,
        "runtime_dependency_id": profile.runtime_dependency_id,
        "runtime_root": profile.runtime_root,
        "runtime_status": prep.runtime_status,
        "downstream_status": prep.downstream_status,
        "paid_actions_authorized": False,
        "candidate_execution_performed": False,
    }
    created_at = _created_after(
        prep.chain.source.created_at,
        prep.chain.patch.created_at,
        prep.chain.atomicity.created_at,
        prep.chain.manifest.created_at,
        authorization.granted_at,
    )
    write_stable_artifact(
        artifact_root,
        artifact_id=profile.profile_reference.artifact_id,
        artifact_kind=profile.profile_reference.artifact_kind,
        semantic_version=profile.profile_reference.semantic_version,
        created_at=created_at,
        inputs=(
            prep.chain.source.reference,
            prep.chain.patch.reference,
            prep.chain.atomicity.reference,
            prep.chain.manifest.reference,
        ),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(Path(artifact_root).absolute(), profile.profile_reference)
    if artifact.payload != payload:
        raise RuntimeError("JupyterLab top profile typed reload mismatch")
    return artifact


def write_top_preparation(
    artifact_root: str | Path,
    candidate: CandidateKey,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist a downstream-pending preparation receipt."""

    if authorization.scope != "candidate-screening":
        raise PermissionError(
            "JupyterLab top preparation authorization scope is invalid"
        )
    prep = prepare_top_candidate(artifact_root, candidate)
    profile = prep.profile
    profile_artifact = _load(Path(artifact_root).absolute(), profile.profile_reference)
    payload: dict[str, object] = {
        "candidate": candidate.to_value(),
        "profile_id": profile.profile_id,
        "state": "prepared",
        "outcome": "downstream_pending",
        "profile_reference": profile.profile_reference.artifact_id,
        "fixed_capsule_reference": profile.capsule_reference.artifact_id,
        "fixed_capsule_status": prep.capsule_status,
        "fixed_source_tree_path": profile.fixed_source_tree_path,
        "production_inverse_paths": list(profile.production_paths),
        "preserved_test_paths": list(profile.patch_test_paths),
        "fixed_test_api_compatibility": prep.api_compatibility_status,
        "reverse_patch_argv": ["git", "apply", "-R", "--whitespace=nowarn", "-"],
        "f2p": {
            "selector": profile.f2p_selector,
            "repetitions": 3,
            "fixed_expected": "pass",
            "buggy_expected": "failure",
        },
        "p2p": {
            "selector": profile.p2p_selector,
            "repetitions": 3,
            "fixed_expected": "pass",
            "buggy_expected": "pass",
        },
        "runtime_status": prep.runtime_status,
        "downstream_status": prep.downstream_status,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    created_at = _created_after(profile_artifact.created_at, authorization.granted_at)
    write_stable_artifact(
        artifact_root,
        artifact_id=profile.preparation_reference.artifact_id,
        artifact_kind=profile.preparation_reference.artifact_kind,
        semantic_version=profile.preparation_reference.semantic_version,
        created_at=created_at,
        inputs=(profile_artifact.reference,),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(Path(artifact_root).absolute(), profile.preparation_reference)
    if artifact.payload != payload:
        raise RuntimeError("JupyterLab top preparation typed reload mismatch")
    return artifact


def write_top_runtime_pending(
    artifact_root: str | Path,
    candidate: CandidateKey,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist an exact offline runtime gap without rejecting the candidate."""

    if authorization.scope != "infrastructure-audit":
        raise PermissionError("JupyterLab top runtime authorization scope is invalid")
    root = Path(artifact_root).absolute()
    prep = prepare_top_candidate(root, candidate)
    profile = prep.profile
    if candidate.change_number != 3867:
        raise ValueError("only JupyterLab 3867 has this runtime preflight")
    capsule = _load(root, profile.capsule_reference)
    fixed = root / profile.fixed_source_tree_path
    tests_package = _regular_file(fixed, "tests/package.json", "tests package")
    core_package = _regular_file(
        fixed, profile.package_metadata_path, "selected test package"
    )
    travis = _regular_file(fixed, ".travis.yml", "historical CI declaration")
    yarn = _regular_file(fixed, "jupyterlab/staging/yarn.js", "captured Yarn bundle")
    try:
        tests_value = json.loads(tests_package.read_text(encoding="utf-8"))
        core_value = json.loads(core_package.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("JupyterLab 3867 runtime metadata is invalid") from error
    dev = tests_value.get("devDependencies")
    expected_packages = {
        "karma": "~1.7.1",
        "mocha": "~3.5.3",
        "karma-mocha": "~0.2.2",
        "webpack": "~2.7.0",
        "typescript": "~2.6.2",
    }
    if (
        type(dev) is not dict
        or tests_value.get("version") != profile.package_version
        or core_value.get("version") != "0.1.5"
        or {name: dev.get(name) for name in expected_packages} != expected_packages
    ):
        raise RuntimeError("JupyterLab 3867 runtime metadata is invalid")
    travis_text = travis.read_text(encoding="utf-8")
    yarn_text = yarn.read_text(encoding="utf-8", errors="surrogateescape")
    if (
        "node_js:\n- '5'" not in travis_text
        or "firefox: latest" not in travis_text
        or '"version": "1.3.2"' not in yarn_text
    ):
        raise RuntimeError("JupyterLab 3867 runtime declaration is invalid")

    reference = ArtifactReference(
        f"dependency_preparation/{profile.runtime_dependency_id}/current/v1",
        "dependency_preparation",
        "1.0",
    )
    payload: dict[str, object] = {
        "complete": True,
        "outcome": "pending",
        "reason": "historical_runtime_dependency_pending",
        "candidate_lifecycle": "active",
        "terminal": False,
        "dependency_id": profile.runtime_dependency_id,
        "runtime_root": profile.runtime_root,
        "upstream_constraints": {
            "node": "5",
            "firefox": "latest_at_historical_ci_time",
        },
        "captured_components": {
            "yarn": "1.3.2",
            "yarn_path": "jupyterlab/staging/yarn.js",
        },
        "required_versions": {
            "karma": "1.7.1",
            "mocha": "3.5.3",
            "karma_mocha": "0.2.2",
            "webpack": "2.7.0",
            "typescript": "2.6.2",
            "yarn": "1.3.2",
        },
        "observed_versions": {
            "git": "2.50.1",
            "node": "22.22.0",
            "npm": "10.9.4",
        },
        "missing_components": [
            "node_5_compatible_binary",
            "matching_npm_closure",
            "karma_1.7.1_closure",
            "mocha_3.5.3_closure",
            "karma_mocha_0.2.2_closure",
            "webpack_2.7.0_closure",
            "typescript_2.6.2_closure",
            "historical_firefox_latest_resolution_and_binary",
        ],
        "mismatched_components": [
            {"component": "node", "expected": "5", "observed": "22.22.0"}
        ],
        "offline_runtime_buildable": False,
        "candidate_execution_performed": False,
        "next_action": "supply_a_registered_historical_runtime_closure",
    }
    created_at = _created_after(
        prep.chain.manifest.created_at,
        capsule.created_at,
        authorization.granted_at,
    )
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=created_at,
        inputs=(profile.preparation_reference, capsule.reference),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, reference)
    if artifact.payload != payload:
        raise RuntimeError("JupyterLab top runtime preflight typed reload mismatch")
    return artifact


def _validated_archive(content: bytes) -> tuple[tarfile.TarInfo, ...]:
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            members = tuple(archive.getmembers())
    except tarfile.TarError as error:
        raise RuntimeError("JupyterLab top source archive is invalid") from error
    names: set[str] = set()
    for member in members:
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or "." in path.parts
            or ".." in path.parts
            or path.as_posix().rstrip("/") != member.name.rstrip("/")
            or member.name in names
            or not (member.isdir() or member.isfile())
        ):
            raise RuntimeError("JupyterLab top source archive is unsafe")
        names.add(member.name)
    if not members:
        raise RuntimeError("JupyterLab top source archive is empty")
    return members


def _extract_archive(parent: Path, destination: Path, content: bytes) -> None:
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".jupyterlab-top-", dir=parent) as name:
        staging = Path(name)
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            for member in archive.getmembers():
                if member.isdir():
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError("JupyterLab top source archive is invalid")
                target = staging.joinpath(*PurePosixPath(member.name).parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o700 if member.mode & 0o111 else 0o600,
                )
                with os.fdopen(descriptor, "wb") as output:
                    while block := source.read(1024 * 1024):
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("JupyterLab top fixed capsule destination exists")
        os.replace(staging, destination)


def materialize_top_fixed_capsule(
    artifact_root: str | Path,
    candidate: CandidateKey,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Materialize only the retained fixed source tree; never execute it."""

    if authorization.scope != "protocol-migration":
        raise PermissionError("JupyterLab top capsule authorization scope is invalid")
    root = Path(artifact_root).absolute()
    prep = prepare_top_candidate(root, candidate)
    profile = prep.profile
    destination = root.joinpath(
        *_safe_relative(profile.fixed_source_tree_path, "fixed source tree").parts
    )
    if not destination.exists():
        mirror = root.joinpath(*_MIRROR.parts)
        info = mirror.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("JupyterLab top retained mirror is unsafe")
        completed = subprocess.run(
            (
                "git",
                f"--git-dir={mirror}",
                "archive",
                "--format=tar",
                f"refs/c1/pull/{candidate.change_number}/head",
            ),
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0 or not completed.stdout:
            raise RuntimeError("JupyterLab top retained fixed revision is unavailable")
        members = _validated_archive(completed.stdout)
        names = {member.name for member in members if member.isfile()}
        required = {
            profile.package_metadata_path,
            profile.karma_config_path,
            profile.webpack_config_path,
            *profile.production_paths,
            *profile.patch_test_paths,
        }
        if not required.issubset(names):
            missing = ", ".join(sorted(required - names))
            raise RuntimeError(f"JupyterLab top fixed source is incomplete: {missing}")
        _extract_archive(destination.parent, destination, completed.stdout)
    else:
        info = destination.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("JupyterLab top fixed capsule is unsafe")
        for relative in (
            profile.package_metadata_path,
            profile.karma_config_path,
            profile.webpack_config_path,
            *profile.production_paths,
            *profile.patch_test_paths,
        ):
            _regular_file(destination, relative, "fixed capsule source")
    payload = {
        "candidate": candidate.to_value(),
        "profile_id": profile.profile_id,
        "source_origin": "captured_merged_revision",
        "source_tree_path": profile.fixed_source_tree_path,
        "production_paths": list(profile.production_paths),
        "test_paths": list(profile.patch_test_paths),
    }
    created_at = _created_after(
        prep.chain.source.created_at,
        prep.chain.patch.created_at,
        prep.chain.atomicity.created_at,
        prep.chain.manifest.created_at,
        authorization.granted_at,
    )
    write_stable_artifact(
        root,
        artifact_id=profile.capsule_reference.artifact_id,
        artifact_kind=profile.capsule_reference.artifact_kind,
        semantic_version=profile.capsule_reference.semantic_version,
        created_at=created_at,
        inputs=(
            prep.chain.source.reference,
            prep.chain.patch.reference,
            prep.chain.atomicity.reference,
            prep.chain.manifest.reference,
        ),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, profile.capsule_reference)
    if artifact.payload != payload:
        raise RuntimeError("JupyterLab top fixed capsule typed reload mismatch")
    return artifact


__all__ = [
    "JupyterLabTopProfile",
    "TopCandidateChain",
    "TopPreparation",
    "load_top_candidate_chain",
    "materialize_top_fixed_capsule",
    "prepare_top_candidate",
    "profile_for_replacement_candidate",
    "profile_for_top_candidate",
    "pending_profiles",
    "replacement_profiles",
    "top_profiles",
    "write_top_preparation",
    "write_top_profile",
]
