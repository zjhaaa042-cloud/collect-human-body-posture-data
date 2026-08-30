import React, { useEffect, useRef, useState } from 'react';
import { Alert, Card, Tabs, Tag, Typography } from 'antd';
import Anthropometry from './Anthropometry';
import CameraConnection from './CameraConnection';
import CompletionPanel from './CompletionPanel';
import DualCaptureWorkspace from './DualCaptureWorkspace';
import SubjectSetup from './SubjectSetup';
import './ControlPanel.css';

const { Text } = Typography;

function ControlPanel({
  connected,
  cameraStatus,
  isCameraConnecting,
  isCameraDisconnecting,
  catalog,
  protocolLoading,
  protocolError,
  busyAction,
  actions
}) {
  const [activeTab, setActiveTab] = useState('subject');
  const previousSubject = useRef('');
  const state = actions.dualSessionState;

  useEffect(() => {
    if (state?.subject_id && previousSubject.current !== state.subject_id) {
      previousSubject.current = state.subject_id;
      setActiveTab(state.progress?.captured >= 8 ? 'measurements' : 'capture');
    }
    if (!state?.subject_id) previousSubject.current = '';
  }, [state?.subject_id, state?.progress?.captured]);

  const continueCurrent = () => {
    if ((state?.progress?.captured || 0) < 8) setActiveTab('capture');
    else if (state?.anthropometry?.complete !== true) setActiveTab('measurements');
    else setActiveTab('completion');
  };

  const tabs = [
    {
      key: 'subject',
      label: '1 受试者登记',
      children: (
        <SubjectSetup
          activeState={state}
          busyAction={busyAction}
          selectedOutputDirectory={actions.selectedOutputDirectory}
          onChooseOutputDirectory={actions.selectOutputDirectory}
          onCreate={actions.createDualSession}
          onOpen={actions.openDualSession}
          onContinue={continueCurrent}
          onStartNew={actions.startNextDualSubject}
        />
      )
    },
    {
      key: 'capture',
      label: '2 双机八角度',
      children: <DualCaptureWorkspace cameraStatus={cameraStatus} state={state} busyAction={busyAction} onCapture={actions.captureDualGroup} onGoMeasurements={() => setActiveTab('measurements')} />
    },
    {
      key: 'measurements',
      label: '3 人体测量',
      children: <Anthropometry definitions={catalog.measurements || []} state={state} busyAction={busyAction} onSave={actions.saveDualAnthropometry} />
    },
    {
      key: 'completion',
      label: '4 完成',
      children: <CompletionPanel state={state} report={actions.dualCompletionReport} busyAction={busyAction} onComplete={actions.completeDualSession} />
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
            <span>双机采集任务</span>
            {state?.subject_id
              ? <Tag color="blue">{state.subject_id} · {state.status || 'ACTIVE'}</Tag>
              : <Text type="secondary">尚未登记受试者</Text>}
          </div>
        }
      >
        {protocolLoading && !catalog.measurements?.length && (
          <Alert type="info" showIcon message="正在加载人体测量字典" />
        )}
        {protocolError && !catalog.measurements?.length && (
          <Alert type="warning" showIcon message="人体测量字典加载失败" description={protocolError} />
        )}
        <Tabs activeKey={activeTab} onChange={setActiveTab} items={tabs} destroyOnHidden={false} className="protocol-tabs" />
      </Card>
    </div>
  );
}

export default React.memo(ControlPanel);
