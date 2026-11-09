import test from 'node:test';
import assert from 'node:assert/strict';
import { SourceStatus, NO_SOUND_TEXT, RECOVER_MS } from '../web/capture/source-status.js';

const loud = { rms: 0.1, silentMs: 0 };
const quiet = (silentMs = 50) => ({ rms: 0, silentMs });

test('before validation levels do not change the text', () => {
  const s = new SourceStatus();
  assert.equal(s.onLevel(loud), null);
});

test('validation success shows normal and later levels keep normal', () => {
  const s = new SourceStatus();
  assert.equal(s.validate(true), '正常');
  assert.equal(s.onLevel(loud), '正常');
  assert.match(s.onLevel(quiet(10_000)), /静音超过 10 秒/);
});

test('failed validation is not overwritten by silent levels', () => {
  const s = new SourceStatus();
  assert.equal(s.validate(false), NO_SOUND_TEXT);
  for (let i = 0; i < 100; i++) assert.equal(s.onLevel(quiet(50 * (i + 1))), NO_SOUND_TEXT);
});

test('failed validation is not cleared by a single loud reading', () => {
  const s = new SourceStatus();
  s.validate(false);
  assert.equal(s.onLevel(loud), NO_SOUND_TEXT);
  assert.equal(s.onLevel(quiet()), NO_SOUND_TEXT);
  assert.equal(s.onLevel(loud), NO_SOUND_TEXT); // loud run restarted
});

test('failed validation recovers after sustained sound', () => {
  const s = new SourceStatus();
  s.validate(false);
  let last;
  for (let ms = 0; ms < RECOVER_MS; ms += 50) last = s.onLevel(loud);
  assert.equal(last, '正常');
  assert.equal(s.onLevel(loud), '正常');
});

test('meeting source uses the meeting-specific no-sound text', () => {
  const s = new SourceStatus('会议音频无声音');
  assert.equal(s.validate(false), '会议音频无声音');
  assert.equal(s.onLevel(quiet(100)), '会议音频无声音');
  assert.equal(s.validate(true), '正常');
  assert.equal(s.onLevel(loud), '正常');
});
