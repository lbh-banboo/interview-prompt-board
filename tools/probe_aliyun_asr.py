"""阿里云 fun-asr-realtime 实测探针（仅合成语音）。

用法：
  DASHSCOPE_API_KEY 放环境变量（不接受 argv）；可选 ALIYUN_ASR_URL 覆盖端点。
  .venv/bin/python tools/probe_aliyun_asr.py [--gen-only] [--limit N]

只会发送 eval/synth 下由 macOS `say` 生成的合成语音，永不读取 eval/audio、data/ 或任何个人录音。
"""
import argparse
import asyncio
import array
import datetime
import os
import re
import statistics
import subprocess
import sys
import time
import wave
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.asr.aliyun import DEFAULT_URL, AliyunASRError, AliyunRealtimeASR  # noqa: E402
from app.audio.vad import VadEvent  # noqa: E402
from app.domain.turn_detector import TurnDetector  # noqa: E402

SYNTH = ROOT / 'eval' / 'synth'
REPORTS = ROOT / 'eval' / 'reports'
VOICE = 'Tingting'
LEAD_MS, TAIL_MS = 300, 1300
FRAME_MS, FRAME_BYTES = 20, 640
ENERGY_THRESHOLD = 300  # 20ms 帧 RMS 超过此值视为有声

# (id, say 文本（[[slnc N]] 为句中停顿 N ms）, 期望文本)
SENTENCES = [
    ('s01', '请你介绍一下你在上一家公司负责的项目', '请你介绍一下你在上一家公司负责的项目'),
    ('s02', 'Kafka 的 consumer group 是怎么工作的', 'Kafka的consumer group是怎么工作的'),
    ('s03', '你为什么选择这个岗位', '你为什么选择这个岗位'),
    ('s04', '请问你[[slnc 450]]对微服务架构有什么理解', '请问你对微服务架构有什么理解'),
    ('s05', 'Redis 的持久化机制 RDB 和 AOF 有什么区别', 'Redis的持久化机制RDB和AOF有什么区别'),
    ('s06', '你在项目里是怎么做 code review 的', '你在项目里是怎么做code review的'),
    ('s07', '讲讲[[slnc 500]]你遇到过最棘手的线上故障', '讲讲你遇到过最棘手的线上故障'),
    ('s08', 'Spring Boot 的自动配置原理是什么', 'Spring Boot的自动配置原理是什么'),
    ('s09', '如果让你设计一个短链接系统，你会怎么考虑高并发', '如果让你设计一个短链接系统你会怎么考虑高并发'),
    ('s10', '你对 Kubernetes 的 pod 调度了解多少', '你对Kubernetes的pod调度了解多少'),
    ('s11', '你的职业规划是什么，[[slnc 400]]未来三年想往哪个方向发展', '你的职业规划是什么未来三年想往哪个方向发展'),
    ('s12', 'MySQL 的索引为什么用 B+ 树', 'MySQL的索引为什么用B+树'),
    ('s13', '请用 Python 解释一下 GIL 对多线程的影响', '请用Python解释一下GIL对多线程的影响'),
    ('s14', '你有什么想问我们的吗', '你有什么想问我们的吗'),
]


def gen_audio(limit=None):
    SYNTH.mkdir(parents=True, exist_ok=True)
    paths = []
    for sid, say_text, _ in SENTENCES[:limit]:
        out = SYNTH / f'{sid}.wav'
        if not out.exists():
            raw = SYNTH / f'{sid}.raw.wav'
            subprocess.run(['say', '-v', VOICE, '-o', str(raw), '--file-format=WAVE', '--data-format=LEI16@16000',
                            say_text], check=True)
            with wave.open(str(raw)) as w:
                assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
                pcm = w.readframes(w.getnframes())
            raw.unlink()
            with wave.open(str(out), 'wb') as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b'\0' * (LEAD_MS * 32) + pcm + b'\0' * (TAIL_MS * 32))
        paths.append(out)
    return paths


def read_pcm(path):
    with wave.open(str(path)) as w:
        return w.readframes(w.getnframes())


def speech_bounds(pcm):
    """已知的合成语音边界（我方真值）：首个/末个有声 20ms 帧。"""
    active = []
    for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES):
        s = array.array('h', pcm[i:i + FRAME_BYTES])
        active.append((sum(x * x for x in s) / len(s)) ** 0.5 > ENERGY_THRESHOLD)
    idx = [i for i, a in enumerate(active) if a]
    return idx[0] * FRAME_MS, (idx[-1] + 1) * FRAME_MS


