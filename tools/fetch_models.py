"""按 models/MANIFEST.json 下载并校验本地模型权重（权重不入库）。

用法：
  .venv/bin/python tools/fetch_models.py           # 缺失或哈希不符的文件重新下载
  .venv/bin/python tools/fetch_models.py --check   # 只校验本地文件的 sha256，不联网
镜像：设置 HF_ENDPOINT（默认 https://huggingface.co），例如 HF_ENDPOINT=https://hf-mirror.com。
下载一律取 main 分支，以 MANIFEST 里的 sha256 为准；哈希不符的下载会被丢弃。
"""
import argparse
import hashlib
import io
import json
import os
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / 'models'
MIRROR_HOST = 'https://hf-mirror.com'  # MANIFEST 里记录的取得地址
WHEEL_MEMBER = 'silero_vad/data/silero_vad_16k_op15.onnx'  # Silero 模型取自 PyPI 轮子内


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def read_url(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'interview-board-fetch-models'})
    with urllib.request.urlopen(req, timeout=60) as resp:
        while chunk := resp.read(1 << 20):
            yield chunk


def download(url, dest, sha):
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + '.part')
    h = hashlib.sha256()
    with open(part, 'wb') as f:
        for chunk in read_url(url):
            h.update(chunk)
            f.write(chunk)
    if h.hexdigest() != sha:
        part.unlink()
        raise RuntimeError(f'{dest.name} 下载后 sha256 不符，已丢弃')
    part.replace(dest)


def targets(entry):
    """[(相对 models/ 的路径, sha256, 下载地址或 None)]；silero 走轮子，下载地址为 None。"""
    endpoint = os.environ.get('HF_ENDPOINT', 'https://huggingface.co').rstrip('/')
    main = entry['file']
    if 'source_wheel_sha256' in entry:
        return [(main, entry['sha256'], None)]
    base = entry['source_url'].replace(MIRROR_HOST, endpoint, 1).rsplit('/', 1)[0]
    rel_dir = str(Path(main).parent)
    out = [(main, entry['sha256'], f'{base}/{Path(main).name}')]
    for name, sha in (entry.get('aux_sha256') or {}).items():
        out.append((str(Path(rel_dir) / name), sha, f'{base}/{name}'))
    return out


def fetch_silero(entry, dest):
    data = b''.join(read_url(entry['source_url']))
    if hashlib.sha256(data).hexdigest() != entry['source_wheel_sha256']:
        raise RuntimeError('silero-vad 轮子 sha256 不符')
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        model = z.read(WHEEL_MEMBER)
    if hashlib.sha256(model).hexdigest() != entry['sha256']:
        raise RuntimeError('silero 模型 sha256 不符')
    dest.write_bytes(model)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true', help='只校验本地文件，不联网')
    check_only = ap.parse_args().check
    bad = 0
    for entry in json.loads((MODELS / 'MANIFEST.json').read_text('utf-8')):
        for rel, sha, url in targets(entry):
            path = MODELS / rel
            if path.is_file() and sha256_of(path) == sha:
                print(f'ok       {rel}')
                continue
            state = '哈希不符' if path.is_file() else '缺失'
            if check_only:
                print(f'{state}   {rel}')
                bad += 1
                continue
            print(f'下载     {rel}（{state}）', flush=True)
            try:
                if url is None:
                    fetch_silero(entry, path)
                else:
                    download(url, path, sha)
            except Exception as exc:  # 网络或校验失败：继续处理其余文件，最后统一报错
                print(f'失败     {rel}: {exc}', file=sys.stderr)
                bad += 1
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
