"""Behavioral proof for the explicitly authorized Kimi capacity exception."""
import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from warptest.image_history_backoff import (
    AUDIT_ENV, POLICY_ENV, REVISION, create_with_backoff,
)


class Capacity(Exception):
    status_code = 429

    def __init__(self, message='TPM limit exceeded: 0 + 3500000 > 3000000 for model kimi-k2.6'):
        super().__init__(message)


def test_frozen_manifest_policy_rejects_drift_and_missing_opt_in():
    from warptest.image_history_backoff import protocol, validate_manifest_policy

    validate_manifest_policy({"image_history_backoff": protocol()}, requested=True)
    with pytest.raises(ValueError, match="requires"):
        validate_manifest_policy({"image_history_backoff": protocol()}, requested=False)
    altered = {**protocol(), "sticky": True}
    with pytest.raises(ValueError, match="Unrecognized"):
        validate_manifest_policy({"image_history_backoff": altered}, requested=True)
    validate_manifest_policy({}, requested=False)


def messages():
    return [
        {'role': 'system', 'content': 'Never remove this instruction'},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'old observation'},
                                    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,YQ=='}}]},
        {'role': 'assistant', 'content': 'Full action and reflection text'},
        {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,Yg=='}}]},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'current observation'},
                                    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,Yw=='}}]},
    ]


def count(kwargs):
    return sum(p.get('type') == 'image_url' for m in kwargs['messages']
               if isinstance(m.get('content'), list) for p in m['content'])


def texts(msgs):
    return [t for m in msgs for t in ([m['content']] if isinstance(m['content'], str)
            else [p['text'] for p in m['content'] if p.get('type') == 'text']) if t]


def call(fn, kwargs, tmp_path, audit=None, enabled=True, usage=None):
    with patch.dict('os.environ', {POLICY_ENV: REVISION if enabled else '', AUDIT_ENV: str(tmp_path)}):
        return create_with_backoff(fn, kwargs, extract_error_usage=lambda e: usage,
                                   kind='agent_s_worker', audit=audit if audit is not None else [])


