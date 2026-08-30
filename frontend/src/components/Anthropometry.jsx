import React, { useEffect, useMemo, useRef, useState } from 'react';
import { SaveOutlined } from '@ant-design/icons';
import { Alert, Button, Collapse, Empty, Typography } from 'antd';
import MeasurementItem from './MeasurementItem';
import { recordsToDraft, validateMeasurements } from '../protocol/protocolUtils.mjs';

const { Paragraph, Text, Title } = Typography;

export default function Anthropometry({ definitions, state, busyAction, onSave }) {
  const [draft, setDraft] = useState({});
  const [errors, setErrors] = useState({});
  const loadedSubject = useRef('');
  const required = useMemo(() => definitions.filter((item) => item.required), [definitions]);
  const optional = useMemo(() => definitions.filter((item) => !item.required), [definitions]);

  useEffect(() => {
    if (state?.subject_id && loadedSubject.current !== state.subject_id) {
      setDraft(recordsToDraft(state.anthropometry?.records));
      setErrors({});
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
    const result = validateMeasurements(definitions, draft);
    setErrors(result.errors);
    if (!result.valid) {
      const firstKey = Object.keys(result.errors)[0];
      window.setTimeout(() => document.getElementById(`${firstKey}-m1`)?.focus(), 0);
      return;
    }
    onSave(result.records, {});
  };

  if (!state?.subject_id) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="请先完成第 1 步受试者登记，再录入人体测量" />;
  }
  if (!definitions.length) {
    return <Alert type="warning" showIcon message="人体测量字典尚未返回" description="请刷新协议目录；旧版后端不会开放测量保存。" />;
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
      <Paragraph type="secondary">每个已填写字段至少测量两次。M01–M13 的每个 field 均必填；M14–M23 可整项留空。所有原始读数会保留。</Paragraph>
      <Alert type="info" showIcon message="无需填写操作员或器材信息" description="保存时仅记录匿名受试者、测量原始读数与时间；完成门禁仍会校验 M01–M13。" />
      {Object.keys(errors).length > 0 && (
        <Alert type="error" showIcon role="alert" message={`还有 ${Object.keys(errors).length} 个字段未满足测量规则`} description="已聚焦到第一个错误，请填写两次有效读数以及被阈值触发的第三次读数。" />
      )}
      <div className="measurement-list">
        {required.map((definition) => (
          <MeasurementItem key={definition.measurement_id} definition={definition} draft={draft} errors={errors} onChange={updateReading} />
        ))}
      </div>
      <Collapse className="optional-measurements" items={optionalPanel} />
      <div className="measurement-actions">
        <Text type="secondary">保存后仍可修改；完成门禁会再次核对所有必填项。</Text>
        <Button type="primary" icon={<SaveOutlined />} loading={busyAction === 'measurements'} disabled={state.status === 'COMPLETE' || state.reconciliation_required === true} onClick={save}>校验并保存人体测量</Button>
      </div>
    </section>
  );
}
