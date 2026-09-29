#!/usr/bin/env python3
"""Prepare two registered Luanti candidates without network or builds.

This is deliberately a small migration/preparation command, not a candidate
miner.  It fills only the missing construction-test capsules from the clean
local ``games/luanti`` history and verifies the registered unit-test prebuild
route.  It never fetches authority data, runs CMake, or materializes a version
pair.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_formal_build import (  # noqa: E402
    LUANTI_FORMAL_TASK_IDS,
    create_formal_prebuild_callback,
    formal_luanti_prebuild_artifact_path,
)
from warptest.lazarus_mining import (  # noqa: E402
    backfill_local_frozen_test_capsules,
    validate_lazarus_trial_provenance,
    validate_upstream_record,
)


DEFAULT_TASK_IDS = (
    "lz-luanti-14396-metadata-set-string-nil",
    "lz-luanti-14586-full-crafting-shift-move",
    "lz-luanti-14555-negative-half-pointing",
)
_CONSTRUCTION_KINDS = frozenset({"fail_to_pass", "pass_to_pass"})
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_CPP_UNITTEST_MODULES = {
    "lz-luanti-14586-full-crafting-shift-move": "TestMoveAction",
    "lz-luanti-14555-negative-half-pointing": "TestVoxelAlgorithms",
}
_LUANTI_14586_COMPATIBILITY_TASK_ID = "lz-luanti-14586-full-crafting-shift-move"
_LUANTI_14586_COMPATIBILITY_TEST_ID = "luanti-pr14586-move-somewhere-partial-f2p"
_LUANTI_14586_FIX_COMMIT = "c044a3c1cabda2f5f61fee9e746dba5caa427085"
_LUANTI_14586_PINNED_REVISION = "c48f698073df253fc6743d16cf056c1489b60dfb"
_LUANTI_14586_SOURCE_PATH = "src/unittest/test_moveaction.cpp"
_LUANTI_14586_CAPSULE_PATH = Path("upstream-tests") / _LUANTI_14586_SOURCE_PATH
_LUANTI_14586_SHIM_SCHEMA = "c1-lazarus-luanti-include-only-shim-v1"
_LUANTI_14586_INCLUDE_ANCHOR = b'#include "mock_serveractiveobject.h"\n'
_LUANTI_14586_INSERTED_INCLUDE = b'#include "servermap.h"\n'


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--task-id", action="append")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def _contained(workspace: Path, value: str | Path, label: str) -> Path:
    path = Path(value).expanduser()
    result = path.resolve() if path.is_absolute() else (workspace / path).resolve()
    if workspace != result and workspace not in result.parents:
        raise ValueError(f"{label} escapes workspace: {value}")
    return result


def _load_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load {label}: {path}: {error}") from error
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return dict(value)


def _relative_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is missing")
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"{label} escapes its registered root")
    return Path(path.as_posix())


def _apply_registered_luanti_compatibility_shims(
    *,
    task_id: str,
    record: Mapping[str, Any],
    declared_tests: Sequence[Mapping[str, Any]],
    backfilled_tests: Sequence[Mapping[str, Any]],
    capsules: Mapping[Path, bytes],
) -> tuple[list[dict[str, Any]], dict[Path, bytes], list[dict[str, str]]]:
    """Apply the sole registered historical Luanti test compatibility bridge.

    The 14586 fixed regression source is authoritative at c044.  The selected
    pinned c48 tree requires one subsequently-added direct ``servermap.h``
    include to compile that otherwise byte-identical F2P source.  This helper
    is deliberately closed over the one task, test, source path, revisions,
    anchor, and inserted line.  It derives the result from the clean local
    c044 capsule and rejects every other source or test-declaration change.
    """

    prepared_tests = [copy.deepcopy(dict(value)) for value in backfilled_tests]
    prepared_capsules = {
        Path(path): bytes(content) for path, content in capsules.items()
    }
    normalized = str(task_id).strip()
    shimmed_test_ids = [
        str(test.get("id") or "")
        for test in declared_tests
        if isinstance(test, Mapping) and "frozen_test_compatibility_shim" in test
    ]
    if normalized != _LUANTI_14586_COMPATIBILITY_TASK_ID:
        if shimmed_test_ids:
            raise ValueError("only the registered 14586 Luanti test may use a shim")
        return prepared_tests, prepared_capsules, []

    if (
        record.get("benchmark") != "luanti"
        or record.get("fix_commit") != _LUANTI_14586_FIX_COMMIT
        or record.get("pinned_revision") != _LUANTI_14586_PINNED_REVISION
    ):
        raise ValueError("14586 compatibility shim has an unexpected version binding")
    if len(declared_tests) != len(prepared_tests):
        raise ValueError("14586 compatibility shim test cardinality drifted")
    if shimmed_test_ids != [_LUANTI_14586_COMPATIBILITY_TEST_ID]:
        raise ValueError("14586 compatibility shim must bind exactly the F2P test")

    declared_by_id = {
        str(test.get("id") or ""): dict(test)
        for test in declared_tests
        if isinstance(test, Mapping)
    }
    prepared_by_id = {
        str(test.get("id") or ""): dict(test)
        for test in prepared_tests
        if isinstance(test, Mapping)
    }
    if (
        len(declared_by_id) != len(declared_tests)
        or len(prepared_by_id) != len(prepared_tests)
        or set(declared_by_id) != set(prepared_by_id)
    ):
        raise ValueError("14586 compatibility shim test identities drifted")

    base_test = prepared_by_id[_LUANTI_14586_COMPATIBILITY_TEST_ID]
    declared_test = declared_by_id[_LUANTI_14586_COMPATIBILITY_TEST_ID]
    if (
        base_test.get("kind") != "fail_to_pass"
        or base_test.get("upstream_test_path") != _LUANTI_14586_SOURCE_PATH
    ):
        raise ValueError("14586 compatibility shim is not bound to its F2P source")
    raw_sources = base_test.get("frozen_test_sources")
    if not isinstance(raw_sources, list) or len(raw_sources) != 1:
        raise ValueError("14586 compatibility shim requires one frozen F2P source")
    source = raw_sources[0]
    if not isinstance(source, Mapping) or source != {
        "source_path": _LUANTI_14586_CAPSULE_PATH.as_posix(),
        "destination_path": _LUANTI_14586_SOURCE_PATH,
        "sha256": base_test.get("upstream_test_sha256"),
    }:
        raise ValueError("14586 compatibility shim F2P source declaration drifted")
    base_bytes = prepared_capsules.get(_LUANTI_14586_CAPSULE_PATH)
    if base_bytes is None:
        raise ValueError("14586 compatibility shim base capsule is missing")
    base_sha256 = hashlib.sha256(base_bytes).hexdigest()
    if base_test.get("upstream_test_sha256") != base_sha256:
        raise ValueError("14586 compatibility shim base capsule does not match c044")
    if (
        base_bytes.count(_LUANTI_14586_INCLUDE_ANCHOR) != 1
        or _LUANTI_14586_INSERTED_INCLUDE in base_bytes
    ):
        raise ValueError("14586 compatibility shim base source is not canonical")
    shimmed_bytes = base_bytes.replace(
        _LUANTI_14586_INCLUDE_ANCHOR,
        _LUANTI_14586_INCLUDE_ANCHOR + _LUANTI_14586_INSERTED_INCLUDE,
    )
    if shimmed_bytes.replace(_LUANTI_14586_INSERTED_INCLUDE, b"") != base_bytes:
        raise ValueError("14586 compatibility shim is not include-only")
    shimmed_sha256 = hashlib.sha256(shimmed_bytes).hexdigest()
    expected_shim = {
        "schema": _LUANTI_14586_SHIM_SCHEMA,
        "base_fix_commit": _LUANTI_14586_FIX_COMMIT,
        "target_pinned_revision": _LUANTI_14586_PINNED_REVISION,
        "base_source_path": _LUANTI_14586_SOURCE_PATH,
        "base_source_sha256": base_sha256,
        "frozen_source_sha256": shimmed_sha256,
        "insertion_after": _LUANTI_14586_INCLUDE_ANCHOR.decode("utf-8").rstrip(),
        "inserted_line": _LUANTI_14586_INSERTED_INCLUDE.decode("utf-8").rstrip(),
    }
    if declared_test.get("frozen_test_compatibility_shim") != expected_shim:
        raise ValueError(
            "14586 compatibility shim is not the registered include-only bridge"
        )

    expected_test = copy.deepcopy(base_test)
    expected_test["frozen_test_compatibility_shim"] = expected_shim
    expected_test["upstream_test_sha256"] = shimmed_sha256
    expected_files = expected_test.get("upstream_test_files")
    if not isinstance(expected_files, list) or len(expected_files) != 1:
        raise ValueError("14586 compatibility shim upstream source declaration drifted")
    expected_files[0]["sha256"] = shimmed_sha256
    expected_test["frozen_test_sources"] = [
        {
            "source_path": _LUANTI_14586_CAPSULE_PATH.as_posix(),
            "destination_path": _LUANTI_14586_SOURCE_PATH,
            "sha256": shimmed_sha256,
        }
    ]
    for test_id, candidate in declared_by_id.items():
        expected = (
            expected_test
            if test_id == _LUANTI_14586_COMPATIBILITY_TEST_ID
            else prepared_by_id[test_id]
        )
        if candidate != expected:
            raise ValueError(
                "14586 compatibility shim would alter a frozen test declaration beyond "
                "the registered include-only bridge"
            )

    prepared_capsules[_LUANTI_14586_CAPSULE_PATH] = shimmed_bytes
    prepared_tests = [
        expected_test
        if str(test.get("id") or "") == _LUANTI_14586_COMPATIBILITY_TEST_ID
        else test
        for test in prepared_tests
    ]
    return (
        prepared_tests,
        prepared_capsules,
        [
            {
                "test_id": _LUANTI_14586_COMPATIBILITY_TEST_ID,
                "source_path": _LUANTI_14586_CAPSULE_PATH.as_posix(),
                "destination_path": _LUANTI_14586_SOURCE_PATH,
                "base_source_path": _LUANTI_14586_SOURCE_PATH,
                "inserted_line": _LUANTI_14586_INSERTED_INCLUDE.decode(
                    "utf-8"
                ).rstrip(),
            }
        ],
    )


def _assert_clone_relative_sources(
    tests: Sequence[Mapping[str, Any]],
    capsules: Mapping[Path, bytes],
) -> None:
    for index, test in enumerate(tests):
        if str(test.get("kind") or "") not in _CONSTRUCTION_KINDS:
            continue
        label = f"tests[{index}]"
        for key in ("argv", "command"):
            argv = test.get(key)
            if not isinstance(argv, list) or not argv:
                raise ValueError(f"{label}.{key} must be a non-empty argv list")
            if any("{harness_root}/games/luanti/" in str(part) for part in argv):
                raise ValueError(
                    f"{label}.{key} reads a workspace Luanti checkout instead of "
                    "the disposable clone"
                )
        sources = test.get("frozen_test_sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError(f"{label} has no frozen construction test sources")
        for source_index, source in enumerate(sources):
            if not isinstance(source, Mapping):
                raise ValueError(
                    f"{label}.frozen_test_sources[{source_index}] is invalid"
                )
            source_path = _relative_path(
                source.get("source_path"),
                f"{label}.frozen_test_sources[{source_index}].source_path",
            )
            _relative_path(
                source.get("destination_path"),
                f"{label}.frozen_test_sources[{source_index}].destination_path",
            )
            if source_path not in capsules:
                raise ValueError(
                    f"{label}.frozen_test_sources[{source_index}] is not backed by "
                    "a local fix-commit capsule"
                )


def _assert_registered_task_contract(
    task_id: str, tests: Sequence[Mapping[str, Any]]
) -> None:
    """Keep registered Luanti commands bound to their disposable clone."""

    module = _CPP_UNITTEST_MODULES.get(task_id)
    if module is not None:
        fail_to_pass = [
            test for test in tests if str(test.get("kind") or "") == "fail_to_pass"
        ]
        if len(fail_to_pass) != 1:
            raise ValueError("registered Luanti C++ task must have one F2P test")
        test = fail_to_pass[0]
        expected = ["./bin/luanti", "--run-unittests", "--test-module", module]
        if test.get("argv") != expected or test.get("command") != expected:
            raise ValueError(
                "registered Luanti C++ F2P must use clone-local ./bin/luanti"
            )
        if test.get("cwd") != ".":
            raise ValueError("registered Luanti C++ F2P must run from clone root")
        return

    if task_id != "lz-luanti-14396-metadata-set-string-nil":
        return
    fail_to_pass = [
        test for test in tests if str(test.get("kind") or "") == "fail_to_pass"
    ]
    if len(fail_to_pass) != 1:
        raise ValueError("registered Luanti multiplayer task must have one F2P test")
    test = fail_to_pass[0]
    expected = ["./util/test_multiplayer.sh"]
    if test.get("argv") != expected or test.get("command") != expected:
        raise ValueError(
            "registered Luanti multiplayer F2P must use clone-local "
            "./util/test_multiplayer.sh"
        )
    if test.get("cwd") != ".":
        raise ValueError("registered Luanti multiplayer F2P must run from clone root")
    sources = test.get("frozen_test_sources")
    if not isinstance(sources, list) or len(sources) != 1:
        raise ValueError(
            "registered Luanti multiplayer F2P must have one frozen source"
        )
    source = sources[0]
    if not isinstance(source, Mapping) or source.get("destination_path") != (
        "games/devtest/mods/unittests/metadata.lua"
    ):
        raise ValueError("registered Luanti multiplayer F2P must overlay metadata.lua")


def _validate_version_request(
    *,
    workspace: Path,
    task_id: str,
    record: Mapping[str, Any],
) -> None:
    request_path = _contained(
        workspace,
        Path("benchmarks/p5/lazarus/authoring") / f"{task_id}.version-request.json",
        "version request",
    )
    request = _load_mapping(request_path, "version request")
    expected_patch = (
        Path("benchmarks/p5/lazarus/defects") / task_id / "revert.patch"
    ).as_posix()
    if (
        request.get("task_id") != task_id
        or request.get("source_repo") != "games/luanti"
        or request.get("fixed_revision") != record.get("pinned_revision")
        or request.get("revert_patch") != expected_patch
        or request.get("patch_sha256") != record.get("patch_sha256")
        or request.get("build_class") != "incremental_cpp"
        or sorted(str(value) for value in request.get("declared_files", []))
        != sorted(str(value) for value in record.get("touched_files", []))
    ):
        raise ValueError(
            f"registered unit-test version request drifted: {request_path}"
        )
    if task_id not in LUANTI_FORMAL_TASK_IDS:
        raise ValueError(f"no registered Luanti prebuild recipe: {task_id}")
    callback = create_formal_prebuild_callback(task_id)
    fingerprint = str(getattr(callback, "lazarus_build_fingerprint", ""))
    if not _FINGERPRINT.fullmatch(fingerprint):
        raise ValueError(
            f"registered Luanti unit-test prebuild recipe is invalid: {task_id}"
        )


def _write_capsule(evidence_root: Path, relative: Path, content: bytes) -> str:
    """Publish one capsule without following links or replacing drifted bytes."""

    if evidence_root.is_symlink() or not evidence_root.is_dir():
        raise ValueError(
            f"evidence root must be a non-symlink directory: {evidence_root}"
        )
    path = _relative_path(relative.as_posix(), "capsule path")
    parent = evidence_root
    for component in path.parts[:-1]:
        parent = parent / component
        if parent.is_symlink():
            raise ValueError(f"capsule parent is a symlink: {parent}")
        if parent.exists() and not parent.is_dir():
            raise ValueError(f"capsule parent is not a directory: {parent}")
    target = evidence_root / path
    if target.is_symlink():
        raise ValueError(f"capsule target is a symlink: {target}")
    if target.exists():
        if not target.is_file():
            raise ValueError(f"capsule target is not a regular file: {target}")
        if target.read_bytes() != content:
            raise ValueError(
                f"existing capsule conflicts with local fix history: {target}"
            )
        return "reused"
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or target.exists() or target.is_symlink():
        raise ValueError(f"capsule target changed while preparing it: {target}")
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix=".capsule-", dir=parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return "written"


def _prepare_one(
    *,
    workspace: Path,
    task_id: str,
    persist: bool,
) -> dict[str, Any]:
    evidence_root = _contained(
        workspace,
        Path("benchmarks/p5/lazarus/defects") / task_id,
        "evidence root",
    )
    record = _load_mapping(evidence_root / "upstream.json", "upstream record")
    if record.get("task_id") != task_id or record.get("benchmark") != "luanti":
        raise ValueError(f"registered evidence identity drifted: {evidence_root}")
    record_errors = validate_upstream_record(record)
    if record_errors:
        raise ValueError(
            f"registered upstream record is invalid: {evidence_root}: "
            + "; ".join(record_errors)
        )
    tests = record.get("tests")
    if not isinstance(tests, list):
        raise ValueError(f"registered upstream tests are invalid: {evidence_root}")
    authoring_path = _contained(
        workspace,
        Path("benchmarks/p5/lazarus/authoring") / f"{task_id}.tests.json",
        "authoring tests",
    )
    authoring = _load_mapping(authoring_path, "authoring tests")
    authoring_tests = authoring.get("tests")
    if not isinstance(authoring_tests, list):
        raise ValueError(f"registered authoring tests are invalid: {authoring_path}")

    backfilled, capsules = backfill_local_frozen_test_capsules(
        benchmark="luanti",
        repo_path=_contained(workspace, "games/luanti", "Luanti source repository"),
        fix_commit=str(record.get("fix_commit") or ""),
        tests=tests,
    )
    backfilled, capsules, compatibility_shims = (
        _apply_registered_luanti_compatibility_shims(
            task_id=task_id,
            record=record,
            declared_tests=authoring_tests,
            backfilled_tests=backfilled,
            capsules=capsules,
        )
    )
    if backfilled != tests or backfilled != authoring_tests:
        raise ValueError(
            "capsule preparation would alter signed test declarations; "
            "refuse the metadata rewrite"
        )
    _assert_clone_relative_sources(backfilled, capsules)
    _assert_registered_task_contract(task_id, backfilled)
    _validate_version_request(workspace=workspace, task_id=task_id, record=record)

    statuses: dict[str, str] = {}
    for relative, content in sorted(
        capsules.items(), key=lambda item: item[0].as_posix()
    ):
        statuses[relative.as_posix()] = (
            _write_capsule(evidence_root, relative, content) if persist else "planned"
        )
    if persist:
        errors = validate_lazarus_trial_provenance(
            record,
            source_root=_contained(
                workspace, "games/luanti", "Luanti source repository"
            ),
            harness_root=workspace,
            frozen_test_root=evidence_root,
        )
        if errors:
            raise ValueError(
                f"prepared Luanti evidence did not reload: {evidence_root}: "
                + "; ".join(errors)
            )
    return {
        "task_id": task_id,
        "fix_commit": record["fix_commit"],
        "capsules": list(sorted(statuses)),
        "capsule_status": statuses,
        "compatibility_shims": compatibility_shims,
        "clone_relative_sources": True,
        "prebuild_mode": "formal_registered_build",
        "prebuild_artifact": formal_luanti_prebuild_artifact_path(task_id),
        "version_request": str(
            authoring_path.with_name(f"{task_id}.version-request.json").relative_to(
                workspace
            )
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    workspace = args.workspace.expanduser().resolve()
    selected = tuple(args.task_id or DEFAULT_TASK_IDS)
    if not selected or len(selected) != len(set(selected)):
        raise ValueError("selected Luanti task ids must be non-empty and unique")
    unknown = sorted(set(selected) - set(DEFAULT_TASK_IDS))
    if unknown:
        raise ValueError(
            "unregistered Luanti preparation task(s): " + ", ".join(unknown)
        )
    results = [
        _prepare_one(workspace=workspace, task_id=task_id, persist=not args.dry_run)
        for task_id in selected
    ]
    print(
        json.dumps(
            {
                "schema": "c1-lazarus-luanti-curated-preparation-v1",
                "dry_run": bool(args.dry_run),
                "complete": len(results) == len(selected),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
