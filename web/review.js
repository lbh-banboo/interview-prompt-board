import { FILTERS, FACT_KINDS, approvedWarning, batchBody, batchSelection, batchUrl, canApprove, describeRejected, editBody, editUrl, enhanceErrorLabel, enhanceLabel, enhancedLabel, factSpan, filterCounts, filterEntries, flagLabel, flagsOf, isConflict, isDeferred, isFabricated, keyAction, orderByQueue, outlineOf, progress, reimportBody, reimportUrl, reviewBody, reviewUrl, statsText, step, variantList, } from './review/logic.js';
const $ = (id) => document.getElementById(id);
function el(tag, text = '', cls = '') {
    const e = document.createElement(tag);
    if (text)
        e.textContent = text;
    if (cls)
        e.className = cls;
    return e;
}
function input(value = '', placeholder = '') {
    const i = document.createElement('input');
    i.type = 'text';
    i.value = value;
    i.placeholder = placeholder;
    return i;
}
const idle = () => ({ status: 'idle', total: 0, done: 0, enhanced: 0, failed: 0, error: '' });
const S = {
    token: '', lib: '', entries: [], queue: [], approvable: new Set(),
    filter: 'all', selected: '', checked: new Set(), enhance: idle(),
};
const say = (t, bad = false) => { const m = $('msg'); m.textContent = t; m.classList.toggle('bad', bad); };
const libBase = () => `/api/libraries/${encodeURIComponent(S.lib)}`;
async function api(url, body, method = 'POST') {
    const init = body === undefined ? {} : {
        method, headers: { 'Content-Type': 'application/json', 'X-Local-Token': S.token }, body: JSON.stringify(body),
    };
    const r = await fetch(url, init);
    const data = await r.json().catch(() => ({}));
    if (!r.ok)
        throw Object.assign(new Error(data.detail || '本地请求失败'), { data });
    return data;
}
function view() {
    const list = filterEntries(S.entries, S.filter, $('search').value);
    return $('queue-on').checked ? orderByQueue(list, S.queue) : list;
}
const current = () => S.entries.find((e) => e.id === S.selected);
function drawProgress() {
    const p = progress(S.entries);
    const label = $('prog-label');
    label.style.whiteSpace = 'normal'; // 窄屏允许换行，避免进度文案撑出横向滚动
    label.textContent = `${p.label} · 待审待核 ${p.verify} · 共 ${p.total} 条`;
    $('prog-fill').style.width = p.pct + '%';
    $('prog').classList.toggle('ready', p.g3Ready);
    $('prog').setAttribute('aria-valuenow', String(Math.min(p.eligible, 50)));
}
function drawEnhance() {
    const p = S.enhance || idle();
    $('enhance-label').textContent = enhanceLabel(p);
    $('enhance-btn').disabled = p.status === 'running' || !S.lib;
}
function drawFilters() {
    const counts = filterCounts(S.entries), box = $('filters');
    box.replaceChildren();
    for (const f of FILTERS) {
        const b = el('button', `${f.label} ${counts[f.key]}`, 'chip' + (f.key === S.filter ? ' on' : ''));
        b.setAttribute('type', 'button');
        b.dataset.filter = f.key;
        b.addEventListener('click', () => { S.filter = f.key; draw(); });
        box.append(b);
    }
}
function badges(e) {
    const row = el('span', '', 'badges');
    for (const f of flagsOf(e))
        row.append(el('span', flagLabel(f), 'tag ' + (f === 'pending_verify' ? 'warn' : 'bad')));
    if (e.attribution_uncertain)
        row.append(el('span', '说话人待核', 'tag warn'));
    const warning = approvedWarning(e);
    if (warning)
        row.append(el('span', warning, 'tag warn'));
    const st = e.status === 'approved' ? '已启用' : e.status === 'rejected' ? '驳回' : isDeferred(e) ? '暂缓' : '待审';
    row.append(el('span', st, 'tag st-' + e.status));
    return row;
}
function drawList() {
    const ul = $('list'), items = view();
    ul.replaceChildren();
    for (const e of items) {
        const cls = 'row' + (e.id === S.selected ? ' sel' : '') + (isFabricated(e) ? ' fab' : '') + (isConflict(e) ? ' conflict' : '');
        const li = el('li', '', cls);
        li.dataset.id = e.id;
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = S.checked.has(e.id);
        cb.disabled = e.status !== 'pending';
        cb.setAttribute('aria-label', '选择 ' + e.id);
        cb.addEventListener('change', () => { cb.checked ? S.checked.add(e.id) : S.checked.delete(e.id); drawBatch(); });
        const open = el('button', '', 'open');
        open.setAttribute('type', 'button');
        open.append(el('b', e.id), el('span', e.question, 'q'), badges(e));
        open.addEventListener('click', () => select(e.id));
        li.append(cb, open);
        ul.append(li);
    }
    if (!items.length)
        ul.append(el('li', '没有符合条件的条目', 'empty'));
}
function drawBatch() {
    const safeView = view().filter((e) => S.approvable.has(e.id));
    const { safe, blocked } = batchSelection(S.entries, S.checked, S.approvable);
    $('batch-info').textContent = `视图内可批量 ${safeView.length} 条；已勾选 ${S.checked.size} 条，可提交 ${safe.length} 条`
        + (blocked.length ? `；${blocked.length} 条含待核/冲突/虚构，提交时会被拦截` : '');
    $('batch-btn').disabled = safe.length === 0 || !$('batch-confirm').checked;
}
function fieldBlock(title, ...kids) {
    const s = el('section', '', 'blk');
    s.append(el('h3', title), ...kids);
    return s;
}
function editGroup(title, ...kids) {
    const g = el('div', '', 'edit-group');
    g.append(el('h4', title), ...kids);
    return g;
}
function listField(title, id, values, placeholder) {
    const box = el('div', '', 'edit-list');
    box.id = id;
    const add = (v = '') => {
        const row = el('div', '', 'edit-row');
        const text = input(v, placeholder);
        const del = el('button', '删除', 'mini danger');
        del.setAttribute('type', 'button');
        del.addEventListener('click', () => row.remove());
        row.append(text, del);
        box.append(row);
    };
    values.forEach(add);
    const addBtn = el('button', '＋ 添加', 'mini');
    addBtn.setAttribute('type', 'button');
    addBtn.addEventListener('click', () => add(''));
    const wrap = el('div', '', 'edit-group');
    wrap.append(el('h4', title), box, addBtn);
    return wrap;
}
const listValues = (id) => Array.from(document.querySelectorAll(`#${id} input[type=text]`))
    .map((i) => i.value.trim()).filter(Boolean);
