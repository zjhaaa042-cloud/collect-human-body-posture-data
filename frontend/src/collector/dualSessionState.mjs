export const ACTIVE_DUAL_SESSION_KEY = 'bodyCollectorActiveDualSession';

export const readActiveDualSession = (storage = window.localStorage) => {
  try {
    return JSON.parse(storage.getItem(ACTIVE_DUAL_SESSION_KEY) || 'null') || null;
  } catch {
    return null;
  }
};

export const activeDualSessionRecord = (state = {}) => ({
  subject_id: state.subject_id,
  output_path: state.output_directory
    || (state.output_root ? state.output_root.replace(/[\\/]body_posture_dual_v2$/, '') : '')
});

export const persistActiveDualSession = (state, storage = window.localStorage) => {
  const record = activeDualSessionRecord(state);
  if (record.subject_id && record.output_path) {
    storage.setItem(ACTIVE_DUAL_SESSION_KEY, JSON.stringify(record));
    return record;
  }
  return null;
};

export const clearActiveDualSession = (storage = window.localStorage) => {
  storage.removeItem(ACTIVE_DUAL_SESSION_KEY);
};

export const dualWriteBlocked = (state) => Boolean(
  state?.reconciliation_required
  || String(state?.integrity?.status || '').toUpperCase() === 'ERROR'
  || String(state?.status || '').toUpperCase() === 'COMPLETE'
);

export const dualIntegrityMessage = (state) => {
  if (!state) return '';
  const errors = state.integrity?.errors || state.recovery_report?.errors || [];
  if (errors.length) return errors.join('；');
  if (state.reconciliation_required) return '状态账本或采集文件需要恢复，修复前已禁止继续写入。';
  const recovered = Number(state.recovery_report?.recovered_attempts || 0);
  const promoted = Number(state.recovery_report?.promoted_staging || 0);
  if (recovered || promoted) {
    return `已自动恢复 ${recovered} 个采集记录，其中 ${promoted} 个从 staging 提升。`;
  }
  return '';
};

export const reduceDualSessionEvent = (previous, type, payload = {}) => {
  if (payload.success === false) return previous;
  if (type === 'dual_session_state') return payload;
  if (['dual_capture_result', 'dual_anthropometry_result', 'dual_completion_result'].includes(type)) {
    return payload.state || previous;
  }
  return previous;
};
