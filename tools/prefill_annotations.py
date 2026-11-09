"""为人工校对预填"提问结束点"候选（仅启发式提示，全部 verified:false，不是真值）。

读 <audio_id>.draft.json（能量 VAD 切段）+ reports/real/<audio_id>.events.jsonl（ASR 事件），
写 eval/annotations/<audio_id>.review.json 与 .review.md。含转写文本，属个人数据，只留在 eval/annotations/。

用法: .venv/bin/python tools/prefill_annotations.py <audio_id> [--ann-dir eval/annotations]
        [--events eval/reports/real/<id>.events.jsonl] [--min N] [--max N]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.domain.semantic import classify  # noqa: E402

TAIL_SLACK_MS = 400  # 句子 e_end 允许比 VAD 段末晚多少仍归入该段
LONG_ANSWER_MS = 12000  # 超过此时长的段视为长回答
LONG_ANSWER_CHARS = 90
SHORT_MS = 1500  # 难例：很短的片段
FOLLOWUP_GAP_MS = 2500  # 难例：紧接着上一个候选之后的短句，疑似追问
FOLLOWUP_MAX_CHARS = 20
TEXT_MAX = 80


def load_events(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def sentences(events):
    """每个句子起点 s 取最后到达的一条（final 优先于 partial 的后续版本）。"""
    last = {}
    for e in events:
        last[e['s']] = e
    return sorted(last.values(), key=lambda e: e['s'])


def seg_text(sents, start, end):
    """归入该段的句子：e_end 落在 (start, end+TAIL_SLACK_MS]。返回 (全文, 是否有 final)。"""
    hit = [e for e in sents if start < e['e_end'] <= end + TAIL_SLACK_MS]
    return ''.join(e['text'] for e in hit), any(e['final'] for e in hit)


def clip(text, n=TEXT_MAX):
    return text if len(text) <= n else '…' + text[-(n - 1):]  # 保留句尾（提问结束点看句尾）


def guess(text, dur_ms):
    """返回 (guess_role, guess_kind, confidence)。"""
    kind = classify(text) if text.strip() else 'backchannel'
    n = len(text)
    if dur_ms > LONG_ANSWER_MS or n > LONG_ANSWER_CHARS:
        return 'answerer', kind, 0.7 if kind == 'statement' else 0.5
    if kind in ('question', 'request'):
        return 'questioner', kind, 0.8 if n <= 40 else 0.6
    if not text.strip() or kind in ('backchannel', 'process'):
        return 'unknown', kind, 0.3
    return ('answerer' if n > 40 else 'unknown'), kind, 0.5 if n > 40 else 0.3


def build_candidates(draft, events, lo=60, hi=120):
    sents = sentences(events)
    rows = []
    for s in draft['segments']:
        st, en = s['start_ms'], s['end_ms']
        text, _ = seg_text(sents, st, en)
        role, kind, conf = guess(text, en - st)
        rows.append(dict(seg=s, text=text, role=role, kind=kind, conf=conf, dur=en - st))
    picked, prev_q_end = {}, None
    for i, r in enumerate(rows):
        st, en = r['seg']['start_ms'], r['seg']['end_ms']
        if r['role'] == 'questioner':
            picked[i] = '主候选'
            prev_q_end = en
            continue
        if r['role'] == 'answerer' or not r['text'].strip():
            continue
        if r['dur'] <= SHORT_MS and r['kind'] not in ('backchannel', 'process'):
            picked[i] = '难例:很短片段'
        elif (prev_q_end is not None and st - prev_q_end <= FOLLOWUP_GAP_MS
              and len(r['text']) <= FOLLOWUP_MAX_CHARS and r['kind'] not in ('backchannel', 'process')):
            picked[i] = '难例:疑似追问'
    idx = sorted(picked)
    if len(idx) > hi:  # 超量：按置信度丢弃最低的，主候选优先保留
        idx = sorted(sorted(idx, key=lambda i: (picked[i] != '主候选', -rows[i]['conf'], i))[:hi])
    if len(idx) < lo:  # 不足：补最短的非回答、有文本的 statement 段作难例
        rest = [i for i, r in enumerate(rows) if i not in picked and r['role'] != 'answerer' and r['text'].strip()
                and r['kind'] not in ('backchannel', 'process')]
        rest.sort(key=lambda i: rows[i]['dur'])
        for i in rest[:lo - len(idx)]:
            picked[i] = '难例:补足'
        idx = sorted(picked)
    items = []
    for n, i in enumerate(idx):
        r = rows[i]
        s = r['seg']
        items.append(dict(id=n + 1, t_end_ms=s['end_ms'], vad_seg=dict(id=s['id'], start_ms=s['start_ms'], end_ms=s['end_ms']),
                          text_final=clip(r['text']), guess_role=r['role'], guess_kind=r['kind'],
                          confidence=r['conf'], pick_reason=picked[i], verified=False,
                          human=dict(is_question_end=None, role=None, is_topic_change=None, note='')))
    return items


def fmt_time(ms):
    s = ms / 1000
    return f'{int(s // 60):02d}:{s % 60:04.1f}'


GUESS_ZH = {'questioner': '面试官?', 'answerer': '候选人?', 'unknown': '未知'}
BOXES = '[ ] 是问题结束点 [ ] 不是 [ ] 追问 [ ] 改题'

HEADER = """# {aid} 提问点校对清单（预填，待人工核对；全部 verified:false）

