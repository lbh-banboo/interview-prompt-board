"""提问点校对台 API：白名单/路径穿越、Range、写回语义与 import_review_md 一致、未判定计数。"""
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import annotations
from tools import import_review_md

H = {'x-local-token': 'tok'}
BOXES = ['是问题结束点', '不是', '追问', '改题']


def item(i):
    return dict(id=i, t_end_ms=1000 * i, text_final=f'句{i}？', guess_role='questioner', guess_kind='question',
                confidence=0.5, verified=False,
                human=dict(is_question_end=None, role=None, is_topic_change=None, note=''))


def md_row(i, box):
    boxes = ' '.join(f'[{"x" if name == box else " "}] {name}' for name in BOXES)
    return f'{i} | 00:0{i}.0 | 文本 | 猜测 | {boxes}'


def make(tmp_path, n=3):
    ann = tmp_path / 'annotations'
    ann.mkdir()
    audio = tmp_path / 'audio16k'
    audio.mkdir()
    doc = dict(audio_id='demo', duration_ms=60000, items=[item(i) for i in range(1, n + 1)])
    (ann / 'demo.review.json').write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    (ann / 'demo.draft.json').write_text('{}', encoding='utf-8')  # 无 .review.json 的草稿不应出现在列表
    (audio / 'demo.wav').write_bytes(b'RIFF' + bytes(range(256)) * 4)
    app = FastAPI()
    annotations.register(app, SimpleNamespace(
        store=None, root=tmp_path, token='tok',
        config={'annotations_dir': str(ann), 'audio16k_dir': str(audio)}))
    return TestClient(app), ann


def stored(ann):
    return json.loads((ann / 'demo.review.json').read_text(encoding='utf-8'))


def test_list_only_review_json_and_counts_unjudged(tmp_path):
    c, _ = make(tmp_path, n=3)
    rows = c.get('/api/annotations').json()
    assert [r['audio_id'] for r in rows] == ['demo']
    assert (rows[0]['total'], rows[0]['judged'], rows[0]['pct'], rows[0]['has_audio']) == (3, 0, 0, True)
    doc = c.get('/api/annotations/demo').json()
    assert [it['id'] for it in doc['items']] == [1, 2, 3]
    assert doc['progress'] == dict(total=3, judged=0, pct=0)


def test_put_yes_matches_import_semantics(tmp_path):
    c, ann = make(tmp_path, n=3)
    r = c.put('/api/annotations/demo/items/1', json={'verdict': 'yes', 'note': '完整问题'}, headers=H)
    assert r.status_code == 200
    it = r.json()['item']
    assert it['verified'] is True
    assert (it['human']['is_question_end'], it['human']['role']) == (True, 'questioner')
    assert (it['human']['is_topic_change'], it['human']['is_followup'], it['human']['note']) == (False, False, '完整问题')
    assert r.json()['progress'] == dict(total=3, judged=1, pct=33)
    assert stored(ann)['items'][0]['verified'] is True


def test_put_no_clears_topic_and_followup(tmp_path):
    c, _ = make(tmp_path)
    it = c.put('/api/annotations/demo/items/1', json={'verdict': 'no'}, headers=H).json()['item']
    assert it['human']['is_question_end'] is False
    assert it['human']['is_topic_change'] is None and it['human']['is_followup'] is False
    assert it['human']['role'] is None  # 与 import_review_md 一致：不是问题结束点不改 role
    assert it['verified'] is True


def test_put_followup_and_topic_change_imply_yes(tmp_path):
    c, _ = make(tmp_path)
    f = c.put('/api/annotations/demo/items/1', json={'verdict': 'followup'}, headers=H).json()['item']
    assert (f['human']['is_question_end'], f['human']['is_followup'], f['human']['is_topic_change']) == (True, True, False)
    t = c.put('/api/annotations/demo/items/2', json={'verdict': 'topic_change'}, headers=H).json()['item']
    assert (t['human']['is_question_end'], t['human']['is_followup'], t['human']['is_topic_change']) == (True, False, True)


