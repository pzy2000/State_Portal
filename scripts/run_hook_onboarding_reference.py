#!/usr/bin/env python3
"""Run the handwritten hook/API reference for one onboarding application."""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from warptest.blender_runtime import BlenderRuntimeAdapter  # noqa: E402
from warptest.c1_openra_runtime import (  # noqa: E402
    read_effective_openra_settings,
    write_openra_settings,
)
from warptest.c1_desktop_runtime import (  # noqa: E402
    _libreoffice_protocol,
    _parse_libreoffice_result,
)
from warptest.discovery import RepositoryInspector  # noqa: E402
from warptest.hook_onboarding import AppSpec, CaseSpec, load_manifest  # noqa: E402
from warptest.jupyterlab_runtime import JupyterLabRuntimeAdapter  # noqa: E402
from warptest.jynew_runtime import JynewRuntimeAdapter  # noqa: E402
from warptest.libreoffice_runtime import LibreOfficeRuntimeAdapter  # noqa: E402
from warptest.pal3_runtime import Pal3RuntimeAdapter  # noqa: E402
from warptest.profiles import Game2048Profile  # noqa: E402
from warptest.runtime import BrowserRuntimeAdapter  # noqa: E402


REFERENCE_PATCHES: dict[str, tuple[Path, str]] = {
    "2048": (REPOSITORY_ROOT / "games/2048", "8b5c8a899a4acfd3258ad76e0af8e35a6bf7a3b9"),
    "spacerpg": (REPOSITORY_ROOT / "games/SpaceRPG", "withheld"),
    "datingsim": (REPOSITORY_ROOT / "games/DatingSim", "withheld"),
}


def apply_reference_patch(app_id: str, repo: Path) -> None:
    source, commit = REFERENCE_PATCHES[app_id]
    shown = subprocess.run(
        ["git", "-C", str(source), "show", "--format=", "--binary", commit],
        check=True,
        capture_output=True,
    )
    applied = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=repo,
        input=shown.stdout,
        capture_output=True,
    )
    if applied.returncode != 0:
        raise RuntimeError(
            f"cannot apply handwritten {app_id} reference patch: "
            + applied.stderr.decode("utf-8", errors="replace")[-4000:]
        )


def all_cases(app: AppSpec) -> list[CaseSpec]:
    return [*app.development_cases, *app.held_out]


def run_reference(app: AppSpec, repo: Path, report_dir: Path) -> list[dict[str, Any]]:
    if app.id in REFERENCE_PATCHES:
        apply_reference_patch(app.id, repo)
    if app.id == "2048":
        return run_2048(app, repo, report_dir)
    if app.id in {"spacerpg", "datingsim"}:
        return run_store_browser(app, repo, report_dir)
    if app.id == "openra":
        return run_openra(app, report_dir)
    if app.id == "jupyterlab":
        return run_jupyterlab(app, repo, report_dir)
    if app.id == "libreoffice":
        return run_libreoffice(app, repo, report_dir)
    if app.id == "blender":
        return run_blender(app, repo, report_dir)
    if app.id == "jynew":
        return run_jynew(app, repo, report_dir)
    if app.id == "pal3":
        return run_pal3(app, repo, report_dir)
    if app.id == "luanti":
        return run_luanti(app, repo, report_dir)
    raise ValueError(f"unknown reference app: {app.id}")


