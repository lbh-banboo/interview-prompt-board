import test from 'node:test';
import assert from 'node:assert/strict';
import {
  endConfirmText, exportName, formatElapsed, liveHref, modeLabel, phaseLabel, phaseOf, shortId,
  sourceSummary, elapsedSeconds, degradedText, canCapture,
} from '../web/live/logic.js';

test('liveHref 编码 session，导出文件名用短 id', () => {
  assert.equal(liveHref('a b'), '/live.html?session=a%20b');
  assert.equal(liveHref('abc'), '/live.html?session=abc');
  assert.equal(shortId('d00f68498625434a'), 'd00f6849');
  assert.equal(exportName('d00f68498625434a'), '面试记录-d00f6849.md');
});

test('模式与阶段文案', () => {
  assert.equal(modeLabel('online'), '线上');
  assert.equal(modeLabel('offline'), '线下');
  assert.equal(modeLabel(''), '未知模式');
  assert.equal(phaseLabel('preparing'), '准备中');
  assert.equal(phaseLabel('capturing'), '采集中');
  assert.equal(phaseLabel('ended'), '已结束');
});

test('阶段推导：active 未采集=准备中，采集=采集中，ended/stopped=已结束', () => {
  assert.equal(phaseOf('active', false), 'preparing');
  assert.equal(phaseOf('active', true), 'capturing');
  assert.equal(phaseOf('ready', false), 'preparing');
  assert.equal(phaseOf('stopped', false), 'ended');
  assert.equal(phaseOf('stopped', true), 'ended');
  assert.equal(phaseOf('ended', true), 'ended');
  assert.equal(canCapture('preparing'), true);
  assert.equal(canCapture('capturing'), true);
  assert.equal(canCapture('ended'), false);
});

test('时长格式：mm:ss 与 h:mm:ss，负值归零', () => {
  assert.equal(formatElapsed(0), '00:00');
  assert.equal(formatElapsed(9), '00:09');
  assert.equal(formatElapsed(65), '01:05');
  assert.equal(formatElapsed(600), '10:00');
  assert.equal(formatElapsed(3661), '1:01:01');
  assert.equal(formatElapsed(-5), '00:00');
  assert.equal(formatElapsed(65.9), '01:05');
});

test('已用时长：从开始采集计（无采集起点回落会话开始），结束后冻结', () => {
  const liveInfo = { status: 'active', started_at: 1000 };
  assert.equal(elapsedSeconds(liveInfo, 1065), 65);
  const ended = { status: 'stopped', started_at: 1000, stopped_at: 1120 };
  assert.equal(elapsedSeconds(ended, 9999), 120);
  assert.equal(elapsedSeconds({ status: 'stopped', started_at: 1000 }, 9999, 1150), 150);
  assert.equal(elapsedSeconds({ status: 'stopped', started_at: 1000 }, 9999), 0);
  assert.equal(elapsedSeconds({ status: 'active' }, 9999), null);
  // 采集起点优先：开始会话 1000 秒后才开始采集，采集 74 秒 → 显示 74 秒
  const capturing = { status: 'active', started_at: 1000, capture_started_at: 2000 };
  assert.equal(elapsedSeconds(capturing, 2074), 74);
  assert.equal(elapsedSeconds({ ...capturing, status: 'stopped', stopped_at: 2074 }, 9999), 74);
});

test('音源摘要：正常/无声音/已断开 + 帧数与缺口', () => {
  const snap = {
    capturing: true,
    frames: { mic: 120, meeting: 40 },
    gaps: 2,
    sources: { mic: { condition: 'ok' }, meeting: { condition: 'silent' } },
  };
  assert.equal(sourceSummary(snap), '麦克风 正常 120 帧 · 会议音频无声音 40 帧 · 缺口 2 帧');
  assert.equal(sourceSummary({ ...snap, sources: { mic: { condition: 'lost' }, meeting: { condition: 'idle' } } }),
    '麦克风 已断开 120 帧 · 会议音频 未开始 40 帧 · 缺口 2 帧');
  assert.match(sourceSummary({ ...snap, sources: { mic: { condition: 'checking' }, meeting: { condition: 'ok' } } }), /麦克风 检测中/);
  assert.match(sourceSummary({ ...snap, sources: { mic: { condition: 'ok' }, meeting: { condition: 'silent' } } }),
    /会议音频无声音 40 帧/);
});

test('降级横幅与结束确认文案', () => {
  assert.equal(degradedText('实时转写不可用'), '降级：实时转写不可用');
  assert.equal(degradedText(''), '');
  assert.equal(degradedText(null), '');
  assert.match(endConfirmText, /结束/);
  assert.match(endConfirmText, /保存/);
});
