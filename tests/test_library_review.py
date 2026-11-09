import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import library_enhance, library_review
from app.core import Store

H = {'x-local-token': 'tok'}


def text(n, flagged=()):
    return '\n'.join(f"**H1-{i:02d}｜00:0{i % 10}｜问题{i}？** 参考回答：答案{i}" + (' [待核]' if i in flagged else '') for i in range(1, n + 1))


def make(tmp_path, n=4, flagged=(2,), config=None):
    store = Store(tmp_path)
    lib = store.import_text('样例', text(n, flagged))['id']
    app = FastAPI()
    library_review.register(app, SimpleNamespace(store=store, root=tmp_path, token='tok', bus=None, config=config or {}))
    return TestClient(app), store, lib


def test_batch_rejects_flagged_whole_batch(tmp_path):
    c, store, lib = make(tmp_path)
    r = c.post(f'/api/libraries/{lib}/approve-batch', json=dict(ids=['H1-01', 'H1-02', 'H1-99'], confirmed=True), headers=H)
    assert r.status_code == 409
    body = r.json()
    assert {x['id'] for x in body['rejected']} == {'H1-02', 'H1-99'}
    assert '含待核标记' in next(x for x in body['rejected'] if x['id'] == 'H1-02')['reasons']
    assert all(e['status'] == 'pending' for e in store.entries(lib))  # 无部分写入


def test_batch_rejects_flags_field(tmp_path):
    c, store, lib = make(tmp_path)
    with store.connect() as db:
        row = db.execute("SELECT payload FROM entries WHERE id='H1-01'").fetchone()
        p = json.loads(row['payload']) | dict(flags=['conflict'])
        db.execute("UPDATE entries SET payload=? WHERE id='H1-01'", (json.dumps(p),))
    r = c.post(f'/api/libraries/{lib}/approve-batch', json=dict(ids=['H1-01'], confirmed=True), headers=H)
    assert r.status_code == 409 and '标记：事实冲突' in r.json()['rejected'][0]['reasons']


def test_batch_success(tmp_path):
    c, store, lib = make(tmp_path)
    r = c.post(f'/api/libraries/{lib}/approve-batch', json=dict(ids=['H1-01', 'H1-03'], confirmed=True), headers=H)
    assert r.status_code == 200 and r.json()['approved'] == 2
    st = {e['id']: e for e in store.entries(lib)}
    assert st['H1-01']['status'] == 'approved' and st['H1-01']['approved_answer'] == '答案1'
    assert st['H1-04']['status'] == 'pending'


def test_batch_needs_confirmed_and_ids(tmp_path):
    c, _, lib = make(tmp_path)
    assert c.post(f'/api/libraries/{lib}/approve-batch', json=dict(ids=['H1-01'], confirmed=False), headers=H).status_code == 400
    assert c.post(f'/api/libraries/{lib}/approve-batch', json=dict(ids=[], confirmed=True), headers=H).status_code == 400


def test_token_required(tmp_path):
    c, store, lib = make(tmp_path)
    body = dict(ids=['H1-01'], confirmed=True)
    assert c.post(f'/api/libraries/{lib}/approve-batch', json=body).status_code == 403
    assert c.post(f'/api/libraries/{lib}/approve-batch', json=body, headers={'x-local-token': 'bad'}).status_code == 403
    assert store.entries(lib)[0]['status'] == 'pending'


def test_unknown_library_404(tmp_path):
    c, _, _ = make(tmp_path)
    assert c.get('/api/libraries/nope/progress').status_code == 404


