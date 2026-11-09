// Original code (DOM only, not unit-tested): MediaStream -> AudioWorklet -> CapturePipeline.
import { CapturePipeline } from './pipeline.js';
import { TARGET_SAMPLE_RATE } from './pcm.js';
import type { LevelReading } from './level.js';
import { SourceWatch, type LostReason } from './source-watch.js';

export interface CaptureHandlers {
  onFrame: (pcm: Int16Array, tClientUs: number) => void;
  onLevel: (reading: LevelReading) => void;
  onLost: (reason: LostReason) => void;
}

export interface CaptureHandle {
  /** AudioContext rate; 16000 means the browser did the resampling. */
  contextRate: number;
  hasEnergy(): boolean;
  stop(): Promise<void>;
}

async function openContext(stream: MediaStream): Promise<{ ctx: AudioContext; source: MediaStreamAudioSourceNode }> {
  try {
    const ctx = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE });
    return { ctx, source: ctx.createMediaStreamSource(stream) };
  } catch {
    // Browser refuses a 16 kHz context for this stream: use the device rate and our own filtered resampler.
    const ctx = new AudioContext();
    return { ctx, source: ctx.createMediaStreamSource(stream) };
  }
}

export async function startCapture(input: MediaStream, handlers: CaptureHandlers): Promise<CaptureHandle> {
  const tracks = input.getAudioTracks();
  if (tracks.length === 0) {
    throw new Error('no audio track');
  }
  const stream = new MediaStream(tracks);
  const { ctx, source } = await openContext(stream);
  await ctx.audioWorklet.addModule(new URL('./pcm-worklet.js', import.meta.url).href);
  const node = new AudioWorkletNode(ctx, 'pcm-tap', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
  const mute = ctx.createGain();
  mute.gain.value = 0;
  source.connect(node).connect(mute).connect(ctx.destination);

  const watch = new SourceWatch({ now: () => performance.now(), onLost: handlers.onLost });
  const pipeline = new CapturePipeline({
    inputRate: ctx.sampleRate,
    startUs: performance.now() * 1000,
    onFrame: (pcm, t) => {
      watch.frame();
      handlers.onFrame(pcm, t);
    },
    onLevel: handlers.onLevel,
  });
  node.port.onmessage = (event: MessageEvent<{ type: string; samples: Float32Array }>) => {
    if (event.data.type === 'samples') {
      pipeline.push(event.data.samples);
    }
  };
  tracks[0]!.onended = () => watch.trackEnded();
  const timer = setInterval(() => watch.tick(), 250);

  return {
    contextRate: ctx.sampleRate,
    hasEnergy: () => pipeline.meter.hasEnergy,
    async stop() {
      clearInterval(timer);
      tracks[0]!.onended = null;
      node.port.onmessage = null;
      source.disconnect();
      await ctx.close();
    },
  };
}
