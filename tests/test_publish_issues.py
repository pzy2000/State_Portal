"""Tests for guarded, idempotent issue publishing."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from scripts import publish_issues


def _write_manifest(tmp_path, *, marker_tag="issue-fingerprint", issues=None):
    issues = (
        issues
        if issues is not None
        else [
            {
                "title": "First issue",
                "body_file": "01.md",
                "labels": ["bug"],
                "fingerprint": "first-fp",
            }
        ]
    )
    (tmp_path / "01.md").write_text(
        "body\n<!-- issue-fingerprint: first-fp -->\n", encoding="utf-8"
    )
    (tmp_path / "02.md").write_text(
        "body\n<!-- issue-fingerprint: second-fp -->\n", encoding="utf-8"
    )
    manifest = {
        "marker_tag": marker_tag,
        "source_repo": "upstream/project",
        "default_publish_repo": "fork/project",
        "issues": issues,
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_dry_run_does_not_require_fingerprints_or_write_journal(
    tmp_path, monkeypatch
) -> None:
    manifest = _write_manifest(
        tmp_path,
        marker_tag=None,
        issues=[{"title": "Draft", "body_file": "01.md", "labels": []}],
    )
    monkeypatch.setattr(sys, "argv", ["publish_issues.py", "--manifest", str(manifest)])
    assert publish_issues.main() == 0
    assert not (tmp_path / "publish-results.json").exists()


def test_live_publish_journals_success_and_resumes_after_partial_failure(
    tmp_path, monkeypatch
) -> None:
    issues = [
        {
            "title": "First issue",
            "body_file": "01.md",
            "labels": [],
            "fingerprint": "first-fp",
        },
        {
            "title": "Second issue",
            "body_file": "02.md",
            "labels": [],
            "fingerprint": "second-fp",
        },
    ]
    manifest = _write_manifest(tmp_path, issues=issues)
    monkeypatch.setattr(publish_issues, "_find_remote_issue", lambda *_args: None)

    first_calls = []

    def first_create(_repo, title, _body, _labels, _dry_run):
        first_calls.append(title)
        return "https://example.test/1" if title == "First issue" else None

    monkeypatch.setattr(publish_issues, "_create_issue", first_create)
    monkeypatch.setattr(
        sys, "argv", ["publish_issues.py", "--manifest", str(manifest), "--yes"]
    )
    assert publish_issues.main() == 1
    assert first_calls == ["First issue", "Second issue"]
    journal = json.loads(
        (tmp_path / "publish-results.json").read_text(encoding="utf-8")
    )
    assert "fork/project::first-fp" in journal["entries"]
    assert "fork/project::second-fp" not in journal["entries"]

    retry_calls = []

    def retry_create(_repo, title, _body, _labels, _dry_run):
        retry_calls.append(title)
        return "https://example.test/2"

    monkeypatch.setattr(publish_issues, "_create_issue", retry_create)
    assert publish_issues.main() == 0
    assert retry_calls == ["Second issue"]
    journal = json.loads(
        (tmp_path / "publish-results.json").read_text(encoding="utf-8")
    )
    assert set(journal["entries"]) == {
        "fork/project::first-fp",
        "fork/project::second-fp",
    }


def test_live_publish_skips_remote_exact_marker_and_journals_it(
    tmp_path, monkeypatch
) -> None:
    manifest = _write_manifest(tmp_path)
    monkeypatch.setattr(
        publish_issues,
        "_find_remote_issue",
        lambda *_args: "https://example.test/existing",
    )
    monkeypatch.setattr(
        publish_issues,
        "_create_issue",
        lambda *_args: pytest.fail("existing issue must not be created again"),
    )
    monkeypatch.setattr(
        sys, "argv", ["publish_issues.py", "--manifest", str(manifest), "--yes"]
    )
    assert publish_issues.main() == 0
    journal = json.loads(
        (tmp_path / "publish-results.json").read_text(encoding="utf-8")
    )
    assert journal["entries"]["fork/project::first-fp"]["status"] == "existing"


def test_remote_search_requires_exact_html_marker(monkeypatch) -> None:
    payload = {
        "items": [
            {"body": "mentions first-fp", "html_url": "https://example.test/wrong"},
            {
                "body": "<!-- issue-fingerprint: first-fp -->",
                "html_url": "https://example.test/right",
            },
        ]
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, json.dumps(payload), ""
        ),
    )
    assert (
        publish_issues._find_remote_issue(
            "fork/project", "issue-fingerprint", "first-fp"
        )
        == "https://example.test/right"
    )


def test_live_publish_fails_closed_without_marker_or_fingerprint(
    tmp_path, monkeypatch
) -> None:
    no_marker = _write_manifest(tmp_path, marker_tag=None)
    monkeypatch.setattr(
        sys, "argv", ["publish_issues.py", "--manifest", str(no_marker), "--yes"]
    )
    with pytest.raises(SystemExit, match="marker_tag"):
        publish_issues.main()

    no_fingerprint = _write_manifest(
        tmp_path,
        issues=[{"title": "Unsafe", "body_file": "01.md", "labels": []}],
    )
    monkeypatch.setattr(
        sys, "argv", ["publish_issues.py", "--manifest", str(no_fingerprint), "--yes"]
    )
    with pytest.raises(SystemExit, match="fingerprint"):
        publish_issues.main()


def test_upstream_target_still_requires_explicit_acknowledgement() -> None:
    manifest = {"source_repo": "upstream/project"}
    with pytest.raises(SystemExit, match="Refusing to publish UPSTREAM"):
        publish_issues._resolve_repo(manifest, "upstream", False)
    assert (
        publish_issues._resolve_repo(manifest, "upstream", True) == "upstream/project"
    )
