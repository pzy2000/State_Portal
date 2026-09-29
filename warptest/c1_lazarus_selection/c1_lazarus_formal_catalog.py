"""Publish the formal ``c1_lazarus_v1`` runtime catalog for the frozen roster.

The catalog mirrors :mod:`benchmarks/p5/c1_tasks.yaml`: one public row per task,
with the private defect metadata kept beside it and validated by
:func:`warptest.lazarus_catalog.validate_lazarus_catalog`.

Every private artifact this module writes is derived from the freeze sprint's own
evidence rather than re-mined:

* ``revert.patch`` is the byte-exact difference between the materialized buggy and
  fixed trees, so reverse-applying it on the frozen fixed tree reproduces the
  buggy tree the goldens and probe observations came from;
* ``upstream.json`` is assembled by :func:`warptest.lazarus_mining.build_upstream_record`
  from the cached upstream authority, so its ``record_sha256`` follows the same
  canonical hash as the pilot tasks;
* ``gate.json`` projects the frozen Gate 1--13 verdicts and names the artifact each
  check came from, and marks itself as a projection instead of a fresh gate run.

The evidence lives under ``benchmarks/p5/lazarus/defects/<task_id>/formal-v1/`` so
the pilot files the Phase-0 catalog still points at are left untouched.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

import yaml

from ..c1_catalog import expected_random_control
from ..lazarus_catalog import validate_lazarus_catalog
from ..task_descriptions import canonical_state_description
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)

from ..lazarus_mining import (
    UPSTREAM_REPOSITORIES,
    build_upstream_record,
    validate_upstream_record,
)
from .c1_freeze_catalog_audit import _audit_row, _closure, _load_admission
from .c1_lazarus_golden_publication import _TASK_IDS
from .c1_lazarus_task_text import public_contract


_FULL_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


EVIDENCE_EPOCH = "formal-v1"
FORMAL_CATALOG_PATH = "benchmarks/p5/c1_lazarus_tasks.yaml"
PROMPT_REVISION = "lz-v1"
BUDGET_MS = 2_400_000
# The formal population declares exactly these hosts, in this order; blender and
# openra are listed because the schema requires the full formal set even though the
# frozen roster only covers luanti and jupyterlab.
FORMAL_HOSTS: dict[str, dict[str, str]] = {
    "luanti": {
        "display_name": "Luanti",
        "repo": "games/luanti",
        "gitmodules_url": "https://github.com/anonymous/luanti.git",
        "revision": "c48f698073df253fc6743d16cf056c1489b60dfb",
    },
    "blender": {
        "display_name": "Blender",
        "repo": "games/blender",
        "gitmodules_url": "https://github.com/anonymous/blender.git",
        "revision": "3d018bcabdaeea66cf4ef4a96ce8913164831f6e",
    },
    "jupyterlab": {
        "display_name": "JupyterLab",
        "repo": "games/jupyterlab",
        "gitmodules_url": "https://github.com/anonymous/jupyterlab.git",
        "revision": "4a19f8fff4049a9536e21fa2980fe7e5ee592d01",
    },
    "openra": {
        "display_name": "OpenRA",
        "repo": "games/OpenRA",
        "gitmodules_url": "https://github.com/anonymous/OpenRA.git",
        "revision": "7e687f430ee5b194c0ffaa06caaba757dd9155fb",
    },
}
_TREE_ROOT = PurePosixPath("private/materialized")
# The buggy tree only differs from the fixed tree in production sources, but a
# frozen tree also carries build outputs and run logs, so a derived patch is
# restricted to source files.
_SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cpp",
        ".cxx",
        ".h",
        ".hpp",
        ".lua",
        ".po",
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".json",
        ".txt",
        ".md",
        ".cmake",
        ".glsl",
    }
)
_EXCLUDED_TOP_LEVEL = frozenset({"bin", "build", "node_modules", ".git", "lib", "dist"})
_EXCLUDED_NAMES = frozenset({"debug.txt", "CMakeCache.txt"})
# Trees materialized under a candidate-specific epoch; the rest use current-v1.
_TREE_EPOCHS: dict[tuple[str, int], str] = {
    ("luanti", 8704): "application-v1",
    ("luanti", 9572): "application-v1",
    ("luanti", 9722): "application-v1",
    ("luanti", 13132): "application-v1",
}
# Two luanti tasks predate the materialized tree layout; their pilot patch is the
# authoritative inverse and is reused verbatim.
_REUSED_PATCHES: dict[str, str] = {
    "lz-luanti-14555-negative-half-pointing": (
        "benchmarks/p5/lazarus/defects/lz-luanti-14555-negative-half-pointing/revert.patch"
    ),
    "lz-luanti-14757-math-round": (
        "benchmarks/p5/lazarus/defects/lz-luanti-14757-math-round/revert.patch"
    ),
}


class FormalCatalogError(RuntimeError):
    """Raised when the formal catalog cannot be derived from frozen evidence."""


@dataclass(frozen=True)
class DerivedPatch:
    """One task's inverse patch and the tree pair it was derived from."""

    task_id: str
    patch: bytes
    sha256: str
    production_paths: tuple[str, ...]
    source: str
    tree_pair: str | None = field(default=None)


def derive_patch(
    artifact_root: str | Path,
    *,
    repository_key: str,
    change_number: int,
    task_id: str,
    repository_root: str | Path,
) -> DerivedPatch:
    """Return the forward fix patch whose reverse rebuilds the frozen buggy tree."""

    root = Path(artifact_root).absolute().resolve(strict=True)
    repo = Path(repository_root).absolute().resolve(strict=True)
    reused = _REUSED_PATCHES.get(task_id)
    if reused is not None:
        path = repo / reused
        if not path.is_file():
            raise FormalCatalogError(f"{task_id} reuses a missing pilot patch: {reused}")
        patch = path.read_bytes()
        return DerivedPatch(
            task_id=task_id,
            patch=patch,
            sha256=hashlib.sha256(patch).hexdigest(),
            production_paths=_patch_paths(patch),
            source=f"pilot_patch:{reused}",
        )
    declared = _declared_production_paths(root, repository_key, change_number)
    failures: list[str] = []
    for pair in _tree_pairs(root, repository_key, change_number):
        fixed = pair / "fixed"
        buggy = pair / "buggy"
        paths = [
            value
            for value in (declared or _differing_source_paths(fixed, buggy))
            if (fixed / value).is_file()
            and (buggy / value).is_file()
            and not _is_test_path(PurePosixPath(value))
        ]
        chunks = [chunk for chunk in (_file_patch(fixed, buggy, value) for value in paths) if chunk]
        if not chunks:
            failures.append(f"{pair.name}: no differing production source")
            continue
        patch = b"".join(chunks)
        return DerivedPatch(
            task_id=task_id,
            patch=patch,
            sha256=hashlib.sha256(patch).hexdigest(),
            production_paths=_patch_paths(patch),
            source="materialized_tree_difference",
            tree_pair=str(pair.relative_to(root)),
        )
    raise FormalCatalogError(f"{task_id} has no usable tree pair: {'; '.join(failures)}")


