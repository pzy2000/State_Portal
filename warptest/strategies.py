from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import CheckpointValidity


SPACERPG_REPLAY_UNSUPPORTED_REASON = (
    "SpaceRPG replay_from_start is intentionally excluded because deterministic source-to-deep-state replay "
    "is not implemented for the WebGL benchmark."
)
OPENRA_REPLAY_UNSUPPORTED_REASON = (
    "OpenRA replay_from_start is supported through deterministic clean-launch gameplay validation."
)
JYNEW_REPLAY_UNSUPPORTED_REASON = (
    "jynew replay_from_start is intentionally excluded because deterministic replay "
    "is not implemented for the Unity batch-mode benchmark."
)
PAL3_REPLAY_UNSUPPORTED_REASON = (
    "Pal3 replay_from_start is intentionally excluded because deterministic replay "
    "is not implemented for the Unity batch-mode benchmark."
)
LUANTI_REPLAY_UNSUPPORTED_REASON = (
    "Luanti replay_from_start is intentionally excluded because deterministic source-to-deep-state "
    "replay is not implemented for the voxel-engine benchmark; checkpoints are synthesized through "
    "the test-gated worldmod and persisted via the engine's own world format."
)
JUPYTERLAB_REPLAY_UNSUPPORTED_REASON = (
    "JupyterLab replay_from_start is intentionally excluded for V1 because checkpoints are synthesized "
    "through isolated Jupyter server Contents/session APIs rather than replaying GUI actions from a clean workspace."
)
LIBREOFFICE_REPLAY_UNSUPPORTED_REASON = (
    "LibreOffice replay_from_start is intentionally excluded for V1 because checkpoints are synthesized "
    "through public UNO/ODF document semantics rather than replaying GUI actions from a clean office profile."
)
BLENDER_REPLAY_UNSUPPORTED_REASON = (
    "Blender replay_from_start is intentionally excluded for V1 because checkpoints are synthesized "
    "through visible Blender GUI sessions driven by bpy/operator APIs rather than pixel/UI replay from a factory startup."
)


@dataclass(frozen=True)
class StrategyPlan:
    mode: str
    strategy_id: str
    validity: CheckpointValidity
    validation_required: bool = True
    unsupported_reason: str | None = None

    @property
    def supported(self) -> bool:
        return self.unsupported_reason is None


class StrategySelector:
    """Resolve ablation mode plus spec-declared capabilities into an executable plan."""

    @staticmethod
    def select(
        spec: dict[str, Any],
        *,
        profile_name: str,
        default_strategy: str,
        default_validity: CheckpointValidity,
        mode: str,
    ) -> StrategyPlan:
        checkpoint = spec.get("checkpoint") if isinstance(spec.get("checkpoint"), dict) else {}
        allowed = checkpoint.get("allowed_strategies") if isinstance(checkpoint, dict) else None
        allowed_strategies = [item for item in allowed if isinstance(item, str)] if isinstance(allowed, list) else []
        validity = checkpoint.get("expected_validity") if isinstance(checkpoint, dict) else None
        expected_validity: CheckpointValidity = validity if validity in {"replay-reachable", "save-restorable", "injection-only", "unknown"} else default_validity

        if mode == "inject_or_restore_no_validation":
            return StrategyPlan(
                mode=mode,
                strategy_id="inject_or_restore_no_validation",
                validity=expected_validity,
                validation_required=False,
            )
        if mode == "replay_from_start":
            if profile_name == "SpaceRPGProfile":
                return StrategyPlan(
                    mode=mode,
                    strategy_id="deterministic_replay_from_start",
                    validity="unknown",
                    unsupported_reason=SPACERPG_REPLAY_UNSUPPORTED_REASON,
                )
            if profile_name == "JynewProfile":
                return StrategyPlan(
                    mode=mode,
                    strategy_id="deterministic_replay_from_start",
                    validity="unknown",
                    unsupported_reason=JYNEW_REPLAY_UNSUPPORTED_REASON,
                )
            if profile_name == "Pal3Profile":
                return StrategyPlan(
                    mode=mode,
                    strategy_id="deterministic_replay_from_start",
                    validity="unknown",
                    unsupported_reason=PAL3_REPLAY_UNSUPPORTED_REASON,
                )
            if profile_name == "LuantiProfile":
                return StrategyPlan(
                    mode=mode,
                    strategy_id="deterministic_replay_from_start",
                    validity="unknown",
                    unsupported_reason=LUANTI_REPLAY_UNSUPPORTED_REASON,
                )
            if profile_name == "JupyterLabProfile":
                return StrategyPlan(
                    mode=mode,
                    strategy_id="deterministic_replay_from_start",
                    validity="unknown",
                    unsupported_reason=JUPYTERLAB_REPLAY_UNSUPPORTED_REASON,
                )
            if profile_name == "LibreOfficeProfile":
                return StrategyPlan(
                    mode=mode,
                    strategy_id="deterministic_replay_from_start",
                    validity="unknown",
                    unsupported_reason=LIBREOFFICE_REPLAY_UNSUPPORTED_REASON,
                )
            if profile_name == "BlenderProfile":
                return StrategyPlan(
                    mode=mode,
                    strategy_id="deterministic_replay_from_start",
                    validity="unknown",
                    unsupported_reason=BLENDER_REPLAY_UNSUPPORTED_REASON,
                )
            if profile_name == "OpenRAProfile":
                if (spec.get("target") or {}).get("kind") == "openra_settings":
                    return StrategyPlan(
                        mode=mode,
                        strategy_id="openra_clean_public_settings",
                        validity="replay-reachable",
                    )
                return StrategyPlan(
                    mode=mode,
                    strategy_id="openra_clean_launch_gameplay",
                    validity="replay-reachable",
                )
            if "replay_from_start" not in spec:
                return StrategyPlan(
                    mode=mode,
                    strategy_id="deterministic_replay_from_start",
                    validity="unknown",
                    unsupported_reason="Spec does not declare replay_from_start baseline.",
                )
            return StrategyPlan(mode=mode, strategy_id="deterministic_replay_from_start", validity="replay-reachable")

        strategy = default_strategy
        if allowed_strategies and default_strategy not in allowed_strategies:
            strategy = allowed_strategies[0]
        if allowed_strategies and strategy not in allowed_strategies:
            return StrategyPlan(
                mode=mode,
                strategy_id=strategy,
                validity=expected_validity,
                unsupported_reason=f"Strategy {strategy} is not allowed by checkpoint.allowed_strategies.",
            )
        return StrategyPlan(mode=mode, strategy_id=strategy, validity=expected_validity)


def is_unsupported_reason(reason: str) -> bool:
    normalized = reason.lower()
    return (
        "unsupported" in normalized
        or "not implemented" in normalized
        or "intentionally excluded" in normalized
        or "does not declare replay_from_start" in normalized
    )
