import test from 'node:test';
import assert from 'node:assert/strict';
import { StreamResampler, resample } from '../web/capture/resampler.js';

const sine = (hz, rate, n, amp = 0.5) => Float32Array.from({ length: n }, (_, i) => amp * Math.sin((2 * Math.PI * hz * i) / rate));
const rms = (a, skip = 0) => { let s = 0; for (let i = skip; i < a.length; i++) s += a[i] * a[i]; return Math.sqrt(s / (a.length - skip)); };

function seededChunks(n, seed) {
  let x = seed;
  const sizes = [];
  let total = 0;
  while (total < n) {
    x = (x * 1664525 + 1013904223) >>> 0;
    const s = 1 + (x >>> 8) % 2000; // 1..2000, includes tiny and 128/512-style sizes
    sizes.push(Math.min(s, n - total));
    total += sizes[sizes.length - 1];
  }
  return sizes;
}

for (const rate of [48000, 44100]) {
  test(`${rate} -> 16k: whole signal and random chunks give identical output`, () => {
    const n = rate * 3;
    const noisy = Float32Array.from({ length: n }, (_, i) => 0.3 * Math.sin(i * 0.01) + 0.2 * Math.sin(i * 0.37) + ((i * 2654435761) % 1000) / 10000);
    const whole = resample(noisy, rate);
    const r = new StreamResampler(rate);
    const parts = [];
    let pos = 0;
    for (const size of seededChunks(n, rate)) {
      parts.push(r.process(noisy.subarray(pos, pos + size)));
      pos += size;
    }
    const joined = new Float32Array(parts.reduce((a, p) => a + p.length, 0));
    let o = 0;
    for (const p of parts) { joined.set(p, o); o += p.length; }
    assert.equal(joined.length, whole.length);
    assert.deepEqual(joined, whole);
  });

  test(`${rate} -> 16k: passband tone keeps its level and frequency`, () => {
    const out = resample(sine(1000, rate, rate * 2), rate);
    assert.ok(Math.abs(rms(out, 200) - 0.5 / Math.SQRT2) < 0.005, `rms ${rms(out, 200)}`);
  });

  test(`${rate} -> 16k: >=30 dB attenuation above 8 kHz (10 kHz sine)`, () => {
    const ref = rms(resample(sine(1000, rate, rate * 2), rate), 200);
    const report = {};
    for (const hz of [8500, 9000, 10000, 12000, 15000, 20000]) {
      const att = 20 * Math.log10(ref / rms(resample(sine(hz, rate, rate * 2), rate), 200));
      report[hz] = att.toFixed(1);
      assert.ok(att >= 30, `${hz} Hz only ${att.toFixed(1)} dB`);
    }
    console.log(`# attenuation dB @${rate}:`, JSON.stringify(report));
  });

  test(`${rate} -> 16k: no sample-count drift over 5 minutes of chunked input`, () => {
    const r = new StreamResampler(rate);
    const chunk = new Float32Array(512);
    const chunks = Math.floor((rate * 300) / 512);
    let out = 0;
    for (let i = 0; i < chunks; i++) out += r.process(chunk).length;
    const ideal = (chunks * 512 * 16000) / rate;
    assert.ok(Math.abs(out - ideal) <= 100, `out ${out} ideal ${ideal}`); // constant filter latency only
  });
}
