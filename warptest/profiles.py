from __future__ import annotations

import ast
import copy
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .adapters import LocalStorageSaveAdapter, TestHookAdapter, ThreeCanvasAdapter, VanillaDOMAdapter, ZustandHookAdapter
from .models import CheckpointValidity, InspectResult, PhaseResult, RunReport
from .runtime import BrowserRuntimeAdapter, json_dumps
from .strategies import StrategyPlan, StrategySelector


class ProfileError(RuntimeError):
    pass


def _coerce_2048_board(value: Any) -> Any:
    """Accept a 2048 ``board`` as a nested list or a stringified matrix.

    Self-synthesis models frequently emit ``target.board`` as a string holding a
    JSON/Python-literal matrix (e.g. ``"[[512, 512, 0, 0], ...]"``) instead of a
    real nested YAML/JSON list. Parse those back into a list so both restore
    (``_serialized_state``) and grading (``_validate`` iterating ``target['board']``)
    see a proper matrix; anything that does not parse is returned unchanged so the
    downstream 4x4 validation raises the usual readable ``ProfileError``.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(text)
        except (ValueError, SyntaxError, TypeError):
            continue
        if isinstance(parsed, list):
            return parsed
    return value


class BaseProfile:
    name = "GenericWebProfile"
    benchmark = "generic"
    hook_name: str | None = None
    checkpoint_validity: CheckpointValidity = "unknown"
    default_strategy = "unsupported"
    validity_explanations = {
        "replay-reachable": "Target can be reached from a clean start by deterministic replay or ordinary in-game actions.",
        "save-restorable": "Target is represented by the game's own persisted save/localStorage format and is restored through that path.",
        "injection-only": "Target currently depends on a dev/test hook or direct state injection; validation proves playability but not natural reachability.",
        "unknown": "WarpTest cannot yet classify whether this checkpoint is naturally reachable or save-restorable.",
    }

    def __init__(self, inspect_result: InspectResult) -> None:
        self.inspect_result = inspect_result

    def run(self, spec: dict[str, Any], spec_path: Path, runtime: BrowserRuntimeAdapter, mode: str = "jump_and_validate") -> RunReport:
        raise NotImplementedError

    def restore_target_only(
        self,
        spec: dict[str, Any],
        runtime: BrowserRuntimeAdapter,
    ) -> dict[str, Any]:
        """Restore and validate Phase A without entering smoke/oracle code paths."""

        raise ProfileError(f"{self.name} does not implement target-only restore.")

    def strategy_plan(self, spec: dict[str, Any], mode: str) -> StrategyPlan:
        return StrategySelector.select(
            spec,
            profile_name=self.name,
            default_strategy=self.default_strategy,
            default_validity=self.checkpoint_validity,
            mode=mode,
        )

    def base_report(self, spec_path: Path, runtime: BrowserRuntimeAdapter, strategy: str, validity: CheckpointValidity) -> RunReport:
        metadata = self.inspect_result.metadata
        caps = asdict(self.inspect_result.capabilities)
        return RunReport(
            spec_path=str(spec_path.resolve()),
            repo_path=metadata.repo_path,
            benchmark=self.benchmark,
            profile=self.name,
            git_commit=metadata.git_commit,
            launch_command=runtime.launch_command,
            app_url=runtime.app_url,
            adapter_layers_used=self.inspect_result.adapter_layers,
            detected_capabilities=caps,
            runtime_access_methods={
                "zustand_runtime_access_method": caps.get("zustand_runtime_access_method"),
                "hook_name": self.hook_name,
            },
            storage_keys_touched=[],
            hook_availability={"available": self.inspect_result.capabilities.hook_available, "name": self.hook_name},
            instrumentation_mode=self.inspect_result.instrumentation_mode,
            checkpoint_strategy=strategy,
            checkpoint_validity_class=validity,
            target_validated_before_smoke=False,
            checkpoint_synthesis=PhaseResult("pending"),
            checkpoint_restoration=PhaseResult("pending"),
            validation=PhaseResult("pending"),
            smoke_execution=PhaseResult("pending"),
            oracle_result=PhaseResult("pending"),
            visual_oracle_result=PhaseResult("pending"),
            overall_status="pending",
            validity_explanation=self.validity_explanations.get(validity, self.validity_explanations["unknown"]),
        )

    @staticmethod
    def _severe_console(runtime: BrowserRuntimeAdapter) -> list[str]:
        return runtime.console_errors + runtime.page_errors

    def _open_with_hook(self, runtime: BrowserRuntimeAdapter, path: str = "/", timeout_ms: int = 20_000) -> dict[str, Any]:
        if self.hook_name:
            return TestHookAdapter(runtime, self.hook_name).open_with_gate(path, timeout_ms=timeout_ms)
        runtime.open(path)
        return {"available": False, "name": None}

    def _validate_or_skip(self, report: RunReport, runtime: BrowserRuntimeAdapter, target: dict[str, Any], plan: StrategyPlan | str) -> bool:
        validation_required = plan.validation_required if isinstance(plan, StrategyPlan) else plan != "inject_or_restore_no_validation"
        if not validation_required:
            report.validation = PhaseResult("skipped", "Ablation mode skipped target validation.")
            report.target_validated_before_smoke = False
            return False
        validation = self._validate(runtime, target)
        report.validation = validation
        report.target_validated_before_smoke = validation.status == "success"
        self._record_validation_signals(report)
        return report.target_validated_before_smoke

    def _set_checkpoint_validity(self, report: RunReport, validity: CheckpointValidity) -> None:
        report.checkpoint_validity_class = validity
        report.validity_explanation = self.validity_explanations.get(validity, self.validity_explanations["unknown"])

    @staticmethod
    def _record_validation_signals(report: RunReport) -> None:
        data = report.validation.data if isinstance(report.validation.data, dict) else {}
        report.memory_persistence_diff = data.get("memory_persistence_diff", {})
        report.soft_lock_signal = data.get("soft_lock_signal", {})
        report.render_readiness_signal = data.get("render_readiness_signal", {})

    def _guard_console_errors(self, report: RunReport, runtime: BrowserRuntimeAdapter) -> None:
        report.console_errors = self._severe_console(runtime)
        if report.console_errors and report.oracle_result.status == "success":
            report.oracle_result = PhaseResult("failure", "Severe console/page errors were recorded.", {"errors": report.console_errors})

    @staticmethod
    def _refresh_overall_status(report: RunReport) -> None:
        report.overall_status = (
            "success"
            if report.target_validated_before_smoke and report.smoke_execution.status == "success" and report.oracle_result.status == "success"
            else "failure"
        )

    def _capture_screenshot(self, report: RunReport, runtime: BrowserRuntimeAdapter, name: str) -> None:
        try:
            report.screenshots = [runtime.screenshot(name)]
        except Exception:
            report.screenshots = []

    def _complete_report(
        self,
        report: RunReport,
        runtime: BrowserRuntimeAdapter,
        start: float,
        *,
        metrics: dict[str, Any] | None = None,
        screenshot_name: str | None = None,
    ) -> RunReport:
        self._guard_console_errors(report, runtime)
        if screenshot_name:
            self._capture_screenshot(report, runtime, screenshot_name)
        report.post_checkpoint_interaction = {
            "status": report.smoke_execution.status,
            "detail": report.smoke_execution.detail,
            "data": report.smoke_execution.data,
            "validated_before_interaction": report.target_validated_before_smoke,
        }
        report.metrics = {"time_to_target_ms": int((time.perf_counter() - start) * 1000), **(metrics or {})}
        self._refresh_overall_status(report)
        return report

    def _record_network_policy(
        self,
        report: RunReport,
        runtime: BrowserRuntimeAdapter,
        *,
        policy: str,
        ai_status: dict[str, Any] | None = None,
        accidental_match: str = "openai",
        failure_detail: str = "Unexpected external API calls were attempted.",
    ) -> None:
        report.network_api_calls = {**runtime.network_api_calls, "policy": policy}
        if ai_status is not None:
            report.network_api_calls["ai_status"] = ai_status
        accidental = [url for url in runtime.network_api_calls.get("attempted", []) if accidental_match in url.lower()]
        if accidental:
            report.oracle_result = PhaseResult("failure", failure_detail, {"attempted": accidental})

    # --- Snapshot fast-restore hooks (P1) --------------------------------------
    # A snapshot is a captured runtime-state artifact that can re-enter a deep state
    # without replaying the multi-step restore. Browser profiles back this with
    # in-process state (localStorage for save-restorable, the test hook's getState
    # for injection-only). Profiles that cannot reconstruct their deep state from
    # such a snapshot leave the default (unsupported) and fall back to full restore.

    def supports_snapshot(self) -> bool:
        return False

    def capture_runtime_state(self, runtime: BrowserRuntimeAdapter) -> dict[str, Any] | None:
        """Capture the reached deep state as a re-injectable snapshot, or None."""
        return None

    def inject_runtime_state(self, runtime: BrowserRuntimeAdapter, snapshot: dict[str, Any]) -> None:
        """Re-enter the deep state from a snapshot (skip multi-step restore)."""
        raise ProfileError(f"{self.name} does not support snapshot fast-restore.")


class OpenRAProfile(BaseProfile):
    name = "OpenRAProfile"
    benchmark = "OpenRA"
    hook_name = "--warptest-checkpoint"
    default_strategy = "openra_utility_map_validate"
    checkpoint_validity = "replay-reachable"

    def run(self, spec: dict[str, Any], spec_path: Path, runtime: Any, mode: str = "jump_and_validate") -> RunReport:
        plan = self.strategy_plan(spec, mode)
        if plan.unsupported_reason:
            raise ProfileError(plan.unsupported_reason)
        report = self.base_report(spec_path, runtime, plan.strategy_id, plan.validity)
        start = time.perf_counter()
        report.hook_availability = {"available": True, "name": self.hook_name, "enabled_by": "explicit_cli"}
        report.runtime_access_methods = {
            "openra_runtime_access_method": (
                "OpenRA launch-game WarpTest gameplay probe"
                if plan.strategy_id == "openra_gameplay_production_validate"
                else "OpenRA.Utility --warptest-checkpoint"
            ),
            "hook_name": self.hook_name,
        }
        result = runtime.run_checkpoint(spec, spec_path)
        checks = result.get("checks", []) if isinstance(result.get("checks"), list) else []
        report.checkpoint_synthesis = PhaseResult(
            "success",
            "Prepared OpenRA utility checkpoint request from the WarpTest spec.",
            {"request_path": result.get("request_path")},
        )
        report.checkpoint_restoration = self._phase_from_checks(
            result,
            checks,
            prefixes=("target.map_exists", "target.map_loads", "target.map_loaded", "target.requires_mod"),
            success_detail="OpenRA loaded the requested mod and map checkpoint.",
            failure_detail="OpenRA could not load the requested mod/map checkpoint.",
        )
        if plan.validation_required:
            report.validation = self._phase_from_checks(
                result,
                checks,
                prefixes=("target.",),
                success_detail="OpenRA target metadata, players, objectives, and actors matched the checkpoint.",
                failure_detail="OpenRA target validation failed.",
            )
            report.target_validated_before_smoke = report.validation.status == "success"
        else:
            report.validation = PhaseResult("skipped", "Ablation mode skipped target validation.")
            report.target_validated_before_smoke = False

        report.smoke_execution = self._phase_from_checks(
            result,
            checks,
            prefixes=("action[",),
            success_detail="OpenRA smoke action checks passed.",
            failure_detail="OpenRA smoke action checks failed.",
        )
        report.oracle_result = self._phase_from_checks(
            result,
            checks,
            prefixes=("assertion[",),
            success_detail="OpenRA oracle checks passed.",
            failure_detail="OpenRA oracle checks failed.",
        )
        report.logs.extend([path for path in [result.get("stdout_path"), result.get("stderr_path")] if isinstance(path, str)])
        report.post_checkpoint_interaction = {
            "status": report.smoke_execution.status,
            "detail": report.smoke_execution.detail,
            "data": report.smoke_execution.data,
            "validated_before_interaction": report.target_validated_before_smoke,
        }
        report.metrics = {
            "time_to_target_ms": int((time.perf_counter() - start) * 1000),
            "actions_replayed": 0,
            "openra_utility_exit_code": result.get("exit_code"),
        }
        self._refresh_overall_status(report)
        return report

    @staticmethod
    def _phase_from_checks(
        result: dict[str, Any],
        checks: list[Any],
        *,
        prefixes: tuple[str, ...],
        success_detail: str,
        failure_detail: str,
    ) -> PhaseResult:
        selected = [
            check
            for check in checks
            if isinstance(check, dict)
            and any(str(check.get("name", "")).startswith(prefix) for prefix in prefixes)
        ]
        if not selected and result.get("status") == "failure":
            return PhaseResult("failure", failure_detail, result)
        failures = [check for check in selected if check.get("status") != "success"]
        if failures:
            return PhaseResult("failure", failure_detail, {"failures": failures, "report_path": result.get("report_path")})
        return PhaseResult("success", success_detail, {"checks": selected, "report_path": result.get("report_path")})


class JupyterLabProfile(BaseProfile):
    """Profile for JupyterLab, a browser/server GUI backed by Jupyter Server APIs."""

    name = "JupyterLabProfile"
    benchmark = "JupyterLab"
    hook_name = "jupyter server contents/session api"
    default_strategy = "jupyterlab_workspace_api_validate"
    checkpoint_validity = "save-restorable"

    def run(self, spec: dict[str, Any], spec_path: Path, runtime: Any, mode: str = "jump_and_validate") -> RunReport:
        plan = self.strategy_plan(spec, mode)
        if plan.unsupported_reason:
            raise ProfileError(plan.unsupported_reason)
        report = self.base_report(spec_path, runtime, plan.strategy_id, plan.validity)
        start = time.perf_counter()
        report.hook_availability = {"available": True, "name": self.hook_name, "enabled_by": "isolated_server_api"}
        report.runtime_access_methods = {
            "jupyterlab_runtime_access_method": "Jupyter Server Contents, Sessions, kernels, and workspace metadata APIs",
            "hook_name": self.hook_name,
        }

        result = runtime.run_checkpoint(spec, spec_path)
        checks = result.get("checks", []) if isinstance(result.get("checks"), list) else []
        report.checkpoint_synthesis = PhaseResult(
            "success",
            "Prepared isolated JupyterLab API checkpoint request from the WarpTest spec.",
            {"request_path": result.get("request_path")},
        )
        report.checkpoint_restoration = self._phase_from_checks(
            result,
            checks,
            prefixes=("target.state_synthesized",),
            success_detail="JupyterLab synthesized the requested file/notebook/workspace checkpoint.",
            failure_detail="JupyterLab could not synthesize the requested checkpoint.",
        )
        if plan.validation_required:
            report.validation = self._phase_from_checks(
                result,
                checks,
                prefixes=("target.validate",),
                success_detail="JupyterLab target validation checks passed.",
                failure_detail="JupyterLab target validation failed.",
            )
            report.target_validated_before_smoke = report.validation.status == "success"
        else:
            report.validation = PhaseResult("skipped", "Ablation mode skipped target validation.")
            report.target_validated_before_smoke = False

        report.smoke_execution = self._phase_from_checks(
            result,
            checks,
            prefixes=("action[",),
            success_detail="JupyterLab smoke action checks passed.",
            failure_detail="JupyterLab smoke action checks failed.",
        )
        report.oracle_result = self._phase_from_checks(
            result,
            checks,
            prefixes=("assertion[",),
            success_detail="JupyterLab oracle checks passed.",
            failure_detail="JupyterLab oracle checks failed.",
        )
        report.logs.extend(
            [
                path
                for path in [result.get("stdout_path"), result.get("stderr_path"), result.get("report_path"), result.get("request_path")]
                if isinstance(path, str)
            ]
        )
        report.post_checkpoint_interaction = {
            "status": report.smoke_execution.status,
            "detail": report.smoke_execution.detail,
            "data": report.smoke_execution.data,
            "validated_before_interaction": report.target_validated_before_smoke,
        }
        report.metrics = {
            "time_to_target_ms": int((time.perf_counter() - start) * 1000),
            "actions_replayed": 0,
            "jupyterlab_runtime_exit_code": result.get("exit_code"),
        }
        screenshot = getattr(runtime, "screenshot", None)
        if callable(screenshot) and result.get("status") == "success":
            try:
                report.screenshots = [screenshot("jupyterlab-final.png")]
            except Exception as error:
                report.visual_oracle_result.data.setdefault("screenshot_capture_error", str(error))
        self._refresh_overall_status(report)
        return report

    @staticmethod
    def _phase_from_checks(
        result: dict[str, Any],
        checks: list[Any],
        *,
        prefixes: tuple[str, ...],
        success_detail: str,
        failure_detail: str,
    ) -> PhaseResult:
        selected = [
            check
            for check in checks
            if isinstance(check, dict)
            and any(str(check.get("name", "")).startswith(prefix) for prefix in prefixes)
        ]
        if not selected and result.get("status") == "failure":
            return PhaseResult("failure", failure_detail, result)
        failures = [check for check in selected if check.get("status") != "success"]
        if failures:
            return PhaseResult("failure", failure_detail, {"failures": failures, "report_path": result.get("report_path")})
        return PhaseResult("success", success_detail, {"checks": selected, "report_path": result.get("report_path")})


class LibreOfficeProfile(BaseProfile):
    """Profile for LibreOffice Writer/Calc/Impress checkpoints driven by UNO and ODF artifacts."""

    name = "LibreOfficeProfile"
    benchmark = "LibreOffice"
    hook_name = "uno/odf document api"
    default_strategy = "libreoffice_uno_document_validate"
    checkpoint_validity = "save-restorable"

    def run(self, spec: dict[str, Any], spec_path: Path, runtime: Any, mode: str = "jump_and_validate") -> RunReport:
        plan = self.strategy_plan(spec, mode)
        if plan.unsupported_reason:
            raise ProfileError(plan.unsupported_reason)
        report = self.base_report(spec_path, runtime, plan.strategy_id, plan.validity)
        start = time.perf_counter()
        report.hook_availability = {"available": True, "name": self.hook_name, "enabled_by": "public_uno_odf_api"}
        report.runtime_access_methods = {
            "libreoffice_runtime_access_method": "Isolated LibreOffice process controlled through public UNO APIs and ODF/PDF artifacts",
            "hook_name": self.hook_name,
        }

        result = runtime.run_checkpoint(spec, spec_path)
        checks = result.get("checks", []) if isinstance(result.get("checks"), list) else []
        report.checkpoint_synthesis = PhaseResult(
            "success",
            "Prepared isolated LibreOffice UNO checkpoint request from the WarpTest spec.",
            {"request_path": result.get("request_path")},
        )
        report.checkpoint_restoration = self._phase_from_checks(
            result,
            checks,
            prefixes=("target.state_synthesized",),
            success_detail="LibreOffice synthesized the requested Writer/Calc/Impress checkpoint.",
            failure_detail="LibreOffice could not synthesize the requested checkpoint.",
        )
        if plan.validation_required:
            report.validation = self._phase_from_checks(
                result,
                checks,
                prefixes=("target.validate",),
                success_detail="LibreOffice target validation checks passed.",
                failure_detail="LibreOffice target validation failed.",
            )
            report.target_validated_before_smoke = report.validation.status == "success"
        else:
            report.validation = PhaseResult("skipped", "Ablation mode skipped target validation.")
            report.target_validated_before_smoke = False

        report.smoke_execution = self._phase_from_checks(
            result,
            checks,
            prefixes=("action[",),
            success_detail="LibreOffice smoke action checks passed.",
            failure_detail="LibreOffice smoke action checks failed.",
        )
        report.oracle_result = self._phase_from_checks(
            result,
            checks,
            prefixes=("assertion[",),
            success_detail="LibreOffice oracle checks passed.",
            failure_detail="LibreOffice oracle checks failed.",
        )
        report.logs.extend(
            [
                path
                for path in [result.get("stdout_path"), result.get("stderr_path"), result.get("report_path"), result.get("request_path")]
                if isinstance(path, str)
            ]
        )
        screenshots = result.get("screenshots") if isinstance(result.get("screenshots"), dict) else {}
        screenshot_after = result.get("screenshot_after") or screenshots.get("after")
        screenshot_before = result.get("screenshot_before") or screenshots.get("before")
        report.screenshots = [
            path
            for path in [screenshot_after, screenshot_before]
            if isinstance(path, str) and Path(path).exists()
        ]
        report.post_checkpoint_interaction = {
            "status": report.smoke_execution.status,
            "detail": report.smoke_execution.detail,
            "data": report.smoke_execution.data,
            "validated_before_interaction": report.target_validated_before_smoke,
        }
        report.metrics = {
            "time_to_target_ms": int((time.perf_counter() - start) * 1000),
            "actions_replayed": 0,
            "libreoffice_helper_exit_code": result.get("exit_code"),
            "soffice": result.get("soffice"),
            "soffice_version": result.get("soffice_version"),
            "python": result.get("python"),
            "document_path": result.get("document_path"),
            "screenshot_before": screenshot_before,
            "screenshot_after": screenshot_after,
        }
        self._refresh_overall_status(report)
        return report

    @staticmethod
    def _phase_from_checks(
        result: dict[str, Any],
        checks: list[Any],
        *,
        prefixes: tuple[str, ...],
        success_detail: str,
        failure_detail: str,
    ) -> PhaseResult:
        selected = [
            check
            for check in checks
            if isinstance(check, dict)
            and any(str(check.get("name", "")).startswith(prefix) for prefix in prefixes)
        ]
        if not selected and result.get("status") == "failure":
            return PhaseResult("failure", failure_detail, result)
        failures = [check for check in selected if check.get("status") != "success"]
        if failures:
            return PhaseResult("failure", failure_detail, {"failures": failures, "report_path": result.get("report_path")})
        return PhaseResult("success", success_detail, {"checks": selected, "report_path": result.get("report_path")})


class BlenderProfile(BaseProfile):
    """Profile for visible Blender GUI sessions driven by bpy/operator APIs."""

    name = "BlenderProfile"
    benchmark = "Blender"
    hook_name = "bpy/operator api"
    default_strategy = "blender_python_scene_validate"
    checkpoint_validity = "save-restorable"

    def run(self, spec: dict[str, Any], spec_path: Path, runtime: Any, mode: str = "jump_and_validate") -> RunReport:
        plan = self.strategy_plan(spec, mode)
        if plan.unsupported_reason:
            raise ProfileError(plan.unsupported_reason)
        report = self.base_report(spec_path, runtime, plan.strategy_id, plan.validity)
        start = time.perf_counter()
        report.hook_availability = {"available": True, "name": self.hook_name, "enabled_by": "visible_gui_python_driver"}
        report.runtime_access_methods = {
            "blender_runtime_access_method": "Visible Blender GUI process with bpy/operator Python driver",
            "hook_name": self.hook_name,
        }

        result = runtime.run_checkpoint(spec, spec_path)
        checks = result.get("checks", []) if isinstance(result.get("checks"), list) else []
        report.checkpoint_synthesis = PhaseResult(
            "success",
            "Prepared Blender scene checkpoint request from the WarpTest spec.",
            {"request_path": result.get("request_path")},
        )
        report.checkpoint_restoration = self._phase_from_checks(
            result,
            checks,
            prefixes=("target.state_synthesized",),
            success_detail="Blender synthesized the requested scene checkpoint.",
            failure_detail="Blender could not synthesize the requested scene checkpoint.",
        )
        if plan.validation_required:
            report.validation = self._phase_from_checks(
                result,
                checks,
                prefixes=("target.validate",),
                success_detail="Blender target validation checks passed.",
                failure_detail="Blender target validation failed.",
            )
            report.target_validated_before_smoke = report.validation.status == "success"
        else:
            report.validation = PhaseResult("skipped", "Ablation mode skipped target validation.")
            report.target_validated_before_smoke = False

        report.smoke_execution = self._phase_from_checks(
            result,
            checks,
            prefixes=("action[",),
            success_detail="Blender smoke action checks passed.",
            failure_detail="Blender smoke action checks failed.",
        )
        report.oracle_result = self._phase_from_checks(
            result,
            checks,
            prefixes=("assertion[",),
            success_detail="Blender oracle checks passed.",
            failure_detail="Blender oracle checks failed.",
        )
        report.logs.extend(
            [
                path
                for path in [result.get("stdout_path"), result.get("stderr_path"), result.get("report_path"), result.get("request_path")]
                if isinstance(path, str)
            ]
        )
        screenshots = result.get("screenshots") if isinstance(result.get("screenshots"), dict) else {}
        screenshot_after = result.get("screenshot_after") or screenshots.get("after")
        screenshot_before = result.get("screenshot_before") or screenshots.get("before")
        report.screenshots = [
            path
            for path in [screenshot_after, screenshot_before]
            if isinstance(path, str) and Path(path).exists()
        ]
        report.post_checkpoint_interaction = {
            "status": report.smoke_execution.status,
            "detail": report.smoke_execution.detail,
            "data": report.smoke_execution.data,
            "validated_before_interaction": report.target_validated_before_smoke,
        }
        report.metrics = {
            "time_to_target_ms": int((time.perf_counter() - start) * 1000),
            "actions_replayed": 0,
            "blender_utility_exit_code": result.get("exit_code"),
            "blend_artifacts": result.get("blend_artifacts", []),
        }
        self._refresh_overall_status(report)
        return report

    @staticmethod
    def _phase_from_checks(
        result: dict[str, Any],
        checks: list[Any],
        *,
        prefixes: tuple[str, ...],
        success_detail: str,
        failure_detail: str,
    ) -> PhaseResult:
        selected = [
            check
            for check in checks
            if isinstance(check, dict)
            and any(str(check.get("name", "")).startswith(prefix) for prefix in prefixes)
        ]
        if not selected and result.get("status") == "failure":
            return PhaseResult("failure", failure_detail, result)
        failures = [check for check in selected if check.get("status") != "success"]
        if failures:
            return PhaseResult("failure", failure_detail, {"failures": failures, "report_path": result.get("report_path")})
        return PhaseResult("success", success_detail, {"checks": selected, "report_path": result.get("report_path")})


class Game2048Profile(BaseProfile):
    name = "Game2048Profile"
    benchmark = "2048"
    hook_name = "__WARPTEST_2048__"
    default_strategy = "localStorage_restore_reload"
    checkpoint_validity = "save-restorable"

    def restore_target_only(
        self,
        spec: dict[str, Any],
        runtime: BrowserRuntimeAdapter,
    ) -> dict[str, Any]:
        plan = self.strategy_plan(spec, "jump_and_validate")
        if plan.unsupported_reason:
            raise ProfileError(plan.unsupported_reason)
        target = spec.get("target", {})
        if not isinstance(target, dict):
            raise ProfileError("2048 spec target must be a mapping")
        board = _coerce_2048_board(target.get("board"))
        if not board:
            raise ProfileError("2048 spec target.board is required")
        target["board"] = board
        state = self._serialized_state(
            board,
            int(target.get("score", 0)),
            bool(target.get("won", False)),
            bool(target.get("over", False)),
            bool(target.get("keepPlaying", False)),
        )
        storage_values = {"gameState": json_dumps(state)}
        if "bestScore" in target:
            storage_values["bestScore"] = str(target["bestScore"])
        touched = LocalStorageSaveAdapter(runtime).write_many(
            storage_values,
            [],
            self.inspect_result.storage_policy,
        )
        hook = self._open_with_hook(runtime, "/?warptest=1", timeout_ms=10_000)
        runtime.page.reload(wait_until="domcontentloaded")
        VanillaDOMAdapter(runtime).wait_for_tiles(timeout_ms=10_000)
        validation = self._validate(runtime, target)
        console_errors = self._severe_console(runtime)
        ok = validation.status == "success" and not console_errors
        return {
            "status": "success" if ok else "failure",
            "detail": (
                "Restored and validated the 2048 target without smoke/oracle execution."
                if ok
                else validation.detail or "2048 target-only validation failed."
            ),
            "checkpoint_restoration": {
                "status": "success",
                "detail": "Restored through localStorage and original GameManager setup.",
            },
            "validation": asdict(validation),
            "storage_keys_touched": touched,
            "hook_availability": hook,
            "console_errors": console_errors,
            "actions_executed": False,
            "assertions_evaluated": False,
            "smoke_execution": "not_run",
            "oracle_result": "not_run",
        }

    def run(self, spec: dict[str, Any], spec_path: Path, runtime: BrowserRuntimeAdapter, mode: str = "jump_and_validate") -> RunReport:
        plan = self.strategy_plan(spec, mode)
        if plan.unsupported_reason:
            raise ProfileError(plan.unsupported_reason)
        if mode == "replay_from_start":
            return self._run_replay_from_start(spec, spec_path, runtime, plan)
        report = self.base_report(spec_path, runtime, plan.strategy_id, plan.validity)
        start = time.perf_counter()
        target = spec.get("target", {})
        board = _coerce_2048_board(target.get("board"))
        if not board:
            raise ProfileError("2048 spec target.board is required")
        # Write the parsed matrix back so ``_validate`` (which reads
        # ``target['board']`` directly) never iterates a raw string char-by-char.
        target["board"] = board
        score = int(target.get("score", 0))
        state = self._serialized_state(board, score, bool(target.get("won", False)), bool(target.get("over", False)), bool(target.get("keepPlaying", False)))
        report.checkpoint_synthesis = PhaseResult("success", "Serialized valid 2048 gameState payload.", {"state": state})
        storage_values = {"gameState": json_dumps(state)}
        if "bestScore" in target:
            storage_values["bestScore"] = str(target["bestScore"])
        report.hook_availability = self._open_with_hook(runtime, "/?warptest=1", timeout_ms=10_000)
        report.storage_keys_touched = LocalStorageSaveAdapter(runtime).write_many(storage_values, [], self.inspect_result.storage_policy)
        runtime.page.reload(wait_until="domcontentloaded")
        VanillaDOMAdapter(runtime).wait_for_tiles(timeout_ms=10_000)
        report.checkpoint_restoration = PhaseResult("success", "Restored through localStorage and original GameManager setup.")

        self._validate_or_skip(report, runtime, target, plan)
        before_score = self._score(runtime)
        smoke = self._run_actions(runtime, spec.get("actions", []))
        report.smoke_execution = smoke
        report.oracle_result = self._assert_oracles(runtime, spec.get("assertions", []), before_score)
        return self._complete_report(
            report,
            runtime,
            start,
            metrics={
                "time_to_first_action_ms": smoke.data.get("time_to_first_action_ms"),
                "actions_replayed": 0,
            },
        )

    def _run_replay_from_start(self, spec: dict[str, Any], spec_path: Path, runtime: BrowserRuntimeAdapter, plan: StrategyPlan | None = None) -> RunReport:
        replay = spec.get("replay_from_start", {})
        random_values = replay.get("random_values")
        if not isinstance(random_values, list):
            raise ProfileError("2048 replay_from_start.random_values is required for deterministic replay baseline.")
        report = self.base_report(spec_path, runtime, (plan.strategy_id if plan else "deterministic_replay_from_start"), (plan.validity if plan else "replay-reachable"))
        start = time.perf_counter()
        target = spec.get("target", {})
        report.checkpoint_synthesis = PhaseResult(
            "success",
            "Loaded deterministic clean-start replay plan.",
            {"random_values": random_values, "replay_actions": replay.get("actions", [])},
        )
        runtime.context.add_init_script(
            script=f"""
            (() => {{
              const values = {json_dumps(random_values)};
              let index = 0;
              const originalRandom = Math.random.bind(Math);
              Math.random = () => index < values.length ? values[index++] : originalRandom();
            }})();
            """,
        )
        report.hook_availability = self._open_with_hook(runtime, "/?warptest=1&warptestReplay=1", timeout_ms=10_000)
        runtime.page.wait_for_selector(".tile-container .tile", timeout=10_000)
        replay_actions = replay.get("actions", [])
        if not isinstance(replay_actions, list):
            raise ProfileError("2048 replay_from_start.actions must be a list when present.")
        report.checkpoint_restoration = self._run_actions(runtime, replay_actions)
        if report.checkpoint_restoration.status == "success":
            report.checkpoint_restoration.detail = f"Replayed clean start with {len(replay_actions)} action(s)."
        self._validate_or_skip(report, runtime, target, plan or "jump_and_validate")
        before_score = self._score(runtime)
        smoke = self._run_actions(runtime, spec.get("actions", []))
        report.smoke_execution = smoke
        report.oracle_result = self._assert_oracles(runtime, spec.get("assertions", []), before_score)
        return self._complete_report(
            report,
            runtime,
            start,
            metrics={
                "time_to_first_action_ms": smoke.data.get("time_to_first_action_ms"),
                "actions_replayed": len(replay_actions),
                "random_decisions_replayed": len(random_values),
            },
        )

    @staticmethod
    def _serialized_state(board_rows: list[list[int]], score: int, won: bool, over: bool, keep_playing: bool) -> dict[str, Any]:
        if len(board_rows) != 4 or any(len(row) != 4 for row in board_rows):
            raise ProfileError("2048 board must be a 4x4 row-major matrix")
        cells: list[list[dict[str, Any] | None]] = []
        for x in range(4):
            column: list[dict[str, Any] | None] = []
            for y in range(4):
                value = int(board_rows[y][x])
                column.append({"position": {"x": x, "y": y}, "value": value} if value else None)
            cells.append(column)
        return {"grid": {"size": 4, "cells": cells}, "score": score, "over": over, "won": won, "keepPlaying": keep_playing}

    @staticmethod
    def _score(runtime: BrowserRuntimeAdapter) -> int:
        return VanillaDOMAdapter(runtime).score()

    def _validate(self, runtime: BrowserRuntimeAdapter, target: dict[str, Any]) -> PhaseResult:
        storage = LocalStorageSaveAdapter(runtime)
        dom = VanillaDOMAdapter(runtime)
        state = storage.read_json("gameState")
        memory_state = runtime.page.evaluate("() => window.__WARPTEST_2048__?.getState?.() ?? null")
        dom_tiles = dom.dom_tiles()
        score = dom.score()
        expected_values = [str(v) for row in target["board"] for v in row if int(v) > 0]
        actual_values = sorted(tile["text"] for tile in dom_tiles)
        if sorted(expected_values) != actual_values:
            return PhaseResult("failure", "DOM tiles do not match restored target board.", {"expected": expected_values, "actual": actual_values})
        if score != int(target.get("score", 0)):
            return PhaseResult("failure", "Score does not match restored target.", {"expected": target.get("score", 0), "actual": score})
        data = {
            "state": state,
            "memory_persistence_diff": storage.memory_vs_persistence(memory_state, "gameState"),
            "soft_lock_signal": runtime.page.evaluate(
                """(state) => {
                  const gameTerminated = !!(state?.over || (state?.won && !state?.keepPlaying));
                  const message = document.querySelector(".game-message");
                  return {
                    game_terminated: gameTerminated,
                    overlay_visible: !!message && message.classList.contains("game-over") || !!message && message.classList.contains("game-won"),
                    won: !!state?.won,
                    over: !!state?.over,
                    keep_playing: !!state?.keepPlaying
                  };
                }""",
                memory_state or state,
            ),
        }
        return PhaseResult("success", "DOM tiles, score, memory state, and localStorage gameState agree.", data)

    @staticmethod
    def _state_diff(memory_state: Any, persisted_state: Any, *, persistence_key: str) -> dict[str, Any]:
        memory_json = json.dumps(memory_state, sort_keys=True) if memory_state is not None else None
        persisted_json = json.dumps(persisted_state, sort_keys=True) if persisted_state is not None else None
        return {
            "persistence_key": persistence_key,
            "memory_present": memory_state is not None,
            "persistence_present": persisted_state is not None,
            "matches": memory_json == persisted_json if memory_json is not None and persisted_json is not None else None,
        }

    def _run_actions(self, runtime: BrowserRuntimeAdapter, actions: list[dict[str, Any]]) -> PhaseResult:
        first_action_ms: int | None = None
        start = time.perf_counter()
        dom = VanillaDOMAdapter(runtime)
        for index, action in enumerate(actions):
            if first_action_ms is None:
                first_action_ms = int((time.perf_counter() - start) * 1000)
            if action.get("type") == "move":
                dom.press_move(action["direction"], int(action.get("wait_ms", 350)))
            elif action.get("type") == "restart":
                dom.restart()
            else:
                return PhaseResult("failure", f"Unsupported 2048 action at index {index}: {action}")
        return PhaseResult("success", f"Ran {len(actions)} action(s).", {"time_to_first_action_ms": first_action_ms})

    def _assert_oracles(self, runtime: BrowserRuntimeAdapter, assertions: list[dict[str, Any]], before_score: int) -> PhaseResult:
        for assertion in assertions:
            kind = assertion.get("type")
            if kind == "score_increased_by_at_least":
                delta = self._score(runtime) - before_score
                if delta < int(assertion["amount"]):
                    return PhaseResult("failure", "Score did not increase enough.", {"delta": delta, "expected": assertion["amount"]})
            elif kind == "tile_value_present":
                values = [tile["text"] for tile in VanillaDOMAdapter(runtime).dom_tiles()]
                if str(assertion["value"]) not in values:
                    return PhaseResult("failure", "Expected tile value is not visible.", {"values": values, "expected": assertion["value"]})
            elif kind in {
                "largest_tile_equals",
                "empty_cell_count_at_least",
                "tile_count_at_least",
                "board_cell_equals",
            }:
                state = runtime.page.evaluate(
                    "() => window.__WARPTEST_2048__?.getState?.() ?? JSON.parse(localStorage.getItem('gameState'))"
                )
                cells = (state or {}).get("grid", {}).get("cells", [])
                board = [
                    [
                        int((cells[column][row] or {}).get("value", 0))
                        if column < len(cells) and row < len(cells[column])
                        else 0
                        for column in range(4)
                    ]
                    for row in range(4)
                ]
                flat = [value for row in board for value in row]
                if kind == "largest_tile_equals":
                    actual = max(flat, default=0)
                    expected = int(assertion["value"])
                    if actual != expected:
                        return PhaseResult("failure", "Largest tile did not match.", {"actual": actual, "expected": expected})
                elif kind == "empty_cell_count_at_least":
                    actual = sum(value == 0 for value in flat)
                    expected = int(assertion["count"])
                    if actual < expected:
                        return PhaseResult("failure", "Board did not contain enough empty cells.", {"actual": actual, "expected": expected})
                elif kind == "tile_count_at_least":
                    expected_value = int(assertion["value"])
                    actual = sum(value == expected_value for value in flat)
                    expected = int(assertion["count"])
                    if actual < expected:
                        return PhaseResult("failure", "Board did not contain enough matching tiles.", {"actual": actual, "expected": expected, "value": expected_value})
                else:
                    row = int(assertion["row"])
                    column = int(assertion["column"])
                    actual = board[row][column]
                    expected = int(assertion["value"])
                    if actual != expected:
                        return PhaseResult("failure", "Board cell did not match.", {"row": row, "column": column, "actual": actual, "expected": expected})
            elif kind == "state_equals":
                actual = runtime.page.evaluate(
                    """(path) => {
                      const state = window.__WARPTEST_2048__?.getState?.() ?? JSON.parse(localStorage.getItem('gameState'));
                      return path.split('.').reduce((value, key) => value?.[key], state);
                    }""",
                    assertion["path"],
                )
                if actual != assertion["value"]:
                    return PhaseResult("failure", f"State assertion failed for {assertion['path']}.", {"actual": actual, "expected": assertion["value"]})
            elif kind == "no_severe_console":
                continue
            else:
                return PhaseResult("failure", f"Unsupported 2048 assertion: {assertion}")
        return PhaseResult("success", "All 2048 oracles passed.")

    def supports_snapshot(self) -> bool:
        return True

    def capture_runtime_state(self, runtime: BrowserRuntimeAdapter) -> dict[str, Any] | None:
        values = runtime.page.evaluate(
            """() => ({
              gameState: window.localStorage.getItem('gameState'),
              bestScore: window.localStorage.getItem('bestScore'),
            })"""
        )
        kept = {key: value for key, value in (values or {}).items() if value is not None}
        if "gameState" not in kept:
            return None
        return {"kind": "localStorage", "values": kept}

    def inject_runtime_state(self, runtime: BrowserRuntimeAdapter, snapshot: dict[str, Any]) -> None:
        values = snapshot.get("values") if isinstance(snapshot, dict) else None
        if not isinstance(values, dict) or "gameState" not in values:
            raise ProfileError("2048 snapshot is missing localStorage gameState.")
        self._open_with_hook(runtime, "/?warptest=1", timeout_ms=10_000)
        LocalStorageSaveAdapter(runtime).write_many(values, [], self.inspect_result.storage_policy)
        runtime.page.reload(wait_until="domcontentloaded")
        VanillaDOMAdapter(runtime).wait_for_tiles(timeout_ms=10_000)


class SpaceRPGProfile(BaseProfile):
    name = "SpaceRPGProfile"
    benchmark = "SpaceRPG"
    hook_name = "__SPACERPG_E2E__"
    default_strategy = "test_hook_jump_and_validate"
    checkpoint_validity = "injection-only"
    flight_render_settle_frames = 12
    flight_render_extra_wait_ms = 3_000

    def restore_target_only(
        self,
        spec: dict[str, Any],
        runtime: BrowserRuntimeAdapter,
    ) -> dict[str, Any]:
        plan = self.strategy_plan(spec, "jump_and_validate")
        if plan.unsupported_reason:
            raise ProfileError(plan.unsupported_reason)
        setup = spec.get("setup") if isinstance(spec.get("setup"), dict) else {}
        storage_values = {"spacerpg-e2e-hook": "enabled"}
        if setup.get("disable_economy_backend", True):
            storage_values["spacerpg-e2e-disable-economy-backend"] = "true"
        touched = LocalStorageSaveAdapter(runtime).write_many(
            storage_values,
            [],
            self.inspect_result.storage_policy,
        )
        hook = self._open_with_hook(runtime, "/?warptest=1", timeout_ms=30_000)
        target = spec.get("target", {})
        if not isinstance(target, dict):
            raise ProfileError("SpaceRPG spec target must be a mapping")
        restoration = self._restore_target(runtime, target)
        validation = (
            self._validate(runtime, target)
            if restoration.status == "success"
            else PhaseResult("failure", "SpaceRPG target restoration failed before validation.")
        )
        console_errors = self._severe_console(runtime)
        ok = (
            restoration.status == "success"
            and validation.status == "success"
            and not console_errors
        )
        return {
            "status": "success" if ok else "failure",
            "detail": (
                "Restored and validated the SpaceRPG target without smoke/oracle execution."
                if ok
                else validation.detail or restoration.detail
            ),
            "checkpoint_restoration": asdict(restoration),
            "validation": asdict(validation),
            "storage_keys_touched": touched,
            "hook_availability": hook,
            "console_errors": console_errors,
            "actions_executed": False,
            "assertions_evaluated": False,
            "smoke_execution": "not_run",
            "oracle_result": "not_run",
        }

    def run(self, spec: dict[str, Any], spec_path: Path, runtime: BrowserRuntimeAdapter, mode: str = "jump_and_validate") -> RunReport:
        plan = self.strategy_plan(spec, mode)
        if plan.unsupported_reason:
            raise ProfileError(plan.unsupported_reason)
        report = self.base_report(spec_path, runtime, plan.strategy_id, plan.validity)
        start = time.perf_counter()
        storage_values = {}
        if spec.get("setup", {}).get("disable_economy_backend", True):
            storage_values["spacerpg-e2e-disable-economy-backend"] = "true"
        storage_values["spacerpg-e2e-hook"] = "enabled"
        report.storage_keys_touched = LocalStorageSaveAdapter(runtime).write_many(storage_values, [], self.inspect_result.storage_policy)
        report.hook_availability = self._open_with_hook(runtime, "/?warptest=1", timeout_ms=30_000)
        report.checkpoint_synthesis = PhaseResult("success", "Composed SpaceRPG target from existing store hook/profile semantics.")

        target = spec.get("target", {})
        restore = self._restore_target(runtime, target)
        report.checkpoint_restoration = restore

        self._validate_or_skip(report, runtime, target, plan)
        report.smoke_execution = self._run_actions(runtime, spec.get("actions", []))
        report.oracle_result = self._assert_oracles(runtime, spec.get("assertions", []))
        return self._complete_report(report, runtime, start, screenshot_name="spacerpg-final.png")

    def _restore_target(self, runtime: BrowserRuntimeAdapter, target: dict[str, Any]) -> PhaseResult:
        kind = target.get("kind", "flight")
        if kind == "flight":
            runtime.page.evaluate(
                """() => {
                  const store = window.__SPACERPG_E2E__.getState();
                  store.newGame?.();
                  store.closeDialogue?.();
                  store.stopEconomyStream?.();
                }"""
            )
            return PhaseResult("success", "Started playable flight state through existing game actions.")
        if kind == "debug_scenario":
            scenario_id = target.get("scenario_id")
            if not scenario_id:
                return PhaseResult("failure", "SpaceRPG debug_scenario target requires scenario_id.")
            runtime.page.evaluate(
                """(scenarioId) => {
                  const hook = window.__SPACERPG_E2E__;
                  const store = hook.getState();
                  store.newGame?.();
                  store.closeDialogue?.();
                  store.stopEconomyStream?.();
                  hook.applyDebugScenario?.(scenarioId);
                  store.closeDialogue?.();
                }""",
                scenario_id,
            )
            return PhaseResult("success", f"Applied SpaceRPG debug scenario through the existing test hook: {scenario_id}.")
        if kind == "galaxy_map":
            mode = target.get("galaxy_map_mode", "gate")
            gate_position = target.get("gate_position")
            runtime.page.evaluate(
                """({ mode, gatePosition }) => {
                  const hook = window.__SPACERPG_E2E__;
                  const store = hook.getState();
                  store.newGame?.();
                  store.closeDialogue?.();
                  store.stopEconomyStream?.();
                  if (Array.isArray(gatePosition) && gatePosition.length === 3) {
                    const current = hook.getState();
                    hook.setState({
                      player: {
                        ...current.player,
                        position: gatePosition,
                        velocity: [0, 0, 0],
                        throttle: 0.25
                      }
                    });
                  }
                  hook.getState().openGalaxyMap?.(mode);
                }""",
                {"mode": mode, "gatePosition": gate_position},
            )
            return PhaseResult("success", f"Opened SpaceRPG galaxy map through store action: {mode}.")
        if kind == "station":
            station_id = target.get("station_id", "helion-prime")
            runtime.page.evaluate(
                """(stationId) => {
                  const store = window.__SPACERPG_E2E__.getState();
                  store.newGame?.();
                  store.closeDialogue?.();
                  store.stopEconomyStream?.();
                  store.dockAt?.(stationId);
                  window.__SPACERPG_E2E__.getState().closeDialogue?.();
                }""",
                station_id,
            )
            return PhaseResult("success", f"Docked through existing dockAt action: {station_id}.")
        if kind == "equipment_loadout":
            station_id = target.get("station_id", "helion-prime")
            equipment_inventory = copy.deepcopy(target.get("equipment_inventory", {}))
            credits = target.get("credits")
            runtime.page.evaluate(
                """({ stationId, equipmentInventory, credits, stationTab, cargo, equipment, unlockedBlueprintIds }) => {
                  const hook = window.__SPACERPG_E2E__;
                  const store = hook.getState();
                  store.newGame?.();
                  store.closeDialogue?.();
                  store.stopEconomyStream?.();
                  store.dockAt?.(stationId);
                  window.__SPACERPG_E2E__.getState().closeDialogue?.();
                  const state = hook.getState();
                  hook.setState({
                    stationTab: stationTab || "Hangar",
                    player: {
                      ...state.player,
                      credits: credits ?? state.player.credits,
                      // A declared cargo map is the complete checkpoint state,
                      // including `{}` for an empty hold. Merging with newGame's
                      // starter commodities silently violated C1 preconditions.
                      cargo: cargo !== null ? { ...cargo } : state.player.cargo,
                      equipment: equipment ?? state.player.equipment,
                      unlockedBlueprintIds:
                        unlockedBlueprintIds ?? state.player.unlockedBlueprintIds,
                      equipmentInventory: {
                        ...(state.player?.equipmentInventory || {}),
                        ...(equipmentInventory || {})
                      }
                    }
                  });
                }""",
                {
                    "stationId": station_id,
                    "equipmentInventory": equipment_inventory,
                    "credits": credits,
                    "stationTab": target.get("station_tab"),
                    "cargo": copy.deepcopy(target.get("cargo")),
                    "equipment": copy.deepcopy(target.get("equipment")),
                    "unlockedBlueprintIds": copy.deepcopy(
                        target.get("unlocked_blueprint_ids")
                    ),
                },
            )
            return PhaseResult("success", f"Prepared SpaceRPG equipment inventory at station: {station_id}.", {"equipment_inventory": equipment_inventory})
        if kind == "state_patch":
            patch = copy.deepcopy(target.get("patch", {}))
            runtime.page.evaluate(
                """(patch) => {
                  const store = window.__SPACERPG_E2E__.getState();
                  store.stopEconomyStream?.();
                  window.__SPACERPG_E2E__.setState(patch);
                }""",
                patch,
            )
            return PhaseResult("success", "Applied state patch through existing __SPACERPG_E2E__.setState.", {"validity": "injection-only"})
        # -- Fuzz injection kinds (C3 robustness) -------------------------- #
        # Split into warptest.spacerpg_fuzz_profile and imported lazily so editing
        # these C3-only injection paths never touches profiles.py's static
        # import closure (see that module's docstring and
        # c2_provenance._C2_EXCLUDED_RUNTIME_MODULES).
        if isinstance(kind, str) and kind.startswith("fuzz_"):
            from .spacerpg_fuzz_profile import restore_fuzz_target

            return restore_fuzz_target(self, runtime, target)
        return PhaseResult("failure", f"Unsupported SpaceRPG target kind: {kind}")

    def _validate(self, runtime: BrowserRuntimeAdapter, target: dict[str, Any]) -> PhaseResult:
        data: dict[str, Any] = {}
        if target.get("kind") in {"flight", "state_patch"}:
            try:
                data["canvas"] = ThreeCanvasAdapter(runtime).wait_ready(timeout_ms=25_000)
            except Exception as error:
                return PhaseResult("failure", f"Canvas/WebGL readiness failed: {error}")
        data["render_readiness_signal"] = {
            "canvas": data.get("canvas", ThreeCanvasAdapter(runtime).readiness_signal()),
            "hook": runtime.page.evaluate("() => window.__SPACERPG_E2E__?.getReadiness?.() ?? null"),
        }
        state = runtime.page.evaluate(
            """() => {
              const s = window.__SPACERPG_E2E__.getState();
              return {
                screen: s.screen,
                currentSystemId: s.currentSystemId,
                currentStationId: s.currentStationId,
                stationTab: s.stationTab,
                galaxyMapMode: s.galaxyMapMode,
                activeMissionIds: (s.activeMissions || []).map((m) => m.id),
                completedMissionIds: s.completedMissionIds || [],
                knownSystems: s.knownSystems || [],
                cargo: s.player?.cargo,
                credits: s.player?.credits,
                equipment: s.player?.equipment || [],
                equipmentInventory: s.player?.equipmentInventory || {},
                activeSaveSlotId: s.activeSaveSlotId,
                hasSave: s.hasSave,
                saveSlotCount: s.saveSlots?.filter((slot) => slot.exists).length ?? 0
              };
            }"""
        )
        data["state"] = state
        data["memory_persistence_diff"] = runtime.page.evaluate(
            """() => {
              const s = window.__SPACERPG_E2E__.getState();
              const slotId = s.activeSaveSlotId;
              const key = slotId ? `spacerpg-save-slot:${slotId}` : undefined;
              let saved = null;
              if (key) {
                try {
                  const raw = localStorage.getItem(key);
                  saved = raw ? JSON.parse(raw) : null;
                } catch {
                  saved = null;
                }
              }
              const memory = {
                screen: s.screen,
                currentSystemId: s.currentSystemId,
                currentStationId: s.currentStationId,
                credits: s.player?.credits,
                cargo: s.player?.cargo,
                activeMissionIds: (s.activeMissions || []).map((m) => m.id),
                completedMissionIds: s.completedMissionIds || []
              };
              const persistence = saved ? {
                screen: saved.screen,
                currentSystemId: saved.currentSystemId,
                currentStationId: saved.currentStationId,
                credits: saved.player?.credits,
                cargo: saved.player?.cargo,
                activeMissionIds: (saved.activeMissions || []).map((m) => m.id),
                completedMissionIds: saved.completedMissionIds || []
              } : null;
              return {
                persistence_key: key ?? null,
                active_save_slot_id: slotId ?? null,
                memory_present: true,
                persistence_present: !!saved,
                matches: persistence ? JSON.stringify(memory) === JSON.stringify(persistence) : null,
                memory,
                persistence
              };
            }"""
        )
        data["soft_lock_signal"] = runtime.page.evaluate(
            """() => {
              const s = window.__SPACERPG_E2E__.getState();
              const canvas = document.querySelector("canvas");
              const rect = canvas?.getBoundingClientRect();
              const actionableButtons = [...document.querySelectorAll("button")].filter((button) => !button.disabled).length;
              return {
                screen: s.screen,
                game_over_screen: s.screen === "gameover",
                dialogue_open: !!s.dialogueState?.active,
                canvas_present: !!canvas,
                canvas_size: { width: rect?.width ?? 0, height: rect?.height ?? 0 },
                actionable_buttons: actionableButtons,
                runtime_message: s.runtime?.message ?? null
              };
            }"""
        )
        expected_screen = target.get("screen")
        if expected_screen and state["screen"] != expected_screen:
            return PhaseResult("failure", "SpaceRPG screen mismatch.", {"expected": expected_screen, "actual": state, **data})
        if target.get("system_id") and state["currentSystemId"] != target["system_id"]:
            return PhaseResult("failure", "SpaceRPG system mismatch.", {"expected": target["system_id"], "actual": state, **data})
        if target.get("station_id") and state["currentStationId"] != target["station_id"]:
            return PhaseResult("failure", "SpaceRPG station mismatch.", {"expected": target["station_id"], "actual": state, **data})
        if target.get("station_tab") and state["stationTab"] != target["station_tab"]:
            return PhaseResult("failure", "SpaceRPG station tab mismatch.", {"expected": target["station_tab"], "actual": state, **data})
        if target.get("galaxy_map_mode") and state["galaxyMapMode"] != target["galaxy_map_mode"]:
            return PhaseResult("failure", "SpaceRPG galaxy map mode mismatch.", {"expected": target["galaxy_map_mode"], "actual": state, **data})
        if "credits" in target and state["credits"] != target["credits"]:
            return PhaseResult("failure", "SpaceRPG credits mismatch.", {"expected": target["credits"], "actual": state, **data})
        if "cargo" in target and state["cargo"] != target["cargo"]:
            return PhaseResult("failure", "SpaceRPG cargo mismatch.", {"expected": target["cargo"], "actual": state, **data})
        if "equipment" in target and state["equipment"] != target["equipment"]:
            return PhaseResult("failure", "SpaceRPG equipment mismatch.", {"expected": target["equipment"], "actual": state, **data})
        for mission_id in target.get("mission_visible", []):
            if mission_id not in state["activeMissionIds"]:
                return PhaseResult("failure", "SpaceRPG mission visibility mismatch.", {"missing": mission_id, **data})
        for mission_id in target.get("completed_mission_visible", []):
            if mission_id not in state["completedMissionIds"]:
                return PhaseResult("failure", "SpaceRPG completed mission mismatch.", {"missing": mission_id, **data})
        for system_id in target.get("known_system_visible", []):
            if system_id not in state["knownSystems"]:
                return PhaseResult("failure", "SpaceRPG known system mismatch.", {"missing": system_id, **data})
        for equipment_id, amount in target.get("equipment_inventory", {}).items():
            if state["equipmentInventory"].get(equipment_id, 0) != amount:
                return PhaseResult("failure", "SpaceRPG equipment inventory mismatch.", {"equipment": equipment_id, "expected": amount, **data})
        return PhaseResult("success", "SpaceRPG semantic state and coarse UI/render readiness validated.", data)

    def _wait_for_flight_scene_rendered(
        self,
        runtime: BrowserRuntimeAdapter,
        *,
        previous_render_frame: int,
        timeout_ms: int = 120_000,
    ) -> dict[str, Any]:
        """Wait for a newly mounted SpaceRPG flight scene to render loaded content.

        SpaceRPG's heartbeat is inside the flight scene's Suspense boundary, so it
        cannot advance until the scene assets have resolved. Requiring several
        frames beyond the pre-transition value prevents the visual-pair runner
        from capturing the mounted HUD while the WebGL scene is still black.
        Allow cold assets and a contended software renderer to reach those
        same frames; the former 25-second cap could expire at frame 11 of 12.
        """
        from .c1_task_deadline import bounded_task_timeout_s

        timeout_ms = max(1, int(bounded_task_timeout_s(timeout_ms / 1000.0) * 1000))
        runtime.page.wait_for_function(
            """({ previousRenderFrame, settleFrames }) => {
              const readiness = window.__SPACERPG_E2E__?.getReadiness?.();
              const renderFrame = Number(readiness?.renderFrame);
              return readiness?.currentScreen === "flight"
                && readiness?.screenMounted === true
                && readiness?.sceneContainerPresent === true
                && readiness?.canvasPresent === true
                && Number(readiness?.canvasSize?.width) > 32
                && Number(readiness?.canvasSize?.height) > 32
                && readiness?.assetManifestReady === true
                && Number.isFinite(renderFrame)
                && renderFrame >= previousRenderFrame + settleFrames;
            }""",
            arg={
                "previousRenderFrame": previous_render_frame,
                "settleFrames": self.flight_render_settle_frames,
            },
            timeout=timeout_ms,
        )
        extra_wait_ms = max(
            1, int(bounded_task_timeout_s(self.flight_render_extra_wait_ms / 1000.0) * 1000)
        )
        runtime.page.wait_for_timeout(extra_wait_ms)
        readiness = runtime.page.evaluate(
            "() => window.__SPACERPG_E2E__?.getReadiness?.() ?? null"
        )
        return readiness if isinstance(readiness, dict) else {}

    def _run_actions(self, runtime: BrowserRuntimeAdapter, actions: list[dict[str, Any]]) -> PhaseResult:
        for action in actions:
            if action.get("type") == "key":
                runtime.page.keyboard.press(action["key"])
                runtime.page.wait_for_timeout(int(action.get("wait_ms", 250)))
            elif action.get("type") == "hook_call":
                TestHookAdapter(runtime, self.hook_name).call(action["method"], action.get("args", []), state_method=True)
            elif action.get("type") == "spacerpg_patch_state":
                runtime.page.evaluate(
                    """(patch) => {
                      const hook = window.__SPACERPG_E2E__;
                      const state = hook.getState();
                      const next = { ...patch };
                      if (patch.player) {
                        next.player = {
                          ...state.player,
                          ...patch.player,
                          cargo: patch.player.cargo
                            ? { ...(state.player?.cargo || {}), ...patch.player.cargo }
                            : state.player.cargo,
                          equipmentInventory: patch.player.equipmentInventory
                            ? { ...(state.player?.equipmentInventory || {}), ...patch.player.equipmentInventory }
                            : state.player.equipmentInventory,
                        };
                      }
                      if (patch.runtime) {
                        next.runtime = { ...state.runtime, ...patch.runtime };
                      }
                      if (patch.factionHeat) {
                        next.factionHeat = { ...state.factionHeat, ...patch.factionHeat };
                      }
                      hook.setState(next);
                    }""",
                    action["patch"],
                )
            elif action.get("type") == "spacerpg_mine_sortie":
                resource = str(action.get("resource") or "")
                amount = int(action.get("amount") or 0)
                system_id = str(action.get("system_id") or "")
                if resource != "iron" or amount != 2 or system_id != "kuro-belt":
                    return PhaseResult(
                        "failure",
                        "SpaceRPG mining sortie is outside the registered C1 fixture.",
                    )
                result = runtime.page.evaluate(
                    """({ resource, amount, systemId }) => {
                      const hook = window.__SPACERPG_E2E__;
                      const state = hook.getState();
                      const previousRenderFrame =
                        Number(window.__SPACERPG_RENDER_HEARTBEAT_FRAME__ ?? 0);
                      if (!state.player.equipment.includes("industrial-mining-beam"))
                        return { ok: false, reason: "industrial mining beam is not installed" };
                      if (state.currentStationId !== "meridian-dock")
                        return { ok: false, reason: "sortie must depart from meridian-dock" };
                      state.jumpToSystem?.(systemId);
                      const flight = hook.getState();
                      if (
                        flight.screen !== "flight" ||
                        flight.currentSystemId !== systemId ||
                        flight.currentStationId !== undefined
                      ) {
                        return {
                          ok: false,
                          reason: "native jump did not produce the requested flight state"
                        };
                      }
                      hook.setState({
                        player: {
                          ...flight.player,
                          cargo: { ...flight.player.cargo, [resource]: amount }
                        },
                        runtime: {
                          ...flight.runtime,
                          mined: { [resource]: amount },
                          message: `Mined ${amount} Iron. Cargo cycle complete.`
                        }
                      });
                      return {
                        ok: true,
                        previousRenderFrame
                      };
                    }""",
                    {
                        "resource": resource,
                        "amount": amount,
                        "systemId": system_id,
                    },
                )
                if not isinstance(result, dict) or result.get("ok") is not True:
                    return PhaseResult(
                        "failure",
                        "SpaceRPG mining sortie precondition failed.",
                        {"result": result},
                    )
                try:
                    self._wait_for_flight_scene_rendered(
                        runtime,
                        previous_render_frame=int(
                            result.get("previousRenderFrame") or 0
                        ),
                    )
                except Exception as error:  # noqa: BLE001 - surface readiness evidence
                    readiness = runtime.page.evaluate(
                        "() => window.__SPACERPG_E2E__?.getReadiness?.() ?? null"
                    )
                    return PhaseResult(
                        "failure",
                        "SpaceRPG mining sortie flight scene did not become render-ready.",
                        {"error": str(error), "readiness": readiness},
                    )
            elif action.get("type") == "wait":
                runtime.page.wait_for_timeout(int(action.get("ms", 250)))
            else:
                return PhaseResult("failure", f"Unsupported SpaceRPG action: {action}")
        return PhaseResult("success", f"Ran {len(actions)} action(s).")

    def _assert_oracles(self, runtime: BrowserRuntimeAdapter, assertions: list[dict[str, Any]]) -> PhaseResult:
        for assertion in assertions:
            kind = assertion.get("type")
            if kind == "state_equals":
                actual = runtime.page.evaluate(
                    """(path) => path.split('.').reduce((value, key) => value?.[key], window.__SPACERPG_E2E__.getState())""",
                    assertion["path"],
                )
                if actual != assertion["value"]:
                    return PhaseResult("failure", f"State assertion failed for {assertion['path']}.", {"actual": actual, "expected": assertion["value"]})
            elif kind == "state_contains":
                actual = runtime.page.evaluate(
                    """(path) => path.split('.').reduce((value, key) => value?.[key], window.__SPACERPG_E2E__.getState())""",
                    assertion["path"],
                )
                expected = assertion["value"]
                if not isinstance(actual, list) or expected not in actual:
                    return PhaseResult("failure", f"State array does not contain expected value for {assertion['path']}.", {"actual": actual, "expected": expected})
            elif kind == "state_gte":
                actual = runtime.page.evaluate(
                    """(path) => path.split('.').reduce((value, key) => value?.[key], window.__SPACERPG_E2E__.getState())""",
                    assertion["path"],
                )
                if not isinstance(actual, (int, float)) or actual < assertion["value"]:
                    return PhaseResult("failure", f"State value is below expected lower bound for {assertion['path']}.", {"actual": actual, "expected_min": assertion["value"]})
            elif kind == "state_absent":
                exists = runtime.page.evaluate(
                    """(path) => path.split('.').reduce((value, key) => value?.[key], window.__SPACERPG_E2E__.getState()) !== undefined""",
                    assertion["path"],
                )
                if exists:
                    return PhaseResult("failure", f"State path should be absent: {assertion['path']}")
            elif kind == "localStorage_key_exists":
                exists = runtime.page.evaluate("(key) => localStorage.getItem(key) !== null", assertion["key"])
                if not exists:
                    return PhaseResult("failure", f"Expected localStorage key missing: {assertion['key']}")
            elif kind == "no_severe_console":
                recovery_visible = runtime.page.evaluate(
                    """() => {
                      const text = document.body?.innerText || "";
                      return text.includes("RECOVERY MODE") ||
                        text.includes("Unable to continue");
                    }"""
                )
                if recovery_visible:
                    return PhaseResult(
                        "failure",
                        "SpaceRPG entered its recovery screen instead of a valid game route.",
                    )
                continue
            else:
                return PhaseResult("failure", f"Unsupported SpaceRPG assertion: {assertion}")
        return PhaseResult("success", "All SpaceRPG oracles passed.")

    def supports_snapshot(self) -> bool:
        return True

    def capture_runtime_state(self, runtime: BrowserRuntimeAdapter) -> dict[str, Any] | None:
        # JSON-clone drops action functions / undefined, leaving the serializable
        # data fields that setState can merge back to reconstruct the store.
        hook_state = runtime.page.evaluate(
            """() => {
              const hook = window.__SPACERPG_E2E__;
              if (!hook || typeof hook.getState !== 'function') return null;
              try { return JSON.parse(JSON.stringify(hook.getState())); }
              catch (e) { return null; }
            }"""
        )
        if not isinstance(hook_state, dict) or not hook_state:
            return None
        return {"kind": "hook_state", "hook": self.hook_name, "hook_state": hook_state}

    def inject_runtime_state(self, runtime: BrowserRuntimeAdapter, snapshot: dict[str, Any]) -> None:
        hook_state = snapshot.get("hook_state") if isinstance(snapshot, dict) else None
        if not isinstance(hook_state, dict) or not hook_state:
            raise ProfileError("SpaceRPG snapshot is missing hook_state.")
        storage_values = {"spacerpg-e2e-disable-economy-backend": "true", "spacerpg-e2e-hook": "enabled"}
        LocalStorageSaveAdapter(runtime).write_many(storage_values, [], self.inspect_result.storage_policy)
        self._open_with_hook(runtime, "/?warptest=1", timeout_ms=30_000)
        runtime.page.evaluate(
            """(state) => {
              const hook = window.__SPACERPG_E2E__;
              hook.getState().stopEconomyStream?.();
              hook.setState(state);
              hook.getState().closeDialogue?.();
            }""",
            hook_state,
        )


class DatingSimProfile(BaseProfile):
    name = "DatingSimProfile"
    benchmark = "DatingSim"
    hook_name = "__WARPTEST_LOVE__"
    default_strategy = "test_hook_seeded_state"
    checkpoint_validity = "injection-only"

    def restore_target_only(
        self,
        spec: dict[str, Any],
        runtime: BrowserRuntimeAdapter,
    ) -> dict[str, Any]:
        plan = self.strategy_plan(spec, "jump_and_validate")
        if plan.unsupported_reason:
            raise ProfileError(plan.unsupported_reason)
        setup = spec.get("setup") if isinstance(spec.get("setup"), dict) else {}
        touched = LocalStorageSaveAdapter(runtime).write_many(
            {"warptest-love-ai-mode": setup.get("ai_mode", "mocked")},
            ["love_emulator_save_v1"],
            self.inspect_result.storage_policy,
        )
        runtime.block_ai_network("mocked")
        hook = self._open_with_hook(
            runtime,
            "/?warptest=1&ai=mocked",
            timeout_ms=20_000,
        )
        target = spec.get("target", {})
        if not isinstance(target, dict):
            raise ProfileError("DatingSim spec target must be a mapping")
        zustand = ZustandHookAdapter(TestHookAdapter(runtime, self.hook_name))
        zustand.initialize(
            {
                "seed": target.get("seed", 1),
                "name": target.get("player_name", "WarpTest"),
                "gender": target.get("gender", "male"),
            }
        )
        if target.get("patch"):
            zustand.patch_state(target["patch"])
        if target.get("save"):
            zustand.save()
        validation = self._validate(runtime, target)
        console_errors = self._severe_console(runtime)
        external_attempts = list(runtime.network_api_calls.get("attempted", []))
        ok = (
            validation.status == "success"
            and not console_errors
            and not external_attempts
        )
        return {
            "status": "success" if ok else "failure",
            "detail": (
                "Restored and validated the DatingSim target without smoke/oracle execution."
                if ok
                else validation.detail or "DatingSim target-only validation failed."
            ),
            "checkpoint_restoration": {
                "status": "success",
                "detail": "Initialized deterministic game state through the test-gated hook.",
            },
            "validation": asdict(validation),
            "storage_keys_touched": touched,
            "hook_availability": hook,
            "console_errors": console_errors,
            "network_api_calls": copy.deepcopy(runtime.network_api_calls),
            "actions_executed": False,
            "assertions_evaluated": False,
            "smoke_execution": "not_run",
            "oracle_result": "not_run",
        }

    def run(self, spec: dict[str, Any], spec_path: Path, runtime: BrowserRuntimeAdapter, mode: str = "jump_and_validate") -> RunReport:
        plan = self.strategy_plan(spec, mode)
        if plan.unsupported_reason:
            raise ProfileError(plan.unsupported_reason)
        if mode == "replay_from_start":
            return self._run_replay_from_start(spec, spec_path, runtime, plan)
        report = self.base_report(spec_path, runtime, plan.strategy_id, plan.validity)
        start = time.perf_counter()
        storage = {"warptest-love-ai-mode": spec.get("setup", {}).get("ai_mode", "mocked")}
        report.storage_keys_touched = LocalStorageSaveAdapter(runtime).write_many(storage, ["love_emulator_save_v1"], self.inspect_result.storage_policy)
        runtime.block_ai_network("mocked")
        report.hook_availability = self._open_with_hook(runtime, "/?warptest=1&ai=mocked", timeout_ms=20_000)
        report.checkpoint_synthesis = PhaseResult("success", "Composed deterministic DatingSim seed/patch target.")
        target = spec.get("target", {})
        zustand = ZustandHookAdapter(TestHookAdapter(runtime, self.hook_name))
        zustand.initialize({"seed": target.get("seed", 1), "name": target.get("player_name", "WarpTest"), "gender": target.get("gender", "male")})
        if target.get("patch"):
            zustand.patch_state(target["patch"])
        if target.get("save"):
            zustand.save()
        report.checkpoint_restoration = PhaseResult("success", "Initialized deterministic game state through test-gated hook.")
        self._validate_or_skip(report, runtime, target, plan)
        report.smoke_execution = self._run_actions(runtime, spec.get("actions", []))
        report.oracle_result = self._assert_oracles(runtime, spec.get("assertions", []))
        ai_status = runtime.page.evaluate("() => window.__WARPTEST_LOVE_AI__ || { mode: 'unknown', mockedCalls: 0, realCalls: 0 }")
        self._record_network_policy(
            report,
            runtime,
            policy="mocked_by_default",
            ai_status=ai_status,
            failure_detail="Default DatingSim smoke attempted a real OpenAI call.",
        )
        return self._complete_report(report, runtime, start, screenshot_name="love-final.png")

    def _run_replay_from_start(self, spec: dict[str, Any], spec_path: Path, runtime: BrowserRuntimeAdapter, plan: StrategyPlan | None = None) -> RunReport:
        replay = spec.get("replay_from_start", {})
        replay_actions = replay.get("actions", [])
        if not isinstance(replay_actions, list):
            raise ProfileError("DatingSim replay_from_start.actions must be a list when present.")
        report = self.base_report(spec_path, runtime, (plan.strategy_id if plan else "deterministic_replay_from_start"), (plan.validity if plan else "replay-reachable"))
        start = time.perf_counter()
        target = spec.get("target", {})
        runtime.block_ai_network("mocked")
        runtime.open("/?ai=mocked")
        runtime.page.locator(".setup-modal").wait_for(state="visible", timeout=20_000)
        gender_index = 1 if target.get("gender") == "female" else 0
        runtime.page.locator(".gender-options button").nth(gender_index).click()
        runtime.page.locator(".name-input").fill(
            str(target.get("player_name", "WarpTest"))
        )
        runtime.page.locator(".setup-actions .primary-btn").click()
        runtime.page.locator(".setup-modal").wait_for(state="hidden", timeout=5_000)
        private_hook_absent = bool(
            runtime.page.evaluate(
                "() => typeof window['__WARPTEST_LOVE__'] === 'undefined'"
            )
        )
        report.storage_keys_touched = []
        report.hook_availability = {
            "available": not private_hook_absent,
            "name": self.hook_name,
            "enabled_by": "public_replay_attestation",
        }
        report.runtime_access_methods = {
            "love_runtime_access_method": "public_setup_and_dom_actions",
            "hook_name": self.hook_name,
            "private_hook_absent": private_hook_absent,
        }
        report.checkpoint_synthesis = PhaseResult(
            "success",
            "Loaded deterministic DatingSim clean-start replay plan.",
            {"replay_actions": replay_actions},
        )
        report.checkpoint_restoration = self._run_public_actions(
            runtime, replay_actions
        )
        if (
            not private_hook_absent
            and report.checkpoint_restoration.status == "success"
        ):
            report.checkpoint_restoration = PhaseResult(
                "failure", "Public replay unexpectedly exposed the private state hook."
            )
        if report.checkpoint_restoration.status == "success":
            report.checkpoint_restoration.detail = f"Replayed clean start with {len(replay_actions)} action(s)."
        from .c1_public_semantics import grade_public_goal, grade_public_start

        start_ok, start_status, start_detail = grade_public_start(
            self, runtime, spec
        ) or (False, "validate_error", {})
        report.validation = PhaseResult(
            "success" if start_ok else "failure",
            start_status,
            start_detail,
        )
        report.target_validated_before_smoke = bool(start_ok)
        public_actions = spec.get("actions", [])
        report.smoke_execution = self._run_public_actions(runtime, public_actions)
        if public_actions or spec.get("assertions"):
            goal_ok, goal_status, goal_detail = grade_public_goal(
                self, runtime, spec
            ) or (False, "assert_error", {})
            report.oracle_result = PhaseResult(
                "success" if goal_ok else "failure",
                goal_status,
                goal_detail,
            )
        else:
            report.oracle_result = PhaseResult(
                "success", "Reference replay stops at the validated public start."
            )
        self._record_network_policy(
            report,
            runtime,
            policy="mocked_by_default",
            failure_detail="Default DatingSim smoke attempted a real OpenAI call.",
        )
        return self._complete_report(
            report,
            runtime,
            start,
            metrics={"actions_replayed": len(replay_actions)},
            screenshot_name="love-final.png",
        )

    def _run_public_actions(
        self,
        runtime: BrowserRuntimeAdapter,
        actions: list[dict[str, Any]],
    ) -> PhaseResult:
        action_indices = {
            "work_coding": 0,
            "gym_workout": 1,
            "spa_visit": 2,
            "date_coffee": 3,
        }
        for action in actions:
            kind = action.get("type")
            if kind == "hook_action":
                action_id = str(action.get("action_id") or "")
                index = action_indices.get(action_id)
                if index is None:
                    return PhaseResult(
                        "failure", f"No public action mapping for {action_id}."
                    )
                runtime.page.locator(".action-grid .action-card").nth(index).click(
                    timeout=5_000
                )
                runtime.page.wait_for_timeout(int(action.get("wait_ms", 500)))
            elif kind == "click":
                runtime.page.locator(str(action["selector"])).click(timeout=5_000)
            elif kind == "wait":
                runtime.page.wait_for_timeout(int(action.get("ms", 250)))
            elif kind == "settle_events":
                settled = self._settle_events(
                    runtime, timeout_s=float(action.get("timeout_s", 12))
                )
                if settled.status != "success":
                    return settled
            else:
                return PhaseResult(
                    "failure", f"Unsupported public DatingSim action: {action}"
                )
        return PhaseResult("success", f"Ran {len(actions)} public action(s).")

    def _settle_events(
        self,
        runtime: BrowserRuntimeAdapter,
        *,
        timeout_s: float,
    ) -> PhaseResult:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            choice = runtime.page.locator(".choices-list .choice-btn")
            if choice.count() and choice.first.is_visible():
                choice.first.click(timeout=2_000)
                runtime.page.wait_for_timeout(100)
                continue
            loading = runtime.page.locator(".event-loading-overlay")
            if loading.count() and loading.first.is_visible():
                runtime.page.wait_for_timeout(100)
                continue
            runtime.page.wait_for_timeout(200)
            if not (
                (choice.count() and choice.first.is_visible())
                or (loading.count() and loading.first.is_visible())
            ):
                return PhaseResult("success", "DatingSim events settled.")
        return PhaseResult(
            "failure", "Timed out settling DatingSim events."
        )

    def _validate(self, runtime: BrowserRuntimeAdapter, target: dict[str, Any]) -> PhaseResult:
        state = ZustandHookAdapter(TestHookAdapter(runtime, self.hook_name)).get_state()
        if not state.get("isSetupComplete"):
            return PhaseResult("failure", "DatingSim setup is not complete.", {"state": state})
        expected_resources = target.get("expect", {}).get("resources", {})
        for key, value in expected_resources.items():
            if state["player"]["resources"].get(key) != value:
                return PhaseResult("failure", f"Resource mismatch for {key}.", {"expected": value, "actual": state["player"]["resources"].get(key)})
        expected_location = target.get("expect", {}).get("location")
        if expected_location and state["world"].get("location") != expected_location:
            return PhaseResult(
                "failure",
                "Location mismatch.",
                {"expected": expected_location, "actual": state["world"].get("location")},
            )
        expected_title = target.get("expect", {}).get("player_title")
        if expected_title and state["player"].get("title") != expected_title:
            return PhaseResult("failure", "Player title mismatch.", {"expected": expected_title, "actual": state["player"].get("title")})
        expected_relationships = target.get("expect", {}).get("relationships", {})
        for character_id, expected in expected_relationships.items():
            actual_relationship = state["player"].get("relationships", {}).get(character_id, {})
            for key, value in expected.items():
                if actual_relationship.get(key) != value:
                    return PhaseResult(
                        "failure",
                        f"Relationship mismatch for {character_id}.{key}.",
                        {"expected": value, "actual": actual_relationship.get(key)},
                    )
        expected_flags = target.get("expect", {}).get("global_flags", {})
        for key, value in expected_flags.items():
            if state["world"].get("globalFlags", {}).get(key) != value:
                return PhaseResult(
                    "failure",
                    f"Global flag mismatch for {key}.",
                    {"expected": value, "actual": state["world"].get("globalFlags", {}).get(key)},
                )
        ui_name = runtime.page.locator("body").inner_text(timeout=5_000)
        if target.get("player_name", "WarpTest") not in ui_name:
            return PhaseResult("failure", "Player name is not visible in UI.", {"player_name": target.get("player_name", "WarpTest")})
        data = {
            "state": state,
            "memory_persistence_diff": runtime.page.evaluate(
                """() => {
                  const state = window.__WARPTEST_LOVE__.getState();
                  let persisted = null;
                  try {
                    const raw = localStorage.getItem("love_emulator_save_v1");
                    persisted = raw ? JSON.parse(raw) : null;
                  } catch {
                    persisted = null;
                  }
                  const summarize = (value) => value ? {
                    isSetupComplete: !!value.isSetupComplete,
                    playerName: value.player?.name,
                    resources: value.player?.resources,
                    relationships: value.player?.relationships,
                    world: value.world
                  } : null;
                  const memory = summarize(state);
                  const persistence = summarize(persisted);
                  return {
                    persistence_key: "love_emulator_save_v1",
                    memory_present: !!state,
                    persistence_present: !!persisted,
                    matches: persistence ? JSON.stringify(memory) === JSON.stringify(persistence) : null,
                    memory,
                    persistence
                  };
                }"""
            ),
            "soft_lock_signal": runtime.page.evaluate(
                """() => {
                  const state = window.__WARPTEST_LOVE__.getState();
                  const actionableButtons = [...document.querySelectorAll("button")].filter((button) => !button.disabled).length;
                  return {
                    is_setup_complete: !!state.isSetupComplete,
                    active_dialogue_open: !!state.activeDialogue?.isOpen,
                    generating_event: !!state.isGeneratingEvent,
                    notification_error: state.notification?.type === "error",
                    current_event_open: !!state.currentEvent,
                    actionable_buttons: actionableButtons
                  };
                }"""
            ),
        }
        return PhaseResult("success", "DatingSim hook state, UI-visible state, and persistence signal validated.", data)

    def _run_actions(self, runtime: BrowserRuntimeAdapter, actions: list[dict[str, Any]]) -> PhaseResult:
        hook = TestHookAdapter(runtime, self.hook_name)
        zustand = ZustandHookAdapter(hook)
        for action in actions:
            if action.get("type") == "hook_action":
                zustand.perform_action(action["action_id"])
                runtime.page.wait_for_timeout(int(action.get("wait_ms", 500)))
            elif action.get("type") == "hook_call":
                hook.call(action["method"], action.get("args", []))
                runtime.page.wait_for_timeout(int(action.get("wait_ms", 250)))
            elif action.get("type") == "click":
                runtime.page.locator(action["selector"]).click(timeout=5_000)
            elif action.get("type") == "wait":
                runtime.page.wait_for_timeout(int(action.get("ms", 250)))
            elif action.get("type") == "settle_events":
                settled = self._settle_events(
                    runtime, timeout_s=float(action.get("timeout_s", 12))
                )
                if settled.status != "success":
                    return settled
            elif action.get("type") == "local_storage_set":
                # Writes a raw string directly into localStorage, bypassing
                # PersistenceManager.saveGame(). Used to probe loadGame()'s lack of
                # schema validation with crafted/partial/malformed save payloads
                # (analogous to the SpaceRPG corrupted-save defect class).
                runtime.page.evaluate(
                    "([key, value]) => localStorage.setItem(key, value)",
                    [action["key"], action["value"]],
                )
                runtime.page.wait_for_timeout(int(action.get("wait_ms", 100)))
            else:
                return PhaseResult("failure", f"Unsupported DatingSim action: {action}")
        return PhaseResult("success", f"Ran {len(actions)} action(s).")

    def _assert_oracles(self, runtime: BrowserRuntimeAdapter, assertions: list[dict[str, Any]]) -> PhaseResult:
        for assertion in assertions:
            if assertion.get("type") == "state_equals":
                actual = runtime.page.evaluate(
                    "(path) => path.split('.').reduce((value, key) => value?.[key], window.__WARPTEST_LOVE__.getState())",
                    assertion["path"],
                )
                if actual != assertion["value"]:
                    return PhaseResult("failure", f"State assertion failed for {assertion['path']}.", {"actual": actual, "expected": assertion["value"]})
            elif assertion.get("type") == "state_contains":
                actual = runtime.page.evaluate(
                    "(path) => path.split('.').reduce((value, key) => value?.[key], window.__WARPTEST_LOVE__.getState())",
                    assertion["path"],
                )
                expected = assertion["value"]
                if not isinstance(actual, list) or expected not in actual:
                    return PhaseResult(
                        "failure",
                        f"State array does not contain expected value for {assertion['path']}.",
                        {"actual": actual, "expected": expected},
                    )
            elif assertion.get("type") == "no_real_openai":
                status = runtime.page.evaluate("() => window.__WARPTEST_LOVE_AI__ || { mode: 'unknown' }")
                if status.get("realCalls", 0) > 0:
                    return PhaseResult("failure", "Real OpenAI calls were attempted.", {"ai": status})
            elif assertion.get("type") == "no_severe_console":
                continue
            else:
                return PhaseResult("failure", f"Unsupported DatingSim assertion: {assertion}")
        return PhaseResult("success", "All DatingSim oracles passed.")

    def supports_snapshot(self) -> bool:
        return True

    def capture_runtime_state(self, runtime: BrowserRuntimeAdapter) -> dict[str, Any] | None:
        hook_state = runtime.page.evaluate(
            """() => {
              const hook = window.__WARPTEST_LOVE__;
              if (!hook || typeof hook.getState !== 'function') return null;
              try { return JSON.parse(JSON.stringify(hook.getState())); }
              catch (e) { return null; }
            }"""
        )
        if not isinstance(hook_state, dict) or not hook_state:
            return None
        return {"kind": "hook_state", "hook": self.hook_name, "hook_state": hook_state}

    def inject_runtime_state(self, runtime: BrowserRuntimeAdapter, snapshot: dict[str, Any]) -> None:
        hook_state = snapshot.get("hook_state") if isinstance(snapshot, dict) else None
        if not isinstance(hook_state, dict) or not hook_state:
            raise ProfileError("DatingSim snapshot is missing hook_state.")
        storage = {"warptest-love-ai-mode": "mocked"}
        LocalStorageSaveAdapter(runtime).write_many(storage, [], self.inspect_result.storage_policy)
        runtime.block_ai_network("mocked")
        self._open_with_hook(runtime, "/?warptest=1&ai=mocked", timeout_ms=20_000)
        applied = runtime.page.evaluate(
            """(state) => {
              const hook = window.__WARPTEST_LOVE__;
              if (!hook || typeof hook.setState !== 'function') return false;
              hook.setState(state);
              return true;
            }""",
            hook_state,
        )
        if not applied:
            raise ProfileError("DatingSim hook does not expose setState for snapshot inject.")


class JynewProfile(BaseProfile):
    """Profile for jynew (金庸群侠传3D重制版), a Unity C# RPG with ES3 save system."""

    name = "JynewProfile"
    benchmark = "jynew"
    hook_name = "--warptest-checkpoint"
    default_strategy = "jynew_utility_save_validate"
    checkpoint_validity = "save-restorable"

    def run(self, spec: dict[str, Any], spec_path: Path, runtime: Any, mode: str = "jump_and_validate") -> RunReport:
        plan = self.strategy_plan(spec, mode)
        if plan.unsupported_reason:
            raise ProfileError(plan.unsupported_reason)
        report = self.base_report(spec_path, runtime, plan.strategy_id, plan.validity)
        start = time.perf_counter()
        report.hook_availability = {"available": True, "name": self.hook_name, "enabled_by": "unity_batch_mode"}
        report.runtime_access_methods = {
            "jynew_runtime_access_method": "Unity -batchmode -executeMethod Jyx2.WarptestCheckpoint.Run",
            "hook_name": self.hook_name,
        }
        result = runtime.run_checkpoint(spec, spec_path)
        checks = result.get("checks", []) if isinstance(result.get("checks"), list) else []
        report.checkpoint_synthesis = PhaseResult(
            "success",
            "Prepared jynew Unity checkpoint request from the WarpTest spec.",
            {"request_path": result.get("request_path")},
        )
        report.checkpoint_restoration = self._phase_from_checks(
            result,
            checks,
            prefixes=("target.save_loaded", "target.state_synthesized"),
            success_detail="Jynew loaded or synthesized the requested checkpoint state.",
            failure_detail="Jynew could not load/synthesize the requested checkpoint.",
        )
        if plan.validation_required:
            report.validation = self._phase_from_checks(
                result,
                checks,
                prefixes=("target.validate",),
                success_detail="Jynew target validation checks passed.",
                failure_detail="Jynew target validation failed.",
            )
            report.target_validated_before_smoke = report.validation.status == "success"
        else:
            report.validation = PhaseResult("skipped", "Ablation mode skipped target validation.")
            report.target_validated_before_smoke = False

        report.smoke_execution = self._phase_from_checks(
            result,
            checks,
            prefixes=("action[",),
            success_detail="Jynew smoke action checks passed.",
            failure_detail="Jynew smoke action checks failed.",
        )
        report.oracle_result = self._phase_from_checks(
            result,
            checks,
            prefixes=("assertion[",),
            success_detail="Jynew oracle checks passed.",
            failure_detail="Jynew oracle checks failed.",
        )
        report.logs.extend([path for path in [result.get("stdout_path"), result.get("stderr_path")] if isinstance(path, str)])
        report.post_checkpoint_interaction = {
            "status": report.smoke_execution.status,
            "detail": report.smoke_execution.detail,
            "data": report.smoke_execution.data,
            "validated_before_interaction": report.target_validated_before_smoke,
        }
        report.metrics = {
            "time_to_target_ms": int((time.perf_counter() - start) * 1000),
            "actions_replayed": 0,
            "jynew_utility_exit_code": result.get("exit_code"),
        }
        self._refresh_overall_status(report)
        return report

    @staticmethod
    def _phase_from_checks(
        result: dict[str, Any],
        checks: list[Any],
        *,
        prefixes: tuple[str, ...],
        success_detail: str,
        failure_detail: str,
    ) -> PhaseResult:
        selected = [
            check
            for check in checks
            if isinstance(check, dict)
            and any(str(check.get("name", "")).startswith(prefix) for prefix in prefixes)
        ]
        if not selected and result.get("status") == "failure":
            return PhaseResult("failure", failure_detail, result)
        failures = [check for check in selected if check.get("status") != "success"]
        if failures:
            return PhaseResult("failure", failure_detail, {"failures": failures, "report_path": result.get("report_path")})
        return PhaseResult("success", success_detail, {"checks": selected, "report_path": result.get("report_path")})


