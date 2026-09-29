from __future__ import annotations

import hashlib
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from warptest.lazarus_upstream import (
    ConstructionCommandDefinition,
    FrozenUpstreamTestSource,
    LazarusUpstreamError,
    UpstreamTestDefinition,
    definitions_from_upstream_record,
    frozen_sources_from_upstream_record,
    run_lazarus_construction_test_subset,
    run_lazarus_per_trial_p2p,
    run_upstream_test_subset,
)


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "stable.txt").write_text("stable\n", encoding="utf-8")
    return source


def _checkpoint_restorer(
    clone: Path, receipt_path: Path, receipt: dict[str, object]
) -> dict[str, object]:
    """A public seam double that proves the isolated clone consumed the receipt."""

    loaded = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert loaded["checkpoint_sha256"] == receipt["checkpoint_sha256"]
    marker = clone / ".warptest" / "lazarus" / "restored-state.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(receipt["checkpoint_sha256"]), encoding="utf-8")
    return {
        "schema_version": 1,
        "state_restore_applied": True,
        "adapter": "test-isolated-checkpoint-restorer-v1",
        "checkpoint_sha256": receipt["checkpoint_sha256"],
        "checkpoint_kind": receipt["checkpoint_kind"],
        "receipt_sha256": receipt["payload_sha256"],
    }


