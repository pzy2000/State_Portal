from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from warptest import cost, p5_scale
from warptest.c1_experiment import (
    C1CredentialError,
    DECISION_MODELS,
    DEFAULT_PREFLIGHT_PROBES,
    PreflightProbe,
    relay_environment,
    calibration_grounder_model,
    resolve_grounder_model,
    run_c1_preflight,
    preflight_probes_for_manifest,
)
from warptest.providers import resolve_model
from warptest.model_request import model_request_profiles_environment


def _credential_file(
    root: Path,
    *,
    secret: str = "local-secret",
    end_user: str | None = "c1-researcher",
) -> Path:
    path = root / "credentials.py"
    lines = [
        "import definitely_not_importable",
        f"SECRET = {secret!r}",
        "BASE = 'https://relay.example/v1/'",
    ]
    if end_user is not None:
        lines.append(f"END_USER = {end_user!r}")
    lines.append("raise RuntimeError('this source must never execute')")
    path.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )
    return path


class _NoReasoningReadMessage:
    content = "ack"

    @property
    def reasoning_content(self):  # pragma: no cover - access would fail the test
        raise AssertionError("reasoning_content must never be read")


def _response(
    content: str | None = "ack",
    *,
    finish_reason: str = "stop",
    prompt: int = 3,
    completion: int = 5,
    reasoning: int = 2,
):
    message = _NoReasoningReadMessage() if content == "ack" else SimpleNamespace(content=content)
    usage = SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage,
    )


class _FakeCompletions:
    def __init__(self, responses=None, error: Exception | None = None) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return _response()


class _FakeClient:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.chat = SimpleNamespace(completions=completions)


