"""Cross-language check: frames packed by the TypeScript sender (node) must be byte-identical to
app.audio.frame.pack() and parse() cleanly. Needs `npm run build` output (web/capture/frame.js)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.audio.frame import pack, parse

ROOT = Path(__file__).resolve().parents[1]
node = shutil.which('node')
pytestmark = pytest.mark.skipif(not node or not (ROOT / 'web/capture/frame.js').exists(),
                                reason='node or built web/capture/frame.js missing')


def ts_frames():
    out = subprocess.run([node, str(ROOT / 'tests_web/emit_frames.mjs')], capture_output=True, check=True, timeout=30)
    return json.loads(out.stdout)


def test_ts_frames_equal_python_pack_and_parse():
    cases = ts_frames()
    assert len(cases) == 3
    for c in cases:
        pcm = b''.join(v.to_bytes(2, 'little', signed=True) for v in c['pcm'])
        data = bytes.fromhex(c['hex'])
        expected = pack(c['source'], c['streamId'], c['generation'], c['seq'], pcm,
                        t_client_us=c['tClientUs'], replay=c['replay'])
        assert data == expected
        f = parse(data)
        assert (f['source'], f['stream_id'], f['generation'], f['seq'], f['t_client_us'], f['replay']) == \
            (c['source'], c['streamId'], c['generation'], c['seq'], c['tClientUs'], c['replay'])
        assert f['pcm'] == pcm
