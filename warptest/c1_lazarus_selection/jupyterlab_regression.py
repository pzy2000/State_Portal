"""Closed execution profiles for selected historical JupyterLab regressions.

This module is preparation-only.  It validates the persisted v2 screening
chain, binds a fixed tree captured at the merged revision, selects one
issue-specific Mocha test plus one preservation test, and requires a fully
registered Node/Karma runtime.  It never installs packages, consults PATH,
starts a browser, builds a tree, or executes a candidate.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Mapping

from .models import CandidateKey
from .pipeline import StageDecision
from .stable_protocol import ArtifactReference, StableArtifact, load_stable_artifact


_ENUMERATION = ArtifactReference(
    "repository_enumeration/jupyterlab/current/v2",
    "repository_enumeration",
    "2.0",
)
_BASE_RUNTIME_COMMANDS = {
    "git": "bin/git",
    "node": "bin/node",
    "npm": "bin/npm",
    "karma": "node_modules/karma/bin/karma",
    "webpack": "node_modules/webpack/bin/webpack.js",
    "typescript": "node_modules/typescript/bin/tsc",
    "firefox": "bin/firefox",
}
_RUNTIME_PACKAGE_METADATA = {
    "npm": "node_modules/npm/package.json",
    "karma": "node_modules/karma/package.json",
    "mocha": "node_modules/mocha/package.json",
    "karma_mocha": "node_modules/karma-mocha/package.json",
    "webpack": "node_modules/webpack/package.json",
    "typescript": "node_modules/typescript/package.json",
}
_COMMON_VERSIONS = {
    "git": "2.51.0",
    "node": "5.12.0",
    "npm": "3.8.6",
    "karma": "0.13.22",
    "mocha": "2.5.3",
    "karma_mocha": "0.2.2",
    "webpack": "1.15.0",
    "firefox": "48.0",
}


def _safe_relative(value: object, label: str) -> PurePosixPath:
    if type(value) is not str or not value:
        raise ValueError(f"{label} is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or "." in path.parts
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise ValueError(f"{label} is unsafe")
    return path


@dataclass(frozen=True)
class JupyterLabRegressionProfile:
    candidate: CandidateKey
    primary_issue_number: int
    linked_issue_numbers: tuple[int, ...]
    profile_id: str
    source_packet_reference: ArtifactReference
    patch_reference: ArtifactReference
    atomicity_reference: ArtifactReference
    test_manifest_reference: ArtifactReference
    fixed_capsule_reference: ArtifactReference
    fixed_source_tree_path: str
    production_paths: tuple[str, ...]
    patch_test_paths: tuple[str, ...]
    selected_test_path: str
    patch_test_marker: str
    fixed_test_marker: str
    mocha_suite_prefix: str
    f2p_selector: str
    p2p_selector: str
    package_version: str
    package_metadata_path: str
    test_harness_root: str
    build_script: str
    runtime_dependency_id: str
    runtime_root: str
    required_versions: Mapping[str, str]
    additional_f2p_selectors: tuple[str, ...] = ()
    additional_patch_test_markers: Mapping[str, str] | None = None
    declared_tool_constraints: Mapping[str, str] | None = None
    authority_attachment_closure_required: bool = False
    single_defect_contract_proven: bool = True
    captured_merged_revision_required: bool = False
    package_manager: Literal["npm", "jlpm"] = "npm"
    test_framework: Literal["karma-mocha"] = "karma-mocha"
    yarn_version: str | None = None
    jest_version: None = None
    repetitions: int = 3

    def __post_init__(self) -> None:
        number = self.candidate.change_number
        expected = (
            ArtifactReference(
                f"candidate_source_packet/jupyterlab/{number}/source/v2",
                "candidate_source_packet",
                "2.0",
            ),
            ArtifactReference(
                f"candidate_test_patch/jupyterlab/{number}/screening/v2",
                "candidate_test_patch",
                "2.0",
            ),
            ArtifactReference(
                f"source_atomicity/jupyterlab/{number}/preannotation/v2",
                "source_atomicity",
                "2.0",
            ),
            ArtifactReference(
                f"candidate_stage/jupyterlab/{number}/test_manifest/v2",
                "candidate_stage",
                "2.0",
            ),
            ArtifactReference(
                f"fixed_source_capsule/jupyterlab/{number}/fixed/v2",
                "fixed_source_capsule",
                "2.0",
            ),
        )
        actual = (
            self.source_packet_reference,
            self.patch_reference,
            self.atomicity_reference,
            self.test_manifest_reference,
            self.fixed_capsule_reference,
        )
        if (
            self.candidate.repository_key != "jupyterlab"
            or self.primary_issue_number < 1
            or actual != expected
            or self.repetitions != 3
            or not self.profile_id
            or not self.build_script
            or not self.production_paths
            or not self.patch_test_paths
            or self.selected_test_path not in self.patch_test_paths
            or len(set(self.production_paths + self.patch_test_paths))
            != len(self.production_paths + self.patch_test_paths)
            or not self.f2p_selector.startswith(self.mocha_suite_prefix)
            or not self.p2p_selector.startswith(self.mocha_suite_prefix)
            or self.f2p_selector == self.p2p_selector
            or any(not selector for selector in self.additional_f2p_selectors)
            or self.f2p_selector in self.additional_f2p_selectors
            or type(self.authority_attachment_closure_required) is not bool
            or type(self.single_defect_contract_proven) is not bool
            or type(self.captured_merged_revision_required) is not bool
            or self.primary_issue_number not in self.linked_issue_numbers
            or len(set(self.linked_issue_numbers)) != len(self.linked_issue_numbers)
            or any(number < 1 for number in self.linked_issue_numbers)
            or set(self.required_versions)
            != (
                set(_COMMON_VERSIONS)
                | {"typescript"}
                | ({"yarn", "jlpm"} if self.package_manager == "jlpm" else set())
            )
            or (self.package_manager == "npm" and self.yarn_version is not None)
            or (
                self.package_manager == "jlpm"
                and (
                    not self.yarn_version
                    or self.required_versions.get("yarn") != self.yarn_version
                    or self.required_versions.get("jlpm") != self.yarn_version
                )
            )
        ):
            raise ValueError("JupyterLab regression profile is invalid")
        additional_markers = dict(self.additional_patch_test_markers or {})
        if not set(additional_markers).issubset(self.patch_test_paths) or any(
            type(marker) is not str or not marker
            for marker in additional_markers.values()
        ):
            raise ValueError("JupyterLab regression profile is invalid")
        if self.declared_tool_constraints is not None and any(
            type(name) is not str or not name or type(value) is not str or not value
            for name, value in self.declared_tool_constraints.items()
        ):
            raise ValueError("JupyterLab regression profile is invalid")
        for value in (
            self.fixed_source_tree_path,
            self.package_metadata_path,
            self.test_harness_root,
            self.runtime_root,
            *self.production_paths,
            *self.patch_test_paths,
        ):
            _safe_relative(value, "JupyterLab profile path")

    @property
    def f2p_selectors(self) -> tuple[str, ...]:
        return (self.f2p_selector, *self.additional_f2p_selectors)

    @property
    def patch_test_markers(self) -> Mapping[str, str]:
        markers = {self.selected_test_path: self.patch_test_marker}
        markers.update(self.additional_patch_test_markers or {})
        return markers


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


def _profile(
    number: int,
    *,
    issue: int,
    profile_id: str,
    production_paths: tuple[str, ...],
    patch_test_paths: tuple[str, ...],
    selected_test_path: str,
    patch_test_marker: str,
    suite: str,
    f2p: str,
    p2p: str,
    package_version: str,
    typescript_version: str,
    package_metadata_path: str = "package.json",
    test_harness_root: str = "test",
    build_script: str = "build:test",
    runtime_versions: Mapping[str, str] | None = None,
    runtime_dependency_id: str | None = None,
    runtime_root: str | None = None,
    additional_f2p_selectors: tuple[str, ...] = (),
    additional_patch_test_markers: Mapping[str, str] | None = None,
    declared_tool_constraints: Mapping[str, str] | None = None,
    authority_attachment_closure_required: bool = False,
    single_defect_contract_proven: bool = True,
    captured_merged_revision_required: bool = False,
    linked_issue_numbers: tuple[int, ...] | None = None,
    package_manager: Literal["npm", "jlpm"] = "npm",
    yarn_version: str | None = None,
) -> JupyterLabRegressionProfile:
    required_versions = {
        **_COMMON_VERSIONS,
        **dict(runtime_versions or {}),
        "typescript": typescript_version,
    }
    if package_manager == "jlpm":
        if yarn_version is None:
            raise ValueError("jlpm profile requires an exact Yarn version")
        required_versions.update({"yarn": yarn_version, "jlpm": yarn_version})
    return JupyterLabRegressionProfile(
        candidate=CandidateKey("jupyterlab", number),
        primary_issue_number=issue,
        linked_issue_numbers=linked_issue_numbers or (issue,),
        profile_id=profile_id,
        source_packet_reference=_reference(number, "source"),
        patch_reference=_reference(number, "patch"),
        atomicity_reference=_reference(number, "atomicity"),
        test_manifest_reference=_reference(number, "manifest"),
        fixed_capsule_reference=_reference(number, "capsule"),
        fixed_source_tree_path=(
            f"private/fixed-capsules/jupyterlab/{number}/captured-merged-tree"
        ),
        production_paths=production_paths,
        patch_test_paths=patch_test_paths,
        selected_test_path=selected_test_path,
        patch_test_marker=patch_test_marker,
        fixed_test_marker=patch_test_marker,
        mocha_suite_prefix=suite,
        f2p_selector=f"{suite} {f2p}",
        p2p_selector=f"{suite} {p2p}",
        package_version=package_version,
        package_metadata_path=package_metadata_path,
        test_harness_root=test_harness_root,
        build_script=build_script,
        runtime_dependency_id=runtime_dependency_id
        or f"jupyterlab-node5-karma-mocha-typescript-{typescript_version}",
        runtime_root=runtime_root
        or (
            "private/dependencies/jupyterlab-node-karma/"
            f"typescript-{typescript_version}/1.0"
        ),
        required_versions=required_versions,
        additional_f2p_selectors=additional_f2p_selectors,
        additional_patch_test_markers=additional_patch_test_markers,
        declared_tool_constraints=declared_tool_constraints,
        authority_attachment_closure_required=authority_attachment_closure_required,
        single_defect_contract_proven=single_defect_contract_proven,
        captured_merged_revision_required=captured_merged_revision_required,
        package_manager=package_manager,
        yarn_version=yarn_version,
    )


_PROFILES = {
    444: _profile(
        444,
        issue=429,
        profile_id="jupyterlab-444-delete-select-next-v1",
        production_paths=(
            "src/notebook/notebook/actions.ts",
            "src/notebook/notebook/model.ts",
        ),
        patch_test_paths=(
            "test/src/common/activitymonitor.spec.ts",
            "test/src/notebook/notebook/actions.spec.ts",
            "test/src/notebook/notebook/model.spec.ts",
            "test/src/notebook/notebook/widget.spec.ts",
        ),
        selected_test_path="test/src/notebook/notebook/actions.spec.ts",
        patch_test_marker="should activate the cell after the last selected cell",
        suite="notebook/notebook/actions NotebookActions #deleteCells()",
        f2p="should activate the cell after the last selected cell",
        p2p="should delete the selected cells",
        package_version="0.1.1",
        typescript_version="1.8.10",
    ),
    574: _profile(
        574,
        issue=564,
        profile_id="jupyterlab-574-paste-below-v1",
        production_paths=("src/notebook/notebook/actions.ts",),
        patch_test_paths=("test/src/notebook/notebook/actions.spec.ts",),
        selected_test_path="test/src/notebook/notebook/actions.spec.ts",
        patch_test_marker="expect(widget.activeCellIndex).to.be(3);",
        suite="notebook/notebook/actions NotebookActions #paste()",
        f2p="should paste cells from a clipboard",
        p2p="should be a no-op if there is no model",
        package_version="0.1.3",
        typescript_version="1.8.10",
    ),
    611: _profile(
        611,
        issue=604,
        profile_id="jupyterlab-611-cancel-kernel-switch-v1",
        production_paths=(
            "src/docregistry/kernelselector.ts",
            "src/notebook/notebook/default-toolbar.ts",
        ),
        patch_test_paths=(
            "test/src/docmanager/mockcontext.ts",
            "test/src/notebook/notebook/default-toolbar.spec.ts",
        ),
        selected_test_path="test/src/notebook/notebook/default-toolbar.spec.ts",
        patch_test_marker="should handle a null kernel",
        suite=(
            "notebook/notebook/default-toolbar ToolbarItems #createKernelStatusItem()"
        ),
        f2p="should handle a null kernel",
        p2p="should handle a change to the kernel",
        package_version="0.1.3",
        typescript_version="1.8.10",
    ),
    1250: _profile(
        1250,
        issue=1212,
        profile_id="jupyterlab-1250-svg-unsanitized-v1",
        production_paths=("src/renderers/index.ts", "src/renderers/widget.ts"),
        patch_test_paths=(
            "test/src/renderers/renderers.spec.ts",
            "test/src/rendermime/rendermime.spec.ts",
        ),
        selected_test_path="test/src/renderers/renderers.spec.ts",
        patch_test_marker="should be `false`",
        suite="renderers SVGRenderer",
        f2p="#isSanitizable() should be `false`",
        p2p="#mimetypes should have the image/svg+xml mimetype",
        package_version="0.8.0",
        typescript_version="2.0.10",
    ),
    1846: _profile(
        1846,
        issue=1834,
        profile_id="jupyterlab-1846-gfm-latex-delimiter-v1",
        production_paths=("src/renderers/latex.ts",),
        patch_test_paths=("test/src/renderers/latex.spec.ts",),
        selected_test_path="test/src/renderers/latex.spec.ts",
        patch_test_marker="should handle `\\\\(` delimiters in GFM",
        suite="jupyter-ui removeMath()",
        f2p="should handle `\\(` delimiters in GFM",
        p2p="should handle code spans",
        package_version="0.18.0-0",
        typescript_version="2.1.6",
        declared_tool_constraints={
            "node": "5",
            "karma": "^0.13.19",
            "mocha": "^2.3.4",
            "typescript": "~2.1.6",
            "webpack": "^1.12.11",
        },
        authority_attachment_closure_required=True,
    ),
    2128: _profile(
        2128,
        issue=2106,
        profile_id="jupyterlab-2128-message-buffers-contract-v1",
        production_paths=(
            "packages/services/src/kernel/messages.ts",
            "packages/services/src/kernel/serialize.ts",
        ),
        patch_test_paths=(
            "packages/services/test/src/kernel/comm.spec.ts",
            "packages/services/test/src/kernel/validate.spec.ts",
        ),
        selected_test_path="packages/services/test/src/kernel/comm.spec.ts",
        patch_test_marker="should pass through a buffers field",
        suite="jupyter.services - Comm IComm #send()",
        f2p="should pass through a buffers field",
        p2p="should send a message to the server",
        package_version="0.42.1",
        typescript_version="2.2.1",
        additional_f2p_selectors=(
            "kernel/validate validateMessage should handle no buffers field",
        ),
        additional_patch_test_markers={
            "packages/services/test/src/kernel/validate.spec.ts": (
                "should handle no buffers field"
            )
        },
        declared_tool_constraints={
            "node": "5",
            "mocha": "^3.2.0",
            "typescript": "^2.2.1",
        },
        single_defect_contract_proven=False,
    ),
    2437: _profile(
        2437,
        issue=2436,
        profile_id="jupyterlab-2437-slideshow-metadata-shape-v1",
        production_paths=("packages/notebook/src/celltools.ts",),
        patch_test_paths=("test/src/notebook/celltools.spec.ts",),
        selected_test_path="test/src/notebook/celltools.spec.ts",
        patch_test_marker=(
            "expect(metadata.get('slideshow')).to.eql({ 'slide_type': 'slide' });"
        ),
        suite="notebook/celltools",
        f2p=("CellTools.createSlideShowSelector() should create a slide show selector"),
        p2p=(
            "CellTools.createNBConvertSelector() should create a raw mimetype selector"
        ),
        package_version="0.6.1",
        package_metadata_path="test/package.json",
        typescript_version="2.2.1",
        runtime_dependency_id=("jupyterlab-node5-karma1-webpack2-typescript-2.2.1"),
        runtime_versions={
            "karma": "1.7.0",
            "mocha": "3.4.2",
            "karma_mocha": "0.2.2",
            "webpack": "2.6.1",
            "firefox": "53.0",
        },
        declared_tool_constraints={
            "node": "5",
            "karma": "^1.5.0",
            "karma_mocha": "^0.2.1",
            "mocha": "^3.2.0",
            "typescript": "^2.2.1",
            "webpack": "^2.2.1",
        },
        authority_attachment_closure_required=True,
        captured_merged_revision_required=True,
    ),
    2752: _profile(
        2752,
        issue=2751,
        profile_id="jupyterlab-2752-multiline-source-v1",
        production_paths=("packages/cells/src/model.ts",),
        patch_test_paths=("test/src/cells/model.spec.ts",),
        selected_test_path="test/src/cells/model.spec.ts",
        patch_test_marker=(
            "should accept a base cell argument with a multiline source"
        ),
        suite="cells/model CellModel #constructor()",
        f2p="should accept a base cell argument with a multiline source",
        p2p="should create a cell model",
        package_version="0.9.0",
        package_metadata_path="test/package.json",
        typescript_version="2.4.2",
        runtime_dependency_id=("jupyterlab-node5-karma1-webpack2-typescript-2.4.2"),
        runtime_versions={
            "karma": "1.7.0",
            "mocha": "3.5.0",
            "karma_mocha": "0.2.2",
            "webpack": "2.7.0",
            "firefox": "54.0",
        },
        declared_tool_constraints={
            "node": "5",
            "karma": "^1.5.0",
            "karma_mocha": "^0.2.1",
            "mocha": "^3.2.0",
            "typescript": "~2.4.1",
            "webpack": "^2.2.1",
        },
        captured_merged_revision_required=True,
    ),
    3662: _profile(
        3662,
        issue=3652,
        profile_id="jupyterlab-3662-file-editor-dirty-title-v1",
        production_paths=("packages/fileeditor/src/widget.ts",),
        patch_test_paths=("tests/test-fileeditor/src/widget.spec.ts",),
        selected_test_path="tests/test-fileeditor/src/widget.spec.ts",
        patch_test_marker="should add the dirty class when the model is dirty",
        suite="fileeditorcodewrapper FileEditor #constructor()",
        f2p="should add the dirty class when the model is dirty",
        p2p="should update the editor text when the model changes",
        package_version="0.1.2",
        package_metadata_path="tests/test-fileeditor/package.json",
        test_harness_root="tests",
        build_script="build",
        typescript_version="2.6.2",
        runtime_dependency_id=("jupyterlab-node5-karma1-webpack2-typescript-2.6.2"),
        runtime_versions={
            "karma": "1.7.1",
            "mocha": "3.5.3",
            "karma_mocha": "0.2.2",
            "webpack": "2.7.0",
            "firefox": "57.0",
        },
        declared_tool_constraints={
            "node": "5",
            "karma": "~1.7.1",
            "karma_mocha": "~0.2.2",
            "mocha": "~3.5.3",
            "typescript": "~2.6.2",
            "webpack": "~2.7.0",
        },
        captured_merged_revision_required=True,
    ),
    3310: _profile(
        3310,
        issue=3309,
        linked_issue_numbers=(3303, 3309),
        profile_id="jupyterlab-3310-dock-focus-tracker-removal-v1",
        production_paths=("packages/application/src/shell.ts",),
        patch_test_paths=("test/src/application/shell.spec.ts",),
        selected_test_path="test/src/application/shell.spec.ts",
        patch_test_marker="widget.parent = null;",
        suite="ApplicationShell",
        f2p="#currentWidget should be the current widget in the shell main area",
        p2p="#isEmpty() should test whether the main area is empty",
        package_version="0.13.0",
        package_metadata_path="test/package.json",
        typescript_version="2.6.2",
        runtime_dependency_id=(
            "jupyterlab-node5-yarn1.3.2-jlpm1.3.2-karma1-typescript-2.6.2"
        ),
        runtime_root=(
            "private/dependencies/jupyterlab-node5-yarn1.3.2-jlpm1.3.2-karma1/"
            "typescript-2.6.2/1.0"
        ),
        runtime_versions={
            "karma": "1.7.1",
            "mocha": "3.5.3",
            "karma_mocha": "0.2.2",
            "webpack": "2.7.0",
            "firefox": "57.0",
        },
        declared_tool_constraints={
            "node": "5",
            "yarn": "bundled 1.3.2",
            "jlpm": "JupyterLab 0.30.0rc1 wrapper over bundled Yarn",
            "karma": "~1.7.1",
            "karma_mocha": "~0.2.2",
            "mocha": "~3.5.3",
            "typescript": "~2.6.2",
            "webpack": "~2.7.0",
            "firefox": "latest at merge time; registered closure selects 57.0",
        },
        authority_attachment_closure_required=True,
        captured_merged_revision_required=True,
        package_manager="jlpm",
        yarn_version="1.3.2",
    ),
}


@dataclass(frozen=True)
class JupyterLabRuntime:
    root: Path
    git: Path
    node: Path
    npm: Path
    karma: Path
    webpack: Path
    typescript: Path
    firefox: Path
    versions: Mapping[str, str]
    yarn: Path | None = None
    jlpm: Path | None = None


@dataclass(frozen=True)
class FixedCapsuleBinding:
    reference: ArtifactReference
    source_tree: Path
    source_origin: Literal["captured_merged_revision"]


@dataclass(frozen=True)
class ProductionInversePlan:
    paths: tuple[str, ...]
    patch: bytes
    apply_argv: tuple[Path | str, ...]

    def __post_init__(self) -> None:
        if (
            not self.paths
            or not self.patch
            or len(self.apply_argv) != 5
            or not isinstance(self.apply_argv[0], Path)
            or not self.apply_argv[0].is_absolute()
            or self.apply_argv[1:] != ("apply", "-R", "--whitespace=nowarn", "-")
        ):
            raise ValueError("JupyterLab production inverse is invalid")


@dataclass(frozen=True)
class RegressionRun:
    phase: Literal["f2p", "p2p"]
    test_path: str
    selector: str
    repetitions: int
    fixed_expected_outcome: Literal["pass"]
    buggy_expected_outcome: Literal["failure", "pass"]


@dataclass(frozen=True)
class JupyterLabRegressionExecutionPlan:
    profile: JupyterLabRegressionProfile
    runtime: JupyterLabRuntime
    fixed_capsule: FixedCapsuleBinding
    production_inverse: ProductionInversePlan
    build_argv: tuple[Path | str, ...]
    test_argv: tuple[Path | str, ...]
    mocha_client_args: tuple[str, str]
    f2p: RegressionRun
    p2p: RegressionRun
    closed_environment: Mapping[str, str]

    def __post_init__(self) -> None:
        expected_build = _build_argv(self.profile, self.runtime)
        if (
            self.build_argv != expected_build
            or (self.profile.package_manager == "jlpm" and self.runtime.jlpm is None)
            or self.test_argv
            != (
                self.runtime.node,
                self.runtime.karma,
                "start",
                f"{self.profile.test_harness_root}/karma.conf.js",
                "--browsers=Firefox",
                "--single-run",
            )
            or self.mocha_client_args != ("--grep", self.f2p.selector)
            or self.f2p.phase != "f2p"
            or self.p2p.phase != "p2p"
            or self.f2p.test_path != self.profile.selected_test_path
            or self.p2p.test_path != self.profile.selected_test_path
            or dict(self.closed_environment)
            != {
                "HOME": str(self.runtime.root / "runtime-home"),
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": str(self.runtime.root / "bin"),
            }
        ):
            raise ValueError("JupyterLab execution plan is invalid")


@dataclass(frozen=True)
class JupyterLabSourceMaterializationPlan:
    """Runtime-independent fixed tree and strict production inverse."""

    profile: JupyterLabRegressionProfile
    fixed_capsule: FixedCapsuleBinding
    production_inverse: ProductionInversePlan

    def __post_init__(self) -> None:
        if (
            self.profile != profile_for_candidate(self.profile.candidate)
            or self.fixed_capsule.reference != self.profile.fixed_capsule_reference
            or self.production_inverse.paths != self.profile.production_paths
        ):
            raise ValueError("JupyterLab source materialization plan is invalid")


def profile_for_candidate(candidate: CandidateKey) -> JupyterLabRegressionProfile:
    if type(candidate) is not CandidateKey:
        raise ValueError("JupyterLab regression candidate is invalid")
    try:
        profile = _PROFILES[candidate.change_number]
    except KeyError as error:
        raise ValueError(
            "candidate has no registered JupyterLab regression profile"
        ) from error
    if profile.candidate != candidate:
        raise ValueError("candidate has no registered JupyterLab regression profile")
    return profile


def plan_jupyterlab_regression_execution(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
) -> JupyterLabRegressionExecutionPlan | StageDecision:
    """Prepare one closed plan without installing, building, or executing it."""

    profile = profile_for_candidate(candidate)
    root = _artifact_root(artifact_root)
    source = _source(root, profile)
    if isinstance(source, StageDecision):
        return source
    source_closure = _source_closure_gate(root, profile, source)
    if source_closure is not None:
        return source_closure
    patch = _patch(root, profile, source)
    if isinstance(patch, StageDecision):
        return patch
    atomicity = _atomicity(root, profile, source, patch)
    if isinstance(atomicity, StageDecision):
        return atomicity
    manifest = _test_manifest(root, profile, patch, atomicity)
    if isinstance(manifest, StageDecision):
        return manifest
    raw_patch = _raw_patch(root, profile, source, patch)
    single_defect = _single_defect_gate(profile, raw_patch)
    if single_defect is not None:
        return single_defect
    runtime = _runtime(root, profile)
    if isinstance(runtime, StageDecision):
        return runtime
    capsule = _capsule(root, profile, source, patch, atomicity, manifest)
    if isinstance(capsule, StageDecision):
        return capsule
    inverse = _production_inverse(profile, raw_patch, runtime.git)

    def run(
        phase: Literal["f2p", "p2p"],
        selector: str,
        buggy: Literal["failure", "pass"],
    ) -> RegressionRun:
        return RegressionRun(
            phase=phase,
            test_path=profile.selected_test_path,
            selector=selector,
            repetitions=3,
            fixed_expected_outcome="pass",
            buggy_expected_outcome=buggy,
        )

    f2p = run("f2p", profile.f2p_selector, "failure")
    p2p = run("p2p", profile.p2p_selector, "pass")
    return JupyterLabRegressionExecutionPlan(
        profile=profile,
        runtime=runtime,
        fixed_capsule=capsule,
        production_inverse=inverse,
        build_argv=_build_argv(profile, runtime),
        test_argv=(
            runtime.node,
            runtime.karma,
            "start",
            f"{profile.test_harness_root}/karma.conf.js",
            "--browsers=Firefox",
            "--single-run",
        ),
        mocha_client_args=("--grep", f2p.selector),
        f2p=f2p,
        p2p=p2p,
        closed_environment={
            "HOME": str(runtime.root / "runtime-home"),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": str(runtime.root / "bin"),
        },
    )


def plan_jupyterlab_source_materialization(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
) -> JupyterLabSourceMaterializationPlan | StageDecision:
    """Prepare fixed/buggy trees without requiring or running the test runtime."""

    profile = profile_for_candidate(candidate)
    root = _artifact_root(artifact_root)
    source = _source(root, profile)
    if isinstance(source, StageDecision):
        return source
    patch = _patch(root, profile, source)
    if isinstance(patch, StageDecision):
        return patch
    atomicity = _atomicity(root, profile, source, patch)
    if isinstance(atomicity, StageDecision):
        return atomicity
    manifest = _test_manifest(root, profile, patch, atomicity)
    if isinstance(manifest, StageDecision):
        return manifest
    raw_patch = _raw_patch(root, profile, source, patch)
    capsule = _capsule(root, profile, source, patch, atomicity, manifest)
    if isinstance(capsule, StageDecision):
        return capsule
    git = Path("/usr/bin/git")
    try:
        info = git.lstat()
    except FileNotFoundError:
        return _blocker(
            profile,
            "source_materialization_git_unavailable",
            {"git_path": str(git)},
        )
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or not os.access(git, os.X_OK)
    ):
        raise RuntimeError("JupyterLab source materialization git is unsafe")
    return JupyterLabSourceMaterializationPlan(
        profile=profile,
        fixed_capsule=capsule,
        production_inverse=_production_inverse(profile, raw_patch, git),
    )


def _build_argv(
    profile: JupyterLabRegressionProfile, runtime: JupyterLabRuntime
) -> tuple[Path | str, ...]:
    if profile.build_script == "build:test":
        if profile.package_manager == "jlpm":
            return runtime.jlpm, "run", profile.build_script
        return runtime.node, runtime.npm, "run", profile.build_script
    package_directory = PurePosixPath(profile.package_metadata_path).parent.as_posix()
    if profile.package_manager == "jlpm":
        return runtime.jlpm, "--cwd", package_directory, "run", profile.build_script
    return (
        runtime.node,
        runtime.npm,
        "--prefix",
        package_directory,
        "run",
        profile.build_script,
    )


def _blocker(
    profile: JupyterLabRegressionProfile,
    reason: str,
    details: Mapping[str, object] | None = None,
) -> StageDecision:
    return StageDecision.terminal(
        "infrastructure_blocked",
        {"profile_id": profile.profile_id, "reason": reason, **(details or {})},
    )


def _source_closure_gate(
    root: Path,
    profile: JupyterLabRegressionProfile,
    source: StableArtifact,
) -> StageDecision | None:
    if not profile.authority_attachment_closure_required:
        return None
    manifest = _source_manifest(root, profile, source)
    attachment_paths = manifest.get("attachment_paths")
    raw_paths = manifest.get("raw_paths")
    if (
        type(attachment_paths) is not list
        or any(type(path) is not str for path in attachment_paths)
        or type(raw_paths) is not list
        or not raw_paths
        or any(type(path) is not str for path in raw_paths)
    ):
        raise RuntimeError("JupyterLab source attachment inventory is invalid")
    for relative in attachment_paths:
        _regular_file(root, relative, "source attachment")
    external_urls: set[str] = set()
    for relative in raw_paths:
        raw = _regular_file(root, relative, "source raw response")
        text = raw.read_text(encoding="utf-8", errors="surrogateescape")
        external_urls.update(
            re.findall(
                r'https://cloud\.githubusercontent\.com/[^"<>\s)\\]+',
                text,
            )
        )
    if external_urls and not attachment_paths:
        return _blocker(
            profile,
            "authority_attachment_closure_incomplete",
            {
                "source_manifest_path": source.payload["raw_evidence_path"],
                "missing_attachment_urls": sorted(external_urls),
            },
        )
    return None


def _single_defect_gate(
    profile: JupyterLabRegressionProfile,
    raw_patch: bytes,
) -> StageDecision | None:
    if profile.single_defect_contract_proven:
        return None
    sections = {section.path: section for section in _patch_sections(raw_patch)}
    if set(sections) != set(profile.production_paths + profile.patch_test_paths):
        raise RuntimeError("JupyterLab single-defect evidence is invalid")
    for path, marker in profile.patch_test_markers.items():
        if marker not in "".join(sections[path].lines):
            raise RuntimeError("JupyterLab single-defect evidence is invalid")
    return StageDecision.terminal(
        "quality_rejected",
        {
            "profile_id": profile.profile_id,
            "reason": "single_defect_contract_unproven",
            "authoritative_issue_number": profile.primary_issue_number,
            "distinct_behaviors": [
                "nested_buffers_payload_preservation",
                "optional_top_level_buffers_field",
            ],
            "production_paths": list(profile.production_paths),
            "test_paths": list(profile.patch_test_paths),
        },
    )


def _load(
    root: Path,
    profile: JupyterLabRegressionProfile,
    reference: ArtifactReference,
    missing_reason: str,
) -> StableArtifact | StageDecision:
    try:
        return load_stable_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
        )
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return _blocker(
                profile, missing_reason, {"artifact_id": reference.artifact_id}
            )
        raise


def _source(
    root: Path, profile: JupyterLabRegressionProfile
) -> StableArtifact | StageDecision:
    artifact = _load(
        root, profile, profile.source_packet_reference, "source_packet_unavailable"
    )
    if isinstance(artifact, StageDecision):
        return artifact
    path = artifact.payload.get("raw_evidence_path")
    if (
        artifact.inputs != (_ENUMERATION,)
        or artifact.payload.get("candidate") != profile.candidate.to_value()
        or artifact.payload.get("complete") is not True
        or type(path) is not str
        or not path
    ):
        raise RuntimeError("JupyterLab source packet is invalid")
    _safe_relative(path, "source evidence path")
    return artifact


def _patch(
    root: Path,
    profile: JupyterLabRegressionProfile,
    source: StableArtifact,
) -> StableArtifact | StageDecision:
    artifact = _load(root, profile, profile.patch_reference, "test_patch_unavailable")
    if isinstance(artifact, StageDecision):
        return artifact
    if (
        artifact.inputs != (_ENUMERATION, source.reference)
        or artifact.payload.get("candidate") != profile.candidate.to_value()
        or artifact.payload.get("complete") is not True
        or artifact.payload.get("outcome") != "patch_eligible"
        or artifact.payload.get("production_paths") != list(profile.production_paths)
        or artifact.payload.get("test_paths") != list(profile.patch_test_paths)
        or artifact.payload.get("source_manifest_path")
        != source.payload.get("raw_evidence_path")
        or type(artifact.payload.get("raw_patch_path")) is not str
    ):
        raise RuntimeError("JupyterLab test patch is invalid")
    _safe_relative(artifact.payload["raw_patch_path"], "raw patch path")
    return artifact


def _atomicity(
    root: Path,
    profile: JupyterLabRegressionProfile,
    source: StableArtifact,
    patch: StableArtifact,
) -> StableArtifact | StageDecision:
    artifact = _load(
        root, profile, profile.atomicity_reference, "source_atomicity_unavailable"
    )
    if isinstance(artifact, StageDecision):
        return artifact
    expected_checkpoint = ArtifactReference(
        f"screening_checkpoint/jupyterlab/{profile.candidate.change_number}/current/v2",
        "screening_checkpoint",
        "2.0",
    )
    payload = artifact.payload
    if (
        artifact.inputs
        != (_ENUMERATION, source.reference, patch.reference, expected_checkpoint)
        or payload.get("candidate") != profile.candidate.to_value()
        or payload.get("complete") is not True
        or payload.get("outcome") != "accepted"
        or payload.get("primary_issue_number") != profile.primary_issue_number
        or payload.get("authoritative_issue_numbers") != [profile.primary_issue_number]
        or payload.get("linked_issue_numbers") != list(profile.linked_issue_numbers)
        or payload.get("authoritative_issue_count") != 1
        or payload.get("linked_issue_count") != len(profile.linked_issue_numbers)
        or payload.get("source_manifest_path")
        != source.payload.get("raw_evidence_path")
    ):
        raise RuntimeError("JupyterLab source atomicity is invalid")
    return artifact


def _test_manifest(
    root: Path,
    profile: JupyterLabRegressionProfile,
    patch: StableArtifact,
    atomicity: StableArtifact,
) -> StableArtifact | StageDecision:
    artifact = _load(
        root, profile, profile.test_manifest_reference, "test_manifest_unavailable"
    )
    if isinstance(artifact, StageDecision):
        return artifact
    details = artifact.payload.get("details")
    if (
        artifact.inputs != (patch.reference, atomicity.reference)
        or artifact.payload.get("candidate") != profile.candidate.to_value()
        or artifact.payload.get("complete") is not True
        or artifact.payload.get("stage") != "test_manifest"
        or artifact.payload.get("state") != "passed"
        or artifact.payload.get("outcome") is not None
        or type(details) is not dict
        or type(details.get("executable_test_paths")) is not list
        or profile.selected_test_path not in details["executable_test_paths"]
        or not set(details["executable_test_paths"]).issubset(profile.patch_test_paths)
        or details.get("test_count") != len(details["executable_test_paths"])
    ):
        raise RuntimeError("JupyterLab test manifest is invalid")
    return artifact


def _raw_patch(
    root: Path,
    profile: JupyterLabRegressionProfile,
    source: StableArtifact,
    patch: StableArtifact,
) -> bytes:
    manifest = _source_manifest(root, profile, source)
    if manifest.get("patch_raw_path") != patch.payload["raw_patch_path"]:
        raise RuntimeError("JupyterLab source evidence is invalid")
    return _regular_file(
        root, patch.payload["raw_patch_path"], "raw patch"
    ).read_bytes()


def _source_manifest(
    root: Path,
    profile: JupyterLabRegressionProfile,
    source: StableArtifact,
) -> dict[str, object]:
    source_path = _regular_file(
        root, source.payload["raw_evidence_path"], "source evidence"
    )
    try:
        manifest = json.loads(source_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("JupyterLab source evidence is invalid") from error
    if (
        type(manifest) is not dict
        or manifest.get("candidate") != profile.candidate.to_value()
        or manifest.get("complete") is not True
    ):
        raise RuntimeError("JupyterLab source evidence is invalid")
    return manifest


def _runtime(
    root: Path, profile: JupyterLabRegressionProfile
) -> JupyterLabRuntime | StageDecision:
    runtime_root = root / profile.runtime_root
    try:
        info = runtime_root.lstat()
    except FileNotFoundError:
        return _runtime_unavailable(root, profile)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("JupyterLab runtime root is unsafe")
    manifest_path = runtime_root / "runtime.json"
    try:
        manifest_info = manifest_path.lstat()
    except FileNotFoundError:
        return _runtime_unavailable(root, profile)
    if stat.S_ISLNK(manifest_info.st_mode) or not stat.S_ISREG(manifest_info.st_mode):
        raise RuntimeError("JupyterLab runtime manifest is unsafe")
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("JupyterLab runtime manifest is invalid") from error
    commands_manifest = _runtime_commands(profile)
    if type(value) is not dict or set(value) != {
        "dependency_id",
        "semantic_version",
        "commands",
        "versions",
        "closure_files",
    }:
        raise RuntimeError("JupyterLab runtime manifest is invalid")
    closure = value.get("closure_files")
    if (
        value.get("dependency_id") != profile.runtime_dependency_id
        or value.get("semantic_version") != "1.0"
        or value.get("commands") != commands_manifest
        or value.get("versions") != dict(profile.required_versions)
        or type(closure) is not list
        or not closure
        or any(type(item) is not str for item in closure)
        or closure != sorted(set(closure))
        or not set(
            (*commands_manifest.values(), *_RUNTIME_PACKAGE_METADATA.values())
        ).issubset(closure)
    ):
        raise RuntimeError("JupyterLab runtime manifest is invalid")
    commands: dict[str, Path] = {}
    for name, relative in commands_manifest.items():
        path = runtime_root / relative
        try:
            command_info = path.lstat()
        except FileNotFoundError:
            return _blocker(
                profile,
                "registered_runtime_unavailable",
                {"runtime_root": profile.runtime_root, "missing_command": name},
            )
        if (
            stat.S_ISLNK(command_info.st_mode)
            or not stat.S_ISREG(command_info.st_mode)
            or command_info.st_uid != os.getuid()
            or command_info.st_mode & 0o111 == 0
        ):
            raise RuntimeError("JupyterLab runtime command is unsafe")
        commands[name] = path.resolve(strict=True)
    for relative in closure:
        _safe_relative(relative, "runtime closure path")
        _regular_file(runtime_root, relative, "runtime closure file")
    actual_closure: list[str] = []
    for path in runtime_root.rglob("*"):
        relative = path.relative_to(runtime_root)
        if "runtime-home" in relative.parts:
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
        ):
            raise RuntimeError("JupyterLab runtime closure is unsafe")
        if stat.S_ISREG(info.st_mode) and path.name != "runtime.json":
            actual_closure.append(relative.as_posix())
    actual_closure.sort()
    if actual_closure != closure:
        raise RuntimeError("JupyterLab runtime closure is invalid")
    return JupyterLabRuntime(
        root=runtime_root.resolve(strict=True),
        versions=dict(profile.required_versions),
        **commands,
    )


def _runtime_commands(profile: JupyterLabRegressionProfile) -> dict[str, str]:
    commands = dict(_BASE_RUNTIME_COMMANDS)
    if profile.package_manager == "jlpm":
        commands.update({"yarn": "bin/yarn", "jlpm": "bin/jlpm"})
    return commands


def _runtime_unavailable(
    root: Path, profile: JupyterLabRegressionProfile
) -> StageDecision:
    reference = ArtifactReference(
        f"dependency_preparation/{profile.runtime_dependency_id}/current/v1",
        "dependency_preparation",
        "1.0",
    )
    try:
        artifact = load_stable_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
        )
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            raise
        return _blocker(
            profile,
            "registered_runtime_unavailable",
            {"runtime_root": profile.runtime_root},
        )
    payload = artifact.payload
    mismatched = payload.get("mismatched_components")
    if (
        artifact.inputs
        or payload.get("complete") is not True
        or payload.get("outcome") != "infrastructure_blocked"
        or payload.get("reason") != "historical_runtime_components_unavailable"
        or payload.get("dependency_id") != profile.runtime_dependency_id
        or payload.get("runtime_root") != profile.runtime_root
        or payload.get("required_versions") != dict(profile.required_versions)
        or type(payload.get("observed_versions")) is not dict
        or type(payload.get("missing_components")) is not list
        or any(type(value) is not str for value in payload["missing_components"])
        or type(mismatched) is not list
        or any(
            type(value) is not dict
            or set(value) != {"component", "expected", "observed"}
            or any(type(item) is not str for item in value.values())
            for value in mismatched
        )
    ):
        raise RuntimeError("JupyterLab runtime preparation is invalid")
    return _blocker(
        profile,
        "historical_runtime_components_unavailable",
        {
            "runtime_root": profile.runtime_root,
            "runtime_preparation_id": reference.artifact_id,
            "missing_components": payload["missing_components"],
            "mismatched_components": mismatched,
        },
    )


def _capsule(
    root: Path,
    profile: JupyterLabRegressionProfile,
    source: StableArtifact,
    patch: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
) -> FixedCapsuleBinding | StageDecision:
    artifact = _load(
        root, profile, profile.fixed_capsule_reference, "fixed_capsule_unavailable"
    )
    if isinstance(artifact, StageDecision):
        return artifact
    expected_payload = {
        "candidate": profile.candidate.to_value(),
        "profile_id": profile.profile_id,
        "source_origin": "captured_merged_revision",
        "source_tree_path": profile.fixed_source_tree_path,
        "production_paths": list(profile.production_paths),
        "test_paths": list(profile.patch_test_paths),
    }
    if (
        artifact.inputs
        != (source.reference, patch.reference, atomicity.reference, manifest.reference)
        or artifact.payload != expected_payload
    ):
        raise RuntimeError("JupyterLab fixed capsule binding is invalid")
    source_tree = root / profile.fixed_source_tree_path
    try:
        info = source_tree.lstat()
    except FileNotFoundError:
        return _blocker(
            profile,
            "fixed_capsule_unavailable",
            {"source_tree_path": profile.fixed_source_tree_path},
        )
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("JupyterLab fixed capsule source tree is unsafe")
    for relative in profile.production_paths + profile.patch_test_paths:
        _regular_file(source_tree, relative, "fixed capsule source")
    selected = (source_tree / profile.selected_test_path).read_text(
        encoding="utf-8", errors="surrogateescape"
    )
    if profile.fixed_test_marker not in selected:
        raise RuntimeError("JupyterLab fixed capsule test binding is invalid")
    return FixedCapsuleBinding(
        reference=artifact.reference,
        source_tree=source_tree.resolve(strict=True),
        source_origin="captured_merged_revision",
    )


@dataclass(frozen=True)
class _PatchSection:
    path: str
    lines: tuple[str, ...]


def _production_inverse(
    profile: JupyterLabRegressionProfile,
    content: bytes,
    git: Path,
) -> ProductionInversePlan:
    sections = _patch_sections(content)
    paths = tuple(section.path for section in sections)
    expected = profile.production_paths + profile.patch_test_paths
    if len(set(paths)) != len(paths) or set(paths) != set(expected):
        raise RuntimeError("JupyterLab raw patch is outside the registered profile")
    by_path = {section.path: section for section in sections}
    selected_test = "".join(by_path[profile.selected_test_path].lines)
    if profile.patch_test_marker not in selected_test:
        raise RuntimeError("JupyterLab raw patch does not bind the selected test")
    production_patch = "".join(
        "".join(by_path[path].lines) for path in profile.production_paths
    ).encode("utf-8", errors="surrogateescape")
    return ProductionInversePlan(
        paths=profile.production_paths,
        patch=production_patch,
        apply_argv=(git, "apply", "-R", "--whitespace=nowarn", "-"),
    )


def _patch_sections(content: bytes) -> tuple[_PatchSection, ...]:
    if not content:
        raise RuntimeError("JupyterLab raw patch is empty")
    sections: list[_PatchSection] = []
    current_path: str | None = None
    current_lines: list[str] = []
    for line in content.decode("utf-8", errors="surrogateescape").splitlines(
        keepends=True
    ):
        if line.startswith("diff --git "):
            if current_path is not None:
                sections.append(_PatchSection(current_path, tuple(current_lines)))
            current_path = _path_from_header(line)
            current_lines = [line]
        elif current_path is not None:
            current_lines.append(line)
    if current_path is not None:
        sections.append(_PatchSection(current_path, tuple(current_lines)))
    if not sections:
        raise RuntimeError("JupyterLab raw patch is invalid")
    return tuple(sections)


def _path_from_header(line: str) -> str:
    try:
        parts = shlex.split(line.rstrip("\r\n"))
    except ValueError as error:
        raise RuntimeError("JupyterLab raw patch header is invalid") from error
    if (
        len(parts) != 4
        or parts[:2] != ["diff", "--git"]
        or not parts[2].startswith("a/")
        or not parts[3].startswith("b/")
        or parts[2][2:] != parts[3][2:]
    ):
        raise RuntimeError("JupyterLab raw patch header is invalid")
    path = parts[2][2:]
    _safe_relative(path, "raw patch path")
    return path


def _regular_file(root: Path, relative: str, label: str) -> Path:
    path = root / _safe_relative(relative, label)
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise RuntimeError(f"JupyterLab {label} is missing") from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
    ):
        raise RuntimeError(f"JupyterLab {label} is unsafe")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as error:
        raise RuntimeError(f"JupyterLab {label} escapes its root") from error
    return resolved


def _artifact_root(value: str | Path) -> Path:
    root = Path(value).absolute()
    try:
        info = root.lstat()
    except FileNotFoundError as error:
        raise RuntimeError("artifact root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return root.resolve(strict=True)


__all__ = [
    "FixedCapsuleBinding",
    "JupyterLabRegressionExecutionPlan",
    "JupyterLabRegressionProfile",
    "JupyterLabRuntime",
    "JupyterLabSourceMaterializationPlan",
    "ProductionInversePlan",
    "RegressionRun",
    "plan_jupyterlab_regression_execution",
    "plan_jupyterlab_source_materialization",
    "profile_for_candidate",
]
