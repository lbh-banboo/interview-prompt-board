"""R-0: fetch port candidates at a full commit SHA into third_party/ and write MANIFEST.json (sha256, git blob sha)."""
import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'third_party'
LOCAL = {  # cloned repos: commit is read from git
    'buluoai6-eng/online-meeting-interview-assistant': ('research/repos/buluoai6-eng_online-meeting-interview-assistant', 'MIT', [
        'LICENSE', 'src/speaker-router.js', 'src/question-detector.js', 'src/turn-guard.js',
        'scripts/test-turn-guard.js', 'scripts/test-question-detector.js']),
    'lesue1/interview-assistant': ('research/repos/lesue1_interview-assistant', 'MIT', [
        'LICENSE', 'main.py', 'preflight.py', 'transcript.py', 'test_gate.py']),
}
REMOTE = {  # fetched via the contents API at the repo's current default-branch commit (raw.githubusercontent.com is unreachable here), then pinned by SHA
    'JWM0203/MeetingCopilot': ('Apache-2.0', [
        'LICENSE', 'NOTICE', 'electron/asr/contract.ts', 'electron/asr/aliyunRealtimeEngine.ts', 'electron/asr/cloudEngine.ts',
        'electron/asr/vad.ts', 'electron/llm/prompts.ts', 'electron/providerErrors.ts', 'electron/diagnostics.ts',
        'tools/funasr_stream_server.py', 'tools/funasr_device.py']),
    'ukez15/interview-copilot': ('MIT', [
        'LICENSE', 'src/desktop/src/capture/pcm-worklet.ts', 'src/desktop/src/capture/pcm.ts',
        'src/desktop/src/capture/frame-buffer.ts', 'src/desktop/src/capture/audio-sender.ts',
        'src/desktop/src/capture/backoff.ts']),
    'Kaalia0912/ai-interview-assistant': ('MIT', [
        'LICENSE', 'rag-assistant/app/core/structurer.py', 'rag-assistant/app/core/reranker.py',
        'rag-assistant/app/core/generator.py', 'rag-assistant/data/mianjing-public/00-语料来源与许可.md']),
}


def get(url):
    req = urllib.request.Request(url, headers={'Accept': 'application/vnd.github.raw'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def record(manifest, repo, commit, lic, rel, data):
    dest = OUT / f"{repo.replace('/', '_')}@{commit[:12]}" / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    blob = hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()
    manifest.append(dict(repo=repo, commit=commit, path=rel, license=lic, sha256=hashlib.sha256(data).hexdigest(),
                         git_blob=blob, bytes=len(data), status='ok'))


def main():
    manifest = []
    for repo, (path, lic, files) in LOCAL.items():
        commit = subprocess.check_output(['git', '-C', str(ROOT / path), 'rev-parse', 'HEAD'], text=True).strip()
        for rel in files:
            f = ROOT / path / rel
            if f.exists():
                record(manifest, repo, commit, lic, rel, f.read_bytes())
            else:
                manifest.append(dict(repo=repo, commit=commit, path=rel, status='missing_local'))
    for repo, (lic, files) in REMOTE.items():
        commit = json.loads(get(f'https://api.github.com/repos/{repo}/commits?per_page=1'))[0]['sha']
        for rel in files:
            try:
                record(manifest, repo, commit, lic, rel, get(f'https://api.github.com/repos/{repo}/contents/{urllib.parse.quote(rel)}?ref={commit}'))
            except urllib.error.HTTPError as exc:
                manifest.append(dict(repo=repo, commit=commit, path=rel, status=f'http_{exc.code}'))
    OUT.mkdir(exist_ok=True)
    (OUT / 'MANIFEST.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    bad = [m for m in manifest if m['status'] != 'ok']
    print(f'{len(manifest) - len(bad)} ok, {len(bad)} not ok')
    for m in bad:
        print(' ', m['repo'], m['path'], m['status'])
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
