import React, { useEffect, useMemo, useState } from 'react';
import { CheckCircleOutlined, SwapOutlined } from '@ant-design/icons';
import { Alert, Checkbox, Tag, Typography } from 'antd';
import { setupChanges } from '../protocol/workflowGroups.mjs';

const { Text } = Typography;

export default function ConditionReadyCheck({ condition, previousCondition, value, onChange }) {
  const [acknowledged, setAcknowledged] = useState(false);
  const changes = useMemo(
    () => setupChanges(previousCondition, condition),
    [condition, previousCondition]
  );
  const repositioning = Number(condition?.repeat_id || 1) > 1;

  useEffect(() => setAcknowledged(false), [condition?.condition_id]);

  const copy = repositioning
    ? '我已确认受试者完全离开站位后重新进入，并已重新核对脚位、姿态与全身入框。'
    : changes.length
      ? `我已按本条件完成${changes.join('、')}调整，并确认全身完整入框。`
      : '我已确认现场状态与上一条件一致，且全身完整入框。';

  return (
    <fieldset className="condition-ready-check">
      <legend>采集前确认</legend>
      {changes.length > 0 && (
        <div className="condition-change-summary" role="status">
          <Text type="secondary">本条件变化</Text>
          <span>{changes.map((item) => <Tag key={item}>{item}</Tag>)}</span>
        </div>
      )}
      {repositioning && (
        <Alert
          type="warning"
          showIcon
          icon={<SwapOutlined />}
          message="独立重新站位"
          description="本条件不能沿用上一站位；请让受试者离开脚位后重新进入。"
        />
      )}
      <Checkbox
        checked={acknowledged}
        onChange={(event) => {
          const next = event.target.checked;
          setAcknowledged(next);
          onChange(next ? {
            distance_marker: true,
            pose_view_clothing: true,
            full_body_visible: true,
            ...(repositioning ? { repositioned: true } : {}),
            confirmation_mode: 'single_ready_check',
            confirmation_summary: copy
          } : {});
        }}
      >
        <CheckCircleOutlined /> {copy}
      </Checkbox>
      {!value && <Text type="warning">确认后才能开始本次五帧采集。</Text>}
    </fieldset>
  );
}
