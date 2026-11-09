// Prints TS-packed frames as JSON for tests/test_frame_compat.py (cross-language byte check).
import { packFrame } from '../web/capture/frame.js';

const cases = [
  { source: 'mic', streamId: 7, generation: 0, seq: 0, tClientUs: 0, replay: false },
  { source: 'meeting', streamId: 0xffffffff, generation: 0xffff, seq: 0xffffffff, tClientUs: 2 ** 48 - 1, replay: true },
  { source: 'room', streamId: 0x01020304, generation: 3, seq: 123456, tClientUs: 1234567890123, replay: false },
];
const out = cases.map((c, n) => {
  const pcm = Int16Array.from({ length: 320 }, (_, i) => ((i * (n + 3) * 97 - 32768) << 16) >> 16);
  return { ...c, pcm: Array.from(pcm), hex: Buffer.from(packFrame({ ...c, pcm })).toString('hex') };
});
process.stdout.write(JSON.stringify(out));
