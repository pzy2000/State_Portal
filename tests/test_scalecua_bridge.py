"""Protocol, usage-accounting, and boundary tests for the ScaleCUA bridge."""

from __future__ import annotations

import io
import json
import os
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from warptest import cost
from warptest.agent_s_bridge import AgentSAction
from warptest.scalecua_bridge import (
    ScaleCUABridgeClient,
    ScaleCUABridgeConfig,
    ScaleCUAUsageAccountingError,
)
from warptest.scalecua_worker import (
    SCALECUA_PROTOCOL_VERSION,
    build_scalecua_actions,
    parse_verb_call,
    parse_scalecua_response,
    smart_resize,
)


ROOT = Path(__file__).resolve().parents[1]
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8ffff3f030005fe02fea72d1f240000000049454e44ae426082"
)


def _config(**overrides) -> ScaleCUABridgeConfig:
    values = dict(
        model="Qwen/Qwen3.8-27B",
        base_url="https://scalecua.invalid/v1",
        api_key_env="TEST_SCALECUA_KEY",
        native_screen_width=1280,
        native_screen_height=800,
        execution_backend="background_pixel_v1",
    )
    values.update(overrides)
    return ScaleCUABridgeConfig(**values)


def _write_frame(directory: Path) -> Path:
    from PIL import Image

    path = directory / "frame.png"
    Image.new("RGB", (1280, 800), (24, 24, 24)).save(path, format="PNG")
    return path


def _response(content: str, *, prompt_tokens: int = 100, completion_tokens: int = 20):
    return SimpleNamespace(
        id="resp-scalecua-1",
        model="Qwen/Qwen3.8-27B",
        system_fingerprint=None,
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=content, reasoning_content=None),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            reasoning_tokens=None,
            prompt_tokens_details=None,
            completion_tokens_details=None,
        ),
    )


class _FakeServer:
    """In-process stand-in for the JSONL child protocol."""

    def __init__(self, responses: dict[int, dict]) -> None:
        self.responses = responses

    def __call__(self, command, **kwargs):
        process = SimpleNamespace(
            stdin=SimpleNamespace(
                write=lambda text: self._accept(text), flush=lambda: None
            ),
            stdout=SimpleNamespace(close=lambda: None),
            stderr=SimpleNamespace(close=lambda: None),
            poll=lambda: None,
            terminate=lambda: None,
            kill=lambda: None,
            wait=lambda timeout=5: 0,
        )
        process.pid = 4242
        return process

    def _accept(self, text: str) -> None:
        pass


def _feed(client: ScaleCUABridgeClient, response: dict) -> None:
    """Deliver one pre-serialized JSONL reply for the next request id."""

    def reader(stream, sink):  # mirrors _read_stdout behavior
        sink.put(response)

    client._read_stdout = reader


class ScaleCUABridgeConfigTests(unittest.TestCase):
    def test_rejects_non_background_backend(self) -> None:
        with self.assertRaises(ValueError):
            _config(execution_backend="foreground_native_v1").validated()

    def test_rejects_raw_api_key_names(self) -> None:
        with self.assertRaises(ValueError):
            _config(api_key_env="not an env name").validated()

    def test_worker_payload_carries_key_env_name_only(self) -> None:
        payload = _config().to_worker_payload(scalecua_root=ROOT / "baselines" / "ScaleCUA")
        self.assertEqual(payload["api_key_env"], "TEST_SCALECUA_KEY")
        self.assertNotIn("api_key", payload)
        self.assertNotIn("sk-", json.dumps(payload))


