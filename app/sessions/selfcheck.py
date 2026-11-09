"""会前自检框架：可插拔检查项，asyncio.gather 并发，单项超时 + 总超时。

思路参考 lesue1 preflight.py 的三级自检（缺什么、怎么补），实现自研（并发、超时、可注入依赖），未移植其代码。
检查项返回 {name, status: PASS|WARN|FAIL, detail}。
"""
import asyncio
import inspect

PASS, WARN, FAIL = 'PASS', 'WARN', 'FAIL'
ITEM_TIMEOUT = 3.0
TOTAL_TIMEOUT = 10.0
DISK_WARN = 1 << 30            # <1GB WARN
DISK_FAIL = 200 * (1 << 20)    # <200MB FAIL

# 失败项 -> 提示板顶部常驻降级标识
DEGRADED_TEXT = {
    'audio_sources': '音源无声',
    'asr_connect': '实时转写不可用',
    'llm_connect': 'AI 生成不可用',
    'voice_enrolled': '声音未登记',
    'disk_space': '磁盘空间不足',
}


class Registry:
    def __init__(self):
        self._items = []

    def register(self, name, fn, modes=('online', 'offline')):
        self._items.append((name, fn, tuple(modes)))

    def for_mode(self, mode):
        return [(n, f) for n, f, m in self._items if mode in m]


def _result(name, status, detail=''):
    return dict(name=name, status=status, detail=detail)


async def _call(fn):
    if inspect.iscoroutinefunction(fn):
        return await fn()
    return await asyncio.to_thread(fn)


async def _one(name, fn, timeout):
    try:
        out = await asyncio.wait_for(_call(fn), timeout)
    except asyncio.TimeoutError:
        return _result(name, FAIL, f'超过 {timeout:g} 秒未响应')
    except Exception as exc:  # 只记类型名，避免异常文本带出凭证
        return _result(name, FAIL, f'检查出错：{type(exc).__name__}')
    status, detail = out
    return _result(name, status, detail)


async def run_selfcheck(checks, item_timeout=ITEM_TIMEOUT, total_timeout=TOTAL_TIMEOUT):
    """checks: [(name, fn)]；fn 返回 (status, detail)，同步或异步均可。"""
    tasks = [asyncio.ensure_future(_one(n, f, item_timeout)) for n, f in checks]
    if tasks:
        await asyncio.wait(tasks, timeout=total_timeout)
    items = []
    for (name, _), t in zip(checks, tasks):
        if t.done():
            items.append(t.result())
        else:
            t.cancel()
            items.append(_result(name, FAIL, f'自检总超时（{total_timeout:g} 秒）'))
    return summarize(items)


def summarize(items):
    failed = [i['name'] for i in items if i['status'] == FAIL]
    overall = FAIL if failed else WARN if any(i['status'] == WARN for i in items) else PASS
    labels = [DEGRADED_TEXT.get(n, '部分自检项失败') for n in failed]
    return dict(items=items, overall=overall, has_fail=bool(failed), can_degrade_start=bool(failed),
                degraded_banner='；'.join(dict.fromkeys(labels)) if failed else None)


def _ping(label, fn, fail_status=FAIL):
    """fn() -> bool | (bool, detail) | (status, detail)；缺依赖即未配置。"""
    def check():
        if fn is None:
            return fail_status, f'{label}未配置'
        out = fn()
        if isinstance(out, tuple):
            status, detail = out
            if status in (PASS, WARN, FAIL):
                return status, detail or '连通'
            return (PASS, detail or '连通') if status else (fail_status, detail or f'{label}不可用')
        return (PASS, '连通') if out else (fail_status, f'{label}不可用')
    return check


def default_registry(deps, approved_count):
    """deps: 可注入依赖字典（缺省项即未配置）：
    audio_sources()->bool|(bool,detail)、asr_ping、llm_ping、search_ping、voice_enrolled、disk_free()->字节数。
    approved_count: 本场所选主库启用条数合计 -> int。"""
    deps = deps or {}
    reg = Registry()

    def disk():
        fn = deps.get('disk_free')
        if fn is None:
            return WARN, '磁盘空间未检测'
        free = fn()
        mb = free // (1 << 20)
        if free < DISK_FAIL:
            return FAIL, f'剩余 {mb} MB，低于 200 MB'
        if free < DISK_WARN:
            return WARN, f'剩余 {mb} MB，低于 1 GB'
        return PASS, f'剩余 {mb} MB'

    def library():
        n = approved_count()
        return (PASS, f'启用 {n} 条') if n > 0 else (WARN, '主库启用 0 条，仅次优库/联网可用')

    reg.register('audio_sources', _ping('音源', deps.get('audio_sources')))
    reg.register('asr_connect', _ping('ASR', deps.get('asr_ping')))
    reg.register('llm_connect', _ping('DeepSeek', deps.get('llm_ping')))
    reg.register('search_connect', _ping('搜索', deps.get('search_ping'), WARN))
    reg.register('voice_enrolled', _ping('声音登记', deps.get('voice_enrolled')), modes=('offline',))
    reg.register('disk_space', disk)
    reg.register('library_approved_count', library)
    return reg
