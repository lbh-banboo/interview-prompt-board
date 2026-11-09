// Ported from ukez15/interview-copilot@4a80d842fa22a359b4ab05eea8b2c8432c113e07:src/desktop/src/capture/frame-buffer.ts, MIT © 2026 ukez15.
// Modified: generic element type; push() returns the dropped oldest element instead of a count;
// added dropWhile() so acknowledged frames can be released from the head.
/** Bounded FIFO. When full, the oldest element is dropped. */
export class FrameBuffer {
    capacity;
    items = [];
    constructor(capacity) {
        this.capacity = capacity;
        if (!(capacity > 0)) {
            throw new Error('Capacity must be positive.');
        }
    }
    get size() {
        return this.items.length;
    }
    /** Returns the element dropped to make room, if any. */
    push(item) {
        this.items.push(item);
        return this.items.length > this.capacity ? this.items.shift() : undefined;
    }
    /** Removes elements from the head while the predicate holds. */
    dropWhile(predicate) {
        let n = 0;
        while (n < this.items.length && predicate(this.items[n])) {
            n++;
        }
        if (n > 0) {
            this.items.splice(0, n);
        }
    }
    snapshot() {
        return this.items;
    }
}
