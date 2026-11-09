"""M12 记录与导出单测。全部为 SIMULATED 合成数据，不读真实库与录音。"""
import io
import json
import time
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import records as records_api
from app.core import Store
from app.records.bundle import write_bundle
from app.records.export import LATE_LABEL, export_markdown
from app.records.review_edit import Deleter, ReviewError, edit_segment
from app.records.store import RecordStore
from app.telemetry.metrics import Metrics

H = {'x-local-token': 'tok'}
SYS_TEXT = '系统建议专用文本ZZZ'
WAV = b'RIFF\x24\x00\x00\x00WAVEfmt ' + b'\x00' * 40


def make(tmp_path):
    core = Store(tmp_path)
    rs = RecordStore(tmp_path / 'records.sqlite3', main=core.connect, clock=lambda: 1000.0)
    return core, rs


def seed(core, rs, tmp_path, sid='s1'):
    with core.connect() as db:
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (sid, 'online', 'stopped', 'lib', 't1'))
        wav = tmp_path / 'meeting.wav'
        wav.write_bytes(WAV)
        db.execute("INSERT INTO recordings VALUES('r1',?,?,?,?,?)", (sid, 'meeting', 'audio/wav', 0, str(wav)))
        db.execute("INSERT INTO recordings VALUES('r2',?,?,?,?,?)", (sid, 'mic', 'audio/wav', 0, str(tmp_path / 'gone.wav')))
        db.execute("INSERT INTO recordings VALUES('r3',?,?,?,?,?)", (sid, 'mic', 'audio/wav', 1, str(tmp_path / 'bad.wav')))
        (tmp_path / 'bad.wav').write_bytes(b'junk')
        db.execute("INSERT INTO recording_gaps(session_id,source,t_start_ms,t_end_ms,reason) VALUES(?,?,?,?,?)",
                   (sid, 'mic', 1000, 2500, 'ws_buffer_full'))
    m = Metrics(core.connect())
    for i, n in enumerate(('t_end', 't_candidate', 't_committed', 't_visible')):
        m.mark(sid, 't1', n, 1_000_000_000 + i * 300_000_000)
    rs.append_segment(sid, 'meeting', 'questioner', '请介绍一下项目', True, 10.0, 12.0)
    rs.append_segment(sid, 'mic', 'answerer', '我负责了后端', True, 14.0, 16.0)
    rs.append_segment(sid, 'meeting', 'unknown', '嗯那个', True, 20.0, 21.0, flags=['unknown', 'overlap'])
    rs.record_turn_event(sid, 'committed', 't1', 1, t=11.0, text='请介绍一下项目', kind='question')
    rs.record_turn_event(sid, 'interrupted', 't1', 1)
    rs.record_turn_event(sid, 'committed', 't2', 2, t=18.0, text='追问细节', kind='followup')
    rs.record_prompt(sid, 't1', 1, 'primary', ['要点A', '要点B'], 'k1', 'primary_lib', level='L1')
    rs.record_prompt(sid, 't1', 1, 'supplement', SYS_TEXT, 'k2', 'web', ['https://e.com/x'])
    rs.record_prompt(sid, 't1', 1, 'supplement', '迟到内容', 'k3', 'web', state='late_archived')


def sections(md):
    out, cur = {}, None
    for line in md.splitlines():
        if line.startswith('## '):
            cur = line[3:]
            out[cur] = []
        elif cur:
            out[cur].append(line)
    return {k: '\n'.join(v) for k, v in out.items()}


def test_m12_t1_structure(tmp_path):
    core, rs = make(tmp_path)
    seed(core, rs, tmp_path)
    secs = sections(export_markdown(rs, 's1', tmp_path))
    assert list(secs)[:8] == ['会话信息', '现场实际发言（逐字稿）', '问答轮次', '系统建议（系统生成，不等于实际发言）',
                              '性能记录', '录音', '录音缺口', '验证状态']
    speech = secs['现场实际发言（逐字稿）']
    assert '面试官' in speech and '回答者' in speech and '[unknown] [overlap]' in speech and '[00:00]' in speech
    turns = secs['问答轮次']
    assert 't2（followup' in turns and '追问=是' in turns and '打断=1' in turns and '已回答' in turns and '未回答' in turns
    sysrec = secs['系统建议（系统生成，不等于实际发言）']
    assert '要点A；要点B' in sysrec and '<https://e.com/x>' in sysrec and '级别 L1' in sysrec
    assert '+300.0' in secs['性能记录'] and '—' in secs['性能记录']
    assert 'ws_buffer_full' in secs['录音缺口']
    assert 'NOT TESTED' in secs['验证状态']


