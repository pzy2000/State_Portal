"""Frozen five-arm C/D/E/F/G Lazarus method contract."""

from __future__ import annotations

from copy import deepcopy

C = "lazarus_agent_s"
D = "lazarus_agent_s_synth"
E = "lazarus_state"
F = "lazarus_scalecua"
G = "lazarus_scalecua_synth"
ARMS = (C, D, E, F, G)
SYNTH_ARMS = (D, E, G)
METHOD_PROTOCOL_V4 = "p2p-only-generated-state-cdefg-v4"
METHOD_PROTOCOL = METHOD_PROTOCOL_V4
ROTATION_RULE_V4 = "cdefg_rotating_buggy_then_reverse_fixed_v4"
ROTATION_RULE = ROTATION_RULE_V4
AUTHORIZATION_SCOPE = "c1-lazarus-p2p-only-11tasks-3models-seeds012-cdefg-v4"
AUTHORIZATION_ID = "exploratory_990_paid_run"
PUBLIC_START_REVISION = "public-start-constraints-v2"
# Runtime observations and serialized-cell admission must recognize the same
# Start receipts. Keep this registry shared so new policy families cannot pass
# the outer protocol check but fail the coordinator's inner validation.
REAL_START_CONSTRUCTION_MODES = frozenset(
    {
        "state_checkpoint_from_cold_start",
        "shared_generated_state_from_cold_start",
        "agent_s_phase_a_from_cold_start",
        "agent_s_already_satisfied_cold_start",
        "scalecua_phase_a_from_cold_start",
        "scalecua_already_satisfied_cold_start",
    }
)


def public_start_constraint(task_id: str) -> str:
    """Public initial-data constraint, shared by C and the D/E synthesis input.

    The existing row must not already contain the boundary value introduced by
    the reference action. No golden-specific value or product outcome is given.
    """
    if task_id == "lzp-jupyterlab-18464-debugger-zero-variable-detail":
        return "Initial data constraint: the existing total entry has a nonzero integer readout. Any nonzero integer is allowed; no specific initial number is required."
    return ""


SYNTHESIS = {
    "consumer": "lazarus_p2p_only_shared_v3",
    "model": "decision_model",
    "explore": True,
    "max_explore_steps": 16,
    "max_tokens": 8192,
    "temperature": 0.2,
    "sharing": "task_model_seed_across_de_and_versions",
    "format_repairs": 1,
    "fallback": "none",
}
AGENT_S = {
    "policy_kind": "agent_s",
    "temperature": 0.2,
    "observer_model": "claude-sonnet-5@relay",
    "grounder_model": "decision_model",
    "phase_b_prompt_revision": "lzp-public-reference-steps-v3",
    "execution_backend": "background_pixel_v1",
    "foreground_required": {"jupyterlab": False, "luanti": True, "openra": False},
    "coordinate_space": "frame_pixels_top_left_v1",
}
# ScaleCUA is a single native model: it plans and grounds through the decision
# model itself, so there is no separate grounder route.
SCALECUA = {
    "policy_kind": "scalecua",
    "temperature": 0.2,
    "observer_model": "claude-sonnet-5@relay",
    "phase_b_prompt_revision": "lzp-public-reference-steps-v3",
    "execution_backend": "background_pixel_v1",
    "foreground_required": {"jupyterlab": False, "luanti": True, "openra": False},
    "coordinate_space": "frame_pixels_top_left_v1",
}
METHODS = {
    C: {
        "start_mode": "from_start",
        "transition": "agent_s",
        "decision_steps": 100,
        "budget_sharing": "phase_a_then_phase_b_remaining",
        **deepcopy(AGENT_S),
    },
    D: {
        "start_mode": "shared_generated_state",
        "transition": "agent_s",
        "decision_steps": 100,
        "budget_sharing": "phase_b_only",
        **deepcopy(AGENT_S),
    },
    E: {
        "start_mode": "shared_generated_state",
        "transition": "generated_semantic_actions",
        "semantic_action_limit": 100,
    },
    F: {
        "start_mode": "from_start",
        "transition": "scalecua",
        "decision_steps": 100,
        "budget_sharing": "phase_a_then_phase_b_remaining",
        **deepcopy(SCALECUA),
    },
    G: {
        "start_mode": "shared_generated_state",
        "transition": "scalecua",
        "decision_steps": 100,
        "budget_sharing": "phase_b_only",
        **deepcopy(SCALECUA),
    },
}
START_GATE = {
    "protocol": "p2p-only-before-transition-v3",
    "requires": ["independent_semantic", "independent_visual", "runtime_healthy"],
    "shared_verdict": "de_per_version_with_local_attestation",
    "failure": "skip_phase_b",
}
COST_ACCOUNTING = {
    "phases": ["synthesis", "restore", "execution", "bootstrap", "grading", "p2p"],
    "actual_synthesis": "once_per_group",
    "attributed_synthesis": "once_per_method_per_group",
}


def method_fields_for(arms) -> dict:
    """Return the frozen five-arm method contract."""

    if tuple(arms) != ARMS:
        raise ValueError("P2P-only method contract requires the frozen C/D/E/F/G arms")
    return deepcopy(
        {
            "method_protocol": METHOD_PROTOCOL_V4,
            "methods": METHODS,
            "shared_synthesis": SYNTHESIS,
            "start_gate": START_GATE,
            "cost_accounting": COST_ACCOUNTING,
        }
    )


def rotation_rule_for(arms) -> str:
    if tuple(arms) != ARMS:
        raise ValueError("P2P-only rotation requires the frozen five-arm schedule")
    return ROTATION_RULE_V4


def cells_per_group_for(arms) -> int:
    return 2 * len(tuple(arms))


def method_fields() -> dict:
    return method_fields_for(ARMS)


def validate_method_fields(manifest: dict, *, arms=None) -> None:
    selected = ARMS if arms is None else tuple(arms)
    if selected != ARMS:
        raise ValueError("P2P-only method contract requires the frozen C/D/E/F/G arms")
    for name, expected in method_fields_for(selected).items():
        if manifest.get(name) != expected:
            raise ValueError(
                f"P2P-only {name} differs from the frozen v3/v4 method contract"
            )