def norm(t):
    return re.sub(r'[\W_]+', '', t, flags=re.UNICODE).lower()


def edit_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


async def run_one(url, pcm):
    """按真实 20ms 节拍推流，返回 [(event, 到达时已推音频ms, 到达墙钟ms(相对首帧推送))]。"""
    adapter = AliyunRealtimeASR(url=url)
    got, state = [], {'pushed': 0, 't0': None}

    async def consume():
        async for ev in adapter.events():
            t0 = state['t0'] or time.perf_counter_ns()
            got.append((ev, state['pushed'], (ev.t_received_ns - t0) / 1e6))

    await adapter.start('probe')
    task = asyncio.create_task(consume())
    loop = asyncio.get_running_loop()
    t_start = loop.time()
    state['t0'] = time.perf_counter_ns()
    for i in range(0, len(pcm), FRAME_BYTES):
        await asyncio.sleep(max(0, t_start + i // FRAME_BYTES * FRAME_MS / 1000 - loop.time()))
        await adapter.push(pcm[i:i + FRAME_BYTES], i // FRAME_BYTES * FRAME_MS)
        state['pushed'] = i // FRAME_BYTES * FRAME_MS + FRAME_MS
    await adapter.close()
    await asyncio.wait_for(task, 10)
    return got


def dry_run(got, t_start, t_end, total_ms):
    """把 partial 流喂给 TurnDetector.on_transcript；返回 (直接条件窗口表, committed 延迟)。"""
    cond = {}
    for win in (200, 300, 500, 700):
        covered = max([ev.audio_covered_until_ms for ev, arr, _ in got if arr <= t_end + win], default=-1)
        cond[win] = covered >= t_end - 100
    td = TurnDetector()
    timeline = [(t_start, 0, 'vad', VadEvent('speech_start', t_start)), (t_end, 0, 'vad', VadEvent('speech_end', t_end))]
    timeline += [(arr, 1, 'tr', ev) for ev, arr, _ in got]
    timeline += [(t, 2, 'tick', t) for t in range(0, min(total_ms, t_end + 1500) + 1, FRAME_MS)]
    for t, _, kind, obj in sorted(timeline, key=lambda x: (x[0], x[1])):
        if t > t_end + 1500:
            break
        if kind == 'vad':
            td.on_vad(obj)
        elif kind == 'tr':
            td.on_transcript(obj, now_ms=t)
        else:
            td.tick(t)
    commits = [e for e in td.history if e.type == 'committed']
    return cond, (commits[0].t_ms - t_end if commits else None), [e.type for e in td.history]


def host_label(url):
    h = urlparse(url).hostname or ''
    return re.sub(r'^[^.]+(?=\.[^.]+\.maas\.)', '<workspace>', h)


def fmt(v):
    return 'n/a' if v is None else f'{v:.0f}'


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--gen-only', action='store_true')
    ap.add_argument('--limit', type=int, default=None)
    args = ap.parse_args()
    paths = gen_audio(args.limit)
    print(f'合成音频 {len(paths)} 条 -> {SYNTH}')
    if args.gen_only:
        return
    url = os.environ.get('ALIYUN_ASR_URL', DEFAULT_URL)
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f'aliyun_probe_{datetime.date.today():%Y%m%d}.md'
    head = ['类别: SYNTHETIC_TTS（合成语音，非真人；不代表真实声音结果）', '',
            f'# 阿里云 fun-asr-realtime 首轮实测（{datetime.date.today()}）', '',
            f'- 端点: `{host_label(url)}`；模型 fun-asr-realtime；max_sentence_silence=200；heartbeat=true',
            f'- 语音: macOS say -v {VOICE}，{len(paths)} 句，前导静音 {LEAD_MS}ms、尾部静音 {TAIL_MS}ms；20ms 帧真实节拍推送', '']
    rows, all_lag, cond_hits, dry = [], [], {200: 0, 300: 0, 500: 0, 700: 0}, []
    errs, ed_total, ref_total, retries = 0, 0, 0, 0
    for (sid, _, expected), path in zip(SENTENCES, paths):
        pcm = read_pcm(path)
        t_start, t_end = speech_bounds(pcm)
        try:
            try:
                got = await run_one(url, pcm)
            except AliyunASRError as e:
                if e.kind != 'timeout':
                    raise
                await asyncio.sleep(3)  # 握手超时只重试一次
                retries += 1
                got = await run_one(url, pcm)
        except AliyunASRError as e:
            if not rows and errs == 0:
                out.write_text('\n'.join(head + [f'## 结果: NOT TESTED', '', f'无法完成会话，原因（已脱敏）: {e}', '']))
                print(f'NOT TESTED: {e}')
                return 1
            errs += 1
            rows.append(f'| {sid} | 失败: {e} |')
            continue
        total_ms = len(pcm) // 32
        partials = [(ev, a, w) for ev, a, w in got if not ev.is_final]
        finals = [(ev, a, w) for ev, a, w in got if ev.is_final]
        first_a = partials[0][1] - t_start if partials else None
        first_w = partials[0][2] - t_start if partials else None
        fin_a = finals[-1][1] - t_end if finals else None  # 取最后一个 final（句中停顿可能被切成两句）
        fin_w = finals[-1][2] - t_end if finals else None
        lags = [a - ev.audio_covered_until_ms for ev, a, _ in got]
        all_lag += lags
        text = ''.join(ev.text for ev, _, _ in finals)
        ed = edit_distance(norm(text), norm(expected))
        ed_total, ref_total = ed_total + ed, ref_total + len(norm(expected))
        starts = {}
        for ev, _, _ in got:
            starts.setdefault(ev.t_audio_start_ms, []).append(ev)
        cond, commit_lat, types = dry_run(got, t_start, t_end, total_ms)
        for k, v in cond.items():
            cond_hits[k] += v
        dry.append((sid, cond, commit_lat, types))
        rows.append(f'| {sid} | {t_start}-{t_end} | {fmt(first_a)} / {fmt(first_w)} | {fmt(fin_a)} / {fmt(fin_w)} | '
                    f'{fmt(statistics.median(lags) if lags else None)} | {len(got)} | {len(starts)} | {ed}/{len(norm(expected))} | '
                    f'{text} |')
    n = len(dry)
    firsts = [r for r in rows]
    body = ['## 逐句结果', '',
            '| 句 | 语音区间 ms | 首个 partial 延迟 ms（音频/墙钟） | final 延迟 ms（音频/墙钟，相对语音结束） | 覆盖滞后中位 ms | 事件数 | 句数(start 键) | 编辑距离/期望字数 | 识别文本 |',
            '|---|---|---|---|---|---|---|---|---|'] + firsts + ['']
    if all_lag:
        body += ['## 汇总（合成语音）', '',
                 f'- 覆盖滞后（到达时已推音频 - audio_covered_until_ms）P50={pct(all_lag, 50):.0f}ms P95={pct(all_lag, 95):.0f}ms（{len(all_lag)} 个事件）',
                 f'- 粗 CER = {ed_total}/{ref_total} = {ed_total / max(ref_total, 1):.1%}（去标点空格、转小写后的字符级编辑距离；英文词若被转写成中文谐音计为错误）', '']
    if n:
        body += ['## TurnDetector 先验（K1）', '',
                 '条件 `audio_covered_until_ms >= t_speech_end - 100`：在 speech_end 之后 W ms 内（按音频时钟到达）收到的事件里，最大覆盖值是否满足。', '',
                 '| W (ms 静音窗口) | 满足句数 | 比例 |', '|---|---|---|']
        body += [f'| {w} | {cond_hits[w]}/{n} | {cond_hits[w] / n:.0%} |' for w in (200, 300, 500, 700)]
        lat = [c for _, _, c, _ in dry if c is not None]
        body += ['', f'- TurnDetector 在 speech_end+1500ms 内产生 committed 的句数: {len(lat)}/{n}；判定延迟(音频ms) P50={fmt(pct(lat, 50))} P95={fmt(pct(lat, 95))}',
                 '- 逐句事件序列: ' + '；'.join(f'{s}:{",".join(t) or "无"}' for s, _, _, t in dry), '']
    body += ['## 说明', '', '- 全部为 SYNTHETIC_TTS，单一音色、无噪声、无口音，只能作为协议与时序的首轮先验，不能当作真实声音 PASS。',
             f'- 失败会话数: {errs}；握手超时后重试成功/尝试次数: {retries}', '']
    out.write_text('\n'.join(head + body))
    print(f'报告 -> {out}')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()) or 0)
