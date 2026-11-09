"""M12-2 Markdown 导出：实际发言与系统建议分章节，绝不混写。

只输出白名单字段；不读取声纹模板、配置快照或 Key。
"""
import json
from pathlib import Path

ROLE_NAMES = {'questioner': '面试官', 'answerer': '回答者'}
PERF_NAMES = ('t_end', 't_candidate', 't_committed', 't_visible', 't_web_first', 't_web_done')
LATE_LABEL = '未展示（迟到归档）'
TRANSCRIPT_INTRO = '以下只含现场识别出的发言；系统建议见后文独立章节。'
ANSWERER_NOTE = '回答者发言未转写（仅录音）：本版只对会议/房间通道做实时转写，回答者一侧只保留录音。'
UNTRANSCRIBED = '未知（回答者未转写）'
NO_PRIMARY_NOTE = '本轮没有命中标准答案（没有主答案）；以下是补充/状态提示，不代表已作答。'
VERIFY_NOTE = ('本记录由后端落库内容生成。性能数值来自埋点，目标达成情况 NOT TESTED；'
               '声纹分角色、自动轮次判定、模型与联网补充的真实效果尚未验收；Mock/合成数据一律视为 SIMULATED。')


def recording_files(store, session, root=None):
    """每路录音分段及文件状态：ok / missing / corrupt（非 RIFF/WAVE 或不足 44 字节头）。"""
    out = []
    for r in store.main_rows('SELECT * FROM recordings WHERE session_id=? ORDER BY source, sequence', (session,)):
        path = Path(r.get('path') or '')
        if root is not None and not path.is_absolute():
            path = Path(root) / path
        if not r.get('path') or not path.is_file():
            status = 'missing'
        else:
            with open(path, 'rb') as f:
                head = f.read(12)
            status = 'ok' if len(head) == 12 and head[:4] == b'RIFF' and head[8:12] == b'WAVE' else 'corrupt'
        seq = r.get('sequence')
        out.append(dict(id=r.get('id'), source=r.get('source') or '', sequence=seq, mime=r.get('mime') or '',
                        path=path, status=status, name=f"{r.get('source') or 'audio'}_{seq if seq is not None else 0}.wav"))
    return out


def _fmt(t, t0):
    if t is None or t0 is None:
        return '--:--'
    s = max(0, int(t - t0))
    return f'{s // 60:02d}:{s % 60:02d}'


def _text(content):
    if isinstance(content, list):
        return '；'.join(str(x) for x in content)
    return '' if content is None else str(content)


def _one_line(s):
    return ' '.join(str(s or '').split())


