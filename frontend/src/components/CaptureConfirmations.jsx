import React from 'react';
import { Checkbox } from 'antd';

const BASE_ITEMS = [
  ['distance_marker', '受试者站在本条件指定距离的地面标记处'],
  ['pose_view_clothing', '朝向、姿态与服装均和条件标签一致'],
  ['full_body_visible', '实时画面中头顶、双脚和身体轮廓均完整可见']
];

export default function CaptureConfirmations({ repeatId, values, onChange }) {
  const items = repeatId > 1
    ? [...BASE_ITEMS, ['repositioned', '受试者已完全离开站位后重新进入，并重新对齐脚位']]
    : BASE_ITEMS;

  return (
    <fieldset className="capture-confirmations">
      <legend>采集前人工确认（每个 attempt 必须重新确认）</legend>
      {items.map(([key, label]) => (
        <Checkbox
          key={key}
          checked={Boolean(values[key])}
          onChange={(event) => onChange(key, event.target.checked)}
        >
          {label}
        </Checkbox>
      ))}
    </fieldset>
  );
}