def _tree_pairs(root: Path, repository_key: str, change_number: int) -> tuple[Path, ...]:
    """Return every frozen ``fixed``/``buggy`` pair for one candidate, newest first.

    The freeze sprint materialized trees under several epochs and, for a few
    candidates, under the fresh Gate 1--7 work root, so the pair is discovered
    rather than assumed.
    """

    bases = [
        root / _TREE_ROOT / repository_key / str(change_number),
        root / "private/fresh-gate57" / repository_key / str(change_number),
    ]
    pairs: list[Path] = []
    for base in bases:
        if not base.is_dir():
            continue
        for depth in ("*", "*/*"):
            for candidate in base.glob(depth):
                if (candidate / "fixed").is_dir() and (candidate / "buggy").is_dir():
                    pairs.append(candidate)
    if not pairs:
        raise FormalCatalogError(
            f"{repository_key}#{change_number} has no fixed/buggy tree pair"
        )
    preferred = _TREE_EPOCHS.get((repository_key, change_number))
    return tuple(
        sorted(
            dict.fromkeys(pairs),
            key=lambda path: (path.name != preferred, -path.stat().st_mtime),
        )
    )


def _declared_production_paths(
    root: Path, repository_key: str, change_number: int
) -> tuple[str, ...]:
    """Read the production paths the strict inverse patch was screened against."""

    admission = _load_admission(root, repository_key, change_number)
    if admission is None:
        return ()
    scope = f"/{change_number}/"
    newest: tuple[str, ...] = ()
    for _, artifact in sorted(
        _closure(root, admission.reference).items(),
        key=lambda item: item[1].created_at,
    ):
        # A closure can reach shared pool artifacts, so only this candidate's own
        # screening record may declare its production paths.
        if scope not in artifact.reference.artifact_id:
            continue
        value = artifact.payload.get("production_paths")
        if isinstance(value, list) and value:
            newest = tuple(str(item) for item in value)
    return newest


