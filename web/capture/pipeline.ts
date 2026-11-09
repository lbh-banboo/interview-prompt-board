// Original code. Pure capture pipeline: worklet batches at any rate -> 16 kHz -> level meter ->
// exact 20 ms PCM16 frames with deterministic sample-count timestamps.
import { FRAME_MS, TARGET_SAMPLE_RATE } from './pcm.js';
import { Framer } from './framer.js';
import { LevelMeter, type LevelReading } from './level.js';
import { StreamResampler } from './resampler.js';

export interface PipelineOptions {
  /** Actual AudioContext rate; equal to 16000 when the browser did the resampling. */
  inputRate: number;
  /** Monotonic client time (us) of the first sample. */
  startUs: number;
  onFrame: (pcm: Int16Array, tClientUs: number) => void;
  onLevel?: (reading: LevelReading) => void;
}

export class CapturePipeline {
  private readonly resampler: StreamResampler | null;
  private readonly framer: Framer;
  readonly meter: LevelMeter;
  private frames = 0;

  constructor(options: PipelineOptions) {
    this.resampler = options.inputRate === TARGET_SAMPLE_RATE ? null : new StreamResampler(options.inputRate);
    this.meter = new LevelMeter(options.onLevel ?? (() => undefined));
    this.framer = new Framer((pcm) => {
      options.onFrame(pcm, options.startUs + this.frames * FRAME_MS * 1000);
      this.frames++;
    });
  }

  push(samples: Float32Array): void {
    const at16k = this.resampler ? this.resampler.process(samples) : samples;
    this.meter.push(at16k);
    this.framer.push(at16k);
  }
}
