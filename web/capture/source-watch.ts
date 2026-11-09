// Original code. Disconnect detection (plan M02 item 7): track.onended, or no frame for 2 s.
export type LostReason = 'track_ended' | 'no_frames';

export interface SourceWatchOptions {
  now: () => number;
  onLost: (reason: LostReason) => void;
  onRecovered?: () => void;
  timeoutMs?: number;
}

export class SourceWatch {
  private lastFrameAt: number;
  private lost = false;
  private ended = false;
  private readonly timeoutMs: number;

  constructor(private readonly options: SourceWatchOptions) {
    this.timeoutMs = options.timeoutMs ?? 2000;
    this.lastFrameAt = options.now();
  }

  get isLost(): boolean {
    return this.lost;
  }

  frame(): void {
    this.lastFrameAt = this.options.now();
    if (this.lost && !this.ended) {
      this.lost = false;
      this.options.onRecovered?.();
    }
  }

  /** Call periodically (e.g. every 250 ms). */
  tick(): void {
    if (!this.lost && this.options.now() - this.lastFrameAt > this.timeoutMs) {
      this.lost = true;
      this.options.onLost('no_frames');
    }
  }

  trackEnded(): void {
    this.ended = true;
    if (!this.lost) {
      this.lost = true;
      this.options.onLost('track_ended');
    }
  }
}
