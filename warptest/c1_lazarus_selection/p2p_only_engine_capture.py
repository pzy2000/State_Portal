"""Engine four-frame capture for the P2P-only Luanti and OpenRA rows.

JupyterLab fixtures load repaired CSS/JS into a Playwright page.  These hosts
have no equivalent document: the repaired surface is a native window.  The
fixture here is therefore an overlay that opens that surface, plus isolated
user data the overlay needs (a world with worldmods, a skipped intro prompt).
The overlay never ships in the inverse; both products receive the same copy.
"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import time
from pathlib import Path

from PIL import Image, ImageChops

from .luanti_12703_mainmenu_runtime import (  # noqa: E402
    _post_key,
)
from .p2p_only_frames import FrameSpec
from .p2p_only_gate16 import Gate16Error, patch_openra_runtime_roll_forward
from warptest.native_input import QuartzNativeInput
from warptest.openra_runtime import (  # noqa: E402
    _download_openra_content_archive,
    _extract_openra_content_archive,
    _load_quick_download,
    _resolve_openra_content_target,
)

_OPENRA_RA_CONTENT = (
    Path(__file__).resolve().parents[2]
    / ".warptest-cache/lazarus-p2p-only/openra-ra-content"
)
_LUANTI_APP = Path("build-app/out/luanti.app")
_LUANTI_BINARY = _LUANTI_APP / "Contents/MacOS/luanti"
_LUANTI_INIT = (
    _LUANTI_APP / "Contents/Resources/builtin/mainmenu/init.lua"
)
# The main-menu F12 path paints a full-width "Saved screenshot" status bar for
# 3.0 seconds.  A tooltip capture takes two screenshots in one session, so the
# second frame must wait until the first frame's status bar has been cleared.
_LUANTI_SCREENSHOT_NOTICE_SETTLE_SECONDS = 3.25
_LUANTI_TOOLTIP_HOVER_SETTLE_SECONDS = 0.6
_LUANTI_INJECTION = """\tui.set_default("maintab")
\ttv_main:show()
\tui.update()

\t-- synchronous, chain parents to only show one at a time
\tlocal parent = tv_main
\tparent = migrate_keybindings(parent)
\tcheck_reinstall_mtg(parent)

