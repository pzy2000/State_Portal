from __future__ import annotations

import json
import math
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import bpy


CHECKS: list[dict[str, Any]] = []
BLEND_ARTIFACTS: list[str] = []
SCREENSHOT_ARTIFACTS: dict[str, str] = {}
ARTIFACT_DIR = Path(".")
RENDER_CENTER: tuple[float, float, float] | None = None
RENDER_ORTHO_SCALE: float | None = None
RENDER_HELPER_PREFIXES = ("WarpTestRender", "WarpTestReference")


def main() -> None:
    request_path, report_path = _parse_args()
    report: dict[str, Any] = {
        "status": "failure",
        "checks": CHECKS,
        "request_path": str(request_path),
        "report_path": str(report_path),
        "blend_artifacts": BLEND_ARTIFACTS,
        "screenshots": SCREENSHOT_ARTIFACTS,
    }
    exit_code = 1
    hold_ms = 0
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        spec = request.get("spec") if isinstance(request.get("spec"), dict) else {}
        global ARTIFACT_DIR
        ARTIFACT_DIR = Path(str(request.get("artifact_dir") or report_path.parent)).expanduser().resolve()
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        hold_ms = int(request.get("hold_ms") or 0)

        target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
        global RENDER_CENTER, RENDER_ORTHO_SCALE
        RENDER_CENTER, RENDER_ORTHO_SCALE = _render_frame_for_spec(spec)
        _run_check("target.state_synthesized", lambda: _restore_target(target))
        _run_check("target.validate.scene", lambda: _validate_target(target))
        _run_check("visual.screenshot_before", lambda: _capture_screenshot(request, "screenshot_before", "before"))
        for index, action in enumerate(spec.get("actions", [])):
            _run_check(
                f"action[{_check_type(action)}]@{index}",
                lambda action=action: _run_action(action),
            )
        _run_check("artifact.final_blend_saved", lambda: _save_final_blend(request))
        for index, assertion in enumerate(spec.get("assertions", [])):
            _run_check(
                f"assertion[{_check_type(assertion)}]@{index}",
                lambda assertion=assertion: _run_assertion(assertion),
            )
        _run_check("visual.screenshot_after", lambda: _capture_screenshot(request, "screenshot_after", "after"))
        report.update(
            {
                "status": "success" if all(check.get("status") == "success" for check in CHECKS) else "failure",
                "checks": CHECKS,
                "blend_artifacts": BLEND_ARTIFACTS,
                "screenshots": SCREENSHOT_ARTIFACTS,
                "screenshot_before": SCREENSHOT_ARTIFACTS.get("before"),
                "screenshot_after": SCREENSHOT_ARTIFACTS.get("after"),
                "scene": _scene_summary(),
                "app": {
                    "version": ".".join(str(part) for part in bpy.app.version),
                    "background": bool(bpy.app.background),
                },
            }
        )
        exit_code = 0 if report["status"] == "success" else 1
    except Exception as error:
        CHECKS.append(
            {
                "name": "harness.exception",
                "status": "failure",
                "detail": str(error),
                "data": {"traceback": traceback.format_exc()},
                "duration_ms": 0,
            }
        )
        report.update({"status": "failure", "checks": CHECKS, "blend_artifacts": BLEND_ARTIFACTS, "screenshots": SCREENSHOT_ARTIFACTS})
    finally:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if hold_ms > 0:
            time.sleep(hold_ms / 1000.0)
        try:
            bpy.ops.wm.quit_blender()
        except Exception:
            pass
    sys.exit(exit_code)


def _parse_args() -> tuple[Path, Path]:
    if "--" not in sys.argv:
        raise SystemExit("Blender WarpTest driver requires -- <request.json> <report.json>.")
    tail = sys.argv[sys.argv.index("--") + 1 :]
    if len(tail) < 2:
        raise SystemExit("Blender WarpTest driver requires request and report paths.")
    return Path(tail[0]).expanduser().resolve(), Path(tail[1]).expanduser().resolve()


def _run_check(name: str, func) -> None:
    start = time.perf_counter()
    try:
        detail, data = func()
        CHECKS.append(
            {
                "name": name,
                "status": "success",
                "detail": detail,
                "data": data,
                "duration_ms": int((time.perf_counter() - start) * 1000),
            }
        )
    except Exception as error:
        CHECKS.append(
            {
                "name": name,
                "status": "failure",
                "detail": str(error),
                "data": {},
                "duration_ms": int((time.perf_counter() - start) * 1000),
            }
        )


