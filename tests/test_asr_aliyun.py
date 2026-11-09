import asyncio
import json
import logging

import pytest

from app.asr import aliyun
from app.asr.aliyun import AliyunASRError, AliyunRealtimeASR, classify_error
from app.asr.base import ReconnectingASR

pytestmark = pytest.mark.asyncio

KEY = 'sk-TESTKEY-0123456789abcdef'
PCM = b'\0' * 640


class FakeWS:
    def __init__(self, auto_start=True, auto_finish=True):
        self.sent = []
        self.inbox = asyncio.Queue()
        self.closed = False
        self.auto_start, self.auto_finish = auto_start, auto_finish
        self.fail_send = None

    async def send(self, data):
        if self.fail_send:
            raise self.fail_send
        self.sent.append(data)
        if isinstance(data, str):
            action = json.loads(data)['header']['action']
            if action == 'run-task' and self.auto_start:
                self.feed(ev('task-started'))
            if action == 'finish-task' and self.auto_finish:
                self.feed(ev('task-finished'))

    async def recv(self):
        item = await self.inbox.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        self.closed = True

    def feed(self, obj):
        self.inbox.put_nowait(obj if isinstance(obj, (str, bytes, Exception)) else json.dumps(obj))

    def json_sent(self):
        return [json.loads(s) for s in self.sent if isinstance(s, str)]


def ev(event, **header):
    return {'header': {'task_id': 't', 'event': event, **header}, 'payload': {}}


def res(text, begin=0, end=None, final=False, words=None, **extra):
    s = {'begin_time': begin, 'end_time': end, 'text': text, 'sentence_end': final, 'sentence_id': 1,
         'words': words or []}
    s.update(extra)
    return {'header': {'event': 'result-generated'}, 'payload': {'output': {'sentence': s}}}


def make(ws=None, **kw):
    ws = ws or FakeWS()
    seen = {}

    async def factory(url, headers):
        seen['url'], seen['headers'] = url, headers
        return ws
    kw.setdefault('api_key', KEY)
    a = AliyunRealtimeASR(ws_factory=factory, **kw)
    return a, ws, seen


async def take(adapter, n, timeout=1):
    out = []

    async def run():
        async for e in adapter.events():
            out.append(e)
            if len(out) >= n:
                return
    await asyncio.wait_for(run(), timeout)
    return out


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


# ---- run-task / start ----
async def test_run_task_payload_and_headers():
    a, ws, seen = make(vocabulary_id='vocab-1', hotwords=['Kafka', 'Redis'])
    await a.start('s1')
    msg = ws.json_sent()[0]
    assert msg['header']['action'] == 'run-task' and msg['header']['streaming'] == 'duplex'
    assert msg['header']['task_id']
    p = msg['payload']
    assert (p['task_group'], p['task'], p['function'], p['model']) == ('audio', 'asr', 'recognition', 'fun-asr-realtime')
    assert p['parameters'] == {'format': 'pcm', 'sample_rate': 16000, 'max_sentence_silence': 200,
                               'heartbeat': True, 'vocabulary_id': 'vocab-1'}
    assert p['input']['context'][0]['content'][0] == {'type': 'input_text', 'text': 'Kafka、Redis'}
    assert seen['headers'] == {'Authorization': f'Bearer {KEY}'}
    assert seen['url'] == aliyun.DEFAULT_URL
    assert KEY not in json.dumps(msg)
    await a.close()


async def test_run_task_minimal_and_env_key(monkeypatch):
    monkeypatch.setenv('DASHSCOPE_API_KEY', KEY)
    a, ws, seen = make(api_key=None, max_sentence_silence=500)
    await a.start('s1')
    p = ws.json_sent()[0]['payload']
    assert p['input'] == {} and 'vocabulary_id' not in p['parameters']
    assert p['parameters']['max_sentence_silence'] == 500
    assert seen['headers']['Authorization'] == f'Bearer {KEY}'
    await a.close()


async def test_missing_key_is_auth_error(monkeypatch):
    monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
    a, _, _ = make(api_key=None)
    with pytest.raises(AliyunASRError) as ei:
        await a.start('s')
    assert ei.value.kind == 'auth'


async def test_start_waits_for_task_started():
    a, ws, _ = make(ws=FakeWS(auto_start=False))
    t = asyncio.create_task(a.start('s'))
    await asyncio.sleep(0.05)
    assert not t.done()
    ws.feed(ev('task-started'))
    await asyncio.wait_for(t, 1)
    await a.close()


