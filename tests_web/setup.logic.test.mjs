import test from 'node:test';
import assert from 'node:assert/strict';
import {
  emptyState, validateStep, firstInvalid, toggle, unionHint, libraryRows, createBody, startBody, summarize, startMode, SHARE_REMINDER,
  liveHref, activeSession, conflictMessage, selectLibrary, initOutbound, outboundPatch,
} from '../web/setup/logic.js';

const libs = [
  { id: 'a', name: '库A', total: 3, approved: 2, selectable: true },
  { id: 'z', name: '库Z', total: 3, approved: 0, selectable: false },
];
const ok = { ...emptyState(), mode: 'online', profileId: 'p', roles: ['管理'], libraryIds: ['a'], sourceConfirmed: true, consent: true };
const rep = (items, extra = {}) => ({
  items, overall: 'PASS', has_fail: false, can_degrade_start: false, degraded_banner: null, ...extra,
});

test('step validation covers every step', () => {
  const s = emptyState();
  assert.match(validateStep(s, 'mode', libs), /收音模式/);
  assert.match(validateStep(s, 'profile', libs), /资料/);
  assert.match(validateStep(s, 'roles', libs), /至少选择一个目标岗位/);
  assert.match(validateStep(s, 'library', libs), /选择一个主库/);
  assert.match(validateStep(s, 'source', libs), /音源/);
  assert.match(validateStep({ ...s, sourceConfirmed: true }, 'source', libs), /同意/);
  assert.equal(firstInvalid(ok, libs), null);
  assert.equal(firstInvalid({ ...ok, roles: [] }, libs), 'roles');
});

test('zero-approved library cannot be chosen (M01-T2); secondary-only path allowed', () => {
  assert.match(validateStep({ ...ok, libraryIds: ['z'] }, 'library', libs), /启用 0 条/);
  assert.match(validateStep({ ...ok, libraryIds: ['nope'] }, 'library', libs), /启用 0 条/);
  assert.equal(validateStep({ ...ok, libraryIds: [], secondaryOnly: true }, 'library', libs), null);
  const none = [libs[1]];
  const emptyHint = validateStep({ ...ok, libraryIds: [] }, 'library', none);
  assert.match(emptyHint, /没有可选的主库/);
  assert.match(emptyHint, /导入资料/);
  const rows = libraryRows(libs);
  assert.equal(rows[0].hint, '');
  assert.match(rows[1].hint, /启用 0 条/);
  assert.match(rows[1].hint, /导入资料/);
  assert.match(rows[0].label, /启用 2 \/ 共 3/);
});

test('主库单选：点选替换，再点已选项取消，createBody 至多 1 个', () => {
  assert.deepEqual(selectLibrary([], 'a'), ['a']);
  assert.deepEqual(selectLibrary(['a'], 'b'), ['b']);
  assert.deepEqual(selectLibrary(['a'], 'a'), []);
  const body = createBody({ ...ok, libraryIds: selectLibrary(['a'], 'b') });
  assert.deepEqual(body.library_ids, ['b']);
});

test('roles toggle and union hint (S2)', () => {
  assert.deepEqual(toggle(['a'], 'b'), ['a', 'b']);
  assert.deepEqual(toggle(['a', 'b'], 'a'), ['b']);
  assert.equal(unionHint({ ...ok, roles: ['技术开发'] }), '');
  assert.match(unionHint({ ...ok, roles: ['技术开发', '管理'] }), /并集/);
});

test('request bodies', () => {
  assert.deepEqual(createBody(ok), { mode: 'online', profile_id: 'p', roles: ['管理'], library_ids: ['a'], consent: true, secondary_only: false, outbound: {} });
  assert.equal(createBody({ ...ok, secondaryOnly: true }).secondary_only, true);
  assert.deepEqual(startBody(true), { degraded: true });
});

