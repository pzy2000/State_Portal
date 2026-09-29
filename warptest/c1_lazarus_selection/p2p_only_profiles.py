"""Typed profiles and Gate 1--5 recomputation for the P2P-only Lazarus track.

The registry at ``benchmarks/p5/lazarus/p2p_only/profiles.yaml`` pins, per
candidate, the upstream records Gate 1 and Gate 2 are decided from and the
SHA-256 of each of them.  This module reloads those captures, recomputes the
verdicts, and refuses to proceed on any drift.  Nothing here is read from a
narrative field: a profile states what must be true, and the captures decide
whether it is.

Gates 3 and 4 are only partly decidable here.  What the patch alone can settle
-- that the inverse touches production material and no test -- is settled here;
what needs the materialized pair is settled by the runner.

Gate 5 has a mechanical half and a judgement half.  The mechanical half is
recomputed: the change must add no test material.  The judgement half is the
authored reason below, which states why the defect is not expressible as a test
assertion.  It is registered rather than derived, because no capture can decide
it.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping

import yaml

from .patches import (
    classify_production_only_patch,
    is_visual_production_path,
    patch_test_paths,
)

PROFILE_REGISTRY = PurePosixPath("benchmarks/p5/lazarus/p2p_only/profiles.yaml")
REGISTRY_SCHEMA = "c1-lazarus-p2p-only-profiles-v1"
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BUG_DENY = ("not a bug", "won't fix", "wontfix", "invalid")
_BUG_ALLOW = ("bug", "regression", "crash")
# One entry per host this track draws from.  The upstream slug decides which
# repository a closing keyword may name; the checkout path is where a control
# test has to exist.
HOST_UPSTREAM: Mapping[str, str] = {
    "jupyterlab": "jupyterlab/jupyterlab",
    "luanti": "luanti-org/luanti",
    "openra": "OpenRA/OpenRA",
}
HOST_CHECKOUT: Mapping[str, str] = {
    "jupyterlab": "games/jupyterlab",
    "luanti": "games/luanti",
    "openra": "games/OpenRA",
}


class P2POnlyProfileError(RuntimeError):
    """Raised when a profile and its frozen captures disagree."""


# Gate 5's judgement half.  Each entry says why the repair could not ship a
# fail-to-pass test, in terms of what the defect does rather than what the fix
# changed, so the reason can be reviewed without reading the patch.
F2P_ABSENCE_REASONS: Mapping[str, str] = {
    "lzp-jupyterlab-18996-htmlselect-caret-offset": (
        "The defect is the painted position of a dropdown caret relative to its "
        "select box. It is a product of stylesheet cascade and absolute "
        "positioning resolved by the browser's layout engine, so an assertion "
        "would have to restate the layout engine's own arithmetic rather than "
        "observe the misplacement."
    ),
    "lzp-jupyterlab-18464-debugger-zero-variable-detail": (
        "The defect is that one rendered detail span is absent for a specific "
        "falsy value. The panel reports no error and its data model is "
        "unchanged, so the defect exists only in what the tree paints."
    ),
    "lzp-jupyterlab-17222-toolbar-button-pressed-toggle": (
        "The defect is that a toggle button keeps painting its unpressed icon "
        "after activation. The activation handler still runs and reports "
        "success, so the discrepancy is visible only in the rendered button."
    ),
    "lzp-jupyterlab-17153-shortcut-chip-contrast": (
        "The defect is the contrast between a chip and the surface it sits on, "
        "decided by which palette entry the chip resolves to. The document is "
        "identical either way, so the shortfall exists only in the painted "
        "colours."
    ),
    "lzp-jupyterlab-17618-high-contrast-selection-layer": (
        "The defect is that one painted layer is indistinguishable from the "
        "surface behind it under one theme, because both resolve to the same "
        "colour. Every value involved is a stylesheet variable the document "
        "never reads, so the collision is observable only once painted."
    ),
    "lzp-jupyterlab-19091-themed-button-glyph-font": (
        "The defect is which font a button's label is painted in, decided by "
        "the relative specificity of two stylesheet rules. Nothing in the "
        "document differs; only the glyph the browser selects does, so the "
        "defect exists only in the raster."
    ),
    "lzp-luanti-16679-unavailable-settings": (
        "The defect is that settings whose requirements are unmet are omitted "
        "from the painted settings list rather than shown disabled. The "
        "settings table itself is unchanged, so the shortfall exists only in "
        "which rows the menu draws."
    ),
    "lzp-luanti-16799-world-mods": (
        "The defect is that mods living in a world's worldmods directory are "
        "absent from the painted select-mods list. The world still loads them, "
        "so the shortfall exists only in what the dialog draws."
    ),
    "lzp-luanti-17152-server-list-search-tooltip": (
        "The defect is the painted tooltip of the server-list search field: "
        "unescaped filter examples collide with formspec markup, so the hover "
        "text the menu draws is truncated or misparsed. The search still runs, "
        "so the shortfall exists only in the painted tip."
    ),
    "lzp-openra-22476-load-game-faction-filter": (
        "The defect is that the Load Game faction filter paints each faction's "
        "internal identifier instead of its display name. The saved games are "
        "the same set either way, so the shortfall exists only in the strings "
        "the dropdown draws."
    ),
    "lzp-openra-22325-color-picker-overflow": (
        "The defect is that a color-picker panel is painted past the right "
        "edge of the window when its parent control sits too far right. The "
        "chosen colour is still applied, so the shortfall exists only in where "
        "the panel is drawn."
    ),
    "lzp-openra-22283-tileset-template-order": (
        "The defect is that the map editor's tile template list is painted in "
        "an order that does not match the tileset YAML. The templates are all "
        "present, so the shortfall exists only in the order the selector draws."
    ),
}


@dataclass(frozen=True)
class P2POnlyProfile:
    """One candidate's immutable Gate 1--5 evidence and Gate 6 plan."""

    task_id: str
    host: str
    pinned_revision: str
    change_number: int
    issue_number: int
    title: str
    issue_title: str
    issue_labels: tuple[str, ...]
    issue_state: str
    issue_link_source: str
    fixed_revision: str
    base_revision: str
    merged_at: str
    package_directory: str
    production_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]
    control_test_path: str
    control_test_relation: str
    raw_root: PurePosixPath
    raw_hashes: Mapping[str, str]
    inverse_patch_sha256: str
    timeline_cites_fix_commit: bool
    timeline_links_fix_change: bool
    catalog_admission: bool
    catalog_exclusion_reason: str

    def __post_init__(self) -> None:
        if not self.task_id.startswith("lzp-"):
            raise P2POnlyProfileError(f"{self.task_id} is not a P2P-only task id")
        if self.change_number <= 0 or self.issue_number <= 0:
            raise P2POnlyProfileError(f"{self.task_id} has an invalid identity")
        if not _FULL_SHA.fullmatch(self.pinned_revision):
            raise P2POnlyProfileError(
                f"{self.task_id} pinned revision must be a full SHA"
            )
        if not _FULL_SHA.fullmatch(self.fixed_revision) or not _FULL_SHA.fullmatch(
            self.base_revision
        ):
            raise P2POnlyProfileError(f"{self.task_id} revisions must be full SHAs")
        if not self.production_paths:
            raise P2POnlyProfileError(f"{self.task_id} declares no production path")
        if any(not is_visual_production_path(path) for path in self.production_paths):
            raise P2POnlyProfileError(
                f"{self.task_id} declares a non-production repair path"
            )
        if not _SHA256.fullmatch(self.inverse_patch_sha256):
            raise P2POnlyProfileError(f"{self.task_id} inverse hash is invalid")
        if self.task_id not in F2P_ABSENCE_REASONS:
            raise P2POnlyProfileError(
                f"{self.task_id} has no registered Gate 5 absence reason"
            )
        if not self.catalog_admission and not self.catalog_exclusion_reason.strip():
            raise P2POnlyProfileError(
                f"{self.task_id} is excluded from the catalog without a reason"
            )

    @property
    def candidate(self) -> dict[str, object]:
        return {"repository_key": self.host, "change_number": self.change_number}

    @property
    def defect_locus(self) -> Literal["at_goal"]:
        # Every row in this track is graded from the frame the reference steps
        # end on, with no path block to restore.
        return "at_goal"

    @property
    def f2p_absence_reason(self) -> str:
        return F2P_ABSENCE_REASONS[self.task_id]

    def raw_path(self, repository_root: Path, name: str) -> Path:
        return repository_root / self.raw_root / name

    def inverse_patch(self, repository_root: Path) -> bytes:
        return self.raw_path(repository_root, "inverse.patch").read_bytes()


