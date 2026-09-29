"""Public Start constraints must agree with observed state and oracle inputs."""
import copy
from pathlib import Path

import pytest
import yaml

from warptest.c1_lazarus_selection.p2p_only_task_text import TASK_TEXT
from warptest.lazarus_p2p_only_generated_runtime import validate_observed_start
from warptest.lazarus_p2p_only_identity import grading_sha256
from warptest.lazarus_p2p_only_native import GeneratedLuantiSession
from warptest.lazarus_p2p_only_spec import state_errors

ROOT = Path(__file__).resolve().parents[1]
VARIABLES = 'lzp-jupyterlab-18464-debugger-zero-variable-detail'
WORLD = 'lzp-luanti-16799-world-mods'
EDITOR = 'lzp-openra-22283-tileset-template-order'


def task(name, host):
    return {'id': name, 'benchmark': host}


@pytest.mark.parametrize('integer', ['1', '7', '-12', '42'])
def test_public_integer_initial_values_are_not_a_hidden_seven_requirement(integer):
    observed = {'visible': True, 'panel_open': False, 'selected': False,
        'state': {'component': 'variables', 'theme': 'light',
                  'props': {'scope': 'Locals', 'variables': [{'name': 'total', 'value': integer}]}}}
    assert validate_observed_start(task(VARIABLES, 'jupyterlab'), observed)
    for phase in ['before', 'fixed', 'buggy']:
        rubric = TASK_TEXT[VARIABLES].rubric(phase)
        assert '"7"' not in ' '.join(rubric['must'])
        assert any('particular nonzero integer' in line for line in rubric['ignore'])
    observed['state']['props']['variables'][0]['value'] = '0'
    assert not validate_observed_start(task(VARIABLES, 'jupyterlab'), observed)
    observed['state']['props']['variables'][0]['value'] = integer
    observed['state']['props']['variables'].append({'name': 'count', 'value': '0'})
    assert not validate_observed_start(task(VARIABLES, 'jupyterlab'), observed)


def test_zero_defect_readout_is_still_required_after_start():
    assert '"count" then "0"' in TASK_TEXT[VARIABLES].fixed_after
    assert 'much smaller "0"' in TASK_TEXT[VARIABLES].buggy_after


def test_world_start_requires_candidate_visible_modded_world():
    observed = {'state': {'tab': 'local', 'worlds': [{'name': 'disk_only', 'mods': ['a']}]},
                'dialog': 'none', 'runtime_healthy': True}
    assert not validate_observed_start(task(WORLD, 'luanti'), observed)
    for worlds in [[], [{'name': 'test_world', 'mods': []}]]:
        observed['visible_worlds'] = worlds
        assert not validate_observed_start(task(WORLD, 'luanti'), observed)
    observed['visible_worlds'] = [{'name': 'any_public_name', 'mods': ['my_mod']}]
    assert validate_observed_start(task(WORLD, 'luanti'), observed)
    observed['dialog'] = 'dlg_config_world'
    assert not validate_observed_start(task(WORLD, 'luanti'), observed)


def test_invalid_game_rejected_before_world_files_change(tmp_path, monkeypatch):
    session = GeneratedLuantiSession(tree=tmp_path, output_dir=tmp_path/'session', task_id=WORLD)
    marker = session.user_root/'worlds/existing/world.mt'
    marker.parent.mkdir(parents=True)
    marker.write_text('gameid = devtest\n')
    calls = []
    def rpc(request):
        calls.append(request)
        return {'available_games': ['devtest']}
    monkeypatch.setattr(session, 'rpc', rpc)
    with pytest.raises(ValueError, match='gameid is unavailable'):
        session.restore_generated_state({'tab':'local','settings':{},'worlds':[
            {'name':'world','gameid':'missing_game','mods':[]}]})
    assert marker.read_text() == 'gameid = devtest\n'
    assert calls == [{'type':'observe'}]