\t-- asynchronous, will only be shown if we're still on "maintab"
\tcheck_new_version()
"""

_LUANTI_OVERLAY = r'''
	ui.set_default("maintab")
	tv_main:show()
	ui.update()
	do
		local mode = core.settings:get("warptest_p2p_mode") or ""
		local dialog = "none"
		local dialog_fits = "na"
		local signature = "na"
		local world_present = false
		for _, world in ipairs(core.get_worlds()) do
			if world.name == "p2p_world" then
				world_present = true
			end
		end
		if mode == "luanti-settings" then
			local dlg = create_settings_dlg("controls_touchscreen")
			dlg:set_parent(tv_main)
			tv_main:hide()
			dlg:show()
			ui.update()
			dialog = "settings"
			local formspec = dlg:get_formspec() or ""
			signature = formspec:find("Unavailable", 1, true) and "1" or "0"
		elseif mode == "luanti-configure-world" then
			local worlds = core.get_worlds()
			local idx = 1
			for i, world in ipairs(worlds) do
				if world.name == "p2p_world" then
					idx = i
					break
				end
			end
			local dlg = create_configure_world_dlg(idx)
			if dlg then
				dlg:set_parent(tv_main)
				tv_main:hide()
				dlg:show()
				ui.update()
				dialog = "configure_world"
				local formspec = dlg:get_formspec() or ""
				signature = formspec:find("World mods", 1, true) and "1" or "0"
				local window = core.get_window_info()
				local maximum = window and window.max_formspec_size
				dialog_fits = maximum and maximum.x >= 11.5 and maximum.y >= 7.5
					and "1" or "0"
			end
		elseif mode == "luanti-online-tooltip" then
			tv_main:set_tab("online")
			ui.update()
			signature = "runtime_tooltip"
		end
		core.log("error", "WARPTEST_P2P_STATE mode=" .. mode ..
			" tab=" .. tostring(tv_main.current_tab) ..
			" dialog=" .. dialog ..
			" dialog_fits=" .. dialog_fits ..
			" world_present=" .. (world_present and "1" or "0") ..
			" signature=" .. signature .. " ok=1")
		core.log("error", "WARPTEST_P2P_READY")
	end
	'''

_OPENRA_CAPTURE_CS = r'''#region Copyright & License Information
/*
 * Copyright (c) The OpenRA Developers and Contributors
 * This file is part of OpenRA, which is free software. It is made
 * available to you under the terms of the GNU General Public License
 * as published by the Free Software Foundation, either version 3 of
 * the License, or (at your option) any later version. For more
 * information, see COPYING.
 */
#endregion

using System;
using System.Linq;
using OpenRA.Primitives;
using OpenRA.Widgets;

namespace OpenRA.Mods.Common.Widgets.Logic
{
	public static class WarptestP2POnlyCapture
	{
		public static void Run(string mode)
		{
			var shot = Environment.GetEnvironmentVariable("WARPTEST_P2P_ONLY_SHOT") ?? "";
			if (string.IsNullOrEmpty(shot))
				return;

			Game.RunAfterDelay(800, () =>
			{
					if (!string.IsNullOrEmpty(mode) && mode != "openra-mainmenu")
					{
						try
						{
							Open(mode);
					}
					catch (Exception e)
					{
						Console.WriteLine("WarpTest P2P-only capture failed: " + e);
						Environment.Exit(2);
						return;
					}
				}

					Game.RunAfterDelay(mode.StartsWith("openra-editor-") ? 4000 : 900, () =>
					{
						try
						{
							var state = ObserveAndArrange(mode);
							Console.WriteLine("WARPTEST_P2P_STATE " + state);
						}
						catch (Exception e)
						{
							Console.WriteLine("WarpTest P2P-only state check failed: " + e);
							Environment.Exit(4);
							return;
						}
						Game.RequestWarptestFuzzScreenshot(shot, path =>
					{
						Environment.Exit(string.IsNullOrEmpty(path) ? 3 : 0);
					});
				});
			});
		}

		static void Open(string mode)
		{
			if (mode == "openra-loadgame-dropdown")
			{
				var panel = Ui.OpenWindow("LOAD_GAME_BROWSER_PANEL", new WidgetArgs
				{
					{ "onExit", () => { } },
					{ "onStart", () => { } }
				});
				var dropdown = panel.GetOrNull<DropDownButtonWidget>("FLT_FACTION_DROPDOWNBUTTON");
				if (dropdown == null)
					throw new InvalidOperationException("faction dropdown is missing");
				dropdown.OnMouseDown(Click());
				return;
			}

			if (mode == "openra-colorpicker" || mode == "openra-colorpicker-closed")
			{
				var settings = Game.OpenWindow("SETTINGS_PANEL", new WidgetArgs
				{
					{ "onExit", () => { } }
				});
				if (mode == "openra-colorpicker")
				{
					var color = settings.GetOrNull<DropDownButtonWidget>("PLAYERCOLOR");
					if (color == null)
						throw new InvalidOperationException("PLAYERCOLOR dropdown is missing");
					settings.Bounds = new WidgetBounds(
						Game.Renderer.Resolution.Width - settings.Bounds.Width - 8,
						settings.Bounds.Y,
						settings.Bounds.Width,
						settings.Bounds.Height);
					color.OnMouseDown(Click());
				}
				return;
			}

			if (mode == "openra-editor-overlays" || mode == "openra-editor-tiles")
			{
				var map = Game.ModData.MapCache
					.FirstOrDefault(m => m.Status == MapStatus.Available && m.Visibility.HasFlag(MapVisibility.Lobby));
				if (map == null)
					throw new InvalidOperationException("no lobby map is available for the editor");
				Game.LoadEditor(map.Uid);
				return;
			}

			throw new InvalidOperationException("unknown P2P-only capture mode: " + mode);
		}

		static string ObserveAndArrange(string mode)
		{
			if (mode == "openra-mainmenu")
			{
				if (Ui.Root.GetOrNull("LOAD_GAME_BROWSER_PANEL") != null || Ui.Root.GetOrNull("MAP_EDITOR") != null)
					throw new InvalidOperationException("main-menu start has an open task surface");
				return "mode=" + mode + " mainmenu=1 load_panel=0 editor=0 ok=1";
			}

				if (mode == "openra-loadgame-dropdown")
				{
					var panel = Ui.Root.GetOrNull("LOAD_GAME_BROWSER_PANEL");
					var dropdown = panel?.GetOrNull<DropDownButtonWidget>("FLT_FACTION_DROPDOWNBUTTON");
					if (panel == null || dropdown == null || !dropdown.HasKeyboardFocus)
						throw new InvalidOperationException("load-game dropdown is not open");
					var menu = Ui.Root.GetOrNull<ScrollPanelWidget>("LABEL_DROPDOWN_TEMPLATE");
					if (menu == null)
						throw new InvalidOperationException("faction dropdown menu is missing");
					var labels = menu.Children
						.Select(item => item.GetOrNull<LabelWidget>("LABEL")?.GetText())
						.Where(label => !string.IsNullOrEmpty(label))
						.Skip(1)
						.ToArray();
					if (labels.Length == 0)
						throw new InvalidOperationException("faction dropdown labels are missing");
					return "mode=" + mode + " mainmenu=0 load_panel=1 dropdown_open=1 faction_labels=" +
						string.Join(",", labels) + " ok=1";
			}

			if (mode == "openra-colorpicker" || mode == "openra-colorpicker-closed")
				return "mode=" + mode + " settings=1 ok=1";

			if (mode == "openra-editor-overlays" || mode == "openra-editor-tiles")
			{
				var editor = Ui.Root.GetOrNull("MAP_EDITOR");
				if (editor == null)
					throw new InvalidOperationException("map editor is not open");
				var tabId = mode == "openra-editor-overlays" ? "OVERLAYS_TAB" : "TILES_TAB";
				var tab = Ui.Root.GetOrNull<ButtonWidget>(tabId);
				if (tab == null)
					throw new InvalidOperationException(tabId + " is missing");
				tab.OnClick();
				var tiles = Ui.Root.GetOrNull("TILE_WIDGETS");
				var list = Ui.Root.GetOrNull<ScrollPanelWidget>("TILETEMPLATE_LIST");
				var expectedVisible = mode == "openra-editor-tiles";
				if (tiles == null || tiles.IsVisible() != expectedVisible)
					throw new InvalidOperationException("tile selector visibility is wrong");
					if (expectedVisible && (list == null || list.Children.Count == 0))
						throw new InvalidOperationException("tile template list is empty");
					var templateIds = expectedVisible
						? list.Children.OfType<ScrollItemWidget>()
							.Select(item => item.GetTooltipText?.Invoke())
							.Where(id => !string.IsNullOrEmpty(id))
							.Take(12)
							.ToArray()
						: Array.Empty<string>();
					if (expectedVisible && templateIds.Length == 0)
						throw new InvalidOperationException("tile template ids are missing");
					return "mode=" + mode + " editor=1 tile_widgets=" + (expectedVisible ? "1" : "0") +
						(expectedVisible ? " template_ids=" + string.Join(",", templateIds) : "") + " ok=1";
			}

			throw new InvalidOperationException("unknown P2P-only capture mode: " + mode);
		}

		static MouseInput Click()
		{
			return new MouseInput(MouseInputEvent.Down, MouseButton.Left, int2.Zero, int2.Zero, Modifiers.None, 0);
		}
	}
}
'''


def capture_engine_variant(
    *,
    host: str,
    tree: Path,
    run_dir: Path,
    spec: FrameSpec,
    variant: str,
) -> dict[str, Path]:
    """Drive one product through the registered start and the after surface."""

    run_dir.mkdir(parents=True, exist_ok=True)
    if variant not in {"fixed", "buggy"}:
        raise RuntimeError(f"invalid engine capture variant: {variant}")
    captured: dict[str, tuple[Path, dict[str, str], dict[str, object]]] = {}
    if host == "luanti":
        captured["before"] = _capture_luanti(
            tree, run_dir, spec, stage="before", mode=spec.engine_before_mode
        )
        captured["after"] = _capture_luanti(
            tree, run_dir, spec, stage="after", mode=spec.engine_after_mode
        )
    elif host == "openra":
        captured["before"] = _capture_openra(
            tree, run_dir, spec, stage="before", mode=spec.engine_before_mode
        )
        captured["after"] = _capture_openra(
            tree, run_dir, spec, stage="after", mode=spec.engine_after_mode
        )
    else:
        raise RuntimeError(f"no engine capture for host {host}")
    before, _, _ = captured["before"]
    after, _, _ = captured["after"]
    _apply_clip(before, spec.before_clip)
    _apply_clip(after, spec.clip)
    stages: dict[str, object] = {}
    for stage, (path, observed, interaction) in captured.items():
        expected = _expected_engine_state(
            spec, variant=variant, stage=stage
        )
        matched = all(observed.get(key) == value for key, value in expected.items())
        stages[stage] = {
            "path": str(path),
            "expected_state": expected,
            "observed_state": observed,
            "matched_expected": matched,
            "interaction": interaction,
        }
        if not matched:
            raise RuntimeError(
                f"{spec.task_id} {variant} {stage} state mismatch: "
                f"expected={expected} observed={observed}"
            )
    report = run_dir / "report.json"
    report.write_text(
        json.dumps(
            {
                "schema": "c1-lazarus-p2p-only-native-capture-v1",
                "schema_version": 1,
                "host": host,
                "task_id": spec.task_id,
                "variant": variant,
                "stages": stages,
                "before": stages["before"],
                "after": stages["after"],
                "step": {
                    "kind": spec.capture_kind,
                    "mode": spec.engine_after_mode,
                    "frame_size": list(spec.engine_frame_size),
                },
                "console": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"before": before, "after": after, "report": report}


def validate_engine_variant_differential(
    *, spec: FrameSpec, captured: dict[str, dict[str, Path]]
) -> None:
    """Bind a native frame differential to task-specific observed content."""

    key = spec.engine_variant_state_key
    if not key:
        return
    payloads: dict[str, dict[str, object]] = {}
    observed: dict[str, str] = {}
    for variant in ("fixed", "buggy"):
        report = captured.get(variant, {}).get("report")
        if report is None:
            raise RuntimeError(f"{spec.task_id} {variant} native report is missing")
        payload = json.loads(report.read_text(encoding="utf-8"))
        after = payload.get("stages", {}).get("after")
        state = after.get("observed_state") if isinstance(after, dict) else None
        value = state.get(key) if isinstance(state, dict) else None
        if not isinstance(value, str) or not value:
            raise RuntimeError(
                f"{spec.task_id} {variant} after state did not observe {key}"
            )
        payloads[variant] = payload
        observed[variant] = value

    expected = {
        "fixed": spec.engine_fixed_state_value,
        "buggy": spec.engine_buggy_state_value,
    }
    for variant, value in expected.items():
        if value and observed[variant] != value:
            raise RuntimeError(
                f"{spec.task_id} {variant} {key} expected {value!r}, "
                f"observed {observed[variant]!r}"
            )
    if observed["fixed"] == observed["buggy"]:
        raise RuntimeError(
            f"{spec.task_id} native {key} did not distinguish fixed from buggy"
        )

    differential = {
        "state_key": key,
        "fixed": observed["fixed"],
        "buggy": observed["buggy"],
        "matched_expected": True,
    }
    for variant in ("fixed", "buggy"):
        report = captured[variant]["report"]
        payloads[variant]["variant_differential"] = differential
        report.write_text(
            json.dumps(payloads[variant], indent=2) + "\n", encoding="utf-8"
        )


def _expected_engine_state(
    spec: FrameSpec, *, variant: str, stage: str
) -> dict[str, str]:
    mode = spec.engine_before_mode if stage == "before" else spec.engine_after_mode
    expected = {"mode": mode, "ok": "1"}
    if mode == "luanti-local":
        expected.update({"tab": "local", "dialog": "none"})
    elif mode == "luanti-local-world":
        expected.update(
            {"tab": "local", "dialog": "none", "world_present": "1"}
        )
    elif mode == "luanti-settings":
        expected.update(
            {
                "tab": "local",
                "dialog": "settings",
                "signature": "1" if variant == "fixed" else "0",
            }
        )
    elif mode == "luanti-configure-world":
        expected.update(
            {
                "tab": "local",
                "dialog": "configure_world",
                "dialog_fits": "1",
                "world_present": "1",
                "signature": "1" if variant == "fixed" else "0",
            }
        )
    elif mode == "luanti-online-tooltip":
        expected.update(
            {
                "tab": "online",
                "dialog": "none",
                "tooltip_visible": "1",
            }
        )
    elif mode == "openra-mainmenu":
        expected.update({"mainmenu": "1", "load_panel": "0", "editor": "0"})
    elif mode == "openra-loadgame-dropdown":
        expected.update({"load_panel": "1", "dropdown_open": "1"})
    elif mode == "openra-editor-overlays":
        expected.update({"editor": "1", "tile_widgets": "0"})
    elif mode == "openra-editor-tiles":
        expected.update({"editor": "1", "tile_widgets": "1"})
    elif mode in {"openra-colorpicker", "openra-colorpicker-closed"}:
        expected.update({"settings": "1"})
    else:
        raise RuntimeError(f"unknown engine capture mode: {mode}")
    if stage == "after" and spec.engine_variant_state_key:
        value = (
            spec.engine_fixed_state_value
            if variant == "fixed"
            else spec.engine_buggy_state_value
        )
        if value:
            expected[spec.engine_variant_state_key] = value
    return expected


def _state_marker(text: str) -> dict[str, str]:
    marker = "WARPTEST_P2P_STATE "
    lines = [line.split(marker, 1)[1].strip() for line in text.splitlines() if marker in line]
    if len(lines) != 1:
        raise RuntimeError(f"expected exactly one native state marker, found {len(lines)}")
    result: dict[str, str] = {}
    for token in lines[0].split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        result[key] = value
    return result


def _apply_clip(path: Path, clip: tuple[int, int, int, int] | None) -> None:
    if clip is None:
        return
    x, y, width, height = clip
    with Image.open(path) as image:
        cropped = image.crop((x, y, x + width, y + height))
        cropped.save(path)


def _cow_copy(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ("cp", "-c", "-R", str(source), str(destination)),
        capture_output=True,
        check=False,
        timeout=1800,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "copy-on-write clone failed: "
            + result.stderr.decode("utf-8", "replace").strip()
        )


def _wait_for_png(path: Path, process: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 1024:
            return
        if process.poll() is not None and not (path.is_file() and path.stat().st_size > 1024):
            raise RuntimeError(
                f"engine exited before writing {path.name} (rc={process.returncode})"
            )
        time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {path}")


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate a new-session wrapper and every child it spawned."""

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (PermissionError, ProcessLookupError):
        return
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            pass
        process.wait()
        return
    # The wrapper shell may exit before a child that ignored SIGTERM.  Reaping
    # the shell and then signaling the original process group closes that race.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (PermissionError, ProcessLookupError):
        pass


