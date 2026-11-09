// 设置页纯逻辑：字段解析/范围、patch 只含改动、外发文案与资料编辑。
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  ANSWERER_ITEM, ASR_OPTIONS, FIELDS, GROUPS, OUTBOUND_ITEMS, buildPatch, defaultHint, effectiveNotice,
  fieldText, outboundLine, outboundPatch, parseField, pathGet, profilePatch, profileRows, renameError,
  thresholdSource,
} from '../web/settings/logic.js';

const spec = (key) => FIELDS.find((f) => f.key === key);

test('字段定义覆盖配置项并写明单位/影响/待标定/估算', () => {
  const keys = FIELDS.map((f) => f.key);
  for (const key of ['turn.T_silence', 'turn.T_long', 'turn.T_hard', 'turn.T_answer_silence', 'turn.mic_as_question',
    'turn.T_new_turn', 'turn.max_turn_ms', 'turn.max_turn_chars', 'turn.monologue_segments', 'turn.T_monologue',
    'retrieval_thresholds.tau_cos', 'retrieval_thresholds.tau_cos_strong', 'retrieval_thresholds.tau_mid',
    'retrieval_thresholds.margin', 'timeouts.web_first_ms', 'timeouts.web_total_ms', 'timeouts.llm_first_ms',
    'timeouts.llm_total_ms', 'budget.daily_limit', 'budget.session_limit', 'budget.max_inflight',
    'budget.asr_daily_limit', 'budget.asr_session_limit',
    'prices.asr_per_hour', 'prices.llm_per_1k_tokens', 'prices.search_per_call', 'prices.asr_note',
    'model.name', 'model.base_url', 'search.live', 'search.endpoint', 'search.count', 'search.need_content'])
    assert.ok(keys.includes(key), key);
  assert.equal(spec('turn.mic_as_question').kind, 'bool');
  assert.match(GROUPS.find((g) => g.key === 'mic_gate').note, /声纹/);
  assert.match(spec('turn.mic_as_question').hint, /关闭后.*不会识别对方提问/);
  assert.match(GROUPS.find((g) => g.key === 'turn').title, /毫秒/);
  assert.match(GROUPS.find((g) => g.key === 'retrieval_thresholds').note, /未经数据标定/);
  assert.match(GROUPS.find((g) => g.key === 'prices').note, /待填写真实单价/);
  assert.match(GROUPS.find((g) => g.key === 'search').note, /DeepSeek Search/);
  assert.match(GROUPS.find((g) => g.key === 'search').note, /复用 DeepSeek Key.*按次计费.*估算占位/s);
  assert.match(spec('turn.T_silence').hint, /静音/);
  assert.match(spec('prices.asr_per_hour').hint, /估算占位值/);
  assert.equal(spec('asr.backend'), undefined, '后端选择单独渲染');
});

test('parseField：整数/小数/留空/字符串与范围提示', () => {
  assert.deepEqual(parseField(spec('turn.T_silence'), '320'), { ok: true, value: 320 });
  assert.match(parseField(spec('turn.T_silence'), '200.5').error, /整数/);
  assert.match(parseField(spec('turn.T_silence'), '1').error, /不能小于 50/);
  assert.match(parseField(spec('turn.T_hard'), '99999').error, /不能大于 10000/);
  assert.match(parseField(spec('budget.daily_limit'), 'abc').error, /必须是数字/);
  assert.deepEqual(parseField(spec('retrieval_thresholds.tau_cos'), ''), { ok: true, value: null });
  assert.deepEqual(parseField(spec('retrieval_thresholds.tau_cos'), '0.42'), { ok: true, value: 0.42 });
  assert.match(parseField(spec('retrieval_thresholds.tau_cos'), '1.5').error, /不能大于 1/);
  assert.deepEqual(parseField(spec('model.name'), '  m1 '), { ok: true, value: 'm1' });
  assert.match(parseField(spec('model.name'), '   ').error, /不能为空/);
  assert.match(parseField(spec('model.base_url'), 'x'.repeat(201)).error, /最多 200/);
  assert.deepEqual(parseField(spec('search.live'), 'false'), { ok: true, value: false });
  assert.deepEqual(parseField(spec('search.live'), 'true'), { ok: true, value: true });
  assert.match(parseField(spec('search.live'), 'yes').error, /必须是布尔值/);
  assert.deepEqual(parseField(spec('search.count'), '5'), { ok: true, value: 5 });
  assert.match(parseField(spec('search.count'), '51').error, /不能大于 50/);
});