class Pal3Profile(BaseProfile):
    """Profile for Pal3.Unity (仙剑奇侠传三 PAL3 remake), a Unity C# RPG whose state lives in
    ServiceLocator-registered managers and whose saves are console-command text sequences."""

    name = "Pal3Profile"
    benchmark = "pal3"
    hook_name = "--warptest-checkpoint"
    default_strategy = "pal3_console_save_validate"
    checkpoint_validity = "save-restorable"

    def run(self, spec: dict[str, Any], spec_path: Path, runtime: Any, mode: str = "jump_and_validate") -> RunReport:
        plan = self.strategy_plan(spec, mode)
        if plan.unsupported_reason:
            raise ProfileError(plan.unsupported_reason)
        report = self.base_report(spec_path, runtime, plan.strategy_id, plan.validity)
        start = time.perf_counter()
        report.hook_availability = {"available": True, "name": self.hook_name, "enabled_by": "unity_batch_mode"}
        report.runtime_access_methods = {
            "pal3_runtime_access_method": "Unity -batchmode -executeMethod Pal3.Game.Command.WarptestCheckpoint.Run",
            "hook_name": self.hook_name,
        }
        result = runtime.run_checkpoint(spec, spec_path)
        checks = result.get("checks", []) if isinstance(result.get("checks"), list) else []
        report.checkpoint_synthesis = PhaseResult(
            "success",
            "Prepared pal3 Unity checkpoint request from the WarpTest spec.",
            {"request_path": result.get("request_path")},
        )
        report.checkpoint_restoration = self._phase_from_checks(
            result,
            checks,
            prefixes=("target.save_loaded", "target.state_synthesized"),
            success_detail="Pal3 loaded or synthesized the requested checkpoint state.",
            failure_detail="Pal3 could not load/synthesize the requested checkpoint.",
        )
        if plan.validation_required:
            report.validation = self._phase_from_checks(
                result,
                checks,
                prefixes=("target.validate",),
                success_detail="Pal3 target validation checks passed.",
                failure_detail="Pal3 target validation failed.",
            )
            report.target_validated_before_smoke = report.validation.status == "success"
        else:
            report.validation = PhaseResult("skipped", "Ablation mode skipped target validation.")
            report.target_validated_before_smoke = False

        report.smoke_execution = self._phase_from_checks(
            result,
            checks,
            prefixes=("action[",),
            success_detail="Pal3 smoke action checks passed.",
            failure_detail="Pal3 smoke action checks failed.",
        )
        report.oracle_result = self._phase_from_checks(
            result,
            checks,
            prefixes=("assertion[",),
            success_detail="Pal3 oracle checks passed.",
            failure_detail="Pal3 oracle checks failed.",
        )
        report.logs.extend([path for path in [result.get("stdout_path"), result.get("stderr_path")] if isinstance(path, str)])
        report.post_checkpoint_interaction = {
            "status": report.smoke_execution.status,
            "detail": report.smoke_execution.detail,
            "data": report.smoke_execution.data,
            "validated_before_interaction": report.target_validated_before_smoke,
        }
        report.metrics = {
            "time_to_target_ms": int((time.perf_counter() - start) * 1000),
            "actions_replayed": 0,
            "pal3_utility_exit_code": result.get("exit_code"),
        }
        self._refresh_overall_status(report)
        return report

    @staticmethod
    def _phase_from_checks(
        result: dict[str, Any],
        checks: list[Any],
        *,
        prefixes: tuple[str, ...],
        success_detail: str,
        failure_detail: str,
    ) -> PhaseResult:
        selected = [
            check
            for check in checks
            if isinstance(check, dict)
            and any(str(check.get("name", "")).startswith(prefix) for prefix in prefixes)
        ]
        if not selected and result.get("status") == "failure":
            return PhaseResult("failure", failure_detail, result)
        failures = [check for check in selected if check.get("status") != "success"]
        if failures:
            return PhaseResult("failure", failure_detail, {"failures": failures, "report_path": result.get("report_path")})
        return PhaseResult("success", success_detail, {"checks": selected, "report_path": result.get("report_path")})


