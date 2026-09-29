from __future__ import annotations

import os
from pathlib import Path

from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.operations import (
    ProcessCapture,
    _apply_buggy_overlay,
    evaluate_test_manifest,
    evaluate_test_repetitions,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-operations/test",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )


def _write_patch(root: Path, number: int, patch: bytes, test_paths: list[str]) -> None:
    raw = root / "raw" / "migrated" / "jupyterlab" / str(number) / "patch.diff"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(patch)
    write_stable_artifact(
        root,
        artifact_id=f"candidate_test_patch/jupyterlab/{number}/screening/v1",
        artifact_kind="candidate_test_patch",
        semantic_version="1.0",
        created_at="2026-08-13T00:00:00Z",
        inputs=(),
        payload={
            "candidate": {
                "repository_key": "jupyterlab",
                "change_number": number,
            },
            "outcome": "patch_eligible",
            "complete": True,
            "production_paths": ["packages/example/src/index.ts"],
            "test_paths": test_paths,
            "support_paths": [],
            "raw_patch_path": raw.relative_to(root).as_posix(),
        },
        authorization=_authorization(),
    )


def test_test_manifest_rejects_comment_only_and_snapshot_only_changes(
    tmp_path: Path,
) -> None:
    comment_patch = b"""\
diff --git a/packages/example/src/index.ts b/packages/example/src/index.ts
--- a/packages/example/src/index.ts
+++ b/packages/example/src/index.ts
@@ -1 +1 @@
-export const value = 1;
+export const value = 2;
diff --git a/packages/example/test/index.spec.ts b/packages/example/test/index.spec.ts
--- a/packages/example/test/index.spec.ts
+++ b/packages/example/test/index.spec.ts
@@ -1 +1,2 @@
 test('value', () => expect(value).toBe(1));
+// Keep this test visible in CI.
"""
    snapshot_patch = b"""\
diff --git a/packages/example/src/index.ts b/packages/example/src/index.ts
--- a/packages/example/src/index.ts
+++ b/packages/example/src/index.ts
@@ -1 +1 @@
-export const value = 1;
+export const value = 2;
diff --git a/packages/example/test/index.spec.ts-snapshots/value.json b/packages/example/test/index.spec.ts-snapshots/value.json
--- a/packages/example/test/index.spec.ts-snapshots/value.json
+++ b/packages/example/test/index.spec.ts-snapshots/value.json
@@ -1 +1 @@
-{"value": 1}
+{"value": 2}
"""
    _write_patch(
        tmp_path,
        1,
        comment_patch,
        ["packages/example/test/index.spec.ts"],
    )
    _write_patch(
        tmp_path,
        2,
        snapshot_patch,
        ["packages/example/test/index.spec.ts-snapshots/value.json"],
    )

    comment = evaluate_test_manifest(tmp_path, CandidateKey("jupyterlab", 1))
    snapshot = evaluate_test_manifest(tmp_path, CandidateKey("jupyterlab", 2))

    assert comment.state == "terminal"
    assert comment.outcome == "quality_rejected"
    assert snapshot.state == "terminal"
    assert snapshot.outcome == "quality_rejected"


def test_test_manifest_records_only_executable_regression_tests(tmp_path: Path) -> None:
    patch = b"""\
diff --git a/packages/example/src/index.ts b/packages/example/src/index.ts
--- a/packages/example/src/index.ts
+++ b/packages/example/src/index.ts
@@ -1 +1 @@
-export const value = 1;
+export const value = 2;
diff --git a/packages/example/test/index.spec.ts b/packages/example/test/index.spec.ts
--- a/packages/example/test/index.spec.ts
+++ b/packages/example/test/index.spec.ts
@@ -1 +1,4 @@
 test('old', () => expect(value).toBe(1));
+test('new behavior', () => {
+  expect(value).toBe(2);
+});
"""
    _write_patch(
        tmp_path,
        3,
        patch,
        ["packages/example/test/index.spec.ts"],
    )

    decision = evaluate_test_manifest(tmp_path, CandidateKey("jupyterlab", 3))

    assert decision.state == "passed"
    assert decision.details == {
        "executable_test_paths": ["packages/example/test/index.spec.ts"],
        "test_count": 1,
    }


def test_repetitions_persist_each_run_and_resume_without_reexecution(
    tmp_path: Path, monkeypatch
) -> None:
    candidate = CandidateKey("jupyterlab", 4)
    patch = b"""\
diff --git a/packages/example/src/index.ts b/packages/example/src/index.ts
--- a/packages/example/src/index.ts
+++ b/packages/example/src/index.ts
@@ -1 +1 @@
-export const value = 1;
+export const value = 2;
diff --git a/packages/example/test/index.spec.ts b/packages/example/test/index.spec.ts
--- a/packages/example/test/index.spec.ts
+++ b/packages/example/test/index.spec.ts
@@ -1 +1,2 @@
 test('old', () => expect(value).toBe(1));
+test('new', () => expect(value).toBe(2));
"""
    _write_patch(
        tmp_path,
        4,
        patch,
        ["packages/example/test/index.spec.ts"],
    )
    patch_artifact = load_stable_artifact(
        tmp_path,
        artifact_id="candidate_test_patch/jupyterlab/4/screening/v1",
        artifact_kind="candidate_test_patch",
        semantic_version="1.0",
    )
    write_stable_artifact(
        tmp_path,
        artifact_id="candidate_stage/jupyterlab/4/test_manifest/v1",
        artifact_kind="candidate_stage",
        semantic_version="1.0",
        created_at="2026-08-13T00:00:01Z",
        inputs=(patch_artifact.reference,),
        payload={
            "candidate": candidate.to_value(),
            "stage": "test_manifest",
            "state": "passed",
            "outcome": None,
            "details": {
                "executable_test_paths": ["packages/example/test/index.spec.ts"],
                "test_count": 1,
            },
            "complete": True,
        },
        authorization=_authorization(),
    )
    workspace = tmp_path / "workspace"
    (workspace / "games" / "jupyterlab").mkdir(parents=True)
    calls: list[str] = []

    def clone(_source: Path, destination: Path) -> None:
        destination.mkdir()

    def execute(
        *, version: str, root: Path, argv: tuple[str, ...], cwd: str, timeout_s: float
    ) -> ProcessCapture:
        del root, argv, cwd, timeout_s
        calls.append(version)
        return ProcessCapture(
            status="completed",
            exit_code=0 if version == "fixed" else 1,
            stdout=f"{version} output".encode(),
            stderr=b"",
            elapsed_ms=12.5,
        )

    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.operations._clone_repository", clone
    )
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.operations._apply_buggy_overlay",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.operations._execute_process", execute
    )

    first = evaluate_test_repetitions(
        tmp_path,
        workspace_root=workspace,
        candidate=candidate,
        authorization=_authorization(),
    )
    assert first.state == "passed"
    assert calls == ["fixed", "buggy"] * 3
    assert first.details["fixed_passes"] == 3
    assert first.details["buggy_failures"] == 3
    assert len(first.details["attempt_artifact_ids"]) == 6

    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.operations._execute_process",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("completed test attempt was executed again")
        ),
    )
    resumed = evaluate_test_repetitions(
        tmp_path,
        workspace_root=workspace,
        candidate=candidate,
        authorization=_authorization(),
    )
    assert resumed.details == first.details


def test_buggy_overlay_reverses_production_but_keeps_regression_test(
    tmp_path: Path,
) -> None:
    source = tmp_path / "packages" / "example" / "src" / "index.ts"
    test = tmp_path / "packages" / "example" / "test" / "index.spec.ts"
    source.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    source.write_text("export const value = 2;\n")
    test.write_text("test('new', () => expect(value).toBe(2));\n")
    patch = b"""\
diff --git a/packages/example/src/index.ts b/packages/example/src/index.ts
--- a/packages/example/src/index.ts
+++ b/packages/example/src/index.ts
@@ -1 +1 @@
-export const value = 1;
+export const value = 2;
diff --git a/packages/example/test/index.spec.ts b/packages/example/test/index.spec.ts
--- a/packages/example/test/index.spec.ts
+++ b/packages/example/test/index.spec.ts
@@ -0,0 +1 @@
+test('new', () => expect(value).toBe(2));
"""

    _apply_buggy_overlay(
        root=tmp_path,
        patch=patch,
        production_paths=("packages/example/src/index.ts",),
    )

    assert source.read_text() == "export const value = 1;\n"
    assert test.read_text() == "test('new', () => expect(value).toBe(2));\n"
