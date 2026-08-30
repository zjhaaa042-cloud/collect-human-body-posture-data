import React from 'react';
import { CheckCircleOutlined, LockOutlined } from '@ant-design/icons';
import { Alert, Button, Empty, List, Result, Tag, Typography } from 'antd';

const { Paragraph, Text, Title } = Typography;

const asList = (value) => {
  if (Array.isArray(value)) return value;
  if (value && typeof value === 'object') return Object.entries(value).map(([key, detail]) => `${key}：${detail}`);
  return [];
};

export default function CompletionPanel({ state, report: returnedReport, busyAction, onComplete }) {
  if (!state?.subject_id) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="请先完成第 1 步受试者登记，系统才能计算完成门禁" />;
  }

  const progress = state.progress || {};
  const report = returnedReport || state.completion?.report || state.completion_report || {};
  const missingConditions = asList(progress.missing_angle_ids || report.missing_angle_ids);
  const missingMeasurements = asList(state.anthropometry?.missing_required);
  const blockers = asList(state.completion?.blockers);
  const integrityErrors = asList(report.integrity_errors || state.completion?.integrity_errors);
  const invalidConditions = asList(report.invalid_condition_ids || state.completion?.invalid_condition_ids);
  const reportStatus = String(report.status || state.completion?.status || state.completion?.integrity_status || state.status || '').toUpperCase();
  const integrityFailed = ['CORRUPTED', 'INVALID', 'FAILED', 'FAIL'].includes(reportStatus)
    || integrityErrors.length > 0
    || invalidConditions.length > 0;
  const capturedAll = Number(progress.expected) > 0 && Number(progress.captured) >= Number(progress.expected);
  const measurementsComplete = state.anthropometry?.complete === true;
  const explicitReady = state.completion?.can_complete ?? state.completion?.ready ?? state.completion?.eligible;
  const baseCanComplete = typeof explicitReady === 'boolean'
    ? explicitReady
    : capturedAll && measurementsComplete && !missingConditions.length && !missingMeasurements.length && !blockers.length;
  const reportReady = report.ready_to_complete;
  const canComplete = baseCanComplete
    && !integrityFailed
    && reportReady !== false
    && state.reconciliation_required !== true;
  const isComplete = String(state.status).toUpperCase() === 'COMPLETE' || state.completion?.completed === true;
  const isCorruptedComplete = integrityFailed && (
    String(state.status).toUpperCase() === 'CORRUPTED'
    || reportStatus === 'CORRUPTED'
    || Boolean(state.completed_at || state.completion?.completed_at)
  );
  const allMissing = [
    ...missingConditions.map((item) => `采集角度：${typeof item === 'string' ? item : item.yaw_deg || JSON.stringify(item)}`),
    ...missingMeasurements.map((item) => `人体测量：${typeof item === 'string' ? item : item.measurement_id || JSON.stringify(item)}`),
    ...blockers.map((item) => `门禁：${typeof item === 'string' ? item : item.message || JSON.stringify(item)}`),
    ...invalidConditions.map((item) => `完整性异常条件：${typeof item === 'string' ? item : item.condition_id || JSON.stringify(item)}`),
    ...integrityErrors.map((item) => `完整性错误：${typeof item === 'string' ? item : item.message || JSON.stringify(item)}`)
  ];

  if (isComplete && !integrityFailed) {
    return (
      <Result
        status="success"
        title={`受试者 ${state.subject_id} 已完成`}
        subTitle={state.completion?.completed_at ? `完成时间：${state.completion.completed_at}` : '数据已通过协议完整性门禁。'}
      />
    );
  }

  if (isCorruptedComplete) {
    return (
      <Result
        status="error"
        title={`受试者 ${state.subject_id} 的已完成记录存在完整性异常`}
        subTitle="状态已标记为 CORRUPTED；请勿将该任务当作有效完成数据。"
        extra={<Alert type="error" showIcon message="完整性检查未通过" description={<List size="small" dataSource={allMissing} renderItem={(item) => <List.Item>{item}</List.Item>} />} />}
      />
    );
  }

  return (
    <section aria-labelledby="completion-heading">
      <div className="section-heading-row">
        <Title level={3} id="completion-heading">4. 完成门禁</Title>
        <Tag color={canComplete ? 'success' : 'warning'} icon={canComplete ? <CheckCircleOutlined /> : <LockOutlined />}>
          {canComplete ? '允许完成' : '已锁定'}
        </Tag>
      </div>
      <Paragraph type="secondary">只有双机八个角度全部成功落盘、M01–M13 必填测量完成且服务端检查通过，才允许结束该受试者任务。</Paragraph>
      <div className="gate-summary">
        <div><Text type="secondary">双机八角度</Text><strong>{progress.captured ?? 0}/{progress.expected ?? 8}</strong></div>
        <div><Text type="secondary">必填测量</Text><strong>{measurementsComplete ? '已完成' : '未完成'}</strong></div>
        <div><Text type="secondary">受试者状态</Text><strong>{state.status || 'IN_PROGRESS'}</strong></div>
      </div>
      {allMissing.length ? (
        <Alert
          type="warning"
          showIcon
          message={`仍有 ${allMissing.length} 项阻止完成`}
          description={<List size="small" dataSource={allMissing} renderItem={(item) => <List.Item>{item}</List.Item>} />}
        />
      ) : (
        <Alert type={canComplete ? 'success' : 'info'} showIcon message={canComplete ? '所有检查已通过' : '等待服务端返回完整门禁状态'} />
      )}
      <Button
        type="primary"
        size="large"
        block
        icon={canComplete ? <CheckCircleOutlined /> : <LockOutlined />}
        disabled={!canComplete}
        loading={busyAction === 'completion'}
        onClick={onComplete}
      >
        确认完成受试者采集
      </Button>
    </section>
  );
}
