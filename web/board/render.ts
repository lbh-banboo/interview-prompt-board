// DOM 渲染：全部用 textContent / createElement，不注入 HTML。主答案按 prompt_id 只建一次，补充只追加。
import { capLamps } from './health.js';
import { BoardState, Outline, clip, levelLabel, queueLabel, statusLabel, turnClosed } from './reducer.js';

const LAMP_TEXT: Record<string, string> = { ok: '正常', silent: '静音', lost: '中断', reconnecting: '重连中', down: '不可用' };
const LAMP_CLASS: Record<string, string> = { ok: 'ok', silent: 'warn', reconnecting: 'warn', lost: 'fail', down: 'fail' };

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls = '', text = ''): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
}
const $ = (id: string) => document.getElementById(id) as HTMLElement;

function outlineNode(o: Outline): DocumentFragment {
  const f = document.createDocumentFragment();
  f.append(el('p', 'conclusion', o.conclusion));
  const ol = el('ol', 'points');
  for (const pt of o.points ?? []) ol.append(el('li', '', pt));
  f.append(ol);
  if (o.examples) { const p = el('p', 'examples'); p.append(el('b', '', '例子/数字　'), document.createTextNode(o.examples)); f.append(p); }
  if (o.cautions) { const p = el('p', 'cautions'); p.append(el('b', '', '注意　'), document.createTextNode(o.cautions)); f.append(p); }
  return f;
}

export class Renderer {
  private primaryId = '';
  private suppCount = 0;
  private turnKey = '';
  private banner = '';
  private subKey = '';
  private statusKey = '';
  private acked = new Set<string>();
  /** 任一提示（主答案 / 状态 / 补充）真正进入可见区后回 ACK；同一 prompt_id 本页只回一次。 */
  onPromptShown: (promptId: string) => void = () => {};

  private ack(promptId: string | undefined): void {
    if (!promptId || this.acked.has(promptId)) return;
    this.acked.add(promptId);
    requestAnimationFrame(() => requestAnimationFrame(() => this.onPromptShown(promptId)));
  }

  render(s: BoardState, connected: boolean): void {
    const root = $('board');
    root.classList.toggle('closed', turnClosed(s));
    root.classList.toggle('ended', s.ended);
    this.bar(s, connected);
    this.notices(s);
    this.question(s);
    this.vad(s);
    this.primary(s);
    this.supplements(s);
    this.side(s);
  }

  /** VAD 提前候选：可能没问完；按 Enter 采用（上行消息由 BoardClient 发送）。 */
  private vad(s: BoardState): void {
    const node = this.ensureNode('vad-cand', 'vad-cand', () => document.querySelector('.question'));
    const c = s.vadCandidate;
    node.hidden = !c;
    if (!c) { node.replaceChildren(); return; }
    const parts: HTMLElement[] = [el('span', 'vad-hint', '可能未问完 · 按 Enter 采用')];
    if (c.text) parts.push(el('span', 'vad-preview', c.text));
    if (c.outline?.conclusion) parts.push(el('p', 'vad-answer', c.outline.conclusion));
    node.replaceChildren(...parts);
  }

  private lamp(id: string, label: string, state: string | undefined): void {
    const e = $(id);
    const known = state !== undefined;
    e.className = 'lamp ' + (known ? LAMP_CLASS[state!] ?? 'idle' : 'idle');
    e.textContent = `${label} ${known ? LAMP_TEXT[state!] ?? state : '未接入'}`;
  }

  private bar(s: BoardState, connected: boolean): void {
    $('mode').textContent = s.mode === 'offline' ? '线下模式' : s.mode === 'online' ? '线上模式' : '会话';
    const offline = s.mode === 'offline';
    this.lamp('lamp-mic', offline ? '房间麦克风' : '回答方音源', offline ? s.sources['room']?.state : s.sources['mic']?.state);
    const meetingLamp = $('lamp-meeting');
    meetingLamp.style.display = offline ? 'none' : '';
    if (!offline) this.lamp('lamp-meeting', '提问方音源', s.sources['meeting']?.state);
    $('caps').replaceChildren(...capLamps(s.health).map((c) => {
      const e = el('span', `lamp cap ${c.cls}`, c.text ? `${c.label} ${c.text}` : c.label);
      e.id = `cap-${c.key}`;
      e.title = c.reason ? `${c.label}：${c.reason}` : c.text || c.label;
      return e;
    }));
    this.lamp('lamp-conn', '看板连接', connected ? 'ok' : 'down');
    const st = $('state');
    st.textContent = statusLabel(s);
    st.dataset.state = s.ended ? 'ended' : s.interrupt || s.status === 'interrupted' ? 'interrupt' : s.waiting_continue ? 'waiting' : s.status;
  }

  setBanner(text: string): void { this.banner = text; }

  /** 固定高度的常驻文案条：降级横幅 + 各能力 notices，出现/消失不挪动下方版面。 */
  private notices(s: BoardState): void {
    const items = [...(this.banner ? [el('span', 'notice banner-degraded', this.banner)] : []), ...s.notices.map((n) => el('span', 'notice', n))];
    $('notices').replaceChildren(...items);
  }

