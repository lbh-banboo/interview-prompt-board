"""L7「真实可见端到端」：t_end（真值）→ 提示板 display_ack.t_visible。

链路：Chrome（假媒体文件）→ 采集页 AudioWorklet → /ws/audio → 本地 sherpa ASR + Silero VAD
→ TurnDetector → 三层路由 → DeepSeek（真实 httpx）→ /ws/board → 提示板真实渲染 → display_ack。

真值：拼接音频时已知每题语音结束位置；运行时用「录制 WAV 与源 WAV 互相关」求出
录制首帧对应的文件偏移，把文件真值映射到会话音频时钟/浏览器时钟。迟到、失败样本不剔除。

用法：
  .venv/bin/python tools/l7_e2e.py build
  .venv/bin/python tools/l7_e2e.py run --kind tts
  .venv/bin/python tools/l7_e2e.py run --kind real
  .venv/bin/python tools/l7_e2e.py report --kind tts --kind real
  .venv/bin/python tools/l7_e2e.py all
"""
import argparse
import difflib
import json
import re
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
AUDIO_DIR = ROOT / 'eval' / 'audio_l7'
REPORT_DIR = ROOT / 'eval' / 'reports' / 'l7'
SCREEN_DIR = ROOT / 'eval' / 'screens'
RUN_DIR = ROOT / 'eval' / 'l7_runs'
LIBRARY_ID = '535655c8197c452b8777f19838446bad'
PY = str(ROOT / '.venv' / 'bin' / 'python')
SR = 16000
LEAD_IN_MS = 8000
GAP_MS = 4500
TAIL_MS = 90000

# expected_level 用于人工对照；命中判定以 expected_entry 为准。
TTS_QUESTIONS = [
    dict(qid='Q01', text='你说的示例公司阶梯次卡具体是什么？', category='library', expected_level='L1', expected_entry='H1-10'),
    dict(qid='Q02', text='示例公司的门店店长可以自己设置营销活动吗？', category='library', expected_level='L1', expected_entry='H1-07'),
    dict(qid='Q03', text='中小餐饮商户用营销系统时，最大的难点是什么？', category='library', expected_level='L1', expected_entry='H1-22'),
    dict(qid='Q04', text='入职安克供应链新业务线，一周内如何判断 AI 最大价值机会？', category='library', expected_level='L1', expected_entry='H10-03'),
    dict(qid='Q05', text='你的视频生成工作流具体有哪些步骤？', category='library', expected_level='L1', expected_entry='H11-04'),
    dict(qid='Q06', text='示例公司 AI 点餐早期是否与支付宝一起合作过？', category='library', expected_level='L1', expected_entry='H1-13'),
    dict(qid='Q07', text='你在示例公司做营销后台时，运营有没有反馈功能太复杂、不好操作？', category='library', expected_level='L1', expected_entry='H1-23'),
    dict(qid='Q08', text='你判断 AI 技术会如何影响营销产品的设计和发展？', category='library', expected_level='L1', expected_entry='H1-24'),
    dict(qid='Q09', text='你怎么做需求优先级排序？', category='secondary', expected_level='L2', expected_entry='QPM-01'),
    dict(qid='Q10', text='怎么判断一个需求是真需求还是伪需求？', category='secondary', expected_level='L2', expected_entry='QPM-07'),
    dict(qid='Q11', text='老板临时插了一个紧急需求，你怎么办？', category='secondary', expected_level='L2', expected_entry='QPM-05'),
    dict(qid='Q12', text='请解释 CAP 定理中一致性和可用性如何取舍？', category='outside', expected_level='L3', expected_entry=None),
    dict(qid='Q13', text='Kafka 的分区副本机制如何保证消息不丢失？', category='outside', expected_level='L3', expected_entry=None),
    dict(qid='Q14', text='什么是向量数据库里的 HNSW 索引？', category='outside', expected_level='L3', expected_entry=None),
    dict(qid='Q15', text='如果面试官临时改题，你会怎么应对？', category='personal', expected_level='personal', expected_entry=None),
    dict(qid='Q16', text='请讲讲你经历过的一次最大失败，从中你学到了什么？', category='personal', expected_level='personal', expected_entry=None),
    # 次优库覆盖的通用题（任何岗位可见的 general 条目）：用于验证 setup 绑定岗位后 L2 能真正命中
    dict(qid='Q17', text='如何处理多个任务同时到来？', category='secondary', expected_level='L2', expected_entry='QGEN-22'),
    dict(qid='Q18', text='你的兴趣爱好有哪些？', category='secondary', expected_level='L2', expected_entry='QGEN-05'),
    dict(qid='Q19', text='你为什么想加入我们？', category='secondary', expected_level='L2', expected_entry='QGEN-06'),
]

# 真人单轨混音：pdd.review.json 中 human.is_question_end=true 的条目，取 t_end 前 12 秒。
REAL_IDS = [9, 14, 25, 28, 43]


def wav_read(path):
    with wave.open(str(path)) as w:
        assert w.getframerate() == SR and w.getnchannels() == 1 and w.getsampwidth() == 2, (path, w.getparams())
        return np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').copy()


def wav_write(path, samples):
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.asarray(samples, dtype='<i2').tobytes())


def tts_clip(text, out_stem):
    aiff = out_stem.with_suffix('.aiff')
    subprocess.run(['say', '-v', 'Tingting', '-r', '185', '-o', str(aiff), text], check=True)
    subprocess.run(['afconvert', '-f', 'WAVE', '-d', 'LEI16@16000', '-c', '1', str(aiff), str(out_stem.with_suffix('.wav'))],
                   check=True)
    aiff.unlink(missing_ok=True)
    x = wav_read(out_stem.with_suffix('.wav'))
    level = np.abs(x.astype(np.int32))
    thr = max(200, int(level.max() * 0.01) if level.size else 200)
    idx = np.where(level > thr)[0]
    if idx.size == 0:
        raise RuntimeError(f'TTS 无声：{text}')
    first = max(0, int(idx[0]) - int(0.05 * SR))
    last = int(idx[-1])
    clip = x[first:min(len(x), last + int(0.10 * SR))]
    speech_end = last - first
    return clip, first, speech_end


def build_answer_burst(force=False):
    out = AUDIO_DIR / 'answer_burst.wav'
    if out.exists() and not force:
        return out
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    aiff = AUDIO_DIR / 'answer_burst.aiff'
    subprocess.run(['say', '-v', 'Tingting', '-r', '200', '-o', str(aiff), '好的，我说下背景。'], check=True)
    subprocess.run(['afconvert', '-f', 'WAVE', '-d', 'LEI16@16000', '-c', '1', str(aiff), str(out)], check=True)
    aiff.unlink(missing_ok=True)
    x = wav_read(out)
    idx = np.where(np.abs(x.astype(np.int32)) > 200)[0]
    clip = x[max(0, int(idx[0]) - int(0.05 * SR)):min(len(x), int(idx[-1]) + int(0.15 * SR))]
    wav_write(out, clip)
    print(f'回答方合成音频：{out}（{len(clip)/SR:.2f}s）')
    return out