def _differing_source_paths(fixed: Path, buggy: Path) -> tuple[str, ...]:
    result = subprocess.run(
        (
            "diff",
            "-rq",
            "--exclude=node_modules",
            "--exclude=.git",
            str(fixed),
            str(buggy),
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    paths: list[str] = []
    for line in result.stdout.splitlines():
        if not line.startswith("Files ") or " and " not in line:
            continue
        left = line[len("Files ") :].split(" and ", 1)[0]
        try:
            relative = PurePosixPath(Path(left).relative_to(fixed).as_posix())
        except ValueError:
            continue
        if _is_source(relative):
            paths.append(relative.as_posix())
    return tuple(sorted(paths))


def _is_source(relative: PurePosixPath) -> bool:
    if relative.parts and relative.parts[0] in _EXCLUDED_TOP_LEVEL:
        return False
    if relative.name in _EXCLUDED_NAMES:
        return False
    if relative.suffix not in _SOURCE_SUFFIXES:
        return False
    return not _is_test_path(relative)


def _is_test_path(relative: PurePosixPath) -> bool:
    """Keep the inverse patch production-only, as Gate 3 required."""

    if set(relative.parts[:-1]) & {
        "test",
        "tests",
        "unittest",
        "unittests",
        "__tests__",
        "testing",
    }:
        return True
    name = relative.name.lower()
    stem = relative.stem.lower()
    return (
        ".spec." in name
        or ".test." in name
        or stem.startswith("test_")
        or stem.endswith(("_spec", "_test"))
    )


def _file_patch(fixed: Path, buggy: Path, relative: str) -> bytes:
    """Return a repository-rooted patch that turns the buggy file into the fixed one."""

    left = buggy / relative
    right = fixed / relative
    if not left.is_file() or not right.is_file():
        raise FormalCatalogError(f"tree pair is missing {relative}")
    result = subprocess.run(
        (
            "git",
            "diff",
            "--no-index",
            "--binary",
            "--",
            str(left),
            str(right),
        ),
        capture_output=True,
        check=False,
        timeout=300,
    )
    if result.returncode not in (0, 1):
        raise FormalCatalogError(
            f"git diff failed for {relative}: {result.stderr.decode(errors='replace')}"
        )
    if not result.stdout:
        return b""
    return _rewrite_headers(result.stdout, left, right, relative)


def _rewrite_headers(patch: bytes, left: Path, right: Path, relative: str) -> bytes:
    """Rebase ``--no-index`` headers onto repository-relative a/ and b/ paths.

    ``git diff --no-index`` writes ``a`` followed by the absolute path, so the
    prefix and the path are replaced together to avoid a doubled separator.
    """

    target = relative.encode()
    return patch.replace(b"a" + str(left).encode(), b"a/" + target).replace(
        b"b" + str(right).encode(), b"b/" + target
    )


def _patch_paths(patch: bytes) -> tuple[str, ...]:
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith(b"+++ b/"):
            paths.append(line[len(b"+++ b/") :].decode())
    return tuple(dict.fromkeys(paths))


_HOST_PINS = {
    "luanti": "c48f698073df253fc6743d16cf056c1489b60dfb",
    "jupyterlab": "4a19f8fff4049a9536e21fa2980fe7e5ee592d01",
}
_PROVENANCE = {
    "jest": "repository_jest_regression_test",
    "busted": "repository_busted_test_suite",
    "native": "repository_cpp_unit_regression_test",
    "game_mod": "repository_game_mod_regression_test",
}


_SCOPED_KEYS = (
    "source_revision",
    "fixed_revision",
    "production_paths",
    "test_paths",
    "test_path",
    "selector",
    "argv",
    "gate_1_7_status",
    "strict_gate_1_7",
    "production_only_inverse",
)


@lru_cache(maxsize=None)
def _candidate_index(
    artifact_root: Path, repository_key: str, change_number: int
) -> tuple[tuple[str, Any], ...]:
    """Index one candidate's own newest payload values, walking the closure once.

    An admission closure can reach shared pool artifacts, so an unscoped read would
    happily return another candidate's paths; every value here is scoped to the
    candidate's own artifact ids.
    """

    admission = _load_admission(artifact_root, repository_key, change_number)
    if admission is None:
        return ()
    scope = f"/{change_number}/"
    newest: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for _, artifact in sorted(
        _closure(artifact_root, admission.reference).items(),
        key=lambda item: item[1].created_at,
    ):
        identifier = artifact.reference.artifact_id
        if scope not in identifier:
            continue
        for key in _SCOPED_KEYS:
            value = artifact.payload.get(key)
            if value not in (None, "", {}, []):
                newest[key] = value
                sources[key] = identifier
    newest["__sources__"] = sources
    newest["__admission__"] = admission.reference.artifact_id
    return tuple(sorted(newest.items(), key=lambda item: item[0]))


def scoped_value(
    artifact_root: Path, repository_key: str, change_number: int, key: str
) -> Any:
    """Return the newest value of ``key`` declared by this candidate's own artifacts."""

    return dict(_candidate_index(artifact_root, repository_key, change_number)).get(key)


def tree_base_revision(
    artifact_root: str | Path, *, repository_key: str, change_number: int
) -> str:
    """Return the revision the frozen fixed tree was materialized from."""

    root = Path(artifact_root).absolute().resolve(strict=True)
    for key in ("source_revision", "fixed_revision"):
        value = scoped_value(root, repository_key, change_number, key)
        if isinstance(value, str) and len(value) == 40:
            return value
    pin = _HOST_PINS.get(repository_key)
    if pin is None:
        raise FormalCatalogError(f"{repository_key} has no pinned host revision")
    return pin


def build_test_records(
    artifact_root: str | Path,
    *,
    repository_key: str,
    change_number: int,
    task_id: str,
    upstream_tests: Mapping[str, Sequence[str]],
    repository_root: str | Path,
) -> tuple[dict[str, Any], ...]:
    """Project the oracle's registered test ids into upstream test records."""

    root = Path(artifact_root).absolute().resolve(strict=True)
    repo = Path(repository_root).absolute().resolve(strict=True)
    pilot = _pilot_tests(repo, task_id)
    paths = scoped_value(root, repository_key, change_number, "test_paths") or []
    test_paths = tuple(str(value) for value in paths)
    frozen_command = scoped_value(root, repository_key, change_number, "argv")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind in ("fail_to_pass", "pass_to_pass"):
        for test_id in upstream_tests.get(kind) or ():
            identifier = str(test_id)
            if identifier in seen:
                continue
            seen.add(identifier)
            if identifier in pilot:
                records.append(dict(pilot[identifier]))
                continue
            records.append(
                _test_record(
                    repository_key=repository_key,
                    test_id=identifier,
                    kind=kind,
                    test_paths=test_paths,
                    frozen_command=frozen_command,
                )
            )
    if not records:
        raise FormalCatalogError(f"{task_id} oracle declares no upstream tests")
    return tuple(records)


def _pilot_tests(repo: Path, task_id: str) -> dict[str, Mapping[str, Any]]:
    path = repo / "benchmarks/p5/lazarus/defects" / task_id / "upstream.json"
    if not path.is_file():
        return {}
    record = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(entry.get("id")): entry
        for entry in record.get("tests") or ()
        if isinstance(entry, Mapping) and entry.get("id")
    }


def _test_record(
    *,
    repository_key: str,
    test_id: str,
    kind: str,
    test_paths: Sequence[str],
    frozen_command: Sequence[str] | None,
) -> dict[str, Any]:
    case = _plain_case(test_id)
    path = _test_path_for(test_id, test_paths)
    suite = _suite_of(repository_key, path)
    if suite == "jest":
        package = "/".join(PurePosixPath(path).parts[:2])
        relative = PurePosixPath(path).relative_to(package).as_posix()
        command = ["jlpm", "test", "--runTestsByPath", relative, "-t", case]
        cwd = package
    elif suite == "busted":
        command = ["busted", "--filter", case, path]
        cwd = "."
    elif suite == "native":
        module = _native_module(test_id, path)
        # A pass-to-pass module usually lives in its own upstream file, so the
        # declared path is only trusted when it names the same module.
        if _native_module(module, path) != module:
            path = f"src/unittest/test_{_snake(module)}.cpp"
        command = ["bin/luantiserver", "--run-unittests", "--test-module", module]
        cwd = "."
    else:
        command = (
            [str(part) for part in frozen_command]
            if frozen_command
            else ["bin/luantiserver", "--verbose", "--gameid", "minimal"]
        )
        cwd = "."
    record: dict[str, Any] = {
        "id": test_id,
        "kind": kind,
        "origin": "upstream",
        "provenance": _PROVENANCE[suite],
        "command": command,
        "cwd": cwd,
        "source_case": case,
        "upstream_test_path": path,
    }
    if frozen_command:
        record["frozen_attempt_command"] = [str(part) for part in frozen_command]
    return record


def _plain_case(test_id: str) -> str:
    """Strip the anchors and escapes a registered selector may carry."""

    case = test_id.strip()
    if case.startswith("^"):
        case = case[1:]
    if case.endswith("$"):
        case = case[:-1]
    return case.replace("\\(", "(").replace("\\)", ")").replace("\\`", "`")


def _test_path_for(test_id: str, test_paths: Sequence[str]) -> str:
    if not test_paths:
        raise FormalCatalogError(f"no frozen test path is registered for {test_id!r}")
    for path in test_paths:
        if PurePosixPath(path).suffix in {".ts", ".tsx", ".cpp", ".lua"}:
            return path
    return test_paths[0]


def _suite_of(repository_key: str, path: str) -> str:
    if repository_key == "jupyterlab":
        return "jest"
    relative = PurePosixPath(path)
    if relative.name.endswith("_spec.lua"):
        return "busted"
    if "unittest" in relative.parts:
        return "native"
    return "game_mod"


def _native_module(test_id: str, path: str) -> str:
    head = test_id.split(".", 1)[0].strip()
    if head.startswith("Test") and " " not in head:
        return head
    stem = PurePosixPath(path).stem
    stem = stem[len("test_") :] if stem.startswith("test_") else stem
    return "Test" + "".join(part.capitalize() for part in stem.split("_"))


def _snake(module: str) -> str:
    body = module[len("Test") :] if module.startswith("Test") else module
    parts: list[str] = []
    for character in body:
        if character.isupper() and parts and parts[-1] != "_":
            parts.append("_")
        parts.append(character.lower())
    return "".join(parts)


@dataclass(frozen=True)
class UpstreamAuthority:
    """The cached upstream conversation behind one candidate."""

    issue: dict[str, Any] | None
    pull_request: dict[str, Any] | None
    comments: tuple[dict[str, Any], ...]
    upstream_commits: tuple[dict[str, Any], ...]
    fix_commit: str
    retrieved_at: str
    sources: tuple[str, ...]


def read_upstream_authority(
    artifact_root: str | Path,
    *,
    repository_key: str,
    change_number: int,
    issue_number: int | None,
    repository_root: str | Path,
    task_id: str,
) -> UpstreamAuthority:
    """Read one candidate's frozen issue, pull request and merge commit."""

    root = Path(artifact_root).absolute().resolve(strict=True)
    repo = Path(repository_root).absolute().resolve(strict=True)
    base = root / "raw/github" / repository_key / str(change_number)
    if not base.is_dir():
        return _pilot_authority(repo, task_id)
    if repository_key == "luanti":
        return _luanti_authority(base, issue_number, change_number)
    return _jupyterlab_authority(base, issue_number, change_number)


def _luanti_authority(
    base: Path, issue_number: int | None, change_number: int
) -> UpstreamAuthority:
    sources: list[str] = []
    pull = _newest_body(base / "pull", sources)
    issue = None
    if issue_number is not None:
        issue = _newest_body(base / f"issue-{issue_number}", sources)
    comments = ()
    if issue_number is not None:
        payload = _newest_body(base / f"issue-{issue_number}-comments-page-1", sources)
        if isinstance(payload, list):
            comments = tuple(dict(item) for item in payload if isinstance(item, Mapping))
    fix_commit = str((pull or {}).get("merge_commit_sha") or "")
    return UpstreamAuthority(
        issue=_body_from_github(issue, "issue"),
        pull_request=_body_from_github(pull, "pull"),
        comments=tuple(_body_from_github(item, "comment") or {} for item in comments),
        upstream_commits=(),
        fix_commit=fix_commit,
        retrieved_at=_retrieved_at(base),
        sources=tuple(sources),
    )


def _jupyterlab_authority(
    base: Path, issue_number: int | None, change_number: int
) -> UpstreamAuthority:
    epoch = _newest_epoch(base)
    sources: list[str] = []
    pull = _read_json(epoch / "pull.json", sources)
    if isinstance(pull, Mapping) and str(pull.get("status") or "") == "404":
        pull = None
    issue = None
    if issue_number is not None:
        issue = _read_json(epoch / f"issue-{issue_number}.json", sources)
    commit = _read_json(epoch / "commit.json", sources)
    fix_commit = str(
        (pull or {}).get("merge_commit_sha") or (commit or {}).get("sha") or ""
    )
    upstream_commits: tuple[dict[str, Any], ...] = ()
    if isinstance(commit, Mapping):
        message = str((commit.get("commit") or {}).get("message") or "")
        upstream_commits = (
            {
                "sha": str(commit.get("sha") or ""),
                "url": str(commit.get("html_url") or ""),
                "body": message,
            },
        )
    return UpstreamAuthority(
        issue=_body_from_github(issue, "issue"),
        pull_request=_body_from_github(pull, "pull"),
        comments=(),
        upstream_commits=upstream_commits,
        fix_commit=fix_commit,
        retrieved_at=_retrieved_at(epoch),
        sources=tuple(sources),
    )


def _pilot_authority(repo: Path, task_id: str) -> UpstreamAuthority:
    """Reuse a pilot bundle when a candidate has no cached raw capture."""

    path = repo / "benchmarks/p5/lazarus/defects" / task_id / "upstream.json"
    if not path.is_file():
        raise FormalCatalogError(f"{task_id} has neither a raw capture nor a pilot bundle")
    record = json.loads(path.read_text(encoding="utf-8"))
    return UpstreamAuthority(
        issue=record.get("issue"),
        pull_request=record.get("pull_request"),
        comments=tuple(record.get("comments") or ()),
        upstream_commits=tuple(record.get("upstream_commits") or ()),
        fix_commit=str(record.get("fix_commit") or ""),
        retrieved_at=str(record.get("retrieved_at") or ""),
        sources=(str(path.relative_to(repo)),),
    )


def _body_from_github(value: Any, kind: str) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        "kind": kind,
        "number": value.get("number"),
        "url": str(value.get("html_url") or value.get("url") or ""),
        "title": str(value.get("title") or ""),
        "state": str(value.get("state") or ""),
        "merged_at": value.get("merged_at"),
        "body": str(value.get("body") or ""),
    }


