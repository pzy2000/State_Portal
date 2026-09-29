from __future__ import annotations

import copy
import hashlib
import json
import random
import re
import threading
import webbrowser
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import yaml

from .c1_evaluation import materialize_legacy_spec, visual_oracle
from .positioning import auxiliary_positioning
from .reports import new_report_dir, write_json
from .runner import run_spec
from .runtime import find_free_port
from .visual import VISUAL_LABELS, validate_visual_oracle


LABEL_ORDER = ["correct", "incorrect", "uncertain"]
BLIND_ERROR_TOKEN_RE = re.compile(
    r"positive|negative|oracle_mismatch|wrong_scene_checkpoint|wrong_action|wrong_checkpoint",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class VisualExperimentSample:
    sample_id: str
    spec_path: str
    truth_label: str
    variant: str
    title: str


def bundled_spec_paths(specs_root: Path = Path("specs"), benchmark: str | None = None) -> list[Path]:
    paths = sorted(specs_root.glob("*/*.yaml"))
    if benchmark is None:
        return paths
    expected = _benchmark_token(benchmark)
    filtered: list[Path] = []
    for spec_path in paths:
        try:
            spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(spec, dict) and _benchmark_token(spec.get("benchmark")) == expected:
            filtered.append(spec_path)
    return filtered


def build_visual_experiment_manifest(
    sample_count: int = 50,
    specs_root: Path = Path("specs"),
    benchmark: str | None = None,
) -> list[VisualExperimentSample]:
    if sample_count < 2:
        raise ValueError("sample_count must be at least 2")
    specs: list[Path] = []
    for spec_path in bundled_spec_paths(specs_root, benchmark=benchmark):
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        if not isinstance(spec, dict):
            raise ValueError(f"Spec must be a mapping: {spec_path}")
        if not isinstance(visual_oracle(spec), dict):
            continue
        validate_visual_oracle(spec, spec_path, required=True)
        # The legacy visual-experiment runner executes OpenRA through the
        # map/gameplay profile. C1 settings specs use a separate persistent
        # foreground runtime and cannot be replayed by that profile.
        if (
            _benchmark_token(spec.get("benchmark")) == "openra"
            and isinstance(spec.get("target"), dict)
            and spec["target"].get("kind") != "openra_map"
        ):
            continue
        specs.append(spec_path)
    if not specs:
        suffix = f" for benchmark {benchmark}" if benchmark else ""
        raise ValueError(f"No bundled visual specs found under {specs_root}{suffix}")

    positives = sample_count // 2
    negatives = sample_count - positives
    sample_specs: list[tuple[Path, str, str]] = []
    for index in range(positives):
        spec_path = specs[index % len(specs)]
        sample_specs.append((spec_path, "correct", "normal"))
    negative_candidates = _negative_sample_candidates(specs)
    if not negative_candidates:
        raise ValueError("No visual negative samples can be generated from bundled visual specs.")
    for index in range(negatives):
        spec_path, variant = negative_candidates[index % len(negative_candidates)]
        sample_specs.append((spec_path, "incorrect", variant))

    random.SystemRandom().shuffle(sample_specs)
    manifest: list[VisualExperimentSample] = []
    for index, (spec_path, truth_label, variant) in enumerate(sample_specs, start=1):
        manifest.append(
            VisualExperimentSample(
                sample_id=f"s{index:03d}",
                spec_path=str(spec_path.resolve()),
                truth_label=truth_label,
                variant=variant,
                title=f"Sample {index}",
            )
        )
    return manifest


def materialize_sample_spec(sample: VisualExperimentSample, all_specs: list[Path], out_dir: Path) -> Path:
    spec_path = Path(sample.spec_path)
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        raise ValueError(f"Spec must be a mapping: {spec_path}")
    spec = materialize_legacy_spec(spec)
    spec.pop("evaluation", None)
    spec["name"] = f"{spec.get('name', spec_path.stem)} [{sample.sample_id} {sample.variant}]"
    _make_repo_absolute(spec, spec_path)
    before_variant = copy.deepcopy(spec)
    if sample.variant == "oracle_mismatch":
        other = _next_spec_with_visual(spec_path, all_specs)
        other_spec = materialize_legacy_spec(
            yaml.safe_load(other.read_text(encoding="utf-8"))
        )
        oracle = copy.deepcopy(other_spec["visual_oracle"])
        golden = Path(str(oracle["golden_image"])).expanduser()
        if not golden.is_absolute():
            oracle["golden_image"] = str((other.parent / golden).resolve())
        spec["visual_oracle"] = oracle
    elif sample.variant == "wrong_action":
        _mutate_wrong_action(spec)
    elif sample.variant == "wrong_checkpoint":
        _mutate_wrong_checkpoint(spec)
    elif sample.variant == "wrong_scene_checkpoint":
        _mutate_jynew_wrong_scene_checkpoint(spec)
    elif sample.variant != "normal":
        raise ValueError(f"Unsupported visual experiment variant: {sample.variant}")
    if sample.variant != "normal" and spec == before_variant:
        raise ValueError(
            f"Visual experiment variant {sample.variant} did not change {spec_path}"
        )
    _stabilize_jynew_visual_sample(spec)
    _stabilize_openra_visual_sample(spec)
    if sample.variant == "normal":
        _align_jynew_normal_visual_scene(spec)
    _make_visual_oracle_absolute(spec, spec_path)
    _set_jynew_live_fallback_image(spec, spec_path)

    target = out_dir / "sample-specs" / f"{sample.sample_id}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=False), encoding="utf-8")
    return target


def _make_visual_oracle_absolute(spec: dict[str, Any], source_spec_path: Path) -> None:
    oracle = spec.get("visual_oracle")
    if not isinstance(oracle, dict):
        return
    for key in ("golden_image", "golden_before", "golden_after", "live_fallback_image"):
        raw = oracle.get(key)
        if not raw:
            continue
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            oracle[key] = str((source_spec_path.parent / path).resolve())


def _visual_live_screenshot(visual: dict[str, Any]) -> str | None:
    for key in ("live_screenshot", "live_after", "live_before"):
        value = visual.get(key)
        if value:
            return str(value)
    return None


def _visual_golden_image(visual: dict[str, Any]) -> str | None:
    for key in ("golden_image", "golden_after", "golden_before"):
        value = visual.get(key)
        if value:
            return str(value)
    return None


def _visual_llm_summary(visual: dict[str, Any], fallback_rationale: str) -> tuple[str, float, str]:
    label = visual.get("label")
    if label in VISUAL_LABELS:
        return str(label), float(visual.get("confidence") or 0.0), str(visual.get("rationale") or fallback_rationale)

    pair = visual.get("pair_verdict")
    if not isinstance(pair, dict):
        return "uncertain", 0.0, fallback_rationale

    stages = [pair.get("before"), pair.get("after")]
    labels = [stage.get("label") for stage in stages if isinstance(stage, dict)]
    confidences = [
        float(stage.get("confidence") or 0.0)
        for stage in stages
        if isinstance(stage, dict) and stage.get("confidence") is not None
    ]
    rationales = [
        f"{name}: {stage.get('rationale')}"
        for name, stage in (("before", pair.get("before")), ("after", pair.get("after")))
        if isinstance(stage, dict) and stage.get("rationale")
    ]
    if pair.get("result") is True and labels and all(label == "correct" for label in labels):
        summary_label = "correct"
    elif pair.get("result") is False or any(label == "incorrect" for label in labels):
        summary_label = "incorrect"
    else:
        summary_label = "uncertain"
    return summary_label, min(confidences) if confidences else 0.0, "; ".join(rationales) or fallback_rationale


def _set_jynew_live_fallback_image(spec: dict[str, Any], source_spec_path: Path) -> None:
    if _benchmark_token(spec.get("benchmark")) != "jynew":
        return
    oracle = spec.get("visual_oracle")
    target = spec.get("target")
    if not isinstance(oracle, dict) or not isinstance(target, dict):
        return

    target_scene = _jynew_target_scene_key(target)
    oracle_scene = oracle.get("scene_key")
    if target_scene == oracle_scene:
        fallback = Path(str(oracle.get("golden_image", ""))).expanduser()
        if not fallback.is_absolute():
            fallback = source_spec_path.parent / fallback
    else:
        fallback = source_spec_path.parent / _jynew_fallback_for_scene(target_scene)
    oracle["live_fallback_image"] = str(fallback.resolve())


def _jynew_target_scene_key(target: dict[str, Any]) -> str:
    map_id = target.get("map_id")
    if map_id == 0 or str(map_id) == "0":
        return "jynew_world_map"
    if map_id == 1 or str(map_id) == "1":
        return "jynew_submap"
    return "jynew_default_world"


def _jynew_fallback_for_scene(scene_key: str) -> Path:
    return {
        "jynew_world_map": Path("visual/world-map-position.golden.png"),
        "jynew_submap": Path("visual/submap-enter.golden.png"),
    }.get(scene_key, Path("visual/team-join.golden.png"))


def _make_repo_absolute(spec: dict[str, Any], source_spec_path: Path) -> None:
    repo = spec.get("repo")
    if not isinstance(repo, str) or not repo:
        return
    repo_path = Path(repo).expanduser()
    if repo_path.is_absolute():
        return
    spec["repo"] = str((source_spec_path.parent / repo_path).resolve())


def _stabilize_jynew_visual_sample(spec: dict[str, Any]) -> None:
    if _benchmark_token(spec.get("benchmark")) != "jynew":
        return
    spec["actions"] = []
    spec["assertions"] = []
    target = spec.get("target")
    if isinstance(target, dict):
        target.pop("validations", None)


def _stabilize_openra_visual_sample(spec: dict[str, Any]) -> None:
    if _benchmark_token(spec.get("benchmark")) != "openra":
        return
    target = spec.get("target")
    if not isinstance(target, dict):
        return
    mod = target.get("mod") or target.get("requires_mod")
    map_id = target.get("map")
    if not mod or not map_id:
        return
    expected_validity = "replay-reachable"
    checkpoint = spec.get("checkpoint")
    if isinstance(checkpoint, dict) and checkpoint.get("expected_validity"):
        expected_validity = str(checkpoint["expected_validity"])
    spec["checkpoint"] = {
        "allowed_strategies": ["openra_utility_map_validate"],
        "expected_validity": expected_validity,
    }
    spec["actions"] = [{"type": "openra_validate_map", "mod": mod, "map": map_id}]
    spec["assertions"] = [
        {"type": "openra_map_loaded", "mod": mod, "map": map_id},
        {"type": "no_openra_utility_errors"},
    ]


def _align_jynew_normal_visual_scene(spec: dict[str, Any]) -> None:
    if _benchmark_token(spec.get("benchmark")) != "jynew":
        return
    target = spec.get("target")
    oracle = spec.get("visual_oracle")
    if not isinstance(target, dict) or not isinstance(oracle, dict):
        return
    scene_key = oracle.get("scene_key")
    if scene_key == "jynew_world_map":
        target["map_id"] = 0
    elif scene_key == "jynew_submap":
        target["map_id"] = 1
    elif scene_key == "jynew_default_world":
        target.pop("map_id", None)


def _next_spec_with_visual(current: Path, all_specs: list[Path]) -> Path:
    current_spec = _load_spec_mapping(current)
    if _benchmark_token(current_spec.get("benchmark")) == "jynew":
        mismatch = _next_jynew_scene_mismatch_spec(current, all_specs)
        if mismatch is None:
            raise ValueError(f"No visually distinct jynew oracle is available for {current}")
        return mismatch

    specs = [path.resolve() for path in all_specs if _spec_has_visual_oracle(path)]
    index = specs.index(current.resolve())
    return specs[(index + 1) % len(specs)]


def _spec_has_visual_oracle(path: Path) -> bool:
    try:
        spec = _load_spec_mapping(path)
    except Exception:
        return False
    return isinstance(spec, dict) and isinstance(visual_oracle(spec), dict)


def _negative_sample_candidates(specs: list[Path]) -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []
    for spec_path in specs:
        for variant in _negative_variants_for_spec(spec_path, specs):
            candidates.append((spec_path, variant))
    return candidates


def _negative_variants_for_spec(spec_path: Path, all_specs: list[Path]) -> list[str]:
    spec = _load_spec_mapping(spec_path)
    benchmark = _benchmark_token(spec.get("benchmark"))
    if benchmark == "openra":
        return ["oracle_mismatch"]
    if benchmark == "luanti":
        variants = ["oracle_mismatch"]
        if _can_mutate_luanti_wrong_action(spec):
            variants.append("wrong_action")
        if _can_mutate_luanti_wrong_checkpoint(spec):
            variants.append("wrong_checkpoint")
        return variants
    if benchmark in {"2048", "spacerpg", "datingsim", "libreoffice"}:
        return ["oracle_mismatch", "wrong_action", "wrong_checkpoint"]
    if benchmark in {"jupyterlab", "blender", "pal3", "pal3unity"}:
        # These profiles do not have registered wrong-action/checkpoint
        # mutators.  Returning those variants used to create an unchanged spec
        # with an incorrect truth label, which is a fail-open calibration bug.
        return ["oracle_mismatch"]
    if benchmark != "jynew":
        return ["oracle_mismatch"]

    variants: list[str] = []
    if _next_jynew_scene_mismatch_spec(spec_path, all_specs) is not None:
        variants.append("oracle_mismatch")
    if _jynew_wrong_scene_map_id(spec) is not None:
        variants.append("wrong_scene_checkpoint")
    return variants


def _load_spec_mapping(path: Path) -> dict[str, Any]:
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        raise ValueError(f"Spec must be a mapping: {path}")
    return materialize_legacy_spec(spec)


_LUANTI_VISIBLE_ACTION_TYPES = {
    "luanti_dig_node",
    "luanti_fill",
    "luanti_give_item",
    "luanti_set_player_inventory",
    "luanti_set_node",
    "luanti_set_player_hp",
    "luanti_set_player_pos",
}


def _can_mutate_luanti_wrong_action(spec: dict[str, Any]) -> bool:
    actions = spec.get("actions")
    return isinstance(actions, list) and any(
        isinstance(action, dict) and action.get("type") in _LUANTI_VISIBLE_ACTION_TYPES
        for action in actions
    )


def _can_mutate_luanti_wrong_checkpoint(spec: dict[str, Any]) -> bool:
    target = spec.get("target")
    if not isinstance(target, dict):
        return False
    return any(
        isinstance(target.get(key), list) and bool(target.get(key))
        for key in ("fill", "nodes", "meta_inventory")
    ) or isinstance(target.get("player"), dict)


def _mutate_luanti_wrong_action(spec: dict[str, Any]) -> None:
    actions = spec.get("actions")
    if not isinstance(actions, list):
        return
    for action in actions:
        if not isinstance(action, dict):
            continue
        atype = action.get("type")
        if atype in {"luanti_set_node", "luanti_fill"} and isinstance(action.get("node"), str):
            action["node"] = _luanti_contrast_node(action["node"])
            action.pop("param2", None)
            return
        if atype == "luanti_dig_node" and isinstance(action.get("pos"), dict):
            pos = copy.deepcopy(action["pos"])
            action.clear()
            action.update({"type": "luanti_set_node", "pos": pos, "node": "basenodes:snowblock"})
            return
        if atype == "luanti_set_player_pos" and isinstance(action.get("pos"), dict):
            pos = copy.deepcopy(action["pos"])
            pos["x"] = int(pos.get("x", 0)) + 12
            pos["z"] = int(pos.get("z", 0)) + 12
            action["pos"] = pos
            return
        if atype == "luanti_set_player_hp":
            action["hp"] = 4
            return
        if atype == "luanti_give_item":
            action["item"] = _luanti_contrast_item(str(action.get("item", "")))
            action["count"] = 1
            return


def _mutate_luanti_wrong_checkpoint(spec: dict[str, Any]) -> None:
    target = spec.get("target")
    if not isinstance(target, dict):
        return
    for key in ("fill", "nodes", "meta_inventory"):
        entries = target.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("node"), str):
                entry["node"] = _luanti_contrast_node(entry["node"])
                entry.pop("param2", None)
                return
    player = target.get("player")
    if isinstance(player, dict):
        if isinstance(player.get("pos"), dict):
            pos = copy.deepcopy(player["pos"])
            pos["x"] = int(pos.get("x", 0)) + 12
            pos["z"] = int(pos.get("z", 0)) + 12
            player["pos"] = pos
            return
        if "hp" in player:
            player["hp"] = 4
            return
        inventory = player.get("inventory")
        if isinstance(inventory, list) and inventory and isinstance(inventory[0], dict):
            inventory[0]["item"] = _luanti_contrast_item(str(inventory[0].get("item", "")))
            inventory[0]["count"] = 1


