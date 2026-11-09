// 资料编辑面板：向导第 2 步与设置页复用同一套字段与请求逻辑。
import {
  CONTENT_HINT, MAX_PROFILE_BYTES, PRIVACY_NOTE, ProfileLike, LibraryOption,
  contentError, libraryChoice, linkLibraries, profileSaveBody, statsText, updateResultText,
  CONTENT_FORMAT_HELP, CONTENT_TEMPLATE, contentErrorHint,
} from './logic.js';

export interface ProfileEditorHost {
  token: string;
  onSaved: (profile: any, message: string) => void;
  onCancel?: () => void;
}

interface EditorOptions { roles: string[]; libraries: LibraryOption[] }

async function api(token: string, url: string, method = 'GET', body?: unknown): Promise<any> {
  const r = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Local-Token': token },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || '本地请求失败');
  return data;
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls = '', text = ''): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
}

export class ProfileEditor {
  readonly el: HTMLElement;
  private profile: ProfileLike | null = null;
  private options: EditorOptions = { roles: [], libraries: [] };
  private draft = { alias: '', roles: [] as string[], note: '', content: '', libraryId: '' };
  private originalContent = '';
  private loaded = false;
  private busy = false;
  private msg = '';
  private msgBad = false;

  constructor(private host: ProfileEditorHost) {
    this.el = el('div', 'profile-editor');
    this.el.id = 'profile-editor';
  }

  async open(profile?: ProfileLike | null): Promise<void> {
    this.profile = profile ?? null;
    this.msg = '';
    this.msgBad = false;
    this.loaded = false;
    this.render();
    try {
      const [opts, detail] = await Promise.all([
        api(this.host.token, '/api/setup/options'),
        profile ? api(this.host.token, `/api/profiles/${encodeURIComponent(profile.id)}`).catch(() => profile) : Promise.resolve(null),
      ]);
      this.options = { roles: Array.isArray(opts?.roles) ? opts.roles : [], libraries: Array.isArray(opts?.libraries) ? opts.libraries : [] };
      const p: ProfileLike | null = detail ?? profile ?? null;
      this.profile = p;
      this.originalContent = p?.content ?? '';
      this.draft = {
        alias: p?.alias ?? '', roles: [...(p?.roles ?? [])], note: p?.note ?? '',
        content: p?.content ?? '', libraryId: libraryChoice(p),
      };
      this.loaded = true;
    } catch (e) {
      this.msg = (e as Error).message;
      this.msgBad = true;
    }
    this.render();
  }

  private setMsg(text: string, bad = false): void {
    this.msg = text;
    this.msgBad = bad;
    this.render();
  }

  private render(): void {
    this.el.replaceChildren();
    if (!this.loaded) {
      this.el.append(el('p', 'muted', this.msg || '加载资料…'));
      return;
    }
    const d = this.draft;
    const editing = !!this.profile;

    const alias = el('input', 'field');
    alias.id = 'pe-alias';
    alias.placeholder = '如：张三 / 代号甲';
    alias.value = d.alias;
    alias.setAttribute('aria-label', '姓名代号');
    alias.addEventListener('input', () => { d.alias = alias.value; });
    this.el.append(this.field('姓名代号', alias));

    const chips = el('div', 'chips');
    for (const r of this.options.roles) {
      const on = d.roles.includes(r);
      const b = el('button', 'chip' + (on ? ' on' : ''), r);
      b.type = 'button';
      b.setAttribute('aria-pressed', String(on));
      b.addEventListener('click', () => {
        d.roles = on ? d.roles.filter((x) => x !== r) : [...d.roles, r];
        this.render();
      });
      chips.append(b);
    }
    this.el.append(this.field('目标岗位', chips));

    const note = el('input', 'field');
    note.id = 'pe-note';
    note.placeholder = '备注（可选）';
    note.value = d.note;
    note.setAttribute('aria-label', '备注');
    note.addEventListener('input', () => { d.note = note.value; });
    this.el.append(this.field('备注', note));

    const file = el('input', 'field');
    file.id = 'pe-file';
    file.type = 'file';
    file.accept = '.md,.markdown,.txt,text/plain,text/markdown';
    file.addEventListener('change', () => { void this.loadFile(file); });
    let association: HTMLSelectElement | null = null;
    const text = el('textarea', 'field pe-content');
    text.id = 'pe-content';
    text.rows = 6;
    text.placeholder = '粘贴资料内容（格式见导入页），保存后会解析成待审核条目';
    text.value = d.content;
    text.addEventListener('input', () => {
      d.content = text.value;
      if (association) association.disabled = !!d.content.trim();
    });
    const hint = el('p', 'pe-hint', CONTENT_HINT + ' ');
    const fmt = el('a', 'pe-link', '格式说明');
    fmt.href = '/import.html';
    hint.append(fmt);
    const help = el('p', 'pe-hint', CONTENT_FORMAT_HELP);
    help.id = 'pe-format-help';
    const tpl = el('button', 'btn small', '插入模板');
    tpl.id = 'pe-template';
    tpl.setAttribute('type', 'button');
    tpl.addEventListener('click', () => {
      text.value = text.value.trim() ? `${text.value.trimEnd()}\n${CONTENT_TEMPLATE}` : CONTENT_TEMPLATE;
      text.dispatchEvent(new Event('input'));
    });
    const stack = el('div', 'pe-stack');
    stack.append(file, text, tpl, help, hint);
    this.el.append(this.field('个人资料内容（.md/文本）', stack));

    const select = el('select', 'field');
    select.id = 'pe-library';
    select.append(new Option('不关联', ''));
    if (this.profile?.content_library_id) select.append(new Option('用资料内容生成/更新答案库', '__content__'));
    for (const lib of this.options.libraries) {
      select.append(new Option(`${lib.name}（启用 ${lib.approved} / 共 ${lib.total} 条）`, lib.id));
    }
    select.value = d.libraryId;
    select.disabled = !!d.content.trim();
    select.addEventListener('change', () => { d.libraryId = select.value; });
    association = select;
    this.el.append(this.field('关联答案库（单选）', select));

    if (editing && this.profile) {
      const stats = el('p', 'pe-status');
      stats.id = 'pe-status';
      stats.textContent = `本资料答案库：${statsText(this.profile.library_stats)}`;
      if (this.profile.library_stats && this.profile.library_stats.approved < this.profile.library_stats.total) {
        const go = el('a', 'pe-link', '去题库审核');
        go.id = 'pe-review';
        go.href = '/review.html';
        stats.append(' ', go);
      }
      this.el.append(stats);
    }

    this.el.append(el('p', 'pe-privacy', PRIVACY_NOTE));

    const actions = el('div', 'pe-actions');
    const save = el('button', 'btn pill', editing ? '保存资料' : '新建资料');
    save.id = 'pe-save';
    save.type = 'button';
    save.disabled = this.busy;
    save.addEventListener('click', () => { void this.save(); });
    actions.append(save);
    if (editing) {
      const sync = el('button', 'btn', this.busy ? '同步中…' : '更新内容并同步答案库');
      sync.id = 'pe-sync';
      sync.type = 'button';
      sync.disabled = this.busy || !d.content.trim();
      sync.addEventListener('click', () => { void this.sync(); });
      actions.append(sync);
    }
    const cancel = el('button', 'btn', '取消');
    cancel.id = 'pe-cancel';
    cancel.type = 'button';
    cancel.addEventListener('click', () => { this.host.onCancel ? this.host.onCancel() : this.el.replaceChildren(); });
    actions.append(cancel);
    this.el.append(actions);

    const msg = el('p', 'pe-msg' + (this.msgBad ? ' bad' : ''), this.msg);
    msg.id = 'pe-msg';
    msg.setAttribute('role', 'status');
    this.el.append(msg);
  }