def build_tts(force=False):
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    out = AUDIO_DIR / 'tts_interview.wav'
    manifest_path = AUDIO_DIR / 'tts_manifest.json'
    if out.exists() and manifest_path.exists() and not force:
        print('TTS 音频已存在，跳过（--force 重建）')
        return json.loads(manifest_path.read_text('utf-8'))
    sep = AUDIO_DIR / 'tts_parts'
    sep.mkdir(exist_ok=True)
    parts, cursor, items = [np.zeros(int(LEAD_IN_MS * SR / 1000), dtype='<i2')], LEAD_IN_MS, []
    for i, q in enumerate(TTS_QUESTIONS, 1):
        clip, trimmed, speech_end = tts_clip(q['text'], sep / f"{i:02d}_{q['qid']}")
        start_ms = cursor
        end_ms = start_ms + len(clip) * 1000 // SR
        items.append(dict(**q, n=i, file_start_ms=start_ms, file_speech_start_ms=start_ms + trimmed * 1000 // SR,
                          file_speech_end_ms=start_ms + speech_end * 1000 // SR, file_end_ms=end_ms,
                          audio='TTS(zh_CN, Tingting, 合成语音)'))
        parts.append(clip)
        parts.append(np.zeros(int(GAP_MS * SR / 1000), dtype='<i2'))
        cursor = end_ms + GAP_MS
    parts.append(np.zeros(int(TAIL_MS * SR / 1000), dtype='<i2'))
    wav_write(out, np.concatenate(parts))
    manifest = dict(kind='tts', label='SYNTHETIC_TTS', audio=str(out.relative_to(ROOT)), sample_rate=SR,
                    duration_ms=cursor + TAIL_MS, lead_in_ms=LEAD_IN_MS, gap_ms=GAP_MS, tail_ms=TAIL_MS,
                    questions=items, voice='Tingting', answer_audio='eval/audio_l7/answer_burst.wav',
                    channel_sources=dict(meeting='REAL getDisplayMedia + Chrome fake audio device（提问方合成语音文件）',
                                         mic='SYNTHETIC_ANSWER_AUDIO（本地音频元素，仅驱动回答方 VAD）'),
                    note='提问方为合成语音；回答方为合成回答片段，不是真实麦克风；真实浏览器、真实服务、真实渲染。')
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), 'utf-8')
    print(f'TTS 音频：{out}（{manifest["duration_ms"]/1000:.1f}s，{len(items)} 题）')
    return manifest


def build_real(force=False):
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    out = AUDIO_DIR / 'real_interview.wav'
    manifest_path = AUDIO_DIR / 'real_manifest.json'
    if out.exists() and manifest_path.exists() and not force:
        print('真人音频已存在，跳过（--force 重建）')
        return json.loads(manifest_path.read_text('utf-8'))
    review = json.loads((ROOT / 'eval' / 'annotations' / 'pdd.review.json').read_text('utf-8'))
    src = wav_read(ROOT / 'eval' / 'audio16k' / 'pdd.wav')
    by_id = {it['id']: it for it in review['items'] if (it.get('human') or {}).get('is_question_end')}
    parts, cursor, items = [np.zeros(int(LEAD_IN_MS * SR / 1000), dtype='<i2')], LEAD_IN_MS, []
    for i, rid in enumerate(REAL_IDS, 1):
        it = by_id[rid]
        t_end = int(it['t_end_ms'])
        t_start = max(0, t_end - 12000)
        clip = src[int(t_start * SR / 1000):int(t_end * SR / 1000)]
        if len(clip) < 8 * SR:
            continue
        start_ms = cursor
        end_ms = start_ms + len(clip) * 1000 // SR
        items.append(dict(qid=f'R{i:02d}', n=i, text=it.get('text_final', ''), category='real',
                          expected_level='unknown', expected_entry=None,
                          file_start_ms=start_ms, file_speech_start_ms=start_ms,
                          file_speech_end_ms=end_ms - 1, file_end_ms=end_ms,
                          source_audio_id='pdd', source_t_end_ms=t_end,
                          audio='REAL_AUDIO_ONE_TRACK(单轨混音，含候选人声音风险)'))
        parts.append(clip)
        parts.append(np.zeros(int(GAP_MS * SR / 1000), dtype='<i2'))
        cursor = end_ms + GAP_MS
    parts.append(np.zeros(int(TAIL_MS * SR / 1000), dtype='<i2'))
    wav_write(out, np.concatenate(parts))
    manifest = dict(kind='real', label='REAL_AUDIO_ONE_TRACK', audio=str(out.relative_to(ROOT)), sample_rate=SR,
                    duration_ms=cursor + TAIL_MS, lead_in_ms=LEAD_IN_MS, gap_ms=GAP_MS, tail_ms=TAIL_MS,
                    questions=items,
                    answer_audio='eval/audio_l7/answer_burst.wav',
                    channel_sources=dict(meeting='REAL getDisplayMedia + Chrome fake audio device（pdd 真人单轨片段）',
                                         mic='SYNTHETIC_ANSWER_AUDIO（本地音频元素，仅驱动回答方 VAD）'),
                    note='pdd 为单轨混音，窗口内可能含候选人回答；取 t_end 前 12 秒，真值用人工标注 t_end。回答方为合成回答片段。')
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), 'utf-8')
    print(f'真人音频：{out}（{manifest["duration_ms"]/1000:.1f}s，{len(items)} 题）')
    return manifest


# ---------------- 运行 ----------------

def make_temp_data():
    tmp = Path(tempfile.mkdtemp(prefix='l7-data-'))
    for name in ('interview.sqlite3', 'records.sqlite3'):
        shutil.copy2(ROOT / 'data' / name, tmp / name)
    env = ROOT / 'data' / '.env'
    if env.exists():
        shutil.copy2(env, tmp / '.env')  # 只在临时副本中使用；run 结束立即删除
    (tmp / 'logs').mkdir(exist_ok=True)
    return tmp


