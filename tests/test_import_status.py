"""导入入口、根路径重定向、/api/status 真实能力文案、次优库种子自动导入。"""
import shutil
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from app import wiring
from app.audio.enroll import VoiceProfileStore
from app.credentials import CredentialStore, MemoryBackend
from app.library.secondary import SecondaryLibrary
from app.sessions import probes
from app.sessions import setup as S
from app.server import create_app

from test_core import TEXT

FIX = Path(__file__).parent / 'fixtures'
OK_SEEDS = FIX / 'seeds_ok'
MB = 1_000_000


def token_headers(c):
    return {'X-Local-Token': c.get('/api/status').json()['token']}


def no_keys():
    return CredentialStore([MemoryBackend({})])


def memory(**values):
    return CredentialStore([MemoryBackend(values)])


def client(tmp_path, **kwargs):
    return TestClient(create_app(tmp_path / 'data', credentials=no_keys(), **kwargs))


# ---------- 重要：导入上限 2MB -> 5MB ----------
def test_import_accepts_between_2mb_and_5mb(tmp_path):
    c = client(tmp_path)
    text = TEXT + '\n' + 'x' * (2_100_000 - len(TEXT))
    r = c.post('/api/import', json={'name': '大文件', 'text': text}, headers=token_headers(c))
    assert r.status_code == 200 and r.json()['count'] == 1


def test_import_rejects_more_than_5mb(tmp_path):
    c = client(tmp_path)
    r = c.post('/api/import', json={'name': 'too-big', 'text': 'x' * (5 * MB + 1)}, headers=token_headers(c))
    assert r.status_code == 422


def test_import_measures_utf8_bytes_not_chars(tmp_path):
    c = client(tmp_path)
    text = TEXT + '\n' + '待' * 1_700_000  # 约 5.1MB，但字符数不足 5M
    r = c.post('/api/import', json={'name': '中文超限', 'text': text}, headers=token_headers(c))
    assert r.status_code == 400 and '5MB' in r.json()['detail']


# ---------- 首页上线：根路径直接提供欢迎页（不再 302 跳工作台） ----------
def test_root_and_index_serve_landing(tmp_path):
    c = client(tmp_path)
    for path in ('/', '/index.html'):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 200 and 'text/html' in r.headers['content-type']
        assert 'Interview Copilot' in r.text and '开始一场会话' in r.text
    assert c.get('/home.html').status_code == 200


# ---------- /api/status 真实能力 ----------
def _sherpa_dir(tmp_path, names=probes.SHERPA_FILES):
    d = tmp_path / 'sherpa'
    d.mkdir(parents=True, exist_ok=True)
    for name in names:
        (d / name).write_bytes(b'x')
    return d


def status_of(tmp_path, monkeypatch, names, creds=None, config=None):
    monkeypatch.setattr(wiring, 'SHERPA_DIR', _sherpa_dir(tmp_path, names))
    c = TestClient(create_app(tmp_path / 'data', credentials=creds or no_keys(), config=config))
    return c.get('/api/status').json()


def test_status_asr_cloud_when_key_and_outbound_on(tmp_path, monkeypatch):
    data = status_of(tmp_path, monkeypatch, (), creds=memory(DASHSCOPE_API_KEY='sk-dash-1234567890'))
    assert data['asr'] == '云转写'


def test_status_asr_local_when_no_cloud_key(tmp_path, monkeypatch):
    data = status_of(tmp_path, monkeypatch, probes.SHERPA_FILES)
    assert data['asr'] == '本地转写'


def test_status_asr_unavailable_when_neither_path(tmp_path, monkeypatch):
    data = status_of(tmp_path, monkeypatch, ('encoder.int8.onnx',))
    assert data['asr'] == '未接入'


def test_status_model_and_search(tmp_path, monkeypatch):
    data = status_of(tmp_path, monkeypatch, probes.SHERPA_FILES)
    assert data['model'] == '未接入' and data['search'] == '未接入'
    data = status_of(tmp_path / 'k', monkeypatch, probes.SHERPA_FILES,
                     creds=memory(DEEPSEEK_API_KEY='sk-deep-1234567890'),
                     config={'search_transport': 'mock'})
    assert data['model'] == '已配置' and data['search'] == '已配置'


