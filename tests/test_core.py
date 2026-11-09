import logging
import os
import stat

import pytest
from app.core import Store, parse_markdown

TEXT = '**H1-01｜00:01｜你的项目是什么？** 参考回答：我做过一个原型。上线规模 [待核]。'

@pytest.fixture
def prepared(tmp_path):
    s = Store(tmp_path)
    lib = s.import_text('样例', TEXT)['id']
    return s, lib


def test_parser_keeps_caveats():
    e = parse_markdown(TEXT)[0]
    assert e['flagged'] and '[待核]' in e['answer'] and e['line'] == 1


def test_duplicate_ids_rejected():
    with pytest.raises(ValueError):
        parse_markdown(TEXT + '\n' + TEXT)


def test_unsupported_format_rejected():
    with pytest.raises(ValueError):
        parse_markdown('# no questions')


def test_import_idempotent(prepared):
    s, lib = prepared
    assert s.import_text('again', TEXT)['id'] == lib


def test_pending_not_searchable(prepared):
    s, lib = prepared
    assert s.search(lib, '你的项目是什么')['results'] == []


def test_requires_confirmation(prepared):
    s, lib = prepared
    with pytest.raises(ValueError):
        s.review(lib, 'H1-01', 'answer', 'approved', False)


def test_no_unreviewed_session(prepared):
    s, lib = prepared
    with pytest.raises(ValueError):
        s.start('online', lib)


def ready(prepared):
    s, lib = prepared
    s.review(lib, 'H1-01', '我做过一个原型。上线规模 [待核]。', 'approved', True)
    return s, lib, s.start('online', lib)['id']


def test_exact_match_extracts_not_invents(prepared):
    s, lib, key = ready(prepared)
    r = s.search(lib, '你的项目是什么？')['results'][0]
    assert r['score'] == 1
    assert '[待核]' in r['outline']['full']
    assert r['outline']['example'] == ''


def test_one_session_and_frozen_library(prepared):
    s, lib, key = ready(prepared)
    with pytest.raises(ValueError):
        s.start('offline', lib)
    with pytest.raises(ValueError):
        s.review(lib, 'H1-01', 'changed', 'approved', True)
    s.stop(key)
    assert s.start('offline', lib)['status'] == 'active'


def test_queue_and_followup(prepared):
    s, lib, key = ready(prepared)
    first = s.event(key, 'question_complete', 'one')['turn_id']
    second = s.event(key, 'question_complete', 'two')['turn_id']
    assert s.session(key)['turn_id'] == first
    s.event(key, 'backchannel')
    assert s.session(key)['turn_id'] == first
    assert s.event(key, 'follow_up', turn_id=first, content='more')['disposition'] == 'visible'
    s.event(key, 'answer_complete', turn_id=first)
    assert s.session(key)['turn_id'] == second


def test_change_and_late_results(prepared):
    s, lib, key = ready(prepared)
    first = s.event(key, 'question_complete', 'one')['turn_id']
    second = s.event(key, 'change_topic', 'two')['turn_id']
    assert first != second
    assert s.event(key, 'supplement', turn_id=first, content='late')['disposition'] == 'archived'
    s.stop(key)
    assert s.event(key, 'supplement', turn_id=second, content='later')['disposition'] == 'archived'


def test_unknown_turn_rejected(prepared):
    s, lib, key = ready(prepared)
    with pytest.raises(ValueError):
        s.event(key, 'supplement', turn_id='other', content='wrong')


def test_export_separates_suggestions(prepared):
    s, lib, key = ready(prepared)
    s.event(key, 'question_complete', 'test')
    result = s.export(key)
    assert '系统建议' in result
    assert '不等于实际发言' in result and '实时 ASR 未接入' in result


def test_store_forces_data_root_permission_700(tmp_path):
    root = tmp_path / 'data'
    root.mkdir()
    os.chmod(root, 0o755)
    Store(root)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_store_chmod_failure_warns_but_does_not_block(tmp_path, monkeypatch, caplog):
    root = tmp_path / 'data'
    root.mkdir()

    def fail(*_a, **_kw):
        raise OSError('chmod not permitted')

    monkeypatch.setattr('app.core.os.chmod', fail)
    with caplog.at_level(logging.WARNING, logger='app.core'):
        s = Store(root)
    assert s.path.exists()
    assert any('0o700' in rec.getMessage() for rec in caplog.records)