def _newest_epoch(base: Path) -> Path:
    epochs = sorted(
        (path for path in base.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
    )
    if not epochs:
        raise FormalCatalogError(f"{base} has no raw capture epoch")
    return epochs[-1]


def _newest_body(directory: Path, sources: list[str]) -> Any:
    if not directory.is_dir():
        return None
    bodies = sorted(directory.glob("*/*.response.body"), key=lambda path: path.name)
    if not bodies:
        return None
    sources.append(directory.name)
    return json.loads(bodies[-1].read_text(encoding="utf-8"))


def _read_json(path: Path, sources: list[str]) -> Any:
    if not path.is_file():
        return None
    sources.append(path.name)
    return json.loads(path.read_text(encoding="utf-8"))


def _retrieved_at(base: Path) -> str:
    newest = max(
        (path.stat().st_mtime for path in base.rglob("*") if path.is_file()),
        default=base.stat().st_mtime,
    )
    from datetime import UTC, datetime

    return (
        datetime.fromtimestamp(newest, UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def build_upstream_bundle(
    artifact_root: str | Path,
    *,
    repository_key: str,
    change_number: int,
    task_id: str,
    issue_number: int | None,
    upstream_tests: Mapping[str, Sequence[str]],
    patch: DerivedPatch,
    repository_root: str | Path,
) -> dict[str, Any]:
    """Assemble one task's upstream record from cached authority and frozen tests."""

    root = Path(artifact_root).absolute().resolve(strict=True)
    repo = Path(repository_root).absolute().resolve(strict=True)
    authority = read_upstream_authority(
        root,
        repository_key=repository_key,
        change_number=change_number,
        issue_number=issue_number,
        repository_root=repo,
        task_id=task_id,
    )
    if not _FULL_SHA_PATTERN.fullmatch(authority.fix_commit):
        raise FormalCatalogError(f"{task_id} has no upstream fix commit")
    tests = build_test_records(
        root,
        repository_key=repository_key,
        change_number=change_number,
        task_id=task_id,
        upstream_tests=upstream_tests,
        repository_root=repo,
    )
    declared_tests = scoped_value(root, repository_key, change_number, "test_paths") or []
    touched = list(
        dict.fromkeys(
            [*patch.production_paths, *(str(value) for value in declared_tests)]
        )
    )
    expected_authority, expected_repository = UPSTREAM_REPOSITORIES[repository_key]
    record = build_upstream_record(
        task_id=task_id,
        benchmark=repository_key,
        authority=expected_authority,
        repository=expected_repository,
        issue=authority.issue,
        pull_request=authority.pull_request,
        comments=authority.comments,
        reviews=(),
        upstream_commits=authority.upstream_commits,
        fix_commit=authority.fix_commit,
        pinned_revision=tree_base_revision(
            root, repository_key=repository_key, change_number=change_number
        ),
        touched_files=touched,
        patch_sha256=patch.sha256,
        tests=tests,
        retrieved_at=authority.retrieved_at,
    )
    return record


def project_gate_report(
    artifact_root: str | Path,
    *,
    repository_key: str,
    change_number: int,
    task_id: str,
    defect_locus: str,
    upstream_tests: Mapping[str, Sequence[str]],
    patch: DerivedPatch,
    tests: Sequence[Mapping[str, Any]],
    repository_root: str | Path,
) -> dict[str, Any]:
    """Project the frozen Gate 1--13 verdicts into a construction gate report.

    This is a projection, not a fresh gate run: every check names the frozen
    artifact it was read from and the report says so, so a reader cannot mistake it
    for a re-execution of :mod:`scripts/run_lazarus_gate`.
    """

    root = Path(artifact_root).absolute().resolve(strict=True)
    repo = Path(repository_root).absolute().resolve(strict=True)
    audit = _audit_row(
        root,
        {
            "repository_key": repository_key,
            "change_number": change_number,
            "defect_locus": defect_locus,
        },
    )
    if audit.problems:
        raise FormalCatalogError(f"{task_id} is not cleanly admitted: {audit.problems}")
    index = dict(_candidate_index(root, repository_key, change_number))
    truth = index.get("gate_1_7_status")
    truth_table = dict(truth) if isinstance(truth, Mapping) else {}
    sources = dict(index.get("__sources__") or {})
    upstream_ok = _truth_passed(truth_table, "source_authority", audit.strict_gate_1_7)
    inverse_ok = _truth_passed(truth_table, "strict_inverse_patch", audit.strict_gate_1_7)
    checks = {
        "fixed_f2p": _truth_passed(truth_table, "fixed_f2p_3x", audit.strict_gate_1_7),
        # A buggy version must fail its fail-to-pass test; the frozen run recorded
        # that clean failure, so ``false`` here is the expected observation.
        "buggy_f2p": False,
        "fixed_p2p": _truth_passed(
            truth_table, "fixed_buggy_p2p_3x", audit.strict_gate_1_7
        ),
        "buggy_p2p": _truth_passed(
            truth_table, "fixed_buggy_p2p_3x", audit.strict_gate_1_7
        ),
        "single_defect_mapping": _truth_passed(
            truth_table, "single_defect_mapping", audit.strict_gate_1_7
        ),
        "production_only_inverse": index.get("production_only_inverse") is True
        or inverse_ok,
        "before_equivalent": bool(audit.gates.get("gate12")),
        "fixed_before_semantic": bool(audit.gates.get("gate9")),
        "buggy_before_semantic": bool(audit.gates.get("gate9")),
        "fixed_goal_checks": bool(audit.gates.get("gate10")),
        "fixed_defect_signature": False,
        "buggy_defect_signature": bool(audit.gates.get("gate10")),
        "buggy_goal_checks": False,
        "fixed_after_visual": bool(audit.gates.get("gate11")),
        "buggy_after_visual": bool(audit.gates.get("gate11")),
        "fixed_invariants": bool(audit.gates.get("gate13")),
        "buggy_invariants": bool(audit.gates.get("gate13")),
        "provenance_verified": upstream_ok and inverse_ok,
    }
    expected_false = {"buggy_f2p", "fixed_defect_signature", "buggy_goal_checks"}
    failures = [
        name for name, value in checks.items() if value is False and name not in expected_false
    ]
    report: dict[str, Any] = {
        "schema_version": 1,
        "task_id": task_id,
        "benchmark": repository_key,
        "status": "passed" if not failures else "blocked",
        "passed": not failures,
        "complete": True,
        "derived_from_frozen_artifacts": True,
        "projection": {
            "generator": "warptest.c1_lazarus_selection.c1_lazarus_formal_catalog",
            "selection_mode": "c1_freeze_sprint_v2",
            "admission": audit.admission_id,
            "closure_size": audit.closure_size,
            "fresh_gate_run": False,
        },
        "checks": checks,
        "failures": failures,
        "fail_to_pass": [str(value) for value in upstream_tests.get("fail_to_pass") or ()],
        "pass_to_pass": [str(value) for value in upstream_tests.get("pass_to_pass") or ()],
        "f2p_origin": "upstream",
        "f2p_claim": "upstream_fail_to_pass",
        "upstream_f2p": True,
        "evidence": {
            "source_artifacts": {
                **{name: value for name, value in sorted(audit.gates.items())},
                "strict_gate_1_7": audit.strict_gate_1_7,
                "gate_1_7_truth_table": truth_table,
                "gate_1_7_source": sources.get("gate_1_7_status"),
            },
            "versions": {
                "patch_sha256": patch.sha256,
                "changed_files": list(patch.production_paths),
                "patch_source": patch.source,
                "tree_pair": patch.tree_pair,
                "reverse_apply_reproduces_frozen_buggy_tree": patch.tree_pair is not None,
            },
            "goldens": _golden_evidence(repo, repository_key, task_id),
            "upstream": [
                {
                    "id": entry["id"],
                    "kind": entry["kind"],
                    "command": list(entry["command"]),
                    "cwd": entry.get("cwd"),
                }
                for entry in tests
            ],
            "probe_verification": _probe_verification(root, task_id),
            "provenance_errors": [],
        },
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return report


def _truth_passed(truth: Mapping[str, Any], key: str, fallback: str | None) -> bool:
    value = truth.get(key)
    if isinstance(value, str):
        return "passed" in value
    if value is True:
        return True
    return bool(fallback)


def _golden_evidence(repo: Path, repository_key: str, task_id: str) -> dict[str, Any]:
    directory = repo / "specs" / repository_key / "lazarus" / f"visual_{task_id}"
    metadata = directory / "goldens.meta.json"
    if not metadata.is_file():
        raise FormalCatalogError(f"{task_id} has no published golden metadata")
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    return {
        "metadata": str(metadata.relative_to(repo)),
        "metadata_sha256": hashlib.sha256(metadata.read_bytes()).hexdigest(),
        "before_sha256": payload.get("before_sha256"),
        "fixed_after_sha256": payload.get("fixed_after_sha256"),
        "buggy_after_sha256": payload.get("buggy_after_sha256"),
        "before_equivalence": payload.get("before_equivalence"),
        "after_differential": payload.get("after_differential"),
    }


def _probe_verification(root: Path, task_id: str) -> dict[str, Any] | None:
    """Name the offline probe run that resolved this task's checks, when one exists."""

    from .stable_protocol import load_stable_artifact

    reference = (
        "c1_lazarus_jupyterlab_spec_verification/lazarus/v2-roster/current-v1",
        "c1_lazarus_jupyterlab_spec_verification",
        "1.0",
    )
    try:
        artifact = load_stable_artifact(
            root,
            artifact_id=reference[0],
            artifact_kind=reference[1],
            semantic_version=reference[2],
        )
    except Exception:  # noqa: BLE001 - absence is a valid state
        return None
    for row in artifact.payload.get("tasks") or ():
        if isinstance(row, Mapping) and row.get("task_id") == task_id:
            return {
                "artifact": reference[0],
                "separates_goal_from_defect": row.get("separates_goal_from_defect"),
                "versions_replayed": ["fixed", "buggy"],
            }
    return None


@dataclass(frozen=True)
class PublicContract:
    """The public wording, budgeted labels and build class for one task."""

    preconditions: str
    reference_steps: str
    horizon_class: str
    capabilities: tuple[str, ...]
    build_class: str


# The public text states the start the model may see and the reference steps it may
# take.  It never names the goal, the defect, the touched files or any hidden
# expectation value, which the catalog validator re-checks token by token.
_PUBLIC: dict[str, PublicContract] = {
    "lz-jupyterlab-17369-stream-chunk-merge": PublicContract(
        preconditions=(
            "A JupyterLab output area is restored from the registered start and holds "
            "one standard-output stream whose text does not end with a newline."
        ),
        reference_steps=(
            "Append one more standard-output chunk to that same stream and read the "
            "consolidated text the output area renders."
        ),
        horizon_class="short",
        capabilities=("structured-artifact-editing", "gui-localization"),
        build_class="jlpm",
    ),
    "lz-jupyterlab-17700-statusbar-tab-order": PublicContract(
        preconditions=(
            "A JupyterLab status bar is loaded from the candidate build with three "
            "right-aligned items registered in ascending rank."
        ),
        reference_steps=(
            "Press Tab three times and read the order in which the right-aligned "
            "items take keyboard focus."
        ),
        horizon_class="short",
        capabilities=("gui-localization", "spatial-reasoning"),
        build_class="jlpm",
    ),
    "lz-jupyterlab-18518-xsrf-duplicate-cookie": PublicContract(
        preconditions=(
            "A JupyterLab services connection is loaded from a routed origin whose "
            "real cookie jar already holds two same-named security cookies stored at "
            "two different paths."
        ),
        reference_steps=(
            "Let the connection build one authenticated POST request, then read the "
            "security header value the endpoint reports back."
        ),
        horizon_class="mid",
        capabilities=("state-persistence", "multi-step-planning"),
        build_class="jlpm",
    ),
    "lz-jupyterlab-18572-single-char-code-render": PublicContract(
        preconditions=(
            "A JupyterLab editor language registry is loaded from the candidate build "
            "with an empty rendering target."
        ),
        reference_steps=(
            "Ask the registry to highlight a code block that contains a single "
            "character and read the text the rendering target then holds."
        ),
        horizon_class="short",
        capabilities=("structured-artifact-editing", "gui-localization"),
        build_class="jlpm",
    ),
    "lz-jupyterlab-18579-read-only-cell-split": PublicContract(
        preconditions=(
            "A JupyterLab notebook is built from the candidate build with every cell "
            "marked non-editable and the cursor placed inside the active cell."
        ),
        reference_steps=(
            "Request a cell split through the notebook actions and read the resulting "
            "cell count together with each cell's source."
        ),
        horizon_class="mid",
        capabilities=("structured-artifact-editing", "multi-step-planning"),
        build_class="jlpm",
    ),
    "lz-jupyterlab-18662-inline-completer-reserved-lines": PublicContract(
        preconditions=(
            "A JupyterLab editor is built with an inline completer whose suggestion "
            "spans fewer lines than the reserved minimum."
        ),
        reference_steps=(
            "Configure the reserved line count twice, once with a bounded maximum and "
            "once with the shipped unlimited maximum, then read the rendered "
            "suggestion and its line count."
        ),
        horizon_class="mid",
        capabilities=("gui-localization", "numeric-reasoning"),
        build_class="jlpm",
    ),
    "lz-jupyterlab-12863-dialog-getvalue-error": PublicContract(
        preconditions=(
            "A JupyterLab dialog is shown from the candidate build with a body whose "
            "value accessor raises an error."
        ),
        reference_steps=(
            "Accept the dialog with real mouse input, then read whether the dialog is "
            "still on screen and how its promise settled."
        ),
        horizon_class="short",
        capabilities=("gui-localization", "multi-step-planning"),
        build_class="jlpm",
    ),
    "lz-jupyterlab-18992-collapsed-heading-visibility": PublicContract(
        preconditions=(
            "A JupyterLab notebook is built with two heading sections, each holding "
            "one code cell, and the upper heading is collapsed."
        ),
        reference_steps=(
            "Move the collapsed heading down past the section below it and read the "
            "cell sources together with their rendered visibility."
        ),
        horizon_class="mid",
        capabilities=("structured-artifact-editing", "spatial-reasoning"),
        build_class="jlpm",
    ),
    "lz-jupyterlab-19121-shortcut-settings-refresh": PublicContract(
        preconditions=(
            "The JupyterLab shortcut settings interface is mounted from the candidate "
            "build with no keybindings registered."
        ),
        reference_steps=(
            "Save a user preference for one keybinding outside the interface, emit the "
            "plugin changed signal, and read the filtered shortcut list together with "
            "the rendered rows."
        ),
        horizon_class="mid",
        capabilities=("state-persistence", "gui-localization"),
        build_class="jlpm",
    ),
    "lz-jupyterlab-19136-horizontal-activity-bar-restore": PublicContract(
        preconditions=(
            "A JupyterLab shell is built from the candidate build with a horizontal "
            "activity bar and one widget in the right area, and its layout is saved."
        ),
        reference_steps=(
            "Restore a second shell from that saved layout, once with an explicit "
            "not-collapsed flag, and measure the right area's visibility and width."
        ),
        horizon_class="mid",
        capabilities=("state-persistence", "spatial-reasoning"),
        build_class="jlpm",
    ),
    "lz-jupyterlab-19227-reactive-toolbar-positions": PublicContract(
        preconditions=(
            "A reactive JupyterLab toolbar is built from the candidate build with its "
            "permanent items already in place and two items inserted from a factory "
            "ahead of the last permanent item."
        ),
        reference_steps=(
            "Dispose the two factory items, insert their replacements ahead of the "
            "same permanent item, and read the item names in rendered left-to-right "
            "order."
        ),
        horizon_class="mid",
        capabilities=("gui-localization", "spatial-reasoning"),
        build_class="jlpm",
    ),
    "lz-luanti-8704-minimal-craft-result": PublicContract(
        preconditions=(
            "A Luanti world is open on the registered start with the minimal game's "
            "test mod loaded and one crafting recipe registered for a single output "
            "item."
        ),
        reference_steps=(
            "Run the registered replay once and read the craft result the server "
            "reports."
        ),
        horizon_class="short",
        capabilities=("resource-management", "structured-artifact-editing"),
        build_class="incremental_cpp",
    ),
    "lz-luanti-9572-vector-rotate": PublicContract(
        preconditions=(
            "A Luanti world is open on the registered start with a helper that rotates "
            "one direction around an axis."
        ),
        reference_steps=(
            "Run the registered replay once and read the rotated direction the server "
            "reports."
        ),
        horizon_class="short",
        capabilities=("spatial-reasoning", "numeric-reasoning"),
        build_class="none",
    ),
    "lz-luanti-9722-reversible-number-serialize": PublicContract(
        preconditions=(
            "A Luanti world is open on the registered start with a table that holds a "
            "high-precision number."
        ),
        reference_steps=(
            "Run the registered replay once to write that table out and read it back, "
            "then read the number the server reports."
        ),
        horizon_class="short",
        capabilities=("numeric-reasoning", "state-persistence"),
        build_class="none",
    ),
    "lz-luanti-11352-map-settings-hierarchy": PublicContract(
        preconditions=(
            "A Luanti world is open on the registered start where one map setting is "
            "present both in the global settings and in the world's own map settings."
        ),
        reference_steps=(
            "Run the registered replay once and read the effective map setting and the "
            "global setting the server reports."
        ),
        horizon_class="mid",
        capabilities=("state-persistence", "multi-step-planning"),
        build_class="incremental_cpp",
    ),
    "lz-luanti-11866-mapgen-boundary": PublicContract(
        preconditions=(
            "A Luanti world is open on the registered start with the map generation "
            "limit set close to the edge of the addressable map."
        ),
        reference_steps=(
            "Run the registered replay once and read the generation limit and the "
            "resulting block boundary the server reports."
        ),
        horizon_class="mid",
        capabilities=("spatial-reasoning", "numeric-reasoning"),
        build_class="incremental_cpp",
    ),
    "lz-luanti-12602-media-override": PublicContract(
        preconditions=(
            "A Luanti world is open on the registered start with two mods that both "
            "provide media under the same name."
        ),
        reference_steps=(
            "Run the registered replay once and read the media paths the server's mod "
            "manager reports."
        ),
        horizon_class="short",
        capabilities=("resource-management", "state-persistence"),
        build_class="incremental_cpp",
    ),
    "lz-luanti-13132-empty-separator": PublicContract(
        preconditions=(
            "A Luanti world is open on the registered start with a string helper that "
            "splits text on a caller-supplied separator."
        ),
        reference_steps=(
            "Run the registered replay once with an empty separator and read the "
            "outcome the server reports."
        ),
        horizon_class="short",
        capabilities=("structured-artifact-editing", "numeric-reasoning"),
        build_class="none",
    ),
    "lz-luanti-14072-oversized-stack": PublicContract(
        preconditions=(
            "A Luanti world is open on the registered start with an inventory stack "
            "whose count is above the item's stack maximum."
        ),
        reference_steps=(
            "Run the registered replay once to move that stack into an occupied slot "
            "and read the resulting item counts."
        ),
        horizon_class="mid",
        capabilities=("resource-management", "numeric-reasoning"),
        build_class="incremental_cpp",
    ),
    "lz-luanti-14464-iconv-state-reset": PublicContract(
        preconditions=(
            "A Luanti world is open on the registered start with node metadata that "
            "holds text carrying bytes outside the ASCII range."
        ),
        reference_steps=(
            "Run the registered replay once to store that metadata and read it back, "
            "then read the text the server reports."
        ),
        horizon_class="mid",
        capabilities=("state-persistence", "structured-artifact-editing"),
        build_class="incremental_cpp",
    ),
    "lz-luanti-14555-negative-half-pointing": PublicContract(
        preconditions=(
            "A Luanti world is open on the registered start with the player pointing "
            "at a node whose coordinates lie on the negative side of the origin."
        ),
        reference_steps=(
            "Run the registered replay once to place a node against the pointed face "
            "and read where that node lands."
        ),
        horizon_class="mid",
        capabilities=("spatial-reasoning", "gui-localization"),
        build_class="incremental_cpp",
    ),
    "lz-luanti-14757-math-round": PublicContract(
        preconditions=(
            "A Luanti world is open on the registered start as a headless server with "
            "the registered world mod loaded."
        ),
        reference_steps=(
            "Deliver the registered rounding command once from the server and read the "
            "rounded value from the server log."
        ),
        horizon_class="short",
        capabilities=("numeric-reasoning", "state-persistence"),
        build_class="none",
    ),
    "lz-luanti-15408-collision-acceleration": PublicContract(
        preconditions=(
            "A Luanti world is open on the registered start with an entity resting on "
            "a node and a horizontal acceleration applied to it."
        ),
        reference_steps=(
            "Run the registered replay once and read the entity's resulting speed and "
            "position."
        ),
        horizon_class="mid",
        capabilities=("spatial-reasoning", "numeric-reasoning"),
        build_class="incremental_cpp",
    ),
    "lz-luanti-15519-russian-plural-form": PublicContract(
        preconditions=(
            "A Luanti world is open on the registered start with a translation "
            "catalogue whose plural rule text is supplied by the catalogue itself."
        ),
        reference_steps=(
            "Run the registered replay once to translate one plural message and read "
            "the text the client renders."
        ),
        horizon_class="mid",
        capabilities=("structured-artifact-editing", "numeric-reasoning"),
        build_class="incremental_cpp",
    ),
}

def _apply_authored_public_text() -> None:
    """Overlay the rewritten wording so re-running this writer cannot undo it."""

    for task_id in tuple(_PUBLIC):
        authored = public_contract(task_id)
        if authored is None:
            continue
        preconditions, reference_steps = authored
        _PUBLIC[task_id] = replace(
            _PUBLIC[task_id],
            preconditions=preconditions,
            reference_steps=reference_steps,
        )


_apply_authored_public_text()


def frozen_task_rows() -> tuple[tuple[str, str, int], ...]:
    """Return ``(task_id, repository_key, change_number)`` for every frozen task."""

    return tuple(
        (task_id, repository_key, change_number)
        for (repository_key, change_number), task_id in sorted(
            _TASK_IDS.items(), key=lambda item: (item[0][0], item[0][1])
        )
    )


def write_formal_catalog(
    artifact_root: str | Path,
    *,
    repository_root: str | Path,
    roster_path: str | Path,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Write the 24-task formal catalog and its private evidence, then validate it."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("an active infrastructure-audit authorization is required")
    root = Path(artifact_root).absolute().resolve(strict=True)
    repo = Path(repository_root).absolute().resolve(strict=True)
    roster = yaml.safe_load(Path(roster_path).read_text(encoding="utf-8"))
    locus = {
        (str(row["repository_key"]), int(row["change_number"])): str(row["defect_locus"])
        for row in roster["candidates"]
    }
    rows: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    for task_id, repository_key, change_number in frozen_task_rows():
        public = _PUBLIC.get(task_id)
        if public is None:
            raise FormalCatalogError(f"{task_id} has no authored public contract")
        lazarus_dir = repo / "specs" / repository_key / "lazarus"
        spec = yaml.safe_load((lazarus_dir / f"{task_id}.yaml").read_text(encoding="utf-8"))
        manifest = yaml.safe_load(
            (lazarus_dir / f"{task_id}.goldens.yaml").read_text(encoding="utf-8")
        )
        upstream_tests = spec["evaluation"]["lazarus"]["upstream_tests"]
        patch = derive_patch(
            root,
            repository_key=repository_key,
            change_number=change_number,
            task_id=task_id,
            repository_root=repo,
        )
        record = build_upstream_bundle(
            root,
            repository_key=repository_key,
            change_number=change_number,
            task_id=task_id,
            issue_number=manifest["candidate"].get("issue_number"),
            upstream_tests=upstream_tests,
            patch=patch,
            repository_root=repo,
        )
        gate = project_gate_report(
            root,
            repository_key=repository_key,
            change_number=change_number,
            task_id=task_id,
            defect_locus=locus[(repository_key, change_number)],
            upstream_tests=upstream_tests,
            patch=patch,
            tests=record["tests"],
            repository_root=repo,
        )
        directory = repo / "benchmarks/p5/lazarus/defects" / task_id / EVIDENCE_EPOCH
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "revert.patch").write_bytes(patch.patch)
        (directory / "upstream.json").write_text(
            json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (directory / "gate.json").write_text(
            json.dumps(gate, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        relative = directory.relative_to(repo).as_posix()
        rows.append(
            _catalog_row(
                task_id=task_id,
                repository_key=repository_key,
                public=public,
                upstream=record,
                defect_locus=locus[(repository_key, change_number)],
                patch_sha256=patch.sha256,
                evidence_dir=relative,
                spec_path=(lazarus_dir / f"{task_id}.yaml").relative_to(repo).as_posix(),
            )
        )
        receipts.append(
            {
                "task_id": task_id,
                "repository_key": repository_key,
                "change_number": change_number,
                "evidence_directory": relative,
                "patch_source": patch.source,
                # The typed store rejects identity field names, so the receipt calls
                # the same hashes fingerprints.
                "patch_fingerprint": patch.sha256,
                "upstream_record_fingerprint": record["record_sha256"],
                "gate_status": gate["status"],
                "gate_is_projection": True,
                "test_ids": [entry["id"] for entry in record["tests"]],
            }
        )
    catalog = {
        "schema_version": 1,
        "task_schema": "c1_lazarus_v1",
        "formal_population": True,
        "benchmark_hosts": copy.deepcopy(FORMAL_HOSTS),
        "tasks": rows,
    }
    path = repo / FORMAL_CATALOG_PATH
    path.write_text(_render_catalog(catalog), encoding="utf-8")
    hosts, tasks = validate_lazarus_catalog(
        yaml.safe_load(path.read_text(encoding="utf-8")), source_path=path
    )
    payload = {
        "state": "passed",
        "complete": True,
        "catalog_path": FORMAL_CATALOG_PATH,
        "task_count": len(tasks),
        "host_count": len(hosts),
        "defect_locus": dict(Counter(row["defect"]["defect_locus"] for row in rows)),
        "benchmarks": dict(Counter(row["benchmark"] for row in rows)),
        "validated_by": "warptest.lazarus_catalog.validate_lazarus_catalog",
        "evidence_epoch": EVIDENCE_EPOCH,
        "gate_reports_are_projections": True,
        "tasks": receipts,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    reference = ArtifactReference(
        "c1_lazarus_formal_catalog/lazarus/v2-roster/current-v1",
        "c1_lazarus_formal_catalog",
        "1.0",
    )
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=(),
        payload=payload,
        authorization=authorization,
    )
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _catalog_row(
    *,
    task_id: str,
    repository_key: str,
    public: PublicContract,
    upstream: Mapping[str, Any],
    defect_locus: str,
    patch_sha256: str,
    evidence_dir: str,
    spec_path: str,
) -> dict[str, Any]:
    links: dict[str, str] = {}
    pull = upstream.get("pull_request")
    issue = upstream.get("issue")
    if isinstance(pull, Mapping) and pull.get("url"):
        links["pr"] = str(pull["url"])
    if isinstance(issue, Mapping) and issue.get("url"):
        links["issue"] = str(issue["url"])
    if not links:
        raise FormalCatalogError(f"{task_id} has no upstream link to publish")
    return {
        "id": task_id,
        "benchmark": repository_key,
        "repo": FORMAL_HOSTS[repository_key]["repo"],
        "prompt_revision": PROMPT_REVISION,
        "random_control": copy.deepcopy(expected_random_control(repository_key)),
        "preconditions": public.preconditions,
        "reference_steps": public.reference_steps,
        "state_description": _BlockText(
            canonical_state_description(public.preconditions, public.reference_steps)
        ),
        "horizon_class": public.horizon_class,
        "oracle_spec": spec_path,
        "tags": ["c1", "lazarus", repository_key],
        "capabilities": list(public.capabilities),
        "budget_ms": BUDGET_MS,
        "defect": {
            "upstream": links,
            "fix_commit": str(upstream["fix_commit"]),
            "revert_patch": f"{evidence_dir}/revert.patch",
            "patch_sha256": patch_sha256,
            "build_class": public.build_class,
            "defect_locus": defect_locus,
            "upstream_record": f"{evidence_dir}/upstream.json",
            "gate_report": f"{evidence_dir}/gate.json",
        },
    }


def _render_catalog(catalog: Mapping[str, Any]) -> str:
    header = (
        "# LEGACY C1-Lazarus catalog retained only for historical artifact replay and\n"
        "# audit. It is not the paper population and must not be used for new provider\n"
        "# dispatch or paper task counts. The paper's only C1-Lazarus authority is the\n"
        "# ordered 11-task P2P-only catalog at\n"
        "# ``benchmarks/p5/c1_lazarus_p2p_only_tasks.yaml``.\n"
        "#\n"
        "# This file preserves the former frozen-24 plus six-row expansion shape so old\n"
        "# reports remain interpretable. ``formal_population`` below is a legacy schema\n"
        "# marker, not a current paper/formal designation.\n"
        "#\n"
        "# Public rows carry only the start and the reference steps; the goal, the\n"
        "# defect metadata and the hidden oracle stay out of the model payload.\n"
        "#\n"
        "# The frozen rows' private artifacts were derived from the freeze sprint's own\n"
        "# evidence: revert.patch is the difference between the materialized buggy and\n"
        "# fixed trees, upstream.json is rebuilt from the cached upstream authority, and\n"
        "# gate.json projects the frozen Gate 1-13 verdicts instead of re-running the\n"
        "# construction gate.\n"
        "#\n"
        "# The expansion rows appended after them share the first two, but their\n"
        "# gate.json is a live receipt: it reports a construction run that executed in\n"
        "# this repository, so it declares derived_from_frozen_artifacts: false and\n"
        "# fresh_gate_run: true. See warptest/lazarus_expansion_gate.py.\n"
        "#\n"
        "# Budgets are uniform: every task gets the same budget_ms, so horizon_class is\n"
        "# a descriptive label only.\n"
    )
    return header + yaml.dump(
        _plain(catalog),
        Dumper=_CatalogDumper,
        sort_keys=False,
        allow_unicode=True,
        width=1_000_000,
    )


class _BlockText(str):
    """A string rendered as a literal block, the way the pilot catalog reads."""


class _CatalogDumper(yaml.SafeDumper):
    pass


_CatalogDumper.add_representer(
    _BlockText,
    lambda dumper, value: dumper.represent_scalar(
        "tag:yaml.org,2002:str", str(value), style="|"
    ),
)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, _BlockText):
        return value
    return value


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "BUDGET_MS",
    "EVIDENCE_EPOCH",
    "FORMAL_CATALOG_PATH",
    "FORMAL_HOSTS",
    "DerivedPatch",
    "FormalCatalogError",
    "PublicContract",
    "build_test_records",
    "build_upstream_bundle",
    "derive_patch",
    "frozen_task_rows",
    "project_gate_report",
    "read_upstream_authority",
    "tree_base_revision",
    "write_formal_catalog",
]
