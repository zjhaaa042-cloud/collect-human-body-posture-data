import React from 'react';
import { Alert, Tag, Typography } from 'antd';

const { Text } = Typography;

const asItems = (value) => {
  if (Array.isArray(value)) return value;
  if (value && typeof value === 'object') {
    return Object.entries(value).map(([code, detail]) => ({ code, detail }));
  }
  return value === null || value === undefined || value === '' ? [] : [value];
};

const describe = (item, fallback) => {
  if (['string', 'number', 'boolean'].includes(typeof item)) return String(item);
  const code = item?.code || item?.reason_code || item?.check_id || item?.name || fallback;
  const detail = item?.message || item?.detail || item?.description || item?.value;
  return detail ? `${code}：${detail}` : String(code);
};

export default function CaptureFeedback({ condition, result }) {
  const matches = result?.condition_id === condition?.condition_id;
  const qc = (matches ? result?.qc : null) || condition?.latest_qc || condition?.qc;
  if (!qc) return null;

  const hardErrors = asItems(qc.hard_errors || qc.errors);
  const warnings = asItems(qc.warnings);
  const reasonCodes = asItems(qc.reason_codes || qc.warning_codes);
  const attemptId = (matches && result?.attempt_id) || condition?.latest_attempt_id;
  const anchor = qc.anchor_frame_id || qc.anchor_frame || (matches && result?.anchor_frame) || 'F03';
  const failed = hardErrors.length > 0 || String((matches && result?.quality_status) || qc.status).toUpperCase() === 'FAIL';
  if (!hardErrors.length && !warnings.length && !reasonCodes.length && !failed) return null;

  return (
    <section className="capture-feedback" aria-label="最近一次采集质量反馈">
      <Alert
        type={failed ? 'error' : 'warning'}
        showIcon
        message={failed ? '最近一次 attempt 未通过，请按原因补采' : '最近一次 attempt 有质量提示'}
        description={(
          <>
            <div className="capture-feedback-meta">
              <Text>Attempt：<Text code>{attemptId || '服务端暂未返回'}</Text></Text>
              <Tag>{`Anchor ${anchor}`}</Tag>
            </div>
            {hardErrors.length > 0 && <ul>{hardErrors.map((item, index) => <li key={`error-${index}`}>{describe(item, `ERROR_${index + 1}`)}</li>)}</ul>}
            {warnings.length > 0 && <ul>{warnings.map((item, index) => <li key={`warning-${index}`}>{describe(item, `WARN_${index + 1}`)}</li>)}</ul>}
            {reasonCodes.length > 0 && <div className="capture-feedback-codes">{reasonCodes.map((item, index) => <Tag color={failed ? 'error' : 'warning'} key={`code-${index}`}>{describe(item, `REASON_${index + 1}`)}</Tag>)}</div>}
          </>
        )}
      />
    </section>
  );
}