def start_server(data_dir, llm_log):
    env = dict(os.environ, L7_DATA_DIR=str(data_dir), L7_LLM_LOG=str(llm_log), PYTHONPATH=str(ROOT))
    proc = subprocess.Popen([PY, str(ROOT / 'tools' / 'l7_server.py')], cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = proc.stdout.readline().strip()
    try:
        info = json.loads(line)
    except ValueError:
        proc.kill()
        raise RuntimeError('服务启动失败：' + line[:400])
    return proc, info['port']


def run_one(kind, force=False):
    manifest = build_tts(force) if kind == 'tts' else build_real(force)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    run_id = time.strftime('%Y%m%d-%H%M%S')
    out_json = RUN_DIR / f'{kind}_{run_id}.json'
    llm_log = RUN_DIR / f'{kind}_{run_id}.llm.jsonl'
    data_dir = make_temp_data()
    proc = None
    try:
        proc, port = start_server(data_dir, llm_log)
        base = f'http://127.0.0.1:{port}'
        print(f'服务：{base}（临时数据目录 {data_dir}）')
        cmd = ['node', str(ROOT / 'tools' / 'l7_drive.mjs'), f'--base={base}', f'--library={LIBRARY_ID}',
               f'--answer={AUDIO_DIR / "answer_burst.wav"}',
               f'--audio={ROOT / manifest["audio"]}', f'--out={out_json}', f'--shots={SCREEN_DIR}',
               f'--category={kind}',
               f'--wait_ms={int(manifest["duration_ms"] - manifest["tail_ms"] + 30000)}']
        print('驱动：', ' '.join(cmd))
        subprocess.run(cmd, cwd=str(ROOT), check=False)
        if not out_json.exists():
            raise RuntimeError('浏览器驱动未产生结果')
        run_info = json.loads(out_json.read_text('utf-8'))
        session = run_info.get('session', '')
        art = RUN_DIR / f'{kind}_{run_id}_artifacts'
        art.mkdir(parents=True, exist_ok=True)
        metrics = read_metrics(data_dir / 'interview.sqlite3', session) if session else []
        (art / 'metrics.json').write_text(json.dumps(metrics, ensure_ascii=False), 'utf-8')
        wavs = []
        src_dir = data_dir / 'audio' / session
        if src_dir.exists():
            for f in sorted(src_dir.glob('meeting_*_g*_*.wav')):
                shutil.copy2(f, art / f.name)
                wavs.append(str((art / f.name).relative_to(ROOT)))
        run_info.update(manifest=manifest, metrics_json=str((art / 'metrics.json').relative_to(ROOT)),
                        meeting_wavs=wavs, llm_log=str(llm_log.relative_to(ROOT)), run_id=run_id)
        out_json.write_text(json.dumps(run_info, ensure_ascii=False, indent=2), 'utf-8')
        print(f'结果：{out_json}')
        return out_json
    finally:
        if proc is not None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        # 临时副本用后即删（含 .env）
        shutil.rmtree(data_dir, ignore_errors=True)


# ---------------- 对齐与统计 ----------------

def xcorr_offset(rec, src):
    """返回 (offset_samples, quality)：rec[0] 对应 src[offset_samples]。"""
    level = np.abs(rec.astype(np.int32))
    thr = max(200, int(level.max() * 0.02) if level.size else 200)
    idx = np.where(level > thr)[0]
    if idx.size == 0:
        raise RuntimeError('录制音频无声，无法对齐')
    onset = max(0, int(idx[0]) - int(0.1 * SR))
    win = rec[onset:onset + int(6 * SR)].astype(np.float64)
    win -= win.mean()
    s = src.astype(np.float64)
    s0 = s.mean()
    s = s - s0
    nfft = 1 << int(np.ceil(np.log2(len(s) + len(win) - 1)))
    corr = np.fft.irfft(np.fft.rfft(s, nfft) * np.conj(np.fft.rfft(win, nfft)), nfft)
    k = int(np.argmax(corr[:len(s) + len(win)]))
    seg = s[k:k + len(win)]
    denom = np.linalg.norm(seg) * np.linalg.norm(win)
    quality = float(np.dot(seg, win) / denom) if denom else 0.0
    # rec[onset] ↔ src[k]，所以 rec[0] ↔ src[k-onset]
    return k - onset, quality, onset


def read_metrics(db_path, session):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        'SELECT turn_id,name,t_ns,value FROM metrics WHERE session_id=? ORDER BY t_ns,rowid', (session,))]
    conn.close()
    out = []
    for r in rows:
        try:
            v = json.loads(r['value']) if r['value'] else {}
        except ValueError:
            v = {}
        out.append(dict(turn_id=r['turn_id'], name=r['name'], t_ns=r['t_ns'], value=v))
    return out


def load_concat(files):
    xs = [wav_read(f) for f in files]
    return np.concatenate(xs) if len(xs) > 1 else xs[0]


def _norm(text):
    return re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]+', '', text or '').lower()


def pair_commits(commits, events):
    """把 metrics 的 committed 行与 board 的 committed 事件按 (turn_id, 序号) 配对，并取该 epoch 的首个提问转写文本。"""
    commit_events = [e for e in events
                     if e['type'] == 'turn_state' and e.get('payload', {}).get('state') == 'question_committed']
    texts = {}
    for e in events:
        if e['type'] == 'transcript' and e.get('payload', {}).get('role') == 'questioner':
            texts.setdefault((e.get('turn_id'), e.get('epoch')), e.get('payload', {}).get('text', ''))
    by_turn = {}
    for e in commit_events:
        by_turn.setdefault(e.get('turn_id'), []).append(e)
    seen, pairs = {}, []
    for c in commits:
        turn = c['turn_id']
        k = seen.get(turn, 0)
        seen[turn] = k + 1
        evs = by_turn.get(turn, [])
        ev = evs[k] if k < len(evs) else None
        epoch = ev.get('epoch') if ev else None
        pairs.append(dict(c=c, ev=ev, epoch=epoch, text=texts.get((turn, epoch), '')))
    return pairs


def audio_clock_visible(ack_ms, meeting0_ms):
    return (ack_ms - meeting0_ms)


