from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from warptest import p5_scale


class C1PreflightGateTests(unittest.TestCase):
    def test_c1_model_override_must_be_registered_manifest_subset(self) -> None:
        manifest = {
            "api": {"provider": "relay"},
            "models": ["model-a@relay", "model-b@relay"],
        }
        p5_scale._validate_c1_models(
            manifest, ["model-a@relay"], ["c1_state"]
        )
        with self.assertRaisesRegex(ValueError, "pre-registered"):
            p5_scale._validate_c1_models(
                manifest, ["unprobed@relay"], ["c1_state"]
            )

    def test_direct_formal_runner_cannot_claim_inactive_request_profile(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch(
            "warptest.p5_scale.run_context.git_commit", return_value="commit-a"
        ):
            root = Path(td)
            orchestrator = self._orchestrator(root, root / "missing.json")
            orchestrator.model_request_profiles = {"model-a": "thinking_disabled"}
            with self.assertRaisesRegex(RuntimeError, "profiles are not active"):
                orchestrator._require_preflight_gate()

    def _orchestrator(self, root: Path, artifact: Path) -> p5_scale.P5Orchestrator:
        manifest = {
            "name": "preflight-test",
            "api": {
                "provider": "relay",
                "key_env": "RELAY_API_KEY",
                "base_url": "https://relay.example/v1",
            },
            "models": ["model-a@relay"],
            "visual_bridge": {
                "observer_model": "observer@relay",
                "grounder_model": "grounder@relay",
            },
            "visual_judge": {"model": "judge@relay"},
            "preflight": {"required": True, "max_age_hours": 24},
            "arms": {"c1_state": {}},
        }
        return p5_scale.P5Orchestrator(
            root,
            manifest,
            run_dir=root / "run",
            models=["model-a@relay"],
            seeds=1,
            arms=["c1_state"],
            preflight_artifact=artifact,
        )

    @staticmethod
    def _result(
        orchestrator: p5_scale.P5Orchestrator,
        name: str,
        model: str,
        modality: str,
    ) -> dict:
        usage = {
            "prompt_tokens": 4,
            "completion_tokens": 3,
            "reasoning_tokens": 2,
            "reasoning_tokens_observed": 2,
            "reasoning_tokens_lower_bound": 2,
            "reasoning_tokens_upper_bound": 2,
            "reasoning_detail_complete": True,
            "reasoning_detail_missing_calls": 0,
            "reasoning_detail_unknown_completion_tokens": 0,
            "reasoning_detail_expected_completion_tokens": 3,
            "reasoning_detail_unknown_fraction": 0.0,
            "reasoning_detail_tolerance_fraction": 0.10,
            "reasoning_detail_within_tolerance": True,
            "cached_prompt_tokens": 1,
            "total_tokens": 7,
        }
        expected_temperature = (
            orchestrator.temperature if name.startswith("decision:") else None
        )
        return {
            "name": name,
            "model": model,
            "provider": "relay",
            "modality": modality,
            "success": True,
            "request_contract_passed": True,
            "reasoning_content_observed": False,
            "reasoning_token_anomaly": False,
            "attempts": 1,
            "finish_reason": "stop",
            "usage": usage,
            "request_profile": orchestrator._model_request_provenance(model),
            "temperature_configured": expected_temperature,
            "temperature_fallback_triggered": False,
            "temperature_omitted_after_rejection": False,
            "attempt_records": [
                {
                    "ordinal": 1,
                    "max_tokens": 8192,
                    "request_succeeded": True,
                    "content_nonempty": True,
                    "finish_reason": "stop",
                    "reasoning_observed": usage["reasoning_tokens"] > 0,
                    "reasoning_content_observed": False,
                    "reasoning_token_anomaly": False,
                    "request_contract_passed": True,
                    "reasoning_exhausted": False,
                    "temperature_configured": expected_temperature,
                    "temperature_requested": expected_temperature,
                    "temperature_fallback_triggered": False,
                    "temperature_omitted_after_rejection": False,
                    "usage": dict(usage),
                }
            ],
        }

    def _write_artifact(
        self,
        path: Path,
        orchestrator: p5_scale.P5Orchestrator,
    ) -> dict:
        results = [
            self._result(orchestrator, "decision:model-a", "model-a", "vision"),
            self._result(orchestrator, "synthesis:model-a", "model-a", "text"),
            self._result(orchestrator, "visual_observer", "observer", "vision"),
            self._result(orchestrator, "agent_s_grounder", "grounder", "vision"),
            self._result(orchestrator, "visual_judge", "judge", "vision"),
        ]
        payload = {
            "schema_version": 4,
            "complete": True,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "probe_count": len(results),
            "initial_max_tokens": 8192,
            "retry_max_tokens": 16384,
            "retry_rule": (
                "one_retry_only_if_empty_content_and_explicit_output_limit"
            ),
            "temperature_contract": {
                "configured": orchestrator.temperature,
                "applies_to": "decision_probes_only",
                "fallback": (
                    "omit_only_after_explicit_http_400_temperature_rejection"
                ),
            },
            "git_commit": orchestrator.git_commit,
            "manifest_hash": orchestrator.manifest_hash,
            "harness_hash": "harness-a",
            "api_provider": orchestrator._api_provider_provenance(),
            "model_request_profiles": orchestrator.model_request_profiles,
            "model_request_profiles_sha256": (
                orchestrator.model_request_profiles_sha256
            ),
            "usage_accounting": orchestrator.usage_accounting,
            "results": results,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def test_complete_artifact_is_bound_and_then_rehashed(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ,
            {"RELAY_END_USER": "tester"},
            clear=False,
        ), patch("warptest.p5_scale.run_context.git_commit", return_value="commit-a"), patch(
            "warptest.p5_scale.c1_harness_hash", return_value="harness-a"
        ):
            root = Path(td)
            artifact = root / "preflight.json"
            orchestrator = self._orchestrator(root, artifact)
            self._write_artifact(artifact, orchestrator)

            orchestrator._require_preflight_gate()

            self.assertTrue(orchestrator.preflight_metadata["complete"])
            self.assertEqual(
                orchestrator.preflight_metadata["artifact_sha256"],
                hashlib.sha256(artifact.read_bytes()).hexdigest(),
            )
            artifact.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "changed during the run"):
                orchestrator._require_preflight_gate()

    def test_route_and_usage_mismatches_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ,
            {"RELAY_END_USER": "tester"},
            clear=False,
        ), patch("warptest.p5_scale.run_context.git_commit", return_value="commit-a"), patch(
            "warptest.p5_scale.c1_harness_hash", return_value="harness-a"
        ):
            root = Path(td)
            artifact = root / "preflight.json"
            orchestrator = self._orchestrator(root, artifact)
            payload = self._write_artifact(artifact, orchestrator)
            payload["results"][0]["model"] = "wrong-model"
            artifact.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                orchestrator._require_preflight_gate()

            orchestrator = self._orchestrator(root, artifact)
            payload = self._write_artifact(artifact, orchestrator)
            payload["results"][0]["usage"]["total_tokens"] = 99
            artifact.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "invalid token usage"):
                orchestrator._require_preflight_gate()

            orchestrator = self._orchestrator(root, artifact)
            payload = self._write_artifact(artifact, orchestrator)
            payload["results"][0]["attempt_records"][0]["max_tokens"] = 64
            artifact.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "attempt protocol"):
                orchestrator._require_preflight_gate()

            orchestrator = self._orchestrator(root, artifact)
            payload = self._write_artifact(artifact, orchestrator)
            payload["generated_at_utc"] = (
                datetime.now(timezone.utc) - timedelta(days=2)
            ).isoformat()
            artifact.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "freshness window"):
                orchestrator._require_preflight_gate()

    def test_resume_rehashes_completed_report_before_skipping(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            report = root / "state-loop.json"
            report.write_text('{"complete": true, "value": 1}', encoding="utf-8")
            artifact = root / "unused-preflight.json"
            with patch(
                "warptest.p5_scale.run_context.git_commit", return_value="commit-a"
            ):
                orchestrator = self._orchestrator(root, artifact)
            orchestrator.resume = True
            orchestrator.index["c1_state"] = [
                {
                    "model": "model-a@relay",
                    "seed": 0,
                    "status": "ok",
                    "report": str(report),
                    "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                }
            ]
            self.assertTrue(
                orchestrator._completed(
                    "c1_state", model="model-a@relay", seed=0
                )
            )
            report.write_text('{"complete": true, "value": 2}', encoding="utf-8")
            self.assertFalse(
                orchestrator._completed(
                    "c1_state", model="model-a@relay", seed=0
                )
            )


if __name__ == "__main__":
    unittest.main()
