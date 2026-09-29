# Agent-S S3 background-pixel bridge

WarpTest pins Agent-S 0.3.2 at commit
`bc6052aa836b18f4a1641cda85d1e9edbde1d800`. Formal C1 arms C/D use
`warptest-agent-s-v2` with `execution_backend=background_pixel_v1`.

Agent-S receives only a real application UI frame. It may return one literal
`agent.<verb>(...)` call from the registered ACI vocabulary; the parent parses
that call with `ast`, validates all literals and bounds, compiles it to a
bounded `InputBatch`, and dispatches it through the application-specific input
backend. Model-authored Python is never passed to `eval` or `exec`.

## Protocol

Each policy step is exactly:

1. Capture one application frame and construct `PolicyFrame(session_id,
   frame_id, path, sha256, width, height, coordinate_space, capture_backend)`.
2. Run Agent-S against those pixel bytes.
3. Validate one ordered `InputBatch(session_id, frame_id, batch_id, actions)`.
4. Dispatch once and persist `ExecutionReceipt(batch_id, accepted,
   event_count, resulting_frame_id, error)`.

The coordinate space is always `frame_pixels_top_left_v1`. A task's dimensions
are immutable. A stale frame, changed dimensions, duplicate or malformed
batch, uncontained path, or backend mismatch fails closed.

Allowed verbs are `click`, `type`, `hotkey`, `hold_and_press`, `scroll`,
`drag_and_drop`, `highlight_text_span`, `wait`, `done`, and `fail`.
`save_to_knowledge` changes only Agent-S's private trajectory memory. Application
switching, `open`, code agents, DOM/control-tree access, task APIs, and semantic
cell operations are rejected. Batches contain at most 64 actions, text at most
4096 Unicode characters, click count 1--3, and waits/drags at most five seconds.
Text uses backend-native Unicode events and never the system clipboard.

## Backends

The formal mapping is registered in `benchmarks/p5/manifest.yaml`:

- 2048, DatingSim, SpaceRPG, and JupyterLab use a 1280x800,
  DPR=1 headless Playwright page screenshot and page-local mouse/keyboard.
- Luanti, OpenRA, Blender, LibreOffice, jynew, and PAL3 use their application
  framebuffer and application-local event bridge. These bridges are nonce
  gated, sequence checked, single-request-in-flight, and disabled outside the
  test launch.
Formal execution never focuses the target, moves the system pointer, writes the
system clipboard, invokes global Quartz/pyautogui events, or captures the whole
desktop. It does not fall back to `foreground_native_v1`. Formal native process
groups are continuously hidden on macOS; Luanti/OpenRA create their real
SDL/OpenGL windows with `SDL_WINDOW_HIDDEN`. Unity deliberately stays headed
(without `-batchmode` or `-nographics`) because its GameView pixels must remain
representative; the process-group guard immediately minimizes/hides unavoidable
startup progress and splash windows within 10 seconds and prevents activation or
focus theft. A Unity-local AppKit hook orders repeat visibility requests back out,
and the independent process-group audit can relocate only target-owned windows
offscreen when AX minimize/hide is acknowledged but ineffective.
Locking the session, sleeping the machine, or terminating the process remains
unsupported.

## Client configuration

```python
from warptest.agent_s_bridge import AgentSBridgeClient, AgentSBridgeConfig

config = AgentSBridgeConfig(
    model="decision-model",
    base_url="https://gateway.example/v1",
    api_key_env="RELAY_API_KEY",
    grounder_model="visual-model",
    grounder_base_url="https://gateway.example/v1",
    grounder_api_key_env="RELAY_API_KEY",
    native_screen_width=1280,
    native_screen_height=800,
    execution_backend="background_pixel_v1",
    coordinate_space="frame_pixels_top_left_v1",
)
```

API key values never cross the JSONL protocol. Only their environment-variable
names and the non-secret `X-End-User`/`X-Custom-Source` headers are accepted.
Usage for decision, reflection, and grounding calls is recorded before an
action is released. Missing or unquantified usage blocks the attempt.

A formal task deadline starts only after the trial owns its checkout and backend
resource leases; queue time is separately bounded and recorded as infrastructure
overhead. The task deadline is then an action-release boundary, not a parent-side
`predict` IPC timeout. Once the child has dispatched a paid provider request, the
parent waits for the child to return or exit, imports every usage event, and only
then checks the task deadline. If the deadline expired while the request was in
flight, the stale action is discarded without touching the GUI. Explicit diagnostic
`predict` timeouts and bounded control requests retain their fail-closed timeout
behavior.

## C1 lane ownership and concurrency

Agent-S clients are persistent per inner lane, never shared across threads. A
formal C1 invocation creates up to `--c1-inner_worker` independent lanes. Each
lane owns its observer, policy bridge, and Agent-S subprocess, sends
`reset(task=...)` before every assigned task, and sends `close` once when that
lane exits. This permits multiple Agent-S instances—including tasks from the
same game—to run concurrently without sharing trajectory, reflection, frame, or
JSONL request state.

The outer scheduler independently overlaps `(arm, model, seed)` invocations via
`--c1-outer_worker`. Defaults are outer=8 and inner=4. The lane assignment is
deterministic, written to the invocation sidecar before execution, and immutable
on resume. Every task receives its own strict APFS CoW repository clone; a clone
failure falls back only that task to the source repository under a serial lease.
See [`c1_concurrency.md`](c1_concurrency.md) for scheduler, sandbox, Unity-canary,
cancellation, and report-provenance details.

## Gates and diagnostics

`c1-calibration-v4` requires exact proof for all 10 backend identities plus
foreground isolation, registered hidden-at-create provenance for Luanti/OpenRA,
and successful transient-window minimization for jynew/PAL3. Native proofs come from the zero-model compatibility
canary; absent proofs produce `passed=false`. Formal aggregation accepts schema
v3 C/D reports only and binds them to the manifest protocol hash, harness hash,
git commit, and application gitlinks. Old foreground C/D reports cannot be
resumed or combined with new results.

`foreground_native_v1` remains an explicit, non-formal diagnostic mode for
legacy comparison only.

Run the no-network protocol tests with:

```bash
python -m pytest -q \
  tests/test_agent_s_background.py \
  tests/test_agent_s_bridge.py \
  tests/test_agent_s_worker_native.py \
  tests/test_c1_background_compatibility.py
```
