import {
  Action, Entry, FILTERS, FilterKey, batchBody, batchBlockers, batchCandidates, batchUrl, canApprove, describeRejected,
  entriesUrl, filterCounts, filterEntries, keyAction, progress, progressUrl, reviewBody, reviewUrl, scanMarks, step,
} from './secondary/logic.js';

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
function el(tag: string, text = '', cls = ''): HTMLElement {
  const e = document.createElement(tag);
  if (text) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
interface Prog { total: number; approved: number; rejected: number; pending: number; by_role: { role: string; total: number; approved: number }[] }
const S = { token: '', role: '', entries: [] as Entry[], prog: { total: 0, approved: 0, rejected: 0, pending: 0, by_role: [] } as Prog,
  filter: 'all' as FilterKey, selected: '', checked: new Set<string>() };
const say = (t: string, bad = false) => { const m = $('msg'); m.textContent = t; m.classList.toggle('bad', bad); };

async function api(url: string, body?: unknown) {
  const r = await fetch(url, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Local-Token': S.token }, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(data.detail || '本地请求失败'), { data });
  return data;
}

function view(): Entry[] { return filterEntries(S.entries, S.filter, $<HTMLInputElement>('search').value); }
const current = () => S.entries.find((e) => e.id === S.selected);

function drawRoles() {
  const sel = $<HTMLSelectElement>('role'), cur = S.role;
  sel.replaceChildren(new Option(`全部岗位（${S.prog.total}）`, ''),
    ...S.prog.by_role.map((r) => new Option(`${r.role}（${r.approved}/${r.total}）`, r.role)));
  sel.value = cur;
}
function drawProgress() {
  const p = progress(S.entries);
  $('prog-label').textContent = p.label + (S.role ? ` · ${S.role}` : '');
  $('prog-fill').style.width = p.pct + '%';
  $('prog').setAttribute('aria-valuenow', String(p.reviewed));
  $('prog').setAttribute('aria-valuemax', String(p.total || 1));
}
function drawFilters() {
  const counts = filterCounts(S.entries), box = $('filters');
  box.replaceChildren();
  for (const f of FILTERS) {
    const b = el('button', `${f.label} ${counts[f.key]}`, 'chip' + (f.key === S.filter ? ' on' : ''));
    b.setAttribute('type', 'button'); b.dataset.filter = f.key;
    b.addEventListener('click', () => { S.filter = f.key; draw(); });
    box.append(b);
  }
}
const STATUS_TEXT: Record<string, string> = { approved: '通过', rejected: '驳回', pending: '未审' };
function badges(e: Entry): HTMLElement {
  const row = el('span', '', 'badges');
  for (const r of e.roles ?? []) row.append(el('span', r, 'tag'));
  if (e.type) row.append(el('span', e.type, 'tag'));
  if (scanMarks(e).length) row.append(el('span', '合规待查', 'tag warn'));
  row.append(el('span', STATUS_TEXT[e.status] ?? e.status, 'tag st-' + e.status));
  return row;
}
function drawList() {
  const ul = $('list'), items = view();
  ul.replaceChildren();
  for (const e of items) {
    const li = el('li', '', 'row' + (e.id === S.selected ? ' sel' : '') + (scanMarks(e).length ? ' fab' : ''));
    li.dataset.id = e.id;
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = S.checked.has(e.id); cb.disabled = e.status !== 'pending';
    cb.setAttribute('aria-label', '选择 ' + e.id);
    cb.addEventListener('change', () => { cb.checked ? S.checked.add(e.id) : S.checked.delete(e.id); drawBatch(); });
    const open = el('button', '', 'open'); open.setAttribute('type', 'button');
    open.append(el('b', e.id), el('span', e.question, 'q'), badges(e));
    open.addEventListener('click', () => select(e.id));
    li.append(cb, open);
    ul.append(li);
  }
  if (!items.length) ul.append(el('li', '没有符合条件的条目', 'empty'));
}
function drawBatch() {
  const blocked = [...S.checked].map((id) => S.entries.find((e) => e.id === id)).filter((e): e is Entry => !!e && batchBlockers(e).length > 0);
  $('batch-info').textContent = `视图内可批量 ${batchCandidates(view()).length} 条（无合规扫描标记）；已勾选 ${S.checked.size} 条`
    + (blocked.length ? `，其中 ${blocked.length} 条含标记，提交将被拒` : '');
  $<HTMLButtonElement>('batch-btn').disabled = S.checked.size === 0 || !$<HTMLInputElement>('batch-confirm').checked;
}

function fieldBlock(title: string, ...kids: HTMLElement[]) {
  const s = el('section', '', 'blk'); s.append(el('h3', title), ...kids); return s;
}
function drawDetail() {
  const box = $('detail'), e = current();
  box.replaceChildren();
  if (!e) { box.append(el('p', '请从左侧选择一条', 'empty')); return; }
  const head = el('div', '', 'dhead');
  head.append(el('h2', `${e.id} · ${e.question}`), badges(e));
  head.append(el('p', [(e.roles ?? []).join(' / '), e.source, STATUS_TEXT[e.status]].filter(Boolean).join(' · '), 'muted'));
  box.append(head);
  if (scanMarks(e).length) {
    box.append(el('div', '合规扫描命中：' + [...new Set(scanMarks(e).map((m) => m.kind))].join('、') + '。需先修订种子内容，不能通过。', 'alert'));
  }
  box.append(el('div', '次优库内容只作为“通用建议”展示，不冒充个人经历；示例是待本人补充的占位。', 'alert warn'));
  box.append(fieldBlock('结论', el('p', e.conclusion || '（空）', 'orig')));
  const points = el('ul', '', 'facts');
  for (const p of e.points ?? []) points.append(el('li', p));
  box.append(fieldBlock('回答要点', points));
  if (e.variants?.length) box.append(fieldBlock('问题变体', ...e.variants.map((v) => el('div', v, 'variant'))));
  box.append(fieldBlock('示例（待本人补充的占位）', el('pre', e.example || '（空）', 'orig')));
  box.append(fieldBlock('注意', el('p', e.caution || '（空）', 'orig')));

  const chk = document.createElement('input'); chk.type = 'checkbox'; chk.id = 'review-confirm';
  const lab = el('label', '', 'check'); lab.append(chk, el('span', '已核对内容为通用建议，不含具体经历或数字'));
  const bar = el('div', '', 'actions');
  const act = (a: Action, text: string, cls: string, key: string) => {
    const b = el('button', `${text}（${key}）`, 'btn ' + cls); b.setAttribute('type', 'button'); b.dataset.action = a;
    b.addEventListener('click', () => void submit(a)); return b;
  };
  bar.append(act('approve', '通过', 'pill', 'a'), act('defer', '暂缓', '', 'h'), act('reject', '驳回', 'danger', 'r'));
  box.append(lab, bar);
}

async function submit(a: Action) {
  const e = current();
  if (!e) return;
  const confirmed = $<HTMLInputElement>('review-confirm').checked;
  if (a === 'approve') {
    const v = canApprove(e, confirmed);
    if (!v.ok) { say(v.why, true); return; }
  }
  try {
    await api(reviewUrl(e.id), reviewBody(a, confirmed));
    const idx = view().findIndex((x) => x.id === e.id);
    await load(false);
    const rest = view();
    S.selected = rest[Math.min(Math.max(idx, 0), rest.length - 1)]?.id ?? S.selected;
    say(`${e.id} 已${a === 'approve' ? '通过' : a === 'defer' ? '暂缓' : '驳回'}`);
    draw();
  } catch (err) { say(String((err as Error).message), true); }
}

async function submitBatch() {
  const rej = $('batch-rejects'); rej.replaceChildren();
  try {
    const r = await api(batchUrl(), batchBody([...S.checked], $<HTMLInputElement>('batch-confirm').checked));
    S.checked.clear(); $<HTMLInputElement>('batch-confirm').checked = false;
    await load(false); say(`已批量通过 ${r.approved} 条`); draw();
  } catch (err) {
    const data = (err as { data?: { rejected?: { id: string; reasons: string[] }[] } }).data;
    say(String((err as Error).message), true);
    for (const line of describeRejected(data?.rejected)) rej.append(el('li', line));
  }
}

function select(id: string) { S.selected = id; draw(); $('detail').scrollTop = 0; }
function draw() { drawProgress(); drawFilters(); drawList(); drawBatch(); drawDetail(); }

async function load(reset = true) {
  const [entries, prog] = await Promise.all([api(entriesUrl(S.role)), api(progressUrl())]);
  S.entries = entries; S.prog = prog;
  if (reset || !current()) S.selected = view()[0]?.id ?? '';
  for (const id of [...S.checked]) if (S.entries.find((e) => e.id === id)?.status !== 'pending') S.checked.delete(id);
}

async function main() {
  S.token = (await api('/api/status')).token;
  await load();
  $('role').addEventListener('change', async () => { S.role = $<HTMLSelectElement>('role').value; S.checked.clear(); await load(); draw(); });
  $('search').addEventListener('input', () => draw());
  $('batch-confirm').addEventListener('change', drawBatch);
  $('batch-btn').addEventListener('click', () => void submitBatch());
  $('select-safe').addEventListener('click', () => { batchCandidates(view()).forEach((e) => S.checked.add(e.id)); drawList(); drawBatch(); });
  document.addEventListener('keydown', (ev) => {
    const k = keyAction(ev);
    if (!k) return;
    ev.preventDefault();
    if (k === 'next' || k === 'prev') select(step(view(), S.selected, k === 'next' ? 1 : -1));
    else void submit(k);
  });
  drawRoles();
  draw();
}
main().catch((e) => say(String(e.message ?? e), true));
