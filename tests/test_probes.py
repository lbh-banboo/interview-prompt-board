"""生产默认自检探测：DeepSeek /models、ASR 后端选择、音源与服务端无法探测项、声纹登记。"""
import asyncio
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from app import wiring
from app.audio.enroll import VoiceProfileStore
from app.core import Store
from app.credentials import CredentialStore, MemoryBackend
from app.sessions import probes
from app.sessions.selfcheck import FAIL, PASS, WARN, default_registry, run_selfcheck

KEY = 'sk-test-secret-1234567890'
BASE = 'https://api.deepseek.com'
GB = 1 << 30


def creds(**values):
    return CredentialStore([MemoryBackend(values)])


def cfg(**outbound):
    return dict(model={'base_url': BASE}, outbound={'llm_snippets': True, 'asr_audio': True, **outbound})


def llm(transport=None, c=None, config=None, timeout=2.0):
    return probes.llm_ping(c or creds(DEEPSEEK_API_KEY=KEY), config or cfg(), transport=transport, timeout=timeout)


def ok_handler(request):
    assert request.headers['authorization'] == f'Bearer {KEY}'
    assert request.url.path == '/models'
    return httpx.Response(200, json={'data': []})


def test_llm_pass_without_leaking_key():
    status, detail = llm(httpx.MockTransport(ok_handler))()
    assert status == PASS and KEY not in detail


def test_llm_no_key_is_fail_not_network(tmp_path, monkeypatch):
    called = []
    transport = httpx.MockTransport(lambda r: called.append(r) or httpx.Response(200))
    status, detail = llm(transport, c=creds())()
    assert status == FAIL and '未配置 DeepSeek 凭证' in detail
    assert called == []


def test_llm_invalid_key_is_fail_and_redacted():
    def handler(request):
        return httpx.Response(401, json={'error': f'invalid key {KEY}'})
    status, detail = llm(httpx.MockTransport(handler))()
    assert status == FAIL and 'Key 无效' in detail and KEY not in detail


def test_llm_timeout_and_network_error_are_short_fails():
    def timeout_handler(request):
        raise httpx.ConnectTimeout('secret detail ' + KEY)
    status, detail = llm(httpx.MockTransport(timeout_handler))()
    assert status == FAIL and '超时' in detail and KEY not in detail

    def down_handler(request):
        raise httpx.ConnectError('secret detail ' + KEY)
    status, detail = llm(httpx.MockTransport(down_handler))()
    assert status == FAIL and '无法连接' in detail and KEY not in detail


def test_llm_outbound_off_sends_nothing():
    def handler(request):
        raise AssertionError('外发关闭时不得发请求')
    status, detail = llm(httpx.MockTransport(handler), config=cfg(llm_snippets=False))()
    assert status == WARN and detail == '外发已关闭'


def test_llm_base_url_from_config():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200)
    status, _ = llm(httpx.MockTransport(handler),
                    config=dict(model={'base_url': 'https://example.invalid/v1'}, outbound={'llm_snippets': True}))()
    assert status == PASS and seen == ['https://example.invalid/v1/models']


def _sherpa(tmp_path, names=('encoder.int8.onnx', 'decoder.int8.onnx', 'tokens.txt')):
    for n in names:
        (tmp_path / n).write_bytes(b'x')
    return tmp_path


def test_asr_sherpa_pass_when_all_files_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, 'SHERPA_DIR', _sherpa(tmp_path))
    status, detail = probes.asr_ping(creds(), cfg())()
    assert status == PASS and 'sherpa' in detail


def test_asr_missing_any_sherpa_file_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, 'SHERPA_DIR', _sherpa(tmp_path, ('encoder.int8.onnx', 'tokens.txt')))
    status, detail = probes.asr_ping(creds(), cfg())()
    assert status == FAIL and '本地模型缺失' in detail


def test_asr_cloud_key_is_warn_without_probe():
    status, detail = probes.asr_ping(creds(DASHSCOPE_API_KEY=KEY), cfg())()
    assert status == WARN and '未做连通探测' in detail and KEY not in detail


