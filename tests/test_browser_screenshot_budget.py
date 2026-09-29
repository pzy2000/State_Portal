import json
from types import SimpleNamespace
import pytest
from warptest.runtime import BrowserRuntimeAdapter
from warptest.c1_task_deadline import c1_task_deadline


def test_capture_budget_respects_trial_deadline_and_preserves_options(tmp_path):
    observed = []
    runtime = BrowserRuntimeAdapter.__new__(BrowserRuntimeAdapter)
    runtime.report_dir = tmp_path
    runtime.page = SimpleNamespace(screenshot=lambda **kwargs: observed.append(kwargs))
    assert runtime.screenshot('before.png', full_page=True) == str(tmp_path / 'before.png')
    assert observed[-1]['timeout'] == 60_000
    assert observed[-1]['full_page'] is True
    with c1_task_deadline('short-capture', 2):
        runtime.screenshot('after.png')
    assert 0 < observed[-1]['timeout'] <= 2000


def test_optional_capture_keeps_raw_diagnostic_and_does_not_accept_failure(tmp_path):
    from warptest.state_store import StateSession
    runtime = BrowserRuntimeAdapter.__new__(BrowserRuntimeAdapter)
    runtime.report_dir = tmp_path
    error = TimeoutError('compositor did not finish')
    def fail(**kwargs):
        raise error
    runtime.page = SimpleNamespace(screenshot=fail)
    assert StateSession._safe_screenshot(runtime, 'before.png') is None
    with pytest.raises(TimeoutError) as caught:
        runtime.screenshot('after.png')
    assert caught.value is error
    rows = [json.loads(line) for line in (tmp_path / 'screenshot-errors.jsonl').read_text().splitlines()]
    assert [row['name'] for row in rows] == ['before.png', 'after.png']
    assert all(row['error_type'] == 'TimeoutError' for row in rows)
    assert all('compositor did not finish' in row['error'] for row in rows)
    assert not (tmp_path / 'before.png').exists()
