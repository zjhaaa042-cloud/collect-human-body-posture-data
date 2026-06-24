import React, { useState, useEffect, useCallback, useRef } from 'react';
import { ConfigProvider, theme, Layout, Typography, Space, Button, message, Modal } from 'antd';
import { SettingOutlined, QuestionCircleOutlined, PoweroffOutlined } from '@ant-design/icons';
import PreviewPanel from './components/PreviewPanel';
import ControlPanel from './components/ControlPanel';
import StatusBar from './components/StatusBar';
import './styles/App.css';

const { Header, Content, Footer } = Layout;
const { Title } = Typography;

const WS_URL = 'ws://localhost:8765';

function App() {
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
  const [leftPanelWidth, setLeftPanelWidth] = useState(70);
  const isResizing = useRef(false);
  const containerRef = useRef(null);
  const wsRef = useRef(null);

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

  const connectWebSocket = useCallback(() => {
    try {
      const ws = new WebSocket(WS_URL);

      ws.onopen = () => {
        setConnected(true);
        setIsCapturing(false);
        message.success('已连接到采集系统');
        ws.send(JSON.stringify({ type: 'start_preview' }));
        ws.send(JSON.stringify({ type: 'get_sessions' }));
        ws.send(JSON.stringify({ type: 'get_captures' }));
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          switch (data.type) {
            case 'preview_frame':
              setPreviewData(data.data);
              if (data.data.distance) {
                setDistanceInfo(data.data.distance);
              }
              break;
            case 'distance_update':
              setDistanceInfo(data.data);
              break;
            case 'capture_result':
              setCaptureResult(data.data);
              if (data.data.success) {
                message.success('采集成功');
                wsRef.current?.send(JSON.stringify({ type: 'get_captures' }));
              } else {
                message.error('采集失败: ' + data.data.error);
              }
              setIsCapturing(false);
              break;
            case 'capture_list':
              setCaptureCount(data.data.count);
              setCaptureHistory(
                (data.data.captures || []).map(c => ({
                  id: `cap_${String(c.index).padStart(3, '0')}`,
                  filename: c.filename,
                  time: new Date(c.time * 1000).toLocaleTimeString()
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
            case 'session_list':
              setSessions(data.data.sessions || []);
              break;
            case 'exit_confirm':
              message.info(data.data.message);
              break;
            case 'capture_image':
              if (data.data.image) {
                setSelectedImage(`data:image/jpeg;base64,${data.data.image}`);
              }
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
        message.warning('连接已断开，正在重连...');
        setTimeout(connectWebSocket, 3000);
      };

      ws.onerror = (error) => {
        console.error('WebSocket error:', error);
      };

      wsRef.current = ws;
    } catch (e) {
      console.error('Failed to connect:', e);
      message.error('连接失败');
    }
  }, []);

  useEffect(() => {
    connectWebSocket();
    return () => {
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
  }, []);

  const handleCapture = useCallback(() => {
    if (!connected) {
      message.warning('未连接到服务器');
      return;
    }
    if (!sessionId) {
      message.warning('请先创建或选择采集会话');
      return;
    }
    setIsCapturing(true);
    sendCommand('capture_single');
  }, [sendCommand, connected, sessionId]);

  const handleCreateSession = useCallback((sessionName) => {
    sendCommand('create_session', { session_name: sessionName });
  }, [sendCommand]);

  const handleSelectSession = useCallback((name) => {
    sendCommand('select_session', { session_name: name });
  }, [sendCommand]);

  const handleFinishSession = useCallback(() => {
    sendCommand('speak', { text: '采集完成' });
    sendCommand('finish_session');
  }, [sendCommand]);

  const handleRefreshSessions = useCallback(() => {
    sendCommand('get_sessions');
  }, [sendCommand]);

  const handleViewImage = useCallback((filename) => {
    sendCommand('get_capture_image', { filename });
  }, [sendCommand]);

  const handleExit = useCallback(() => {
    Modal.confirm({
      title: '确认退出',
      content: '确定要关闭采集系统吗？',
      okText: '确定退出',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: () => {
        sendCommand('exit_app');
        message.info('正在关闭系统...');
        setTimeout(() => {
          window.close();
        }, 2000);
      }
    });
  }, [sendCommand]);

  return (
    <ConfigProvider
      theme={{
        algorithm: theme.darkAlgorithm,
        token: {
          colorPrimary: '#FF6900',
          colorBgContainer: '#1A1A1A',
          colorBgElevated: '#2A2A2A',
          borderRadius: 12,
          colorText: '#FFFFFF',
          colorTextSecondary: '#B3B3B3',
        },
      }}
    >
      <Layout className="app-layout">
        <Header className="app-header">
          <div className="header-left">
            <div className="logo">
              <div className="logo-icon">
                <svg width="32" height="32" viewBox="0 0 32 32" fill="none" role="img" aria-label="Logo">
                  <rect width="32" height="32" rx="8" fill="url(#gradient)" />
                  <path d="M16 8L22 12V20L16 24L10 20V12L16 8Z" fill="white" fillOpacity="0.9" />
                  <defs>
                    <linearGradient id="gradient" x1="0" y1="0" x2="32" y2="32">
                      <stop stopColor="#FF6900" />
                      <stop offset="1" stopColor="#FF8533" />
                    </linearGradient>
                  </defs>
                </svg>
              </div>
              <Title level={4} className="logo-text">体态数据采集系统</Title>
            </div>
          </div>
          <div className="header-right">
            <Space>
              <Button type="text" icon={<SettingOutlined />} className="header-btn" />
              <Button type="text" icon={<QuestionCircleOutlined />} className="header-btn" />
              <Button
                type="text"
                icon={<PoweroffOutlined />}
                className="header-btn exit-btn"
                onClick={handleExit}
                danger
              />
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
              />
            </div>

            <div className="panel-splitter" onMouseDown={handleResizeStart} role="separator" aria-orientation="vertical" />
            
            <div className="panel-right" style={{ width: `${100 - leftPanelWidth}%` }}>
              <ControlPanel
                connected={connected}
                distanceInfo={distanceInfo}
                isCapturing={isCapturing}
                captureCount={captureCount}
                captureHistory={captureHistory}
                sessionId={sessionId}
                sessions={sessions}
                voiceActive={voiceActive}
                onCapture={handleCapture}
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
      </Layout>

      <Modal
        open={!!selectedImage}
        title="采集图像"
        footer={null}
        onCancel={() => setSelectedImage(null)}
        width={700}
        centered
      >
        {selectedImage && (
          <img src={selectedImage} alt="capture" style={{ width: '100%' }} />
        )}
      </Modal>
    </ConfigProvider>
  );
}

export default App;
