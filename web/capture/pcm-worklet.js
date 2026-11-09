// Ported from ukez15/interview-copilot@4a80d842fa22a359b4ab05eea8b2c8432c113e07:src/desktop/src/capture/pcm-worklet.ts, MIT © 2026 ukez15.
// Modified: BATCH_SAMPLES 2048 -> 512 (plan §3.3); dropped the 'rate' message (the page reads
// AudioContext.sampleRate); marked as a module so declarations stay file-local.
// AudioWorklet processor: downmixes each 128-sample render block and posts 512-sample batches.
// Runs on the audio thread; keep it allocation-light.
export const BATCH_SAMPLES = 512;
class PcmTapProcessor extends AudioWorkletProcessor {
    batch = new Float32Array(BATCH_SAMPLES);
    filled = 0;
    process(inputs) {
        const channels = inputs[0];
        const first = channels?.[0];
        if (!channels || !first) {
            return true;
        }
        for (let i = 0; i < first.length; i++) {
            let sum = 0;
            for (const channel of channels) {
                sum += channel[i] ?? 0;
            }
            this.batch[this.filled++] = sum / channels.length;
            if (this.filled === BATCH_SAMPLES) {
                this.port.postMessage({ type: 'samples', samples: this.batch }, [this.batch.buffer]);
                this.batch = new Float32Array(BATCH_SAMPLES);
                this.filled = 0;
            }
        }
        return true;
    }
}
registerProcessor('pcm-tap', PcmTapProcessor);