def load_profile_registry(repository_root: str | Path) -> tuple[P2POnlyProfile, ...]:
    """Load every registered P2P-only profile."""

    root = Path(repository_root).absolute().resolve(strict=True)
    document = yaml.safe_load((root / PROFILE_REGISTRY).read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise P2POnlyProfileError("profile registry is malformed")
    if document.get("schema") != REGISTRY_SCHEMA or document.get("schema_version") != 1:
        raise P2POnlyProfileError("profile registry schema is unsupported")
    profiles = document.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise P2POnlyProfileError("profile registry declares no profile")
    hosts = document.get("hosts")
    if not isinstance(hosts, Mapping) or not hosts:
        raise P2POnlyProfileError("profile registry declares no hosts")
    loaded = tuple(_profile_from(entry, hosts=hosts) for entry in profiles)
    if len({item.task_id for item in loaded}) != len(loaded):
        raise P2POnlyProfileError("profile registry has duplicate task ids")
    for host in sorted({item.host for item in loaded}):
        checkout = HOST_CHECKOUT.get(host)
        if checkout is None:
            raise P2POnlyProfileError(f"profile registry host {host} is unknown")
        expected = {item.pinned_revision for item in loaded if item.host == host}
        if len(expected) != 1:
            raise P2POnlyProfileError(f"profile registry host {host} has multiple pins")
        result = subprocess.run(
            ("git", "-C", str(root / checkout), "rev-parse", "HEAD"),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        observed = result.stdout.strip() if result.returncode == 0 else ""
        pinned = next(iter(expected))
        if observed != pinned:
            raise P2POnlyProfileError(
                f"profile registry host {host} pins {pinned}, checkout is {observed or 'unavailable'}"
            )
    return loaded


def load_profile(repository_root: str | Path, task_id: str) -> P2POnlyProfile:
    for profile in load_profile_registry(repository_root):
        if profile.task_id == task_id:
            return profile
    raise P2POnlyProfileError(f"{task_id} is not a registered P2P-only profile")


def _profile_from(entry: object, *, hosts: Mapping[str, object]) -> P2POnlyProfile:
    if not isinstance(entry, Mapping):
        raise P2POnlyProfileError("profile entry is malformed")
    hashes = entry.get("raw_hashes")
    if not isinstance(hashes, Mapping) or not hashes:
        raise P2POnlyProfileError("profile entry has no capture hashes")
    host = str(entry["host"])
    host_record = hosts.get(host)
    if not isinstance(host_record, Mapping):
        raise P2POnlyProfileError(f"profile entry host {host} is missing")
    pinned_revision = str(host_record.get("pinned_revision") or "")
    expected_path = HOST_CHECKOUT.get(host)
    if expected_path is None or str(host_record.get("repo_path") or "") != expected_path:
        raise P2POnlyProfileError(f"profile entry host {host} checkout path drifted")
    return P2POnlyProfile(
        task_id=str(entry["task_id"]),
        host=host,
        pinned_revision=pinned_revision,
        change_number=int(entry["change_number"]),
        issue_number=int(entry["issue_number"]),
        title=str(entry.get("title") or ""),
        issue_title=str(entry.get("issue_title") or ""),
        issue_labels=tuple(str(value) for value in entry.get("issue_labels") or ()),
        issue_state=str(entry.get("issue_state") or ""),
        issue_link_source=str(entry.get("issue_link_source") or ""),
        fixed_revision=str(entry["fixed_revision"]),
        base_revision=str(entry["base_revision"]),
        merged_at=str(entry.get("merged_at") or ""),
        package_directory=str(entry["package_directory"]),
        production_paths=tuple(str(value) for value in entry["production_paths"]),
        changed_paths=tuple(str(value) for value in entry.get("changed_paths") or ()),
        control_test_path=str(entry["control_test_path"]),
        control_test_relation=str(entry.get("control_test_relation") or "same_package"),
        raw_root=PurePosixPath(str(entry["raw_root"])),
        raw_hashes={str(key): str(value) for key, value in hashes.items()},
        inverse_patch_sha256=str(entry["inverse_patch_sha256"]),
        timeline_cites_fix_commit=bool(entry.get("timeline_cites_fix_commit")),
        timeline_links_fix_change=bool(entry.get("timeline_links_fix_change")),
        catalog_admission=bool(entry.get("catalog_admission", True)),
        catalog_exclusion_reason=str(entry.get("catalog_exclusion_reason") or ""),
    )


@dataclass(frozen=True)
class StaticGateEvidence:
    """What Gate 1--5 recomputed from the frozen captures."""

    task_id: str
    gate_1_source_authority: str
    gate_2_single_defect_mapping: str
    gate_3_strict_inverse_patch: str
    gate_4_test_api_compatibility: str
    gate_5_f2p_absence: str
    evidence: Mapping[str, Any]

    @property
    def passed(self) -> bool:
        return all(
            value.startswith("passed")
            for value in (
                self.gate_1_source_authority,
                self.gate_2_single_defect_mapping,
                self.gate_3_strict_inverse_patch,
                self.gate_4_test_api_compatibility,
                self.gate_5_f2p_absence,
            )
        )

    def to_value(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "gate_1_source_authority": self.gate_1_source_authority,
            "gate_2_single_defect_mapping": self.gate_2_single_defect_mapping,
            "gate_3_strict_inverse_patch": self.gate_3_strict_inverse_patch,
            "gate_4_test_api_compatibility": self.gate_4_test_api_compatibility,
            "gate_5_f2p_absence": self.gate_5_f2p_absence,
            "passed": self.passed,
            "evidence": dict(self.evidence),
        }


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _closing_issue_numbers(text: str, slug: str) -> tuple[int, ...]:
    pattern = re.compile(
        r"(?i)\b(?:fix|fixes|fixed|close|closes|closed|resolve|resolves|resolved)\s+"
        r"(?:https://github\.com/" + re.escape(slug) + r"/issues/|#)(\d+)"
    )
    return tuple(
        dict.fromkeys(int(match.group(1)) for match in pattern.finditer(text))
    )


def timeline_fix_linkage(
    timeline: object,
    *,
    upstream_slug: str,
    change_number: int,
    fixed_revision: str,
) -> tuple[bool, bool]:
    """Return direct-commit and exact-fixing-change timeline bindings.

    GitHub records a direct ``referenced`` event for some squash merges, but for
    many valid ``Fixes #...`` merges it records only a cross-reference to the
    fixing pull request followed by a close event.  The latter is still an exact
    authority chain because the frozen pull record independently binds that PR
    number to ``fixed_revision``.
    """

    if not isinstance(timeline, list):
        return False, False
    repository_url = f"https://api.github.com/repos/{upstream_slug}"
    pull_url = f"{repository_url}/pulls/{change_number}"
    direct_commit = False
    fixing_change = False
    for item in timeline:
        if not isinstance(item, Mapping):
            continue
        if (
            item.get("event") in {"referenced", "closed"}
            and str(item.get("commit_id") or "") == fixed_revision
        ):
            direct_commit = True
        if item.get("event") != "cross-referenced":
            continue
        source = item.get("source")
        issue = source.get("issue") if isinstance(source, Mapping) else None
        pull = issue.get("pull_request") if isinstance(issue, Mapping) else None
        if (
            isinstance(issue, Mapping)
            and issue.get("number") == change_number
            and issue.get("repository_url") == repository_url
            and isinstance(pull, Mapping)
            and pull.get("url") == pull_url
        ):
            fixing_change = True
    return direct_commit, fixing_change


def recompute_static_gates(
    repository_root: str | Path,
    profile: P2POnlyProfile,
    *,
    upstream_slug: str | None = None,
) -> StaticGateEvidence:
    """Recompute Gate 1--5 from the frozen captures alone."""

    root = Path(repository_root).absolute().resolve(strict=True)
    if upstream_slug is None:
        upstream_slug = HOST_UPSTREAM.get(profile.host, "")
    if not upstream_slug:
        raise P2POnlyProfileError(f"{profile.task_id} host {profile.host} is unknown")
    checkout = HOST_CHECKOUT[profile.host]
    captures: dict[str, Any] = {}
    for name, expected in sorted(profile.raw_hashes.items()):
        path = profile.raw_path(root, name)
        if not path.is_file():
            raise P2POnlyProfileError(f"{profile.task_id} capture {name} is missing")
        payload = path.read_bytes()
        if _sha256(payload) != expected:
            raise P2POnlyProfileError(
                f"{profile.task_id} capture {name} hash drifted"
            )
        if name.endswith(".json"):
            captures[name] = json.loads(payload.decode("utf-8"))

    pull = captures.get("pull.json")
    commit = captures.get("commit.json")
    issue = captures.get(f"issue-{profile.issue_number}.json")
    timeline = captures.get("issue-timeline.json")
    if not isinstance(pull, Mapping) or not isinstance(commit, Mapping):
        raise P2POnlyProfileError(f"{profile.task_id} pull/commit capture is malformed")
    if not isinstance(issue, Mapping):
        raise P2POnlyProfileError(f"{profile.task_id} issue capture is malformed")

    # Gate 1: the repair is merged and its identity is the one the pair will be
    # materialized from.
    parents = [
        str(item.get("sha"))
        for item in commit.get("parents") or ()
        if isinstance(item, Mapping)
    ]
    gate1_problems: list[str] = []
    if pull.get("merged") is not True:
        gate1_problems.append("pull_request_is_not_merged")
    if pull.get("number") != profile.change_number:
        gate1_problems.append("pull_request_identity_drift")
    if str(pull.get("merge_commit_sha") or "") != profile.fixed_revision:
        gate1_problems.append("merge_commit_identity_drift")
    if str(commit.get("sha") or "") != profile.fixed_revision:
        gate1_problems.append("fix_commit_identity_drift")
    if parents != [profile.base_revision]:
        gate1_problems.append("base_revision_drift")
    if issue.get("number") != profile.issue_number:
        gate1_problems.append("issue_identity_drift")
    if issue.get("pull_request") is not None:
        gate1_problems.append("authority_is_a_pull_request")
    if issue.get("state") != "closed":
        gate1_problems.append("authority_is_not_closed")
    if not isinstance(timeline, list):
        gate1_problems.append("issue_timeline_is_missing")
    else:
        timeline_cites_fix, timeline_links_change = timeline_fix_linkage(
            timeline,
            upstream_slug=upstream_slug,
            change_number=profile.change_number,
            fixed_revision=profile.fixed_revision,
        )
        if timeline_cites_fix != profile.timeline_cites_fix_commit:
            gate1_problems.append("timeline_fix_commit_binding_drift")
        if timeline_links_change != profile.timeline_links_fix_change:
            gate1_problems.append("timeline_fix_change_binding_drift")
        if not (timeline_cites_fix or timeline_links_change):
            gate1_problems.append("issue_timeline_has_no_fix_binding")
    gate1 = "passed" if not gate1_problems else "failed:" + ",".join(gate1_problems)

    # Gate 2: exactly one same-repository bug issue.
    message = str(
        commit.get("commit", {}).get("message", "")
        if isinstance(commit.get("commit"), Mapping)
        else ""
    )
    from_message = _closing_issue_numbers(message, upstream_slug)
    from_body = tuple(
        value
        for value in _closing_issue_numbers(str(pull.get("body") or ""), upstream_slug)
        if value != profile.change_number
    )
    resolved = from_message or from_body
    labels = sorted(
        str(item.get("name"))
        for item in issue.get("labels") or ()
        if isinstance(item, Mapping) and item.get("name")
    )
    folded = [label.casefold() for label in labels]
    is_bug = not any(
        token in label for label in folded for token in _BUG_DENY
    ) and any(token in label for label in folded for token in _BUG_ALLOW)
    gate2_problems: list[str] = []
    if len(resolved) != 1 or resolved[0] != profile.issue_number:
        gate2_problems.append(f"authoritative_issue_set={list(resolved)}")
    if not is_bug:
        gate2_problems.append(f"authority_is_not_a_bug={labels}")
    if labels != list(profile.issue_labels):
        gate2_problems.append("issue_labels_drift")
    gate2 = (
        f"passed_authoritative_issue_{profile.issue_number}"
        if not gate2_problems
        else "failed:" + ",".join(gate2_problems)
    )

    # Gate 3: the inverse is production-only and covers exactly the declared set.
    inverse = profile.inverse_patch(root)
    if _sha256(inverse) != profile.inverse_patch_sha256:
        raise P2POnlyProfileError(f"{profile.task_id} inverse patch hash drifted")
    decision = classify_production_only_patch(inverse)
    gate3_problems: list[str] = []
    if decision.status != "production_only":
        gate3_problems.append(str(decision.reason))
    elif sorted(decision.production_paths) != sorted(profile.production_paths):
        gate3_problems.append("inverse_scope_differs_from_declared_production_paths")
    if patch_test_paths(inverse):
        gate3_problems.append("inverse_reaches_test_material")
    gate3 = (
        f"passed_production_only_{len(profile.production_paths)}_paths"
        if not gate3_problems
        else "failed:" + ",".join(gate3_problems)
    )

    # Gate 4: the control test is untouched by the repair, so it cannot depend on
    # an API the inverse removes.  The runner completes this against the pair.
    changed = sorted(
        str(item.get("filename"))
        for item in commit.get("files") or ()
        if isinstance(item, Mapping) and item.get("filename")
    )
    gate4_problems: list[str] = []
    if profile.control_test_path in changed:
        gate4_problems.append("control_test_was_modified_by_the_repair")
    if not (root / checkout / profile.control_test_path).is_file():
        gate4_problems.append("control_test_is_absent_from_the_checkout")
    gate4 = (
        "passed_static_control_test_untouched"
        if not gate4_problems
        else "failed:" + ",".join(gate4_problems)
    )

    # Gate 5: the repair ships no test material at all.
    added_tests = [path for path in changed if not is_visual_production_path(path)]
    test_material = patch_test_paths(
        profile.raw_path(root, "commit.diff").read_bytes()
    )
    gate5_problems: list[str] = []
    if test_material:
        gate5_problems.append(f"repair_adds_test_material={list(test_material)}")
    if not profile.f2p_absence_reason.strip():
        gate5_problems.append("absence_reason_is_empty")
    gate5 = (
        "passed_no_f2p"
        if not gate5_problems
        else "failed:" + ",".join(gate5_problems)
    )

    return StaticGateEvidence(
        task_id=profile.task_id,
        gate_1_source_authority=gate1,
        gate_2_single_defect_mapping=gate2,
        gate_3_strict_inverse_patch=gate3,
        gate_4_test_api_compatibility=gate4,
        gate_5_f2p_absence=gate5,
        evidence={
            "authoritative_bug_count": 1,
            "authoritative_issue": profile.issue_number,
            "issue_labels": labels,
            "issue_link_source": (
                "commit_message" if from_message else "pull_request_body"
            ),
            "timeline_cites_fix_commit": profile.timeline_cites_fix_commit,
            "timeline_links_fix_change": profile.timeline_links_fix_change,
            "merged": pull.get("merged") is True,
            "fixed_revision": profile.fixed_revision,
            "base_revision": profile.base_revision,
            "production_paths": list(profile.production_paths),
            "changed_paths": changed,
            "non_production_changed_paths": added_tests,
            "inverse_patch_hash": profile.inverse_patch_sha256,
            "control_test_path": profile.control_test_path,
            "control_test_relation": profile.control_test_relation,
            "f2p_absence": {
                "reason": profile.f2p_absence_reason,
                "audited_paths": list(test_material),
            },
        },
    )


__all__ = [
    "F2P_ABSENCE_REASONS",
    "HOST_CHECKOUT",
    "HOST_UPSTREAM",
    "P2POnlyProfile",
    "P2POnlyProfileError",
    "PROFILE_REGISTRY",
    "StaticGateEvidence",
    "load_profile",
    "load_profile_registry",
    "recompute_static_gates",
    "timeline_fix_linkage",
]
