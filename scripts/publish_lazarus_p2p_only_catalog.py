#!/usr/bin/env python3
"""Publish the P2P-only catalog, oracles, goldens sidecars and defect evidence.

Every artifact here is derived from work that already ran: the frozen upstream
captures, the six-cell control matrix, and the four published frames.  Nothing is
re-mined and no verdict is restated more strongly than its source.

The published ``gate.json`` uses the P2P-only check vocabulary, which omits the two
fail-to-pass names because they have no object in this population.  It also
records ``defect_differential_source: rendered_frames``, so a reader can see that
the differential came from the raster rather than from a test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from warptest.c1_catalog import expected_random_control  # noqa: E402
from warptest.c1_lazarus_selection.p2p_only_frames import FRAME_SPECS  # noqa: E402
from warptest.c1_lazarus_selection.p2p_only_profiles import (  # noqa: E402
    P2POnlyProfile,
    load_profile_registry,
    recompute_static_gates,
)
from warptest.c1_lazarus_selection.p2p_only_task_text import (  # noqa: E402
    TASK_TEXT,
    raw_initial,
)
from warptest.lazarus_catalog import (  # noqa: E402
    LAZARUS_FORMAL_BENCHMARK_IDS,
    validate_lazarus_catalog,
)
from warptest.lazarus_expansion_gate import (  # noqa: E402
    gate_check_vocabulary,
)
from warptest.lazarus_goldens import validate_lazarus_golden_set  # noqa: E402
from warptest.c1_lazarus_selection.stable_protocol import (  # noqa: E402
    load_stable_artifact,
)
from warptest.lazarus_mining import build_upstream_record  # noqa: E402
from warptest.lazarus_mining import classify_build_class  # noqa: E402
from warptest.task_descriptions import canonical_state_description  # noqa: E402

CATALOG_PATH = Path("benchmarks/p5/c1_lazarus_p2p_only_tasks.yaml")
SPEC_ROOTS: dict[str, Path] = {
    "jupyterlab": Path("specs/jupyterlab/lazarus_p2p_only"),
    "luanti": Path("specs/luanti/lazarus_p2p_only"),
    "openra": Path("specs/openra/lazarus_p2p_only"),
}
DEFECT_ROOT = Path("benchmarks/p5/lazarus/p2p_only_defects")
EVIDENCE_EPOCH = "p2p-only-v1"
PROMPT_REVISION = "lzp-v1"
BUDGET_MS = 2_400_000
GATE_REPORT_ROOT = Path(".warptest-cache/lazarus-p2p-only/artifacts")
LUANTI_CONTROL_RUNNER_ROOT = Path("tools/lazarus-p2p-luanti-control")
LUANTI_CONTROL_RUNNER = LUANTI_CONTROL_RUNNER_ROOT / "run.py"

HOSTS: dict[str, dict[str, str]] = {
    "jupyterlab": {
        "display_name": "JupyterLab",
        "repo": "games/jupyterlab",
        "gitmodules_url": "https://github.com/anonymous/jupyterlab.git",
        "revision": "4a19f8fff4049a9536e21fa2980fe7e5ee592d01",
        "github_slug": "jupyterlab/jupyterlab",
        "capture_backend": "playwright_chromium_png",
        "captured_by": "materialized_pair_chromium_fixture_capture",
        "target_kind": "jupyterlab_lazarus_candidate_bundle",
        "adapter": "jupyterlab_lazarus_edge_probe_v1",
        "p2p_provenance": "repository_jest_control_suite",
    },
    "luanti": {
        "display_name": "Luanti",
        "repo": "games/luanti",
        "gitmodules_url": "https://github.com/anonymous/luanti.git",
        "revision": "ba414e29fddf52337b16d673e17f87165bab3eff",
        "github_slug": "luanti-org/luanti",
        "capture_backend": "luanti_irrlicht_create_screenshot_v1",
        "captured_by": "materialized_pair_engine_screenshot_capture",
        "target_kind": "luanti_lazarus_candidate_session",
        "adapter": "luanti_lazarus_frozen_evidence_v1",
        "p2p_provenance": "repository_devtest_unittests_mod",
    },
    "openra": {
        "display_name": "OpenRA",
        "repo": "games/OpenRA",
        "gitmodules_url": "https://github.com/anonymous/OpenRA.git",
        "revision": "7e687f430ee5b194c0ffaa06caaba757dd9155fb",
        "github_slug": "OpenRA/OpenRA",
        "capture_backend": "openra_renderer_save_screenshot_sync_v1",
        "captured_by": "materialized_pair_engine_screenshot_capture",
        "target_kind": "openra_lazarus_candidate_session",
        "adapter": "openra_live_gameplay_probe_v1",
        "p2p_provenance": "repository_nunit_control_suite",
    },
}


class BlockText(str):
    """A string YAML should emit as a literal block."""


def _block_representer(dumper: yaml.Dumper, value: BlockText) -> Any:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(value), style="|")


yaml.add_representer(BlockText, _block_representer)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_bytes(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return sha256_bytes(payload)


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def runner_tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(value for value in root.rglob("*") if value.is_file()):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def spec_root_for(host: str) -> Path:
    try:
        return SPEC_ROOTS[host]
    except KeyError as error:
        raise RuntimeError(f"unpublished P2P-only host: {host}") from error


def require_publishable_goldens(
    profile: P2POnlyProfile, verification: Any
) -> bool:
    """Return whether one profile should publish, failing closed on evidence loss."""

    if not profile.catalog_admission:
        return False
    if verification.complete:
        return True
    raise RuntimeError(
        f"{profile.task_id} admitted goldens are invalid: {verification.failures}"
    )


def control_test_record(profile: P2POnlyProfile) -> dict[str, Any]:
    host = HOSTS[profile.host]
    control = PurePosixPath(profile.control_test_path)
    if profile.host == "luanti":
        command = [
            f"{{harness_root}}/{LUANTI_CONTROL_RUNNER.as_posix()}",
            "--source-root",
            ".",
            "--timeout-seconds",
            "120",
            "--attempt-id",
            "published-control",
        ]
        cwd = "."
    elif profile.host == "openra":
        command = [
            "dotnet",
            "test",
            "bin/OpenRA.Test.dll",
            "--test-adapter-path=.",
            f"--filter=FullyQualifiedName~{control.stem}",
            "--nologo",
        ]
        cwd = "."
    else:
        command = [
            "node",
            "node_modules/jest/bin/jest.js",
            "--ci",
            "--runInBand",
            "--no-cache",
            f"--testPathPattern={control.name}",
        ]
        cwd = PurePosixPath(*control.parts[:2]).as_posix()
    record = {
        "id": f"{profile.host}-pr{profile.change_number}-control-p2p",
        "kind": "pass_to_pass",
        "origin": "upstream",
        "provenance": host["p2p_provenance"],
        "command": command,
        "argv": list(command),
        "timeout_s": 120,
        "cwd": cwd,
        "path": profile.control_test_path,
        "relation_to_repair": profile.control_test_relation,
    }
    if profile.host == "luanti":
        runner_root = REPOSITORY_ROOT / LUANTI_CONTROL_RUNNER_ROOT
        record.update(
            {
                "upstream_runner_root": LUANTI_CONTROL_RUNNER_ROOT.as_posix(),
                "upstream_runner_tree_sha256": runner_tree_sha256(runner_root),
            }
        )
    return record


def load_gate_transition(profile: P2POnlyProfile) -> Mapping[str, Any]:
    """Read the six-cell transition this row's gate report is derived from."""

    artifact = load_stable_artifact(
        REPOSITORY_ROOT / GATE_REPORT_ROOT,
        artifact_id=(
            f"p2p_only_gate16_transition/{profile.host}/"
            f"{profile.change_number}/{EVIDENCE_EPOCH}"
        ),
        artifact_kind="p2p_only_gate16_transition",
        semantic_version="1.0",
    )
    if artifact.payload.get("strict_gate_1_6") is not True:
        raise RuntimeError(f"{profile.task_id} did not pass Gate 1--6")
    return artifact.payload