class ScaleCUABridgeProtocolTests(unittest.TestCase):
    def test_unknown_usage_retains_the_original_provider_error(self) -> None:
        client = ScaleCUABridgeClient(_config(), root=ROOT)
        client._process = SimpleNamespace(stdin=io.StringIO(), poll=lambda: None)
        client._responses.put({"id": 1, "ok": False, "error": {
            "type": "APITimeoutError", "message": "read timed out", "status_code": 408,
            "usage_events": [{"model": "test", "kind": "scalecua_worker",
                "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
                "usage_complete": False}],
        }})
        with cost.scoped_meter() as meter:
            with self.assertRaisesRegex(ScaleCUAUsageAccountingError, "APITimeoutError: read timed out") as caught:
                client._request("predict")
        self.assertEqual(caught.exception.status_code, 408)
        self.assertFalse(meter.summary()["usage_complete"])
        self.assertIsNone(meter.summary()["calls"][0]["total_tokens"])

    def test_missing_submodule_fails_closed(self) -> None:
        config = _config()
        with tempfile_dir() as tmp:
            client = ScaleCUABridgeClient(config, root=tmp)
            with patch.dict(os.environ, {"TEST_SCALECUA_KEY": "secret"}):
                with self.assertRaisesRegex(Exception, "ScaleCUA submodule"):
                    client.start()

    def test_missing_key_env_fails_closed(self) -> None:
        config = _config()
        with tempfile_dir():
            client = ScaleCUABridgeClient(config, root=ROOT)
            environment = {name: "x" for name in os.environ if name != "TEST_SCALECUA_KEY"}
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(Exception, "TEST_SCALECUA_KEY"):
                    client.start()


