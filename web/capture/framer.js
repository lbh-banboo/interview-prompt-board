// Ported from ukez15/interview-copilot@4a80d842fa22a359b4ab05eea8b2c8432c113e07:src/desktop/src/capture/pcm.ts (FrameAssembler), MIT © 2026 ukez15.
// Modified: 100 ms -> 20 ms (320 samples); emits Int16Array PCM without the speaker byte.
import { FRAME_SAMPLES, floatToPcm16 } from './pcm.js';
/** Collects 16 kHz samples of any chunk length and emits exactly 320-sample PCM16 frames. */
export class Framer {
    emit;
    pending = new Float32Array(FRAME_SAMPLES);
    filled = 0;
    constructor(emit) {
        this.emit = emit;
    }
    push(samples) {
        let offset = 0;
        while (offset < samples.length) {
            const take = Math.min(FRAME_SAMPLES - this.filled, samples.length - offset);
            this.pending.set(samples.subarray(offset, offset + take), this.filled);
            this.filled += take;
            offset += take;
            if (this.filled === FRAME_SAMPLES) {
                this.emit(floatToPcm16(this.pending));
                this.filled = 0;
            }
        }
    }
}
