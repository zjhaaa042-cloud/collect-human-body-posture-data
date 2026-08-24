import React from 'react';

const cameraName = (code) => code === 'CD435I' ? 'Intel RealSense D435i' : 'Orbbec Gemini 336L';

export default function ConditionDetails({ condition }) {
  if (!condition) return null;
  const items = [
    ['相机', cameraName(condition.camera_code)],
    ['距离', condition.distance_mm ? `${(condition.distance_mm / 1000).toFixed(1)} m` : '--'],
    ['朝向', `${condition.view_yaw_deg ?? '--'}°`],
    ['光照', condition.light_id || 'LSTD'],
    ['姿态', condition.pose_id || 'P1'],
    ['服装', condition.clothing_id || 'CF'],
    ['站位', `R${String(condition.repeat_id || 1).padStart(2, '0')}`]
  ];
  return (
    <dl className="condition-details">
      {items.map(([term, value]) => (
        <div key={term}><dt>{term}</dt><dd>{value}</dd></div>
      ))}
    </dl>
  );
}