def test_editor_start_checks_actual_palette_not_just_editor_screen():
    observed = {'state': {'screen':'editor','map_index':0}, 'runtime_healthy':True,
                'goal_dialog_open':False}
    assert not validate_observed_start(task(EDITOR, 'openra'), observed)
    observed['widgets'] = [{'id':'TILE_WIDGETS','visible':True}, {'id':'TILE_PREVIEW','visible':True}]
    assert not validate_observed_start(task(EDITOR, 'openra'), observed)
    observed['widgets'][0]['visible'] = False
    observed['widgets'][1]['visible'] = False
    assert validate_observed_start(task(EDITOR, 'openra'), observed)
    observed['widgets'][0]['visible'] = True  # An explicitly observed empty palette is allowed.
    assert validate_observed_start(task(EDITOR, 'openra'), observed)


def test_editor_tab_schema_preserves_old_state_and_rejects_unsupported_tabs():
    state = {'screen':'editor','map_index':0}
    assert not state_errors(state, 'openra')
    for tab in ['overlays','tiles']:
        assert not state_errors({**state,'editor_tab':tab}, 'openra')
    assert state_errors({**state,'editor_tab':'secret-task-start'}, 'openra')
    assert state_errors({**state,'editor_tab':[]}, 'openra')
    assert state_errors({'screen':'mainmenu','editor_tab':'overlays'}, 'openra')


def test_start_rubrics_do_not_ignore_actual_invalid_starts():
    editor = TASK_TEXT[EDITOR].rubric('before')
    assert 'populated' in ' '.join(editor['must'])
    assert 'Map identity' in ' '.join(editor['ignore'])
    world = TASK_TEXT[WORLD].rubric('before')
    assert 'visibly lists at least one world' in ' '.join(world['must'])
    settings = TASK_TEXT['lzp-luanti-16679-unavailable-settings'].rubric('before')
    assert 'No settings dialog' in ' '.join(settings['must'])
    assert 'not an open settings dialog' in ' '.join(settings['ignore'])


def test_oracle_change_invalidates_grading_identity(tmp_path, monkeypatch):
    import warptest.lazarus_p2p_only_identity as identity
    monkeypatch.setattr(identity, 'file_sha256', lambda p: p.read_text() if p.exists() else 'fixed-code')
    p = tmp_path/'specs/luanti/lazarus_p2p_only/lzp-test.yaml'
    p.parent.mkdir(parents=True)
    p.write_text('first rubric')
    before = grading_sha256(tmp_path)
    p.write_text('different rubric')
    assert grading_sha256(tmp_path) != before


def test_authored_registry_regenerates_the_checked_in_oracles():
    from scripts.apply_lazarus_p2p_only_task_text import _apply_visual_text
    for name in [VARIABLES, WORLD, EDITOR, 'lzp-luanti-16679-unavailable-settings']:
        p=ROOT/'specs'/name.split('-')[1]/'lazarus_p2p_only'/f'{name}.yaml'
        original=yaml.safe_load(p.read_text())
        regenerated=copy.deepcopy(original)
        _apply_visual_text(regenerated, TASK_TEXT[name])
        assert regenerated == original


def test_nonzero_constraint_is_public_in_c_and_shared_de_prompts(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from warptest import lazarus_p2p_only_paid_runtime as paid
    from warptest.lazarus_p2p_only_contract import C, public_start_constraint
    from warptest.lazarus_p2p_only_synthesis import public_issue
    catalog=yaml.safe_load((ROOT/'benchmarks/p5/c1_lazarus_p2p_only_tasks.yaml').read_text())
    entry=next(t for t in catalog['tasks'] if t['id']==VARIABLES)
    context=SimpleNamespace(task=entry,key=SimpleNamespace(task_id=VARIABLES,arm=C),
                            agent_source=SimpleNamespace(forbidden_tokens=()))
    constraint=public_start_constraint(VARIABLES)
    assert constraint and '7' not in constraint
    assert constraint in public_issue(context).issue
    captured={}
    monkeypatch.setenv('RELAY_BASE_URL','http://not-called.invalid')
    monkeypatch.setattr(paid, 'FixedVisualBridge', lambda **kw: object())
    monkeypatch.setattr(paid, '_run_phase', lambda **kw: captured.update(kw))
    monkeypatch.setattr(paid, '_persist_phase_actions', lambda *a: None)
    runner=paid.P2POnlyPaidRuntimeRunner(workspace=ROOT,runtime_root=tmp_path)
    runner._phase(object(),object(),context,tmp_path,'phase-a',100)
    assert constraint in captured['instruction']