def _check_type(item: Any) -> str:
    if isinstance(item, dict) and isinstance(item.get("type"), str):
        return str(item["type"])
    return "unknown"


def _restore_target(target: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if target.get("kind") not in {"scene", "blend_file"}:
        raise ValueError(f"Unsupported Blender target.kind: {target.get('kind')}")
    _clear_scene()
    for obj_spec in _list(target.get("objects")):
        _create_object(obj_spec)
    frame = target.get("frame")
    if frame is not None:
        bpy.context.scene.frame_set(int(frame))
    if isinstance(target.get("save_as"), str):
        path = _artifact_path(str(target["save_as"]))
        _save_as(path)
        if target.get("reopen_after_restore"):
            _open_mainfile(path)
    return "Synthesized Blender scene through bpy/operator APIs.", _scene_summary()


def _validate_target(target: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    for obj_spec in _list(target.get("objects")):
        obj = _object(str(obj_spec.get("name")))
        expected_type = obj_spec.get("type")
        if expected_type and obj.type != expected_type:
            raise AssertionError(f"{obj.name} type mismatch: expected {expected_type}, got {obj.type}")
        material = obj_spec.get("material")
        if isinstance(material, dict):
            _assert_material(obj, material)
    return "Blender target scene validation checks passed.", _scene_summary()


def _run_action(action: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    action_type = action.get("type")
    if action_type == "wait":
        time.sleep(int(action.get("ms", action.get("wait_ms", 0))) / 1000.0)
        return "Waited.", {}
    if action_type == "blender_add_primitive":
        obj = _create_object(action)
        return "Added Blender primitive object.", {"object": obj.name}
    if action_type == "blender_assign_material":
        obj = _object(str(action.get("object")))
        material = action.get("material")
        if not isinstance(material, dict):
            raise ValueError("blender_assign_material.material must be a mapping")
        _assign_material(obj, material)
        return "Assigned Blender material.", {"object": obj.name, "material": material.get("name")}
    if action_type == "blender_set_object_location":
        obj = _object(str(action.get("object")))
        obj.location = _vector(action.get("location"), 3, "location")
        bpy.context.view_layer.update()
        return "Set object location.", {"object": obj.name, "location": list(obj.location)}
    if action_type == "blender_set_object_scale":
        obj = _object(str(action.get("object")))
        obj.scale = _vector(action.get("scale"), 3, "scale")
        bpy.context.view_layer.update()
        return "Set object scale.", {"object": obj.name, "scale": list(obj.scale)}
    if action_type == "blender_set_object_rotation":
        obj = _object(str(action.get("object")))
        obj.rotation_euler = _vector(action.get("rotation"), 3, "rotation")
        bpy.context.view_layer.update()
        return "Set object rotation.", {"object": obj.name, "rotation": list(obj.rotation_euler)}
    if action_type == "blender_set_frame":
        frame = int(action.get("frame"))
        bpy.context.scene.frame_set(frame)
        bpy.context.view_layer.update()
        return "Set Blender current frame.", {"frame": frame}
    if action_type == "blender_add_modifier":
        obj = _object(str(action.get("object")))
        modifier_name = str(action.get("name") or action.get("modifier") or "WarpTestModifier")
        modifier_type = str(action.get("modifier_type") or "BEVEL")
        modifier = obj.modifiers.new(modifier_name, modifier_type)
        for key in ["width", "segments", "levels", "render_levels"]:
            if key in action and hasattr(modifier, key):
                setattr(modifier, key, action[key])
        bpy.context.view_layer.update()
        return "Added Blender modifier.", {"object": obj.name, "modifier": modifier.name, "type": modifier.type}
    if action_type == "blender_apply_modifier":
        obj = _object(str(action.get("object")))
        modifier_name = str(action.get("modifier") or action.get("name"))
        _select_active(obj)
        bpy.ops.object.modifier_apply(modifier=modifier_name)
        bpy.context.view_layer.update()
        return "Applied Blender modifier.", {"object": obj.name, "modifier": modifier_name, "vertices": _vertex_count(obj)}
    if action_type == "blender_set_keyframe":
        obj = _object(str(action.get("object")))
        data_path = str(action.get("data_path") or "location")
        frame = int(action.get("frame"))
        value = action.get("value")
        bpy.context.scene.frame_set(frame)
        if data_path in {"location", "scale", "rotation_euler"}:
            setattr(obj, data_path, _vector(value, 3, data_path))
        else:
            raise ValueError(f"Unsupported keyframe data_path: {data_path}")
        bpy.context.view_layer.update()
        obj.keyframe_insert(data_path=data_path, frame=frame)
        return "Inserted Blender keyframe.", {"object": obj.name, "data_path": data_path, "frame": frame}
    if action_type == "blender_save_as":
        path = _artifact_path(str(action.get("path") or action.get("filepath") or "warptest.blend"))
        _save_as(path)
        return "Saved Blender file.", {"path": str(path)}
    if action_type == "blender_reopen":
        path = _artifact_path(str(action.get("path") or action.get("filepath") or "warptest.blend"))
        _open_mainfile(path)
        return "Reopened Blender file.", {"path": str(path)}
    raise ValueError(f"Unsupported Blender action type: {action_type}")


def _run_assertion(assertion: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    assertion_type = assertion.get("type")
    if assertion_type == "blender_object_exists":
        obj = _object(str(assertion.get("object") or assertion.get("name")))
        expected_type = assertion.get("object_type")
        if expected_type and obj.type != expected_type:
            raise AssertionError(f"{obj.name} type mismatch: expected {expected_type}, got {obj.type}")
        return "Object exists.", {"object": obj.name, "type": obj.type}
    if assertion_type == "blender_material_color":
        obj = _object(str(assertion.get("object")))
        material = {"name": assertion.get("material"), "color": assertion.get("color")}
        _assert_material(obj, material)
        return "Material color matched.", {"object": obj.name, "material": material["name"]}
    if assertion_type == "blender_object_location":
        obj = _object(str(assertion.get("object")))
        expected = _vector(assertion.get("location"), 3, "location")
        tolerance = float(assertion.get("tolerance", 0.001))
        actual = list(obj.location)
        if any(abs(actual[index] - expected[index]) > tolerance for index in range(3)):
            raise AssertionError(f"{obj.name} location mismatch: expected {list(expected)}, got {actual}")
        return "Object location matched.", {"object": obj.name, "location": actual}
    if assertion_type in {"blender_object_scale", "blender_object_rotation"}:
        obj = _object(str(assertion.get("object")))
        key = "scale" if assertion_type == "blender_object_scale" else "rotation"
        actual_vector = obj.scale if key == "scale" else obj.rotation_euler
        expected = _vector(assertion.get(key), 3, key)
        tolerance = float(assertion.get("tolerance", 0.001))
        actual = list(actual_vector)
        if any(abs(actual[index] - expected[index]) > tolerance for index in range(3)):
            raise AssertionError(f"{obj.name} {key} mismatch: expected {list(expected)}, got {actual}")
        return f"Object {key} matched.", {"object": obj.name, key: actual}
    if assertion_type == "blender_current_frame_equals":
        expected = int(assertion.get("frame"))
        actual = int(bpy.context.scene.frame_current)
        if actual != expected:
            raise AssertionError(f"Current frame mismatch: expected {expected}, got {actual}")
        return "Current Blender frame matched.", {"frame": actual}
    if assertion_type == "blender_file_exists":
        path = _artifact_path(str(assertion.get("path") or assertion.get("filepath") or "warptest.blend"))
        if not path.exists():
            raise AssertionError(f"Expected Blender file does not exist: {path}")
        return "Blender file exists.", {"path": str(path), "bytes": path.stat().st_size}
    if assertion_type == "blender_modifier_exists":
        obj = _object(str(assertion.get("object")))
        modifier_name = str(assertion.get("modifier") or assertion.get("name"))
        if modifier_name not in obj.modifiers:
            raise AssertionError(f"Modifier not found on {obj.name}: {modifier_name}")
        return "Modifier exists.", {"object": obj.name, "modifier": modifier_name}
    if assertion_type == "blender_modifier_applied":
        obj = _object(str(assertion.get("object")))
        modifier_name = str(assertion.get("modifier") or assertion.get("name"))
        if modifier_name in obj.modifiers:
            raise AssertionError(f"Modifier still present on {obj.name}: {modifier_name}")
        min_vertices = assertion.get("vertex_count_min")
        vertices = _vertex_count(obj)
        if min_vertices is not None and vertices < int(min_vertices):
            raise AssertionError(f"{obj.name} vertex count {vertices} < {min_vertices}")
        return "Modifier was applied.", {"object": obj.name, "modifier": modifier_name, "vertices": vertices}
    if assertion_type == "blender_mesh_geometry":
        obj = _object(str(assertion.get("object")))
        observed: dict[str, Any] = {
            "object": obj.name,
            "vertex_count": len(obj.data.vertices),
            "edge_count": len(obj.data.edges),
            "face_count": len(obj.data.polygons),
        }
        for key in ("vertex_count", "edge_count", "face_count"):
            expected = assertion.get(key)
            if expected is not None and observed[key] != int(expected):
                raise AssertionError(
                    f"{obj.name} {key} {observed[key]} != {int(expected)}"
                )
        expected_volume = assertion.get("volume")
        if expected_volume is not None:
            observed["volume"] = _mesh_volume(obj)
            tolerance = float(assertion.get("volume_tolerance") or 0.001)
            if abs(observed["volume"] - float(expected_volume)) > tolerance:
                raise AssertionError(
                    f"{obj.name} volume {observed['volume']:.6f} is not within "
                    f"{tolerance} of {float(expected_volume):.6f}"
                )
        return "Mesh geometry matched.", observed
    if assertion_type == "blender_animation_keyframes":
        obj = _object(str(assertion.get("object")))
        data_path = str(assertion.get("data_path") or "location")
        expected = {int(frame) for frame in _list(assertion.get("frames"))}
        actual = _keyframe_frames(obj, data_path)
        missing = sorted(expected - actual)
        if missing:
            raise AssertionError(f"{obj.name} missing keyframes for {data_path}: {missing}")
        return "Animation keyframes matched.", {"object": obj.name, "data_path": data_path, "frames": sorted(actual)}
    if assertion_type == "no_blender_python_errors":
        return "No Blender Python harness errors were recorded.", {}
    raise ValueError(f"Unsupported Blender assertion type: {assertion_type}")


def _clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()


def _create_object(spec: dict[str, Any]):
    primitive = str(spec.get("primitive") or "cube")
    name = str(spec.get("name") or "WarpTestObject")
    location = _vector(spec.get("location", [0, 0, 0]), 3, "location")
    if primitive == "cube":
        bpy.ops.mesh.primitive_cube_add(size=float(spec.get("size", 2.0)), location=location)
    elif primitive == "uv_sphere":
        bpy.ops.mesh.primitive_uv_sphere_add(segments=int(spec.get("segments", 32)), ring_count=int(spec.get("ring_count", 16)), location=location)
    elif primitive == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(vertices=int(spec.get("vertices", 32)), radius=float(spec.get("radius", 1.0)), depth=float(spec.get("depth", 2.0)), location=location)
    elif primitive == "cone":
        bpy.ops.mesh.primitive_cone_add(vertices=int(spec.get("vertices", 32)), radius1=float(spec.get("radius1", 1.0)), radius2=float(spec.get("radius2", 0.0)), depth=float(spec.get("depth", 2.0)), location=location)
    else:
        raise ValueError(f"Unsupported Blender primitive: {primitive}")
    obj = bpy.context.object
    obj.name = name
    obj.data.name = f"{name}Mesh"
    if "scale" in spec:
        obj.scale = _vector(spec.get("scale"), 3, "scale")
    if "rotation" in spec:
        obj.rotation_euler = _vector(spec.get("rotation"), 3, "rotation")
    material = spec.get("material")
    if isinstance(material, dict):
        _assign_material(obj, material)
    bpy.context.view_layer.update()
    return obj


def _assign_material(obj, spec: dict[str, Any]) -> None:
    name = str(spec.get("name") or f"{obj.name}Material")
    material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    color = _vector(spec.get("color", [1, 1, 1, 1]), 4, "material.color")
    material.diffuse_color = color
    obj.color = color
    obj.data.materials.clear()
    obj.data.materials.append(material)


def _assert_material(obj, spec: dict[str, Any]) -> None:
    name = spec.get("name")
    if not obj.data.materials:
        raise AssertionError(f"{obj.name} has no material")
    material = obj.data.materials[0]
    if name and material.name != name:
        raise AssertionError(f"{obj.name} material mismatch: expected {name}, got {material.name}")
    if spec.get("color") is not None:
        expected = _vector(spec.get("color"), 4, "material.color")
        actual = list(material.diffuse_color)
        if any(abs(actual[index] - expected[index]) > 0.001 for index in range(4)):
            raise AssertionError(f"{obj.name} material color mismatch: expected {list(expected)}, got {actual}")


def _save_as(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    filepaths = bpy.context.preferences.filepaths
    original_preview_type = filepaths.file_preview_type
    filepaths.file_preview_type = "NONE"
    try:
        bpy.ops.wm.save_as_mainfile(filepath=str(path), check_existing=False)
    finally:
        filepaths.file_preview_type = original_preview_type
    artifact = str(path)
    if artifact not in BLEND_ARTIFACTS:
        BLEND_ARTIFACTS.append(artifact)


def _save_final_blend(request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    spec_path = Path(str(request.get("spec_path") or "blender-scene.yaml"))
    stem = spec_path.stem or "blender-scene"
    path = _artifact_path(f"{stem}.blend")
    _save_as(path)
    return "Saved final Blender scene artifact.", {"path": str(path), "bytes": path.stat().st_size}


def _capture_screenshot(request: dict[str, Any], request_key: str, label: str) -> tuple[str, dict[str, Any]]:
    spec_path = Path(str(request.get("spec_path") or "blender-scene.yaml"))
    stem = spec_path.stem or "blender-scene"
    requested = request.get(request_key)
    relative = str(requested).strip() if isinstance(requested, str) and requested.strip() else f"screenshots/{stem}.{label}.png"
    path = _artifact_path(relative)
    _render_scene_png(path)
    SCREENSHOT_ARTIFACTS[label] = str(path)
    return f"Rendered Blender {label} screenshot.", {"path": str(path), "bytes": path.stat().st_size}


def _render_scene_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    original = {
        "camera": scene.camera,
        "filepath": scene.render.filepath,
        "resolution_x": scene.render.resolution_x,
        "resolution_y": scene.render.resolution_y,
        "film_transparent": scene.render.film_transparent,
        "image_format": scene.render.image_settings.file_format,
        "engine": scene.render.engine,
    }
    temp_objects = []
    try:
        camera, light = _add_render_camera_and_light()
        temp_objects.extend([camera, light])
        temp_objects.extend(_add_render_reference_objects(camera))
        scene.camera = camera
        scene.render.filepath = str(path)
        scene.render.resolution_x = 1280
        scene.render.resolution_y = 720
        scene.render.film_transparent = False
        scene.render.image_settings.file_format = "PNG"
        _configure_render_display(scene)
        bpy.ops.render.render(write_still=True)
    finally:
        scene.camera = original["camera"]
        scene.render.filepath = original["filepath"]
        scene.render.resolution_x = original["resolution_x"]
        scene.render.resolution_y = original["resolution_y"]
        scene.render.film_transparent = original["film_transparent"]
        scene.render.image_settings.file_format = original["image_format"]
        try:
            scene.render.engine = original["engine"]
        except Exception:
            pass
        for obj in temp_objects:
            _remove_temp_object(obj)


def _configure_render_display(scene) -> None:
    for engine in ("BLENDER_WORKBENCH", "BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        try:
            scene.render.engine = engine
            break
        except Exception:
            continue
    try:
        scene.display.shading.color_type = "MATERIAL"
        scene.display.shading.light = "STUDIO"
    except Exception:
        pass
    try:
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "Medium High Contrast"
        scene.view_settings.exposure = 0
        scene.view_settings.gamma = 1
    except Exception:
        pass


def _add_render_camera_and_light():
    center, scale = _render_frame()
    distance = max(7.0, scale * 1.15)
    camera_location = (
        center[0] + distance,
        center[1] - distance,
        center[2] + max(3.0, distance * 0.65),
    )
    camera_data = bpy.data.cameras.new("WarpTestRenderCameraData")
    camera = bpy.data.objects.new("WarpTestRenderCamera", camera_data)
    bpy.context.scene.collection.objects.link(camera)
    camera.location = camera_location
    camera.name = "WarpTestRenderCamera"
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = scale
    _look_at(camera, center)

    light_data = bpy.data.lights.new("WarpTestRenderLightData", type="AREA")
    light = bpy.data.objects.new("WarpTestRenderLight", light_data)
    bpy.context.scene.collection.objects.link(light)
    light.location = (center[0] + distance * 0.5, center[1] - distance * 0.5, center[2] + distance)
    light.name = "WarpTestRenderLight"
    light.data.energy = 450
    light.data.size = max(4.0, scale * 0.6)
    return camera, light


def _add_render_reference_objects(camera) -> list[Any]:
    extent = _reference_extent()
    axis_depth = max(0.025, extent * 0.004)
    grid_depth = max(0.008, extent * 0.0015)
    axis_label_size = max(0.32, extent * 0.055)
    object_label_size = max(0.24, extent * 0.04)
    grid_z = 0.0

    red = _render_material("WarpTestReferenceRed", (1.0, 0.05, 0.04, 1.0))
    green = _render_material("WarpTestReferenceGreen", (0.0, 0.75, 0.18, 1.0))
    blue = _render_material("WarpTestReferenceBlue", (0.08, 0.22, 1.0, 1.0))
    grid = _render_material("WarpTestReferenceGrid", (0.62, 0.65, 0.68, 1.0))
    label = _render_material("WarpTestReferenceLabel", (0.92, 0.94, 0.96, 1.0))

    objects: list[Any] = []
    objects.append(_add_curve_line("WarpTestReferenceAxisX", (-extent, 0.0, grid_z), (extent, 0.0, grid_z), red, axis_depth))
    objects.append(_add_curve_line("WarpTestReferenceAxisY", (0.0, -extent, grid_z), (0.0, extent, grid_z), green, axis_depth))
    objects.append(_add_curve_line("WarpTestReferenceAxisZ", (0.0, 0.0, 0.0), (0.0, 0.0, extent), blue, axis_depth))

    for value in range(-extent, extent + 1):
        if value == 0:
            continue
        objects.append(
            _add_curve_line(
                f"WarpTestReferenceGridX{value:+d}",
                (-extent, float(value), grid_z),
                (extent, float(value), grid_z),
                grid,
                grid_depth,
            )
        )
        objects.append(
            _add_curve_line(
                f"WarpTestReferenceGridY{value:+d}",
                (float(value), -extent, grid_z),
                (float(value), extent, grid_z),
                grid,
                grid_depth,
            )
        )

    label_offset = max(0.35, extent * 0.06)
    label_extent = extent * 0.82
    objects.append(_add_text_label("WarpTestReferenceLabelOrigin", "O [0,0,0]", (0.0, 0.0, label_offset), camera, object_label_size, label))
    objects.append(_add_text_label("WarpTestReferenceLabelX", "X", (label_extent, 0.0, label_offset), camera, axis_label_size, red))
    objects.append(_add_text_label("WarpTestReferenceLabelY", "Y", (0.0, label_extent, label_offset), camera, axis_label_size, green))
    objects.append(
        _add_text_label(
            "WarpTestReferenceLabelZ",
            "Z",
            (-label_offset * 1.8, -label_offset * 1.8, max(1.2, extent * 0.25)),
            camera,
            axis_label_size,
            blue,
        )
    )

    for index, obj in enumerate(_real_scene_objects()):
        location = tuple(float(value) for value in obj.location)
        dimensions = getattr(obj, "dimensions", (1.0, 1.0, 1.0))
        height = max(float(value) for value in dimensions) if dimensions else 1.0
        label_location = (location[0], location[1], location[2] + max(0.45, height * 0.7))
        text = f"{obj.name} {_format_coord(location)}"
        objects.append(_add_text_label(f"WarpTestReferenceObjectLabel{index}", text, label_location, camera, object_label_size, label))

    return objects


def _reference_extent() -> int:
    points: list[tuple[float, float, float]] = [(0.0, 0.0, 0.0)]
    for obj in _real_scene_objects():
        location = tuple(float(value) for value in obj.location)
        points.append(location)
        dimensions = getattr(obj, "dimensions", (1.0, 1.0, 1.0))
        extent = max(float(value) for value in dimensions) / 2.0 if dimensions else 0.5
        points.append((location[0] + extent, location[1] + extent, location[2] + extent))
        points.append((location[0] - extent, location[1] - extent, location[2] - extent))
    if RENDER_CENTER is not None and RENDER_ORTHO_SCALE is not None:
        half = RENDER_ORTHO_SCALE / 2.0
        points.append((RENDER_CENTER[0] + half, RENDER_CENTER[1] + half, RENDER_CENTER[2] + half))
        points.append((RENDER_CENTER[0] - half, RENDER_CENTER[1] - half, RENDER_CENTER[2] - half))
    max_abs = max(max(abs(value) for value in point) for point in points)
    return max(4, int(math.ceil(max_abs)) + 1)


def _add_curve_line(name: str, start: tuple[float, float, float], end: tuple[float, float, float], material, bevel_depth: float):
    curve = bpy.data.curves.new(f"{name}Data", "CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = 1
    curve.bevel_depth = bevel_depth
    curve.bevel_resolution = 2
    spline = curve.splines.new("POLY")
    spline.points.add(1)
    spline.points[0].co = (start[0], start[1], start[2], 1.0)
    spline.points[1].co = (end[0], end[1], end[2], 1.0)
    obj = bpy.data.objects.new(name, curve)
    bpy.context.scene.collection.objects.link(obj)
    obj.data.materials.append(material)
    return obj


def _add_text_label(name: str, body: str, location: tuple[float, float, float], camera, size: float, material):
    data = bpy.data.curves.new(f"{name}Data", "FONT")
    data.body = body
    data.align_x = "CENTER"
    data.align_y = "CENTER"
    data.size = size
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = location
    obj.data.materials.append(material)
    _face_camera(obj, camera)
    return obj


def _render_material(name: str, color: tuple[float, float, float, float]):
    material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    material.diffuse_color = color
    return material


def _render_frame() -> tuple[tuple[float, float, float], float]:
    if RENDER_CENTER is not None and RENDER_ORTHO_SCALE is not None:
        return RENDER_CENTER, RENDER_ORTHO_SCALE
    center, radius = _scene_bounds((0.0, 0.0, 0.75))
    return center, max(7.0, radius * 2.6)


def _render_frame_for_spec(spec: dict[str, Any]) -> tuple[tuple[float, float, float], float]:
    points: list[tuple[tuple[float, float, float], float]] = []
    target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
    for obj_spec in _list(target.get("objects")):
        if not isinstance(obj_spec, dict):
            continue
        location = _optional_vector(obj_spec.get("location"), 3)
        if location is None:
            location = (0.0, 0.0, 0.0)
        extent = max(0.5, float(obj_spec.get("size", 1.0)) / 2.0 if isinstance(obj_spec.get("size"), (int, float)) else 0.5)
        points.append((location, extent))
    for action in _list(spec.get("actions")):
        if not isinstance(action, dict):
            continue
        if action.get("type") == "blender_set_object_location":
            location = _optional_vector(action.get("location"), 3)
            if location is not None:
                points.append((location, 0.75))
        elif action.get("type") == "blender_set_keyframe":
            value = _optional_vector(action.get("value"), 3)
            if value is not None:
                points.append((value, 0.75))
    if not points:
        return (0.0, 0.0, 0.75), 7.0
    center = tuple(sum(point[0][index] for point in points) / len(points) for index in range(3))
    radius = 1.0
    for point, extent in points:
        distance = sum((point[index] - center[index]) ** 2 for index in range(3)) ** 0.5
        radius = max(radius, distance + extent)
    return center, max(8.5, radius * 4.0)


def _scene_bounds(default_center: tuple[float, float, float]) -> tuple[tuple[float, float, float], float]:
    objects = _real_scene_objects()
    if not objects:
        return default_center, 2.0
    locations = [[float(value) for value in obj.location] for obj in objects]
    center = tuple(sum(location[index] for location in locations) / len(locations) for index in range(3))
    radius = 1.0
    for obj, location in zip(objects, locations):
        dimensions = getattr(obj, "dimensions", (1.0, 1.0, 1.0))
        extent = max(float(value) for value in dimensions) / 2.0
        distance = sum((location[index] - center[index]) ** 2 for index in range(3)) ** 0.5
        radius = max(radius, distance + extent)
    return center, radius


def _look_at(obj, target: tuple[float, float, float]) -> None:
    from mathutils import Vector

    direction = Vector(target) - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def _face_camera(obj, camera) -> None:
    from mathutils import Vector

    direction = camera.location - Vector(obj.location)
    obj.rotation_euler = direction.to_track_quat("Z", "Y").to_euler()


def _remove_temp_object(obj) -> None:
    try:
        data = getattr(obj, "data", None)
        bpy.data.objects.remove(obj, do_unlink=True)
        if data is None or getattr(data, "users", 0) != 0:
            return
        for collection_name in ("cameras", "lights", "curves", "meshes"):
            collection = getattr(bpy.data, collection_name, None)
            if collection is not None:
                try:
                    collection.remove(data)
                    return
                except Exception:
                    pass
    except Exception:
        pass


def _open_mainfile(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(path)
    bpy.ops.wm.open_mainfile(filepath=str(path))


def _artifact_path(value: str) -> Path:
    text = value.strip()
    if not text:
        raise ValueError("Blender artifact path must be a non-empty relative path")
    path = Path(text)
    if not path.name:
        raise ValueError(f"Blender artifact path must name a file: {value}")
    if path.is_absolute():
        raise ValueError(f"Blender artifact path must be relative: {value}")
    root = ARTIFACT_DIR.resolve()
    resolved = (root / path).resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"Blender artifact path escapes artifact dir: {value}")
    return resolved


def _object(name: str):
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise AssertionError(f"Blender object not found: {name}")
    return obj


def _select_active(obj) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def _vertex_count(obj) -> int:
    data = getattr(obj, "data", None)
    vertices = getattr(data, "vertices", None)
    return len(vertices) if vertices is not None else 0


def _mesh_volume(obj) -> float:
    import bmesh  # noqa: PLC0415 - only available inside Blender

    mesh = bmesh.new()
    try:
        mesh.from_mesh(obj.data)
        return float(mesh.calc_volume())
    finally:
        mesh.free()


def _keyframe_frames(obj, data_path: str) -> set[int]:
    action = obj.animation_data.action if obj.animation_data and obj.animation_data.action else None
    if action is None:
        return set()
    frames: set[int] = set()
    for curve in _action_fcurves(action):
        if curve.data_path == data_path:
            frames.update(int(point.co.x) for point in curve.keyframe_points)
    return frames


def _action_fcurves(action) -> list[Any]:
    fcurves = getattr(action, "fcurves", None)
    if fcurves is not None:
        return list(fcurves)

    curves: list[Any] = []
    for layer in getattr(action, "layers", []):
        for strip in getattr(layer, "strips", []):
            for channelbag in getattr(strip, "channelbags", []):
                curves.extend(list(getattr(channelbag, "fcurves", [])))
    return curves


def _scene_summary() -> dict[str, Any]:
    return {
        "objects": [
            {
                "name": obj.name,
                "type": obj.type,
                "location": [round(float(value), 4) for value in obj.location],
                "vertices": _vertex_count(obj),
                "materials": [material.name for material in getattr(obj.data, "materials", [])],
                "modifiers": [modifier.name for modifier in obj.modifiers],
            }
            for obj in _real_scene_objects()
        ],
        "frame": int(bpy.context.scene.frame_current),
    }


def _real_scene_objects() -> list[Any]:
    return [
        obj
        for obj in bpy.context.scene.objects
        if getattr(obj, "type", None) not in {"CAMERA", "LIGHT"} and not _is_render_helper_object(obj)
    ]


def _is_render_helper_object(obj) -> bool:
    name = str(getattr(obj, "name", ""))
    return any(name.startswith(prefix) for prefix in RENDER_HELPER_PREFIXES)


def _format_coord(values: tuple[float, float, float]) -> str:
    return "[" + ",".join(_format_number(value) for value in values) + "]"


def _format_number(value: float) -> str:
    if abs(value - round(value)) < 0.0001:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _vector(value: Any, length: int, label: str):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"{label} must be a {length}-item list")
    return tuple(float(item) for item in value)


def _optional_vector(value: Any, length: int) -> tuple[float, ...] | None:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        return None
    try:
        return tuple(float(item) for item in value)
    except Exception:
        return None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


if __name__ == "__main__":
    main()
