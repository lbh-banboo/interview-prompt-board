"""开源前密钥扫描：只报文件名与行号，绝不打印 Key 值。

规则：
1. 项目内形如 sk-[A-Za-z0-9]{16,} 的 DeepSeek 风格 Key（测试夹具行可写
   `# pragma: allowlist secret` 显式豁免；真实值命中不受豁免影响）。
2. 当前进程环境变量 DASHSCOPE_API_KEY / DEEPSEEK_API_KEY / SEARCH_API_KEY 的实际值，
   以及 data/.env 中同名变量的值，是否出现在任何将被提交的文件里。

排除目录：.venv、node_modules、data/（本机凭证存储，由 .gitignore 忽略）。
退出码：0 未发现；1 发现；2 参数错误。
"""
import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAMES = ('DASHSCOPE_API_KEY', 'DEEPSEEK_API_KEY', 'SEARCH_API_KEY')
SKIP_DIRS = {'.venv', 'node_modules', 'data', '.git', '__pycache__', '.pytest_cache', '.mypy_cache'}
SKIP_SUFFIXES = {'.pyc', '.pyo', '.so', '.dylib', '.dll', '.onnx', '.wav', '.mp3', '.m4a', '.png', '.jpg',
                 '.jpeg', '.gif', '.pdf', '.zip', '.gz', '.sqlite3', '.coverage'}
PRAGMA = 'allowlist secret'
PATTERN = re.compile(r'sk-[A-Za-z0-9]{16,}')
MAX_BYTES = 8_000_000


@dataclass(frozen=True)
class Hit:
    path: Path
    line: int
    kind: str          # 类别描述，不含值


def _read_text(path):
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b'\x00' in raw[:4096]:
        return None
    return raw.decode('utf-8', errors='ignore')


def dotenv_values(path=ROOT / 'data' / '.env'):
    """读取本机 data/.env 的凭证值；只用于比对，不落日志。"""
    values = {}
    try:
        lines = Path(path).read_text('utf-8').splitlines()
    except OSError:
        return values
    for line in lines:
        key, sep, val = line.partition('=')
        if sep and key.strip() in NAMES:
            v = val.strip().strip('"\'')
            if v:
                values[key.strip()] = v
    return values


def current_values(environ=None):
    env = os.environ if environ is None else environ
    return {n: env[n] for n in NAMES if env.get(n)}


def scan(root=ROOT, values=None):
    """返回 Hit 列表。values 形如 {'DASHSCOPE_API_KEY': '值'}；默认取环境变量 + data/.env。"""
    root = Path(root)
    if values is None:
        values = {}
        values.update(current_values())
        values.update(dotenv_values(root / 'data' / '.env'))
    values = {n: v for n, v in dict(values).items() if v and len(v) >= 8}
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            text = _read_text(path)
            if text is None:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if PRAGMA in line:
                    pass  # 仅豁免假 Key 形态；真实值仍按 values 比对
                elif PATTERN.search(line):
                    hits.append(Hit(path, lineno, 'sk- 形态 Key'))
                for key, value in values.items():
                    if value in line:
                        hits.append(Hit(path, lineno, f'{key} 的实际值'))
    return hits


def format_hits(hits, root=ROOT):
    root = Path(root)
    lines = []
    for h in hits:
        try:
            rel = h.path.relative_to(root)
        except ValueError:
            rel = h.path
        lines.append(f'{rel}:{h.line}: {h.kind}')
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description='开源前扫描：只报位置，不打印 Key')
    ap.add_argument('root', nargs='?', default=str(ROOT), help='项目根目录（默认仓库根）')
    args = ap.parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print('目录不存在', file=sys.stderr)
        return 2
    hits = scan(root)
    if not hits:
        print('未发现疑似 Key')
        return 0
    print('发现疑似 Key（仅位置；请清除或轮换，勿复制值到 issue/日志）：')
    for line in format_hits(hits, root):
        print(line)
    return 1


if __name__ == '__main__':
    sys.exit(main())