def _luanti_contrast_item(item: str) -> str:
    if item == "basetools:pick_steel":
        return "basenodes:apple"
    return _luanti_contrast_node(item)


def _luanti_contrast_node(node: str) -> str:
    if node in {"basenodes:water_source", "basenodes:water_flowing", "basenodes:river_water_source", "basenodes:river_water_flowing"}:
        return "basenodes:lava_source"
    if node in {"basenodes:lava_source", "basenodes:lava_flowing"}:
        return "basenodes:water_source"
    if node == "chest:chest":
        return "basenodes:mossycobble"
    if node.startswith("stairs:"):
        return "basenodes:snowblock"
    if node in {"basenodes:stone", "basenodes:cobble", "basenodes:mossycobble", "basenodes:desert_stone"}:
        return "basenodes:snowblock"
    if node in {"basenodes:dirt", "basenodes:dirt_with_grass", "basenodes:sand", "basenodes:desert_sand"}:
        return "basenodes:cobble"
    if "tree" in node or "leaves" in node or node in {"basenodes:junglegrass", "basenodes:apple"}:
        return "basenodes:desert_stone"
    return "basenodes:snowblock"


def _next_jynew_scene_mismatch_spec(current: Path, all_specs: list[Path]) -> Path | None:
    current_path = current.resolve()
    current_meta = _jynew_visual_metadata(current_path)
    if current_meta is None:
        return None
    candidates = [
        path.resolve()
        for path in all_specs
        if path.resolve() == current_path or _jynew_visual_metadata(path.resolve()) is not None
    ]
    try:
        index = candidates.index(current_path)
    except ValueError:
        return None

    for offset in range(1, len(candidates)):
        candidate = candidates[(index + offset) % len(candidates)]
        metadata = _jynew_visual_metadata(candidate)
        if metadata is None:
            continue
        if metadata["scene_key"] != current_meta["scene_key"] and metadata["golden_hash"] != current_meta["golden_hash"]:
            return candidate
    return None


