"""Pytest configuration for the WarpTest suite.

Large parts of the suite read from the game repositories under ``games/``,
which are heavyweight git submodules (tens of gigabytes in total) and are not
checked out in CI. To keep the suite green both locally (submodules present)
and in CI (submodules absent), tests that require a game submodule are skipped
automatically when the submodule they need is not checked out.
"""

from __future__ import annotations

import inspect
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GAMES_DIR = ROOT / "games"

# Submodule directory names as declared in .gitmodules.
SUBMODULES = {
    "2048",
    "SpaceRPG",
    "DatingSim",
    "OpenRA",
    "jupyterlab",
    "LibreOffice",
    "blender",
    "jynew",
    "Pal3.Unity",
    "luanti",
}

# Tests that reach a game repository indirectly (through synthesis, inspection,
# grading or effort/paper aggregation helpers) and therefore cannot be detected
# by the static source scan below because their own body does not mention a
# ``games/<name>`` path. These are skipped whenever the full submodule set is
# not checked out (e.g. in CI); locally, with all submodules present, they run.
_TESTS_REQUIRING_SUBMODULES = {
    "test_aggregate_rejects_legacy_invocation_level_formal_fixture",
    "test_atomic_complete_fixture_is_true_then_tamper_is_false",
    "test_blender_deterministic_synthesis",
    "test_c2_harness_binds_current_runtime_tasks_and_clean_game_pins",
    "test_checkpoint_synthesis_outputs_required_artifacts_without_hidden_reads",
    "test_core_synthesis_benchmark_static_report_includes_baseline_fairness",
    "test_core_graders_accept_canonical_hidden_benchmark_ids",
    "test_deterministic_agent_accepts_feedback",
    "test_effort_report_counts_profiles_specs_and_instrumentation",
    "test_exploration_agent_runs_react_loop_with_fake_client",
    "test_explore_execute_grading_mode_forces_visual_policy",
    "test_explore_execute_grading_mode_honours_explicit_semantic_only",
    "test_explore_execute_grading_mode_upgrades_adaptive_policy_to_visual",
    "test_explore_final_call_is_separate_synthesis_cost_kind",
    "test_explore_grader_reports_blocked_runtime",
    "test_explore_grader_requires_target_execution_and_visual",
    "test_explore_grader_semantic_only_when_visual_skipped",
    "test_explore_solvability_checker_passes",
    "test_explore_summary_counts_blocked_separately",
    "test_exploration_echoes_reasoning_but_never_persists_or_parses_it",
    "test_hidden_grader_scores_method_checkpoint_without_exposing_oracle_to_agent",
    "test_jupyterlab_deterministic_synthesis",
    "test_jynew_deterministic_synthesis",
    # Builds the path from separate ``games`` and ``jynew`` components, so the
    # literal ``games/<name>`` source scan below cannot recognize it.
    "test_jynew_startup_routes_to_public_mod_panel_during_c1",
    "test_libreoffice_deterministic_synthesis",
    "test_llm_agent_reports_invalid_output_and_private_leaks",
    "test_llm_prompt_includes_state_loop_feedback_without_private_refs",
    "test_llm_prompt_uses_public_inputs_without_private_grader_refs",
    "test_luanti_deterministic_synthesis",
    "test_openra_production_llm_prompt_exposes_gameplay_contract",
    # Builds the OpenRA path from separate components, so the literal
    # ``games/<name>`` source scan below cannot recognize it.
    "test_openra_read_only_grades_run_during_render_ticks",
    "test_pal3_deterministic_synthesis",
    "test_paper_tables_cli_writes_json_and_markdown",
    "test_paper_tables_include_required_tables_and_openra_coverage",
    "test_strict_grader_accepts_model_spec_that_covers_hidden",
    "test_strict_grader_fails_when_model_returns_no_spec_mapping",
    # Drives a real browser run against a randomly chosen bundled game spec.
    "test_visual_experiment_navigation_and_redo_overwrites_rating",
    # The P2P-only profile loader validates every registered host checkout.
    "test_every_profile_declares_a_timeline_fix_binding",
    "test_openra_catalog_build_class_is_mechanical",
    "test_published_luanti_control_is_a_hash_bound_executable_definition",
    "test_runtime_cache_manifest_is_bound_to_the_profile_pin",
    "test_catalog_publisher_only_skips_explicit_exclusions",
    # These synthesis/exploration tests resolve their repository through helper
    # objects, so their own source does not contain a literal games/<name> path.
    "test_lazarus_exploration_can_grep_lua_source_evidence",
    "test_lazarus_exploration_rejects_a_zero_tool_final_before_source_read",
    "test_lazarus_exploration_rejects_source_evidence_for_an_unread_file",
    "test_non_c1_exploration_keeps_accepting_a_zero_tool_final",
    # These integration checks reach real git history or files through helpers.
    "test_registered_materialization_bases_accept_their_own_patch",
    "test_openra_hotkey_overlay_is_exact_and_refuses_a_second_install",
    "test_openra_phase0_overlay_is_exact_and_refuses_a_second_install",
    "test_dry_run_uses_only_registered_luanti_evidence_and_prebuild_recipes",
    # These source-transition tests inspect the 2048 checkout through the
    # SourceExplorer helper, so their own source contains only checkout-local
    # paths such as ``js/game_manager.js``.
    "test_source_loop_generates_actions_assertions_evidence_and_caches",
    "test_source_loop_rejects_zero_tool_final_before_accepting_transition",
    "test_source_loop_runs_exactly_one_public_format_repair",
    "test_source_cache_is_sensitive_to_pair_start_fingerprint",
    # These P2P-only integration tests load the complete profile registry or
    # execute a registered upstream control path from a game checkout.
    "test_readiness_uses_scoped_identity_without_presentation_fields",
    "test_run_index_keeps_model_in_group_identity",
    "test_group_runner_is_model_scoped_and_resume_imports_only_atomic_groups",
    "test_p2p_only_executor_accepts_control_path_without_f2p_source_capsule",
}

