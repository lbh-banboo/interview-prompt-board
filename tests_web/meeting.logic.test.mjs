import test from 'node:test';
import assert from 'node:assert/strict';
import {
  DISPLAY_CONSTRAINTS, DISPLAY_SURFACE_WARNING, MEETING_NO_SOUND_TEXT, NO_AUDIO_HINT,
  displaySurfaceOf, displaySurfaceWarning, isVirtualDevice, meetingAudioConstraints,
  meetingDeviceLabel, meetingInputs,
} from '../web/capture/meeting.js';

const track = (displaySurface) => ({ getSettings: () => ({ displaySurface }) });

test('共享整个屏幕/窗口时立刻给出改选标签页或虚拟声卡的提示', () => {
  assert.equal(displaySurfaceWarning(track('monitor')), DISPLAY_SURFACE_WARNING);
  assert.equal(displaySurfaceWarning(track('window')), DISPLAY_SURFACE_WARNING);
  assert.match(DISPLAY_SURFACE_WARNING, /整个屏幕\/窗口/);
  assert.match(DISPLAY_SURFACE_WARNING, /Chrome 标签页/);
  assert.match(DISPLAY_SURFACE_WARNING, /共享标签页音频/);
  assert.match(DISPLAY_SURFACE_WARNING, /虚拟声卡/);
  assert.equal(displaySurfaceWarning(track('browser')), null);
  assert.equal(displaySurfaceWarning(undefined), null);
  assert.equal(displaySurfaceWarning({ getSettings: () => ({}) }), null);
  assert.equal(displaySurfaceOf(track('monitor')), 'monitor');
});

test('getDisplayMedia 约束只含 Chrome 支持的提示参数', () => {
  assert.deepEqual(Object.keys(DISPLAY_CONSTRAINTS).sort(),
    ['audio', 'preferCurrentTab', 'selfBrowserSurface', 'surfaceSwitching', 'systemAudio', 'video']);
  assert.equal(DISPLAY_CONSTRAINTS.video, true);
  assert.equal(DISPLAY_CONSTRAINTS.audio, true);
  assert.equal(DISPLAY_CONSTRAINTS.selfBrowserSurface, 'exclude');
  assert.equal(DISPLAY_CONSTRAINTS.systemAudio, 'include');
  assert.equal(DISPLAY_CONSTRAINTS.surfaceSwitching, 'include');
  assert.equal(DISPLAY_CONSTRAINTS.preferCurrentTab, false);
});

test('虚拟声卡识别与标注：BlackHole / Loopback / Soundflower / VB-Cable', () => {
  for (const label of ['BlackHole 2ch', 'Loopback Audio', 'Soundflower (2ch)', 'VB-Cable', 'CABLE Input (VB-Audio)', '虚拟声卡']) {
    assert.equal(isVirtualDevice(label), true, label);
    assert.match(meetingDeviceLabel(label), /（虚拟声卡）$/, label);
  }
  assert.equal(isVirtualDevice('MacBook Pro 麦克风'), false);
  assert.equal(meetingDeviceLabel('MacBook Pro 麦克风'), 'MacBook Pro 麦克风');
});

test('会议设备列表排除麦克风所选设备，并让虚拟声卡优先且保持原顺序', () => {
  const devices = [
    { kind: 'audioinput', deviceId: 'mic1', label: 'MacBook Pro 麦克风' },
    { kind: 'audioinput', deviceId: 'usb', label: 'USB 麦克风' },
    { kind: 'audioinput', deviceId: 'bh', label: 'BlackHole 2ch' },
    { kind: 'audiooutput', deviceId: 'spk', label: '扬声器' },
    { kind: 'audioinput', deviceId: '', label: '无 id 设备' },
    { kind: 'audioinput', deviceId: 'vb', label: 'VB-Cable' },
    { kind: 'audioinput', deviceId: 'mic2', label: '第二麦克风' },
  ];
  assert.deepEqual(meetingInputs(devices, 'mic1').map((d) => d.deviceId), ['bh', 'vb', 'usb', 'mic2']);
  assert.match(meetingInputs(devices, 'mic1')[0].label, /BlackHole 2ch（虚拟声卡）/);
  assert.deepEqual(meetingInputs(devices, 'bh').map((d) => d.deviceId), ['vb', 'mic1', 'usb', 'mic2']);
  assert.deepEqual(meetingInputs([], ''), []);
});

test('虚拟声卡约束：精确设备 + 关闭回声消除/降噪/自动增益', () => {
  const c = meetingAudioConstraints('blackhole-2ch');
  assert.deepEqual(c.audio.deviceId, { exact: 'blackhole-2ch' });
  assert.equal(c.audio.echoCancellation, false);
  assert.equal(c.audio.noiseSuppression, false);
  assert.equal(c.audio.autoGainControl, false);
  assert.equal(c.video, undefined);
});

test('会议无声音文案固定，供状态条与面板复用', () => {
  assert.equal(MEETING_NO_SOUND_TEXT, '会议音频无声音');
  assert.match(NO_AUDIO_HINT, /共享标签页音频/);
  assert.match(NO_AUDIO_HINT, /虚拟声卡/);
});
