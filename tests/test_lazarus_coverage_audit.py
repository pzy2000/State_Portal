"""Coverage gaps must not be mislabeled as corrupt evidence."""
import hashlib
import json
from itertools import product

from scripts.audit_lazarus_p2p_only_run import audit, EXPECTED_MODELS, PAPER_TASK_IDS


def fixture(tmp_path):
    keys = ['task_id', 'model', 'seed', 'arm', 'version']
    cells = [dict(zip(keys, row)) for row in product(PAPER_TASK_IDS, EXPECTED_MODELS,
        [0, 1, 2], ['lazarus_agent_s', 'lazarus_agent_s_synth', 'lazarus_state'], ['buggy', 'fixed'])]
    row = {**cells[0], 'complete': True, 'treatment_tokens': 0}
    cell = tmp_path/'cell.json'
    cell.write_text(json.dumps(row))
    identity = {'fixture': 'same identity'}
    data = {'run.json': {'schema':'c1-lazarus-p2p-only-run-v3', 'identity':identity,
                         'complete':False, 'reports':[row]},
            'readiness.json': {'identity':identity,'complete':True,'expected_cells':594,'plan':{'cells':cells}},
            'run-index.json': {'identity':identity,'complete':False,'groups':[{'complete':False,
                'cells':[{'path':str(cell),'sha256':hashlib.sha256(cell.read_bytes()).hexdigest()}]}]}}
    for name,value in data.items():
        (tmp_path/name).write_text(json.dumps(value))
    return cell


def test_partial_coverage_can_have_intact_evidence(tmp_path):
    fixture(tmp_path)
    result = audit(tmp_path)
    assert result['complete'] is False
    assert result['coverage_complete'] is False
    assert result['evidence_integrity_complete'] is True
    assert result['integrity_failures'] == []
    assert len(result['missing_trial_keys']) == 593


def test_partial_coverage_still_rejects_corrupt_snapshots(tmp_path):
    cell = fixture(tmp_path)
    cell.write_text('tampered')
    result = audit(tmp_path)
    assert result['evidence_integrity_complete'] is False
    assert any('snapshot SHA drift' in f for f in result['integrity_failures'])
