import test from 'node:test';
import assert from 'node:assert/strict';
import { AudioSender, BUFFER_FRAMES } from '../web/capture/sender.js';

const pcm = new Int16Array(320).fill(5);
const hdr = (b) => ({
  replay: (b[4] & 1) === 1, source: b[3], stream: new DataView(b.buffer, b.byteOffset).getUint32(6),
  gen: new DataView(b.buffer, b.byteOffset).getUint16(10), seq: new DataView(b.buffer, b.byteOffset).getUint32(14),
});

class FakeSocket {
  constructor() { this.readyState = 0; this.sent = []; this.binaryType = ''; }
  send(d) { this.sent.push(d); }
  close() { this.readyState = 3; }
  open() { this.readyState = 1; this.onopen(); }
  drop(code = 1006) { this.readyState = 3; this.onclose({ code }); }
  ack(obj) { this.onmessage({ data: JSON.stringify(obj) }); }
}

function make(extra = {}) {
  const sockets = [], statuses = [], gaps = [], timers = [];
  const sender = new AudioSender({
    url: 'ws://x', streamIds: { mic: 11, meeting: 22 },
    createSocket: () => { const s = new FakeSocket(); sockets.push(s); return s; },
    onStatus: (s) => statuses.push(s), onGap: (g) => gaps.push(g),
    schedule: (fn, ms) => { timers.push({ fn, ms }); return () => {}; }, ...extra,
  });
  return { sender, sockets, statuses, gaps, timers };
}

test('seq increases per source and frames are live on an open socket', () => {
  const { sender, sockets } = make();
  sender.start(); sockets[0].open();
  sender.send('mic', pcm, 0); sender.send('mic', pcm, 20000); sender.send('meeting', pcm, 0);
  assert.deepEqual(sockets[0].sent.map((b) => [hdr(b).source, hdr(b).seq, hdr(b).replay]), [[1, 0, false], [1, 1, false], [2, 0, false]]);
});

test('buffer full drops the oldest frame and reports the gap', () => {
  const { sender, gaps } = make();
  sender.start(); // never opens: everything is buffered
  for (let i = 0; i < BUFFER_FRAMES + 3; i++) sender.send('mic', pcm, i * 20000);
  assert.equal(BUFFER_FRAMES, 1500);
  assert.equal(sender.pending('mic'), 1500);
  assert.equal(sender.droppedFrames, 3);
  assert.deepEqual(gaps.map((g) => g.seq), [0, 1, 2]);
  assert.equal(gaps[0].source, 'mic');
  assert.equal(gaps[0].tClientUs, 0);
});

test('reconnect: generation+1, seq restarts at 0, unacked frames replayed with replay flag', () => {
  const { sender, sockets, statuses, timers } = make();
  sender.start(); sockets[0].open();
  for (let i = 0; i < 5; i++) sender.send('mic', pcm, i);
  sockets[0].drop();
  assert.equal(statuses.at(-1), 'reconnecting');
  assert.equal(timers[0].ms, 500);
  sender.send('mic', pcm, 5); // captured during the outage: still the old generation
  timers[0].fn();
  sockets[1].open();
  const replayed = sockets[1].sent.map(hdr);
  assert.deepEqual(replayed.map((h) => [h.gen, h.seq, h.replay]), [0, 1, 2, 3, 4, 5].map((s) => [0, s, true]));
  sender.send('mic', pcm, 6);
  const live = hdr(sockets[1].sent.at(-1));
  assert.deepEqual([live.gen, live.seq, live.replay], [1, 0, false]);
  assert.equal(sender.currentGeneration, 1);
  assert.equal(statuses.at(-1), 'live');
});

test('ack_seq releases acknowledged frames only', () => {
  const { sender, sockets } = make();
  sender.start(); sockets[0].open();
  for (let i = 0; i < 25; i++) sender.send('mic', pcm, i);
  sockets[0].ack({ ack_seq: 9, source: 'mic', stream_id: 11, generation: 0 });
  assert.equal(sender.pending('mic'), 15);
  sockets[0].ack({ ack_seq: 99, source: 'mic', stream_id: 999, generation: 0 }); // other stream: ignored
  sockets[0].ack({ ack_seq: 99, source: 'meeting', stream_id: 11, generation: 0 });
  assert.equal(sender.pending('mic'), 15);
});

test('after replay is acked, older-generation frames are released with the new generation ack', () => {
  const { sender, sockets, timers } = make();
  sender.start(); sockets[0].open();
  for (let i = 0; i < 5; i++) sender.send('mic', pcm, i);
  sockets[0].drop(); timers[0].fn(); sockets[1].open();
  for (let i = 0; i < 10; i++) sender.send('mic', pcm, 10 + i);
  sockets[1].ack({ ack_seq: 9, source: 'mic', stream_id: 11, generation: 1 });
  assert.equal(sender.pending('mic'), 0);
});

test('fatal close code stops retrying; stop() closes', () => {
  const { sender, sockets, statuses, timers } = make();
  sender.start(); sockets[0].open(); sockets[0].drop(4403);
  assert.equal(statuses.at(-1), 'error');
  assert.equal(timers.length, 0);
  const b = make(); b.sender.start(); b.sender.stop();
  assert.equal(b.statuses.at(-1), 'stopped');
});

test('backoff schedule 0.5s,1s,2s,4s', () => {
  const { sender, sockets, timers } = make();
  sender.start();
  for (let i = 0; i < 4; i++) { sockets[i].drop(); timers[i].fn(); }
  assert.deepEqual(timers.map((t) => t.ms), [500, 1000, 2000, 4000]);
});

test('frames buffered before the first connection are not flagged replay', () => {
  const { sender, sockets } = make();
  sender.start();
  for (let i = 0; i < 3; i++) sender.send('mic', pcm, i);
  sockets[0].open();
  assert.deepEqual(sockets[0].sent.map((b) => [hdr(b).seq, hdr(b).replay]), [[0, false], [1, false], [2, false]]);
});

test('first connect after failed attempts still sends buffered frames as live', () => {
  const { sender, sockets, timers } = make();
  sender.start();
  sender.send('mic', pcm, 0);
  sockets[0].drop(); // never opened
  timers[0].fn();
  sockets[1].open();
  assert.deepEqual(sockets[1].sent.map((b) => [hdr(b).gen, hdr(b).seq, hdr(b).replay]), [[0, 0, false]]);
});

test('dropping never-sent frames reports gap; dropping sent-but-unacked frames does not', () => {
  const { sender, sockets, gaps } = make({ bufferFrames: 3 });
  sender.start(); sockets[0].open();
  for (let i = 0; i < 5; i++) sender.send('mic', pcm, i); // 2 sent frames evicted before any ack
  assert.equal(gaps.length, 0);
  assert.equal(sender.droppedFrames, 2);
});

test('sent-but-unacked drops go to onUnackedDrop, never-sent drops to onGap', () => {
  const unacked = [];
  const { sender, sockets, gaps } = make({ bufferFrames: 2, onUnackedDrop: (g) => unacked.push(g.seq) });
  sender.start(); sockets[0].open();
  for (let i = 0; i < 4; i++) sender.send('mic', pcm, i);
  assert.deepEqual(unacked, [0, 1]);
  assert.equal(gaps.length, 0);
  sockets[0].drop(); // outage: frames 4..7 are never sent and overflow the buffer
  for (let i = 4; i < 8; i++) sender.send('mic', pcm, i);
  assert.deepEqual(gaps.map((g) => g.seq), [4, 5]);
  assert.deepEqual(unacked, [0, 1, 2, 3]);
});
