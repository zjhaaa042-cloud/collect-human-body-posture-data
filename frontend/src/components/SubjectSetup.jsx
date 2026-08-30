import React, { useEffect, useState } from 'react';
import { FolderOpenOutlined, LoginOutlined, UserAddOutlined } from '@ant-design/icons';
import { Alert, Button, Form, Input, InputNumber, Space, Typography } from 'antd';

const { Text, Title } = Typography;

const parseSubjectNumber = (value) => {
  const digits = String(value ?? '').replace(/\D/g, '');
  return Math.max(1, Math.min(9999, Number(digits) || 1));
};

export const formatSubjectId = (value) => `S${String(parseSubjectNumber(value)).padStart(4, '0')}`;

export default function SubjectSetup({
  activeState,
  busyAction,
  selectedOutputDirectory,
  onChooseOutputDirectory,
  onCreate,
  onOpen,
  onContinue,
  onStartNew
}) {
  const [form] = Form.useForm();
  const [subjectInput, setSubjectInput] = useState('S0001');
  const subjectNumber = parseSubjectNumber(subjectInput);

  useEffect(() => {
    if (selectedOutputDirectory) form.setFieldValue('output_path', selectedOutputDirectory);
  }, [form, selectedOutputDirectory]);

  useEffect(() => {
    if (!activeState?.subject_id) return;
    setSubjectInput(activeState.subject_id);
    form.setFieldsValue({
      output_path: activeState.output_directory,
      clothing_note: activeState.clothing_note,
      target_distance_mm: activeState.target_distance_mm
    });
  }, [activeState?.subject_id, activeState?.output_directory, activeState?.clothing_note, activeState?.target_distance_mm, form]);

  const chooseOutputDirectory = async () => {
    const selected = await window.electronAPI?.selectOutputDirectory?.();
    if (selected) form.setFieldValue('output_path', selected);
    else onChooseOutputDirectory?.();
  };
  const normalizeSubjectInput = () => setSubjectInput(formatSubjectId(subjectInput));
  const changeSubjectNumber = (offset) => setSubjectInput(formatSubjectId(subjectNumber + offset));
  const payload = (values) => ({ ...values, subject_id: formatSubjectId(subjectInput) });
  const openExisting = async () => onOpen(payload(await form.validateFields(['output_path'])));
  const startNew = () => {
    const next = formatSubjectId(subjectNumber + 1);
    onStartNew?.();
    setSubjectInput(next);
    form.resetFields();
    if (selectedOutputDirectory) form.setFieldValue('output_path', selectedOutputDirectory);
  };

  if (activeState?.active) {
    const captured = activeState.progress?.captured ?? 0;
    return (
      <section aria-labelledby="subject-heading">
        <Title level={3} id="subject-heading">1. 受试者登记</Title>
        <Alert
          type={activeState.status === 'COMPLETE' ? 'success' : 'info'}
          showIcon
          message={`${activeState.subject_id} · ${activeState.status === 'COMPLETE' ? '任务已完成' : '任务进行中'}`}
          description={`输出文件夹：${activeState.output_directory || activeState.output_root}；双机八角度进度：${captured}/8${activeState.clothing_note ? `；服装备注：${activeState.clothing_note}` : ''}`}
        />
        <Space wrap className="subject-existing-actions">
          <Button type="primary" icon={<LoginOutlined />} onClick={onContinue}>
            {captured < 8 ? '继续双机八角度采集' : '继续人体测量与完成'}
          </Button>
          <Button onClick={startNew}>登记下一位受试者</Button>
        </Space>
      </section>
    );
  }

  return (
    <section aria-labelledby="subject-heading">
      <Title level={3} id="subject-heading">1. 受试者登记</Title>
      <Text type="secondary">登记一次后，受试者编号、输出文件夹、服装备注和默认距离会直接用于第 2 步双机八角度采集。</Text>
      <Form form={form} layout="vertical" onFinish={(values) => onCreate(payload(values))} requiredMark="optional" className="subject-form" initialValues={{ target_distance_mm: 2500 }}>
        <Form.Item label="匿名受试者编号" required>
          <Space.Compact block className="subject-number-picker">
            <Button aria-label="选择上一位受试者" disabled={subjectNumber <= 1} onClick={() => changeSubjectNumber(-1)}>上一位</Button>
            <Input aria-label="当前受试者编号" value={subjectInput} maxLength={5} onChange={(event) => setSubjectInput(event.target.value)} onBlur={normalizeSubjectInput} onPressEnter={normalizeSubjectInput} placeholder="S0001" />
            <Button aria-label="选择下一位受试者" disabled={subjectNumber >= 9999} onClick={() => changeSubjectNumber(1)}>下一位</Button>
          </Space.Compact>
          <Text type="secondary" className="subject-number-hint">可直接输入 <code>S0008</code> 或 <code>8</code>，也可点击按钮切换。</Text>
        </Form.Item>
        <Form.Item name="output_path" label="数据输出文件夹" rules={[{ required: true, message: '请选择或输入任意可写 Windows 文件夹' }]}>
          <Space.Compact block>
            <Input aria-label="数据输出文件夹" prefix={<FolderOpenOutlined />} placeholder="例如 D:\\人体数据\\本次采集" autoComplete="off" />
            <Button loading={busyAction === 'select-output-directory'} onClick={chooseOutputDirectory}>选择</Button>
          </Space.Compact>
        </Form.Item>
        <Form.Item name="clothing_note" label="服装备注（可选）"><Input placeholder="例如：短袖、长裤、外套" maxLength={500} /></Form.Item>
        <Form.Item name="target_distance_mm" label="默认距离 mm（可选，每个角度仍可修改）"><InputNumber min={250} max={6000} step={50} precision={0} style={{ width: '100%' }} /></Form.Item>
        <Alert type="info" showIcon message="新建与继续不会混用数据" description="新建任务拒绝覆盖已有编号；如果该编号已经采集过，请点击“继续已有任务”。" />
        <Space wrap>
          <Button type="primary" htmlType="submit" icon={<UserAddOutlined />} loading={busyAction === 'create-dual-session'}>登记并建立任务</Button>
          <Button icon={<LoginOutlined />} loading={busyAction === 'open-dual-session'} onClick={openExisting}>继续已有任务</Button>
        </Space>
      </Form>
    </section>
  );
}
