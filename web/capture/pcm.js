// Ported from ukez15/interview-copilot@4a80d842fa22a359b4ab05eea8b2c8432c113e07:src/desktop/src/capture/pcm.ts, MIT © 2026 ukez15.
// Modified: 100 ms speaker-byte frames -> 20 ms / 320-sample frames with the §7.2 header (frame.ts);
// the linear-interpolation Resampler was NOT ported (no anti-aliasing; see resampler.ts); the
// FrameAssembler moved to framer.ts. Only downmix() and floatToPcm16() are kept.
export const TARGET_SAMPLE_RATE = 16_000;
export const FRAME_MS = 20;
export const FRAME_SAMPLES = (TARGET_SAMPLE_RATE * FRAME_MS) / 1000;
/** Averages channels into one mono channel. */
export function downmix(channels) {
    const first = channels[0];
    if (!first) {
        return new Float32Array(0);
    }
    if (channels.length === 1) {
        return first;
    }
    const mono = new Float32Array(first.length);
    for (const channel of channels) {
        for (let i = 0; i < mono.length; i++) {
            mono[i] += (channel[i] ?? 0) / channels.length;
        }
    }
    return mono;
}
/** Converts [-1, 1] floats to signed 16-bit samples, clamping out-of-range values. */
export function floatToPcm16(samples) {
    const pcm = new Int16Array(samples.length);
    for (let i = 0; i < samples.length; i++) {
        const s = Math.max(-1, Math.min(1, samples[i]));
        pcm[i] = s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7fff);
    }
    return pcm;
}
