import React, { useEffect, useRef, useState } from 'react';
import { CheckOutlined, CloseOutlined, WarningOutlined } from '@ant-design/icons';
import { Alert, Button, Input, Skeleton, Space, Tag, Typography } from 'antd';

const { Text } = Typography;

const imageSource = (value, mimeType = 'image/jpeg') => {
  if (!value) return null;
  return String(value).startsWith('data:') ? String(value) : `data:${mimeType || 'image/jpeg'};base64,${value}`;
};

const itemParts = (item, fallback) => {
  if (['string', 'number', 'boolean'].includes(typeof item)) {
    return { code: String(item), detail: '' };
  }
  return {
    code: String(item?.code || item?.reason_code || item?.check_id || item?.name || fallback),
    detail: String(item?.message || item?.detail || item?.description || item?.value || '')
  };
};

export default function ReviewPanel({
  context, conditionId, preview, previewLoading, previewError,
  busyAction, onReview, onRequestPreview
}) {
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const [imageStatus, setImageStatus] = useState({ color: 'idle', depth: 'idle' });
  const reasonRef = useRef(null);
  const colorSrc = imageSource(preview?.color, preview?.color_mime_type);
  const depthSrc = imageSource(preview?.depth, preview?.depth_mime_type);

  useEffect(() => {
    setReason('');
    setError('');
  }, [context.attemptId]);

  useEffect(() => {
    setImageStatus({
      color: colorSrc ? 'loading' : 'idle',
      depth: depthSrc ? 'loading' : 'idle'
    });
  }, [colorSrc, context.attemptId, depthSrc]);

  if (!context.required) return null;

  const submit = (decision) => {
    if (decision === 'ACCEPT' && !evidenceReady) {
      setError('必须先成功加载该 attempt 已保存的 F03 RGB 与深度证据，才能接受。');
      return;
    }
    const normalized = reason.trim();
    if (normalized.length < 5) {
      setError('请填写至少 5 个字的复核依据，说明接受或驳回的现场判断。');
      reasonRef.current?.focus();
      return;
    }
    setError('');
    onReview(conditionId, context.attemptId, decision, normalized);
  };
  const hasDetails = context.reasonCodes.length || context.checks.length || context.warnings.length;
  const evidenceReady = Boolean(
    preview?.source === 'verified_committed_files'
    && colorSrc
    && depthSrc
    && imageStatus.color === 'loaded'
    && imageStatus.depth === 'loaded'
  );
  const evidenceFailed = imageStatus.color === 'error' || imageStatus.depth === 'error';

  return (
    <section className="review-panel" aria-labelledby="review-heading">
      <Alert
        type="warning"
        showIcon
        icon={<WarningOutlined />}
        message={<span id="review-heading">启发式 QC 待人工复核</span>}
        description="该 attempt 已保存；Pilot 阈值尚未冻结，WARN 不等于失败。请先人工接受或驳回，不能直接反复补采。"
      />
      <div className="review-meta">
        <Text type="secondary">Attempt</Text>
        <Text code>{context.attemptId || '服务端暂未返回'}</Text>
        {context.policyVersion && <Tag>{context.policyVersion}</Tag>}
      </div>
      <section className="review-evidence" aria-labelledby="review-evidence-heading">
        <Text strong id="review-evidence-heading">已保存且经哈希验证的 {preview?.anchor_frame || 'F03'} 复核证据（非实时画面）</Text>
        {previewLoading && !colorSrc && !depthSrc && <div className="review-evidence-loading" role="status"><Skeleton.Image active /><Text>正在读取已落盘证据…</Text></div>}
        {(colorSrc || depthSrc) && (
          <div className="review-evidence-grid">
            {colorSrc && <figure><img src={colorSrc} alt={`已保存的 ${preview?.anchor_frame || 'F03'} RGB 证据`} onLoad={() => setImageStatus((value) => ({ ...value, color: 'loaded' }))} onError={() => setImageStatus((value) => ({ ...value, color: 'error' }))} /><figcaption>RGB · {preview?.anchor_frame || 'F03'}</figcaption></figure>}
            {depthSrc && <figure><img src={depthSrc} alt={`已保存的 ${preview?.anchor_frame || 'F03'} 深度证据`} onLoad={() => setImageStatus((value) => ({ ...value, depth: 'loaded' }))} onError={() => setImageStatus((value) => ({ ...value, depth: 'error' }))} /><figcaption>Depth · {preview?.anchor_frame || 'F03'}</figcaption></figure>}
          </div>
        )}
        {colorSrc && depthSrc && !evidenceReady && !evidenceFailed && <Text type="secondary" role="status">正在验证两张证据图可读取…</Text>}
        {(previewError || evidenceFailed || (!previewLoading && (!colorSrc || !depthSrc))) && (
          <Alert
            type="error"
            showIcon
            message="已保存的 RGB/Depth 证据不完整，禁止接受"
            description={previewError || (evidenceFailed ? '至少一张证据图无法解码。' : '必须从该 attempt 读取 F03 的 RGB 与深度图。')}
            action={<Button size="small" disabled={!context.attemptId} onClick={() => onRequestPreview?.(conditionId, context.attemptId)}>重新读取</Button>}
          />
        )}
      </section>
      {context.reasonCodes.length > 0 && (
        <div className="review-detail-block">
          <Text strong>Reason codes</Text>
          <div className="review-code-list">
            {context.reasonCodes.map((item, index) => {
              const { code, detail } = itemParts(item, `REASON_${index + 1}`);
              return <Tag color="warning" key={`${code}-${index}`}>{code}{detail ? ` · ${detail}` : ''}</Tag>;
            })}
          </div>
        </div>
      )}
      {context.checks.length > 0 && (
        <div className="review-detail-block">
          <Text strong>QC checks</Text>
          <ul className="review-check-list">
            {context.checks.map((item, index) => {
              const { code, detail } = itemParts(item, `CHECK_${index + 1}`);
              const status = String(item?.status || item?.result || 'WARN').toUpperCase();
              return <li key={`${code}-${index}`}><Tag color={status === 'PASS' ? 'success' : 'warning'}>{status}</Tag><span><strong>{code}</strong>{detail ? `：${detail}` : ''}</span></li>;
            })}
          </ul>
        </div>
      )}
      {context.warnings.length > 0 && (
        <div className="review-detail-block">
          <Text strong>告警说明</Text>
          <ul className="review-warning-list">
            {context.warnings.map((item, index) => {
              const { code, detail } = itemParts(item, `WARN_${index + 1}`);
              return <li key={`${code}-${index}`}>{detail || code}</li>;
            })}
          </ul>
        </div>
      )}
      {!hasDetails && <Alert type="info" showIcon message="服务端尚未返回结构化 reason codes/checks，请结合实时预览与现场情况复核。" />}
      {!context.attemptId && <Alert type="error" showIcon message="缺少 attempt_id，复核操作已锁定；请刷新受试者状态。" />}
      <div className="review-reason-field">
        <label htmlFor="review-reason">复核依据（必填）</label>
        <Input.TextArea
          id="review-reason"
          ref={reasonRef}
          rows={3}
          value={reason}
          maxLength={500}
          showCount
          status={error ? 'error' : undefined}
          aria-describedby={error ? 'review-reason-error' : undefined}
          placeholder="例如：人工确认头脚完整入框，亮度告警来自深色服装，接受本次数据。"
          onChange={(event) => setReason(event.target.value)}
          onBlur={() => reason.trim() && reason.trim().length < 5 && setError('复核依据至少填写 5 个字。')}
        />
        {error && <Text id="review-reason-error" type="danger" role="alert">{error}</Text>}
      </div>
      <Space wrap className="review-actions">
        <Button type="primary" icon={<CheckOutlined />} loading={busyAction === 'review'} disabled={!context.attemptId || !evidenceReady} onClick={() => submit('ACCEPT')}>接受本次 attempt</Button>
        <Button danger icon={<CloseOutlined />} loading={busyAction === 'review'} disabled={!context.attemptId} onClick={() => submit('REJECT')}>驳回并进入补采</Button>
      </Space>
    </section>
  );
}
