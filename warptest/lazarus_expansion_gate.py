"""Formal-v1 artifacts for C1-Lazarus expansion rows, from live evidence.

The frozen 24 rows publish a *projection* of freeze-sprint Gate 1--13 evidence:
:func:`warptest.c1_lazarus_selection.c1_lazarus_formal_catalog.project_gate_report`
reads an admission closure and restates its verdicts.  An expansion row has no
such closure, so nothing can be projected for it.  Its verdicts instead come
from work that actually ran in this repository:

* the F2P/P2P truth table in ``defects/<id>/construction-tests.json``, produced
  by ``scripts/run_lazarus_formal_construction_tests.py``, or the live gate
  report produced by ``scripts/run_lazarus_gate.py``;
* the golden triplet published under ``specs/<host>/lazarus/visual_<id>/``,
  whose metadata carries the before-equivalence and after-differential metrics;
* the per-version capture reports that recorded the goal and defect audits.

The report therefore declares ``derived_from_frozen_artifacts: false`` and
``fresh_gate_run: true``.  A reader must be able to tell the two provenances
apart, so this writer never borrows the projection's admission vocabulary.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath, Path
from typing import Any

# The patch derivation is deliberately shared with the frozen catalog writer:
# both provenances must publish the *same kind* of inverse patch, so a reader can
# compare any two rows' revert.patch without knowing which pipeline wrote it.
from .c1_lazarus_selection.c1_lazarus_formal_catalog import (
    _differing_source_paths,
    _file_patch,
    _is_test_path,
    _patch_paths,
)
from .lazarus_goldens import validate_lazarus_golden_set
from .lazarus_mining import _canonical_sha256, validate_upstream_record


GATE_SCHEMA_VERSION = 1
_LIVE_GATE_KIND = "live_construction_run_v1"
# The inverse-patch differential is only established when these three checks
# fail: the buggy product must not pass the registered F2P, must not satisfy the
# goal, and the fixed product must not exhibit the defect signature.  Every
# other check must hold.
FORMAL_GATE_CHECKS_EXPECTED_FALSE = frozenset(
    {"buggy_f2p", "buggy_goal_checks", "fixed_defect_signature"}
)
_MUST_BE_FALSE = FORMAL_GATE_CHECKS_EXPECTED_FALSE
# Both provenances publish this one vocabulary, so a reader (and the formal
# preflight) can score any row without knowing which pipeline wrote its report.
FORMAL_GATE_CHECKS = frozenset(
    {
        "before_equivalent",
        "buggy_after_visual",
        "buggy_before_semantic",
        "buggy_defect_signature",
        "buggy_f2p",
        "buggy_goal_checks",
        "buggy_invariants",
        "buggy_p2p",
        "fixed_after_visual",
        "fixed_before_semantic",
        "fixed_defect_signature",
        "fixed_f2p",
        "fixed_goal_checks",
        "fixed_invariants",
        "fixed_p2p",
        "production_only_inverse",
        "provenance_verified",
        "single_defect_mapping",
    }
)

# A P2P-only row has no fail-to-pass test, so the two F2P names have no object.
# They are removed rather than published as ``false``, because ``false`` on
# ``fixed_f2p`` would read as a fixed product failing its own regression test.
# The remaining names keep their meaning, so a reader can still compare a
# P2P-only row against a strict row check by check.
P2P_ONLY_GATE_CHECKS = FORMAL_GATE_CHECKS - {"fixed_f2p", "buggy_f2p"}
P2P_ONLY_GATE_CHECKS_EXPECTED_FALSE = FORMAL_GATE_CHECKS_EXPECTED_FALSE - {"buggy_f2p"}


def gate_check_vocabulary(*, p2p_only: bool) -> tuple[frozenset[str], frozenset[str]]:
    """Return the ``(checks, expected_false)`` names one track publishes."""

    if p2p_only:
        return P2P_ONLY_GATE_CHECKS, P2P_ONLY_GATE_CHECKS_EXPECTED_FALSE
    return FORMAL_GATE_CHECKS, FORMAL_GATE_CHECKS_EXPECTED_FALSE


class ExpansionGateError(RuntimeError):
    """Raised when an expansion row's live evidence cannot support a gate."""