def _take_luanti_screenshot(
    *,
    process: subprocess.Popen[bytes],
    screenshots: Path,
    timeout: float = 20,
    request_key=None,
    retry_interval_s: float | None = None,
    capture_audit: dict | None = None,
) -> Path:
    before = set(screenshots.glob("*.png"))
    deadline = time.monotonic() + timeout
    next_request = 0.0
    requests = 0
    while time.monotonic() < deadline:
        new = [path for path in screenshots.glob("*.png") if path not in before]
        if new:
            time.sleep(0.2)
            return new[0]
        if process.poll() is not None:
            raise RuntimeError("luanti exited before a screenshot was written")
        if time.monotonic() >= next_request:
            if request_key is None:
                _post_key(process.pid, 111)
            else:
                request_key()
            requests += 1
            if capture_audit is not None:
                capture_audit["requests"] = requests
            next_request = time.monotonic() + (retry_interval_s or timeout + 1)
        time.sleep(0.1)
    raise RuntimeError("timed out waiting for the luanti F12 screenshot")


def _image_change_ratio(first: Path, second: Path) -> float:
    with Image.open(first) as first_image, Image.open(second) as second_image:
        first_rgb = first_image.convert("RGB")
        second_rgb = second_image.convert("RGB")
        if first_rgb.size != second_rgb.size:
            raise RuntimeError("Luanti hover validation frame dimensions differ")
        histogram = ImageChops.difference(first_rgb, second_rgb).convert("L").histogram()
    total = sum(histogram) or 1
    return sum(histogram[24:]) / total