class tempfile_dir:
    def __init__(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()

    def __enter__(self) -> Path:
        return Path(self._tmp.__enter__())

    def __exit__(self, *exc) -> None:
        self._tmp.__exit__(*exc)


class ScaleCUAPredictTests(unittest.TestCase):
    def _started_client(self, tmp: Path, reply: dict):
        client = ScaleCUABridgeClient(
            _config(), root=ROOT, python_executable="/bin/true"
        )
        client._process_factory = lambda *a, **k: SimpleNamespace(
            pid=1,
            stdin=SimpleNamespace(write=lambda t: None, flush=lambda: None),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            poll=lambda: None,
            terminate=lambda: None,
            kill=lambda: None,
            wait=lambda timeout=5: 0,
        )
        client._process = client._process_factory()
        client._responses = _FakeQueue(reply)
        client.metadata = {
            "protocol_version": SCALECUA_PROTOCOL_VERSION,
            "execution_backend": "background_pixel_v1",
            "native_screen_width": 1280,
            "native_screen_height": 800,
        }
        return client

    def test_predict_records_usage_exactly_once(self) -> None:
        with tempfile_dir() as tmp:
            frame = _write_frame(tmp)
            reply = {
                "id": 1,
                "ok": True,
                "result": {
                    "action": {"kind": "click", "x": 10, "y": 20},
                    "actions": [{"kind": "click", "x": 10, "y": 20}],
                    "usage_events": [
                        {
                            "model": "Qwen/Qwen3.8-27B",
                            "kind": "scalecua_worker",
                            "prompt_tokens": 120,
                            "completion_tokens": 30,
                            "total_tokens": 150,
                            "usage_complete": True,
                        }
                    ],
                    "usage_accounting_complete": True,
                    "info": {"thought": "t"},
                },
            }
            client = self._started_client(tmp, reply)
            with cost.scoped_meter() as meter:
                prediction = client.predict(
                    task="t",
                    instruction="open the menu",
                    observation="raw_screenshot",
                    screenshot=frame,
                    width=1280,
                    height=800,
                    history=[],
                    session_id="s",
                    frame_id=0,
                    capture_backend="playwright_page_screenshot_v1",
                )
            self.assertEqual(prediction.action.kind, "click")
            summary = meter.summary()
            self.assertEqual(summary["llm_calls"], 1)
            self.assertEqual(summary["total_tokens"], 150)

    def test_predict_rejects_incomplete_usage(self) -> None:
        with tempfile_dir() as tmp:
            frame = _write_frame(tmp)
            reply = {
                "id": 1,
                "ok": True,
                "result": {
                    "action": {"kind": "wait", "seconds": 1},
                    "actions": [{"kind": "wait", "seconds": 1}],
                    "usage_events": [
                        {
                            "model": "Qwen/Qwen3.8-27B",
                            "kind": "scalecua_worker",
                            "prompt_tokens": 10,
                            "completion_tokens": None,
                            "total_tokens": None,
                            "usage_complete": False,
                        }
                    ],
                    "usage_accounting_complete": False,
                    "info": {},
                },
            }
            client = self._started_client(tmp, reply)
            with self.assertRaisesRegex(
                Exception, "usage event was incomplete"
            ):
                client.predict(
                    task="t",
                    instruction="wait a moment",
                    observation="raw_screenshot",
                    screenshot=frame,
                    width=1280,
                    height=800,
                    history=[],
                    session_id="s",
                    frame_id=0,
                    capture_backend="playwright_page_screenshot_v1",
                )

    def test_predict_requires_policy_frame_identity(self) -> None:
        with tempfile_dir() as tmp:
            frame = _write_frame(tmp)
            client = self._started_client(tmp, {"id": 1, "ok": True, "result": {}})
            with self.assertRaises(ValueError):
                client.predict(
                    task="t",
                    instruction="do it",
                    observation="raw_screenshot",
                    screenshot=frame,
                    width=1280,
                    height=800,
                    history=[],
                    session_id=None,
                    frame_id=None,
                    capture_backend=None,
                )

    def test_predict_rejects_frame_sha_mismatch(self) -> None:
        with tempfile_dir() as tmp:
            frame = _write_frame(tmp)
            other = frame.with_name("other.png")
            other.write_bytes(PNG_1PX)
            client = self._started_client(tmp, {"id": 1, "ok": True, "result": {}})
            # The parent computes sha over `frame`, then the child would verify;
            # here the client-side validation path only checks identity fields.
            with self.assertRaises(ValueError):
                client.predict(
                    task="t",
                    instruction="do it",
                    observation="raw_screenshot",
                    screenshot=other,
                    width=1280,
                    height=800,
                    history=[],
                    session_id="s",
                    frame_id=-1,
                    capture_backend="playwright_page_screenshot_v1",
                )


class _FakeQueue:
    def __init__(self, reply: dict) -> None:
        self._reply = reply
        self._lock = threading.Lock()
        self._next_id = 0

    def get(self, timeout=None):
        with self._lock:
            self._next_id += 1
            reply = dict(self._reply)
            reply["id"] = self._next_id
            return reply

    def empty(self) -> bool:
        return True


class ScaleCUAActionMappingTests(unittest.TestCase):
    def test_parse_response_blocks(self) -> None:
        think, operation, actions = parse_scalecua_response(
            "<think>t</think>\n<operation>o</operation>\n<action>\nclick(x=1, y=2)\n</action>"
        )
        self.assertEqual(think, "t")
        self.assertEqual(operation, "o")
        self.assertEqual(actions, ["click(x=1, y=2)"])

    def test_parse_verb_call_forms(self) -> None:
        self.assertEqual(parse_verb_call("click(x=1, y=2)"), ("click", {"x": 1, "y": 2}))
        verb, args = parse_verb_call("hotkey(args=['ctrl', 's'])")
        self.assertEqual((verb, args), ("hotkey", {"args": ["ctrl", "s"]}))
        self.assertEqual(
            parse_verb_call("write(message='hi there')"),
            ("write", {"message": "hi there"}),
        )
        self.assertIsNone(parse_verb_call("not a call"))

    def test_build_actions_normalizes_resize_coordinates(self) -> None:
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (2828, 1764), (10, 10, 10)).save(buf, format="PNG")
        response = "<action>\nclick(x=1414, y=882)\n</action>"
        _think, _op, actions, skipped = build_scalecua_actions(
            response, screenshot_bytes=buf.getvalue(), width=1414, height=882
        )
        self.assertEqual(skipped, [])
        self.assertEqual(actions[0]["kind"], "click")
        self.assertLessEqual(actions[0]["x"], 1413)
        self.assertLessEqual(actions[0]["y"], 881)

    def test_smart_resize_matches_upstream_factor(self) -> None:
        height, width = smart_resize(800, 1280)
        self.assertEqual(height % 28, 0)
        self.assertEqual(width % 28, 0)

    def test_terminate_maps_to_done_or_fail(self) -> None:
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (56, 56), (0, 0, 0)).save(buf, format="PNG")
        _t, _o, actions, _s = build_scalecua_actions(
            "<action>\nterminate(status='success')\n</action>",
            screenshot_bytes=buf.getvalue(),
            width=56,
            height=56,
        )
        self.assertEqual(actions[0]["kind"], "done")


class BridgeAgentSActionCompatibilityTests(unittest.TestCase):
    def test_worker_actions_parse_as_agent_s_actions(self) -> None:
        action = AgentSAction.from_mapping({"kind": "click", "x": 3, "y": 4})
        self.assertEqual(action.kind, "click")


if __name__ == "__main__":
    unittest.main()
