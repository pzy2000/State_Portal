"""Capture real Pal3.Unity visual-oracle golden images.

Run from a machine with Unity 6000.1.10f1 and the PAL3 CPK data mounted in
Application.persistentDataPath/PAL3. The script executes each PAL3 spec through
the Unity checkpoint adapter in headed visual-capture mode and only replaces a
golden image after the semantic run succeeds and Unity returns an informative
frame.

Usage:
    UNITY_EDITOR_PATH=/Applications/Unity/Unity-6000.1.10f1/Unity.app/Contents/MacOS/Unity \
      python3 specs/pal3/visual/capture_goldens.py
    python3 specs/pal3/visual/capture_goldens.py --only team-join-single scene-load-yuzhou
    python3 specs/pal3/visual/capture_goldens.py --repo games/Pal3.Unity
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from warptest.pal3_runtime import Pal3RuntimeAdapter  # noqa: E402
from warptest.reports import new_report_dir, write_json  # noqa: E402
from warptest.visual import screenshot_quality  # noqa: E402

PAL3_REPO = REPO / "games" / "Pal3.Unity"
VISUAL_DIR = REPO / "specs" / "pal3" / "visual"


def pal3_specs(only: list[str] | None) -> list[Path]:
    selected = set(only or [])
    specs = []
    for path in sorted(VISUAL_DIR.parent.glob("*.yaml")):
        if selected and path.stem not in selected:
            continue
        specs.append(path)
    return specs


def capture_one(repo: Path, spec_path: Path, report_root: Path) -> dict[str, object]:
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        raise ValueError(f"PAL3 spec must be a YAML mapping: {spec_path}")
    out_dir = report_root / spec_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {
        "spec": str(spec_path),
        "golden": str(VISUAL_DIR / f"{spec_path.stem}.golden.png"),
        "report_dir": str(out_dir),
        "status": "failed",
    }

    with Pal3RuntimeAdapter(repo, out_dir, headless=False, visual_capture=True) as runtime:
        checkpoint = runtime.run_checkpoint(spec, spec_path)
        result["checkpoint_status"] = checkpoint.get("status")
        result["screenshot_status"] = checkpoint.get("screenshot_status")
        result["screenshot_source"] = checkpoint.get("screenshot_source")
        result["screenshot_detail"] = checkpoint.get("screenshot_detail")
        if checkpoint.get("status") != "success":
            result["detail"] = checkpoint.get("utility_report", {}).get("detail", "semantic checkpoint failed")
            return result
        if checkpoint.get("live_source") != "unity_capture":
            result["detail"] = checkpoint.get("screenshot_detail") or "Unity did not return a captured frame"
            return result

        captured = Path(runtime.screenshot(f"{spec_path.stem}.captured.png"))

    quality = screenshot_quality(captured)
    result["quality"] = quality
    if not quality["informative"]:
        result["detail"] = f"captured PNG is not informative: {quality}"
        return result

    VISUAL_DIR.mkdir(parents=True, exist_ok=True)
    golden = VISUAL_DIR / f"{spec_path.stem}.golden.png"
    shutil.copy2(captured, golden)
    result["status"] = "success"
    result["detail"] = str(golden)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture real PAL3 golden images.")
    parser.add_argument("--only", nargs="*", help="Only capture these PAL3 spec stems.")
    parser.add_argument("--repo", default=str(PAL3_REPO), help="Path to the Pal3.Unity checkout.")
    parser.add_argument("--report-dir", help="Directory for per-spec Unity reports.")
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    report_root = Path(args.report_dir).expanduser().resolve() if args.report_dir else new_report_dir() / "pal3-golden-capture"
    report_root.mkdir(parents=True, exist_ok=True)
    specs = pal3_specs(args.only)
    print(f"Capturing {len(specs)} PAL3 golden(s) using {repo}")
    print(f"report_dir={report_root}")

    results = []
    for spec_path in specs:
        item = capture_one(repo, spec_path, report_root)
        results.append(item)
        status = "OK " if item["status"] == "success" else "FAIL"
        print(f"  [{status}] {spec_path.stem:24s} {item.get('detail', '')}", flush=True)

    summary = {
        "captured": sum(1 for item in results if item["status"] == "success"),
        "failed": sum(1 for item in results if item["status"] != "success"),
        "results": results,
    }
    write_json(report_root / "pal3-golden-capture.json", summary)
    print(f"== captured {summary['captured']}, failed {summary['failed']} ==")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