def test_m12_t2_partition(tmp_path):
    core, rs = make(tmp_path)
    seed(core, rs, tmp_path)
    secs = sections(export_markdown(rs, 's1', tmp_path))
    speech = secs['现场实际发言（逐字稿）']
    for t in (SYS_TEXT, '要点A', '迟到内容'):
        assert t not in speech
        assert t not in secs['问答轮次']
    assert SYS_TEXT in secs['系统建议（系统生成，不等于实际发言）']


def test_late_archived_label(tmp_path):
    core, rs = make(tmp_path)
    seed(core, rs, tmp_path)
    secs = sections(export_markdown(rs, 's1', tmp_path))
    assert LATE_LABEL in secs['系统建议（系统生成，不等于实际发言）']
    assert rs.mark_late_archived('k2') and not rs.mark_late_archived('nope')
    assert export_markdown(rs, 's1', tmp_path).count(LATE_LABEL) == 2


def test_recordings_missing_corrupt_still_exports(tmp_path):
    core, rs = make(tmp_path)
    seed(core, rs, tmp_path)
    rec = sections(export_markdown(rs, 's1', tmp_path))['录音']
    assert 'meeting_0.wav（/api/recordings/r1）' in rec
    assert '录音缺失' in rec and '录音损坏' in rec


def test_no_recordings_and_empty_sections(tmp_path):
    _, rs = make(tmp_path)
    secs = sections(export_markdown(rs, 'none', tmp_path))
    assert '录音缺失：没有录音记录' in secs['录音']
    assert '（无逐字稿）' in secs['现场实际发言（逐字稿）'] and '（无轮次）' in secs['问答轮次']
    assert '（无系统建议）' in secs['系统建议（系统生成，不等于实际发言）'] and '无埋点' in secs['性能记录']
    assert '无缺口' in secs['录音缺口']


def test_no_main_db_and_relative_path(tmp_path):
    rs = RecordStore(tmp_path / 'r.sqlite3')
    assert 'NOT TESTED' in export_markdown(rs, 's')
    core, rs = make(tmp_path)
    (tmp_path / 'a.wav').write_bytes(WAV)
    with core.connect() as db:
        db.execute("INSERT INTO recordings VALUES('r1','s','meeting','audio/wav',0,'a.wav')")
        db.execute("INSERT INTO recordings VALUES('r2','s','mic','audio/wav',0,'')")
    rec = sections(export_markdown(rs, 's', tmp_path))['录音']
    assert '/api/recordings/r1' in rec and '录音缺失' in rec


def test_export_has_no_secret_fields(tmp_path):
    core, rs = make(tmp_path)
    seed(core, rs, tmp_path)
    with core.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS voice_profiles(id, embedding BLOB)")
        db.execute("INSERT INTO voice_profiles VALUES('v', 'VOICEPRINT_SECRET')")
    md = export_markdown(rs, 's1', tmp_path)
    assert 'VOICEPRINT_SECRET' not in md and 'embedding' not in md and 'sk-' not in md


def test_partial_not_stored_and_flags_string(tmp_path):
    _, rs = make(tmp_path)
    assert rs.append_segment('s', 'mic', 'answerer', '半句', False) is None
    sid = rs.append_segment('s', 'mic', 'answerer', '整句', True, flags='overlap,unknown')
    rows = rs.rows('SELECT * FROM segments')
    assert len(rows) == 1 and rows[0]['id'] == sid and rows[0]['flags'] == 'overlap,unknown' and rows[0]['orig_text'] == '整句'


def test_prompt_idempotent(tmp_path):
    _, rs = make(tmp_path)
    assert rs.record_prompt('s', 't1', 1, 'primary', ['a'], 'k') is True
    assert rs.record_prompt('s', 't1', 1, 'primary', ['a'], 'k') is False
    assert rs.record_prompt('s', 't1', 1, 'primary', ['b']) is True  # 无 key：按内容哈希
    assert rs.record_prompt('s', 't1', 1, 'primary', ['b']) is False
    assert len(rs.rows('SELECT * FROM prompts')) == 2


def test_turn_event_state_machine(tmp_path):
    _, rs = make(tmp_path)
    for i, (typ, kw) in enumerate([('candidate', dict(t=1.0, text='q?')), ('withdrawn', {}), ('candidate', dict(t=2.0)),
                                   ('committed', dict(t=3.0, text='q', text_version=1, kind='question')),
                                   ('rolled_back', {}), ('queued', {}), ('closed', dict(reason='answer_complete'))]):
        rs.record_turn_event('s', typ, 't1', 1, event_seq=i, **kw)
    t = rs.rows('SELECT * FROM turns')[0]
    assert (t['t_candidate'], t['t_committed'], t['rolled_back_count'], t['state'], t['closed_reason'], t['kind']) == \
           (2.0, 3.0, 1, 'closed', 'answer_complete', 'question')
    assert len(rs.rows('SELECT * FROM turn_events')) == 7


