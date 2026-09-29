#!/usr/bin/env python3
"""Single-turn relay smoke for the ScaleCUA bridge (A-arm policy path).

Generates a synthetic screenshot, drives one real predict through the pinned
worker, and prints the parsed action plus the fail-closed usage ledger.

Usage:
  MODELSCOPE_BASE_URL=http://... MODELSCOPE_API_KEY=... \
    python scripts/smoke_scalecua_bridge.py [--model "Qwen/Qwen3.8-27B@modelscope"]
"""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest import cost
from warptest.providers import resolve_model
from warptest.scalecua_bridge import (
    ScaleCUABridgeClient,
    ScaleCUABridgeConfig,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="Qwen/Qwen3.8-27B@modelscope")
    args = parser.parse_args()

    from PIL import Image, ImageDraw

    clean_model, provider = resolve_model(
        args.model, default_base_url="http://127.0.0.1:9/v1", default_api_key_env="MODELSCOPE_API_KEY"
    )
    config = ScaleCUABridgeConfig(
        model=clean_model,
        base_url=provider.base_url,
        api_key_env=provider.api_key_env,
        default_headers=dict(provider.extra_headers),
        temperature=0.2,
        native_screen_width=1280,
        native_screen_height=800,
        execution_backend="background_pixel_v1",
    )
    with tempfile.TemporaryDirectory() as tmp:
        frame = Path(tmp) / "frame.png"
        image = Image.new("RGB", (1280, 800), (250, 248, 244))
        draw = ImageDraw.Draw(image)
        draw.rectangle([80, 60, 400, 110], fill=(52, 110, 220))
        draw.text((100, 78), "Settings", fill=(255, 255, 255))
        draw.rectangle([80, 160, 620, 200], outline=(120, 120, 120))
        draw.text((95, 172), "brightness: 80%", fill=(30, 30, 30))
        image.save(frame, format="PNG")

        with ScaleCUABridgeClient(config, root=ROOT) as client:
            client.reset(task="smoke-scalecua-single-turn")
            with cost.scoped_meter() as meter:
                prediction = client.predict(
                    task="smoke-scalecua-single-turn",
                    instruction="Open the Settings panel by clicking its button.",
                    observation="raw_screenshot",
                    screenshot=frame,
                    width=1280,
                    height=800,
                    history=[],
                    session_id="smoke-scalecua",
                    frame_id=0,
                    capture_backend="smoke_synthetic_pil_v1",
                )
        print("== prediction ==")
        print("action:", prediction.action.to_dict())
        print("actions:", [action.to_dict() for action in prediction.actions])
        print("info keys:", sorted(prediction.info))
        print("thought:", (prediction.info.get("thought") or "")[:200])
        summary = meter.summary()
        print("== ledger ==")
        print("llm_calls:", summary.get("llm_calls"))
        print("total_tokens:", summary.get("total_tokens"))
        rows = meter.records
        incomplete = [
            row for row in rows if getattr(row, "usage_complete", True) is False
        ]
        print("incomplete_usage_rows:", len(incomplete))
        for row in rows:
            print(
                "  ledger row: kind=%s model=%s prompt=%s completion=%s total=%s" % (
                    row.kind, row.model, row.prompt_tokens,
                    row.completion_tokens, row.total_tokens,
                )
            )
        return 0 if prediction.actions and not incomplete else 1


if __name__ == "__main__":
    raise SystemExit(main())
