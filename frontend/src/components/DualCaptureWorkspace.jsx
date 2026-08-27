import React, { useEffect, useState } from 'react';
import { FolderOpenOutlined, SyncOutlined } from '@ant-design/icons';
import { Alert, Button, Checkbox, Form, Input, InputNumber, Progress, Space, Tag, Typography } from 'antd';

const { Text, Title } = Typography;

const parseSubjectNumber = (value) => {
  const digits = String(value ?? '').replace(/\D/g, '');
  return Math.max(1, Math.min(9999, Number(digits) || 1));
};
const formatSubjectId = (value) => `S${String(parseSubjectNumber(value)).padStart(4, '0')}`;

export default function DualCaptureWorkspace({ cameraStatus, state, busyAction, selectedOutputDirectory, onChooseOutputDirectory, onCreate, onCapture, onStartNext }) {
  const [form] = Form.useForm();
  const [ready, setReady] = useState(false);
  const [subjectInput, setSubjectInput] = useState('S0001');
  const angles = state?.angles || [];
  const nextYaw = state?.next_yaw_deg;
  const distance = Form.useWatch('target_distance_mm', form);
  const captured = state?.progress?.captured || 0;
  const expected = state?.progress?.expected || 8;
  const dualReady = cameraStatus?.dual_ready === true;
  const percent = Math.round(captured / expected * 100);
  useEffect(() => {
    if (selectedOutputDirectory) form.setFieldValue('output_path', selectedOutputDirectory);
  }, [form, selectedOutputDirectory]);
  const chooseOutputDirectory = async () => {
    const selected = await window.electronAPI?.selectOutputDirectory?.();
    if (selected) {
      form.setFieldValue('output_path', selected);
    } else {
      onChooseOutputDirectory?.();
    }
  };
  const subjectNumber = parseSubjectNumber(subjectInput);
  const changeSubjectNumber = (offset) => setSubjectInput(formatSubjectId(subjectNumber + offset));
  const normalizeSubjectInput = () => setSubjectInput(formatSubjectId(subjectInput));
  const createDualSession = (values) => onCreate({ ...values, subject_id: formatSubjectId(subjectInput) });
  const startNextSubject = () => {
    const completedNumber = Number(String(state?.subject_id || '').replace(/^S/i, ''));
    setSubjectInput(formatSubjectId(completedNumber + 1));
    setReady(false);
    onStartNext?.();
  };

  if (!state?.active) {
    return (
      <section aria-labelledby="dual-capture-heading">
        <Title level={3} id="dual-capture-heading">双机八角度采集</Title>
        <Alert type="info" showIcon message="一个角度组包含两台相机各 5 帧五模态数据" description="服装备注与目标距离均为可选记录；输出路径可填写任意可写 Windows 文件夹。" />
        <Form form={form} layout="vertical" className="subject-form" onFinish={createDualSession} requiredMark="optional" initialValues={{ target_distance_mm: 2500 }}>
          <Form.Item label="匿名受试者编号">
            <Space.Compact block className="subject-number-picker">
              <Button aria-label="选择上一位受试者" disabled={subjectNumber <= 1} onClick={() => changeSubjectNumber(-1)}>上一位</Button>
              <Input aria-label="当前受试者编号" value={subjectInput} maxLength={5} onChange={(event) => setSubjectInput(event.target.value)} onBlur={normalizeSubjectInput} onPressEnter={normalizeSubjectInput} placeholder="S0001" />
              <Button aria-label="选择下一位受试者" onClick={() => changeSubjectNumber(1)}>下一位</Button>
            </Space.Compact>
            <Text type="secondary" className="subject-number-hint">可直接输入 <code>S0008</code> 或 <code>8</code>，也可点击按钮切换。</Text>
          </Form.Item>
          <Form.Item name="output_path" label="数据输出文件夹" rules={[{ required: true, message: '请选择或输入任意可写 Windows 文件夹' }]}><Input prefix={<FolderOpenOutlined />} placeholder="例如 D:\\人体数据\\本次采集" autoComplete="off" addonAfter={<Button type="link" size="small" loading={busyAction === 'select-output-directory'} onClick={chooseOutputDirectory}>选择</Button>} /></Form.Item>
          <Form.Item name="clothing_note" label="服装备注（可选）"><Input placeholder="例如：短袖、长裤、外套" maxLength={500} /></Form.Item>
          <Form.Item name="target_distance_mm" label="默认距离 mm（可选，可在每组修改）"><InputNumber min={250} max={6000} step={50} precision={0} style={{ width: '100%' }} /></Form.Item>
          <Button type="primary" htmlType="submit" loading={busyAction === 'create-dual-session'}>创建双机八角度任务</Button>
        </Form>
      </section>
    );
  }

  return (
    <section aria-labelledby="dual-capture-heading">
      <div className="section-heading-row"><Title level={3} id="dual-capture-heading">双机八角度采集</Title><Tag color={dualReady ? 'success' : 'warning'}>{dualReady ? '双机就绪' : '请连接两台相机'}</Tag></div>
      <Progress percent={percent} status={percent === 100 ? 'success' : 'active'} />
      <div className="dual-session-meta"><Text>受试者：<strong>{state.subject_id}</strong></Text><Text>输出：{state.output_root}</Text>{state.clothing_note && <Text>服装：{state.clothing_note}</Text>}</div>
      {nextYaw == null ? <><Alert type="success" showIcon message="八个角度均已采集完成" /><Button type="primary" block className="next-subject-button" onClick={startNextSubject}>采集下一位受试者（{formatSubjectId(Number(String(state.subject_id).replace(/^S/i, '')) + 1)}）</Button></> : <>
        <div className="dual-angle-card">
          <Text type="secondary">下一角度</Text><strong>{nextYaw}°</strong>
          <Text type="secondary">请按地垫从正面顺时针转到 {nextYaw}°，确认全身入镜后采集。</Text>
          <InputNumber aria-label="本角度距离（毫米）" value={distance ?? state.target_distance_mm} onChange={(value) => form.setFieldValue('target_distance_mm', value)} min={250} max={6000} step={50} precision={0} addonAfter="mm" style={{ width: '100%' }} />
          <Checkbox checked={ready} onChange={(event) => setReady(event.target.checked)}>已确认当前角度正确、全身完整入框且两台相机画面稳定</Checkbox>
          <Button type="primary" size="large" block icon={<SyncOutlined />} loading={busyAction === 'capture-dual'} disabled={!dualReady || !ready} onClick={() => { onCapture(nextYaw, distance ?? state.target_distance_mm); setReady(false); }}>双机同步采集此角度</Button>
        </div>
        {!dualReady && <Alert type="warning" showIcon message="请在上方依次连接 Gemini 336L 与 D435i；两台均连接后会自动显示双画面预览。" />}
      </>}
      <ol className="dual-angle-list">{angles.map((item) => <li key={item.yaw_deg}><span>{item.yaw_deg}°</span><Tag color={item.status === 'CAPTURED' ? 'success' : 'default'}>{item.status === 'CAPTURED' ? '已采集' : '待采集'}</Tag></li>)}</ol>
    </section>
  );
}
