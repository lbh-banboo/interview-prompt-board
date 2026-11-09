"""阿里云百炼 fun-asr-realtime 流式 ASR 适配器（M04-2）。

协议（官方文档 fun-asr-realtime WebSocket 接入指南 / 客户端事件 / 服务端事件）：
  握手头 Authorization: Bearer <key>；run-task(JSON) -> task-started -> 二进制 PCM16 帧
  -> result-generated* -> finish-task -> task-finished；失败为 task-failed。
  注意：该协议没有 continue-task，音频直接以二进制帧发送；fun-asr-realtime 的参数里也没有
  semantic_punctuation_enabled（那是 paraformer 的参数），所以这里不发送。

时间轴：阿里返回的 begin_time/end_time 相对本次任务收到的第一帧音频，
适配器把第一次 push 的 t_ms 当作基准加回去，得到统一音频时间轴。

字段映射：
  is_final            = sentence_end
  text_version        = 一句内从 1 递增；final 之后下一句从 1 重新开始
  t_audio_start_ms    = 该句首个有文本事件的 begin_time；一经确定整句恒定（TurnDetector 用它做句子 key）。
                        该事件无 begin_time 时用到达时已推送音频总长推算，之后同样保持不变
  audio_covered_until_ms = end_time（有字级时间戳时取末字 end_time）。服务端没给时，
                        回退为“该句已确认的最大覆盖值，至少是句首”：这是**偏保守的下界**，
                        不是已推送音频总长（后者会把尚未识别的音频冒充成已覆盖），
                        因此语义完整度会被 TurnDetector 降级，而不是被冒充为完整。
错误分类：AliyunASRError.kind ∈ auth / rate_limit / timeout / other，供 ReconnectingASR 之上的策略判断。
API key 只用于握手头，任何异常 / 日志文本都会先脱敏。
"""
import asyncio
import contextlib
import json
import logging
import os
import time
import uuid

from app.asr.base import ASRAdapter, TranscriptEvent

log = logging.getLogger(__name__)

DEFAULT_URL = 'wss://dashscope.aliyuncs.com/api-ws/v1/inference'
START_TIMEOUT_S = 10.0
FINISH_TIMEOUT_S = 5.0
CONTEXT_MAX_CHARS = 400

_AUTH_CODES = ('invalidapikey', 'unauthorized', 'accessdenied', 'forbidden', 'authentication', 'apikey')
_RATE_CODES = ('throttling', 'ratelimit', 'toomanyrequests', 'quota', 'arrearage')


class AliyunASRError(ConnectionError):
    """kind: auth（鉴权失败，重连无意义）/ rate_limit / timeout / other。"""

    def __init__(self, kind, message, code=''):
        super().__init__(f'[{kind}] {message}')
        self.kind, self.code = kind, code


def classify_error(code='', message='', status=None):
    c = (code or '').lower()
    m = (message or '').lower()
    if status in (401, 403) or any(k in c for k in _AUTH_CODES):
        return 'auth'
    if status == 429 or any(k in c for k in _RATE_CODES):
        return 'rate_limit'
    if 'timeout' in c or 'timeout' in m or 'timed out' in m:
        return 'timeout'
    return 'other'


async def _default_ws_factory(url, headers):
    from websockets.asyncio.client import connect
    return await connect(url, additional_headers=headers, open_timeout=START_TIMEOUT_S)


