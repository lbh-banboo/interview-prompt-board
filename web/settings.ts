import {
  ACTIVE_WARNING, ANSWERER_ITEM, ASR_OPTIONS, DEVELOPER_NOTE, FIELDS, GROUPS, OUTBOUND_ITEMS,
  buildPatch, defaultHint, effectiveNotice, fieldText, outboundLine, outboundPatch, pathGet,
  profilePatch, profileRows, renameError, thresholdSource,
} from './settings/logic.js';
import { ProfileEditor } from './profile/editor.js';

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls = '', text = ''): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
}
const S = {
  token: '', config: {} as any, defaults: {} as any, active: false, profiles: [] as any[],
  editor: null as ProfileEditor | null, editorPid: '',
};

function say(id: string, text: string, bad = false): void {
  const node = document.getElementById(id);
  if (!node) return;
  node.textContent = text;
  node.classList.toggle('bad', bad);
}

async function api(url: string, method = 'GET', body?: unknown): Promise<any> {
  const res = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Local-Token': S.token },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || '本地请求失败');
  return data;
}

function renderActive(): void {
  const note = $('active-note');
  note.hidden = !S.active;
  note.textContent = S.active ? ACTIVE_WARNING : '';
}

function renderOutbound(): void {
  const box = $('outbound');
  box.replaceChildren(...[...OUTBOUND_ITEMS, ANSWERER_ITEM].map((item) => {
    const row = el('div', 'switch-row');
    const sw = el('label', 'switch');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = S.config?.outbound?.[item.key] !== false;
    input.dataset.switchKey = item.key;
    input.setAttribute('aria-label', item.title);
    input.disabled = false;
    const track = el('span', 'track');
    sw.append(input, track);
    const text = el('div', 'switch-text');
    text.append(el('strong', '', item.title), el('span', '', item.text));
    const state = el('span', 'switch-state' + (input.checked ? '' : ' off'), input.checked ? '开' : '关');
    input.addEventListener('change', () => {
      const on = input.checked;
      void saveOutbound(item.key, on, input);
    });
    row.append(sw, text, state);
    return row;
  }));
  $('outbound-msg').textContent = '';
}

async function saveOutbound(key: string, on: boolean, input: HTMLInputElement): Promise<void> {
  input.disabled = true;
  try {
    const r = await api('/api/config', 'PUT', outboundPatch(key as any, on));
    S.config = r.config;
    S.active = !!r.active_session;
    renderOutbound();
    renderActive();
    say('outbound-msg', `已保存：${on ? '开' : '关'}。${effectiveNotice(S.active)}`);
  } catch (e) {
    input.checked = !on;
    input.disabled = false;
    say('outbound-msg', `保存失败：${(e as Error).message}`, true);
  }
}

function renderAsr(): void {
  const sel = $('asr-backend') as HTMLSelectElement;
  sel.replaceChildren(...ASR_OPTIONS.map((o) => {
    const opt = document.createElement('option');
    opt.value = o.value;
    opt.textContent = o.label;
    opt.disabled = o.disabled;
    return opt;
  }));
  sel.value = S.config?.asr?.backend ?? 'auto';
  const current = ASR_OPTIONS.find((o) => o.value === sel.value);
  $('asr-hint').textContent = current?.hint ?? '';
  $('asr-msg').textContent = '';
  sel.onchange = async () => {
    const next = sel.value;
    try {
      const r = await api('/api/config', 'PUT', { asr: { backend: next } });
      S.config = r.config;
      S.active = !!r.active_session;
      renderAsr();
      renderActive();
      say('asr-msg', `已保存转写后端：${ASR_OPTIONS.find((o) => o.value === next)?.label ?? next}。${effectiveNotice(S.active)}`);
    } catch (e) {
      sel.value = S.config?.asr?.backend ?? 'auto';
      say('asr-msg', `保存失败：${(e as Error).message}`, true);
    }
  };
}