class FakeBus:
    def __init__(self, evs):
        self.evs = evs

    def replay(self, session, since):
        return [e for e in self.evs if e['event_seq'] > since]


def ev(seq, type_, payload, turn='t1', epoch=1, tv=0):
    return dict(type=type_, session='s', turn_id=turn, epoch=epoch, event_seq=seq, text_version=tv,
                t_server=100.0 + seq, payload=payload)


def board_events():
    return [
        ev(1, 'candidate', dict(text='候选', kind='question')),
        ev(2, 'candidate', dict(text='候选', withdrawn=True)),
        ev(3, 'turn_state', dict(state='question_committed', reason='r')),
        ev(4, 'transcript', dict(source='meeting', role='questioner', text='请讲讲', is_final=True, kind='question')),
        ev(5, 'transcript', dict(source='meeting', role='questioner', text='请讲', is_final=False)),
        ev(6, 'prompt_primary', dict(level='L1', outline=['x'], source_tag='lib')),
        ev(7, 'prompt_supplement', dict(source_tag='web', content='补', url='http://u')),
        ev(8, 'prompt_supplement', dict(source_tag='web', content='晚', late=True, urls=['http://v'])),
        ev(9, 'turn_rollback', dict(text='x')),
        ev(10, 'interrupt', dict(kind='switch')),
        ev(11, 'turn_state', dict(state='closed', reason='answer_complete')),
        ev(12, 'turn_state', dict(state='answering', reason='x')),
        ev(13, 'health', {}),
    ]


def test_ingest_board_events_and_dedupe(tmp_path):
    _, rs = make(tmp_path)
    evs = board_events()
    rs.ingest_board_events(evs)
    rs.ingest_board_events(evs)  # 重放不重复
    assert len(rs.rows('SELECT * FROM segments')) == 1
    p = rs.rows('SELECT * FROM prompts ORDER BY id')
    assert [x['state'] for x in p] == ['sent', 'sent', 'late_archived'] and p[0]['level'] == 'L1'
    assert json.loads(p[1]['source_urls_json']) == ['http://u']
    t = rs.rows('SELECT * FROM turns')[0]
    assert t['state'] == 'closed' and t['text'] == '请讲讲' and t['level'] == 'L1' and t['rolled_back_count'] == 1
    types = [e['type'] for e in rs.rows('SELECT type FROM turn_events ORDER BY id')]
    assert types == ['candidate', 'withdrawn', 'committed', 'text_updated', 'rolled_back', 'interrupted', 'closed']


def test_ingest_without_seq(tmp_path):
    _, rs = make(tmp_path)
    e = ev(1, 'prompt_primary', dict(outline='o'))
    e.pop('event_seq')
    rs.ingest_board_events([e, e])
    assert len(rs.rows('SELECT * FROM prompts')) == 1


def test_prompt_status_ingested_and_ack_marks_state(tmp_path):
    _, rs = make(tmp_path)
    rs.ingest_board_events([
        ev(7, 'prompt_status', dict(text='外部补充不可用', prompt_id='p7')),
        ev(8, 'prompt_supplement', dict(source_tag='web', content='补', prompt_id='p8')),
    ])
    rows = rs.rows('SELECT * FROM prompts ORDER BY id')
    assert [r['block'] for r in rows] == ['status', 'supplement']
    assert json.loads(rows[0]['content_json']) == '外部补充不可用'
    assert rs.mark_ack('s', 'p7', 1234500.0) is True
    p = rs.rows('SELECT state,display_ack_at FROM prompts WHERE block=?', ('status',))[0]
    assert p['state'] == 'acked' and p['display_ack_at'] == 1234.5
    assert rs.mark_ack('s', 'p999', 1.0) is False  # 未入档的 prompt_id 不改任何行


def test_ack_before_ingest_is_applied_when_prompt_arrives(tmp_path):
    """浏览器 ACK 可能先于事件入档：入档时必须补写 acked 状态，不能丢 ACK。"""
    _, rs = make(tmp_path)
    assert rs.mark_ack('s', 'p7', 1234500.0) is False
    rs.ingest_board_events([ev(7, 'prompt_status', dict(text='外部补充不可用', prompt_id='p7'))])
    p = rs.rows('SELECT state,display_ack_at FROM prompts')[0]
    assert p['state'] == 'acked' and p['display_ack_at'] == 1234.5


def test_answerer_transcript_is_a_segment_not_a_turn_event(tmp_path):
    core, rs = make(tmp_path)
    seed(core, rs, tmp_path)
    rs.ingest_board_events([dict(ev(1, 'transcript', dict(source='mic', role='answerer', text='我负责后端', is_final=True)),
                                 session='s1')])
    segs = rs.rows("SELECT source, role, text FROM segments WHERE role='answerer' ORDER BY id")
    assert segs == [dict(source='mic', role='answerer', text='我负责了后端'),
                    dict(source='mic', role='answerer', text='我负责后端')]
    assert not [e for e in rs.rows('SELECT type FROM turn_events') if e['type'] == 'text_updated']
    assert '回答者（mic）：我负责后端' in export_markdown(rs, 's1', tmp_path)