def test_probe_detail_reports_source_category_only():
    from app.credentials import EnvBackend
    env_creds = CredentialStore([EnvBackend({'DASHSCOPE_API_KEY': KEY, 'DEEPSEEK_API_KEY': KEY})])
    status, detail = probes.asr_ping(env_creds, cfg())()
    assert status == WARN and '环境变量' in detail and KEY not in detail
    status, detail = probes.llm_ping(env_creds, cfg(), transport=httpx.MockTransport(ok_handler))()
    assert status == PASS and '环境变量' in detail and KEY not in detail


def test_asr_cloud_off_falls_back_to_local(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, 'SHERPA_DIR', _sherpa(tmp_path))
    status, _ = probes.asr_ping(creds(DASHSCOPE_API_KEY=KEY), cfg(asr_audio=False))()
    assert status == PASS


def test_audio_and_search_are_warn_not_fail():
    status, detail = probes.audio_ping()
    assert status == WARN and '授权' in detail
    status, detail = probes.search_ping()()
    assert status == WARN and '搜索未配置' in detail


def test_search_ping_no_key_is_warn_without_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError('自检不得发真实搜索请求')
    monkeypatch.setattr(httpx, 'AsyncClient', boom)
    status, detail = probes.search_ping(creds(), cfg())()
    assert status == WARN and detail == '搜索未配置，联网补充不可用'


def test_search_ping_configured_is_warn_without_probe_and_reports_source_only():
    from app.credentials import EnvBackend
    creds_env = CredentialStore([EnvBackend({'DEEPSEEK_API_KEY': KEY})])
    status, detail = probes.search_ping(creds_env, cfg())()
    assert status == WARN and '未做连通探测' in detail and '环境变量' in detail and KEY not in detail
    status, detail = probes.search_ping(creds(SEARCH_API_KEY=KEY, DEEPSEEK_API_KEY='sk-other-0000000000'), cfg())()
    assert status == WARN and '未做连通探测' in detail and 'SEARCH' not in detail


def test_search_ping_outbound_off_and_live_off():
    status, detail = probes.search_ping(creds(DEEPSEEK_API_KEY=KEY), cfg(web_questions=False))()
    assert status == WARN and detail == '外发已关闭'
    status, detail = probes.search_ping(creds(DEEPSEEK_API_KEY=KEY),
                                        {**cfg(), 'search': {'live': False}})()
    assert status == WARN and '未启用' in detail


def _store(tmp_path):
    store = Store(tmp_path)
    return store, VoiceProfileStore(store.connect, store.lock, store.root)


def test_voice_enrolled_reads_local_profile(tmp_path):
    store, vp = _store(tmp_path)
    status, detail = probes.voice_ping(store, tmp_path)()
    assert status == FAIL and '声音未登记' in detail
    vp.save('default', np.ones(8, np.float32), {})
    status, detail = probes.voice_ping(store, tmp_path)()
    assert status == PASS


def test_audio_warn_keeps_session_startable(tmp_path, monkeypatch):
    """ASR/DeepSeek 都通过、音源只是服务端不可探测时，整体不得变成降级开始。"""
    (tmp_path / 'sherpa').mkdir()
    monkeypatch.setattr(wiring, 'SHERPA_DIR', _sherpa(tmp_path / 'sherpa'))
    ctx = SimpleNamespace(config=dict(credential_store=creds(DEEPSEEK_API_KEY=KEY),
                                      llm_transport=httpx.MockTransport(ok_handler)),
                          store=Store(tmp_path), root=tmp_path)
    deps = probes.default_deps(ctx, timeout=1.0)
    deps['disk_free'] = lambda: 9 * GB
    reg = default_registry(deps, lambda: 2)
    report = asyncio.run(run_selfcheck(reg.for_mode('online')))
    items = {i['name']: i for i in report['items']}
    assert items['audio_sources']['status'] == WARN
    assert items['llm_connect']['status'] == PASS
    assert items['asr_connect']['status'] == PASS
    assert not report['has_fail'] and not report['can_degrade_start']
