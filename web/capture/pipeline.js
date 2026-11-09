// Original code. Pure capture pipeline: worklet batches at any rate -> 16 kHz -> level meter ->
// exact 20 ms PCM16 frames with deterministic sample-count timestamps.
import { FRAME_MS, TARGET_SAMPLE_RATE } from './pcm.js';
import { Framer } from './framer.js';
import { LevelMeter } from './level.js';
import { StreamResampler } from './resampler.js';
export class CapturePipeline {
    resampler;
    framer;
    meter;
    frames = 0;
    constructor(options) {
        this.resampler = options.inputRate === TARGET_SAMPLE_RATE ? null : new StreamResampler(options.inputRate);
        this.meter = new LevelMeter(options.onLevel ?? (() => undefined));
        this.framer = new Framer((pcm) => {
            options.onFrame(pcm, options.startUs + this.frames * FRAME_MS * 1000);
            this.frames++;
        });
    }
    push(samples) {
        const at16k = this.resampler ? this.resampler.process(samples) : samples;
        this.meter.push(at16k);
        this.framer.push(at16k);
    }
}
