import pytest
from fastapi.testclient import TestClient
from app.server import create_app
from test_core import TEXT

@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path))


def headers(c):
    return {'X-Local-Token': c.get('/api/status').json()['token']}


def prepared(c):
    h = headers(c)
    lib = c.post('/api/import', json={'name':'sample','text':TEXT}, headers=h).json()['id']
    c.post(f'/api/libraries/{lib}/entries/H1-01/review', json={'status':'approved','answer':'这是经确认的练习原型。','confirmed':True}, headers=h)
    sid = c.post('/api/sessions', json={'mode':'online','library':lib,'consent':True}, headers=h).json()['id']
    return h, lib, sid


def test_csrf_and_host(client):
    assert client.post('/api/import', json={'name':'sample','text':TEXT}).status_code == 403
    h = headers(client)
    h['Origin'] = 'https://evil.example'
    assert client.post('/api/import', json={'name':'sample','text':TEXT}, headers=h).status_code == 403
    assert client.get('/api/status', headers={'Host':'evil.example'}).status_code == 400


def test_safe_frontend_and_status(client):
    response = client.get('/')
    assert response.status_code == 200
    assert "script-src 'self'" in response.headers['content-security-policy']
    assert client.get('/api/status').json()['asr'] == '未接入'


def test_import_review_search(client):
    h = headers(client)
    lib = client.post('/api/import', json={'name':'sample','text':TEXT}, headers=h).json()['id']
    assert not client.get('/api/search', params={'library':lib,'q':'你的项目是什么'}).json()['results']
    assert client.post(f'/api/libraries/{lib}/entries/H1-01/review', json={'status':'approved','answer':'练习稿','confirmed':False}, headers=h).status_code == 400
    assert client.post(f'/api/libraries/{lib}/entries/H1-01/review', json={'status':'approved','answer':'练习稿','confirmed':True}, headers=h).status_code == 200
    assert client.get('/api/search', params={'library':lib,'q':'你的项目是什么？'}).json()['results']


def test_audio_save_download_retry(client):
    h, lib, sid = prepared(client)
    h['Content-Type'] = 'audio/webm'
    url = f'/api/sessions/{sid}/audio/mic/0'
    response = client.post(url, content=b'fake-media-container', headers=h)
    assert response.status_code == 200
    rid = response.json()['id']
    assert client.post(url, content=b'fake-media-container', headers=h).json()['id'] == rid
    assert client.get(f'/api/recordings/{rid}').content == b'fake-media-container'
    assert client.get(f'/api/sessions/{sid}/recordings').json()[0]['sequence'] == 0
    assert '实际发言' in client.get(f'/api/sessions/{sid}/export').text


def test_audio_boundaries(client):
    h, lib, sid = prepared(client)
    h['Content-Type'] = 'text/html'
    assert client.post(f'/api/sessions/{sid}/audio/mic/0', content=b'abc',headers=h).status_code == 400
    h['Content-Type'] = 'audio/webm'
    assert client.post(f'/api/sessions/{sid}/audio/mic/-1', content=b'abc',headers=h).status_code == 400
    assert client.post(f'/api/sessions/{sid}/audio/mic/0', content=b'',headers=h).status_code == 400
    assert client.get('/api/recordings/missing').status_code == 404


def test_late_event_after_stop(client):
    h, lib, sid = prepared(client)
    turn = client.post(f'/api/sessions/{sid}/events', json={'kind':'question_complete','question':'test'},headers=h).json()['turn_id']
    assert client.post(f'/api/sessions/{sid}/stop',json={},headers=h).status_code == 200
    r = client.post(f'/api/sessions/{sid}/events',json={'kind':'supplement','turn_id':turn,'content':'late'},headers=h)
    assert r.json()['disposition'] == 'archived'


