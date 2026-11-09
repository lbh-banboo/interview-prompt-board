import {
  FORMAT_EXAMPLE, FORMAT_RULES, byteLength, importedText, libraryRowText, secondaryImportText,
  secondaryRolesText, secondarySummary, validateImport,
} from './import/logic.js';

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
let token = '';

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls = '', text = ''): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
}
function say(id: string, text: string, bad = false): void {
  const m = $(id);
  m.textContent = text;
  m.classList.toggle('bad', bad);
}

async function api(url: string, method = 'GET', body?: unknown): Promise<any> {
  const res = await fetch(url, method === 'GET' ? {} : {
    method, headers: { 'Content-Type': 'application/json', 'X-Local-Token': token },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || '本地请求失败');
  return data;
}

async function loadLibraries(): Promise<void> {
  const list = await api('/api/libraries');
  const rows = Array.isArray(list) ? list : [];
  const ul = $('libs');
  ul.replaceChildren(...rows.map((l: any) => el('li', '', libraryRowText(l))));
  $('libs-empty').hidden = rows.length > 0;
}

async function loadSecondary(): Promise<void> {
  const [progress, roles] = await Promise.all([api('/api/secondary/progress'), api('/api/secondary/roles')]);
  $('secondary-summary').textContent = secondarySummary(progress);
  $('secondary-roles').textContent = secondaryRolesText(roles);
}

async function submitImport(): Promise<void> {
  const name = ($('name') as HTMLInputElement).value.trim();
  const text = ($('text') as HTMLTextAreaElement).value;
  const error = validateImport(name, byteLength(text), text);
  if (error) return say('msg', error, true);
  say('msg', '导入中…');
  try {
    const result = await api('/api/import', 'POST', { name, text });
    const box = $('result');
    box.replaceChildren(el('span', '', importedText(result.count, result.reused)));
    const go = el('a', 'go', '去审核');
    go.setAttribute('href', '/review.html');
    box.append(' ', go);
    box.hidden = false;
    say('msg', '');
    ($<HTMLInputElement>('file')).value = '';
    ($<HTMLTextAreaElement>('text')).value = '';
    await loadLibraries();
  } catch (e) {
    say('msg', (e as Error).message, true);
  }
}

async function chooseFile(): Promise<void> {
  const input = $<HTMLInputElement>('file');
  const file = input.files?.[0];
  if (!file) return;
  if (file.size > 5_000_000) {
    input.value = '';
    return say('msg', '资料超过 5MB，请拆分后导入', true);
  }
  const text = await file.text();
  ($<HTMLTextAreaElement>('text')).value = text;
  const name = $<HTMLInputElement>('name');
  if (!name.value.trim()) name.value = file.name.replace(/\.md$/i, '');
  say('msg', `已读取 ${file.name}（${(file.size / 1024).toFixed(0)} KB），确认后点“导入资料”`);
}

async function importSecondary(): Promise<void> {
  say('secondary-msg', '导入中…');
  try {
    const result = await api('/api/secondary/import', 'POST');
    say('secondary-msg', secondaryImportText(result));
    await loadSecondary();
  } catch (e) {
    say('secondary-msg', (e as Error).message, true);
  }
}

async function main(): Promise<void> {
  const status = await api('/api/status');
  token = typeof status?.token === 'string' ? status.token : '';
  $('format-rules').replaceChildren(...FORMAT_RULES.map((r) => el('li', '', r)));
  $('format-example').textContent = FORMAT_EXAMPLE;
  $('submit').addEventListener('click', () => void submitImport());
  $('file').addEventListener('change', () => void chooseFile());
  $('secondary-import').addEventListener('click', () => void importSecondary());
  await Promise.all([loadLibraries(), loadSecondary()]);
}

main().catch((e) => say('msg', (e as Error).message, true));
