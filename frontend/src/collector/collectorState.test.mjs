import test from 'node:test';
import assert from 'node:assert/strict';

import { backendUrls, reconnectDelayMs } from './collectorTransport.mjs';
import {
  activeDualSessionRecord,
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

test('完整性错误和完成状态都会阻止写入', () => {
  assert.equal(dualWriteBlocked({ reconciliation_required: true }), true);
  assert.equal(dualWriteBlocked({ integrity: { status: 'ERROR' } }), true);
  assert.equal(dualWriteBlocked({ status: 'COMPLETE' }), true);
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
