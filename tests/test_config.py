import json

import pytest

from app.config import ConfigManager, load_config, validate_patch


def test_defaults_load_and_merge(tmp_path):
    cfg, errors = load_config()
    assert errors == [] and cfg['model']['name'] == 'deepseek-flash'
    assert cfg['outbound'] == dict(asr_audio=True, llm_snippets=True, web_questions=True, asr_answerer=False)
    assert cfg['budget']['max_inflight'] == 4 and cfg['session']['idle_timeout_s'] == 7200
    user = tmp_path / 'config.json'
    user.write_text(json.dumps({'turn': {'T_silence': 300}, 'outbound': {'web_questions': False}}))
    cfg, errors = load_config(user_path=user)
    assert errors == [] and cfg['turn']['T_silence'] == 300 and cfg['turn']['T_long'] == 700
    assert cfg['outbound']['web_questions'] is False and cfg['outbound']['asr_audio'] is True


def test_missing_and_corrupt_user_file(tmp_path):
    assert load_config(user_path=tmp_path / 'none.json')[1] == []
    bad = tmp_path / 'config.json'
    bad.write_text('{oops')
    cfg, errors = load_config(user_path=bad)
    assert len(errors) == 1 and cfg['turn']['T_silence'] == 200


@pytest.mark.parametrize('patch', [
    {'turn': {'T_silence': 'x'}}, {'turn': {'T_silence': 1}}, {'turn': {'T_silence': True}},
    {'turn': {'T_silence': 200.5}}, {'outbound': {'asr_audio': 1}}, {'model': {'name': ''}},
    {'model': {'name': 5}}, {'budget': {'daily_limit': -1}}, {'retrieval_thresholds': {'margin': 2}},
    {'nope': 1}, {'turn': {'nope': 1}}, [],
])
def test_invalid_rejected(patch):
    good, errors = validate_patch(patch)
    assert errors and not good


def test_optional_threshold_and_numbers():
    good, errors = validate_patch({'retrieval_thresholds': {'tau_cos': None, 'margin': 0.05}, 'budget': {'daily_limit': 0}})
    assert errors == [] and good['retrieval_thresholds.tau_cos'] is None and good['budget.daily_limit'] == 0


@pytest.mark.parametrize('key, value', [
    ('model', 'https://api.example.test/v1'), ('search', 'https://api.deepseek.com/v1/tools/search'),
    ('model', 'http://127.0.0.1:9000/v1'), ('search', 'http://localhost:9000/search'),
])
def test_url_items_accept_https_and_loopback_http(key, value):
    leaf = 'base_url' if key == 'model' else 'endpoint'
    good, errors = validate_patch({key: {leaf: value}})
    assert errors == [] and good[f'{key}.{leaf}'] == value


@pytest.mark.parametrize('value', [
    'http://api.example.test/v1',            # 明文 http 会把 Key 送出本机
    'ftp://api.example.test/v1', 'file:///etc/passwd', 'javascript:alert(1)',
    'https://user:pw@api.example.test/v1',   # 账号密码内嵌
    'https:///no-host', 'https://', 'not a url at all',
    'http://127.0.0.1.evil.example/v1',      # 回环前缀伪装
])
def test_url_items_reject_unsafe_targets(value):
    for patch in ({'model': {'base_url': value}}, {'search': {'endpoint': value}}):
        good, errors = validate_patch(patch)
        assert errors and not good, value


def test_unsafe_url_in_user_file_falls_back_to_default(tmp_path):
    user = tmp_path / 'config.json'
    user.write_text(json.dumps({'model': {'base_url': 'http://api.example.test/v1'}}))
    cfg, errors = load_config(user_path=user)
    assert cfg['model']['base_url'] == 'https://api.deepseek.com' and errors


def test_mic_as_question_default_on_and_toggle_validated():
    cfg, errors = load_config()
    assert errors == [] and cfg['turn']['mic_as_question'] is True
    good, errors = validate_patch({'turn': {'mic_as_question': False}})
    assert errors == [] and good['turn.mic_as_question'] is False
    good, errors = validate_patch({'turn': {'mic_as_question': 'yes'}})
    assert not good and errors


