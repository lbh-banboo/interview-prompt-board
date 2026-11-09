// Original code. Byte-exact mirror of app/audio/frame.py (plan §7.2): 24-byte header + 640 B PCM16LE.
//   magic 'IA'(2) | version(1)=1 | source(1) | flags(1, bit0=replay) | reserved(1) |
//   stream_id(4, BE) | capture_generation(2, BE) | reserved(2) | seq(4, BE) | t_client_us(6, BE)
import { FRAME_SAMPLES } from './pcm.js';
export const HEADER_SIZE = 24;
export const PAYLOAD_SIZE = FRAME_SAMPLES * 2;
export const FRAME_SIZE = HEADER_SIZE + PAYLOAD_SIZE;
export const FLAGS_OFFSET = 4;
export const FLAG_REPLAY = 1;
export const SOURCE_CODES = { mic: 1, meeting: 2, room: 3 };
export function packFrame(f) {
    if (f.pcm.length !== FRAME_SAMPLES) {
        throw new Error(`PCM frame must hold ${FRAME_SAMPLES} samples`);
    }
    const out = new Uint8Array(FRAME_SIZE);
    const view = new DataView(out.buffer);
    out[0] = 0x49; // 'I'
    out[1] = 0x41; // 'A'
    out[2] = 1;
    out[3] = SOURCE_CODES[f.source];
    out[FLAGS_OFFSET] = f.replay ? FLAG_REPLAY : 0;
    view.setUint32(6, f.streamId >>> 0);
    view.setUint16(10, f.generation & 0xffff);
    view.setUint32(14, f.seq >>> 0);
    const t = Math.max(0, Math.floor(f.tClientUs));
    view.setUint16(18, Math.floor(t / 2 ** 32) & 0xffff);
    view.setUint32(20, t % 2 ** 32);
    for (let i = 0; i < FRAME_SAMPLES; i++) {
        view.setInt16(HEADER_SIZE + i * 2, f.pcm[i], true);
    }
    return out;
}
/** Copy of a packed frame with the replay flag set. */
export function withReplayFlag(frame) {
    const copy = frame.slice();
    copy[FLAGS_OFFSET] = copy[FLAGS_OFFSET] | FLAG_REPLAY;
    return copy;
}