function factRow(f) {
    const row = el('div', '', 'fact-row');
    const kind = document.createElement('select');
    kind.dataset.fKind = '1';
    for (const k of FACT_KINDS) {
        const o = document.createElement('option');
        o.value = k;
        o.textContent = k;
        kind.append(o);
    }
    kind.value = f.kind && FACT_KINDS.includes(f.kind) ? f.kind : 'metric';
    const text = input(f.text, '事实原文');
    text.dataset.fText = '1';
    const span = input(factSpan(f), '出处片段（逐字）');
    span.dataset.fSpan = '1';
    const tags = el('span', '', 'badges');
    if (f.conflict_group)
        tags.append(el('span', '冲突：' + f.conflict_group, 'tag bad'));
    if (f.flag)
        tags.append(el('span', flagLabel(f.flag), 'tag bad'));
    if (f.origin)
        tags.append(el('span', '来源：' + f.origin, 'tag'));
    const del = el('button', '删除', 'mini danger');
    del.setAttribute('type', 'button');
    del.addEventListener('click', () => row.remove());
    row.append(kind, text, span, tags, del);
    return row;
}
function factsEditor(facts) {
    const box = el('div', '', 'edit-list facts-edit');
    box.id = 'edit-facts';
    facts.forEach((f) => box.append(factRow(f)));
    const add = el('button', '＋ 添加事实', 'mini');
    add.setAttribute('type', 'button');
    add.addEventListener('click', () => box.append(factRow({ text: '' })));
    const wrap = el('div', '', 'edit-group');
    wrap.append(el('h4', '个人事实（保留条目出处）'), box, add);
    return wrap;
}
function collectFacts() {
    return Array.from(document.querySelectorAll('#edit-facts .fact-row')).map((row) => ({
        kind: row.querySelector('select').value,
        text: row.querySelector('[data-f-text]').value.trim(),
        source_span: row.querySelector('[data-f-span]').value.trim(),
    })).filter((f) => f.text);
}
function editorBlock(e) {
    const sec = el('section', '', 'blk editor');
    sec.append(el('h3', '编辑条目（保存后回到待审核）'));
    const q = input(e.question, '问题');
    q.id = 'edit-question';
    sec.append(editGroup('问题', q));
    sec.append(listField('问题变体（可增删）', 'edit-variants', variantList(e), '同义问法'));
    const o = outlineOf(e);
    const conclusion = document.createElement('textarea');
    conclusion.id = 'edit-conclusion';
    conclusion.value = o.conclusion;
    conclusion.placeholder = '结论';
    sec.append(editGroup('提纲 · 结论', conclusion));
    sec.append(listField('提纲 · 要点', 'edit-points', o.points, '要点'));
    sec.append(listField('提纲 · 示例', 'edit-examples', o.examples, '示例'));
    sec.append(listField('提纲 · 注意', 'edit-cautions', o.cautions, '注意事项'));
    sec.append(factsEditor(e.facts ?? []));
    const save = el('button', '保存编辑', 'btn pill');
    save.id = 'edit-save';
    save.setAttribute('type', 'button');
    save.addEventListener('click', () => void saveEdit());
    sec.append(save);
    return sec;
}
function drawDetail() {
    const box = $('detail'), e = current();
    box.replaceChildren();
    if (!e) {
        box.append(el('p', '请从左侧选择一条', 'empty'));
        return;
    }
    const head = el('div', '', 'dhead');
    head.append(el('h2', `${e.id} · ${e.question}`), badges(e));
    const range = e.line_start != null ? `原文第 ${e.line_start}-${e.line_end ?? e.line_start} 行` : e.line != null ? `原文第 ${e.line} 行` : '';
    head.append(el('p', [e.section, range, `版本 v${e.version ?? '?'}`, enhancedLabel(e)].filter(Boolean).join(' / '), 'muted'));
    box.append(head);
    if (isFabricated(e))
        box.append(el('div', '疑似虚构/跨条目拼接：禁止启用，只能暂缓或驳回。', 'alert'));
    if (isConflict(e))
        box.append(el('div', `事实冲突（跨条目）：禁止启用，单条与批量都会被后端拦截。冲突组：${[...new Set([...conflictGroupsOf(e)])].join('、')}`, 'alert'));
    else if (flagsOf(e).includes('pending_verify'))
        box.append(el('div', '含 [待核]：不得当作已确认事实，启用时须保留待核提醒。', 'alert warn'));
    if (e.attribution_uncertain)
        box.append(el('div', '说话人待核：不能当作确定的历史发问。', 'alert warn'));
    if (approvedWarning(e))
        box.append(el('div', `${approvedWarning(e)}：历史已启用数据不会被本页自动改回；如需修正请结束会话后编辑并重新审核。`, 'alert warn'));
    const area = document.createElement('textarea');
    area.id = 'review-answer';
    area.value = e.approved_answer || e.answer;
    area.setAttribute('aria-label', '审核答案');
    const orig = el('pre', e.original || '（无原文）', 'orig');
    orig.id = 'orig';
    const cmp = el('div', '', 'compare');
    cmp.append(fieldBlock('原文（只读）', orig), fieldBlock('审核答案（可编辑，不覆盖原文）', area));
    box.append(cmp);
    box.append(editorBlock(e));
    const chk = document.createElement('input');
    chk.type = 'checkbox';
    chk.id = 'review-confirm';
    const lab = el('label', '', 'check');
    lab.append(chk, el('span', '已核对事实并保留待核提醒'));
    const bar = el('div', '', 'actions');
    const act = (a, text, cls, key) => {
        const b = el('button', `${text}（${key}）`, 'btn ' + cls);
        b.setAttribute('type', 'button');
        b.dataset.action = a;
        b.addEventListener('click', () => submit(a));
        return b;
    };
    bar.append(act('approve', '启用', 'pill', 'a'), act('defer', '暂缓', '', 'h'), act('reject', '驳回', 'danger', 'r'));
    box.append(lab, bar);
}
function conflictGroupsOf(e) {
    return e.conflict_groups?.length ? e.conflict_groups : e.conflict_group ? [e.conflict_group] : [];
}
async function submit(a) {
    const e = current();
    if (!e)
        return;
    const answer = $('review-answer').value, confirmed = $('review-confirm').checked;
    if (a === 'approve') {
        const v = canApprove(e, confirmed, answer);
        if (!v.ok) {
            say(v.why, true);
            return;
        }
    }
    try {
        await api(reviewUrl(S.lib, e.id), reviewBody(a, answer, confirmed));
        const idx = view().findIndex((x) => x.id === e.id);
        await load(false);
        const rest = view();
        S.selected = rest[Math.min(Math.max(idx, 0), rest.length - 1)]?.id ?? S.selected;
        say(`${e.id} 已${a === 'approve' ? '启用' : a === 'defer' ? '暂缓' : '驳回'}`);
        draw();
    }
    catch (err) {
        say(String(err.message), true);
    }
}
async function submitBatch() {
    const { safe, blocked } = batchSelection(S.entries, S.checked, S.approvable);
    const rej = $('batch-rejects');
    rej.replaceChildren();
    for (const line of describeRejected(blocked))
        rej.append(el('li', line));
    if (!safe.length) {
        say('没有可批量启用的安全条目：冲突/待核/疑似虚构条目会被拦截', true);
        return;
    }
    try {
        const r = await api(batchUrl(S.lib), batchBody(safe, $('batch-confirm').checked));
        for (const id of safe)
            S.checked.delete(id);
        $('batch-confirm').checked = false;
        const n = Array.isArray(r.approved) ? r.approved.length : r.approved;
        await load(false);
        say(`已批量启用 ${n} 条`);
        draw();
    }
    catch (err) {
        const data = err.data;
        say(String(err.message), true);
        for (const line of describeRejected(data?.rejected))
            rej.append(el('li', line));
    }
}
function select(id) { S.selected = id; draw(); $('detail').scrollTop = 0; }
function draw() { drawProgress(); drawEnhance(); drawFilters(); drawList(); drawBatch(); drawDetail(); }
async function saveEdit() {
    const e = current();
    if (!e)
        return;
    const body = editBody($('edit-question').value.trim(), listValues('edit-variants'), { conclusion: $('edit-conclusion').value.trim(), points: listValues('edit-points'),
        examples: listValues('edit-examples'), cautions: listValues('edit-cautions') }, collectFacts());
    try {
        await api(editUrl(S.lib, e.id), body, 'PUT');
        S.checked.delete(e.id);
        say(`${e.id} 已保存，状态回到待审核`);
        await load(false);
        draw();
    }
    catch (err) {
        say(String(err.message), true);
    }
}
async function pollEnhance() {
    const token = Symbol('poll');
    pollIds.set(S.lib, token);
    const lib = S.lib;
    while (pollIds.get(lib) === token) {
        await new Promise((r) => setTimeout(r, 250));
        const p = await api(`/api/libraries/${encodeURIComponent(lib)}/enhance`).catch(() => null);
        if (!p || S.lib !== lib)
            return;
        S.enhance = p;
        drawEnhance();
        if (p.status !== 'running') {
            await load(false);
            draw();
            if (p.error)
                say(`增强不可用（${enhanceErrorLabel(p.error)}），已降级为规则拆解`, true);
            else
                say(`增强完成：${p.enhanced} 条，失败 ${p.failed} 条`);
            return;
        }
    }
}
const pollIds = new Map();
async function startEnhance() {
    if (!S.lib)
        return;
    try {
        S.enhance = await api(`${libBase()}/enhance`, {});
        drawEnhance();
        say('增强拆解已开始');
        await pollEnhance();
    }
    catch (err) {
        say(String(err.message), true);
    }
}
async function reimport() {
    const file = $('reimport-file').files?.[0];
    if (!file) {
        say('请先选择新版 .md 文件', true);
        return;
    }
    const text = await file.text();
    try {
        const r = await api(reimportUrl(S.lib), reimportBody(file.name, text));
        $('reimport-stats').textContent = statsText(r.stats);
        say(`重新导入完成：版本 v${r.version}`);
        S.checked.clear();
        await load(false);
        draw();
    }
    catch (err) {
        say(String(err.message), true);
    }
}
async function load(reset = true) {
    if (!S.lib) {
        S.entries = [];
        S.queue = [];
        S.approvable = new Set();
        S.enhance = idle();
        return;
    }
    const base = libBase();
    const [entries, queue, appr, enh] = await Promise.all([
        api(base + '/review-entries'),
        api(base + '/review-queue').catch(() => []),
        api(base + '/batch-approvable').catch(() => ({ ids: [] })),
        api(base + '/enhance').catch(() => idle()),
    ]);
    S.entries = entries;
    S.queue = queue;
    S.approvable = new Set(appr.ids || []);
    S.enhance = enh;
    if (reset || !current())
        S.selected = view()[0]?.id ?? '';
    for (const id of [...S.checked])
        if (S.entries.find((e) => e.id === id)?.status !== 'pending')
            S.checked.delete(id);
}
async function main() {
    S.token = (await api('/api/status')).token;
    const libs = await api('/api/libraries'), sel = $('library');
    for (const l of libs)
        sel.add(new Option(l.name, l.id));
    const change = async () => {
        S.lib = sel.value;
        S.checked.clear();
        $('reimport-stats').textContent = '';
        if (S.lib)
            await load();
        draw();
    };
    sel.addEventListener('change', change);
    $('search').addEventListener('input', () => draw());
    $('queue-on').addEventListener('change', () => draw());
    $('batch-confirm').addEventListener('change', drawBatch);
    $('batch-btn').addEventListener('click', submitBatch);
    $('select-safe').addEventListener('click', () => {
        for (const e of view())
            if (S.approvable.has(e.id))
                S.checked.add(e.id);
        drawList();
        drawBatch();
    });
    $('enhance-btn').addEventListener('click', () => void startEnhance());
    $('reimport-btn').addEventListener('click', () => void reimport());
    document.addEventListener('keydown', (ev) => {
        const k = keyAction(ev);
        if (!k)
            return;
        ev.preventDefault();
        if (k === 'next' || k === 'prev')
            select(step(view(), S.selected, k === 'next' ? 1 : -1));
        else
            void submit(k);
    });
    await change();
}
main().catch((e) => say(String(e.message ?? e), true));
