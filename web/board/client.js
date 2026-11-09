// 快照 + WS 订阅：先拉快照，再以 since=快照 event_seq 订阅；断线 / 事件缺口都重新走一遍。
import { bannerFrom } from './health.js';
import { fromSnapshot, reduce } from './reducer.js';
export class BoardClient {
    session;
    token;
    hooks;
    state = null;
    ws = null;
    retry = 0;
    stopped = false;
    constructor(session, token, hooks) {
        this.session = session;
        this.token = token;
        this.hooks = hooks;
    }
    async start() {
        if (this.stopped)
            return;
        try {
            const res = await fetch(`/api/sessions/${encodeURIComponent(this.session)}/snapshot`);
            if (!res.ok)
                throw new Error(String(res.status));
            const snap = await res.json();
            if (snap.session_id !== this.session)
                throw new Error('session mismatch');
            this.state = { ...fromSnapshot(snap), mode: snap.mode ?? '' };
            this.hooks.onState(this.state, false);
            void this.loadBanner();
            this.open();
        }
        catch {
            this.schedule();
        }
    }
    /** 降级横幅：接口 404 / 无字段即忽略。 */
    async loadBanner() {
        try {
            const res = await fetch(`/api/setup/${encodeURIComponent(this.session)}`);
            this.hooks.onBanner?.(res.ok ? bannerFrom(await res.json()) : '');
        }
        catch {
            this.hooks.onBanner?.('');
        }
    }
    open() {
        const proto = location.protocol === 'https:' ? 'wss' : 'ws';
        const ws = (this.ws = new WebSocket(`${proto}://${location.host}/ws/board/${encodeURIComponent(this.session)}?token=${encodeURIComponent(this.token)}&since=${this.state.event_seq}`));
        ws.onopen = () => { this.retry = 0; this.hooks.onState(this.state, true); };
        ws.onmessage = (m) => {
            let ev;
            try {
                ev = JSON.parse(m.data);
            }
            catch {
                return;
            }
            this.state = reduce(this.state, ev);
            this.hooks.onState(this.state, true);
            if (this.state.needResync) {
                ws.onclose = null;
                ws.close();
                void this.start();
            }
        };
        ws.onclose = () => { this.hooks.onState(this.state, false); this.schedule(); };
    }
    schedule() {
        if (this.stopped)
            return;
        this.retry = Math.min(this.retry + 1, 5);
        setTimeout(() => void this.start(), 500 * this.retry);
    }
    /** VAD 候选被用户确认（Enter）：把 candidate_id 发给后端升级为主答案。 */
    acceptCandidate(candidateId) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ type: 'candidate_accept', candidate_id: candidateId }));
        }
    }
    ack(promptId) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN)
            this.hooks.sendAck(this.ws, promptId);
    }
}