test('buildPatch 只提交改动，非法字段不进入 patch', () => {
  const current = { turn: { T_silence: 200 }, retrieval_thresholds: { tau_cos: null }, model: { name: 'm' },
    prices: { asr_note: '备注' } };
  const r = buildPatch(FIELDS, current, {
    'turn.T_silence': '200', 'retrieval_thresholds.tau_cos': '', 'model.name': 'm', 'prices.asr_note': '备注',
  });
  assert.deepEqual(r.patch, {}); assert.deepEqual(r.errors, []); assert.equal(r.changed, 0);

  const r2 = buildPatch(FIELDS, current, {
    'turn.T_silence': '320', 'retrieval_thresholds.tau_cos': '0.35', 'model.name': 'deepseek-flash',
    'prices.asr_note': '新备注',
  });
  assert.deepEqual(r2.patch, {
    turn: { T_silence: 320 }, retrieval_thresholds: { tau_cos: 0.35 }, model: { name: 'deepseek-flash' },
    prices: { asr_note: '新备注' },
  });
  assert.equal(r2.changed, 4); assert.deepEqual(r2.errors, []);

  const bad = buildPatch(FIELDS, current, { 'turn.T_silence': '1', 'model.name': 'ok' });
  assert.deepEqual(bad.patch, { model: { name: 'ok' } });
  assert.equal(bad.errors.length, 1); assert.match(bad.errors[0], /不能小于 50/);

  const cleared = buildPatch(FIELDS, { retrieval_thresholds: { tau_cos: 0.5 } },
    { 'retrieval_thresholds.tau_cos': '' });
  assert.deepEqual(cleared.patch, { retrieval_thresholds: { tau_cos: null } });
});

test('外发开关：单键 patch、开关文案与生效提示', () => {
  assert.deepEqual(outboundPatch('llm_snippets', false), { outbound: { llm_snippets: false } });
  assert.equal(outboundLine({ asr_audio: true, llm_snippets: false, web_questions: true }),
    '本场外发：音频→云转写 开 · 片段→DeepSeek 关 · 联网 开');
  assert.equal(outboundLine(undefined), '本场外发：音频→云转写 开 · 片段→DeepSeek 开 · 联网 开');
  assert.match(effectiveNotice(true), /快照.*下一场生效/);
  assert.match(effectiveNotice(false), /已保存/);
  assert.equal(OUTBOUND_ITEMS.length, 3);
  assert.ok(OUTBOUND_ITEMS.every((i) => i.title && i.text));
  assert.match(OUTBOUND_ITEMS.find((i) => i.key === 'asr_audio').text, /阿里云/);
  assert.match(OUTBOUND_ITEMS.find((i) => i.key === 'llm_snippets').text, /DeepSeek/);
  assert.match(OUTBOUND_ITEMS.find((i) => i.key === 'web_questions').text, /脱敏.*DeepSeek Search/);
  assert.match(ANSWERER_ITEM.text, /默认关闭/);
  assert.deepEqual(ASR_OPTIONS.map((o) => o.value), ['auto', 'aliyun', 'sherpa']);
  assert.equal(ASR_OPTIONS.find((o) => o.value === 'auto').disabled, false);
});

test('阈值来源与估算标注', () => {
  assert.match(thresholdSource({ retrieval_thresholds: { tau_cos: null, margin: null } }), /默认文件/);
  assert.match(thresholdSource({ retrieval_thresholds: { tau_cos: 0.6 } }), /配置快照.*下一场/);
  assert.match(GROUPS.find((g) => g.key === 'turn').note, /待标定/);
  assert.match(spec('budget.asr_daily_limit').hint, /估算/);
  assert.match(spec('budget.asr_session_limit').hint, /估算/);
});

test('默认值提示与资料编辑', () => {
  assert.equal(defaultHint(spec('turn.T_silence'), { turn: { T_silence: 200 } }), '默认 200');
  assert.equal(defaultHint(spec('retrieval_thresholds.tau_cos'), { retrieval_thresholds: { tau_cos: null } }),
    '默认 未标定');
  assert.equal(fieldText(spec('retrieval_thresholds.tau_cos'), null), '');
  assert.equal(pathGet({ turn: { T_silence: 1 } }, 'turn.T_silence'), 1);
  const p = { id: 'p1', alias: '甲', roles: ['管理'], note: 'n', library_ids: ['l1'] };
  assert.deepEqual(profilePatch(p, ' 乙 '), { alias: '乙', roles: ['管理'], note: 'n', library_ids: ['l1'] });
  assert.equal(renameError('  '), '资料代号不能为空');
  assert.equal(renameError('乙'), null);
  assert.equal(profileRows([p])[0].rolesText, '管理');
  assert.match(profileRows([p])[0].librariesText, /1 套主库/);
});
