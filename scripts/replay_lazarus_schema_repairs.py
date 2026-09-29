#!/usr/bin/env python3
"""Replay archived Lazarus synthesis/repair outputs without provider calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from warptest.lazarus_p2p_only_schema import REVISION  # noqa: E402
from warptest.synthesis import (  # noqa: E402
    ModelCallResult,
    SynthesisError,
    finalize_formal_c1_model_output,
)
from warptest.task_schema import PublicIssue  # noqa: E402


class RecordedRepair:
    def __init__(self, path: Path):
        self.path = path
        self.calls = 0

    def converse(self, model, messages, **kwargs):
        self.calls += 1
        if self.calls > 1 or not self.path.is_file():
            raise SynthesisError(
                "No recorded repair response available; offline replay does not call a provider."
            )
        return ModelCallResult(content=self.path.read_text(), latency_ms=0)


def replay(run_report: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=False)
    run_bytes = run_report.read_bytes()
    run = json.loads(run_bytes)
    seen = set()
    rows = []
    for cell in run["reports"]:
        shared = (cell.get("runtime") or {}).get("trace", {}).get("synthesis") or {}
        if cell.get("arm") != "lazarus_state" or shared.get("usable") is not False:
            continue
        if shared["synthesis_id"] in seen:
            continue
        seen.add(shared["synthesis_id"])
        source = Path(shared["artifact_dir"])
        saved = json.loads((source.parent / "checkpoint.json").read_text())
        public = saved["identity"]["issue"]
        task = PublicIssue(**public)
        raw = source / "raw-response.txt"
        client = RecordedRepair(source / "format-repair/response.txt")
        item = {
            "task_id": cell["task_id"],
            "model": cell["model"],
            "seed": cell["seed"],
            "synthesis_id": shared["synthesis_id"],
            "original_failure": saved["failure"],
            "source_artifacts": str(source),
            "contract_valid_after_replay": False,
        }
        if raw.is_file():
            item["original_output_sha256"] = hashlib.sha256(
                raw.read_bytes()
            ).hexdigest()
            try:
                checkpoint, metadata = finalize_formal_c1_model_output(
                    task=task,
                    raw_output=raw.read_text(),
                    client=client,
                    model=cell["model"],
                    located={"repo": str(source), "inputs_read": []},
                    root=ROOT,
                    artifact_dir=out / shared["synthesis_id"],
                    source_cost_kind="checkpoint_exploration",
                )
                read_paths = set(
                    json.loads((source / "synthesis-meta.json").read_text()).get(
                        "files_read", []
                    )
                )
                unread = [
                    entry["source_path"]
                    for entry in checkpoint.source_evidence
                    if entry.get("kind") != "public_task"
                    and entry["source_path"].replace("\\", "/").removeprefix("./")
                    not in read_paths
                ]
                if unread:
                    raise SynthesisError(
                        "Recorded source read evidence does not cover citations: "
                        + repr(unread)
                    )
                item.update(
                    contract_valid_after_replay=True,
                    finalization=metadata,
                    generated_spec=checkpoint.model_generated_spec,
                    source_evidence=checkpoint.source_evidence,
                )
            except SynthesisError as error:
                item["replay_failure"] = str(error)
        else:
            item["replay_failure"] = (
                "No final raw output was recorded; cannot replay missing model output."
            )
        item["recorded_repair_responses_requested"] = client.calls
        rows.append(item)
    result = {
        "schema": "lazarus-synthesis-schema-offline-replay-v1",
        "schema_revision": REVISION,
        "source_run": str(run_report),
        "source_run_sha256": hashlib.sha256(run_bytes).hexdigest(),
        "provider_calls": 0,
        "unique_failed_syntheses": len(rows),
        "contract_valid_after_replay": sum(
            r["contract_valid_after_replay"] for r in rows
        ),
        "still_invalid_or_missing": sum(
            not r["contract_valid_after_replay"] for r in rows
        ),
        "scope": "Reuses recorded outputs and source-read evidence. Contract validity is not runtime execution or task success. Historical results are unchanged.",
        "rows": rows,
    }
    (out / "replay.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    )
    assert run_report.read_bytes() == run_bytes
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-report", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    result = replay(args.run_report.resolve(), args.out_dir.resolve())
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=2))


if __name__ == "__main__":
    main()
