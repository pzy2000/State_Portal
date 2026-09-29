#!/usr/bin/env python3
"""Provider-free v3 interface canary. Test checkpoints/pixel policies are not model evidence."""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import yaml
from PIL import Image, ImageStat

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_grading import VisualJudgement
from warptest.lazarus_orchestrator import LazarusTrialKey
from warptest.lazarus_p2p_only_contract import (
    ARMS,
    C,
    F,
    METHOD_PROTOCOL,
    cells_per_group_for,
)
from warptest.lazarus_p2p_only_identity import (
    canonical_sha256,
    file_sha256,
    runtime_identities,
)
from warptest.lazarus_p2p_only_paid_runtime import P2POnlyPaidRuntimeRunner
from warptest.lazarus_p2p_only_experiment import _materialize_profile_pair
from warptest.lazarus_p2p_only_executor import _runtime_definitions
from warptest.lazarus_trial import (
    LazarusVisualEvidence, _validate_observation, _p2p_checkpoint_kind,
    _validate_p2p, _p2p_passed,
)
from warptest.lazarus_upstream import (
    definitions_from_upstream_record,
    run_lazarus_per_trial_p2p,
)
from warptest.c1_lazarus_selection.p2p_only_profiles import load_profile_registry


def require_canary_p2p(p2p, *, arm, checkpoint_sha256, task_id, version, test_ids):
    """Exercise the coordinator's receipt admission, not just the producer."""
    failures = []
    kind = _p2p_checkpoint_kind(arm)
    ids = tuple(test_ids)
    _validate_p2p(
        p2p, task_id=task_id, version=version, expected_ids=ids,
        expected_checkpoint_sha256=checkpoint_sha256,
        expected_checkpoint_kind=kind, failures=failures,
    )
    if failures or not _p2p_passed(
        p2p, ids,
        checkpoint_sha256=checkpoint_sha256,
        checkpoint_kind=kind,
    ):
        raise RuntimeError("P2P restore/control failed coordinator admission: " + "; ".join(failures))


