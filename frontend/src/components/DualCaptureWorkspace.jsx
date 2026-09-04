import React, { useEffect, useState } from 'react';
import { SyncOutlined } from '@ant-design/icons';
import { Alert, Button, Checkbox, Empty, InputNumber, Progress, Tag, Typography } from 'antd';
import { dualIntegrityMessage, dualWriteBlocked } from '../collector/dualSessionState.mjs';

const { Text, Title } = Typography;

export default function DualCaptureWorkspace({ cameraStatus, state, busyAction, onCapture, onGoMeasurements }) {
  const [ready, setReady] = useState(false);
  const [distance, setDistance] = useState(null);
  const angles = state?.angles || [];
  const nextYaw = state?.next_yaw_deg;
  const captured = state?.progress?.captured || 0;
  const expected = state?.progress?.expected || 8;
  const dualReady = cameraStatus?.dual_ready === true;
  const writeBlocked = dualWriteBlocked(state);
  const integrityMessage = dualIntegrityMessage(state);
  const percent = Math.round(captured / expected * 100);

  useEffect(() => {
    setDistance(state?.target_distance_mm ?? null);
    setReady(false);
  }, [state?.subject_id, state?.target_distance_mm, nextYaw]);

  if (!state?.active) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="请先在第 1 步完成受试者登记，再开始双机八角度采集" />;
  }

  return (
    <section aria-labelledby="dual-capture-heading">
      <div className="section-heading-row">
        <Title level={3} id="dual-capture-heading">2. 双机八角度采集</Title>
        <Tag color={dualReady ? 'success' : 'warning'}>{dualReady ? '双机就绪' : '请连接两台相机'}</Tag>
      </div>
      <Progress percent={percent} status={percent === 100 ? 'success' : 'active'} />
      <div className="dual-session-meta">
        <Text>受试者：<strong>{state.subject_id}</strong></Text>
        <Text>输出：{state.output_directory || state.output_root}</Text>
        {state.clothing_note && <Text>服装：{state.clothing_note}</Text>}
        <Text>默认距离：{state.target_distance_mm ? `${state.target_distance_mm} mm` : '未记录'}</Text>
      </div>
      <Alert
        type="info"
        showIcon
        message="每个角度由两台相机近同步采集，各保存 5 帧"
        description="Gemini 是全身主视角；D435i 是辅助 RGB-D 视角，2.5 m 处允许因硬件 FOV 产生的画幅裁切，不作为全身入框门禁。每帧保存 RGB、原始/对齐深度、两类伪彩深度和带颜色 PLY 点云。"
      />
      {integrityMessage && (
        <Alert
          type={state.reconciliation_required ? 'error' : 'info'}
          showIcon
          role={state.reconciliation_required ? 'alert' : 'status'}
          message={state.reconciliation_required ? '任务已因完整性问题锁定' : '任务恢复信息'}
          description={integrityMessage}
        />
      )}
      {nextYaw == null ? (
        <>
          <Alert type="success" showIcon message="八个角度均已采集完成" description="登记信息和八角度数据已经统一保存在当前受试者目录中。" />
          <Button type="primary" block className="next-subject-button" onClick={onGoMeasurements}>进入第 3 步人体测量</Button>
        </>
      ) : (
        <>
          <div className="dual-angle-card">
            <Text type="secondary">下一角度</Text><strong>{nextYaw}°</strong>
            <Text type="secondary">请按地垫从正面顺时针转到 {nextYaw}°，确认 Gemini 全身入镜后采集。</Text>
            <div className="angle-distance-field">
              <InputNumber aria-label="本角度距离（毫米）" value={distance} onChange={setDistance} min={250} max={6000} step={50} precision={0} style={{ width: '100%' }} />
              <Text type="secondary">mm</Text>
            </div>
            <Checkbox disabled={writeBlocked} checked={ready} onChange={(event) => setReady(event.target.checked)}>已确认当前角度正确、Gemini 全身完整入框，且两台相机画面稳定</Checkbox>
            <Button type="primary" size="large" block icon={<SyncOutlined />} loading={busyAction === 'capture-dual'} disabled={!dualReady || !ready || writeBlocked} onClick={() => { onCapture(nextYaw, distance); setReady(false); }}>双机近同步采集此角度</Button>
          </div>
          {!dualReady && <Alert type="warning" showIcon message="请在上方依次连接 Gemini 336L 与 D435i；两台均连接后会自动显示双画面预览。" />}
        </>
      )}
      <ol className="dual-angle-list">{angles.map((item) => <li key={item.yaw_deg}><span>{item.yaw_deg}°</span><Tag color={item.status === 'CAPTURED' ? 'success' : 'default'}>{item.status === 'CAPTURED' ? '已采集' : '待采集'}</Tag></li>)}</ol>
    </section>
  );
}