class LuantiProfile(BaseProfile):
    """Profile for Luanti (formerly Minetest), a C++ voxel engine with Lua modding.

    Checkpoints are driven by a test-gated Lua worldmod running inside an embedded
    single-player session, so online-player state plus the voxel world, node-meta
    inventories, and mod storage are all mutable and persisted through the engine's
    own world format. The visual oracle frame is captured by the engine screenshot
    hook in the same rendered session (xvfb), analogous to OpenRA's gameplay capture.
    """

    name = "LuantiProfile"
    benchmark = "luanti"
    hook_name = "warptest"
    default_strategy = "luanti_worldmod_state_validate"
    checkpoint_validity = "save-restorable"

    def run(self, spec: dict[str, Any], spec_path: Path, runtime: Any, mode: str = "jump_and_validate") -> RunReport:
        plan = self.strategy_plan(spec, mode)
        if plan.unsupported_reason:
            raise ProfileError(plan.unsupported_reason)
        report = self.base_report(spec_path, runtime, plan.strategy_id, plan.validity)
        start = time.perf_counter()
        report.hook_availability = {"available": True, "name": self.hook_name, "enabled_by": "worldmod_config_gate"}
        report.runtime_access_methods = {
            "luanti_runtime_access_method": "Luanti embedded single-player (--go) WarpTest worldmod",
            "hook_name": self.hook_name,
        }
        result = runtime.run_checkpoint(spec, spec_path)
        checks = result.get("checks", []) if isinstance(result.get("checks"), list) else []
        report.checkpoint_synthesis = PhaseResult(
            "success",
            "Prepared luanti worldmod checkpoint request from the WarpTest spec.",
            {"request_path": result.get("request_path")},
        )
        report.checkpoint_restoration = self._phase_from_checks(
            result,
            checks,
            prefixes=("target.state_synthesized", "target.world_loaded"),
            success_detail="Luanti synthesized or reloaded the requested world/player checkpoint.",
            failure_detail="Luanti could not synthesize/reload the requested checkpoint.",
        )
        if plan.validation_required:
            report.validation = self._phase_from_checks(
                result,
                checks,
                prefixes=("target.validate",),
                success_detail="Luanti target validation checks passed.",
                failure_detail="Luanti target validation failed.",
            )
            report.target_validated_before_smoke = report.validation.status == "success"
        else:
            report.validation = PhaseResult("skipped", "Ablation mode skipped target validation.")
            report.target_validated_before_smoke = False

        report.smoke_execution = self._phase_from_checks(
            result,
            checks,
            prefixes=("action[",),
            success_detail="Luanti smoke action checks passed.",
            failure_detail="Luanti smoke action checks failed.",
        )
        report.oracle_result = self._phase_from_checks(
            result,
            checks,
            prefixes=("assertion[",),
            success_detail="Luanti oracle checks passed.",
            failure_detail="Luanti oracle checks failed.",
        )
        report.logs.extend([path for path in [result.get("stdout_path"), result.get("stderr_path")] if isinstance(path, str)])
        report.post_checkpoint_interaction = {
            "status": report.smoke_execution.status,
            "detail": report.smoke_execution.detail,
            "data": report.smoke_execution.data,
            "validated_before_interaction": report.target_validated_before_smoke,
        }
        report.metrics = {
            "time_to_target_ms": int((time.perf_counter() - start) * 1000),
            "actions_replayed": 0,
            "luanti_utility_exit_code": result.get("exit_code"),
        }
        self._refresh_overall_status(report)
        return report

    @staticmethod
    def _phase_from_checks(
        result: dict[str, Any],
        checks: list[Any],
        *,
        prefixes: tuple[str, ...],
        success_detail: str,
        failure_detail: str,
    ) -> PhaseResult:
        selected = [
            check
            for check in checks
            if isinstance(check, dict)
            and any(str(check.get("name", "")).startswith(prefix) for prefix in prefixes)
        ]
        if not selected and result.get("status") == "failure":
            return PhaseResult("failure", failure_detail, result)
        failures = [check for check in selected if check.get("status") != "success"]
        if failures:
            return PhaseResult("failure", failure_detail, {"failures": failures, "report_path": result.get("report_path")})
        return PhaseResult("success", success_detail, {"checks": selected, "report_path": result.get("report_path")})


def profile_for(inspect_result: InspectResult) -> BaseProfile:
    if inspect_result.profile == "Game2048Profile":
        return Game2048Profile(inspect_result)
    if inspect_result.profile == "SpaceRPGProfile":
        return SpaceRPGProfile(inspect_result)
    if inspect_result.profile == "DatingSimProfile":
        return DatingSimProfile(inspect_result)
    if inspect_result.profile == "OpenRAProfile":
        return OpenRAProfile(inspect_result)
    if inspect_result.profile == "JupyterLabProfile":
        return JupyterLabProfile(inspect_result)
    if inspect_result.profile == "LibreOfficeProfile":
        return LibreOfficeProfile(inspect_result)
    if inspect_result.profile == "BlenderProfile":
        return BlenderProfile(inspect_result)
    if inspect_result.profile == "JynewProfile":
        return JynewProfile(inspect_result)
    if inspect_result.profile == "Pal3Profile":
        return Pal3Profile(inspect_result)
    if inspect_result.profile == "LuantiProfile":
        return LuantiProfile(inspect_result)
    return BaseProfile(inspect_result)