def _capture_luanti_tooltip_frame(
    *,
    process: subprocess.Popen[bytes],
    screenshots: Path,
    native: QuartzNativeInput,
    hover_point: tuple[int, int],
) -> tuple[Path, float, dict[str, object]]:
    """Capture a tooltip without inheriting the baseline F12 status bar."""

    baseline = _take_luanti_screenshot(process=process, screenshots=screenshots)
    time.sleep(_LUANTI_SCREENSHOT_NOTICE_SETTLE_SECONDS)
    native.move(*hover_point)
    time.sleep(_LUANTI_TOOLTIP_HOVER_SETTLE_SECONDS)
    captured = _take_luanti_screenshot(process=process, screenshots=screenshots)
    changed_pixel_ratio = _image_change_ratio(baseline, captured)
    if changed_pixel_ratio < 0.001:
        raise RuntimeError(
            "Luanti tooltip hover did not produce a visible runtime change"
        )
    return (
        captured,
        changed_pixel_ratio,
        {
            "kind": "quartz_hover",
            "content_point": list(hover_point),
            "baseline_notice_settle_seconds": (
                _LUANTI_SCREENSHOT_NOTICE_SETTLE_SECONDS
            ),
            "hover_settle_seconds": _LUANTI_TOOLTIP_HOVER_SETTLE_SECONDS,
            "changed_pixel_ratio": changed_pixel_ratio,
        },
    )


