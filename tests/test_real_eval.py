"""真人录音评测工具链测试：全部使用合成夹具，不含任何真实个人数据。"""
import json
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))

import import_review_md as imp  # noqa: E402
import prefill_annotations as pre  # noqa: E402
import replay_turns_real as rp  # noqa: E402
from app.domain.turn_detector import TurnEvent  # noqa: E402

RATE = 16000
Q1 = '你能讲讲分布式事务的区别吗'
Q2 = '为什么选择这个方案呢'
STMT = '我们公司主要做电商业务'


def tone(ms):
    x = np.arange(RATE * ms // 1000) / RATE
    return (9000 * np.sin(2 * np.pi * 200 * x)).astype('<i2')


def silence(ms):
    return np.zeros(RATE * ms // 1000, '<i2')


# 时间轴(ms): Q1 语音 300-2300；回答 3800-5300(非提问点)；Q2 语音 7000-8500；尾部静音至 10500
@pytest.fixture
def fx(tmp_path):
    pcm = np.concatenate([silence(300), tone(2000), silence(1500), tone(1500), silence(1700), tone(1500), silence(2000)])
    wav = tmp_path / 'a.wav'
    with wave.open(str(wav), 'wb') as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    events = [
        dict(arr_ms=2200, text=Q1[:6], final=False, ver=1, cov=2200, s=300, e_end=2200),
        dict(arr_ms=2400, text=Q1, final=True, ver=2, cov=2300, s=300, e_end=2300),
        dict(arr_ms=5400, text=STMT, final=True, ver=1, cov=5300, s=3800, e_end=5300),
        dict(arr_ms=8400, text=Q2[:5], final=False, ver=1, cov=8400, s=7000, e_end=8400),
        dict(arr_ms=8600, text=Q2, final=True, ver=2, cov=8500, s=7000, e_end=8500),
    ]
    ev = tmp_path / 'a.events.jsonl'
    ev.write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in events), encoding='utf-8')
    draft = dict(audio_id='a', source_wav='a.wav', duration_ms=10500, segments=[
        dict(id=0, start_ms=300, end_ms=2300), dict(id=1, start_ms=3800, end_ms=5300), dict(id=2, start_ms=7000, end_ms=8500)])
    (tmp_path / 'a.draft.json').write_text(json.dumps(draft), encoding='utf-8')
    return tmp_path, wav, ev


def item(i, t_end, start, verified, qend):
    return dict(id=i, t_end_ms=t_end, vad_seg=dict(id=i, start_ms=start, end_ms=t_end), text_final='x', guess_role='questioner',
                guess_kind='question', confidence=0.8, verified=verified,
                human=dict(is_question_end=qend, role=None, is_topic_change=None, note=''))


def write_review(tmp, items):
    p = tmp / 'a.review.json'
    p.write_text(json.dumps(dict(audio_id='a', items=items)), encoding='utf-8')
    return p


# ---- 预填 ----
def test_prefill_structure(fx):
    tmp, _, ev = fx
    items = pre.prefill('a', str(tmp), ev, lo=0, hi=120)
    assert [i['t_end_ms'] for i in items] == [2300, 5300, 8500]
    assert items[1]['pick_reason'].startswith('难例') and items[1]['guess_role'] != 'questioner'  # 1.5s 短片段只作难例
    for i in items:
        assert set(i) >= {'id', 't_end_ms', 'vad_seg', 'text_final', 'guess_role', 'guess_kind', 'confidence', 'verified', 'human'}
        assert i['verified'] is False and set(i['human']) == {'is_question_end', 'role', 'is_topic_change', 'note'}
        assert all(v in (None, '') for v in i['human'].values())
        assert len(i['text_final']) <= 80
    assert items[0]['text_final'] == Q1 and items[0]['guess_role'] == 'questioner' and items[0]['guess_kind'] == 'question'
    md = (tmp / 'a.review.md').read_text(encoding='utf-8')
    assert '前 5 秒' in md and '00:02.3' in md and '[ ] 是问题结束点 [ ] 不是 [ ] 追问 [ ] 改题' in md
    assert json.loads((tmp / 'a.review.json').read_text(encoding='utf-8'))['items'][0]['verified'] is False


def test_prefill_truncates_and_caps():
    assert len(pre.clip('字' * 200)) == 80 and pre.clip('字' * 200).endswith('字')
    sents = [dict(s=i * 1000, text='你能讲讲分布式事务吗', final=True, e_end=i * 1000 + 900) for i in range(10)]
    draft = dict(segments=[dict(id=i, start_ms=i * 1000, end_ms=i * 1000 + 900) for i in range(10)])
    assert len(pre.build_candidates(draft, sents, lo=0, hi=4)) == 4


# ---- 导入 ----
def test_import_roundtrip(fx):
    tmp, _, ev = fx
    pre.prefill('a', str(tmp), ev, lo=0, hi=120)
    md_path = tmp / 'a.review.md'
    lines = md_path.read_text(encoding='utf-8').splitlines()
    out = []
    for ln in lines:
        if ln.startswith('1 |'):
            ln = ln.replace('[ ] 是问题结束点', '[x] 是问题结束点').replace('[ ] 改题', '[x] 改题')
        elif ln.startswith('2 |'):
            ln = ln.replace('[ ] 不是', '[X] 不是')
        out.append(ln)
    md_path.write_text('\n'.join(out), encoding='utf-8')
    doc = json.loads((tmp / 'a.review.json').read_text(encoding='utf-8'))
    done, conflicts, blank = imp.apply(doc, imp.parse_md(md_path.read_text(encoding='utf-8')))
    assert (done, conflicts, blank) == (2, [], 1)
    a, b, _ = doc['items']
    assert a['verified'] and a['human']['is_question_end'] is True and a['human']['is_topic_change'] is True
    assert a['human']['role'] == 'questioner'
    assert b['verified'] and b['human']['is_question_end'] is False


