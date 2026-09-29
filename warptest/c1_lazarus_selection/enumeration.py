"""Stable repository enumeration and complete terminal/audit coverage."""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from .epoch import (
    SELECTION_EPOCH_V1,
    SELECTION_EPOCH_V2,
    SelectionEpoch,
    require_selection_epoch,
)
from .models import CandidateKey
from .providers.gitea import _issue_numbers as _gitea_issue_numbers
from .resource_guard import require_write_capacity
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)


_GIT_OBJECT_ID = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?")


@dataclass(frozen=True)
class RepositoryEnumeration:
    repository_key: str
    merged_change_numbers: tuple[int, ...]
    authoritative_issue_link_missing: tuple[int, ...]
    repair_not_in_pinned: tuple[int, ...]
    candidates: tuple[CandidateKey, ...]
    infrastructure_blocked: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        groups = (
            self.merged_change_numbers,
            self.authoritative_issue_link_missing,
            self.repair_not_in_pinned,
            self.infrastructure_blocked,
        )
        if any(
            type(group) is not tuple
            or group != tuple(sorted(group))
            or len(set(group)) != len(group)
            or any(type(number) is not int or number <= 0 for number in group)
            for group in groups
        ):
            raise ValueError("repository enumeration number sets are invalid")
        if (
            type(self.candidates) is not tuple
            or self.candidates != tuple(sorted(self.candidates))
            or len(set(self.candidates)) != len(self.candidates)
            or any(
                candidate.repository_key != self.repository_key
                for candidate in self.candidates
            )
        ):
            raise ValueError("repository enumeration candidates are invalid")
        candidate_numbers = tuple(
            candidate.change_number for candidate in self.candidates
        )
        partitions = (
            set(self.authoritative_issue_link_missing),
            set(self.repair_not_in_pinned),
            set(candidate_numbers),
            set(self.infrastructure_blocked),
        )
        if any(
            left & right
            for index, left in enumerate(partitions)
            for right in partitions[index + 1 :]
        ):
            raise ValueError("repository enumeration partitions overlap")
        if set(self.merged_change_numbers) != set().union(*partitions):
            raise ValueError("repository enumeration coverage is incomplete")


@dataclass(frozen=True)
class EnumerationAuditResult:
    repository_key: str
    merged_count: int
    audit_count: int
    terminal_count: int
    deep_screen_count: int


@dataclass(frozen=True)
class EnumerationRecomputeResult:
    """Read-only proposed replacement for one repository enumeration epoch.

    The result deliberately contains stable candidate numbers only.  Git
    object identifiers are used transiently to inspect the local mirror, and
    never appear in the result or in a stable artifact.
    """

    baseline: RepositoryEnumeration
    recomputed: RepositoryEnumeration
    capture_file_count: int
    promoted_to_candidate_numbers: tuple[int, ...]
    reclassified_to_issue_link_missing_numbers: tuple[int, ...]
    genuine_nonancestor_numbers: tuple[int, ...]
    blocked_capture_structural_numbers: tuple[int, ...]
    blocked_merge_capture_missing_numbers: tuple[int, ...]
    blocked_merge_object_missing_numbers: tuple[int, ...]
    blocked_relationship_unverified_numbers: tuple[int, ...]
    blocked_ancestry_unavailable_numbers: tuple[int, ...]

    def __post_init__(self) -> None:
        if type(self.recomputed) is not RepositoryEnumeration:
            raise ValueError("enumeration recomputation is not typed")
        if self.baseline.repository_key != self.recomputed.repository_key:
            raise ValueError("enumeration recomputation repository drift")
        if type(self.capture_file_count) is not int or self.capture_file_count <= 0:
            raise ValueError("enumeration recomputation capture count is invalid")
        fields = (
            self.promoted_to_candidate_numbers,
            self.reclassified_to_issue_link_missing_numbers,
            self.genuine_nonancestor_numbers,
            self.blocked_capture_structural_numbers,
            self.blocked_merge_capture_missing_numbers,
            self.blocked_merge_object_missing_numbers,
            self.blocked_relationship_unverified_numbers,
            self.blocked_ancestry_unavailable_numbers,
        )
        repair = set(self.baseline.repair_not_in_pinned)
        if any(
            type(numbers) is not tuple
            or numbers != tuple(sorted(numbers))
            or len(numbers) != len(set(numbers))
            or not set(numbers) <= repair
            for numbers in fields
        ):
            raise ValueError("enumeration recomputation change sets are invalid")
        if any(
            left & right
            for index, left in enumerate(map(set, fields))
            for right in tuple(map(set, fields))[index + 1 :]
        ):
            raise ValueError("enumeration recomputation outcomes overlap")
        if set().union(*(set(numbers) for numbers in fields)) != repair:
            raise ValueError("enumeration recomputation outcomes are incomplete")
        if self.recomputed.merged_change_numbers != self.baseline.merged_change_numbers:
            raise ValueError("enumeration recomputation merged population drift")
        baseline_candidates = {
            candidate.change_number for candidate in self.baseline.candidates
        }
        recomputed_candidates = {
            candidate.change_number for candidate in self.recomputed.candidates
        }
        if set(self.promoted_to_candidate_numbers) != (
            recomputed_candidates - baseline_candidates
        ):
            raise ValueError("enumeration recomputation candidate delta is invalid")
        if set(self.recomputed.repair_not_in_pinned) != set(
            self.genuine_nonancestor_numbers
        ):
            raise ValueError("enumeration recomputation repair delta is invalid")
        if set(self.recomputed.infrastructure_blocked) != set(self.blocked_numbers):
            raise ValueError(
                "enumeration recomputation infrastructure delta is invalid"
            )
        if set(self.recomputed.authoritative_issue_link_missing) != (
            set(self.baseline.authoritative_issue_link_missing)
            | set(self.reclassified_to_issue_link_missing_numbers)
        ):
            raise ValueError("enumeration recomputation changes issue-link outcomes")

    @property
    def blocked_numbers(self) -> tuple[int, ...]:
        """All explicit v2 infrastructure terminals, in stable number order."""

        return tuple(
            sorted(
                (
                    *self.blocked_capture_structural_numbers,
                    *self.blocked_merge_capture_missing_numbers,
                    *self.blocked_merge_object_missing_numbers,
                    *self.blocked_relationship_unverified_numbers,
                    *self.blocked_ancestry_unavailable_numbers,
                )
            )
        )

    @property
    def blocked_merge_unavailable_numbers(self) -> tuple[int, ...]:
        """Merge evidence absent from capture or unavailable in the mirror."""

        return tuple(
            sorted(
                (
                    *self.blocked_merge_capture_missing_numbers,
                    *self.blocked_merge_object_missing_numbers,
                )
            )
        )

    @property
    def target_reference(self) -> ArtifactReference:
        """The future stable v2 target, without writing it."""

        return SELECTION_EPOCH_V2.enumeration_reference(self.baseline.repository_key)

    def dry_run_value(self) -> dict[str, object]:
        """Return an explicit, JSON-safe plan for a later v2 transition."""

        return {
            "mode": "read_only_dry_run",
            "write_performed": False,
            "target": {
                "artifact_id": self.target_reference.artifact_id,
                "artifact_kind": self.target_reference.artifact_kind,
                "semantic_version": self.target_reference.semantic_version,
            },
            "repository_key": self.baseline.repository_key,
            "capture_file_count": self.capture_file_count,
            "baseline_partitions": _enumeration_value(self.baseline),
            "proposed_partitions": _enumeration_value(self.recomputed),
            "v2_write_permitted": True,
            "changes": {
                "promoted_to_candidate_numbers": list(
                    self.promoted_to_candidate_numbers
                ),
                "removed_from_repair_not_in_pinned": list(
                    sorted(
                        (
                            *self.promoted_to_candidate_numbers,
                            *self.reclassified_to_issue_link_missing_numbers,
                            *self.blocked_numbers,
                        )
                    )
                ),
                "reclassified_to_authoritative_issue_link_missing": list(
                    self.reclassified_to_issue_link_missing_numbers
                ),
            },
            "fail_closed": {
                "genuine_nonancestor_numbers": list(self.genuine_nonancestor_numbers),
                "blocked_numbers": list(self.blocked_numbers),
            },
            "blocked_reason_buckets": {
                "capture_structural_numbers": list(
                    self.blocked_capture_structural_numbers
                ),
                "merge_unavailable_numbers": list(
                    self.blocked_merge_unavailable_numbers
                ),
                "merge_capture_missing_numbers": list(
                    self.blocked_merge_capture_missing_numbers
                ),
                "merge_object_missing_numbers": list(
                    self.blocked_merge_object_missing_numbers
                ),
                "relationship_unverified_numbers": list(
                    self.blocked_relationship_unverified_numbers
                ),
                "ancestry_unavailable_numbers": list(
                    self.blocked_ancestry_unavailable_numbers
                ),
            },
        }