class CanaryRunner(P2POnlyPaidRuntimeRunner):
    """Known test-supplied policies, with production restore and execution interfaces."""

    def __init__(self, *, samples, **kwargs):
        super().__init__(**kwargs)
        self.samples = samples
        self.generated = set()

    def _client(self, context):
        return object()

    def _synthesis(self, context):
        task = context.key.task_id
        spec = self.samples[task]["spec"]
        hit = task in self.generated
        self.generated.add(task)
        return {
            "usable": True,
            "generated_spec": spec,
            "failure": None,
            "checkpoint_sha256": canonical_sha256(spec),
            "duration_ms": 0,
            "usage": {"llm_calls": 0, "total_tokens": 0},
            "synthesis_id": canonical_sha256({"test_sample": spec}),
            "artifact_dir": str(self.runtime_root / "test-supplied-checkpoints"),
        }, hit

    def _phase(self, session, client, context, output, phase, budget):
        task = context.key.task_id
        sample = self.samples[task]
        state = sample["spec"]["target"]["state"]
        host = context.task["benchmark"]
        events = []
        if host == "jupyterlab":

            def click(locator):
                locator.scroll_into_view_if_needed()
                b = locator.bounding_box()
                if not b:
                    raise RuntimeError("canary pixel target not visible")
                session.page.mouse.click(
                    b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
                )
                events.append({"kind": "click", "box": b})
                session.page.wait_for_timeout(300)

            def fill(frame, selector, text):
                click(frame.locator(selector))
                session.page.keyboard.press("Meta+A")
                session.page.keyboard.insert_text(text)
                events.append({"kind": "type", "text": text})

            if phase == "phase-a":
                click(session.page.get_by_text("Open task workspace", exact=True).first)
                # Opening the real Lab widget is asynchronous. The pixel
                # dispatch settling delay above is not a frame-ready signal.
                try:
                    session.page.locator('iframe[name="stateportal-p2p-task"]').wait_for(
                        state="attached", timeout=30_000
                    )
                except Exception:
                    diagnostic = output / phase / "workspace-open-failed.png"
                    diagnostic.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        session.page.screenshot(path=str(diagnostic))
                    except Exception:
                        pass
                    raise
                frame = session._task_frame()
                frame.wait_for_function('typeof window.p2pRestore === "function"')
                fill(frame, "#state-json", json.dumps(state))
                click(frame.locator("#apply-state"))
            else:
                frame = session._task_frame()
                for action in sample["spec"]["actions"]:
                    op = action["type"]
                    if op == "click":
                        click(frame.locator(action["selector"]))
                    elif op == "set_class":
                        fill(frame, "#selector", action["selector"])
                        fill(frame, "#class-name", action["class_name"])
                        click(frame.locator("#apply-class"))
                    elif op == "append_button":
                        fill(frame, "#button-text", action["text"])
                        fill(frame, "#button-class", action["class_name"])
                        click(frame.locator("#append-button"))
                    elif op == "add_variable":
                        fill(frame, "#variable-name", action["name"])
                        fill(frame, "#variable-value", action["value"])
                        click(frame.locator("#add-variable"))
                    elif op == "open_panel":
                        fill(frame, "#option-value", action["value"])
                        fill(frame, "#options", ",".join(action["options"]))
                        click(frame.locator(".strip button"))
                    elif op == "select_text":
                        box = frame.locator(action["selector"]).bounding_box()
                        session.page.mouse.move(
                            box["x"] + 1, box["y"] + box["height"] / 2
                        )
                        session.page.mouse.down()
                        session.page.mouse.move(
                            box["x"] + box["width"] - 1,
                            box["y"] + box["height"] / 2,
                            steps=10,
                        )
                        session.page.mouse.up()
                        events.append({"kind": "drag", "box": box})
            session.page.wait_for_timeout(500)
        else:
            from warptest.gui_agent import AgentAction, _apply_action_batch
            import uuid

            def pixel(x, y):
                session.capture_policy_frame(
                    output / phase / f"pixel-{len(events)}.png"
                )
                _apply_action_batch(
                    session,
                    [
                        AgentAction(
                            kind="click", x=int(x), y=int(y), button="left", clicks=1
                        )
                    ],
                    session_id=session.session_id,
                    frame_id=session.last_policy_frame_id,
                    batch_id=uuid.uuid4().hex,
                    width=session.width,
                    height=session.height,
                )
                events.append({"kind": "click", "x": x, "y": y})
                time.sleep(0.8)

            def widget(name):
                visible = [
                    w
                    for w in session.observe_start()["widgets"]
                    if w["id"] == name
                    and w["visible"]
                    and w["width"] > 0
                    and w["height"] > 0
                ]
                if not visible:
                    raise RuntimeError("canary visible widget missing: " + name)
                w = visible[0]
                pixel(w["x"] + w["width"] / 2, w["y"] + w["height"] / 2)

            if host == "openra":
                if phase == "phase-a" and state["screen"] == "editor":
                    for name in (
                        "EXTRAS_BUTTON",
                        "MAP_EDITOR_BUTTON",
                        "LOAD_MAP_BUTTON",
                        "BUTTON_OK",
                    ):
                        widget(name)
                    time.sleep(3)
                    widget("OVERLAYS_TAB")
                elif phase == "phase-b":
                    if state["screen"] == "mainmenu":
                        for name in (
                            "SINGLEPLAYER_BUTTON",
                            "LOAD_BUTTON",
                            "FILTERS_TOGGLE",
                            "FLT_FACTION_DROPDOWNBUTTON",
                        ):
                            widget(name)
                    else:
                        widget("TILES_TAB")
                        widget("CATEGORIES_DROPDOWN")
            else:
                sequence = (
                    [["click", 215, 140 if session.height > 800 else 95]]
                    if phase == "phase-a"
                    else sample["pixel_actions"]
                )
                for kind, x, y in sequence:
                    if kind == "move":
                        session.native.move(x, y)
                        events.append({"kind": "move", "x": x, "y": y})
                        time.sleep(0.8)
                    else:
                        pixel(x, y)
        if len(events) > budget:
            raise RuntimeError("canary policy exceeded budget")
        directory = output / phase
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "actions.json").write_text(json.dumps(events, indent=2))
        for event in events:
            event["execution_receipt"] = {"accepted": True, "event_count": 1}
        return SimpleNamespace(steps=len(events), actions=events, incomplete=False)


