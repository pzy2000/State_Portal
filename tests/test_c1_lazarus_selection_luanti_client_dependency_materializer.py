from __future__ import annotations

import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_client_dependency_materializer import (
    MATERIALIZATION_CONFIRMATION,
    load_luanti_client_dependency_closure,
    materialize_luanti_client_dependency_closure,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-test/luanti-client-dependency-materializer",
        scope="preannotation-execution",
        granted_at="2026-08-14T03:00:00Z",
        local_user_id=os.getuid(),
    )


def _seed_plan(root: Path, entries: list[dict[str, str]]) -> None:
    feasibility = ArtifactReference(
        "native_client_feasibility/luanti/15519/null-video/v1",
        "native_client_feasibility",
        "1.0",
    )
    plan = ArtifactReference(
        "client_dependency_packaging_plan/luanti/15519/darwin-arm64/v2",
        "client_dependency_packaging_plan",
        "2.0",
    )
    write_stable_artifact(
        root,
        artifact_id=feasibility.artifact_id,
        artifact_kind=feasibility.artifact_kind,
        semantic_version=feasibility.semantic_version,
        created_at="2026-08-14T03:00:01Z",
        inputs=(),
        payload={"complete": True},
        authorization=_authorization(),
    )
    write_stable_artifact(
        root,
        artifact_id=plan.artifact_id,
        artifact_kind=plan.artifact_kind,
        semantic_version=plan.semantic_version,
        created_at="2026-08-14T03:00:02Z",
        inputs=(feasibility,),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 15519},
            "host": "darwin-arm64",
            "feasible_offline": True,
            "copy_entries": entries,
            "destination_root": "private/dependencies/luanti-client/2.0",
            "packaging_performed": False,
            "client_build_started": False,
            "network_required": False,
            "downstream_state": "ready_for_offline_packaging",
            "complete": True,
        },
        authorization=_authorization(),
    )


def _source_entries(root: Path) -> list[dict[str, str]]:
    source = root / "registered-source"
    header = source / "include/registered.h"
    library = source / "lib/libregistered.dylib"
    header.parent.mkdir(parents=True)
    library.parent.mkdir(parents=True)
    header.write_text("registered header\n", encoding="utf-8")
    library.write_bytes(b"registered library\n")
    return [
        {
            "source": str(header.resolve()),
            "destination": "packages/freetype/include/registered.h",
        },
        {
            "source": str(library.resolve()),
            "destination": "packages/freetype/lib/libregistered.dylib",
        },
    ]


def test_materializes_only_registered_files_and_typed_reload(tmp_path: Path) -> None:
    entries = _source_entries(tmp_path)
    _seed_plan(tmp_path, entries)

    result = materialize_luanti_client_dependency_closure(
        tmp_path,
        authorization=_authorization(),
        confirmation=MATERIALIZATION_CONFIRMATION,
    )

    assert result.resumed is False
    assert result.copied_files == 2
    assert result.artifact.payload["packaging_performed"] is True
    assert result.artifact.payload["client_build_started"] is False
    assert result.artifact.payload["network_used"] is False
    assert result.artifact.payload["install_name_rewrites_applied"] is False
    destination = tmp_path / "private/dependencies/luanti-client/2.0"
    assert (
        destination / "packages/freetype/include/registered.h"
    ).read_text() == "registered header\n"
    assert (
        destination / "packages/freetype/lib/libregistered.dylib"
    ).read_bytes() == b"registered library\n"

    resumed = materialize_luanti_client_dependency_closure(
        tmp_path,
        authorization=_authorization(),
        confirmation=MATERIALIZATION_CONFIRMATION,
    )
    assert resumed.resumed is True
    assert resumed.copied_files == 0
    assert load_luanti_client_dependency_closure(tmp_path).reference == (
        result.artifact.reference
    )


def test_rejects_symlinked_registered_source(tmp_path: Path) -> None:
    external = tmp_path / "external.h"
    external.write_text("external\n", encoding="utf-8")
    link = tmp_path / "registered-link.h"
    link.symlink_to(external)
    _seed_plan(
        tmp_path,
        [{"source": str(link), "destination": "packages/png/include/png.h"}],
    )

    with pytest.raises(RuntimeError, match="source is unsafe"):
        materialize_luanti_client_dependency_closure(
            tmp_path,
            authorization=_authorization(),
            confirmation=MATERIALIZATION_CONFIRMATION,
        )


@pytest.mark.parametrize(
    "destination",
    ["../escape", "/absolute/escape", "packages/../../escape", "outside/file"],
)
def test_rejects_destination_escape(tmp_path: Path, destination: str) -> None:
    source = tmp_path / "registered.h"
    source.write_text("registered\n", encoding="utf-8")
    _seed_plan(
        tmp_path,
        [{"source": str(source.resolve()), "destination": destination}],
    )

    with pytest.raises(RuntimeError, match="destination is unsafe"):
        materialize_luanti_client_dependency_closure(
            tmp_path,
            authorization=_authorization(),
            confirmation=MATERIALIZATION_CONFIRMATION,
        )
