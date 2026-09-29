#!/usr/bin/env python3
"""Publish formal-v1 evidence for one C1-Lazarus expansion row from live runs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_expansion_gate import (  # noqa: E402
    write_expansion_formal_artifacts,
)
from warptest.lazarus_expansion_specs import EXPANSION_ROWS_BY_ID  # noqa: E402


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _luanti_checks(payload: dict) -> list[dict]:
    """Collect every named check a Luanti checkpoint report recorded."""

    found: list[dict] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("name"), str) and "status" in node:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


def _luanti_stage(payload: dict, *, stage: str, field: str, expected: str) -> dict:
    """Read one Luanti stage's audit and invariant evidence.

    The Luanti runtime writes a checkpoint report rather than the JupyterLab
    capture receipt, so both observations have to be read out of its named
    checks: the registered ``luanti_node_is`` assertion for the replayed state,
    the ``target.validate.*`` checks for the start, and the registered mod-error
    invariant for both.
    """

    checks = _luanti_checks(payload)
    invariant = [
        check
        for check in checks
        if str(check["name"]).startswith("assertion[no_luanti_mod_errors]")
    ]
    if len(invariant) != 1:
        raise SystemExit(f"Luanti {stage} report must record one mod-error invariant")

    if stage == "after":
        nodes = [
            check
            for check in checks
            if str(check["name"]).startswith("assertion[luanti_node_is]")
        ]
        if len(nodes) != 1:
            raise SystemExit("Luanti after report must hold one node assertion")
        observed = str(nodes[0].get("detail") or "").rsplit("=", 1)[-1].strip()
        if not observed:
            raise SystemExit("Luanti node assertion has no observed node name")
    else:
        validations = [
            check
            for check in checks
            if str(check["name"]).startswith("target.validate.")
        ]
        if not validations:
            raise SystemExit("Luanti before report recorded no start validation")
        observed = (
            expected
            if all(check.get("status") == "success" for check in validations)
            else "start_not_validated"
        )
    return {
        "audit": {"field": field, "observed": observed, "expected": expected},
        "invariants": {
            "check": "no_luanti_mod_errors",
            "ok": invariant[0].get("status") == "success",
        },
    }


def _captures_from_reports(
    paths: dict[tuple[str, str], Path],
    *,
    destination: Path,
    kind: str,
    audit_field: str,
    start_field: str,
    expectations: dict[str, str],
    workspace: Path,
) -> dict[str, dict]:
    """Store each version/stage receipt beside the row's other evidence."""

    destination.mkdir(parents=True, exist_ok=True)
    captures: dict[str, dict] = {}
    for (version, stage), path in sorted(paths.items()):
        payload = _load(path)
        stored = destination / f"{version}-{stage}.json"
        stored.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        expected = (
            expectations["start"] if stage == "before" else expectations[version]
        )
        field = start_field if stage == "before" else audit_field
        if kind == "luanti":
            parsed = _luanti_stage(
                payload, stage=stage, field=field, expected=expected
            )
            live_source = "luanti_client_screenshot_capture"
        else:
            recorded = payload["audit"]
            invariants = payload.get("invariants")
            if not isinstance(invariants, dict) or "ok" not in invariants:
                raise SystemExit(
                    f"{version} {stage} capture receipt records no invariant evidence; "
                    "re-capture with the current driver"
                )
            parsed = {
                "audit": {
                    "field": recorded["field"],
                    "observed": recorded["observed"],
                    "expected": recorded["expected"],
                },
                "invariants": {
                    "check": invariants.get("check"),
                    "ok": bool(invariants["ok"]),
                    "console_errors": invariants.get("console_errors", []),
                    "page_errors": invariants.get("page_errors", []),
                },
            }
            live_source = payload.get("live_source")
        captures.setdefault(version, {})[stage] = {
            "report": str(stored.relative_to(workspace)),
            "live_source": live_source,
            **parsed,
        }
    return captures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id", choices=sorted(EXPANSION_ROWS_BY_ID))
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument(
        "--prebuilt-root", type=Path, default=ROOT / ".warptest-cache/lazarus/prebuilt"
    )
    parser.add_argument(
        "--capture-report",
        action="append",
        default=[],
        metavar="VERSION-STAGE=PATH",
        help=(
            "per-version, per-stage capture receipt, e.g. "
            "fixed-before=/path/before.json"
        ),
    )
    parser.add_argument(
        "--live-gate-report",
        type=Path,
        help="use a registered live gate report instead of a construction receipt",
    )
    args = parser.parse_args(argv)

    row = EXPANSION_ROWS_BY_ID[args.task_id]
    defects = args.workspace / "benchmarks/p5/lazarus/defects" / row.task_id
    goldens_dir = (
        args.workspace / "specs" / row.benchmark / "lazarus" / row.visual_dir
    )

    required = {
        (version, stage)
        for version in ("fixed", "buggy")
        for stage in ("before", "after")
    }
    capture_paths: dict[tuple[str, str], Path] = {}
    for value in args.capture_report:
        key, _, path = value.partition("=")
        version, _, stage = key.partition("-")
        if (version, stage) not in required or not path:
            parser.error(f"invalid --capture-report {value!r}")
        capture_paths[(version, stage)] = Path(path).expanduser().resolve()
    if capture_paths and set(capture_paths) != required:
        parser.error(
            "--capture-report must name every version and stage, or none: "
            "the start state needs its own observation"
        )
    if not capture_paths and args.live_gate_report is None:
        parser.error("--capture-report is required without a live gate report")

    construction_report = None
    live_gate_report = None
    if args.live_gate_report is not None:
        live_gate_report = _load(args.live_gate_report.expanduser().resolve())
    else:
        construction_report = _load(defects / "construction-tests.json")

    artifacts = write_expansion_formal_artifacts(
        row.task_id,
        repository_root=args.workspace,
        prebuilt_root=args.prebuilt_root,
        goldens_dir=goldens_dir,
        spec_test_ids=(*row.fail_to_pass, *row.pass_to_pass),
        captures=(
            _captures_from_reports(
                capture_paths,
                destination=defects / "capture",
                kind=row.benchmark,
                audit_field=row.audit_field,
                start_field=row.start_field,
                expectations={
                    "fixed": row.goal_value,
                    "buggy": row.defect_value,
                    "start": row.start_value,
                },
                workspace=args.workspace,
            )
            if capture_paths
            else None
        ),
        construction_report=construction_report,
        live_gate_report=live_gate_report,
    )
    print(
        json.dumps(
            {
                "task_id": artifacts.task_id,
                "patch_sha256": artifacts.patch_sha256,
                "revert_patch": str(artifacts.revert_patch.relative_to(args.workspace)),
                "upstream_record": str(
                    artifacts.upstream_record.relative_to(args.workspace)
                ),
                "gate_report": str(artifacts.gate_report.relative_to(args.workspace)),
                "production_paths": list(artifacts.production_paths),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