@dataclass(frozen=True)
class _CapturedPull:
    merge_revisions: tuple[str, ...]
    relationship_state: str
    capture_state: str


def recompute_repository_enumeration_from_github_captures(
    baseline: RepositoryEnumeration,
    *,
    capture_root: str | Path,
    mirror_path: str | Path,
    pinned_ref: str,
    expected_repository_name: str,
) -> EnumerationRecomputeResult:
    """Propose an exact v2 partition from frozen GitHub captures and a mirror.

    This is intentionally read-only: it never writes an artifact, mutates a
    checkpoint, fetches a remote, or executes source/patch screening.  It only
    revisits entries already rejected as ``repair_not_in_pinned``.  Existing
    source-relationship rejections and existing deep-screen candidates remain
    untouched; a promotion requires an authoritative captured relationship in
    addition to a merge revision that is provably reachable from ``pinned_ref``.
    """

    if type(baseline) is not RepositoryEnumeration:
        raise ValueError("enumeration baseline is not typed")
    if (
        type(expected_repository_name) is not str
        or expected_repository_name.count("/") != 1
    ):
        raise ValueError("expected repository name is invalid")
    if (
        type(pinned_ref) is not str
        or not pinned_ref.startswith("refs/remotes/origin/")
        or any(character.isspace() for character in pinned_ref)
    ):
        raise ValueError("pinned mirror ref is invalid")

    captures, capture_file_count = _load_github_capture_population(
        capture_root,
        expected_repository_name=expected_repository_name,
        expected_numbers=set(baseline.merged_change_numbers),
    )
    mirror = _safe_git_mirror(mirror_path)
    promoted: list[int] = []
    reclassified_to_missing: list[int] = []
    genuine_nonancestor: list[int] = []
    blocked_capture_structural: list[int] = []
    blocked_merge_capture_missing: list[int] = []
    blocked_merge_object_missing: list[int] = []
    blocked_relationship_unverified: list[int] = []
    blocked_ancestry_unavailable: list[int] = []
    repair_numbers = set(baseline.repair_not_in_pinned)
    usable_revisions = {
        capture.merge_revisions[0]
        for number, capture in captures.items()
        if (
            number in repair_numbers
            and capture.capture_state == "usable"
            and len(capture.merge_revisions) == 1
        )
    }
    ancestry_by_revision = _classify_merge_revisions(
        mirror,
        pinned_ref=pinned_ref,
        revisions=usable_revisions,
    )
    for number in baseline.repair_not_in_pinned:
        capture = captures.get(number)
        if capture is None or capture.capture_state == "structural_invalid":
            blocked_capture_structural.append(number)
            continue
        if (
            capture.capture_state == "merge_unavailable"
            or len(capture.merge_revisions) != 1
        ):
            blocked_merge_capture_missing.append(number)
            continue
        merge_revision = capture.merge_revisions[0]
        ancestry = ancestry_by_revision.get(merge_revision)
        if ancestry == "object_missing":
            blocked_merge_object_missing.append(number)
        elif ancestry == "ancestry_unavailable":
            blocked_ancestry_unavailable.append(number)
        elif ancestry == "not_reachable":
            genuine_nonancestor.append(number)
        elif ancestry != "reachable":
            blocked_ancestry_unavailable.append(number)
        elif capture.relationship_state == "present":
            promoted.append(number)
        elif capture.relationship_state == "absent":
            reclassified_to_missing.append(number)
        else:
            blocked_relationship_unverified.append(number)

    promoted_tuple = tuple(promoted)
    reclassified_tuple = tuple(reclassified_to_missing)
    genuine_nonancestor_tuple = tuple(genuine_nonancestor)
    blocked_capture_structural_tuple = tuple(blocked_capture_structural)
    blocked_merge_capture_missing_tuple = tuple(blocked_merge_capture_missing)
    blocked_merge_object_missing_tuple = tuple(blocked_merge_object_missing)
    blocked_relationship_unverified_tuple = tuple(blocked_relationship_unverified)
    blocked_ancestry_unavailable_tuple = tuple(blocked_ancestry_unavailable)
    blocked_tuple = tuple(
        sorted(
            (
                *blocked_capture_structural_tuple,
                *blocked_merge_capture_missing_tuple,
                *blocked_merge_object_missing_tuple,
                *blocked_relationship_unverified_tuple,
                *blocked_ancestry_unavailable_tuple,
            )
        )
    )
    recomputed = RepositoryEnumeration(
        repository_key=baseline.repository_key,
        merged_change_numbers=baseline.merged_change_numbers,
        authoritative_issue_link_missing=tuple(
            sorted(
                (
                    *baseline.authoritative_issue_link_missing,
                    *reclassified_tuple,
                )
            )
        ),
        repair_not_in_pinned=genuine_nonancestor_tuple,
        candidates=tuple(
            sorted(
                (
                    *baseline.candidates,
                    *(
                        CandidateKey(baseline.repository_key, number)
                        for number in promoted_tuple
                    ),
                )
            )
        ),
        infrastructure_blocked=blocked_tuple,
    )
    return EnumerationRecomputeResult(
        baseline=baseline,
        recomputed=recomputed,
        capture_file_count=capture_file_count,
        promoted_to_candidate_numbers=promoted_tuple,
        reclassified_to_issue_link_missing_numbers=reclassified_tuple,
        genuine_nonancestor_numbers=genuine_nonancestor_tuple,
        blocked_capture_structural_numbers=blocked_capture_structural_tuple,
        blocked_merge_capture_missing_numbers=blocked_merge_capture_missing_tuple,
        blocked_merge_object_missing_numbers=blocked_merge_object_missing_tuple,
        blocked_relationship_unverified_numbers=blocked_relationship_unverified_tuple,
        blocked_ancestry_unavailable_numbers=blocked_ancestry_unavailable_tuple,
    )


