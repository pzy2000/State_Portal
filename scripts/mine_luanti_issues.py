#!/usr/bin/env python3
"""Mine merged bug-fix PRs from the Luanti UPSTREAM repo into frozen bug cards.

SWE-bench-style collection, step 1. Uses ``gh api graphql`` against the upstream
resolved from ``benchmarks/upstream/sources.yaml`` (``luanti-org/luanti``), NOT the
fork. For every merged PR that looks like a fix (a bug-ish label OR a body closing
reference), it emits one provenance-carrying bug card to ``bug_cards.jsonl``.

Design points (see warptest/upstream_contracts.py):
  * pre/post fix SHA are derived from the MERGE COMMIT (parents[0] / oid), never
    from ``baseRefOid`` (Contract 2).
  * Bug-ish labels are read DYNAMICALLY from the repo's actual label set and
    selected by a regex (``--label-regex``); nothing is hardcoded.
  * ``closingIssuesReferences`` (GraphQL), body ``fixes/closes #N``, and other
    ``#N`` references are stored SEPARATELY.
  * PRs with NO closing issue are allowed (a fix PR need not close an issue).
  * ``--since/--until/--limit`` + cursor pagination make the dataset freezable.

Usage:
    python scripts/mine_luanti_issues.py --limit 40 \
        --out benchmarks/upstream/luanti/bug_cards.jsonl
    python scripts/mine_luanti_issues.py --since 2024-01-01 --until 2025-01-01
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from warptest.bug_cards import new_card, validate_card  # noqa: E402
from warptest.upstream_contracts import (  # noqa: E402
    derive_pre_post_sha,
    parse_body_closing_refs,
    parse_body_related_refs,
    sha256_text,
)
from warptest.upstream_sources import get_source  # noqa: E402

_PR_QUERY = """
query($owner:String!, $name:String!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequests(states: MERGED, first: 50, after: $cursor,
                 orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body url mergedAt baseRefOid headRefOid
        labels(first: 30) { nodes { name } }
        mergeCommit { oid parents(first: 5) { nodes { oid } } }
        closingIssuesReferences(first: 20) { nodes { number title url } }
        files(first: 100) { nodes { path } }
      }
    }
  }
}
"""


def _gh_graphql(owner: str, name: str, cursor: str | None) -> dict[str, Any]:
    cmd = [
        "gh", "api", "graphql",
        "-f", f"query={_PR_QUERY}",
        "-F", f"owner={owner}",
        "-F", f"name={name}",
    ]
    if cursor:
        cmd += ["-f", f"cursor={cursor}"]
    else:
        cmd += ["-F", "cursor="]  # empty -> treated as null by the server default
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh graphql failed: {proc.stderr.strip()[:500]}")
    return json.loads(proc.stdout)


def _fetch_pr_diff(owner: str, name: str, number: int) -> str | None:
    """Unified diff for patch_digest. Tolerates failure (returns None)."""

    proc = subprocess.run(
        ["gh", "pr", "diff", str(number), "--repo", f"{owner}/{name}"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def _within(dt_iso: str | None, since: str | None, until: str | None) -> bool:
    if not dt_iso:
        return False
    day = dt_iso[:10]
    if since and day < since:
        return False
    if until and day > until:
        return False
    return True


def _is_fix_pr(labels: list[str], label_re: re.Pattern, closing: list, body_refs: list[int]) -> tuple[bool, list[str]]:
    matched = [lb for lb in labels if label_re.search(lb)]
    if matched:
        return True, matched
    # No bug-ish label, but it explicitly closes an issue -> still a fix PR.
    if closing or body_refs:
        return True, matched
    return False, matched


def mine(
    *,
    owner: str,
    name: str,
    fork: str,
    limit: int,
    since: str | None,
    until: str | None,
    label_regex: str,
    with_diff: bool,
) -> list[dict[str, Any]]:
    label_re = re.compile(label_regex, re.IGNORECASE)
    cards: list[dict[str, Any]] = []
    cursor: str | None = None
    scanned = 0
    fetched_at = datetime.now(timezone.utc).isoformat()
    while len(cards) < limit:
        data = _gh_graphql(owner, name, cursor)
        conn = data["data"]["repository"]["pullRequests"]
        for pr in conn["nodes"]:
            scanned += 1
            merged_at = pr.get("mergedAt")
            if not _within(merged_at, since, until):
                continue
            labels = [n["name"] for n in (pr.get("labels", {}) or {}).get("nodes", [])]
            closing = [
                {"number": n["number"], "title": n.get("title"), "url": n.get("url")}
                for n in (pr.get("closingIssuesReferences", {}) or {}).get("nodes", [])
            ]
            body = pr.get("body") or ""
            body_closing = parse_body_closing_refs(body)
            is_fix, matched = _is_fix_pr(labels, label_re, closing, body_closing)
            if not is_fix:
                continue
            try:
                shas = derive_pre_post_sha(pr.get("mergeCommit"))
            except ValueError as err:
                # Squashed/rebased merges without a usable merge commit cannot be
                # bounded per Contract 2 -> skip with a note rather than mis-bound.
                print(f"  skip PR #{pr['number']}: {err}")
                continue
            changed = [n["path"] for n in (pr.get("files", {}) or {}).get("nodes", [])]
            closing_nums = {c["number"] for c in closing}
            related = parse_body_related_refs(body, exclude=set(body_closing) | closing_nums)
            patch_digest = None
            if with_diff:
                diff = _fetch_pr_diff(owner, name, pr["number"])
                if diff is not None:
                    patch_digest = sha256_text(diff)
            card = new_card(
                benchmark="luanti",
                upstream_repo=f"{owner}/{name}",
                fork_repo=fork,
                fetched_at=fetched_at,
                source_url=pr.get("url"),
                pr_number=pr["number"],
                title=pr.get("title"),
                body=body,
                labels=labels,
                matched_labels=matched,
                closing_issues=closing,
                body_closing_refs=body_closing,
                related_issues=related,
                merged_at=merged_at,
                merge_commit=shas.merge_commit,
                pre_fix_sha=shas.pre_fix_sha,
                post_fix_sha=shas.post_fix_sha,
                base_ref_oid=pr.get("baseRefOid"),
                head_ref_oid=pr.get("headRefOid"),
                changed_files=changed,
                patch_digest=patch_digest,
            )
            problems = validate_card(card)
            if problems:
                print(f"  skip PR #{pr['number']}: invalid card ({'; '.join(problems)})")
                continue
            cards.append(card)
            print(f"  card PR #{pr['number']}  labels={matched or '-'}  files={len(changed)}  pre={shas.pre_fix_sha[:8]} post={shas.post_fix_sha[:8]}")
            if len(cards) >= limit:
                break
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    print(f"scanned {scanned} merged PRs; collected {len(cards)} fix cards")
    return cards


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--benchmark", default="luanti")
    ap.add_argument("--out", default="benchmarks/upstream/luanti/bug_cards.jsonl")
    ap.add_argument("--limit", type=int, default=40, help="Max cards to collect (freeze size).")
    ap.add_argument("--since", default=None, help="Only PRs merged on/after YYYY-MM-DD.")
    ap.add_argument("--until", default=None, help="Only PRs merged on/before YYYY-MM-DD.")
    ap.add_argument(
        "--label-regex",
        default=r"bug|regression|crash|fix",
        help="Case-insensitive regex selecting bug-ish labels from the repo's actual label set.",
    )
    ap.add_argument("--no-diff", action="store_true", help="Skip fetching PR diffs (no patch_digest).")
    args = ap.parse_args()

    src = get_source(args.benchmark, ROOT)
    owner, name = src["source_repo"].split("/", 1)
    cards = mine(
        owner=owner,
        name=name,
        fork=src.get("fork"),
        limit=args.limit,
        since=args.since,
        until=args.until,
        label_regex=args.label_regex,
        with_diff=not args.no_diff,
    )
    out = (ROOT / args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for card in cards:
            fh.write(json.dumps(card, ensure_ascii=False) + "\n")
    print(f"Wrote {len(cards)} bug cards to {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