class AliyunRealtimeASR(ASRAdapter):
    def __init__(self, api_key=None, model='fun-asr-realtime', url=DEFAULT_URL, max_sentence_silence=200,
                 vocabulary_id=None, hotwords=None, source='meeting', ws_factory=None,
                 start_timeout_s=START_TIMEOUT_S, finish_timeout_s=FINISH_TIMEOUT_S,
                 clock_ns=time.perf_counter_ns):
        self._key = api_key or os.environ.get('DASHSCOPE_API_KEY') or ''
        self._model, self._url = model, url
        self._silence = max_sentence_silence
        self._vocab, self._hotwords = vocabulary_id, hotwords
        self._source = source
        self._factory = ws_factory or _default_ws_factory
        self._start_timeout, self._finish_timeout = start_timeout_s, finish_timeout_s
        self._clock_ns = clock_ns
        self._ws = None
        self._task_id = str(uuid.uuid4())
        self._queue = asyncio.Queue()
        self._reader = None
        self._started = asyncio.Event()
        self._finished = asyncio.Event()
        self._closing = False
        self._error = None
        self._base_ms = None   # 第一次 push 的 t_ms
        self._pushed_ms = 0    # 已推送音频总长（相对 base）
        self._reset_sentence()

    # ---- 辅助 ----
    def _scrub(self, text):
        text = str(text)
        return text.replace(self._key, '***') if self._key else text

    def _fail(self, err):
        if self._error is None:
            self._error = err
            log.warning('aliyun asr failed: %s', err)
            self._queue.put_nowait(err)

    def _reset_sentence(self):
        self._version = 0
        self._sent_start = None
        self._covered = 0

    def _run_task(self):
        params = {'format': 'pcm', 'sample_rate': 16000, 'max_sentence_silence': self._silence, 'heartbeat': True}
        if self._vocab:
            params['vocabulary_id'] = self._vocab
        inp = {}
        if self._hotwords:
            text = '、'.join(self._hotwords)[:CONTEXT_MAX_CHARS]
            inp['context'] = [{'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}]
        return {
            'header': {'action': 'run-task', 'task_id': self._task_id, 'streaming': 'duplex'},
            'payload': {'task_group': 'audio', 'task': 'asr', 'function': 'recognition',
                        'model': self._model, 'parameters': params, 'input': inp},
        }

    # ---- ASRAdapter ----
    async def start(self, stream_id):
        self.stream_id = stream_id
        if not self._key:
            raise AliyunASRError('auth', '未提供 API key（参数或环境变量 DASHSCOPE_API_KEY）')
        try:
            self._ws = await self._factory(self._url, {'Authorization': f'Bearer {self._key}'})
            await self._ws.send(json.dumps(self._run_task()))
        except Exception as e:
            raise self._wrap(e) from None
        self._reader = asyncio.create_task(self._read_loop())
        try:
            await asyncio.wait_for(self._started.wait(), self._start_timeout)
        except asyncio.TimeoutError:
            await self._teardown()
            raise AliyunASRError('timeout', '等待 task-started 超时') from None
        if self._error:
            await self._teardown()
            raise self._error

    def _wrap(self, e):
        if isinstance(e, AliyunASRError):
            return e
        status = getattr(getattr(e, 'response', None), 'status_code', None)
        kind = classify_error(message=str(e), status=status)
        if kind == 'other' and isinstance(e, (asyncio.TimeoutError, TimeoutError)):
            kind = 'timeout'
        detail = f'HTTP {status}' if status else type(e).__name__
        return AliyunASRError(kind, self._scrub(f'{detail} {e}'))

    async def push(self, pcm, t_ms):
        if self._error:
            raise self._error
        if self._closing or self._ws is None:
            raise AliyunASRError('other', '适配器已关闭')
        if self._base_ms is None:
            self._base_ms = t_ms
        try:
            await self._ws.send(pcm)
        except Exception as e:
            err = self._wrap(e)
            self._fail(err)
            raise err from None
        self._pushed_ms = max(self._pushed_ms, t_ms - self._base_ms + len(pcm) // 32)

    async def events(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    async def close(self):
        if self._closing:
            return
        self._closing = True
        if self._ws is not None and self._started.is_set() and not self._error:
            with contextlib.suppress(Exception):
                await self._ws.send(json.dumps({
                    'header': {'action': 'finish-task', 'task_id': self._task_id, 'streaming': 'duplex'},
                    'payload': {'input': {}}}))
                await asyncio.wait_for(self._finished.wait(), self._finish_timeout)
        await self._teardown()
        self._queue.put_nowait(None)

    async def _teardown(self):
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()

    def health(self):
        if self._error:
            return {'state': 'down', 'reason': str(self._error)}
        return {'state': 'ok', 'reason': ''}

    # ---- 读取与映射 ----
    async def _read_loop(self):
        try:
            while True:
                msg = await self._ws.recv()
                if isinstance(msg, (bytes, bytearray)):
                    continue
                try:
                    data = json.loads(msg)
                except ValueError:
                    continue
                if self._handle(data):
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not self._closing:
                self._fail(AliyunASRError('other', self._scrub(f'连接断开: {type(e).__name__} {e}')))

    def _handle(self, data):
        """返回 True 表示读取循环应结束。"""
        header = data.get('header') or {}
        kind = header.get('event')
        if kind == 'task-started':
            self._started.set()
        elif kind == 'result-generated':
            self._on_result(((data.get('payload') or {}).get('output') or {}).get('sentence') or {})
        elif kind == 'task-finished':
            self._finished.set()
            if not self._closing:
                self._fail(AliyunASRError('other', '服务端提前结束任务'))
            return True
        elif kind == 'task-failed':
            code = header.get('error_code') or ''
            msg = self._scrub(header.get('error_message') or '')
            self._fail(AliyunASRError(classify_error(code, msg), f'{code}: {msg}', code))
            self._started.set()  # 让 start() 立即看到失败
            return True
        return False

    def _on_result(self, s):
        if s.get('heartbeat'):
            return
        text = (s.get('text') or '').strip()
        final = bool(s.get('sentence_end'))
        if not text:
            if final:
                self._reset_sentence()
            return
        base = self._base_ms or 0
        if self._sent_start is None:
            begin = s.get('begin_time')
            self._sent_start = base + begin if begin is not None else base + self._pushed_ms
        words = s.get('words') or []
        end = s.get('end_time')
        if words and words[-1].get('end_time') is not None:
            end = words[-1]['end_time']
        if end is not None:
            self._covered = max(self._covered, base + end)
        self._covered = max(self._covered, self._sent_start)
        self._version += 1
        ev = TranscriptEvent(self._source, text, final, self._version, self._covered,
                             self._sent_start, self._covered, self._clock_ns())
        self._queue.put_nowait(ev)
        if final:
            self._reset_sentence()