def _jynew_visual_metadata(spec_path: Path) -> dict[str, str] | None:
    try:
        spec = _load_spec_mapping(spec_path)
    except Exception:
        return None
    if _benchmark_token(spec.get("benchmark")) != "jynew":
        return None
    oracle = spec.get("visual_oracle")
    if not isinstance(oracle, dict):
        return None
    scene_key = oracle.get("scene_key")
    if not isinstance(scene_key, str) or not scene_key.strip():
        return None
    golden_value = oracle.get("golden_image")
    if not isinstance(golden_value, str) or not golden_value.strip():
        return None
    golden = Path(golden_value).expanduser()
    if not golden.is_absolute():
        golden = spec_path.parent / golden
    if not golden.exists() or not golden.is_file():
        return None
    return {
        "scene_key": scene_key.strip(),
        "golden_hash": hashlib.sha256(golden.read_bytes()).hexdigest(),
    }


def _mutate_wrong_action(spec: dict[str, Any]) -> None:
    benchmark = spec.get("benchmark")
    benchmark_token = _benchmark_token(benchmark)
    actions = spec.get("actions")
    if not isinstance(actions, list):
        spec["actions"] = []
        return
    if _benchmark_token(benchmark) == "luanti":
        _mutate_luanti_wrong_action(spec)
        return
    if _benchmark_token(benchmark) == "libreoffice":
        _mutate_libreoffice_wrong_action(spec)
        return
    if benchmark_token == "2048":
        for action in actions:
            if isinstance(action, dict) and action.get("type") == "move":
                action["direction"] = {"left": "right", "right": "left", "up": "down", "down": "up"}.get(action.get("direction"), "right")
                return
    if benchmark_token == "spacerpg":
        spec["actions"] = [{"type": "key", "key": "Escape", "wait_ms": 300}]
        return
    if benchmark_token == "datingsim":
        spec["actions"] = [{"type": "wait", "ms": 300}]
        return
    if benchmark_token == "openra":
        _mutate_openra_target_to_different_map(spec)
    if benchmark == "jynew":
        for action in actions:
            if isinstance(action, dict):
                atype = action.get("type", "")
                if atype == "jynew_join_team":
                    action["type"] = "jynew_leave_team"
                    return
                if atype == "jynew_leave_team":
                    action["type"] = "jynew_join_team"
                    return
                if atype == "jynew_level_up":
                    action["type"] = "jynew_rest"
                    return
                if atype == "jynew_add_item":
                    action["item_id"] = action.get("item_id", 1) + 99
                    return
                if atype == "jynew_learn_skill":
                    action["skill_id"] = action.get("skill_id", 1) + 99
                    return
        spec["actions"] = [{"type": "jynew_rest"}]


