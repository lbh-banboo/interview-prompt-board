// Original code. Level meter over 16 kHz samples: one RMS reading per 50 ms (800 samples),
// plus how long the input has been continuously silent (plan M02 item 4: warn after 10 s).
import { TARGET_SAMPLE_RATE } from './pcm.js';

export const LEVEL_WINDOW_MS = 50;
export const LEVEL_WINDOW_SAMPLES = (TARGET_SAMPLE_RATE * LEVEL_WINDOW_MS) / 1000;
/** RMS below this (about -66 dBFS) counts as silence. */
export const SILENCE_RMS = 0.0005;
export const SILENT_WARN_MS = 10_000;

export interface LevelReading {
  rms: number;
  silentMs: number;
}

export class LevelMeter {
  private sumSquares = 0;
  private count = 0;
  private silentMs = 0;
  private everNonZero = false;

  constructor(private readonly onLevel: (reading: LevelReading) => void) {}

  /** True once any non-zero sample has been seen (used for the 3 s source validation). */
  get hasEnergy(): boolean {
    return this.everNonZero;
  }

  push(samples: Float32Array): void {
    for (let i = 0; i < samples.length; i++) {
      const s = samples[i]!;
      if (s !== 0) {
        this.everNonZero = true;
      }
      this.sumSquares += s * s;
      if (++this.count === LEVEL_WINDOW_SAMPLES) {
        const rms = Math.sqrt(this.sumSquares / this.count);
        this.silentMs = rms < SILENCE_RMS ? this.silentMs + LEVEL_WINDOW_MS : 0;
        this.sumSquares = 0;
        this.count = 0;
        this.onLevel({ rms, silentMs: this.silentMs });
      }
    }
  }
}
