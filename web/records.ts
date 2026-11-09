// 记录库：左侧历史会话列表 + 右侧概览/逐字稿/轮次与提示/复盘编辑。
// 数据来自 GET /api/sessions（只读列表）与既有 records 接口；导出/打包/删除需 X-Local-Token。
import {
  ANSWERER_HINT, answererNote, blockLabel, bundleName, canEdit, deleteBlocked, exportName,
  FILTERS, filterCounts, filterSessions, formatDateTime, formatDuration, isEdited, kindLabel, levelLabel,
  modeLabel, noPrimaryNote, promptStateLabel, promptText, promptUrls, roleLabel, segmentFlags, segmentOffset,
  segmentOriginal, segmentText, statusLabel, transcriptStart, turnGroups, turnLevel, turnRollbacks, visibleDelay,
  type FilterKey, type Prompt, type Segment, type SessionRow, type Turn,
} from './records/logic.js';

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
function el(tag: string, text = '', cls = ''): HTMLElement {
  const e = document.createElement(tag);
  if (text) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
function button(text: string, cls = '', act = '', data: Record<string, string> = {}): HTMLButtonElement {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = `btn ${cls}`.trim();
  b.textContent = text;
  if (act) b.dataset.act = act;
  for (const [k, v] of Object.entries(data)) b.dataset[k] = v;
  return b;
}
const tag = (text: string, cls = ''): HTMLElement => el('span', text, `tag ${cls}`.trim());

type Tab = 'overview' | 'transcript' | 'turns' | 'review';
const TABS: { key: Tab; label: string }[] = [
  { key: 'overview', label: '概览' },
  { key: 'transcript', label: '逐字稿' },
  { key: 'turns', label: '轮次与提示' },
  { key: 'review', label: '复盘编辑' },
];

interface Recording { id: string; source: string; sequence: number }

const S = {
  token: '',
  sessions: [] as SessionRow[],
  selected: '',
  tab: 'overview' as Tab,
  filter: 'all' as FilterKey,
  detail: null as Record<string, unknown> | null,
  segments: [] as Segment[],
  turns: [] as Turn[],
  prompts: [] as Prompt[],
  recordings: [] as Recording[],
  confirmToken: '',
};

const say = (text: string, bad = false): void => {
  const m = $('msg');
  m.textContent = text;
  m.classList.toggle('bad', bad);
};
const current = (): SessionRow | undefined => S.sessions.find((s) => s.id === S.selected);

async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(url, { headers: { 'X-Local-Token': S.token } });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error((data as { detail?: string }).detail || `请求失败（${res.status}）`),
    { status: res.status });
  return data as T;
}

async function download(url: string, name: string, okText: string): Promise<void> {
  const res = await fetch(url, { headers: { 'X-Local-Token': S.token } });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail || `下载失败（${res.status}）`);
  }
  const href = URL.createObjectURL(await res.blob());
  const a = document.createElement('a');
  a.href = href;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
  say(okText);
}

// ---------- 列表 ----------
function drawFilters(): void {
  const box = $('filters');
  const counts = filterCounts(S.sessions);
  box.replaceChildren();
  for (const f of FILTERS) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'chip' + (f.key === S.filter ? ' on' : '');
    b.textContent = `${f.label} ${counts[f.key]}`;
    b.dataset.filter = f.key;
    box.append(b);
  }
}

function drawList(): void {
  const ul = $('list');
  ul.replaceChildren();
  const items = filterSessions(S.sessions, S.filter, $<HTMLInputElement>('search').value);
  for (const s of items) {
    const li = el('li', '', 'row rec-row' + (s.id === S.selected ? ' sel' : '') + (s.deleted ? ' gone' : ''));
    li.dataset.id = s.id;
    const open = button('', 'open');
    const head = el('span', '', 'rec-head');
    head.append(el('span', formatDateTime(s.started_at), 'rec-time'));
    const st = tag(s.deleted ? '已删除' : statusLabel(s.status), s.deleted ? 'bad' : s.status === 'active' ? 'warn' : 'ok');
    head.append(st);
    const who = el('span', s.profile_alias || '未关联资料', 'rec-who');
    const meta = el('span', '', 'rec-meta');
    const bits = [modeLabel(s.mode), (s.roles || []).join('、'), formatDuration(s.duration_s)];
    if (s.library_name) bits.push(s.library_name);
    meta.textContent = bits.filter(Boolean).join(' · ');
    open.append(head, who, meta);
    open.addEventListener('click', () => { void select(s.id); });
    li.append(open);
    ul.append(li);
  }
  if (!items.length) ul.append(el('li', '没有符合条件的会话', 'empty'));
  $('list-info').textContent = `共 ${S.sessions.length} 场 · 当前 ${items.length} 场`;
}