class CredentialTests(unittest.TestCase):
    def test_ast_loader_never_executes_and_restores_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = _credential_file(Path(temp))
            with patch.dict(
                os.environ,
                {
                    "RELAY_API_KEY": "old-key",
                    "RELAY_END_USER": "old-user",
                },
                clear=False,
            ):
                os.environ.pop("RELAY_BASE_URL", None)
                with relay_environment(path) as metadata:
                    self.assertEqual(os.environ["RELAY_API_KEY"], "local-secret")
                    self.assertEqual(os.environ["RELAY_BASE_URL"], "https://relay.example/v1")
                    self.assertEqual(os.environ["RELAY_END_USER"], "c1-researcher")
                    self.assertNotIn("local-secret", repr(metadata))
                self.assertEqual(os.environ["RELAY_API_KEY"], "old-key")
                self.assertNotIn("RELAY_BASE_URL", os.environ)
                self.assertEqual(os.environ["RELAY_END_USER"], "old-user")

    def test_nonliteral_secret_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "credentials.py"
            path.write_text(
                "SECRET = read_secret()\nBASE = 'https://example/v1'\nEND_USER = 'u'\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(C1CredentialError, "SECRET must be a string literal"):
                with relay_environment(path):
                    self.fail("invalid credential source must not activate an environment")

    def test_duplicate_and_missing_required_assignments_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            duplicate = root / "duplicate.py"
            duplicate.write_text(
                "SECRET='a'\nSECRET='b'\nBASE='https://x'\nEND_USER='u'\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(C1CredentialError, "Duplicate SECRET"):
                with relay_environment(duplicate):
                    pass
            missing = root / "missing.py"
            missing.write_text("SECRET='a'\n", encoding="utf-8")
            with self.assertRaisesRegex(C1CredentialError, "BASE"):
                with relay_environment(missing):
                    pass

    def test_missing_end_user_omits_header_and_restores_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = _credential_file(Path(temp), end_user=None)
            with patch.dict(
                os.environ,
                {"RELAY_END_USER": "old-user"},
                clear=False,
            ):
                with relay_environment(path) as metadata:
                    self.assertEqual(os.environ["RELAY_END_USER"], "")
                    self.assertEqual(metadata.end_user, "")
                    _, provider = resolve_model(
                        "gpt-5.6-terra@relay",
                        default_base_url="https://default.invalid/v1",
                        default_api_key_env="DEFAULT_API_KEY",
                    )
                    self.assertEqual(provider.extra_headers, {})
                self.assertEqual(os.environ["RELAY_END_USER"], "old-user")

    def test_base_url_cannot_smuggle_credentials_or_query_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            for index, base_url in enumerate(
                (
                    "https://token@example.invalid/v1",
                    "https://example.invalid/v1?api_key=secret",
                    "file:///tmp/provider",
                )
            ):
                path = Path(temp) / f"credentials-{index}.py"
                path.write_text(
                    f"SECRET='a'\nBASE={base_url!r}\nEND_USER='u'\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(C1CredentialError, "without credentials"):
                    with relay_environment(path):
                        pass


class PreflightTests(unittest.TestCase):
    def test_reasoning_detail_gap_warns_but_does_not_fail_preflight(self) -> None:
        missing_detail = _response(
            content=None,
            finish_reason="length",
            prompt=3,
            completion=4,
            reasoning=0,
        )
        missing_detail.usage.completion_tokens_details = SimpleNamespace()
        completions = _FakeCompletions(
            responses=[missing_detail, _response(content="ack")]
        )
        with tempfile.TemporaryDirectory() as temp:
            report = run_c1_preflight(
                _credential_file(Path(temp)),
                probes=(
                    PreflightProbe(
                        "decision:test", "test@relay", "vision", "decision"
                    ),
                ),
                client_factory=lambda **_: _FakeClient(completions),
            )

        self.assertTrue(report["complete"])
        self.assertTrue(report["results"][0]["success"])
        usage = report["results"][0]["usage"]
        self.assertFalse(usage["reasoning_detail_complete"])
        self.assertEqual(usage["reasoning_detail_missing_calls"], 1)
        self.assertIsNone(usage["reasoning_tokens"])
        self.assertEqual(usage["reasoning_tokens_lower_bound"], 2)
        self.assertEqual(usage["reasoning_tokens_upper_bound"], 6)
        self.assertFalse(usage["reasoning_detail_within_tolerance"])
        first_attempt_usage = report["results"][0]["attempt_records"][0]["usage"]
        self.assertEqual(first_attempt_usage["reasoning_tokens_observed"], 0)
        self.assertEqual(first_attempt_usage["reasoning_tokens_upper_bound"], 4)
        self.assertTrue(
            p5_scale.P5Orchestrator._validate_preflight_attempts(
                report["results"][0],
                initial_max_tokens=8192,
                retry_max_tokens=16384,
                expected_temperature=None,
            )
        )

    def test_default_matrix_is_three_decision_three_synthesis_and_visual_roles(self) -> None:
        self.assertEqual(
            DECISION_MODELS,
            (
                "gpt-5.6-terra@relay",
                "gemini-3.5-flash@relay",
                "kimi-k2.6@relay",
            ),
        )
        decision_probe_count = len(DECISION_MODELS)
        self.assertEqual(
            tuple(
                probe.model
                for probe in DEFAULT_PREFLIGHT_PROBES[:decision_probe_count]
            ),
            DECISION_MODELS,
        )
        synthesis = DEFAULT_PREFLIGHT_PROBES[
            decision_probe_count : decision_probe_count * 2
        ]
        self.assertEqual(
            tuple(probe.name for probe in synthesis),
            tuple(
                f"synthesis:{model.split('@', 1)[0]}"
                for model in DECISION_MODELS
            ),
        )
        self.assertTrue(all(probe.modality == "text" for probe in synthesis))
        self.assertEqual(
            tuple(
                probe.name
                for probe in DEFAULT_PREFLIGHT_PROBES[decision_probe_count * 2 :]
            ),
            ("visual_observer", "agent_s_grounder", "visual_judge"),
        )
        self.assertTrue(
            all(
                probe.modality == "vision"
                for probe in (
                    DEFAULT_PREFLIGHT_PROBES[:decision_probe_count]
                    + DEFAULT_PREFLIGHT_PROBES[decision_probe_count * 2 :]
                )
            )
        )

    def test_manifest_matrix_uses_selected_decision_models_and_visual_roles(self) -> None:
        probes = preflight_probes_for_manifest(
            {
                "models": ["gpt-5.6-terra@relay"],
                "visual_bridge": {
                    "observer_model": "observer@relay",
                    "grounder_model": "grounder@relay",
                },
                "visual_judge_model": "judge@relay",
            }
        )
        self.assertEqual(
            [(probe.name, probe.model) for probe in probes],
            [
                ("decision:gpt-5.6-terra", "gpt-5.6-terra@relay"),
                ("synthesis:gpt-5.6-terra", "gpt-5.6-terra@relay"),
                ("visual_observer", "observer@relay"),
                ("agent_s_grounder", "grounder@relay"),
                ("visual_judge", "judge@relay"),
            ],
        )
        self.assertEqual(
            [probe.modality for probe in probes],
            ["vision", "text", "vision", "vision", "vision"],
        )

    def test_manifest_matrix_can_ground_with_each_decision_model(self) -> None:
        models = [
            "gpt-5.6-terra@relay",
            "gemini-3.5-flash@relay",
            "kimi-k2.6@relay",
        ]
        probes = preflight_probes_for_manifest(
            {
                "models": models,
                "visual_bridge": {
                    "observer_model": "claude-sonnet-5@relay",
                    "grounder_model": "match_decision_model",
                },
                "visual_judge_model": "qwen3-vl-flash@relay",
            }
        )
        self.assertEqual(
            [(probe.name, probe.model) for probe in probes],
            [
                ("decision:gpt-5.6-terra", models[0]),
                ("decision:gemini-3.5-flash", models[1]),
                ("decision:kimi-k2.6", models[2]),
                ("synthesis:gpt-5.6-terra", models[0]),
                ("synthesis:gemini-3.5-flash", models[1]),
                ("synthesis:kimi-k2.6", models[2]),
                ("visual_observer", "claude-sonnet-5@relay"),
                ("agent_s_grounder:gpt-5.6-terra", models[0]),
                ("agent_s_grounder:gemini-3.5-flash", models[1]),
                ("agent_s_grounder:kimi-k2.6", models[2]),
                ("visual_judge", "qwen3-vl-flash@relay"),
            ],
        )

    def test_source_transition_v2_adds_one_text_probe_per_decision_model(self) -> None:
        models = [
            "gpt-5.6-terra@relay",
            "gemini-3.5-flash@relay",
            "kimi-k2.6@relay",
        ]
        probes = preflight_probes_for_manifest(
            {
                "models": models,
                "arms": {
                    "c1_state_minimal_agent": {
                        "transition_action_source": "source_agent_v2"
                    }
                },
                "visual_bridge": {
                    "observer_model": "claude-sonnet-5@relay",
                    "grounder_model": "match_decision_model",
                },
                "visual_judge_model": "qwen3-vl-flash@relay",
            }
        )
        source = [
            probe for probe in probes if probe.name.startswith("source_transition:")
        ]
        self.assertEqual(len(probes), 14)
        self.assertEqual([probe.model for probe in source], models)
        self.assertTrue(all(probe.modality == "text" for probe in source))
        self.assertTrue(
            all(probe.cost_kind == "preflight_source_transition" for probe in source)
        )
        self.assertEqual(
            {
                model: resolve_grounder_model(
                    "match_decision_model",
                    observer_model="claude-sonnet-5@relay",
                    decision_model=model,
                )
                for model in models
            },
            {model: model for model in models},
        )
        self.assertEqual(
            calibration_grounder_model(
                "match_decision_model",
                observer_model="claude-sonnet-5@relay",
            ),
            "claude-sonnet-5@relay",
        )

    def test_formal_preflight_can_follow_execution_model_override(self) -> None:
        completions = _FakeCompletions()
        client = _FakeClient(completions)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            credentials = _credential_file(root, end_user=None)
            manifest_path = root / "manifest.yaml"
            manifest_path.write_text(
                "name: c1\napi:\n  provider: relay\n  provider_timeout_s: 900.0\n"
                "  temperature: 1.0\n"
                "models: [default-model@relay]\n"
                "model_request_profiles:\n  gpt-5.6-terra: reasoning_medium\n"
                "visual_bridge:\n  observer_model: observer@relay\n"
                "  grounder_model: grounder@relay\n"
                "visual_judge_model: judge@relay\narms: {}\n",
                encoding="utf-8",
            )
            with (
                patch("warptest.c1_experiment.c1_harness_hash", return_value="harness-a"),
                patch("warptest.c1_experiment.run_context.git_commit", return_value="commit-a"),
                patch("warptest.c1_experiment.c1_application_revisions", return_value={}),
                patch("warptest.c1_experiment.validate_c1_environment", return_value={"status": "matched"}),
            ):
                report = run_c1_preflight(
                    credentials,
                    decision_models=("gpt-5.6-terra@relay",),
                    client_factory=lambda **_: client,
                    root=root,
                    manifest_path=manifest_path,
                )

        self.assertTrue(report["complete"])
        self.assertEqual(report["probe_count"], 5)
        self.assertIsNone(report["api_provider"]["end_user_sha256"])
        self.assertEqual(
            [result["model"] for result in report["results"]],
            ["gpt-5.6-terra", "gpt-5.6-terra", "observer", "grounder", "judge"],
        )
        self.assertEqual(completions.calls[0]["reasoning_effort"], "medium")
        self.assertEqual(completions.calls[0]["temperature"], 1.0)
        self.assertEqual(
            completions.calls[0]["messages"][0]["content"][1]["type"],
            "image_url",
        )
        self.assertTrue(
            all("temperature" not in call for call in completions.calls[1:])
        )
        self.assertEqual(report["temperature_contract"]["configured"], 1.0)

    def test_formal_preflight_temperature_fallback_requires_explicit_400(self) -> None:
        class TemperatureRejected(RuntimeError):
            status_code = 400

            def __init__(self) -> None:
                super().__init__("Unsupported value: temperature")

        completions = _FakeCompletions(
            responses=[TemperatureRejected(), _response(), _response(), _response(), _response()]
        )
        client = _FakeClient(completions)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            credentials = _credential_file(root)
            manifest_path = root / "manifest.yaml"
            manifest_path.write_text(
                "name: c1\napi:\n  provider: relay\n  provider_timeout_s: 900.0\n"
                "  temperature: 1.0\nmodels: [gpt-5.6-terra@relay]\n"
                "model_request_profiles:\n  gpt-5.6-terra: reasoning_medium\n"
                "visual_bridge:\n  observer_model: observer@relay\n"
                "  grounder_model: grounder@relay\n"
                "visual_judge_model: judge@relay\narms: {}\n",
                encoding="utf-8",
            )
            with (
                patch("warptest.c1_experiment.c1_harness_hash", return_value="harness-a"),
                patch("warptest.c1_experiment.run_context.git_commit", return_value="commit-a"),
                patch("warptest.c1_experiment.c1_application_revisions", return_value={}),
                patch("warptest.c1_experiment.validate_c1_environment", return_value={"status": "matched"}),
            ):
                report = run_c1_preflight(
                    credentials,
                    client_factory=lambda **_: client,
                    root=root,
                    manifest_path=manifest_path,
                )

        self.assertTrue(report["complete"])
        self.assertEqual(len(completions.calls), 6)
        self.assertEqual(completions.calls[0]["temperature"], 1.0)
        self.assertNotIn("temperature", completions.calls[1])
        self.assertEqual(completions.calls[1]["reasoning_effort"], "medium")
        decision = report["results"][0]
        self.assertEqual(decision["attempts"], 2)
        self.assertTrue(decision["temperature_fallback_triggered"])
        self.assertTrue(decision["temperature_omitted_after_rejection"])
        self.assertEqual(decision["attempt_records"][0]["status_code"], 400)

    def test_reasoning_high_preflight_never_downgrades_after_rejection(self) -> None:
        class HighRejected(RuntimeError):
            status_code = 400

            def __init__(self) -> None:
                super().__init__("reasoning_effort high is unsupported")

        completions = _FakeCompletions(
            responses=[HighRejected(), _response(), _response(), _response()]
        )
        client = _FakeClient(completions)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            credentials = _credential_file(root)
            manifest_path = root / "manifest.yaml"
            manifest_path.write_text(
                "name: c1\napi:\n  provider: relay\n  provider_timeout_s: 900.0\n"
                "models: [kimi-k3@relay]\n"
                "model_request_profiles:\n  kimi-k3: reasoning_high\n"
                "visual_bridge:\n  observer_model: observer@relay\n"
                "  grounder_model: grounder@relay\n"
                "visual_judge_model: judge@relay\narms: {}\n",
                encoding="utf-8",
            )
            with (
                patch("warptest.c1_experiment.c1_harness_hash", return_value="harness-a"),
                patch("warptest.c1_experiment.run_context.git_commit", return_value="commit-a"),
                patch("warptest.c1_experiment.c1_application_revisions", return_value={}),
                patch("warptest.c1_experiment.validate_c1_environment", return_value={"status": "matched"}),
            ):
                report = run_c1_preflight(
                    credentials,
                    client_factory=lambda **_: client,
                    root=root,
                    manifest_path=manifest_path,
                )

        self.assertFalse(report["complete"])
        decision = report["results"][0]
        self.assertFalse(decision["success"])
        self.assertEqual(decision["request_profile"]["profile"], "reasoning_high")
        self.assertEqual(completions.calls[0]["reasoning_effort"], "high")
        self.assertTrue(
            all(call.get("reasoning_effort") != "medium" for call in completions.calls)
        )

    def test_formal_preflight_stamps_content_provenance_without_credentials(self) -> None:
        completions = _FakeCompletions()
        client = _FakeClient(completions)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _credential_file(root, secret="never-serialize-this")
            manifest_path = root / "manifest.yaml"
            manifest_path.write_text(
                "name: c1\napi:\n  provider: relay\n  provider_timeout_s: 900.0\n"
                "model_request_profiles:\n"
                "  kimi-k2.6: thinking_disabled\n"
                "arms: {}\n",
                encoding="utf-8",
            )
            with (
                patch("warptest.c1_experiment.c1_harness_hash", return_value="harness-a"),
                patch("warptest.c1_experiment.run_context.git_commit", return_value="commit-a"),
                patch("warptest.c1_experiment.c1_application_revisions", return_value={}),
                patch("warptest.c1_experiment.validate_c1_environment", return_value={"status": "matched"}),
            ):
                report = run_c1_preflight(
                    path,
                    client_factory=lambda **_: client,
                    root=root,
                    manifest_path=manifest_path,
                )

        self.assertEqual(report["schema_version"], 4)
        self.assertEqual(report["git_commit"], "commit-a")
        self.assertEqual(report["harness_hash"], "harness-a")
        self.assertEqual(report["api_provider"]["name"], "relay")
        kimi_index = next(
            index
            for index, result in enumerate(report["results"])
            if result["model"] == "kimi-k2.6"
        )
        self.assertEqual(
            completions.calls[kimi_index]["extra_body"],
            {"thinking": {"type": "disabled"}},
        )
        self.assertNotIn("extra_body", completions.calls[0])
        self.assertEqual(
            report["results"][kimi_index]["request_profile"]["profile"],
            "thinking_disabled",
        )
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn("never-serialize-this", serialized)
        self.assertNotIn("c1-researcher", serialized)

    def test_explicit_preflight_omits_end_user_and_returns_content_free_usage(self) -> None:
        completions = _FakeCompletions()
        client = _FakeClient(completions)
        factory_calls: list[dict] = []

        def factory(**kwargs):
            factory_calls.append(kwargs)
            return client

        with tempfile.TemporaryDirectory() as temp:
            path = _credential_file(
                Path(temp),
                secret="do-not-report-this-key",
                end_user=None,
            )
            with cost.scoped_meter() as meter:
                report = run_c1_preflight(path, client_factory=factory)

        self.assertTrue(report["complete"])
        self.assertEqual(report["probe_count"], 9)
        self.assertEqual(len(completions.calls), 9)
        self.assertEqual(len(factory_calls), 1)
        self.assertEqual(factory_calls[0]["api_key"], "do-not-report-this-key")
        self.assertEqual(factory_calls[0]["base_url"], "https://relay.example/v1")
        self.assertNotIn("default_headers", factory_calls[0])
        self.assertGreaterEqual(factory_calls[0]["timeout"], 300)
        self.assertTrue(all(call["max_tokens"] == 8192 for call in completions.calls))
        self.assertEqual(
            [call["model"] for call in completions.calls[: len(DECISION_MODELS)]],
            [model.split("@", 1)[0] for model in DECISION_MODELS],
        )
        self.assertIsInstance(completions.calls[0]["messages"][0]["content"], list)
        self.assertEqual(
            completions.calls[0]["messages"][0]["content"][1]["type"],
            "image_url",
        )
        self.assertIsInstance(completions.calls[-1]["messages"][0]["content"], list)
        self.assertEqual(report["results"][0]["usage"]["reasoning_tokens"], 2)
        self.assertEqual(len(meter.records), 9)
        self.assertEqual(meter.records[0].reasoning_tokens, 2)
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn("do-not-report-this-key", serialized)
        def string_values(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from string_values(item)
            elif isinstance(value, list):
                for item in value:
                    yield from string_values(item)

        self.assertNotIn("ack", set(string_values(report)))
        self.assertNotIn('"reasoning_content":', serialized)
        self.assertTrue(
            all(
                result["reasoning_content_observed"] is False
                for result in report["results"]
            )
        )

    def test_disabled_profile_allows_reported_reasoning_tokens_as_anomaly(self) -> None:
        completions = _FakeCompletions(
            responses=[_response("ack", reasoning=3)]
        )
        client = _FakeClient(completions)
        probe = PreflightProbe(
            "decision:kimi-k2.6",
            "kimi-k2.6@relay",
            "text",
            "preflight_decision",
        )
        with tempfile.TemporaryDirectory() as temp:
            path = _credential_file(Path(temp))
            with model_request_profiles_environment(
                {"kimi-k2.6": "thinking_disabled"}
            ):
                report = run_c1_preflight(
                    path,
                    probes=(probe,),
                    client_factory=lambda **_: client,
                )

        result = report["results"][0]
        self.assertTrue(report["complete"])
        self.assertTrue(result["success"])
        self.assertTrue(result["reasoning_token_anomaly"])
        self.assertFalse(result["reasoning_content_observed"])
        self.assertEqual(
            completions.calls[0]["extra_body"],
            {"thinking": {"type": "disabled"}},
        )

    def test_disabled_profile_blocks_actual_reasoning_content(self) -> None:
        response = _response("visible", reasoning=1)
        response.choices[0].message.reasoning_content = "private reasoning"
        completions = _FakeCompletions(responses=[response])
        client = _FakeClient(completions)
        probe = PreflightProbe(
            "decision:kimi-k2.6",
            "kimi-k2.6@relay",
            "text",
            "preflight_decision",
        )
        with tempfile.TemporaryDirectory() as temp:
            path = _credential_file(Path(temp))
            with model_request_profiles_environment(
                {"kimi-k2.6": "thinking_disabled"}
            ):
                report = run_c1_preflight(
                    path,
                    probes=(probe,),
                    client_factory=lambda **_: client,
                )

        result = report["results"][0]
        self.assertFalse(report["complete"])
        self.assertFalse(result["success"])
        self.assertFalse(result["request_contract_passed"])
        self.assertTrue(result["reasoning_content_observed"])
        self.assertNotIn("private reasoning", json.dumps(report, sort_keys=True))

    def test_reasoning_only_length_response_retries_once_at_16384(self) -> None:
        completions = _FakeCompletions(
            responses=[
                _response(None, finish_reason="length", prompt=10, completion=8192, reasoning=8192),
                _response("ack", finish_reason="stop", prompt=11, completion=4, reasoning=1),
            ]
        )
        client = _FakeClient(completions)
        probe = PreflightProbe("decision:test", "test-model@relay", "text", "preflight_decision")
        with tempfile.TemporaryDirectory() as temp:
            path = _credential_file(Path(temp))
            with cost.scoped_meter() as meter:
                report = run_c1_preflight(path, probes=(probe,), client_factory=lambda **_: client)

        result = report["results"][0]
        self.assertTrue(result["success"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["usage"]["prompt_tokens"], 21)
        self.assertEqual(result["usage"]["reasoning_tokens"], 8193)
        self.assertEqual([call["max_tokens"] for call in completions.calls], [8192, 16384])
        self.assertEqual(
            [row["max_tokens"] for row in result["attempt_records"]],
            [8192, 16384],
        )
        self.assertFalse(result["attempt_records"][0]["content_nonempty"])
        self.assertTrue(result["attempt_records"][0]["reasoning_exhausted"])
        self.assertTrue(result["attempt_records"][1]["content_nonempty"])
        self.assertEqual(report["initial_max_tokens"], 8192)
        self.assertEqual(report["retry_max_tokens"], 16384)
        self.assertEqual(len(meter.records), 2)

    def test_reasoning_only_stop_response_is_not_retried(self) -> None:
        completions = _FakeCompletions(
            responses=[
                _response(
                    None,
                    finish_reason="stop",
                    prompt=10,
                    completion=40,
                    reasoning=40,
                ),
            ]
        )
        client = _FakeClient(completions)
        probe = PreflightProbe(
            "decision:test",
            "test-model@relay",
            "text",
            "preflight_decision",
        )
        with tempfile.TemporaryDirectory() as temp:
            path = _credential_file(Path(temp))
            with cost.scoped_meter() as meter:
                report = run_c1_preflight(
                    path,
                    probes=(probe,),
                    client_factory=lambda **_: client,
                )

        result = report["results"][0]
        self.assertFalse(result["success"])
        self.assertEqual(result["attempts"], 1)
        self.assertFalse(result["attempt_records"][0]["reasoning_exhausted"])
        self.assertEqual([call["max_tokens"] for call in completions.calls], [8192])
        self.assertEqual(len(meter.records), 1)
        self.assertEqual(
            report["retry_rule"],
            "one_retry_only_if_empty_content_and_explicit_output_limit",
        )

    def test_exception_text_is_not_serialized(self) -> None:
        request_error = RuntimeError("quota exceeded; gateway echoed local-secret")
        request_error.status_code = 429
        completions = _FakeCompletions(error=request_error)
        client = _FakeClient(completions)
        probe = PreflightProbe("decision:test", "test-model@relay", "text", "preflight_decision")
        with tempfile.TemporaryDirectory() as temp:
            path = _credential_file(Path(temp))
            with cost.scoped_meter() as meter:
                report = run_c1_preflight(
                    path, probes=(probe,), client_factory=lambda **_: client
                )
        serialized = json.dumps(report, sort_keys=True)
        self.assertFalse(report["complete"])
        self.assertEqual(report["results"][0]["error_type"], "RuntimeError")
        self.assertEqual(meter.records, [])
        self.assertNotIn("gateway echoed", serialized)
        self.assertNotIn("local-secret", serialized)

    def test_success_response_without_usage_fails_closed(self) -> None:
        response = _response()
        response.usage = None
        completions = _FakeCompletions(responses=[response])
        client = _FakeClient(completions)
        probe = PreflightProbe(
            "decision:test", "test-model@relay", "text", "preflight_decision"
        )
        with tempfile.TemporaryDirectory() as temp:
            path = _credential_file(Path(temp))
            with cost.scoped_meter() as meter:
                report = run_c1_preflight(
                    path, probes=(probe,), client_factory=lambda **_: client
                )

        self.assertFalse(report["complete"])
        self.assertEqual(report["results"][0]["error_type"], "UsageAccountingError")
        self.assertFalse(meter.summary()["usage_complete"])
        self.assertEqual(meter.summary()["incomplete_calls"], 1)

    def test_second_attempt_transport_error_preserves_first_attempt_usage(self) -> None:
        completions = _FakeCompletions(
            responses=[
                _response(
                    None,
                    finish_reason="length",
                    prompt=10,
                    completion=8192,
                    reasoning=8192,
                ),
                TimeoutError("second attempt transport timeout"),
            ]
        )
        client = _FakeClient(completions)
        probe = PreflightProbe(
            "decision:test", "test-model@relay", "text", "preflight_decision"
        )
        with tempfile.TemporaryDirectory() as temp:
            path = _credential_file(Path(temp))
            with cost.scoped_meter() as meter:
                report = run_c1_preflight(
                    path, probes=(probe,), client_factory=lambda **_: client
                )

        result = report["results"][0]
        self.assertFalse(result["success"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["usage"]["prompt_tokens"], 10)
        self.assertEqual(result["usage"]["completion_tokens"], 8192)
        self.assertEqual(result["usage"]["reasoning_tokens"], 8192)
        self.assertEqual(len(meter.records), 2)
        self.assertFalse(meter.summary()["usage_complete"])
        self.assertEqual(result["error_type"], "UsageAccountingError")

    def test_failed_preflight_call_with_usage_is_counted(self) -> None:
        request_error = RuntimeError("provider failure")
        request_error.usage = SimpleNamespace(
            prompt_tokens=7,
            completion_tokens=3,
            total_tokens=10,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=3),
        )
        completions = _FakeCompletions(error=request_error)
        client = _FakeClient(completions)
        probe = PreflightProbe(
            "decision:test", "test-model@relay", "text", "preflight_decision"
        )
        with tempfile.TemporaryDirectory() as temp:
            path = _credential_file(Path(temp))
            with cost.scoped_meter() as meter:
                report = run_c1_preflight(
                    path, probes=(probe,), client_factory=lambda **_: client
                )

        self.assertFalse(report["complete"])
        self.assertEqual(report["results"][0]["usage"]["total_tokens"], 10)
        self.assertEqual(meter.summary()["llm_calls"], 1)
        self.assertEqual(meter.summary()["reasoning_tokens"], 3)


if __name__ == "__main__":
    unittest.main()