def test_attach_pull(tmp_path):
    _, rs = make(tmp_path)
    bus = FakeBus(board_events()[:4])
    pull = rs.attach(bus, 's')
    assert pull() == 4 and pull() == 0
    bus.evs += board_events()[4:7]
    assert pull() == 3
    assert len(rs.rows('SELECT * FROM prompts')) == 2


def test_edit_keeps_original(tmp_path):
    core, rs = make(tmp_path)
    seed(core, rs, tmp_path)
    seg = rs.rows("SELECT id FROM segments WHERE role='unknown'")[0]['id']
    r = edit_segment(rs, 's1', seg, text='嗯，那个', role='answerer')
    assert r['text'] == '嗯那个' and r['orig_text'] == '嗯那个' and r['edited_text'] == '嗯，那个'
    assert r['role'] == 'unknown' and r['edited_role'] == 'answerer'
    edit_segment(rs, 's1', seg, text='第二版')
    edits = rs.rows('SELECT field, old, new FROM segment_edits ORDER BY id')
    assert edits[-1] == dict(field='text', old='嗯，那个', new='第二版') and len(edits) == 3
    edit_segment(rs, 's1', seg, role='questioner')
    assert rs.rows('SELECT old FROM segment_edits ORDER BY id')[-1]['old'] == 'answerer'
    md = export_markdown(rs, 's1', tmp_path)
    assert '第二版' in md and '[已修订]' in md and '嗯那个' not in md
    with pytest.raises(ReviewError) as e:
        edit_segment(rs, 's1', seg)
    assert e.value.code == 400
    with pytest.raises(ReviewError) as e:
        edit_segment(rs, 's1', 9999, text='x')
    assert e.value.code == 404
    with pytest.raises(ReviewError):
        edit_segment(rs, 'other', seg, text='x')


def test_delete_moves_to_trash(tmp_path):
    core, rs = make(tmp_path)
    seed(core, rs, tmp_path)
    d = Deleter(rs, tmp_path / 'trash', tmp_path)
    with pytest.raises(ReviewError):
        d.confirm('s1', 'bogus')
    token = d.request('s1')
    with pytest.raises(ReviewError):
        d.confirm('s2', token)
    dest = d.confirm('s1', token)
    assert dest.parent == tmp_path / 'trash' and (dest / 'session.json').is_file()
    assert (dest / 'recordings' / 'meeting_0.wav').read_bytes() == WAV and not (tmp_path / 'meeting.wav').exists()
    dump = json.loads((dest / 'session.json').read_text('utf-8'))
    assert len(dump['segments']) == 3 and len(dump['metrics']) == 4
    assert rs.rows('SELECT * FROM segments') == [] and rs.rows('SELECT * FROM deleted_sessions')[0]['session_id'] == 's1'
    with pytest.raises(ReviewError):  # token 一次性
        d.confirm('s1', token)
    # 同秒再次删除不覆盖
    rs.append_segment('s1', 'mic', 'answerer', 'x', True)
    t2 = d.request('s1')
    assert d.confirm('s1', t2) != dest


def test_delete_active_session_refused(tmp_path):
    core, rs = make(tmp_path)
    with core.connect() as db:
        db.execute("INSERT INTO sessions VALUES('live','online','active','l','t')")
    d = Deleter(rs, tmp_path / 'trash')
    with pytest.raises(ReviewError) as e:
        d.confirm('live', d.request('live'))
    assert e.value.code == 409 and not (tmp_path / 'trash').exists()


def test_bundle_contents(tmp_path):
    core, rs = make(tmp_path)
    seed(core, rs, tmp_path)
    buf = io.BytesIO()
    man = write_bundle(rs, 's1', buf, tmp_path)
    zf = zipfile.ZipFile(buf)
    assert set(zf.namelist()) == {'transcript.md', 'metrics.csv', 'manifest.json', 'recordings/meeting_0.wav'}
    assert zf.read('recordings/meeting_0.wav') == WAV
    assert zf.read('metrics.csv').decode().count('\n') == 5
    assert json.loads(zf.read('manifest.json')) == json.loads(json.dumps(man))
    st = {f['name']: f['status'] for f in man['files']}
    assert st['recordings/mic_0.wav'] == 'missing' and st['recordings/mic_1.wav'] == 'corrupt'
    assert '现场实际发言' in zf.read('transcript.md').decode()