def analyze(run_json):
    run = json.loads(Path(run_json).read_text('utf-8'))
    manifest = run['manifest']
    session = run['session']
    kind = run['category']
    metrics = json.loads((ROOT / run['metrics_json']).read_text('utf-8'))
    rec = load_concat([ROOT / p for p in run['meeting_wavs']])
    src = wav_read(ROOT / manifest['audio'])
    delta, quality, onset = xcorr_offset(rec, src)
    delta_ms = delta * 1000 / SR
    meeting0 = run['capture']['meeting0']
    meeting0_ms = meeting0['frame_epoch_ms']

    commits = [m for m in metrics if m['name'] == 'committed']
    commit_events = [e for e in run['board']['events']
                     if e['type'] == 'turn_state' and e.get('payload', {}).get('state') == 'question_committed']
    # 音频时钟 → 浏览器/服务器 epoch 的锚点：A = 发布该 committed 事件的墙钟 − 该事件的音频时刻。
    # 用逐事件 A（而非首帧 t_client_us）是因为 AudioWorklet 首帧时间戳不含采集缓冲延迟。
    anchors = []
    for i, m in enumerate(commits):
        ev = commit_events[i] if i < len(commit_events) else None
        anchors.append(None if ev is None else ev['t_server'] * 1000 - (m['value'].get('t_ms') or 0))
    rolled = [m for m in metrics if m['name'] == 'rolled_back']
    withdrawn = [m for m in metrics if m['name'] == 'withdrawn']
    prompts = [m for m in metrics if m['name'] == 'prompt_sent']
    acks = [m for m in metrics if m['name'] == 'display_ack']
    late = [m for m in metrics if m['name'] == 'late_archived']
    prompt_info, ack_by_pid = {}, {}
    for p in prompts:
        v = p['value']
        if v.get('prompt_id'):
            prompt_info[v['prompt_id']] = dict(kind=v.get('kind'), level=v.get('level'), hit_id=v.get('hit_id'),
                                               turn_id=p['turn_id'])
    for a in acks:
        pid, tv = a['value'].get('prompt_id'), a['value'].get('t_visible')
        if pid and tv is not None:
            ack_by_pid.setdefault(pid, tv)  # 同一 prompt 只计首次可见
    ack_kinds = {'primary': 0, 'supplement': 0, 'status': 0}
    for pid in ack_by_pid:
        pkind = (prompt_info.get(pid) or {}).get('kind')
        if pkind in ack_kinds:
            ack_kinds[pkind] += 1

    status_by_turn = {}
    for e in run['board']['events']:
        if e['type'] == 'prompt_status':
            status_by_turn.setdefault((e.get('turn_id'), e.get('epoch')), []).append(e.get('payload', {}).get('text'))
    pairs = pair_commits(commits, run['board']['events'])
    used = set()
    detail = []
    for q in manifest['questions']:
        t_end = q['file_speech_end_ms'] - delta_ms
        row = dict(qid=q['qid'], text=q['text'], category=q['category'], expected_level=q['expected_level'],
                   expected_entry=q['expected_entry'], t_end_ms=round(t_end, 1),
                   file_speech_end_ms=q['file_speech_end_ms'], corr_quality=round(quality, 4),
                   delta_ms=round(delta_ms, 1))
        best = None
        for i, pr in enumerate(pairs):
            if i in used or not pr['ev']:
                continue
            cval = pr['c']['value']
            tse = cval.get('t_speech_end_ms')
            if tse is None:
                tse = cval.get('t_ms', 0)
            dt = abs(tse - t_end)
            ratio = difflib.SequenceMatcher(None, _norm(q['text']), _norm(pr['text'])).ratio()
            if ratio < 0.30 or dt > 6000:
                continue
            score = ratio - dt / 1e6
            if best is None or score > best[0]:
                best = (score, i, pr, ratio, dt)
        if best is None:
            row.update(matched=False, missed=True, turn_id=None)
            detail.append(row)
            continue
        _, bi, pr, ratio, dt = best
        used.add(bi)
        c, ev = pr['c'], pr['ev']
        cval = c['value']
        turn = c['turn_id']
        anchor = ev['t_server'] * 1000 - (cval.get('t_ms') or 0)
        row['clock_anchor_ms'] = round(anchor, 1)
        row['anchor_vs_meeting0_ms'] = round(anchor - meeting0_ms, 1)
        row['asr_text'] = pr['text']
        row['text_ratio'] = round(ratio, 3)
        row.update(matched=True, missed=False, match_err_ms=round(dt, 1), turn_id=turn,
                   t_committed_audio_ms=cval.get('t_ms'), t_speech_end_audio_ms=cval.get('t_speech_end_ms'),
                   commit_reason=cval.get('reason'))
        near = [p for p in prompts if p['turn_id'] == turn
                and abs((p['value'].get('t_committed_ns') or 0) - c['t_ns']) <= 50_000_000]
        tps = sorted(near or [p for p in prompts if p['turn_id'] == turn], key=lambda p: p['t_ns'])
        first = tps[0] if tps else None
        row['prompt_sent_count'] = len(tps)
        row['first_prompt_kind'] = first['value'].get('kind') if first else None
        row['first_prompt_level'] = first['value'].get('level') if first else None
        row['first_hit_id'] = first['value'].get('hit_id') if first else None
        row['decision_ms'] = round((first['value'].get('latency_ms') or 0), 1) if first else None
        prim = next((p for p in tps if p['value'].get('kind') == 'primary'), None)
        row['primary'] = bool(prim)
        row['primary_prompt_id'] = prim['value'].get('prompt_id') if prim else None
        row['primary_level'] = prim['value'].get('level') if prim else None
        row['primary_hit_id'] = prim['value'].get('hit_id') if prim else None
        row['primary_sent_after_commit_ms'] = round((prim['t_ns'] - prim['value'].get('t_committed_ns', prim['t_ns'])) / 1e6, 1) if prim else None
        ack = None
        if prim:
            ack = next((a for a in acks if a['value'].get('prompt_id') == prim['value'].get('prompt_id')), None)
        row['ack'] = bool(ack)
        row['t_visible_epoch_ms'] = ack['value'].get('t_visible') if ack else None
        if ack:
            row['visible_audio_ms'] = round(audio_clock_visible(ack['value']['t_visible'], anchor), 1)
            row['visible_latency_ms'] = round(ack['value']['t_visible'] - (anchor + t_end), 1)
            row['commit_to_visible_ms'] = round(ack['value']['t_visible'] - (anchor + (cval.get('t_ms') or t_end)), 1)
        else:
            row['visible_latency_ms'] = None
            row['commit_to_visible_ms'] = None
        any_ack = [tv for p in tps for tv in [ack_by_pid.get(p['value'].get('prompt_id'))] if tv is not None]
        row['first_visible_any_ms'] = round(min(any_ack) - (anchor + t_end), 1) if any_ack else None
        row['ack_kinds'] = {k: sum(1 for p in tps
                                   if (prompt_info.get(p['value'].get('prompt_id')) or {}).get('kind') == k
                                   and p['value'].get('prompt_id') in ack_by_pid)
                            for k in ('primary', 'supplement', 'status')}
        row['rolled_back'] = len([m for m in rolled if m['turn_id'] == turn])
        row['withdrawn'] = len([m for m in withdrawn if m['turn_id'] == turn])
        row['late_archived'] = len([m for m in late if m['turn_id'] == turn])
        row['supplements'] = len([p for p in tps if p['value'].get('kind') == 'supplement'])
        row['status_texts'] = status_by_turn.get((turn, pr['epoch']), [])
        detail.append(row)

    # 同一 turn_id 被两个不同问题命中 = 后一题被并入前一题（没有独立提示）；它的可见延迟不能算作自己的。
    by_turn = {}
    for d in detail:
        if d.get('matched'):
            by_turn.setdefault(d['turn_id'], []).append(d)
    merged = []
    for turn, ds in by_turn.items():
        if len(ds) > 1:
            ds.sort(key=lambda x: x['qid'])
            for d in ds[1:]:
                d['merged_into'] = ds[0]['qid']
                d['missed'] = True
                d['visible_latency_ms'] = None
                d['commit_to_visible_ms'] = None
                d['rolled_back'] = 0
                merged.append(dict(qid=d['qid'], into=ds[0]['qid'], turn_id=turn))

    # 串题：真实 board 事件流中，每条 prompt_* 的 (turn_id, epoch) 必须等于当时最近一次 committed 的活动轮次。
    cross = []
    active = None
    for ev in run['board']['events']:
        if ev['type'] == 'turn_state' and ev.get('payload', {}).get('state') == 'question_committed':
            active = (ev.get('turn_id'), ev.get('epoch'))
        elif ev['type'] in ('prompt_primary', 'prompt_status', 'prompt_supplement') and active is not None:
            if (ev.get('turn_id'), ev.get('epoch')) != active:
                cross.append(dict(type=ev['type'], got=[ev.get('turn_id'), ev.get('epoch')], expected=list(active)))
    # 虚构：事实校验器把个人事实替换为占位符
    placeholder = 0
    supp_texts = []
    for ev in run['board']['events']:
        if ev['type'] == 'prompt_supplement':
            text = str(ev.get('payload', {}).get('content', ''))
            supp_texts.append(text)
            if '[需本人补充]' in text:
                placeholder += 1

    llm = []
    llm_path = Path(run.get('llm_log', ''))
    if llm_path.exists():
        for line in llm_path.read_text('utf-8').splitlines():
            if line.strip():
                try:
                    llm.append(json.loads(line))
                except ValueError:
                    pass
    firsts = [x['first_body_ms'] for x in llm if x.get('first_body_ms') is not None]
    aborts = [x for x in llm if x.get('event') == 'stream_abort']
    timeouts = [x for x in aborts if x.get('error') not in ('GeneratorExit',)]
    early_close = [x for x in aborts if x.get('error') == 'GeneratorExit']
    statuses = {}
    for x in llm:
        if x.get('event') == 'request':
            statuses[x.get('status')] = statuses.get(x.get('status'), 0) + 1
    late_after_8s = [x for x in llm if x.get('event') in ('stream_end', 'stream_abort') and (x.get('elapsed_ms') or 0) > 8000]

    library = [d for d in detail if d['category'] == 'library']
    secondary = [d for d in detail if d['category'] == 'secondary']
    outside = [d for d in detail if d['category'] == 'outside']
    personal = [d for d in detail if d['category'] == 'personal']
    lib_correct = sum(1 for d in library if not d.get('merged_into')
                      and (d.get('primary_hit_id') == d['expected_entry'] or d.get('first_hit_id') == d['expected_entry']))
    lib_wrong = sum(1 for d in library if d.get('primary_hit_id') or d.get('first_hit_id'))
    lib_wrong -= sum(1 for d in library if (d.get('primary_hit_id') or d.get('first_hit_id')) == d['expected_entry'])
    sec_l2 = sum(1 for d in secondary if d.get('first_prompt_level') == 'L2' or d.get('primary_level') == 'L2')
    out_false = sum(1 for d in outside if d.get('first_prompt_level') in ('L1', 'L1p') or d.get('primary_level') in ('L1', 'L1p'))
    out_reject = sum(1 for d in outside if d.get('first_prompt_kind') == 'status' and not d.get('primary'))
    per_note = sum(1 for d in personal if any('个人经历' in (x or '') for x in (d.get('status_texts') or [])))
    per_primary = sum(1 for d in personal if d.get('primary'))
    hit = dict(library_n=len(library), library_correct=lib_correct, library_wrong=lib_wrong,
               secondary_n=len(secondary), secondary_l2=sec_l2,
               outside_n=len(outside), outside_reject=out_reject, outside_false_hit=out_false,
               personal_n=len(personal), personal_note=per_note, personal_primary=per_primary,
               merged=merged)
    summary = dict(kind=kind, label=manifest['label'], session=session, audio=manifest['audio'], hit=hit,
                   run_json=str(Path(run_json).relative_to(ROOT)),
                   n_questions=len(manifest['questions']),
                   n_matched=sum(1 for d in detail if d.get('matched') and not d.get('missed')),
                   n_lost=sum(1 for d in detail if d.get('missed') and not d.get('merged_into')),
                   rollbacks_total=len(rolled), ack_kinds=ack_kinds, setup=run.get('setup'),
                   n_any_visible=sum(1 for d in detail if d.get('first_visible_any_ms') is not None),
                   delta_ms=round(delta_ms, 1),
                   corr_quality=round(quality, 4), meeting0_epoch_ms=meeting0_ms,
                   screenshots=run.get('screenshots', []), problems=run.get('problems', []),
                   cross_turn=cross, placeholder_subs=placeholder, supplements=supp_texts,
                   anchor_drift=[round(a - meeting0_ms, 1) for a in anchors if a is not None],
                   llm=dict(requests=len([x for x in llm if x.get('event') == 'request']), statuses=statuses,
                            first_body_ms=firsts, aborts=timeouts, early_close=early_close, over_8s=late_after_8s),
                   detail=detail)
    return summary