# These tests intentionally validate locally retained Lazarus selection evidence.
# The reports and raw API captures are excluded from Git and therefore are not
# present in a clean GitHub Actions checkout.
_TESTS_REQUIRING_LOCAL_LAZARUS_ARTIFACTS = {
    "test_curated_catalog_selects_24_quality_floor_candidates",
    "test_curated_selection_consumes_strict_audits_without_inheriting_them",
    "test_curated_construction_revalidation_executes_and_persists_fresh_matrix",
    "test_curated_revalidation_projects_registered_version_incompatibility",
    "test_curated_revalidation_projects_prepare_failure_as_infrastructure",
    "test_curated_revalidation_rejects_missing_or_corrupt_subprocess_capture",
    "test_curated_revalidation_preserves_a_registered_openra_gate_receipt",
    "test_v2_transition_refuses_direct_reuse_without_v2_candidate_chain",
    "test_v2_transition_requires_exact_identity_repetitions_and_normal_chain",
    "test_v2_eligibility_requires_canonical_raw_source_to_patch_paths",
    "test_v2_transition_rejects_aggregate_that_detaches_from_attempt_table",
    "test_v2_transition_rejects_non_v2_or_dangling_stage_evidence",
    "test_v2_curated_selection_requires_v2_population_coverage_audits_and_admissions",
    "test_v2_curated_selection_ignores_mapped_or_unresolved_old24_labels",
    "test_catalog_replaces_terminal_14586_with_14757",
    "test_existing_fresh_epoch_has_three_distinct_truth_table_repetitions",
    "test_current_catalog_admits_14757",
    "test_8704_patch_split_classifies_the_embedded_test_as_support_relocation",
    "test_8704_patch_split_fails_closed_when_relocation_body_changes",
}

_LOCAL_LAZARUS_ARTIFACT_PATHS = (
    ROOT / "reports/lazarus/formal-v1-mining-candidates.json",
    ROOT / ".warptest-cache/c1_lazarus_selection_v1",
)

_GAMES_REF = re.compile(r"games/([\w.]+)")


def _is_available(name: str) -> bool:
    directory = GAMES_DIR / name
    try:
        return directory.is_dir() and any(directory.iterdir())
    except OSError:
        return False


def _available_submodules() -> set[str]:
    return {name for name in SUBMODULES if _is_available(name)}


def _referenced_submodules(func) -> set[str]:
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError):
        return set()
    return {name for name in _GAMES_REF.findall(source) if name in SUBMODULES}


def pytest_collection_modifyitems(config, items):
    available = _available_submodules()
    all_available = available == SUBMODULES
    local_lazarus_artifacts_available = all(
        path.exists() for path in _LOCAL_LAZARUS_ARTIFACT_PATHS
    )
    for item in items:
        func = getattr(item, "function", None)
        if func is None:
            continue

        test_name = getattr(item, "originalname", None) or item.name

        missing = sorted(_referenced_submodules(func) - available)
        if missing:
            item.add_marker(
                pytest.mark.skip(
                    reason=f"requires game submodule(s) not checked out: {', '.join(missing)}"
                )
            )
            continue

        if not all_available and test_name in _TESTS_REQUIRING_SUBMODULES:
            item.add_marker(
                pytest.mark.skip(
                    reason="requires game submodule(s) not checked out"
                )
            )
            continue

        if (
            not local_lazarus_artifacts_available
            and test_name in _TESTS_REQUIRING_LOCAL_LAZARUS_ARTIFACTS
        ):
            item.add_marker(
                pytest.mark.skip(
                    reason="requires local Lazarus reports/cache not tracked by Git"
                )
            )


@pytest.fixture(autouse=True)
def _portable_lazarus_unit_test_resources(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep unit fixtures portable without weakening production safeguards."""

    if sys.platform != "darwin":
        # Formal runs still require APFS clonefile(2). Linux CI exercises the
        # surrounding isolation/materialization logic with a byte-copy test
        # double; the Darwin-only clone contract itself is tested separately.
        import warptest.c1_sandbox as c1_sandbox

        class _PortableCloneLibrary:
            def clonefile(
                self, source: bytes, destination: bytes, _flags: int
            ) -> int:
                shutil.copytree(
                    os.fsdecode(source),
                    os.fsdecode(destination),
                    symlinks=True,
                )
                return 0

        monkeypatch.setattr(
            c1_sandbox,
            "_clonefile_library",
            lambda: _PortableCloneLibrary(),
        )

    module = getattr(request.node, "module", None)
    module_name = getattr(module, "__name__", "").rsplit(".", 1)[-1]
    if not module_name.startswith("test_c1_lazarus_selection") or module_name.endswith(
        "resource_guard"
    ):
        return

    # Selection tests write only tiny tmp_path fixtures. Give them a stable
    # capacity reading so hosted-runner disk size does not decide unit results;
    # tests for pause/low-capacity branches override these bindings explicitly.
    from warptest.c1_lazarus_selection import (
        pipeline,
        resource_guard,
        revalidation,
        screening,
    )
    from warptest.c1_lazarus_selection.resource_guard import DiskCapacity, GIB

    def ample_capacity(_path: str | Path) -> DiskCapacity:
        return DiskCapacity(
            free_bytes=60 * GIB,
            may_start_build=True,
            may_start_candidate=True,
            may_write=True,
        )

    for target in (resource_guard, pipeline, screening, revalidation):
        monkeypatch.setattr(target, "disk_capacity", ample_capacity)