def test_m12_t3_perf_30min_synthetic(tmp_path):
    """SIMULATED：合成 30 分钟会话（360 轮、2 路各 24MB WAV），宽松阈值 <5s；真实 30 分钟录音 NOT TESTED。"""
    core, rs = make(tmp_path)
    with core.connect() as db:
        for i, src in enumerate(('meeting', 'mic')):
            p = tmp_path / f'{src}.wav'
            with open(p, 'wb') as f:
                f.write(WAV)
                f.write(b'\x01' * (24 << 20))
            db.execute("INSERT INTO recordings VALUES(?,?,?,?,?,?)", (f'r{i}', 's', src, 'audio/wav', 0, str(p)))
    m = Metrics(core.connect())
    with rs.lock:
        for i in range(360):
            t = i * 5.0
            rs.conn.execute("INSERT INTO segments(session_id,source,role,text,is_final,t_start,t_end,flags,orig_text)"
                            " VALUES('s','meeting','questioner','问题%d',1,?,?,'','')" % i, (t, t + 2))
            rs.conn.execute("INSERT INTO segments(session_id,source,role,text,is_final,t_start,t_end,flags,orig_text)"
                            " VALUES('s','mic','answerer','回答%d',1,?,?,'','')" % i, (t + 2, t + 4))
        rs.conn.commit()
    for i in range(360):
        rs.record_turn_event('s', 'committed', f't{i}', 1, t=i * 5.0, text=f'问题{i}')
        rs.record_prompt('s', f't{i}', 1, 'primary', ['要点'] * 3, f'k{i}', 'lib', level='L1')
        for n in ('t_end', 't_committed', 't_visible'):
            m.mark('s', f't{i}', n, i * 5_000_000_000)
    t0 = time.perf_counter()
    buf = io.BytesIO()
    write_bundle(rs, 's', buf, tmp_path)
    assert time.perf_counter() - t0 < 5.0
    assert len(zipfile.ZipFile(buf).read('transcript.md')) > 10000


def client(tmp_path):
    core, rs = make(tmp_path)
    seed(core, rs, tmp_path)
    app = FastAPI()
    ctx = SimpleNamespace(store=core, root=tmp_path, token='tok', bus=None, config={'trash_dir': tmp_path / 'tr'})
    store = records_api.register(app, ctx)
    return TestClient(app), store


def test_api_token_required(tmp_path):
    c, _ = client(tmp_path)
    for method, url in [('get', '/api/sessions/s1/transcript'), ('get', '/api/sessions/s1/turns'),
                        ('get', '/api/sessions/s1/prompts'),
                        ('get', '/api/sessions/s1/bundle'), ('post', '/api/sessions/s1/segments/1/edit'),
                        ('delete', '/api/sessions/s1')]:
        kw = dict(json={}) if method == 'post' else {}
        assert getattr(c, method)(url, **kw).status_code == 401
        assert getattr(c, method)(url, headers={'x-local-token': 'bad'}, **kw).status_code == 401


def test_api_reads_and_exports(tmp_path):
    c, _ = client(tmp_path)
    assert len(c.get('/api/sessions/s1/transcript', headers=H).json()) == 3
    assert len(c.get('/api/sessions/s1/turns', headers=H).json()) == 2
    assert len(c.get('/api/sessions/s1/prompts', headers=H).json()) == 3
    # 导出只保留 create_app 里提前注册的免令牌路由（浏览器直接下载）；records 模块不再重复注册。
    r = c.get('/api/sessions/s1/bundle', headers=H)
    assert r.headers['content-type'] == 'application/zip'
    assert 'manifest.json' in zipfile.ZipFile(io.BytesIO(r.content)).namelist()


def test_api_edit_and_delete_flow(tmp_path):
    c, store = client(tmp_path)
    sid = c.get('/api/sessions/s1/transcript', headers=H).json()[0]['id']
    r = c.post(f'/api/sessions/s1/segments/{sid}/edit', json=dict(text='改过'), headers=H)
    assert r.status_code == 200 and r.json()['edited_text'] == '改过' and r.json()['orig_text'] == '请介绍一下项目'
    assert c.post(f'/api/sessions/s1/segments/{sid}/edit', json={}, headers=H).status_code == 400
    assert c.post('/api/sessions/s1/segments/999/edit', json=dict(text='x'), headers=H).status_code == 404

    r = c.delete('/api/sessions/s1', headers=H)  # 无 confirm：只发 token，不删
    assert r.status_code == 409 and r.json()['detail']['need_confirm']
    token = r.json()['detail']['confirm_token']
    assert store.rows('SELECT * FROM segments')
    assert c.delete('/api/sessions/s1', params=dict(confirm='true', confirm_token='bad'), headers=H).status_code == 409
    r = c.delete('/api/sessions/s1', params=dict(confirm='true', confirm_token=token), headers=H)
    assert r.status_code == 200 and (tmp_path / 'tr').is_dir() and list((tmp_path / 'tr').iterdir())
    assert c.get('/api/sessions/s1/transcript', headers=H).status_code == 404
    assert c.get('/api/sessions/s1/bundle', headers=H).status_code == 404