def test_only_after_rejection_preserves_all_text_current_pixels_and_original_history(tmp_path):
    source = messages()
    untouched = copy.deepcopy(source)
    calls = []
    audit = []

    def provider(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        if count(kwargs) > 1:
            raise Capacity()
        return 'ok'

    kwargs = {'model': 'kimi-k2.6', 'messages': source,
              'extra_body': {'thinking': {'type': 'disabled'}}, 'max_tokens': 8192, 'temperature': .2}
    assert call(provider, kwargs, tmp_path, audit) == 'ok'
    assert [count(k) for k in calls] == [3, 2, 1]
    assert source == untouched
    assert all(texts(k['messages']) == texts(source) for k in calls)
    assert all(k['messages'][-1] == source[-1] for k in calls)
    assert all(k['extra_body'] == kwargs['extra_body'] and k['max_tokens'] == 8192 for k in calls)
    assert len(audit) == 1
    receipt = json.loads(Path(audit[0]['receipt_path']).read_text())
    assert receipt['status'] == 'response_received'
    assert len(receipt['rejections']) == 2
    assert all(len(r['dropped_image_sha256']) == 64 for r in receipt['rejections'])
    # A fresh logical request must try the original full history again.
    call(provider, {'model': 'kimi-k2.6', 'messages': source}, tmp_path)
    assert count(calls[3]) == 3


@pytest.mark.parametrize('error', [RuntimeError('connection lost'),
    Capacity('TPM limit exceeded: 2900000 + 200000 > 3000000 for model kimi-k2.6'),
    RuntimeError('429 Too Many Requests'), RuntimeError('401 Unauthorized')])
def test_other_failures_never_trim_or_retry(tmp_path, error):
    calls = []
    def provider(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        raise error
    kwargs = {'model': 'kimi-k2.6', 'messages': messages()}
    with pytest.raises(type(error)):
        call(provider, kwargs, tmp_path)
    assert len(calls) == 1 and count(kwargs) == 3
    assert not list(tmp_path.glob('*.json'))


@pytest.mark.parametrize('model,enabled,usage', [
    ('gpt-5.6-terra', True, None), ('gemini-3.5-flash', True, None),
    ('kimi-k2.6', False, None), ('kimi-k2.6', True, {}),
    ('kimi-k2.6', True, {'total_tokens': 10}),
])
def test_scope_and_usage_guard(tmp_path, model, enabled, usage):
    calls = []
    def provider(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        raise Capacity()
    with pytest.raises(Capacity):
        call(provider, {'model': model, 'messages': messages()}, tmp_path,
             enabled=enabled, usage=usage)
    assert len(calls) == 1


def test_single_frame_remains_terminal_and_receipt_survives_failure(tmp_path):
    calls = []
    def provider(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        raise Capacity()
    with pytest.raises(Capacity):
        call(provider, {'model': 'kimi-k2.6', 'messages': messages()}, tmp_path)
    assert [count(k) for k in calls] == [3, 2, 1]
    receipt = json.loads(next(tmp_path.glob('*.json')).read_text())
    assert receipt['status'] == 'failed_after_backoff'
    assert receipt['sent_image_count'] == 1


def test_native_sdk_boundary_records_actual_sent_messages_and_one_paid_response(tmp_path):
    from warptest.agent_s_worker import ChatCompletionsBackend, _NativeUsageRecordingCompletions
    from warptest.model_request import model_request_profiles_environment
    from warptest.gui_agent import _native_info_fields
    from types import SimpleNamespace

    calls = []
    class Provider:
        def create(self, **kwargs):
            calls.append(copy.deepcopy(kwargs))
            if count(kwargs) > 2:
                raise Capacity()
            return {'id': 'fixture', 'model': 'kimi-k2.6',
                    'choices': [{'message': {'content': 'OK'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 1, 'total_tokens': 11}}
    events = []
    backend = ChatCompletionsBackend(model='kimi-k2.6', base_url='https://provider.invalid/v1',
        api_key_env='TEST_KEY', default_headers={}, temperature=.2, timeout_s=30,
        max_retries=0, usage_sink=events, client=object())
    proxy = _NativeUsageRecordingCompletions(Provider(), backend=backend, kind='agent_s_worker')
    original = messages()
    with patch.dict('os.environ', {POLICY_ENV: REVISION, AUDIT_ENV: str(tmp_path)}), \
            model_request_profiles_environment({'kimi-k2.6': 'thinking_disabled'}):
        proxy.create(model='kimi-k2.6', messages=original)
    assert [count(k) for k in calls] == [3, 2]
    assert all(k['extra_body']['thinking']['type'] == 'disabled' for k in calls)
    assert len(events) == 1 and events[0]['total_tokens'] == 11
    assert len(events[0]['image_history_backoff']) == 1
    assert count({'messages': events[0]['messages']}) == 2
    fields = _native_info_fields(SimpleNamespace(info={}, usage_events=events))
    assert fields['image_history_backoff'][0]['original_image_count'] == 3
    assert count({'messages': original}) == 3


def test_opt_in_reaches_only_the_selected_worker_environment(tmp_path):
    from warptest.agent_s_bridge import AgentSBridgeConfig, _worker_environment
    config = AgentSBridgeConfig(model='kimi-k2.6', base_url='https://provider.invalid/v1',
        api_key_env='TEST_KEY', grounder_model='kimi-k2.6',
        grounder_base_url='https://provider.invalid/v1', grounder_api_key_env='TEST_KEY')
    with patch.dict('os.environ', {POLICY_ENV: REVISION, AUDIT_ENV: str(tmp_path), 'UNRELATED_SECRET': 'excluded'}):
        env = _worker_environment(config, Path('/venv/bin/python'))
    assert env[POLICY_ENV] == REVISION and env[AUDIT_ENV] == str(tmp_path)
    assert 'UNRELATED_SECRET' not in env