def test_writeback_identical_to_import_review_md(tmp_path):
    """同一批判定：API 写回与 .review.md 导入（apply）得到完全相同的 human/verified。"""
    c, ann = make(tmp_path, n=3)
    cases = {1: ('yes', '是问题结束点'), 2: ('no', '不是'), 3: ('topic_change', '改题')}
    for n, (verdict, _) in cases.items():
        assert c.put(f'/api/annotations/demo/items/{n}', json={'verdict': verdict}, headers=H).status_code == 200
    api_doc = stored(ann)

    md = json.loads((ann / 'demo.review.json').read_text(encoding='utf-8'))
    md['items'] = [item(i) for i in range(1, 4)]
    text = '\n'.join(md_row(n, box) for n, (_, box) in cases.items())
    done, conflicts, blank = import_review_md.apply(md, import_review_md.parse_md(text))
    assert (done, conflicts, blank) == (3, [], 0)
    for a, b in zip(api_doc['items'], md['items']):
        assert a['verified'] == b['verified']
        assert a['human'] == b['human']


def test_unjudged_item_stays_out_of_judged_count(tmp_path):
    c, ann = make(tmp_path, n=3)
    c.put('/api/annotations/demo/items/1', json={'verdict': 'yes'}, headers=H)
    r = c.put('/api/annotations/demo/items/2', json={'verdict': None, 'note': '听不清'}, headers=H)
    assert r.status_code == 200
    assert r.json()['item']['verified'] is False and r.json()['item']['human']['is_question_end'] is None
    assert r.json()['progress'] == dict(total=3, judged=1, pct=33)
    assert stored(ann)['items'][1]['human']['note'] == '听不清'


def test_reject_unknown_verdict_and_missing_item(tmp_path):
    c, _ = make(tmp_path)
    assert c.put('/api/annotations/demo/items/1', json={'verdict': 'maybe'}, headers=H).status_code == 400
    assert c.put('/api/annotations/demo/items/99', json={'verdict': 'yes'}, headers=H).status_code == 404


def test_put_requires_local_token(tmp_path):
    c, _ = make(tmp_path)
    assert c.put('/api/annotations/demo/items/1', json={'verdict': 'yes'}).status_code == 403


@pytest.mark.parametrize('bad', ['..', '../secret', '..%2F..%2Fsecret', 'demo/../../secret', '/etc/passwd'])
def test_path_traversal_rejected(tmp_path, bad):
    c, ann = make(tmp_path)
    (tmp_path / 'secret.review.json').write_text(json.dumps(dict(items=[item(1)])), encoding='utf-8')
    assert c.get(f'/api/annotations/{bad}').status_code == 404
    assert c.get(f'/api/annotations/{bad}/audio').status_code == 404
    assert c.put(f'/api/annotations/{bad}/items/1', json={'verdict': 'yes'}, headers=H).status_code == 404
    assert stored(ann)['items'][0]['verified'] is False  # 目录外文件未被写入


def test_audio_range(tmp_path):
    c, _ = make(tmp_path)
    size = (tmp_path / 'audio16k' / 'demo.wav').stat().st_size
    full = c.get('/api/annotations/demo/audio')
    assert full.status_code == 200 and full.content[:4] == b'RIFF'
    assert full.headers['content-type'].startswith('audio/wav')
    assert full.headers['accept-ranges'] == 'bytes'

    part = c.get('/api/annotations/demo/audio', headers={'Range': 'bytes=100-199'})
    assert part.status_code == 206
    assert len(part.content) == 100
    assert part.headers['content-range'] == f'bytes 100-199/{size}'

    bad = c.get('/api/annotations/demo/audio', headers={'Range': f'bytes={size + 10}-'})
    assert bad.status_code == 416


def test_missing_wav_returns_404(tmp_path):
    c, ann = make(tmp_path)
    (tmp_path / 'audio16k' / 'demo.wav').unlink()
    assert c.get('/api/annotations/demo/audio').status_code == 404
    assert c.get('/api/annotations').json()[0]['has_audio'] is False