def _mutate_wrong_checkpoint(spec: dict[str, Any]) -> None:
    benchmark = spec.get("benchmark")
    benchmark_token = _benchmark_token(benchmark)
    target = spec.get("target")
    if not isinstance(target, dict):
        return
    if benchmark_token == "2048":
        target["board"] = [[4, 4, 0, 0], [2, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]
        target["score"] = 0
        target["bestScore"] = 0
    elif benchmark_token == "spacerpg":
        if target.get("kind") == "flight":
            target.update({"kind": "station", "screen": "station", "station_id": "helion-prime"})
        else:
            target.update({"kind": "flight", "screen": "flight"})
            target.pop("station_id", None)
    elif benchmark_token == "datingsim":
        resources = target.setdefault("patch", {}).setdefault("player", {}).setdefault("resources", {})
        resources["money"] = 50
        resources["energy"] = 10
    elif benchmark_token == "openra":
        _mutate_openra_target_to_different_map(spec)
    elif benchmark == "jynew":
        _mutate_jynew_wrong_scene_checkpoint(spec)
    elif _benchmark_token(benchmark) == "luanti":
        _mutate_luanti_wrong_checkpoint(spec)
    elif _benchmark_token(benchmark) == "libreoffice":
        _mutate_libreoffice_wrong_checkpoint(spec)


def _mutate_libreoffice_wrong_action(spec: dict[str, Any]) -> None:
    actions = spec.get("actions")
    if not isinstance(actions, list):
        actions = []
        spec["actions"] = actions
    kind = (spec.get("target") or {}).get("kind") if isinstance(spec.get("target"), dict) else None
    if kind == "calc_spreadsheet":
        mutation = {
            "type": "libreoffice_set_cell",
            "sheet": _libreoffice_active_sheet(spec),
            "cell": "A1",
            "value": "WRONG VISUAL STATE",
        }
    elif kind == "impress_presentation":
        mutation = {
            "type": "libreoffice_set_slide_title",
            "slide": _libreoffice_active_slide(spec),
            "title": "WRONG VISUAL STATE",
        }
    elif kind == "writer_document":
        first_text = _libreoffice_first_writer_text(spec)
        mutation = (
            {"type": "libreoffice_replace_text", "old": first_text, "new": "WRONG VISUAL STATE"}
            if first_text
            else {"type": "libreoffice_append_paragraph", "text": "WRONG VISUAL STATE"}
        )
    else:
        spec["actions"] = []
        return
    _insert_before_libreoffice_persistence(actions, mutation)


def _mutate_libreoffice_wrong_checkpoint(spec: dict[str, Any]) -> None:
    target = spec.get("target")
    if not isinstance(target, dict):
        return
    kind = target.get("kind")
    if kind == "calc_spreadsheet":
        sheet_name = _libreoffice_active_sheet(spec)
        if isinstance(target.get("sheets"), dict):
            sheet = target["sheets"].setdefault(sheet_name, {})
            cells = sheet.setdefault("cells", {})
        else:
            target["sheet"] = sheet_name
            cells = target.setdefault("cells", {})
        cells["A1"] = "WRONG CHECKPOINT"
    elif kind == "impress_presentation":
        slides = target.setdefault("slides", [])
        slide_index = _libreoffice_active_slide(spec) - 1
        while len(slides) <= slide_index:
            slides.append({})
        if isinstance(slides[slide_index], dict):
            slides[slide_index]["title"] = "WRONG CHECKPOINT"
            slides[slide_index]["bullets"] = ["This slide intentionally mismatches the oracle."]
    elif kind == "writer_document":
        paragraphs = target.setdefault("paragraphs", [])
        if paragraphs and isinstance(paragraphs[0], dict):
            paragraphs[0]["text"] = "WRONG CHECKPOINT"
        else:
            paragraphs.insert(0, {"text": "WRONG CHECKPOINT", "style": "Heading 1"})


def _insert_before_libreoffice_persistence(actions: list[Any], mutation: dict[str, Any]) -> None:
    for index, action in enumerate(actions):
        if isinstance(action, dict) and action.get("type") in {
            "libreoffice_save",
            "libreoffice_reopen",
            "libreoffice_export_pdf",
        }:
            actions.insert(index, mutation)
            return
    actions.append(mutation)


def _libreoffice_active_sheet(spec: dict[str, Any]) -> str:
    capture = _libreoffice_capture_options(spec)
    active = capture.get("after_active_sheet") or capture.get("active_sheet")
    if isinstance(active, str) and active:
        return active
    target = spec.get("target")
    if isinstance(target, dict):
        sheet = target.get("sheet")
        if isinstance(sheet, str) and sheet:
            return sheet
        sheets = target.get("sheets")
        if isinstance(sheets, dict) and sheets:
            return str(next(iter(sheets.keys())))
    return "Sheet1"


def _libreoffice_active_slide(spec: dict[str, Any]) -> int:
    capture = _libreoffice_capture_options(spec)
    active = capture.get("after_active_slide") or capture.get("active_slide") or 1
    try:
        return max(1, int(active))
    except Exception:
        return 1


def _libreoffice_capture_options(spec: dict[str, Any]) -> dict[str, Any]:
    oracle = spec.get("visual_oracle")
    capture = oracle.get("capture") if isinstance(oracle, dict) else None
    return capture if isinstance(capture, dict) else {}


def _libreoffice_first_writer_text(spec: dict[str, Any]) -> str | None:
    target = spec.get("target")
    paragraphs = target.get("paragraphs") if isinstance(target, dict) else None
    if not isinstance(paragraphs, list):
        return None
    for paragraph in paragraphs:
        if isinstance(paragraph, dict) and paragraph.get("text"):
            return str(paragraph["text"])
    return None


def _mutate_jynew_wrong_scene_checkpoint(spec: dict[str, Any]) -> None:
    target = spec.setdefault("target", {})
    if not isinstance(target, dict):
        raise ValueError("jynew wrong_scene_checkpoint requires target to be a mapping")
    map_id = _jynew_wrong_scene_map_id(spec)
    if map_id is None:
        raise ValueError("jynew spec does not declare a stable visual scene_key for wrong_scene_checkpoint")
    target["map_id"] = map_id


def _jynew_wrong_scene_map_id(spec: dict[str, Any]) -> int | None:
    oracle = spec.get("visual_oracle")
    scene_key = oracle.get("scene_key") if isinstance(oracle, dict) else None
    if scene_key == "jynew_submap":
        return 0
    if scene_key in {"jynew_world_map", "jynew_default_world"}:
        return 1
    return None


def _mutate_openra_target_to_different_map(spec: dict[str, Any]) -> None:
    target = spec.get("target")
    if not isinstance(target, dict):
        return
    current = (str(target.get("mod") or ""), str(target.get("map") or ""))
    replacements = {
        ("cnc", "gdi01"): ("cnc", "nod03a"),
        ("cnc", "nod03a"): ("cnc", "gdi01"),
        ("cnc", "the-hot-box"): ("ra", "drop-zone"),
        ("ra", "allies-03a"): ("ra", "soviet-05"),
        ("ra", "soviet-05"): ("ra", "allies-03a"),
        ("ra", "drop-zone"): ("cnc", "the-hot-box"),
    }
    mod, map_id = replacements.get(current, ("cnc", "gdi01"))
    target["mod"] = mod
    target["requires_mod"] = mod
    target["map"] = map_id
    for action in spec.get("actions", []):
        if isinstance(action, dict) and action.get("type") == "openra_validate_map":
            action["mod"] = mod
            action["map"] = map_id


def cohen_kappa(human_labels: list[str], llm_labels: list[str], labels: list[str] | None = None) -> float:
    labels = labels or LABEL_ORDER
    if len(human_labels) != len(llm_labels):
        raise ValueError("human_labels and llm_labels must have the same length")
    n = len(human_labels)
    if n == 0:
        return 0.0
    observed = sum(1 for human, llm in zip(human_labels, llm_labels) if human == llm) / n
    expected = 0.0
    for label in labels:
        human_rate = sum(1 for value in human_labels if value == label) / n
        llm_rate = sum(1 for value in llm_labels if value == label) / n
        expected += human_rate * llm_rate
    if expected == 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1 - expected)


