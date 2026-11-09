// Original code. Per-source status text for the capture page: the 3 s validation result must not be
// overwritten by the 50 ms level readings until the source shows sustained sound.
import { SILENCE_RMS, SILENT_WARN_MS, LEVEL_WINDOW_MS } from './level.js';
export const NO_SOUND_TEXT = '未检测到声音';
/** Continuous sound needed to clear a failed validation. */
export const RECOVER_MS = 500;
export class SourceStatus {
    noSoundText;
    verified = false;
    failed = false;
    loudMs = 0;
    constructor(noSoundText = NO_SOUND_TEXT) {
        this.noSoundText = noSoundText;
    }
    /** Records the 3 s validation result and returns the text to show. */
    validate(hasEnergy) {
        this.verified = true;
        this.failed = !hasEnergy;
        this.loudMs = 0;
        return this.failed ? this.noSoundText : '正常';
    }
    /** Returns the text for a level reading, or null while the source is not validated yet. */
    onLevel(reading) {
        if (!this.verified) {
            return null;
        }
        if (this.failed) {
            this.loudMs = reading.rms >= SILENCE_RMS ? this.loudMs + LEVEL_WINDOW_MS : 0;
            if (this.loudMs < RECOVER_MS) {
                return this.noSoundText;
            }
            this.failed = false;
        }
        return reading.silentMs >= SILENT_WARN_MS ? `静音超过 ${Math.floor(reading.silentMs / 1000)} 秒` : '正常';
    }
}
