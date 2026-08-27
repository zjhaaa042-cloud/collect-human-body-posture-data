import React, { useEffect, useMemo, useRef, useState } from 'react';
import { CameraOutlined } from '@ant-design/icons';
import { Alert, Button, Empty, Progress, Tag, Typography } from 'antd';
import ConditionReadyCheck from './ConditionReadyCheck';
import CaptureFeedback from './CaptureFeedback';
import ConditionList from './ConditionList';
import ConditionDetails from './ConditionDetails';
import ConditionInstructions from './ConditionInstructions';
import ReviewPanel from './ReviewPanel';
import RetakeDialog from './RetakeDialog';
import ReviewQueue from './ReviewQueue';
import { conditionLabel, getReviewContext, inferCameraCode } from '../protocol/protocolUtils.mjs';
import { conditionStatus, isCaptured } from '../protocol/conditionStatus.jsx';
import { buildWorkflowGroups, workflowGroupFor } from '../protocol/workflowGroups.mjs';

const { Text, Title } = Typography;

const cameraName = (code) => code === 'CD435I' ? 'Intel RealSense D435i' : 'Orbbec Gemini 336L';
export default function ConditionRunner({
  state, catalog, cameraStatus, lastCaptureResult, reviewPreview, reviewPreviewLoading,
  reviewPreviewError, busyAction, selectedId, onCapture, onReview,
  onRequestReviewPreview, onSelect
}) {
  const conditions = state?.conditions || [];
  const nextConditionId = state?.next_condition_id || conditions.find((condition) => !isCaptured(condition.status))?.condition_id;
  const [retakeOpen, setRetakeOpen] = useState(false);
  const [confirmations, setConfirmations] = useState({});
  const requestedPreviewRef = useRef('');
  const effectiveSelectedId = selectedId || nextConditionId || conditions[0]?.condition_id || '';

  const selected = useMemo(
    () => conditions.find((condition) => condition.condition_id === effectiveSelectedId) || conditions[0],
    [conditions, effectiveSelectedId]
  );
  const attemptCount = selected?.attempt_ids?.length || 0;
  const confirmationNonce = selected?.confirmation_nonce || selected?.confirmations?.nonce || null;
  useEffect(() => {
    setRetakeOpen(false);
    setConfirmations({});
  }, [effectiveSelectedId, selected?.status, attemptCount, confirmationNonce]);
  const selectedIndex = conditions.findIndex((condition) => condition.condition_id === selected?.condition_id);
  const previousCondition = selectedIndex > 0 ? conditions[selectedIndex - 1] : null;
  const groups = useMemo(() => buildWorkflowGroups(conditions), [conditions]);
  const selectedGroup = workflowGroupFor(selected);
  const group = groups.find((item) => item.id === selectedGroup.id);
  const groupCaptured = group?.conditions.filter((item) => isCaptured(item.status)).length || 0;
  const next = conditions.find((condition) => condition.condition_id === nextConditionId);
  const progress = state?.progress || {};
  const captured = progress.captured ?? conditions.filter((item) => isCaptured(item.status)).length;
  const expected = progress.expected ?? conditions.length;
  const percent = progress.percent ?? (expected ? Math.round((captured / expected) * 100) : 0);
  const connectedCameraCode = inferCameraCode(cameraStatus);
  const cameraMismatch = Boolean(
    cameraStatus?.connected
    && selected?.camera_code
    && selected.camera_code !== connectedCameraCode
  );
  const cameraReady = Boolean(
    cameraStatus?.connected
    && selected?.camera_code
    && selected.camera_code === connectedCameraCode
  );
  const outOfSequence = Boolean(
    nextConditionId
    && selected?.condition_id !== nextConditionId
    && !isCaptured(selected?.status)
  );
  const selectedStatus = conditionStatus(selected?.status);
  const reviewContext = useMemo(() => getReviewContext(selected, lastCaptureResult), [lastCaptureResult, selected]);
  const reconciliationRequired = Boolean(
    state?.reconciliation_required || reviewContext.reconciliationRequired
  );
  const reviewPreviewMatches = Boolean(
    reviewPreview
    && reviewPreview.source === 'verified_committed_files'
    && String(reviewPreview.condition_id) === String(selected?.condition_id)
    && String(reviewPreview.attempt_id) === String(reviewContext.attemptId)
  );
  useEffect(() => {
    if (!reviewContext.required) {
      requestedPreviewRef.current = '';
      return;
    }
    if (!reviewContext.attemptId || reviewPreviewMatches) return;
    const key = `${state?.subject_id}:${selected?.condition_id}:${reviewContext.attemptId}`;
    if (requestedPreviewRef.current !== key) {
      requestedPreviewRef.current = key;
      onRequestReviewPreview?.(selected.condition_id, reviewContext.attemptId);
    }
  }, [onRequestReviewPreview, reviewContext.attemptId, reviewContext.required, reviewPreviewMatches, selected?.condition_id, state?.subject_id]);
  const requiredConfirmationKeys = selected?.repeat_id > 1
    ? ['distance_marker', 'pose_view_clothing', 'full_body_visible', 'repositioned']
    : ['distance_marker', 'pose_view_clothing', 'full_body_visible'];
  const allConfirmed = requiredConfirmationKeys.every((key) => confirmations[key] === true);
  const confirmationPayload = () => ({
    ...Object.fromEntries(requiredConfirmationKeys.map((key) => [key, confirmations[key]])),
    ...(confirmationNonce ? { nonce: confirmationNonce } : {})
  });
  const submitCapture = (metadata = {}) => {
    const sent = onCapture(selected.condition_id, confirmationPayload(), metadata);
    if (sent !== false) setConfirmations({});
    return sent;
  };

  if (!state?.subject_id) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="请先建立或选择受试者任务" />;
  }

  if (!conditions.length) {
    return (
      <Alert
        type="warning"
        showIcon
        message="当前受试者尚未获得条件矩阵"
        description="请确认服务端已返回 protocol_subject_state.conditions。现有后端未升级时不会开放采集按钮。"
      />
    );
  }

  return (
    <section aria-labelledby="runner-heading">
      <div className="section-heading-row">
        <Title level={3} id="runner-heading">2. 条件采集</Title>
        <Text strong className="protocol-progress-count">{captured}/{expected}</Text>
      </div>
      <Progress percent={Math.min(Math.max(percent, 0), 100)} status={percent >= 100 ? 'success' : 'active'} />
      <div className="workflow-group-summary" aria-label="当前采集组进度">
        <Text type="secondary">当前任务组</Text>
        <Text strong>{selectedGroup.title}</Text>
        <Text type="secondary">{groupCaptured}/{group?.conditions.length || 0} · {selectedGroup.description}</Text>
      </div>

      <ReviewQueue conditions={conditions} selectedId={selected?.condition_id} onSelect={onSelect} />

      <div className="condition-focus-card" aria-live="polite">
        <div className="condition-focus-header">
          <div>
            <Text type="secondary">当前选中条件</Text>
            <Title level={4}>{selected?.condition_id || '--'}</Title>
          </div>
          <Tag color={selectedStatus.color} icon={selectedStatus.icon}>{selectedStatus.text}</Tag>
        </div>
        <ConditionDetails condition={selected} />
        <ConditionInstructions condition={selected} catalog={catalog} />
        {!reviewContext.required && !reconciliationRequired && <Alert
          type={cameraMismatch ? 'error' : 'info'}
          showIcon
          message={`本条件必须使用 ${cameraName(selected?.camera_code)}`}
          description={cameraMismatch ? `当前识别到 ${cameraName(connectedCameraCode)}，请先切换相机。` : '按下采集后等待 2 秒稳定，再保存 RGB、raw/aligned Depth、IR 的 5 帧同步 burst；F03 为 anchor。'}
        />}
        {reconciliationRequired ? (
          <Alert
            type="error"
            showIcon
            message="文件已落盘，但状态账本待恢复"
            description="请停止当前任务操作并重启采集服务。不要重复采集、复核或保存人体测量；重启后系统会从 durable sidecar 恢复。"
          />
        ) : reviewContext.required ? (
          <ReviewPanel
            context={reviewContext}
            conditionId={selected.condition_id}
            preview={reviewPreviewMatches ? reviewPreview : null}
            previewLoading={reviewPreviewLoading}
            previewError={reviewPreviewError}
            busyAction={busyAction}
            onReview={onReview}
            onRequestPreview={onRequestReviewPreview}
          />
        ) : <>
        <CaptureFeedback condition={selected} result={lastCaptureResult} />
        <ConditionReadyCheck
          condition={selected}
          previousCondition={previousCondition}
          value={allConfirmed}
          onChange={setConfirmations}
        />
        {!confirmationNonce && <Text type="warning" role="status">兼容模式：服务端暂未返回 confirmation_nonce；新版服务端会要求刷新状态后重新确认。</Text>}
        <Button
          type="primary"
          size="large"
          block
          icon={<CameraOutlined />}
          loading={busyAction === 'capture'}
          disabled={!cameraReady || !allConfirmed || outOfSequence || ['COMPLETE', 'CORRUPTED'].includes(String(state.status).toUpperCase())}
          onClick={() => isCaptured(selected?.status)
            ? setRetakeOpen(true)
            : submitCapture()}
        >
          {isCaptured(selected?.status) ? '填写原因后重采此已通过条件' : '采集此条件的 5 帧 burst'}
        </Button>
        {!cameraStatus?.connected && <Text type="danger">采集按钮已锁定：请先连接正确摄像头。</Text>}
        {!allConfirmed && <Text type="warning">采集按钮已锁定：请完成本条件的一次就位确认。</Text>}
        {outOfSequence && <Text type="warning">采集按钮已锁定：请先完成协议下一项；已完成条件仍可只追加补采。</Text>}
        </>}
        <RetakeDialog
          open={retakeOpen && !reconciliationRequired}
          condition={selected}
          busyAction={busyAction}
          onCancel={() => setRetakeOpen(false)}
          onConfirm={submitCapture}
        />
      </div>

      <div className="next-condition-line">
        <Text type="secondary">协议下一项</Text>
        <Text strong>{next ? `${next.condition_id} · ${conditionLabel(next)}` : '所有条件均已处理'}</Text>
      </div>
      <ConditionList conditions={conditions} expected={expected} selectedId={selected?.condition_id} onSelect={onSelect} />
    </section>
  );
}
