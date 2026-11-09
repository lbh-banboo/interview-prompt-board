export class SourceWatch {
    options;
    lastFrameAt;
    lost = false;
    ended = false;
    timeoutMs;
    constructor(options) {
        this.options = options;
        this.timeoutMs = options.timeoutMs ?? 2000;
        this.lastFrameAt = options.now();
    }
    get isLost() {
        return this.lost;
    }
    frame() {
        this.lastFrameAt = this.options.now();
        if (this.lost && !this.ended) {
            this.lost = false;
            this.options.onRecovered?.();
        }
    }
    /** Call periodically (e.g. every 250 ms). */
    tick() {
        if (!this.lost && this.options.now() - this.lastFrameAt > this.timeoutMs) {
            this.lost = true;
            this.options.onLost('no_frames');
        }
    }
    trackEnded() {
        this.ended = true;
        if (!this.lost) {
            this.lost = true;
            this.options.onLost('track_ended');
        }
    }
}
