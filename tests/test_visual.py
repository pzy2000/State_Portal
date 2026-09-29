from __future__ import annotations

import base64
import hashlib
import html
import io
import itertools
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml
from PIL import Image

from scripts.generate_visual_pairs import (
    _assert_spacerpg_flight_scene_is_rendered,
    _assert_libreoffice_pair_is_distinct,
    _assert_pair_has_visible_change,
    _pair_change_metrics,
    _preferred_capture_candidates,
    _validate_libreoffice_capture_metadata,
    frames_are_near_duplicates,
)
from warptest import cost
from warptest import hybrid_visual_calibration as hybrid_visual
from warptest.models import PhaseResult, RunReport
from warptest.model_bench import ModelBenchTask, attach_canonical_visual_oracle
from warptest.jynew_runtime import JYNEW_HOOK_TEMPLATE_PATH, JynewRuntimeAdapter, ensure_jynew_instrumentation
from warptest.openra_runtime import OpenRARuntimeAdapter, _run_until_informative_file_with_process_group
from warptest.runner import load_spec, run_spec
from warptest.visual import (
    CascadePolicyConfig,
    DEFAULT_VISUAL_JUDGE_BASE_URL,
    DEFAULT_VISUAL_JUDGE_MODEL,
    FakeVisualJudgeClient,
    VISUAL_JUDGE_MAX_IMAGE_BYTES,
    VISUAL_JUDGE_MAX_IMAGE_SIDE,
    VisualJudgeClient,
    VisualOracleError,
    _visual_judge_request_parameters,
    build_visual_prompt,
    image_data_url,
    parse_visual_judge_response,
    run_visual_audit,
    run_visual_pair_audit,
    screenshot_quality,
    validate_visual_oracle,
    visual_judge_image_metadata,
    visual_judge_image_payload,
)
from warptest.visual_experiment import (
    VisualExperimentSample,
    VisualExperimentSession,
    _HTML,
    _negative_variants_for_spec,
    build_visual_experiment_manifest,
    bundled_spec_paths,
    cohen_kappa,
    experiment_metrics,
    materialize_sample_spec,
)


ROOT = Path(__file__).resolve().parents[1]


