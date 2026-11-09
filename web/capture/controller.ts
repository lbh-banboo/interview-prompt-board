// 采集控制（无页面耦合）：capture.html 与会话页共用同一套采集流程与文案。
import { startCapture, type CaptureHandle } from './capture.js';
import { micConstraints, micProblemText } from './constraints.js';
import type { SourceName } from './frame.js';
import type { LevelReading } from './level.js';
import { AudioSender, type SenderStatus } from './sender.js';
import { NO_SOUND_TEXT, SourceStatus } from './source-status.js';
import {
  DISPLAY_CONSTRAINTS, MEETING_NO_SOUND_TEXT, NO_AUDIO_HINT, NO_MEETING_DEVICE_HINT,
  displaySurfaceWarning, meetingAudioConstraints, meetingInputs,
} from './meeting.js';
import { CAPTURE_HINT } from './session.js';

export type AudioSource = 'mic' | 'meeting' | 'room';
export type CaptureMode = 'online' | 'offline';
export type SourceCondition = 'idle' | 'checking' | 'ok' | 'silent' | 'lost';
export const SOURCE_LABEL: Record<AudioSource, string> = { mic: '麦克风', meeting: '会议音频', room: '房间麦克风' };
export const SOURCE_STATUS_TEXT: Record<SenderStatus, string> = {
  connecting: '连接中', live: '已连接', reconnecting: '重连中', error: '连接被拒绝', stopped: '已停止',
};
export const MIC_SILENT_HINT = '未检测到声音：请确认麦克风没有静音、系统输入设备正确，并对着麦克风说一句话。';

export interface CaptureView {
  device: HTMLSelectElement;
  consent: HTMLInputElement;
  start: HTMLButtonElement;
  meeting?: HTMLButtonElement;
  meetingMode?: HTMLSelectElement;
  meetingDevice?: HTMLSelectElement;
  stop?: HTMLButtonElement;
  ws: HTMLElement;
  stats: HTMLElement;
  level: (source: AudioSource) => HTMLProgressElement;
  sourceText: (source: AudioSource) => HTMLElement;
  say: (text: string) => void;
  /** 麦克风电平（引导条用）：授权后实时回调。 */
  onMicLevel?: (reading: LevelReading) => void;
  /** 麦克风问题：denied=授权失败/无设备，silent=3 秒内无声音。 */
  onMicProblem?: (kind: 'denied' | 'silent', text: string) => void;
}

export interface CaptureSnapshot {
  capturing: boolean;
  ws: SenderStatus | 'idle';
  frames: Record<AudioSource, number>;
  gaps: number;
  generation: number;
  sources: Record<AudioSource, { condition: SourceCondition }>;
}

export interface CaptureControllerOptions {
  session: string;
  token: string;
  view: CaptureView;
  /** 线下 offline 只发一路 room 帧；默认 online（capture.html 行为不变）。 */
  mode?: CaptureMode;
}

export class CaptureController {
  private sender: AudioSender | null = null;
  private socket: WebSocket | null = null;
  private readonly handles = new Map<AudioSource, CaptureHandle>();
  private readonly statuses = new Map<AudioSource, SourceStatus>();
  private readonly lost = new Set<AudioSource>();
  private readonly warned = new Set<AudioSource>();
  private readonly frames: Record<AudioSource, number> = { mic: 0, meeting: 0, room: 0 };
  private readonly conditions: Record<AudioSource, SourceCondition> = { mic: 'idle', meeting: 'idle', room: 'idle' };
  private readonly mode: CaptureMode;
  private readonly micSource: AudioSource;
  private micStream: MediaStream | null = null;
  private gaps = 0;
  private capturing = false;
  private wsStatus: SenderStatus | 'idle' = 'idle';
  private audioInputs: MediaDeviceInfo[] = [];

  constructor(private readonly options: CaptureControllerOptions) {
    this.mode = options.mode ?? 'online';
    this.micSource = this.mode === 'offline' ? 'room' : 'mic';
  }

