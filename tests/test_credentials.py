import pytest

from app.credentials import (CredentialStore, DotenvBackend, EnvBackend, KeyringBackend, MemoryBackend, mask, selfcheck)

KEY = 'sk-TESTKEY12abcd'  # pragma: allowlist secret（假 Key，长度 16 触发露尾掩码）


class FakeKeyring:
    def __init__(self, fail=False):
        self.d, self.fail = {}, fail

    def get_password(self, s, n):
        if self.fail:
            raise OSError(KEY)
        return self.d.get((s, n))

    def set_password(self, s, n, v):
        if self.fail:
            raise OSError(v)
        self.d[(s, n)] = v

    def delete_password(self, s, n):
        if self.fail:
            raise OSError
        self.d.pop((s, n), None)


def test_mask():
    assert mask(KEY) == '****abcd' and mask('short') == '****'


def test_priority_env_keyring_dotenv(tmp_path):
    env = tmp_path / '.env'
    kr = FakeKeyring()
    s = CredentialStore.default(env, environ={}, keyring_module=kr)
    assert s.status('DEEPSEEK_API_KEY') == 'missing' and s.masked('DEEPSEEK_API_KEY') == ''
    s.set('DEEPSEEK_API_KEY', KEY)
    assert kr.d and not env.exists()  # 钥匙串优先写入
    DotenvBackend(env).set('DEEPSEEK_API_KEY', 'from-dotenv-0000000000')
    assert s.get('DEEPSEEK_API_KEY') == KEY
    s2 = CredentialStore.default(env, environ={'DEEPSEEK_API_KEY': 'from-env-000000000000'}, keyring_module=kr)
    assert s2.get('DEEPSEEK_API_KEY') == 'from-env-000000000000'
    s.delete('DEEPSEEK_API_KEY')
    assert s.status('DEEPSEEK_API_KEY') == 'missing'


def test_without_keyring_falls_back_to_dotenv(tmp_path, monkeypatch):
    import builtins
    real = builtins.__import__

    def no_keyring(name, *a, **k):
        if name == 'keyring':
            raise ImportError
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, '__import__', no_keyring)
    assert not KeyringBackend().available
    s = CredentialStore.default(tmp_path / '.env', environ={})
    s.set('SEARCH_API_KEY', KEY)
    assert (tmp_path / '.env').stat().st_mode & 0o777 == 0o600
    assert s.get('SEARCH_API_KEY') == KEY
    s.set('SEARCH_API_KEY', 'other-value-0000')
    assert (tmp_path / '.env').read_text().count('SEARCH_API_KEY') == 1


def test_keyring_failure_degrades_and_never_leaks(tmp_path):
    kr = FakeKeyring(fail=True)
    s = CredentialStore.default(tmp_path / '.env', environ={}, keyring_module=kr)
    s.set('DEEPSEEK_API_KEY', KEY)  # 钥匙串失败 -> 落到 .env
    assert s.get('DEEPSEEK_API_KEY') == KEY
    s.delete('DEEPSEEK_API_KEY')
    only_kr = CredentialStore([KeyringBackend(kr)])
    with pytest.raises(RuntimeError) as e:
        only_kr.set('DEEPSEEK_API_KEY', KEY)
    assert KEY not in repr(e.value) and KEY not in str(e.value.__cause__)


def test_no_writable_backend_and_validation():
    s = CredentialStore([EnvBackend({})])
    with pytest.raises(RuntimeError):
        s.set('DEEPSEEK_API_KEY', KEY)
    s = CredentialStore([MemoryBackend()])
    for bad in ('', '  ', 'a\nb', None, 'x' * 600):
        with pytest.raises(ValueError):
            s.set('DEEPSEEK_API_KEY', bad)
    for bad in ('lower', '', None, 'A'):
        with pytest.raises(ValueError):
            s.get(bad)


def test_dotenv_parsing(tmp_path):
    p = tmp_path / '.env'
    p.write_text('# c\nA_KEY="quoted"\nEMPTY=\n')
    d = DotenvBackend(p)
    assert d.get('A_KEY') == 'quoted' and d.get('EMPTY') is None and d.get('NOPE') is None
    assert DotenvBackend(tmp_path / 'absent').get('A_KEY') is None


def test_report_source_is_category_only(tmp_path, monkeypatch):
    import builtins
    real = builtins.__import__

    def no_keyring(name, *a, **k):
        if name == 'keyring':
            raise ImportError
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, '__import__', no_keyring)
    env = tmp_path / '.env'
    s = CredentialStore.default(env, environ={'DEEPSEEK_API_KEY': KEY})
    r = s.report(('DEEPSEEK_API_KEY', 'SEARCH_API_KEY'))
    assert r['DEEPSEEK_API_KEY'] == dict(status='configured', mask='****abcd', source='env')
    assert r['SEARCH_API_KEY'] == dict(status='missing', mask='', source=None)
    assert KEY not in repr(r)

    s2 = CredentialStore([MemoryBackend({'SEARCH_API_KEY': KEY}), DotenvBackend(env)])
    assert s2.report(('SEARCH_API_KEY',))['SEARCH_API_KEY']['source'] == 'memory'


def test_selfcheck_degraded_when_key_deleted():
    s = CredentialStore([MemoryBackend({'DEEPSEEK_API_KEY': KEY})])
    assert selfcheck(s)['missing'] == ['DASHSCOPE_API_KEY', 'SEARCH_API_KEY']
    s.delete('DEEPSEEK_API_KEY')
    r = selfcheck(s)
    assert r['degraded'] and 'DEEPSEEK_API_KEY' in r['missing'] and KEY not in repr(r)