def test_invalid_user_items_fall_back_and_cross_check(tmp_path):
    user = tmp_path / 'config.json'
    user.write_text(json.dumps({'turn': {'T_silence': 'bad', 'T_long': 600}}))
    cfg, errors = load_config(user_path=user)
    assert errors and cfg['turn']['T_long'] == 600 and cfg['turn']['T_silence'] == 200
    user.write_text(json.dumps({'turn': {'T_silence': 900}}))
    cfg, errors = load_config(user_path=user)
    assert any('T_silence' in e for e in errors) and cfg['turn']['T_silence'] == 200
    user.write_text(json.dumps({'timeouts': {'web_first_ms': 9000}}))
    assert any('联网' in e for e in load_config(user_path=user)[1])


def test_update_persists_and_rejects(tmp_path):
    m = ConfigManager(tmp_path / 'sub' / 'config.json')
    m.update({'turn': {'T_silence': 250}})
    m.update({'outbound': {'llm_snippets': False}})
    saved = json.loads((tmp_path / 'sub' / 'config.json').read_text())
    assert saved == {'turn': {'T_silence': 250}, 'outbound': {'llm_snippets': False}}
    assert ConfigManager(tmp_path / 'sub' / 'config.json').config['turn']['T_silence'] == 250
    with pytest.raises(ValueError):
        m.update({'turn': {'T_silence': 'x'}})
    with pytest.raises(ValueError, match='T_hard'):
        m.update({'turn': {'T_hard': 300, 'T_long': 700}})
    assert json.loads((tmp_path / 'sub' / 'config.json').read_text()) == saved


def test_snapshot_is_frozen_for_session(tmp_path):
    m = ConfigManager(tmp_path / 'config.json')
    snap = m.snapshot_for_session()
    m.update({'turn': {'T_silence': 400}})
    assert json.loads(snap)['turn']['T_silence'] == 200
    assert json.loads(m.snapshot_for_session())['turn']['T_silence'] == 400


def test_asr_budget_defaults_and_validation(tmp_path):
    cfg, errors = load_config()
    assert errors == []
    assert cfg['budget']['asr_daily_limit'] == 5.0 and cfg['budget']['asr_session_limit'] == 1.5
    assert cfg['turn']['T_new_turn'] == 1200 and cfg['turn']['T_monologue'] == 10000
    good, errors = validate_patch({'budget': {'asr_daily_limit': 0, 'asr_session_limit': 2.5}})
    assert errors == [] and good['budget.asr_daily_limit'] == 0
    for patch in ({'budget': {'asr_daily_limit': -1}}, {'budget': {'asr_session_limit': 'x'}},
                  {'turn': {'max_turn_chars': 1}}, {'turn': {'monologue_segments': 11}}):
        good, errors = validate_patch(patch)
        assert not good and errors


def test_index_provider_uses_session_snapshot_thresholds(tmp_path):
    from app.core import Store
    from app.realtime.answer import IndexProvider
    from app.retrieval.index import HashEmbedder
    from app.retrieval.search import Thresholds, load_thresholds

    store = Store(tmp_path)
    from test_core import TEXT
    lib = store.import_text('库', TEXT)['id']
    provider = IndexProvider(store, HashEmbedder())
    provider.prepare(lib)
    assert provider.threshold_source == 'default'
    base = load_thresholds(provider(lib).index.embedder.revision)
    assert provider(lib).th == base

    provider.set_thresholds_provider(lambda: {'tau_cos': 0.5, 'tau_cos_strong': None,
                                              'tau_mid': None, 'margin': None})
    provider.prepare(lib)
    th = provider(lib).th
    assert provider.threshold_source == 'snapshot'
    assert th.tau_cos == 0.5 and th.tau_cos_strong == base.tau_cos_strong
    assert isinstance(th, Thresholds)
