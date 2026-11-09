import test from 'node:test';
import assert from 'node:assert/strict';
import { LevelMeter, SILENT_WARN_MS } from '../web/capture/level.js';
import { SourceWatch } from '../web/capture/source-watch.js';

test('level meter reports RMS every 50 ms (800 samples at 16 kHz)', () => {
  const readings = [];
  const m = new LevelMeter((r) => readings.push(r));
  m.push(Float32Array.from({ length: 2400 }, (_, i) => (i % 2 ? 0.5 : -0.5)));
  assert.equal(readings.length, 3);
  assert.ok(Math.abs(readings[0].rms - 0.5) < 1e-6);
  assert.equal(readings[0].silentMs, 0);
});

test('silence accumulates and resets; hasEnergy tracks non-zero input', () => {
  const readings = [];
  const m = new LevelMeter((r) => readings.push(r));
  m.push(new Float32Array(800 * 200));
  assert.equal(m.hasEnergy, false);
  assert.equal(readings.at(-1).silentMs, SILENT_WARN_MS);
  m.push(new Float32Array(800).fill(0.1));
  assert.equal(readings.at(-1).silentMs, 0);
  assert.equal(m.hasEnergy, true);
});

test('source watch: lost after 2 s without frames, recovers on frame, track end is lost', () => {
  let now = 0; const events = [];
  const w = new SourceWatch({ now: () => now, onLost: (r) => events.push(r), onRecovered: () => events.push('ok') });
  now = 2000; w.tick(); assert.deepEqual(events, []);
  now = 2001; w.tick(); assert.deepEqual(events, ['no_frames']);
  now = 2100; w.tick(); assert.equal(events.length, 1);
  w.frame(); assert.deepEqual(events, ['no_frames', 'ok']);
  w.trackEnded(); assert.deepEqual(events, ['no_frames', 'ok', 'track_ended']);
  w.frame(); assert.equal(w.isLost, true);
});