def test_setup_start_prepares_retrieval_index_and_keeps_roles(client):
    """正常产品流程（setup 创建 -> 自检 -> 开始）必须和采集页的 POST /api/sessions 一样预建主库索引，
    否则 setup 会话的每个问题都会落到“检索不可用”。"""
    h = headers(client)
    lib = client.post('/api/import', json={'name': 'sample', 'text': TEXT}, headers=h).json()['id']
    client.post(f'/api/libraries/{lib}/entries/H1-01/review',
                json={'status': 'approved', 'answer': '这是经确认的练习原型。', 'confirmed': True}, headers=h)
    pid = client.post('/api/profiles', json={'alias': '甲', 'roles': ['产品经理'], 'library_ids': [lib]},
                      headers=h).json()['id']
    sid = client.post('/api/setup/sessions', json={'mode': 'online', 'profile_id': pid,
                                                   'roles': ['产品经理'], 'library_ids': [lib], 'consent': True},
                      headers=h).json()['id']
    report = client.post(f'/api/setup/{sid}/selfcheck', headers=h).json()
    assert client.post(f'/api/setup/{sid}/start', json={'degraded': report['has_fail']}, headers=h).status_code == 200
    assert client.get(f'/api/setup/{sid}').json()['roles'] == ['产品经理']
    assert client.app.state.index_provider(lib) is not None  # 主库索引已建，L1 可用


def test_capture_reuses_active_setup_session(client):
    """/api/status 的 active_session 必须是 setup 会话；旧 POST /api/sessions 在有 active 会话时必须失败。"""
    h = headers(client)
    lib = client.post('/api/import', json={'name': 'sample', 'text': TEXT}, headers=h).json()['id']
    client.post(f'/api/libraries/{lib}/entries/H1-01/review',
                json={'status': 'approved', 'answer': '这是经确认的练习原型。', 'confirmed': True}, headers=h)
    pid = client.post('/api/profiles', json={'alias': '甲', 'roles': ['产品经理'], 'library_ids': [lib]},
                      headers=h).json()['id']
    sid = client.post('/api/setup/sessions', json={'mode': 'online', 'profile_id': pid,
                                                   'roles': ['产品经理'], 'library_ids': [lib], 'consent': True},
                      headers=h).json()['id']
    report = client.post(f'/api/setup/{sid}/selfcheck', headers=h).json()
    assert client.post(f'/api/setup/{sid}/start', json={'degraded': report['has_fail']}, headers=h).status_code == 200
    active = client.get('/api/status').json()['active_session']
    assert active['id'] == sid and active['mode'] == 'online'
    r = client.post('/api/sessions', json={'mode': 'online', 'library': lib, 'consent': True}, headers=h)
    assert r.status_code == 400 and '已有运行中的会话' in r.json()['detail']


def test_active_setup_session_finalized_by_sessions_stop_route(client):
    """setup 页“结束该会话”走 /api/sessions/{sid}/stop：完整收尾（含管线/事件），之后可创建新会话。"""
    h = headers(client)
    lib = client.post('/api/import', json={'name': 'sample', 'text': TEXT}, headers=h).json()['id']
    client.post(f'/api/libraries/{lib}/entries/H1-01/review',
                json={'status': 'approved', 'answer': '这是经确认的练习原型。', 'confirmed': True}, headers=h)
    pid = client.post('/api/profiles', json={'alias': '甲', 'roles': ['产品经理'], 'library_ids': [lib]},
                      headers=h).json()['id']
    body = {'mode': 'online', 'profile_id': pid, 'roles': ['产品经理'], 'library_ids': [lib], 'consent': True}
    sid = client.post('/api/setup/sessions', json=body, headers=h).json()['id']
    report = client.post(f'/api/setup/{sid}/selfcheck', headers=h).json()
    assert client.post(f'/api/setup/{sid}/start', json={'degraded': report['has_fail']}, headers=h).status_code == 200
    assert client.post(f'/api/sessions/{sid}/stop', json={}, headers=h).status_code == 200
    assert client.get('/api/status').json()['active_session'] is None
    assert client.get(f'/api/setup/{sid}').json()['status'] == 'stopped'
    assert client.post('/api/setup/sessions', json=body, headers=h).status_code == 200


def test_no_consent(client):
    h=headers(client)
    assert client.post('/api/sessions',json={'mode':'online','library':'missing','consent':False},headers=h).status_code == 400
