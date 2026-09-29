#!/usr/bin/env python3
"""Mine merged upstream repairs that ship no fail-to-pass test.

The strict C1-Lazarus screening requires every candidate to carry its own
relevant regression test.  The P2P-only track admits the opposite population:
a merged repair for one authoritative bug whose defect is not expressible as a
test assertion, graded instead from a visual differential plus an existing
repository control test.

This tool only reads.  It enumerates pull-request numbers from the pinned
submodule history, so every candidate's repair is by construction present in the
checkout the pair is materialized from, then reads the upstream authority through
``gh`` for the mechanical Gate 1/2/5 signals:

* Gate 1 -- merged, with a merge commit reachable from the pinned revision;
* Gate 2 -- exactly one authoritative bug issue;
* Gate 5 -- the change adds no test material at all.

Two further signals are scored rather than enforced, because they are judgement
calls a reviewer must make: whether the touched surface is observable in a
screenshot (the P2P-only track's only defect evidence), and which existing
repository test can serve as the Gate 6 control.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from warptest.c1_lazarus_selection.patches import (  # noqa: E402
    is_visual_production_path,
)
from warptest.c1_lazarus_selection.patches import _is_test  # noqa: E402

DEFAULT_OUTPUT = Path("reports/lazarus-p2p-only-candidates.json")

# "Not a bug" and "Won't fix" contain the token but deny it, so the deny list is
# checked first.
_BUG_LABEL_DENY = ("not a bug", "won't fix", "wontfix", "invalid")
_BUG_LABEL_ALLOW = ("bug", "regression", "crash")
_CLOSING_KEYWORD_TEMPLATE = (
    r"(?i)\b(?:fix|fixes|fixed|close|closes|closed|resolve|resolves|resolved)\s+"
    r"(?:https://github\.com/{slug}/issues/|#)(\d+)"
)


def closing_keyword_pattern(slug: str) -> re.Pattern[str]:
    """Return a same-repository closing-keyword matcher.

    The URL form has to name this repository.  Accepting any project's issue URL
    would resolve a foreign ``/issues/153`` against this repository's issue 153,
    which is a different defect entirely.
    """

    return re.compile(_CLOSING_KEYWORD_TEMPLATE.format(slug=re.escape(slug)))
_LOG_PR_NUMBER = re.compile(r"(?:^|\s)(?:Merge pull request )?#(\d+)|\(#(\d+)\)\s*$")


@dataclass(frozen=True)
class HostSpec:
    """One upstream repository the P2P-only track may mine."""

    key: str
    owner: str
    name: str
    repo_path: str
    # A production path is scored as screenshot-observable when it matches one of
    # these fragments.  The fragments name rendering, widget and input surfaces.
    visual_fragments: tuple[str, ...]
    # Where the repository keeps tests that already pass on both versions.
    test_globs: tuple[str, ...]
    # OpenRA squash subjects omit `(#N)`, so numbers have to come from the
    # commit-to-PR association rather than from `git log` subjects.
    pr_source: str = "git_log"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


HOSTS: dict[str, HostSpec] = {
    "jupyterlab": HostSpec(
        key="jupyterlab",
        owner="jupyterlab",
        name="jupyterlab",
        repo_path="games/jupyterlab",
        visual_fragments=(
            "apputils",
            "cells",
            "codemirror",
            "completer",
            "csvviewer",
            "debugger",
            "docmanager",
            "docregistry",
            "filebrowser",
            "htmlviewer",
            "imageviewer",
            "launcher",
            "logconsole",
            "mainmenu",
            "markdownviewer",
            "notebook",
            "outputarea",
            "rendermime",
            "running",
            "settingeditor",
            "statusbar",
            "terminal",
            "toolbar",
            "tooltip",
            "ui-components",
        ),
        test_globs=("packages/*/test/**/*.spec.ts", "packages/*/test/*.spec.ts"),
    ),
    "luanti": HostSpec(
        key="luanti",
        owner="luanti-org",
        name="luanti",
        repo_path="games/luanti",
        visual_fragments=(
            "builtin/",
            "src/client/",
            "src/gui/",
            "src/hud",
            "src/script/lua_api/",
            "formspec",
            "font",
            "hud",
            "inventory",
        ),
        test_globs=(
            "src/unittest/*.cpp",
            "games/devtest/mods/unittests/*.lua",
        ),
    ),
    "openra": HostSpec(
        key="openra",
        owner="OpenRA",
        name="OpenRA",
        repo_path="games/OpenRA",
        visual_fragments=(
            "OpenRA.Game/Graphics/",
            "OpenRA.Game/Widgets/",
            "OpenRA.Mods.Common/Traits/Render",
            "OpenRA.Mods.Common/Widgets/",
            "chrome/",
            "chrome.yaml",
            "Widgets/Logic/",
        ),
        test_globs=("OpenRA.Test/**/*.cs",),
        pr_source="associated_commits",
    ),
}


@dataclass
class Candidate:
    """One mechanically screened P2P-only candidate."""

    host: str
    number: int
    title: str
    merged_at: str
    merge_commit: str
    issue_number: int
    issue_title: str
    issue_labels: tuple[str, ...]
    issue_link_source: str
    production_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]
    visual_paths: tuple[str, ...]
    control_tests: tuple[str, ...]
    rejections: list[str] = field(default_factory=list)
    inverse_status: str = "not_checked"

    @property
    def admitted(self) -> bool:
        return not self.rejections

    @property
    def score(self) -> int:
        """Rank admitted candidates by how likely a four-frame capture works."""

        score = 0
        score += 30 if self.visual_paths else 0
        score += 20 if self.control_tests else 0
        # A small production diff keeps the inverse patch reviewable and keeps the
        # after-differential attributable to one change.
        if len(self.production_paths) <= 2:
            score += 20
        elif len(self.production_paths) <= 5:
            score += 10
        if any("bug" in label.casefold() for label in self.issue_labels):
            score += 15
        score += 15 if self.issue_link_source == "closing_reference" else 0
        # A stylesheet or chrome-layout-only repair is almost always observable.
        if self.production_paths and all(
            path.rsplit(".", 1)[-1] in {"css", "scss", "sass", "less", "yaml", "yml"}
            for path in self.production_paths
        ):
            score += 25
        return score

    def to_value(self) -> dict[str, object]:
        return {
            "host": self.host,
            "pr": self.number,
            "title": self.title,
            "merged_at": self.merged_at,
            "merge_commit": self.merge_commit,
            "issue": {
                "number": self.issue_number,
                "title": self.issue_title,
                "labels": list(self.issue_labels),
                "link_source": self.issue_link_source,
            },
            "production_paths": list(self.production_paths),
            "changed_paths": list(self.changed_paths),
            "visual_paths": list(self.visual_paths),
            "control_tests": list(self.control_tests),
            "inverse_status": self.inverse_status,
            "admitted": self.admitted,
            "rejections": list(self.rejections),
            "score": self.score,
        }


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo), *args),
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )
    return result.stdout


def pinned_pull_request_numbers(repo: Path, *, limit: int) -> tuple[int, ...]:
    """Return pull-request numbers reachable from the pinned revision.

    Reading numbers out of the pinned history is what makes the "repair present
    in the checkout" property free: a number that is not in this log describes a
    repair the materialized pair could not contain.
    """

    log = run_git(repo, "log", "--format=%H%x00%s", f"-{limit}", "HEAD")
    numbers: list[int] = []
    seen: set[int] = set()
    for line in log.splitlines():
        if "\x00" not in line:
            continue
        _, subject = line.split("\x00", 1)
        for match in _LOG_PR_NUMBER.finditer(subject):
            raw = match.group(1) or match.group(2)
            if raw is None:
                continue
            number = int(raw)
            if number not in seen:
                seen.add(number)
                numbers.append(number)
    return tuple(numbers)


def pinned_commit_shas(repo: Path, *, limit: int) -> tuple[str, ...]:
    log = run_git(repo, "log", "--format=%H", f"-{limit}", "HEAD")
    return tuple(line for line in log.splitlines() if len(line) == 40)


def associated_pull_request_numbers(
    host: HostSpec, shas: Sequence[str], *, batch_size: int = 20
) -> tuple[int, ...]:
    """Return PR numbers GitHub associates with commits in the pinned history.

    OpenRA's squash subjects do not carry `(#N)`, so reading the log is not
    enough to know which repairs the checkout contains.  These associations
    are discovery hints only: ``mine_host`` separately requires the PR's merge
    commit to be one of these pinned commits before Gate 1 can pass.
    """

    numbers: list[int] = []
    seen: set[int] = set()
    total = len(shas)
    for start in range(0, total, batch_size):
        batch = shas[start : start + batch_size]
        aliases = []
        for index, sha in enumerate(batch):
            aliases.append(
                f'c{index}: object(oid:"{sha}") {{ ... on Commit {{ '
                "associatedPullRequests(first: 3) { nodes { number } } } }"
            )
        query = (
            "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){"
            + " ".join(aliases)
            + "}}"
        )
        try:
            data = gh_graphql(query, owner=host.owner, name=host.name)
        except RuntimeError as error:
            raise RuntimeError(
                "associated-PR coverage is incomplete for pinned commits "
                f"{start}..{start + len(batch) - 1}: {error}"
            ) from error
        repository = data.get("repository")
        if not isinstance(repository, Mapping):
            continue
        for index in range(len(batch)):
            node = repository.get(f"c{index}")
            if not isinstance(node, Mapping):
                continue
            pulls = node.get("associatedPullRequests")
            pull_nodes = (
                pulls.get("nodes") or () if isinstance(pulls, Mapping) else ()
            )
            for item in pull_nodes:
                if not isinstance(item, Mapping):
                    continue
                number = int(item.get("number") or 0)
                if number and number not in seen:
                    seen.add(number)
                    numbers.append(number)
        done = start + len(batch)
        if done == total or done % 200 == 0:
            print(f"  associated PRs: {done}/{total} commits, {len(numbers)} numbers")
    return tuple(numbers)


def pinned_pull_request_numbers_for_host(
    host: HostSpec, repo: Path, *, limit: int
) -> tuple[int, ...]:
    if host.pr_source == "associated_commits":
        shas = pinned_commit_shas(repo, limit=limit)
        print(f"  {len(shas)} pinned commits; resolving associated pull requests")
        return associated_pull_request_numbers(host, shas)
    return pinned_pull_request_numbers(repo, limit=limit)


def fetch_pull_request_diff(host: HostSpec, number: int) -> bytes:
    result = subprocess.run(
        (
            "gh",
            "api",
            f"repos/{host.slug}/pulls/{number}",
            "-H",
            "Accept: application/vnd.github.v3.diff",
        ),
        capture_output=True,
        check=False,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def inverse_patch_applies(repo: Path, diff: bytes) -> tuple[bool, str]:
    """Check that the upstream diff can be reversed on the pinned checkout.

    A later change that rewrote the same lines makes an earlier repair
    un-revertible: the code the patch expects no longer exists at the pin.  Such
    a candidate cannot be materialized at all, so it is worth rejecting during
    mining rather than after a reviewer has picked it.
    """

    if not diff.strip():
        return False, "empty_diff"
    result = subprocess.run(
        (
            "git",
            "-C",
            str(repo),
            "apply",
            "-R",
            "--check",
            "--whitespace=nowarn",
            "-",
        ),
        input=diff,
        capture_output=True,
        check=False,
        timeout=300,
    )
    if result.returncode == 0:
        return True, "reverse_applies_clean"
    detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
    return False, detail[0] if detail else "reverse_apply_failed"


def gh_graphql(
    query: str, **variables: object
) -> Mapping[str, object]:
    argv = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        argv.extend(["-F", f"{key}={value}"])
    last_error = "gh graphql failed"
    for attempt in range(1, 6):
        result = subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=300
        )
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            if payload.get("errors"):
                raise RuntimeError(f"gh graphql errors: {payload['errors']}")
            data = payload.get("data")
            if not isinstance(data, Mapping):
                raise RuntimeError("gh graphql returned no data")
            return data
        last_error = result.stderr.strip() or result.stdout.strip() or "gh graphql failed"
        # GitHub returns 503 when the GraphQL edge is overloaded; backing off
        # and retrying keeps a transient outage from silently dropping a batch.
        if "503" not in last_error and "502" not in last_error:
            break
        time.sleep(min(20, 2 ** attempt))
    raise RuntimeError(f"gh graphql failed: {last_error}")


_PR_FRAGMENT = """
    p{alias}: pullRequest(number: {number}) {{
      number
      title
      merged
      mergedAt
      body
      mergeCommit {{ oid }}
      closingIssuesReferences(first: 10) {{
        nodes {{ number title labels(first: 30) {{ nodes {{ name }} }} }}
      }}
      files(first: 100) {{ nodes {{ path }} }}
    }}