async function loadList(): Promise<void> {
  const data = await getJSON<{ sessions: SessionRow[] }>('/api/sessions?limit=200');
  S.sessions = data.sessions || [];
}

async function select(sid: string): Promise<void> {
  if (S.selected === sid && S.detail) return;
  S.selected = sid;
  S.confirmToken = '';
  const url = new URL(window.location.href);
  url.searchParams.set('session', sid);
  window.history.replaceState(null, '', url);
  drawList();
  await loadDetail();
  drawDetail();
}

async function loadDetail(): Promise<void> {
  const sid = S.selected;
  S.detail = null;
  S.segments = [];
  S.turns = [];
  S.prompts = [];
  S.recordings = [];
  if (!sid) return;
  const s = current();
  S.detail = await getJSON<Record<string, unknown>>(`/api/setup/${encodeURIComponent(sid)}`)
    .catch(() => null);
  if (s?.deleted) return;
  const base = `/api/sessions/${encodeURIComponent(sid)}`;
  try {
    const [segs, turns, prompts, recs] = await Promise.all([
      getJSON<Segment[]>(`${base}/transcript`).catch(() => []),
      getJSON<Turn[]>(`${base}/turns`).catch(() => []),
      getJSON<Prompt[]>(`${base}/prompts`).catch(() => []),
      getJSON<Recording[]>(`${base}/recordings`).catch(() => []),
    ]);
    S.segments = segs || [];
    S.turns = turns || [];
    S.prompts = prompts || [];
    S.recordings = recs || [];
  } catch (e) {
    say((e as Error).message, true);
  }
}

// ---------- 详情 ----------
function kv(rows: [string, string][]): HTMLElement {
  const dl = el('dl', '', 'kv');
  for (const [k, v] of rows) dl.append(el('dt', k), el('dd', v));
  return dl;
}

function dhead(s: SessionRow): HTMLElement {
  const head = el('header', '', 'rec-dhead');
  const title = el('div');
  title.append(el('h2', s.profile_alias || '未关联资料'));
  const meta = el('p', '', 'muted');
  meta.textContent = [modeLabel(s.mode), statusLabel(s.status), formatDateTime(s.started_at),
    `时长 ${formatDuration(s.duration_s)}`, s.library_name || ''].filter(Boolean).join(' · ');
  title.append(meta);
  head.append(title);

  const bar = el('div', '', 'dhead-actions');
  const blocked = deleteBlocked(s);
  const exp = button('导出 Markdown', 'pill', 'export');
  exp.disabled = !!s.deleted;
  exp.title = s.deleted ? '已移至回收目录' : '下载本场 Markdown（组件分区）';
  const zip = button('下载 zip', '', 'zip');
  zip.disabled = !!s.deleted;
  const del = button('删除到回收', 'danger', 'delete');
  del.disabled = !!blocked;
  if (blocked) del.title = blocked;
  bar.append(exp, zip);
  if (S.confirmToken) {
    bar.append(button('确认移到回收', 'danger', 'delete-confirm'), button('取消', '', 'delete-cancel'));
  } else {
    bar.append(del);
  }
  head.append(bar);
  const note = deleteBlocked(s);
  if (note) head.append(el('p', note, 'muted'));
  return head;
}

function tabsBar(): HTMLElement {
  const nav = el('nav', '', 'tabs');
  nav.setAttribute('role', 'tablist');
  for (const t of TABS) {
    const b = button(t.label, 'tab-btn' + (t.key === S.tab ? ' on' : ''), '', { tab: t.key });
    b.setAttribute('role', 'tab');
    b.setAttribute('aria-selected', String(t.key === S.tab));
    nav.append(b);
  }
  return nav;
}