# ---------- 记录库：会话历史列表 ----------

def history_client(tmp_path):
    """含 4 场会话：进行中 / 已结束（config 快照取别名）/ 已结束（profiles 取别名）/ 旧会话（仅 5 列）。"""
    from app.sessions import profiles as P
    from app.sessions.setup import ensure_sessions_columns

    core, rs = make(tmp_path)
    with core.connect() as db:
        db.execute("INSERT INTO libraries(id,name,hash,source) VALUES('lib','合成主库','h-hist','')")
        db.execute("INSERT INTO sessions(id,mode,status,library_id,turn_id) VALUES('legacy','offline','ended','lib',NULL)")
    ensure_sessions_columns(core)
    pid = P.create_profile(core, '合成候选人B', ['技术开发'])['id']
    cfg = json.dumps(dict(profile=dict(alias='合成候选人A'), roles=['产品经理'],
                          libraries={'lib': '合成主库'}), ensure_ascii=False)
    with core.connect() as db:
        db.execute("INSERT INTO sessions(id,mode,status,library_id,turn_id,roles_json,config_snapshot_json,"
                   "created_at,started_at,stopped_at,degraded_banner) VALUES(?,?,?,?,NULL,?,?,?,?,?,?)",
                   ('s1', 'online', 'stopped', 'lib', json.dumps(['产品经理'], ensure_ascii=False), cfg,
                    900.0, 1000.0, 1600.0, '自检有失败项：ASR 未配置（降级开始）'))
        db.execute("INSERT INTO sessions(id,mode,status,library_id,turn_id,profile_id,roles_json,"
                   "created_at,started_at,stopped_at) VALUES(?,?,?,?,NULL,?,?,?,?,?)",
                   ('s2', 'offline', 'stopped', 'lib', pid, json.dumps(['技术开发'], ensure_ascii=False),
                    1900.0, 2000.0, 2030.0))
        db.execute("INSERT INTO sessions(id,mode,status,library_id,turn_id,created_at,started_at) "
                   "VALUES('slive','online','active','lib',NULL,3000.0,3000.0)")
        wav = tmp_path / 'hist.wav'
        wav.write_bytes(WAV)
        db.execute("INSERT INTO recordings VALUES('h1','s1','meeting','audio/wav',0,?)", (str(wav),))
        db.execute("INSERT INTO recording_gaps(session_id,source,t_start_ms,t_end_ms,reason) VALUES('s1','meeting',0,500,'ws_buffer_full')")
    rs.append_segment('s1', 'meeting', 'questioner', '逐字稿正文ZZZ 请介绍一个合成项目', True, 10.0, 12.0)
    rs.append_segment('s1', 'mic', 'answerer', '我负责过合成项目', True, 14.0, 16.0)
    rs.record_turn_event('s1', 'committed', 't1', 1, t=11.0, text='请介绍一个合成项目', kind='question')
    rs.record_prompt('s1', 't1', 1, 'primary', ['要点A', '要点B'], 'k-h1', 'primary_lib', level='L1')
    rs.record_turn_event('s2', 'committed', 't9', 1, t=2001.0, text='讲讲协作', kind='question')
    rs.append_segment('s2', 'meeting', 'questioner', '讲讲协作', True, 2001.0, 2003.0)
    app = FastAPI()
    ctx = SimpleNamespace(store=core, root=tmp_path, token='tok', bus=None, config={'trash_dir': tmp_path / 'tr'})
    store = records_api.register(app, ctx)
    return TestClient(app), core, store


def test_api_sessions_list_fields_order_and_pagination(tmp_path):
    c, _, _ = history_client(tmp_path)
    r = c.get('/api/sessions')  # 只读，不要求令牌
    assert r.status_code == 200
    body = r.json()
    assert body['total'] == 4 and body['limit'] == 50 and body['offset'] == 0
    rows = body['sessions']
    assert [x['id'] for x in rows] == ['slive', 's2', 's1', 'legacy'], '按开始时间倒序，旧会话无开始时间排最后'

    s1 = rows[2]
    assert set(s1) >= {'id', 'mode', 'status', 'started_at', 'stopped_at', 'duration_s', 'profile_alias', 'roles',
                       'library_name', 'turns', 'prompts', 'recordings', 'gaps', 'has_recording', 'deleted', 'deleted_at'}
    assert s1['status'] == 'stopped' and s1['mode'] == 'online'
    assert (s1['started_at'], s1['stopped_at'], s1['duration_s']) == (1000.0, 1600.0, 600.0)
    assert s1['profile_alias'] == '合成候选人A' and s1['roles'] == ['产品经理'] and s1['library_name'] == '合成主库'
    assert (s1['turns'], s1['prompts'], s1['recordings'], s1['gaps']) == (1, 1, 1, 1)
    assert s1['has_recording'] is True and s1['deleted'] is False and s1['deleted_at'] is None

    assert rows[1]['profile_alias'] == '合成候选人B' and rows[1]['roles'] == ['技术开发']
    assert rows[0]['status'] == 'active'
    assert '逐字稿正文ZZZ' not in r.text, '列表不返回逐字稿正文'
    assert 'sk-' not in r.text

    page = c.get('/api/sessions', params={'limit': 2, 'offset': 0}).json()
    assert [x['id'] for x in page['sessions']] == ['slive', 's2'] and page['total'] == 4
    page = c.get('/api/sessions', params={'limit': 2, 'offset': 2}).json()
    assert [x['id'] for x in page['sessions']] == ['s1', 'legacy']
    assert c.get('/api/sessions', params={'limit': 2, 'offset': 99}).json()['sessions'] == []
    assert c.get('/api/sessions', params={'limit': 0}).json()['limit'] == 1
    assert c.get('/api/sessions', params={'limit': 9999}).json()['limit'] == 200