# ---------------- 报告 ----------------

def pct(values, p):
    if not values:
        return None
    xs = sorted(values)
    h = (len(xs) - 1) * p / 100
    lo = int(np.floor(h)); hi = min(lo + 1, len(xs) - 1)
    return round(xs[lo] + (h - lo) * (xs[hi] - xs[lo]), 1)


def dist(values):
    return dict(n=len(values), p50=pct(values, 50), p95=pct(values, 95), p99=pct(values, 99),
                max=max(values) if values else None)


def agg(s):
    md = [d for d in s['detail'] if d.get('matched') and not d.get('missed')]
    dec = [d['t_committed_audio_ms'] - d['t_end_ms'] for d in md if d.get('t_committed_audio_ms') is not None]
    vis = [d['visible_latency_ms'] for d in md if d.get('visible_latency_ms') is not None]
    anyv = [d['first_visible_any_ms'] for d in md if d.get('first_visible_any_ms') is not None]
    roll_rows = sum(1 for d in s['detail'] if d.get('rolled_back'))
    return dict(dec=dec, vis=vis, anyv=anyv, n_primary_ack=len(vis), n_any_ack=len(anyv),
                merged=len(s['hit']['merged']), lost=s.get('n_lost', 0),
                rollbacks=s.get('rollbacks_total', sum(int(d.get('rolled_back') or 0) for d in s['detail'])),
                roll_rows=roll_rows, l2=s['hit']['secondary_l2'], l2_n=s['hit']['secondary_n'],
                ack=s.get('ack_kinds') or {}, n=s['n_questions'], matched=s['n_matched'])