function renderParams(): void {
  const box = $('params');
  box.replaceChildren();
  for (const group of GROUPS) {
    const sec = el('div', 'group');
    sec.append(el('h3', '', group.title), el('p', 'hint', group.note));
    if (group.key === 'retrieval_thresholds') {
      sec.append(el('p', 'hint threshold-source', `${thresholdSource(S.config)}；修改下一场会话生效。`));
    }
    for (const spec of group.fields) {
      const line = el('div', 'row-line');
      const field = el('label', 'field');
      field.setAttribute('for', `f-${spec.key.replace(/\./g, '-')}`);
      field.append(el('span', 'label', spec.label));
      const input = document.createElement('input');
      input.className = 'input';
      input.id = `f-${spec.key.replace(/\./g, '-')}`;
      input.dataset.key = spec.key;
      input.setAttribute('aria-label', spec.label);
      if (spec.kind === 'bool') {
        input.type = 'checkbox';
        input.checked = pathGet(S.config, spec.key) === true;
      } else if (spec.kind === 'str') {
        input.type = 'text';
      } else {
        input.type = 'number';
        if (spec.min !== undefined) input.min = String(spec.min);
        if (spec.max !== undefined) input.max = String(spec.max);
        input.step = String(spec.step ?? (spec.kind === 'int' ? 1 : 0.01));
      }
      input.value = fieldText(spec, pathGet(S.config, spec.key));
      const hint = el('span', 'hint', `${spec.hint} ${defaultHint(spec, S.defaults)}`);
      field.append(input, hint);
      const def = el('button', 'default', '默认');
      def.type = 'button';
      def.setAttribute('aria-label', `${spec.label} 恢复默认`);
      def.addEventListener('click', () => {
        if (spec.kind === 'bool') input.checked = pathGet(S.defaults, spec.key) === true;
        else input.value = fieldText(spec, pathGet(S.defaults, spec.key));
        input.focus();
      });
      line.append(field, def);
      sec.append(line);
    }
    box.append(sec);
  }
}

function renderErrors(errors: string[]): void {
  $('errors').replaceChildren(...errors.map((t) => el('li', '', t)));
}

async function saveParams(): Promise<void> {
  const raw: Record<string, string> = {};
  document.querySelectorAll<HTMLInputElement>('input[data-key]').forEach((input) => {
    raw[input.dataset.key as string] = input.type === 'checkbox' ? String(input.checked) : input.value;
  });
  const { patch, errors, changed } = buildPatch(FIELDS, S.config, raw);
  if (errors.length) {
    renderErrors(errors);
    say('save-msg', '未保存：请先修正下方错误', true);
    return;
  }
  if (!changed) {
    renderErrors([]);
    say('save-msg', '没有改动');
    return;
  }
  try {
    const r = await api('/api/config', 'PUT', patch);
    S.config = r.config;
    S.active = !!r.active_session;
    renderParams();
    renderOutbound();
    renderAsr();
    renderActive();
    renderErrors([]);
    say('save-msg', effectiveNotice(S.active));
  } catch (e) {
    const detail = (e as Error).message;
    renderErrors(detail.split('；'));
    say('save-msg', '保存失败：请修正下方错误', true);
  }
}

async function loadProfiles(): Promise<void> {
  S.profiles = await api('/api/profiles');
  renderProfiles();
}

