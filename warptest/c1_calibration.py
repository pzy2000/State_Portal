"""Fail-closed background-pixel calibration for formal C1 C/D runs.

The browser proof is produced directly by a headless Playwright page.  Native
application proofs are imported from the zero-model compatibility canary so a
missing executable or bridge can never be silently represented as a pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import cost, run_context
from .c1_experiment import relay_environment
from .c1_provenance import (
    C1EnvironmentMismatch,
    c1_application_revisions,
    c1_harness_hash,
    validate_c1_environment,
)
from .c1_provider_runtime import manifest_provider_timeout_s
from .native_input import LUANTI_TEXT_INPUT_PROTOCOL
from .visual_bridge import (
    DEFAULT_GROUNDER_MODEL,
    DEFAULT_OBSERVER_MODEL,
    FixedVisualBridge,
    FixedVisualGrounder,
)


LUANTI_TYPING_PROTOCOL = LUANTI_TEXT_INPUT_PROTOCOL
LUANTI_TYPING_PROBE_ARGUMENTS = "alpha-beta-42 gamma-delta-73"
LUANTI_TYPING_PROBE_TEXT = (
    f"/warptestcalibrate {LUANTI_TYPING_PROBE_ARGUMENTS}"
)


def run_c1_calibration(
    root: str | Path,
    *,
    output: str | Path,
    observer_model: str = DEFAULT_OBSERVER_MODEL,
    grounder_model: str = DEFAULT_GROUNDER_MODEL,
    base_url: str,
    api_key_env: str = "RELAY_API_KEY",
    luanti_seed: int = 0,
    manifest_path: str | Path = "benchmarks/p5/manifest.yaml",
    headless: bool = True,
    backend_proof_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run paid visual localization plus real click-event calibration.

    This function is intentionally explicit and never loads credentials itself.
    Callers must place the configured key in ``api_key_env`` (the module CLI uses
    :func:`relay_environment`).  The artifact stores no model output or key.
    """

    base = Path(root).expanduser().resolve()
    manifest_file = Path(manifest_path).expanduser()
    if not manifest_file.is_absolute():
        manifest_file = base / manifest_file
    manifest = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("C1 calibration manifest must be a mapping")
    provider_timeout_s = manifest_provider_timeout_s(manifest)
    calibration_cfg = (
        manifest.get("calibration")
        if isinstance(manifest, dict)
        and isinstance(manifest.get("calibration"), dict)
        else {}
    )
    if calibration_cfg.get("schema") != "c1-calibration-v4":
        raise RuntimeError("C1 manifest must register c1-calibration-v4")
    declared_protocol = calibration_cfg.get("luanti_input_protocol")
    if declared_protocol not in {None, LUANTI_TYPING_PROTOCOL}:
        raise RuntimeError(
            "C1 manifest declares an unsupported Luanti input calibration protocol"
        )
    out_path = Path(output).expanduser().resolve()
    artifact_dir = out_path.parent / (out_path.stem + "-artifacts")
    background_protocol = manifest.get("c1_background_pixel_protocol")
    if not isinstance(background_protocol, dict):
        raise RuntimeError("C1 manifest lacks c1_background_pixel_protocol")
    benchmark_identities = background_protocol.get("benchmarks")
    if not isinstance(benchmark_identities, dict) or len(benchmark_identities) != 10:
        raise RuntimeError("C1 background protocol must register all 10 benchmarks")
    canonical_protocol = json.dumps(
        background_protocol,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    protocol_sha256 = hashlib.sha256(canonical_protocol.encode("utf-8")).hexdigest()
    # Validate the full task/spec/runtime/gitlink snapshot before making any paid
    # request, then verify it stayed unchanged across calibration.
    harness_hash = c1_harness_hash(base)
    application_revisions = c1_application_revisions(base)
    try:
        environment_evidence = validate_c1_environment(
            base,
            manifest=manifest,
        )
    except C1EnvironmentMismatch as error:
        payload = {
            "schema_version": 4,
            "schema": "c1-calibration-v4",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "git_commit": run_context.git_commit(base),
            "manifest_hash": run_context.manifest_hash(manifest),
            "harness_hash": harness_hash,
            "c1_application_revisions": application_revisions,
            "c1_environment": error.evidence,
            "failure_stage": "environment_preflight",
            "backends": {},
            "passed": False,
            "cost": cost.CostMeter().summary(),
        }
        _atomic_json(out_path, payload)
        return {**payload, "artifact": str(out_path)}
    artifact_dir.mkdir(parents=True, exist_ok=True)
    observer = FixedVisualBridge(
        model=observer_model,
        base_url=base_url,
        api_key_env=api_key_env,
        max_tokens=8192,
        provider_timeout_s=provider_timeout_s,
    )
    grounder = FixedVisualGrounder(
        model=grounder_model,
        base_url=base_url,
        api_key_env=api_key_env,
        max_tokens=8192,
        provider_timeout_s=provider_timeout_s,
    )
    with cost.scoped_meter() as meter:
        browser = _calibrate_browser(
            observer,
            grounder,
            artifact_dir / "browser",
            headless=bool(headless),
        )
    del luanti_seed
    browser_isolation = {
        "target_never_frontmost": bool(browser.get("headless") is True),
        "system_pointer_unchanged": bool(browser.get("headless") is True),
        "system_clipboard_unchanged": bool(browser.get("headless") is True),
        "foreground_text_unchanged": bool(browser.get("headless") is True),
    }
    proof_root = (
        Path(backend_proof_dir).expanduser().resolve()
        if backend_proof_dir is not None
        else None
    )
    backends: dict[str, dict[str, Any]] = {}
    for benchmark, identity in benchmark_identities.items():
        expected = {
            "capture_backend": identity.get("capture_backend"),
            "input_backend": identity.get("input_backend"),
            "frame": identity.get("frame"),
        }
        if expected["capture_backend"] == "playwright_page_screenshot_v1":
            backends[str(benchmark)] = {
                **expected,
                "passed": bool(browser.get("passed") is True),
                "foreground_isolation": dict(browser_isolation),
                "canary": browser,
            }
        else:
            backends[str(benchmark)] = _load_native_backend_proof(
                proof_root, str(benchmark), expected
            )
    if c1_harness_hash(base) != harness_hash:
        raise RuntimeError("C1 harness changed while calibration was running")
    current_environment = validate_c1_environment(base, manifest=manifest)
    if current_environment != environment_evidence:
        raise RuntimeError("C1 environment changed while calibration was running")
    cost_summary = meter.summary()
    api_cfg = manifest.get("api") if isinstance(manifest, dict) else None
    end_user = (
        os.environ.get("RELAY_END_USER", "")
        if api_key_env == "RELAY_API_KEY"
        else ""
    )
    api_provider = {
        "name": api_cfg.get("provider") if isinstance(api_cfg, dict) else None,
        "base_url": base_url.rstrip("/"),
        "end_user_sha256": (
            hashlib.sha256(end_user.encode("utf-8")).hexdigest() if end_user else None
        ),
    }
    payload = {
        "schema_version": 4,
        "schema": "c1-calibration-v4",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": run_context.git_commit(base),
        "manifest_hash": run_context.manifest_hash(manifest),
        "harness_hash": harness_hash,
        "c1_application_revisions": application_revisions,
        "c1_environment": environment_evidence,
        "api_provider": api_provider,
        "provider_timeout_s": provider_timeout_s,
        "observer_model": observer_model,
        "grounder_model": grounder_model,
        "background_pixel_protocol_sha256": protocol_sha256,
        "backends": backends,
        "passed": (
            all(proof.get("passed") is True for proof in backends.values())
            and cost_summary.get("usage_complete") is True
        ),
        "cost": cost_summary,
    }
    _atomic_json(out_path, payload)
    return {**payload, "artifact": str(out_path)}


def _load_native_backend_proof(
    proof_root: Path | None,
    benchmark: str,
    expected: dict[str, Any],
) -> dict[str, Any]:
    """Load one canary proof, rejecting omissions and identity drift."""

    unavailable = {
        **expected,
        "passed": False,
        "foreground_isolation": {
            "target_never_frontmost": False,
            "system_pointer_unchanged": False,
            "system_clipboard_unchanged": False,
            "foreground_text_unchanged": False,
            "background_window_guard_passed": False,
            "window_hidden_at_create_passed": False,
            "transient_window_policy_passed": False,
        },
        "error": "native zero-model canary proof is unavailable",
    }
    if proof_root is None:
        return unavailable
    path = proof_root / f"{benchmark}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {**unavailable, "error": f"cannot read {path}: {type(error).__name__}"}
    if not isinstance(payload, dict):
        return {**unavailable, "error": "native canary proof is not an object"}
    for field, value in expected.items():
        if payload.get(field) != value:
            return {**unavailable, "error": f"native canary {field} mismatch"}
    isolation = payload.get("foreground_isolation")
    required_isolation = (
        "target_never_frontmost",
        "system_pointer_unchanged",
        "system_clipboard_unchanged",
        "foreground_text_unchanged",
        "background_window_guard_passed",
        "window_hidden_at_create_passed",
        "transient_window_policy_passed",
    )
    if (
        payload.get("schema_version") != 1
        or payload.get("benchmark") != benchmark
        or payload.get("passed") is not True
        or not isinstance(isolation, dict)
        or any(isolation.get(field) is not True for field in required_isolation)
    ):
        return {**unavailable, "error": "native canary proof did not pass strict validation"}
    return dict(payload)


def _calibrate_browser(
    observer: FixedVisualBridge,
    grounder: FixedVisualGrounder,
    artifact_dir: Path,
    *,
    headless: bool,
) -> dict[str, Any]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            try:
                context = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    device_scale_factor=1,
                )
                page = context.new_page()
                page.set_content(
                    """
                    <style>html,body{margin:0;width:100%;height:100%;background:#172033;color:white}</style>
                    <button id="target" style="position:absolute;left:420px;top:310px;width:300px;height:100px;font-size:24px"
                      onclick="window.__clicked=(window.__clicked||0)+1">WarpTest C1 calibration</button>
                    """
                )
                screenshot = artifact_dir / "browser-calibration.png"
                page.screenshot(path=str(screenshot))
                observation = observer.observe(
                    screenshot,
                    instruction="Locate the button labeled WarpTest C1 calibration.",
                    width=1280,
                    height=800,
                )
                grounded = grounder.ground(
                    screenshot,
                    instruction="Click the button labeled WarpTest C1 calibration.",
                    observation=observation,
                    width=1280,
                    height=800,
                )
                box = page.locator("#target").bounding_box() or {}
                x, y = grounded["x"], grounded["y"]
                inside = bool(
                    float(box.get("x", -1))
                    <= x
                    <= float(box.get("x", -1)) + float(box.get("width", 0))
                    and float(box.get("y", -1))
                    <= y
                    <= float(box.get("y", -1)) + float(box.get("height", 0))
                )
                page.mouse.click(x, y)
                clicked = int(page.evaluate("() => window.__clicked || 0"))
                dpr = float(page.evaluate("() => devicePixelRatio"))
                return {
                    "passed": bool(inside and clicked == 1 and dpr == 1.0),
                    "backend": "playwright+fixed_visual_bridge",
                    "headless": bool(headless),
                    "frame": {"width": 1280, "height": 800, "device_scale_factor": dpr},
                    "grounded_point": {"x": x, "y": y},
                    "target_bbox": {
                        key: box.get(key) for key in ("x", "y", "width", "height")
                    },
                    "point_inside_target": inside,
                    "click_event_count": clicked,
                }
            finally:
                browser.close()
    except Exception as error:
        return {
            "passed": False,
            "backend": "playwright+fixed_visual_bridge",
            "headless": bool(headless),
            "error_type": type(error).__name__,
        }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path.cwd()))
    parser.add_argument("--output", required=True)
    parser.add_argument("--credentials", default="test_model_batch.py")
    parser.add_argument("--observer-model", default=DEFAULT_OBSERVER_MODEL)
    parser.add_argument("--grounder-model", default=DEFAULT_GROUNDER_MODEL)
    parser.add_argument("--manifest", default="benchmarks/p5/manifest.yaml")
    parser.add_argument(
        "--background-pixel",
        action="store_true",
        help="Require the registered fail-closed background-pixel protocol.",
    )
    parser.add_argument(
        "--backend-proof-dir",
        help="Directory containing strict native zero-model canary JSON proofs.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help=(
            "Legacy rejected flag; formal background-pixel browser calibration "
            "must remain headless."
        ),
    )
    args = parser.parse_args(argv)
    if not args.background_pixel:
        parser.error("formal C1 calibration requires --background-pixel")
    if args.headed:
        parser.error("background-pixel browser calibration must remain headless")
    root = Path(args.root).expanduser().resolve()
    credential_path = Path(args.credentials)
    if not credential_path.is_absolute():
        credential_path = root / credential_path
    with relay_environment(credential_path):
        result = run_c1_calibration(
            root,
            output=args.output,
            observer_model=args.observer_model,
            grounder_model=args.grounder_model,
            base_url=os.environ["RELAY_BASE_URL"],
            manifest_path=args.manifest,
            headless=True,
            backend_proof_dir=args.backend_proof_dir,
        )
    print(json.dumps({"artifact": result["artifact"], "passed": result["passed"]}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
