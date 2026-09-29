from __future__ import annotations

import os
import shutil
from pathlib import Path

from warptest.c1_lazarus_selection.luanti_15519_headless_adapter import (
    _PROBE_BASE,
    _persist_downstream,
    _attempt_semantically_complete,
    _semantic_golden_payload,
    _prepare_variant,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        "local-test/luanti-15519-headless-adapter",
        "preannotation-execution",
        "2026-08-14T00:00:00Z",
        os.getuid(),
    )


def _artifact(root: Path, artifact_id: str, kind: str, version: str, payload: dict[str, object]) -> StableArtifact:
    return StableArtifact(
        ArtifactReference(artifact_id, kind, version),
        "2026-08-14T00:00:01Z",
        (),
        payload,
        _authorization(),
        root / "unused.json",
        root / "unused-head.json",
    )


def _binding(root: Path):
    from warptest.c1_lazarus_selection.luanti_15519_headless_adapter import HeadlessProbeBinding

    fixed = root / "fixed"
    buggy = root / "buggy"
    for tree in (fixed, buggy):
        (tree / "bin").mkdir(parents=True)
        binary = tree / "bin/luanti"
        binary.write_bytes(b"registered")
        binary.chmod(0o700)
    fixture = root / "fixture"
    for relative in (
        "game.conf",
        "minetest.conf",
        "mods/c1_plural/mod.conf",
        "mods/c1_plural/init.lua",
        "mods/c1_plural/locale/c1_plural.ru.po",
    ):
        path = fixture / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    return HeadlessProbeBinding(
        _artifact(root, "source/v2", "source", "2.0", {}),
        _artifact(root, "patch/v2", "patch", "2.0", {}),
        _artifact(root, "atomicity/v2", "atomicity", "2.0", {}),
        _artifact(root, "manifest/v2", "manifest", "2.0", {}),
        _artifact(root, "capsule/v2", "capsule", "2.0", {}),
        _artifact(root, "client/v1", "client", "1.0", {}),
        _artifact(root, "materialization/v1", "materialization", "1.0", {}),
        _artifact(
            root,
            "mapping/v2",
            "mapping",
            "2.0",
            {"fixture_root": "fixture"},
        ),
        fixed,
        buggy,
        fixed / "bin/luanti",
        buggy / "bin/luanti",
    )


def test_semantic_completion_requires_real_start_replay_and_oracle() -> None:
    common = {
        "process_outcome": "completed",
        "registered_start": True,
        "shutdown_marker_observed": True,
        "process_group_reaped": True,
        "ordered_replay": [
            "C1 plural fixture loaded",
            "C1_HEADLESS_REPLAY_STEP=semantic_observation",
            "C1_HEADLESS_DONE",
        ],
    }
    fixed = {
        **common,
        "semantic_observation": {
            "fixture_loaded": True,
            "invalid_plural_forms_observed": False,
            "translated_russian_observed": True,
            "invariant_oracle": True,
        },
    }
    buggy = {
        **common,
        "semantic_observation": {
            "fixture_loaded": True,
            "invalid_plural_forms_observed": True,
            "translated_russian_observed": False,
            "invariant_oracle": True,
        },
    }
    assert _attempt_semantically_complete("fixed", fixed)
    assert _attempt_semantically_complete("buggy", buggy)
    fallback_shutdown = dict(fixed)
    fallback_shutdown["ordered_replay"] = [
        "C1 plural fixture loaded",
        "C1_HEADLESS_REPLAY_STEP=semantic_observation",
        "Server: Shutting down",
    ]
    assert _attempt_semantically_complete("fixed", fallback_shutdown)
    broken = dict(fixed)
    broken["shutdown_marker_observed"] = False
    assert not _attempt_semantically_complete("fixed", broken)