  snapshot(): CaptureSnapshot {
    return {
      capturing: this.capturing, ws: this.wsStatus, frames: { ...this.frames }, gaps: this.gaps,
      generation: this.sender?.currentGeneration ?? 0,
      sources: {
        mic: { condition: this.conditions.mic }, meeting: { condition: this.conditions.meeting },
        room: { condition: this.conditions.room },
      },
    };
  }

  async initDevices(): Promise<void> {
    const { view } = this.options;
    this.audioInputs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'audioinput');
    view.device.append(...this.audioInputs.filter((d) => d.deviceId).map((d) => new Option(d.label || d.deviceId, d.deviceId)));
    if (this.mode === 'offline' || !view.meetingMode) return;
    this.renderMeetingDevices();
    // 麦克风改选后，会议设备列表要排除新选中的设备。
    view.device.addEventListener('change', () => { void this.refreshMeetingDevices(); });
    const syncMode = () => {
      view.meetingDevice?.closest('label')?.toggleAttribute('hidden', view.meetingMode!.value !== 'device');
    };
    view.meetingMode.addEventListener('change', syncMode);
    syncMode();
  }

  /** 重新枚举输入设备（例如刚插上虚拟声卡），并保持已选会议设备。 */
  async refreshMeetingDevices(): Promise<void> {
    this.audioInputs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'audioinput');
    this.renderMeetingDevices();
  }

  private renderMeetingDevices(): void {
    const { view } = this.options;
    const select = view.meetingDevice;
    if (!select) return;
    const keep = select.value;
    const inputs = meetingInputs(this.audioInputs, view.device.value);
    select.replaceChildren(new Option('（选择输入设备）', ''));
    for (const d of inputs) select.append(new Option(d.label, d.deviceId));
    if (inputs.some((d) => d.deviceId === keep)) select.value = keep;
  }

  private say(text: string): void {
    this.options.view.say(text);
  }

  /** 只授权麦克风并开始本地电平检测（不发帧）；线上授权 mic，线下授权 room。 */
  async authorizeMic(): Promise<boolean> {
    const { view } = this.options;
    if (!view.consent.checked) {
      view.onMicProblem?.('denied', '请先勾选知情同意');
      this.say('请先确认录音知情许可');
      return false;
    }
    if (this.handles.has(this.micSource)) return true;
    try {
      this.micStream = await navigator.mediaDevices.getUserMedia(micConstraints(this.mode, view.device.value));
      await this.attach(this.micSource, this.micStream);
      return true;
    } catch (e) {
      const text = micProblemText(e);
      view.onMicProblem?.('denied', text);
      this.say(text);
      return false;
    }
  }

  async start(): Promise<boolean> {
    const { session, token, view } = this.options;
    if (!session) {
      this.say(CAPTURE_HINT);
      return false;
    }
    if (!view.consent.checked) {
      this.say('请先确认录音知情许可');
      return false;
    }
    if (this.capturing) return true;
    try {
      if (!this.handles.has(this.micSource)) {
        const ok = await this.authorizeMic();
        if (!ok) return false;
      }
      const ids = new Uint32Array(2);
      crypto.getRandomValues(ids);
      const streamIds: Partial<Record<SourceName, number>> = this.mode === 'offline'
        ? { room: ids[0]! }
        : { mic: ids[0]!, meeting: ids[1]! };
      this.sender = new AudioSender({
        url: `ws://${location.host}/ws/audio/${session}?token=${encodeURIComponent(token)}`,
        createSocket: (url) => (this.socket = new WebSocket(url)) as any,
        streamIds,
        onStatus: (status) => {
          this.wsStatus = status;
          view.ws.textContent = `WebSocket：${SOURCE_STATUS_TEXT[status]}`;
          this.drawStats();
        },
        onGap: () => {
          this.gaps++;
          this.say('录音缺口：本地连接中断超过 30 秒，部分音频未能补发');
          this.drawStats();
        },
      });
      this.sender.start();
      this.capturing = true;
      view.start.disabled = true;
      if (view.meeting && this.mode === 'online' && !this.handles.has('meeting')) view.meeting.disabled = false;
      if (view.stop) view.stop.disabled = false;
      this.showCapturingText();
      if (this.mode === 'offline') this.say('采集中（房间麦克风，同时收录双方）');
      else if (!this.handles.has('meeting')) this.say('采集中');
      return true;
    } catch (e) {
      this.say(String(e));
      return false;
    }
  }

  private showCapturingText(): void {
    for (const source of this.handles.keys()) {
      if (this.lost.has(source) || this.warned.has(source)) continue;
      const state = this.options.view.sourceText(source);
      const handle = this.handles.get(source);
      state.textContent = `采集中（AudioContext ${handle?.contextRate ?? '-'} Hz${handle?.contextRate === 16000 ? '，浏览器重采样' : '，滤波重采样'}）`;
      state.dataset.state = 'checking';
    }
  }

  async addMeeting(): Promise<boolean> {
    if (this.mode === 'offline') {
      this.say('线下同室只用一路麦克风，不需要添加会议音频');
      return false;
    }
    const { view } = this.options;
    return view.meetingMode?.value === 'device' ? this.addMeetingDevice() : this.addMeetingSurface();
  }

  /** 网页会议：共享 Chrome 标签页并勾选共享标签页音频。 */
  private async addMeetingSurface(): Promise<boolean> {
    const { view } = this.options;
    try {
      const display = await navigator.mediaDevices.getDisplayMedia(DISPLAY_CONSTRAINTS as DisplayMediaStreamOptions);
      // displaySurface 只在视频轨上；先读再停用视频轨。
      const warning = displaySurfaceWarning(display.getVideoTracks()[0]);
      display.getVideoTracks().forEach((t) => { t.stop(); display.removeTrack(t); });
      if (display.getAudioTracks().length === 0) {
        this.say(NO_AUDIO_HINT);
        return false;
      }
      await this.attach('meeting', display);
      if (view.meeting) view.meeting.disabled = true;
      if (warning) {
        this.warned.add('meeting');
        const state = view.sourceText('meeting');
        state.textContent = '所选共享方式在 macOS 上通常没有声音';
        state.dataset.state = 'warn';
        this.say(warning);
      } else {
        this.say(this.capturing ? '采集中（会议音频已加入）' : '会议音频已加入，开始采集后发送');
      }
      return true;
    } catch (e) {
      this.say(String(e));
      return false;
    }
  }

  /** 桌面客户端：把虚拟声卡/第二输入设备当作会议音频输入。 */
  private async addMeetingDevice(): Promise<boolean> {
    const { view } = this.options;
    const deviceId = view.meetingDevice?.value ?? '';
    if (!deviceId) {
      this.say(NO_MEETING_DEVICE_HINT);
      return false;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia(meetingAudioConstraints(deviceId));
      await this.attach('meeting', stream);
      if (view.meeting) view.meeting.disabled = true;
      const label = view.meetingDevice?.selectedOptions[0]?.textContent ?? '输入设备';
      this.say(this.capturing ? `采集中（会议音频已加入：${label}）` : `会议音频已加入：${label}，开始采集后发送`);
      return true;
    } catch (e) {
      this.say(`无法打开会议音频设备：${String(e)}`);
      return false;
    }
  }

  /** 停轨道 -> 等 WS 关闭；调用方随后 POST /stop，保证 finalize 不跟迟到帧竞争。 */
  async stop(): Promise<void> {
    const { view } = this.options;
    for (const handle of this.handles.values()) {
      await handle.stop();
    }
    this.handles.clear();
    this.micStream?.getTracks().forEach((t) => t.stop());
    this.micStream = null;
    const closing = this.socket;
    this.sender?.stop();
    if (closing && closing.readyState !== WebSocket.CLOSED) {
      await new Promise<void>((resolve) => {
        closing.addEventListener('close', () => resolve());
        setTimeout(resolve, 3000);
      });
    }
    this.socket = null;
    this.capturing = false;
    view.start.disabled = true;
    if (view.meeting) view.meeting.disabled = true;
    if (view.stop) view.stop.disabled = true;
  }

  private async attach(source: AudioSource, stream: MediaStream): Promise<void> {
    const { view } = this.options;
    const noSound = source === 'meeting' ? MEETING_NO_SOUND_TEXT : NO_SOUND_TEXT;
    const status = new SourceStatus(noSound);
    this.statuses.set(source, status);
    this.lost.delete(source);
    this.warned.delete(source);
    this.conditions[source] = 'checking';
    const state = view.sourceText(source);
    const level = view.level(source);
    state.dataset.state = 'checking';
    const preview = !this.capturing && source === this.micSource;
    if (preview) state.textContent = '已授权，请说一句话';
    const handle = await startCapture(stream, {
      onFrame: (pcm, t) => {
        if (!this.capturing) return;
        this.sender?.send(source, pcm, t);
        this.frames[source]++;
      },
      onLevel: (reading: LevelReading) => {
        level.value = Math.min(1, reading.rms * 4);
        if (source === this.micSource) view.onMicLevel?.(reading);
        if (this.lost.has(source) || this.warned.has(source)) return;
        const text = status.onLevel(reading);
        if (text !== null) {
          state.textContent = preview ? (text === '正常' ? '麦克风正常' : text) : text;
          this.conditions[source] = text === '正常' ? 'ok' : 'silent';
          state.dataset.state = text === '正常' ? 'ok' : 'silent';
        }
      },
      onLost: (reason) => {
        this.lost.add(source);
        this.conditions[source] = 'lost';
        state.dataset.state = 'lost';
        state.textContent = reason === 'track_ended' ? '音源已断开（设备被拔出或共享已停止）' : '音源已断开（超过 2 秒无音频）';
        this.say(`${SOURCE_LABEL[source]}丢失：请重新选择音源`);
        if (source === this.micSource) view.onMicProblem?.('denied', '麦克风已断开：请重新连接设备后点“重新授权”。');
      },
    });
    this.handles.set(source, handle);
    if (!preview) {
      const detail = `AudioContext ${handle.contextRate} Hz${handle.contextRate === 16000 ? '，浏览器重采样' : '，滤波重采样'}`;
      state.textContent = this.capturing ? `采集中（${detail}）` : `已加入，开始采集后发送（${detail}）`;
    }
    setTimeout(() => {
      if (this.lost.has(source) || this.warned.has(source)) return;
      const text = status.validate(handle.hasEnergy());
      state.textContent = preview ? (text === '正常' ? '麦克风正常' : text) : text;
      this.conditions[source] = text === '正常' ? 'ok' : 'silent';
      state.dataset.state = text === '正常' ? 'ok' : 'silent';
      if (source === 'meeting' && text === MEETING_NO_SOUND_TEXT) {
        this.say('会议音频无声音：网页会议请选标签页并勾选共享音频；桌面客户端请用虚拟声卡（见“会议音频怎么选”）');
      } else if (source === this.micSource && text === NO_SOUND_TEXT) {
        this.say(MIC_SILENT_HINT);
        if (preview) view.onMicProblem?.('silent', MIC_SILENT_HINT);
      }
    }, 3000);
  }

  drawStats(): void {
    const sent = this.mode === 'offline'
      ? `room ${this.frames.room}`
      : `mic ${this.frames.mic} / meeting ${this.frames.meeting}`;
    this.options.view.stats.textContent =
      `已发送帧：${sent}；缺口帧：${this.gaps}；重连代数：${this.sender?.currentGeneration ?? 0}`;
  }
}