function renderProfiles(): void {
  const list = $('profiles');
  list.replaceChildren(...profileRows(S.profiles).map((p) => {
    const li = el('li', 'profile');
    li.dataset.pid = p.id;
    const field = el('label', 'field');
    field.append(el('span', 'label', '资料代号'));
    const input = document.createElement('input');
    input.className = 'input';
    input.value = p.alias;
    input.dataset.pid = p.id;
    input.setAttribute('aria-label', `资料代号 ${p.alias}`);
    field.append(input);
    const save = el('button', 'btn small', '保存');
    save.type = 'button';
    save.dataset.action = 'save-profile';
    save.dataset.pid = p.id;
    save.addEventListener('click', () => { void renameProfile(p, input); });
    const del = el('button', 'btn small danger', '删除');
    del.type = 'button';
    del.dataset.action = 'delete-profile';
    del.dataset.pid = p.id;
    del.addEventListener('click', () => { void deleteProfile(p); });
    const edit = el('button', 'btn small', '编辑资料');
    edit.type = 'button';
    edit.dataset.action = 'edit-profile';
    edit.dataset.pid = p.id;
    edit.addEventListener('click', () => { void openProfileEditor(p); });
    li.append(field, save, del, edit);
    li.append(el('span', 'meta', `${p.rolesText || '未设岗位'} · ${p.librariesText} · ${p.id.slice(0, 8)}`));
    return li;
  }));
  if (S.editorPid && S.editor) {
    const host = list.querySelector(`li[data-pid="${S.editorPid}"]`);
    if (host) host.append(S.editor.el);
  }
}

async function openProfileEditor(p: any): Promise<void> {
  if (!S.editor) {
    S.editor = new ProfileEditor({
      token: S.token,
      onSaved: async (_profile, message) => {
        S.editorPid = '';
        await loadProfiles();
        say('profile-msg', message);
      },
      onCancel: () => { S.editorPid = ''; renderProfiles(); },
    });
  }
  S.editorPid = p.id;
  renderProfiles();
  await S.editor.open(p);
}

async function renameProfile(p: any, input: HTMLInputElement): Promise<void> {
  const err = renameError(input.value);
  if (err) { say('profile-msg', err, true); return; }
  try {
    await api(`/api/profiles/${encodeURIComponent(p.id)}`, 'PUT', profilePatch(p, input.value));
    await loadProfiles();
    say('profile-msg', `已重命名为「${input.value.trim()}」`);
  } catch (e) { say('profile-msg', `重命名失败：${(e as Error).message}`, true); }
}

async function deleteProfile(p: any): Promise<void> {
  if (!window.confirm(`删除资料「${p.alias}」？关联的主库关系一并删除，不影响已有会话记录。`)) return;
  try {
    await api(`/api/profiles/${encodeURIComponent(p.id)}`, 'DELETE');
    await loadProfiles();
    say('profile-msg', `已删除资料「${p.alias}」`);
  } catch (e) { say('profile-msg', `删除失败：${(e as Error).message}`, true); }
}

async function main(): Promise<void> {
  $('dev-note').textContent = DEVELOPER_NOTE;
  const status = await fetch('/api/status').then((r) => r.json()).catch(() => null);
  S.token = typeof status?.token === 'string' ? status.token : '';
  S.active = !!status?.active_session;
  const [cfg, profiles] = await Promise.all([
    fetch('/api/config').then((r) => (r.ok ? r.json() : null)).catch(() => null),
    fetch('/api/profiles').then((r) => (r.ok ? r.json() : [])).catch(() => []),
  ]);
  if (!cfg) {
    $('msg').textContent = '配置暂不可读，请刷新重试。';
    $('msg').classList.add('bad');
    return;
  }
  S.config = cfg.config ?? {};
  S.defaults = cfg.defaults ?? {};
  S.profiles = Array.isArray(profiles) ? profiles : [];
  if (Array.isArray(cfg.errors) && cfg.errors.length) {
    $('msg').textContent = `配置文件有 ${cfg.errors.length} 项问题（已回落默认值）：${cfg.errors.join('；')}`;
    $('msg').classList.add('bad');
  }
  // 便于联调时核对当前外发文案（与会话页同一函数）
  $('outbound').setAttribute('data-line', outboundLine(S.config.outbound));
  renderActive();
  renderOutbound();
  renderAsr();
  renderParams();
  renderProfiles();
  $('save').addEventListener('click', () => { void saveParams(); });
}

void main().catch((e) => {
  $('msg').textContent = `设置页加载失败：${(e as Error).message}`;
  $('msg').classList.add('bad');
});