def test_probe_fixture_isolated_and_requests_deterministic_shutdown(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    run = tmp_path / _PROBE_BASE / "probe-1"
    run.parent.mkdir(parents=True)
    spec = _prepare_variant(
        tmp_path,
        run,
        binding,
        "fixed",
        binding.fixed_tree,
        binding.fixed_binary,
    )
    script = (spec.cwd / "games/c1_plural/mods/c1_plural/init.lua").read_text()
    assert "C1_HEADLESS_REPLAY_STEP=semantic_observation" in script
    assert "C1_HEADLESS_DONE" in script
    assert "request_shutdown" in script
    assert "register_on_mods_loaded" in script
    assert "minetest.after(0.1" in script
    assert 'core.get_translated_string("ru", S("apple"))' not in script
    assert (tmp_path / "fixture/mods/c1_plural/init.lua").read_text() == "fixture\n"
    assert "C1_HEADLESS" not in (binding.fixed_tree / "games/c1_plural").as_posix()
    shutil.rmtree(run)


def test_v4_downstream_receipts_include_explicit_oracle_before_visuals(
    tmp_path: Path,
) -> None:
    from warptest.c1_lazarus_selection.luanti_15519_headless_adapter import (
        _ORACLE,
    )

    binding = _binding(tmp_path)
    common = {
        "process_outcome": "completed",
        "registered_start": True,
        "shutdown_marker_observed": True,
        "process_group_reaped": True,
        "ordered_replay": [
            "C1 plural fixture loaded",
            "C1_HEADLESS_REPLAY_STEP=semantic_observation",
            "Server: Shutting down",
        ],
    }
    attempts = tuple(
        _artifact(
            tmp_path,
            f"attempt/{variant}/v4",
            "candidate_headless_server_attempt",
            "4.0",
            {
                **common,
                "variant": variant,
                "semantic_observation": {
                    "fixture_loaded": True,
                    "invalid_plural_forms_observed": variant == "buggy",
                    "translated_russian_observed": variant == "fixed",
                    "invariant_oracle": True,
                },
            },
        )
        for variant in ("fixed", "buggy")
    )
    result = _persist_downstream(
        tmp_path,
        binding=binding,
        attempts=attempts,
        state="passed",
        outcome=None,
        reason=None,
        authorization=_authorization(),
    )
    assert result.state == "passed"
    assert result.oracle.reference == _ORACLE
    assert result.oracle.payload["hidden_oracle"] is True
    assert result.construction.payload["oracle_state"] == "passed"
    assert result.construction.payload["golden_state"] == "pending_null_video_frame_capture"


def test_semantic_golden_uses_real_after_states_and_never_invents_before(
    tmp_path: Path,
) -> None:
    common = {
        "process_outcome": "completed",
        "returncode": 0,
        "registered_start": True,
        "shutdown_marker_observed": True,
        "process_group_reaped": True,
        "ordered_replay": [
            "C1 plural fixture loaded",
            "C1_HEADLESS_REPLAY_STEP=semantic_observation",
            "Server: Shutting down",
        ],
    }
    attempts = tuple(
        _artifact(
            tmp_path,
            f"attempt/{variant}/v4",
            "candidate_headless_server_attempt",
            "4.0",
            {
                **common,
                "variant": variant,
                "semantic_observation": {
                    "fixture_loaded": True,
                    "invalid_plural_forms_observed": variant == "buggy",
                    "translated_russian_observed": variant == "fixed",
                    "invariant_oracle": True,
                },
            },
        )
        for variant in ("fixed", "buggy")
    )
    payload = _semantic_golden_payload(attempts)  # type: ignore[arg-type]
    assert payload["state"] == "downstream_pending"
    assert payload["common_before"] == {
        "state": "missing",
        "reason": "v4_replay_registered_after_start_only",
        "synthetic": False,
    }
    assert payload["fixed_after"]["synthetic"] is False
    assert payload["buggy_after"]["synthetic"] is False
    assert payload["fixed_after"]["observation"]["translated_russian_observed"] is True
    assert payload["buggy_after"]["observation"]["invalid_plural_forms_observed"] is True
