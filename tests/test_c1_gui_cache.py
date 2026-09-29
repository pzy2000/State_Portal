from __future__ import annotations

import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from warptest import gui_agent
from warptest.gui_agent import PhaseResult


class LuantiGoldenCacheCallsiteTests(unittest.TestCase):
    @staticmethod
    def _task() -> SimpleNamespace:
        return SimpleNamespace(
            id="luanti-set-node",
            benchmark="luanti",
            state_description="a node is ready",
            preconditions="a node is ready",
            reference_steps="set the node",
            goal_description="the node is set",
            horizon_class="long",
            issue=SimpleNamespace(id="luanti-set-node", benchmark="luanti"),
            oracle_spec="oracle.yaml",
        )

    def test_cache_hit_starts_only_the_agent_luanti_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            initial = root / "initial.png"
            before = root / "before.png"
            initial.write_bytes(b"initial")
            before.write_bytes(b"before")
            goldens = SimpleNamespace(
                before=str(root / "gold-before.png"),
                after=str(root / "gold-after.png"),
                fingerprint="luanti-goldens",
                cache_hit=True,
                cacheable=True,
                consistency={"source": "verified_cache"},
                quality={},
                after_description=None,
                after_rubric=None,
            )
            session = MagicMock()
            session_cm = MagicMock()
            session_cm.__enter__.return_value = session
            runtime = MagicMock()
            runtime.calibration_metadata.return_value = {}
            runtime.screenshot.return_value = str(initial)
            session_factory = MagicMock(return_value=session_cm)

            with (
                patch.object(gui_agent, "resolve_task_repo", return_value=root),
                patch.object(
                    gui_agent,
                    "load_spec",
                    return_value={"checkpoint": {}, "assertions": []},
                ),
                patch.object(
                    gui_agent,
                    "audit_scripted_reference_replay",
                    return_value={"status": "success", "reference_reachable": True},
                ),
                patch.object(gui_agent, "native_runtime_lock", return_value=nullcontext()),
                patch.object(
                    gui_agent, "lookup_cached_oracle_goldens", return_value=goldens
                ),
                patch.object(gui_agent, "LuantiWarmSession", session_factory),
                patch.object(gui_agent, "LuantiGuiRuntime", return_value=runtime),
                patch.object(
                    gui_agent, "_record_luanti_semantic_evidence", return_value=True
                ),
                patch.object(gui_agent, "_record_luanti_seed_evidence"),
                patch.object(
                    gui_agent,
                    "_run_phase",
                    return_value=PhaseResult(
                        reached=False,
                        steps=1,
                        screenshot=str(before),
                        elapsed_ms=3,
                    ),
                ),
                patch.object(
                    gui_agent,
                    "_luanti_semantic_grade",
                    return_value=(False, "validate_failed", {}),
                ),
            ):
                result = gui_agent._run_luanti_agent_task(
                    root,
                    self._task(),
                    model="model",
                    base_url="https://example.invalid/v1",
                    api_key="key",
                    api_key_env="KEY",
                    synth_model="model",
                    max_explore_steps=2,
                    temperature=0,
                    out_dir=root / "report",
                    max_steps=4,
                    start_mode="from_start",
                    policy_kind="gui_agent",
                    policy_client=MagicMock(),
                    observer=None,
                    seed=0,
                )

        self.assertEqual(session_factory.call_count, 1)
        self.assertTrue(result["oracle_goldens"]["cache_hit"])

    def test_cache_read_error_is_structured_before_luanti_boot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            session_factory = MagicMock()
            with (
                patch.object(gui_agent, "resolve_task_repo", return_value=root),
                patch.object(gui_agent, "load_spec", return_value={"checkpoint": {}}),
                patch.object(
                    gui_agent,
                    "audit_scripted_reference_replay",
                    return_value={"status": "success", "reference_reachable": True},
                ),
                patch.object(gui_agent, "native_runtime_lock", return_value=nullcontext()),
                patch.object(
                    gui_agent,
                    "lookup_cached_oracle_goldens",
                    side_effect=OSError("cache unreadable"),
                ),
                patch.object(gui_agent, "LuantiWarmSession", session_factory),
            ):
                result = gui_agent._run_luanti_agent_task(
                    root,
                    self._task(),
                    model="model",
                    base_url="https://example.invalid/v1",
                    api_key="key",
                    api_key_env="KEY",
                    synth_model="model",
                    max_explore_steps=2,
                    temperature=0,
                    out_dir=root / "report",
                    max_steps=4,
                    start_mode="from_start",
                    policy_kind="gui_agent",
                    policy_client=MagicMock(),
                    observer=None,
                    seed=0,
                )

        session_factory.assert_not_called()
        self.assertTrue(result["incomplete"])
        self.assertIn("Luanti oracle golden generation failed", result["reason"])


if __name__ == "__main__":
    unittest.main()