def test_api_sessions_list_legacy_missing_fields(tmp_path):
    """旧会话只有 id/mode/status/library_id：开始/结束时间、别名、岗位缺失时不得报错。"""
    c, _, _ = history_client(tmp_path)
    legacy = next(x for x in c.get('/api/sessions').json()['sessions'] if x['id'] == 'legacy')
    assert legacy['status'] == 'stopped', 'ended 映射为 stopped'
    assert legacy['started_at'] is None and legacy['stopped_at'] is None and legacy['duration_s'] is None
    assert legacy['profile_alias'] is None and legacy['roles'] == []
    assert legacy['library_name'] == '合成主库'
    assert (legacy['turns'], legacy['prompts'], legacy['recordings'], legacy['gaps']) == (0, 0, 0, 0)
    assert legacy['has_recording'] is False


def test_api_sessions_deleted_flag_and_active_delete_refused(tmp_path):
    c, _, _ = history_client(tmp_path)
    # 进行中的会话：发确认 token 后仍拒绝
    r = c.delete('/api/sessions/slive', headers=H)
    assert r.status_code == 409 and r.json()['detail']['need_confirm']
    token = r.json()['detail']['confirm_token']
    r = c.delete('/api/sessions/slive', params={'confirm': 'true', 'confirm_token': token}, headers=H)
    assert r.status_code == 409 and '进行中的会话不能删除' in r.json()['detail']

    # 已结束会话：删除到回收目录后，列表标 deleted，数据计数归零
    r = c.delete('/api/sessions/s1', headers=H)
    token = r.json()['detail']['confirm_token']
    assert c.delete('/api/sessions/s1', params={'confirm': 'true', 'confirm_token': token}, headers=H).status_code == 200
    rows = {x['id']: x for x in c.get('/api/sessions').json()['sessions']}
    gone = rows['s1']
    assert gone['deleted'] is True and isinstance(gone['deleted_at'], float)
    assert (gone['turns'], gone['prompts'], gone['recordings'], gone['gaps']) == (0, 0, 0, 0)
    assert gone['has_recording'] is False
    assert c.get('/api/sessions/s1/transcript', headers=H).status_code == 404


# ---------- 结束会话：stopped_at 与记录库时长 ----------

def test_stop_writes_stopped_at_and_list_duration(tmp_path):
    """结束会话必须写 stopped_at（旧数据可能为 NULL）；记录库时长按 started_at/stopped_at 计算。"""
    from app.sessions.setup import ensure_sessions_columns

    core, rs = make(tmp_path)
    ensure_sessions_columns(core)
    with core.connect() as db:
        db.execute("INSERT INTO sessions(id,mode,status,library_id,turn_id,started_at) "
                   "VALUES('new','online','active','lib',NULL,1000.0)")
    before = time.time()
    ended = core.stop('new')
    after = time.time()
    assert ended['status'] == 'ended'
    assert isinstance(ended['stopped_at'], float) and before <= ended['stopped_at'] <= after

    rows = records_api.session_list(rs, core)['sessions']
    row = next(x for x in rows if x['id'] == 'new')
    assert row['status'] == 'stopped'
    assert row['stopped_at'] == ended['stopped_at']
    assert row['duration_s'] == pytest.approx(ended['stopped_at'] - 1000.0)
    assert core.stop('new')['stopped_at'] == ended['stopped_at'], '重复结束不刷新结束时间'