  private question(s: BoardState): void {
    const q = s.question;
    const text = q ? q.text : '等待提问…';
    const t = $('qtext');
    if (t.textContent !== text) t.textContent = text;
    t.classList.toggle('empty', !q);
    const badge = $('qbadge');
    badge.textContent = q?.corrected ? '已修正' : '';
    badge.hidden = !q?.corrected;
    const level = $('level');
    level.textContent = s.primary ? levelLabel(s.primary.level) : '';
    level.hidden = !s.primary;
    const rev = $('revisions');
    rev.replaceChildren(...(q?.revisions ?? []).map((r) => el('li', '', `原文（v${r.from_version ?? '?'}）：${r.text}`)));
    rev.hidden = !q?.revisions.length;
    const subs = q?.sub_questions ?? [];
    const sub = $('subq');
    const key = subs.join('\n');
    if (key !== this.subKey) {
      this.subKey = key;
      sub.replaceChildren(...subs.map((t, i) => el('li', '', i === 0 ? `主 ${clip(t, 14)}` : `${i + 1}. ${clip(t, 14)}`)));
    }
    sub.hidden = subs.length < 2;
  }

  private ensureNode(id: string, cls: string, anchor: () => Element | null): HTMLElement {
    let n = document.getElementById(id);
    if (!n) {
      n = el('p', cls);
      n.id = id;
      anchor()?.after(n);
    }
    return n;
  }

  /** 主答案红标：含未核实内容时醒目提示，但不遮挡正文。 */
  private alertNode(): HTMLElement {
    const n = this.ensureNode('primary-alert', 'primary-alert', () => $('primary').querySelector('.meta'));
    n.setAttribute('role', 'alert');
    return n;
  }

  /** 主答案存在时状态提示不能消失：常驻在主答案下方。 */
  private statusNode(): HTMLElement {
    return this.ensureNode('primary-status', 'primary-status', () => $('primary-body'));
  }

  private primary(s: BoardState): void {
    const box = $('primary');
    const key = `${s.turn_id}:${s.epoch}`;
    const id = s.primary?.prompt_id ?? '';
    const empty = $('primary-empty');
    box.classList.toggle('stale', s.primaryStale);
    $('primary-note').textContent = s.primaryStale ? '问题已修正，此提示对应修正前的文本' : '';
    if (key !== this.turnKey || id !== this.primaryId) {
      const swapped = key !== this.turnKey;
      this.turnKey = key;
      this.primaryId = id;
      const body = $('primary-body');
      body.replaceChildren();
      if (swapped) { box.classList.remove('enter'); void box.offsetWidth; box.classList.add('enter'); }
      if (s.primary) {
        body.append(outlineNode(s.primary.outline));
        $('source-tag').textContent = s.primary.source_tag;
        const pid = s.primary.prompt_id;
        this.ack(pid);
      }
    }
    const alert = this.alertNode();
    const flagged = !!s.primary?.flagged;
    alert.hidden = !flagged;
    alert.textContent = flagged
      ? `含未核实内容，非已确认口径${s.primary?.pending_marks ? `（待核 ${s.primary!.pending_marks} 处）` : ''}`
      : '';
    empty.hidden = !!s.primary;
    empty.textContent = s.primary ? '' : (s.promptStatus || (s.question ? '正在匹配答案…' : ''));
    const status = this.statusNode();
    status.hidden = !(s.primary && s.promptStatus);
    status.textContent = s.primary ? s.promptStatus : '';
    if (s.promptStatus) {  // 主答案为空时在占位行显示；主答案存在时在主答案下方常驻
      const statusKey = `${s.promptStatusId}:${s.promptStatus}`;
      if (statusKey !== this.statusKey) {
        this.statusKey = statusKey;
        this.ack(s.promptStatusId);
      }
    }
    $('primary-body').hidden = !s.primary;
    $('source-tag').hidden = !s.primary;
  }

  private supplements(s: BoardState): void {
    const list = $('supp-list');
    if (s.supplements.length < this.suppCount || this.turnKey !== list.dataset.turn) {
      list.replaceChildren();
      this.suppCount = 0;
      list.dataset.turn = this.turnKey;
    }
    for (const x of s.supplements.slice(this.suppCount)) {
      const li = el('li', 'supp-item');
      li.append(el('span', 'tag', x.source_tag), el('span', 'content', x.content));
      if (x.url) { const a = el('a', 'src', '来源'); a.href = x.url; a.rel = 'noreferrer noopener'; a.target = '_blank'; li.append(a); }
      if (x.elapsed_ms != null) li.append(el('span', 'ms', `${x.elapsed_ms} ms`));
      list.append(li);
      this.ack(x.prompt_id);
    }
    this.suppCount = s.supplements.length;
    $('supp-empty').hidden = s.supplements.length > 0;
  }

  private side(s: BoardState): void {
    const hits = s.candidate?.hits ?? [];
    const cand = $('cand-list');
    const items = hits.map((h: any) => el('li', '', String(h?.title ?? h?.text ?? h ?? '')));
    if (s.candidate?.text) items.unshift(el('li', 'heard', `听到：${s.candidate.text}`));
    cand.replaceChildren(...items);
    $('cand-empty').hidden = items.length > 0;
    const q = $('queue');
    q.replaceChildren(...s.queue.map((t, i) => el('li', '', queueLabel(i, t))));
    $('queue-empty').hidden = s.queue.length > 0;
  }
}