def _capture_luanti(
    tree: Path,
    run_dir: Path,
    spec: FrameSpec,
    *,
    stage: str,
    mode: str,
) -> tuple[Path, dict[str, str], dict[str, object]]:
    session = run_dir / f"{stage}-session"
    if session.exists():
        shutil.rmtree(session)
    app = session / "luanti.app"
    _cow_copy(tree / _LUANTI_APP, app)
    init = app / "Contents/Resources/builtin/mainmenu/init.lua"
    original = init.read_text(encoding="utf-8")
    if original.count(_LUANTI_INJECTION) != 1:
        raise RuntimeError("luanti main-menu overlay injection point is not unique")
    init.write_text(original.replace(_LUANTI_INJECTION, _LUANTI_OVERLAY, 1), encoding="utf-8")
    subprocess.run(
        ("codesign", "--force", "--deep", "--sign", "-", "--timestamp=none", str(app)),
        check=True,
        capture_output=True,
        timeout=120,
    )

    home = session / "home"
    user = home / "Library/Application Support/minetest"
    user.mkdir(parents=True, exist_ok=True)
    if mode in {"luanti-local-world", "luanti-configure-world"}:
        worlds = user / "worlds" / "p2p_world"
        mod = worlds / "worldmods" / "p2p_world_mod"
        mod.mkdir(parents=True, exist_ok=True)
        (worlds / "world.mt").write_text(
            "gameid = devtest\nbackend = dummy\n", encoding="utf-8"
        )
        (mod / "mod.conf").write_text(
            "name = p2p_world_mod\ndescription = p2p capture fixture\n",
            encoding="utf-8",
        )
        (mod / "init.lua").write_text("-- capture fixture\n", encoding="utf-8")

    screenshots = session / "screenshots"
    screenshots.mkdir(parents=True, exist_ok=True)
    shot = run_dir / f"{stage}.png"
    if shot.exists():
        shot.unlink()
    config = session / "minetest.conf"
    frame_width, frame_height = spec.engine_frame_size
    config.write_text(
        "\n".join(
            (
                "video_driver = opengl",
                f"screen_w = {frame_width}",
                f"screen_h = {frame_height}",
                "fullscreen = false",
                "enable_sound = false",
                "enable_update_checker = false",
                "enable_esc_dialog = false",
                "language = en",
                "maintab_LAST = local",
                "touch_controls = false",
                "show_advanced = true",
                f"screenshot_path = {screenshots.resolve()}",
                "screenshot_format = png",
                f"warptest_p2p_mode = {mode}",
                "serverlist_url =",
                "tooltip_show_delay = 0",
                "",
            )
        ),
        encoding="utf-8",
    )
    binary = (app / "Contents/MacOS/luanti").resolve()
    env = {
        **os.environ,
        "HOME": str(home.resolve()),
        "LANG": "C",
        "LC_ALL": "C",
        "MINETEST_USER_PATH": str(user.resolve()),
    }
    log = session / "luanti.log"
    process = subprocess.Popen(
        (str(binary), "--config", str(config.resolve()), "--logfile", str(log.resolve())),
        cwd=str(session.resolve()),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    started = time.monotonic()
    tooltip_changed_pixel_ratio: float | None = None
    try:
        while time.monotonic() - started < 40:
            if log.is_file() and "WARPTEST_P2P_READY" in log.read_text(
                encoding="utf-8", errors="replace"
            ):
                break
            if process.poll() is not None:
                raise RuntimeError(
                    "luanti exited before the main-menu overlay was ready:\n"
                    + (log.read_text(encoding="utf-8", errors="replace") if log.is_file() else "")
                )
            time.sleep(0.1)
        else:
            raise RuntimeError("timed out waiting for the luanti main-menu overlay")
        time.sleep(0.5)
        native = QuartzNativeInput(
            process.pid,
            frame_width=frame_width,
            frame_height=frame_height,
        )
        interaction: dict[str, object] = {"kind": "none"}
        if mode == "luanti-online-tooltip":
            if spec.hover_point is None:
                raise RuntimeError("Luanti tooltip capture has no registered hover point")
            native.focus()
            captured, tooltip_changed_pixel_ratio, interaction = (
                _capture_luanti_tooltip_frame(
                    process=process,
                    screenshots=screenshots,
                    native=native,
                    hover_point=spec.hover_point,
                )
            )
        else:
            native.focus()
            captured = _take_luanti_screenshot(
                process=process, screenshots=screenshots
            )
        shutil.copy2(captured, shot)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
    text = log.read_text(encoding="utf-8", errors="replace")
    observed = _state_marker(text)
    if tooltip_changed_pixel_ratio is not None:
        observed["tooltip_visible"] = "1"
        observed["tooltip_changed_pixel_ratio"] = (
            f"{tooltip_changed_pixel_ratio:.9f}"
        )
    return shot, observed, interaction


def _patch_once(path: Path, old: str, new: str, *, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise RuntimeError(f"{label} injection point is not unique in {path.name}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _prepare_openra_overlay(tree: Path) -> None:
    helper = tree / "OpenRA.Mods.Common/Widgets/Logic/WarptestP2POnlyCapture.cs"
    helper.write_text(_OPENRA_CAPTURE_CS, encoding="utf-8")
    _patch_once(
        tree / "OpenRA.Mods.Common/LoadScreens/BlankLoadScreen.cs",
        "\t\t\tGame.LoadShellMap();\n\t\t\tGame.Settings.Save();\n\t\t}",
        "\t\t\tvar p2pMode = Environment.GetEnvironmentVariable(\"WARPTEST_P2P_ONLY_MODE\") ?? \"\";\n"
        "\t\t\tif (!string.IsNullOrEmpty(Environment.GetEnvironmentVariable(\"WARPTEST_P2P_ONLY_SHOT\")))\n"
        "\t\t\t\tGame.OnShellmapLoaded += () => OpenRA.Mods.Common.Widgets.Logic.WarptestP2POnlyCapture.Run(p2pMode);\n"
        "\t\t\tGame.LoadShellMap();\n\t\t\tGame.Settings.Save();\n\t\t}",
        label="openra blank load screen",
    )
    _patch_once(
        tree / "OpenRA.Mods.Common/Widgets/Logic/LoadGameBrowserLogic.cs",
        "\t\t\t\t\tfactions.Insert(0, null);\n",
        "\t\t\t\t\tfactions.Insert(0, null);\n"
        "\t\t\t\t\tif (factions.Count == 1)\n"
        "\t\t\t\t\t{\n"
        "\t\t\t\t\t\tfactions.Add(\"allies\");\n"
        "\t\t\t\t\t\tfactions.Add(\"soviet\");\n"
        "\t\t\t\t\t}\n",
        label="openra load-game faction fixture",
    )
    dotnet = shutil.which("dotnet")
    if dotnet is None:
        raise Gate16Error("dotnet is not available")
    result = subprocess.run(
        (dotnet, "build", "-c", "Release", "--nologo"),
        cwd=tree,
        capture_output=True,
        check=False,
        timeout=1800,
        env={**os.environ, "DOTNET_NOLOGO": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(
            "openra capture rebuild failed:\n"
            + result.stdout.decode("utf-8", "replace")[-2000:]
            + result.stderr.decode("utf-8", "replace")[-2000:]
        )
    patch_openra_runtime_roll_forward(tree)


def _ensure_openra_ra_content(work: Path) -> Path:
    """Install Red Alert quick content once so capture does not sit on the installer."""

    support = _OPENRA_RA_CONTENT
    support.mkdir(parents=True, exist_ok=True)
    download = _load_quick_download(work, "ra")
    required = [
        _resolve_openra_content_target(support, target)
        for target in download["extract"]
    ]
    if required and all(path.exists() and path.stat().st_size > 0 for path in required):
        return support
    log = support / "install.log"
    cache = Path.home() / ".cache" / "warptest" / "openra-content"
    cache.mkdir(parents=True, exist_ok=True)
    archive = _download_openra_content_archive(download, cache, log)
    _extract_openra_content_archive(archive, support, download, log)
    missing = [path for path in required if not path.exists() or path.stat().st_size == 0]
    if missing:
        raise RuntimeError(
            "OpenRA RA content install missed: "
            + ", ".join(str(path) for path in missing[:5])
        )
    return support


def _capture_openra(
    tree: Path,
    run_dir: Path,
    spec: FrameSpec,
    *,
    stage: str,
    mode: str,
) -> tuple[Path, dict[str, str], dict[str, object]]:
    session = run_dir / f"{stage}-session"
    work = run_dir / "tree"
    if stage == "before":
        if session.exists():
            shutil.rmtree(session)
        if work.exists():
            shutil.rmtree(work)
        _cow_copy(tree, work)
        _prepare_openra_overlay(work)
    else:
        session.mkdir(parents=True, exist_ok=True)

    content = _ensure_openra_ra_content(work)
    support = session / "support"
    _cow_copy(content, support)
    (support / "settings.yaml").write_text(
        "Game:\n"
        "\tIntroductionPromptVersion: 99\n"
        "\tFetchNews: False\n"
        "Debug:\n"
        "\tSystemInformationVersionPrompt: 99\n"
        "\tSendSystemInformation: False\n"
        "\tCheckVersion: False\n",
        encoding="utf-8",
    )
    shot = run_dir / f"{stage}.png"
    if shot.exists():
        shot.unlink()
    env = {
        **os.environ,
        "WARPTEST_P2P_ONLY_MODE": mode,
        "WARPTEST_P2P_ONLY_SHOT": str(shot.resolve()),
        "DOTNET_ROLL_FORWARD": os.environ.get("DOTNET_ROLL_FORWARD", "LatestMajor"),
        "DOTNET_NOLOGO": "1",
    }
    argv = [
        str((work / "launch-game.sh").resolve()),
        "Game.Mod=ra",
        f"Engine.SupportDir={support.resolve()}",
        "Game.FetchNews=false",
        "Game.EnableDiscordService=false",
        "Sound.Mute=true",
        "Server.AdvertiseOnline=false",
        "Server.AdvertiseOnLocalNetwork=false",
        "Graphics.Mode=Windowed",
        "Graphics.WindowedSize=800,600",
        "Graphics.UIScale=1",
    ]
    stdout_log = session / "openra.stdout.log"
    stderr_log = session / "openra.stderr.log"
    process = subprocess.Popen(
        argv,
        cwd=work,
        env=env,
        stdout=stdout_log.open("wb"),
        stderr=stderr_log.open("wb"),
        start_new_session=True,
    )
    try:
        _wait_for_png(shot, process, timeout=180)
    finally:
        _terminate_process_group(process)
    stdout_text = stdout_log.read_text(encoding="utf-8", errors="replace")
    return (
        shot,
        _state_marker(stdout_text),
        {"kind": "openra_capture_helper", "mode": mode},
    )


__all__ = ["capture_engine_variant", "validate_engine_variant_differential"]
