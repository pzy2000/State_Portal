from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmarks/p5/c1_kimi_k3_high_16tasks.yaml"


def test_kimi_k3_manifest_freezes_exact_formal_matrix() -> None:
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))

    assert data["models"] == ["kimi-k3@relay"]
    assert data["model_request_profiles"] == {"kimi-k3": "reasoning_high"}
    assert data["seeds"] == 1
    assert data["seed_base"] == 0
    assert data["c1_task_count"] == 16
    assert len(data["c1_task_ids"]) == len(set(data["c1_task_ids"])) == 16
    assert list(data["arms"]) == ["c1_agent_s", "c1_agent_s_synth", "c1_state"]
    assert data["usage_accounting"]["reasoning_detail"]["tolerance_fraction"] == 0.10
    assert data["c1_background_pixel_protocol"]["foreground_required"] is False