function overview(s: SessionRow): HTMLElement {
  const box = el('div', '', 'panel-body');
  if (s.deleted) {
    box.append(el('div', `已于 ${formatDateTime(s.deleted_at)} 移至回收目录：逐字稿、导出与复盘不可用；`
      + '恢复需人工处理数据目录下的回收目录。', 'alert'));
  }
  const banner = typeof S.detail?.degraded_banner === 'string' ? S.detail.degraded_banner : '';
  if (banner) box.append(el('div', `降级开始：${banner}`, 'alert warn'));
  if (s.status === 'active') box.append(el('p', '会话仍在进行中，记录可能不完整；结束后再复盘。', 'muted'));
  const dash = '—';
  box.append(kv([
    ['会话编号', s.id],
    ['模式', modeLabel(s.mode)],
    ['状态', s.deleted ? '已删除（回收目录）' : statusLabel(s.status)],
    ['开始时间', formatDateTime(s.started_at)],
    ['结束时间', formatDateTime(s.stopped_at)],
    ['时长', formatDuration(s.duration_s)],
    ['回答者资料', s.profile_alias || dash],
    ['岗位', (s.roles || []).join('、') || dash],
    ['主库', s.library_name || dash],
    ['逐字稿片段', s.deleted ? dash : String(S.segments.length)],
    ['轮次', s.deleted ? dash : String(s.turns ?? 0)],
    ['系统提示', s.deleted ? dash : String(s.prompts ?? 0)],
    ['录音', s.deleted ? dash : `${s.recordings ?? 0} 段`],
    ['录音缺口', s.deleted ? dash : `${s.gaps ?? 0} 处`],
  ]));

  const rec = el('section', '', 'rec-block');
  rec.append(el('h3', '录音'));
  if (s.deleted) {
    rec.append(el('p', '录音已随会话移入回收目录。', 'muted'));
  } else if (!S.recordings.length) {
    rec.append(el('p', '没有录音记录（导出与 zip 仍会生成）。', 'muted'));
  } else {
    const ul = el('ul', '', 'rec-files');
    for (const r of S.recordings) {
      const li = el('li');
      li.append(el('span', `${r.source || '音源'} 第 ${r.sequence} 段`));
      const a = document.createElement('a');
      a.href = `/api/recordings/${encodeURIComponent(r.id)}`;
      a.textContent = '下载';
      a.className = 'src';
      a.setAttribute('download', '');
      li.append(a);
      ul.append(li);
    }
    rec.append(ul);
  }
  box.append(rec);
  if (!s.deleted) {
    box.append(el('p', '录音文件缺失/损坏状态以导出 Markdown 或 zip manifest 为准；'
      + '预算用量只按日汇总，未按会话拆分。', 'muted'));
  }
  return box;
}

function transcript(s: SessionRow): HTMLElement {
  const box = el('div', '', 'panel-body');
  if (s.deleted) {
    box.append(el('p', '逐字稿已移入回收目录，不再提供读取。', 'empty'));
    return box;
  }
  const note = answererNote(S.segments);
  if (note) box.append(el('div', note, 'alert warn'));
  box.append(el('p', ANSWERER_HINT, 'muted'));
  if (!S.segments.length) {
    box.append(el('p', '没有逐字稿片段。', 'empty'));
    return box;
  }
  const t0 = transcriptStart(S.segments);
  const ol = el('ol', '', 'transcript');
  for (const seg of S.segments) {
    const li = el('li', '', 'seg');
    li.dataset.role = seg.edited_role || seg.role || 'unknown';
    li.append(el('span', formatDuration(segmentOffset(seg, t0)), 'seg-time'));
    li.append(el('span', roleLabel(seg.edited_role || seg.role), 'seg-role'));
    const body = el('div', '', 'seg-body');
    const meta = el('div', '', 'seg-meta');
    meta.append(el('span', seg.source || '未知音源', 'seg-src'));
    for (const f of segmentFlags(seg)) meta.append(tag(f, f === 'unknown' || f === 'overlap' ? 'warn' : ''));
    if (isEdited(seg)) meta.append(tag('已修订', 'ok'));
    body.append(meta);
    if (isEdited(seg)) body.append(el('p', `原文：${segmentOriginal(seg)}`, 'seg-orig'));
    body.append(el('p', segmentText(seg), 'seg-text'));
    li.append(body);
    ol.append(li);
  }
  box.append(ol);
  return box;
}

function promptItem(p: Prompt): HTMLElement {
  const li = el('li', '', 'prompt');
  const head = el('div', '', 'prompt-head');
  head.append(tag(blockLabel(p.block), 'src'), tag(`来源 ${p.source_tag || '—'}`), tag(`级别 ${levelLabel(p.level)}`));
  head.append(tag(promptStateLabel(p.state), p.state === 'late_archived' ? 'warn' : ''));
  li.append(head);
  const text = promptText(p);
  if (text) li.append(el('p', text, 'prompt-text'));
  for (const u of promptUrls(p)) {
    const a = document.createElement('a');
    a.href = u;
    a.textContent = u;
    a.className = 'src';
    a.target = '_blank';
    a.rel = 'noreferrer';
    li.append(a);
  }
  return li;
}

