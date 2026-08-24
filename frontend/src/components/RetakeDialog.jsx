import React, { useEffect, useRef, useState } from 'react';
import { Alert, Input, Modal, Radio, Space, Typography } from 'antd';
import { createRetakeMetadata } from '../protocol/protocolUtils.mjs';

const { Text } = Typography;

export default function RetakeDialog({ open, condition, busyAction, onCancel, onConfirm }) {
  const [reason, setReason] = useState('');
  const [invalidatePrior, setInvalidatePrior] = useState(null);
  const [error, setError] = useState('');
  const reasonRef = useRef(null);
  const targetAttemptId = condition?.accepted_attempt_id || null;

  useEffect(() => {
    if (open) {
      setReason('');
      setInvalidatePrior(null);
      setError('');
    }
  }, [condition?.condition_id, open]);

  const submit = () => {
    try {
      const metadata = createRetakeMetadata(targetAttemptId, invalidatePrior, reason);
      setError('');
      const sent = onConfirm(metadata);
      if (sent !== false) onCancel();
    } catch (validationError) {
      setError(validationError.message);
      reasonRef.current?.focus();
    }
  };

  return (
    <Modal
      title={`重采已通过条件 ${condition?.condition_id || ''}`}
      open={open}
      onCancel={onCancel}
      onOk={submit}
      okText="确认并开始重采"
      cancelText="取消"
      confirmLoading={busyAction === 'capture'}
      okButtonProps={{ disabled: !targetAttemptId }}
      destroyOnHidden
      afterOpenChange={(visible) => visible && window.setTimeout(() => reasonRef.current?.focus(), 0)}
    >
      <Space direction="vertical" size={12} className="retake-dialog-body">
        <Alert
          type="warning"
          showIcon
          message="重采只追加新 attempt，不会静默覆盖旧数据"
          description={`目标旧 attempt：${targetAttemptId || '服务端尚未返回 accepted_attempt_id'}`}
        />
        <div className="retake-reason-field">
          <label htmlFor="retake-reason">重采原因（必填）</label>
          <Input.TextArea
            id="retake-reason"
            ref={reasonRef}
            rows={3}
            maxLength={500}
            showCount
            value={reason}
            status={error ? 'error' : undefined}
            aria-describedby={error ? 'retake-error' : undefined}
            placeholder="例如：发现旧 attempt 的 F03 左脚被遮挡，需要重新采集。"
            onChange={(event) => setReason(event.target.value)}
          />
        </div>
        <fieldset className="retake-disposition">
          <legend>旧数据处理方式（必须二选一）</legend>
          <Radio.Group value={invalidatePrior} onChange={(event) => setInvalidatePrior(event.target.value)}>
            <Space direction="vertical">
              <Radio value={true}><strong>旧数据已确认无效，立即作废</strong></Radio>
              <Radio value={false}><strong>仅做额外复采，旧数据暂时有效</strong></Radio>
            </Space>
          </Radio.Group>
        </fieldset>
        {invalidatePrior === true && <Alert type="error" showIcon message="提交后旧 attempt 将被标记为无效，请确认复核依据充分。" />}
        {error && <Text id="retake-error" type="danger" role="alert">{error}</Text>}
      </Space>
    </Modal>
  );
}