def test_progress_counts_and_g3(tmp_path):
    c, store, lib = make(tmp_path, n=52, flagged=(2,))
    p = c.get(f'/api/libraries/{lib}/progress').json()
    assert (p['total'], p['approved'], p['flagged_pending'], p['g3_ready']) == (52, 0, 1, False)
    ids = [f'H1-{i:02d}' for i in range(1, 52) if i != 2]
    assert len(ids) == 50
    assert c.post(f'/api/libraries/{lib}/approve-batch', json=dict(ids=ids[:49], confirmed=True), headers=H).status_code == 200
    p = c.get(f'/api/libraries/{lib}/progress').json()
    assert p['approved'] == 49 and not p['g3_ready'] and '49/50' in p['hint']
    c.post(f'/api/libraries/{lib}/approve-batch', json=dict(ids=ids[49:], confirmed=True), headers=H)
    store.review(lib, 'H1-02', '练习稿 [待核]', 'rejected', False)
    p = c.get(f'/api/libraries/{lib}/progress').json()
    assert p['approved'] == 50 and p['g3_ready'] and p['rejected'] == 1 and p['reviewed'] == 51 and 'G3 可用' in p['hint']


def test_default_queue_unflagged_first(tmp_path):
    c, store, lib = make(tmp_path, flagged=(1,))
    store.review(lib, 'H1-03', '答案', 'approved', True)
    q = c.get(f'/api/libraries/{lib}/review-queue').json()
    assert [x['id'] for x in q] == ['H1-02', 'H1-04', 'H1-01']
    assert q[-1]['flagged'] and q[0]['n'] == 1


def test_queue_from_file_filters_unknown_and_reviewed(tmp_path):
    f = tmp_path / 'q.json'
    f.write_text(json.dumps([dict(n=1, id='H1-03', tier='A', why='x'), dict(n=2, id='ZZ', tier='A', why='x'), dict(n=3, id='H1-01', tier='B', why='y')]))
    c, store, lib = make(tmp_path / 'db', config=dict(review_queue_path=str(f)))
    store.review(lib, 'H1-01', '答案', 'approved', True)
    assert [x['id'] for x in c.get(f'/api/libraries/{lib}/review-queue').json()] == ['H1-03']


def test_queue_missing_file_falls_back(tmp_path):
    c, _, lib = make(tmp_path, config=dict(review_queue_path=str(tmp_path / 'none.json')))
    assert len(c.get(f'/api/libraries/{lib}/review-queue').json()) == 4


CONFLICT = ('**H1-01｜00:01｜介绍“星河”项目？** 参考回答：我负责“星河”项目，团队 8 人。\n'
            '**H1-02｜00:02｜介绍“星河”规模？** 参考回答：在“星河”项目里带过 12 人团队。\n'
            '**H1-03｜00:03｜怎么协作？** 参考回答：与团队同步。')


def make_conflict(tmp_path):
    store = Store(tmp_path)
    lib = store.import_text('冲突库', CONFLICT)['id']
    app = FastAPI()
    ctx = SimpleNamespace(store=store, root=tmp_path, token='tok', bus=None, config={})
    library_review.register(app, ctx)
    library_enhance.register(app, ctx)
    return TestClient(app), store, lib


def test_batch_hard_blocks_facts_conflict(tmp_path):
    c, store, lib = make_conflict(tmp_path)
    rows = {e['id']: e for e in c.get(f'/api/libraries/{lib}/review-entries').json()}
    assert rows['H1-01']['flags'] == ['conflict'] and not rows['H1-01']['can_approve']
    assert rows['H1-02']['conflict_group'] == '星河|#人'
    r = c.post(f'/api/libraries/{lib}/approve-batch', json=dict(ids=['H1-01', 'H1-03'], confirmed=True), headers=H)
    assert r.status_code == 409
    reasons = next(x for x in r.json()['rejected'] if x['id'] == 'H1-01')['reasons']
    assert any('事实冲突' in x for x in reasons)
    assert all(e['status'] == 'pending' for e in store.entries(lib))
    assert c.post(f'/api/libraries/{lib}/approve-batch', json=dict(ids=['H1-03'], confirmed=True), headers=H).status_code == 200


def test_single_approve_hard_blocks_facts_conflict(tmp_path):
    from app.server import create_app
    app = create_app(tmp_path / 'app', config=dict(enhancer=None))
    store = app.state.store
    lib = store.import_text('冲突库', CONFLICT)['id']
    c = TestClient(app)
    token = c.get('/api/status').json()['token']
    body = dict(answer='确认', status='approved', confirmed=True)
    r = c.post(f'/api/libraries/{lib}/entries/H1-01/review', json=body, headers={'X-Local-Token': token})
    assert r.status_code == 400 and '冲突' in r.json()['detail']
    assert c.post(f'/api/libraries/{lib}/entries/H1-03/review', json=body,
                  headers={'X-Local-Token': token}).status_code == 200


