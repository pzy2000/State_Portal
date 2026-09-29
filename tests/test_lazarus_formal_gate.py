from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import warptest.lazarus_openra_hotkey_gate as openra_gate

from warptest.lazarus_formal_gate import (
    run_formal_construction_test_matrix,
    run_registered_construction_test_matrix,
)
from warptest.lazarus_upstream import ConstructionCommandDefinition


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_formal_construction_runner_qualifies_fixed_buggy_truth_table(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    frozen = evidence / "upstream-tests"
    frozen.mkdir(parents=True)
    (frozen / "f2p.txt").write_text("frozen\n", encoding="utf-8")
    (frozen / "p2p.txt").write_text("frozen\n", encoding="utf-8")
    (frozen / "p2p-extra.txt").write_text("frozen\n", encoding="utf-8")
    roots = {version: tmp_path / version for version in ("fixed", "buggy")}
    for version, root in roots.items():
        root.mkdir()
        (root / "f2p.txt").write_text("old\n", encoding="utf-8")
        (root / "p2p.txt").write_text("old\n", encoding="utf-8")
        (root / "f2p-outcome.txt").write_text(
            "pass\n" if version == "fixed" else "fail\n", encoding="utf-8"
        )
        (root / "p2p-outcome.txt").write_text("pass\n", encoding="utf-8")
        (root / "p2p-extra-outcome.txt").write_text("pass\n", encoding="utf-8")
    command = (
        "import pathlib,sys;"
        "sys.exit(0 if pathlib.Path(sys.argv[1]).read_text().strip()=='pass' else 1)"
    )
    tests = []
    for kind, name in (
        ("fail_to_pass", "f2p"),
        ("pass_to_pass", "p2p"),
        ("pass_to_pass", "p2p-extra"),
    ):
        tests.append(
            {
                "id": name,
                "kind": kind,
                "origin": "upstream",
                "cwd": ".",
                "timeout_s": 30,
                "argv": [sys.executable, "-c", command, f"{name}-outcome.txt"],
                "frozen_test_sources": [
                    {
                        "source_path": f"upstream-tests/{name}.txt",
                        "destination_path": f"{name}.txt",
                        "sha256": _sha(frozen / f"{name}.txt"),
                    }
                ],
            }
        )
    (evidence / "upstream.json").write_text(
        json.dumps(
            {
                "task_id": "lz-test",
                "record_sha256": "a" * 64,
                "patch_sha256": "b" * 64,
                "tests": tests,
            }
        ),
        encoding="utf-8",
    )

    report = run_formal_construction_test_matrix(
        task_id="lz-test",
        evidence_root=evidence,
        fixed_root=roots["fixed"],
        buggy_root=roots["buggy"],
        sandbox_root=tmp_path / "sandbox",
        harness_root=tmp_path,
    )

    assert report["complete"] is True
    assert report["qualified"] is True
    assert report["fixed_tests"] == {"f2p": True, "p2p": True, "p2p-extra": True}
    assert report["buggy_tests"] == {"f2p": False, "p2p": True, "p2p-extra": True}
    assert len(report["executions"]) == 2


def test_formal_construction_prepare_failure_is_a_structured_infrastructure_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A non-terminal setup failure must not become a fake F2P/P2P table."""

    evidence = tmp_path / "evidence"
    frozen = evidence / "upstream-tests"
    frozen.mkdir(parents=True)
    (frozen / "f2p.txt").write_text("frozen\n", encoding="utf-8")
    (frozen / "p2p.txt").write_text("frozen\n", encoding="utf-8")
    roots = {version: tmp_path / version for version in ("fixed", "buggy")}
    for root in roots.values():
        root.mkdir()
    tests = []
    for kind, name in (("fail_to_pass", "f2p"), ("pass_to_pass", "p2p")):
        tests.append(
            {
                "id": name,
                "kind": kind,
                "origin": "upstream",
                "cwd": ".",
                "timeout_s": 30,
                "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                "frozen_test_sources": [
                    {
                        "source_path": f"upstream-tests/{name}.txt",
                        "destination_path": f"{name}.txt",
                        "sha256": _sha(frozen / f"{name}.txt"),
                    }
                ],
            }
        )
    (evidence / "upstream.json").write_text(
        json.dumps(
            {
                "task_id": "lz-prepare-failure",
                "record_sha256": "a" * 64,
                "patch_sha256": "b" * 64,
                "tests": tests,
            }
        ),
        encoding="utf-8",
    )
    failed_prepare = ConstructionCommandDefinition(
        id="registered-prepare",
        argv=(
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('formal-prepare-forensic-marker'); raise SystemExit(17)",
        ),
        cwd=".",
        timeout_s=30,
    )
    monkeypatch.setattr(
        "warptest.lazarus_formal_gate.formal_construction_prepare_commands",
        lambda task_id, *, test_cwds: (
            (failed_prepare,)
            if task_id == "lz-prepare-failure" and test_cwds == (".", ".")
            else ()
        ),
    )

    report = run_formal_construction_test_matrix(
        task_id="lz-prepare-failure",
        evidence_root=evidence,
        fixed_root=roots["fixed"],
        buggy_root=roots["buggy"],
        sandbox_root=tmp_path / "sandbox",
        harness_root=tmp_path,
    )

    assert report["complete"] is False
    assert report["qualified"] is False
    assert report["fixed_tests"] == {}
    assert report["buggy_tests"] == {}
    assert report["terminal_failure_class"] is None
    assert report["failures"] == [
        "fixed: construction command registered-prepare exited 17",
        "buggy: construction command registered-prepare exited 17",
    ]
    assert [execution["results"] for execution in report["executions"]] == [(), ()]
    assert [
        execution["prepare_results"][0]["exit_code"]
        for execution in report["executions"]
    ] == [17, 17]
    assert [execution["failures"] for execution in report["executions"]] == [
        ("construction command registered-prepare exited 17",),
        ("construction command registered-prepare exited 17",),
    ]
    assert not tuple((tmp_path / "sandbox").glob("lazarus-construction-*"))
    assert [
        execution["prepare_results"][0]["output_capture"]
        for execution in report["executions"]
    ] == [
        {
            "schema": "c1-lazarus-subprocess-output-v1",
            "schema_version": 1,
            "redaction": "environment_and_credential_markers_v1",
            "stdout": "",
            "stderr": "formal-prepare-forensic-marker",
            "stdout_truncated": False,
            "stderr_truncated": False,
        },
        {
            "schema": "c1-lazarus-subprocess-output-v1",
            "schema_version": 1,
            "redaction": "environment_and_credential_markers_v1",
            "stdout": "",
            "stderr": "formal-prepare-forensic-marker",
            "stdout_truncated": False,
            "stderr_truncated": False,
        },
    ]


def test_luanti_cpp_construction_overlays_then_rebuilds_each_version(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The frozen C++ source must affect both clone-local binaries.

    The fixed and buggy roots deliberately begin with the same obsolete test
    source.  The registered preparation command makes its mock ``bin/luanti``
    from the overlaid test and product-specific bytes, so running it before
    the overlay or only for the fixed clone would fail this truth table.
    """

    task_id = "lz-luanti-14586-full-crafting-shift-move"
    evidence = tmp_path / "evidence"
    frozen_test = evidence / "upstream-tests/src/unittest/test_moveaction.cpp"
    frozen_test.parent.mkdir(parents=True)
    frozen_test.write_text("fixed-test\n", encoding="utf-8")
    roots = {version: tmp_path / version for version in ("fixed", "buggy")}
    for version, root in roots.items():
        source = root / "src/unittest/test_moveaction.cpp"
        source.parent.mkdir(parents=True)
        source.write_text("obsolete-test\n", encoding="utf-8")
        (root / "product.txt").write_text(version + "\n", encoding="utf-8")
        stale_cache = root / "build-lazarus-unittests/CMakeCache.txt"
        stale_cache.parent.mkdir()
        stale_cache.write_text(
            "CMAKE_CACHEFILE_DIR:INTERNAL=/missing/staging/prebuilt/" + version,
            encoding="utf-8",
        )

    reconfigure_program = (
        "from pathlib import Path; "
        "assert Path('src/unittest/test_moveaction.cpp').read_text() == 'fixed-test\\n'; "
        "cache=Path('build-lazarus-unittests/CMakeCache.txt'); "
        "cache.write_text('CMAKE_CACHEFILE_DIR:INTERNAL=' + str(Path.cwd()))"
    )
    build_program = (
        "from pathlib import Path; "
        "assert Path('src/unittest/test_moveaction.cpp').read_text() == 'fixed-test\\n'; "
        "assert Path('build-lazarus-unittests/CMakeCache.txt').read_text() == "
        "'CMAKE_CACHEFILE_DIR:INTERNAL=' + str(Path.cwd()); "
        "binary=Path('bin/luanti'); binary.parent.mkdir(); "
        "binary.write_text(Path('product.txt').read_text().strip() + '|' + "
        "Path('src/unittest/test_moveaction.cpp').read_text())"
    )
    reconfigure = ConstructionCommandDefinition(
        id="luanti-unit-test-reconfigure",
        argv=(sys.executable, "-c", reconfigure_program),
        cwd=".",
        timeout_s=30,
    )
    rebuild = ConstructionCommandDefinition(
        id="luanti-unit-test-rebuild",
        argv=(sys.executable, "-c", build_program),
        cwd=".",
        timeout_s=30,
    )
    monkeypatch.setattr(
        "warptest.lazarus_formal_gate.formal_construction_prepare_commands",
        lambda observed_task_id, *, test_cwds: (
            (reconfigure, rebuild)
            if observed_task_id == task_id and test_cwds == (".", ".")
            else ()
        ),
    )

    f2p_program = (
        "from pathlib import Path; "
        "binary=Path('bin/luanti').read_text(); "
        "raise SystemExit(0 if binary == 'fixed|fixed-test\\n' else 1)"
    )
    p2p_program = (
        "from pathlib import Path; "
        "raise SystemExit(0 if Path('bin/luanti').read_text().endswith('fixed-test\\n') else 1)"
    )
    source = {
        "source_path": "upstream-tests/src/unittest/test_moveaction.cpp",
        "destination_path": "src/unittest/test_moveaction.cpp",
        "sha256": _sha(frozen_test),
    }
    (evidence / "upstream.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "record_sha256": "a" * 64,
                "patch_sha256": "b" * 64,
                "tests": [
                    {
                        "id": "f2p",
                        "kind": "fail_to_pass",
                        "origin": "upstream",
                        "cwd": ".",
                        "timeout_s": 30,
                        "argv": [sys.executable, "-c", f2p_program],
                        "frozen_test_sources": [source],
                    },
                    {
                        "id": "p2p",
                        "kind": "pass_to_pass",
                        "origin": "upstream",
                        "cwd": ".",
                        "timeout_s": 30,
                        "argv": [sys.executable, "-c", p2p_program],
                        "frozen_test_sources": [source],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    report = run_formal_construction_test_matrix(
        task_id=task_id,
        evidence_root=evidence,
        fixed_root=roots["fixed"],
        buggy_root=roots["buggy"],
        sandbox_root=tmp_path / "sandbox",
        harness_root=tmp_path,
    )

    assert report["complete"] is True
    assert report["qualified"] is True
    assert report["fixed_tests"] == {"f2p": True, "p2p": True}
    assert report["buggy_tests"] == {"f2p": False, "p2p": True}
    assert [
        [result["id"] for result in execution["prepare_results"]]
        for execution in report["executions"]
    ] == [
        ["luanti-unit-test-reconfigure", "luanti-unit-test-rebuild"],
        ["luanti-unit-test-reconfigure", "luanti-unit-test-rebuild"],
    ]
    assert all(
        execution["overlays"][0]["destination_path"]
        == "src/unittest/test_moveaction.cpp"
        for execution in report["executions"]
    )


def test_registered_openra_task_uses_only_its_closed_gui_gate_adapter(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict[str, object]] = []
    expected = {
        "complete": True,
        "qualified": True,
        "fixed_tests": {"f2p": True, "p2p": True},
        "buggy_tests": {"f2p": False, "p2p": True},
        "failures": [],
        "executions": [],
    }

    def adapter(**kwargs):
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(
        openra_gate,
        "run_openra_pr21453_revalidation_matrix",
        adapter,
    )

    report = run_registered_construction_test_matrix(
        task_id="lz-openra-21453-hotkey-ready-building",
        evidence_root=tmp_path / "evidence",
        fixed_root=tmp_path / "fixed",
        buggy_root=tmp_path / "buggy",
        sandbox_root=tmp_path / "sandbox",
        harness_root=tmp_path,
        total_timeout_s=123.0,
    )

    assert report is expected
    assert calls == [
        {
            "task_id": "lz-openra-21453-hotkey-ready-building",
            "evidence_root": tmp_path / "evidence",
            "fixed_root": tmp_path / "fixed",
            "buggy_root": tmp_path / "buggy",
            "sandbox_root": tmp_path / "sandbox",
            "harness_root": tmp_path,
            "total_timeout_s": 123.0,
        }
    ]
