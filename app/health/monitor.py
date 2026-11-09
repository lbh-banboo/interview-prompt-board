"""健康监测器（M13-1）。

- 外部服务（llm/search）：10s 周期探测，每个探测是独立 asyncio 任务，超时 2s，异常不外溢。
- ASR 与音源：事件驱动（on_asr_health / on_source_frame / on_vad_speech / on_asr_text），
  check(now) 只做截止时间判定，由调用方高频（建议 ≤250ms）驱动，保证故障提示 ≤3s。
- ASR 停滞 = 本地 VAD 判定有语音且 3s 内无任何 partial/final；静音不触发。
- 磁盘：随周期探测检查，<1GB WARN，<200MB 告警。
时钟与 sleep 全部可注入。

错误分类部分参考 MeetingCopilot 的 14 类错误码思路。
Ported from JWM0203/MeetingCopilot@337973a0a2e34424fb541a158d89533b5c4514cd:electron/providerErrors.ts, Apache-2.0 © 2026 JWM0203.
Modified: TypeScript -> Python；只保留错误码与中文文案及按异常类型/HTTP 状态的映射，去掉英文文案、正则规则表和脱敏；
分类结果不含原始异常文本，避免凭证外泄。
"""
import asyncio
import shutil
import socket
import ssl

from .capabilities import DEGRADED, DOWN

PROBE_INTERVAL_S = 10.0
PROBE_TIMEOUT_S = 2.0
ASR_STALL_S = 3.0
SOURCE_GAP_S = 2.0
TICK_S = 0.25
DISK_WARN_BYTES = 1 << 30
DISK_CRIT_BYTES = 200 << 20

ERROR_MESSAGES = {
    'OK': '连接成功',
    'INVALID_KEY': 'API Key 无效、已删除或粘贴不完整',
    'PERMISSION_DENIED': '当前 Key 没有访问该模型或业务空间的权限',
    'INSUFFICIENT_BALANCE': '账号余额、免费额度或套餐额度不足',
    'RATE_LIMITED': '请求过于频繁或达到当前额度限制',
    'MODEL_NOT_FOUND': '当前模型不可用或模型名已变化',
    'REGION_MISMATCH': 'Key、地区与服务地址不匹配',
    'NETWORK_UNREACHABLE': '无法连接服务商，请检查网络',
    'DNS_ERROR': '域名解析失败',
    'TLS_ERROR': '安全连接失败',
    'PROXY_ERROR': '代理连接失败',
    'TIMEOUT': '服务连接超时',
    'PROVIDER_ERROR': '服务商暂时异常',
    'UNKNOWN_ERROR': '未知错误',
}
_STATUS_CODES = {401: 'INVALID_KEY', 403: 'PERMISSION_DENIED', 402: 'INSUFFICIENT_BALANCE',
                 429: 'RATE_LIMITED', 404: 'MODEL_NOT_FOUND'}


def classify_error(exc):
    """异常 -> 错误码。只看类型与 status，不读异常文本。"""
    status = getattr(exc, 'status', None)
    if isinstance(status, int):
        if status in _STATUS_CODES:
            return _STATUS_CODES[status]
        if status >= 500:
            return 'PROVIDER_ERROR'
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return 'TIMEOUT'
    if isinstance(exc, socket.gaierror):
        return 'DNS_ERROR'
    if isinstance(exc, ssl.SSLError):
        return 'TLS_ERROR'
    if isinstance(exc, OSError):
        return 'NETWORK_UNREACHABLE'
    return 'UNKNOWN_ERROR'


