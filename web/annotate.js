import { FILTERS, audioUrl, clipWindow, fmtTime, guessConfidence, guessLabel, isJudged, itemUrl, judgedLabel, keyAction, nextTodoId, noteOf, progress, stepId, verdictBody, visibleItems, } from './annotate/logic.js';
const $ = (id) => document.getElementById(id);
function el(tag, text = '', cls = '') {
    const e = document.createElement(tag);
    if (text)
        e.textContent = text;
    if (cls)
        e.className = cls;
    return e;
}
const S = { token: '', audioId: '', items: [], audios: [], filter: 'todo', selected: 0 };
let stopAt = 0; // 自动播放窗口的结束秒；0 表示不自动停
const say = (t, bad = false) => { const m = $('msg'); m.textContent = t; m.classList.toggle('bad', bad); };
async function api(url, method = 'GET', body) {
    const r = await fetch(url, body === undefined ? { method } : {
        method, headers: { 'Content-Type': 'application/json', 'X-Local-Token': S.token }, body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok)
        throw Object.assign(new Error(data.detail || '本地请求失败'), { data });
    return data;
}
const current = () => S.items.find((it) => it.id === S.selected);
function drawProgress() {
    const p = progress(S.items);
    $('prog-label').textContent = p.label;
    $('prog-fill').style.width = p.pct + '%';
    $('prog').setAttribute('aria-valuenow', String(p.judged));
    $('prog').setAttribute('aria-valuemax', String(p.total));
    $('prog').classList.toggle('ready', p.total > 0 && p.judged === p.total);
    const opt = $('audio').selectedOptions[0];
    if (opt)
        opt.textContent = `${S.audioId} · ${p.judged}/${p.total}`;
}
function drawFilters() {
    const box = $('filters');
    box.replaceChildren();
    for (const f of FILTERS) {
        const n = visibleItems(S.items, f.key).length;
        const b = el('button', `${f.label} ${n}`, 'chip' + (f.key === S.filter ? ' on' : ''));
        b.setAttribute('type', 'button');
        b.setAttribute('aria-pressed', String(f.key === S.filter));
        b.addEventListener('click', () => { S.filter = f.key; draw(); });
        box.append(b);
    }
}
function drawList() {
    const ul = $('list');
    ul.replaceChildren();
    const rows = visibleItems(S.items, S.filter);
    for (const it of rows) {
        const li = el('li', '', 'row' + (it.id === S.selected ? ' sel' : ''));
        li.dataset.id = String(it.id);
        const b = el('button', '', 'open');
        b.setAttribute('type', 'button');
        b.setAttribute('aria-label', `${fmtTime(it.t_end_ms)} ${it.text_final}；${judgedLabel(it)}`);
        if (it.id === S.selected)
            b.setAttribute('aria-current', 'true');
        b.append(el('span', it.text_final, 'q'), el('span', `${fmtTime(it.t_end_ms)} · ${judgedLabel(it)}`, 't'));
        b.addEventListener('click', () => select(it.id));
        li.append(b);
        ul.append(li);
    }
    if (!rows.length)
        ul.append(el('li', '没有符合条件的条目', 'empty'));
}
function drawDetail() {
    const box = $('detail');
    box.replaceChildren();
    const it = current();
    if (!it) {
        box.append(el('p', '请从左侧选择一条', 'empty'));
        return;
    }
    const head = el('div', '', 'dhead');
    head.append(el('h2', `${fmtTime(it.t_end_ms)} · 第 ${it.id} 条`), el('p', it.text_final, 'stmt'));
    const idx = S.items.findIndex((x) => x.id === it.id) + 1;
    box.append(head, el('p', `猜测：${guessLabel(it)} · 置信 ${guessConfidence(it)} · 第 ${idx}/${S.items.length} 条 · 状态：${judgedLabel(it)}`, 'muted'));
    const bar = el('div', '', 'verdicts');
    const act = (v, label, key, cls = '') => {
        const b = el('button', `${label}（${key}）`, 'btn ' + cls);
        b.setAttribute('type', 'button');
        b.setAttribute('aria-keyshortcuts', key);
        b.dataset.verdict = v;
        b.addEventListener('click', () => { if (v === 'skip')
            skip();
        else
            void judge(v); });
        return b;
    };
    bar.append(act('yes', '是问题结束点', 'y', 'primary'), act('no', '不是', 'n'), act('followup', '追问', 'f'), act('topic_change', '改题', 't'), act('skip', '跳过', 's', 'danger'));
    const nav = el('div', '', 'actions');
    const prev = el('button', '上一条（k）', 'btn');
    prev.setAttribute('type', 'button');
    prev.addEventListener('click', () => move(-1));
    const next = el('button', '下一条（j）', 'btn');
    next.setAttribute('type', 'button');
    next.addEventListener('click', () => move(1));
    const replay = el('button', '重播（r）', 'btn');
    replay.setAttribute('type', 'button');
    replay.addEventListener('click', playClip);
    nav.append(prev, next, replay);
    const note = document.createElement('textarea');
    note.id = 'note';
    note.value = noteOf(it);
    note.placeholder = '备注（可选，随判定一起保存）';
    note.setAttribute('aria-label', '备注');
    box.append(bar, nav, note);
}
function draw() {
    drawProgress();
    drawFilters();
    drawList();
    drawDetail();
}
function playClip() {
    const it = current();
    if (!it)
        return;
    const a = $('player');
    const w = clipWindow(it.t_end_ms);
    stopAt = w.end;
    const go = () => {
        try {
            a.currentTime = w.start;
        }
        catch { /* 元数据未就绪时由 loadedmetadata 重试 */
            return;
        }
        a.play().catch(() => say('浏览器拦截了自动播放：点播放器或按 r 重播'));
    };
    if (a.readyState >= 1)
        go();
    else
        a.addEventListener('loadedmetadata', go, { once: true });
}
function select(id) {
    if (!S.items.some((it) => it.id === id))
        return;
    S.selected = id;
    draw();
    playClip();
}
function move(dir) {
    const rows = visibleItems(S.items, S.filter);
    const next = stepId(rows, S.selected, dir);
    if (next)
        select(next);
}
function skip() {
    const rows = visibleItems(S.items, S.filter);
    const next = stepId(rows, S.selected, 1);
    if (next && next !== S.selected)
        select(next);
    else
        say('已是最后一条');
}
async function judge(v) {
    const it = current();
    if (!it)
        return;
    const note = $('note').value;
    try {
        const r = await api(itemUrl(S.audioId, it.id), 'PUT', verdictBody(v, note));
        const i = S.items.findIndex((x) => x.id === it.id);
        if (i >= 0)
            S.items[i] = r.item;
        say(`${fmtTime(r.item.t_end_ms)} 已判：${judgedLabel(r.item)}`);
        const nxt = nextTodoId(S.items, it.id);
        if (nxt)
            select(nxt);
        else
            draw();
    }
    catch (err) {
        say(String(err.message), true);
    }
}
async function loadAudios() {
    S.audios = await api('/api/annotations');
    const sel = $('audio');
    sel.replaceChildren(...S.audios.map((a) => {
        const o = document.createElement('option');
        o.value = a.audio_id;
        o.textContent = `${a.audio_id} · ${a.judged}/${a.total}`;
        return o;
    }));
    if (!S.audios.length) {
        say('没有找到 .review.json 标注文件', true);
        return;
    }
    await openAudio(S.audios[0].audio_id);
}
async function openAudio(id) {
    S.audioId = id;
    const doc = await api(`/api/annotations/${encodeURIComponent(id)}`);
    S.items = doc.items;
    const firstTodo = S.items.find((it) => !isJudged(it)) ?? S.items[0];
    S.selected = firstTodo?.id ?? 0;
    const a = $('player');
    a.src = audioUrl(id);
    a.playbackRate = Number($('speed').value);
    $('audio').value = id;
    draw();
    playClip();
}
async function main() {
    S.token = (await api('/api/status')).token;
    $('audio').addEventListener('change', () => { openAudio($('audio').value).catch((e) => say(String(e.message), true)); });
    $('speed').addEventListener('change', () => { $('player').playbackRate = Number($('speed').value); });
    $('player').addEventListener('timeupdate', () => {
        const a = $('player');
        if (stopAt && a.currentTime >= stopAt) {
            a.pause();
            stopAt = 0;
        }
    });
    document.addEventListener('keydown', (ev) => {
        const k = keyAction(ev);
        if (!k)
            return;
        ev.preventDefault();
        if (k === 'next')
            move(1);
        else if (k === 'prev')
            move(-1);
        else if (k === 'skip')
            skip();
        else if (k === 'replay')
            playClip();
        else
            void judge(k);
    });
    await loadAudios();
    draw();
}
main().catch((e) => say(String(e.message ?? e), true));
