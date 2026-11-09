import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { packFrame, withReplayFlag, FRAME_SIZE } from '../web/capture/frame.js';

const pcm = Int16Array.from({ length: 320 }, (_, i) => ((i * 37 - 5000) << 16) >> 16);

// Golden values produced by app/audio/frame.py pack('meeting', 0x01020304, 0x0506, 0x0708090a, pcm, t=0x010203040506, replay=True)
const GOLDEN_HEADER = '49410102010001020304050600000708090a010203040506';
const GOLDEN_SHA256 = '9c38be5fd0132930d83dc49aeef11430f01d4781759e53958e91a9fc9fe1663b';

test('frame header matches the Python golden vector byte for byte', () => {
  const f = packFrame({ source: 'meeting', streamId: 0x01020304, generation: 0x0506, seq: 0x0708090a, tClientUs: 0x010203040506, replay: true, pcm });
  assert.equal(f.length, FRAME_SIZE);
  assert.equal(Buffer.from(f.subarray(0, 24)).toString('hex'), GOLDEN_HEADER);
  assert.equal(createHash('sha256').update(f).digest('hex'), GOLDEN_SHA256);
});

test('withReplayFlag sets bit0 on a copy only', () => {
  const live = packFrame({ source: 'mic', streamId: 1, generation: 0, seq: 0, tClientUs: 0, pcm });
  const replay = withReplayFlag(live);
  assert.equal(live[4], 0);
  assert.equal(replay[4], 1);
});

test('rejects wrong PCM length', () => {
  assert.throws(() => packFrame({ source: 'mic', streamId: 1, generation: 0, seq: 0, tClientUs: 0, pcm: new Int16Array(319) }));
});