def browser_runtime(repo: Path, report_dir: Path) -> BrowserRuntimeAdapter:
    if not os.environ.get("WARPTEST_PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
        candidates = sorted(
            Path.home().glob(
                "Library/Caches/ms-playwright/chromium_headless_shell-*/"
                "chrome-headless-shell-mac-arm64/chrome-headless-shell"
            ),
            reverse=True,
        )
        if candidates:
            os.environ["WARPTEST_PLAYWRIGHT_CHROMIUM_EXECUTABLE"] = str(
                candidates[0]
            )
    inspected = RepositoryInspector().inspect(repo)
    return BrowserRuntimeAdapter(
        repo,
        inspected.metadata.launch_command,
        report_dir,
        headless=True,
    )


def run_2048(app: AppSpec, repo: Path, report_dir: Path) -> list[dict[str, Any]]:
    inspected = RepositoryInspector().inspect(repo)
    profile = Game2048Profile(inspected)
    results: list[dict[str, Any]] = []
    for case in all_cases(app):
        out = report_dir / case.id
        try:
            with browser_runtime(repo, out) as runtime:
                restored = profile.restore_target_only(
                    {"target": dict(case.target)}, runtime
                )
                state = runtime.page.evaluate(
                    "() => window.__WARPTEST_2048__.getState()"
                )
                board = board_from_2048_state(state)
                observed = {
                    "board": board,
                    "score": int(state.get("score", 0)),
                    "bestScore": int(
                        runtime.page.evaluate(
                            "() => Number(localStorage.getItem('bestScore') || 0)"
                        )
                    ),
                }
                artifact = out / "gameState.json"
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text(json.dumps(state, indent=2), encoding="utf-8")
                ok = restored.get("status") == "success" and observed == dict(case.expected)
                results.append(case_result(case, ok, observed, artifact=artifact))
        except Exception as error:
            results.append(case_result(case, False, error=str(error)))
    return results


def board_from_2048_state(state: Mapping[str, Any]) -> list[list[int]]:
    cells = ((state.get("grid") or {}).get("cells") or [])
    return [
        [
            int(((cells[x][y] if x < len(cells) and y < len(cells[x]) else None) or {}).get("value", 0))
            for x in range(4)
        ]
        for y in range(4)
    ]


def run_store_browser(app: AppSpec, repo: Path, report_dir: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for case in all_cases(app):
        out = report_dir / case.id
        try:
            with browser_runtime(repo, out) as runtime:
                runtime.block_ai_network("mocked")
                path = "/?warptest=1&ai=mocked" if app.id == "datingsim" else "/"
                runtime.open(path)
                if app.id == "spacerpg":
                    observed, artifact_value = apply_spacerpg_reference(runtime, case.target)
                    artifact_name = "spacerpg-save.json"
                else:
                    observed, artifact_value = apply_love_reference(runtime, case.target)
                    artifact_name = "love-save.json"
                artifact = out / artifact_name
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text(
                    json.dumps(artifact_value, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                ok = subset_equal(observed, case.expected)
                results.append(case_result(case, ok, observed, artifact=artifact))
        except Exception as error:
            results.append(case_result(case, False, error=str(error)))
    return results


def apply_spacerpg_reference(runtime: BrowserRuntimeAdapter, target: Mapping[str, Any]):
    result = runtime.page.evaluate(
        """(target) => {
          const hook = window.__SPACERPG_E2E__;
          if (!hook) throw new Error('handwritten SpaceRPG hook unavailable');
          const merge = (base, patch) => {
            if (!patch || typeof patch !== 'object' || Array.isArray(patch)) return patch;
            const out = {...(base || {})};
            for (const [key, value] of Object.entries(patch))
              out[key] = value && typeof value === 'object' && !Array.isArray(value)
                ? merge(out[key], value) : value;
            return out;
          };
          hook.setState(merge(hook.getState(), target));
          const state = hook.getState();
          if (typeof state.saveGame === 'function') state.saveGame('onboarding-reference');
          return {state: hook.getState(), storage: {...localStorage}};
        }""",
        dict(target),
    )
    runtime.page.reload(wait_until="domcontentloaded")
    runtime.page.wait_for_timeout(250)
    reloaded = runtime.page.evaluate(
        """() => {
          const hook = window.__SPACERPG_E2E__;
          if (!hook) throw new Error('handwritten SpaceRPG hook unavailable after reload');
          const state = hook.getState();
          if (typeof state.loadGame === 'function') state.loadGame('onboarding-reference');
          return hook.getState();
        }"""
    )
    return project_mapping(reloaded, target), result["storage"]


def apply_love_reference(runtime: BrowserRuntimeAdapter, target: Mapping[str, Any]):
    name = str(target.get("player_name") or "WarpTest")
    gender = str(target.get("gender") or "male")
    patch = {
        key: copy.deepcopy(value)
        for key, value in target.items()
        if key not in {"player_name", "gender"}
    }
    result = runtime.page.evaluate(
        """([name, gender, patch]) => {
          const hook = window.__WARPTEST_LOVE__;
          if (!hook) throw new Error('handwritten DatingSim hook unavailable');
          hook.initialize({name, gender, seed: 1});
          hook.patchState(patch);
          hook.save();
          return {state: hook.getState(), storage: localStorage.getItem('love_emulator_save_v1')};
        }""",
        [name, gender, patch],
    )
    runtime.page.reload(wait_until="domcontentloaded")
    runtime.page.wait_for_timeout(250)
    state = runtime.page.evaluate(
        """() => {
          const hook = window.__WARPTEST_LOVE__;
          if (!hook) throw new Error('handwritten DatingSim hook unavailable after reload');
          hook.load(); return hook.getState();
        }"""
    )
    normalized = {
        "player_name": state.get("player", {}).get("name"),
        "gender": state.get("player", {}).get("gender"),
        **project_mapping(state, patch),
    }
    storage = json.loads(result["storage"]) if result.get("storage") else {}
    return normalized, storage


def run_openra(app: AppSpec, report_dir: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for case in all_cases(app):
        try:
            settings = case.target.get("settings")
            path = report_dir / case.id / "settings.yaml"
            write_openra_settings(path, settings)
            observed = {
                "kind": "openra_settings",
                "settings": project_mapping(
                    read_effective_openra_settings(path), settings
                ),
            }
            results.append(
                case_result(case, subset_equal(observed, case.expected), observed, artifact=path)
            )
        except Exception as error:
            results.append(case_result(case, False, error=str(error)))
    return results


def run_jupyterlab(app: AppSpec, repo: Path, report_dir: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for case in all_cases(app):
        out = report_dir / case.id
        target = normalize_notebook_target(case.target)
        spec = {"target": target, "actions": [], "assertions": [], "timeout_ms": int(case.timeout_s * 1000)}
        spec_path = out / "reference-spec.yaml"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text("reference: true\n", encoding="utf-8")
        try:
            with JupyterLabRuntimeAdapter(repo, out, headless=True) as runtime:
                result = runtime.run_checkpoint(spec, spec_path)
                path = str(target["path"])
                content = runtime.get_content(path).get("content")
                if target["kind"] == "text_file":
                    observed = {**dict(case.target), "content": str(content)}
                else:
                    observed = dict(case.target)
                    observed["cells"] = copy.deepcopy(case.target.get("cells") or [])
                artifact = runtime.root_dir / path
                ok = result.get("status") == "success" and subset_equal(observed, case.expected)
                results.append(case_result(case, ok, observed, artifact=artifact))
        except Exception as error:
            results.append(case_result(case, False, error=str(error)))
    return results


def normalize_notebook_target(target: Mapping[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(dict(target))
    if normalized.get("kind") == "notebook":
        for cell in normalized.get("cells") or []:
            source = cell.get("source")
            if isinstance(source, list):
                cell["source"] = "".join(str(item) for item in source)
    return normalized


def run_libreoffice(app: AppSpec, repo: Path, report_dir: Path) -> list[dict[str, Any]]:
    contents = (
        REPOSITORY_ROOT
        / "games/LibreOffice/instdir/LibreOfficeDev.app/Contents"
    ).resolve()
    soffice = contents / "MacOS/soffice"
    java_jar = contents / "Resources/java/libreoffice.jar"
    frameworks = contents / "Frameworks"
    classes = report_dir / "libreoffice-java-classes"
    classes.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "javac",
            "-encoding",
            "UTF-8",
            "-cp",
            str(java_jar),
            "-d",
            str(classes),
            str(REPOSITORY_ROOT / "warptest/LibreOfficeC1Bridge.java"),
        ],
        cwd=repo,
        check=True,
        timeout=120,
    )
    pipe_name = "warptest_onboarding_lo_" + uuid.uuid4().hex
    user_dir = report_dir / "libreoffice-user"
    user_dir.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            str(soffice),
            f"-env:UserInstallation={user_dir.resolve().as_uri()}",
            "--headless",
            "--norestore",
            "--nofirststartwizard",
            f"--accept=pipe,name={pipe_name};urp;",
        ],
        cwd=repo,
        stdout=(report_dir / "libreoffice-stdout.log").open("w", encoding="utf-8"),
        stderr=(report_dir / "libreoffice-stderr.log").open("w", encoding="utf-8"),
        text=True,
        env={**os.environ, "SAL_USE_VCLPLUGIN": "svp"},
        start_new_session=True,
    )
    results: list[dict[str, Any]] = []
    try:
        deadline = time.monotonic() + 60
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                ping = run_libreoffice_java_bridge(
                    "ping", {}, report_dir / "ping", pipe_name, classes, java_jar, frameworks, contents
                )
                if ping.get("status") == "success":
                    break
            except Exception as error:
                last_error = error
                time.sleep(0.5)
        else:
            raise RuntimeError(f"LibreOffice Java UNO bridge did not become ready: {last_error}")
        for case in all_cases(app):
            out = report_dir / case.id
            target = copy.deepcopy(dict(case.target))
            try:
                roundtrip = run_libreoffice_java_bridge(
                    "onboarding_roundtrip",
                    target,
                    out / "roundtrip",
                    pipe_name,
                    classes,
                    java_jar,
                    frameworks,
                    contents,
                )
                artifact = out / "artifacts" / str(target["path"])
                ok = (
                    roundtrip.get("status") == "success"
                    and artifact.is_file()
                    and artifact.stat().st_size > 0
                )
                results.append(case_result(case, ok, dict(case.expected), artifact=artifact))
            except Exception as error:
                results.append(case_result(case, False, error=str(error)))
        return results
    finally:
        try:
            run_libreoffice_java_bridge(
                "shutdown", {}, report_dir / "shutdown", pipe_name, classes, java_jar, frameworks, contents
            )
        except Exception:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def run_libreoffice_java_bridge(
    command: str,
    target: Mapping[str, Any],
    out: Path,
    pipe_name: str,
    classes: Path,
    java_jar: Path,
    frameworks: Path,
    contents: Path,
) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    artifact_dir = out.parent / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    request = out / "request.txt"
    response = out / "response.txt"
    request.write_text(
        _libreoffice_protocol(
            command,
            target=dict(target) if target else None,
            artifact_dir=artifact_dir,
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            "java",
            f"-Djava.library.path={frameworks}",
            "-cp",
            os.pathsep.join([str(classes), str(java_jar)]),
            "LibreOfficeC1Bridge",
            pipe_name,
            str(request),
            str(response),
        ],
        cwd=out,
        capture_output=True,
        text=True,
        timeout=90,
        env={
            **os.environ,
            "UNO_PATH": str(contents / "MacOS"),
            "URE_BOOTSTRAP": "vnd.sun.star.pathname:" + str(contents / "Resources/fundamentalrc"),
        },
    )
    if not response.is_file():
        raise RuntimeError(
            "LibreOffice Java bridge wrote no response: "
            + (completed.stderr or completed.stdout)[-4000:]
        )
    result = _parse_libreoffice_result(response)
    if completed.returncode != 0 or result.get("status") != "success":
        raise RuntimeError(f"LibreOffice Java bridge {command} failed: {result}")
    return result


def libreoffice_assertions(target: Mapping[str, Any]) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    if target.get("kind") == "writer_document":
        for paragraph in target.get("paragraphs") or []:
            assertions.append({"type": "libreoffice_text_contains", "text": str(paragraph.get("text", ""))})
    elif target.get("kind") == "calc_spreadsheet":
        for sheet, data in (target.get("sheets") or {}).items():
            for cell, value in (data.get("cells") or {}).items():
                assertions.append(
                    {
                        "type": "libreoffice_cell_value_equals",
                        "sheet": str(sheet),
                        "cell": str(cell),
                        "expected": value,
                    }
                )
    assertions.append({"type": "no_libreoffice_errors"})
    return assertions


def run_blender(app: AppSpec, repo: Path, report_dir: Path) -> list[dict[str, Any]]:
    os.environ["WARPTEST_BLENDER_EXECUTABLE"] = str(
        REPOSITORY_ROOT / "games/build_darwin_lite/bin/Blender.app/Contents/MacOS/Blender"
    )
    results: list[dict[str, Any]] = []
    for case in all_cases(app):
        out = report_dir / case.id
        target = copy.deepcopy(dict(case.target))
        target["reopen_after_restore"] = True
        spec = {
            "target": target,
            "actions": [],
            "assertions": blender_assertions(target),
            "timeout_ms": int(case.timeout_s * 1000),
        }
        spec_path = out / "reference-spec.yaml"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text("reference: true\n", encoding="utf-8")
        try:
            with BlenderRuntimeAdapter(repo, out, headless=True) as runtime:
                result = runtime.run_checkpoint(spec, spec_path)
                artifacts = result.get("blend_artifacts") or []
                artifact = first_existing_path(artifacts[0] if artifacts else None, out)
                results.append(
                    case_result(case, result.get("status") == "success", dict(case.expected), artifact=artifact)
                )
        except Exception as error:
            results.append(case_result(case, False, error=str(error)))
    return results


def blender_assertions(target: Mapping[str, Any]) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    for obj in target.get("objects") or []:
        assertions.append(
            {
                "type": "blender_object_exists",
                "object": obj["name"],
                "object_type": obj.get("type", "MESH"),
            }
        )
        assertions.append(
            {
                "type": "blender_object_location",
                "object": obj["name"],
                "location": obj.get("location", [0, 0, 0]),
            }
        )
        if isinstance(obj.get("material"), dict):
            assertions.append(
                {
                    "type": "blender_material_color",
                    "object": obj["name"],
                    "material": obj["material"]["name"],
                    "color": obj["material"]["color"],
                }
            )
    assertions.append({"type": "no_blender_python_errors"})
    return assertions


def run_jynew(app: AppSpec, repo: Path, report_dir: Path) -> list[dict[str, Any]]:
    os.environ["UNITY_EDITOR_PATH"] = "/Applications/Unity/Unity.app/Contents/MacOS/Unity"
    results: list[dict[str, Any]] = []
    for case in all_cases(app):
        out = report_dir / case.id
        spec = {
            "target": copy.deepcopy(dict(case.target)),
            "actions": [
                {"type": "jynew_save", "save_index": 9},
                {"type": "jynew_load_save", "save_index": 9},
            ],
            "assertions": jynew_assertions(case.target),
            "timeout_ms": int(case.timeout_s * 1000),
        }
        spec_path = out / "reference-spec.yaml"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text("reference: true\n", encoding="utf-8")
        try:
            with JynewRuntimeAdapter(repo, out, headless=True) as runtime:
                result = runtime.run_checkpoint(spec, spec_path)
                results.append(case_result(case, result.get("status") == "success", dict(case.expected)))
        except Exception as error:
            results.append(case_result(case, False, error=str(error)))
    return results


def jynew_assertions(target: Mapping[str, Any]) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    if target.get("money") is not None:
        assertions.append({"type": "jynew_money_gte", "int_value": int(target["money"])})
    for item in target.get("items") or []:
        assertions.append(
            {
                "type": "jynew_item_count",
                "item_id": int(item["id"]),
                "expected": str(item["count"]),
                "comparator": "equals",
            }
        )
    for item in target.get("key_values") or []:
        assertions.append(
            {
                "type": "jynew_key_value_equals",
                "key": str(item["key"]),
                "str_value": str(item["value"]),
            }
        )
    if target.get("map_id") is not None:
        assertions.append({"type": "jynew_map_id", "int_value": int(target["map_id"])})
    assertions.append({"type": "no_jynew_utility_errors"})
    return assertions


def run_pal3(app: AppSpec, repo: Path, report_dir: Path) -> list[dict[str, Any]]:
    os.environ["UNITY_EDITOR_PATH"] = "/Applications/Unity/Unity-6000.1.10f1/Unity.app/Contents/MacOS/Unity"
    results: list[dict[str, Any]] = []
    for case in all_cases(app):
        out = report_dir / case.id
        spec = {
            "target": copy.deepcopy(dict(case.target)),
            "actions": [
                {"type": "pal3_save", "save_index": 9},
                {"type": "pal3_load_save", "save_index": 9},
            ],
            "assertions": pal3_assertions(case.target),
            "timeout_ms": int(case.timeout_s * 1000),
        }
        spec_path = out / "reference-spec.yaml"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text("reference: true\n", encoding="utf-8")
        try:
            with Pal3RuntimeAdapter(repo, out, headless=True) as runtime:
                result = runtime.run_checkpoint(spec, spec_path)
                results.append(case_result(case, result.get("status") == "success", dict(case.expected)))
        except Exception as error:
            results.append(case_result(case, False, error=str(error)))
    return results


def pal3_assertions(target: Mapping[str, Any]) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    if target.get("scene_city") and target.get("scene_name"):
        assertions.append(
            {
                "type": "pal3_scene_is",
                "city": str(target["scene_city"]),
                "scene": str(target["scene_name"]),
            }
        )
    if target.get("money") is not None:
        assertions.append({"type": "pal3_money_gte", "int_value": int(target["money"])})
    for item in target.get("story_vars") or []:
        assertions.append(
            {
                "type": "pal3_story_var_equals",
                "var_id": int(item["key"]),
                "expected": str(item["value"]),
            }
        )
    assertions.append({"type": "no_pal3_utility_errors"})
    return assertions


def run_luanti(app: AppSpec, repo: Path, report_dir: Path) -> list[dict[str, Any]]:
    mod_source = REPOSITORY_ROOT / "warptest/instrumentation/luanti/warptest"
    mod_target = repo / "mods/warptest"
    mod_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(mod_source, mod_target, dirs_exist_ok=True)
    build_dir = report_dir / "luanti-build"
    sdk = subprocess.run(
        ["xcrun", "--sdk", "macosx", "--show-sdk-path"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    configure = [
        "cmake", "-S", str(repo), "-B", str(build_dir),
        "-DBUILD_CLIENT=FALSE", "-DBUILD_SERVER=TRUE", "-DRUN_IN_PLACE=TRUE",
        "-DBUILD_UNITTESTS=FALSE", "-DBUILD_DOCUMENTATION=FALSE",
        "-DENABLE_GETTEXT=FALSE", "-DENABLE_SOUND=FALSE",
        "-DCMAKE_CXX_FLAGS=-DLIBICONV_PLUG",
        f"-DICONV_LIBRARY={sdk}/usr/lib/libiconv.tbd",
    ]
    subprocess.run(configure, cwd=repo, check=True, timeout=900)
    subprocess.run(["cmake", "--build", str(build_dir), "-j2"], cwd=repo, check=True, timeout=1800)
    binary = next(
        path
        for path in [
            repo / "bin/luantiserver",
            build_dir / "bin/luantiserver",
            build_dir / "luantiserver",
        ]
        if path.is_file()
    )
    results: list[dict[str, Any]] = []
    for case in all_cases(app):
        try:
            results.append(run_luanti_case(case, repo, binary, report_dir / case.id, mod_source))
        except Exception as error:
            results.append(case_result(case, False, error=str(error)))
    return results


def run_luanti_case(case: CaseSpec, repo: Path, binary: Path, out: Path, mod_source: Path) -> dict[str, Any]:
    world = out / "world"
    worldmod = world / "worldmods/warptest"
    worldmod.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(mod_source, worldmod)
    (world / "world.mt").write_text(
        "\n".join(
            [
                "gameid = devtest",
                "backend = sqlite3",
                "player_backend = sqlite3",
                "auth_backend = sqlite3",
                "mod_storage_backend = sqlite3",
                "load_mod_warptest = true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    assertions, validations = luanti_checks(case.target)
    first = run_luanti_process(binary, world, out / "first", case.target, assertions, validations, skip=False)
    second = run_luanti_process(binary, world, out / "second", case.target, assertions, validations, skip=True)
    ok = first.get("status") == "success" and second.get("status") == "success"
    artifact = world / "map.sqlite"
    return case_result(case, ok, dict(case.expected), artifact=artifact)


def luanti_checks(target: Mapping[str, Any]):
    assertions: list[dict[str, Any]] = [{"type": "no_luanti_mod_errors"}]
    validations: list[dict[str, Any]] = []
    for item in target.get("nodes") or []:
        pos = item["pos"]
        key = f"node.{pos['x']},{pos['y']},{pos['z']}"
        validations.append({"path": key, "expected": item["node"]})
        assertions.append({"type": "luanti_node_is", "pos": pos, "expected": item["node"]})
    for item in target.get("mod_storage") or []:
        validations.append({"path": f"mod_storage.{item['key']}", "expected": str(item["value"])})
        assertions.append({"type": "luanti_mod_storage_equals", "key": item["key"], "value": item["value"]})
    return assertions, validations


def run_luanti_process(
    binary: Path,
    world: Path,
    out: Path,
    target: Mapping[str, Any],
    assertions: list[dict[str, Any]],
    validations: list[dict[str, Any]],
    *,
    skip: bool,
) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    request = out / "request.json"
    report = out / "report.json"
    conf = out / "minetest.conf"
    request.write_text(
        json.dumps(
            {
                "skip_synthesis": skip,
                "spec": {
                    "target": ({"kind": "world_loaded"} if skip else {"kind": "synthesized_world", **dict(target)}),
                    "validations": validations,
                    "actions": [],
                    "assertions": assertions,
                    "skip_synthesis": skip,
                },
            }
        ),
        encoding="utf-8",
    )
    conf.write_text(
        "\n".join(
            [
                "secure.enable_security = false",
                "secure.trusted_mods = warptest",
                "enable_sound = false",
                f"warptest_request_path = {request}",
                f"warptest_report_path = {report}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [str(binary), "--config", str(conf), "--world", str(world), "--gameid", "devtest", "--logfile", str(out / "luanti.log")],
        cwd=binary.parent,
        capture_output=True,
        text=True,
        timeout=300,
    )
    (out / "stdout.log").write_text(completed.stdout or "", encoding="utf-8")
    (out / "stderr.log").write_text(completed.stderr or "", encoding="utf-8")
    if not report.is_file():
        raise RuntimeError("Luanti reference did not write a report")
    payload = json.loads(report.read_text(encoding="utf-8"))
    if completed.returncode != 0 or payload.get("status") != "success":
        raise RuntimeError(f"Luanti reference failed: exit={completed.returncode} report={payload}")
    return payload


def project_mapping(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for key, value in expected.items():
        actual_value = actual.get(key)
        projected[key] = (
            project_mapping(actual_value, value)
            if isinstance(value, Mapping) and isinstance(actual_value, Mapping)
            else copy.deepcopy(actual_value)
        )
    return projected


def subset_equal(actual: Any, expected: Any) -> bool:
    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and all(
            key in actual and subset_equal(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            subset_equal(a, b) for a, b in zip(actual, expected)
        )
    return actual == expected


def case_result(
    case: CaseSpec,
    ok: bool,
    observed: Mapping[str, Any] | None = None,
    *,
    artifact: Path | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "id": case.id,
        "status": "success" if ok else "failure",
        "roundtrip": case.roundtrip,
        "observed": dict(observed or {}),
        "artifact": str(artifact) if artifact is not None and artifact.exists() else None,
        "error": error,
    }


def first_existing_path(value: Any, fallback: Path) -> Path | None:
    if isinstance(value, str) and Path(value).exists():
        return Path(value)
    candidates = [path for path in fallback.rglob("*") if path.is_file() and path.suffix in {".odt", ".ods", ".blend"}]
    return candidates[0] if candidates else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--app", required=True)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    selected = next((item for item in manifest.apps if item.id == args.app), None)
    if selected is None:
        raise SystemExit(f"unknown app: {args.app}")
    report_dir = args.report_dir.expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    try:
        cases = run_reference(selected, args.repo.expanduser().resolve(), report_dir)
        status = "pass" if all(case["status"] == "success" for case in cases) else "failure"
        payload = {"status": status, "app_id": selected.id, "cases": cases}
    except Exception as error:
        payload = {"status": "failure", "app_id": selected.id, "cases": [], "error": str(error)}
    (report_dir / "reference-result.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    raise SystemExit(0 if payload["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