## 怎么校对（每条约 10 秒）
1. 用任意播放器打开 `{wav}`，跳到该条时间点**前 5 秒**开始听。
2. 判断：**该时间点是否是面试官一个完整问题刚说完**？勾选对应方框：
   - `是问题结束点`：面试官刚问完一个完整问题。
   - `不是`：回答、半句话、附和、问题还没说完、说话人不是面试官。
   - `追问`：与「是问题结束点」同勾，表示这是对上一题的追问/补充。
   - `改题`：与「是问题结束点」同勾，表示换了新话题。
   - 同一条不要同时勾「是」和「不是」。不确定就留空，该条不进评测分母。
3. 「猜测」列只是启发式提示（文本像不像问句、段落长短），不是真值；单轨混音没有通道分离，说话人必须靠你听。

## 回填方式
- 方式 A：直接在本文件的 `[ ]` 里改成 `[x]`，然后运行
  `.venv/bin/python tools/import_review_md.py {aid}`，它会更新 `{aid}.review.json` 的 human 与 verified。
- 方式 B：直接编辑 `{aid}.review.json` 每条的 `human` 字段并把 `verified` 设为 true。
- 只有勾选了「是」或「不是」的条目才会变成 verified:true。

序号 | mm:ss.t | 文本 | 猜测 | 勾选
--- | --- | --- | --- | ---
"""


def render_md(aid, wav, items):
    lines = [f'{it["id"]} | {fmt_time(it["t_end_ms"])} | {it["text_final"].replace("|", "｜")} | '
             f'{GUESS_ZH[it["guess_role"]]}/{it["guess_kind"]} | {BOXES}' for it in items]
    return HEADER.format(aid=aid, wav=wav) + '\n'.join(lines) + '\n'


def prefill(audio_id, ann_dir='eval/annotations', events_path=None, lo=60, hi=120):
    ann = Path(ann_dir)
    draft = json.loads((ann / f'{audio_id}.draft.json').read_text(encoding='utf-8'))
    events = load_events(events_path or ROOT / 'eval/reports/real' / f'{audio_id}.events.jsonl')
    items = build_candidates(draft, events, lo, hi)
    doc = dict(audio_id=audio_id, source_wav=draft.get('source_wav'), duration_ms=draft.get('duration_ms'),
               note='预填候选，全部 verified:false，不是真值；t_end_ms 为能量 VAD 段末', items=items)
    (ann / f'{audio_id}.review.json').write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
    (ann / f'{audio_id}.review.md').write_text(render_md(audio_id, draft.get('source_wav'), items), encoding='utf-8')
    return items


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('audio_id')
    ap.add_argument('--ann-dir', default='eval/annotations')
    ap.add_argument('--events')
    ap.add_argument('--min', type=int, default=60)
    ap.add_argument('--max', type=int, default=120)
    a = ap.parse_args()
    its = prefill(a.audio_id, a.ann_dir, a.events, a.min, a.max)
    from collections import Counter
    print('total', len(its))
    for k in ('guess_role', 'guess_kind', 'pick_reason'):
        print(k, dict(Counter(i[k] for i in its)))