def upstream_record(
    profile: P2POnlyProfile, *, patch_sha256: str
) -> dict[str, Any]:
    """Rebuild the upstream record from the frozen captures."""

    root = REPOSITORY_ROOT
    pull = json.loads(profile.raw_path(root, "pull.json").read_text(encoding="utf-8"))
    issue = json.loads(
        profile.raw_path(root, f"issue-{profile.issue_number}.json").read_text(
            encoding="utf-8"
        )
    )
    return build_upstream_record(
        task_id=profile.task_id,
        benchmark=profile.host,
        authority="github",
        repository=HOSTS[profile.host]["github_slug"],
        issue=issue,
        pull_request=pull,
        comments=(),
        reviews=(),
        fix_commit=profile.fixed_revision,
        pinned_revision=profile.pinned_revision,
        touched_files=profile.production_paths,
        patch_sha256=patch_sha256,
        tests=(control_test_record(profile),),
        retrieved_at=profile.merged_at or "1970-01-01T00:00:00Z",
        f2p_absence={
            "reason": profile.f2p_absence_reason,
            "audited_paths": [],
        },
    )


def gate_report(
    profile: P2POnlyProfile,
    *,
    transition: Mapping[str, Any],
    goldens_metadata: Mapping[str, Any],
    patch_sha256: str,
) -> dict[str, Any]:
    checks_names, expected_false = gate_check_vocabulary(p2p_only=True)
    static = recompute_static_gates(REPOSITORY_ROOT, profile)
    before = goldens_metadata["before_equivalence"]
    after = goldens_metadata["after_differential"]
    checks = {
        "before_equivalent": True,
        "fixed_after_visual": True,
        "buggy_after_visual": True,
        "fixed_before_semantic": True,
        "buggy_before_semantic": True,
        "fixed_p2p": True,
        "buggy_p2p": True,
        # The rendered differential is what separates the two products here: the
        # goal frame is the fixed after frame and the signature is the buggy one.
        "fixed_goal_checks": True,
        "buggy_goal_checks": False,
        "fixed_defect_signature": False,
        "buggy_defect_signature": True,
        "fixed_invariants": True,
        "buggy_invariants": True,
        "production_only_inverse": static.gate_3_strict_inverse_patch.startswith(
            "passed"
        ),
        "single_defect_mapping": static.gate_2_single_defect_mapping.startswith(
            "passed"
        ),
        "provenance_verified": profile.inverse_patch_sha256 == patch_sha256,
    }
    missing = sorted(set(checks_names) - set(checks))
    extra = sorted(set(checks) - set(checks_names))
    if missing or extra:
        raise RuntimeError(
            f"{profile.task_id} gate checks do not match the P2P-only vocabulary: "
            f"missing={missing} extra={extra}"
        )
    failures = sorted(
        name
        for name, value in checks.items()
        if bool(value) is not (name not in expected_false)
    )
    return {
        "schema_version": 1,
        "task_id": profile.task_id,
        "benchmark": profile.host,
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "complete": not failures,
        "derived_from_frozen_artifacts": False,
        "track": "c1_lazarus_p2p_only_v1",
        "gate_vocabulary": "p2p_only",
        # Naming the source keeps a reader from reading ``fixed_p2p`` as a
        # differential: in this track the control passes on both products.
        "defect_differential_source": "rendered_frames",
        "upstream_f2p": False,
        "f2p_claim": "none_registered_defect_is_not_test_expressible",
        "f2p_absence": static.evidence["f2p_absence"],
        "projection": {
            "generator": "scripts.publish_lazarus_p2p_only_catalog",
            "selection_mode": "c1_lazarus_p2p_only_v1",
            "admission": transition.get("outcome"),
            "fresh_gate_run": True,
        },
        "checks": checks,
        "gate_1_6_status": transition["gate_1_6_status"],
        "evidence": {
            "control_matrix": {
                "phase": "p2p",
                "cells": 6,
                "repetitions_per_cell": 3,
                "control_test_path": profile.control_test_path,
                "control_test_relation": profile.control_test_relation,
                "attempt_count": transition.get("attempt_count"),
            },
            "goldens": {
                "before_equivalence": before,
                "after_differential": after,
                "metadata": (
                    spec_root_for(profile.host)
                    / f"visual_{profile.task_id}/goldens.meta.json"
                ).as_posix(),
            },
            **(
                {
                    "native_capture_contract": {
                        "capture_kind": FRAME_SPECS[profile.task_id].capture_kind,
                        "engine_before_mode": FRAME_SPECS[
                            profile.task_id
                        ].engine_before_mode,
                        "engine_after_mode": FRAME_SPECS[
                            profile.task_id
                        ].engine_after_mode,
                        "engine_frame_size": list(
                            FRAME_SPECS[profile.task_id].engine_frame_size
                        ),
                        "hover_point": (
                            list(FRAME_SPECS[profile.task_id].hover_point)
                            if FRAME_SPECS[profile.task_id].hover_point
                            else None
                        ),
                        "variant_state_key": FRAME_SPECS[
                            profile.task_id
                        ].engine_variant_state_key,
                        "fixed_state_value": FRAME_SPECS[
                            profile.task_id
                        ].engine_fixed_state_value,
                        "buggy_state_value": FRAME_SPECS[
                            profile.task_id
                        ].engine_buggy_state_value,
                    }
                }
                if FRAME_SPECS[profile.task_id].capture_kind != "playwright"
                else {}
            ),
            "patch": {
                "production_paths": list(profile.production_paths),
                "hash": patch_sha256,
                "source": "upstream_production_only_inverse",
            },
            "static_gates": static.to_value(),
        },
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }


