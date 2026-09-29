#!/usr/bin/env python3
"""Freeze the upstream authority for selected P2P-only candidates.

For each selected pull request this fetches the records Gate 1 and Gate 2 are
recomputed from -- the merged pull request, its squash commit, the authoritative
issue and that issue's timeline -- stores them as raw captures, and writes a
hash-pinned profile registry.

The registry is the immutable half of the track: the runner recomputes Gate 1--5
from these captures and refuses to proceed when any capture hash has drifted, so
a later edit to a capture cannot quietly change a verdict.

Nothing here materializes a tree, builds, runs a test or captures a frame.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess

import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from warptest.c1_lazarus_selection.patches import (  # noqa: E402
    classify_production_only_patch,
    is_visual_production_path,
)
from warptest.c1_lazarus_selection.p2p_only_profiles import (  # noqa: E402
    timeline_fix_linkage,
)

EVIDENCE_ROOT = Path(".warptest-cache/lazarus-p2p-only")
PROFILE_REGISTRY = Path("benchmarks/p5/lazarus/p2p_only/profiles.yaml")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

# The selected population.  Each entry names the pull request, the stable task
# slug, and the existing repository test that serves as the Gate 6 control.
SELECTION: tuple[dict[str, object], ...] = (
    {
        "pr": 18996,
        "host": "jupyterlab",
        "slug": "htmlselect-caret-offset",
        "package": "packages/ui-components",
        "control_test": "packages/ui-components/test/styling.spec.ts",
    },
    {
        "pr": 18464,
        "host": "jupyterlab",
        "slug": "debugger-zero-variable-detail",
        "package": "packages/debugger",
        "control_test": "packages/debugger/test/config.spec.ts",
    },
    {
        "pr": 17222,
        "host": "jupyterlab",
        "slug": "toolbar-button-pressed-toggle",
        "package": "packages/ui-components",
        "control_test": "packages/ui-components/test/styling.spec.ts",
    },
    {
        "pr": 17153,
        "host": "jupyterlab",
        "slug": "shortcut-chip-contrast",
        "package": "packages/shortcuts-extension",
        "control_test": "packages/shortcuts-extension/test/shortcuts.spec.ts",
    },
    {
        "pr": 17618,
        "host": "jupyterlab",
        "slug": "high-contrast-selection-layer",
        "package": "packages/theme-dark-high-contrast-extension",
        # The touched package ships no test suite of its own, so the control is
        # the shell suite that renders against these theme variables.  The
        # relation is recorded in the profile rather than left implicit.
        "control_test": "packages/application/test/shell.spec.ts",
    },
    {
        "pr": 19091,
        "host": "jupyterlab",
        "slug": "themed-button-glyph-font",
        "package": "packages/application",
        "control_test": "packages/application/test/shell.spec.ts",
    },
    {
        "pr": 16679,
        "host": "luanti",
        "slug": "unavailable-settings",
        "package": "builtin",
        "control_test": "games/devtest/mods/unittests/misc.lua",
    },
    {
        "pr": 16799,
        "host": "luanti",
        "slug": "world-mods",
        "package": "builtin",
        "control_test": "games/devtest/mods/unittests/misc.lua",
    },
    {
        "pr": 17152,
        "host": "luanti",
        "slug": "server-list-search-tooltip",
        "package": "builtin",
        "control_test": "games/devtest/mods/unittests/misc.lua",
    },
    {
        "pr": 22476,
        "host": "openra",
        "slug": "load-game-faction-filter",
        "package": "OpenRA.Mods.Common",
        "control_test": "OpenRA.Test/OpenRA.Game/MiniYamlTest.cs",
    },
    {
        "pr": 22325,
        "host": "openra",
        "slug": "color-picker-overflow",
        "package": "OpenRA.Mods.Common",
        "control_test": "OpenRA.Test/OpenRA.Game/MiniYamlTest.cs",
        "catalog_admission": False,
        "catalog_exclusion_reason": (
            "four-frame luma differential is zero at the registered capture geometries"
        ),
    },
    {
        "pr": 22283,
        "host": "openra",
        "slug": "tileset-template-order",
        "package": "OpenRA.Mods.Common",
        "control_test": "OpenRA.Test/OpenRA.Game/MiniYamlTest.cs",
    },
)

HOSTS: dict[str, dict[str, str]] = {
    "jupyterlab": {
        "key": "jupyterlab",
        "slug": "jupyterlab/jupyterlab",
        "repo_path": "games/jupyterlab",
        # A JupyterLab control must not boot a Jupyter server: Gate 6 has to
        # separate "both products run" from "a service was reachable".
        "forbidden_control_token": "JupyterServer",
    },
    "luanti": {
        "key": "luanti",
        "slug": "luanti-org/luanti",
        "repo_path": "games/luanti",
        "forbidden_control_token": "",
    },
    "openra": {
        "key": "openra",
        "slug": "OpenRA/OpenRA",
        "repo_path": "games/OpenRA",
        "forbidden_control_token": "",
    },
}




def _gh(argv: list[str]) -> bytes:
    """Call ``gh`` and retry the GitHub 5xx answers that abort a freeze."""

    last_error = ""
    for attempt in range(4):
        completed = subprocess.run(argv, capture_output=True, timeout=120)
        if completed.returncode == 0:
            return completed.stdout
        last_error = completed.stderr.decode("utf-8", "replace") or completed.stdout.decode(
            "utf-8", "replace"
        )
        if "50" not in last_error[:80] and "502" not in last_error and "503" not in last_error:
            break
        time.sleep(2 ** attempt)
    raise RuntimeError(f"{' '.join(argv[:4])} failed: {last_error[-2000:]}")


def gh_json(path: str, *, accept: str | None = None) -> object:
    argv = ["gh", "api", path]
    if accept:
        argv.extend(["-H", f"Accept: {accept}"])
    return json.loads(_gh(argv))


def gh_raw(path: str, accept: str) -> bytes:
    return _gh(["gh", "api", path, "-H", f"Accept: {accept}"])


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def write_capture(directory: Path, name: str, payload: bytes) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(payload)
    return sha256_bytes(payload)


def closing_issue_numbers(message: str, *, slug: str) -> tuple[int, ...]:
    """Return same-repository issues the text closes.

    The URL form must be anchored to this repository.  A bare ``/issues/153`` in
    another project's URL would otherwise resolve to whatever issue happens to
    carry that number here, which is how an unrelated design ticket can be
    mistaken for the authoritative bug.
    """

    pattern = re.compile(
        r"(?i)\b(?:fix|fixes|fixed|close|closes|closed|resolve|resolves|resolved)\s+"
        r"(?:https://github\.com/" + re.escape(slug) + r"/issues/|#)(\d+)"
    )
    return tuple(
        dict.fromkeys(int(match.group(1)) for match in pattern.finditer(message))
    )


def production_only_diff(diff: bytes) -> bytes:
    """Drop any file section that is not repair material for a rendered frame."""

    sections: list[list[bytes]] = []
    current: list[bytes] | None = None
    keep = False
    for line in diff.splitlines(keepends=True):
        if line.startswith(b"diff --git "):
            if current is not None and keep:
                sections.append(current)
            current = [line]
            parts = line.decode("utf-8", "replace").split()
            path = parts[3][2:] if len(parts) == 4 and parts[3].startswith("b/") else ""
            keep = bool(path) and is_visual_production_path(path)
            continue
        if current is not None:
            current.append(line)
    if current is not None and keep:
        sections.append(current)
    return b"".join(b"".join(section) for section in sections)


def build_profile(entry: Mapping[str, object]) -> dict[str, object]:
    host = HOSTS[str(entry["host"])]
    repo = REPOSITORY_ROOT / str(host["repo_path"])
    number = int(entry["pr"])
    slug = str(entry["slug"])
    raw_dir = REPOSITORY_ROOT / EVIDENCE_ROOT / "raw" / host["key"] / str(number)
    print(f"  fetching upstream authority for #{number}")

    pull = gh_json(f"repos/{host['slug']}/pulls/{number}")
    if not isinstance(pull, Mapping):
        raise RuntimeError(f"#{number} pull payload is malformed")
    if pull.get("merged") is not True:
        raise RuntimeError(f"#{number} is not merged")
    merge_commit = str(pull.get("merge_commit_sha") or "")
    if not _FULL_SHA.fullmatch(merge_commit):
        raise RuntimeError(f"#{number} has no merge commit")

    commit = gh_json(f"repos/{host['slug']}/commits/{merge_commit}")
    if not isinstance(commit, Mapping):
        raise RuntimeError(f"#{number} commit payload is malformed")
    parents = [
        str(item.get("sha")) for item in commit.get("parents") or () if isinstance(item, Mapping)
    ]
    if len(parents) != 1:
        raise RuntimeError(f"#{number} squash commit does not have exactly one parent")

    message = str(
        commit.get("commit", {}).get("message", "")
        if isinstance(commit.get("commit"), Mapping)
        else ""
    )
    # A squash merge usually carries the closing keyword into the commit message,
    # but some authors leave it only in the pull-request body.  Both are upstream
    # authority; which one was used is recorded so Gate 2 evidence stays honest.
    upstream_slug = str(host["slug"])
    closing = closing_issue_numbers(message, slug=upstream_slug)
    link_source = "commit_message"
    if not closing:
        closing = tuple(
            value
            for value in closing_issue_numbers(
                str(pull.get("body") or ""), slug=upstream_slug
            )
            if value != number
        )
        link_source = "pull_request_body"
    if len(closing) != 1:
        raise RuntimeError(f"#{number} closes {len(closing)} issues, not one")
    issue_number = closing[0]

    issue = gh_json(f"repos/{host['slug']}/issues/{issue_number}")
    timeline = gh_json(f"repos/{host['slug']}/issues/{issue_number}/timeline?per_page=100")
    diff = gh_raw(
        f"repos/{host['slug']}/pulls/{number}", "application/vnd.github.v3.diff"
    )
    inverse = production_only_diff(diff)
    decision = classify_production_only_patch(inverse)
    if decision.status != "production_only":
        raise RuntimeError(
            f"#{number} production-only classification failed: {decision.reason}"
        )

    changed = sorted(
        str(item.get("filename"))
        for item in commit.get("files") or ()
        if isinstance(item, Mapping) and item.get("filename")
    )
    production = sorted(decision.production_paths)
    if set(production) != set(changed):
        # A change that also edits a non-rendered file (a build script, a
        # changelog) would make the inverse narrower than the upstream repair.
        # Record it explicitly rather than silently publishing a partial defect.
        print(
            f"    note: #{number} touches {sorted(set(changed) - set(production))} "
            "outside the rendered surface; the inverse covers only the rendered files"
        )

    raw_hashes = {
        "pull.json": write_capture(raw_dir, "pull.json", canonical_json(pull)),
        "commit.json": write_capture(raw_dir, "commit.json", canonical_json(commit)),
        f"issue-{issue_number}.json": write_capture(
            raw_dir, f"issue-{issue_number}.json", canonical_json(issue)
        ),
        "issue-timeline.json": write_capture(
            raw_dir, "issue-timeline.json", canonical_json(timeline)
        ),
        "commit.diff": write_capture(raw_dir, "commit.diff", diff),
        "inverse.patch": write_capture(raw_dir, "inverse.patch", inverse),
    }

    if not isinstance(issue, Mapping):
        raise RuntimeError(f"#{number} issue payload is malformed")
    if issue.get("pull_request") is not None:
        raise RuntimeError(f"#{number} authority #{issue_number} is a pull request")
    if issue.get("state") != "closed":
        raise RuntimeError(f"#{number} authority #{issue_number} is not closed")
    labels = sorted(
        str(item.get("name"))
        for item in (issue.get("labels") or ())
        if isinstance(item, Mapping) and item.get("name")
    )
    folded = [label.casefold() for label in labels]
    if any(
        token in label
        for label in folded
        for token in ("not a bug", "won't fix", "wontfix", "invalid")
    ) or not any(
        token in label for label in folded for token in ("bug", "regression", "crash")
    ):
        raise RuntimeError(
            f"#{number} authority #{issue_number} is not labelled a bug: {labels}"
        )
    timeline_cites_fix, timeline_links_change = timeline_fix_linkage(
        timeline,
        upstream_slug=upstream_slug,
        change_number=number,
        fixed_revision=merge_commit,
    )

    control_test = str(entry["control_test"])
    control_path = repo / control_test
    if not control_path.is_file():
        raise RuntimeError(f"#{number} control test {control_test} is missing")
    # Gate 6 has to separate "both products run" from "a service was reachable".
    # A control that boots a Jupyter server measures the second as well, so it
    # cannot serve as this track's control.
    forbidden = str(host.get("forbidden_control_token") or "")
    if forbidden and forbidden in control_path.read_text(
        encoding="utf-8", errors="replace"
    ):
        raise RuntimeError(
            f"#{number} control test {control_test} depends on {forbidden}"
        )

    return {
        "task_id": f"lzp-{host['key']}-{number}-{slug}",
        "host": host["key"],
        "change_number": number,
        "issue_number": issue_number,
        "title": str(pull.get("title") or ""),
        "issue_title": str(issue.get("title") or "") if isinstance(issue, Mapping) else "",
        "issue_labels": labels,
        "issue_state": str(issue.get("state") or "") if isinstance(issue, Mapping) else "",
        "issue_link_source": link_source,
        "fixed_revision": merge_commit,
        "base_revision": parents[0],
        "merged_at": str(pull.get("merged_at") or ""),
        "package_directory": str(entry["package"]),
        "production_paths": production,
        "changed_paths": changed,
        "control_test_path": control_test,
        "control_test_relation": (
            "same_package"
            if control_test.startswith(str(entry["package"]) + "/")
            else "cross_package_touched_package_ships_no_tests"
        ),
        "raw_root": (EVIDENCE_ROOT / "raw" / host["key"] / str(number)).as_posix(),
        "raw_hashes": raw_hashes,
        "inverse_patch_sha256": raw_hashes["inverse.patch"],
        "timeline_cites_fix_commit": timeline_cites_fix,
        "timeline_links_fix_change": timeline_links_change,
        "added_test_paths": [],
        "catalog_admission": bool(entry.get("catalog_admission", True)),
        "catalog_exclusion_reason": str(entry.get("catalog_exclusion_reason") or ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pr",
        action="append",
        type=int,
        help="restrict to one pull request; repeatable (default: the whole selection)",
    )
    parser.add_argument("--output", type=Path, default=PROFILE_REGISTRY)
    args = parser.parse_args()

    wanted = set(args.pr or [])
    entries: Sequence[Mapping[str, object]] = [
        entry for entry in SELECTION if not wanted or int(entry["pr"]) in wanted
    ]
    if not entries:
        print("no selected pull request matched", file=sys.stderr)
        return 1

    output = args.output
    if not output.is_absolute():
        output = REPOSITORY_ROOT / output
    existing: dict[str, object] = {}
    if output.is_file():
        loaded = yaml.safe_load(output.read_text(encoding="utf-8")) or {}
        for profile in loaded.get("profiles") or ():
            existing[str(profile.get("task_id"))] = profile

    current_host = ""
    for entry in entries:
        if str(entry["host"]) != current_host:
            current_host = str(entry["host"])
            print(f"== {HOSTS[current_host]['slug']} ==")
        profile = build_profile(entry)
        existing[str(profile["task_id"])] = profile

    profiles = [existing[key] for key in sorted(existing)]
    used = sorted({str(profile["host"]) for profile in profiles})
    document = {
        "schema": "c1-lazarus-p2p-only-profiles-v1",
        "schema_version": 1,
        "hosts": {
            key: {
                **HOSTS[key],
                # Each host is pinned separately: a profile is only replayable
                # against the checkout its captures were frozen from.
                "pinned_revision": subprocess.run(
                    (
                        "git",
                        "-C",
                        str(REPOSITORY_ROOT / str(HOSTS[key]["repo_path"])),
                        "rev-parse",
                        "HEAD",
                    ),
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=60,
                ).stdout.strip(),
            }
            for key in used
        },
        "profiles": profiles,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    print(f"\nwrote {output.relative_to(REPOSITORY_ROOT)} with {len(profiles)} profiles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