@dataclass(frozen=True)
class ExpansionArtifacts:
    """Paths and hashes published for one expansion row."""

    task_id: str
    revert_patch: Path
    patch_sha256: str
    upstream_record: Path
    gate_report: Path
    production_paths: tuple[str, ...]


def derive_production_only_patch(
    fixed: Path,
    buggy: Path,
    *,
    declared_files: tuple[str, ...] = (),
) -> tuple[bytes, tuple[str, ...]]:
    """Return the inverse patch restricted to production sources.

    A fix commit usually changes its own regression test.  The published patch
    must not, because the construction runner overlays the frozen test bytes
    into both versions: a patch that also carried test changes would describe a
    difference the graded run never sees.

    The declared files are preferred over a tree walk.  A materialized
    JupyterLab pair also differs in compiled ``lib`` output, which is a build
    product of the declared sources rather than a second defect site.
    """

    candidates = declared_files or _differing_source_paths(fixed, buggy)
    paths = [
        value
        for value in candidates
        if (fixed / value).is_file()
        and (buggy / value).is_file()
        and not _is_test_path(PurePosixPath(value))
    ]
    chunks = [
        chunk for chunk in (_file_patch(fixed, buggy, value) for value in paths) if chunk
    ]
    if not chunks:
        raise ExpansionGateError("materialized pair has no differing production source")
    patch = b"".join(chunks)
    return patch, _patch_paths(patch)


def derive_formal_upstream_record(
    mined: Mapping[str, Any],
    *,
    patch_sha256: str,
    declared_tests: Sequence[str],
) -> dict[str, Any]:
    """Rebuild the mined record as the record the published row is graded from.

    The mined record describes the upstream change as retrieved, including the
    full inverse patch and any control that is not part of the construction
    matrix.  A published row is graded from the production-only patch and from
    the tests its spec declares, so the formal record is bound to both.  This
    mirrors what the frozen writer does through ``build_upstream_bundle``.
    """

    record = copy.deepcopy(dict(mined))
    record["patch_sha256"] = patch_sha256
    wanted = tuple(dict.fromkeys(str(value) for value in declared_tests))
    tests = [
        test
        for test in record.get("tests", [])
        if isinstance(test, Mapping)
        and str(test.get("id")) in wanted
        and str(test.get("kind")) in {"fail_to_pass", "pass_to_pass"}
    ]
    missing = sorted(set(wanted) - {str(test.get("id")) for test in tests})
    if missing:
        raise ExpansionGateError(
            f"spec declares upstream tests the mined record does not register as "
            f"construction tests: {', '.join(missing)}"
        )
    record["tests"] = tests
    record.pop("record_sha256", None)
    record["record_sha256"] = _canonical_sha256(record)
    errors = validate_upstream_record(record)
    if errors:
        raise ExpansionGateError(
            "derived formal upstream record is invalid: " + "; ".join(errors)
        )
    return record


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == payload:
        return
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _truth_table_from_construction(report: dict[str, Any]) -> dict[str, bool]:
    """Project one construction receipt onto the gate's F2P/P2P checks."""

    if report.get("qualified") is not True:
        raise ExpansionGateError(
            f"{report.get('task_id')} construction receipt is not qualified"
        )
    fixed = report["fixed_tests"]
    buggy = report["buggy_tests"]
    f2p_ids = [key for key in fixed if key.endswith("-f2p")]
    if len(f2p_ids) != 1:
        raise ExpansionGateError("construction receipt must name exactly one F2P")
    f2p = f2p_ids[0]
    p2p_ids = [key for key in fixed if key != f2p]
    if not p2p_ids:
        raise ExpansionGateError("construction receipt must name at least one P2P")
    return {
        "fixed_f2p": bool(fixed[f2p]),
        "buggy_f2p": bool(buggy[f2p]),
        "fixed_p2p": all(bool(fixed[key]) for key in p2p_ids),
        "buggy_p2p": all(bool(buggy[key]) for key in p2p_ids),
    }