function turnsPanel(s: SessionRow): HTMLElement {
  const box = el('div', '', 'panel-body');
  if (s.deleted) {
    box.append(el('p', '轮次与提示已移入回收目录。', 'empty'));
    return box;
  }
  const { groups, orphans } = turnGroups(S.turns, S.prompts);
  if (!groups.length && !orphans.length) {
    box.append(el('p', '没有轮次或系统提示记录。', 'empty'));
    return box;
  }
  const noPrimary = noPrimaryNote(S.prompts);
  if (noPrimary) box.append(el('p', noPrimary, 'alert warn'));
  for (const g of groups) {
    const card = el('article', '', 'turn');
    card.dataset.turn = g.turn.id;
    const head = el('div', '', 'turn-head');
    head.append(tag(`级别 ${levelLabel(turnLevel(g.turn, g.prompts))}`, 'level'));
    head.append(tag(kindLabel(g.turn.kind), 'src'));
    if (g.turn.state) head.append(tag(g.turn.state));
    card.append(head);
    card.append(el('p', g.turn.text || '（题目文本缺失）', 'turn-q'));
    const meta = [];
    if (turnRollbacks(g.turn)) meta.push(`回滚 ${turnRollbacks(g.turn)} 次`);
    const delay = visibleDelay(g.turn);
    if (delay !== null) meta.push(`可见延迟 ${delay.toFixed(2)} 秒`);
    if (g.turn.parent_id) meta.push(`父题 ${g.turn.parent_id}`);
    if (meta.length) card.append(el('p', meta.join(' · '), 'turn-meta'));
    if (g.prompts.length) {
      const ul = el('ul', '', 'prompts');
      for (const p of g.prompts) ul.append(promptItem(p));
      card.append(ul);
    } else {
      card.append(el('p', '本题没有系统提示。', 'muted'));
    }
    box.append(card);
  }
  if (orphans.length) {
    const sec = el('section', '', 'rec-block');
    sec.append(el('h3', '未关联轮次的提示'));
    const ul = el('ul', '', 'prompts');
    for (const p of orphans) ul.append(promptItem(p));
    sec.append(ul);
    box.append(sec);
  }
  return box;
}

function reviewPanel(s: SessionRow): HTMLElement {
  const box = el('div', '', 'panel-body');
  if (!canEdit(s)) {
    if (s.deleted) box.append(el('p', '已在回收目录，不能复盘编辑。', 'alert'));
    else if (s.status === 'active') box.append(el('p', '进行中的会话不能复盘编辑，请先在会话页结束。', 'alert warn'));
    else box.append(el('p', '会话尚未结束，暂不能复盘编辑。', 'muted'));
    return box;
  }
  if (!S.segments.length) {
    box.append(el('p', '没有可编辑的逐字稿片段。', 'empty'));
    return box;
  }
  box.append(el('p', '修订另存为 edited_text，原文保留可回看；不会覆盖现场数据。', 'muted'));
  const t0 = transcriptStart(S.segments);
  for (const seg of S.segments) {
    const card = el('article', '', 'edit-row');
    card.dataset.seg = String(seg.id);
    const head = el('div', '', 'edit-head');
    head.append(el('span', formatDuration(segmentOffset(seg, t0)), 'seg-time'));
    head.append(el('span', roleLabel(seg.edited_role || seg.role), 'seg-role'));
    head.append(el('span', seg.source || '', 'seg-src'));
    if (isEdited(seg)) head.append(tag('已修订', 'ok'));
    card.append(head);
    if (isEdited(seg)) card.append(el('p', `原文：${segmentOriginal(seg)}`, 'seg-orig'));
    const area = document.createElement('textarea');
    area.id = `edit-${seg.id}`;
    area.value = segmentText(seg);
    area.setAttribute('aria-label', `编辑片段 ${seg.id}`);
    card.append(area);
    card.append(button('保存修订', 'pill', '', { save: String(seg.id) }));
    box.append(card);
  }
  return box;
}

function panel(s: SessionRow): HTMLElement {
  if (S.tab === 'transcript') return transcript(s);
  if (S.tab === 'turns') return turnsPanel(s);
  if (S.tab === 'review') return reviewPanel(s);
  return overview(s);
}

