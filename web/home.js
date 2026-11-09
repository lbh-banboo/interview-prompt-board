import { capLamps } from './board/health.js';
import { credentialRows, configRows, navCards, progressView, sessionView, statusLamps, usageView } from './home/logic.js';
const $ = (id) => document.getElementById(id);
function el(tag, cls = '', text = '') {
    const e = document.createElement(tag);
    if (cls)
        e.className = cls;
    if (text)
        e.textContent = text;
    return e;
}
async function get(url) {
    try {
        const res = await fetch(url);
        return res.ok ? await res.json() : null;
    }
    catch {
        return null;
    }
}
const lamp = (cls, text) => el('li', `lamp ${cls}`, text);
let token = '';
async function mutate(url, method, body) {
    const res = await fetch(url, { method, headers: { 'Content-Type': 'application/json', 'X-Local-Token': token },
        body: body === undefined ? undefined : JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok)
        throw new Error(data.detail || '本地请求失败');
    return data;
}
function drawCreds(report) {
    $('creds').replaceChildren(...credentialRows(report).map((r) => {
        const card = el('div', `cred ${r.configured ? 'ok' : 'missing'}`);
        card.dataset.name = r.name;
        const head = el('div', 'cred-head');
        head.append(el('span', 'name', r.label), el('code', 'name-code', r.name), el('span', 'desc', r.desc), el('span', 'st', r.text));
        if (r.sourceLabel)
            head.append(el('span', 'src', `来源：${r.sourceLabel}`));
        card.append(head);
        if (r.mask)
            card.append(el('code', 'mask', r.mask));
        if (r.warning)
            card.append(el('p', 'warn-text', r.warning));
        const form = el('div', 'cred-form');
        const input = document.createElement('input');
        input.type = 'password';
        input.autocomplete = 'off';
        input.spellcheck = false;
        input.placeholder = r.configured ? '输入新 Key（保存后不回显）' : '粘贴 Key';
        input.setAttribute('aria-label', `${r.label} ${r.name}`);
        const save = el('button', 'save', '保存');
        const del = el('button', 'del', '删除');
        save.type = 'button';
        del.type = 'button';
        save.disabled = r.envLocked;
        del.disabled = r.envLocked;
        if (r.envLocked)
            save.title = del.title = r.warning;
        save.addEventListener('click', async () => {
            const value = input.value.trim();
            input.value = ''; // 明文只在本次请求的局部变量里，立刻清空
            if (!value) {
                $('cred-msg').textContent = '请输入 Key';
                return;
            }
            try {
                await mutate(`/api/credentials/${encodeURIComponent(r.name)}`, 'PUT', { value });
                $('cred-msg').textContent = `${r.label} 已保存（不回显）`;
                await refreshCreds();
            }
            catch (e) {
                $('cred-msg').textContent = `保存失败：${e.message}`;
            }
            finally {
                input.value = '';
            }
        });
        del.addEventListener('click', async () => {
            if (!window.confirm(`删除 ${r.name}？`))
                return;
            try {
                await mutate(`/api/credentials/${encodeURIComponent(r.name)}`, 'DELETE');
                $('cred-msg').textContent = `${r.label} 已删除`;
                await refreshCreds();
            }
            catch (e) {
                $('cred-msg').textContent = `删除失败：${e.message}`;
            }
        });
        form.append(input, save, del);
        card.append(form);
        return card;
    }));
}
async function refreshCreds() {
    drawCreds(await get('/api/credentials/status'));
}
async function main() {
    const status = await get('/api/status');
    token = typeof status?.token === 'string' ? status.token : '';
    const session = sessionView(status);
    $('session').textContent = session.label;
    $('session').dataset.active = String(session.active);
    $('cards').replaceChildren(...navCards(session).map((c) => {
        const a = el(c.disabled ? 'div' : 'a', 'card' + (c.disabled ? ' disabled' : ''));
        a.dataset.key = c.key;
        a.append(el('h2', '', c.title), el('p', '', c.desc));
        if (!c.disabled)
            a.href = c.href;
        else
            a.setAttribute('aria-disabled', 'true');
        return a;
    }));
    $('lamps').replaceChildren(...statusLamps(status).map((l) => lamp(l.cls, `${l.label} ${l.text}`)));
    if (session.active) {
        const snap = await get(`/api/sessions/${encodeURIComponent(session.id)}/snapshot`);
        const caps = capLamps(snap?.health ?? {});
        $('lamps').replaceChildren(...caps.map((c) => { const e = lamp(c.cls, c.text ? `${c.label} ${c.text}` : c.label); if (c.reason)
            e.title = c.reason; return e; }));
        $('lamps').dataset.source = 'session';
    }
    const libs = await get('/api/libraries');
    const sel = $('library');
    const list = Array.isArray(libs) ? libs : [];
    sel.replaceChildren(...list.map((l) => { const o = el('option', '', l.name || l.id); o.value = l.id; return o; }));
    const drawProgress = async () => {
        const p = progressView(sel.value ? await get(`/api/libraries/${encodeURIComponent(sel.value)}/progress`) : null);
        $('prog-label').textContent = p ? p.label : '暂无主库审核数据';
        $('prog-fill').style.setProperty('width', (p?.pct ?? 0) + '%');
        $('prog').classList.toggle('ready', !!p?.g3Ready);
        $('g3').hidden = !p?.g3Ready;
    };
    sel.addEventListener('change', () => void drawProgress());
    await drawProgress();
    const [cfg, cred, usage] = await Promise.all([get('/api/config'), get('/api/credentials/status'), get('/api/usage')]);
    $('config').replaceChildren(...(cfg ? configRows(cfg) : ['配置暂不可读']).map((t) => el('li', '', t)));
    drawCreds(cred);
    const u = usage ? usageView(usage) : null;
    $('usage').replaceChildren(...(u ? [u.total, ...u.rows.map((r) => r.text), u.note] : ['用量暂不可读']).map((t) => el('li', '', t)));
}
void main();
