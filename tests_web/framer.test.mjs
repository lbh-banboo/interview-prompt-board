import test from 'node:test';
import assert from 'node:assert/strict';
import { Framer } from '../web/capture/framer.js';
import { CapturePipeline } from '../web/capture/pipeline.js';

test('framer emits exactly 320-sample frames for arbitrary chunk lengths', () => {
  const frames = [];
  const framer = new Framer((p) => frames.push(p));
  const signal = Float32Array.from({ length: 3200 }, (_, i) => Math.sin(i / 10) * 0.5);
  let pos = 0;
  for (const n of [1, 7, 319, 320, 321, 512, 1000, 3200]) {
    framer.push(signal.subarray(pos, Math.min(pos + n, signal.length)));
    pos += n;
  }
  assert.equal(frames.length, 10);
  assert.ok(frames.every((f) => f.length === 320 && f instanceof Int16Array));
  assert.equal(frames[0][100], Math.round(signal[100] * 0x7fff));
});

test('pipeline timestamps advance 20 ms per frame', () => {
  const ts = [];
  const p = new CapturePipeline({ inputRate: 16000, startUs: 1000, onFrame: (_, t) => ts.push(t) });
  p.push(new Float32Array(1024));
  assert.deepEqual(ts, [1000, 21000, 41000]);
});