def test_sessions_stop_route_sets_stopped_at(tmp_path):
    """前端结束走 /api/sessions/{sid}/stop：路由落地后记录库应显示真实时长。"""
    from fastapi.testclient import TestClient

    from app.server import create_app

    c = TestClient(create_app(tmp_path / 'data'))
    h = {'X-Local-Token': c.get('/api/status').json()['token']}
    with c.app.state.store.connect() as db:
        db.execute("INSERT INTO sessions(id,mode,status,library_id,turn_id,started_at) "
                   "VALUES('route-stop','online','active','lib',NULL,1000.0)")
    assert c.post('/api/sessions/route-stop/stop', json={}, headers=h).status_code == 200
    row = next(x for x in c.get('/api/sessions').json()['sessions'] if x['id'] == 'route-stop')
    assert row['status'] == 'stopped' and isinstance(row['stopped_at'], float)
    assert row['duration_s'] == pytest.approx(row['stopped_at'] - 1000.0)


# ---------- 导出诚实性：未转写 / 未标注 / 无标准答案 ----------

def test_export_without_answerer_says_untranscribed_not_unanswered(tmp_path):
    _, rs = make(tmp_path)
    rs.record_turn_event('s3', 'committed', 't1', 1, t=10.0, text='只转写提问方', kind='question')
    secs = sections(export_markdown(rs, 's3'))
    speech = secs['现场实际发言（逐字稿）']
    assert '回答者发言未转写（仅录音）' in speech
    turns = secs['问答轮次']
    assert '未知（回答者未转写）' in turns and '未回答' not in turns


def test_export_answerer_transcribed_flag_shared_with_bundle(tmp_path):
    _, rs = make(tmp_path)
    rs.append_segment('s4', 'meeting', 'questioner', '问', True, 1.0, 2.0)
    assert '回答者发言未转写' not in export_markdown(rs, 's4', answerer_transcribed=True)
    assert '回答者发言未转写' in export_markdown(rs, 's4', answerer_transcribed=False)
    buf = io.BytesIO()
    write_bundle(rs, 's4', buf, answerer_transcribed=False)
    assert '回答者发言未转写（仅录音）' in zipfile.ZipFile(buf).read('transcript.md').decode()
    buf = io.BytesIO()
    write_bundle(rs, 's4', buf, answerer_transcribed=True)
    assert '回答者发言未转写' not in zipfile.ZipFile(buf).read('transcript.md').decode()
    # 未显式传入时按逐字稿推断：有回答者片段就不再提示
    rs.append_segment('s4', 'mic', 'answerer', '答', True, 3.0, 4.0)
    assert '回答者发言未转写' not in export_markdown(rs, 's4')


def test_export_unmarked_level_and_no_primary_section(tmp_path):
    _, rs = make(tmp_path)
    rs.record_prompt('s5', 't1', 1, 'supplement', '补充内容', 'k5', 'web')
    hint = '本轮没有命中标准答案'
    sysrec = sections(export_markdown(rs, 's5'))['系统建议（系统生成，不等于实际发言）']
    assert '级别 未标注' in sysrec and '级别 -' not in sysrec
    assert hint in sysrec
    rs.record_prompt('s5', 't1', 1, 'primary', ['要点'], 'k6', 'lib', level='L1')
    assert hint not in sections(export_markdown(rs, 's5'))['系统建议（系统生成，不等于实际发言）']


def test_ingest_prompt_source_entry_id(tmp_path):
    _, rs = make(tmp_path)
    rs.ingest_board_events([ev(1, 'prompt_primary',
                               dict(outline=['x'], level='L1', source_tag='lib', source_entry_id='E1'))])
    assert rs.rows('SELECT source_entry_id FROM prompts')[0]['source_entry_id'] == 'E1'


def test_late_archived_metric_rows_not_collapsed_and_block_is_not_primary(tmp_path):
    """P2：迟到归档遥测按 reason + 原始 epoch 分别留档，不再全压成 block='primary'。"""
    from app.wiring import Wiring

    _, rs = make(tmp_path)
    w = Wiring.__new__(Wiring)
    w.records = rs
    reasons = ['cancelled', 'superseded', 'stale_turn', 'turn_quota']
    for epoch in range(1, 9):
        for reason in reasons:
            w.on_late_metric('s1', 't1', 'late_archived',
                             json.dumps(dict(reason=reason, hit_id='H1-01', epoch=epoch), ensure_ascii=False))
    rows = rs.rows("SELECT block, epoch, state, content_json FROM prompts WHERE session_id='s1'")
    assert len(rows) == 32  # 32 条各自留档，不再压成 3 条
    assert {r['block'] for r in rows} == {'late_archived'}
    assert {r['state'] for r in rows} == {'late_archived'}
    assert {r['epoch'] for r in rows} == set(range(1, 9))
    # 同一 reason + epoch 重放仍幂等
    w.on_late_metric('s1', 't1', 'late_archived',
                     json.dumps(dict(reason='cancelled', hit_id='H1-01', epoch=1), ensure_ascii=False))
    assert len(rs.rows("SELECT 1 FROM prompts WHERE session_id='s1'")) == 32
