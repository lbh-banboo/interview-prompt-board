"""本地 sherpa-onnx 流式 ASR 适配器（降级基线；规划 §3.4）。全程本地推理，不外发任何音频。

模型：sherpa-onnx-streaming-paraformer-bilingual-zh-en（int8）。该模型**没有字级/token 时间戳**
（实测 result.timestamps 恒为空），文本约每 600ms（一个解码 chunk）才更新一次。

字段映射（与 AliyunRealtimeASR 一致）：
  is_final            = sherpa 自带 endpoint（rule1/2/3）触发，随后 reset 开启新句；close() 时若有未结文本也补一个 final
  text_version        = 一句内从 1 递增；final 之后下一句从 1 重新开始
  t_audio_start_ms    = 本句所在片段的起点（上一次 endpoint 时已喂入的音频时刻，首句为第一次 push 的 t_ms）。
                        一句内恒定。它是“偏早的上界”（可能含句首静音），只用作句子 key
  t_audio_end_ms      = audio_covered_until_ms
  partial             = 解码后文本变化时发出；文本不变但覆盖推进时也发一次同文本的 partial（版本递增），
                        否则说话人停下后覆盖永远不会被报出

audio_covered_until_ms 方案（保守下界，**不是**已推音频总长）：
  covered = 已喂入并已 decode 完毕的音频时刻 − lookahead_ms，句内单调不减，且不低于句首。
  lookahead_ms 默认 600 = 一个解码 chunk：CIF 解码会把尾部 token 推迟到后续 chunk，所以某次 decode 看到的文本
  至多只能保证覆盖到“少一个 chunk”之前。该值是依据 chunk 大小的工程估计，没有字级真值可校准，
  故只能说它偏保守；调小（如 0/200）会让覆盖更早但不再有保守保证，仅用于敏感性分析。
  若模型返回了 token 时间戳（本模型没有；该分支仅用假 recognizer 测过，未在真实模型上验证），
  则改用末 token 的时间（start_time + timestamps[-1]，token 起点，同样偏保守）。

线程：accept_waveform/decode_stream 是同步 CPU 推理，放进单线程池，不阻塞事件循环；push 串行，保序。
每次 push 的推理耗时（毫秒）记录在 infer_ms，供 RTF / P95 统计。
"""
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.asr.base import ASRAdapter, TranscriptEvent

SAMPLE_RATE = 16000
DEFAULT_MODEL_DIR = 'models/sherpa-streaming-paraformer-bilingual-zh-en'
DEFAULT_LOOKAHEAD_MS = 600
_CACHE, _CACHE_LOCK = {}, threading.Lock()  # 识别器只共享模型，不共享流状态；进程内单例


def build_recognizer(model_dir=DEFAULT_MODEL_DIR, num_threads=2, rule1=2.4, rule2=1.2, rule3=20.0):
    """按模型路径与参数缓存识别器：每路重连只新建 stream，不重复加载模型。"""
    key = (str(model_dir), int(num_threads), float(rule1), float(rule2), float(rule3))
    with _CACHE_LOCK:
        rec = _CACHE.get(key)
        if rec is None:
            rec = _CACHE[key] = _create_recognizer(model_dir, num_threads, rule1, rule2, rule3)
        return rec


def _create_recognizer(model_dir, num_threads, rule1, rule2, rule3):
    import sherpa_onnx
    return sherpa_onnx.OnlineRecognizer.from_paraformer(
        tokens=f'{model_dir}/tokens.txt', encoder=f'{model_dir}/encoder.int8.onnx',
        decoder=f'{model_dir}/decoder.int8.onnx', num_threads=num_threads, enable_endpoint_detection=True,
        rule1_min_trailing_silence=rule1, rule2_min_trailing_silence=rule2, rule3_min_utterance_length=rule3)


