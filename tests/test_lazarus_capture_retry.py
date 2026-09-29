from types import SimpleNamespace
import pytest
from warptest.c1_lazarus_selection import p2p_only_engine_capture as engine


def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(engine.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(engine.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


def test_passive_retry_recovers_dropped_request_without_reusing_frame(tmp_path, monkeypatch):
    now = clock(monkeypatch)
    (tmp_path / 'old.png').write_bytes(b'old')
    calls = []
    def request():
        calls.append(now[0])
        if len(calls) == 2:
            (tmp_path / 'new.png').write_bytes(b'fresh')
    audit = {}
    result = engine._take_luanti_screenshot(process=SimpleNamespace(poll=lambda: None),
        screenshots=tmp_path, request_key=request, retry_interval_s=4.25, capture_audit=audit)
    assert result.name == 'new.png'
    assert len(calls) == 2 and calls[1] >= 4.25
    assert audit['requests'] == 2 and now[0] < 20


def test_retry_keeps_original_timeout_and_does_not_accept_old_frame(tmp_path, monkeypatch):
    now = clock(monkeypatch)
    (tmp_path / 'old.png').write_bytes(b'old')
    calls = []
    with pytest.raises(RuntimeError, match='timed out'):
        engine._take_luanti_screenshot(process=SimpleNamespace(poll=lambda: None),
            screenshots=tmp_path, request_key=lambda: calls.append(now[0]), retry_interval_s=4.25)
    assert 20 <= now[0] < 20.2
    assert len(calls) == 5


def test_historical_default_still_requests_once(tmp_path, monkeypatch):
    clock(monkeypatch)
    calls = []
    monkeypatch.setattr(engine, '_post_key', lambda pid,key: calls.append((pid,key)))
    with pytest.raises(RuntimeError, match='timed out'):
        engine._take_luanti_screenshot(process=SimpleNamespace(poll=lambda: None,pid=42),screenshots=tmp_path)
    assert calls == [(42,111)]
