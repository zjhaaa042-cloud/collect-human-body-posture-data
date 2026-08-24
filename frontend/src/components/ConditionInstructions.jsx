import React from 'react';
import { InfoCircleOutlined } from '@ant-design/icons';
import { Alert, Typography } from 'antd';

const { Text } = Typography;

const poseFallback = {
  P1: '双脚约肩宽并直立，双臂离躯干 20–30°，肘自然伸直，保持正常呼吸。',
  P2: '自然直立，双臂自然下垂，不刻意展开。',
  P3: '在 P1 基础上将双臂展开约 40–45°，保持左右对称。'
};

const clothingFallback = {
  CF: '穿着规范贴身采集服，整理褶皱并确认身体轮廓无遮挡。',
  CN: '保持当前日常服装，不增减外套或配饰。'
};

const viewFallback = (yaw) => {
  if (Number(yaw) === 0) return '正面朝向相机，脚尖与身体中线对准 V000 标记。';
  if (Number(yaw) === 180) return '背面朝向相机，脚尖与身体中线对准 V180 标记。';
  return `脚尖与身体中线对准现场 V${String(yaw ?? 0).padStart(3, '0')} 朝向标记。`;
};

const catalogCondition = (condition, catalog) => {
  const all = [
    ...(catalog?.conditions || []),
    ...(catalog?.profiles || []).flatMap((profile) => profile.conditions || [])
  ];
  return all.find((item) => item.condition_id === condition?.condition_id) || {};
};

export default function ConditionInstructions({ condition, catalog }) {
  if (!condition) return null;
  const fromCatalog = catalogCondition(condition, catalog);
  const instructions = catalog?.instructions?.[condition.condition_id] || {};
  const get = (key) => condition[key] || fromCatalog[key] || instructions[key];
  const items = [
    ['朝向动作', get('view_instruction') || viewFallback(condition.view_yaw_deg)],
    ['姿态动作', get('pose_instruction') || poseFallback[condition.pose_id] || `执行现场 ${condition.pose_id} 姿态卡动作。`],
    ['服装检查', get('clothing_instruction') || clothingFallback[condition.clothing_id] || `按现场 ${condition.clothing_id} 服装规范整理。`]
  ];
  return (
    <Alert
      type="info"
      showIcon
      icon={<InfoCircleOutlined />}
      message="本条件执行口令"
      description={<ul className="condition-instructions">{items.map(([label, text]) => <li key={label}><Text strong>{label}：</Text>{text}</li>)}</ul>}
    />
  );
}