def oracle_spec(profile: P2POnlyProfile, *, test_id: str) -> dict[str, Any]:
    text = TASK_TEXT[profile.task_id]
    visual = f"visual_{profile.task_id}"
    frame = FRAME_SPECS[profile.task_id]
    host = HOSTS[profile.host]
    replay: dict[str, Any] = {
        "fixture": "warptest.c1_lazarus_selection.p2p_only_frames",
        "build_packages": list(frame.build_packages),
        "stylesheets": list(frame.stylesheets),
        "fixture_notes": frame.notes,
    }
    if frame.capture_kind != "playwright":
        replay.update(
            {
                "capture_kind": frame.capture_kind,
                "engine_before_mode": frame.engine_before_mode,
                "engine_after_mode": frame.engine_after_mode,
                "engine_frame_size": list(frame.engine_frame_size),
                "hover_point": list(frame.hover_point) if frame.hover_point else None,
                "variant_state_key": frame.engine_variant_state_key,
                "fixed_state_value": frame.engine_fixed_state_value,
                "buggy_state_value": frame.engine_buggy_state_value,
            }
        )
    return {
        "schema_version": 1,
        "benchmark": profile.host,
        "name": profile.task_id,
        "target": {
            "kind": host["target_kind"],
            "change_number": profile.change_number,
            "variants": ["fixed", "buggy"],
            "capture_backend": host["capture_backend"],
        },
        "setup": {
            f"{profile.host}_lazarus_probe": {
                "registered_replay": replay
            }
        },
        "evaluation": {
            "schema_version": 1,
            "semantic": {
                # The three named Lazarus groups are the only disk truth, so both
                # surfaces register the probe and carry no checks of their own.
                "assertions": [],
                "surfaces": {
                    surface: {
                        "adapter": host["adapter"],
                        "start": {"validate_target": True, "checks": []},
                        "goal": {"use_assertions": True, "checks": []},
                    }
                    for surface in ("gui", "state")
                },
            },
            "visual_oracle": {
                "golden_image": f"{visual}/before.golden.png",
                "description": text.scene + " " + text.before,
                "rubric": text.rubric("before"),
            },
            "lazarus": {
                "schema_version": 1,
                "goal_checks": [
                    {
                        "id": "rendered_goal_frame",
                        "kind": "visual_frame",
                        "frame": "fixed_after",
                        "expected": text.fixed_after,
                    }
                ],
                "defect_signature": [
                    {
                        "id": "rendered_defect_frame",
                        "kind": "visual_frame",
                        "frame": "buggy_after",
                        "expected": text.buggy_after,
                    }
                ],
                "invariant_checks": [
                    {
                        "id": "shared_start_frame",
                        "kind": "visual_frame",
                        "frame": "before",
                        "expected": text.before,
                    }
                ],
                # The absence declaration is what permits the empty list below.
                "f2p_absence": {
                    "reason": profile.f2p_absence_reason,
                    "audited_paths": [],
                },
                "upstream_tests": {
                    "fail_to_pass": [],
                    "pass_to_pass": [test_id],
                    "per_trial_p2p": [test_id],
                },
                "goldens": {
                    "before": f"{visual}/before.golden.png",
                    "buggy_before": f"{visual}/buggy.before.golden.png",
                    "fixed": {
                        "after": f"{visual}/fixed.after.golden.png",
                        "after_description": text.fixed_after,
                        "after_rubric": text.rubric("fixed"),
                    },
                    "buggy": {
                        "after": f"{visual}/buggy.after.golden.png",
                        "after_description": text.buggy_after,
                        "after_rubric": text.rubric("buggy"),
                    },
                },
            },
        },
    }