test('本场外发：默认跟随全局，只能关不能开，写入 createBody', () => {
  const s = initOutbound(emptyState(), { outbound: { asr_audio: true, llm_snippets: false, web_questions: true } });
  assert.deepEqual(s.outbound, { asr_audio: true, llm_snippets: false, web_questions: true });
  assert.deepEqual(s.outboundBaseline, s.outbound);
  assert.deepEqual(outboundPatch(s), {}, '沿用全局时不提交覆盖');
  const closed = { ...s, outbound: { ...s.outbound, asr_audio: false } };
  assert.deepEqual(outboundPatch(closed), { asr_audio: false });
  assert.deepEqual(createBody({ ...ok, outbound: closed.outbound, outboundBaseline: closed.outboundBaseline }).outbound,
    { asr_audio: false });
  const triedToEnable = { ...s, outbound: { ...s.outbound, llm_snippets: true } };
  assert.deepEqual(outboundPatch(triedToEnable), {}, '全局已关闭的项不允许本场开启');
  assert.deepEqual(initOutbound(emptyState(), null).outbound, { asr_audio: true, llm_snippets: true, web_questions: true });
});

test('selfcheck summary keeps FAIL visible and blocks silent start (S3)', () => {
  const r = rep([
    { name: 'asr_connect', status: 'FAIL', detail: 'ASR未配置' },
    { name: 'disk_space', status: 'WARN', detail: '' },
    { name: 'custom', status: 'PASS', detail: '' },
  ], { overall: 'FAIL', has_fail: true, degraded_banner: '实时转写不可用' });
  const s = summarize(r);
  assert.deepEqual(s.counts, { PASS: 1, WARN: 1, FAIL: 1 });
  assert.match(s.headline, /不能静默开始/);
  assert.equal(s.rows[0].label, 'ASR 连通');
  assert.equal(s.rows[2].label, 'custom');
  assert.equal(s.needsDegrade, true);
  assert.equal(s.banner, '实时转写不可用');
  assert.equal(startMode(r, false), 'blocked');
  assert.equal(startMode(r, true), 'degrade');
});

test('headlines and start modes without failures', () => {
  assert.equal(summarize(rep([])).headline, '全部通过');
  assert.match(summarize(rep([{ name: 'a', status: 'WARN', detail: '' }], { overall: 'WARN' })).headline, /有提醒/);
  assert.equal(startMode(null, false), 'wait');
  assert.equal(startMode(rep([]), false), 'start');
});

test('share reminder text', () => {
  assert.equal(SHARE_REMINDER, '请只共享需要演示的应用窗口，不要共享整个屏幕');
});

test('active session card: the only way in is the live session page', () => {
  assert.equal(liveHref('a b'), '/live.html?session=a%20b');
  assert.equal(liveHref('abc'), '/live.html?session=abc');
  assert.deepEqual(activeSession({ active_session: null }), { active: false, id: '', label: '' });
  const on = activeSession({ active_session: { id: 'd00f68498625434a', mode: 'online' } });
  assert.equal(on.active, true);
  assert.equal(on.id, 'd00f68498625434a');
  assert.match(on.label, /进行中的会话（线上）/);
  assert.match(on.label, /d00f6849/);
  const msg = conflictMessage('已有进行中的会话，请先结束后再创建');
  assert.match(msg, /卡片继续/);
  assert.match(msg, /先结束该会话/);
  assert.equal(conflictMessage('主库不存在'), '主库不存在');
});

// ---------- 线下声音登记 ----------
import {
  stepsFor, STEP_TITLES, enrollReady, enrollResultText, profileText, qualityText, ENROLL_NOTICE,
  ENROLL_SEGMENTS, ENROLL_HINT, VOICE_OPTIONAL_NOTE, durationText, recordButtonText, submitEnabled,
} from '../web/setup/logic.js';

test('所有模式统一六步；线上声音登记可选、线下必填', () => {
  const six = ['mode', 'profile', 'roles', 'library', 'voice', 'source'];
  assert.deepEqual(stepsFor('online'), six);
  assert.deepEqual(stepsFor('offline'), six);
  assert.deepEqual(stepsFor(''), six);
  assert.equal(STEP_TITLES.voice, '声音登记');
  assert.match(VOICE_OPTIONAL_NOTE, /可选/);
  assert.match(VOICE_OPTIONAL_NOTE, /线下同室/);
  const online = { ...ok, mode: 'online' };
  assert.equal(validateStep(online, 'voice', libs), null, '线上不阻塞');
  assert.equal(firstInvalid(online, libs), null);
  const on = { ...ok, mode: 'online', voiceEnrolled: false };
  assert.equal(validateStep(on, 'voice', libs), null);
  assert.equal(validateStep({ ...ok, mode: 'offline' }, 'voice', libs) === null, false);
  const off = { ...ok, mode: 'offline' };
  assert.match(validateStep(off, 'voice', libs), /声音登记/);
  assert.equal(validateStep(off, 'source', libs), null, 'source 校验不受影响');
  assert.equal(validateStep({ ...off, voiceEnrolled: true }, 'voice', libs), null);
  assert.equal(validateStep({ ...off, voiceUnavailable: true }, 'voice', libs), null, '模型不可用有降级出路');
  assert.equal(firstInvalid({ ...off, voiceEnrolled: true }, libs), null);
  assert.equal(firstInvalid(off, libs), 'voice');
});