class HealthMonitor:
    def __init__(self, board, clock, probes=None, disk_probe=None, disk_path='.', sleep=asyncio.sleep,
                 probe_interval=PROBE_INTERVAL_S, probe_timeout=PROBE_TIMEOUT_S, asr_stall_s=ASR_STALL_S,
                 source_gap_s=SOURCE_GAP_S, tick_s=TICK_S):
        self.board = board
        self.clock = clock
        self.probes = dict(probes or {})  # 'llm' / 'search' -> async fn()，抛异常或返回 False 视为不可用
        self.disk_probe = disk_probe or (lambda: shutil.disk_usage(disk_path).free)
        self.sleep = sleep
        self.probe_interval = probe_interval
        self.probe_timeout = probe_timeout
        self.asr_stall_s = asr_stall_s
        self.source_gap_s = source_gap_s
        self.tick_s = tick_s
        self._last_frame = {}  # source -> 最近帧时刻
        self._speech_pending = {}  # source -> 尚未得到任何 partial/final 的语音起点
        self._tasks = []

    # ---- 事件驱动入口 ----
    def on_asr_health(self, state):
        """state 为 pipeline.health() 形态：{'state': ok|reconnecting|down, 'reason': str}。"""
        if state.get('state', 'ok') == 'ok':
            self.board.clear_fault('asr_disconnected')
        else:
            self.board.raise_fault('asr_disconnected', reason=state.get('reason', ''))

    def on_source_frame(self, source, t):
        self._last_frame[source] = t
        self.board.clear_fault('source_lost', source)

    def on_vad_speech(self, source, t):
        self._speech_pending.setdefault(source, t)

    def on_asr_text(self, source, t):
        self._speech_pending.pop(source, None)
        self.board.clear_fault('asr_stalled', source)

    def on_source_closed(self, source):
        """会话正常结束/主动停止某音源：不再按“无帧”判故障。"""
        self._last_frame.pop(source, None)
        self._speech_pending.pop(source, None)
        self.board.clear_fault('source_lost', source)
        self.board.clear_fault('asr_stalled', source)

    def on_uncertain(self, flag):
        if flag:
            self.board.raise_fault('uncertain')
        else:
            self.board.clear_fault('uncertain')

    def on_voice_health(self, ok, reason=''):
        if ok:
            self.board.clear_fault('voice_unavailable')
        else:
            self.board.raise_fault('voice_unavailable', reason=reason)

    # ---- 截止时间判定 ----
    def check(self, now=None):
        now = self.clock() if now is None else now
        for source, last in self._last_frame.items():
            if now - last >= self.source_gap_s:
                self.board.raise_fault('source_lost', source, reason='无帧')
        for source, since in self._speech_pending.items():
            if now - since >= self.asr_stall_s:
                self.board.raise_fault('asr_stalled', source, reason='有语音但无识别结果')

    def check_disk(self):
        try:
            free = self.disk_probe()
        except Exception:
            return
        if free < DISK_CRIT_BYTES:
            self.board.raise_fault('disk_low', reason='剩余不足 200MB', state=DOWN)
        elif free < DISK_WARN_BYTES:
            self.board.raise_fault('disk_low', reason='剩余不足 1GB', state=DEGRADED)
        else:
            self.board.clear_fault('disk_low')

    # ---- 外部服务探测 ----
    async def probe_once(self, name):
        fault = f'{name}_down'
        try:
            res = await asyncio.wait_for(self.probes[name](), self.probe_timeout)
            err = None if res is not False else 'UNKNOWN_ERROR'
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # 探测异常不外溢
            err = classify_error(e)
        if err is None:
            self.board.clear_fault(fault)
        else:
            self.board.raise_fault(fault, reason=ERROR_MESSAGES[err])

    async def _probe_loop(self, name):
        while True:
            await self.probe_once(name)
            await self.sleep(self.probe_interval)

    async def _disk_loop(self):
        while True:
            self.check_disk()
            await self.sleep(self.probe_interval)

    async def _tick_loop(self):
        while True:
            self.check()
            await self.sleep(self.tick_s)

    def start(self):
        """启动独立任务：每个探测一个、磁盘一个、截止时间 tick 一个。需在运行中的事件循环内调用。"""
        if self._tasks:
            return
        self._tasks = [asyncio.create_task(self._probe_loop(n)) for n in self.probes]
        self._tasks += [asyncio.create_task(self._disk_loop()), asyncio.create_task(self._tick_loop())]

    async def stop(self):
        tasks, self._tasks = self._tasks, []
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
