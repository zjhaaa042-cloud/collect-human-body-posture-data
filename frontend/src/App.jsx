import React, { useState, useEffect, useCallback, useRef } from 'react';
import { ConfigProvider, App as AntApp, theme, Layout, Typography, Space, Button, Tooltip, Modal, Input, Alert, Descriptions } from 'antd';
import {
  AimOutlined,
  SettingOutlined,
  QuestionCircleOutlined,
  PoweroffOutlined,
  ApiOutlined,
  ReloadOutlined,
  LinkOutlined
} from '@ant-design/icons';
import PreviewPanel from './components/PreviewPanel';
import ControlPanel from './components/ControlPanel';
import StatusBar from './components/StatusBar';
import './styles/App.css';

const { Header, Content, Footer } = Layout;
const { Title, Text } = Typography;

const BACKEND_PORT = '8765';

const getDefaultBackendHost = () => {
  const params = new URLSearchParams(window.location.search);
  const fromQuery = params.get('backend');
  if (fromQuery) return fromQuery.replace(/^https?:\/\//, '').replace(/^wss?:\/\//, '').replace(/\/$/, '');

  const saved = localStorage.getItem('backendHost');
  if (saved) return saved;

  const host = window.location.hostname;
  if (host && host !== 'localhost' && host !== '127.0.0.1') {
    return `${host}:${BACKEND_PORT}`;
  }
  return `localhost:${BACKEND_PORT}`;
};

const normalizeBackendHost = (value) => {
  const clean = String(value || '').trim()
    .replace(/^https?:\/\//, '')
    .replace(/^wss?:\/\//, '')
    .replace(/\/$/, '');
  return clean || `localhost:${BACKEND_PORT}`;
};

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
  const [backendHost, setBackendHost] = useState(getDefaultBackendHost);
  const [backendHostInput, setBackendHostInput] = useState(getDefaultBackendHost);
  const [backendModalOpen, setBackendModalOpen] = useState(false);
  const [backendCheck, setBackendCheck] = useState({
    checking: false,
    http: '未检测',
    token: '未检测',
    websocket: '未检测',
    detail: '尚未运行检测'
  });
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
  const wsUrl = `ws://${backendHost}`;
  const backendHttpUrl = `http://${backendHost}`;

  const fetchAuthToken = useCallback(async () => {
    if (authTokenRef.current) return;
    try {
      const resp = await fetch(`${backendHttpUrl}/auth-token`);
      if (resp.ok) {
        const data = await resp.json();
        authTokenRef.current = data.token || '';
      }
    } catch {
    }
  }, [backendHttpUrl]);

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
      const ws = new WebSocket(wsUrl);

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
  }, [message, fetchAuthToken, wsUrl]);

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

  const reconnectBackend = useCallback((nextHost = backendHost) => {
    const normalized = normalizeBackendHost(nextHost);
    localStorage.setItem('backendHost', normalized);
    setBackendHost(normalized);
    setBackendHostInput(normalized);
    authTokenRef.current = '';
    shouldReconnectRef.current = true;
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setConnected(false);
    reconnectTimerRef.current = setTimeout(connectWebSocket, 200);
  }, [backendHost, connectWebSocket]);

  const openBackendProbe = useCallback(() => {
    window.open(`${backendHttpUrl}/health`, '_blank', 'noopener,noreferrer');
  }, [backendHttpUrl]);

  const testBackend = useCallback(async () => {
    const normalized = normalizeBackendHost(backendHostInput);
    const httpUrl = `http://${normalized}`;
    const testWsUrl = `ws://${normalized}`;
    setBackendCheck({
      checking: true,
      http: '检测中',
      token: '等待中',
      websocket: '等待中',
      detail: `正在检测 ${httpUrl}`
    });

    let token = '';
    try {
      const healthResp = await fetch(`${httpUrl}/health`, { cache: 'no-store' });
      setBackendCheck(prev => ({
        ...prev,
        http: healthResp.ok ? `正常 (${healthResp.status})` : `异常 (${healthResp.status})`,
        detail: healthResp.ok ? 'HTTP 健康检查已响应' : 'HTTP 已响应，但状态码异常'
      }));
    } catch (error) {
      setBackendCheck({
        checking: false,
        http: '失败',
        token: '未检测',
        websocket: '未检测',
        detail: `HTTP 连接失败：${error.message}`
      });
      return;
    }

    try {
      const tokenResp = await fetch(`${httpUrl}/auth-token`, { cache: 'no-store' });
      if (!tokenResp.ok) {
        setBackendCheck({
          checking: false,
          http: '正常',
          token: `失败 (${tokenResp.status})`,
          websocket: '未检测',
          detail: '后端可访问，但鉴权 token 获取失败。请确认后端已更新到最新版，并允许局域网前端来源。'
        });
        return;
      }
      const data = await tokenResp.json();
      token = data.token || '';
      if (!token) throw new Error('token 为空');
      setBackendCheck(prev => ({
        ...prev,
        token: '正常',
        detail: '已获取鉴权 token，正在测试 WebSocket'
      }));
    } catch (error) {
      setBackendCheck({
        checking: false,
        http: '正常',
        token: '失败',
        websocket: '未检测',
        detail: `token 获取失败：${error.message}`
      });
      return;
    }

    await new Promise((resolve) => {
      let done = false;
      const ws = new WebSocket(testWsUrl);
      const timeout = setTimeout(() => {
        if (done) return;
        done = true;
        try { ws.close(); } catch {}
        setBackendCheck(prev => ({
          ...prev,
          checking: false,
          websocket: '超时',
          detail: 'WebSocket 握手超时。常见原因是防火墙未放行 8765 端口，或后端没有监听 0.0.0.0。'
        }));
        resolve();
      }, 5000);

      ws.onopen = () => {
        ws.send(JSON.stringify({ type: 'auth', token }));
      };
      ws.onmessage = (event) => {
        if (done) return;
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'auth_success') {
            done = true;
            clearTimeout(timeout);
            ws.close();
            setBackendCheck({
              checking: false,
              http: '正常',
              token: '正常',
              websocket: '正常',
              detail: '后端连接正常，可以保存该地址并重新连接。'
            });
            resolve();
          }
        } catch {
        }
      };
      ws.onerror = () => {
        if (done) return;
        done = true;
        clearTimeout(timeout);
        setBackendCheck(prev => ({
          ...prev,
          checking: false,
          websocket: '失败',
          detail: 'WebSocket 连接失败。请检查后端是否启动、防火墙是否放行 8765 端口、地址是否填对。'
        }));
        resolve();
      };
      ws.onclose = () => {
        if (done) return;
        done = true;
        clearTimeout(timeout);
        setBackendCheck(prev => ({
          ...prev,
          checking: false,
          websocket: '失败',
          detail: 'WebSocket 在鉴权前关闭。请查看后端控制台日志。'
        }));
        resolve();
      };
    });
  }, [backendHostInput]);

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
            <Tooltip title="后端连接诊断">
              <Button
                type="text"
                icon={<ApiOutlined />}
                className="header-btn"
                aria-label="后端连接诊断"
                onClick={() => setBackendModalOpen(true)}
              />
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

      <Modal
        title="后端连接诊断"
        open={backendModalOpen}
        onCancel={() => setBackendModalOpen(false)}
        footer={[
          <Button key="open" icon={<LinkOutlined />} onClick={openBackendProbe}>
            打开后端
          </Button>,
          <Button key="test" icon={<ApiOutlined />} loading={backendCheck.checking} onClick={testBackend}>
            检测连接
          </Button>,
          <Button key="save" type="primary" icon={<ReloadOutlined />} onClick={() => reconnectBackend(backendHostInput)}>
            保存并重连
          </Button>
        ]}
      >
        <Space direction="vertical" size={12} className="backend-diagnostic">
          <Alert
            type={connected ? 'success' : 'warning'}
            showIcon
            message={connected ? '当前已连接后端' : '当前未连接后端'}
            description={`当前地址：${backendHttpUrl}`}
          />
          <Input
            addonBefore="后端地址"
            value={backendHostInput}
            onChange={e => setBackendHostInput(e.target.value)}
            placeholder="例如 192.168.1.23:8765"
          />
          <Descriptions size="small" bordered column={1}>
            <Descriptions.Item label="HTTP 健康检查">{backendCheck.http}</Descriptions.Item>
            <Descriptions.Item label="鉴权 token">{backendCheck.token}</Descriptions.Item>
            <Descriptions.Item label="WebSocket">{backendCheck.websocket}</Descriptions.Item>
            <Descriptions.Item label="说明">{backendCheck.detail}</Descriptions.Item>
          </Descriptions>
          <Alert
            type="info"
            showIcon
            message="跨设备连接提示"
            description="如果前端运行在另一台设备，请把后端地址填成运行后端那台电脑的局域网 IP，例如 192.168.1.23:8765，并确认 Windows 防火墙放行 8765 端口。"
          />
        </Space>
      </Modal>
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