@dataclass(frozen=True)
class _GiteaApiScope:
    scheme: str
    netloc: str
    api_path: str
    repository_path: str


@dataclass(frozen=True)
class _GiteaPullMetadata:
    merge_revision: str | None
    title: str | None
    body: str | None
    capture_state: str


def recompute_repository_enumeration_from_gitea_captures(
    baseline: RepositoryEnumeration,
    *,
    capture_root: str | Path,
    mirror_path: str | Path,
    pinned_ref: str,
    expected_repository_name: str,
    expected_api_base: str,
) -> EnumerationRecomputeResult:
    """Propose a v2 partition from frozen Gitea captures and a local mirror.

    The replayer is deliberately offline and read-only.  It considers only
    entries previously placed in ``repair_not_in_pinned``; all other baseline
    partitions remain unchanged.  Each candidate promotion requires one
    canonical closed-pull capture, complete canonical timeline pagination, an
    authoritative relationship recognized by the production Gitea parser, and
    a merge revision reachable from the caller's explicit pinned remote ref.
    """

    if type(baseline) is not RepositoryEnumeration:
        raise ValueError("enumeration baseline is not typed")
    if (
        type(expected_repository_name) is not str
        or expected_repository_name.count("/") != 1
    ):
        raise ValueError("expected repository name is invalid")
    if (
        type(pinned_ref) is not str
        or not pinned_ref.startswith("refs/remotes/origin/")
        or any(character.isspace() for character in pinned_ref)
    ):
        raise ValueError("pinned mirror ref is invalid")
    scope = _gitea_api_scope(expected_api_base, expected_repository_name)
    captures, capture_file_count = _load_gitea_capture_population(
        capture_root,
        baseline=baseline,
        scope=scope,
        expected_repository_name=expected_repository_name,
    )
    return _recompute_from_captured_pulls(
        baseline,
        captures=captures,
        capture_file_count=capture_file_count,
        mirror_path=mirror_path,
        pinned_ref=pinned_ref,
    )


def _recompute_from_captured_pulls(
    baseline: RepositoryEnumeration,
    *,
    captures: dict[int, _CapturedPull],
    capture_file_count: int,
    mirror_path: str | Path,
    pinned_ref: str,
) -> EnumerationRecomputeResult:
    """Apply the shared reachable-merge partitioning to provider captures."""

    mirror = _safe_git_mirror(mirror_path)
    promoted: list[int] = []
    reclassified_to_missing: list[int] = []
    genuine_nonancestor: list[int] = []
    blocked_capture_structural: list[int] = []
    blocked_merge_capture_missing: list[int] = []
    blocked_merge_object_missing: list[int] = []
    blocked_relationship_unverified: list[int] = []
    blocked_ancestry_unavailable: list[int] = []
    repair_numbers = set(baseline.repair_not_in_pinned)
    usable_revisions = {
        capture.merge_revisions[0]
        for number, capture in captures.items()
        if (
            number in repair_numbers
            and capture.capture_state == "usable"
            and len(capture.merge_revisions) == 1
        )
    }
    ancestry_by_revision = _classify_merge_revisions(
        mirror,
        pinned_ref=pinned_ref,
        revisions=usable_revisions,
    )
    for number in baseline.repair_not_in_pinned:
        capture = captures.get(number)
        if capture is None or capture.capture_state == "structural_invalid":
            blocked_capture_structural.append(number)
            continue
        if (
            capture.capture_state == "merge_unavailable"
            or len(capture.merge_revisions) != 1
        ):
            blocked_merge_capture_missing.append(number)
            continue
        merge_revision = capture.merge_revisions[0]
        ancestry = ancestry_by_revision.get(merge_revision)
        if ancestry == "object_missing":
            blocked_merge_object_missing.append(number)
        elif ancestry == "ancestry_unavailable":
            blocked_ancestry_unavailable.append(number)
        elif ancestry == "not_reachable":
            genuine_nonancestor.append(number)
        elif ancestry != "reachable":
            blocked_ancestry_unavailable.append(number)
        elif capture.relationship_state == "present":
            promoted.append(number)
        elif capture.relationship_state == "absent":
            reclassified_to_missing.append(number)
        else:
            blocked_relationship_unverified.append(number)

    promoted_tuple = tuple(promoted)
    reclassified_tuple = tuple(reclassified_to_missing)
    genuine_nonancestor_tuple = tuple(genuine_nonancestor)
    blocked_capture_structural_tuple = tuple(blocked_capture_structural)
    blocked_merge_capture_missing_tuple = tuple(blocked_merge_capture_missing)
    blocked_merge_object_missing_tuple = tuple(blocked_merge_object_missing)
    blocked_relationship_unverified_tuple = tuple(blocked_relationship_unverified)
    blocked_ancestry_unavailable_tuple = tuple(blocked_ancestry_unavailable)
    blocked_tuple = tuple(
        sorted(
            (
                *blocked_capture_structural_tuple,
                *blocked_merge_capture_missing_tuple,
                *blocked_merge_object_missing_tuple,
                *blocked_relationship_unverified_tuple,
                *blocked_ancestry_unavailable_tuple,
            )
        )
    )
    recomputed = RepositoryEnumeration(
        repository_key=baseline.repository_key,
        merged_change_numbers=baseline.merged_change_numbers,
        authoritative_issue_link_missing=tuple(
            sorted(
                (
                    *baseline.authoritative_issue_link_missing,
                    *reclassified_tuple,
                )
            )
        ),
        repair_not_in_pinned=genuine_nonancestor_tuple,
        candidates=tuple(
            sorted(
                (
                    *baseline.candidates,
                    *(
                        CandidateKey(baseline.repository_key, number)
                        for number in promoted_tuple
                    ),
                )
            )
        ),
        infrastructure_blocked=blocked_tuple,
    )
    return EnumerationRecomputeResult(
        baseline=baseline,
        recomputed=recomputed,
        capture_file_count=capture_file_count,
        promoted_to_candidate_numbers=promoted_tuple,
        reclassified_to_issue_link_missing_numbers=reclassified_tuple,
        genuine_nonancestor_numbers=genuine_nonancestor_tuple,
        blocked_capture_structural_numbers=blocked_capture_structural_tuple,
        blocked_merge_capture_missing_numbers=blocked_merge_capture_missing_tuple,
        blocked_merge_object_missing_numbers=blocked_merge_object_missing_tuple,
        blocked_relationship_unverified_numbers=(blocked_relationship_unverified_tuple),
        blocked_ancestry_unavailable_numbers=blocked_ancestry_unavailable_tuple,
    )


