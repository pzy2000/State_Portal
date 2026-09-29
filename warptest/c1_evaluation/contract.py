from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping


EVALUATION_SCHEMA_VERSION = 1

FORMAL_SURFACES = frozenset({"gui", "state"})
OPTIONAL_SURFACES = frozenset({"gui_from_start_baseline"})
VALID_METRICS = frozenset(
    {"equals", "greater_than", "greater_or_equal", "contains"}
)
VALID_ADAPTERS = frozenset(
    {
        "2048_state_profile_v1",
        "blender_native_state_v1",
        "spacerpg_production_dom_save_v1",
        "spacerpg_gui_from_start_baseline_v1",
        "spacerpg_state_profile_v1",
        "jupyterlab_lazarus_app_probe_v1",
        "jupyterlab_lazarus_edge_probe_v1",
        "jupyterlab_live_contents_v1",
        "jynew_unity_live_v1",
        "libreoffice_uno_persistence_v1",
        "love_production_save_roundtrip_v1",
        "love_state_profile_v1",
        "luanti_lazarus_frozen_evidence_v1",
        "luanti_live_world_v1",
        "openra_live_gameplay_probe_v1",
        "openra_public_settings_yaml",
        "pal3_unity_live_v1",
    }
)
VALID_PROBES = frozenset(
    {
        "common.path",
        "spacerpg.basic_food",
        "spacerpg.basic_food_sell_intent",
        "spacerpg.credits",
        "spacerpg.current_system",
        "spacerpg.gate_mode",
        "spacerpg.last_played_slot",
        "spacerpg.load_intent",
        "spacerpg.normalized_tab",
        "spacerpg.numeric_delta",
        "spacerpg.save_fingerprint_changed",
        "spacerpg.save_intent",
        "spacerpg.screen_trace_end",
        "spacerpg.screen_trace_ordered",
        "spacerpg.screen_trace_start",
        "spacerpg.station_id",
        "spacerpg.throttle_intent_count",
        "spacerpg.throttle_percent",
        "love.new_log_entries",
        "love.target_action_present",
        "love.target_action_count",
    }
)

