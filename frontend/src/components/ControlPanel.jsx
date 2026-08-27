import React, { useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Tabs, Tag, Typography } from 'antd';
import Anthropometry from './Anthropometry';
import CameraConnection from './CameraConnection';
import CompletionPanel from './CompletionPanel';
import ConditionRunner from './ConditionRunner';
import DualCaptureWorkspace from './DualCaptureWorkspace';
import SubjectSetup from './SubjectSetup';
import { resolveSelectedCondition } from '../protocol/protocolUtils.mjs';
import './ControlPanel.css';

const { Text } = Typography;

function ControlPanel({
  connected,
  cameraStatus,
  isCameraConnecting,
  isCameraDisconnecting,
  catalog,
  subjects,
  protocolState,
  protocolLoading,
  protocolError,
  busyAction,
  actions
}) {
  const [activeTab, setActiveTab] = useState('dual');
  const [selectedConditionId, setSelectedConditionId] = useState('');
  const previousSubject = useRef('');
  const conditions = protocolState?.conditions || [];
  const measurementDefinitions = protocolState?.measurement_definitions?.length
    ? protocolState.measurement_definitions
    : catalog.measurements;
  const nextConditionId = protocolState?.next_condition_id || conditions[0]?.condition_id || '';
  const selectedCondition = resolveSelectedCondition(
    conditions,
    selectedConditionId,
    nextConditionId
  );

  useEffect(() => {
    if (protocolState?.subject_id && previousSubject.current !== protocolState.subject_id) {
      previousSubject.current = protocolState.subject_id;
      setActiveTab('conditions');
    }
  }, [protocolState?.subject_id]);

  useEffect(() => {
    if (nextConditionId) setSelectedConditionId(nextConditionId);
    else if (!protocolState?.subject_id) setSelectedConditionId('');
  }, [nextConditionId, protocolState?.subject_id]);

  useEffect(() => {
    if (protocolState?.subject_id && selectedCondition?.condition_id) {
      actions.selectPreviewCondition?.(selectedCondition.condition_id);
    }
  }, [actions.selectPreviewCondition, protocolState?.subject_id, selectedCondition?.condition_id]);

  const tabs = [
    {
      key: 'dual',
      label: '双机 8 角度',
      children: <DualCaptureWorkspace cameraStatus={cameraStatus} state={actions.dualSessionState} busyAction={busyAction} selectedOutputDirectory={actions.selectedOutputDirectory} onChooseOutputDirectory={actions.selectOutputDirectory} onCreate={actions.createDualSession} onCapture={actions.captureDualGroup} onStartNext={actions.startNextDualSubject} />
    },
    {
      key: 'subject',
      label: '1 受试者',
      children: (
        <SubjectSetup
          profiles={catalog.profiles}
          defaultProfileId={catalog.default_profile_id}
          subjects={subjects}
          activeSubjectId={protocolState?.subject_id}
          busyAction={busyAction}
          onCreate={actions.createSubject}
          onSelect={actions.selectSubject}
          onRefresh={actions.refreshProtocol}
        />
      )
    },
    {
      key: 'conditions',
      label: '2 条件采集',
      children: (
        <ConditionRunner
          state={protocolState}
          catalog={catalog}
          cameraStatus={cameraStatus}
          selectedId={selectedCondition?.condition_id || ''}
          lastCaptureResult={actions.lastCaptureResult}
          reviewPreview={actions.reviewPreview}
          reviewPreviewLoading={actions.reviewPreviewLoading}
          reviewPreviewError={actions.reviewPreviewError}
          busyAction={busyAction}
          onCapture={actions.captureCondition}
          onReview={actions.reviewCapture}
          onRequestReviewPreview={actions.requestReviewPreview}
          onSelect={setSelectedConditionId}
        />
      )
    },
    {
      key: 'measurements',
      label: '3 人体测量',
      children: <Anthropometry definitions={measurementDefinitions} state={protocolState} busyAction={busyAction} onSave={actions.saveAnthropometry} />
    },
    {
      key: 'completion',
      label: '4 完成',
      children: <CompletionPanel state={protocolState} report={actions.completionReport} busyAction={busyAction} onComplete={actions.completeSubject} />
    }
  ];
  const renderedTabs = catalog.profiles.length
    ? tabs
    : [
      tabs[0],
      {
        key: 'legacy-unavailable',
        label: '旧协议',
        children: (
          <Alert
            type={protocolError ? 'error' : 'warning'}
            showIcon
            message="旧版采集协议尚不可用"
            description={protocolError || '服务端尚未返回 protocol_catalog。双机八角度采集不受此影响；如需使用旧协议，请确认后端已升级后刷新。'}
            action={<Button size="small" onClick={actions.refreshProtocol}>刷新协议</Button>}
          />
        )
      }
    ];

  return (
    <div className="control-panel">
      <Card className="control-card camera-card" variant="borderless">
        <CameraConnection
          connected={connected}
          cameraStatus={cameraStatus}
          isConnecting={isCameraConnecting}
          isDisconnecting={isCameraDisconnecting}
          requiredCameraCode={selectedCondition?.camera_code}
          onConnect={actions.connectCamera}
          onDisconnect={actions.disconnectCamera}
          onRefresh={actions.refreshCamera}
        />
      </Card>
      <Card
        className="control-card protocol-card"
        variant="borderless"
        title={
          <div className="protocol-card-title">
            <span>协议任务</span>
            {protocolState?.subject_id
              ? <Tag color="blue">{protocolState.subject_id} · {protocolState.status || 'IN_PROGRESS'}</Tag>
              : <Text type="secondary">尚未选择受试者</Text>}
          </div>
        }
      >
        {protocolState?.reconciliation_required && (
          <Alert
            type="error"
            showIcon
            message="该受试者有已落盘但待恢复的状态事务"
            description="请停止所有写操作并重启采集服务；重启时会根据 sidecar 和哈希清单自动恢复。"
          />
        )}
        {protocolLoading && !catalog.profiles.length && activeTab === 'dual' && (
          <Alert type="info" showIcon message="正在加载旧版协议" description="双机八角度任务可以直接使用，无需等待旧版协议加载完成。" />
        )}
        <Tabs
          activeKey={activeTab}
          onChange={setActiveTab}
          items={renderedTabs}
          destroyOnHidden={false}
          className="protocol-tabs"
        />
      </Card>
    </div>
  );
}

export default React.memo(ControlPanel);
