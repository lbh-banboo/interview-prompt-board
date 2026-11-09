import asyncio
import time

from app.sessions.selfcheck import FAIL, PASS, WARN, Registry, default_registry, run_selfcheck, summarize

GB = 1 << 30
OK = lambda: True  # noqa: E731


def deps(**over):
    d = dict(audio_sources=OK, asr_ping=OK, llm_ping=OK, search_ping=OK, voice_enrolled=OK, disk_free=lambda: 5 * GB)
    d.update(over)
    return d


def run(d, mode='offline', count=3, **kw):
    reg = default_registry(d, lambda: count)
    return asyncio.run(run_selfcheck(reg.for_mode(mode), **kw))


def status(report):
    return {i['name']: i['status'] for i in report['items']}


def test_all_pass_offline_has_seven_items():
    r = run(deps())
    assert r['overall'] == PASS and not r['has_fail'] and r['degraded_banner'] is None
    assert len(r['items']) == 7 and set(status(r).values()) == {PASS}


def test_online_skips_voice_enroll():
    assert 'voice_enrolled' not in status(run(deps(), mode='online'))


def test_asr_fail_gives_degraded_banner_T3():
    r = run(deps(asr_ping=None))
    assert status(r)['asr_connect'] == FAIL
    assert r['has_fail'] and r['can_degrade_start'] and r['degraded_banner'] == '实时转写不可用'
    assert '未配置' in [i for i in r['items'] if i['name'] == 'asr_connect'][0]['detail']


def test_ping_tuple_detail_and_false():
    r = run(deps(llm_ping=lambda: (False, '密钥无效'), search_ping=lambda: (True, '200ms')))
    s = {i['name']: i for i in r['items']}
    assert s['llm_connect']['status'] == FAIL and s['llm_connect']['detail'] == '密钥无效'
    assert s['search_connect']['detail'] == '200ms'
    assert 'AI 生成不可用' in r['degraded_banner']


def test_ping_accepts_explicit_status_tuple():
    r = run(deps(audio_sources=lambda: (WARN, '音源需在采集页授权后检测')))
    item = [i for i in r['items'] if i['name'] == 'audio_sources'][0]
    assert item['status'] == WARN and item['detail'] == '音源需在采集页授权后检测'
    assert not r['has_fail']


def test_search_fail_is_only_warn():
    r = run(deps(search_ping=lambda: False))
    assert status(r)['search_connect'] == WARN and r['overall'] == WARN and not r['has_fail']


def test_disk_thresholds():
    assert status(run(deps(disk_free=lambda: GB - 1)))['disk_space'] == WARN
    assert status(run(deps(disk_free=lambda: 200 * (1 << 20) - 1)))['disk_space'] == FAIL
    assert status(run(deps(disk_free=lambda: GB)))['disk_space'] == PASS
    assert status(run({}))['disk_space'] == WARN


def test_zero_approved_warn():
    r = run(deps(), count=0)
    item = [i for i in r['items'] if i['name'] == 'library_approved_count'][0]
    assert item['status'] == WARN and '仅次优库/联网可用' in item['detail']


def test_exception_does_not_leak_message():
    def boom():
        raise RuntimeError('key=sk-secret')
    r = run(deps(asr_ping=boom))
    d = [i for i in r['items'] if i['name'] == 'asr_connect'][0]
    assert d['status'] == FAIL and 'sk-secret' not in d['detail'] and 'RuntimeError' in d['detail']


def test_concurrent_and_item_timeout():
    async def slow():
        await asyncio.sleep(5)
        return PASS, ''

    async def quick():
        await asyncio.sleep(0.2)
        return PASS, 'ok'

    reg = Registry()
    reg.register('slow', slow)
    for n in range(5):
        reg.register(f'q{n}', quick)
    t = time.perf_counter()
    r = asyncio.run(run_selfcheck(reg.for_mode('online'), item_timeout=0.5, total_timeout=10))
    assert time.perf_counter() - t < 1.0  # 并发：6 项不是串行相加
    s = status(r)
    assert s['slow'] == FAIL and all(s[f'q{n}'] == PASS for n in range(5))


def test_total_timeout_marks_unfinished():
    async def slow():
        await asyncio.sleep(5)
        return PASS, ''
    reg = Registry()
    reg.register('slow', slow)
    r = asyncio.run(run_selfcheck(reg.for_mode('online'), item_timeout=9, total_timeout=0.2))
    assert r['items'][0]['status'] == FAIL and '总超时' in r['items'][0]['detail']


def test_sync_slow_dependency_times_out():
    reg = default_registry(deps(asr_ping=lambda: time.sleep(0.6) or True), lambda: 1)
    r = asyncio.run(run_selfcheck(reg.for_mode('online'), item_timeout=0.1))
    assert status(r)['asr_connect'] == FAIL


def test_empty_and_unknown_fail_label():
    assert summarize([])['overall'] == PASS
    r = summarize([dict(name='custom', status=FAIL, detail='')])
    assert r['degraded_banner'] == '部分自检项失败'
    assert asyncio.run(run_selfcheck([]))['items'] == []