def edit_body(**over):
    data = dict(question='改后的问题？', variants=['a', 'b', 'c'],
                outline=dict(conclusion='改后结论', points=['1', '2', '3'], examples=['例'], cautions=['注意']),
                facts=[dict(kind='metric', text='8 人', source_span='团队 8 人')])
    data.update(over)
    return data


EDIT_TEXT = ('**H1-01｜00:01｜介绍“星河”项目？** 参考回答：我负责“星河”项目，团队 8 人。\n'
             '**H1-02｜00:02｜怎么协作？** 参考回答：与团队同步。')


def make_edit(tmp_path):
    store = Store(tmp_path)
    lib = store.import_text('编辑库', EDIT_TEXT)['id']
    app = FastAPI()
    ctx = SimpleNamespace(store=store, root=tmp_path, token='tok', bus=None, config={})
    library_review.register(app, ctx)
    library_enhance.register(app, ctx)
    return TestClient(app), store, lib


def test_edit_saves_fields_and_resets_approved_to_pending(tmp_path):
    c, store, lib = make_edit(tmp_path)
    assert c.post(f'/api/libraries/{lib}/approve-batch', json=dict(ids=['H1-01'], confirmed=True), headers=H).status_code == 200
    r = c.put(f'/api/libraries/{lib}/entries/H1-01', json=edit_body(), headers=H)
    assert r.status_code == 200 and r.json()['status'] == 'pending'
    e = next(x for x in c.get(f'/api/libraries/{lib}/review-entries').json() if x['id'] == 'H1-01')
    assert e['question'] == '改后的问题？' and e['status'] == 'pending' and e['approved_answer'] == ''
    assert e['enhancement']['question_variants'] == ['a', 'b', 'c']
    assert e['enhancement']['answer_outline']['conclusion'] == '改后结论'
    assert [(f['kind'], f['text'], f['origin'], f['flag']) for f in e['facts']] == [('metric', '8 人', 'manual', None)]
    assert e['flags'] == [] and e['can_approve']


def test_edit_keeps_fact_boundary_flags(tmp_path):
    c, store, lib = make_edit(tmp_path)
    body = edit_body(facts=[dict(kind='metric', text='团队 99 人', source_span='团队 99 人')])
    assert c.put(f'/api/libraries/{lib}/entries/H1-01', json=body, headers=H).status_code == 200
    e = next(x for x in c.get(f'/api/libraries/{lib}/review-entries').json() if x['id'] == 'H1-01')
    assert 'suspected_fabrication' in e['flags'] and not e['can_approve']


def test_edit_validation_and_token(tmp_path):
    c, store, lib = make_edit(tmp_path)
    url = f'/api/libraries/{lib}/entries/H1-01'
    assert c.put(url, json=edit_body()).status_code == 403
    assert c.put(url, json=edit_body(question='  '), headers=H).status_code == 400
    assert c.put(url, json=edit_body(variants=['a'] * 9), headers=H).status_code in (400, 422)
    assert c.put(url, json=edit_body(facts=[dict(kind='bogus', text='x')]), headers=H).status_code == 400
    assert c.put(url, json=edit_body(facts=[dict(kind='metric', text='x' * 501)]), headers=H).status_code in (400, 422)
    assert c.put(f'/api/libraries/{lib}/entries/H9-99', json=edit_body(), headers=H).status_code == 404
    assert c.put('/api/libraries/nope/entries/H1-01', json=edit_body(), headers=H).status_code == 404


def test_edit_blocked_while_session_active(tmp_path):
    c, store, lib = make_edit(tmp_path)
    store.review(lib, 'H1-01', '确认', 'approved', True)
    store.start('online', lib)
    assert c.put(f'/api/libraries/{lib}/entries/H1-01', json=edit_body(), headers=H).status_code == 409
