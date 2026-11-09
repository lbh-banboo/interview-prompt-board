// Ported from ukez15/interview-copilot@4a80d842fa22a359b4ab05eea8b2c8432c113e07:src/desktop/src/capture/audio-sender.ts, MIT © 2026 ukez15.
// Modified: rewritten for the §7.2 24-byte header and several sources on one socket; unacked frames
// are kept (not just unsent ones) in a 30 s / 1500-frame buffer per source; server ack_seq releases
// them; after a reconnect capture_generation+1 and seq restarts at 0 while the buffered frames are
// replayed with their original (generation, seq) and the replay flag (frames buffered before the very first
// connection are sent as live); overflow drops the oldest
// frame and reports it through onGap; close codes 4403/4404 are fatal.
import { backoffDelay } from './backoff.js';
import { FrameBuffer } from './frame-buffer.js';
import { packFrame, withReplayFlag } from './frame.js';
const OPEN = 1;
/** 1003/1008 policy errors, 4403 bad token/origin, 4404 no active session: do not retry. */
const FATAL_CLOSE_CODES = new Set([1003, 1008, 4403, 4404]);
/** 30 s of 20 ms frames, per source. */
export const BUFFER_FRAMES = 1500;
export class AudioSender {
    options;
    socket = null;
    streams = new Map();
    attempt = 0;
    cancelTimer = null;
    stopped = false;
    connectedOnce = false;
    generation = 0;
    dropped = 0;
    constructor(options) {
        this.options = options;
        for (const source of Object.keys(options.streamIds)) {
            this.streams.set(source, {
                streamId: options.streamIds[source],
                seq: 0,
                buffer: new FrameBuffer(options.bufferFrames ?? BUFFER_FRAMES),
            });
        }
    }
    get droppedFrames() {
        return this.dropped;
    }
    get currentGeneration() {
        return this.generation;
    }
    /** Frames kept for a possible replay (sent but not yet acknowledged, or not yet sent). */
    pending(source) {
        return this.streams.get(source)?.buffer.size ?? 0;
    }
    start() {
        this.stopped = false;
        this.options.onStatus('connecting');
        this.connect();
    }
    send(source, pcm, tClientUs) {
        const stream = this.streams.get(source);
        if (this.stopped || !stream) {
            return;
        }
        const entry = {
            generation: this.generation,
            seq: stream.seq++,
            tClientUs,
            bytes: new Uint8Array(0),
            sent: false,
        };
        entry.bytes = packFrame({
            source, streamId: stream.streamId, generation: entry.generation, seq: entry.seq, tClientUs, pcm,
        });
        const lost = stream.buffer.push(entry);
        if (lost) {
            this.dropped++;
            const info = {
                source, streamId: stream.streamId, generation: lost.generation, seq: lost.seq, tClientUs: lost.tClientUs,
            };
            (lost.sent ? this.options.onUnackedDrop : this.options.onGap)?.(info);
        }
        if (this.socket?.readyState === OPEN) {
            this.socket.send(entry.bytes);
            entry.sent = true;
        }
    }
    stop() {
        this.stopped = true;
        this.cancelTimer?.();
        this.cancelTimer = null;
        this.socket?.close(1000);
        this.socket = null;
        this.options.onStatus('stopped');
    }
    connect() {
        const socket = this.options.createSocket(this.options.url);
        socket.binaryType = 'arraybuffer';
        socket.onopen = () => {
            this.attempt = 0;
            const reconnect = this.connectedOnce;
            if (reconnect) {
                this.generation = (this.generation + 1) & 0xffff;
                for (const stream of this.streams.values()) {
                    stream.seq = 0;
                }
            }
            this.connectedOnce = true;
            for (const stream of this.streams.values()) {
                for (const entry of stream.buffer.snapshot()) {
                    socket.send(reconnect ? withReplayFlag(entry.bytes) : entry.bytes);
                    entry.sent = true;
                }
            }
            this.options.onStatus('live');
        };
        socket.onmessage = (event) => {
            if (this.socket === socket) {
                this.handleMessage(event.data);
            }
        };
        socket.onclose = (event) => {
            if (this.stopped || this.socket !== socket) {
                return;
            }
            if (FATAL_CLOSE_CODES.has(event.code)) {
                this.stopped = true;
                this.options.onStatus('error');
                return;
            }
            this.options.onStatus('reconnecting');
            this.cancelTimer = (this.options.schedule ?? defaultSchedule)(() => this.connect(), backoffDelay(this.attempt++));
        };
        this.socket = socket;
    }
    handleMessage(data) {
        if (typeof data !== 'string') {
            return;
        }
        let ack;
        try {
            ack = JSON.parse(data);
        }
        catch {
            return;
        }
        if (typeof ack.ack_seq !== 'number' || typeof ack.generation !== 'number') {
            return;
        }
        const stream = this.streams.get(ack.source);
        if (!stream || stream.streamId !== ack.stream_id) {
            return;
        }
        // Everything older than the acked frame was sent earlier on this ordered socket, so it is stored too.
        stream.buffer.dropWhile((e) => e.generation < ack.generation
            || (e.generation === ack.generation && e.seq <= ack.ack_seq));
    }
}
function defaultSchedule(fn, ms) {
    const id = setTimeout(fn, ms);
    return () => clearTimeout(id);
}