def confusion_matrix(human_labels: list[str], llm_labels: list[str], labels: list[str] | None = None) -> dict[str, dict[str, int]]:
    labels = labels or LABEL_ORDER
    matrix = {human: {llm: 0 for llm in labels} for human in labels}
    for human, llm in zip(human_labels, llm_labels):
        if human in matrix and llm in matrix[human]:
            matrix[human][llm] += 1
    return matrix


def experiment_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rated = [row for row in rows if row.get("human_label") in VISUAL_LABELS and not row.get("capture_failure")]
    human_labels = [row["human_label"] for row in rated]
    llm_labels = [row.get("llm_label", "uncertain") for row in rated]
    truth_labels = [row["truth_label"] for row in rated]
    covered = [row for row in rated if row.get("llm_label") != "uncertain"]
    return {
        "rated": len(rated),
        "kappa": cohen_kappa(human_labels, llm_labels) if rated else 0.0,
        "confusion_matrix": confusion_matrix(human_labels, llm_labels),
        "strict_llm_accuracy": _accuracy(llm_labels, truth_labels),
        "coverage_excluding_uncertain": len(covered) / len(rated) if rated else 0.0,
        "accuracy_excluding_uncertain": _accuracy(
            [row.get("llm_label", "uncertain") for row in covered],
            [row["truth_label"] for row in covered],
        ),
    }


def _accuracy(predicted: list[str], truth: list[str]) -> float:
    if not truth:
        return 0.0
    return sum(1 for pred, actual in zip(predicted, truth) if pred == actual) / len(truth)


