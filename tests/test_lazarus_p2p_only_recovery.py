from warptest import cost
from warptest.lazarus_p2p_only_recovery import classify_failure
from warptest.synthesis import SynthesisError


def test_transient_classification_preserves_terminal_usage_and_auth():
    assert classify_failure(SynthesisError('outage', artifacts={'failure_mode':'gateway_outage'}))['retryable']
    assert classify_failure(ConnectionError('connection refused'))['retryable']
    error = RuntimeError('rate limit')
    error.status_code = 429
    assert classify_failure(error)['retryable']
    for error in (
        cost.UsageAccountingError('unknown provider usage'),
        SynthesisError('timeout', artifacts={'failure_mode':'usage_accounting_incomplete'}),
        SynthesisError('rejected', artifacts={'failure_mode':'client_error','status_code':401}),
        SynthesisError('request too large', artifacts={'failure_mode':'provider_capacity','status_code':429}),
        TimeoutError('read timeout'),
        ValueError('checkpoint integrity drift'),
    ):
        assert not classify_failure(error)['retryable']


def test_nested_cost_observer_counts_calls_once_and_preserves_phase_totals():
    with cost.observe_calls() as ledger, cost.scoped_meter() as outer:
        cost.record_call('test', prompt_tokens=2, completion_tokens=3, total_tokens=5)
        with cost.scoped_meter() as phase:
            cost.record_call('test', prompt_tokens=3, completion_tokens=4, total_tokens=7)
        assert phase.summary()['total_tokens'] == 7
        assert outer.summary()['total_tokens'] == 5
    assert ledger.summary()['total_tokens'] == 12
    assert ledger.summary()['llm_calls'] == 2
    with cost.scoped_meter():
        cost.record_call('test', total_tokens=1)
    assert ledger.summary()['llm_calls'] == 2


def test_recorded_agent_s_429_is_recoverable_but_capacity_and_read_timeout_are_not(tmp_path):
    import json
    from warptest.lazarus_p2p_only_executor import _recorded_provider_failure
    phase = tmp_path / "phase-a"
    phase.mkdir()
    row = {"runtime": {"trace": {"trajectory": {"artifact_dir": str(tmp_path)}}}}
    for message, retryable in (
        ("AgentSWorkerError: HTTP 429 rate limit", True),
        ("InternalServerError: Error code: 500 - {'error': {'status': 500}}", True),
        ("APIStatusError: {'status_code': 502}", True),
        ("APIConnectionError: Connection error.", True),
        ("AgentSWorkerError: HTTP 401 unauthorized", False),
        ("TPM limit exceeded: 0 + 200 > 100 for model test", False),
    ):
        (phase / "actions.json").write_text(json.dumps({"actions":[{"error":message,"infrastructure_error":True}]}))
        assert _recorded_provider_failure(row)["retryable"] is retryable
    (phase / "actions.json").write_text(json.dumps({"actions":[{"error":"read timeout","infrastructure_error":True}]}))
    assert _recorded_provider_failure(row) is None
    (phase / "actions.json").write_text(json.dumps({"actions":[{
        "error":"invalid model action containing HTTP 500", "infrastructure_error":False
    }]}))
    assert _recorded_provider_failure(row) is None


def test_preflight_failure_keeps_prior_billable_usage(monkeypatch):
    import pytest
    from scripts import preflight_lazarus_p2p_only_api as preflight
    def failed(*args, **kwargs):
        cost.record_call("test", prompt_tokens=2, completion_tokens=3, total_tokens=5)
        raise SynthesisError("outage", artifacts={"failure_mode":"gateway_outage"})
    monkeypatch.setattr(preflight, "_probe", failed)
    with pytest.raises(SynthesisError) as caught:
        preflight._measured_probe("test", roles=["decision"])
    assert caught.value.probe_usage["total_tokens"] == 5
    assert classify_failure(caught.value)["retryable"]


def test_deadline_recovery_requires_complete_provider_usage():
    from warptest.c1_task_deadline import C1TaskTimeout
    from warptest.lazarus_p2p_only_executor import _accounted_failure
    error = C1TaskTimeout("registered deadline exceeded")
    assert _accounted_failure(error, {"usage_complete":True})["retryable"]
    assert not _accounted_failure(error, {"usage_complete":False})["retryable"]
    assert not _accounted_failure(error, {})["retryable"]


def test_p2p_timeout_recovery_rejects_assertions_missing_evidence_and_unknown_usage():
    import copy
    from warptest.lazarus_p2p_only_executor import _recorded_p2p_timeout, _accounted_failure
    row = {
        'failures': ['per-trial P2P did not pass for control', 'P2P subset failed'],
        'per_trial_p2p_evidence': {
            'completed': True, 'passed': False,
            'failures': ['upstream test control timed out'],
            'results': [{'id': 'control', 'passed': False, 'timed_out': True, 'exit_code': None}],
        },
    }
    recovery = _recorded_p2p_timeout(row)
    assert recovery['retryable'] and recovery['category'] == 'p2p_control_timeout'
    error = RuntimeError('incomplete P2P')
    error.recovery = recovery
    assert _accounted_failure(error, {'usage_complete': False})['category'] == 'usage_unknown'
    bad = copy.deepcopy(row)
    bad['per_trial_p2p_evidence']['results'][0].update(timed_out=False, exit_code=1)
    assert _recorded_p2p_timeout(bad) is None
    bad = copy.deepcopy(row)
    bad['failures'].append('before evidence is missing')
    assert _recorded_p2p_timeout(bad) is None
    bad = copy.deepcopy(row)
    bad['per_trial_p2p_evidence']['completed'] = False
    assert _recorded_p2p_timeout(bad) is None
    assert _recorded_p2p_timeout({}) is None