def test_status_speaker_follows_local_enrollment(tmp_path, monkeypatch):
    data = status_of(tmp_path, monkeypatch, probes.SHERPA_FILES)
    assert data['speaker'] == '未登记'
    store = create_app(tmp_path / 'voice', credentials=no_keys()).state.store
    VoiceProfileStore(store.connect, store.lock, store.root).save('default', np.ones(8, np.float32), {})
    monkeypatch.setattr(wiring, 'SHERPA_DIR', _sherpa_dir(tmp_path / 'voice-sherpa', probes.SHERPA_FILES))
    c = TestClient(create_app(tmp_path / 'voice', credentials=no_keys()))
    assert c.get('/api/status').json()['speaker'] == '已登记'


def test_status_never_leaks_keys(tmp_path, monkeypatch):
    canary_key = 'sk-leak-canary-1234567890'
    monkeypatch.setattr(wiring, 'SHERPA_DIR', _sherpa_dir(tmp_path, probes.SHERPA_FILES))
    c = TestClient(create_app(tmp_path / 'data', credentials=memory(
        DASHSCOPE_API_KEY=canary_key, DEEPSEEK_API_KEY=canary_key, SEARCH_API_KEY=canary_key)))
    body = c.get('/api/status').text
    assert canary_key not in body and 'sk-leak' not in body


# ---------- 种子自动导入 ----------
def test_create_app_auto_imports_secondary_seeds(tmp_path):
    app = create_app(tmp_path / 'data', credentials=no_keys(), config={'seeds_dir': str(OK_SEEDS)})
    progress = SecondaryLibrary(app.state.store).progress()
    assert progress['total'] == 7 and progress['pending'] == 7
    assert {'tech', 'ops', 'mgmt', 'general'} <= {r['role'] for r in progress['by_role']}


def test_auto_import_is_idempotent_and_keeps_approved(tmp_path):
    create_app(tmp_path / 'data', credentials=no_keys(), config={'seeds_dir': str(OK_SEEDS)})
    app = create_app(tmp_path / 'data', credentials=no_keys(), config={'seeds_dir': str(OK_SEEDS)})
    store = app.state.store
    lib = SecondaryLibrary(store)
    assert lib.review('QTECH-01', 'approved', True)
    create_app(tmp_path / 'data', credentials=no_keys(), config={'seeds_dir': str(OK_SEEDS)})
    lib = SecondaryLibrary(store)
    statuses = {e['id']: e['status'] for e in lib.candidates(['tech'], include_pending=True)}
    assert statuses['QTECH-01'] == 'approved' and lib.progress()['total'] == 7 and lib.progress()['approved'] == 1


def test_changed_seed_triggers_auto_import(tmp_path):
    seeds = tmp_path / 'seeds'
    shutil.copytree(OK_SEEDS, seeds)
    create_app(tmp_path / 'data', credentials=no_keys(), config={'seeds_dir': str(seeds)})
    (seeds / 'tech.md').write_text((seeds / 'tech.md').read_text('utf-8').replace('讲讲CAP理论', '讲讲CAP定理'), 'utf-8')
    app = create_app(tmp_path / 'data', credentials=no_keys(), config={'seeds_dir': str(seeds)})
    entries = {e['id']: e for e in SecondaryLibrary(app.state.store).entries()}
    assert 'CAP定理' in entries['QTECH-01']['question'] and entries['QTECH-01']['status'] == 'pending'


def test_bad_or_missing_seed_dir_does_not_block_startup(tmp_path):
    bad = tmp_path / 'bad'
    bad.mkdir()
    (bad / 'tech.md').write_text('### 坏标题\n', 'utf-8')
    app = create_app(tmp_path / 'data', credentials=no_keys(), config={'seeds_dir': str(bad)})
    assert app.state.store is not None
    app = create_app(tmp_path / 'data2', credentials=no_keys(), config={'seeds_dir': str(tmp_path / 'missing')})
    assert app.state.store is not None


# ---------- 向导后端：多个 library_ids 的行为明确 ----------
def test_setup_accepts_library_id_list_and_uses_first_as_session_library(tmp_path):
    store = create_app(tmp_path / 'data', credentials=no_keys()).state.store
    a = store.import_text('A', TEXT)['id']
    b = store.import_text('B', TEXT.replace('H1-01', 'H2-01'))['id']
    for lib, key in ((a, 'H1-01'), (b, 'H2-01')):
        store.review(lib, key, '练习稿', 'approved', True)
    pid = __import__('app.sessions.profiles', fromlist=['create_profile']).create_profile(
        store, '甲', ['产品经理'], '', [a])['id']  # 资料关联库单选；会话仍可带多库列表
    view = S.create_session_v2(store, 'online', pid, ['产品经理'], [a, b], True)
    assert view['status'] == 'draft' and view['config']['library_ids'] == [a, b]
    assert view['library_id'] == a