class VisualExperimentSession:
    def __init__(self, *, sample_count: int, headed: bool, out_dir: Path | None = None, benchmark: str | None = None) -> None:
        self.out_dir = out_dir or new_report_dir()
        self.headed = headed
        self.benchmark = benchmark
        self.all_specs = bundled_spec_paths(benchmark=benchmark)
        self.samples = build_visual_experiment_manifest(sample_count, benchmark=benchmark)
        self.index = 0
        self.runs: dict[str, dict[str, Any]] = {}
        self.ratings: dict[str, dict[str, Any]] = {}
        self.current_run: dict[str, Any] | None = None
        self.lock = threading.Lock()
        write_json(
            self.out_dir / "visual-experiment-manifest.json",
            {
                "positioning": auxiliary_positioning("visual-experiment"),
                "benchmark": benchmark,
                "samples": [asdict(sample) for sample in self.samples],
            },
        )

    def state(self, *, reveal: bool = False) -> dict[str, Any]:
        with self.lock:
            current = self.samples[self.index] if self.index < len(self.samples) else None
            current_id = current.sample_id if current else None
            run_source = None
            if current_id:
                run_source = self.ratings.get(current_id) or (
                    self.current_run if self.current_run and self.current_run.get("sample_id") == current_id else None
                ) or self.runs.get(current_id)
            run = copy.deepcopy(run_source)
            if run and not run.get("human_label"):
                run.pop("llm_label", None)
                run.pop("llm_rationale", None)
                run.pop("llm_confidence", None)
            public_run = _public_run(run) if run else None
            rows = self._rated_rows_locked()
            completed = self._completed_count_locked()
            return {
                "complete": completed >= len(self.samples),
                "index": self.index,
                "total": len(self.samples),
                "rated": len(self.ratings),
                "can_previous": self.index > 0,
                "can_next": self.index < len(self.samples) - 1,
                "current_sample": _public_sample(current) if current else None,
                "current_run": public_run,
                "metrics": _public_metrics(rows),
            }

    def run_current(self, *, force: bool = False) -> dict[str, Any]:
        with self.lock:
            if self.index >= len(self.samples):
                complete = True
            else:
                complete = False
            sample = self.samples[self.index] if not complete else None
            if not complete and not force and sample and sample.sample_id in self.runs:
                already_run = True
            else:
                already_run = False
        if complete:
            return self.state(reveal=True)
        if already_run:
            with self.lock:
                assert sample is not None
                self.current_run = copy.deepcopy(self.ratings.get(sample.sample_id) or self.runs[sample.sample_id])
            return self.state(reveal=False)
        assert sample is not None

        sample_dir = self.out_dir / "samples" / sample.sample_id
        sample_spec = materialize_sample_spec(sample, self.all_specs, sample_dir)
        try:
            report = run_spec(
                sample_spec,
                report_dir=sample_dir / "run",
                headless=not self.headed,
                visual_required=True,
                visual_on_semantic_failure=True,
            )
            visual = report.visual_oracle_result.data
            capture_failure = bool(visual.get("capture_failure")) or (
                report.benchmark == "jynew" and visual.get("live_source") != "unity_capture"
            )
            live_screenshot = _visual_live_screenshot(visual)
            golden_image = _visual_golden_image(visual)
            llm_label, llm_confidence, llm_rationale = _visual_llm_summary(
                visual,
                report.visual_oracle_result.detail,
            )
            run = {
                "sample_id": sample.sample_id,
                "truth_label": sample.truth_label,
                "variant": sample.variant,
                "status": "failed" if capture_failure or not live_screenshot else "success",
                "run_report_path": str(sample_dir / "run" / "report.json"),
                "run_report_md_path": str(sample_dir / "run" / "report.md"),
                "live_screenshot": None if capture_failure else live_screenshot,
                "live_source": visual.get("live_source"),
                "capture_failure": capture_failure,
                "golden_image": golden_image,
                "llm_label": llm_label,
                "llm_confidence": llm_confidence,
                "llm_rationale": llm_rationale,
                "visual_status": report.visual_oracle_result.status,
                "semantic_status": {
                    "validation": report.validation.status,
                    "smoke": report.smoke_execution.status,
                    "oracle": report.oracle_result.status,
                },
            }
            if capture_failure:
                run["error"] = "Capture failed before visual scoring."
            elif not live_screenshot:
                run["error"] = "Visual scoring did not produce a live screenshot."
        except Exception as error:
            run = {
                "sample_id": sample.sample_id,
                "truth_label": sample.truth_label,
                "variant": sample.variant,
                "status": "failed",
                "error": str(error),
                "llm_label": "uncertain",
                "llm_confidence": 0.0,
                "llm_rationale": str(error),
            }
        with self.lock:
            self.runs[sample.sample_id] = copy.deepcopy(run)
            if force:
                self.ratings.pop(sample.sample_id, None)
            self.current_run = run
            self._write_reports_locked()
        return self.state(reveal=False)

    def rate_current(self, human_label: str) -> dict[str, Any]:
        if human_label not in VISUAL_LABELS:
            raise ValueError("human_label must be correct, incorrect, or uncertain")
        with self.lock:
            sample = self.samples[self.index]
            base = self.runs.get(sample.sample_id) or self.ratings.get(sample.sample_id) or self.current_run
            if base is None:
                raise ValueError("Run the current sample before rating it.")
            if base.get("capture_failure") or base.get("status") == "failed" or not base.get("live_screenshot"):
                raise ValueError("Capture failed before visual scoring; this sample cannot be rated.")
            row = copy.deepcopy(base)
            row["human_label"] = human_label
            self.ratings[sample.sample_id] = row
            self.current_run = row
            self._write_reports_locked()
        return self.state(reveal=True)

    def next_sample(self) -> dict[str, Any]:
        with self.lock:
            if self.index < len(self.samples) - 1:
                self.index += 1
            self.current_run = None
        return self.state()

    def previous_sample(self) -> dict[str, Any]:
        with self.lock:
            if self.index > 0:
                self.index -= 1
            self.current_run = None
        return self.state(reveal=True)

    def _write_reports_locked(self) -> None:
        rows = self._rated_rows_locked()
        payload = {
            "positioning": auxiliary_positioning("visual-experiment"),
            "samples": self._sample_rows_locked(),
            "ratings": rows,
            "metrics": experiment_metrics(rows),
        }
        write_json(self.out_dir / "human-ratings.json", {"ratings": rows})
        write_json(self.out_dir / "visual-experiment.json", payload)
        (self.out_dir / "visual-experiment.md").write_text(_experiment_markdown(payload), encoding="utf-8")

    def _sample_rows_locked(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        run_fields = {
            "status",
            "run_report_path",
            "run_report_md_path",
            "live_screenshot",
            "live_source",
            "capture_failure",
            "golden_image",
            "llm_label",
            "llm_confidence",
            "llm_rationale",
            "visual_status",
            "semantic_status",
            "human_label",
            "error",
        }
        for sample in self.samples:
            row = asdict(sample)
            run = self.ratings.get(sample.sample_id) or self.runs.get(sample.sample_id)
            if run:
                for key in run_fields:
                    if key in run and (key != "live_screenshot" or run[key]):
                        row[key] = copy.deepcopy(run[key])
                if run.get("status") == "failed" or run.get("error"):
                    row["run_status"] = "failed"
                    row["run_error"] = _public_run_error(str(run.get("error") or "Run failed."))
            rows.append(row)
        return rows

    def _rated_rows_locked(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(self.ratings[sample.sample_id]) for sample in self.samples if sample.sample_id in self.ratings]

    def _completed_count_locked(self) -> int:
        completed = 0
        for sample in self.samples:
            if sample.sample_id in self.ratings:
                completed += 1
                continue
            run = self.runs.get(sample.sample_id)
            if run and run.get("capture_failure"):
                completed += 1
        return completed


def _public_sample(sample: VisualExperimentSample) -> dict[str, str]:
    return {"sample_id": sample.sample_id, "title": sample.title}


def _public_run(run: dict[str, Any]) -> dict[str, Any]:
    public_keys = {
        "sample_id",
        "live_screenshot",
        "golden_image",
        "human_label",
        "llm_label",
        "llm_confidence",
        "llm_rationale",
        "live_source",
    }
    public = {
        key: copy.deepcopy(run[key])
        for key in public_keys
        if key in run and (key != "live_screenshot" or run[key])
    }
    if run.get("status") == "failed" or run.get("error"):
        public["run_status"] = "failed"
        public["run_error"] = _public_run_error(str(run.get("error") or "Run failed."))
    return public


def _public_run_error(message: str) -> str:
    cleaned = BLIND_ERROR_TOKEN_RE.sub("[redacted]", message).strip()
    return cleaned or "Run failed."


def _public_metrics(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {"rated": sum(1 for row in rows if row.get("human_label") in VISUAL_LABELS)}


def _experiment_markdown(payload: dict[str, Any]) -> str:
    metrics = payload["metrics"]
    lines = [
        "# WarpTest Visual Experiment",
        "",
        f"- Paper role: `{payload.get('positioning', {}).get('paper_role', 'unspecified')}`",
        f"- Primary claim evidence: `{payload.get('positioning', {}).get('primary_claim', False)}`",
        f"- Note: {payload.get('positioning', {}).get('note', '')}",
        f"- Rated: `{metrics['rated']}`",
        f"- Cohen's Kappa: `{metrics['kappa']:.4f}`",
        f"- Strict LLM accuracy: `{metrics['strict_llm_accuracy']:.4f}`",
        f"- Coverage excluding uncertain: `{metrics['coverage_excluding_uncertain']:.4f}`",
        f"- Accuracy excluding uncertain: `{metrics['accuracy_excluding_uncertain']:.4f}`",
        "",
        "## Confusion Matrix",
        "",
        "| Human \\ LLM | correct | incorrect | uncertain |",
        "|---|---:|---:|---:|",
    ]
    matrix = metrics["confusion_matrix"]
    for human in LABEL_ORDER:
        row = matrix[human]
        lines.append(f"| {human} | {row['correct']} | {row['incorrect']} | {row['uncertain']} |")
    lines.extend(["", "## Ratings", "", "| Sample | Truth | Human | LLM | Variant |", "|---|---|---|---|---|"])
    for row in payload["ratings"]:
        lines.append(
            f"| {row.get('sample_id')} | {row.get('truth_label')} | {row.get('human_label')} | {row.get('llm_label')} | {row.get('variant')} |"
        )
    return "\n".join(lines) + "\n"


def run_visual_experiment(sample_count: int = 50, headed: bool = True, benchmark: str | None = None) -> Path:
    session = VisualExperimentSession(sample_count=sample_count, headed=headed, benchmark=benchmark)
    port = find_free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler_for(session))
    url = f"http://127.0.0.1:{port}"
    print(f"visual_experiment_url={url}", flush=True)
    print(f"visual_experiment_report_dir={session.out_dir}", flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return session.out_dir


def _benchmark_token(value: Any) -> str:
    return str(value or "").lower().replace("_", "").replace("-", "")


def _handler_for(session: VisualExperimentSession):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send_html(_HTML)
            elif parsed.path == "/state":
                self._send_json(session.state())
            elif parsed.path == "/file":
                query = parse_qs(parsed.query)
                self._send_file(query.get("path", [""])[0])
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/run":
                    self._send_json(session.run_current())
                elif parsed.path == "/rerun":
                    self._send_json(session.run_current(force=True))
                elif parsed.path == "/rate":
                    payload = self._read_json()
                    state = session.rate_current(str(payload.get("label", "")))
                    self._send_json(state)
                elif parsed.path == "/next":
                    self._send_json(session.next_sample())
                elif parsed.path == "/previous":
                    self._send_json(session.previous_sample())
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except Exception as error:
                self._send_json({"error": str(error)}, status=500)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("content-length", "0"))
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _send_json(self, value: dict[str, Any], status: int = 200) -> None:
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_html(self, value: str) -> None:
            data = value.encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_file(self, raw_path: str) -> None:
            path = Path(unquote(raw_path)).expanduser().resolve()
            allowed_roots = [Path.cwd().resolve(), session.out_dir.resolve()]
            if not any(path == root or root in path.parents for root in allowed_roots):
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            content_type = _image_content_type(path)
            if content_type is None:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if not path.exists() or not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def _image_content_type(path: Path) -> str | None:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(path.suffix.lower())


_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WarpTest Visual Experiment</title>
  <style>
    body { font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; color: #202124; background: #f8faf9; }
    header { padding: 16px 24px; background: #1f2937; color: white; display: flex; justify-content: space-between; align-items: center; }
    main { padding: 20px 24px; max-width: 1280px; margin: 0 auto; }
    button { border: 1px solid #9ca3af; background: white; padding: 9px 12px; border-radius: 6px; cursor: pointer; font-weight: 600; }
    button.primary { background: #0f766e; border-color: #0f766e; color: white; }
    button:disabled { opacity: 0.5; cursor: wait; }
    .controls { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
    .row { display: flex; gap: 16px; align-items: flex-start; }
    .panel { border: 1px solid #d1d5db; background: white; border-radius: 8px; padding: 14px; margin-bottom: 16px; }
    .image { flex: 1; min-width: 0; }
    img { max-width: 100%; border: 1px solid #d1d5db; background: #111827; }
    .muted { color: #6b7280; }
    .labels button { margin-right: 8px; }
    pre { white-space: pre-wrap; background: #f3f4f6; padding: 12px; border-radius: 6px; }
  </style>
</head>
<body>
  <header>
    <div><strong>WarpTest Visual Experiment</strong></div>
    <div id="progress"></div>
  </header>
  <main>
    <section class="panel">
      <div id="sample"></div>
      <div class="controls">
        <button id="previous">Previous</button>
        <button id="run" class="primary">Run Current Sample</button>
        <button id="rerun">Re-run Current</button>
        <button id="next">Next</button>
      </div>
    </section>
    <section class="row">
      <div class="panel image">
        <h3>Live screenshot</h3>
        <div id="live"></div>
      </div>
      <div class="panel image">
        <h3>Golden oracle</h3>
        <div id="golden"></div>
      </div>
    </section>
    <section class="panel">
      <h3>Your label</h3>
      <p class="muted">Score only from what you can see. LLM verdict stays hidden until you submit.</p>
      <div class="labels">
        <button data-label="correct">Correct</button>
        <button data-label="incorrect">Incorrect</button>
        <button data-label="uncertain">Uncertain</button>
      </div>
    </section>
    <section class="panel">
      <h3>Status</h3>
      <pre id="verdict">Hidden</pre>
    </section>
    <section class="panel">
      <h3>Metrics</h3>
      <pre id="metrics"></pre>
    </section>
  </main>
  <script>
    const runButton = document.querySelector("#run");
    const rerunButton = document.querySelector("#rerun");
    const previousButton = document.querySelector("#previous");
    const nextButton = document.querySelector("#next");
    const labelButtons = [...document.querySelectorAll("[data-label]")];
    let lastRenderedState = null;
    let completionAudioContext = null;
    let notificationPermissionRequest = null;
    async function api(path, opts = {}) {
      const res = await fetch(path, opts);
      const json = await res.json();
      if (!res.ok) throw new Error(json.error || res.statusText);
      return json;
    }
    function fileUrl(path) {
      return path ? "/file?path=" + encodeURIComponent(path) : "";
    }
    function notificationSupported() {
      return typeof window.Notification !== "undefined";
    }
    function audioContextClass() {
      return window.AudioContext || window.webkitAudioContext;
    }
    async function unlockCompletionAudio() {
      try {
        const AudioContextClass = audioContextClass();
        if (!AudioContextClass) return;
        completionAudioContext = completionAudioContext || new AudioContextClass();
        if (completionAudioContext.state === "suspended") await completionAudioContext.resume();
      } catch (_err) {}
    }
    async function requestCompletionNotificationPermission() {
      if (!notificationSupported()) return "unsupported";
      if (Notification.permission !== "default") return Notification.permission;
      if (!notificationPermissionRequest) {
        try {
          const permission = Notification.requestPermission();
          notificationPermissionRequest = permission && typeof permission.then === "function"
            ? permission.catch(() => "denied")
            : Promise.resolve(Notification.permission);
        } catch (_err) {
          notificationPermissionRequest = Promise.resolve("denied");
        }
      }
      return notificationPermissionRequest;
    }
    function prepareCompletionAlerts() {
      void unlockCompletionAudio();
      void requestCompletionNotificationPermission();
    }
    function successfulScreenshotState(state) {
      const run = state && state.current_run ? state.current_run : {};
      return !!run.live_screenshot && run.run_status !== "failed" && !run.run_error;
    }
    function notifyCompletion(state) {
      playCompletionChime();
      try {
        if (!notificationSupported() || Notification.permission !== "granted") return;
        const sample = state.current_sample || {};
        const sampleText = [sample.title, sample.sample_id].filter(Boolean).join(" · ");
        new Notification("WarpTest sample ready", {
          body: sampleText ? `${sampleText} finished rendering.` : "Current sample finished rendering.",
          tag: `warptest-visual-${sample.sample_id || state.index}`,
          renotify: true,
          silent: true,
        });
      } catch (_err) {}
    }
    function shouldNotifyCompletion(beforeState, state, force) {
      if (!successfulScreenshotState(state)) return false;
      if (force) return true;
      if (!successfulScreenshotState(beforeState)) return true;
      const beforeRun = beforeState.current_run || {};
      const run = state.current_run || {};
      const beforeSample = beforeState.current_sample || {};
      const sample = state.current_sample || {};
      return beforeSample.sample_id !== sample.sample_id || beforeRun.live_screenshot !== run.live_screenshot;
    }
    function playCompletionChime() {
      try {
        const AudioContextClass = audioContextClass();
        if (!AudioContextClass) return;
        completionAudioContext = completionAudioContext || new AudioContextClass();
        const ctx = completionAudioContext;
        const play = () => {
          const now = ctx.currentTime;
          const gain = ctx.createGain();
          gain.gain.setValueAtTime(0.0001, now);
          gain.gain.exponentialRampToValueAtTime(0.08, now + 0.02);
          gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.32);
          gain.connect(ctx.destination);
          [880, 1174.66].forEach((frequency, index) => {
            const start = now + index * 0.09;
            const oscillator = ctx.createOscillator();
            oscillator.type = "sine";
            oscillator.frequency.setValueAtTime(frequency, start);
            oscillator.connect(gain);
            oscillator.start(start);
            oscillator.stop(start + 0.18);
            oscillator.onended = () => oscillator.disconnect();
          });
          window.setTimeout(() => {
            try { gain.disconnect(); } catch (_err) {}
          }, 450);
        };
        if (ctx.state === "suspended") {
          void ctx.resume().then(play).catch(() => {});
        } else {
          play();
        }
      } catch (_err) {}
    }
    function render(state, reveal = false) {
      document.querySelector("#progress").textContent = `${state.index + 1} / ${state.total} · Rated ${state.rated}/${state.total}${state.complete ? " · Complete" : ""}`;
      const run = state.current_run || {};
      const failed = run.run_status === "failed" || !!run.run_error;
      document.querySelector("#sample").innerHTML = state.current_sample
        ? `<strong>${state.current_sample.title}</strong><div class="muted">${state.current_sample.sample_id}${run.human_label ? " · your label: " + run.human_label : ""}</div>`
        : "<strong>Complete</strong>";
      document.querySelector("#live").innerHTML = run.live_screenshot
        ? `<img src="${fileUrl(run.live_screenshot)}">`
        : `<span class='muted'>${failed ? "Run failed; see status below." : "Run the sample to capture the live screenshot."}</span>`;
      document.querySelector("#golden").innerHTML = run.golden_image ? `<img src="${fileUrl(run.golden_image)}">` : "";
      const showVerdict = (reveal || !!run.human_label) && run.llm_label;
      document.querySelector("#verdict").textContent = failed
        ? JSON.stringify({ run_status: run.run_status || "failed", run_error: run.run_error || "Run failed." }, null, 2)
        : showVerdict
        ? JSON.stringify({ human_label: run.human_label, llm_label: run.llm_label, confidence: run.llm_confidence, rationale: run.llm_rationale }, null, 2)
        : "Hidden until you submit a label for this sample.";
      document.querySelector("#metrics").textContent = JSON.stringify(state.metrics, null, 2);
      runButton.disabled = !!run.live_screenshot || failed;
      rerunButton.disabled = !state.current_sample;
      previousButton.disabled = !state.can_previous;
      nextButton.disabled = !state.can_next;
      labelButtons.forEach(btn => btn.disabled = !run.live_screenshot || failed);
      lastRenderedState = state;
    }
    async function runSample(force = false) {
      const beforeState = lastRenderedState;
      runButton.disabled = true;
      rerunButton.disabled = true;
      const oldRunText = runButton.textContent;
      const oldRerunText = rerunButton.textContent;
      runButton.textContent = "Running...";
      rerunButton.textContent = "Running...";
      try {
        const state = await api(force ? "/rerun" : "/run", { method: "POST" });
        const shouldNotify = shouldNotifyCompletion(beforeState, state, force);
        render(state, false);
        if (shouldNotify) notifyCompletion(state);
      }
      catch (err) { alert(err.message); }
      finally {
        runButton.textContent = oldRunText;
        rerunButton.textContent = oldRerunText;
      }
    }
    runButton.addEventListener("click", () => {
      prepareCompletionAlerts();
      runSample(false);
    });
    rerunButton.addEventListener("click", () => {
      prepareCompletionAlerts();
      runSample(true);
    });
    previousButton.addEventListener("click", async () => {
      render(await api("/previous", { method: "POST" }), true);
    });
    nextButton.addEventListener("click", async () => {
      prepareCompletionAlerts();
      const state = await api("/next", { method: "POST" });
      render(state, false);
      if (!state.current_run || !state.current_run.live_screenshot) await runSample(false);
    });
    labelButtons.forEach(btn => btn.addEventListener("click", async () => {
      const state = await api("/rate", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ label: btn.dataset.label }) });
      render(state, true);
    }));
    api("/state").then(async state => {
      render(state, false);
      if (!state.current_run || !state.current_run.live_screenshot) await runSample(false);
    });
  </script>
</body>
</html>
"""