  private field(label: string, control: HTMLElement): HTMLElement {
    const wrap = el('label', 'pe-field');
    wrap.append(el('span', 'pe-label', label), control);
    return wrap;
  }

  private async loadFile(input: HTMLInputElement): Promise<void> {
    const file = input.files?.[0];
    if (!file) return;
    if (file.size > MAX_PROFILE_BYTES) {
      input.value = '';
      this.setMsg('资料超过 5MB，请拆分后导入', true);
      return;
    }
    this.draft.content = await file.text();
    this.setMsg(`已读取 ${file.name}（${(file.size / 1024).toFixed(0)} KB），保存后解析`);
  }

  private async save(): Promise<void> {
    if (this.busy) return;
    const body = profileSaveBody(this.draft);
    if (!body.alias) return this.setMsg('资料代号不能为空', true);
    if (this.draft.content.trim()) {
      const err = contentError(this.draft.content);
      if (err) return this.setMsg(err, true);
    }
    this.busy = true;
    this.render();
    try {
      let p: any;
      let message: string;
      if (this.profile) {
        p = await api(this.host.token, `/api/profiles/${encodeURIComponent(this.profile.id)}`, 'PUT', {
          alias: body.alias, roles: body.roles, note: body.note,
          library_ids: linkLibraries(this.draft.libraryId, this.profile),
        });
        message = '已保存资料';
        if (this.draft.content.trim() && this.draft.content !== this.originalContent) {
          message += '；内容有修改，请点“更新内容并同步答案库”';
        }
      } else {
        p = await api(this.host.token, '/api/profiles', 'POST', body);
        message = p?.library_stats
          ? `已生成答案库：${statsText(p.library_stats)}；新条目待审核，未自动启用`
          : '已新建资料';
      }
      this.busy = false;
      this.profile = p;
      this.msg = message;
      this.msgBad = false;
      this.host.onSaved(p, message);
      this.render();
    } catch (e) {
      this.busy = false;
      this.setMsg(`保存失败：${contentErrorHint((e as Error).message)}`, true);
    }
  }

  private async sync(): Promise<void> {
    if (this.busy || !this.profile) return;
    const err = contentError(this.draft.content);
    if (err) return this.setMsg(err, true);
    this.busy = true;
    this.render();
    try {
      const r = await api(this.host.token, `/api/profiles/${encodeURIComponent(this.profile.id)}/content`, 'POST', {
        text: this.draft.content,
      });
      this.busy = false;
      this.profile = r.profile;
      this.originalContent = this.draft.content;
      this.draft.libraryId = libraryChoice(r.profile);
      const message = updateResultText(r.stats) + (r.reused ? '（内容未变化）' : '');
      this.msg = message;
      this.msgBad = false;
      this.host.onSaved(r.profile, message);
      this.render();
    } catch (e) {
      this.busy = false;
      this.setMsg(`更新失败：${contentErrorHint((e as Error).message)}`, true);
    }
  }
}