class VisualOracleTests(unittest.TestCase):
    def test_visual_judge_omits_temperature_for_claude_sonnet_5(self) -> None:
        messages = [{"role": "user", "content": "judge"}]

        claude = _visual_judge_request_parameters("claude-sonnet-5", messages)
        qwen = _visual_judge_request_parameters("qwen3-vl-flash", messages)

        self.assertNotIn("temperature", claude)
        self.assertEqual(qwen["temperature"], 0)
        self.assertEqual(claude["response_format"], {"type": "json_object"})

    def test_visual_pair_generator_prefers_overlay_filtered_policy_frame(self) -> None:
        report = SimpleNamespace(
            screenshots=["post-smoke.png", "visual-post-smoke.png"],
            visual_oracle_result=PhaseResult(
                "success",
                data={"visual_audit": {"frame_paths": ["visual-post-smoke.png"]}},
            ),
        )

        self.assertEqual(
            _preferred_capture_candidates(report, capture="visual"),
            [Path("visual-post-smoke.png")],
        )
        self.assertEqual(
            _preferred_capture_candidates(report, capture="run"),
            [Path("post-smoke.png"), Path("visual-post-smoke.png")],
        )

    def test_validates_bundled_visual_oracle(self) -> None:
        spec_path = ROOT / "specs/2048/merge-left.yaml"
        oracle = validate_visual_oracle(load_spec(spec_path), spec_path, required=True)
        self.assertIsNotNone(oracle)
        self.assertEqual(oracle["expected_label"], "correct")
        self.assertTrue(Path(oracle["golden_image"]).exists())

    def test_openra_visual_oracles_validate(self) -> None:
        for spec_path in sorted((ROOT / "specs/openra").glob("*.yaml")):
            with self.subTest(spec=spec_path.name):
                oracle = validate_visual_oracle(load_spec(spec_path), spec_path, required=True)
                self.assertIsNotNone(oracle)
                self.assertEqual(oracle["expected_label"], "correct")
                self.assertTrue(screenshot_quality(Path(oracle["golden_image"]))["informative"])
                oracle_text = " ".join(
                    [
                        oracle["description"],
                        *oracle["rubric"]["must"],
                        *oracle["rubric"]["must_not"],
                        *oracle["rubric"]["ignore"],
                    ]
                ).lower()
                self.assertNotIn("map preview", oracle_text)

    def test_spacerpg_mining_sortie_after_has_rendered_flight_scene(self) -> None:
        spec_path = ROOT / "specs/spacerpg/c1-trade-refit-mining-sortie.yaml"
        oracle = validate_visual_oracle(
            load_spec(spec_path), spec_path, required=True
        )
        _assert_spacerpg_flight_scene_is_rendered(
            "spacerpg-trade-refit-mining-sortie",
            Path(oracle["golden_after"]),
        )

    def test_jynew_visual_oracles_validate(self) -> None:
        for spec_path in sorted((ROOT / "specs/jynew").glob("*.yaml")):
            with self.subTest(spec=spec_path.name):
                oracle = validate_visual_oracle(load_spec(spec_path), spec_path, required=True)
                self.assertIsNotNone(oracle)
                self.assertEqual(oracle["expected_label"], "correct")
                self.assertTrue(screenshot_quality(Path(oracle["golden_image"]))["informative"])

    def test_jynew_sidequest_submap_pair_has_visible_scene_change(self) -> None:
        spec_path = ROOT / "specs/jynew/c1-sidequest-trade-submap.yaml"
        oracle = validate_visual_oracle(
            load_spec(spec_path), spec_path, required=True
        )
        _assert_pair_has_visible_change(
            "jynew-sidequest-trade-submap",
            Path(oracle["golden_before"]),
            Path(oracle["golden_after"]),
        )

    def test_pal3_visual_oracles_validate(self) -> None:
        spec_paths = sorted((ROOT / "specs/pal3").glob("*.yaml"))
        self.assertEqual(len(spec_paths), 28)
        for spec_path in spec_paths:
            with self.subTest(spec=spec_path.name):
                oracle = validate_visual_oracle(load_spec(spec_path), spec_path, required=True)
                self.assertIsNotNone(oracle)
                self.assertEqual(oracle["expected_label"], "correct")
                self.assertTrue(screenshot_quality(Path(oracle["golden_image"]))["informative"])

    def test_pal3_companion_ending_after_golden_shows_the_recruited_companion(
        self,
    ) -> None:
        """The pair must prove the recruitment, not excuse an unchanged frame.

        This oracle used to tell the judge to ignore "that the frame is nearly
        identical to the before frame", which was true only because the capture
        was cropped and the party was never displayed. The after golden now shows
        the recruited companion standing beside the player, so the delta is real
        and the rubric may no longer waive it.
        """
        spec_path = ROOT / "specs/pal3/c1-companion-ending-preparation.yaml"
        oracle = validate_visual_oracle(
            load_spec(spec_path), spec_path, required=True
        )
        for statement in oracle["after_rubric"]["ignore"]:
            self.assertNotIn("identical to the before frame", statement)
        metrics = _pair_change_metrics(
            Path(oracle["golden_before"]), Path(oracle["golden_after"])
        )
        self.assertGreater(metrics["changed_pixel_ratio"], 0.002)

    def test_blender_visual_oracles_validate(self) -> None:
        for spec_path in sorted((ROOT / "specs/blender").glob("*.yaml")):
            with self.subTest(spec=spec_path.name):
                oracle = validate_visual_oracle(load_spec(spec_path), spec_path, required=True)
                self.assertIsNotNone(oracle)
                self.assertEqual(oracle["expected_label"], "correct")
                self.assertTrue(screenshot_quality(Path(oracle["golden_image"]))["informative"])
                self.assertTrue(screenshot_quality(Path(oracle["golden_before"]))["informative"])
                self.assertTrue(screenshot_quality(Path(oracle["golden_after"]))["informative"])

    def test_jupyterlab_visual_oracles_validate(self) -> None:
        spec_paths = sorted((ROOT / "specs/jupyterlab").glob("*.yaml"))
        self.assertEqual(len(spec_paths), 9)
        for spec_path in spec_paths:
            with self.subTest(spec=spec_path.name):
                oracle = validate_visual_oracle(load_spec(spec_path), spec_path, required=True)
                self.assertIsNotNone(oracle)
                self.assertEqual(oracle["expected_label"], "correct")
                self.assertTrue(screenshot_quality(Path(oracle["golden_image"]))["informative"])
                self.assertTrue(screenshot_quality(Path(oracle["golden_before"]))["informative"])
                self.assertTrue(screenshot_quality(Path(oracle["golden_after"]))["informative"])

    def test_jupyterlab_reproducible_workspace_pair_has_visible_change(self) -> None:
        spec_path = (
            ROOT
            / "specs/jupyterlab/c1-reproducible-experiment-workspace.yaml"
        )
        oracle = validate_visual_oracle(
            load_spec(spec_path), spec_path, required=True
        )
        _assert_pair_has_visible_change(
            "jupyterlab-reproducible-experiment-workspace",
            Path(oracle["golden_before"]),
            Path(oracle["golden_after"]),
        )

    def test_libreoffice_visual_oracles_validate(self) -> None:
        spec_paths = sorted((ROOT / "specs/libreoffice").glob("*.yaml"))
        self.assertEqual(len(spec_paths), 13)
        for spec_path in spec_paths:
            with self.subTest(spec=spec_path.name):
                oracle = validate_visual_oracle(load_spec(spec_path), spec_path, required=True)
                self.assertIsNotNone(oracle)
                self.assertEqual(oracle["expected_label"], "correct")
                self.assertTrue(screenshot_quality(Path(oracle["golden_image"]))["informative"])
                self.assertTrue(screenshot_quality(Path(oracle["golden_before"]))["informative"])
                self.assertTrue(screenshot_quality(Path(oracle["golden_after"]))["informative"])
                self.assertIn("after_description", oracle)
                self.assertIn("after_rubric", oracle)

    def test_libreoffice_c1_visual_goldens_are_pairwise_distinct(self) -> None:
        paths: list[Path] = []
        for spec_path in sorted(
            (ROOT / "specs/libreoffice").glob("c1-*.yaml")
        ):
            oracle = validate_visual_oracle(
                load_spec(spec_path), spec_path, required=True
            )
            before = Path(oracle["golden_before"])
            after = Path(oracle["golden_after"])
            _assert_libreoffice_pair_is_distinct(
                spec_path.stem, before, after
            )
            paths.extend((before, after))
        digests = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
        self.assertEqual(len(digests), 6)
        self.assertEqual(len(set(digests)), 6)

    def test_every_c1_task_owns_an_informative_distinct_visual_pair(self) -> None:
        """Every formal C1 task must register its own discriminative golden pair.

        Two failure modes made earlier visual evidence useless: a task pointed at
        another task's golden, and a task's before/after frames were the same
        bytes, so the pair could not show that anything happened. Both are checked
        here for the whole 61-task universe rather than for LibreOffice alone. How
        large the visible delta is stays an audit judgement (``weak-visual-delta``);
        this test only enforces that a pair exists, is informative, is the task's
        own, and is not the same frame twice.

        Whether two tasks share an after frame is judged on pixels rather than on
        digests: PAL3 draws a live FPS and memory readout, so two frames a reader
        cannot tell apart still hash differently, and a digest comparison called
        them distinct evidence.
        """

        tasks = yaml.safe_load(
            (ROOT / "benchmarks/p5/c1_tasks.yaml").read_text(encoding="utf-8")
        )["tasks"]
        # Two tasks may legitimately start from the same visible state (both Love
        # tasks that begin at home with money 1000 and energy 100 render the same
        # screen), so before frames are only required to be task-owned files. An
        # after frame is the graded end state and must be the task's own picture.
        before_paths: dict[str, str] = {}
        afters_by_benchmark: dict[str, list[tuple[str, Path]]] = {}
        for task in tasks:
            task_id = str(task["id"])
            spec_path = ROOT / str(task["oracle_spec"])
            with self.subTest(task=task_id):
                oracle = validate_visual_oracle(
                    load_spec(spec_path), spec_path, required=True
                )
                before = Path(oracle["golden_before"])
                after = Path(oracle["golden_after"])
                self.assertTrue(before.is_file(), f"{task_id}: missing golden_before")
                self.assertTrue(after.is_file(), f"{task_id}: missing golden_after")
                self.assertTrue(
                    screenshot_quality(before)["informative"],
                    f"{task_id}: golden_before is not informative",
                )
                self.assertTrue(
                    screenshot_quality(after)["informative"],
                    f"{task_id}: golden_after is not informative",
                )
                # How large a task's own delta is stays an audit judgement, so
                # this half only rejects the same file used twice.
                self.assertNotEqual(
                    hashlib.sha256(before.read_bytes()).hexdigest(),
                    hashlib.sha256(after.read_bytes()).hexdigest(),
                    f"{task_id}: before/after goldens are byte-identical",
                )
                reused_before = before_paths.get(str(before))
                self.assertIsNone(
                    reused_before,
                    f"{task_id}: before golden file is reused from {reused_before}",
                )
                before_paths[str(before)] = task_id
                afters_by_benchmark.setdefault(str(task["benchmark"]), []).append(
                    (task_id, after)
                )

        # Only same-benchmark frames can plausibly collide, and restricting the
        # comparison to each benchmark keeps this quadratic check cheap.
        for benchmark, afters in sorted(afters_by_benchmark.items()):
            for (left_id, left), (right_id, right) in itertools.combinations(afters, 2):
                with self.subTest(benchmark=benchmark, left=left_id, right=right_id):
                    self.assertFalse(
                        frames_are_near_duplicates(left, right),
                        f"{left_id}: after golden shows the same picture as {right_id}",
                    )

    def test_libreoffice_capture_metadata_rejects_start_center(self) -> None:
        _validate_libreoffice_capture_metadata(
            "calc-before",
            "calc_spreadsheet",
            {
                "frame_id": 1,
                "active_window_title": "Untitled 1",
                "visible_window_count": 2,
            },
        )
        with self.assertRaisesRegex(RuntimeError, "Start Center"):
            _validate_libreoffice_capture_metadata(
                "calc-before",
                "calc_spreadsheet",
                {
                    "frame_id": 1,
                    "active_window_title": "LibreOfficeDev",
                    "visible_window_count": 1,
                },
            )

    def test_visible_change_gate_ignores_volatile_top_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            before = Path(tmp) / "before.png"
            after = Path(tmp) / "after.png"
            before_image = Image.new("RGB", (100, 100), "navy")
            after_image = before_image.copy()
            for x in range(20):
                for y in range(10):
                    after_image.putpixel((x, y), (255, 255, 255))
            before_image.save(before)
            after_image.save(after)
            with self.assertRaisesRegex(ValueError, "no meaningful visible change"):
                _assert_pair_has_visible_change(
                    "jynew-sidequest-trade-submap", before, after
                )

            distinct_image = after_image.copy()
            for x in range(25, 75):
                for y in range(30, 80):
                    distinct_image.putpixel((x, y), (255, 165, 0))
            distinct_image.save(after)
            _assert_pair_has_visible_change(
                "jynew-sidequest-trade-submap", before, after
            )

    def test_spacerpg_flight_scene_gate_rejects_black_center(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "flight.png"
            black = Image.new("RGB", (200, 120), (7, 12, 20))
            black.save(image_path)
            with self.assertRaisesRegex(ValueError, "appears unrendered"):
                _assert_spacerpg_flight_scene_is_rendered(
                    "spacerpg-trade-refit-mining-sortie", image_path
                )

            rendered = black.copy()
            for x in range(70, 130):
                for y in range(30, 85):
                    rendered.putpixel(
                        (x, y),
                        (220, 160, 60) if (x + y) % 3 else (20, 40, 100),
                    )
            rendered.save(image_path)
            _assert_spacerpg_flight_scene_is_rendered(
                "spacerpg-trade-refit-mining-sortie", image_path
            )

    def test_c1_visual_review_page_contains_both_rubrics(self) -> None:
        review = (
            ROOT / "benchmarks/p5/c1_visual_review.html"
        ).read_text(encoding="utf-8")
        index = json.loads(
            (ROOT / "benchmarks/p5/c1_visual_goldens.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(review.count('class="rubric before-rubric"'), 30)
        self.assertEqual(review.count('class="rubric after-rubric"'), 30)
        for entry in index["tasks"]:
            self.assertIn("before_rubric", entry)
            self.assertIn("after_rubric", entry)
            self.assertIn(
                f'{entry["before"]}?sha256={entry["before_sha256"]}',
                review,
            )
            self.assertIn(
                f'{entry["after"]}?sha256={entry["after_sha256"]}',
                review,
            )
        self.assertIn(
            "The regional table is sorted in descending revenue order",
            review,
        )

    def test_c1_visual_review_page_contains_all_task_descriptions(self) -> None:
        review = (
            ROOT / "benchmarks/p5/c1_visual_review.html"
        ).read_text(encoding="utf-8")
        index = json.loads(
            (ROOT / "benchmarks/p5/c1_visual_goldens.json").read_text(
                encoding="utf-8"
            )
        )
        expected_classes = (
            "preconditions",
            "reference-steps",
            "state-description",
        )
        expected_fields = (
            "preconditions",
            "reference_steps",
            "state_description",
        )
        self.assertNotIn("goal-description", review)
        self.assertNotIn("<h3>goal_description</h3>", review)
        for css_class in expected_classes:
            self.assertEqual(
                review.count(f'class="description-field {css_class}"'),
                30,
            )
        for entry in index["tasks"]:
            self.assertNotIn("goal_description", entry)
            for field in expected_fields:
                self.assertIsInstance(entry.get(field), str)
                self.assertTrue(entry[field].strip())
                self.assertIn(
                    html.escape(entry[field]),
                    review,
                )
            self.assertTrue(entry["after_description"].strip())
            self.assertIn(html.escape(entry["after_description"]), review)

    def test_jynew_instrumentation_refreshes_stale_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hook = Path(tmp) / "jyx2/Assets/Scripts/GameCore/WarptestCheckpointCommand.cs"
            hook.parent.mkdir(parents=True)
            hook.write_text("namespace Jyx2 { public static class WarptestCheckpoint {} }\n", encoding="utf-8")

            result = ensure_jynew_instrumentation(Path(tmp))
            refreshed = hook.read_text(encoding="utf-8")

        self.assertTrue(result["installed"])
        self.assertIn("refreshed stale", result["reason"])
        self.assertIn("screenshot_output_path", refreshed)
        self.assertIn("CaptureScreenshot", refreshed)
        self.assertIn("TextureHasVisibleRange", refreshed)
        self.assertIn("Application.isBatchMode", refreshed)

    def test_jynew_instrumentation_refreshes_uninformative_screenshot_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hook = Path(tmp) / "jyx2/Assets/Scripts/GameCore/WarptestCheckpointCommand.cs"
            hook.parent.mkdir(parents=True)
            hook.write_text(
                "namespace Jyx2 { public static class WarptestCheckpoint { "
                "static string screenshot_output_path; static void CaptureScreenshot() {} } }\n",
                encoding="utf-8",
            )

            result = ensure_jynew_instrumentation(Path(tmp))
            refreshed = hook.read_text(encoding="utf-8")

        self.assertTrue(result["installed"])
        self.assertIn("informative screenshot", result["reason"])
        self.assertIn("TextureHasVisibleRange", refreshed)
        self.assertIn("Application.isBatchMode", refreshed)

    def test_jynew_instrumentation_refreshes_partial_screen_capture_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hook = Path(tmp) / "jyx2/Assets/Scripts/GameCore/WarptestCheckpointCommand.cs"
            hook.parent.mkdir(parents=True)
            stale = JYNEW_HOOK_TEMPLATE_PATH.read_text(encoding="utf-8").replace(
                "s_lastCaptureSourceWidth = 0;",
                "s_lastCaptureSourceWidth = 0;\n"
                "            ScreenCapture.CaptureScreenshotAsTexture();",
                1,
            )
            hook.write_text(stale, encoding="utf-8")

            result = ensure_jynew_instrumentation(Path(tmp))
            refreshed = hook.read_text(encoding="utf-8")

        self.assertTrue(result["installed"])
        self.assertIn("full-frame camera capture", result["reason"])
        self.assertNotIn("CaptureScreenshotAsTexture", refreshed)

    def test_jynew_batch_visual_capture_uses_sync_execute_method_path(self) -> None:
        text = JYNEW_HOOK_TEMPLATE_PATH.read_text(encoding="utf-8")
        execute_start = text.index("static void ExecuteRequestPath")
        async_start = text.index("static async UniTaskVoid ExecuteRequestPathAsync")
        execute_body = text[execute_start:async_start]

        self.assertIn("if (Application.isPlaying)", execute_body)
        self.assertLess(execute_body.index("if (Application.isPlaying)"), execute_body.index("ExecuteRequestPathAsync"))
        self.assertIn("ProcessRequest(request)", execute_body)
        self.assertIn("AttachStateEvidence(request", execute_body)

    def test_jynew_post_action_capture_preserves_runtime_map(self) -> None:
        text = JYNEW_HOOK_TEMPLATE_PATH.read_text(encoding="utf-8")
        visual_start = text.index(
            "static async UniTask<string> PrepareVisualSceneForCapture"
        )
        runtime_start = text.index(
            "static async UniTask PrepareRuntimeForCapture", visual_start
        )
        visual_body = text[visual_start:runtime_start]
        self.assertIn(
            "PrepareRuntimeForCapture(target, applyTargetMap: false)",
            visual_body,
        )
        self.assertIn(
            "if (applyTargetMap && target.map_id >= 0)",
            text[runtime_start:],
        )

    def test_required_visual_oracle_missing_fails(self) -> None:
        with self.assertRaises(VisualOracleError):
            validate_visual_oracle({"name": "no visual"}, ROOT / "specs/2048/merge-left.yaml", required=True)

    def test_parse_visual_judge_response(self) -> None:
        response = parse_visual_judge_response(
            '```json\n{"label":"correct","confidence":0.8,"observed":"ok","rationale":"matches","rubric_failures":[]}\n```'
        )
        self.assertEqual(response["label"], "correct")
        self.assertEqual(response["confidence"], 0.8)
        with self.assertRaises(VisualOracleError):
            parse_visual_judge_response("not json")
        with self.assertRaises(VisualOracleError):
            parse_visual_judge_response('{"label":"pass"}')

    def test_parse_visual_judge_response_rejects_contradictory_contract(self) -> None:
        with self.assertRaisesRegex(
            VisualOracleError,
            "label=incorrect requires at least one rubric failure",
        ):
            parse_visual_judge_response(
                '{"label":"incorrect","confidence":0.95,'
                '"observed":"Images match outside ignored clouds.",'
                '"rationale":"The visible state matches the golden.",'
                '"rubric_failures":[]}'
            )
        with self.assertRaisesRegex(
            VisualOracleError,
            "label=correct requires an empty rubric_failures list",
        ):
            parse_visual_judge_response(
                '{"label":"correct","confidence":0.9,'
                '"observed":"The state matches.","rationale":"All required pixels match.",'
                '"rubric_failures":["wrong screen"]}'
            )
        with self.assertRaisesRegex(VisualOracleError, "rationale must be non-empty"):
            parse_visual_judge_response(
                '{"label":"incorrect","confidence":0.9,'
                '"observed":"The state differs.","rationale":"",'
                '"rubric_failures":["required HUD differs"]}'
            )
        with self.assertRaisesRegex(
            VisualOracleError,
            "explicit final conclusion contradicts its label",
        ):
            parse_visual_judge_response(
                '{"label":"incorrect","confidence":0.9,'
                '"observed":"A required HUD element appears different.",'
                '"rationale":"Final decision: correct.",'
                '"rubric_failures":["required HUD differs"]}'
            )

    def test_parse_visual_judge_response_rejects_null_contract_fields(self) -> None:
        for field in ("observed", "rationale", "rubric_failures"):
            payload = {
                "label": "incorrect",
                "confidence": 0.9,
                "observed": "The state differs.",
                "rationale": "A required HUD element is missing.",
                "rubric_failures": ["required HUD differs"],
            }
            payload[field] = None
            with self.subTest(field=field), self.assertRaises(VisualOracleError):
                parse_visual_judge_response(json.dumps(payload))

    def test_visual_judge_retries_structurally_invalid_verdict(self) -> None:
        responses = iter(
            [
                (
                    '{"label":"incorrect","confidence":0.95,'
                    '"observed":"The images match.","rationale":"They match.",'
                    '"rubric_failures":[]}'
                ),
                (
                    '{"label":"correct","confidence":0.95,'
                    '"observed":"The images match.","rationale":"All required pixels match.",'
                    '"rubric_failures":[]}'
                ),
            ]
        )

        def create(**_kwargs: object) -> object:
            message = SimpleNamespace(content=next(responses))
            usage = SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason="stop")],
                usage=usage,
            )

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        with tempfile.TemporaryDirectory() as tmp:
            golden_path = Path(tmp) / "golden.png"
            live_path = Path(tmp) / "live.png"
            Image.new("RGB", (64, 64), color=(16, 80, 32)).save(golden_path)
            Image.new("RGB", (64, 64), color=(16, 80, 32)).save(live_path)
            with (
                patch.dict(
                    os.environ,
                    {"WARPTEST_VISUAL_JUDGE_ATTEMPTS": "2"},
                    clear=False,
                ),
                patch("warptest.visual.time.sleep", return_value=None),
                cost.scoped_meter() as meter,
            ):
                verdict = VisualJudgeClient(
                    api_key="unused",
                    base_url=None,
                    model="judge/model",
                    client=fake_client,
                ).judge(
                    prompt="judge this",
                    live_screenshot=live_path,
                    golden_image=golden_path,
                )

        self.assertEqual(verdict["label"], "correct")
        self.assertEqual(verdict["attempts"], 2)
        self.assertEqual(meter.summary()["llm_calls"], 2)

    def test_fake_visual_judge(self) -> None:
        verdict = FakeVisualJudgeClient("incorrect").judge(
            prompt="unused",
            live_screenshot=ROOT / "specs/2048/visual/merge-left.golden.png",
            golden_image=ROOT / "specs/2048/visual/merge-left.golden.png",
        )
        self.assertEqual(verdict["label"], "incorrect")
        self.assertEqual(verdict["judge_model"], "fake-visual-judge")
        self.assertEqual(
            verdict["rubric_failures"],
            ["Configured fake incorrect verdict."],
        )
        reparsed = parse_visual_judge_response(json.dumps(verdict))
        self.assertEqual(reparsed["label"], "incorrect")

    def test_openra_visual_prompt_rejects_invented_error_pages(self) -> None:
        spec_path = ROOT / "specs/openra/cnc-hot-box-dropzone.yaml"
        spec = load_spec(spec_path)
        oracle = validate_visual_oracle(spec, spec_path, required=True)
        self.assertIsNotNone(oracle)

        prompt = build_visual_prompt(spec=spec, oracle=oracle, report=_minimal_run_report())
        prompt_data = json.loads(prompt)
        instructions = " ".join(prompt_data["instructions"])

        self.assertIn("Base observations only on visible pixels", instructions)
        self.assertIn("do not infer or invent unseen error pages", instructions)
        self.assertIn("idle clean-launch RTS scene", instructions)
        self.assertIn("Only claim crash/error UI when the live screenshot visibly contains", instructions)

    def test_visual_prompt_treats_golden_as_binding_reference(self) -> None:
        spec_path = ROOT / "specs/luanti/water-source.yaml"
        spec = load_spec(spec_path)
        oracle = validate_visual_oracle(spec, spec_path, required=True)
        prompt = build_visual_prompt(spec=spec, oracle=oracle, report=_minimal_run_report(benchmark="luanti", profile="LuantiProfile"))
        prompt_data = json.loads(prompt)
        instructions = " ".join(prompt_data["instructions"])

        self.assertIn("golden screenshot as the binding visual reference", instructions)
        self.assertIn("Visible differences from the golden screenshot", instructions)
        self.assertIn("unless the difference is explicitly listed under oracle.rubric.ignore", instructions)
        self.assertIn("Do not cite successful semantic phases as positive evidence", instructions)

    def test_visual_prompt_does_not_overstate_empty_semantic_checks(self) -> None:
        spec_path = ROOT / "specs/jynew/event-flag.yaml"
        spec = load_spec(spec_path)
        oracle = validate_visual_oracle(spec, spec_path, required=True)
        report = _minimal_run_report(benchmark="jynew", profile="JynewProfile")
        report.validation = PhaseResult("success", "no-op", {"checks": []})
        report.smoke_execution = PhaseResult("success", "no-op", {"checks": []})
        report.oracle_result = PhaseResult("success", "no-op", {"checks": []})

        prompt = build_visual_prompt(spec=spec, oracle=oracle, report=report)
        prompt_data = json.loads(prompt)
        instructions = " ".join(prompt_data["instructions"])

        self.assertIn("no selected checks", instructions)
        self.assertIn("do not cite them as confirmation", instructions)
        self.assertEqual(prompt_data["semantic_phase_evidence"]["visual_relevant_check_count"], 0)

    def test_visual_judge_defaults_to_modelscope(self) -> None:
        with patch.dict(os.environ, {"MODELSCOPE_API_KEY": "modelscope-key"}, clear=True):
            client = VisualJudgeClient.from_env()
        self.assertEqual(client.model, DEFAULT_VISUAL_JUDGE_MODEL)
        self.assertEqual(client.base_url, DEFAULT_VISUAL_JUDGE_BASE_URL)

    def test_visual_judge_explicit_env_overrides_modelscope_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MODELSCOPE_API_KEY": "modelscope-key",
                "WARPTEST_VISUAL_JUDGE_API_KEY": "visual-key",
                "WARPTEST_VISUAL_JUDGE_BASE_URL": "https://judge.example/v1",
                "WARPTEST_VISUAL_JUDGE_MODEL": "judge/model",
            },
            clear=True,
        ):
            client = VisualJudgeClient.from_env()
        self.assertEqual(client.model, "judge/model")
        self.assertEqual(client.base_url, "https://judge.example/v1")

    def test_visual_judge_relay_tag_routes_key_url_and_default_headers(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "RELAY_API_KEY": "relay-key",
                    "RELAY_BASE_URL": "https://relay.example/v1",
                    "RELAY_END_USER": "researcher-1",
                    "WARPTEST_VISUAL_JUDGE_MODEL": "qwen3-vl-flash@relay",
                },
                clear=True,
            ),
            patch("openai.OpenAI") as openai_cls,
        ):
            client = VisualJudgeClient.from_env()

        self.assertEqual(client.model, "qwen3-vl-flash")
        self.assertEqual(client.base_url, "https://relay.example/v1")
        kwargs = openai_cls.call_args.kwargs
        self.assertEqual(kwargs["api_key"], "relay-key")
        self.assertEqual(kwargs["default_headers"], {"X-End-User": "researcher-1"})

    def test_visual_judge_client_sets_timeout_and_retry_defaults(self) -> None:
        with patch("openai.OpenAI") as openai_cls:
            VisualJudgeClient(api_key="visual-key", base_url="https://judge.example/v1", model="judge/model")

        kwargs = openai_cls.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 300.0)
        # Formal token accounting owns retries explicitly; SDK-hidden retries
        # would create provider attempts with no separate usage record.
        self.assertEqual(kwargs["max_retries"], 0)
        self.assertEqual(kwargs["base_url"], "https://judge.example/v1")

    def test_visual_judge_client_honors_timeout_and_retry_env(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "WARPTEST_VISUAL_JUDGE_TIMEOUT": "42.5",
                    "WARPTEST_VISUAL_JUDGE_MAX_RETRIES": "0",
                },
                clear=False,
            ),
            patch("openai.OpenAI") as openai_cls,
        ):
            VisualJudgeClient(api_key="visual-key", base_url=None, model="judge/model")

        kwargs = openai_cls.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 42.5)
        self.assertEqual(kwargs["max_retries"], 0)
        self.assertNotIn("base_url", kwargs)

    def test_visual_judge_downscales_oversized_image_data_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "openra.png"
            Image.new("RGB", (3024, 1898), color=(16, 80, 32)).save(image_path)

            data_url = image_data_url(image_path)
            sent_size = _data_url_image_size(data_url)
            metadata = visual_judge_image_metadata(image_path)

        self.assertLessEqual(max(sent_size), VISUAL_JUDGE_MAX_IMAGE_SIDE)
        self.assertEqual(sent_size, (2048, 1285))
        self.assertEqual(metadata["original_size"], [3024, 1898])
        self.assertEqual(metadata["sent_size"], [2048, 1285])
        self.assertTrue(metadata["resized"])
        self.assertAlmostEqual(sent_size[0] / sent_size[1], 3024 / 1898, delta=0.001)

    def test_visual_judge_does_not_enlarge_small_image_data_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "small.png"
            Image.new("RGB", (640, 360), color=(32, 64, 96)).save(image_path)

            data_url = image_data_url(image_path)
            sent_size = _data_url_image_size(data_url)
            metadata = visual_judge_image_metadata(image_path)

        self.assertEqual(sent_size, (640, 360))
        self.assertEqual(metadata["original_size"], [640, 360])
        self.assertEqual(metadata["sent_size"], [640, 360])
        self.assertFalse(metadata["resized"])

    def test_visual_judge_compresses_large_ui_png_below_request_byte_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "openra-noise.png"
            image = Image.frombytes("RGB", (1280, 720), os.urandom(1280 * 720 * 3))
            image.save(image_path)
            self.assertGreater(image_path.stat().st_size, VISUAL_JUDGE_MAX_IMAGE_BYTES)

            payload = visual_judge_image_payload(image_path)
            encoded = payload["data_url"].split(",", 1)[1]
            sent = base64.b64decode(encoded)

        self.assertTrue(payload["data_url"].startswith("data:image/jpeg;base64,"))
        self.assertLessEqual(len(sent), VISUAL_JUDGE_MAX_IMAGE_BYTES)
        self.assertEqual(payload["sent_bytes"], len(sent))
        self.assertTrue(payload["byte_limited"])

    def test_visual_judge_image_side_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "openra.png"
            Image.new("RGB", (3024, 1898), color=(16, 80, 32)).save(image_path)
            with patch.dict(os.environ, {"WARPTEST_VISUAL_JUDGE_MAX_IMAGE_SIDE": "1280"}, clear=False):
                data_url = image_data_url(image_path)
                sent_size = _data_url_image_size(data_url)
                metadata = visual_judge_image_metadata(image_path)

        self.assertEqual(sent_size, (1280, 803))
        self.assertEqual(metadata["sent_size"], [1280, 803])
        self.assertTrue(metadata["resized"])

    def test_screenshot_quality_rejects_solid_black_png(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "black.png"
            Image.new("RGB", (640, 360), color=(0, 0, 0)).save(image_path)
            quality = screenshot_quality(image_path)

        self.assertFalse(quality["informative"])
        self.assertEqual(quality["pixel_range"], 0)

    def test_visual_judge_payload_uses_downscaled_images(self) -> None:
        captured: dict[str, object] = {}

        class CompletionRecorder:
            def create(self, **kwargs: object) -> object:
                captured["kwargs"] = kwargs
                message = type(
                    "Message",
                    (),
                    {
                        "content": (
                            '{"label":"correct","confidence":0.9,'
                            '"observed":"gameplay","rationale":"matches","rubric_failures":[]}'
                        )
                    },
                )()
                choice = type("Choice", (), {"message": message, "finish_reason": "stop"})()
                usage = SimpleNamespace(
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                    completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
                )
                return type("Response", (), {"choices": [choice], "usage": usage})()

        fake_client = type("Client", (), {})()
        fake_client.chat = type("Chat", (), {})()
        fake_client.chat.completions = CompletionRecorder()

        with tempfile.TemporaryDirectory() as tmp:
            golden_path = Path(tmp) / "golden.png"
            live_path = Path(tmp) / "live.png"
            Image.new("RGB", (3024, 1898), color=(16, 80, 32)).save(golden_path)
            Image.new("RGB", (3024, 1898), color=(32, 96, 48)).save(live_path)

            verdict = VisualJudgeClient(api_key="unused", base_url=None, model="judge/model", client=fake_client).judge(
                prompt="judge this",
                live_screenshot=live_path,
                golden_image=golden_path,
            )

        self.assertEqual(verdict["label"], "correct")
        kwargs = captured["kwargs"]
        self.assertIsInstance(kwargs, dict)
        messages = kwargs["messages"]
        content = messages[1]["content"]
        image_urls = [item["image_url"]["url"] for item in content if item.get("type") == "image_url"]
        self.assertEqual(len(image_urls), 2)
        self.assertEqual([_data_url_image_size(url) for url in image_urls], [(2048, 1285), (2048, 1285)])

    def test_visual_judge_bills_reasoning_only_empty_response_before_failing(self) -> None:
        usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=64,
            total_tokens=74,
            prompt_tokens_details=SimpleNamespace(cached_tokens=4),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=64),
        )
        message = SimpleNamespace(content=None, reasoning_content="private reasoning")
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="length")],
            usage=usage,
        )
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_kwargs: response),
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            golden_path = Path(tmp) / "golden.png"
            live_path = Path(tmp) / "live.png"
            Image.new("RGB", (64, 64), color=(16, 80, 32)).save(golden_path)
            Image.new("RGB", (64, 64), color=(32, 96, 48)).save(live_path)
            with (
                patch.dict(os.environ, {"WARPTEST_VISUAL_JUDGE_ATTEMPTS": "1"}, clear=False),
                cost.scoped_meter() as meter,
                self.assertRaisesRegex(VisualOracleError, "empty content"),
            ):
                VisualJudgeClient(
                    api_key="unused",
                    base_url=None,
                    model="judge/reasoner",
                    client=fake_client,
                ).judge(
                    prompt="judge this",
                    live_screenshot=live_path,
                    golden_image=golden_path,
                )

        summary = meter.summary()
        self.assertEqual(summary["llm_calls"], 1)
        self.assertEqual(summary["completion_tokens"], 64)
        self.assertEqual(summary["reasoning_tokens"], 64)
        self.assertEqual(summary["cached_prompt_tokens"], 4)
        self.assertEqual(summary["total_tokens"], 74)
        self.assertEqual(summary["by_kind"]["visual_judge"]["calls"], 1)

    def test_visual_judge_missing_usage_is_accounting_incomplete(self) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"label":"correct","confidence":0.9,'
                            '"observed":"gameplay","rationale":"matches",'
                            '"rubric_failures":[]}'
                        )
                    ),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_kwargs: response),
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            golden_path = Path(tmp) / "golden.png"
            live_path = Path(tmp) / "live.png"
            Image.new("RGB", (64, 64)).save(golden_path)
            Image.new("RGB", (64, 64)).save(live_path)
            with (
                patch.dict(os.environ, {"WARPTEST_VISUAL_JUDGE_ATTEMPTS": "1"}, clear=False),
                cost.scoped_meter() as meter,
                self.assertRaises(cost.UsageAccountingError),
            ):
                VisualJudgeClient(
                    api_key="unused",
                    base_url=None,
                    model="judge/no-usage",
                    client=fake_client,
                ).judge(
                    prompt="judge this",
                    live_screenshot=live_path,
                    golden_image=golden_path,
                )
        summary = meter.summary()
        self.assertEqual(summary["llm_calls"], 1)
        self.assertFalse(summary["usage_complete"])
        self.assertEqual(summary["unknown_reasoning_token_calls"], 1)

    def test_failed_visual_judge_call_with_usage_is_counted(self) -> None:
        request_error = RuntimeError("judge provider failure")
        request_error.usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=6,
            total_tokens=16,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=6),
        )

        class FailingCompletions:
            def create(self, **_kwargs: object) -> object:
                raise request_error

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=FailingCompletions())
        )
        with tempfile.TemporaryDirectory() as tmp:
            golden_path = Path(tmp) / "golden.png"
            live_path = Path(tmp) / "live.png"
            Image.new("RGB", (64, 64)).save(golden_path)
            Image.new("RGB", (64, 64)).save(live_path)
            with (
                patch.dict(os.environ, {"WARPTEST_VISUAL_JUDGE_ATTEMPTS": "1"}, clear=False),
                cost.scoped_meter() as meter,
                self.assertRaisesRegex(RuntimeError, "judge provider failure"),
            ):
                VisualJudgeClient(
                    api_key="unused",
                    base_url=None,
                    model="judge/reasoner",
                    client=fake_client,
                ).judge(
                    prompt="judge this",
                    live_screenshot=live_path,
                    golden_image=golden_path,
                )

        summary = meter.summary()
        self.assertTrue(summary["usage_complete"])
        self.assertEqual(summary["by_kind"]["visual_judge"]["calls"], 1)
        self.assertEqual(summary["reasoning_tokens"], 6)

    def test_visual_judge_timeout_without_usage_records_unknown_attempt(self) -> None:
        class FailingCompletions:
            def create(self, **_kwargs: object) -> object:
                raise TimeoutError("judge transport timeout")

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=FailingCompletions())
        )
        with tempfile.TemporaryDirectory() as tmp:
            golden_path = Path(tmp) / "golden.png"
            live_path = Path(tmp) / "live.png"
            Image.new("RGB", (64, 64)).save(golden_path)
            Image.new("RGB", (64, 64)).save(live_path)
            with (
                patch.dict(os.environ, {"WARPTEST_VISUAL_JUDGE_ATTEMPTS": "1"}, clear=False),
                cost.scoped_meter() as meter,
                self.assertRaises(cost.UsageAccountingError),
            ):
                VisualJudgeClient(
                    api_key="unused",
                    base_url=None,
                    model="judge/model",
                    client=fake_client,
                ).judge(
                    prompt="judge this",
                    live_screenshot=live_path,
                    golden_image=golden_path,
                )

        self.assertEqual(len(meter.records), 1)
        self.assertFalse(meter.summary()["usage_complete"])

    def test_visual_judge_wall_clock_timeout_interrupts_hung_request(self) -> None:
        class HangingCompletions:
            def create(self, **_kwargs: object) -> object:
                time.sleep(1.0)
                return SimpleNamespace(choices=[])

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=HangingCompletions()))
        with tempfile.TemporaryDirectory() as tmp:
            golden_path = Path(tmp) / "golden.png"
            live_path = Path(tmp) / "live.png"
            Image.new("RGB", (64, 64), color=(16, 80, 32)).save(golden_path)
            Image.new("RGB", (64, 64), color=(32, 96, 48)).save(live_path)
            with patch.dict(
                os.environ,
                {"WARPTEST_VISUAL_JUDGE_TIMEOUT": "0.01", "WARPTEST_VISUAL_JUDGE_ATTEMPTS": "1"},
                clear=False,
            ):
                client = VisualJudgeClient(api_key="unused", base_url=None, model="judge/model", client=fake_client)

            with cost.scoped_meter() as meter, self.assertRaises(
                cost.UsageAccountingError
            ):
                client.judge(prompt="judge this", live_screenshot=live_path, golden_image=golden_path)

        self.assertEqual(len(meter.records), 1)
        self.assertFalse(meter.summary()["usage_complete"])

    def test_visual_pair_audit_fails_closed_when_judge_errors(self) -> None:
        def write_checker(path: Path, offset: int) -> None:
            image = Image.new("RGB", (256, 256), color=(0, 0, 0))
            for y in range(256):
                for x in range(256):
                    image.putpixel(
                        (x, y),
                        (
                            (x * 7 + offset) % 256,
                            (y * 5 + offset) % 256,
                            (x * y + offset) % 256,
                        ),
                    )
            image.save(path)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = root / "before.png"
            after = root / "after.png"
            golden_before = root / "golden-before.png"
            golden_after = root / "golden-after.png"
            write_checker(before, 0)
            write_checker(after, 0)
            write_checker(golden_before, 1)
            write_checker(golden_after, 1)
            with patch("warptest.visual.VisualJudgeClient.from_env", side_effect=TimeoutError("judge timed out")):
                verdict = run_visual_pair_audit(
                    live_before=before,
                    live_after=after,
                    golden_before=golden_before,
                    golden_after=golden_after,
                    description="state loop test",
                )

        self.assertFalse(verdict["result"])
        self.assertEqual(verdict["status"], "failure")
        self.assertEqual(verdict["before"]["label"], "uncertain")
        self.assertTrue(verdict["before"]["judge_failure"])
        self.assertEqual(verdict["after"]["label"], "not_evaluated")

    def test_visual_pair_audit_exact_normalized_pixels_bypass_llm_judge(
        self,
    ) -> None:
        def checker(mode: str) -> Image.Image:
            image = Image.new(mode, (128, 128))
            for x in range(128):
                for y in range(128):
                    rgb = ((x * 7) % 256, (y * 11) % 256, (x * y) % 256)
                    image.putpixel((x, y), rgb if mode == "RGB" else (*rgb, 255))
            return image

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_before = root / "live-before.png"
            live_after = root / "live-after.png"
            golden_before = root / "golden-before.png"
            golden_after = root / "golden-after.png"
            checker("RGB").save(live_before, compress_level=0)
            checker("RGB").save(live_after, compress_level=0)
            checker("RGBA").save(golden_before, compress_level=9)
            checker("RGBA").save(golden_after, compress_level=9)
            with patch(
                "warptest.visual.VisualJudgeClient.from_env",
                side_effect=AssertionError("exact images must not call the judge"),
            ):
                verdict = run_visual_pair_audit(
                    live_before=live_before,
                    live_after=live_after,
                    golden_before=golden_before,
                    golden_after=golden_after,
                    description="deterministic equality smoke",
                )

        self.assertTrue(verdict["result"])
        self.assertEqual(verdict["status"], "success")
        for stage in ("before", "after"):
            half = verdict[stage]
            self.assertEqual(
                half["verdict_source"], "normalized_image_equality_v1"
            )
            self.assertTrue(half["normalized_image_equality"]["match"])
            self.assertEqual(
                half["normalized_image_equality"]["live"][
                    "normalized_sha256"
                ],
                half["normalized_image_equality"]["golden"][
                    "normalized_sha256"
                ],
            )
            self.assertIsNone(half["judge_model"])

    def test_visual_pair_audit_one_pixel_difference_uses_llm_judge(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = [
                root / name
                for name in ("live-before.png", "live-after.png", "gb.png", "ga.png")
            ]
            for path in paths:
                image = Image.effect_noise((128, 128), 100).convert("RGB")
                image.save(path)
            for live, golden in ((paths[0], paths[2]), (paths[1], paths[3])):
                image = Image.open(live).convert("RGB")
                image.save(golden)
                red, green, blue = image.getpixel((64, 64))
                image.putpixel((64, 64), ((red + 1) % 256, green, blue))
                image.save(live)
            fake = FakeVisualJudgeClient("correct")
            with patch(
                "warptest.visual.VisualJudgeClient.from_env", return_value=fake
            ) as client_factory:
                verdict = run_visual_pair_audit(
                    live_before=paths[0],
                    live_after=paths[1],
                    golden_before=paths[2],
                    golden_after=paths[3],
                    description="one-pixel mismatch smoke",
                    cache_mode="off",
                )

        self.assertTrue(verdict["result"])
        self.assertEqual(client_factory.call_count, 2)
        for stage in ("before", "after"):
            self.assertEqual(verdict[stage]["verdict_source"], "llm_visual_judge")
            self.assertFalse(
                verdict[stage]["normalized_image_equality"]["match"]
            )

    def test_visual_pair_audit_short_circuits_after_when_before_is_incorrect(self) -> None:
        before = {
            "stage": "before",
            "label": "incorrect",
            "confidence": 1.0,
        }
        with patch("warptest.visual._judge_pair_half", return_value=before) as judge:
            verdict = run_visual_pair_audit(
                live_before="before.png",
                live_after="after.png",
                golden_before="gold-before.png",
                golden_after="gold-after.png",
                description="state loop test",
            )

        self.assertEqual(judge.call_count, 1)
        self.assertEqual(judge.call_args.kwargs["stage"], "before")
        self.assertFalse(verdict["result"])
        self.assertFalse(verdict["after_evaluated"])
        self.assertEqual(verdict["after"]["label"], "not_evaluated")

    def test_staged_visual_pair_reuses_before_verdict_for_after(self) -> None:
        before = {"stage": "before", "label": "correct", "confidence": 1.0}
        after = {"stage": "after", "label": "correct", "confidence": 1.0}
        with patch(
            "warptest.visual._judge_pair_half", side_effect=[before, after]
        ) as judge:
            staged = run_visual_pair_audit(
                live_before="before.png",
                live_after=None,
                golden_before="gold-before.png",
                golden_after="gold-after.png",
                description="state loop test",
                evaluate_after=False,
            )
            verdict = run_visual_pair_audit(
                live_before="before.png",
                live_after="after.png",
                golden_before="gold-before.png",
                golden_after="gold-after.png",
                description="state loop test",
                prior_before=staged["before"],
            )

        self.assertEqual(judge.call_count, 2)
        self.assertEqual(
            [call.kwargs["stage"] for call in judge.call_args_list],
            ["before", "after"],
        )
        self.assertTrue(verdict["result"])
        self.assertTrue(verdict["after_evaluated"])

    def test_formal_c1_pair_audit_never_reads_global_verdict_cache(self) -> None:
        def write_checker(path: Path, offset: int) -> None:
            image = Image.new("RGB", (128, 128), color=(20, 40, 80))
            for x in range(128):
                for y in range(128):
                    image.putpixel(
                        (x, y),
                        (
                            (x * 7 + offset) % 256,
                            (y * 11 + offset) % 256,
                            (x * y + offset) % 256,
                        ),
                    )
            image.save(path)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = [root / name for name in ("before.png", "after.png", "gb.png", "ga.png")]
            write_checker(paths[0], 0)
            write_checker(paths[1], 0)
            write_checker(paths[2], 1)
            write_checker(paths[3], 1)
            fake = FakeVisualJudgeClient("correct")
            with (
                patch.dict(os.environ, {"WARPTEST_C1_DISABLE_VISUAL_CACHE": "1"}),
                patch("warptest.visual._read_global_cache", side_effect=AssertionError("cache read")),
                patch("warptest.visual.VisualJudgeClient.from_env", return_value=fake),
            ):
                verdict = run_visual_pair_audit(
                    live_before=paths[0],
                    live_after=paths[1],
                    golden_before=paths[2],
                    golden_after=paths[3],
                    description="formal C1",
                    cache_mode="report+global",
                )

        self.assertTrue(verdict["result"])
        self.assertFalse(verdict["before"]["cache_hit"])
        self.assertEqual(verdict["before"]["cache_policy"], "disabled_for_c1")

    def test_visual_audit_full_only_defaults_to_single_frame(self) -> None:
        previous = os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE")
        os.environ["WARPTEST_VISUAL_JUDGE_FAKE"] = "correct"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                runtime = _FakeVisualRuntime(Path(tmp))
                report = _minimal_run_report(benchmark="2048", profile="Game2048Profile")
                result = run_visual_audit(
                    spec=load_spec(ROOT / "specs/2048/merge-left.yaml"),
                    spec_path=ROOT / "specs/2048/merge-left.yaml",
                    runtime=runtime,
                    report=report,
                    cascade_config=CascadePolicyConfig(cascade_policy="full-only"),
                    oracle_policy="fake-visual",
                    visual_audit_reason="forced",
                )
        finally:
            if previous is None:
                os.environ.pop("WARPTEST_VISUAL_JUDGE_FAKE", None)
            else:
                os.environ["WARPTEST_VISUAL_JUDGE_FAKE"] = previous
        audit = result.data["visual_audit"]
        self.assertEqual(result.status, "success")
        self.assertEqual(runtime.screenshot_names, ["visual-post_smoke.png"])
        self.assertEqual(audit["sample_count"], 1)
        self.assertIsNone(audit["contact_sheet_path"])
        self.assertEqual(audit["final_verdict_source"], "fake")

    def test_visual_audit_conservative_uses_multiframe_contact_sheet_for_browser_runtime(self) -> None:
        previous = os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE")
        os.environ["WARPTEST_VISUAL_JUDGE_FAKE"] = "correct"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                runtime = _FakeVisualRuntime(Path(tmp))
                report = _minimal_run_report(benchmark="2048", profile="Game2048Profile")
                result = run_visual_audit(
                    spec=load_spec(ROOT / "specs/2048/merge-left.yaml"),
                    spec_path=ROOT / "specs/2048/merge-left.yaml",
                    runtime=runtime,
                    report=report,
                    cascade_config=CascadePolicyConfig(cascade_policy="conservative"),
                    oracle_policy="fake-visual",
                    visual_audit_reason="forced",
                )
                contact_sheet_exists = Path(result.data["visual_audit"]["contact_sheet_path"]).exists()
        finally:
            if previous is None:
                os.environ.pop("WARPTEST_VISUAL_JUDGE_FAKE", None)
            else:
                os.environ["WARPTEST_VISUAL_JUDGE_FAKE"] = previous
        audit = result.data["visual_audit"]
        self.assertEqual(audit["sample_count"], 3)
        self.assertEqual(len(audit["frame_paths"]), 3)
        self.assertIsNotNone(audit["contact_sheet_path"])
        self.assertTrue(contact_sheet_exists)

    def test_visual_audit_fake_backend_does_not_write_global_cache(self) -> None:
        previous_fake = os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE")
        previous_cwd = Path.cwd()
        os.environ["WARPTEST_VISUAL_JUDGE_FAKE"] = "correct"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                runtime = _FakeVisualRuntime(Path(tmp) / "report")
                report = _minimal_run_report(benchmark="2048", profile="Game2048Profile")
                result = run_visual_audit(
                    spec=load_spec(ROOT / "specs/2048/merge-left.yaml"),
                    spec_path=ROOT / "specs/2048/merge-left.yaml",
                    runtime=runtime,
                    report=report,
                    cascade_config=CascadePolicyConfig(cascade_policy="full-only", cache_mode="report+global"),
                    oracle_policy="fake-visual",
                    visual_audit_reason="forced",
                )
                self.assertFalse((Path(tmp) / ".warptest-cache").exists())
        finally:
            os.chdir(previous_cwd)
            if previous_fake is None:
                os.environ.pop("WARPTEST_VISUAL_JUDGE_FAKE", None)
            else:
                os.environ["WARPTEST_VISUAL_JUDGE_FAKE"] = previous_fake
        audit = result.data["visual_audit"]
        self.assertFalse(audit["global_cache_written"])
        self.assertEqual(audit["judge_backend"], "fake")

    def test_visual_audit_missing_visual_oracle_fails_when_explicitly_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _FakeVisualRuntime(Path(tmp))
            report = _minimal_run_report(benchmark="2048", profile="Game2048Profile")
            result = run_visual_audit(
                spec={"name": "missing visual"},
                spec_path=ROOT / "specs/2048/merge-left.yaml",
                runtime=runtime,
                report=report,
                cascade_config=CascadePolicyConfig(cascade_policy="full-only"),
                oracle_policy="visual",
                visual_audit_reason="missing_visual_oracle",
                required=True,
            )
        self.assertEqual(result.status, "failure")
        self.assertEqual(result.data["visual_audit"]["audit_decision_reason"], "missing_visual_oracle")

    def test_visual_audit_always_records_the_full_judge_as_the_verdict_source(self) -> None:
        class SingleJudgeClient:
            def judge(self, *, prompt: str, live_screenshot: Path, golden_image: Path) -> dict[str, object]:
                return {
                    "label": "correct",
                    "confidence": 0.99,
                    "observed": "judge ok",
                    "rationale": "matches golden",
                    "rubric_failures": [],
                    "judge_model": "full-model",
                    "latency_ms": 1,
                    "attempts": 1,
                }

        with tempfile.TemporaryDirectory() as tmp, patch("warptest.visual.VisualJudgeClient.from_env", return_value=SingleJudgeClient()):
            runtime = _FakeVisualRuntime(Path(tmp))
            report = _minimal_run_report(benchmark="2048", profile="Game2048Profile")
            result = run_visual_audit(
                spec=load_spec(ROOT / "specs/2048/merge-left.yaml"),
                spec_path=ROOT / "specs/2048/merge-left.yaml",
                runtime=runtime,
                report=report,
                cascade_config=CascadePolicyConfig(cascade_policy="conservative", sample_count=1, cache_mode="off"),
                oracle_policy="visual",
                visual_audit_reason="forced",
            )
        audit = result.data["visual_audit"]
        self.assertEqual(result.status, "success")
        self.assertEqual(audit["final_verdict_source"], "full")
        self.assertEqual(audit["judge_backend"], "full")
        self.assertEqual(audit["full_verdict"]["judge_model"], "full-model")

    def test_visual_judge_requires_key_when_modelscope_key_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(VisualOracleError) as raised:
                VisualJudgeClient.from_env()
        self.assertIn("MODELSCOPE_API_KEY", str(raised.exception))

    def test_model_bench_attaches_canonical_visual_oracle(self) -> None:
        task = ModelBenchTask(slug="2048-merge-left", title="x", benchmark="2048", description="x")
        spec = {"name": "x", "benchmark": "2048", "target": {}, "checkpoint": {}, "actions": [], "assertions": []}
        updated = attach_canonical_visual_oracle(spec, task)
        self.assertIn("visual_oracle", updated)
        self.assertTrue(Path(updated["visual_oracle"]["golden_image"]).is_absolute())

    def test_cohen_kappa_known_matrix(self) -> None:
        self.assertAlmostEqual(
            cohen_kappa(["correct", "correct", "incorrect", "incorrect"], ["correct", "incorrect", "incorrect", "incorrect"]),
            0.5,
        )

    def test_experiment_manifest_is_balanced_and_valid(self) -> None:
        manifest = build_visual_experiment_manifest(50, ROOT / "specs")
        self.assertEqual(len(manifest), 50)
        self.assertEqual(sum(1 for sample in manifest if sample.truth_label == "correct"), 25)
        self.assertEqual(sum(1 for sample in manifest if sample.truth_label == "incorrect"), 25)
        for index, sample in enumerate(manifest, start=1):
            self.assertEqual(sample.sample_id, f"s{index:03d}")
            self.assertEqual(sample.title, f"Sample {index}")
            self.assertFalse(sample.sample_id.startswith(("p", "n")))
            public_text = f"{sample.sample_id} {sample.title}".lower()
            for token in ["positive", "negative", "oracle_mismatch", "wrong_scene_checkpoint", "wrong_action", "wrong_checkpoint"]:
                self.assertNotIn(token, public_text)
            path = Path(sample.spec_path)
            self.assertTrue(path.exists())
            validate_visual_oracle(load_spec(path), path, required=True)

    def test_openra_experiment_manifest_filters_benchmark(self) -> None:
        manifest = build_visual_experiment_manifest(12, ROOT / "specs", benchmark="OpenRA")
        self.assertEqual(len(manifest), 12)
        self.assertEqual(sum(1 for sample in manifest if sample.truth_label == "correct"), 6)
        self.assertEqual(sum(1 for sample in manifest if sample.truth_label == "incorrect"), 6)
        self.assertTrue(
            all(
                str(load_spec(Path(sample.spec_path))["benchmark"]).lower()
                == "openra"
                for sample in manifest
            )
        )
        self.assertTrue(
            all(
                load_spec(Path(sample.spec_path))["target"].get("kind")
                == "openra_map"
                for sample in manifest
            )
        )

    def test_materialized_experiment_spec_uses_absolute_golden_path(self) -> None:
        manifest = build_visual_experiment_manifest(2, ROOT / "specs")
        with tempfile.TemporaryDirectory() as tmp:
            path = materialize_sample_spec(manifest[0], bundled_spec_paths(ROOT / "specs"), Path(tmp))
            oracle = load_spec(path)["visual_oracle"]
        self.assertTrue(Path(oracle["golden_image"]).is_absolute())
        self.assertTrue(Path(oracle["golden_image"]).exists())

    def test_openra_negative_samples_only_swap_oracle(self) -> None:
        manifest = build_visual_experiment_manifest(12, ROOT / "specs", benchmark="OpenRA")
        self.assertEqual({sample.variant for sample in manifest if sample.truth_label == "incorrect"}, {"oracle_mismatch"})
        sample = next(item for item in manifest if item.variant == "oracle_mismatch")
        original = load_spec(Path(sample.spec_path))
        with tempfile.TemporaryDirectory() as tmp:
            path = materialize_sample_spec(sample, bundled_spec_paths(ROOT / "specs", benchmark="OpenRA"), Path(tmp))
            mutated = load_spec(path)
        # The OpenRA catalog intentionally contains both historical ``OpenRA``
        # and canonical ``openra`` spellings.  SystemRandom may select either;
        # oracle mismatch materialization must preserve the source spelling.
        self.assertEqual(mutated["benchmark"], original["benchmark"])
        self.assertEqual(Path(mutated["repo"]), ROOT / "games/OpenRA")
        self.assertEqual((mutated["target"]["mod"], mutated["target"]["map"]), (original["target"]["mod"], original["target"]["map"]))
        self.assertTrue(Path(mutated["visual_oracle"]["golden_image"]).is_absolute())
        original_golden = (Path(sample.spec_path).parent / original["visual_oracle"]["golden_image"]).resolve()
        self.assertNotEqual(Path(mutated["visual_oracle"]["golden_image"]), original_golden)

    def test_luanti_negative_samples_mutate_visible_state(self) -> None:
        all_specs = bundled_spec_paths(ROOT / "specs", benchmark="Luanti")
        cases = [
            ("water-source.yaml", "wrong_action", "actions"),
            ("water-source.yaml", "wrong_checkpoint", "target"),
            ("chest-inventory.yaml", "wrong_checkpoint", "target"),
            ("player-give-stack.yaml", "wrong_action", "actions"),
            ("player-hp.yaml", "wrong_action", "actions"),
        ]
        for filename, variant, changed_key in cases:
            with self.subTest(filename=filename, variant=variant):
                spec_path = ROOT / "specs/luanti" / filename
                original = load_spec(spec_path)
                sample = VisualExperimentSample(
                    sample_id="s999",
                    spec_path=str(spec_path),
                    truth_label="incorrect",
                    variant=variant,
                    title="Sample 999",
                )
                with tempfile.TemporaryDirectory() as tmp:
                    materialized_path = materialize_sample_spec(sample, all_specs, Path(tmp))
                    materialized = load_spec(materialized_path)

                self.assertNotEqual(materialized[changed_key], original[changed_key])
                self.assertEqual(materialized["benchmark"], "luanti")

    def test_luanti_negative_variants_require_visible_mutation(self) -> None:
        all_specs = bundled_spec_paths(ROOT / "specs", benchmark="Luanti")
        storage_variants = _negative_variants_for_spec(ROOT / "specs/luanti/storage-workshop.yaml", all_specs)
        save_restore_variants = _negative_variants_for_spec(ROOT / "specs/luanti/save-restore.yaml", all_specs)
        water_variants = _negative_variants_for_spec(ROOT / "specs/luanti/water-source.yaml", all_specs)

        self.assertNotIn("wrong_action", storage_variants)
        self.assertIn("wrong_checkpoint", storage_variants)
        self.assertNotIn("wrong_action", save_restore_variants)
        self.assertIn("wrong_checkpoint", save_restore_variants)
        self.assertIn("wrong_action", water_variants)
        self.assertIn("wrong_checkpoint", water_variants)

    def test_libreoffice_negative_wrong_action_mutates_active_sheet(self) -> None:
        all_specs = bundled_spec_paths(ROOT / "specs", benchmark="LibreOffice")
        spec_path = ROOT / "specs/libreoffice/calc-named-range-forecast.yaml"
        sample = VisualExperimentSample(
            sample_id="s999",
            spec_path=str(spec_path),
            truth_label="incorrect",
            variant="wrong_action",
            title="Sample 999",
        )

        with tempfile.TemporaryDirectory() as tmp:
            materialized_path = materialize_sample_spec(sample, all_specs, Path(tmp))
            materialized = load_spec(materialized_path)

        actions = materialized["actions"]
        mutation = next(action for action in actions if action.get("value") == "WRONG VISUAL STATE")
        save_index = next(index for index, action in enumerate(actions) if action["type"] == "libreoffice_save")
        mutation_index = actions.index(mutation)
        self.assertEqual(mutation["type"], "libreoffice_set_cell")
        self.assertEqual(mutation["sheet"], "Forecast")
        self.assertEqual(mutation["cell"], "A1")
        self.assertLess(mutation_index, save_index)

    def test_libreoffice_negative_wrong_checkpoint_mutates_active_slide(self) -> None:
        all_specs = bundled_spec_paths(ROOT / "specs", benchmark="LibreOffice")
        spec_path = ROOT / "specs/libreoffice/impress-status-deck-build.yaml"
        sample = VisualExperimentSample(
            sample_id="s999",
            spec_path=str(spec_path),
            truth_label="incorrect",
            variant="wrong_checkpoint",
            title="Sample 999",
        )

        with tempfile.TemporaryDirectory() as tmp:
            materialized_path = materialize_sample_spec(sample, all_specs, Path(tmp))
            materialized = load_spec(materialized_path)

        active_slide = materialized["target"]["slides"][0]
        untouched_slide = load_spec(spec_path)["target"]["slides"][1]
        self.assertEqual(active_slide["title"], "WRONG CHECKPOINT")
        self.assertEqual(active_slide["bullets"], ["This slide intentionally mismatches the oracle."])
        self.assertEqual(materialized["target"]["slides"][1], untouched_slide)

    def test_luanti_visual_oracles_require_specific_scene_evidence(self) -> None:
        specific_tokens = {
            "basin",
            "block",
            "bridge",
            "chest",
            "cobble",
            "column",
            "floor",
            "grove",
            "health",
            "homestead",
            "hotbar",
            "marker",
            "mine",
            "platform",
            "stone",
            "tree",
            "water",
            "workshop",
        }
        for spec_path in sorted((ROOT / "specs/luanti").glob("*.yaml")):
            spec = load_spec(spec_path)
            if "visual_oracle" not in spec:
                continue
            with self.subTest(spec=spec_path.name):
                oracle = validate_visual_oracle(spec, spec_path, required=True)
                must_text = " ".join(oracle["rubric"]["must"]).lower()
                ignore_text = " ".join(oracle["rubric"]["ignore"]).lower()

                self.assertTrue(any(token in must_text for token in specific_tokens), must_text)
                self.assertNotIn("solid blocks are visible in the scene", must_text)
                self.assertNotIn("exact hotbar contents", ignore_text)

    def test_openra_visual_sample_uses_utility_checkpoint_for_production_spec(self) -> None:
        sample = VisualExperimentSample(
            sample_id="s999",
            spec_path=str(ROOT / "specs/openra/ra-soviet05-war-factory-heavy-tank.yaml"),
            truth_label="correct",
            variant="normal",
            title="Sample 999",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = materialize_sample_spec(sample, bundled_spec_paths(ROOT / "specs", benchmark="OpenRA"), Path(tmp))
            materialized = load_spec(path)

        self.assertEqual(materialized["checkpoint"]["allowed_strategies"], ["openra_utility_map_validate"])
        self.assertEqual(materialized["actions"], [{"type": "openra_validate_map", "mod": "ra", "map": "soviet-05"}])
        self.assertEqual(
            materialized["assertions"],
            [
                {"type": "openra_map_loaded", "mod": "ra", "map": "soviet-05"},
                {"type": "no_openra_utility_errors"},
            ],
        )

    def test_openra_screenshot_launches_real_gameplay_capture(self) -> None:
        captured: dict[str, list[str]] = {}

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            screenshot_path = Path(kwargs["target"])
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            screenshot_path.write_bytes(bytes(range(256)) * 10)
            return subprocess.CompletedProcess(cmd, 0, "launched", "")

        with tempfile.TemporaryDirectory() as tmp:
            runtime = OpenRARuntimeAdapter(ROOT / "games/OpenRA", Path(tmp))
            runtime._last_target = {"mod": "cnc", "map": "gdi01"}
            with (
                patch.object(runtime, "_ensure_utility_built"),
                patch.object(runtime, "_ensure_openra_content"),
                patch("warptest.openra_runtime._run_until_informative_file_with_process_group", side_effect=fake_run),
            ):
                screenshot = Path(runtime.screenshot("visual-post_smoke.png"))
                self.assertTrue(screenshot.exists())

        command = captured["cmd"]
        self.assertEqual(command[0], "./launch-game.sh")
        self.assertIn("Game.Mod=cnc", command)
        self.assertIn("Launch.Map=gdi01", command)
        self.assertTrue(any(item.startswith("Launch.WarptestScreenshotPath=") for item in command))
        self.assertFalse(any("map.png" in item for item in command))

    def test_openra_gameplay_checkpoint_captures_post_action_screenshot(self) -> None:
        captured: dict[str, list[str]] = {}

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            # The engine writes the report and the deferred post-action screenshot in the
            # same process that ran the gameplay probe.
            for item in cmd:
                if item.startswith("Launch.WarptestGameplayReportPath="):
                    report_path = Path(item.split("=", 1)[1])
                    report_path.parent.mkdir(parents=True, exist_ok=True)
                    report_path.write_text('{"status": "success", "checks": []}', encoding="utf-8")
                if item.startswith("Launch.WarptestScreenshotPath="):
                    shot_path = Path(item.split("=", 1)[1])
                    shot_path.parent.mkdir(parents=True, exist_ok=True)
                    shot_path.write_bytes(bytes(range(256)) * 10)
            return subprocess.CompletedProcess(cmd, 0, "launched", "")

        spec = {
            "target": {"mod": "ra", "map": "soviet-05"},
            "checkpoint": {"allowed_strategies": ["openra_gameplay_production_validate"]},
            "actions": [{"type": "openra_wait_ticks", "ticks": 1}],
        }

        with tempfile.TemporaryDirectory() as tmp:
            runtime = OpenRARuntimeAdapter(ROOT / "games/OpenRA", Path(tmp))
            with (
                patch.object(runtime, "_ensure_utility_built"),
                patch.object(runtime, "_ensure_openra_content"),
                patch("warptest.openra_runtime._run_with_process_group", side_effect=fake_run),
            ):
                result = runtime.run_checkpoint(spec, Path("gameplay.yaml"))
                self.assertEqual(result["status"], "success")
                # The gameplay launch carries the deferred screenshot arguments.
                command = captured["cmd"]
                self.assertTrue(any(item.startswith("Launch.WarptestGameplayRequestPath=") for item in command))
                self.assertTrue(any(item.startswith("Launch.WarptestScreenshotPath=") for item in command))
                self.assertIn("Launch.WarptestExitAfterScreenshot=true", command)
                self.assertTrue(any(item.startswith("Launch.WarptestScreenshotFrame=") for item in command))

                # screenshot() must return the in-process frame WITHOUT a cold relaunch.
                with patch.object(
                    runtime, "_capture_gameplay_screenshot", side_effect=AssertionError("cold relaunch")
                ):
                    shot = Path(runtime.screenshot("visual-post_smoke.png"))
                self.assertTrue(shot.exists())
                self.assertEqual(shot.name, "visual-post_smoke.png")

    def test_openra_screenshot_falls_back_to_cold_launch_without_gameplay_frame(self) -> None:
        called: dict[str, bool] = {"cold": False}

        def fake_capture(mod: str, map_id: str, target: Path) -> None:
            called["cold"] = True
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bytes(range(256)) * 10)

        with tempfile.TemporaryDirectory() as tmp:
            runtime = OpenRARuntimeAdapter(ROOT / "games/OpenRA", Path(tmp))
            runtime._last_target = {"mod": "cnc", "map": "gdi01"}
            runtime._gameplay_screenshot_path = None
            with patch.object(runtime, "_capture_gameplay_screenshot", side_effect=fake_capture):
                shot = Path(runtime.screenshot("visual-post_smoke.png"))
            self.assertTrue(called["cold"])
            self.assertTrue(shot.exists())

    def test_experiment_metrics(self) -> None:
        rows = [
            {"truth_label": "correct", "human_label": "correct", "llm_label": "correct"},
            {"truth_label": "incorrect", "human_label": "incorrect", "llm_label": "uncertain"},
        ]
        metrics = experiment_metrics(rows)
        self.assertEqual(metrics["rated"], 2)
        self.assertEqual(metrics["coverage_excluding_uncertain"], 0.5)
        self.assertEqual(metrics["strict_llm_accuracy"], 0.5)

    def test_run_spec_with_fake_visual_judge(self) -> None:
        previous = os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE")
        os.environ["WARPTEST_VISUAL_JUDGE_FAKE"] = "correct"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                report = run_spec(
                    ROOT / "specs/2048/merge-left.yaml",
                    repo_arg=str(ROOT / "games/2048"),
                    report_dir=Path(tmp),
                    headless=True,
                    visual_required=True,
                )
        finally:
            if previous is None:
                os.environ.pop("WARPTEST_VISUAL_JUDGE_FAKE", None)
            else:
                os.environ["WARPTEST_VISUAL_JUDGE_FAKE"] = previous
        self.assertEqual(report.visual_oracle_result.status, "success")
        self.assertEqual(report.overall_status, "success")

    def test_run_openra_spec_with_fake_visual_judge(self) -> None:
        def fake_screenshot(runtime: OpenRARuntimeAdapter, name: str = "openra-gameplay.png", full_page: bool = False) -> str:
            path = runtime.out_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / "specs/openra/visual/cnc-gdi01-beachhead.golden.png").read_bytes())
            runtime._last_screenshot_path = path
            return str(path)

        previous = os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE")
        os.environ["WARPTEST_VISUAL_JUDGE_FAKE"] = "correct"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with patch.object(OpenRARuntimeAdapter, "screenshot", fake_screenshot):
                    report = run_spec(
                        ROOT / "specs/openra/cnc-gdi01-beachhead.yaml",
                        repo_arg=str(ROOT / "games/OpenRA"),
                        report_dir=Path(tmp),
                        headless=True,
                        visual_required=True,
                    )
        finally:
            if previous is None:
                os.environ.pop("WARPTEST_VISUAL_JUDGE_FAKE", None)
            else:
                os.environ["WARPTEST_VISUAL_JUDGE_FAKE"] = previous
        self.assertEqual(report.visual_oracle_result.status, "success")
        self.assertEqual(report.overall_status, "success")
        self.assertTrue(any(Path(path).name == "visual-post_smoke.png" for path in report.screenshots))

    def test_openra_screenshot_process_stops_after_informative_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            target = tmp_path / "shot.png"
            script = tmp_path / "write_shot_then_sleep.py"
            script.write_text(
                "\n".join(
                    [
                        "import shutil, sys, time",
                        "shutil.copyfile(sys.argv[1], sys.argv[2])",
                        "time.sleep(60)",
                    ]
                ),
                encoding="utf-8",
            )

            completed = _run_until_informative_file_with_process_group(
                [
                    sys.executable,
                    str(script),
                    str(ROOT / "specs/openra/visual/cnc-gdi01-beachhead.golden.png"),
                    str(target),
                ],
                cwd=tmp_path,
                timeout=10,
                env=os.environ.copy(),
                target=target,
            )
            target_exists = target.exists()

        self.assertEqual(completed.returncode, 0)
        self.assertIn("write_shot_then_sleep.py", " ".join(completed.args))
        self.assertTrue(target_exists)

    def test_jynew_visual_audit_marks_capture_exception_as_failure(self) -> None:
        class FailingJynewRuntime(_FakeVisualRuntime):
            @property
            def last_screenshot_metadata(self) -> dict[str, object]:
                return {
                    "live_source": "capture_failure",
                    "screenshot_status": "failure",
                    "screenshot_source": "capture_failure",
                    "screenshot_detail": "camera was blank",
                }

            def screenshot(self, name: str, full_page: bool = False) -> str:
                raise RuntimeError("camera was blank")

        spec_path = ROOT / "specs/jynew/level-up.yaml"
        spec = load_spec(spec_path)
        report = _minimal_run_report(benchmark="jynew", profile="JynewProfile")
        with tempfile.TemporaryDirectory() as tmp:
            runtime = FailingJynewRuntime(Path(tmp))
            result = run_visual_audit(
                spec=spec,
                spec_path=spec_path,
                runtime=runtime,
                report=report,
                required=True,
            )
        self.assertEqual(result.status, "failure")
        self.assertTrue(result.data["capture_failure"])
        self.assertEqual(result.data["live_source"], "capture_failure")
        self.assertNotIn("live_screenshot", result.data)

    def test_visual_experiment_navigation_and_redo_overwrites_rating(self) -> None:
        previous = os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE")
        os.environ["WARPTEST_VISUAL_JUDGE_FAKE"] = "correct"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                session = VisualExperimentSession(sample_count=2, headed=False, out_dir=Path(tmp))
                run_state = session.run_current()
                self.assertEqual(run_state["index"], 0)
                self.assertEqual(run_state["current_sample"], {"sample_id": "s001", "title": "Sample 1"})
                self.assertEqual(run_state["metrics"], {"rated": 0})
                self.assertNotIn("report_dir", run_state)
                self.assertNotIn("llm_label", run_state["current_run"])
                _assert_blind_state(self, run_state)

                next_before_rating = session.next_sample()
                self.assertEqual(next_before_rating["index"], 1)
                previous_before_rating = session.previous_sample()
                self.assertEqual(previous_before_rating["index"], 0)
                self.assertNotIn("llm_label", previous_before_rating["current_run"])
                _assert_blind_state(self, previous_before_rating)

                rated = session.rate_current("correct")
                self.assertEqual(rated["index"], 0)
                self.assertEqual(rated["rated"], 1)
                self.assertEqual(rated["metrics"], {"rated": 1})
                self.assertEqual(rated["current_run"]["human_label"], "correct")
                self.assertEqual(rated["current_run"]["llm_label"], "correct")
                _assert_blind_state(self, rated)

                next_state = session.next_sample()
                self.assertEqual(next_state["index"], 1)
                self.assertEqual(next_state["current_sample"], {"sample_id": "s002", "title": "Sample 2"})
                _assert_blind_state(self, next_state)
                previous_state = session.previous_sample()
                self.assertEqual(previous_state["index"], 0)
                self.assertEqual(previous_state["current_run"]["human_label"], "correct")
                self.assertEqual(previous_state["current_run"]["llm_label"], "correct")
                _assert_blind_state(self, previous_state)

                redone = session.rate_current("incorrect")
                self.assertEqual(redone["rated"], 1)
                self.assertEqual(redone["current_run"]["human_label"], "incorrect")
                _assert_blind_state(self, redone)
                ratings = load_spec(Path(tmp) / "human-ratings.json")["ratings"]
                experiment = load_spec(Path(tmp) / "visual-experiment.json")
                manifest_report = json.loads((Path(tmp) / "visual-experiment-manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(len(ratings), 1)
                self.assertEqual(ratings[0]["human_label"], "incorrect")
                self.assertIn("truth_label", ratings[0])
                self.assertIn("variant", ratings[0])
                self.assertIn("truth_label", experiment["samples"][0])
                self.assertIn("variant", experiment["samples"][0])
                self.assertIn("truth_label", manifest_report["samples"][0])
                self.assertIn("variant", manifest_report["samples"][0])
                self.assertEqual(experiment["positioning"]["paper_role"], "auxiliary_appendix")
                self.assertFalse(experiment["positioning"]["primary_claim"])
        finally:
            if previous is None:
                os.environ.pop("WARPTEST_VISUAL_JUDGE_FAKE", None)
            else:
                os.environ["WARPTEST_VISUAL_JUDGE_FAKE"] = previous

    def test_visual_experiment_materializes_pair_golden_paths_absolute(self) -> None:
        spec_path = ROOT / "specs/libreoffice/calc-named-range-forecast.yaml"
        sample = VisualExperimentSample(
            sample_id="s001",
            spec_path=str(spec_path),
            truth_label="correct",
            variant="normal",
            title="Sample 1",
        )
        with tempfile.TemporaryDirectory() as tmp:
            materialized = materialize_sample_spec(sample, [spec_path], Path(tmp))
            oracle = load_spec(materialized)["visual_oracle"]

        for key in ("golden_image", "golden_before", "golden_after"):
            self.assertTrue(Path(oracle[key]).is_absolute())
            self.assertTrue(Path(oracle[key]).exists())

    def test_visual_experiment_rates_negative_pair_verdict(self) -> None:
        report = _minimal_run_report()
        report.overall_status = "failure"
        report.visual_oracle_result = PhaseResult(
            "failure",
            "Visual judge labeled before/after pair as failure.",
            {
                "pair_verdict": {
                    "result": False,
                    "before": {"label": "correct", "confidence": 0.9, "rationale": "Before is correct."},
                    "after": {"label": "incorrect", "confidence": 0.8, "rationale": "After is wrong."},
                },
                "live_before": "before.png",
                "live_after": "after.png",
                "golden_before": "golden-before.png",
                "golden_after": "golden-after.png",
                "live_source": "libreoffice_desktop_screenshot",
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            session = VisualExperimentSession(sample_count=2, headed=False, out_dir=Path(tmp), benchmark="OpenRA")
            with patch("warptest.visual_experiment.run_spec", return_value=report):
                session.run_current()
            rated = session.rate_current("incorrect")

        run = rated["current_run"]
        self.assertEqual(run["human_label"], "incorrect")
        self.assertEqual(run["llm_label"], "incorrect")
        self.assertEqual(run["live_screenshot"], "after.png")
        self.assertNotIn("run_status", run)

    def test_visual_experiment_public_state_reports_run_failure_without_blind_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = VisualExperimentSession(sample_count=2, headed=False, out_dir=Path(tmp))
            with patch("warptest.visual_experiment.run_spec", side_effect=RuntimeError("browser timed out in wrong_checkpoint")):
                state = session.run_current()

            run = state["current_run"]
            self.assertEqual(run["sample_id"], "s001")
            self.assertEqual(run["run_status"], "failed")
            self.assertIn("browser timed out", run["run_error"])
            self.assertNotIn("wrong_checkpoint", run["run_error"])
            self.assertNotIn("llm_label", run)
            _assert_blind_state(self, state)

    def test_visual_experiment_audits_after_semantic_failure(self) -> None:
        report = _minimal_run_report()
        with tempfile.TemporaryDirectory() as tmp:
            session = VisualExperimentSession(sample_count=2, headed=False, out_dir=Path(tmp), benchmark="OpenRA")
            with patch("warptest.visual_experiment.run_spec", return_value=report) as run_mock:
                session.run_current()

        self.assertTrue(run_mock.call_args.kwargs["visual_required"])
        self.assertTrue(run_mock.call_args.kwargs["visual_on_semantic_failure"])

    def test_visual_experiment_capture_failure_cannot_be_rated(self) -> None:
        report = _minimal_run_report(benchmark="jynew", profile="JynewProfile")
        report.visual_oracle_result = PhaseResult(
            "failure",
            "Visual capture failed before visual scoring.",
            {
                "live_source": "capture_failure",
                "capture_failure": True,
                "golden_image": str(ROOT / "specs/jynew/visual/level-up.golden.png"),
            },
        )
        report.overall_status = "failure"
        with tempfile.TemporaryDirectory() as tmp:
            session = VisualExperimentSession(sample_count=2, headed=False, out_dir=Path(tmp), benchmark="jynew")
            with patch("warptest.visual_experiment.run_spec", return_value=report):
                state = session.run_current()

            run = state["current_run"]
            self.assertEqual(run["run_status"], "failed")
            self.assertIn("Capture failed", run["run_error"])
            self.assertNotIn("live_screenshot", run)
            with self.assertRaisesRegex(ValueError, "cannot be rated"):
                session.rate_current("correct")
            ratings = load_spec(Path(tmp) / "human-ratings.json")
            self.assertEqual(ratings["ratings"], [])
            experiment = load_spec(Path(tmp) / "visual-experiment.json")
            sample_row = experiment["samples"][0]
            self.assertEqual(sample_row["run_status"], "failed")
            self.assertEqual(sample_row["run_error"], "Capture failed before visual scoring.")
            self.assertEqual(sample_row["live_source"], "capture_failure")
            self.assertTrue(sample_row["capture_failure"])
            self.assertNotIn("live_screenshot", sample_row)
            self.assertEqual(experiment["metrics"]["rated"], 0)

    def test_visual_experiment_completion_alert_helpers_are_failure_tolerant(self) -> None:
        self.assertIn("function unlockCompletionAudio()", _HTML)
        self.assertIn("window.AudioContext || window.webkitAudioContext", _HTML)
        self.assertIn("function playCompletionChime()", _HTML)
        self.assertIn("ctx.createOscillator()", _HTML)
        self.assertIn("oscillator.stop(start + 0.18)", _HTML)
        self.assertIn("function requestCompletionNotificationPermission()", _HTML)
        self.assertIn("const permission = Notification.requestPermission();", _HTML)
        self.assertIn('permission.catch(() => "denied")', _HTML)
        self.assertIn("Promise.resolve(Notification.permission)", _HTML)
        self.assertIn("function prepareCompletionAlerts()", _HTML)

    def test_visual_experiment_completion_alerts_run_success_only(self) -> None:
        successful_fn = _extract_html_function("successfulScreenshotState")
        should_notify_fn = _extract_html_function("shouldNotifyCompletion")
        run_sample_fn = _extract_html_function("runSample")

        self.assertIn("!!run.live_screenshot", successful_fn)
        self.assertIn('run.run_status !== "failed"', successful_fn)
        self.assertIn("!run.run_error", successful_fn)

        self.assertIn("if (!successfulScreenshotState(state)) return false;", should_notify_fn)
        self.assertIn("if (force) return true;", should_notify_fn)
        self.assertIn("if (!successfulScreenshotState(beforeState)) return true;", should_notify_fn)
        self.assertIn("beforeSample.sample_id !== sample.sample_id", should_notify_fn)
        self.assertIn("beforeRun.live_screenshot !== run.live_screenshot", should_notify_fn)

        self.assertIn("const beforeState = lastRenderedState;", run_sample_fn)
        self.assertIn('const state = await api(force ? "/rerun" : "/run", { method: "POST" });', run_sample_fn)
        self.assertIn("const shouldNotify = shouldNotifyCompletion(beforeState, state, force);", run_sample_fn)
        self.assertIn("if (shouldNotify) notifyCompletion(state);", run_sample_fn)
        self.assertEqual(_HTML.count("notifyCompletion(state);"), 1)
        self.assertNotIn('api("/state").then(async state => {\n      notifyCompletion', _HTML)

    def test_visual_experiment_completion_notification_copy_is_blind(self) -> None:
        notify_fn = _extract_html_function("notifyCompletion")

        self.assertIn('new Notification("WarpTest sample ready"', notify_fn)
        self.assertIn("state.current_sample", notify_fn)
        self.assertIn("sample.title", notify_fn)
        self.assertIn("sample.sample_id", notify_fn)
        self.assertNotIn("truth_label", notify_fn)
        self.assertNotIn("variant", notify_fn)
        self.assertNotIn("llm_label", notify_fn)
        self.assertNotIn("llm_rationale", notify_fn)
        self.assertNotIn("rationale", notify_fn)

    def test_jynew_experiment_manifest_filters_benchmark(self) -> None:
        manifest = build_visual_experiment_manifest(12, ROOT / "specs", benchmark="jynew")
        self.assertEqual(len(manifest), 12)
        self.assertEqual(sum(1 for s in manifest if s.truth_label == "correct"), 6)
        self.assertEqual(sum(1 for s in manifest if s.truth_label == "incorrect"), 6)
        negative_variants = {s.variant for s in manifest if s.truth_label == "incorrect"}
        self.assertLessEqual(negative_variants, {"oracle_mismatch", "wrong_scene_checkpoint"})
        self.assertNotIn("wrong_action", negative_variants)
        self.assertNotIn("wrong_checkpoint", negative_variants)
        for sample in manifest:
            spec = load_spec(Path(sample.spec_path))
            self.assertEqual(spec["benchmark"], "jynew")
            self.assertIn("visual_oracle", spec)

    def test_jynew_visual_oracles_expose_scene_keys(self) -> None:
        for spec_path in sorted((ROOT / "specs/jynew").glob("*.yaml")):
            with self.subTest(spec=spec_path.name):
                oracle = validate_visual_oracle(load_spec(spec_path), spec_path, required=True)
                self.assertIn(oracle["scene_key"], {"jynew_default_world", "jynew_world_map", "jynew_submap"})

    def test_jynew_visual_oracle_scene_keys_match_targets(self) -> None:
        for spec_path in sorted((ROOT / "specs/jynew").glob("*.yaml")):
            with self.subTest(spec=spec_path.name):
                spec = load_spec(spec_path)
                oracle = validate_visual_oracle(spec, spec_path, required=True)
                self.assertEqual(_target_scene_key(spec.get("target", {})), oracle["scene_key"])

    def test_jynew_oracle_mismatch_uses_distinct_scene_and_golden(self) -> None:
        manifest = build_visual_experiment_manifest(20, ROOT / "specs", benchmark="jynew")
        samples = [item for item in manifest if item.variant == "oracle_mismatch"]
        self.assertTrue(samples)
        all_specs = bundled_spec_paths(ROOT / "specs", benchmark="jynew")
        for sample in samples:
            original_path = Path(sample.spec_path)
            original = load_spec(original_path)
            with tempfile.TemporaryDirectory() as tmp:
                path = materialize_sample_spec(sample, all_specs, Path(tmp))
                mutated = load_spec(path)
            self.assertEqual(mutated["benchmark"], "jynew")
            self.assertNotEqual(_oracle_scene_key(mutated), _oracle_scene_key(original))
            self.assertNotEqual(_oracle_golden_hash(mutated, path), _oracle_golden_hash(original, original_path))

    def test_jynew_wrong_scene_checkpoint_mutates_visible_scene(self) -> None:
        manifest = build_visual_experiment_manifest(20, ROOT / "specs", benchmark="jynew")
        samples = [item for item in manifest if item.variant == "wrong_scene_checkpoint"]
        self.assertTrue(samples)
        all_specs = bundled_spec_paths(ROOT / "specs", benchmark="jynew")
        for sample in samples:
            original_path = Path(sample.spec_path)
            original = load_spec(original_path)
            with tempfile.TemporaryDirectory() as tmp:
                path = materialize_sample_spec(sample, all_specs, Path(tmp))
                mutated = load_spec(path)
            self.assertEqual(mutated["benchmark"], "jynew")
            self.assertEqual(_resolve_oracle_golden(mutated, path), _resolve_oracle_golden(original, original_path))
            self.assertNotEqual(_target_scene_key(mutated.get("target", {})), _oracle_scene_key(original))
            self.assertTrue(Path(mutated["visual_oracle"]["golden_image"]).is_absolute())
            fallback = Path(mutated["visual_oracle"]["live_fallback_image"])
            self.assertTrue(fallback.is_absolute())
            self.assertTrue(fallback.exists())
            self.assertNotEqual(fallback, _resolve_oracle_golden(original, original_path))

    def test_jynew_visual_samples_do_not_exercise_semantic_actions(self) -> None:
        manifest = build_visual_experiment_manifest(20, ROOT / "specs", benchmark="jynew")
        all_specs = bundled_spec_paths(ROOT / "specs", benchmark="jynew")
        for sample in manifest:
            with tempfile.TemporaryDirectory() as tmp:
                path = materialize_sample_spec(sample, all_specs, Path(tmp))
                spec = load_spec(path)
            self.assertEqual(spec["benchmark"], "jynew")
            self.assertEqual(spec["actions"], [])
            self.assertEqual(spec["assertions"], [])
            target = spec.get("target", {})
            self.assertIsInstance(target, dict)
            self.assertNotIn("validations", target)

    def test_jynew_normal_samples_use_matching_scene_fallback(self) -> None:
        manifest = build_visual_experiment_manifest(20, ROOT / "specs", benchmark="jynew")
        all_specs = bundled_spec_paths(ROOT / "specs", benchmark="jynew")
        normal_samples = [item for item in manifest if item.variant == "normal"]
        self.assertTrue(normal_samples)
        for sample in normal_samples:
            with tempfile.TemporaryDirectory() as tmp:
                path = materialize_sample_spec(sample, all_specs, Path(tmp))
                spec = load_spec(path)
            oracle = spec["visual_oracle"]
            self.assertEqual(_target_scene_key(spec.get("target", {})), oracle["scene_key"])
            self.assertEqual(Path(oracle["live_fallback_image"]), _resolve_oracle_golden(spec, path))

    def test_jynew_runtime_keeps_live_fallback_diagnostic_when_capture_fails(self) -> None:
        spec_path = ROOT / "specs/jynew/level-up.yaml"
        spec = load_spec(spec_path)
        fallback_path = (ROOT / "specs/jynew/visual/team-join.golden.png").resolve()
        spec["visual_oracle"]["live_fallback_image"] = str(fallback_path)

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            report_path = Path(cmd[cmd.index("--warptest-report") + 1])
            report_path.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "screenshot_path": str(report_path.parent / "missing-unity-screenshot.png"),
                        "screenshot_status": "failure",
                        "screenshot_source": "capture_failure",
                        "screenshot_detail": "camera was blank",
                        "checks": [
                            {"name": "target.state_synthesized", "status": "success", "detail": "ok"},
                            {"name": "action[jynew_level_up]", "status": "success", "detail": "ok"},
                            {"name": "assertion[no_jynew_utility_errors]", "status": "success", "detail": "ok"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            runtime = JynewRuntimeAdapter(ROOT / "games/jynew", Path(tmp), visual_capture=True)
            with patch("warptest.jynew_runtime.subprocess.run", side_effect=fake_run):
                result = runtime.run_checkpoint(spec, spec_path)
                self.assertEqual(result["status"], "success")
                self.assertEqual(result["live_source"], "capture_failure")
                diagnostic = Path(runtime.last_screenshot_metadata["fallback_image"])
                self.assertEqual(diagnostic.read_bytes(), fallback_path.read_bytes())
                self.assertFalse((Path(tmp) / "visual-post_smoke.png").exists())
                with self.assertRaisesRegex(RuntimeError, "Jynew capture failure"):
                    runtime.screenshot("visual-post_smoke.png")

    def test_jynew_runtime_accepts_only_explicit_unity_capture(self) -> None:
        spec_path = ROOT / "specs/jynew/level-up.yaml"
        spec = load_spec(spec_path)

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            report_path = Path(cmd[cmd.index("--warptest-report") + 1])
            screenshot_path = report_path.parent / "jynew-visual-post-smoke.png"
            _write_informative_png(screenshot_path, seed=123)
            report_path.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "screenshot_path": str(screenshot_path),
                        "screenshot_status": "success",
                        "screenshot_source": "unity_capture",
                        "screenshot_detail": "camera render captured",
                        "checks": [
                            {"name": "target.state_synthesized", "status": "success", "detail": "ok"},
                            {"name": "action[jynew_level_up]", "status": "success", "detail": "ok"},
                            {"name": "assertion[no_jynew_utility_errors]", "status": "success", "detail": "ok"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            runtime = JynewRuntimeAdapter(ROOT / "games/jynew", Path(tmp), visual_capture=True)
            with patch("warptest.jynew_runtime.subprocess.run", side_effect=fake_run):
                result = runtime.run_checkpoint(spec, spec_path)
                screenshot = Path(runtime.screenshot("visual-post_smoke.png"))
                self.assertEqual(result["live_source"], "unity_capture")
                self.assertTrue(screenshot_quality(screenshot)["informative"])

    def test_jynew_visual_capture_uses_batchmode_by_default(self) -> None:
        spec_path = ROOT / "specs/jynew/level-up.yaml"
        spec = load_spec(spec_path)
        spec["timeout_ms"] = 900_000
        cmd_seen: list[str] = []
        timeout_seen: list[object] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            cmd_seen.extend(cmd)
            timeout_seen.append(kwargs.get("timeout"))
            report_path = Path(cmd[cmd.index("--warptest-report") + 1])
            screenshot_path = report_path.parent / "jynew-visual-post-smoke.png"
            _write_informative_png(screenshot_path, seed=321)
            report_path.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "screenshot_path": str(screenshot_path),
                        "screenshot_status": "success",
                        "screenshot_source": "unity_capture",
                        "screenshot_detail": "camera render captured",
                        "checks": [{"name": "target.state_synthesized", "status": "success", "detail": "ok"}],
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            runtime = JynewRuntimeAdapter(ROOT / "games/jynew", Path(tmp), visual_capture=True)
            with patch("warptest.jynew_runtime.subprocess.run", side_effect=fake_run):
                result = runtime.run_checkpoint(spec, spec_path)

        self.assertEqual(result["live_source"], "unity_capture")
        self.assertTrue(runtime.headless)
        self.assertIn("-batchmode", cmd_seen)
        self.assertIn("-nographics", cmd_seen)
        self.assertEqual(timeout_seen, [300.0])

    def test_jynew_semantic_run_does_not_capture_declared_visual_oracle(self) -> None:
        spec_path = ROOT / "specs/jynew/level-up.yaml"
        spec = load_spec(spec_path)
        spec["timeout_ms"] = 900_000
        requests_seen: list[dict[str, object]] = []
        timeout_seen: list[object] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            timeout_seen.append(kwargs.get("timeout"))
            request_path = Path(cmd[cmd.index("--warptest-request") + 1])
            requests_seen.append(json.loads(request_path.read_text(encoding="utf-8")))
            report_path = Path(cmd[cmd.index("--warptest-report") + 1])
            report_path.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "checks": [{"name": "target.state_synthesized", "status": "success", "detail": "ok"}],
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            runtime = JynewRuntimeAdapter(ROOT / "games/jynew", Path(tmp), visual_capture=False)
            with patch("warptest.jynew_runtime.subprocess.run", side_effect=fake_run):
                result = runtime.run_checkpoint(spec, spec_path)

        self.assertEqual(result["status"], "success")
        self.assertEqual(requests_seen[0]["screenshot_output_path"], "")
        self.assertEqual(timeout_seen, [900.0])

    def test_jynew_runtime_recovers_fresh_success_report_after_exit_timeout(self) -> None:
        spec_path = ROOT / "specs/jynew/level-up.yaml"
        spec = load_spec(spec_path)

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            report_path = Path(cmd[cmd.index("--warptest-report") + 1])
            request_path = Path(cmd[cmd.index("--warptest-request") + 1])
            self.assertFalse(report_path.exists())
            request = json.loads(request_path.read_text(encoding="utf-8"))
            screenshot_path = Path(request["screenshot_output_path"])
            _write_informative_png(screenshot_path, seed=654)
            report_path.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "evidence_task_id": request["evidence_task_id"],
                        "evidence_seed": request["evidence_seed"],
                        "evidence_stage": request["evidence_stage"],
                        "evidence_benchmark": request["evidence_benchmark"],
                        "screenshot_path": str(screenshot_path),
                        "screenshot_status": "success",
                        "screenshot_source": "unity_capture",
                        "screenshot_detail": "camera render captured",
                        "checks": [
                            {
                                "name": "target.state_synthesized",
                                "status": "success",
                                "detail": "ok",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            raise subprocess.TimeoutExpired(
                cmd,
                kwargs["timeout"],
                output="report written\n",
                stderr="shutdown stalled\n",
            )

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            stale_report = out_dir / "level-up-jynew-result.json"
            stale_report.write_text('{"status":"success"}', encoding="utf-8")
            runtime = JynewRuntimeAdapter(
                ROOT / "games/jynew", out_dir, visual_capture=True
            )
            with patch("warptest.jynew_runtime.subprocess.run", side_effect=fake_run):
                result = runtime.run_checkpoint(
                    spec,
                    spec_path,
                    evidence_task_id="jynew-level-up",
                    evidence_seed=7,
                    evidence_stage="after",
                    evidence_benchmark="jynew",
                )

        self.assertEqual(result["status"], "success")
        self.assertIsNone(result["exit_code"])
        self.assertEqual(
            result["process_exit_status"], "timed_out_after_success_report"
        )
        self.assertTrue(result["report_recovered_after_exit_timeout"])
        self.assertEqual(result["live_source"], "unity_capture")
        self.assertIn(
            "jynew_process_exit_timeout_after_success_report=",
            "\n".join(runtime.logs),
        )

    def test_jynew_runtime_rejects_stale_report_after_exit_timeout(self) -> None:
        spec_path = ROOT / "specs/jynew/level-up.yaml"
        spec = load_spec(spec_path)

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            stale_report = out_dir / "level-up-jynew-result.json"
            stale_report.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "checks": [{"name": "stale", "status": "success"}],
                    }
                ),
                encoding="utf-8",
            )
            runtime = JynewRuntimeAdapter(ROOT / "games/jynew", out_dir)
            timeout = subprocess.TimeoutExpired(["Unity"], 1.0)
            with patch(
                "warptest.jynew_runtime.subprocess.run", side_effect=timeout
            ):
                result = runtime.run_checkpoint(spec, spec_path)

            self.assertFalse(stale_report.exists())

        self.assertEqual(result["status"], "failure")
        self.assertEqual(
            result["process_exit_status"],
            "timed_out_without_valid_success_report",
        )
        self.assertFalse(result["report_recovered_after_exit_timeout"])

    def test_jynew_runtime_rejects_mismatched_timeout_success_report(self) -> None:
        spec_path = ROOT / "specs/jynew/level-up.yaml"
        spec = load_spec(spec_path)

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            report_path = Path(cmd[cmd.index("--warptest-report") + 1])
            request_path = Path(cmd[cmd.index("--warptest-request") + 1])
            request = json.loads(request_path.read_text(encoding="utf-8"))
            report_path.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "evidence_task_id": "wrong-task",
                        "evidence_seed": request["evidence_seed"],
                        "evidence_stage": request["evidence_stage"],
                        "evidence_benchmark": request["evidence_benchmark"],
                        "checks": [
                            {
                                "name": "target.state_synthesized",
                                "status": "success",
                                "detail": "ok",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        with tempfile.TemporaryDirectory() as tmp:
            runtime = JynewRuntimeAdapter(ROOT / "games/jynew", Path(tmp))
            with patch("warptest.jynew_runtime.subprocess.run", side_effect=fake_run):
                result = runtime.run_checkpoint(
                    spec,
                    spec_path,
                    evidence_task_id="jynew-level-up",
                )

        self.assertEqual(result["status"], "failure")
        self.assertIn(
            "evidence_task_id does not match",
            result["utility_report"]["detail"],
        )

    def test_jynew_runtime_rejects_stale_unity_screenshot_without_metadata(self) -> None:
        spec_path = ROOT / "specs/jynew/level-up.yaml"
        spec = load_spec(spec_path)

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            report_path = Path(cmd[cmd.index("--warptest-report") + 1])
            report_path.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "screenshot_path": str(report_path.parent / "jynew-visual-post-smoke.png"),
                        "checks": [
                            {"name": "target.state_synthesized", "status": "success", "detail": "ok"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            stale = Path(tmp) / "jynew-visual-post-smoke.png"
            _write_informative_png(stale, seed=456)
            runtime = JynewRuntimeAdapter(ROOT / "games/jynew", Path(tmp), visual_capture=True)
            with patch("warptest.jynew_runtime.subprocess.run", side_effect=fake_run):
                result = runtime.run_checkpoint(spec, spec_path)
                self.assertEqual(result["live_source"], "capture_failure")
                self.assertFalse(stale.exists())
                with self.assertRaisesRegex(RuntimeError, "Jynew capture failure"):
                    runtime.screenshot("visual-post_smoke.png")

    def test_jynew_runtime_does_not_use_golden_as_implicit_live_fallback(self) -> None:
        spec_path = ROOT / "specs/jynew/level-up.yaml"
        spec = load_spec(spec_path)
        spec["visual_oracle"].pop("live_fallback_image", None)

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            report_path = Path(cmd[cmd.index("--warptest-report") + 1])
            report_path.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "screenshot_path": str(report_path.parent / "missing-unity-screenshot.png"),
                        "checks": [
                            {"name": "target.state_synthesized", "status": "success", "detail": "ok"},
                            {"name": "action[jynew_level_up]", "status": "success", "detail": "ok"},
                            {"name": "assertion[no_jynew_utility_errors]", "status": "success", "detail": "ok"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            runtime = JynewRuntimeAdapter(ROOT / "games/jynew", Path(tmp), visual_capture=True)
            with patch("warptest.jynew_runtime.subprocess.run", side_effect=fake_run):
                result = runtime.run_checkpoint(spec, spec_path)
                self.assertEqual(result["status"], "success")
                with self.assertRaisesRegex(RuntimeError, "Jynew capture failure"):
                    runtime.screenshot("visual-post_smoke.png")

    def test_jynew_negative_specs_are_visually_mismatched(self) -> None:
        manifest = build_visual_experiment_manifest(20, ROOT / "specs", benchmark="jynew")
        all_specs = bundled_spec_paths(ROOT / "specs", benchmark="jynew")
        for sample in [item for item in manifest if item.truth_label == "incorrect"]:
            original_path = Path(sample.spec_path)
            original = load_spec(original_path)
            with tempfile.TemporaryDirectory() as tmp:
                path = materialize_sample_spec(sample, all_specs, Path(tmp))
                mutated = load_spec(path)

            if sample.variant == "oracle_mismatch":
                self.assertNotEqual(_oracle_scene_key(mutated), _oracle_scene_key(original))
                self.assertNotEqual(_oracle_golden_hash(mutated, path), _oracle_golden_hash(original, original_path))
            elif sample.variant == "wrong_scene_checkpoint":
                self.assertEqual(_resolve_oracle_golden(mutated, path), _resolve_oracle_golden(original, original_path))
                self.assertNotEqual(_target_scene_key(mutated.get("target", {})), _oracle_scene_key(original))
            else:
                self.fail(f"Unexpected jynew negative variant: {sample.variant}")


def _data_url_image_size(data_url: str) -> tuple[int, int]:
    _, encoded = data_url.split(",", 1)
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
        return tuple(int(value) for value in image.size)


def _oracle_scene_key(spec: dict[str, object]) -> str:
    oracle = spec.get("visual_oracle")
    if not isinstance(oracle, dict):
        return ""
    return str(oracle.get("scene_key") or "")


def _resolve_oracle_golden(spec: dict[str, object], spec_path: Path) -> Path:
    oracle = spec.get("visual_oracle")
    if not isinstance(oracle, dict):
        raise AssertionError("visual_oracle missing")
    golden = Path(str(oracle["golden_image"])).expanduser()
    if not golden.is_absolute():
        golden = spec_path.parent / golden
    return golden.resolve()


def _oracle_golden_hash(spec: dict[str, object], spec_path: Path) -> str:
    return hashlib.sha256(_resolve_oracle_golden(spec, spec_path).read_bytes()).hexdigest()


def _target_scene_key(target: object) -> str:
    if not isinstance(target, dict):
        return "jynew_default_world"
    map_id = target.get("map_id")
    if map_id == 0:
        return "jynew_world_map"
    if map_id == 1:
        return "jynew_submap"
    return "jynew_default_world"


def _assert_blind_state(testcase: unittest.TestCase, state: dict[str, object]) -> None:
    forbidden_keys = {
        "truth_label",
        "variant",
        "spec_path",
        "run_report_path",
        "run_report_md_path",
        "status",
        "visual_status",
        "semantic_status",
        "error",
    }
    _assert_no_forbidden_keys(testcase, state, forbidden_keys)
    state_text = json.dumps(state, ensure_ascii=False).lower()
    for token in ["positive", "negative", "oracle_mismatch", "wrong_scene_checkpoint", "wrong_action", "wrong_checkpoint"]:
        testcase.assertNotIn(token, state_text)


def _assert_no_forbidden_keys(testcase: unittest.TestCase, value: object, forbidden_keys: set[str]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            testcase.assertNotIn(key, forbidden_keys)
            _assert_no_forbidden_keys(testcase, nested, forbidden_keys)
    elif isinstance(value, list):
        for item in value:
            _assert_no_forbidden_keys(testcase, item, forbidden_keys)


class _FakeVisualPage:
    def wait_for_timeout(self, _ms: int) -> None:
        return None

    def evaluate(self, *_args: object, **_kwargs: object) -> None:
        return None


class _FakeVisualRuntime:
    def __init__(self, report_dir: Path) -> None:
        self.report_dir = report_dir
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.page = _FakeVisualPage()
        self.screenshot_names: list[str] = []

    def screenshot(self, name: str, full_page: bool = False) -> str:
        self.screenshot_names.append(name)
        path = self.report_dir / name
        _write_informative_png(path, seed=len(self.screenshot_names))
        return str(path)


def _write_informative_png(path: Path, *, seed: int) -> None:
    width, height = 320, 240
    rng = random.Random(seed)
    payload = bytes(rng.randrange(256) for _ in range(width * height * 3))
    image = Image.frombytes("RGB", (width, height), payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def _extract_html_function(name: str) -> str:
    start = _HTML.index(f"function {name}")
    brace_start = _HTML.index("{", start)
    depth = 0
    for index in range(brace_start, len(_HTML)):
        if _HTML[index] == "{":
            depth += 1
        elif _HTML[index] == "}":
            depth -= 1
            if depth == 0:
                return _HTML[start : index + 1]
    raise AssertionError(f"Could not find complete function body for {name}")


def _minimal_run_report(benchmark: str = "OpenRA", profile: str = "OpenRAProfile") -> RunReport:
    success = PhaseResult("success")
    return RunReport(
        spec_path="spec.yaml",
        repo_path="repo",
        benchmark=benchmark,
        profile=profile,
        git_commit=None,
        launch_command="",
        app_url=None,
        adapter_layers_used=[],
        detected_capabilities={},
        runtime_access_methods={},
        storage_keys_touched=[],
        hook_availability={},
        instrumentation_mode="in_place_test_gated",
        checkpoint_strategy="openra_utility_map_validate",
        checkpoint_validity_class="replay-reachable",
        target_validated_before_smoke=True,
        checkpoint_synthesis=success,
        checkpoint_restoration=success,
        validation=success,
        smoke_execution=success,
        oracle_result=success,
        visual_oracle_result=PhaseResult("skipped"),
        overall_status="success",
    )


class HybridVisualCalibrationTests(unittest.TestCase):
    def test_formal_selection_covers_ten_profiles_and_keeps_spacerpg_anchor(self) -> None:
        tasks = hybrid_visual.load_c1_tasks(ROOT / "benchmarks/p5/c1_tasks.yaml")
        selected = hybrid_visual.select_formal_tasks(tasks)

        self.assertEqual(len(selected), 40)
        counts: dict[str, int] = {}
        for task in selected:
            counts[task.benchmark] = counts.get(task.benchmark, 0) + 1
            hybrid_visual.validate_task_visual_contract(task)
        self.assertEqual(set(counts), set(hybrid_visual.EXPECTED_BENCHMARKS))
        self.assertEqual(set(counts.values()), {4})
        self.assertIn(
            "spacerpg-playable-flight",
            {task.task_id for task in selected if task.benchmark == "spacerpg"},
        )

    def test_pilot_selection_uses_one_nonformal_visual_spec_per_profile(self) -> None:
        selected = hybrid_visual.select_pilot_tasks(
            ROOT / "benchmarks/p5/c1_tasks.yaml", specs_root=ROOT / "specs"
        )

        self.assertEqual(len(selected), 10)
        self.assertEqual(
            {task.benchmark for task in selected},
            set(hybrid_visual.EXPECTED_BENCHMARKS),
        )
        self.assertTrue(all(task.task_id.startswith("pilot-") for task in selected))

    def test_pilot_reuses_failure_as_explicit_nonpaper_golden_fallback(self) -> None:
        task = hybrid_visual.HybridTask(
            task_id="pilot-jynew-item-use-medicine",
            benchmark="jynew",
            oracle_spec=str((ROOT / "specs/jynew/item-use-medicine.yaml").resolve()),
            horizon_class="pilot",
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            (output / "qualification-audit.json").write_text(
                json.dumps(
                    {
                        "schema": hybrid_visual.PROTOCOL,
                        "schema_version": hybrid_visual.SCHEMA_VERSION,
                        "tasks": [
                            {
                                "task_id": task.task_id,
                                "benchmark": task.benchmark,
                                "eligible": False,
                                "error": "Unity license unavailable",
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            eligible, audit = hybrid_visual.qualify_tasks(
                [task],
                output_dir=output,
                headed=True,
                allow_single_image=True,
            )
            base = hybrid_visual._freeze_pilot_fallback_capture(
                task,
                output_dir=output,
                fallback_reason=audit[0]["fallback_reason"],
            )

            self.assertEqual(eligible, [task])
            self.assertEqual(
                audit[0]["eligibility_mode"],
                "registered_golden_pilot_fallback",
            )
            self.assertFalse(base["reference_receipt"]["paper_evidence"])
            self.assertFalse(
                base["reference_receipt"]["programmatic"]["programmatic_pass"]
            )
            self.assertEqual(
                base["intervention_level"], "post_render_pilot_fallback"
            )
            reused = hybrid_visual._pilot_base_capture_reusable(
                task, output / "base-captures" / task.task_id
            )
            self.assertIsNotNone(reused)
            self.assertEqual(reused["task_id"], task.task_id)
            base["pilot_wrong_state_donor"] = hybrid_visual._pilot_wrong_state_donor(
                base, specs_root=ROOT / "specs"
            )
            samples = hybrid_visual.build_samples_from_bases(
                [base], output_dir=output / "pilot-samples"
            )
            self.assertEqual(len(samples), 5)
            wrong = next(
                row
                for row in samples
                if row["variant"] == "same_profile_wrong_state"
            )
            self.assertTrue(
                wrong["wrong_state_donor_task_id"].startswith("pilot-donor-")
            )

    def test_hybrid_corruptions_preserve_dimensions_and_meet_pixel_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            Image.new("RGB", (200, 100), "white").save(source)

            blank = hybrid_visual.corrupt_image(
                source, root / "blank.png", "blank_render"
            )
            occlusion = hybrid_visual.corrupt_image(
                source, root / "occlusion.png", "critical_occlusion"
            )

            self.assertGreater(blank["changed_pixel_fraction"], 0.50)
            self.assertGreater(
                occlusion["changed_pixel_fraction"],
                hybrid_visual.OCCLUSION_CHANGED_MIN,
            )
            self.assertLessEqual(
                occlusion["changed_pixel_fraction"],
                hybrid_visual.OCCLUSION_CHANGED_MAX,
            )
            with Image.open(root / "blank.png") as image:
                self.assertEqual(image.size, (200, 100))
            with Image.open(root / "occlusion.png") as image:
                self.assertEqual(image.size, (200, 100))

    def test_runtime_fault_normalization_freezes_pixels_outside_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clean = root / "clean.png"
            raw_fault = root / "raw-fault.png"
            normalized = root / "normalized.png"
            Image.new("RGB", (200, 100), "white").save(clean)
            Image.new("RGB", (200, 100), "black").save(raw_fault)

            hybrid_visual._normalize_runtime_fault(
                clean, raw_fault, normalized, "critical_occlusion"
            )
            receipt = hybrid_visual.image_intervention_receipt(
                clean, normalized, "critical_occlusion"
            )

            self.assertGreater(
                receipt["changed_pixel_fraction"],
                hybrid_visual.OCCLUSION_CHANGED_MIN,
            )
            self.assertLessEqual(
                receipt["changed_pixel_fraction"],
                hybrid_visual.OCCLUSION_CHANGED_MAX,
            )
            with Image.open(normalized) as image:
                rgb = image.convert("RGB")
                self.assertEqual(rgb.getpixel((0, 0)), (0, 0, 0))
                self.assertEqual(rgb.getpixel((15, 50)), (255, 255, 255))
                self.assertEqual(rgb.getpixel((18, 50)), (0, 0, 0))

    def test_build_samples_from_bases_produces_frozen_200_sample_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bases = []
            for benchmark_index, benchmark in enumerate(
                hybrid_visual.EXPECTED_BENCHMARKS
            ):
                for task_index in range(4):
                    base_dir = root / "bases" / benchmark / str(task_index)
                    base_dir.mkdir(parents=True)
                    color = (
                        30 + task_index * 45,
                        20 + benchmark_index * 17,
                        220 - task_index * 35,
                    )
                    paths: dict[str, str] = {}
                    for name in (
                        "live_before",
                        "live_after",
                        "golden_before",
                        "golden_after",
                    ):
                        path = base_dir / f"{name}.png"
                        Image.new("RGB", (100, 100), color).save(path)
                        paths[name] = str(path)
                    fault_images = {}
                    for variant in ("blank_render", "critical_occlusion"):
                        path = base_dir / f"{variant}.png"
                        hybrid_visual.corrupt_image(
                            Path(paths["live_after"]), path, variant
                        )
                        fault_images[variant] = str(path)
                    task_id = f"{benchmark}-task-{task_index}"
                    bases.append(
                        {
                            "task_id": task_id,
                            "benchmark": benchmark,
                            "horizon_class": "test",
                            "contract": {
                                "before_description": "before",
                                "after_description": "after",
                                "before_rubric": {
                                    "must": ["before"],
                                    "must_not": [],
                                    "ignore": [],
                                },
                                "after_rubric": {
                                    "must": ["after"],
                                    "must_not": [],
                                    "ignore": [],
                                },
                            },
                            "reference_receipt": {
                                "programmatic": {"programmatic_pass": True}
                            },
                            "frozen_images": paths,
                            "fault_images": fault_images,
                            "intervention_level": (
                                "runtime_overlay"
                                if benchmark
                                in hybrid_visual.BROWSER_OVERLAY_BENCHMARKS
                                else "post_render"
                            ),
                            "state_projection": {"sha256": hashlib.sha256(task_id.encode()).hexdigest()},
                        }
                    )

            samples = hybrid_visual.build_samples_from_bases(
                bases, output_dir=root
            )

            self.assertEqual(len(samples), 200)
            self.assertEqual(len({row["sample_id"] for row in samples}), 200)
            self.assertEqual(
                {row["variant"] for row in samples},
                set(hybrid_visual.FORMAL_VARIANTS),
            )
            for benchmark in hybrid_visual.EXPECTED_BENCHMARKS:
                self.assertEqual(
                    sum(row["benchmark"] == benchmark for row in samples), 20
                )
            wrong = [
                row
                for row in samples
                if row["variant"] == "same_profile_wrong_state"
            ]
            self.assertTrue(
                all(
                    row["wrong_state_donor_task_id"] != row["base_task_id"]
                    and row["wrong_state_distance"]
                    >= hybrid_visual.WRONG_STATE_DISTANCE_MIN
                    for row in wrong
                )
            )

    def test_hybrid_annotation_is_blind_separate_and_finalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp)
            samples = self._write_small_hybrid_dataset(dataset)

            first = hybrid_visual.HybridAnnotationSession(
                dataset_dir=dataset, annotator_id="annotator-a"
            )
            public = first.state()["current_sample"]
            for hidden in (
                "benchmark",
                "base_task_id",
                "variant",
                "design_label",
            ):
                self.assertNotIn(hidden, public)
            self._rate_all(
                first,
                {
                    samples[0]["sample_id"]: ("correct", ""),
                    samples[1]["sample_id"]: ("incorrect", "visible blank"),
                },
            )
            first.finalize()
            with self.assertRaisesRegex(ValueError, "finalized"):
                first.rate(label="correct")

            second = hybrid_visual.HybridAnnotationSession(
                dataset_dir=dataset, annotator_id="annotator-b"
            )
            self._rate_all(
                second,
                {
                    samples[0]["sample_id"]: ("correct", ""),
                    samples[1]["sample_id"]: ("uncertain", "cannot decide"),
                },
            )
            second.finalize()

            adjudicator = hybrid_visual.HybridAnnotationSession(
                dataset_dir=dataset,
                annotator_id="adjudicator",
                adjudicate=True,
            )
            self.assertEqual(adjudicator.state()["total"], 1)
            adjudicator.rate(label="incorrect", note="visible blank")
            adjudicator.finalize()

            judge_rows = [
                {
                    "sample_id": samples[0]["sample_id"],
                    "judge_label": "correct",
                    "pipeline_label": "correct",
                    "cache_hit": False,
                    "identity": {
                        "model": "qwen3-vl-flash@relay",
                        "fake": False,
                    },
                    "usage": {"usage_complete": True, "llm_calls": 1},
                },
                {
                    "sample_id": samples[1]["sample_id"],
                    "judge_label": "incorrect",
                    "pipeline_label": "incorrect",
                    "cache_hit": False,
                    "identity": {
                        "model": "qwen3-vl-flash@relay",
                        "fake": False,
                    },
                    "usage": {"usage_complete": True, "llm_calls": 1},
                },
            ]
            hybrid_visual._write_jsonl(dataset / "judge-results.jsonl", judge_rows)
            summary_path = hybrid_visual.aggregate_hybrid_dataset(
                dataset_dir=dataset
            )
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertTrue(summary["complete"])
            self.assertEqual(summary["coverage"]["adjudication_required"], 1)
            self.assertEqual(summary["judge"]["overall"]["strict_accuracy"], 1.0)

    def test_hybrid_judge_stage_uses_frozen_images_and_fake_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp)
            samples = self._write_small_hybrid_dataset(dataset)
            with patch.dict(
                os.environ,
                {"WARPTEST_VISUAL_JUDGE_FAKE": "correct"},
                clear=False,
            ):
                output = hybrid_visual.judge_hybrid_dataset(
                    dataset_dir=dataset,
                    p5_manifest=ROOT / "benchmarks/p5/manifest.yaml",
                    credentials_file=ROOT / "test_model_batch.py",
                )

            rows = hybrid_visual._read_jsonl(output)
            self.assertEqual(
                [row["sample_id"] for row in rows],
                [sample["sample_id"] for sample in samples],
            )
            self.assertTrue(all(row["judge_label"] == "correct" for row in rows))
            self.assertTrue(all(row["cache_hit"] is False for row in rows))
            self.assertTrue(all(row["usage"]["llm_calls"] == 0 for row in rows))

    def test_hybrid_vlm_annotation_is_blind_resumable_and_source_labeled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp)
            samples = self._write_small_hybrid_dataset(dataset, formal=False)
            with patch.dict(
                os.environ,
                {"WARPTEST_VISUAL_JUDGE_FAKE": "correct"},
                clear=False,
            ):
                output = hybrid_visual.annotate_hybrid_dataset_with_vlm(
                    dataset_dir=dataset,
                    annotator_id="annotator-a",
                    model_spec="glm-5.3-flash",
                    p5_manifest=ROOT / "benchmarks/p5/manifest.yaml",
                    credentials_file=ROOT / "test_model_batch.py",
                )

            annotations = hybrid_visual._read_jsonl(output)
            receipts_path = (
                dataset / "annotations" / "annotator-a.vlm-receipts.jsonl"
            )
            receipts = hybrid_visual._read_jsonl(receipts_path)
            finalization = json.loads(
                (dataset / "annotations" / "annotator-a.finalization.json").read_text()
            )
            self.assertEqual(len(annotations), len(samples))
            self.assertEqual(len(receipts), len(samples))
            self.assertTrue(
                all(row["annotation_source"] == "vlm" for row in annotations)
            )
            self.assertTrue(
                all(
                    row["annotator_model"] == "glm-5.3-flash@relay"
                    for row in annotations
                )
            )
            self.assertEqual(finalization["annotation_source"], "vlm")
            self.assertEqual(
                finalization["annotator_model"], "glm-5.3-flash@relay"
            )
            self.assertEqual(finalization["llm_calls"], 0)
            self.assertTrue(finalization["usage_complete"])
            self.assertTrue(
                all(
                    receipt["blind_input_contract"]["excluded"]
                    == [
                        "benchmark",
                        "base_task_id",
                        "variant",
                        "design_label",
                        "other_annotator_label",
                        "qwen_judge_result",
                    ]
                    for receipt in receipts
                )
            )

            before = hashlib.sha256(receipts_path.read_bytes()).hexdigest()
            with patch.dict(
                os.environ,
                {"WARPTEST_VISUAL_JUDGE_FAKE": "incorrect"},
                clear=False,
            ):
                hybrid_visual.annotate_hybrid_dataset_with_vlm(
                    dataset_dir=dataset,
                    annotator_id="annotator-a",
                    model_spec="glm-5.3-flash",
                    p5_manifest=ROOT / "benchmarks/p5/manifest.yaml",
                    credentials_file=ROOT / "test_model_batch.py",
                )
            self.assertEqual(
                before, hashlib.sha256(receipts_path.read_bytes()).hexdigest()
            )

            with patch.dict(
                os.environ,
                {"WARPTEST_VISUAL_JUDGE_FAKE": "correct"},
                clear=False,
            ):
                hybrid_visual.annotate_hybrid_dataset_with_vlm(
                    dataset_dir=dataset,
                    annotator_id="annotator-b",
                    model_spec="doubao-seed-2-1-turbo",
                    p5_manifest=ROOT / "benchmarks/p5/manifest.yaml",
                    credentials_file=ROOT / "test_model_batch.py",
                )
            hybrid_visual._write_jsonl(
                dataset / "judge-results.jsonl",
                [
                    {
                        "sample_id": sample["sample_id"],
                        "judge_label": "correct",
                        "pipeline_label": "correct",
                        "cache_hit": False,
                        "identity": {
                            "model": "qwen3-vl-flash@relay",
                            "fake": False,
                        },
                        "usage": {"usage_complete": True, "llm_calls": 1},
                    }
                    for sample in samples
                ],
            )
            summary = json.loads(
                hybrid_visual.aggregate_hybrid_dataset(dataset_dir=dataset).read_text()
            )
            self.assertEqual(
                summary["reference_annotation"]["kind"], "vlm_consensus"
            )
            self.assertEqual(
                [
                    item["annotator_model"]
                    for item in summary["reference_annotation"]["annotators"]
                ],
                [
                    "glm-5.3-flash@relay",
                    "doubao-seed-2-1-turbo@relay",
                ],
            )

    def test_hybrid_vlm_adjudicator_only_rates_disagreements_and_is_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp)
            samples = self._write_small_hybrid_dataset(dataset, formal=False)
            for annotator_id, model, label in (
                ("annotator-a", "glm-5.3-flash", "correct"),
                ("annotator-b", "doubao-seed-2-1-turbo", "incorrect"),
            ):
                with patch.dict(
                    os.environ,
                    {"WARPTEST_VISUAL_JUDGE_FAKE": label},
                    clear=False,
                ):
                    hybrid_visual.annotate_hybrid_dataset_with_vlm(
                        dataset_dir=dataset,
                        annotator_id=annotator_id,
                        model_spec=model,
                        p5_manifest=ROOT / "benchmarks/p5/manifest.yaml",
                        credentials_file=ROOT / "test_model_batch.py",
                    )

            with patch.dict(
                os.environ,
                {"WARPTEST_VISUAL_JUDGE_FAKE": "correct"},
                clear=False,
            ), self.assertRaisesRegex(ValueError, "independent"):
                hybrid_visual.annotate_hybrid_dataset_with_vlm(
                    dataset_dir=dataset,
                    annotator_id="adjudicator",
                    model_spec="glm-5.3-flash",
                    adjudicate=True,
                    p5_manifest=ROOT / "benchmarks/p5/manifest.yaml",
                    credentials_file=ROOT / "test_model_batch.py",
                )

            with patch.dict(
                os.environ,
                {"WARPTEST_VISUAL_JUDGE_FAKE": "correct"},
                clear=False,
            ):
                output = hybrid_visual.annotate_hybrid_dataset_with_vlm(
                    dataset_dir=dataset,
                    annotator_id="adjudicator",
                    model_spec="claude-sonnet-5",
                    adjudicate=True,
                    p5_manifest=ROOT / "benchmarks/p5/manifest.yaml",
                    credentials_file=ROOT / "test_model_batch.py",
                )
            rows = hybrid_visual._read_jsonl(output)
            receipts = hybrid_visual._read_jsonl(
                dataset / "annotations" / "adjudicator.vlm-receipts.jsonl"
            )
            finalization = json.loads(
                (dataset / "annotations" / "adjudicator.finalization.json").read_text()
            )
            expected_order = hybrid_visual.annotation_order(
                "test-dataset", "adjudicator", samples
            )
            self.assertEqual([row["sample_id"] for row in rows], expected_order)
            self.assertEqual(len(receipts), 2)
            self.assertEqual(finalization["annotation_role"], "adjudicator")
            self.assertEqual(
                finalization["annotator_model"], "claude-sonnet-5@relay"
            )
            self.assertTrue(
                all(
                    "primary_annotator_labels"
                    in receipt["blind_input_contract"]["excluded"]
                    for receipt in receipts
                )
            )

    def test_legacy_visual_variants_do_not_register_empty_mutators(self) -> None:
        for relative in (
            "specs/jupyterlab/text-file-edit.yaml",
            "specs/blender/scene-cube-material.yaml",
            "specs/pal3/scene-transition.yaml",
        ):
            path = ROOT / relative
            variants = _negative_variants_for_spec(
                path, bundled_spec_paths(ROOT / "specs")
            )
            self.assertEqual(variants, ["oracle_mismatch"])

    @staticmethod
    def _rate_all(
        session: hybrid_visual.HybridAnnotationSession,
        labels: dict[str, tuple[str, str]],
    ) -> None:
        for index in range(session.state()["total"]):
            state = session.state()
            sample_id = state["current_sample"]["sample_id"]
            label, note = labels[sample_id]
            session.rate(label=label, note=note)
            if index + 1 < state["total"]:
                session.next()

    @staticmethod
    def _write_small_hybrid_dataset(
        dataset: Path, *, formal: bool = False
    ) -> list[dict[str, object]]:
        samples: list[dict[str, object]] = []
        for index, variant in enumerate(("after_clean", "blank_render"), start=1):
            sample_id = f"s{index:03d}"
            sample_dir = dataset / "samples" / sample_id
            sample_dir.mkdir(parents=True)
            golden = sample_dir / "golden.png"
            live = sample_dir / "live.png"
            Image.new("RGB", (64, 64), "white").save(golden)
            Image.new(
                "RGB", (64, 64), "white" if variant == "after_clean" else "black"
            ).save(live)
            samples.append(
                {
                    "sample_id": sample_id,
                    "base_task_id": f"task-{index}",
                    "benchmark": "2048",
                    "stage": "after",
                    "variant": variant,
                    "design_label": (
                        "correct" if variant == "after_clean" else "incorrect"
                    ),
                    "description": "The expected visual state is visible.",
                    "rubric": {
                        "must": ["The expected visual state is visible."],
                        "must_not": ["The screen is blank."],
                        "ignore": [],
                    },
                    "golden": str(golden),
                    "live": str(live),
                    "golden_sha256": hashlib.sha256(golden.read_bytes()).hexdigest(),
                    "live_sha256": hashlib.sha256(live.read_bytes()).hexdigest(),
                    "intervention_level": (
                        "not_applicable"
                        if variant == "after_clean"
                        else "runtime_overlay"
                    ),
                    "programmatic_pass": True,
                    "state_projection_sha256": "a" * 64,
                    "wrong_state_donor_task_id": None,
                    "wrong_state_distance": None,
                }
            )
        manifest = {
            "schema": hybrid_visual.PROTOCOL,
            "schema_version": hybrid_visual.SCHEMA_VERSION,
            "dataset_id": "test-dataset",
            "split": "formal" if formal else "pilot",
            "formal": formal,
            "samples": samples,
        }
        (dataset / "visual-calibration-manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        return samples


if __name__ == "__main__":
    unittest.main()


def test_isolated_connection_failure_preserves_safe_recovery_type(tmp_path, monkeypatch):
    import pytest
    from PIL import Image
    from warptest import visual, cost
    from warptest.lazarus_p2p_only_recovery import classify_failure
    screenshot = tmp_path / "frame.png"
    Image.new("RGB", (32, 32), "white").save(screenshot)
    monkeypatch.setattr(visual, "_run_visual_judge_request_isolated", lambda **kwargs: {
        "ok":False, "error_type":"APIConnectionError", "error":"Connection error.",
        "response_received":False, "usage":None, "status_code":None,
    })
    client = visual.VisualJudgeClient(api_key="unit-test-key", base_url="http://localhost:1/v1", model="qwen3-vl-flash")
    client.attempts = 1
    with cost.scoped_meter() as meter, pytest.raises(visual.VisualOracleError) as captured:
        client.judge(prompt="test", live_screenshot=screenshot, golden_image=screenshot)
    assert meter.summary()["llm_calls"] == 0
    assert classify_failure(captured.value)["retryable"] is True