def _image_mad(observed: Path, golden: Path) -> float:
    from PIL import ImageChops

    with Image.open(observed) as left, Image.open(golden) as right:
        a = left.convert("RGB")
        b = right.convert("RGB")
        if a.size != b.size:
            b = b.resize(a.size)
        return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", action="append")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "reports/lazarus-p2p-only/runtime-canary-v3.json",
    )
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2,
                        help="Canary preparation concurrency; use one lane during native recovery.")
    parser.add_argument("--candidate-assets-root", type=Path,
                        help="Reuse compiled assets from this experiment; cell evidence remains fresh.")
    parser.add_argument("--adopt-current-complete", action="store_true")
    args = parser.parse_args()
    if args.adopt_current_complete:
        raise SystemExit(
            "v3 requires fresh execution; old canaries cannot be relabeled"
        )
    args.run_root.mkdir(parents=True, exist_ok=False)
    if args.candidate_assets_root:
        assets = args.candidate_assets_root.expanduser().resolve()
        if ROOT not in assets.parents or not assets.is_dir():
            raise ValueError("candidate assets must be an existing workspace directory")
        (args.run_root / "candidate-assets-v3").symlink_to(assets, target_is_directory=True)
    samples = yaml.safe_load(
        (ROOT / "tests/fixtures/lazarus_p2p_only_v3_specs.yaml").read_text()
    )
    tasks = {
        t["id"]: t
        for t in yaml.safe_load(
            (ROOT / "benchmarks/p5/c1_lazarus_p2p_only_tasks.yaml").read_text()
        )["tasks"]
    }
    if set(args.task or ()) - set(tasks):
        raise SystemExit("unknown task")
    profiles = {
        p.task_id: p for p in load_profile_registry(ROOT) if p.catalog_admission
    }
    identities = runtime_identities(ROOT)
    runner = CanaryRunner(workspace=ROOT, runtime_root=args.run_root, samples=samples)
    rows = []
    published = {}
    publish_lock = threading.Lock()

    def persist():
        payload = {
            "schema": "c1-lazarus-p2p-only-runtime-canary-v3",
            "schema_version": 3,
            "method_protocol": METHOD_PROTOCOL,
            "start_protocol": "real_cold_start_v1",
            "runtime_identities": identities,
            "evidence_kind": "test_supplied_spec_and_pixel_policy_no_provider",
            "expected_tasks": 11,
            "expected_cells": cells_per_group_for(ARMS) * 11,
            "complete": len(rows) == 11 and all(r["complete"] for r in rows),
            "tasks": rows,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        temporary.replace(args.output)
        return payload

    def publish(row):
        nonlocal rows
        with publish_lock:
            published[row["task_id"]] = copy.deepcopy(row)
            rows = [published[task_id] for task_id in tasks if task_id in published]
            persist()

    def run_task(task_id, task):
        print(task_id, flush=True)
        row = {
            "task_id": task_id,
            "complete": False,
            "runtime_sha256": identities[task["benchmark"]],
            "versions": {},
            "failures": [],
        }
        publish(row)
        try:
            fixed, buggy, _, _ = _materialize_profile_pair(ROOT, profiles[task_id])
            upstream = json.loads(
                (ROOT / task["defect"]["upstream_record"]).read_text()
            )
            definitions = _runtime_definitions(
                definitions_from_upstream_record(upstream), task=task
            )
            spec = yaml.safe_load((ROOT / task["oracle_spec"]).read_text())
            ids = spec["evaluation"]["lazarus"]["upstream_tests"]["per_trial_p2p"]
            for version, tree in (("buggy", buggy), ("fixed", fixed)):
                row["versions"][version] = {}
                for arm in ARMS:
                    ctx = SimpleNamespace(
                        key=LazarusTrialKey(
                            arm, "test-supplied-no-provider", 0, task_id, version
                        ),
                        task=task,
                        artifact=SimpleNamespace(root=tree),
                    )

                    def gate(obs):
                        with Image.open(obs.before_screenshot) as img:
                            informative = (
                                max(ImageStat.Stat(img.convert("RGB")).stddev) > 1
                            )
                        judgement = VisualJudgement(
                            informative, file_sha256(obs.before_screenshot), None
                        )
                        return LazarusVisualEvidence(
                            judgement,
                            VisualJudgement(None, None, None),
                            VisualJudgement(None, None, None),
                            {"test_gate": "informative_frame_only"},
                        )

                    obs = runner.run_cell(ctx, start_gate=gate)
                    observation_failures = []
                    _validate_observation(
                        obs,
                        observation_failures,
                        require_semantic_after=False,
                        require_real_start=True,
                    )
                    if observation_failures:
                        raise RuntimeError(
                            f"{arm}/{version}: production observation validation failed: "
                            + "; ".join(observation_failures)
                        )
                    if (
                        not obs.before_semantic_ok
                        or not obs.invariants_ok
                        or obs.after_screenshot is None
                    ):
                        raise RuntimeError(
                            f"{arm}/{version}: Start or action interface failed"
                        )
                    p2p = run_lazarus_per_trial_p2p(
                        task_id=task_id,
                        version=version,
                        checkpoint_sha256=obs.checkpoint_sha256,
                        checkpoint_kind=(
                            "agent_s_from_start"
                            if arm == C
                            else "scalecua_from_start"
                            if arm == F
                            else "semantic_state"
                        ),
                        source_root=tree,
                        definitions=definitions,
                        test_ids=ids,
                        sandbox_root=args.run_root / "p2p" / ctx.key.invocation_id(),
                        harness_root=ROOT,
                        checkpoint_restorer=obs.p2p_restore_adapter,
                    )
                    try:
                        require_canary_p2p(
                            p2p, arm=arm, checkpoint_sha256=obs.checkpoint_sha256,
                            task_id=task_id, version=version, test_ids=ids,
                        )
                    except RuntimeError:
                        row["failed_p2p"] = p2p
                        raise
                    row["versions"][version][arm] = {
                        "complete": True,
                        "before": str(obs.before_screenshot),
                        "after": str(obs.after_screenshot),
                        "same_live_lineage": True,
                        "start_constructed": True,
                        "phase_b": obs.trace["phase_b"],
                        "p2p": p2p,
                        "checkpoint_sha256": obs.checkpoint_sha256,
                    }
                    publish(row)
            row.update(
                complete=True,
                same_live_lineage=True,
                state_transition_executed=True,
                agent_s_pixel_transition_executed=True,
                scalecua_pixel_transition_executed=True,
                state_start_constructed=True,
                agent_s_start_constructed=True,
                synth_agent_s_start_constructed=True,
                synth_agent_s_pixel_transition_executed=True,
                synth_scalecua_start_constructed=True,
                p2p_restore_passed=True,
            )
        except Exception as error:
            row["failures"].append(f"{type(error).__name__}: {error}")
            print("  BLOCKED " + row["failures"][-1], flush=True)
        publish(row)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(run_task, task_id, task)
            for task_id, task in tasks.items()
            if not args.task or task_id in args.task
        ]
        for future in futures:
            future.result()
    result = persist()
    print("wrote " + str(args.output))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