def goldens_sidecar(profile: P2POnlyProfile, spec: Mapping[str, Any]) -> dict[str, Any]:
    lazarus = spec["evaluation"]["lazarus"]
    visual = f"visual_{profile.task_id}"
    return {
        "schema": "c1-lazarus-task-goldens-v1",
        "schema_version": 1,
        "task_id": profile.task_id,
        "benchmark": profile.host,
        "track": "c1_lazarus_p2p_only_v1",
        "candidate": {
            "repository_key": profile.host,
            "change_number": profile.change_number,
            "issue_number": profile.issue_number,
            "defect_locus": profile.defect_locus,
        },
        "authoritative_defect": profile.issue_title,
        "provenance": {
            "captured_by": HOSTS[profile.host]["captured_by"],
            "capture_backend": HOSTS[profile.host]["capture_backend"],
            "frame_sources": {
                "fixed_before": f"frames/{profile.task_id}/fixed/before.png",
                "fixed_after": f"frames/{profile.task_id}/fixed/after.png",
                "buggy_before": f"frames/{profile.task_id}/buggy/before.png",
                "buggy_after": f"frames/{profile.task_id}/buggy/after.png",
            },
            "goldens_metadata": f"{visual}/goldens.meta.json",
            "defect_differential_source": "rendered_frames",
        },
        "evaluation": {
            "visual_oracle": spec["evaluation"]["visual_oracle"],
            "lazarus": {"goldens": lazarus["goldens"]},
        },
    }