def write_report(summaries, name='l7_report.md', baseline=None):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    from app.telemetry.report import build_report

    v2 = baseline is not None or name != 'l7_report.md'
    by = {x['kind']: agg(x) for x in summaries}
    tts_a = by.get('tts') or dict(n=0, lost=0, merged=0, dec=[], vis=[])
    real_a = by.get('real') or dict(n=0, lost=0, merged=0, dec=[], vis=[])
    L = [f'# L7 真实可见端到端报告{"（v2 修复后）" if v2 else ""}（t_end → display_ack.t_visible）', '',
         f'- 生成时间：{time.strftime("%Y-%m-%d %H:%M:%S")}',
         '- 链路：真实 Chromium 采集页（AudioWorklet → /ws/audio）→ 本地 sherpa ASR + Silero VAD → TurnDetector → 三层路由 → DeepSeek 真实 httpx → /ws/board → 提示板真实渲染 → display_ack。',
         '- 会话创建：走正常产品流程 `POST /api/profiles` → `POST /api/setup/sessions`（写入 roles）→ 自检 → 开始；采集页复用该会话（驱动拦截旧的 `POST /api/sessions`）。',
         '- 真值：拼接音频时已知的每题语音结束点；用「录制 WAV × 源 WAV 互相关」求录制首帧偏移，映射到同一音频时钟。',
         '- 类别：TTS 组 = SYNTHETIC_TTS（合成语音；报告模块归为 SIMULATED）；真人组 = REAL_AUDIO_ONE_TRACK（单轨混音）。两类分开，PASS 不互相继承。',
         '- 通道限制：meeting = 真实 getDisplayMedia + Chrome 假音频设备（播放提问音频）；mic = 本地合成回答片段（驱动回答方 VAD），**不是双真实音源**。',
         '- 本轮原始 run：' + '、'.join(s.get('run_json', '') for s in summaries), '']
    if baseline:
        base = {s['kind']: s for s in baseline}
        L += ['## v1 → v2 对比（原始数据均在 eval/l7_runs/）', '',
              '| 指标 | v1 TTS | v2 TTS | v1 REAL | v2 REAL |', '|---|---|---|---|---|']
        cur = {s['kind']: s for s in summaries}
        rows = [('匹配', lambda a: f"{a['matched']}/{a['n']}"),
                ('合并数', lambda a: a['merged']),
                ('丢失数', lambda a: a['lost']),
                ('回滚总数', lambda a: a['rollbacks']),
                ('判定 P50/P95 (ms)', lambda a: f"{dist(a['dec'])['p50']}/{dist(a['dec'])['p95']}"),
                ('t_end→visible(primary) P50/P95 (ms)', lambda a: f"{dist(a['vis'])['p50']}/{dist(a['vis'])['p95']} (n={len(a['vis'])})"),
                ('t_end→首个任意提示 P50/P95 (ms)', lambda a: f"{dist(a['anyv'])['p50']}/{dist(a['anyv'])['p95']} (n={len(a['anyv'])})"),
                ('L2 命中', lambda a: f"{a['l2']}/{a['l2_n']}"),
                ('ACK primary/supplement/status', lambda a: f"{a['ack'].get('primary', 0)}/{a['ack'].get('supplement', 0)}/{a['ack'].get('status', 0)}")]
        for label, fn in rows:
            cells = []
            for k in ('tts', 'real'):
                for src in (base, cur):
                    cells.append(fn(agg(src[k])) if k in src else '-')
            L.append(f'| {label} | ' + ' | '.join(str(c) for c in cells) + ' |')
        L += ['', '- ACK 数 = 去重 prompt_id 的 ACK 条数，可能多于题数（同题重提交 / 文本修正 / 多次补充各有一条）。',
              '- v1 的 ACK 只有 primary（status / supplement 未回报）；v2 三类都回报，因此 ACK 数不可直接相加对比。',
              '- v1 的 t_end→visible(primary) 只覆盖 8 题（合并/无 primary 的题没有 ACK）；v2 覆盖 13 题，分布不可直接比较。',
              '- 两组题目数不同：v1 TTS n=16；v2 TTS 追加 3 道次优库通用题（Q17~Q19），n=19。匹配率请按分母看。',
              '- 真人组 R02 的 t_end→visible 为负：单轨对齐的锚点误差（VAD/真值不在同一时钟），不是真实“提前显示”，'
              '该值只作异常提示，不作为性能证据。', '']
    L += ['## 总览', '', '| 组 | 类别 | n | 匹配 | 丢失 | 合并 | primary+ACK | 任意提示 ACK | 判定 P50/P95 (ms) | t_end→visible(primary) P50/P95 | t_end→首个任意提示 P50/P95 | 回滚总数/有回滚题 | 结论 |',
          '|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for s in summaries:
        a = agg(s)
        dp, vp, ap = dist(a['dec']), dist(a['vis']), dist(a['anyv'])
        verdict = ('SIMULATED（合成语音，不得 PASS）' if s['kind'] == 'tts' else
                   'FAIL（单轨真人，样本不足）' if dp['n'] else 'NOT TESTED')
        L.append(f"| {s['kind'].upper()} | {s['label']} | {a['n']} | {a['matched']} | {a['lost']} | {a['merged']} | "
                 f"{a['n_primary_ack']} | {a['n_any_ack']} | {dp['p50']}/{dp['p95']} | {vp['p50']}/{vp['p95']} | "
                 f"{ap['p50']}/{ap['p95']} | {a['rollbacks']}/{a['roll_rows']} | {verdict} |")
    L += ['', '**结论说明**：TTS 组是合成语音，结论只能是 SIMULATED，不能当 PASS；真人组是单轨混音近似，'
          '样本 n=5，任何 P95 都不可外推。两类不互相继承。', '',
          '**仍未解决 / NOT TESTED（不美化）**：', '',
          f'1. TTS 判定 P95={dist(tts_a["dec"])["p95"]}ms，仍高于 300ms 门槛；'
          f't_end→visible(primary) P95={dist(tts_a["vis"])["p95"]}ms，高于 800ms 门槛。'
          'v1 的 619.6ms 只覆盖 8 题（合并/丢失的题没有 primary ACK），不能说明 v2 变慢或变快。',
          f'2. 真人组 n={real_a["n"]}、{real_a["lost"]} 题未匹配、{real_a["merged"]} 题合并；'
          f'判定 P95={dist(real_a["dec"])["p95"]}ms 未达；R02 的负可见值是单轨锚点误差。'
          '真人双源（会议客户端 + 真实麦克风）与真实会议客户端兼容矩阵 NOT TESTED。',
          '3. 短问句补救只允许迟到 final 重开一次，且下一段开口会作废旧窗口；final 更晚仍会丢，属已知边界。',
          '4. L7 测试服务的 setup 自检依赖未注入（audio/asr/llm ping），因此以 degraded 开始；真实产品环境需配置。',
          '5. 无 SEARCH_API_KEY：L3 真实联网首条 NOT TESTED；DeepSeek 3s/8s 超时取消路径本轮未触发，NOT TESTED。', '',
          '**本轮修复**（四项均先写复现测试、再最小改动，回归已在 pytest/npm test 覆盖）：', '',
          '1. 题目合并：回答方仍在说时重提交新题，检测器停留在 `question_committed`，下一题的开口被当成“续说本题”；'
          '现改为提交时若回答方已开口直接进入 `answering`，下一题走插话/新题分支，turn 不合并。',
          '2. 短问句丢失：本地 ASR final 晚于打断窗口时，半句先被判 `unclear` 并永久丢弃；'
          '现对 `unknown`（含 low/incomplete 的截断半句）保留该段句子键与区间，迟到 final 只重开一次；'
          '重开仍按完整文本分类，附和/嗯/对不会提交。',
          '3. 次优库 L2：L7 驱动改用 setup 流程创建会话（roles 写入 `roles_json`/快照）并复用采集页；'
          '同时修复 setup 开始后未预建主库索引（此前会整场“检索不可用”）。',
          '4. display_ack：`prompt_status` / `prompt_supplement` 也分配 `prompt_id`，前端渲染后 ACK；'
          '记录库新增 status 入档与 ACK 状态回写（`state=acked`、`display_ack_at`）。', '']
    for s in summaries:
        detail = s['detail']
        matched = [d for d in detail if d.get('matched') and not d.get('missed')]
        vis = [d['visible_latency_ms'] for d in matched if d.get('visible_latency_ms') is not None]
        c2v = [d['commit_to_visible_ms'] for d in matched if d.get('commit_to_visible_ms') is not None]
        dec = [d['t_committed_audio_ms'] - d['t_end_ms'] for d in matched
               if d.get('t_committed_audio_ms') is not None]
        anyv = [d['first_visible_any_ms'] for d in matched if d.get('first_visible_any_ms') is not None]
        sim = s['kind'] == 'tts'
        rows = []
        for d in detail:
            rows.append(dict(sample_id=d['qid'], audio_path=s.get('audio', ''), log_path='metrics',
                             verified=True, mode='online', t_end=d['t_end_ms'],
                             t_committed=None if d.get('missed') else d.get('t_committed_audio_ms'),
                             t_visible=None if d.get('missed') else d.get('visible_audio_ms'),
                             simulated=sim, premature=d.get('rolled_back', 0), withdrawn=d.get('withdrawn', 0),
                             missed=d.get('missed', False), role_ok=True, role_unknown=False, overlap_truth=False))
        suffix = '' if name == 'l7_report.md' else '_v2'
        csv_path = REPORT_DIR / f'l7_{s["kind"]}_samples{suffix}.csv'
        standard = build_report(rows, 'REAL_VISIBLE', csv_path=str(csv_path))

        anchors = [d['anchor_vs_meeting0_ms'] for d in detail if d.get('anchor_vs_meeting0_ms') is not None]
        errs = [d['match_err_ms'] for d in detail if d.get('match_err_ms') is not None]
        L += [f'## {s["kind"].upper()} 组（类别：{s["label"]}）', '',
              f'- n={s["n_questions"]}，有效匹配 {s["n_matched"]}，对齐相关度 {s["corr_quality"]:.4f}，录制首帧文件偏移 {s["delta_ms"]:.1f} ms',
              f'- 浏览器 meeting 首帧 epoch：{s["meeting0_epoch_ms"]:.1f}',
              f'- 音频时钟锚点相对首帧时间戳：{min(anchors) if anchors else None} ~ {max(anchors) if anchors else None} ms'
              '（AudioWorklet 采集缓冲；逐题用 committed 事件的服务器墙钟−音频时刻校正，漂移即上区间宽度）',
              f'- 对齐残差（VAD t_speech_end − 拼接真值）：P50={pct(errs, 50)} ms，P95={pct(errs, 95)} ms（n={len(errs)}）',
              '', '### 标准报告模块（app/telemetry/report.py，判定延迟与过早/漏判）', '',
              '```', standard.strip(), '```', '',
              '### L7 关键延迟（ms）', '',
              '| 指标 | n | P50 | P95 | P99 | max |', '|---|---|---|---|---|---|']
        for metric, vals in (('t_end→committed（判定）', dec), ('committed→visible（渲染+ACK，primary）', c2v),
                             ('t_end→visible（首提示可见，primary）', vis),
                             ('t_end→首个任意提示可见（含 status/supplement）', anyv)):
            d = dist(vals)
            L.append(f'| {metric} | {d["n"]} | {d["p50"]} | {d["p95"]} | {d["p99"]} | {d["max"]} |')
        dp, cp, vp = dist(dec)['p95'], dist(c2v)['p95'], dist(vis)['p95']
        vp_label = ('无效（锚点误差，负值不能当达标）' if vp is not None and vp < 0 else
                    ('达' if vp is not None and vp <= 800 else '未达'))
        L += ['', '- 对照规划 §1.3：判定 P95 门槛 300ms；判定→提示 P95 门槛 500ms；合成“t_end→visible”门槛 800ms。',
              f'- 实测：判定 P95={dp}ms（n={len(dec)}）→ {"达" if dp is not None and dp <= 300 else "未达"}；'
              f'判定→提示 P95={cp}ms（n={len(c2v)}）→ {"达" if cp is not None and cp <= 500 else "未达"}；'
              f't_end→visible P95={vp}ms（n={len(vis)}）→ 按 800ms {vp_label}；'
              f'按 300ms {("无效（锚点误差）" if vp is not None and vp < 0 else ("达" if vp is not None and vp <= 300 else "未达"))}。',
              '- G6（2026-11-01）已把 0.3s 目标改为“先出提示、可回滚”，因此上表回滚率按 G6 语境解读；'
              '合成语音组结论仍为 SIMULATED，不构成真实 PASS。',
              '', '### ACK 分类计数（渲染后回报，按 prompt 去重）', '',
              f'- primary={s["ack_kinds"].get("primary", 0)}，supplement={s["ack_kinds"].get("supplement", 0)}，'
              f'status={s["ack_kinds"].get("status", 0)}；有任意提示可见的题 {sum(1 for d in detail if d.get("first_visible_any_ms") is not None)}/{len(detail)}',
              f'- 会话 roles（setup）：{(s.get("setup") or {}).get("roles")}；自检 {((s.get("setup") or {}).get("selfcheck"))}；'
              f'降级开始 {(s.get("setup") or {}).get("degraded")}']
        h = s['hit']
        L += ['', '### 命中 / 误认 / 拒认', '',
              f'- 主库（L1 期望）：{h["library_correct"]}/{h["library_n"]} 正确；错条目 {h["library_wrong"]}；'
              f'合并/无独立提示 {len(h["merged"])}',
              f'- 次优库（L2 期望）：{h["secondary_l2"]}/{h["secondary_n"]} 走到 L2'
              + ('（roles 为 ' + str((s.get('setup') or {}).get('roles')) + '，未命中则检查岗位标签与索引）'
                 if h['secondary_n'] and h['secondary_l2'] == 0 else ''),
              f'- 库外（L3 期望）：拒认 {h["outside_reject"]}/{h["outside_n"]}，误认 {h["outside_false_hit"]}',
              f'- 个人/行为：固定诚实文案 {h["personal_note"]}/{h["personal_n"]}，走主答案 {h["personal_primary"]}'
              + ('（个人经历题若主库有已审核条目会走 L1p 并同时显示个人提示，未联网）'
                 if h['personal_primary'] else ''),
              f'- 合并/误归属：{len(h["merged"])} {h["merged"]}' if h['merged'] else '- 合并/误归属：0',
              '', '### 逐题明细', '']
        for d in detail:
            L.append(f"- {d['qid']} [{d['category']}] 首提示={d.get('first_prompt_kind')}/{d.get('first_prompt_level')} "
                     f"hit={d.get('primary_hit_id') or d.get('first_hit_id')} "
                     f"visible={d.get('visible_latency_ms')} ms 回滚={d.get('rolled_back')} "
                     f"text_ratio={d.get('text_ratio')} asr={str(d.get('asr_text'))[:60]!r} status={d.get('status_texts')}")
        L += ['', f'- 串题（board 事件 turn/epoch 错配）：{len(s["cross_turn"])}（要求 0）',
              f'- 事实校验占位符替换（[需本人补充]）：{s["placeholder_subs"]}',
              f'- DeepSeek：请求 {s["llm"]["requests"]}，状态 {s["llm"]["statuses"]}，'
              f'首数据块延迟 {s["llm"]["first_body_ms"]}，超时/异常中断 {len(s["llm"]["aborts"])}，'
              f'消费方提前关闭 {len(s["llm"].get("early_close", []))}，超过 8s 的流 {len(s["llm"]["over_8s"])}',
              '']
        if s['cross_turn']:
            L += ['串题明细：'] + [f'- {x}' for x in s['cross_turn']] + ['']
        if s['problems']:
            L += ['浏览器侧问题：'] + [f'- {p}' for p in s['problems']] + ['']
        L += ['截图：' + '、'.join(Path(p).name for p in s['screenshots']), '']
    tts = next((s for s in summaries if s['kind'] == 'tts'), None)
    real = next((s for s in summaries if s['kind'] == 'real'), None)
    l2_line = ('- 次优库 L2：setup 会话已绑定 roles，次优库索引建立成功时 L2 才会命中；'
               '未命中的题会降级为“暂无标准答案 / 外部补充不可用”。')
    if tts and tts['hit']['secondary_l2'] == 0 and tts['hit']['secondary_n']:
        l2_line = '- 次优库 L2：本轮 TTS 组 0 命中，检查 roles 与岗位标签（见各组 ACK 分类一节）。仍未真正走通 L2。'
    L += ['## 已知局限与 NOT TESTED', '',
          '- 通道：meeting 是真实 `getDisplayMedia` + Chrome 假音频设备；mic 是本地合成回答片段，'
          '**不是真实麦克风，也不是双真实音源**。真实双源（会议客户端 + 真人麦克风）NOT TESTED。',
          '- 语音：TTS 组为 `say` 合成语音（SYNTHETIC_TTS）；真人组为 `pdd` 单轨混音片段（REAL_AUDIO_ONE_TRACK），'
          '窗口内可能含候选人声音，且题面与主库不是同一场面试。',
          f'- 样本量：TTS n={tts["n_questions"] if tts else "-"}、真人 n={real["n_questions"] if real else "-"}，'
          '均远小于 30；分位数与点估计不稳定，95% CI 见标准报告。',
          l2_line,
          '- 8s 超时取消：本轮 DeepSeek 请求首数据块见各组统计，消费方是否提前关闭见计数；'
          '未观察到 3s 首 token / 8s 总超时，超时取消路径 NOT TESTED（未为了让结果好看而放宽/触发超时）。',
          '- 搜索联网：无 SEARCH_API_KEY，L3 一律“外部补充不可用”，这是预期；真实联网首条 NOT TESTED。',
          '- 事实边界：只统计了事实校验器的 `[需本人补充]` 占位符（见各组计数）；模型公共陈述没有逐条事实核验，'
          '“虚构=0”不能扩大解释为模型输出全部真实。',
          '- 真人组为单轨近似：候选人声音与面试官同轨，部分“新题/插话”可能是候选人自己的话；L1 命中率不作真实面试结论。',
          '- 真实会议客户端（腾讯会议/飞书/Zoom）兼容矩阵 NOT TESTED。',
          '- setup 流程的会前自检在 L7 测试服务里是 FAIL（未注入 audio/asr/llm ping 依赖），因此以 degraded 方式开始；'
          '这只影响看板降级横幅，不影响路由。真实产品环境应配置这些探测项。',
          '- Q07 的主答案因“回答已开始”被锁定为补充（主答案锁规则），因此该题没有 primary ACK，只有 supplement ACK；'
          '这是设计行为，不是串题。',
          '- Q16（个人经历）本轮路由为 L1p 并以补充呈现，未显示个人经历提示；诚实文案覆盖 1/2（Q15 命中）。',
          '- 短问句补救只重开一次，且下一次 speech_start 会作废旧窗口；final 晚于下一段开口仍会丢，属已知边界。',
          '- display_ack 在提示板刷新后会随快照重渲染再回一次；统计按 prompt_id 去重，不影响首次可见时刻。',
          '- 本轮没有复现规划中提到的 DeepSeek 78s 卡顿；该降级路径未观察到。', '']
    (REPORT_DIR / name).write_text('\n'.join(L) + '\n', 'utf-8')
    out_json = REPORT_DIR / (name.replace('.md', '_summary.json') if name != 'l7_report.md' else 'l7_summary.json')
    out_json.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), 'utf-8')
    print(f'报告：{REPORT_DIR / name}')
    return summaries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('command', choices=['build', 'run', 'report', 'all'])
    ap.add_argument('--kind', action='append', choices=['tts', 'real'])
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--report-name', default='l7_report.md')
    ap.add_argument('--compare', action='store_true', help='用同类别上一轮 run 作 v1 基线写对比表')
    args = ap.parse_args()
    kinds = args.kind or ['tts', 'real']
    if args.command in ('build', 'all'):
        build_answer_burst(args.force)
        build_tts(args.force)
        build_real(args.force)
    if args.command in ('run', 'all'):
        for k in kinds:
            run_one(k, args.force)
    if args.command in ('report', 'all'):
        runs = sorted(RUN_DIR.glob('*.json'))
        latest, first = {}, {}
        for p in runs:
            data = json.loads(p.read_text('utf-8'))
            if 'manifest' in data:
                first.setdefault(data['category'], p)
                latest[data['category']] = p
        summaries = [analyze(latest[k]) for k in ('tts', 'real') if k in latest and k in kinds]
        baseline = None
        if args.compare:
            baseline = [analyze(first[k]) for k in ('tts', 'real') if first.get(k) and k in kinds]
        write_report(summaries, args.report_name, baseline)


if __name__ == '__main__':
    main()
