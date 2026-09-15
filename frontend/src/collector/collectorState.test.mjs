import test from 'node:test';
import assert from 'node:assert/strict';

import { backendUrls, reconnectDelayMs } from './collectorTransport.mjs';
import {
  activeDualSessionRecord,
  dualCaptureWriteBlocked,
  dualIntegrityMessage,
  dualWriteBlocked,
  reduceDualSessionEvent
} from './dualSessionState.mjs';

test('连接地址跟随页面协议并限制重连退避', () => {
  assert.deepEqual(backendUrls('localhost:8765', 'http:'), {
    token: 'http://localhost:8765/auth-token',
    socket: 'ws://localhost:8765'
  });
  assert.equal(backendUrls('example.test', 'https:').socket, 'wss://example.test');
  assert.equal(reconnectDelayMs(0), 1000);
  assert.equal(reconnectDelayMs(99), 10000);
});

test('双机恢复记录保持原输出目录', () => {
  assert.deepEqual(activeDualSessionRecord({
    subject_id: 'S0008', output_root: 'D:\\data\\body_posture_dual_v2'
  }), { subject_id: 'S0008', output_path: 'D:\\data' });
});

test('仅完整性异常会阻止双机写入；完成任务仍可修订人体测量', () => {
  assert.equal(dualWriteBlocked({ reconciliation_required: true }), true);
  assert.equal(dualWriteBlocked({ integrity: { status: 'ERROR' } }), true);
  assert.equal(dualWriteBlocked({ status: 'COMPLETE' }), false);
  assert.equal(dualCaptureWriteBlocked({ status: 'COMPLETE' }), true);
  assert.equal(dualWriteBlocked({ status: 'ACTIVE', integrity: { status: 'OK' } }), false);
  assert.match(
    dualIntegrityMessage({ recovery_report: { recovered_attempts: 1, promoted_staging: 1 } }),
    /自动恢复/
  );
});

test('纯 reducer 只接受成功的双机状态', () => {
  const previous = { subject_id: 'S0001' };
  const next = { subject_id: 'S0001', progress: { captured: 1 } };
  assert.equal(
    reduceDualSessionEvent(previous, 'dual_capture_result', { success: true, state: next }),
    next
  );
  assert.equal(
    reduceDualSessionEvent(previous, 'dual_capture_result', { success: false, state: next }),
    previous
  );
});

test('写入异常持续显示，历史恢复信息不能冒充本次恢复', () => {
  const state = { capture_error: '角度 45° 写入异常：磁盘写入失败', recovery_report: { recovered_attempts: 1 } };
  assert.match(dualIntegrityMessage(state), /磁盘写入失败/);
  assert.doesNotMatch(dualIntegrityMessage(state), /本次角度数据已校验恢复/);
  assert.match(dualIntegrityMessage({ ...state, capture_recovered: true }), /本次角度数据已校验恢复/);
  assert.match(dualIntegrityMessage({ ...state, integrity: { errors: ['部分文件缺失'] } }), /磁盘写入失败.*部分文件缺失/);
  const updated = reduceDualSessionEvent({}, 'dual_session_state', { ...state, event: 'write_failed' });
  assert.equal(updated.capture_error, state.capture_error);
});
