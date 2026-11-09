// 快照 + WS 订阅：先拉快照，再以 since=快照 event_seq 订阅；断线 / 事件缺口都重新走一遍。
import { bannerFrom } from './health.js';
import { BoardEvent, BoardState, fromSnapshot, reduce } from './reducer.js';

export interface ClientHooks {
  onState: (s: BoardState, connected: boolean) => void;
  sendAck: (ws: WebSocket, promptId: string) => void;
  onBanner?: (text: string) => void;
}

export class BoardClient {
  state: BoardState | null = null;
  private ws: WebSocket | null = null;
  private retry = 0;
  private stopped = false;
  constructor(private session: string, private token: string, private hooks: ClientHooks) {}

  async start(): Promise<void> {
    if (this.stopped) return;
    try {
      const res = await fetch(`/api/sessions/${encodeURIComponent(this.session)}/snapshot`);
      if (!res.ok) throw new Error(String(res.status));
      const snap = await res.json();
      if (snap.session_id !== this.session) throw new Error('session mismatch');
      this.state = { ...fromSnapshot(snap), mode: snap.mode ?? '' };
      this.hooks.onState(this.state, false);
      void this.loadBanner();
      this.open();
    } catch {
      this.schedule();
    }
  }

  /** 降级横幅：接口 404 / 无字段即忽略。 */
  private async loadBanner(): Promise<void> {
    try {
      const res = await fetch(`/api/setup/${encodeURIComponent(this.session)}`);
      this.hooks.onBanner?.(res.ok ? bannerFrom(await res.json()) : '');
    } catch { this.hooks.onBanner?.(''); }
  }

  private open(): void {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = (this.ws = new WebSocket(`${proto}://${location.host}/ws/board/${encodeURIComponent(this.session)}?token=${encodeURIComponent(this.token)}&since=${this.state!.event_seq}`));
    ws.onopen = () => { this.retry = 0; this.hooks.onState(this.state!, true); };
    ws.onmessage = (m) => {
      let ev: BoardEvent;
      try { ev = JSON.parse(m.data); } catch { return; }
      this.state = reduce(this.state!, ev);
      this.hooks.onState(this.state, true);
      if (this.state.needResync) { ws.onclose = null; ws.close(); void this.start(); }
    };
    ws.onclose = () => { this.hooks.onState(this.state!, false); this.schedule(); };
  }

  private schedule(): void {
    if (this.stopped) return;
    this.retry = Math.min(this.retry + 1, 5);
    setTimeout(() => void this.start(), 500 * this.retry);
  }

  /** VAD 候选被用户确认（Enter）：把 candidate_id 发给后端升级为主答案。 */
  acceptCandidate(candidateId: string): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: 'candidate_accept', candidate_id: candidateId }));
    }
  }

  ack(promptId: string): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.hooks.sendAck(this.ws, promptId);
  }
}