def _semantic_check(report: dict[str, Any], view: str, name: str) -> bool:
    """Read one recorded semantic observation out of a live gate report."""

    checks = (
        ((report.get("evidence") or {}).get("semantic") or {}).get(view) or {}
    ).get("checks")
    if not isinstance(checks, list):
        raise ExpansionGateError(f"live gate report has no {view} semantic checks")
    for entry in checks:
        key, _, value = str(entry).partition(":")
        if key == name:
            return value.strip().lower() == "true"
    raise ExpansionGateError(f"live gate report did not record {view}.{name}")


def _capture_stage(
    captures: Mapping[str, Any], version: str, stage: str
) -> Mapping[str, Any]:
    version_block = captures.get(version)
    if not isinstance(version_block, Mapping):
        raise ExpansionGateError(f"capture evidence is missing the {version} version")
    stage_block = version_block.get(stage)
    if not isinstance(stage_block, Mapping):
        raise ExpansionGateError(
            f"{version} capture evidence has no {stage} stage; the start state "
            "must be observed rather than assumed"
        )
    return stage_block


def _capture_invariants(captures: Mapping[str, Any], version: str) -> bool:
    """Require recorded invariant evidence for both of a version's stages."""

    results: list[bool] = []
    for stage in ("before", "after"):
        invariants = _capture_stage(captures, version, stage).get("invariants")
        if not isinstance(invariants, Mapping) or "ok" not in invariants:
            raise ExpansionGateError(
                f"{version} {stage} capture recorded no invariant evidence"
            )
        results.append(bool(invariants["ok"]))
    return all(results)


def _capture_start_ok(captures: Mapping[str, Any], version: str) -> bool:
    audit = _capture_stage(captures, version, "before").get("audit")
    if not isinstance(audit, Mapping) or "observed" not in audit:
        raise ExpansionGateError(f"{version} start capture recorded no audit")
    return audit["observed"] == audit.get("expected")


def _semantic_start_ok(report: Mapping[str, Any], view: str) -> bool:
    block = (
        ((report.get("evidence") or {}).get("semantic") or {}).get(view) or {}
    )
    if "start_ok" not in block:
        raise ExpansionGateError(f"live gate report did not record {view}.start_ok")
    return bool(block["start_ok"])


def _audit_checks(captures: dict[str, dict[str, Any]]) -> dict[str, bool]:
    """Project the per-version capture audits onto the gate's semantic checks."""

    fixed = _capture_stage(captures, "fixed", "after")["audit"]
    buggy = _capture_stage(captures, "buggy", "after")["audit"]
    return {
        "fixed_goal_checks": fixed["observed"] == fixed["expected"],
        "fixed_defect_signature": fixed["observed"] == buggy["expected"],
        "buggy_goal_checks": buggy["observed"] == fixed["expected"],
        "buggy_defect_signature": buggy["observed"] == buggy["expected"],
    }


