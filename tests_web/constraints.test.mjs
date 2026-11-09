import test from 'node:test';
import assert from 'node:assert/strict';
import {
  micConstraints, micProblemText, onlineMicConstraints, roomAudioConstraints, ROOM_MIC_NOTE,
} from '../web/capture/constraints.js';

test('线下单麦关闭回声消除、降噪、自动增益（保留远场人声）', () => {
  const c = roomAudioConstraints();
  assert.equal(c.audio.echoCancellation, false);
  assert.equal(c.audio.noiseSuppression, false);
  assert.equal(c.audio.autoGainControl, false);
  assert.equal(c.audio.deviceId, undefined);
  const withDevice = micConstraints('offline', 'mic-1');
  assert.deepEqual(withDevice.audio.deviceId, { exact: 'mic-1' });
  assert.match(ROOM_MIC_NOTE, /回声消除/);
});

test('线上麦克风保持浏览器默认处理，行为不变', () => {
  assert.equal(onlineMicConstraints().audio, true);
  assert.deepEqual(onlineMicConstraints('mic-1'), { audio: { deviceId: { exact: 'mic-1' } } });
  assert.equal(micConstraints('online').audio, true);
});

test('授权失败给 macOS 系统设置路径，不原样抛浏览器错误', () => {
  const denied = micProblemText(Object.assign(new Error('x'), { name: 'NotAllowedError' }));
  assert.match(denied, /隐私与安全性/);
  assert.match(denied, /麦克风/);
  assert.match(micProblemText({ name: 'NotFoundError' }), /没有可用的麦克风/);
  assert.match(micProblemText({ name: 'NotReadableError' }), /占用/);
});
