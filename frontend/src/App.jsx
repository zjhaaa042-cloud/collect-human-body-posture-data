import React, { useState, useEffect, useCallback, useRef } from 'react';
import { ConfigProvider, App as AntApp, theme, Layout, Typography, Space, Button, Tooltip } from 'antd';
import {
  AimOutlined,
  SettingOutlined,
  QuestionCircleOutlined,
  PoweroffOutlined
} from '@ant-design/icons';
import PreviewPanel from './components/PreviewPanel';
import ControlPanel from './components/ControlPanel';
import StatusBar from './components/StatusBar';
import './styles/App.css';

const { Header, Content, Footer } = Layout;
const { Title, Text } = Typography;

const WS_URL = 'ws://localhost:8765';
const WS_HTTP_URL = 'http://localhost:8765';

function AppContent() {
  const { message, modal } = AntApp.useApp();
  const [connected, setConnected] = useState(false);
  const [previewData, setPreviewData] = useState(null);
  const [distanceInfo, setDistanceInfo] = useState(null);
  const [captureResult, setCaptureResult] = useState(null);
  const [isCapturing, setIsCapturing] = useState(false);
  const [captureCount, setCaptureCount] = useState(0);
  const [captureHistory, setCaptureHistory] = useState([]);
  const [sessionId, setSessionId] = useState(null);
  const [voiceActive, setVoiceActive] = useState(false);
  const [sessions, setSessions] = useState([]);
  const [selectedImage, setSelectedImage] = useState(null);
  const [cameraStatus, setCameraStatus] = useState({
    sdk_available: false,
    device_present: false,
    connected: false,
    initialized: false,
    streaming: false,
    device: {},
    message: '摄像头状态未知'
  });
  const [isCameraConnecting, setIsCameraConnecting] = useState(false);
  const [autoCaptureStatus, setAutoCaptureStatus] = useState({
    enabled: false,
    stable_frames: 0,
    required_frames: 10,
    captured: 0,
    target_count: 3,
    state: 'idle',
    message: '自动采集未开启'
  });
  const [leftPanelWidth, setLeftPanelWidth] = useState(70);
  const isResizing = useRef(false);
  const containerRef = useRef(null);
  const wsRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const shouldReconnectRef = useRef(true);
  const authTokenRef = useRef(window.electronAPI?.getWsToken?.() || '');

  const fetchAuthToken = useCallback(async () => {
    if (authTokenRef.current) return;
    try {
      const resp = await fetch(`${WS_HTTP_URL}/auth-token`);
      if (resp.ok) {
        const data = await resp.json();
        authTokenRef.current = data.token || '';
      }
    } catch {
    }
  }, []);

  const handleResizeStart = useCallback((e) => {
    e.preventDefault();
    isResizing.current = true;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  }, []);

  useEffect(() => {
    const handleResizeMove = (e) => {
      if (!isResizing.current || !containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const percent = (x / rect.width) * 100;
      setLeftPanelWidth(Math.min(Math.max(percent, 30), 80));
    };

    const handleResizeEnd = () => {
      isResizing.current = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };

    window.addEventListener('mousemove', handleResizeMove);
    window.addEventListener('mouseup', handleResizeEnd);

    return () => {
      window.removeEventListener('mousemove', handleResizeMove);
      window.removeEventListener('mouseup', handleResizeEnd);
    };
  }, []);

  const connectWebSocket = useCallback(async () => {
    if (!shouldReconnectRef.current) return;

    await fetchAuthToken();
    if (!authTokenRef.current) {
      setConnected(false);
      message.warning('正在等待后端鉴权令牌...');
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
      }
      reconnectTimerRef.current = setTimeout(connectWebSocket, 3000);
      return;
    }

    try {
      const ws = new WebSocket(WS_URL);

      ws.onopen = () => {
        const token = authTokenRef.current;
        ws.send(JSON.stringify({ type: 'auth', token }));
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          switch (data.type) {
            case 'auth_success':
              setConnected(true);
              setIsCapturing(false);
              message.success('已连接到采集系统');
              ws.send(JSON.stringify({ type: 'start_preview' }));
              ws.send(JSON.stringify({ type: 'get_camera_status' }));
              ws.send(JSON.stringify({ type: 'get_sessions' }));
              ws.send(JSON.stringify({ type: 'get_captures' }));
              break;
            case 'preview_frame':
              setPreviewData(data.data);
              if (data.data.distance) {
                setDistanceInfo(data.data.distance);
              }
              break;
            case 'distance_update':
              setDistanceInfo(data.data);
              break;
            case 'camera_status':
              setCameraStatus(data.data);
              setIsCameraConnecting(false);
              if (!data.data.connected) {
                setPreviewData(null);
                setDistanceInfo({
                  distance_mm: 0,
                  status: 'no_data',
                  message: data.data.message || '摄像头未连接',
                  confidence: 0
                });
              }
              if (data.data.action === 'connect') {
                if (data.data.connected) {
                  message.success(data.data.message || '摄像头连接成功');
                } else {
                  message.warning(data.data.message || '摄像头连接失败');
                }
              } else if (data.data.action === 'disconnect') {
                message.info('摄像头已断开');
              }
              break;
            case 'capture_result':
              setCaptureResult(data.data);
              if (data.data.success) {
                if (data.data.session_id) {
                  setSessionId(data.data.session_id);
                }
                message.success('采集成功');
                wsRef.current?.send(JSON.stringify({ type: 'get_sessions' }));
                wsRef.current?.send(JSON.stringify({ type: 'get_captures' }));
              } else {
                message.error(`采集失败：${data.data.error}`);
              }
              setIsCapturing(false);
              break;
            case 'capture_list':
              setCaptureCount(data.data.count);
              setCaptureHistory(
                (data.data.captures || []).map(c => ({
                  id: `cap_${String(c.index).padStart(3, '0')}`,
                  filename: c.filename,
                  time: c.time ? new Date(c.time * 1000).toLocaleTimeString() : '--',
                  hasImage: c.has_image !== false
                })).reverse().slice(0, 10)
              );
              break;
            case 'session_created':
              setSessionId(data.data.session_id);
              ws.send(JSON.stringify({ type: 'get_sessions' }));
              ws.send(JSON.stringify({ type: 'get_captures' }));
              break;
            case 'session_finished':
              message.info(`采集完成，共采集 ${data.data.capture_count} 组数据`);
              wsRef.current?.send(JSON.stringify({ type: 'get_captures' }));
              break;
            case 'voice_activity':
              setVoiceActive(data.data.active);
              break;
            case 'auto_capture_status':
              setAutoCaptureStatus(data.data);
              break;
            case 'session_list':
              setSessions(data.data.sessions || []);
              break;
            case 'exit_confirm':
              message.info(data.data.message);
              break;
            case 'capture_image':
              if (data.data.image) {
                setSelectedImage(`data:image/jpeg;base64,${data.data.image}`);
              } else {
                message.warning('当前记录没有可预览的 RGB 图像');
              }
              break;
            case 'error':
              message.error(data.message || '服务端处理失败');
              setIsCapturing(false);
              setIsCameraConnecting(false);
              break;
            default:
              break;
          }
        } catch (e) {
          console.error('Failed to parse message:', e);
        }
      };

      ws.onclose = () => {
        setConnected(false);
        setIsCapturing(false);
        if (!shouldReconnectRef.current) {
          return;
        }
        message.warning('连接已断开，正在重连...');
        if (reconnectTimerRef.current) {
          clearTimeout(reconnectTimerRef.current);
        }
        reconnectTimerRef.current = setTimeout(connectWebSocket, 3000);
      };

      ws.onerror = (error) => {
        console.error('WebSocket error:', error);
      };

      wsRef.current = ws;
    } catch (e) {
      console.error('Failed to connect:', e);
      message.error('连接失败');
    }
  }, [message, fetchAuthToken]);

  useEffect(() => {
    shouldReconnectRef.current = true;
    connectWebSocket();
    return () => {
      shouldReconnectRef.current = false;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
      }
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, [connectWebSocket]);

  const sendCommand = useCallback((type, data = {}) => {
    try {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type, ...data }));
      } else {
        message.warning('未连接到服务器');
      }
    } catch (e) {
      message.error('发送命令失败');
    }
  }, [message]);

  const handleCapture = useCallback((options) => {
    if (!connected) {
      message.warning('未连接到服务器');
      return;
    }
    setIsCapturing(true);
    sendCommand('capture_single', { options });
  }, [sendCommand, connected, message]);

  const handleStartAutoCapture = useCallback((payload) => {
    if (!connected) {
      message.warning('未连接到服务器');
      return;
    }
    sendCommand('start_auto_capture', payload);
  }, [sendCommand, connected, message]);

  const handleStopAutoCapture = useCallback(() => {
    sendCommand('stop_auto_capture');
  }, [sendCommand]);

  const handleConnectCamera = useCallback((deviceId) => {
    if (!connected) {
      message.warning('未连接到服务器');
      return;
    }
    setIsCameraConnecting(true);
    sendCommand('connect_camera', { device_id: deviceId || '' });
  }, [sendCommand, connected, message]);

  const handleDisconnectCamera = useCallback(() => {
    sendCommand('disconnect_camera');
  }, [sendCommand]);

  const handleRefreshCameraStatus = useCallback(() => {
    sendCommand('get_camera_status');
  }, [sendCommand]);

  const handleCreateSession = useCallback((sessionName) => {
    sendCommand('create_session', { session_name: sessionName });
  }, [sendCommand]);

  const handleSelectSession = useCallback((name) => {
    sendCommand('select_session', { session_name: name });
  }, [sendCommand]);

  const handleFinishSession = useCallback(() => {
    sendCommand('finish_session');
  }, [sendCommand]);

  const handleRefreshSessions = useCallback(() => {
    sendCommand('get_sessions');
  }, [sendCommand]);

  const handleViewImage = useCallback((filename) => {
    sendCommand('get_capture_image', { filename });
  }, [sendCommand]);

  const handleExit = useCallback(() => {
    modal.confirm({
      title: '确认退出',
      content: '确定要关闭采集系统吗？',
      okText: '确定退出',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: () => {
        shouldReconnectRef.current = false;
        if (reconnectTimerRef.current) {
          clearTimeout(reconnectTimerRef.current);
        }
        sendCommand('exit_app');
        message.info('正在关闭当前采集服务...');
        setTimeout(() => {
          if (window.electronAPI?.closeWindow) {
            window.electronAPI.closeWindow();
          } else {
            window.close();
          }
        }, 2000);
      }
    });
  }, [sendCommand, modal, message]);

  return (
    <Layout className="app-layout">
      <Header className="app-header">
        <div className="header-left">
          <div className="logo">
            <div className="logo-icon" aria-hidden="true">
              <AimOutlined />
            </div>
            <div className="logo-copy">
              <Title level={4} className="logo-text">体态数据采集系统</Title>
              <Text className="logo-subtitle">Posture Capture Lab</Text>
            </div>
          </div>
        </div>

        <div className="header-right">
          <Space size={8}>
            <Tooltip title="系统设置">
              <Button type="text" icon={<SettingOutlined />} className="header-btn" aria-label="系统设置" />
            </Tooltip>
            <Tooltip title="帮助说明">
              <Button type="text" icon={<QuestionCircleOutlined />} className="header-btn" aria-label="帮助说明" />
            </Tooltip>
            <Tooltip title="退出系统">
              <Button
                type="text"
                icon={<PoweroffOutlined />}
                className="header-btn exit-btn"
                onClick={handleExit}
                danger
                aria-label="退出系统"
              />
            </Tooltip>
          </Space>
        </div>
      </Header>

      <Content className="app-content">
        <div className="main-container" ref={containerRef}>
          <div className="panel-left" style={{ width: `${leftPanelWidth}%` }}>
            <PreviewPanel
              previewData={previewData}
              distanceInfo={distanceInfo}
              isCapturing={isCapturing}
              cameraStatus={cameraStatus}
            />
          </div>

          <div className="panel-splitter" onMouseDown={handleResizeStart} role="separator" aria-orientation="vertical" />

          <div className="panel-right" style={{ width: `${100 - leftPanelWidth}%` }}>
            <ControlPanel
              connected={connected}
              distanceInfo={distanceInfo}
              captureResult={captureResult}
              isCapturing={isCapturing}
              captureCount={captureCount}
              captureHistory={captureHistory}
              sessionId={sessionId}
              sessions={sessions}
              voiceActive={voiceActive}
              cameraStatus={cameraStatus}
              isCameraConnecting={isCameraConnecting}
              autoCaptureStatus={autoCaptureStatus}
              onCapture={handleCapture}
              onConnectCamera={handleConnectCamera}
              onDisconnectCamera={handleDisconnectCamera}
              onRefreshCameraStatus={handleRefreshCameraStatus}
              onStartAutoCapture={handleStartAutoCapture}
              onStopAutoCapture={handleStopAutoCapture}
              onCreateSession={handleCreateSession}
              onSelectSession={handleSelectSession}
              onFinishSession={handleFinishSession}
              onRefreshSessions={handleRefreshSessions}
              onViewImage={handleViewImage}
            />
          </div>
        </div>
      </Content>

      <Footer className="app-footer">
        <StatusBar
          connected={connected}
          captureCount={captureCount}
          sessionId={sessionId}
          voiceActive={voiceActive}
        />
      </Footer>

      {selectedImage && (
        <div className="image-modal-overlay" onClick={() => setSelectedImage(null)}>
          <div className="image-modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="image-modal-header">
              <span>采集图像预览</span>
              <button className="image-modal-close" onClick={() => setSelectedImage(null)}>&times;</button>
            </div>
            <img src={selectedImage} alt="采集图像" className="capture-preview-image" />
          </div>
        </div>
      )}
    </Layout>
  );
}

function App() {
  return (
    <ConfigProvider
      theme={{
        algorithm: theme.defaultAlgorithm,
        token: {
          colorPrimary: '#2F6F9F',
          colorInfo: '#2F6F9F',
          colorSuccess: '#1F9D8A',
          colorWarning: '#C58A12',
          colorError: '#C84A4A',
          colorBgLayout: '#EEF3F7',
          colorBgContainer: '#FFFFFF',
          colorBgElevated: '#FFFFFF',
          colorText: '#1F2A33',
          colorTextSecondary: '#60727F',
          colorBorder: '#D7E1E8',
          borderRadius: 8,
          wireframe: false
        }
      }}
    >
      <AntApp>
        <AppContent />
      </AntApp>
    </ConfigProvider>
  );
}

export default App;
