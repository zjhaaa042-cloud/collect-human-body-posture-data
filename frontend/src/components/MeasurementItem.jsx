import React from 'react';
import { Alert, InputNumber, Tag, Typography } from 'antd';
import {
  expandMeasurements,
  needsThirdReading,
  recordKey,
  reduceMeasurementReadings
} from '../protocol/protocolUtils.mjs';

const { Text } = Typography;
const EQUIPMENT_LABELS = {
  stadiometer: '身高计',
  calibrated_scale: '校准电子秤',
  anthropometer_or_large_sliding_caliper: 'Anthropometer / 大型滑动卡尺',
  non_stretch_tape: '无弹性人体测量软尺',
  anthropometer_or_wall_scale: 'Anthropometer / 墙面标尺',
  anthropometer_or_non_stretch_tape: 'Anthropometer / 无弹性软尺'
};

export default function MeasurementItem({ definition, draft, errors, onChange }) {
  const rows = expandMeasurements([definition]);
  const threshold = definition.third_measurement_threshold;

  return (
    <fieldset className={`measurement-item ${definition.required ? 'required' : 'optional'}`}>
      <legend>
        <span>{definition.measurement_id} · {definition.display_name_zh}</span>
        <Tag color={definition.required ? 'blue' : 'default'}>{definition.required ? '必填' : '选填'}</Tag>
      </legend>
      <div className="measurement-rule">
        <Text type="secondary">
          单位：{definition.unit}；前两次需分别重新定位；{threshold == null ? '规范未设置强制第三测阈值' : `差值 > ${threshold}${definition.unit} 时必须第三测`}
        </Text>
        <Text type="secondary">
          工具：{(definition.required_equipment || []).map((item) => EQUIPMENT_LABELS[item] || item).join('、') || '按现场 SOP'}
        </Text>
        {definition.protocol_note && <Text type="secondary">{definition.protocol_note}</Text>}
      </div>
      <div className="reading-column-headings" aria-hidden="true">
        <span>字段</span><span>第 1 次</span><span>第 2 次</span><span>第 3 次</span>
      </div>
      {rows.map((row) => {
        const key = recordKey(row.measurement_id, row.field_name);
        const values = draft[key] || { m1: '', m2: '', m3: '' };
        const needsThird = needsThirdReading(definition, values);
        const thirdProvided = values.m3 !== '' && values.m3 != null;
        let reduction = null;
        if (!errors[key]) {
          try {
            reduction = reduceMeasurementReadings(definition, values);
          } catch {
            reduction = null;
          }
        }
        return (
          <div className="measurement-reading" key={key}>
            <label className="measurement-field-name" htmlFor={`${key}-m1`}>
              {rows.length > 1 ? row.field_label : definition.display_name_zh}
            </label>
            {['m1', 'm2', 'm3'].map((slot, index) => (
              <InputNumber
                key={slot}
                id={`${key}-${slot}`}
                aria-label={`${definition.measurement_id} ${row.field_label} 第 ${index + 1} 次读数，单位 ${definition.unit}`}
                value={values[slot] === '' ? null : values[slot]}
                onChange={(value) => onChange(key, slot, value ?? '')}
                min={0.01}
                precision={2}
                step={0.1}
                status={errors[key] ? 'error' : undefined}
                placeholder={slot === 'm3' ? (needsThird ? '必填' : '按需') : '必填'}
              />
            ))}
            {errors[key] && <div className="measurement-error" role="alert">{errors[key]}</div>}
            {needsThird && !thirdProvided && !errors[key] && (
              <Alert className="third-reading-alert" type="warning" showIcon message="差值已超阈值，请填写第三次读数" />
            )}
            {reduction && (!needsThird || thirdProvided) && (
              <Alert
                className="third-reading-alert"
                type={reduction.qc_status === 'REVIEW_REQUIRED' ? 'warning' : 'success'}
                showIcon
                message={`归约值 ${reduction.final_value.toFixed(2)} ${definition.unit}`}
                description={reduction.qc_status === 'REVIEW_REQUIRED'
                  ? '三次读数仍较分散，已保留归约值并标记复核；建议现场重测。'
                  : `采用第 ${reduction.selected_trial_indices.join('、')} 次读数。`}
              />
            )}
          </div>
        );
      })}
    </fieldset>
  );
}
