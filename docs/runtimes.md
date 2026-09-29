# Runtime Setup

This page covers model/provider configuration and the per-application runtime
requirements. For installation and a first run, see the
[Quick Start](../README.md#quick-start) in the README.

- [Model providers](#model-providers)
- [Visual judge](#visual-judge)
- [Per-application prerequisites](#per-application-prerequisites)
- [JupyterLab](#jupyterlab-webserver-gui) · [LibreOffice](#libreoffice-unoodf-document-app) · [Blender](#blender-native-gui-app) · [PAL3](#pal3--pal3unity-unity-native) · [Luanti](#luanti-native-voxel-engine)

## Model providers

`model-bench` and the default LLM paths use a ModelScope token:

```bash
export MODELSCOPE_API_KEY=...
```

Models can be routed to different OpenAI-compatible gateways with an `@<provider>`
tag on the model id. Untagged ids (including ModelScope `:DashScope` routing) use
the default provider; `@gateway` routes to a second OpenAI-compatible gateway:

```bash
export GATEWAY_API_KEY=...                     # required for @gateway models
export GATEWAY_BASE_URL=...                    # optional; defaults to http://gateway.example/v1
export GATEWAY_END_USER=...                    # optional; sent as X-End-User
export GATEWAY_CUSTOM_SOURCE=...               # optional; sent as X-Custom-Source
```

## Visual judge

Visual oracle judging defaults to the same ModelScope endpoint and token. Set only
`MODELSCOPE_API_KEY` for the default judge (`Qwen/Qwen3.5-397B-A17B`), or override
the judge explicitly:

```bash
export WARPTEST_VISUAL_JUDGE_API_KEY=...      # optional; defaults to MODELSCOPE_API_KEY
export WARPTEST_VISUAL_JUDGE_BASE_URL=...     # optional; defaults to https://api-inference.modelscope.cn/v1
export WARPTEST_VISUAL_JUDGE_MODEL=...        # optional; defaults to Qwen/Qwen3.5-397B-A17B
```

For local harness checks without a model call, set `WARPTEST_VISUAL_JUDGE_FAKE=correct`.
Visual audit policy design notes are in [`visual-audit-policy.md`](visual-audit-policy.md).

## Per-application prerequisites

Browser tasks (2048/SpaceRPG/DatingSim) need the dev servers + Playwright Chromium.
The non-browser runtimes each need their engine present and, for the GUI engines, a
logged-in / unlocked macOS desktop session (screen not locked) so the client can open
a GL window:

| Benchmark | Requirement |
| --- | --- |
| 2048 / SpaceRPG / DatingSim | Node dev servers + Playwright Chromium |
| Luanti | built luanti bins; visual capture needs desktop GL (else run `--oracle-policy semantic`) |
| Blender | `games/build_darwin_lite/bin/Blender.app` + unlocked desktop |
| JupyterLab | JupyterLab importable in the env |
| LibreOffice | `soffice` plus matching Python/pyuno (`WARPTEST_LIBREOFFICE_*`) |
| jynew | Unity 2020.3.32f1c1 (`/Applications/Unity/Unity.app`) + unlocked desktop |
| PAL3 | Unity 6000.1.10f1 + local (never-committed) PAL3 CPK data + unlocked desktop |
| OpenRA | `dotnet` build (needs mono on macOS); reported `blocked` if it cannot build |

Runtimes that cannot execute on a host are reported as `blocked`, never as failures.

## JupyterLab (web/server GUI)

`games/jupyterlab` is driven as a web/server GUI profile rather than a native
desktop app. StatePortal launches an isolated localhost JupyterLab server with a
temporary root/config/data/runtime/workspaces tree under the report directory,
uses token/password-disabled test config, and validates checkpoints through
public Jupyter Server Contents, sessions/kernel, workspace metadata, and
Galata-compatible browser surfaces. It does not modify the JupyterLab checkout
or add private hooks.

Runtime smoke needs a Python environment that can import `jupyterlab`,
`jupyter_server`, and `jupyter_client`. For a built browser UI, follow the
upstream source workflow inside `games/jupyterlab`:

```bash
python -m pip install -e ".[dev]"
jlpm
jlpm build:all
```

Without those dependencies/build artifacts, inspect/schema/model-bench checks
still run, while real `warptest run specs/jupyterlab/*.yaml` reports a
dependency-blocked runtime launch.

## LibreOffice (UNO/ODF document app)

`games/LibreOffice` is driven as a Writer/Calc/Impress document benchmark through public
LibreOffice runtime surfaces rather than pixel clicks or GUI replay. StatePortal
launches `soffice` with an isolated `UserInstallation`, connects through UNO,
writes ODF/PDF artifacts under the report directory, and does not modify the
LibreOffice checkout or add private hooks. Semantic runs stay headless; visual
oracle runs use before/after desktop screenshots from the same UNO checkpoint.

Runtime discovery checks `WARPTEST_LIBREOFFICE_SOFFICE`, then
`games/LibreOffice/instdir` build outputs, then `PATH` for `soffice`. The Python
process used for the UNO helper defaults to the current interpreter and can be
overridden with `WARPTEST_LIBREOFFICE_PYTHON`; if pyuno is not importable, real
runs report a dependency-blocked diagnostic mentioning `WARPTEST_LIBREOFFICE_PYTHON`,
`WARPTEST_LIBREOFFICE_SOFFICE`, `URE_BOOTSTRAP`/`PYTHONPATH`, and
`WARPTEST_LIBREOFFICE_PROGRAM_DIR`.

## Blender (native GUI app)

`games/blender` is driven as a hidden macOS GUI app bundle. StatePortal does not
run Blender headlessly: the runtime launches `Blender --factory-startup
--python warptest/blender_driver.py -- <request> <report>` so the Blender window
remains a real GUI/rendering process while checkpoint creation, actions, and validation use official
`bpy`/operator APIs instead of pixel clicks or keyboard GUI-agent replay. The
formal C1 child is created with `--no-window-focus` and kept offscreen by the
process-group guard while Blender's real GUI framebuffer remains available. The
Blender source checkout is not modified; request/report/logs and generated
`.blend` files are written under the StatePortal report directory.

Initialize only Blender's current prebuilt dependencies, then build a GUI-capable
lite app bundle:

```bash
cd games/blender
python3 build_files/utils/make_update.py --no-blender --architecture arm64
make lite ninja BUILD_DIR=../build_darwin_lite NPROCS=8
```

Runtime discovery checks `WARPTEST_BLENDER_EXECUTABLE`,
`WARPTEST_BLENDER_BUILD_DIR`, `games/build_darwin_lite/bin/Blender.app`,
`PATH`, and `/Applications/Blender*.app` in that order. Use `--headed` when
running specs or core bench if you want the GUI window held open briefly:

```bash
python3 -m warptest inspect games/blender
python3 -m warptest run specs/blender/scene-cube-material.yaml --repo games/blender --headed
```

## PAL3 / Pal3.Unity (Unity native)

`games/Pal3.Unity` is driven through the same native checkpoint contract used by
jynew: StatePortal installs a test-gated Unity editor hook, writes a JSON request,
enters play mode, mutates game managers through PAL3 console commands, and reads
semantic checks plus an optional Unity screenshot.

Local execution needs Unity `6000.1.10f1` and the original PAL3 CPK data mounted
outside the repository, for example:

```bash
export UNITY_EDITOR_PATH=/Applications/Unity/Unity-6000.1.10f1/Unity.app/Contents/MacOS/Unity
export PAL3_DATA=/path/to/PAL3              # your local copy of the original game data
ln -sfn "$PAL3_DATA" "$HOME/Library/Application Support/Jackil/PAL3/PAL3"
ln -sfn "$PAL3_DATA" "$HOME/Library/Application Support/unity.Jackil.PAL3/PAL3"
```

Do not copy PAL3 CPK data into the repo or `Assets/StreamingAssets`; Unity will
scan those large files and the copyrighted game data must remain local.

PAL3 visual-oracle goldens are real Unity captures under
`specs/pal3/visual/*.golden.png`. Regenerate them from a logged-in desktop
session with:

```bash
UNITY_EDITOR_PATH=/Applications/Unity/Unity-6000.1.10f1/Unity.app/Contents/MacOS/Unity \
python3 specs/pal3/visual/capture_goldens.py
```

## Luanti (native voxel engine)

`games/luanti` (formerly Minetest) is a source-available C++ voxel engine driven
through a test-gated Lua worldmod (`mods/warptest`, auto-installed). Voxel world,
node-meta inventories (chests), and mod storage are mutated server-side and
persisted through the engine's own world format. Online-player state and the
visual-oracle screenshot additionally need a rendered single-player client.

Two execution modes (chosen automatically per spec):

- **Headless dedicated server** (`bin/luantiserver`): used for world-only specs
  (no online player, semantic policy). Needs **no display at all** — runs in CI
  and over SSH. 17 of the 25 bundled specs run this way.
- **Rendered single-player client** (`bin/luanti --go`): used when a spec touches
  the online player (`target.player`, `luanti_set_player_*`, `luanti_give_item`,
  `luanti_player_*`, `luanti_inventory_contains`) or when the oracle policy
  captures the visual frame. Needs a GL display: **Linux + xvfb + software GL**,
  or an interactive **macOS desktop session**.

Build prerequisites:

```bash
# Linux: cmake + C++ toolchain + engine libs (SDL2, sqlite3, luajit, freetype,
#   zlib, libpng/jpeg, gettext, openal, vorbis); xvfb + mesa for the visual path.
sudo apt-get install -y build-essential cmake xvfb libgl1-mesa-dri   # plus engine deps

# macOS: cmake + Homebrew libs (the adapter adds -DLIBICONV_PLUG / framework flags).
brew install cmake freetype gettext gmp jpeg-turbo jsoncpp libogg libpng \
  libvorbis luajit zstd sdl2 openal-soft sqlite

# The harness builds bin/luanti + bin/luantiserver on first run (RUN_IN_PLACE),
# or point at prebuilt binaries:
export LUANTI_BIN=/path/to/bin/luanti
export LUANTI_SERVER_BIN=/path/to/bin/luantiserver
```

Visual-oracle goldens under `specs/luanti/visual/*.golden.png` are committed as
deterministic placeholders (the StatePortal/CI shell cannot create a GL context).
Regenerate them with real frames from an interactive session:

```bash
# macOS desktop (logged in, screen unlocked) — builds/uses a signed luanti.app:
python3 specs/luanti/visual/capture_goldens.py
# Linux: run the visual bench under xvfb instead.
xvfb-run -a python3 -m warptest bench   # captures via the client path
```

On hosts without a display/built binary, the player/visual specs report a
`probe.*` failure check; the world-only specs, inspect, effort, paper-tables, and
schema all run anywhere.
