import React, { useEffect, useMemo, useRef, useState } from 'react';
import { SaveOutlined } from '@ant-design/icons';
import { Alert, Button, Checkbox, Collapse, Empty, Input, Typography } from 'antd';
import MeasurementItem from './MeasurementItem';
import { recordsToDraft, validateMeasurements } from '../protocol/protocolUtils.mjs';

const { Paragraph, Text, Title } = Typography;

export default function Anthropometry({ definitions, state, busyAction, onSave }) {
  const [draft, setDraft] = useState({});
  const [errors, setErrors] = useState({});
  const [equipmentError, setEquipmentError] = useState('');
  const [equipment, setEquipment] = useState({
    stadiometer_id: '',
    scale_id: '',
    tape_id: '',
    anthropometer_id: '',
    equipment_check_confirmed: false
  });
  const loadedSubject = useRef('');
  const required = useMemo(() => definitions.filter((item) => item.required), [definitions]);
  const optional = useMemo(() => definitions.filter((item) => !item.required), [definitions]);

  useEffect(() => {
    if (state?.subject_id && loadedSubject.current !== state.subject_id) {
      const storedEquipment = state.anthropometry?.metadata?.equipment
        || state.anthropometry?.equipment
        || {};
      setDraft(recordsToDraft(state.anthropometry?.records));
      setErrors({});
      setEquipmentError('');
      setEquipment({
        stadiometer_id: storedEquipment.stadiometer_id || '',
        scale_id: storedEquipment.scale_id || '',
        tape_id: storedEquipment.tape_id || '',
        anthropometer_id: storedEquipment.anthropometer_id || '',
        equipment_check_confirmed: storedEquipment.equipment_check_confirmed === true
      });
      loadedSubject.current = state.subject_id;
    }
    if (!state?.subject_id) loadedSubject.current = '';
  }, [state?.anthropometry?.records, state?.subject_id]);

  const updateReading = (key, slot, value) => {
    setDraft((previous) => ({
      ...previous,
      [key]: { m1: '', m2: '', m3: '', ...previous[key], [slot]: value }
    }));
    setErrors((previous) => {
      if (!previous[key]) return previous;
      const next = { ...previous };
      delete next[key];
      return next;
    });
  };

  const save = () => {
    const missingEquipment = [
      ['stadiometer_id', '身高计编号'],
      ['scale_id', '电子秤编号'],
      ['tape_id', '无弹性软尺编号'],
      ['anthropometer_id', 'Anthropometer/大型卡尺编号']
    ].filter(([key]) => !equipment[key].trim()).map(([, label]) => label);
    if (missingEquipment.length || !equipment.equipment_check_confirmed) {
      setEquipmentError(
        missingEquipment.length
          ? `请填写：${missingEquipment.join('、')}`
          : '请确认全部工具已按现场 SOP 完成零点/校准检查'
      );
      return;
    }
    setEquipmentError('');
    const result = validateMeasurements(definitions, draft);
    setErrors(result.errors);
    if (!result.valid) {
      const firstKey = Object.keys(result.errors)[0];
      window.setTimeout(() => document.getElementById(`${firstKey}-m1`)?.focus(), 0);
      return;
    }
    onSave(result.records, equipment);
  };

  if (!state?.subject_id) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="请先选择受试者，再录入人体测量" />;
  }
  if (!definitions.length) {
    return (
      <Alert
        type="warning"
        showIcon
        message="人体测量字典尚未返回"
        description="请刷新协议目录；旧版后端不会开放测量保存。"
      />
    );
  }

  const optionalPanel = [{
    key: 'optional',
    label: `M14–M23 选填项目（${optional.length} 项，可整项留空）`,
    children: optional.map((definition) => (
      <MeasurementItem key={definition.measurement_id} definition={definition} draft={draft} errors={errors} onChange={updateReading} />
    ))
  }];

  return (
    <section aria-labelledby="anthropometry-heading">
      <Title level={3} id="anthropometry-heading">3. 人体测量</Title>
      <Paragraph type="secondary">
        每个已填写字段至少测量两次。M01–M13 的每个 field 均必填；M14–M23 可整项留空。所有原始读数会保留。
      </Paragraph>
      <Alert
        type="warning"
        showIcon
        message="M01–M13 的有效采集必须具备四类测量工具"
        description="缺少身高计、校准电子秤、无弹性人体测量软尺或 Anthropometer/大型滑动卡尺时，可继续做图像联调，但不能保存合格的人体测量并通过完成门禁。"
      />
      <fieldset className="anthropometry-equipment">
        <legend>本次测量工具与检查记录</legend>
        <label>身高计编号<Input value={equipment.stadiometer_id} onChange={(event) => setEquipment((value) => ({ ...value, stadiometer_id: event.target.value }))} /></label>
        <label>校准电子秤编号<Input value={equipment.scale_id} onChange={(event) => setEquipment((value) => ({ ...value, scale_id: event.target.value }))} /></label>
        <label>无弹性软尺编号<Input value={equipment.tape_id} onChange={(event) => setEquipment((value) => ({ ...value, tape_id: event.target.value }))} /></label>
        <label>Anthropometer / 大型卡尺编号<Input value={equipment.anthropometer_id} onChange={(event) => setEquipment((value) => ({ ...value, anthropometer_id: event.target.value }))} /></label>
        <Checkbox checked={equipment.equipment_check_confirmed} onChange={(event) => setEquipment((value) => ({ ...value, equipment_check_confirmed: event.target.checked }))}>
          已完成电子秤零点、量具完好性及适用校准状态检查
        </Checkbox>
        {equipmentError && <Text type="danger" role="alert">{equipmentError}</Text>}
      </fieldset>
      {Object.keys(errors).length > 0 && (
        <Alert
          type="error"
          showIcon
          role="alert"
          message={`还有 ${Object.keys(errors).length} 个字段未满足测量规则`}
          description="已聚焦到第一个错误，请填写两次有效读数以及被阈值触发的第三次读数。"
        />
      )}
      <div className="measurement-list">
        {required.map((definition) => (
          <MeasurementItem key={definition.measurement_id} definition={definition} draft={draft} errors={errors} onChange={updateReading} />
        ))}
      </div>
      <Collapse className="optional-measurements" items={optionalPanel} />
      <div className="measurement-actions">
        <Text type="secondary">保存后仍可修改；完成门禁会再次核对所有必填项。</Text>
        <Button type="primary" icon={<SaveOutlined />} loading={busyAction === 'measurements'} disabled={state.status === 'COMPLETE' || state.reconciliation_required === true} onClick={save}>
          校验并保存人体测量
        </Button>
      </div>
    </section>
  );
}
