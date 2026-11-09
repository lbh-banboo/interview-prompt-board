import test from 'node:test';
import assert from 'node:assert/strict';
import {
  canStart, capturingSummary, createGuide, guideSteps, guideSummary, startBlockedReason, START_READY_TEXT,
  stepIcon, stepStatus, MIC_DENIED_HELP, SOURCE_PLAN, MIC_GATE_NOTICE, MIC_GATE_UNENROLLED, MIC_GATE_SWITCHED,
  micGateStatus, micOnlyNotice,
} from '../web/live/guide.js';

const g = (patch = {}) => ({ ...createGuide(), consent: true, mic: 'ok', ...patch });

test('线下/线上都有三步，第三步名称随模式变化', () => {
  const off = guideSteps('offline', createGuide());
  const on = guideSteps('online', createGuide());
  assert.equal(off.length, 3);
  assert.deepEqual(off.map((s) => s.id), ['choose', 'authorize', 'source']);
  assert.equal(off[2].title, '确认外放测试');
  assert.equal(on[2].title, '添加会议音频');
  assert.match(SOURCE_PLAN.offline, /一路麦克风/);
  assert.match(SOURCE_PLAN.offline, /声纹/);
  assert.match(SOURCE_PLAN.online, /会议音频/);
});

test('步骤推进：同意 -> 授权 -> 线下外放测试/线上会议音频', () => {
  const initial = guideSteps('offline', createGuide());
  assert.equal(initial[0].phase, 'active');
  assert.equal(initial[1].phase, 'pending');
  assert.equal(initial[2].phase, 'pending');
  assert.equal(canStart('offline', createGuide()), false);

  const consent = guideSteps('offline', { ...createGuide(), consent: true });
  assert.equal(consent[0].phase, 'done');
  assert.equal(consent[1].phase, 'active');
  assert.equal(consent[2].phase, 'pending');

  const mic = guideSteps('offline', g());
  assert.equal(mic[1].phase, 'done');
  assert.equal(mic[2].phase, 'active');

  assert.equal(canStart('offline', g()), false);
  assert.equal(canStart('offline', g({ speaker: 'ok' })), true);
  assert.equal(canStart('offline', g({ speaker: 'skipped' })), true, '跳过外放测试也算就绪');
  assert.equal(canStart('offline', g({ speaker: 'failed' })), false);
});

test('线上会议音频未决（pending）时不能开始；拒绝授权给出可执行指引', () => {
  assert.equal(canStart('online', g()), false);
  assert.equal(canStart('online', g({ meeting: 'ok' })), true);
  const denied = guideSteps('online', g({ mic: 'denied' }));
  assert.equal(denied[1].phase, 'attention');
  assert.equal(denied[1].status, '需要处理');
  assert.match(denied[1].hint, /隐私与安全性/);
  assert.match(MIC_DENIED_HELP, /Chrome/);
  assert.equal(guideSteps('online', g({ mic: 'silent' }))[1].phase, 'attention');
});

test('第三步骤在未完成第二步时保持未开始', () => {
  const steps = guideSteps('offline', { ...createGuide(), consent: true, mic: 'checking' });
  assert.equal(steps[2].phase, 'pending');
});

test('摘要与图标文案', () => {
  assert.match(guideSummary('offline', g({ speaker: 'ok' })), /房间麦克风/);
  assert.match(guideSummary('offline', g({ speaker: 'ok' })), /外放测试通过/);
  assert.match(guideSummary('offline', g({ speaker: 'skipped' })), /已跳过/);
  assert.match(guideSummary('online', g({ meeting: 'ok' })), /麦克风 \+ 会议音频/);
  assert.match(capturingSummary('offline'), /声纹区分双方/);
  assert.equal(stepIcon('done'), '✓');
  assert.equal(stepIcon('attention'), '!');
  assert.equal(stepIcon('pending'), '○');
  assert.equal(stepIcon('skipped'), '–');
  assert.equal(stepStatus('active'), '进行中');
  assert.equal(stepStatus('skipped'), '已跳过');
});

test('线上：会议音频可跳过，麦克风就绪后即可开始，并明确提示未采集对方声音', () => {
  assert.equal(canStart('online', g({ meeting: 'skipped' })), true, '跳过会议音频也算就绪');
  assert.equal(canStart('online', g({ meeting: 'failed' })), true, '会议音频失败时仍可只用麦克风开始');
  assert.equal(canStart('online', g({ mic: 'pending', meeting: 'skipped' })), false, '麦克风没就绪不能开始');
  const s = guideSteps('online', g({ meeting: 'skipped' }));
  assert.equal(s[2].phase, 'skipped');
  assert.match(s[2].hint, /对方声音/);
  assert.match(guideSummary('online', g({ meeting: 'skipped' })), /仅麦克风/);
  assert.match(capturingSummary('online', g({ meeting: 'skipped' })), /单麦模式/);
});