def test_import_conflict_and_blank():
    doc = dict(items=[item(1, 1, 0, False, None), item(2, 2, 0, False, None), item(3, 3, 0, False, None)])
    box = '[{}] 是问题结束点 [{}] 不是 [ ] 追问 [ ] 改题'
    md = f'1 | 00:01.0 | t | g | {box.format("x", "x")}\n2 | 00:02.0 | t | g | {box.format(" ", " ")}\n3 | 00:03.0 | t | g | [ ] 是问题结束点 [ ] 不是 [x] 追问 [ ] 改题\n'
    done, conflicts, blank = imp.apply(doc, imp.parse_md(md))
    assert (done, conflicts, blank) == (1, [1], 1)
    assert [i['verified'] for i in doc['items']] == [False, False, True]
    assert doc['items'][2]['human']['is_question_end'] is True and doc['items'][2]['human']['is_followup'] is True


# ---- 回放 ----
def run_fx(tmp, wav, ev, items, **kw):
    rev = write_review(tmp, items)
    return rp.run(wav, ev, rev, vad_kind='energy', out_dir=tmp / 'out', **kw)


def test_replay_exact_latency_and_false_commit(fx):
    tmp, wav, ev = fx
    items = [item(1, 2300, 300, True, True), item(2, 5300, 3800, True, False), item(3, 8500, 7000, True, True)]
    per, fc, base = run_fx(tmp, wav, ev, items)
    # 能量 VAD 在 t_end 后约 100ms 发 speech_end，T_silence=200，tick 以 20ms 帧对齐 -> 提交在 t_end 之后 200~220ms
    q1, q2 = per
    assert q1['t_end'] == 2300 and 2300 + 200 <= q1['t_committed'] <= 2300 + 220
    assert q2['t_end'] == 8500 and 8500 + 200 <= q2['t_committed'] <= 8500 + 220
    assert q1['premature'] == 0 and q2['premature'] == 0
    assert fc['total_commits'] == 2 and fc['false_commits'] == 0  # 陈述句不提交
    text = Path(str(base) + '.md').read_text(encoding='utf-8')
    assert '真人单轨近似' in text and 'n=2' in text and 'PROVISIONAL' in text


def test_replay_false_commit_counted(fx):
    tmp, wav, ev = fx
    # 把"陈述"段的文本换成问句 -> 在已核实的负例点产生误 commit
    lines = [json.loads(x) for x in ev.read_text(encoding='utf-8').splitlines()]
    lines[2]['text'] = Q1
    ev.write_text('\n'.join(json.dumps(x, ensure_ascii=False) for x in lines), encoding='utf-8')
    items = [item(1, 2300, 300, True, True), item(2, 5300, 3800, True, False)]
    per, fc, _ = run_fx(tmp, wav, ev, items)
    assert fc['false_commits'] == 1 and len(per) == 1


def test_replay_unverified_excluded_and_counted(fx):
    tmp, wav, ev = fx
    items = [item(1, 2300, 300, True, True), item(2, 8500, 7000, False, None)]
    per, fc, base = run_fx(tmp, wav, ev, items)
    assert len(per) == 1
    text = Path(str(base) + '.md').read_text(encoding='utf-8')
    assert '未校对排除 1' in text and '未校对条目排除: 1' in text and 'UNVERIFIED-DEBUG' not in text


def test_replay_debug_flag_labels_output(fx):
    tmp, wav, ev = fx
    items = [item(1, 2300, 300, False, None)]
    per, _, base = run_fx(tmp, wav, ev, items, allow_unverified=True)
    assert len(per) == 1 and 'UNVERIFIED-DEBUG' in str(base)
    assert 'UNVERIFIED-DEBUG' in Path(str(base) + '.md').read_text(encoding='utf-8')


def ce(t_ms, te):
    return TurnEvent('committed', 'q', 1, t_ms, 'r', t_speech_end_ms=te)


def test_miss_window_boundary():
    pos = [item(1, 10000, 9000, True, True)]
    per, _ = rp.evaluate([ce(13000, 10000)], pos, [])
    assert per[0]['t_committed'] == 13000  # 恰好 3000ms 命中
    per, _ = rp.evaluate([ce(13001, 10000)], pos, [])
    assert per[0]['t_committed'] is None  # 3001ms 漏判
    per, _ = rp.evaluate([ce(9999, 9900)], pos, [])
    assert per[0]['t_committed'] is None  # t_end 之前的 commit 不算命中


def test_premature_counts_rollbacks_of_same_commit():
    pos = [item(1, 10000, 9000, True, True)]
    hist = [ce(10200, 10000), TurnEvent('rolled_back', 'q', 2, 10500, 'r', t_speech_end_ms=10000),
            TurnEvent('rolled_back', 'q', 3, 10600, 'r', t_speech_end_ms=77),
            TurnEvent('withdrawn', 'q', 1, 9500, 'r', t_speech_end_ms=9400)]
    per, _ = rp.evaluate(hist, pos, [])
    assert per[0]['premature'] == 1 and per[0]['withdrawn'] == 1


def test_trigger_vad_unsupported_exits(monkeypatch):
    class Old:
        def __init__(self, T_silence=200):
            pass
    monkeypatch.setattr(rp, 'TurnDetector', Old)
    with pytest.raises(SystemExit):
        rp.make_detector('vad')
    assert isinstance(rp.make_detector('coverage'), Old)
