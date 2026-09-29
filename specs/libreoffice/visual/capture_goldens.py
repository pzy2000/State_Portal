#!/usr/bin/env python3
"""Regenerate LibreOffice V2 before/after visual oracle goldens.

This script runs each LibreOffice V2 spec through the normal WarpTest runtime with
visual capture enabled and copies the runtime-produced before/after screenshots
over the committed golden pair. It requires a matching soffice/pyuno setup plus an
unlocked desktop session.
"""
from __future__ import annotations

import shutil
import hashlib
import json
import sys
from pathlib import Path

from PIL import Image
import yaml

ROOT = Path(__file__).resolve().parents[3]
SPEC_DIR = ROOT / "specs" / "libreoffice"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.libreoffice_runtime import LibreOfficeRuntimeAdapter


def _png_info(path: Path) -> dict[str, object]:
    with Image.open(path) as image:
        width, height = image.size
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
        "width": width,
        "height": height,
    }


def main() -> int:
    specs = sorted(path for path in SPEC_DIR.glob("*.yaml") if path.is_file())
    manifest = []
    for spec_path in specs:
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        if not isinstance(spec, dict):
            raise RuntimeError(f"Spec is not a mapping: {spec_path}")
        visual = spec.get("visual_oracle")
        if not isinstance(visual, dict):
            raise RuntimeError(f"Spec is missing visual_oracle: {spec_path}")
        report_dir = ROOT / "reports" / "warptest" / "libreoffice-golden-capture" / spec_path.stem
        with LibreOfficeRuntimeAdapter(ROOT / "games" / "LibreOffice", report_dir, headless=False, visual_capture=True) as runtime:
            result = runtime.run_checkpoint(spec, spec_path)
        if result.get("status") != "success":
            raise RuntimeError(f"Runtime did not complete {spec_path}: {result.get('report_path')}")
        before_value = result.get("screenshot_before")
        after_value = result.get("screenshot_after")
        before = Path(str(before_value)) if before_value else None
        after = Path(str(after_value)) if after_value else None
        if before is None or after is None or not before.exists() or not after.exists():
            raise RuntimeError(f"Runtime did not produce before/after screenshots for {spec_path}")
        if before.stat().st_size <= 0 or after.stat().st_size <= 0:
            raise RuntimeError(f"Runtime produced empty screenshots for {spec_path}")
        before_info = _png_info(before)
        after_info = _png_info(after)
        entry = {
            "spec": str(spec_path.relative_to(ROOT)),
            "run_report_path": str(Path(result["report_path"]).resolve()),
            "runtime_screenshot_before": before_info,
            "runtime_screenshot_after": after_info,
            "goldens": {},
        }
        for key, source in [("golden_before", before), ("golden_after", after), ("golden_image", after)]:
            declared = visual.get(key)
            if not isinstance(declared, str) or not declared:
                raise RuntimeError(f"{spec_path} missing visual_oracle.{key}")
            target = Path(declared)
            if not target.is_absolute():
                target = spec_path.parent / target
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target_info = _png_info(target)
            target_info["path"] = str(target.relative_to(ROOT))
            entry["goldens"][key] = target_info
            print(f"{spec_path.name}: wrote {target.relative_to(ROOT)}")
        manifest.append(entry)
    manifest_path = ROOT / "reports" / "warptest" / "libreoffice-golden-capture" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"specs": manifest}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {manifest_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