test('单麦模式文案与状态机：已登记/未登记/切回双通道/开关关闭', () => {
  assert.match(MIC_GATE_NOTICE, /声纹/);
  assert.match(MIC_GATE_NOTICE, /外放或同一房间/);
  assert.match(MIC_GATE_NOTICE, /拿不准的语音不会当成提问/);
  assert.match(MIC_GATE_UNENROLLED, /声音登记/);
  assert.match(MIC_GATE_UNENROLLED, /未登记时单麦模式不可用/);
  assert.match(MIC_GATE_SWITCHED, /已切换为双通道模式/);

  const enrolled = { gateOn: true, known: true, enrolled: true, switched: false };
  assert.equal(micOnlyNotice(enrolled), MIC_GATE_NOTICE);
  assert.equal(micOnlyNotice({ ...enrolled, enrolled: false }), MIC_GATE_UNENROLLED);
  assert.equal(micOnlyNotice({ ...enrolled, switched: true }), MIC_GATE_SWITCHED);
  assert.match(micOnlyNotice({ ...enrolled, gateOn: false }), /提示板不会出现提示/);
  assert.match(capturingSummary('online', g({ meeting: 'skipped' }), true, false), /单麦模式不可用/);
  assert.match(capturingSummary('online', g({ meeting: 'skipped' }), false, true), /对方声音没有被采集/);
  assert.match(guideSummary('online', g({ meeting: 'skipped' }), false), /未加入会议音频/);
});

test('单麦门控计数展示：已识别提问 / 未识别 / 声纹拿不准 / 切换双通道', () => {
  assert.equal(micGateStatus(null), '');
  assert.equal(micGateStatus({ state: 'off' }), '');
  assert.equal(micGateStatus({ state: 'switched', counts: { questioner: 1 } }), '已切换为双通道模式');
  const text = micGateStatus({ state: 'active', counts: { questioner: 2, answerer: 1, unknown: 3 }, avg_latency_ms: 800 });
  assert.match(text, /单麦模式/);
  assert.match(text, /已识别提问 2 次/);
  assert.match(text, /未识别 1 次/);
  assert.match(text, /声纹拿不准 3 次/);
  assert.match(text, /角色判定约 800ms/);
  assert.match(micGateStatus({ state: 'active', counts: {} }), /已识别提问 0 次.*未识别 0 次.*拿不准 0 次/);
});

test('开始按钮禁用原因：各未满足条件各有具体文案，就绪后为"可以开始采集"', () => {
  assert.equal(startBlockedReason('online', createGuide()), '还差：第①步勾选“我已知情并同意本场录音与转写”');
  assert.match(startBlockedReason('online', { ...createGuide(), consent: true }) ?? '', /第②步.*授权/);
  assert.match(startBlockedReason('online', { ...createGuide(), consent: true, mic: 'checking' }) ?? '', /正在打开麦克风/);
  assert.match(startBlockedReason('online', { ...createGuide(), consent: true, mic: 'denied' }) ?? '', /重新授权/);
  assert.match(startBlockedReason('online', { ...createGuide(), consent: true, mic: 'silent' }) ?? '', /没有检测到麦克风声音/);
  assert.match(startBlockedReason('online', g()) ?? '', /第③步.*添加会议音频.*跳过，仅用麦克风/,
    '明确告诉用户卡在第③步，并给出跳过出路');
  assert.match(startBlockedReason('offline', g()) ?? '', /第③步.*外放测试/);
  assert.match(startBlockedReason('offline', g({ speaker: 'failed' })) ?? '', /没听到声音/);
  assert.equal(startBlockedReason('online', g({ meeting: 'ok' })), null);
  assert.equal(startBlockedReason('online', g({ meeting: 'skipped' })), null);
  assert.equal(startBlockedReason('online', g({ meeting: 'failed' })), null);
  assert.equal(startBlockedReason('offline', g({ speaker: 'ok' })), null);
  assert.equal(startBlockedReason('offline', g({ speaker: 'skipped' })), null);
  assert.equal(startBlockedReason('online', g({ meeting: 'ok' })) === null, canStart('online', g({ meeting: 'ok' })),
    '原因与 canStart 同步');
  assert.equal(START_READY_TEXT, '可以开始采集');
});
