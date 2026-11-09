// Original code (not ported). Replaces ukez15's linear-interpolation Resampler, which has no
// anti-aliasing filter. This is a streaming Kaiser-windowed-sinc low-pass resampler: the kernel
// cutoff is 7.4 kHz, so content above the 8 kHz output Nyquist is attenuated before decimation.
// State (history window + output counter) is kept across process() calls, so any chunking of the
// input yields bit-identical output and the sample count does not drift.
import { TARGET_SAMPLE_RATE } from './pcm.js';
const CUTOFF_HZ = 7400;
const HALF_WIDTH = 96; // kernel half width in input samples (192 taps)
const KAISER_BETA = 9;
const TABLE_RES = 256; // kernel table entries per input sample (linearly interpolated)
function bessel0(x) {
    let sum = 1;
    let term = 1;
    for (let k = 1; k < 40; k++) {
        term *= (x / (2 * k)) ** 2;
        sum += term;
    }
    return sum;
}
function buildKernel(inputRate) {
    const cutoff = Math.min(CUTOFF_HZ, 0.46 * inputRate);
    const g = (2 * cutoff) / inputRate;
    const norm = bessel0(KAISER_BETA);
    const table = new Float64Array(2 * HALF_WIDTH * TABLE_RES + 2);
    for (let j = 0; j < table.length; j++) {
        const x = -HALF_WIDTH + j / TABLE_RES;
        const u = x / HALF_WIDTH;
        if (Math.abs(u) >= 1) {
            continue;
        }
        const sinc = x === 0 ? 1 : Math.sin(Math.PI * g * x) / (Math.PI * g * x);
        table[j] = g * sinc * (bessel0(KAISER_BETA * Math.sqrt(1 - u * u)) / norm);
    }
    return table;
}
export class StreamResampler {
    step;
    kernel;
    /** Input samples not yet fully consumed; history[0] has absolute index `base`. */
    history = new Float32Array(HALF_WIDTH);
    base = -HALF_WIDTH;
    produced = 0;
    constructor(inputRate, outputRate = TARGET_SAMPLE_RATE) {
        if (!(inputRate > 0) || !(outputRate > 0)) {
            throw new Error('Sample rates must be positive.');
        }
        this.step = inputRate / outputRate;
        this.kernel = buildKernel(inputRate);
    }
    process(input) {
        const x = new Float32Array(this.history.length + input.length);
        x.set(this.history);
        x.set(input, this.history.length);
        const total = this.base + x.length;
        const out = new Float32Array(Math.ceil(x.length / this.step) + 2);
        let count = 0;
        for (;;) {
            const c = this.produced * this.step;
            const floor = Math.floor(c);
            if (floor + HALF_WIDTH >= total) {
                break;
            }
            const fr = (c - floor) * TABLE_RES;
            const j0 = Math.floor(fr);
            const w = fr - j0;
            let acc = 0;
            for (let k = floor - HALF_WIDTH + 1; k <= floor + HALF_WIDTH; k++) {
                const idx = j0 + (floor - k + HALF_WIDTH) * TABLE_RES;
                const a = this.kernel[idx];
                acc += x[k - this.base] * (a + (this.kernel[idx + 1] - a) * w);
            }
            out[count++] = acc;
            this.produced++;
        }
        const keepFrom = Math.max(this.base, Math.floor(this.produced * this.step) - HALF_WIDTH + 1);
        this.history = x.slice(keepFrom - this.base);
        this.base = keepFrom;
        return out.slice(0, count);
    }
}
/** One-shot helper: resamples a whole signal (the last HALF_WIDTH input samples of latency are not flushed). */
export function resample(input, inputRate, outputRate = TARGET_SAMPLE_RATE) {
    return new StreamResampler(inputRate, outputRate).process(input);
}