# A probe declaration is data, not an expression language.  Keep the accepted
# argument shape and adapter binding explicit so a typo cannot silently select
# a different observation surface.  The resolver still fails closed as a
# second line of defence if an unvalidated declaration reaches it.
_PROBE_ARGUMENTS: dict[str, frozenset[str]] = {
    "common.path": frozenset({"phase", "path"}),
    "love.new_log_entries": frozenset(),
    "love.target_action_present": frozenset({"path"}),
    "love.target_action_count": frozenset({"action_id"}),
    "spacerpg.basic_food": frozenset({"phase"}),
    "spacerpg.basic_food_sell_intent": frozenset(),
    "spacerpg.credits": frozenset({"phase"}),
    "spacerpg.current_system": frozenset({"phase"}),
    "spacerpg.gate_mode": frozenset({"phase"}),
    "spacerpg.last_played_slot": frozenset({"phase"}),
    "spacerpg.load_intent": frozenset({"slot"}),
    "spacerpg.normalized_tab": frozenset({"phase"}),
    "spacerpg.numeric_delta": frozenset({"path"}),
    "spacerpg.save_fingerprint_changed": frozenset({"slot"}),
    "spacerpg.save_intent": frozenset({"slot"}),
    "spacerpg.screen_trace_end": frozenset(),
    "spacerpg.screen_trace_ordered": frozenset({"values"}),
    "spacerpg.screen_trace_start": frozenset(),
    "spacerpg.station_id": frozenset({"phase"}),
    "spacerpg.throttle_intent_count": frozenset(),
    "spacerpg.throttle_percent": frozenset({"phase"}),
}
_ADAPTER_PROBES: dict[str, frozenset[str]] = {
    "love_production_save_roundtrip_v1": frozenset(
        {name for name in VALID_PROBES if name.startswith("love.")} | {"common.path"}
    ),
    "spacerpg_production_dom_save_v1": frozenset(
        {name for name in VALID_PROBES if name.startswith("spacerpg.")} | {"common.path"}
    ),
    "spacerpg_gui_from_start_baseline_v1": frozenset({"common.path"}),
}
_BENCHMARK_SURFACE_ADAPTERS: dict[str, dict[str, frozenset[str]]] = {
    "2048": {
        "gui": frozenset({"2048_state_profile_v1"}),
        "state": frozenset({"2048_state_profile_v1"}),
    },
    "datingsim": {
        "gui": frozenset({"love_production_save_roundtrip_v1"}),
        "state": frozenset({"love_state_profile_v1"}),
    },
    "spacerpg": {
        "gui": frozenset({"spacerpg_production_dom_save_v1"}),
        "state": frozenset({"spacerpg_state_profile_v1"}),
        "gui_from_start_baseline": frozenset(
            {"spacerpg_gui_from_start_baseline_v1"}
        ),
    },
    "jupyterlab": {
        # The lazarus probes observe a task-scoped browser replay, so they are
        # further surfaces beside the live contents adapter.  The edge probe
        # drives a candidate-local replay bundle; the app probe drives the whole
        # materialized JupyterLab application.
        "gui": frozenset(
            {
                "jupyterlab_live_contents_v1",
                "jupyterlab_lazarus_edge_probe_v1",
                "jupyterlab_lazarus_app_probe_v1",
            }
        ),
        "state": frozenset(
            {
                "jupyterlab_live_contents_v1",
                "jupyterlab_lazarus_edge_probe_v1",
                "jupyterlab_lazarus_app_probe_v1",
            }
        ),
    },
    "libreoffice": {
        "gui": frozenset({"libreoffice_uno_persistence_v1"}),
        "state": frozenset({"libreoffice_uno_persistence_v1"}),
    },
    "jynew": {
        "gui": frozenset({"jynew_unity_live_v1"}),
        "state": frozenset({"jynew_unity_live_v1"}),
    },
    "pal3": {
        "gui": frozenset({"pal3_unity_live_v1"}),
        "state": frozenset({"pal3_unity_live_v1"}),
    },
    "openra": {
        "gui": frozenset(
            {"openra_live_gameplay_probe_v1", "openra_public_settings_yaml"}
        ),
        "state": frozenset(
            {"openra_live_gameplay_probe_v1", "openra_public_settings_yaml"}
        ),
    },
    "luanti": {
        # A frozen candidate whose closure captured a windowed client cannot be
        # replayed offline, so its spec observes the admitted Gate 10 evidence
        # through a surface that says so rather than claiming a live world.
        "gui": frozenset(
            {"luanti_live_world_v1", "luanti_lazarus_frozen_evidence_v1"}
        ),
        "state": frozenset(
            {"luanti_live_world_v1", "luanti_lazarus_frozen_evidence_v1"}
        ),
    },
    "blender": {
        "gui": frozenset({"blender_native_state_v1"}),
        "state": frozenset({"blender_native_state_v1"}),
    },
}

JUPYTER_MEANINGFUL_ASSERTIONS = frozenset(
    {
        "jupyterlab_file_text_equals",
        "jupyterlab_notebook_output_contains",
    }
)
LIBREOFFICE_NATIVE_CHECKS = frozenset(
    {
        "transition.session_same",
        "transition.saved_url",
        "transition.persisted_file",
        "transition.not_modified",
        "transition.document_reopened",
    }
)
_UNITY_ADAPTERS = frozenset({"jynew_unity_live_v1", "pal3_unity_live_v1"})

_PHASE_KEYS = frozenset(
    {
        "validate_target",
        "use_assertions",
        "checks",
        "expected_checks",
        "requirements",
        "native_checks",
    }
)


def evaluation_contract(spec: Mapping[str, Any]) -> dict[str, Any] | None:
    value = spec.get("evaluation")
    return value if isinstance(value, dict) else None


def semantic_assertions(spec: Mapping[str, Any]) -> Any:
    contract = evaluation_contract(spec)
    if contract is not None:
        semantic = contract.get("semantic")
        return semantic.get("assertions") if isinstance(semantic, dict) else None
    return spec.get("assertions")


def visual_oracle(spec: Mapping[str, Any]) -> Any:
    contract = evaluation_contract(spec)
    if contract is not None:
        return contract.get("visual_oracle")
    return spec.get("visual_oracle")


def surface_contract(
    spec: Mapping[str, Any], surface: str
) -> dict[str, Any] | None:
    contract = evaluation_contract(spec)
    semantic = contract.get("semantic") if contract is not None else None
    surfaces = semantic.get("surfaces") if isinstance(semantic, dict) else None
    value = surfaces.get(surface) if isinstance(surfaces, dict) else None
    return value if isinstance(value, dict) else None