def _gitea_api_scope(
    expected_api_base: str,
    expected_repository_name: str,
) -> _GiteaApiScope:
    if type(expected_api_base) is not str:
        raise ValueError("expected Gitea API base is invalid")
    parsed = urlparse(expected_api_base)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("expected Gitea API base is invalid")
    api_path = parsed.path.rstrip("/")
    if not api_path.startswith("/") or api_path == "/":
        raise ValueError("expected Gitea API base is invalid")
    owner, repository = expected_repository_name.split("/", maxsplit=1)
    if not owner or not repository:
        raise ValueError("expected repository name is invalid")
    return _GiteaApiScope(
        scheme=parsed.scheme,
        netloc=parsed.netloc.casefold(),
        api_path=api_path,
        repository_path=f"{api_path}/repos/{owner}/{repository}",
    )


def _load_gitea_capture_population(
    capture_root: str | Path,
    *,
    baseline: RepositoryEnumeration,
    scope: _GiteaApiScope,
    expected_repository_name: str,
) -> tuple[dict[int, _CapturedPull], int]:
    """Read only canonical direct-root Gitea captures.

    Historical ``superseded-pull-pages`` deliberately remains outside this
    enumeration.  The direct root is the canonical frozen response set; a
    duplicate page there is an error rather than an opportunity to pick a
    convenient response.
    """

    root = _safe_capture_directory(capture_root)
    expected_numbers = set(baseline.merged_change_numbers)
    repair_numbers = set(baseline.repair_not_in_pinned)
    pull_pages: dict[int, Path] = {}
    timeline_pages: dict[int, dict[int, Path]] = {}
    capture_file_count = 0
    for path in sorted(root.iterdir()):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("repository enumeration capture is unsafe")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError("repository enumeration capture is unsafe")
        if not path.name.endswith(".gitea-response.json"):
            raise RuntimeError("Gitea enumeration capture name is invalid")
        final_url, _, _ = _gitea_capture_envelope(path, root)
        kind, number, page = _gitea_capture_endpoint(final_url, scope)
        capture_file_count += 1
        if kind == "pulls":
            if page in pull_pages:
                raise RuntimeError("Gitea enumeration duplicate pull page")
            pull_pages[page] = path
        elif number in expected_numbers:
            pages = timeline_pages.setdefault(number, {})
            if page in pages:
                raise RuntimeError("Gitea enumeration duplicate timeline page")
            pages[page] = path

    if not pull_pages:
        raise RuntimeError("Gitea enumeration pull capture population is missing")
    metadata = _load_gitea_pull_metadata(
        pull_pages,
        root=root,
        expected_numbers=expected_numbers,
        repair_numbers=repair_numbers,
        expected_repository_name=expected_repository_name,
    )
    captures: dict[int, _CapturedPull] = {}
    for number in baseline.repair_not_in_pinned:
        pull = metadata.get(number)
        if pull is None or pull.capture_state == "structural_invalid":
            captures[number] = _CapturedPull((), "unverified", "structural_invalid")
            continue
        revisions = (pull.merge_revision,) if type(pull.merge_revision) is str else ()
        if pull.capture_state == "merge_unavailable":
            captures[number] = _CapturedPull(
                revisions,
                "unverified",
                "merge_unavailable",
            )
            continue
        timeline_state, rows = _load_gitea_timeline(
            timeline_pages.get(number),
            root=root,
            scope=scope,
            expected_number=number,
        )
        if timeline_state == "structural_invalid":
            captures[number] = _CapturedPull(
                revisions,
                "unverified",
                "structural_invalid",
            )
            continue
        if timeline_state != "usable":
            captures[number] = _CapturedPull(revisions, "unverified", "usable")
            continue
        relationship_state = _gitea_relationship_state(
            {"title": pull.title, "body": pull.body},
            rows,
            scope=scope,
        )
        captures[number] = _CapturedPull(
            revisions,
            relationship_state,
            "usable",
        )
    return captures, capture_file_count


