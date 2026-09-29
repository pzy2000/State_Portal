from __future__ import annotations

import json
import os
from pathlib import Path

from warptest.c1_lazarus_selection.luanti_15519_client_feasibility import (
    audit_luanti_15519_client_feasibility,
    write_luanti_15519_client_feasibility,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-test/luanti-15519-client-feasibility",
        scope="preannotation-execution",
        granted_at="2026-08-14T01:00:00Z",
        local_user_id=os.getuid(),
    )


def _write(
    root: Path,
    reference: ArtifactReference,
    *,
    inputs: tuple[ArtifactReference, ...] = (),
    payload: dict[str, object],
    second: int,
) -> None:
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=f"2026-08-14T01:00:{second:02d}Z",
        inputs=inputs,
        payload=payload,
        authorization=_authorization(),
    )


def _seed(
    root: Path, packages: tuple[str, ...] = ("sqlite3", "zlib", "zstd", "iconv")
) -> None:
    mapping = ArtifactReference(
        "lazarus_task_mapping/luanti/15519/issue-15518/v2",
        "lazarus_task_mapping",
        "2.0",
    )
    _write(
        root,
        mapping,
        payload={"candidate": {"repository_key": "luanti", "change_number": 15519}},
        second=1,
    )
    fixed_observer = ArtifactReference(
        "native_server_closure_step/luanti/15519/fixed/observer/v1",
        "native_server_closure_step",
        "1.0",
    )
    buggy_observer = ArtifactReference(
        "native_server_closure_step/luanti/15519/buggy/observer/v1",
        "native_server_closure_step",
        "1.0",
    )
    aggregate = ArtifactReference(
        "native_server_closure/luanti/15519/fixed-buggy/v1",
        "native_server_closure",
        "1.0",
    )
    for second, reference, classification in (
        (2, fixed_observer, "fixed_accepts_registered_russian_plural_form"),
        (3, buggy_observer, "buggy_rejects_registered_russian_plural_form"),
    ):
        _write(
            root,
            reference,
            inputs=(mapping,),
            payload={"semantic_classification": classification},
            second=second,
        )
    _write(
        root,
        aggregate,
        inputs=(fixed_observer, buggy_observer),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 15519},
            "server_only": True,
            "fixed_build": "passed",
            "buggy_build": "passed",
            "fixed_observer": "fixed_accepts_registered_russian_plural_form",
            "buggy_observer": "buggy_rejects_registered_russian_plural_form",
            "semantic_qualified": True,
            "downstream_state": "pending",
            "missing_required_evidence": [
                "complete_fixed_buggy_visual_client_pair",
                "raw_fixed_and_buggy_application_frames",
                "visual_goldens_and_cross_matrix",
                "same_lineage_visual_construction_receipt",
            ],
            "permanent_terminal_written": False,
            "paid_actions_authorized": False,
            "complete": True,
        },
        second=4,
    )
    for variant in ("fixed", "buggy"):
        tree = root / f"private/luanti-15519-server-closure/v1/{variant}"
        rendering = tree / "src/client/renderingengine.cpp"
        rendering.parent.mkdir(parents=True)
        rendering.write_text(
            'video::EDT_NULL, {"null", "NULL Driver"}\n'
            "params.DriverType = *requested_driver;\n",
            encoding="utf-8",
        )
        null_driver = tree / "irr/src/CNullDriver.cpp"
        null_driver.parent.mkdir(parents=True)
        null_driver.write_text(
            "IImage *CNullDriver::createScreenShot() { return 0; }\n",
            encoding="utf-8",
        )
        cmake = tree / "src/CMakeLists.txt"
        cmake.write_text(
            "if(BUILD_CLIENT)\nfind_package(Freetype REQUIRED)\nendif()\n",
            encoding="utf-8",
        )
        irr_cmake = tree / "irr/src/CMakeLists.txt"
        irr_cmake.write_text(
            "find_package(JPEG REQUIRED)\nfind_package(PNG REQUIRED)\n",
            encoding="utf-8",
        )
    toolchain = root / "private/dependencies/luanti-native/3.0"
    toolchain.mkdir(parents=True)
    (toolchain / "runtime.json").write_text(
        json.dumps({"package_roots": {name: f"packages/{name}" for name in packages}}),
        encoding="utf-8",
    )


def test_audit_separates_null_semantics_from_real_visual_goldens(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)

    result = audit_luanti_15519_client_feasibility(tmp_path)

    assert result.server_semantic_qualified is True
    assert result.source_pair_complete is True
    assert result.null_video_supported is True
    assert result.null_video_screenshot_capable is False
    assert result.missing_registered_dependencies == ("freetype", "jpeg", "png")
    assert result.null_client_status == "blocked_registered_client_dependencies"
    assert result.visual_status == "blocked_null_driver_has_no_framebuffer"


def test_profile_is_ready_for_null_semantic_build_only_with_registered_deps(
    tmp_path: Path,
) -> None:
    _seed(tmp_path, packages=("freetype", "jpeg", "png", "sqlite3", "zlib"))

    result = audit_luanti_15519_client_feasibility(tmp_path)

    assert result.missing_registered_dependencies == ()
    assert result.null_client_status == "ready_for_bounded_build_not_executed"
    assert result.visual_status == "blocked_null_driver_has_no_framebuffer"


def test_writer_persists_pending_without_claiming_visual_or_execution(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)

    artifact = write_luanti_15519_client_feasibility(
        tmp_path, authorization=_authorization()
    )

    assert artifact.reference == ArtifactReference(
        "native_client_feasibility/luanti/15519/null-video/v1",
        "native_client_feasibility",
        "1.0",
    )
    assert artifact.payload["downstream_state"] == "pending"
    assert artifact.payload["candidate_execution_performed"] is False
    assert artifact.payload["null_video_screenshot_capable"] is False
    assert artifact.payload["visual_golden_disposition"] == "still_required"
    assert artifact.payload["permanent_terminal_written"] is False
