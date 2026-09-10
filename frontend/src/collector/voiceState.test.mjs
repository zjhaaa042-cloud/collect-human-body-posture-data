import test from 'node:test';
import assert from 'node:assert/strict';

import {
  readVoicePreferences,
  persistVoicePreferences,
  voiceRemainingSeconds,
  voiceStatusPresentation
} from './voiceState.mjs';

const memoryStorage = () => {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value))
  };
};

test('语音偏好默认开启并可持久化两个独立开关', () => {
  const storage = memoryStorage();
  assert.deepEqual(readVoicePreferences(storage), {
    output_enabled: true,
    recognition_enabled: true
  });
  persistVoicePreferences(storage, {
    output_enabled: false,
    recognition_enabled: true
  });
  assert.deepEqual(readVoicePreferences(storage), {
    output_enabled: false,
    recognition_enabled: true
  });
});

test('语音待命状态按服务端过期时间计算倒计时', () => {
  const now = Date.parse('2026-09-03T12:00:00.000Z');
  const status = {
    output_enabled: true,
    recognition_enabled: true,
    recognition_available: true,
    listening: true,
    capture_armed: true,
    expires_at: '2026-09-03T12:00:30.000Z'
  };
  assert.equal(voiceRemainingSeconds(status, now), 30);
  assert.deepEqual(voiceStatusPresentation(status, now), {
    text: '采集语音待命 30 秒',
    color: 'processing',
    state: 'armed'
  });
});

test('识别、播报不可用和全部关闭均提供明确状态文本', () => {
  assert.equal(voiceStatusPresentation({
    output_enabled: true,
    recognition_enabled: true,
    recognition_available: false
  }).state, 'error');
  assert.equal(voiceStatusPresentation({
    output_enabled: true,
    output_available: false,
    recognition_enabled: false
  }).text, '语音播报不可用');
  assert.equal(voiceStatusPresentation({
    output_enabled: false,
    recognition_enabled: false
  }).state, 'off');
});