"""


def _batches(values: Sequence[int], size: int) -> Iterator[Sequence[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def fetch_pull_requests(
    host: HostSpec, numbers: Sequence[int], *, batch_size: int = 20
) -> dict[int, Mapping[str, object]]:
    fetched: dict[int, Mapping[str, object]] = {}
    for batch in _batches(list(numbers), batch_size):
        body = "".join(
            _PR_FRAGMENT.format(alias=index, number=number)
            for index, number in enumerate(batch)
        )
        query = (
            "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){"
            + body
            + "}}"
        )
        try:
            data = gh_graphql(query, owner=host.owner, name=host.name)
        except RuntimeError as error:
            if len(batch) == 1:
                raise RuntimeError(
                    "pull-request coverage is incomplete: "
                    f"#{batch[0]} could not be fetched: {error}"
                ) from error
            print(
                f"  ! batch {batch[0]}..{batch[-1]} split after: {error}",
                file=sys.stderr,
            )
            fetched.update(fetch_pull_requests(host, batch, batch_size=1))
            continue
        repository = data.get("repository")
        if not isinstance(repository, Mapping):
            continue
        for index, number in enumerate(batch):
            node = repository.get(f"p{index}")
            if isinstance(node, Mapping):
                fetched[number] = node
    missing = [number for number in numbers if number not in fetched]
    if missing:
        preview = ", ".join(f"#{number}" for number in missing[:10])
        suffix = "..." if len(missing) > 10 else ""
        raise RuntimeError(
            "pull-request coverage is incomplete: missing " + preview + suffix
        )
    return fetched


def fetch_issues(
    host: HostSpec, numbers: Sequence[int], *, batch_size: int = 20
) -> dict[int, Mapping[str, object]]:
    fetched: dict[int, Mapping[str, object]] = {}
    for batch in _batches(numbers, batch_size):
        body = "".join(
            f'i{index}: issue(number: {number}) {{ number title state '
            f"labels(first: 30) {{ nodes {{ name }} }} }}"
            for index, number in enumerate(batch)
        )
        query = (
            "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){"
            + body
            + "}}"
        )
        try:
            data = gh_graphql(query, owner=host.owner, name=host.name)
        except RuntimeError:
            continue
        repository = data.get("repository")
        if not isinstance(repository, Mapping):
            continue
        for index, number in enumerate(batch):
            node = repository.get(f"i{index}")
            if isinstance(node, Mapping):
                fetched[number] = node
    return fetched


def is_bug_label(labels: Iterable[str]) -> bool:
    folded = [label.casefold() for label in labels]
    if any(deny in label for label in folded for deny in _BUG_LABEL_DENY):
        return False
    return any(allow in label for label in folded for allow in _BUG_LABEL_ALLOW)


def repository_test_inventory(host: HostSpec, repo: Path) -> tuple[str, ...]:
    paths: set[str] = set()
    for pattern in host.test_globs:
        for match in repo.glob(pattern):
            if match.is_file():
                paths.add(match.relative_to(repo).as_posix())
    return tuple(sorted(paths))


def _path_tokens(path: str) -> frozenset[str]:
    generic = {
        "src",
        "test",
        "tests",
        "spec",
        "specs",
        "packages",
        "lib",
        "index",
        "common",
        "game",
        "mods",
        "openra",
        "unittest",
        "unittests",
        "builtin",
    }
    return frozenset(
        token
        for token in re.split(r"[^a-z0-9]+", path.casefold())
        if len(token) >= 4 and token not in generic
    )


def related_control_tests(
    production_paths: Sequence[str], inventory: Sequence[str]
) -> tuple[str, ...]:
    """Return existing repository tests that share a module token with the repair."""

    wanted: set[str] = set()
    for production in production_paths:
        tokens = _path_tokens(production)
        if not tokens:
            continue
        for test in inventory:
            if tokens & _path_tokens(test):
                wanted.add(test)
    return tuple(sorted(wanted))


def screen_candidate(
    host: HostSpec,
    node: Mapping[str, object],
    *,
    inventory: Sequence[str],
    issue_cache: Mapping[int, Mapping[str, object]],
    pinned_merge_commits: frozenset[str] | None = None,
) -> Candidate | None:
    number = int(node.get("number") or 0)
    if not number:
        return None
    rejections: list[str] = []

    if node.get("merged") is not True:
        return None
    merge_commit = node.get("mergeCommit")
    merge_oid = (
        str(merge_commit.get("oid")) if isinstance(merge_commit, Mapping) else ""
    )
    if not merge_oid:
        rejections.append("gate1_no_merge_commit")
    elif (
        pinned_merge_commits is not None
        and merge_oid not in pinned_merge_commits
    ):
        rejections.append("gate1_merge_commit_not_in_pinned_history")

    issue_number = 0
    issue_title = ""
    issue_labels: tuple[str, ...] = ()
    link_source = "none"
    closing = node.get("closingIssuesReferences")
    closing_nodes = (
        [item for item in closing.get("nodes") or () if isinstance(item, Mapping)]
        if isinstance(closing, Mapping)
        else []
    )
    if len(closing_nodes) == 1:
        issue = closing_nodes[0]
        issue_number = int(issue.get("number") or 0)
        issue_title = str(issue.get("title") or "")
        labels = issue.get("labels")
        issue_labels = tuple(
            str(item.get("name"))
            for item in (labels.get("nodes") or () if isinstance(labels, Mapping) else ())
            if isinstance(item, Mapping) and item.get("name")
        )
        link_source = "closing_reference"
    elif len(closing_nodes) > 1:
        rejections.append(
            f"gate2_multiple_authoritative_issues({len(closing_nodes)})"
        )
    else:
        # The platform relationship API can miss a link the author wrote in prose.
        # Recovering it keeps a real single-defect repair minable, and the source
        # is recorded so Gate 1/2 evidence stays honest about where it came from.
        referenced = {
            int(match.group(1))
            for match in closing_keyword_pattern(host.slug).finditer(
                str(node.get("body") or "")
            )
        }
        referenced.discard(number)
        if len(referenced) == 1:
            issue_number = referenced.pop()
            cached = issue_cache.get(issue_number)
            if isinstance(cached, Mapping):
                issue_title = str(cached.get("title") or "")
                labels = cached.get("labels")
                issue_labels = tuple(
                    str(item.get("name"))
                    for item in (
                        labels.get("nodes") or ()
                        if isinstance(labels, Mapping)
                        else ()
                    )
                    if isinstance(item, Mapping) and item.get("name")
                )
                link_source = "body_keyword"
            else:
                rejections.append("gate2_referenced_issue_unavailable")
        elif len(referenced) > 1:
            rejections.append(
                f"gate2_multiple_referenced_issues({len(referenced)})"
            )
        else:
            rejections.append("gate2_no_authoritative_issue")

    if issue_number and not is_bug_label(issue_labels):
        rejections.append("gate2_issue_is_not_labelled_a_bug")

    files = node.get("files")
    changed = tuple(
        sorted(
            str(item.get("path"))
            for item in (files.get("nodes") or () if isinstance(files, Mapping) else ())
            if isinstance(item, Mapping) and item.get("path")
        )
    )
    if not changed:
        rejections.append("gate3_no_changed_files")
    test_paths = tuple(path for path in changed if _is_test(path))
    # A stylesheet carries a rendered defect as directly as a source file, so this
    # track counts it as repair material.
    production_paths = tuple(path for path in changed if is_visual_production_path(path))
    if test_paths:
        # This is the P2P-only track's exclusion rule: a change carrying test
        # material belongs to the strict track, not here.
        rejections.append(f"gate5_carries_test_material({len(test_paths)})")
    if not production_paths:
        rejections.append("gate3_no_production_repair")

    visual_paths = tuple(
        path
        for path in production_paths
        if any(
            fragment.casefold() in path.casefold() for fragment in host.visual_fragments
        )
    )
    control_tests = related_control_tests(production_paths, inventory)

    return Candidate(
        host=host.key,
        number=number,
        title=str(node.get("title") or ""),
        merged_at=str(node.get("mergedAt") or ""),
        merge_commit=merge_oid,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_labels=issue_labels,
        issue_link_source=link_source,
        production_paths=production_paths,
        changed_paths=changed,
        visual_paths=visual_paths,
        control_tests=control_tests[:12],
        rejections=rejections,
    )


def verify_inverse_patches(
    host: HostSpec, repo: Path, candidates: Sequence[Candidate]
) -> None:
    """Reject admitted candidates whose repair can no longer be reversed."""

    for candidate in candidates:
        if not candidate.admitted:
            continue
        try:
            diff = fetch_pull_request_diff(host, candidate.number)
        except RuntimeError as error:
            candidate.inverse_status = f"diff_unavailable: {error}"
            candidate.rejections.append("gate3_diff_unavailable")
            continue
        applies, detail = inverse_patch_applies(repo, diff)
        candidate.inverse_status = detail
        if not applies:
            candidate.rejections.append("gate3_inverse_does_not_apply_at_pin")


def mine_host(host: HostSpec, *, log_limit: int, verify_inverse: bool) -> list[Candidate]:
    repo = REPOSITORY_ROOT / host.repo_path
    if not repo.is_dir():
        print(f"  ! {host.repo_path} is not checked out", file=sys.stderr)
        return []
    numbers = pinned_pull_request_numbers_for_host(host, repo, limit=log_limit)
    pinned_merge_commits = (
        frozenset(pinned_commit_shas(repo, limit=log_limit))
        if host.pr_source == "associated_commits"
        else None
    )
    print(f"  {len(numbers)} pull-request numbers in the pinned history")
    nodes = fetch_pull_requests(host, numbers)
    print(f"  {len(nodes)} fetched from {host.slug}")

    # Pre-resolve issues that only appear as prose references.
    wanted_issues: set[int] = set()
    for number, node in nodes.items():
        closing = node.get("closingIssuesReferences")
        closing_nodes = (
            [item for item in closing.get("nodes") or () if isinstance(item, Mapping)]
            if isinstance(closing, Mapping)
            else []
        )
        if closing_nodes:
            continue
        for match in closing_keyword_pattern(host.slug).finditer(
            str(node.get("body") or "")
        ):
            candidate_issue = int(match.group(1))
            if candidate_issue != number:
                wanted_issues.add(candidate_issue)
    issue_cache = fetch_issues(host, sorted(wanted_issues)) if wanted_issues else {}

    inventory = repository_test_inventory(host, repo)
    print(f"  {len(inventory)} existing repository tests available as controls")

    candidates: list[Candidate] = []
    for number in sorted(nodes, reverse=True):
        candidate = screen_candidate(
            host,
            nodes[number],
            inventory=inventory,
            issue_cache=issue_cache,
            pinned_merge_commits=pinned_merge_commits,
        )
        if candidate is not None:
            candidates.append(candidate)
    if verify_inverse:
        pending = [item for item in candidates if item.admitted]
        print(f"  verifying the inverse patch of {len(pending)} candidates at the pin")
        verify_inverse_patches(host, repo, candidates)
        print(f"  {sum(1 for item in candidates if item.admitted)} still admitted")
    return candidates


def render_report(candidates: Sequence[Candidate]) -> str:
    admitted = [item for item in candidates if item.admitted]
    admitted.sort(key=lambda item: (-item.score, item.host, -item.number))
    lines = [
        f"Screened {len(candidates)} merged pull requests; "
        f"{len(admitted)} pass the mechanical Gate 1/2/5 filter.",
        "",
    ]
    for item in admitted:
        lines.append(
            f"[{item.score:>3}] {item.host}#{item.number} (issue #{item.issue_number}) "
            f"{item.title[:70]}"
        )
        lines.append(
            f"        production={len(item.production_paths)} "
            f"visual={len(item.visual_paths)} controls={len(item.control_tests)} "
            f"link={item.issue_link_source} labels={','.join(item.issue_labels) or '-'}"
        )
        for path in item.production_paths[:4]:
            lines.append(f"        + {path}")
        if item.control_tests:
            lines.append(f"        control: {item.control_tests[0]}")
    if not admitted:
        reasons: dict[str, int] = {}
        for item in candidates:
            for reason in item.rejections:
                key = reason.split("(", 1)[0]
                reasons[key] = reasons.get(key, 0) + 1
        lines.append("Rejection histogram:")
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {count:>5}  {reason}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        action="append",
        choices=sorted(HOSTS),
        help="restrict mining to one host; repeatable (default: all)",
    )
    parser.add_argument(
        "--log-limit",
        type=int,
        default=4000,
        help="how many pinned commits to read pull-request numbers from",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--verify-inverse",
        action="store_true",
        help="reject candidates whose repair cannot be reversed on the pinned tree",
    )
    args = parser.parse_args()

    selected = [HOSTS[key] for key in (args.host or sorted(HOSTS))]
    candidates: list[Candidate] = []
    for host in selected:
        print(f"== {host.slug} ==")
        candidates.extend(
            mine_host(
                host,
                log_limit=args.log_limit,
                verify_inverse=args.verify_inverse,
            )
        )

    report = render_report(candidates)
    print()
    print(report)

    output = args.output
    if not output.is_absolute():
        output = REPOSITORY_ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "c1-lazarus-p2p-only-candidates-v1",
        "schema_version": 1,
        "hosts": [host.slug for host in selected],
        "screened": len(candidates),
        "admitted": sum(1 for item in candidates if item.admitted),
        "candidates": [item.to_value() for item in candidates],
    }
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote {output.relative_to(REPOSITORY_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
