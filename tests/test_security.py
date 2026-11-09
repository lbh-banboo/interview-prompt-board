"""M16-1/T1：Host / Origin / token / 大小限制 / 安全头 / 不泄漏凭证与内部路径（HTTP 与 WS 一致）。"""
import json

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from app.server import create_app
from test_api import prepared, headers
from test_core import TEXT


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path))


def ws_close_code(client, url, **kw):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(url, **kw) as ws:
            ws.receive_json()
    return exc.value.code


def ws_denied(client, url, **kw):
    """握手阶段被拒（HTTP 400/403）或 accept 后立刻 close，都算拒绝。"""
    try:
        with client.websocket_connect(url, **kw) as ws:
            ws.receive_json()
    except (WebSocketDisconnect, WebSocketDenialResponse):
        return True
    return False


# ---------- WS ----------
@pytest.mark.parametrize('path', ['/ws/board/{sid}', '/ws/audio/{sid}'])
def test_ws_requires_token(client, path):
    h, lib, sid = prepared(client)
    base = path.format(sid=sid)
    assert ws_close_code(client, base) == 4403
    assert ws_close_code(client, base + '?token=') == 4403
    assert ws_close_code(client, base + '?token=wrong') == 4403


@pytest.mark.parametrize('path', ['/ws/board/{sid}', '/ws/audio/{sid}'])
def test_ws_rejects_cross_origin_and_foreign_host(client, path):
    h, lib, sid = prepared(client)
    url = path.format(sid=sid) + '?token=' + h['X-Local-Token']
    assert ws_close_code(client, url, headers={'origin': 'https://evil.example'}) == 4403
    assert ws_close_code(client, url, headers={'origin': 'null'}) == 4403
    assert ws_denied(client, url, headers={'host': 'evil.example'})


def test_board_ws_unknown_session_4404(client):
    h = headers(client)
    assert ws_close_code(client, '/ws/board/nope?token=' + h['X-Local-Token']) == 4404


def test_board_ws_accepts_same_origin(client):
    h, lib, sid = prepared(client)
    url = f"/ws/board/{sid}?token={h['X-Local-Token']}"
    with client.websocket_connect(url, headers={'origin': 'http://testserver'}) as ws:
        client.app.state.bus.publish_queue(sid, ['x'])
        assert ws.receive_json()['type'] == 'queue_update'


# ---------- POST ----------
def test_post_token_origin_and_size_limits(client):
    h = headers(client)
    body = {'name': 'n', 'text': TEXT}
    assert client.post('/api/import', json=body).status_code == 403
    assert client.post('/api/import', json=body, headers={'X-Local-Token': 'bad'}).status_code == 403
    for origin in ('https://evil.example', 'null', 'http://127.0.0.1.evil.example'):
        assert client.post('/api/import', json=body, headers={**h, 'Origin': origin}).status_code == 403
    assert client.post('/api/import', json=body, headers={**h, 'Origin': 'http://testserver'}).status_code == 200
    assert client.post('/api/import', json=body, headers={**h, 'Content-Length': '9000000'}).status_code == 413
    assert client.post('/api/import', json=body, headers={**h, 'Content-Length': 'abc'}).status_code in (400, 413)


def test_token_check_runs_before_size_limit(client):
    assert client.post('/api/import', content=b'x', headers={'Content-Length': '9000000'}).status_code == 403


def test_get_api_does_not_need_token_but_host_is_checked(client):
    assert client.get('/api/libraries').status_code == 200
    assert client.get('/api/libraries', headers={'Host': 'evil.example'}).status_code == 400
    assert client.get('/api/libraries', headers={'Host': '192.168.1.5:8765'}).status_code == 400


# ---------- 安全头 ----------
@pytest.mark.parametrize('path', ['/', '/api/status', '/board.html', '/api/missing', '/nope.js'])
def test_security_headers_on_every_response(client, path):
    r = client.get(path)
    csp = r.headers['content-security-policy']
    for part in ("default-src 'self'", "script-src 'self'", "style-src 'self'", "frame-ancestors 'none'",
                 "base-uri 'none'", "form-action 'none'", "object-src 'none'"):
        assert part in csp
    assert 'unsafe-inline' not in csp and 'unsafe-eval' not in csp
    assert r.headers['referrer-policy'] == 'no-referrer'
    assert r.headers['x-frame-options'] == 'DENY'
    assert r.headers['x-content-type-options'] == 'nosniff'
    assert r.headers['cache-control'] == 'no-store'


# ---------- 不泄漏 ----------
def test_status_has_no_credentials_but_keeps_local_token(client, monkeypatch):
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'sk-secret-should-never-appear')
    data = client.get('/api/status').json()
    text = json.dumps(data, ensure_ascii=False).lower()
    assert data['token']  # 本地会话令牌是设计内的
    assert 'sk-secret' not in text
    for word in ('dashscope', 'api_key', 'apikey', 'secret', 'password', 'authorization'):
        assert word not in text
    keys = set(data)
    assert not any(k for k in keys if 'key' in k.lower() or 'secret' in k.lower())


def test_errors_do_not_echo_internal_paths_or_traces(tmp_path):
    app = create_app(tmp_path)

    async def boom(request):
        raise RuntimeError('/Users/example/secret/path.py exploded')

    app.router.routes.insert(0, Route('/api/boom', boom))
    c = TestClient(app, raise_server_exceptions=False)
    cases = [c.get('/api/boom'), c.get('/api/sessions/nope/snapshot'), c.get('/api/search?library=x&q='),
             c.post('/api/import', json={'name': ''}, headers=headers(c)), c.get('/no/such/file')]
    assert cases[0].status_code == 500 and cases[0].json() == {'detail': '服务内部错误'}
    for r in cases:
        text = r.text
        assert 'Traceback' not in text and '/Users/' not in text and '.py' not in text