async def test_start_timeout():
    a, ws, _ = make(ws=FakeWS(auto_start=False), start_timeout_s=0.05)
    with pytest.raises(AliyunASRError) as ei:
        await a.start('s')
    assert ei.value.kind == 'timeout' and ws.closed


async def test_start_task_failed_raises_classified():
    a, ws, _ = make(ws=FakeWS(auto_start=False))
    ws.feed(ev('task-failed', error_code='InvalidApiKey', error_message='bad key'))
    with pytest.raises(AliyunASRError) as ei:
        await a.start('s')
    assert ei.value.kind == 'auth' and ei.value.code == 'InvalidApiKey'


class _Resp:
    def __init__(self, code):
        self.status_code = code


class _HandshakeError(Exception):
    def __init__(self, code, text=''):
        super().__init__(text or f'server rejected WebSocket connection: HTTP {code}')
        self.response = _Resp(code)


@pytest.mark.parametrize('code,kind', [(401, 'auth'), (403, 'auth'), (429, 'rate_limit'), (500, 'other')])
async def test_handshake_status_classified(code, kind):
    async def factory(url, headers):
        raise _HandshakeError(code)
    a = AliyunRealtimeASR(api_key=KEY, ws_factory=factory)
    with pytest.raises(AliyunASRError) as ei:
        await a.start('s')
    assert ei.value.kind == kind


async def test_handshake_timeout_and_scrub():
    async def factory(url, headers):
        raise TimeoutError(f'timed out, header Bearer {KEY}')
    a = AliyunRealtimeASR(api_key=KEY, ws_factory=factory)
    with pytest.raises(AliyunASRError) as ei:
        await a.start('s')
    assert ei.value.kind == 'timeout' and KEY not in str(ei.value)


async def test_default_factory_uses_websockets(monkeypatch):
    import websockets.asyncio.client as c
    calls = {}

    async def fake_connect(url, **kw):
        calls['url'], calls['kw'] = url, kw
        return FakeWS()
    monkeypatch.setattr(c, 'connect', fake_connect)
    a = AliyunRealtimeASR(api_key=KEY)
    await a.start('s')
    assert calls['kw']['additional_headers'] == {'Authorization': f'Bearer {KEY}'}
    await a.close()


# ---- 映射 ----
async def test_partial_to_final_versions_restart():
    a, ws, _ = make(source='mic')
    await a.start('s')
    for m in (res('你', 0, None), res('你好', 0, None), res('你好吗', 0, 900, final=True),
              res('下', 1000, None), res('下一句', 1000, 1600, final=True)):
        ws.feed(m)
    evs = await take(a, 5)
    assert [(e.text, e.is_final, e.text_version) for e in evs] == [
        ('你', False, 1), ('你好', False, 2), ('你好吗', True, 3), ('下', False, 1), ('下一句', True, 2)]
    assert all(e.source == 'mic' and e.t_received_ns > 0 for e in evs)
    assert evs[2].audio_covered_until_ms == 900 and evs[2].t_audio_end_ms == 900
    await a.close()


async def test_sentence_start_constant_when_begin_time_drifts():
    """真实服务中 begin_time 会在 0 / 360 / 400 间漂移；t_audio_start_ms 必须整句恒定。"""
    a, ws, _ = make()
    await a.start('s')
    for m in (res('', 0, None), res('请', 0, None), res('请问', 360, None), res('请问你', 400, None),
              res('请问你对', 360, 3680, final=True), res('第二句', 5000, None), res('第二句话', 5040, 6000, final=True)):
        ws.feed(m)
    evs = await take(a, 6)
    assert {e.t_audio_start_ms for e in evs[:4]} == {0}
    assert {e.t_audio_start_ms for e in evs[4:]} == {5000}
    await a.close()


async def test_sentence_start_inferred_when_begin_missing_then_kept():
    a, ws, _ = make()
    await a.start('s')
    await a.push(PCM, 1000)           # 基准 1000，已推 20ms
    await a.push(PCM, 1020)
    ws.feed(res('你', None, None))
    ws.feed(res('你好', 300, 700, final=True))
    evs = await take(a, 2)
    assert evs[0].t_audio_start_ms == evs[1].t_audio_start_ms == 1000 + 40
    await a.close()


