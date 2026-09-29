"""Candidate-native typed IPC. Commands are parameters, never task-id lookups."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .lazarus_p2p_only_runtime import LuantiOnlineSession
from .lazarus_p2p_only_spec import action_errors, state_errors

LUANTI_BRIDGE = r"""
ui.set_default("maintab")
tv_main:show()
ui.update()
do
 local control = core.settings:get("warptest_generated_control")
 local settings_keys = {}
 local function observe()
  local dialog = "none"
  for name, child in pairs(ui.childlist) do
   if name ~= "maintab" and child.type == "toplevel" then
    local form = child:get_formspec()
    if form and form ~= "" then dialog = name end
   end
  end
  local settings = {}
  for key,_ in pairs(settings_keys) do settings[key] = core.settings:get(key) end
  local available_games, visible_worlds = {}, {}
  for _, game in ipairs(pkgmgr.games or {}) do table.insert(available_games, game.id) end
  if tv_main.current_tab == "local" and dialog == "none" and menudata.worldlist then
   for _, world in ipairs(menudata.worldlist:get_list()) do
    table.insert(visible_worlds, {name=world.name, gameid=world.gameid,
     mods=core.get_dir_list(world.path .. DIR_DELIM .. "worldmods", true)})
   end
  end
  return {tab=tv_main.current_tab, dialog=dialog, runtime_healthy=true, settings=settings,
   available_games=available_games, visible_worlds=visible_worlds}
 end
 local function run(request)
  local op = request.type
  if op == "restore" then
   for name, child in pairs(ui.childlist) do if name ~= "maintab" and child.type == "toplevel" then child:hide() end end
   settings_keys = request.state.settings
   for key,value in pairs(request.state.settings) do core.settings:set(key,value) end
   if menudata.worldlist then menudata.worldlist:refresh() end
   local first_world = request.state.worlds[1]
   local game = first_world and pkgmgr.find_by_gameid(first_world.gameid)
   if first_world then
    assert(game, "world gameid is unavailable")
    core.settings:set("menu_last_game", game.id)
   end
   tv_main:show(); tv_main:set_tab(request.state.tab); ui.update()
   if game and request.state.tab == "local" then
    apply_game(game)
    for index, world in ipairs(menudata.worldlist:get_list()) do
     if world.name == first_world.name then
      core.settings:set("mainmenu_last_selected_world", menudata.worldlist:get_raw_index(index))
      break
     end
    end
    ui.update()
   end
  elseif op == "set_tab" then tv_main:set_tab(request.tab); ui.update()
  elseif op == "menu_event" then core.button_handler(request.fields); ui.update()
  elseif op == "open_settings" then
   local dlg = create_settings_dlg(request.section); dlg:set_parent(tv_main); tv_main:hide(); dlg:show(); ui.update()
  elseif op == "configure_world" then
   local index = nil
   for i, world in ipairs(core.get_worlds()) do if world.name == request.world then index = i end end
   assert(index, "world is unavailable")
   local dlg = create_configure_world_dlg(index); assert(dlg, "world dialog unavailable")
   dlg:set_parent(tv_main); tv_main:hide(); dlg:show(); ui.update()
  elseif op ~= "observe" then error("unknown generated operation") end
  return observe()
 end
 local function poll()
  local file = control and io.open(control,"r")
  if file then
   local content=file:read("*a"); file:close(); os.remove(control)
   local request=core.parse_json(content)
   local ok,result=pcall(run,request)
   local response=io.open(control..".response","w")
   response:write(core.write_json({id=request.id,ok=ok,result=ok and result or tostring(result)})); response:close()
  end
  core.handle_async(function(_) return true end, {}, function(_) poll() end)
 end
 core.handle_async(function(_) return true end, {}, function(_) poll() end)
 core.log("error","WARPTEST_P2P_READY")
