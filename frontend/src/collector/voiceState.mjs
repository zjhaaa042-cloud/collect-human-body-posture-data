export const VOICE_OUTPUT_STORAGE_KEY = 'bodyPosture.voice.outputEnabled';
export const VOICE_RECOGNITION_STORAGE_KEY = 'bodyPosture.voice.recognitionEnabled';

export const DEFAULT_VOICE_PREFERENCES = Object.freeze({
  output_enabled: true,
  recognition_enabled: true
});

const readBoolean = (storage, key, fallback) => {
  try {
    const value = storage?.getItem(key);
    if (value === 'true') return true;
    if (value === 'false') return false;
  } catch {
    // Storage can be blocked in hardened browser contexts.
  }
  return fallback;
};

export const readVoicePreferences = (storage) => ({
  output_enabled: readBoolean(
    storage,
    VOICE_OUTPUT_STORAGE_KEY,
    DEFAULT_VOICE_PREFERENCES.output_enabled
  ),
  recognition_enabled: readBoolean(
    storage,
    VOICE_RECOGNITION_STORAGE_KEY,
    DEFAULT_VOICE_PREFERENCES.recognition_enabled
  )
});

export const persistVoicePreferences = (storage, preferences) => {
  try {
    storage?.setItem(
      VOICE_OUTPUT_STORAGE_KEY,
      String(Boolean(preferences.output_enabled))
    );
    storage?.setItem(
      VOICE_RECOGNITION_STORAGE_KEY,
      String(Boolean(preferences.recognition_enabled))
    );
  } catch {
    // Runtime state still works when local persistence is unavailable.
  }
  return {
    output_enabled: Boolean(preferences.output_enabled),
    recognition_enabled: Boolean(preferences.recognition_enabled)
  };
};

export const voiceRemainingSeconds = (status, now = Date.now()) => {
  if (!status?.capture_armed || !status?.expires_at) return 0;
  const expiresAt = Date.parse(status.expires_at);
  if (!Number.isFinite(expiresAt)) return Number(status.remaining_seconds) || 0;
  return Math.max(0, Math.ceil((expiresAt - now) / 1000));
};

export const voiceStatusPresentation = (status, now = Date.now()) => {
  if (!status?.output_enabled && !status?.recognition_enabled) {
    return { text: '语音已关闭', color: 'default', state: 'off' };
  }
  if (status?.recognition_enabled && !status?.recognition_available) {
    return { text: '语音识别不可用', color: 'error', state: 'error' };
  }
  if (status?.recognition_enabled && !status?.listening) {
    return { text: '麦克风未启动', color: 'warning', state: 'error' };
  }
  if (status?.capture_armed) {
    const remaining = voiceRemainingSeconds(status, now);
    return {
      text: `采集语音待命 ${remaining} 秒`,
      color: remaining > 0 ? 'processing' : 'warning',
      state: 'armed'
    };
  }
  if (status?.output_enabled && !status?.output_available) {
    return { text: '语音播报不可用', color: 'error', state: 'error' };
  }
  if (status?.speaking) {
    return { text: '正在语音播报', color: 'processing', state: 'speaking' };
  }
  if (status?.activity) {
    return { text: '正在聆听', color: 'processing', state: 'listening' };
  }
  if (status?.recognition_enabled && status?.listening) {
    return { text: '语音命令就绪', color: 'success', state: 'ready' };
  }
  if (status?.output_enabled) {
    return { text: '语音播报已开启', color: 'success', state: 'output-only' };
  }
  return { text: '麦克风已关闭', color: 'default', state: 'mic-off' };
};
