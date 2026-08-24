import React, { useCallback, useState } from 'react';
import { App as AntApp, ConfigProvider, Layout, theme } from 'antd';
import AppHeader from './components/AppHeader';
import BackendDiagnostic from './components/BackendDiagnostic';
import ControlPanel from './components/ControlPanel';
import PreviewPanel from './components/PreviewPanel';
import StatusBar from './components/StatusBar';
import useCollectorSocket from './hooks/useCollectorSocket';
import './styles/App.css';

const { Content, Footer } = Layout;
const BACKEND_PORT = '8765';

const defaultBackendHost = () => {
  const fromQuery = new URLSearchParams(window.location.search).get('backend');
  if (fromQuery) return fromQuery.replace(/^https?:\/\//, '').replace(/^wss?:\/\//, '').replace(/\/$/, '');
  const saved = localStorage.getItem('backendHost');
  if (saved) return saved;
  const host = window.location.hostname;
  return host && !['localhost', '127.0.0.1'].includes(host) ? `${host}:${BACKEND_PORT}` : `localhost:${BACKEND_PORT}`;
};

function AppContent() {
  const { message, modal } = AntApp.useApp();
  const [backendHost, setBackendHost] = useState(defaultBackendHost);
  const [connectionVersion, setConnectionVersion] = useState(0);
  const [diagnosticOpen, setDiagnosticOpen] = useState(false);
  const collector = useCollectorSocket({ backendHost, message, connectionVersion });

  const reconnectBackend = useCallback((nextHost) => {
    localStorage.setItem('backendHost', nextHost);
    setBackendHost(nextHost);
    setConnectionVersion((version) => version + 1);
    setDiagnosticOpen(false);
  }, []);

  const exitApp = useCallback(() => {
    modal.confirm({
      title: '确认退出采集系统',
      content: '未提交的人体测量输入不会保存。已成功落盘的数据不会被删除。',
      okText: '确定退出',
      cancelText: '继续采集',
      okButtonProps: { danger: true },
      onOk: () => {
        collector.send('exit_app');
        window.setTimeout(() => {
          if (window.electronAPI?.closeWindow) window.electronAPI.closeWindow();
          else window.close();
        }, 1000);
      }
    });
  }, [collector, modal]);

  return (
    <Layout className="app-layout">
      <a className="skip-link" href="#protocol-workspace">跳到协议工作区</a>
      <AppHeader onOpenDiagnostics={() => setDiagnosticOpen(true)} onExit={exitApp} />
      <Content className="app-content">
        <main className="main-container" id="protocol-workspace">
          <section className="panel-left" aria-label="RGB-D 实时预览">
            <PreviewPanel
              previewData={collector.previewData}
              previewStatus={collector.previewStatus}
              distanceInfo={collector.distanceInfo}
              isCapturing={collector.busyAction === 'capture'}
              cameraStatus={collector.cameraStatus}
            />
          </section>
          <aside className="panel-right" aria-label="采集协议控制台">
            <ControlPanel
              connected={collector.connected}
              cameraStatus={collector.cameraStatus}
              isCameraConnecting={collector.isCameraConnecting}
              isCameraDisconnecting={collector.isCameraDisconnecting}
              catalog={collector.catalog}
              subjects={collector.subjects}
              protocolState={collector.protocolState}
              protocolLoading={collector.protocolLoading}
              protocolError={collector.protocolError}
              busyAction={collector.busyAction}
              actions={collector.controlActions}
            />
          </aside>
        </main>
      </Content>
      <Footer className="app-footer">
        <StatusBar
          connected={collector.connected}
          cameraConnected={collector.cameraStatus?.connected}
          subjectId={collector.protocolState?.subject_id}
          progress={collector.protocolState?.progress}
        />
      </Footer>
      <BackendDiagnostic
        open={diagnosticOpen}
        connected={collector.connected}
        backendHost={backendHost}
        onClose={() => setDiagnosticOpen(false)}
        onReconnect={reconnectBackend}
      />
    </Layout>
  );
}

export default function App() {
  return (
    <ConfigProvider
      theme={{
        algorithm: theme.defaultAlgorithm,
        token: {
          colorPrimary: '#2F6F9F', colorInfo: '#2F6F9F', colorSuccess: '#1F8A78',
          colorWarning: '#A86F00', colorError: '#B43C3C', colorBgLayout: '#EEF3F7',
          colorBgContainer: '#FFFFFF', colorText: '#1F2A33', colorTextSecondary: '#526671',
          colorBorder: '#CAD8E1', borderRadius: 8, controlHeight: 40, wireframe: false
        }
      }}
    >
      <AntApp><AppContent /></AntApp>
    </ConfigProvider>
  );
}