class SherpaStreamingASR(ASRAdapter):
    def __init__(self, source='meeting', recognizer=None, lookahead_ms=DEFAULT_LOOKAHEAD_MS,
                 clock_ns=time.perf_counter_ns, **recognizer_kw):
        self._source = source
        self._rec = recognizer
        self._recognizer_kw = recognizer_kw
        self._lookahead = lookahead_ms
        self._clock_ns = clock_ns
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sherpa')
        self._queue = asyncio.Queue()
        self._stream = None
        self._closing = False
        self._error = None
        self._lock = asyncio.Lock()
        self._fed_ms = None       # 已喂入音频的末尾时刻（绝对 t_ms）
        self._seg_begin = None    # 当前片段起点
        self._t0 = 0              # 第一次 push 的 t_ms（token 时间戳的原点）
        self.infer_ms = []        # 每次 push 的推理耗时
        self.n_events = 0
        self._reset_sentence()

    def _reset_sentence(self):
        self._version = 0
        self._text = ''
        self._covered = 0

    # ---- ASRAdapter ----
    async def start(self, stream_id):
        self.stream_id = stream_id
        try:
            if self._rec is None:
                self._rec = await asyncio.get_running_loop().run_in_executor(
                    self._pool, lambda: build_recognizer(**self._recognizer_kw))
            self._stream = self._rec.create_stream()
        except Exception as e:
            self._error = e
            raise

    async def push(self, pcm, t_ms):
        if self._error:
            raise self._error
        if self._closing or self._stream is None:
            raise ConnectionError('适配器已关闭')
        async with self._lock:
            if self._fed_ms is None:
                self._t0 = self._seg_begin = t_ms
            self._fed_ms = t_ms + len(pcm) // 32
            fed = self._fed_ms
            try:
                results, dt = await asyncio.get_running_loop().run_in_executor(self._pool, self._feed, pcm)
            except Exception as e:
                self._error = e
                raise
            self.infer_ms.append(dt)
            for text, final, ts_ms in results:
                self._emit(text, final, fed, ts_ms)

    async def events(self):
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.put_nowait(None)  # 多次迭代都能结束
                return
            yield item

    async def close(self):
        if self._closing:
            return
        self._closing = True
        if self._stream is not None and not self._error:
            async with self._lock:
                try:
                    text, ts_ms = await asyncio.get_running_loop().run_in_executor(self._pool, self._flush)
                    if text:
                        self._emit(text, True, self._fed_ms, ts_ms)
                except Exception as e:
                    self._error = e
        self._pool.shutdown(wait=False)
        self._queue.put_nowait(None)

    def health(self):
        if self._error:
            return {'state': 'down', 'reason': f'{type(self._error).__name__}: {self._error}'}
        return {'state': 'ok', 'reason': ''}

    # ---- 线程内（同步）----
    def _feed(self, pcm):
        import numpy as np
        t0 = time.perf_counter()
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
        self._stream.accept_waveform(SAMPLE_RATE, x)
        decoded, out = False, []
        while self._rec.is_ready(self._stream):
            self._rec.decode_stream(self._stream)
            decoded = True
        if decoded:
            text, ts_ms = self._read()
            ep = self._rec.is_endpoint(self._stream)
            if ep:
                self._rec.reset(self._stream)
            if text or ep:  # 空句 endpoint 以 ('', True) 通知主线程推进片段起点
                out.append((text, ep, ts_ms))
        return out, (time.perf_counter() - t0) * 1000

    def _flush(self):
        self._stream.input_finished()
        while self._rec.is_ready(self._stream):
            self._rec.decode_stream(self._stream)
        return self._read()

    def _read(self):
        r = self._rec.get_result_all(self._stream)
        ts = list(getattr(r, 'timestamps', None) or [])
        ts_ms = int((getattr(r, 'start_time', 0) + ts[-1]) * 1000) if ts else None
        return r.text.strip(), ts_ms

    # ---- 主线程：映射 ----
    def _emit(self, text, final, fed, ts_ms):
        if not text:  # 空句 endpoint 或空 flush：新片段从此刻开始
            if final:
                self._seg_begin = fed
                self._reset_sentence()
            return
        cov = self._t0 + ts_ms if ts_ms is not None else fed - self._lookahead
        cov = max(self._covered, cov, self._seg_begin)
        if text == self._text and cov == self._covered and not final:
            return  # 文本和覆盖都没变：不发
        self._text, self._covered = text, cov
        self._version += 1
        self.n_events += 1
        self._queue.put_nowait(TranscriptEvent(self._source, text, final, self._version, cov,
                                               self._seg_begin, cov, self._clock_ns()))
        if final:
            self._seg_begin = fed
            self._reset_sentence()