async def test_time_offset_uses_first_push_t_ms():
    a, ws, _ = make()
    await a.start('s')
    await a.push(PCM, 2000)
    ws.feed(res('你好', 100, 500, final=True))
    (e,) = await take(a, 1)
    assert (e.t_audio_start_ms, e.audio_covered_until_ms, e.t_audio_end_ms) == (2100, 2500, 2500)
    await a.close()


async def test_covered_prefers_last_word_end_and_is_monotonic():
    a, ws, _ = make()
    await a.start('s')
    ws.feed(res('请问', 0, None, words=[{'begin_time': 0, 'end_time': 300}, {'begin_time': 300, 'end_time': 720}]))
    ws.feed(res('请问你', 0, 500))  # 回退的 end_time 不得让覆盖值倒退
    e1, e2 = await take(a, 2)
    assert e1.audio_covered_until_ms == 720 and e2.audio_covered_until_ms == 720
    await a.close()


async def test_covered_fallback_is_conservative_not_pushed_total():
    a, ws, _ = make()
    await a.start('s')
    for i in range(50):
        await a.push(PCM, i * 20)     # 已推 1000ms
    ws.feed(res('你好', 200, None))
    (e,) = await take(a, 1)
    assert e.audio_covered_until_ms == 200 < 1000
    await a.close()


async def test_heartbeat_and_empty_results_skipped_and_empty_final_resets():
    a, ws, _ = make()
    await a.start('s')
    ws.feed(res('', 0, None, heartbeat=True))
    ws.feed(res('嗯', 0, 10, heartbeat=True))
    ws.feed(res('', 0, None))
    ws.feed(res('你好', 100, None))
    ws.feed(res('', 100, 200, final=True))     # 空 final：句子被丢弃，版本复位
    ws.feed(res('再见', 900, None))
    e1, e2 = await take(a, 2)
    assert (e1.text, e1.text_version) == ('你好', 1) and (e2.text, e2.text_version) == ('再见', 1)
    assert e2.t_audio_start_ms == 900
    await a.close()


async def test_ignores_binary_garbage_and_unknown_events():
    a, ws, _ = make()
    await a.start('s')
    ws.feed(b'\x00\x01')
    ws.feed('not json')
    ws.feed(ev('something-else'))
    ws.feed(res('好', 0, 100, final=True))
    (e,) = await take(a, 1)
    assert e.text == '好'
    await a.close()


# ---- 推流 / 断连 / close ----
async def test_push_forwards_each_frame_unbuffered():
    a, ws, _ = make()
    await a.start('s')
    for i in range(3):
        await a.push(PCM, i * 20)
    assert [s for s in ws.sent if isinstance(s, bytes)] == [PCM] * 3
    await a.close()


async def test_disconnect_raises_from_events_and_push():
    from websockets.exceptions import ConnectionClosedError
    a, ws, _ = make()
    await a.start('s')
    ws.feed(res('你', 0, None))
    ws.feed(ConnectionClosedError(None, None))
    got = []
    with pytest.raises(AliyunASRError):
        async for e in a.events():
            got.append(e)
    assert [e.text for e in got] == ['你']
    assert a.health()['state'] == 'down'
    with pytest.raises(AliyunASRError):
        await a.push(PCM, 0)
    await a.close()


async def test_push_send_failure_raises():
    a, ws, _ = make()
    await a.start('s')
    ws.fail_send = RuntimeError(f'broken pipe {KEY}')
    with pytest.raises(AliyunASRError) as ei:
        await a.push(PCM, 0)
    assert KEY not in str(ei.value)
    assert a.health()['state'] == 'down'
    with pytest.raises(AliyunASRError):
        async for _ in a.events():
            pass
    await a.close()


async def test_unexpected_task_finished_is_error():
    a, ws, _ = make()
    await a.start('s')
    ws.feed(ev('task-finished'))
    with pytest.raises(AliyunASRError):
        async for _ in a.events():
            pass


async def test_close_sends_finish_task_waits_and_ends_events():
    a, ws, _ = make()
    await a.start('s')
    ws.feed(res('尾巴', 0, 500, final=True))
    await settle()
    await a.close()
    fin = ws.json_sent()[-1]
    assert fin['header']['action'] == 'finish-task' and fin['payload'] == {'input': {}}
    assert fin['header']['task_id'] == ws.json_sent()[0]['header']['task_id']
    assert ws.closed
    assert [e.text async for e in a.events()] == ['尾巴']   # close 后残留事件仍可读，随后结束
    assert a.health() == {'state': 'ok', 'reason': ''}
    with pytest.raises(AliyunASRError):
        await a.push(PCM, 0)
    await a.close()  # 幂等