def materialize_legacy_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Return an isolated legacy view without creating a second disk truth.

    Generated and non-C1 legacy specs already expose root ``assertions`` and
    ``visual_oracle`` and pass through unchanged (apart from the defensive deep
    copy).  Formal evaluation-v1 specs are projected only in trusted process
    memory so existing profile/runtime APIs can migrate incrementally.
    """

    materialized = copy.deepcopy(dict(spec))
    contract = evaluation_contract(spec)
    if contract is None:
        return materialized
    materialized["assertions"] = copy.deepcopy(semantic_assertions(spec))
    materialized["visual_oracle"] = copy.deepcopy(visual_oracle(spec))
    return materialized


def validate_evaluation_contract(
    spec: Mapping[str, Any],
    spec_path: str | Path,
    *,
    allow_lazarus: bool = False,
) -> list[str]:
    """Validate an evaluation-v1 contract without executing configuration."""

    if "evaluation" not in spec:
        return []
    errors: list[str] = []
    value = spec.get("evaluation")
    if not isinstance(value, dict):
        return ["evaluation must be a mapping"]
    if "assertions" in spec or "visual_oracle" in spec:
        errors.append(
            "evaluation-v1 specs must not mix root assertions/visual_oracle with evaluation"
        )
    allowed_keys = {"schema_version", "semantic", "visual_oracle"}
    if allow_lazarus:
        allowed_keys.add("lazarus")
    unknown = set(value) - allowed_keys
    if unknown:
        errors.append(f"evaluation has unknown keys: {sorted(unknown)}")
    if value.get("schema_version") != EVALUATION_SCHEMA_VERSION:
        errors.append(
            f"evaluation.schema_version must be {EVALUATION_SCHEMA_VERSION}"
        )
    semantic = value.get("semantic")
    if not isinstance(semantic, dict):
        errors.append("evaluation.semantic must be a mapping")
        return errors
    semantic_unknown = set(semantic) - {"assertions", "surfaces"}
    if semantic_unknown:
        errors.append(
            f"evaluation.semantic has unknown keys: {sorted(semantic_unknown)}"
        )
    assertions = semantic.get("assertions")
    if not isinstance(assertions, list):
        errors.append("evaluation.semantic.assertions must be a list")
    surfaces = semantic.get("surfaces")
    if not isinstance(surfaces, dict):
        errors.append("evaluation.semantic.surfaces must be a mapping")
        return errors
    missing = FORMAL_SURFACES - set(surfaces)
    if missing:
        errors.append(
            f"evaluation.semantic.surfaces is missing {sorted(missing)}"
        )
    unknown_surfaces = set(surfaces) - FORMAL_SURFACES - OPTIONAL_SURFACES
    if unknown_surfaces:
        errors.append(
            "evaluation.semantic.surfaces has unknown surfaces: "
            + repr(sorted(unknown_surfaces))
        )
    benchmark = str(spec.get("benchmark") or "").strip().lower()
    adapter_map = _BENCHMARK_SURFACE_ADAPTERS.get(benchmark)
    if adapter_map is None:
        errors.append(
            f"evaluation-v1 has no registered benchmark adapters for {benchmark!r}"
        )
    for surface_name, surface in surfaces.items():
        prefix = f"evaluation.semantic.surfaces.{surface_name}"
        _validate_surface(surface, prefix, errors)
        adapter = surface.get("adapter") if isinstance(surface, dict) else None
        if surface_name == "gui_from_start_baseline":
            if adapter != "spacerpg_gui_from_start_baseline_v1":
                errors.append(
                    f"{prefix}.adapter must be spacerpg_gui_from_start_baseline_v1"
                )
        elif adapter == "spacerpg_gui_from_start_baseline_v1":
            errors.append(
                f"{prefix}.adapter is only valid for gui_from_start_baseline"
            )
        allowed_adapters = adapter_map.get(surface_name) if adapter_map else None
        if allowed_adapters is None or adapter not in allowed_adapters:
            errors.append(
                f"{prefix}.adapter {adapter!r} is not registered for benchmark "
                f"{benchmark!r}"
            )
    if benchmark == "openra":
        target = spec.get("target")
        target_kind = target.get("kind") if isinstance(target, Mapping) else None
        expected_adapter = (
            "openra_public_settings_yaml"
            if target_kind == "openra_settings"
            else "openra_live_gameplay_probe_v1"
        )
        for surface_name in FORMAL_SURFACES:
            surface = surfaces.get(surface_name)
            adapter = surface.get("adapter") if isinstance(surface, dict) else None
            if adapter != expected_adapter:
                errors.append(
                    f"evaluation.semantic.surfaces.{surface_name}.adapter must be "
                    f"{expected_adapter!r} for OpenRA target kind {target_kind!r}"
                )
    if not isinstance(value.get("visual_oracle"), dict):
        errors.append("evaluation.visual_oracle must be a mapping")
    if allow_lazarus:
        errors.extend(validate_lazarus_evaluation_contract(spec, spec_path))
    return errors


def validate_lazarus_evaluation_contract(
    spec: Mapping[str, Any], spec_path: str | Path
) -> list[str]:
    """Validate the private three-way Lazarus oracle extension.

    The base evaluation-v1 validator remains strict by default.  Callers must
    opt in explicitly so a misspelled key in a frozen C1 oracle cannot be
    reinterpreted as an experiment extension.
    """

    source = Path(spec_path)
    evaluation = evaluation_contract(spec)
    if not isinstance(evaluation, dict):
        return ["evaluation.lazarus requires evaluation-v1"]
    lazarus = evaluation.get("lazarus")
    if not isinstance(lazarus, dict):
        return ["evaluation.lazarus must be a mapping"]

    errors: list[str] = []
    expected_keys = {
        "schema_version",
        "goal_checks",
        "defect_signature",
        "invariant_checks",
        "upstream_tests",
        "goldens",
    }
    # A P2P-only row states why the defect has no fail-to-pass test.  That
    # declaration is the only thing that lets ``fail_to_pass`` be empty, so a row
    # graded from an F2P differential cannot lose it by deleting one list.
    optional_keys = {"f2p_absence"}
    unknown = set(lazarus) - expected_keys - optional_keys
    missing = expected_keys - set(lazarus)
    if unknown:
        errors.append(f"evaluation.lazarus has unknown keys: {sorted(unknown)}")
    if missing:
        errors.append(f"evaluation.lazarus is missing keys: {sorted(missing)}")
    if lazarus.get("schema_version") != 1:
        errors.append("evaluation.lazarus.schema_version must be 1")

    f2p_absence_declared = "f2p_absence" in lazarus
    if f2p_absence_declared:
        prefix = "evaluation.lazarus.f2p_absence"
        absence = lazarus.get("f2p_absence")
        if not isinstance(absence, dict):
            errors.append(f"{prefix} must be a mapping")
            f2p_absence_declared = False
        else:
            if set(absence) != {"reason", "audited_paths"}:
                errors.append(
                    f"{prefix} must contain exactly reason, audited_paths"
                )
            reason = absence.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"{prefix}.reason must be a non-empty string")
            audited = absence.get("audited_paths")
            if not isinstance(audited, list) or not all(
                isinstance(item, str) and item.strip() for item in audited
            ):
                errors.append(f"{prefix}.audited_paths must be a string list")

    semantic = evaluation.get("semantic")
    if isinstance(semantic, dict) and semantic.get("assertions") != []:
        errors.append(
            "evaluation.semantic.assertions must be empty for Lazarus; "
            "the three named groups are the only disk truth"
        )
    for key in ("goal_checks", "defect_signature", "invariant_checks"):
        checks = lazarus.get(key)
        if not isinstance(checks, list) or not checks:
            errors.append(f"evaluation.lazarus.{key} must be a non-empty list")
            continue
        if not all(isinstance(check, dict) and check for check in checks):
            errors.append(
                f"evaluation.lazarus.{key} must contain non-empty mappings"
            )

    upstream = lazarus.get("upstream_tests")
    test_keys = {"fail_to_pass", "pass_to_pass", "per_trial_p2p"}
    if not isinstance(upstream, dict):
        errors.append("evaluation.lazarus.upstream_tests must be a mapping")
    else:
        if set(upstream) != test_keys:
            errors.append(
                "evaluation.lazarus.upstream_tests must contain exactly "
                "fail_to_pass, pass_to_pass, per_trial_p2p"
            )
        normalized: dict[str, list[str]] = {}
        for key in sorted(test_keys):
            values = upstream.get(key)
            # The two control lists stay mandatory even for a P2P-only row: they
            # are what that row is graded from.
            allow_empty = key == "fail_to_pass" and f2p_absence_declared
            if (
                not isinstance(values, list)
                or (not values and not allow_empty)
                or not all(isinstance(value, str) and value.strip() for value in values)
            ):
                errors.append(
                    f"evaluation.lazarus.upstream_tests.{key} must be a "
                    + ("string list" if allow_empty else "non-empty string list")
                )
                continue
            normalized[key] = [value.strip() for value in values]
            if len(normalized[key]) != len(set(normalized[key])):
                errors.append(
                    f"evaluation.lazarus.upstream_tests.{key} contains duplicates"
                )
        if f2p_absence_declared and normalized.get("fail_to_pass"):
            errors.append(
                "evaluation.lazarus.upstream_tests.fail_to_pass must be empty when "
                "evaluation.lazarus.f2p_absence is declared"
            )
        f2p = set(normalized.get("fail_to_pass", []))
        p2p = set(normalized.get("pass_to_pass", []))
        per_trial = set(normalized.get("per_trial_p2p", []))
        if f2p & p2p:
            errors.append("Lazarus fail_to_pass and pass_to_pass ids must be disjoint")
        if per_trial and not per_trial <= p2p:
            errors.append("Lazarus per_trial_p2p ids must be a pass_to_pass subset")

    goldens = lazarus.get("goldens")
    if not isinstance(goldens, dict):
        errors.append("evaluation.lazarus.goldens must be a mapping")
    else:
        # ``before`` stays the start frame both arms are judged against.
        # ``buggy_before`` publishes the buggy arm's own start for review; it is
        # optional here because Phase-0 sets captured before it became public
        # still hold three frames, and mandatory for every catalog task.
        required = {"before", "fixed", "buggy"}
        if not required <= set(goldens) or not set(goldens) <= required | {
            "buggy_before"
        }:
            errors.append(
                "evaluation.lazarus.goldens must contain before, fixed, buggy "
                "and may add buggy_before"
            )
        for name in ("before", "buggy_before"):
            if name not in goldens:
                continue
            start = goldens.get(name)
            prefix = f"evaluation.lazarus.goldens.{name}"
            if not isinstance(start, str) or not start.strip():
                errors.append(f"{prefix} must be a path")
            else:
                _validate_relative_artifact_path(start, prefix, source, errors)
        for version in ("fixed", "buggy"):
            branch = goldens.get(version)
            prefix = f"evaluation.lazarus.goldens.{version}"
            if not isinstance(branch, dict):
                errors.append(f"{prefix} must be a mapping")
                continue
            if set(branch) != {"after", "after_description", "after_rubric"}:
                errors.append(
                    f"{prefix} must contain exactly after, after_description, "
                    "after_rubric"
                )
            after = branch.get("after")
            if not isinstance(after, str) or not after.strip():
                errors.append(f"{prefix}.after must be a path")
            else:
                _validate_relative_artifact_path(
                    after, f"{prefix}.after", source, errors
                )
            description = branch.get("after_description")
            if not isinstance(description, str) or not description.strip():
                errors.append(f"{prefix}.after_description must be non-empty")
            rubric = branch.get("after_rubric")
            if not isinstance(rubric, dict) or not rubric:
                errors.append(f"{prefix}.after_rubric must be a non-empty mapping")
    return errors


def _validate_relative_artifact_path(
    value: str, prefix: str, source: Path, errors: list[str]
) -> None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        errors.append(f"{prefix} must stay below the oracle directory: {source}")


def _validate_surface(value: Any, prefix: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"{prefix} must be a mapping")
        return
    unknown = set(value) - {"adapter", "start", "goal"}
    if unknown:
        errors.append(f"{prefix} has unknown keys: {sorted(unknown)}")
    adapter = value.get("adapter")
    if adapter not in VALID_ADAPTERS:
        errors.append(f"{prefix}.adapter must be a registered adapter")
    for phase_name in ("start", "goal"):
        phase = value.get(phase_name)
        phase_prefix = f"{prefix}.{phase_name}"
        if not isinstance(phase, dict):
            errors.append(f"{phase_prefix} must be a mapping")
            continue
        unknown_phase = set(phase) - _PHASE_KEYS
        if unknown_phase:
            errors.append(
                f"{phase_prefix} has unknown keys: {sorted(unknown_phase)}"
            )
        boolean_key = "validate_target" if phase_name == "start" else "use_assertions"
        if type(phase.get(boolean_key)) is not bool:
            errors.append(f"{phase_prefix}.{boolean_key} must be a boolean")
        checks = phase.get("checks")
        if not isinstance(checks, list):
            errors.append(f"{phase_prefix}.checks must be a list")
        else:
            names: set[str] = set()
            for index, check in enumerate(checks):
                _validate_check(
                    check,
                    f"{phase_prefix}.checks[{index}]",
                    errors,
                    adapter=adapter,
                )
                if isinstance(check, dict) and isinstance(check.get("name"), str):
                    if check["name"] in names:
                        errors.append(
                            f"{phase_prefix}.checks contains duplicate name {check['name']!r}"
                        )
                    names.add(check["name"])
        for list_key in ("expected_checks", "requirements", "native_checks"):
            if list_key in phase and not isinstance(phase[list_key], list):
                errors.append(f"{phase_prefix}.{list_key} must be a list")
        _validate_expected_checks(
            phase.get("expected_checks"), adapter, phase_prefix, errors
        )
        _validate_requirements(
            phase.get("requirements"), adapter, phase_prefix, errors
        )
        _validate_native_checks(
            phase.get("native_checks"), adapter, phase_prefix, errors
        )
    if adapter == "spacerpg_gui_from_start_baseline_v1":
        start = value.get("start") if isinstance(value.get("start"), dict) else {}
        goal = value.get("goal") if isinstance(value.get("goal"), dict) else {}
        if start.get("validate_target") is not False or start.get("checks") != []:
            errors.append(
                f"{prefix}.start must disable target validation and declare no checks"
            )
        if goal.get("use_assertions") is not False or not goal.get("checks"):
            errors.append(
                f"{prefix}.goal must disable assertions and declare checks"
            )
        if not isinstance(goal.get("requirements"), list):
            errors.append(f"{prefix}.goal must declare a GUI baseline recipe")
    else:
        start = value.get("start") if isinstance(value.get("start"), dict) else {}
        goal = value.get("goal") if isinstance(value.get("goal"), dict) else {}
        if start.get("validate_target") is not True:
            errors.append(f"{prefix}.start.validate_target must be true")
        if goal.get("use_assertions") is not True:
            errors.append(f"{prefix}.goal.use_assertions must be true")


def _validate_check(
    value: Any,
    prefix: str,
    errors: list[str],
    *,
    adapter: Any,
) -> None:
    if not isinstance(value, dict):
        errors.append(f"{prefix} must be a mapping")
        return
    if set(value) != {"name", "metric", "actual", "expected"}:
        errors.append(
            f"{prefix} must contain exactly name, metric, actual, expected"
        )
        return
    if not isinstance(value.get("name"), str) or not value.get("name"):
        errors.append(f"{prefix}.name must be a non-empty string")
    if value.get("metric") not in VALID_METRICS:
        errors.append(f"{prefix}.metric must be a registered comparison")
    _validate_operand(
        value.get("actual"), f"{prefix}.actual", errors, adapter=adapter
    )
    _validate_operand(
        value.get("expected"), f"{prefix}.expected", errors, adapter=adapter
    )


def _validate_operand(
    value: Any,
    prefix: str,
    errors: list[str],
    *,
    adapter: Any,
) -> None:
    if not isinstance(value, dict):
        errors.append(f"{prefix} must be a mapping")
        return
    has_value = "value" in value
    has_probe = "probe" in value
    if has_value == has_probe:
        errors.append(f"{prefix} must contain exactly one of value or probe")
        return
    if has_value:
        if set(value) != {"value"}:
            errors.append(f"{prefix} literal has unknown keys")
        return
    probe = value.get("probe")
    if probe not in VALID_PROBES:
        errors.append(f"{prefix}.probe must be a registered probe")
        return
    allowed = _ADAPTER_PROBES.get(str(adapter), frozenset())
    if probe not in allowed:
        errors.append(
            f"{prefix}.probe {probe!r} is not registered for adapter {adapter!r}"
        )
    argument_keys = set(value) - {"probe"}
    expected_arguments = _PROBE_ARGUMENTS[probe]
    if argument_keys != expected_arguments:
        errors.append(
            f"{prefix} probe {probe!r} requires exactly arguments "
            f"{sorted(expected_arguments)!r}"
        )
    phase = value.get("phase")
    if phase is not None and phase not in {
        "current",
        "before",
        "after",
        "trace",
        "spec",
        "extras",
    }:
        errors.append(f"{prefix}.phase is invalid")
    if "path" in value and not isinstance(value.get("path"), str):
        errors.append(f"{prefix}.path must be a string")
    if "slot" in value and not isinstance(value.get("slot"), str):
        errors.append(f"{prefix}.slot must be a string")
    if "action_id" in value and value.get("action_id") is not None and not isinstance(
        value.get("action_id"), str
    ):
        errors.append(f"{prefix}.action_id must be a string or null")
    if "values" in value and not isinstance(value.get("values"), list):
        errors.append(f"{prefix}.values must be a list")
    _validate_registered_probe_values(value, prefix, errors)


def _validate_registered_probe_values(
    value: Mapping[str, Any], prefix: str, errors: list[str]
) -> None:
    probe = value.get("probe")
    phase = value.get("phase")
    if probe == "common.path" and phase != "current":
        errors.append(f"{prefix}.phase must be 'current' for common.path")
    elif probe in {
        "spacerpg.basic_food",
        "spacerpg.current_system",
        "spacerpg.gate_mode",
        "spacerpg.last_played_slot",
        "spacerpg.normalized_tab",
        "spacerpg.station_id",
    } and phase != "current":
        errors.append(f"{prefix}.phase must be 'current' for {probe}")
    elif probe in {"spacerpg.credits", "spacerpg.throttle_percent"} and phase not in {
        "before",
        "after",
    }:
        errors.append(f"{prefix}.phase must be 'before' or 'after' for {probe}")

    slot = value.get("slot")
    if probe in {"spacerpg.load_intent"} and slot != "manual-1":
        errors.append(f"{prefix}.slot must be 'manual-1' for {probe}")
    elif probe in {"spacerpg.save_intent"} and slot not in {"auto", "manual-1"}:
        errors.append(f"{prefix}.slot is invalid for {probe}")
    elif probe == "spacerpg.save_fingerprint_changed" and slot != "auto":
        errors.append(f"{prefix}.slot must be 'auto' for {probe}")

    if probe == "spacerpg.numeric_delta" and value.get("path") != "basic_food":
        errors.append(f"{prefix}.path must be 'basic_food' for {probe}")
    if probe == "spacerpg.screen_trace_ordered":
        values = value.get("values")
        if not values or not all(isinstance(item, str) and item for item in values):
            errors.append(f"{prefix}.values must contain non-empty strings")


def _validate_expected_checks(
    value: Any, adapter: Any, prefix: str, errors: list[str]
) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        return
    if adapter == "openra_public_settings_yaml":
        seen: set[tuple[str, str]] = set()
        for index, item in enumerate(value):
            item_prefix = f"{prefix}.expected_checks[{index}]"
            if not isinstance(item, dict) or set(item) != {
                "section",
                "key",
                "expected",
            }:
                errors.append(
                    f"{item_prefix} must contain exactly section, key, expected"
                )
                continue
            section, key = item.get("section"), item.get("key")
            if not isinstance(section, str) or not section:
                errors.append(f"{item_prefix}.section must be a non-empty string")
            if not isinstance(key, str) or not key:
                errors.append(f"{item_prefix}.key must be a non-empty string")
            identity = (str(section), str(key))
            if identity in seen:
                errors.append(f"{prefix}.expected_checks contains duplicate setting")
            seen.add(identity)
        return
    if adapter == "openra_live_gameplay_probe_v1":
        _validate_named_shapes(value, prefix, errors)
        return
    if adapter in _UNITY_ADAPTERS:
        for index, item in enumerate(value):
            item_prefix = f"{prefix}.expected_checks[{index}]"
            if not isinstance(item, dict) or set(item) != {"any_of"}:
                errors.append(f"{item_prefix} must contain exactly any_of")
                continue
            alternatives = item.get("any_of")
            if not isinstance(alternatives, list) or not alternatives or not all(
                isinstance(name, str) and name for name in alternatives
            ):
                errors.append(f"{item_prefix}.any_of must be non-empty strings")
                continue
            if len(alternatives) != len(set(alternatives)):
                errors.append(f"{item_prefix}.any_of contains duplicates")
        return
    if value:
        errors.append(f"{prefix}.expected_checks is not supported by adapter {adapter!r}")


def _validate_named_shapes(
    value: list[Any], prefix: str, errors: list[str]
) -> None:
    names: set[str] = set()
    for index, item in enumerate(value):
        item_prefix = f"{prefix}.expected_checks[{index}]"
        if not isinstance(item, dict) or set(item) != {"name"}:
            errors.append(f"{item_prefix} must contain exactly name")
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            errors.append(f"{item_prefix}.name must be a non-empty string")
        elif name in names:
            errors.append(f"{prefix}.expected_checks contains duplicate name {name!r}")
        else:
            names.add(name)


def _validate_requirements(
    value: Any, adapter: Any, prefix: str, errors: list[str]
) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        return
    if adapter == "jupyterlab_live_contents_v1":
        for index, item in enumerate(value):
            item_prefix = f"{prefix}.requirements[{index}]"
            expected_keys = {
                "name",
                "probe",
                "metric",
                "expected",
                "meaningful_assertions",
            }
            if not isinstance(item, dict) or set(item) != expected_keys:
                errors.append(f"{item_prefix} has an invalid Jupyter requirement shape")
                continue
            if (
                item.get("name") != "transition.changed_from_start"
                or item.get("probe") != "jupyter.changed_from_start"
                or item.get("metric") != "equals"
                or type(item.get("expected")) is not bool
            ):
                errors.append(f"{item_prefix} is not the registered Jupyter requirement")
            meaningful = item.get("meaningful_assertions")
            if (
                not isinstance(meaningful, list)
                or not meaningful
                or not all(isinstance(name, str) for name in meaningful)
                or len(meaningful) != len(set(meaningful))
                or any(name not in JUPYTER_MEANINGFUL_ASSERTIONS for name in meaningful)
            ):
                errors.append(f"{item_prefix}.meaningful_assertions is invalid")
        if len(value) != 1:
            errors.append(f"{prefix}.requirements must contain one Jupyter requirement")
        return
    if adapter == "spacerpg_gui_from_start_baseline_v1":
        recipes = {
            "playable_flight",
            "station_save",
            "save_restore",
            "trading_cargo",
            "galaxy_jump",
        }
        for index, item in enumerate(value):
            item_prefix = f"{prefix}.requirements[{index}]"
            if not isinstance(item, dict) or set(item) != {
                "name",
                "recipe",
                "reason",
            }:
                errors.append(f"{item_prefix} has an invalid GUI baseline requirement shape")
                continue
            if (
                item.get("name") != "spacerpg.gui_from_start_recipe"
                or item.get("recipe") not in recipes
                or not isinstance(item.get("reason"), str)
                or not item.get("reason")
            ):
                errors.append(f"{item_prefix} is not a registered GUI baseline requirement")
        if len(value) != 1:
            errors.append(f"{prefix}.requirements must contain one GUI baseline recipe")
        return
    if adapter in _UNITY_ADAPTERS:
        for index, item in enumerate(value):
            item_prefix = f"{prefix}.requirements[{index}]"
            if not isinstance(item, dict) or set(item) != {
                "name",
                "kind",
                "slot",
                "ordered_events",
            }:
                errors.append(f"{item_prefix} has an invalid Unity requirement shape")
                continue
            slot = item.get("slot")
            allowed_slots = range(0, 3) if adapter.startswith("jynew_") else range(1, 6)
            expected_events = (
                ["save", "load"]
                if adapter.startswith("jynew_")
                else ["save", "reset", "scene_load", "load"]
            )
            if (
                item.get("name") != "c1.public_save_load_witness"
                or item.get("kind") != "save_then_load"
                or type(slot) is not int
                or slot not in allowed_slots
                or item.get("ordered_events") != expected_events
            ):
                errors.append(f"{item_prefix} is not a registered Unity requirement")
        if len(value) > 1:
            errors.append(f"{prefix}.requirements may contain at most one Unity requirement")
        return
    if value:
        errors.append(f"{prefix}.requirements is not supported by adapter {adapter!r}")


def _validate_native_checks(
    value: Any, adapter: Any, prefix: str, errors: list[str]
) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        return
    if adapter != "libreoffice_uno_persistence_v1":
        if value:
            errors.append(f"{prefix}.native_checks is not supported by adapter {adapter!r}")
        return
    if not all(isinstance(name, str) for name in value):
        errors.append(f"{prefix}.native_checks must contain strings")
        return
    if len(value) != len(set(value)):
        errors.append(f"{prefix}.native_checks contains duplicates")
    unknown = [name for name in value if name not in LIBREOFFICE_NATIVE_CHECKS]
    if unknown:
        errors.append(f"{prefix}.native_checks contains unknown checks: {unknown!r}")