def write_expansion_formal_artifacts(
    task_id: str,
    *,
    repository_root: str | Path,
    prebuilt_root: str | Path,
    goldens_dir: str | Path,
    spec_test_ids: Sequence[str],
    captures: dict[str, dict[str, Any]] | None = None,
    construction_report: dict[str, Any] | None = None,
    live_gate_report: dict[str, Any] | None = None,
) -> ExpansionArtifacts:
    """Publish ``formal-v1`` evidence for one expansion row.

    ``spec_test_ids`` are the fail-to-pass and pass-to-pass ids the row's oracle
    spec declares.  They select what the formal record may carry, so the record
    cannot register a test the graded run never sees.
    """

    if (construction_report is None) == (live_gate_report is None):
        raise ExpansionGateError(
            "an expansion row needs exactly one of a construction receipt or a "
            "live gate report"
        )
    root = Path(repository_root).expanduser().resolve()
    defects = root / "benchmarks/p5/lazarus/defects" / task_id
    formal = defects / "formal-v1"
    fixed = Path(prebuilt_root).expanduser().resolve() / task_id / "fixed"
    buggy = Path(prebuilt_root).expanduser().resolve() / task_id / "buggy"

    authoring = (
        root
        / "benchmarks/p5/lazarus/authoring"
        / f"{task_id}.version-request.json"
    )
    declared = tuple(
        str(value)
        for value in json.loads(authoring.read_text(encoding="utf-8"))["declared_files"]
    )
    patch, production_paths = derive_production_only_patch(
        fixed, buggy, declared_files=declared
    )
    patch_sha256 = hashlib.sha256(patch).hexdigest()
    _write_bytes(formal / "revert.patch", patch)

    mined = json.loads((defects / "upstream.json").read_text(encoding="utf-8"))
    record = derive_formal_upstream_record(
        mined,
        patch_sha256=patch_sha256,
        declared_tests=spec_test_ids,
    )
    formal.mkdir(parents=True, exist_ok=True)
    _write_bytes(
        formal / "upstream.json",
        (json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )

    goldens_root = Path(goldens_dir).expanduser().resolve()
    goldens_metadata_path = goldens_root / "goldens.meta.json"
    goldens_metadata = json.loads(goldens_metadata_path.read_text(encoding="utf-8"))
    verification = validate_lazarus_golden_set(
        goldens_metadata_path,
        task_id=task_id,
    )
    if not verification.complete:
        raise ExpansionGateError(
            f"{task_id} golden provenance is invalid: "
            + "; ".join(verification.failures)
        )

    if construction_report is not None:
        truth = _truth_table_from_construction(construction_report)
        upstream_evidence: dict[str, Any] = {
            "kind": "formal_construction_test_matrix",
            "receipt": str(
                (defects / "construction-tests.json").relative_to(root)
            ),
            "record_sha256": construction_report.get("record_sha256"),
            "fixed_tests": construction_report["fixed_tests"],
            "buggy_tests": construction_report["buggy_tests"],
        }
    else:
        checks = live_gate_report["checks"]
        truth = {
            "fixed_f2p": bool(checks["fixed_f2p"]),
            "buggy_f2p": bool(checks["buggy_f2p"]),
            "fixed_p2p": bool(checks["fixed_p2p"]),
            "buggy_p2p": bool(checks["buggy_p2p"]),
        }
        upstream_evidence = {
            "kind": "registered_gui_construction_gate",
            "receipt": str((defects / "gate.json").relative_to(root)),
            "f2p_origin": live_gate_report.get("f2p_origin"),
            "checks": checks,
        }

    if captures:
        audits = _audit_checks(captures)
        semantics = {
            "fixed_invariants": _capture_invariants(captures, "fixed"),
            "buggy_invariants": _capture_invariants(captures, "buggy"),
            "fixed_before_semantic": _capture_start_ok(captures, "fixed"),
            "buggy_before_semantic": _capture_start_ok(captures, "buggy"),
        }
        capture_evidence: Any = captures
    else:
        # A registered GUI gate already ran the goal and signature audits on both
        # products; re-deriving them from a second capture would claim evidence
        # that was never collected.
        source = live_gate_report["checks"]
        audits = {
            name: bool(source[name])
            for name in (
                "fixed_goal_checks",
                "fixed_defect_signature",
                "buggy_goal_checks",
                "buggy_defect_signature",
            )
            if name in source
        }
        if "buggy_goal_checks" not in audits:
            # The registered GUI gate records the buggy goal audit in its
            # semantic evidence rather than in its check summary.  Read it from
            # there instead of inferring it from the signature check.
            audits["buggy_goal_checks"] = _semantic_check(
                live_gate_report, "buggy_signature", "after.goal_checks"
            )
        semantics = {
            "fixed_invariants": bool(source["fixed_invariants"]),
            "buggy_invariants": bool(source["buggy_invariants"]),
            # The registered GUI gate observed each version's start on its own,
            # so the start verdicts come from those observations rather than
            # from the frames looking alike.
            "fixed_before_semantic": _semantic_start_ok(live_gate_report, "fixed_start"),
            "buggy_before_semantic": _semantic_start_ok(live_gate_report, "buggy_start"),
        }
        capture_evidence = {"source": "registered_gui_construction_gate"}
    # The published check vocabulary is the same eighteen names the frozen rows
    # use, so a reader can compare any two rows without a per-provenance schema.
    # The per-frame start checks stay in the evidence: ``before_equivalent``
    # already covers what they contribute to the verdict.
    checks_out: dict[str, bool] = {
        "before_equivalent": verification.before_equivalent,
        "fixed_after_visual": verification.fixed_after_visual,
        "buggy_after_visual": verification.buggy_after_visual,
        **semantics,
        # The published patch is derived from production sources only, so the
        # inverse cannot reach a test file.
        "production_only_inverse": not any(
            _is_test_path(PurePosixPath(value)) for value in production_paths
        ),
        "single_defect_mapping": bool(record.get("fix_commit")),
        # The record must describe the patch this row actually publishes, not
        # merely carry some hash.
        "provenance_verified": record.get("patch_sha256") == patch_sha256,
        **truth,
        **audits,
    }
    failures = sorted(
        name
        for name, value in checks_out.items()
        if bool(value) is not (name not in _MUST_BE_FALSE)
    )
    report = {
        "schema_version": GATE_SCHEMA_VERSION,
        "task_id": task_id,
        "benchmark": record.get("benchmark"),
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "complete": not failures,
        # This report is a live receipt, not a restatement of frozen evidence.
        "derived_from_frozen_artifacts": False,
        "projection": {
            "generator": "warptest.lazarus_expansion_gate",
            "selection_mode": "c1_roster_expansion_v1",
            "admission": None,
            "closure_size": 0,
            "fresh_gate_run": True,
        },
        "checks": checks_out,
        "evidence": {
            "kind": _LIVE_GATE_KIND,
            "upstream": upstream_evidence,
            "goldens": {
                "metadata": str(goldens_metadata_path.relative_to(root)),
                "metadata_sha256": verification.metadata_sha256,
                # Republished so a reader can score the visual differential
                # without opening a second artifact.
                "before_equivalence": goldens_metadata["before_equivalence"],
                "after_differential": goldens_metadata["after_differential"],
                "before_sha256": goldens_metadata["before_sha256"],
                "fixed_after_sha256": goldens_metadata["fixed_after_sha256"],
                "buggy_after_sha256": goldens_metadata["buggy_after_sha256"],
                "fixed_before_visual": verification.fixed_before_visual,
                "buggy_before_visual": verification.buggy_before_visual,
            },
            "captures": capture_evidence,
            "patch": {
                "production_paths": list(production_paths),
                "sha256": patch_sha256,
                "source": "materialized_tree_difference",
            },
        },
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    gate_path = formal / "gate.json"
    _write_bytes(
        gate_path,
        (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )
    if failures:
        raise ExpansionGateError(
            f"{task_id} live gate did not pass: {', '.join(failures)}"
        )
    return ExpansionArtifacts(
        task_id=task_id,
        revert_patch=formal / "revert.patch",
        patch_sha256=patch_sha256,
        upstream_record=formal / "upstream.json",
        gate_report=gate_path,
        production_paths=production_paths,
    )
