#!/usr/bin/env python3
"""Publish issue drafts from a build_*_issues manifest to GitHub (guarded).

This is the ONE place that decides WHICH GitHub repo an issue is filed to. The
draft builders (e.g. ``build_luanti_issues_split.py``) only render Markdown + a
manifest; they never touch GitHub.

Safety model:
  * Default target is the manifest's ``default_publish_repo`` (the FORK).
  * ``--target fork`` (default) files to the fork; ``--target upstream`` files to
    ``source_repo`` and is REFUSED unless ``--i-understand-upstream`` is also given.
  * Default mode is DRY-RUN (prints what it would do). Nothing is created without
    ``--yes``.
  * Live mode requires the manifest fingerprint marker, skips exact remote matches,
    and journals each success to ``publish-results.json`` for safe partial retries.

Usage:
  # dry-run to the fork (default):
  python scripts/publish_issues.py --manifest .../github-issues/manifest.json
  # actually file to the fork:
  python scripts/publish_issues.py --manifest ... --yes
  # file upstream (guarded, explicit):
  python scripts/publish_issues.py --manifest ... --target upstream --i-understand-upstream --yes
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any


def _resolve_repo(
    manifest: dict[str, Any], target: str, understand_upstream: bool
) -> str:
    if target == "fork":
        repo = manifest.get("default_publish_repo")
        if not repo:
            raise SystemExit("manifest has no default_publish_repo")
        return repo
    if target == "upstream":
        if not understand_upstream:
            raise SystemExit(
                "Refusing to publish UPSTREAM without --i-understand-upstream. "
                "Upstream issues are public bug reports to the original project; "
                "re-run with --target upstream --i-understand-upstream --yes if intended."
            )
        repo = manifest.get("source_repo")
        if not repo:
            raise SystemExit("manifest has no source_repo")
        return repo
    raise SystemExit(f"unknown --target {target!r}")


def _create_issue(
    repo: str, title: str, body_path: Path, labels: list[str], dry_run: bool
) -> str | None:
    cmd = [
        "gh",
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        title,
        "--body-file",
        str(body_path),
    ]
    for label in labels or []:
        cmd += ["--label", label]
    if dry_run:
        print(f"  DRY-RUN would run: {' '.join(cmd)}")
        return None
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"  ERROR filing {title!r}: {proc.stderr.strip()[:300]}")
        return None
    url = proc.stdout.strip()
    print(f"  created: {url}")
    return url


def _journal_key(repo: str, fingerprint: str) -> str:
    return f"{repo}::{fingerprint}"


def _load_journal(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "entries": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        raise ValueError(f"malformed publish journal: {path}")
    return data


def _write_journal(path: Path, journal: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(journal, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temp.replace(path)


def _find_remote_issue(repo: str, marker_tag: str, fingerprint: str) -> str | None:
    marker = f"<!-- {marker_tag}: {fingerprint} -->"
    query = f'repo:{repo} is:issue in:body "{marker_tag}: {fingerprint}"'
    proc = subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "GET",
            "search/issues",
            "-f",
            f"q={query}",
            "-f",
            "per_page=20",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"GitHub duplicate check failed: {proc.stderr.strip()[:300]}"
        )
    payload = json.loads(proc.stdout)
    for item in payload.get("items") or []:
        if marker in str(item.get("body") or ""):
            return str(item.get("html_url") or "") or None
    return None


def _validate_live_manifest(
    manifest: dict[str, Any], issues: list[dict[str, Any]]
) -> str:
    marker_tag = str(manifest.get("marker_tag") or "").strip()
    if not marker_tag:
        raise SystemExit(
            "manifest has no marker_tag; refusing non-idempotent live publishing"
        )
    missing = [
        str(issue.get("title") or "<untitled>")
        for issue in issues
        if not issue.get("fingerprint")
    ]
    if missing:
        raise SystemExit(
            "manifest issue(s) have no fingerprint; refusing non-idempotent live publishing: "
            + ", ".join(missing)
        )
    return marker_tag


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--manifest", required=True, help="Path to a github-issues/manifest.json."
    )
    ap.add_argument("--target", choices=["fork", "upstream"], default="fork")
    ap.add_argument(
        "--i-understand-upstream",
        action="store_true",
        help="Required to publish to upstream.",
    )
    ap.add_argument(
        "--yes",
        action="store_true",
        help="Actually create issues (default is dry-run).",
    )
    ap.add_argument(
        "--no-labels",
        action="store_true",
        help="Do not pass labels (upstream may reject unknown labels).",
    )
    args = ap.parse_args()

    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    repo = _resolve_repo(manifest, args.target, args.i_understand_upstream)
    dry_run = not args.yes

    print(
        f"Target repo: {repo}  (target={args.target}, {'DRY-RUN' if dry_run else 'LIVE'})"
    )
    issues = manifest.get("issues") or []
    marker_tag = ""
    journal_path = manifest_path.parent / "publish-results.json"
    journal: dict[str, Any] = {"schema_version": 1, "entries": {}}
    if not dry_run:
        marker_tag = _validate_live_manifest(manifest, issues)
        try:
            journal = _load_journal(journal_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise SystemExit(str(error)) from error
    created = 0
    skipped = 0
    failed = 0
    for issue in issues:
        body_path = manifest_path.parent / issue["body_file"]
        labels = [] if args.no_labels else issue.get("labels") or []
        print(f"- {issue.get('title')}")
        if not dry_run:
            fingerprint = str(issue["fingerprint"])
            key = _journal_key(repo, fingerprint)
            recorded = (journal.get("entries") or {}).get(key)
            if isinstance(recorded, dict) and recorded.get("url"):
                print(f"  skipped (journal): {recorded['url']}")
                skipped += 1
                continue
            try:
                existing_url = _find_remote_issue(repo, marker_tag, fingerprint)
            except (RuntimeError, json.JSONDecodeError) as error:
                print(f"  ERROR checking {issue['title']!r}: {error}")
                failed += 1
                continue
            if existing_url:
                print(f"  skipped (existing marker): {existing_url}")
                journal.setdefault("entries", {})[key] = {
                    "repo": repo,
                    "fingerprint": fingerprint,
                    "url": existing_url,
                    "status": "existing",
                }
                _write_journal(journal_path, journal)
                skipped += 1
                continue
        url = _create_issue(repo, issue["title"], body_path, labels, dry_run)
        if url:
            created += 1
            fingerprint = str(issue["fingerprint"])
            journal.setdefault("entries", {})[_journal_key(repo, fingerprint)] = {
                "repo": repo,
                "fingerprint": fingerprint,
                "url": url,
                "status": "created",
            }
            _write_journal(journal_path, journal)
        elif not dry_run:
            failed += 1
    if dry_run:
        print(
            f"DRY-RUN complete: {len(issues)} issue(s) would be filed to {repo}. Re-run with --yes to file."
        )
    else:
        print(
            f"Publish complete: created={created} skipped={skipped} failed={failed} "
            f"total={len(issues)} repo={repo}."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
