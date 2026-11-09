"""故障注入开关（M13-3，Mock）。供 L8 测试对 ASR/LLM/搜索/声纹/磁盘/WS/外网逐项注入（source=某音源停止送帧，由调用方据 is_on 停喂）。

用法：用 wrap_probe / asr_health / voice_ok / disk_free 包装真实依赖，注入后即按故障表现；
模式 'down' 立即抛连接错误，'hang' 永不返回（用于验证 2s 探测超时）。
"""
import asyncio

FAULTS = ('network', 'ws', 'asr', 'asr_stall', 'llm', 'search', 'voice', 'disk', 'source')
_EXTERNAL = {'llm': ('network', 'llm'), 'search': ('network', 'search')}


class FaultInjector:
    def __init__(self):
        self._on = {}  # fault -> mode

    def inject(self, fault, mode='down'):
        if fault not in FAULTS:
            raise ValueError(f'未知故障: {fault}')
        self._on[fault] = mode

    def clear(self, fault=None):
        if fault is None:
            self._on.clear()
        else:
            self._on.pop(fault, None)

    def is_on(self, *faults):
        return any(f in self._on for f in faults)

    def wrap_probe(self, name, fn):
        """包装外部服务探测（name: llm/search）；network 同样使其失败。"""
        async def wrapped():
            for f in _EXTERNAL[name]:
                mode = self._on.get(f)
                if mode == 'hang':
                    await asyncio.Event().wait()
                if mode:
                    raise ConnectionError('injected')
            return await fn()
        return wrapped

    def asr_health(self, real=None):
        """pipeline.health() 形态；asr / ws / network 注入时为 down。"""
        for f in ('ws', 'network', 'asr'):
            if f in self._on:
                return dict(state='down', reason='injected:' + f)
        return real() if real else dict(state='ok', reason='')

    def drop_asr_text(self):
        """asr_stall 注入时调用方应丢弃 partial/final（模拟识别停滞）。"""
        return 'asr_stall' in self._on

    def voice_ok(self):
        return 'voice' not in self._on

    def disk_free(self, real_free):
        """disk 注入时返回模式给定的剩余字节数（inject('disk', 150<<20)），默认 0。"""
        if 'disk' not in self._on:
            return real_free
        mode = self._on['disk']
        return mode if isinstance(mode, int) else 0