def test_upstream_subset_runs_without_shell_in_an_isolated_cow_clone(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    definitions = {
        "p2p.write-only-in-clone": UpstreamTestDefinition(
            id="p2p.write-only-in-clone",
            argv=(
                sys.executable,
                "-c",
                "from pathlib import Path; Path('trial-only.txt').write_text('ok')",
            ),
            cwd=".",
            timeout_s=10.0,
            origin="upstream",
        )
    }

    result = run_upstream_test_subset(
        source,
        definitions,
        ("p2p.write-only-in-clone",),
        sandbox_root=tmp_path / "sandboxes",
    )

    assert result.complete is True
    assert result.passed is True
    assert result.results[0].exit_code == 0
    assert result.results[0].timed_out is False
    assert result.isolation == "apfs_cow_clone"
    assert not (source / "trial-only.txt").exists()


def test_upstream_subset_expands_registered_harness_tokens(tmp_path: Path) -> None:
    source = _source(tmp_path)
    harness = tmp_path / "harness"
    harness.mkdir()
    script = harness / "probe.py"
    script.write_text("raise SystemExit(0)\n", encoding="utf-8")
    definition = UpstreamTestDefinition(
        id="registered-probe",
        argv=("{python}", "{harness_root}/probe.py"),
        cwd=".",
        timeout_s=10,
        origin="upstream",
    )

    result = run_upstream_test_subset(
        source,
        {definition.id: definition},
        (definition.id,),
        sandbox_root=tmp_path / "sandboxes",
        harness_root=harness,
    )

    assert result.passed is True


def test_galata_title_selectors_escape_playwright_regular_expression_tokens(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    galata = source / "galata"
    galata.mkdir()
    definition = UpstreamTestDefinition(
        id="galata-literal-title",
        argv=(
            sys.executable,
            "-c",
            "import re,sys; raise SystemExit(0 if sys.argv[-1] == re.escape('Alt + A') else 9)",
            "--grep",
            "Alt + A",
        ),
        cwd="galata",
        timeout_s=10,
        origin="upstream",
    )

    result = run_upstream_test_subset(
        source,
        {definition.id: definition},
        (definition.id,),
        sandbox_root=tmp_path / "sandboxes",
    )

    assert result.passed is True


def test_upstream_subset_passes_trusted_git_only_to_the_runtime(tmp_path: Path) -> None:
    source = _source(tmp_path)
    trusted_git = tmp_path / "metadata.git"
    trusted_git.mkdir()
    definition = UpstreamTestDefinition(
        id="trusted-git",
        argv=(
            sys.executable,
            "-c",
            "import os, sys; sys.exit(0 if os.environ.get('WARPTEST_LUANTI_TRUSTED_GIT_DIR') else 9)",
        ),
        cwd=".",
        timeout_s=10,
        origin="upstream",
    )

    result = run_upstream_test_subset(
        source,
        {definition.id: definition},
        (definition.id,),
        sandbox_root=tmp_path / "sandboxes",
        trusted_git_dir=trusted_git,
    )

    assert result.passed is True


def test_upstream_subset_fails_closed_on_failure_timeout_and_missing_id(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    definitions = {
        "fails": UpstreamTestDefinition(
            id="fails",
            argv=(sys.executable, "-c", "raise SystemExit(7)"),
            cwd=".",
            timeout_s=10.0,
            origin="upstream",
        ),
        "times-out": UpstreamTestDefinition(
            id="times-out",
            argv=(sys.executable, "-c", "import time; time.sleep(2)"),
            cwd=".",
            timeout_s=0.05,
            origin="upstream",
        ),
    }

    failed = run_upstream_test_subset(
        source,
        definitions,
        ("fails",),
        sandbox_root=tmp_path / "failed-sandboxes",
    )
    timed_out = run_upstream_test_subset(
        source,
        definitions,
        ("times-out",),
        sandbox_root=tmp_path / "timeout-sandboxes",
    )
    missing = run_upstream_test_subset(
        source,
        definitions,
        ("not-registered",),
        sandbox_root=tmp_path / "missing-sandboxes",
    )

    assert failed.complete is True and failed.passed is False
    assert failed.results[0].exit_code == 7
    assert timed_out.complete is True and timed_out.passed is False
    assert timed_out.results[0].timed_out is True
    assert missing.complete is False and missing.passed is False
    assert "not-registered" in missing.failures[0]


def test_construction_subprocess_captures_survive_clone_cleanup_and_redact_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later receipt can classify failures after its disposable clone is gone."""

    source = _source(tmp_path)
    secret = "c1-construction-test-secret"
    monkeypatch.setenv("C1_CONSTRUCTION_TEST_TOKEN", secret)
    prepare = ConstructionCommandDefinition(
        id="registered-prepare",
        argv=(
            sys.executable,
            "-c",
            (
                "import os, sys; "
                "print('prepare-stdout-marker'); "
                "sys.stderr.write('TOKEN=' + os.environ['C1_CONSTRUCTION_TEST_TOKEN'] "
                "+ '; ephemeral_token=unregistered-secret');"
            ),
        ),
        cwd=".",
        timeout_s=30,
    )
    upstream = UpstreamTestDefinition(
        id="f2p-forensic-marker",
        argv=(
            sys.executable,
            "-c",
            (
                "import os, sys; "
                "print('upstream-stdout-marker'); "
                "sys.stderr.write('Bearer ' + os.environ['C1_CONSTRUCTION_TEST_TOKEN']); "
                "raise SystemExit(17)"
            ),
        ),
        cwd=".",
        timeout_s=30,
        origin="upstream",
    )
    sandbox = tmp_path / "sandboxes"

    result = run_lazarus_construction_test_subset(
        source,
        {upstream.id: upstream},
        (upstream.id,),
        sandbox_root=sandbox,
        prepare_commands=(prepare,),
    )

    # A non-zero F2P remains a normal measured failure; this change only
    # retains its forensic output and must not change construction semantics.
    assert result.complete is False
    assert result.passed is False
    assert not tuple(sandbox.glob("lazarus-construction-*"))
    prepare_capture = result.prepare_results[0].output_capture
    upstream_capture = result.results[0].output_capture
    assert prepare_capture.schema == "c1-lazarus-subprocess-output-v1"
    assert prepare_capture.stdout == "prepare-stdout-marker\n"
    assert secret not in prepare_capture.stderr
    assert "unregistered-secret" not in prepare_capture.stderr
    assert "<redacted>" in prepare_capture.stderr
    assert upstream_capture.stdout == "upstream-stdout-marker\n"
    assert secret not in upstream_capture.stderr
    assert "Bearer <redacted>" in upstream_capture.stderr


def test_upstream_subprocess_output_capture_is_bounded_to_a_retained_tail(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    upstream = UpstreamTestDefinition(
        id="large-output",
        argv=(sys.executable, "-c", "print('x' * 20000)"),
        cwd=".",
        timeout_s=30,
        origin="upstream",
    )

    result = run_upstream_test_subset(
        source,
        {upstream.id: upstream},
        (upstream.id,),
        sandbox_root=tmp_path / "sandboxes",
    )

    capture = result.results[0].output_capture
    assert capture.stdout_truncated is True
    assert capture.stdout.startswith(
        "[... output truncated; retained tail follows ...]"
    )
    assert len(capture.stdout.encode("utf-8")) <= 16 * 1024
    assert capture.stderr == ""
    assert capture.stderr_truncated is False


def test_upstream_definition_rejects_path_escape_shell_string_and_over_300s(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    invalid = {
        "escape": UpstreamTestDefinition(
            id="escape",
            argv=(sys.executable, "-V"),
            cwd="../outside",
            timeout_s=10.0,
            origin="upstream",
        )
    }
    with pytest.raises(LazarusUpstreamError, match="escapes"):
        run_upstream_test_subset(
            source,
            invalid,
            ("escape",),
            sandbox_root=tmp_path / "escape-sandboxes",
        )

    with pytest.raises(LazarusUpstreamError, match="argv"):
        UpstreamTestDefinition.from_mapping(
            {
                "id": "shell",
                "argv": "python -V && echo unsafe",
                "cwd": ".",
                "timeout_s": 10,
                "origin": "upstream",
            }
        )


def test_upstream_record_definitions_bind_operational_argv_to_frozen_test_ids() -> None:
    record = {
        "tests": [
            {
                "id": "f2p",
                "kind": "fail_to_pass",
                "provenance": "pull_request_body",
                "origin": "upstream",
                "argv": ["{python}", "-V"],
                "cwd": ".",
                "timeout_s": 30,
            },
            {
                "id": "p2p",
                "kind": "pass_to_pass",
                "provenance": "repository_test",
                "origin": "upstream",
                "argv": ["{python}", "-V"],
                "cwd": ".",
                "timeout_s": 30,
            },
        ]
    }

    definitions = definitions_from_upstream_record(record)

    assert tuple(definitions) == ("f2p", "p2p")
    assert definitions["f2p"].argv == ("{python}", "-V")
    broken = {"tests": [dict(record["tests"][0], id="f2p", argv="unsafe shell")]}
    with pytest.raises(LazarusUpstreamError, match="argv"):
        definitions_from_upstream_record(broken)
    with pytest.raises(LazarusUpstreamError, match="300"):
        UpstreamTestDefinition.from_mapping(
            {
                "id": "too-slow",
                "argv": [sys.executable, "-V"],
                "cwd": ".",
                "timeout_s": 301,
                "origin": "upstream",
            }
        )


def test_frozen_sources_are_derived_only_from_registered_construction_test_ids() -> (
    None
):
    record = {
        "tests": [
            {
                "id": "f2p",
                "upstream_test_path": "tests/regression.py",
                "upstream_test_sha256": "a" * 64,
                "frozen_test_sources": [
                    {
                        "source_path": "upstream-tests/tests/regression.py",
                        "destination_path": "tests/regression.py",
                        "sha256": "a" * 64,
                    },
                    {
                        "source_path": "upstream-tests/tests/fixture.bin",
                        "destination_path": "tests/fixture.bin",
                        "sha256": "c" * 64,
                    },
                ],
            },
            {
                "id": "p2p",
                "upstream_test_path": "tests/stable.py",
                "upstream_test_sha256": "b" * 64,
                "frozen_test_sources": [],
            },
        ]
    }

    sources = frozen_sources_from_upstream_record(record, ("f2p",))

    assert sources == (
        FrozenUpstreamTestSource(
            source_path="upstream-tests/tests/fixture.bin",
            destination_path="tests/fixture.bin",
            sha256="c" * 64,
        ),
        FrozenUpstreamTestSource(
            source_path="upstream-tests/tests/regression.py",
            destination_path="tests/regression.py",
            sha256="a" * 64,
        ),
    )
    with pytest.raises(LazarusUpstreamError, match="missing frozen test source"):
        frozen_sources_from_upstream_record(
            {"tests": [{"id": "f2p", "upstream_test_path": "tests/test.py"}]},
            ("f2p",),
        )


def test_per_trial_p2p_emits_the_phase0_evidence_contract_in_a_fresh_clone(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    definition = UpstreamTestDefinition(
        id="p2p",
        argv=(sys.executable, "-c", "raise SystemExit(0)"),
        cwd=".",
        timeout_s=10,
        origin="upstream",
    )

    evidence = run_lazarus_per_trial_p2p(
        task_id="lz-example",
        version="fixed",
        checkpoint_sha256="c" * 64,
        checkpoint_kind="semantic_state",
        source_root=source,
        definitions={definition.id: definition},
        test_ids=(definition.id,),
        sandbox_root=tmp_path / "sandboxes",
        checkpoint_restorer=_checkpoint_restorer,
    )

    assert evidence["schema_version"] == 1
    assert evidence["task_id"] == "lz-example"
    assert evidence["version"] == "fixed"
    assert evidence["completed"] is True
    assert evidence["passed"] is True
    assert evidence["timeout_s"] == 300.0
    assert evidence["isolation"] == "apfs_cow_clone"
    assert evidence["checkpoint_restore"] == {
        "schema_version": 1,
        "applied": True,
        "checkpoint_sha256": "c" * 64,
        "checkpoint_kind": "semantic_state",
        "runtime_restored": True,
        "path": ".warptest/lazarus/checkpoint-restore.json",
        "payload_sha256": evidence["checkpoint_restore"]["payload_sha256"],
        "state_restore": {
            "schema_version": 1,
            "state_restore_applied": True,
            "adapter": "test-isolated-checkpoint-restorer-v1",
            "checkpoint_sha256": "c" * 64,
            "checkpoint_kind": "semantic_state",
            "receipt_sha256": evidence["checkpoint_restore"]["payload_sha256"],
        },
    }
    assert evidence["results"] == [
        {
            "id": "p2p",
            "passed": True,
            "timed_out": False,
            "duration_ms": pytest.approx(evidence["results"][0]["duration_ms"]),
            "exit_code": 0,
            "stdout_sha256": evidence["results"][0]["stdout_sha256"],
            "stderr_sha256": evidence["results"][0]["stderr_sha256"],
            "output_capture": {
                "schema": "c1-lazarus-subprocess-output-v1",
                "schema_version": 1,
                "redaction": "environment_and_credential_markers_v1",
                "stdout": "",
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
            },
        }
    ]


def test_per_trial_p2p_rejects_non_lazarus_version_and_preserves_failure_evidence(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    definition = UpstreamTestDefinition(
        id="p2p-fails",
        argv=(sys.executable, "-c", "raise SystemExit(5)"),
        cwd=".",
        timeout_s=10,
        origin="upstream",
    )

    evidence = run_lazarus_per_trial_p2p(
        task_id="lz-example",
        version="buggy",
        checkpoint_sha256="c" * 64,
        checkpoint_kind="semantic_state",
        source_root=source,
        definitions={definition.id: definition},
        test_ids=(definition.id,),
        sandbox_root=tmp_path / "sandboxes",
        checkpoint_restorer=_checkpoint_restorer,
    )

    assert evidence["completed"] is True
    assert evidence["passed"] is False
    assert evidence["results"][0]["exit_code"] == 5
    with pytest.raises(LazarusUpstreamError, match="version"):
        run_lazarus_per_trial_p2p(
            task_id="lz-example",
            version="unbound",
            checkpoint_sha256="c" * 64,
            checkpoint_kind="semantic_state",
            source_root=source,
            definitions={definition.id: definition},
            test_ids=(definition.id,),
            sandbox_root=tmp_path / "sandboxes",
            checkpoint_restorer=_checkpoint_restorer,
        )


def test_per_trial_p2p_restores_the_bound_checkpoint_inside_its_isolated_clone(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    definition = UpstreamTestDefinition(
        id="restored-context",
        argv=(
            sys.executable,
            "-c",
            (
                "import json, os, sys; "
                "payload = json.load(open(os.environ['WARPTEST_LAZARUS_CHECKPOINT_RESTORE_PATH'])); "
                "marker = open('.warptest/lazarus/restored-state.txt').read(); "
                "sys.exit(0 if marker == 'd' * 64 and payload['checkpoint_sha256'] == 'd' * 64 and "
                "payload['checkpoint_kind'] == 'agent_s_from_start' and "
                "payload['runtime_restored'] is True else 9)"
            ),
        ),
        cwd=".",
        timeout_s=10,
        origin="upstream",
    )

    evidence = run_lazarus_per_trial_p2p(
        task_id="lz-example",
        version="buggy",
        checkpoint_sha256="d" * 64,
        checkpoint_kind="agent_s_from_start",
        source_root=source,
        definitions={definition.id: definition},
        test_ids=(definition.id,),
        sandbox_root=tmp_path / "sandboxes",
        checkpoint_restorer=_checkpoint_restorer,
    )

    assert evidence["passed"] is True
    restore = evidence["checkpoint_restore"]
    assert restore["applied"] is True
    assert restore["checkpoint_sha256"] == "d" * 64
    assert restore["checkpoint_kind"] == "agent_s_from_start"
    assert restore["state_restore"]["state_restore_applied"] is True


def test_per_trial_p2p_rejects_a_receipt_without_an_isolated_state_restorer(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    definition = UpstreamTestDefinition(
        id="p2p",
        argv=(sys.executable, "-c", "raise SystemExit(0)"),
        cwd=".",
        timeout_s=10,
        origin="upstream",
    )

    with pytest.raises(LazarusUpstreamError, match="state restorer"):
        run_lazarus_per_trial_p2p(
            task_id="lz-example",
            version="buggy",
            checkpoint_sha256="c" * 64,
            checkpoint_kind="semantic_state",
            source_root=source,
            definitions={definition.id: definition},
            test_ids=(definition.id,),
            sandbox_root=tmp_path / "sandboxes",
        )


def test_construction_subset_overlays_hash_bound_fixed_test_only_inside_clone(
    tmp_path: Path,
) -> None:
    buggy = _source(tmp_path)
    regression = buggy / "tests" / "regression.py"
    regression.parent.mkdir()
    regression.write_text("EXPECTED = 'buggy'\n", encoding="utf-8")
    frozen = tmp_path / "fixed-test-source"
    fixed_regression = frozen / "tests" / "regression.py"
    fixed_regression.parent.mkdir(parents=True)
    fixed_regression.write_text("EXPECTED = 'fixed'\n", encoding="utf-8")
    fixed_sha = hashlib.sha256(fixed_regression.read_bytes()).hexdigest()
    build = ConstructionCommandDefinition(
        id="compile-fixed-regression",
        argv=(
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "assert \"EXPECTED = 'fixed'\" in Path('tests/regression.py').read_text(); "
                "Path('build').mkdir(); Path('build/compiled.txt').write_text('fixed')"
            ),
        ),
        cwd=".",
        timeout_s=600,
    )
    test = UpstreamTestDefinition(
        id="upstream-fixed-regression",
        argv=(
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "raise SystemExit(0 if Path('build/compiled.txt').read_text() == 'fixed' "
                "and \"EXPECTED = 'fixed'\" in Path('tests/regression.py').read_text() else 9)"
            ),
        ),
        cwd=".",
        timeout_s=10,
        origin="upstream",
    )

    result = run_lazarus_construction_test_subset(
        buggy,
        {test.id: test},
        (test.id,),
        sandbox_root=tmp_path / "sandboxes",
        frozen_source_root=frozen,
        frozen_sources=(
            FrozenUpstreamTestSource(
                source_path="tests/regression.py",
                destination_path="tests/regression.py",
                sha256=fixed_sha,
            ),
        ),
        prepare_commands=(build,),
    )

    assert result.complete is True
    assert result.passed is True
    assert result.prepare_results[0].id == "compile-fixed-regression"
    assert result.overlays[0].sha256 == fixed_sha
    assert regression.read_text(encoding="utf-8") == "EXPECTED = 'buggy'\n"
    assert not (buggy / "build").exists()


def test_construction_subset_scopes_a_runtime_environment_to_the_disposable_clone(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    lifecycle: list[str] = []

    @contextmanager
    def runtime_environment(prepared: Path):
        assert (prepared / "stable.txt").read_text(encoding="utf-8") == "stable\n"
        lifecycle.append("entered")
        try:
            yield {"TARGET_URL": "http://127.0.0.1:31415"}
        finally:
            lifecycle.append("closed")

    upstream = UpstreamTestDefinition(
        id="runtime-bound-upstream-test",
        argv=(
            sys.executable,
            "-c",
            (
                "import os; "
                "raise SystemExit(0 if os.environ.get('TARGET_URL') == "
                "'http://127.0.0.1:31415' else 23)"
            ),
        ),
        cwd=".",
        timeout_s=10,
        origin="upstream",
    )

    result = run_lazarus_construction_test_subset(
        source,
        {upstream.id: upstream},
        (upstream.id,),
        sandbox_root=tmp_path / "sandboxes",
        runtime_environment_factory=runtime_environment,
    )

    assert result.complete is True
    assert result.passed is True
    assert lifecycle == ["entered", "closed"]


def test_construction_subset_rejects_frozen_source_drift_and_path_escape(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    frozen = tmp_path / "fixed-test-source"
    frozen.mkdir()
    (frozen / "test.py").write_text("fixed\n", encoding="utf-8")
    definition = UpstreamTestDefinition(
        id="upstream-test",
        argv=(sys.executable, "-c", "raise SystemExit(0)"),
        cwd=".",
        timeout_s=10,
        origin="upstream",
    )

    with pytest.raises(LazarusUpstreamError, match="content SHA drift"):
        run_lazarus_construction_test_subset(
            source,
            {definition.id: definition},
            (definition.id,),
            sandbox_root=tmp_path / "drift-sandboxes",
            frozen_source_root=frozen,
            frozen_sources=(
                FrozenUpstreamTestSource(
                    source_path="test.py",
                    destination_path="test.py",
                    sha256="0" * 64,
                ),
            ),
        )

    real_sha = hashlib.sha256((frozen / "test.py").read_bytes()).hexdigest()
    for source_path, destination_path in (
        ("../test.py", "test.py"),
        ("test.py", "../test.py"),
    ):
        with pytest.raises(LazarusUpstreamError, match="escapes"):
            run_lazarus_construction_test_subset(
                source,
                {definition.id: definition},
                (definition.id,),
                sandbox_root=tmp_path
                / f"escape-{len(source_path)}-{len(destination_path)}",
                frozen_source_root=frozen,
                frozen_sources=(
                    FrozenUpstreamTestSource(
                        source_path=source_path,
                        destination_path=destination_path,
                        sha256=real_sha,
                    ),
                ),
            )

    linked = frozen / "linked.py"
    linked.symlink_to("test.py")
    with pytest.raises(LazarusUpstreamError, match="contains a symlink"):
        run_lazarus_construction_test_subset(
            source,
            {definition.id: definition},
            (definition.id,),
            sandbox_root=tmp_path / "symlink-sandboxes",
            frozen_source_root=frozen,
            frozen_sources=(
                FrozenUpstreamTestSource(
                    source_path="linked.py",
                    destination_path="test.py",
                    sha256=real_sha,
                ),
            ),
        )

    linked_destination = source / "linked-destination.py"
    linked_destination.symlink_to("stable.txt")
    with pytest.raises(LazarusUpstreamError, match="contains a symlink"):
        run_lazarus_construction_test_subset(
            source,
            {definition.id: definition},
            (definition.id,),
            sandbox_root=tmp_path / "destination-symlink-sandboxes",
            frozen_source_root=frozen,
            frozen_sources=(
                FrozenUpstreamTestSource(
                    source_path="test.py",
                    destination_path="linked-destination.py",
                    sha256=real_sha,
                ),
            ),
        )


def test_construction_setup_has_a_separate_long_timeout_and_fails_closed(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    upstream = UpstreamTestDefinition(
        id="must-not-run-after-build-failure",
        argv=(
            sys.executable,
            "-c",
            "from pathlib import Path; Path('test-ran').write_text('yes')",
        ),
        cwd=".",
        timeout_s=10,
        origin="upstream",
    )
    failed_build = ConstructionCommandDefinition(
        id="compiled-setup",
        argv=(sys.executable, "-c", "raise SystemExit(17)"),
        cwd=".",
        timeout_s=600,
    )

    result = run_lazarus_construction_test_subset(
        source,
        {upstream.id: upstream},
        (upstream.id,),
        sandbox_root=tmp_path / "sandboxes",
        prepare_commands=(failed_build,),
    )

    assert result.complete is False
    assert result.passed is False
    assert result.prepare_results[0].exit_code == 17
    assert result.results == ()
    assert "compiled-setup" in result.failures[0]
    assert not (source / "test-ran").exists()


def test_construction_subset_preserves_a_registered_version_incompatibility(
    tmp_path: Path,
) -> None:
    """A frozen-test compiler failure is a candidate terminal, not an outage."""

    source = _source(tmp_path)
    upstream = UpstreamTestDefinition(
        id="must-not-run-after-incompatible-build",
        argv=(sys.executable, "-c", "raise SystemExit(0)"),
        cwd=".",
        timeout_s=10,
        origin="upstream",
    )
    incompatible_build = ConstructionCommandDefinition(
        id="registered-frozen-test-compile",
        argv=(sys.executable, "-c", "raise SystemExit(2)"),
        cwd=".",
        timeout_s=600,
        failure_class="version_incompatible",
    )

    result = run_lazarus_construction_test_subset(
        source,
        {upstream.id: upstream},
        (upstream.id,),
        sandbox_root=tmp_path / "sandboxes",
        prepare_commands=(incompatible_build,),
    )

    assert result.complete is False
    assert result.passed is False
    assert result.terminal_failure_class == "version_incompatible"