async def test_close_delivers_results_arriving_during_finish():
    ws = FakeWS(auto_finish=False)
    a, _, _ = make(ws=ws)
    await a.start('s')
    t = asyncio.create_task(a.close())
    await asyncio.sleep(0.02)
    ws.feed(res('最后一句', 0, 800, final=True))
    ws.feed(ev('task-finished'))
    await asyncio.wait_for(t, 1)
    assert [e.text async for e in a.events()] == ['最后一句']


async def test_close_timeout_forces_close():
    ws = FakeWS(auto_finish=False)
    a, _, _ = make(ws=ws, finish_timeout_s=0.05)
    await a.start('s')
    await asyncio.wait_for(a.close(), 1)
    assert ws.closed
    assert [e async for e in a.events()] == []


async def test_close_without_start():
    a = AliyunRealtimeASR(api_key=KEY)
    await a.close()
    assert [e async for e in a.events()] == []


async def test_events_ends_when_closed_while_consumer_waiting():
    a, ws, _ = make()
    await a.start('s')
    t = asyncio.create_task(take(a, 99))
    await asyncio.sleep(0.02)
    await a.close()
    assert await asyncio.wait_for(t, 1) == []


# ---- 错误归一化 / 脱敏 ----
@pytest.mark.parametrize('code,msg,kind', [
    ('InvalidApiKey', 'x', 'auth'), ('AccessDenied', 'x', 'auth'),
    ('Throttling.RateQuota', 'x', 'rate_limit'), ('Arrearage', 'x', 'rate_limit'),
    ('CLIENT_ERROR', 'request timeout after 23 seconds.', 'timeout'),
    ('InvalidParameter', 'bad', 'other'), ('', '', 'other')])
async def test_task_failed_classification_mid_stream(code, msg, kind):
    a, ws, _ = make()
    await a.start('s')
    ws.feed(ev('task-failed', error_code=code, error_message=msg))
    with pytest.raises(AliyunASRError) as ei:
        async for _ in a.events():
            pass
    assert ei.value.kind == kind
    assert a.health()['state'] == 'down'
    assert classify_error(code, msg) == kind
    await a.close()


async def test_error_is_connection_error_for_reconnecting_wrapper():
    assert issubclass(AliyunASRError, ConnectionError)


async def test_key_never_in_exceptions_logs_or_health(caplog):
    caplog.set_level(logging.DEBUG)
    a, ws, _ = make()
    await a.start('s')
    ws.feed(ev('task-failed', error_code='InvalidApiKey', error_message=f'key {KEY} is invalid'))
    with pytest.raises(AliyunASRError) as ei:
        async for _ in a.events():
            pass
    texts = [str(ei.value), repr(ei.value), a.health()['reason'], caplog.text, repr(a.health())]
    assert all(KEY not in t for t in texts)
    assert KEY not in json.dumps(ws.json_sent())
    await a.close()


async def test_key_not_in_disconnect_error():
    a, ws, _ = make()
    await a.start('s')
    ws.feed(ConnectionResetError(f'reset by peer, Authorization: Bearer {KEY}'))
    with pytest.raises(AliyunASRError) as ei:
        async for _ in a.events():
            pass
    assert KEY not in str(ei.value) and KEY not in a.health()['reason']
    await a.close()


# ---- 与 ReconnectingASR 对接 ----
async def test_reconnecting_asr_recovers_from_aliyun_disconnect():
    wss = [FakeWS(), FakeWS()]
    it = iter(wss)

    async def factory(url, headers):
        return next(it)

    async def nosleep(_):
        pass
    r = ReconnectingASR(lambda: AliyunRealtimeASR(api_key=KEY, ws_factory=factory), sleep=nosleep)
    await r.start('s')
    wss[0].feed(res('你好', 0, 300))
    wss[0].feed(ConnectionResetError('boom'))
    first = await take(r, 1)
    assert first[0].text == '你好'
    for _ in range(50):
        if r.health()['state'] == 'ok' and wss[1].sent:
            break
        await asyncio.sleep(0.01)
    wss[1].feed(res('新连接', 0, 200, final=True))
    (e,) = await take(r, 1)
    assert e.text == '新连接' and e.text_version > first[0].text_version
    await r.close()
