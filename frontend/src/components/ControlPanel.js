import React, { useState } from 'react';
import { Card, Button, Checkbox, Space, Typography, Tag, List, Input, Select, message } from 'antd';
import {
  CameraOutlined,
  AudioOutlined,
  AudioMutedOutlined,
  FolderOutlined,
  HistoryOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  ReloadOutlined
} from '@ant-design/icons';
import './ControlPanel.css';

const { Text, Title } = Typography;

const ControlPanel = ({
  connected,
  distanceInfo,
  isCapturing,
  captureCount,
  sessionId,
  sessions = [],
  voiceActive,
  onCapture,
  onCreateSession,
  onSelectSession,
  onFinishSession,
  onRefreshSessions
}) => {
  const [saveRgb, setSaveRgb] = useState(true);
  const [saveDepth, setSaveDepth] = useState(true);
  const [savePointcloud, setSavePointcloud] = useState(true);
  const [sessionName, setSessionName] = useState('');
  const [captureHistory, setCaptureHistory] = useState([]);

  const handleCreateSession = () => {
    const name = sessionName.trim() || undefined;
    onCreateSession(name);
    message.success('会话已创建');
  };

  const handleCapture = () => {
    if (!sessionId) {
      message.warning('请先创建或选择采集会话');
      return;
    }
    onCapture();
    setCaptureHistory(prev => [
      { id: captureCount + 1, time: new Date().toLocaleTimeString() },
      ...prev.slice(0, 9)
    ]);
  };

  const distanceStatus = distanceInfo?.status || 'no_data';
  const distanceM = distanceInfo?.distance_mm
    ? (distanceInfo.distance_mm / 1000).toFixed(2)
    : '--';

  const statusColors = {
    optimal: '#00C853',
    too_close: '#FF1744',
    too_far: '#FFD600',
    no_data: '#808080'
  };

  return (
    <div className="control-panel">
      <Card
        title="控制面板"
        className="control-card"
        bordered={false}
      >
        <div className="control-section">
          <div className="distance-display">
            <div className="distance-circle" style={{ borderColor: statusColors[distanceStatus] }}>
              <div className="distance-number">{distanceM}</div>
              <div className="distance-unit">米</div>
            </div>
            <Tag
              color={statusColors[distanceStatus]}
              className="distance-status-tag"
            >
              {distanceInfo?.message || '等待数据...'}
            </Tag>
          </div>
        </div>

        <div className="control-section">
          <Title level={5}>采集会话</Title>
          <Space.Compact style={{ width: '100%' }}>
            <Input
              placeholder="新建会话名称"
              value={sessionName}
              onChange={e => setSessionName(e.target.value)}
              prefix={<FolderOutlined />}
              size="small"
            />
            <Button type="primary" onClick={handleCreateSession} size="small">
              新建
            </Button>
          </Space.Compact>
          
          {sessions.length > 0 && (
            <div className="session-selector" style={{ marginTop: 8 }}>
              <Select
                placeholder="选择已有会话"
                value={sessionId}
                onChange={onSelectSession}
                style={{ flex: 1 }}
                size="small"
                options={sessions.map(s => ({ label: s, value: s }))}
              />
              <Button 
                icon={<ReloadOutlined />} 
                size="small" 
                onClick={onRefreshSessions}
              />
            </div>
          )}
          
          {sessionId && (
            <Tag color="success" style={{ marginTop: 6, fontSize: 11 }}>
              当前: {sessionId}
            </Tag>
          )}
        </div>

        <div className="control-section">
          <Title level={5}>数据类型</Title>
          <Space direction="vertical" size={4}>
            <Checkbox checked={saveRgb} onChange={e => setSaveRgb(e.target.checked)}>
              RGB 彩色图像
            </Checkbox>
            <Checkbox checked={saveDepth} onChange={e => setSaveDepth(e.target.checked)}>
              深度数据 (NPZ)
            </Checkbox>
            <Checkbox checked={savePointcloud} onChange={e => setSavePointcloud(e.target.checked)}>
              3D 点云 (PLY)
            </Checkbox>
          </Space>
        </div>

        <div className="control-section">
          <Button
            type="primary"
            size="large"
            icon={<CameraOutlined />}
            block
            loading={isCapturing}
            onClick={handleCapture}
            className="capture-button"
          >
            {isCapturing ? '采集中...' : '开始采集'}
          </Button>
          <Button
            block
            onClick={onFinishSession}
            style={{ marginTop: 6 }}
            disabled={!sessionId}
            size="small"
          >
            完成采集
          </Button>
        </div>

        <div className="control-section">
          <Title level={5}>
            <Space>
              <AudioOutlined />
              语音控制
            </Space>
          </Title>
          <div className="voice-section">
            <div className={`voice-indicator ${voiceActive ? 'active' : ''}`}>
              {voiceActive ? (
                <AudioOutlined className="voice-mic-icon active" />
              ) : (
                <AudioMutedOutlined className="voice-mic-icon" />
              )}
              <div>
                <div className={`voice-status-text ${voiceActive ? 'active' : ''}`}>
                  {voiceActive ? '正在接收语音...' : '等待语音输入'}
                </div>
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {connected ? '语音识别已启用' : '未连接'}
                </Text>
              </div>
            </div>
            <div className="voice-commands">
              <Text type="secondary" style={{ fontSize: 11 }}>支持指令：</Text>
              <div className="command-tags">
                <Tag>开始采集</Tag>
                <Tag>停止</Tag>
                <Tag>下一个</Tag>
                <Tag>完成</Tag>
              </div>
            </div>
          </div>
        </div>

        <div className="control-section">
          <Title level={5}>
            <Space>
              <HistoryOutlined />
              采集历史
            </Space>
          </Title>
          <div className="history-stats">
            <Text>已采集: <Text strong style={{ color: '#FF6900' }}>{captureCount}</Text> 组</Text>
          </div>
          <List
            size="small"
            className="history-list"
            dataSource={captureHistory}
            renderItem={item => (
              <List.Item>
                <Text type="secondary">#{item.id}</Text>
                <Text>{item.time}</Text>
              </List.Item>
            )}
            locale={{ emptyText: '暂无记录' }}
          />
        </div>
      </Card>
    </div>
  );
};

export default ControlPanel;
