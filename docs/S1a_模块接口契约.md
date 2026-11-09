# S1a 并行研发接口契约（2026-10-29）

所有模块共用**音频时间轴**：毫秒整数，`t_ms = seq * 20`（每路流独立，从该流 capture_generation 的 seq=0 起算）。核心逻辑禁止读墙钟，时间一律由调用方传入，保证可重放、可确定性测试。
帧：16kHz 单声道 PCM16，20ms=320 样本=640 字节（`app/audio/frame.py`）。source ∈ {mic, meeting, room}。

## 1. ASR（`app/asr/`，M04-1/5）
```python
@dataclass(frozen=True)
class TranscriptEvent:
    source: str; text: str; is_final: bool
    text_version: int            # 同一句内单调递增
    audio_covered_until_ms: int  # 该文本已覆盖到的音频时刻
    t_audio_start_ms: int; t_audio_end_ms: int
    t_received_ns: int           # 后端收到时刻（perf_counter_ns），仅埋点
class ASRAdapter(ABC):
    async def start(self, stream_id: str) -> None
    async def push(self, pcm: bytes, t_ms: int) -> None
    def events(self) -> AsyncIterator[TranscriptEvent]
    async def close(self) -> None
    def health(self) -> dict   # {"state": "ok|reconnecting|down", "reason": str}
```
## 2. VAD（`app/audio/vad.py`，M05-1）
`VadSegmenter(source).feed(pcm: bytes, t_ms: int) -> list[VadEvent]`；`VadEvent(kind: 'speech_start'|'speech_end', t_ms: int)`。`speech_end.t_ms` 为最后一个语音帧的结束时刻（不含静音等待）。

## 3. 轮次判定（`app/domain/turn_detector.py`，M05-2/3）
输入：提问方通道的 `VadEvent` 与 `TranscriptEvent`，以及 `tick(now_ms)`。输出 `TurnEvent(type, turn_text, text_version, t_ms, reason)`，type ∈ candidate / withdrawn / committed / rolled_back / waiting_continue / text_updated。`text_updated` 仅 `trigger='vad'` 模式产生：commit 之后迟到转写只是文本补全/校正（classify 与完整度不变、编辑距离<=2、未判改义），同 turn、text_version 递增、reason='refine'、不计过早结束，供下游刷新检索；其余改动一律 rolled_back。`TurnDetector(..., trigger='coverage'|'vad', min_words_for_vad_commit=0)`，默认 coverage（行为不变）；vad 模式 candidate/committed 事件带 `coverage_stale`。参数 `T_silence=200, T_long=700`（构造参数，可调）。语义完整度只信 `audio_covered_until_ms >= t_speech_end - 100` 的 partial。

## 4. 遥测与回放（`app/telemetry/`，M14-1/2/3/5）
`Metrics.mark(session, turn_id, name, t_ns)` 写 SQLite `metrics`；`replay_wav(path, source, chunk_ms=20, realtime=True)` 产出按实时速率节拍的 `pack()` 帧；`report` 给 P50/P95/P99/max、n、Wilson 95% CI，报告首行标注类别：`ALGO_REPLAY` / `REAL_BROWSER` / `REAL_VISIBLE`；Mock 一律 `SIMULATED`。

## 5. 角色（`app/domain/roles.py`，M03-1）
线上：`mic→answerer`、`meeting→questioner`、`room→unknown`；回声能量比函数。

## 文件归属（互不重叠，禁止改他人文件）
- 前端采集：`web/*`、`package.json`
- ASR+角色：`app/asr/*`、`app/domain/roles.py`、`tests/test_asr.py`、`tests/test_roles.py`
- VAD+轮次：`app/audio/vad.py`、`app/domain/turn_detector.py`、`app/domain/semantic.py`、`tests/test_vad.py`、`tests/test_turn.py`、`models/*`
- 遥测：`app/telemetry/*`、`tests/test_telemetry.py`
- 禁止改 `requirements.txt`、`app/server.py`、`app/core.py`（由主线整合）。需要新依赖就在报告中说明。