test('登记质量与失败文案用后端 Reason/MESSAGES，不吞掉原因', () => {
  assert.equal(qualityText({ effective_speech_s: 12.3, snr_db: 20.1, min_pair_similarity: 0.92 }),
    '有效语音 12.3 秒 / SNR 20.1 dB / 最低相似度 0.92');
  assert.equal(qualityText(null), '');
  assert.equal(profileText(null), '未登记');
  assert.match(profileText({ enrolled: true, quality: { effective_speech_s: 9 }, created_at: 1762300000 }), /已登记/);
  assert.match(profileText({ enrolled: true, quality: { effective_speech_s: 9 }, created_at: 1762300000 }), /2025-11-05|2025-11-04/);
  assert.deepEqual(enrollResultText({ ok: false, reason: 'too_short_speech', message: '有效语音不足 8 秒，请按提示文本完整朗读并重录。' }),
    { ok: false, text: '有效语音不足 8 秒，请按提示文本完整朗读并重录。', detail: '' });
  const success = enrollResultText({ ok: true, quality: { effective_speech_s: 9.5, snr_db: 18 } });
  assert.equal(success.ok, true);
  assert.match(success.text, /登记成功/);
  assert.equal(enrollReady(ENROLL_SEGMENTS - 1), false);
  assert.equal(enrollReady(ENROLL_SEGMENTS), true);
  assert.match(ENROLL_NOTICE, /本机/);
  assert.match(ENROLL_NOTICE, /删除/);
});

test('连续录音：开始/停止/重录文案、时长格式、提交条件与失败指引', () => {
  assert.equal(ENROLL_SEGMENTS, 1, '连续录音只提交一段');
  assert.equal(recordButtonText(false, false), '开始录音');
  assert.equal(recordButtonText(true, false), '停止录音');
  assert.equal(recordButtonText(true, true), '停止录音');
  assert.equal(recordButtonText(false, true), '重新录制');

  assert.equal(durationText(0), '00:00');
  assert.equal(durationText(15.3), '00:15');
  assert.equal(durationText(65.9), '01:05');
  assert.equal(durationText(Number.NaN), '00:00');

  assert.equal(submitEnabled(false, false, 0, false), false, '没有录音不能提交');
  assert.equal(submitEnabled(true, true, 15, false), false, '录音中不能提交');
  assert.equal(submitEnabled(false, true, 0.9, false), false, '不足 1 秒不能提交');
  assert.equal(submitEnabled(false, true, 1, false), true, '≥1 秒即可提交，最终由后端判定');
  assert.equal(submitEnabled(false, true, 15, true), false, '提交中禁用');

  assert.match(ENROLL_HINT, /开始录音/);
  assert.match(ENROLL_HINT, /停止录音/);
  assert.match(ENROLL_HINT, /15 秒/);
  assert.doesNotMatch(ENROLL_HINT, /3 段|第 \d 段/);

  const fail = enrollResultText({
    ok: false, reason: 'too_short_speech', message: '有效语音不足 8 秒，请按提示文本完整朗读并重录。',
    quality: { effective_speech_s: 6.72, snr_db: 22.9 },
  });
  assert.equal(fail.ok, false);
  assert.match(fail.text, /6\.72/);
  assert.match(fail.text, /15 秒/);
  assert.match(fail.detail, /SNR 22\.9 dB/);
});

test('所有模式的步骤里都包含声音登记，首页入口 ?step=voice 可直达', async () => {
  const { stepsFor } = await import('../web/setup/logic.js');
  assert.ok(stepsFor('offline').includes('voice'));
  assert.ok(stepsFor('online').includes('voice'));
  assert.equal(stepsFor('online').indexOf('voice'), 4);
});
