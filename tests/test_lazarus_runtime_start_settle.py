from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_lazarus_runtime import (  # noqa: E402
    _LuantiSession,
    _luanti_phase0_context,
    _luanti_public_generated_spec,
)

from warptest.lazarus_phase0 import LazarusPhase0CellContext  # noqa: E402
from warptest.lazarus_runtime import (  # noqa: E402
    _LUANTI_PUBLIC_START_VIEW_ATTEMPTS,
    LuantiLazarusStateExecution,
)


def _context_with_oracle(tmp_path: Path) -> LazarusPhase0CellContext:
    context = _luanti_phase0_context(tmp_path, version="buggy")
    spec_path = tmp_path / "specs" / "luanti.yaml"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(
        yaml.safe_dump(
            {
                "target": {"kind": "synthesized_world"},
                "evaluation": {
                    "lazarus": {
                        "goal_checks": [
                            {
                                "type": "luanti_lazarus_state_audit",
                                "field": "marker_node",
                                "expected": "basenodes:dirt_with_grass",
                            }
                        ],
                        "defect_signature": [
                            {
                                "type": "luanti_lazarus_state_audit",
                                "field": "marker_node",
                                "expected": "basenodes:desert_sand",
                            }
                        ],
                        "invariant_checks": [{"type": "no_luanti_mod_errors"}],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return LazarusPhase0CellContext(
        **{
            **context.__dict__,
            "task": {
                **context.task,
                "oracle_spec": str(spec_path.relative_to(tmp_path)),
            },
        }
    )


def _execute(
    tmp_path: Path, *, view_failures: int
) -> tuple[Any, _LuantiSession, list[float]]:
    sessions: list[_LuantiSession] = []
    settled: list[float] = []

    def session_factory(repo, out_dir, **kwargs):
        session = _LuantiSession(repo, out_dir, kwargs)
        session.public_view_failures = view_failures
        sessions.append(session)
        return session

    executor = LuantiLazarusStateExecution(
        workspace=tmp_path,
        runtime_root=tmp_path / f"runtime-{view_failures}",
        session_factory=session_factory,
        asset_sync=lambda root: {"status": "synced", "files": 1, "root": str(root)},
        sleep_fn=settled.append,
    )
    result = executor.execute(
        _context_with_oracle(tmp_path),
        _luanti_public_generated_spec(),
        checkpoint_sha256="e" * 64,
    )
    return result, sessions[0], settled


def _view_checks(session: _LuantiSession) -> list[tuple[str, list, list]]:
    return [
        call
        for call in session.calls
        if call[0] == "fast"
        and call[2]
        and call[2][0].get("type") == "luanti_player_view_matches"
    ]


def test_an_unsettled_camera_is_retried_after_a_fresh_player_restore(
    tmp_path: Path,
) -> None:
    result, session, settled = _execute(tmp_path, view_failures=1)

    assert result.before_semantic_ok is True
    assert result.goal_checks_ok is False
    assert len(_view_checks(session)) == 2
    # Each retry re-issues the coordinator's player restore before re-checking.
    assert [call[0] for call in session.calls[:4]] == [
        "restore",
        "restore",
        "fast",
        "restore",
    ]
    # The registered settle happens only after the view is accepted.
    assert settled == [16.0, 2.0]


def test_a_genuinely_wrong_start_stays_a_bounded_failure(tmp_path: Path) -> None:
    result, session, settled = _execute(
        tmp_path, view_failures=_LUANTI_PUBLIC_START_VIEW_ATTEMPTS + 5
    )

    assert result.before_semantic_ok is False
    assert result.goal_checks_ok is None
    assert result.defect_signature_ok is None
    assert len(_view_checks(session)) == _LUANTI_PUBLIC_START_VIEW_ATTEMPTS
    assert settled == []


def test_an_applied_camera_is_checked_once_before_the_registered_settle(
    tmp_path: Path,
) -> None:
    result, session, settled = _execute(tmp_path, view_failures=0)

    assert result.before_semantic_ok is True
    assert len(_view_checks(session)) == 1
    assert settled == [16.0, 2.0]