end
"""


class NativeIPC:
    def rpc(self, value: dict) -> dict:
        request = {**value, "id": uuid.uuid4().hex}
        reply = Path(str(self.control) + ".response")
        reply.unlink(missing_ok=True)
        temporary = self.control.with_suffix(".tmp")
        temporary.write_text(json.dumps(request))
        temporary.replace(self.control)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if reply.is_file():
                try:
                    result = json.loads(reply.read_text())
                except json.JSONDecodeError:
                    time.sleep(0.05)
                    continue
                if result.get("id") == request["id"]:
                    if result.get("ok") is not True:
                        raise RuntimeError(str(result.get("result")))
                    return result["result"]
            process = getattr(self, "process", None)
            if process is not None and process.poll() is not None:
                raise RuntimeError("candidate process exited")
            time.sleep(0.1)
        raise RuntimeError("candidate typed-state IPC timed out")


class GeneratedLuantiSession(LuantiOnlineSession, NativeIPC):
    generated_overlay = LUANTI_BRIDGE

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.control = self.user_root / "generated-control.json"
        self.generated_config = f"warptest_generated_control = {self.control}\n"
        self._settings_keys = []

    def __enter__(self):
        try:
            super().__enter__()
            # READY is emitted during Lua initialization. Wait for an actual menu
            # event-loop round trip before sending the first F12 capture request.
            self.rpc({"type": "observe"})
            return self
        except BaseException:
            self.close()
            raise

    @property
    def hold_process(self):
        return self.process

    @property
    def user_root(self) -> Path:
        return self.output_dir / "session/home/Library/Application Support/minetest"

    def restore_generated_state(self, state: dict) -> dict:
        errors = state_errors(state, "luanti")
        if errors:
            raise ValueError("; ".join(errors))
        available = self.rpc({"type": "observe"}).get("available_games") or []
        unavailable = sorted({world["gameid"] for world in state["worlds"]} - set(available))
        if unavailable:
            # Validate before changing disk/menu state; never substitute a game.
            raise ValueError(f"world gameid is unavailable: {unavailable}; available: {available}")
        worlds = self.user_root / "worlds"
        if worlds.exists():
            shutil.rmtree(worlds)
        worlds.mkdir(parents=True)
        for world in state["worlds"]:
            root = worlds / world["name"]
            root.mkdir()
            (root / "world.mt").write_text(
                f"gameid = {world['gameid']}\nbackend = dummy\n"
            )
            for mod in world["mods"]:
                directory = root / "worldmods" / mod["name"]
                directory.mkdir(parents=True)
                (directory / "mod.conf").write_text(
                    f"name = {mod['name']}\ndescription = {mod['description']}\n"
                )
                (directory / "init.lua").write_text(
                    "-- Public world data; no product implementation.\n"
                )
        self._settings_keys = list(state["settings"])
        self.rpc({"type": "restore", "state": state})
        return self.export_state()

    def _worlds(self) -> list:
        result = []
        for root in sorted((self.user_root / "worlds").glob("*")):
            if not root.is_dir():
                continue
            settings = {}
            for line in (root / "world.mt").read_text().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    settings[k.strip()] = v.strip()
            mods = []
            for path in sorted((root / "worldmods").glob("*/mod.conf")):
                values = dict(
                    line.split("=", 1)
                    for line in path.read_text().splitlines()
                    if "=" in line
                )
                values = {k.strip(): v.strip() for k, v in values.items()}
                mods.append(
                    {
                        "name": values["name"],
                        "description": values.get("description", ""),
                    }
                )
            result.append(
                {"name": root.name, "gameid": settings.get("gameid", ""), "mods": mods}
            )
        return result

    def export_state(self) -> dict:
        observed = self.rpc({"type": "observe"})
        # Query real persisted settings after the engine processes restore.
        return {
            "tab": observed["tab"],
            "settings": (observed.get("settings") or {}),
            "worlds": self._worlds(),
        }

    def observe_start(self) -> dict:
        observation = self.rpc({"type": "observe"})
        return {**observation, "state": self.export_state()}

    def apply(self, action):
        self.native.focus()
        return super().apply(action)

    def execute_semantic(self, action: dict) -> dict:
        errors = action_errors(action, "luanti")
        if errors:
            raise ValueError("; ".join(errors))
        if action["type"] == "pointer_move":
            self.native.move(action["x"], action["y"])
            time.sleep(0.4)
            return self.observe_start()
        return self.rpc(action)


OPENRA_BRIDGE = r"""using System;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Reflection;
using OpenRA.Network;
using OpenRA.Primitives;
using OpenRA.Widgets;
namespace OpenRA.Mods.Common.Widgets.Logic
{
 public static class WarptestGeneratedState
 {
  static bool running;
  static int mapIndex;
  static bool includeEditorTab;
  public static void Run()
  {
   if (running) return;
   running = true;
   var path = Environment.GetEnvironmentVariable("WARPTEST_GENERATED_CONTROL");
   Action poll = null;
   poll = () => {
    if (!string.IsNullOrEmpty(path) && File.Exists(path)) {
     string id = "";
     try {
      using var doc = JsonDocument.Parse(File.ReadAllText(path)); File.Delete(path);
      var r = doc.RootElement; id=r.GetProperty("id").GetString();
      var op=r.GetProperty("type").GetString();
      string restoreEditorTab = null;
      if (op=="restore") {
       var state=r.GetProperty("state"); var screen=state.GetProperty("screen").GetString();
       if (screen=="editor") {
        includeEditorTab=state.TryGetProperty("editor_tab", out var tab);
        restoreEditorTab=includeEditorTab ? tab.GetString() : "overlays";
        if (restoreEditorTab!="overlays" && restoreEditorTab!="tiles") throw new InvalidOperationException("invalid editor_tab");
        mapIndex=state.GetProperty("map_index").GetInt32();
        var maps=Game.ModData.MapCache.Where(m => m.Status==MapStatus.Available && m.Visibility.HasFlag(MapVisibility.Lobby)).OrderBy(m => m.Uid, StringComparer.Ordinal).ToArray();
        Game.LoadEditor(maps[mapIndex].Uid);
       } else if (screen!="mainmenu") throw new InvalidOperationException("invalid screen");
      } else if (op=="widget_event") {
       var button=Ui.Root.GetOrNull<ButtonWidget>(r.GetProperty("id_arg").GetString());
       if (button==null) throw new InvalidOperationException("button missing");
       if (r.GetProperty("event").GetString()=="click") button.OnClick();
       else button.OnMouseDown(new MouseInput(MouseInputEvent.Down,MouseButton.Left,int2.Zero,int2.Zero,Modifiers.None,0));
      } else if (op=="set_widget_text") {
       var widget=Ui.Root.GetOrNull<TextFieldWidget>(r.GetProperty("id_arg").GetString());
       if (widget==null) throw new InvalidOperationException("text field missing");
       widget.Text=r.GetProperty("text").GetString();
      } else if(op!="observe") throw new InvalidOperationException("unknown operation");
      var requestId=id;
      Game.RunAfterDelay(op=="restore" ? 1500 : 150, () => {
       try {
       var manager=(OrderManager)typeof(Game).GetField("OrderManager",BindingFlags.NonPublic|BindingFlags.Static).GetValue(null);
       var world=manager.World;
       var editor=world.Type==WorldType.Editor;
       if (editor && restoreEditorTab!=null) {
        var tabButton=Ui.Root.GetOrNull<ButtonWidget>(restoreEditorTab=="overlays" ? "OVERLAYS_TAB" : "TILES_TAB");
        if(tabButton==null) throw new InvalidOperationException("editor tab button missing");
        tabButton.OnClick();
       }
       if (editor) mapIndex=Array.FindIndex(Game.ModData.MapCache.Where(m => m.Status==MapStatus.Available && m.Visibility.HasFlag(MapVisibility.Lobby)).OrderBy(m => m.Uid, StringComparer.Ordinal).ToArray(), m => m.Uid==world.Map.Uid);
       var tiles=Ui.Root.GetOrNull("TILE_WIDGETS");
       var layers=Ui.Root.GetOrNull("LAYER_WIDGETS");
       var editorTab=editor && tiles!=null && tiles.IsVisible() ? "tiles" :
        editor && layers!=null && layers.IsVisible() ? "overlays" : "other";
       var state=new System.Collections.Generic.Dictionary<string,object>{{"screen",editor ? "editor" : "mainmenu"}};
       if(editor) { state["map_index"]=mapIndex; if(includeEditorTab) state["editor_tab"]=editorTab; }
       var result=new {state, runtime_healthy=true,
        goal_dialog_open=Ui.Root.GetOrNull("LOAD_GAME_BROWSER_PANEL")!=null,
        widget_ids=Ids(Ui.Root).ToArray(), widgets=Widgets(Ui.Root,true).ToArray()};
       File.WriteAllText(path+".response",JsonSerializer.Serialize(new {id=requestId,ok=true,result}));
       } catch(Exception e) { File.WriteAllText(path+".response",JsonSerializer.Serialize(new {id=requestId,ok=false,result=e.Message})); }
      });
     } catch(Exception e) { File.WriteAllText(path+".response",JsonSerializer.Serialize(new {id,ok=false,result=e.Message})); }
    }
    Game.RunAfterDelay(100,poll);
   };
   Game.RunAfterDelay(100,poll);
  }
  static System.Collections.Generic.IEnumerable<object> Widgets(Widget w, bool parentVisible) {
   var visible=parentVisible && w.IsVisible(); var b=w.RenderBounds;
   if (!string.IsNullOrEmpty(w.Id)) yield return new {id=w.Id,visible,x=b.X,y=b.Y,width=b.Width,height=b.Height};
   foreach(var c in w.Children) foreach(var item in Widgets(c,visible)) yield return item;
  }
  static System.Collections.Generic.IEnumerable<string> Ids(Widget w) {
   if (!string.IsNullOrEmpty(w.Id)) yield return w.Id;
   foreach(var c in w.Children) foreach(var id in Ids(c)) yield return id;
  }
 }
}
"""


def prepare_generated_openra_tree(source: Path, destination: Path) -> Path:
    from .c1_lazarus_selection.p2p_only_engine_capture import _cow_copy, _patch_once
    from .lazarus_p2p_only_identity import canonical_sha256, file_sha256

    identity = canonical_sha256(
        {
            "source": str(source),
            "bridge": OPENRA_BRIDGE,
            "adapter_sha256": file_sha256(Path(__file__)),
            "assembly": file_sha256(
                source / "OpenRA.Mods.Common/LoadScreens/BlankLoadScreen.cs"
            ),
        }
    )
    marker = destination / ".generated-state-v3"
    if marker.is_file() and marker.read_text() == identity:
        return destination
    if destination.exists():
        raise ValueError("generated OpenRA build identity mismatch")
    _cow_copy(source, destination)
    (
        destination / "OpenRA.Mods.Common/Widgets/Logic/WarptestGeneratedState.cs"
    ).write_text(OPENRA_BRIDGE)
    # Preserve the track's public saved-game-data scaffold on both versions.
    # It supplies available inputs only; it neither selects Start nor invokes a transition.
    main = destination / "OpenRA.Mods.Common/Widgets/Logic/MainMenuLogic.cs"
    _patch_once(
        main,
        "loadButton.IsDisabled = () => !LoadGameBrowserLogic.IsLoadPanelEnabled(modData.Manifest);",
        "loadButton.IsDisabled = () => false;",
        label="public saved-game fixture availability",
    )
    load = destination / "OpenRA.Mods.Common/Widgets/Logic/LoadGameBrowserLogic.cs"
    _patch_once(
        load,
        "\t\t\t\t\tfactions.Insert(0, null);\n",
        '\t\t\t\t\tfactions.Insert(0, null);\n\t\t\t\t\tif (factions.Count == 1) { factions.Add("allies"); factions.Add("soviet"); }\n',
        label="public saved-game faction data",
    )

    blank = destination / "OpenRA.Mods.Common/LoadScreens/BlankLoadScreen.cs"
    _patch_once(
        blank,
        "\t\t\tGame.LoadShellMap();\n\t\t\tGame.Settings.Save();\n\t\t}",
        "\t\t\tGame.OnShellmapLoaded += () => OpenRA.Mods.Common.Widgets.Logic.WarptestGeneratedState.Run();\n\t\t\tGame.LoadShellMap();\n\t\t\tGame.Settings.Save();\n\t\t}",
        label="generic state IPC",
    )
    result = subprocess.run(
        ["dotnet", "build", "-c", "Release", "--nologo"],
        cwd=destination,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if result.returncode:
        raise RuntimeError(
            "generated OpenRA bridge build failed: "
            + result.stdout[-3000:]
            + result.stderr[-1000:]
        )
    marker.write_text(identity)
    return destination


class GeneratedOpenRAAdapter(NativeIPC):
    def __init__(self, session, control):
        self.session = session
        self.control = control

    def __getattr__(self, name):
        return getattr(self.session, name)

    def restore_generated_state(self, state):
        errors = state_errors(state, "openra")
        if errors:
            raise ValueError("; ".join(errors))
        self.rpc({"type": "restore", "state": state})
        return self.export_state()

    def export_state(self):
        return self.rpc({"type": "observe"})["state"]

    def observe_start(self):
        return self.rpc({"type": "observe"})

    def execute_semantic(self, action):
        errors = action_errors(action, "openra")
        if errors:
            raise ValueError("; ".join(errors))
        request = dict(action)
        request["id_arg"] = request.pop("id")
        return self.rpc(request)


@contextmanager
def generated_openra_session(tree: Path, output: Path):
    from .c1_openra_runtime import OpenRASettingsSession

    output.mkdir(parents=True, exist_ok=True)
    control = output.resolve() / "generated-control.json"
    previous = os.environ.get("WARPTEST_GENERATED_CONTROL")
    os.environ["WARPTEST_GENERATED_CONTROL"] = str(control)
    try:
        with OpenRASettingsSession(
            tree, output, settings={}, experiment_seed=0
        ) as session:
            (session.support_dir / "Saves/ra/{DEV_VERSION}").mkdir(
                parents=True, exist_ok=True
            )
            yield GeneratedOpenRAAdapter(session, control)
    finally:
        if previous is None:
            os.environ.pop("WARPTEST_GENERATED_CONTROL", None)
        else:
            os.environ["WARPTEST_GENERATED_CONTROL"] = previous
