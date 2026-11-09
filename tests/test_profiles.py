from types import SimpleNamespace

import pytest

from app.core import Store
from app.sessions import profiles as P
from app.sessions import setup as S


def make(tmp_path):
    store = Store(tmp_path)
    lib = store.import_text('库A', '**H1-01｜00:01｜问题一？** 参考回答：答案')['id']
    return store, lib


def test_crud_and_single_library(tmp_path):
    store, lib = make(tmp_path)
    p = P.create_profile(store, ' 代号甲 ', ['管理', '管理', '运营'], '备注', [lib, lib], now=lambda: 5.0)
    assert p['alias'] == '代号甲' and p['roles'] == ['管理', '运营'] and p['created_at'] == 5.0
    assert p['library_ids'] == [lib]
    q = P.update_profile(store, p['id'], '代号乙', ['技术开发'], '', [lib])
    assert q['alias'] == '代号乙' and q['library_ids'] == [lib]
    assert [x['id'] for x in P.list_profiles(store)] == [p['id']]
    P.delete_profile(store, p['id'])
    assert P.list_profiles(store) == []
    with pytest.raises(P.ProfileError) as e:
        P.get_profile(store, p['id'])
    assert e.value.status == 404


def test_validation(tmp_path):
    store, lib = make(tmp_path)
    with pytest.raises(P.ProfileError) as e:
        P.create_profile(store, '  ')
    assert e.value.status == 400
    with pytest.raises(P.ProfileError) as e:
        P.create_profile(store, 'x', library_ids=['nope'])
    assert e.value.status == 404


def test_delete_blocked_by_active_session(tmp_path):
    store, lib = make(tmp_path)
    store.review(lib, 'H1-01', '答案', 'approved', True)
    p = P.create_profile(store, 'x', ['管理'])
    s = S.create_session_v2(store, 'online', p['id'], ['管理'], [lib], True)
    S.mark_ready(store, s['id'], dict(has_fail=False, degraded_banner=None))
    S.start_session(store, s['id'])
    with pytest.raises(P.ProfileError) as e:
        P.delete_profile(store, p['id'])
    assert e.value.status == 409
    S.stop_session(store, s['id'])
    P.delete_profile(store, p['id'])