function drawDetail(): void {
  const box = $('detail');
  box.replaceChildren();
  const s = current();
  if (!s) {
    box.append(el('p', '请从左侧选择一场会话', 'empty'));
    return;
  }
  box.append(dhead(s), tabsBar(), panel(s));
}

// ---------- 操作 ----------
async function requestDelete(): Promise<void> {
  const sid = S.selected;
  const res = await fetch(`/api/sessions/${encodeURIComponent(sid)}`, {
    method: 'DELETE', headers: { 'X-Local-Token': S.token },
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 409 && (data as { detail?: { need_confirm?: boolean; confirm_token?: string } }).detail?.need_confirm) {
    S.confirmToken = (data as { detail: { confirm_token: string } }).detail.confirm_token;
    drawDetail();
    say('请再次确认：删除只移入回收目录，不直接销毁。');
    return;
  }
  throw new Error((data as { detail?: string }).detail || `删除失败（${res.status}）`);
}

async function confirmDelete(): Promise<void> {
  const sid = S.selected;
  const url = `/api/sessions/${encodeURIComponent(sid)}?confirm=true&confirm_token=${encodeURIComponent(S.confirmToken)}`;
  const res = await fetch(url, { method: 'DELETE', headers: { 'X-Local-Token': S.token } });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((data as { detail?: string }).detail || `删除失败（${res.status}）`);
  S.confirmToken = '';
  await loadList();
  await loadDetail();
  draw();
  say('已移至回收目录；列表已标记为已删除。');
}

async function saveSegment(id: number): Promise<void> {
  const area = document.getElementById(`edit-${id}`) as HTMLTextAreaElement | null;
  if (!area) return;
  const res = await fetch(`/api/sessions/${encodeURIComponent(S.selected)}/segments/${id}/edit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Local-Token': S.token },
    body: JSON.stringify({ text: area.value }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((data as { detail?: string }).detail || `保存失败（${res.status}）`);
  const idx = S.segments.findIndex((x) => x.id === id);
  if (idx >= 0) S.segments[idx] = data as Segment;
  drawDetail();
  say('片段已保存；原文保留，可回看。');
}

function draw(): void {
  drawFilters();
  drawList();
}

async function main(): Promise<void> {
  S.token = (await getJSON<{ token: string }>('/api/status')).token;
  await loadList();
  const wanted = new URLSearchParams(window.location.search).get('session') || '';
  const first = wanted && S.sessions.some((s) => s.id === wanted) ? wanted : S.sessions[0]?.id || '';
  if (wanted && !S.sessions.some((s) => s.id === wanted)) say('未找到该会话，已显示最近一场。', true);
  if (first) {
    S.selected = first;
    const url = new URL(window.location.href);
    url.searchParams.set('session', first);
    window.history.replaceState(null, '', url);
    await loadDetail();
  }
  draw();
  drawDetail();

  $<HTMLInputElement>('search').addEventListener('input', drawList);
  $('filters').addEventListener('click', (ev) => {
    const chip = (ev.target as HTMLElement).closest<HTMLElement>('[data-filter]');
    if (!chip?.dataset.filter) return;
    S.filter = chip.dataset.filter as FilterKey;
    draw();
  });
  $('detail').addEventListener('click', (ev) => {
    const btn = (ev.target as HTMLElement).closest<HTMLButtonElement>('button');
    if (!btn) return;
    const sid = S.selected;
    try {
      if (btn.dataset.tab) {
        S.tab = btn.dataset.tab as Tab;
        drawDetail();
      } else if (btn.dataset.act === 'export') {
        void download(`/api/sessions/${encodeURIComponent(sid)}/export`, exportName(sid), '已导出 Markdown');
      } else if (btn.dataset.act === 'zip') {
        void download(`/api/sessions/${encodeURIComponent(sid)}/bundle`, bundleName(sid), '已下载 zip');
      } else if (btn.dataset.act === 'delete') {
        say('');
        void requestDelete().catch((e) => say((e as Error).message, true));
      } else if (btn.dataset.act === 'delete-confirm') {
        void confirmDelete().catch((e) => say((e as Error).message, true));
      } else if (btn.dataset.act === 'delete-cancel') {
        S.confirmToken = '';
        drawDetail();
        say('已取消删除');
      } else if (btn.dataset.save) {
        void saveSegment(Number(btn.dataset.save)).catch((e) => say((e as Error).message, true));
      }
    } catch (e) {
      say((e as Error).message, true);
    }
  });
}

main().catch((e) => say(String((e as Error).message ?? e), true));