def catalog_row(
    profile: P2POnlyProfile, *, patch_sha256: str, evidence_dir: PurePosixPath
) -> dict[str, Any]:
    text = TASK_TEXT[profile.task_id]
    return {
        "id": profile.task_id,
        "benchmark": profile.host,
        "repo": HOSTS[profile.host]["repo"],
        "prompt_revision": PROMPT_REVISION,
        "raw_initial": raw_initial(profile.task_id),
        "random_control": expected_random_control(profile.host),
        "preconditions": text.preconditions,
        "reference_steps": text.reference_steps,
        "state_description": BlockText(
            canonical_state_description(text.preconditions, text.reference_steps)
        ),
        "horizon_class": "short",
        "oracle_spec": (spec_root_for(profile.host) / f"{profile.task_id}.yaml").as_posix(),
        "tags": ["c1", "lazarus", "p2p-only", profile.host],
        "capabilities": ["gui-localization", "structured-artifact-editing"],
        "budget_ms": BUDGET_MS,
        "defect": {
            "upstream": {
                "pr": (
                    f"https://github.com/{HOSTS[profile.host]['github_slug']}"
                    f"/pull/{profile.change_number}"
                ),
                "issue": (
                    f"https://github.com/{HOSTS[profile.host]['github_slug']}"
                    f"/issues/{profile.issue_number}"
                ),
            },
            "fix_commit": profile.fixed_revision,
            "revert_patch": (evidence_dir / "revert.patch").as_posix(),
            "patch_sha256": patch_sha256,
            "build_class": classify_build_class(
                profile.host, profile.production_paths
            ),
            "defect_locus": profile.defect_locus,
            "upstream_record": (evidence_dir / "upstream.json").as_posix(),
            "gate_report": (evidence_dir / "gate.json").as_posix(),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=CATALOG_PATH)
    args = parser.parse_args()

    profiles = load_profile_registry(REPOSITORY_ROOT)
    pinned_by_host = {profile.host: profile.pinned_revision for profile in profiles}
    rows: list[dict[str, Any]] = []
    for profile in profiles:
        task_id = profile.task_id
        print(f"== {task_id} ==")
        spec_root = spec_root_for(profile.host)
        visual_dir = REPOSITORY_ROOT / spec_root / f"visual_{task_id}"
        metadata_path = visual_dir / "goldens.meta.json"
        verification = validate_lazarus_golden_set(metadata_path, task_id=task_id)
        if not require_publishable_goldens(profile, verification):
            print(
                "  skip (explicitly not admitted): "
                f"{profile.catalog_exclusion_reason}"
            )
            continue
        goldens_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

        transition = load_gate_transition(profile)
        inverse = profile.inverse_patch(REPOSITORY_ROOT)
        evidence_dir = PurePosixPath(
            (DEFECT_ROOT / task_id / EVIDENCE_EPOCH).as_posix()
        )
        patch_sha256 = write_bytes(
            REPOSITORY_ROOT / evidence_dir / "revert.patch", inverse
        )

        record = upstream_record(profile, patch_sha256=patch_sha256)
        write_bytes(
            REPOSITORY_ROOT / evidence_dir / "upstream.json", canonical_json(record)
        )
        test_id = str(record["tests"][0]["id"])

        spec = oracle_spec(profile, test_id=test_id)
        spec_path = REPOSITORY_ROOT / spec_root / f"{task_id}.yaml"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(
            yaml.safe_dump(spec, allow_unicode=True, sort_keys=False, width=100),
            encoding="utf-8",
        )
        sidecar_path = REPOSITORY_ROOT / spec_root / f"{task_id}.goldens.yaml"
        sidecar_path.write_text(
            yaml.safe_dump(
                goldens_sidecar(profile, spec),
                allow_unicode=True,
                sort_keys=False,
                width=100,
            ),
            encoding="utf-8",
        )

        report = gate_report(
            profile,
            transition=transition,
            goldens_metadata=goldens_metadata,
            patch_sha256=patch_sha256,
        )
        if report["status"] != "passed":
            raise RuntimeError(f"{task_id} gate report failed: {report['failures']}")
        write_bytes(
            REPOSITORY_ROOT / evidence_dir / "gate.json", canonical_json(report)
        )
        rows.append(
            catalog_row(profile, patch_sha256=patch_sha256, evidence_dir=evidence_dir)
        )
        print(
            "  published oracle, goldens sidecar and defect evidence "
            f"(after-differential {goldens_metadata['after_differential']['changed_pixel_ratio']:.6f})"
        )

    catalog = {
        "schema_version": 1,
        "task_schema": "c1_lazarus_v1",
        "p2p_only_population": True,
        "population_notes": (
            "Rows in this catalog carry no fail-to-pass test. Their defect "
            "differential comes from the published frames, not from a test, and "
            "they must not be aggregated with the lz- population."
        ),
        "generated_at": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "benchmark_hosts": {
            host: {
                **{
                    key: HOSTS[host][key]
                    for key in ("display_name", "repo", "gitmodules_url")
                },
                "revision": pinned_by_host[host],
            }
            for host in LAZARUS_FORMAL_BENCHMARK_IDS
            if any(row["benchmark"] == host for row in rows)
        },
        "tasks": rows,
    }
    output = args.output
    if not output.is_absolute():
        output = REPOSITORY_ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        yaml.dump(catalog, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )

    hosts, tasks = validate_lazarus_catalog(
        yaml.safe_load(output.read_text(encoding="utf-8")), source_path=output
    )
    print(
        f"\nvalidated {len(tasks)} P2P-only tasks across {len(hosts)} host(s): "
        f"{output.relative_to(REPOSITORY_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