def export_markdown(store, session, root=None, answerer_transcribed=None):
    """answerer_transcribed：本场是否转写回答者。None 时按逐字稿推断（有回答者片段即视为已转写）。
    该参数同时决定“回答者未转写”说明与轮次的回答状态，避免把“未转写”写成“未回答”。"""
    segs = store.rows('SELECT * FROM segments WHERE session_id=? ORDER BY COALESCE(t_start, 0), id', (session,))
    turns = store.rows('SELECT * FROM turns WHERE session_id=? ORDER BY COALESCE(t_committed, t_candidate, 0), rowid',
                       (session,))
    events = store.rows('SELECT turn_id, type FROM turn_events WHERE session_id=?', (session,))
    prompts = store.rows('SELECT * FROM prompts WHERE session_id=? ORDER BY id', (session,))
    starts_ = [s['t_start'] for s in segs if s['t_start'] is not None]
    t0 = min(starts_) if starts_ else None
    info = (store.main_rows('SELECT * FROM sessions WHERE id=?', (session,)) or [{}])[0]
    has_answerer = any((s['edited_role'] or s['role']) == 'answerer' for s in segs)
    answerer_known = has_answerer if answerer_transcribed is None else bool(answerer_transcribed) or has_answerer

    out = [f'# 面试记录 {session}', '', '## 会话信息', f'- 会话：{session}']
    if info:
        out += [f"- 模式：{info.get('mode')}", f"- 状态：{info.get('status')}"]
    out += [f'- 逐字稿片段：{len(segs)}；轮次：{len(turns)}；系统提示：{len(prompts)}', '']

    out += ['## 现场实际发言（逐字稿）', TRANSCRIPT_INTRO]
    if not answerer_known:
        out.append(ANSWERER_NOTE)
    out.append('')
    for s in segs:
        flags = [f for f in (s['flags'] or '').split(',') if f]
        role = s['edited_role'] or s['role']
        text = s['edited_text'] if s['edited_text'] is not None else s['text']
        mark = ''.join(f' [{f}]' for f in flags) + (' [已修订]' if s['edited_text'] is not None or s['edited_role'] else '')
        out.append(f"- [{_fmt(s['t_start'], t0)}] {ROLE_NAMES.get(role, role)}（{s['source']}）{mark}：{_one_line(text)}")
    if not segs:
        out.append('（无逐字稿）')

    out += ['', '## 问答轮次']
    answered_at = sorted(s['t_start'] for s in segs
                         if (s['edited_role'] or s['role']) == 'answerer' and s['t_start'] is not None)
    starts = [t['t_committed'] for t in turns]
    for i, t in enumerate(turns):
        nxt = next((x for x in starts[i + 1:] if x is not None), None)
        evs = [e['type'] for e in events if e['turn_id'] == t['id']]
        if t['t_committed'] is None:
            answered = '未知'
        elif not answerer_known:
            answered = UNTRANSCRIBED
        else:
            hit = any(t['t_committed'] <= a and (nxt is None or a < nxt) for a in answered_at)
            answered = '已回答' if hit else '未回答'
        rel = f"，子问题（父 {t['parent_id']}）" if t['parent_id'] else ''
        out.append(f"- {t['id']}（{t['kind'] or '未分类'}{rel}）：{_one_line(t['text'])}"
                   f"；追问={'是' if t['kind'] == 'followup' else '否'}；打断={evs.count('interrupted')}"
                   f"；回滚={t['rolled_back_count']}；{answered}")
    if not turns:
        out.append('（无轮次）')

    out += ['', '## 系统建议（系统生成，不等于实际发言）']
    if not any(p['block'] == 'primary' for p in prompts):
        out.append(NO_PRIMARY_NOTE)
    for p in prompts:
        label = LATE_LABEL if p['state'] == 'late_archived' else p['state']
        block = {'primary': '主答案', 'supplement': '补充'}.get(p['block'], p['block'])
        urls = json.loads(p['source_urls_json'] or '[]')
        line = (f"- {p['turn_id']} {block}（级别 {p['level'] or '未标注'}；来源 {p['source_tag'] or '-'}；{label}）："
                f"{_one_line(_text(json.loads(p['content_json'])))}")
        out.append(line + ''.join(f' <{u}>' for u in urls))
    if not prompts:
        out.append('（无系统建议）')

    out += ['', '## 性能记录', '时间为 ms，相对 t_end（缺失则取该题最早项）；SIMULATED/NOT TESTED 之外的达标结论本记录不下。', '',
            '| 轮次 | ' + ' | '.join(PERF_NAMES) + ' |', '|---|' + '---|' * len(PERF_NAMES)]
    perf = {}
    for m in store.main_rows('SELECT * FROM metrics WHERE session_id=? ORDER BY t_ns, rowid', (session,)):
        perf.setdefault(m['turn_id'], {}).setdefault(m['name'], m['t_ns'])
    for tid, d in perf.items():
        base = d.get('t_end', min(d.values()))
        out.append(f'| {tid} | ' + ' | '.join(
            f'{(d[n] - base) / 1e6:+.1f}' if n in d else '—' for n in PERF_NAMES) + ' |')
    if not perf:
        out.append('| （无埋点） |' + ' |' * len(PERF_NAMES))

    out += ['', '## 录音']
    recs = recording_files(store, session, root)
    for r in recs:
        if r['status'] == 'ok':
            out.append(f"- {r['source']} 第{r['sequence']}段：{r['name']}（/api/recordings/{r['id']}）")
        else:
            out.append(f"- {r['source']} 第{r['sequence']}段：{'录音缺失' if r['status'] == 'missing' else '录音损坏'}（{r['name']}）")
    if not recs:
        out.append('录音缺失：没有录音记录')

    out += ['', '## 录音缺口']
    gaps = store.main_rows('SELECT * FROM recording_gaps WHERE session_id=?', (session,))
    for g in gaps:
        a, b = g.get('t_start_ms', g.get('t_start')), g.get('t_end_ms', g.get('t_end'))
        out.append(f"- {g.get('source') or g.get('stream_id') or '-'}：{a}–{b}（{g.get('reason')}）")
    if not gaps:
        out.append('（无缺口记录）')

    out += ['', '## 验证状态', VERIFY_NOTE, '']
    return '\n'.join(out)