def _load_gitea_pull_metadata(
    pull_pages: dict[int, Path],
    *,
    root: Path,
    expected_numbers: set[int],
    repair_numbers: set[int],
    expected_repository_name: str,
) -> dict[int, _GiteaPullMetadata]:
    if set(pull_pages) != set(range(1, max(pull_pages) + 1)):
        raise RuntimeError("Gitea enumeration pull pagination is incomplete")
    total_count: int | None = None
    all_numbers: set[int] = set()
    expected_seen: set[int] = set()
    metadata: dict[int, _GiteaPullMetadata] = {}
    row_count = 0
    for page in sorted(pull_pages):
        _, headers, body = _gitea_capture_body(pull_pages[page], root)
        if type(body) is not list or any(type(row) is not dict for row in body):
            raise RuntimeError("Gitea enumeration pull page is invalid")
        page_total = _gitea_total_count(headers, "pull")
        if total_count is None:
            total_count = page_total
        elif page_total != total_count:
            raise RuntimeError("Gitea enumeration pull total is inconsistent")
        if page != max(pull_pages) and not body:
            raise RuntimeError("Gitea enumeration pull pagination is incomplete")
        row_count += len(body)
        for row in body:
            number = row.get("number")
            if type(number) is not int or number <= 0:
                raise RuntimeError("Gitea enumeration pull number is invalid")
            if number in all_numbers:
                raise RuntimeError("Gitea enumeration pull population duplicates")
            all_numbers.add(number)
            if number not in expected_numbers:
                continue
            expected_seen.add(number)
            if number in repair_numbers:
                metadata[number] = _gitea_pull_metadata(
                    row,
                    number=number,
                    expected_repository_name=expected_repository_name,
                )
    if (
        total_count is None
        or row_count != total_count
        or len(all_numbers) != total_count
    ):
        raise RuntimeError("Gitea enumeration pull population is incomplete")
    if expected_seen != expected_numbers:
        raise RuntimeError("Gitea enumeration capture population is incomplete")
    if set(metadata) != repair_numbers:
        raise RuntimeError("Gitea enumeration repair capture population is incomplete")
    return metadata


def _gitea_pull_metadata(
    row: dict[str, object],
    *,
    number: int,
    expected_repository_name: str,
) -> _GiteaPullMetadata:
    base = row.get("base")
    repository = base.get("repo") if type(base) is dict else None
    structural_invalid = (
        row.get("number") != number
        or row.get("state") != "closed"
        or row.get("merged") is not True
        or not isinstance(row.get("merged_at"), str)
        or not row.get("merged_at")
        or not isinstance(row.get("title"), str)
        or not isinstance(row.get("body"), str)
        or type(repository) is not dict
        or repository.get("full_name") != expected_repository_name
    )
    revision = row.get("merge_commit_sha")
    merge_revision = revision if type(revision) is str else None
    merge_unavailable = merge_revision is None or not _GIT_OBJECT_ID.fullmatch(
        merge_revision
    )
    return _GiteaPullMetadata(
        merge_revision=merge_revision,
        title=row.get("title") if type(row.get("title")) is str else None,
        body=row.get("body") if type(row.get("body")) is str else None,
        capture_state=(
            "structural_invalid"
            if structural_invalid
            else "merge_unavailable"
            if merge_unavailable
            else "usable"
        ),
    )


def _load_gitea_timeline(
    paths: dict[int, Path] | None,
    *,
    root: Path,
    scope: _GiteaApiScope,
    expected_number: int,
) -> tuple[str, list[dict[str, object]]]:
    if not paths or set(paths) != set(range(1, max(paths) + 1)):
        return "structural_invalid", []
    total_count: int | None = None
    rows: list[dict[str, object]] = []
    for page in sorted(paths):
        try:
            final_url, headers, body = _gitea_capture_body(paths[page], root)
            kind, number, actual_page = _gitea_capture_endpoint(final_url, scope)
            page_total = _gitea_total_count(headers, "timeline")
        except RuntimeError:
            return "structural_invalid", []
        if (
            kind != "timeline"
            or number != expected_number
            or actual_page != page
            or type(body) is not list
        ):
            return "structural_invalid", []
        if total_count is None:
            total_count = page_total
        elif page_total != total_count:
            return "structural_invalid", []
        if page != max(paths) and not body:
            return "structural_invalid", []
        if any(type(row) is not dict for row in body):
            return "unverified", []
        rows.extend(body)
    if total_count is None or len(rows) != total_count:
        return "structural_invalid", []
    return "usable", rows


def _gitea_relationship_state(
    pull: dict[str, object],
    rows: list[dict[str, object]],
    *,
    scope: _GiteaApiScope,
) -> str:
    for row in rows:
        event_type = row.get("type")
        if type(event_type) is not str:
            return "unverified"
        if event_type == "comment_ref":
            if _same_gitea_issue_state(row.get("ref_issue"), scope) != "present":
                return "unverified"
        elif event_type in {
            "cross_reference",
            "cross-reference",
            "close",
            "closed",
            "referenced",
        }:
            source = row.get("source")
            issue = source.get("issue") if type(source) is dict else None
            if _same_gitea_issue_state(issue, scope) != "present":
                return "unverified"
    try:
        return "present" if _gitea_issue_numbers(pull, rows) else "absent"
    except (TypeError, ValueError, AttributeError):
        return "unverified"


