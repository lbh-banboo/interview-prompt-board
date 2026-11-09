"""M15-2 凭证存储：环境变量 -> macOS 钥匙串(keyring，可选) -> .env。明文只在进程内流转，不进 HTTP/日志/异常。"""
import os
import re
from pathlib import Path

SERVICE = 'interview-board'
KNOWN_NAMES = ('DEEPSEEK_API_KEY', 'DASHSCOPE_API_KEY', 'SEARCH_API_KEY')
NAME_RE = re.compile(r'^[A-Z][A-Z0-9_]{1,63}$')
SOURCE_NAMES = {'EnvBackend': 'env', 'KeyringBackend': 'keyring', 'DotenvBackend': 'dotenv',
                'MemoryBackend': 'memory'}
SOURCE_LABELS = {'env': '环境变量', 'keyring': '钥匙串', 'dotenv': '.env', 'memory': '内存'}


def _name(name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError('凭证名无效')
    return name


def mask(value):
    """掩码：足够长才露出末 4 位。"""
    return '****' + value[-4:] if len(value) >= 16 else '****'


class MemoryBackend:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, name):
        return self.data.get(name)

    def set(self, name, value):
        self.data[name] = value

    def delete(self, name):
        self.data.pop(name, None)


class EnvBackend:
    """只读环境变量；映射可注入（测试不碰真实 os.environ）。"""

    def __init__(self, environ=None):
        self.environ = os.environ if environ is None else environ

    def get(self, name):
        return self.environ.get(name) or None

    def set(self, name, value):
        raise NotImplementedError

    def delete(self, name):
        pass


class DotenvBackend:
    def __init__(self, path):
        self.path = Path(path)

    def _lines(self):
        try:
            return self.path.read_text('utf-8').splitlines()
        except OSError:
            return []

    def get(self, name):
        for line in self._lines():
            key, sep, val = line.partition('=')
            if sep and key.strip() == name:
                return val.strip().strip('"\'') or None
        return None

    def _write(self, lines):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + ('\n' if lines else ''))
        os.chmod(self.path, 0o600)

    def _without(self, name):
        return [ln for ln in self._lines() if ln.partition('=')[0].strip() != name]

    def set(self, name, value):
        self._write(self._without(name) + [f'{name}={value}'])

    def delete(self, name):
        self._write(self._without(name))


class KeyringBackend:
    """keyring 未安装时 available=False，调用方降级到 .env。"""

    def __init__(self, module=None):
        if module is None:
            try:
                import keyring as module
            except ImportError:
                module = None
        self.kr = module

    @property
    def available(self):
        return self.kr is not None

    def get(self, name):
        try:
            return self.kr.get_password(SERVICE, name) or None
        except Exception:
            return None

    def set(self, name, value):
        try:
            self.kr.set_password(SERVICE, name, value)
        except Exception:
            raise RuntimeError('钥匙串写入失败') from None

    def delete(self, name):
        try:
            self.kr.delete_password(SERVICE, name)
        except Exception:
            pass


class CredentialStore:
    """backends 按读取优先级排列；写入落在第一个可写后端（环境变量只读）。"""

    def __init__(self, backends):
        self.backends = list(backends)

    @classmethod
    def default(cls, env_path, environ=None, keyring_module=None):
        kr = KeyringBackend(keyring_module)
        chain = [EnvBackend(environ)] + ([kr] if kr.available else []) + [DotenvBackend(env_path)]
        return cls(chain)

    def get(self, name):
        _name(name)
        for b in self.backends:
            value = b.get(name)
            if value:
                return value
        return None

    def set(self, name, value):
        _name(name)
        if not isinstance(value, str) or not value.strip() or len(value) > 512 or re.search(r'[\r\n\x00]', value):
            raise ValueError('凭证值无效')
        last = None
        for b in self.backends:
            try:
                b.set(name, value.strip())
                return
            except (NotImplementedError, RuntimeError) as e:
                last = e
        raise RuntimeError('没有可写的凭证后端') from last

    def delete(self, name):
        _name(name)
        for b in self.backends:
            b.delete(name)

    def status(self, name):
        return 'configured' if self.get(name) else 'missing'

    def source(self, name):
        """命中的后端类别名（env/keyring/dotenv/memory）；不返回值，也不返回文件路径。"""
        _name(name)
        for b in self.backends:
            if b.get(name):
                return SOURCE_NAMES.get(type(b).__name__, 'other')
        return None

    def masked(self, name):
        value = self.get(name)
        return mask(value) if value else ''

    def report(self, names=KNOWN_NAMES):
        return {n: dict(status=self.status(n), mask=self.masked(n), source=self.source(n)) for n in names}


def selfcheck(store, names=KNOWN_NAMES):
    """启动自检：缺 Key 不阻止启动，只标记降级。"""
    rep = store.report(names)
    missing = [n for n, v in rep.items() if v['status'] == 'missing']
    return dict(credentials=rep, missing=missing, degraded=bool(missing))