def test_runtime_phase_timeout_recovers_without_enumerating_call_sites():
    from warptest.lazarus_p2p_only_executor import _local_runtime_timeout, _accounted_failure
    known = {'usage_complete': True, 'llm_calls': 0}
    billed = {'usage_complete': True, 'llm_calls': 17, 'total_tokens': 63643}

    def row(error, *, phase='runtime execution failed', category='infrastructure_or_integrity',
            retryable=False, extra=()):
        return {
            'failures': [f'{phase}: {error}', 'runtime observation is incomplete', *extra],
            'infrastructure_failure': {'retryable': retryable, 'category': category, 'error': error},
        }

    # Every observed call site recovers, and so does an unseen one: the phase
    # prefix is the signal, not the message.
    for error in (
        'TimeoutError: Page.screenshot: Timeout 30000ms exceeded.',
        'TimeoutError: Locator.screenshot: Timeout 29975ms exceeded.',
        'RuntimeError: timed out waiting for the luanti F12 screenshot',
        'RuntimeError: candidate typed-state IPC timed out',
        'TimeoutError: OpenRA background capture timed out: [Errno 2] No such file',
        'RuntimeError: some future adapter handshake timed out',
    ):
        for usage in (known, billed):
            recovered = _local_runtime_timeout(row(error), usage)
            assert recovered['retryable'] and recovered['category'] == 'local_runtime_timeout'
        assert _local_runtime_timeout(row(error), {'usage_complete': False}) is None
        assert _local_runtime_timeout(row(error), {}) is None

    timeout = 'TimeoutError: Page.screenshot: Timeout 30000ms exceeded.'
    # A stall reported by another phase is not the local runtime runner's.
    assert _local_runtime_timeout(row(timeout, phase='grading failed'), known) is None
    # Integrity drift and non-timeout runtime errors stay terminal.
    assert _local_runtime_timeout(row('ValueError: checkpoint integrity drift'), known) is None
    assert _local_runtime_timeout(row('RuntimeError: luanti exited before a screenshot'), known) is None
    # Unknown usage and already-typed dispositions are left alone.
    assert _local_runtime_timeout(row(timeout, category='usage_unknown'), known) is None
    assert _local_runtime_timeout(row(timeout, category='provider_transient', retryable=True), known) is None
    # ScaleCUA requests share the phase but may have generated tokens off-ledger.
    for stall in (
        'RuntimeError: ScaleCUA worker timed out during predict after 360.0s',
        'RuntimeError: ScaleCUA provider request timed out after dispatch',
    ):
        assert _local_runtime_timeout(row(stall), known) is None
    assert _local_runtime_timeout({}, known) is None
    # Provider read timeouts never reach the row path and stay terminal.
    assert not _accounted_failure(TimeoutError('read timeout'), known)['retryable']


def test_eight_policy_lanes_share_two_p2p_slots_and_release_after_failure():
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    import pytest
    from warptest.lazarus_p2p_only_executor import _p2p_admission
    slots = threading.BoundedSemaphore(2)
    barrier = threading.Barrier(8)
    lock = threading.Lock()
    active = peak = completed = 0
    def lane():
        nonlocal active, peak, completed
        barrier.wait(timeout=5)
        with _p2p_admission(slots) as wait_ms:
            assert wait_ms >= 0
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
                completed += 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: lane(), range(8)))
    assert peak == 2 and completed == 8
    with pytest.raises(ValueError):
        with _p2p_admission(slots):
            raise ValueError('control failure')
    assert slots.acquire(blocking=False)
    assert slots.acquire(blocking=False)
    slots.release()
    slots.release()


def test_p2p_admission_wait_is_bounded_by_existing_trial_deadline():
    import pytest
    from warptest.c1_task_deadline import c1_task_deadline
    from warptest.lazarus_p2p_only_executor import _p2p_admission, _accounted_failure
    observed = []
    class UnavailableSlots:
        def acquire(self, *, timeout):
            observed.append(timeout)
            return False
        def release(self):
            raise AssertionError('unacquired slot must not be released')
    with c1_task_deadline('waiting-control', 7), pytest.raises(TimeoutError) as caught:
        with _p2p_admission(UnavailableSlots()):
            raise AssertionError('must not enter')
    assert 0 < observed[0] <= 7
    assert _accounted_failure(caught.value, {'usage_complete': True})['retryable']
    assert _accounted_failure(caught.value, {'usage_complete': False})['category'] == 'usage_unknown'