def _same_gitea_issue_state(value: object, scope: _GiteaApiScope) -> str:
    if type(value) is not dict:
        return "unverified"
    number = value.get("number")
    url = value.get("url")
    if type(number) is not int or number <= 0 or type(url) is not str:
        return "unverified"
    parsed = urlparse(url)
    if (
        parsed.scheme != scope.scheme
        or parsed.netloc.casefold() != scope.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return "unverified"
    expected_path = f"{scope.repository_path}/issues/{number}"
    return "present" if parsed.path == expected_path else "unverified"


def _gitea_capture_envelope(
    path: Path,
    root: Path,
) -> tuple[str, dict[str, str], str]:
    _require_regular_owned_file(path, root)
    document = _strict_json(path.read_bytes(), "Gitea repository enumeration capture")
    final_url = document.get("final_url")
    headers = document.get("headers")
    body_base64 = document.get("body_base64")
    if (
        type(final_url) is not str
        or type(headers) is not dict
        or any(
            type(key) is not str or type(value) is not str
            for key, value in headers.items()
        )
        or type(body_base64) is not str
        or document.get("status_code") != 200
    ):
        raise RuntimeError("Gitea repository enumeration capture envelope is invalid")
    normalized_headers: dict[str, str] = {}
    for key, value in headers.items():
        normalized_key = key.casefold()
        if normalized_key in normalized_headers:
            raise RuntimeError("Gitea repository enumeration capture headers duplicate")
        normalized_headers[normalized_key] = value
    return final_url, normalized_headers, body_base64


def _gitea_capture_body(
    path: Path,
    root: Path,
) -> tuple[str, dict[str, str], object]:
    final_url, headers, encoded_body = _gitea_capture_envelope(path, root)
    try:
        body = base64.b64decode(encoded_body, validate=True)
    except (ValueError, UnicodeEncodeError) as error:
        raise RuntimeError("Gitea repository enumeration body is invalid") from error
    return (
        final_url,
        headers,
        _strict_json_value(
            body,
            "Gitea repository enumeration response",
        ),
    )


def _gitea_capture_endpoint(
    final_url: str,
    scope: _GiteaApiScope,
) -> tuple[str, int, int]:
    parsed = urlparse(final_url)
    if (
        parsed.scheme != scope.scheme
        or parsed.netloc.casefold() != scope.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.fragment
    ):
        raise RuntimeError("Gitea repository enumeration URL is invalid")
    query = _gitea_query(parsed.query)
    pulls_path = f"{scope.repository_path}/pulls"
    if parsed.path == pulls_path:
        if set(query) != {"state", "sort", "limit", "page"}:
            raise RuntimeError("Gitea repository enumeration pull URL is invalid")
        if (
            query["state"] != "closed"
            or query["sort"] != "oldest"
            or query["limit"] != "100"
        ):
            raise RuntimeError("Gitea repository enumeration pull URL is invalid")
        return "pulls", 0, _positive_decimal(query["page"], "pull page")
    prefix = f"{scope.repository_path}/issues/"
    suffix = "/timeline"
    if not parsed.path.startswith(prefix) or not parsed.path.endswith(suffix):
        raise RuntimeError("Gitea repository enumeration URL is invalid")
    number_text = parsed.path[len(prefix) : -len(suffix)]
    if "/" in number_text or not number_text:
        raise RuntimeError("Gitea repository enumeration timeline URL is invalid")
    if set(query) != {"limit", "page"} or query["limit"] != "100":
        raise RuntimeError("Gitea repository enumeration timeline URL is invalid")
    return (
        "timeline",
        _positive_decimal(number_text, "timeline number"),
        _positive_decimal(query["page"], "timeline page"),
    )


def _gitea_query(value: str) -> dict[str, str]:
    try:
        pairs = parse_qsl(value, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise RuntimeError("Gitea repository enumeration query is invalid") from error
    result: dict[str, str] = {}
    for key, item in pairs:
        if key in result:
            raise RuntimeError("Gitea repository enumeration query duplicates")
        result[key] = item
    return result


def _positive_decimal(value: str, label: str) -> int:
    if not value.isdecimal() or value.startswith("0"):
        raise RuntimeError(f"Gitea repository enumeration {label} is invalid")
    number = int(value)
    if number <= 0:
        raise RuntimeError(f"Gitea repository enumeration {label} is invalid")
    return number


def _gitea_total_count(headers: dict[str, str], label: str) -> int:
    value = headers.get("x-total-count")
    if type(value) is not str or not value.isdecimal():
        raise RuntimeError(f"Gitea repository enumeration {label} total is invalid")
    return int(value)


def _strict_json_value(value: bytes, label: str) -> object:
    def no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise RuntimeError(f"{label} has duplicate object keys")
            result[key] = item
        return result

    try:
        return json.loads(
            value.decode("utf-8"),
            object_pairs_hook=no_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                RuntimeError(f"{label} has a non-finite number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{label} is invalid JSON") from error


def _enumeration_value(enumeration: RepositoryEnumeration) -> dict[str, object]:
    return {
        "merged_change_numbers": list(enumeration.merged_change_numbers),
        "authoritative_issue_link_missing": list(
            enumeration.authoritative_issue_link_missing
        ),
        "repair_not_in_pinned": list(enumeration.repair_not_in_pinned),
        "infrastructure_blocked": list(enumeration.infrastructure_blocked),
        "candidate_numbers": [
            candidate.change_number for candidate in enumeration.candidates
        ],
    }


def _load_github_capture_population(
    capture_root: str | Path,
    *,
    expected_repository_name: str,
    expected_numbers: set[int],
) -> tuple[dict[int, _CapturedPull], int]:
    root = _safe_capture_directory(capture_root)
    paths = tuple(
        sorted(path for path in root.iterdir() if path.name.endswith(".response.json"))
    )
    if not paths:
        raise RuntimeError("repository enumeration capture population is missing")

    records: dict[int, list[dict[str, object]]] = {}
    total_counts: set[int] = set()
    for path in paths:
        _require_regular_owned_file(path, root)
        document = _strict_json(path.read_bytes(), "repository enumeration capture")
        connection = _github_pull_connection(
            document,
            expected_repository_name=expected_repository_name,
        )
        total_counts.add(connection["total_count"])
        for node in connection["nodes"]:
            number = node.get("number")
            if type(number) is not int or number <= 0:
                raise RuntimeError(
                    "repository enumeration capture pull number is invalid"
                )
            records.setdefault(number, []).append(node)

    if len(total_counts) != 1 or total_counts.pop() != len(expected_numbers):
        raise RuntimeError("repository enumeration capture total coverage is invalid")
    if set(records) != expected_numbers:
        raise RuntimeError("repository enumeration capture population is incomplete")
    return (
        {
            number: _captured_pull(rows, expected_repository_name)
            for number, rows in records.items()
        },
        len(paths),
    )


def _safe_capture_directory(value: str | Path) -> Path:
    path = Path(value).absolute()
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError(
            "repository enumeration capture directory is missing"
        ) from error
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("repository enumeration capture directory is unsafe")
    return resolved


def _require_regular_owned_file(path: Path, root: Path) -> None:
    if path.parent.resolve(strict=True) != root:
        raise RuntimeError("repository enumeration capture is outside its root")
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
    ):
        raise RuntimeError("repository enumeration capture is unsafe")


def _strict_json(value: bytes, label: str) -> dict[str, object]:
    def no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise RuntimeError(f"{label} has duplicate object keys")
            result[key] = item
        return result

    try:
        document = json.loads(
            value.decode("utf-8"), object_pairs_hook=no_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{label} is invalid JSON") from error
    if type(document) is not dict:
        raise RuntimeError(f"{label} is not an object")
    return document


def _github_pull_connection(
    document: dict[str, object],
    *,
    expected_repository_name: str,
) -> dict[str, object]:
    data = document.get("data")
    repository = data.get("repository") if type(data) is dict else None
    connection = repository.get("pullRequests") if type(repository) is dict else None
    if (
        type(repository) is not dict
        or repository.get("nameWithOwner") != expected_repository_name
        or type(connection) is not dict
    ):
        raise RuntimeError(
            "repository enumeration capture repository identity is invalid"
        )
    total_count = connection.get("totalCount")
    nodes = connection.get("nodes")
    if (
        type(total_count) is not int
        or total_count < 0
        or type(nodes) is not list
        or any(type(node) is not dict for node in nodes)
    ):
        raise RuntimeError("repository enumeration capture connection is invalid")
    return {"total_count": total_count, "nodes": nodes}


def _captured_pull(
    rows: list[dict[str, object]],
    expected_repository_name: str,
) -> _CapturedPull:
    revisions: set[str] = set()
    structural_invalid = False
    merge_unavailable = False
    relationship_states: set[str] = set()
    for row in rows:
        if row.get("state") != "MERGED":
            structural_invalid = True
        merge = row.get("mergeCommit")
        revision = merge.get("oid") if type(merge) is dict else None
        if type(revision) is not str or not _GIT_OBJECT_ID.fullmatch(revision):
            merge_unavailable = True
        else:
            revisions.add(revision)
        relationship_states.add(_relationship_state(row, expected_repository_name))
    if len(revisions) > 1:
        structural_invalid = True
    if not revisions:
        merge_unavailable = True
    relationship_state = (
        relationship_states.pop() if len(relationship_states) == 1 else "unverified"
    )
    capture_state = (
        "structural_invalid"
        if structural_invalid
        else "merge_unavailable"
        if merge_unavailable
        else "usable"
    )
    return _CapturedPull(
        merge_revisions=tuple(sorted(revisions)),
        relationship_state=relationship_state,
        capture_state=capture_state,
    )


def _relationship_state(row: dict[str, object], expected_repository_name: str) -> str:
    closing = row.get("closingIssuesReferences")
    closing_nodes = closing.get("nodes") if type(closing) is dict else None
    cross_references = row.get("crossReferencedIssues")
    cross_nodes = (
        cross_references.get("nodes") if type(cross_references) is dict else None
    )
    if type(closing_nodes) is not list or type(cross_nodes) is not list:
        return "unverified"
    state = "absent"
    for node in closing_nodes:
        if type(node) is not dict:
            return "unverified"
        issue_state = _same_repository_issue_state(node, expected_repository_name)
        if issue_state == "unverified":
            return "unverified"
        if issue_state == "present":
            state = "present"
    for node in cross_nodes or ():
        if type(node) is not dict:
            return "unverified"
        source = node.get("source")
        if type(source) is not dict:
            return "unverified"
        source_type = source.get("__typename")
        if type(source_type) is not str:
            return "unverified"
        if source_type == "Issue":
            issue_state = _same_repository_issue_state(source, expected_repository_name)
            if issue_state == "unverified":
                return "unverified"
            if issue_state == "present":
                state = "present"
    return state


def _same_repository_issue_state(value: object, expected_repository_name: str) -> str:
    if type(value) is not dict:
        return "unverified"
    number = value.get("number")
    url = value.get("url")
    if type(number) is not int or number <= 0 or type(url) is not str:
        return "unverified"
    parsed = urlparse(url)
    expected_path = f"/{expected_repository_name}/issues/{number}".casefold()
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return "unverified"
    return "present" if parsed.path.casefold() == expected_path else "absent"


def _safe_git_mirror(value: str | Path) -> Path:
    path = Path(value).absolute()
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("repository enumeration mirror is missing") from error
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("repository enumeration mirror is unsafe")
    code, output = _git(resolved, "rev-parse", "--is-bare-repository")
    if code != 0 or output.strip() != "true":
        raise RuntimeError("repository enumeration mirror is not bare")
    return resolved


def _is_commit(mirror: Path, revision: str) -> bool:
    code, _ = _git(mirror, "cat-file", "-e", f"{revision}^{{commit}}")
    return code == 0


def _classify_merge_revisions(
    mirror: Path,
    *,
    pinned_ref: str,
    revisions: set[str],
) -> dict[str, str]:
    """Classify all revisions with one pinned history walk and one batch probe."""

    if not revisions:
        return {}
    if not _is_commit(mirror, pinned_ref):
        return {revision: "ancestry_unavailable" for revision in revisions}
    code, reachable_text = _git(mirror, "rev-list", pinned_ref)
    if code != 0:
        return {revision: "ancestry_unavailable" for revision in revisions}
    reachable = set(reachable_text.splitlines())
    code, object_types = _git_batch_object_types(mirror, revisions)
    if code != 0:
        return {revision: "ancestry_unavailable" for revision in revisions}
    return {
        revision: (
            "reachable"
            if revision in reachable
            else "not_reachable"
            if object_types.get(revision) == "commit"
            else "object_missing"
        )
        for revision in revisions
    }


def _git_batch_object_types(
    mirror: Path,
    revisions: set[str],
) -> tuple[int, dict[str, str]]:
    try:
        completed = subprocess.run(
            (
                "git",
                f"--git-dir={mirror}",
                "cat-file",
                "--batch-check=%(objectname) %(objecttype)",
            ),
            check=False,
            input="".join(f"{revision}\n" for revision in sorted(revisions)),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 128, {}
    if completed.returncode != 0:
        return completed.returncode, {}
    types: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        revision, separator, object_type = line.partition(" ")
        if not separator or not _GIT_OBJECT_ID.fullmatch(revision):
            return 128, {}
        types[revision] = object_type
    if set(types) != revisions:
        return 128, {}
    return 0, types


def _git(mirror: Path, *arguments: str) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            ("git", f"--git-dir={mirror}", *arguments),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 128, ""
    return completed.returncode, completed.stdout


def write_repository_enumeration(
    artifact_root: str | Path,
    enumeration: RepositoryEnumeration,
    *,
    authorization: AuthorizationRecord,
    selection_epoch: SelectionEpoch = SELECTION_EPOCH_V1,
) -> Path:
    if type(enumeration) is not RepositoryEnumeration:
        raise ValueError("repository enumeration is not typed")
    epoch = require_selection_epoch(selection_epoch)
    if not epoch.is_v2 and enumeration.infrastructure_blocked:
        raise ValueError("v1 enumeration cannot contain infrastructure terminals")
    require_write_capacity(artifact_root)
    payload: dict[str, object] = {
        "repository_key": enumeration.repository_key,
        "merged_change_numbers": list(enumeration.merged_change_numbers),
        "authoritative_issue_link_missing": list(
            enumeration.authoritative_issue_link_missing
        ),
        "repair_not_in_pinned": list(enumeration.repair_not_in_pinned),
        "candidates": [candidate.to_value() for candidate in enumeration.candidates],
        "complete": True,
    }
    if epoch.is_v2:
        payload["infrastructure_blocked"] = list(enumeration.infrastructure_blocked)
    return write_stable_artifact(
        artifact_root,
        artifact_id=epoch.enumeration_reference(enumeration.repository_key).artifact_id,
        artifact_kind="repository_enumeration",
        semantic_version=epoch.semantic_version,
        created_at=authorization.granted_at,
        inputs=(),
        payload=payload,
        authorization=authorization,
    )


def load_repository_enumeration(
    artifact_root: str | Path,
    *,
    repository_key: str,
    selection_epoch: SelectionEpoch = SELECTION_EPOCH_V1,
) -> RepositoryEnumeration:
    epoch = require_selection_epoch(selection_epoch)
    artifact = load_stable_artifact(
        artifact_root,
        artifact_id=epoch.enumeration_reference(repository_key).artifact_id,
        artifact_kind="repository_enumeration",
        semantic_version=epoch.semantic_version,
    )
    payload = artifact.payload
    if (
        payload.get("repository_key") != repository_key
        or payload.get("complete") is not True
    ):
        raise RuntimeError("repository enumeration identity is invalid")
    return RepositoryEnumeration(
        repository_key=repository_key,
        merged_change_numbers=_number_tuple(payload.get("merged_change_numbers")),
        authoritative_issue_link_missing=_number_tuple(
            payload.get("authoritative_issue_link_missing")
        ),
        repair_not_in_pinned=_number_tuple(payload.get("repair_not_in_pinned")),
        candidates=tuple(
            CandidateKey.from_value(value)
            for value in _plain_list(payload.get("candidates"), "candidates")
        ),
        infrastructure_blocked=(
            _number_tuple(payload.get("infrastructure_blocked"))
            if epoch.is_v2
            else _number_tuple_or_empty(payload.get("infrastructure_blocked"))
        ),
    )


def write_enumeration_audit_population(
    artifact_root: str | Path,
    enumeration: RepositoryEnumeration,
    *,
    authorization: AuthorizationRecord,
    persistence_workers: int = 2,
    selection_epoch: SelectionEpoch = SELECTION_EPOCH_V1,
) -> EnumerationAuditResult:
    if type(persistence_workers) is not int or not 1 <= persistence_workers <= 2:
        raise ValueError("enumeration persistence worker count is invalid")
    epoch = require_selection_epoch(selection_epoch)
    if not epoch.is_v2 and enumeration.infrastructure_blocked:
        raise ValueError("v1 enumeration cannot contain infrastructure terminals")
    root = Path(artifact_root)
    reference = epoch.enumeration_reference(enumeration.repository_key)
    missing = set(enumeration.authoritative_issue_link_missing)
    not_pinned = set(enumeration.repair_not_in_pinned)
    blocked = set(enumeration.infrastructure_blocked)

    def persist(change_number: int) -> int:
        require_write_capacity(root)
        candidate = CandidateKey(enumeration.repository_key, change_number)
        audit_id = (
            f"enumeration_audit/{enumeration.repository_key}/{change_number}/"
            f"current/{epoch.label}"
        )
        write_stable_artifact(
            root,
            artifact_id=audit_id,
            artifact_kind="enumeration_audit",
            semantic_version=epoch.semantic_version,
            created_at=authorization.granted_at,
            inputs=(reference,),
            payload={"candidate": candidate.to_value(), "complete": True},
            authorization=authorization,
        )
        load_stable_artifact(
            root,
            artifact_id=audit_id,
            artifact_kind="enumeration_audit",
            semantic_version=epoch.semantic_version,
        )
        reason = (
            "authoritative_issue_link_missing"
            if change_number in missing
            else "repair_not_in_pinned"
            if change_number in not_pinned
            else "infrastructure_blocked"
            if change_number in blocked
            else None
        )
        if reason is None:
            return 0
        stage = (
            "issue-link"
            if reason == "authoritative_issue_link_missing"
            else "not-in-pinned"
            if reason == "repair_not_in_pinned"
            else "infrastructure-blocked"
        )
        terminal_id = (
            f"enumeration_terminal/{enumeration.repository_key}/"
            f"{change_number}/{stage}/{epoch.label}"
        )
        write_stable_artifact(
            root,
            artifact_id=terminal_id,
            artifact_kind="enumeration_terminal",
            semantic_version=epoch.semantic_version,
            created_at=authorization.granted_at,
            inputs=(reference,),
            payload={
                "candidate": candidate.to_value(),
                "reason": reason,
                "complete": True,
            },
            authorization=authorization,
        )
        load_stable_artifact(
            root,
            artifact_id=terminal_id,
            artifact_kind="enumeration_terminal",
            semantic_version=epoch.semantic_version,
        )
        return 1

    with ThreadPoolExecutor(max_workers=persistence_workers) as pool:
        terminal_count = sum(pool.map(persist, enumeration.merged_change_numbers))
    return EnumerationAuditResult(
        repository_key=enumeration.repository_key,
        merged_count=len(enumeration.merged_change_numbers),
        audit_count=len(enumeration.merged_change_numbers),
        terminal_count=terminal_count,
        deep_screen_count=len(enumeration.candidates),
    )


def _plain_list(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise RuntimeError(f"repository enumeration {label} are invalid")
    return value


def _number_tuple(value: object) -> tuple[int, ...]:
    raw = _plain_list(value, "number sets")
    if any(type(number) is not int for number in raw):
        raise RuntimeError("repository enumeration number set is invalid")
    return tuple(raw)


def _number_tuple_or_empty(value: object) -> tuple[int, ...]:
    return () if value is None else _number_tuple(value)


__all__ = [
    "EnumerationAuditResult",
    "EnumerationRecomputeResult",
    "RepositoryEnumeration",
    "recompute_repository_enumeration_from_gitea_captures",
    "load_repository_enumeration",
    "recompute_repository_enumeration_from_github_captures",
    "write_enumeration_audit_population",
    "write_repository_enumeration",
]
