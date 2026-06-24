import React, { useState } from 'react';
import { Card, Button, Checkbox, Space, Typography, Tag, List, Input, Select, Tooltip } from 'antd';
import {
  CameraOutlined,
  AudioOutlined,
  AudioMutedOutlined,
  FolderOutlined,
  HistoryOutlined,
  ReloadOutlined,
  DatabaseOutlined,
  CheckCircleOutlined
} from '@ant-design/icons';
import './ControlPanel.css';

const { Text, Title } = Typography;

const statusColors = {
  optimal: '#1F9D8A',
  too_close: '#C84A4A',
  too_far: '#C58A12',
  no_data: '#8796A1',
  no_human: '#8796A1'
};

const ControlPanel = ({
  connected,
  distanceInfo,
  isCapturing,
  captureCount,
  captureHistory = [],
  sessionId,
  sessions = [],
  voiceActive,
  onCapture,
  onCreateSession,
  onSelectSession,
  onFinishSession,
  onRefreshSessions,
  onViewImage
}) => {
  const [saveRgb, setSaveRgb] = useState(true);
  const [saveDepth, setSaveDepth] = useState(true);
  const [savePointcloud, setSavePointcloud] = useState(true);
  const [sessionName, setSessionName] = useState('');

  const handleCreateSession = () => {
    const name = sessionName.trim() || undefined;
    onCreateSession(name);
  };

  const handleCapture = () => {
    onCapture({
      save_rgb: saveRgb,
      save_depth: saveDepth,
      save_pointcloud: savePointcloud,
      colored_pointcloud: true,
      quality_check: true
    });
  };

  const distanceStatus = distanceInfo?.status || 'no_data';
  const distanceM = distanceInfo?.distance_mm
    ? (distanceInfo.distance_mm / 1000).toFixed(2)
    : '--';
  const currentStatusColor = statusColors[distanceStatus] || statusColors.no_data;

  const distancePercent = distanceInfo?.distance_mm
    ? Math.min(Math.max((distanceInfo.distance_mm / 1000 / 2.0) * 100, 0), 100)
    : 0;

  return (
    <div className="control-panel">
      <Card
        title="控制面板"
        className="control-card"
        variant="borderless"
      >
        <div className="control-section distance-section">
          <div className="distance-display">
            <div
              className="distance-circle"
              style={{
                '--distance-color': currentStatusColor,
                '--distance-percent': `${distancePercent}%`
              }}
            >
              <div className="distance-number">{distanceM}</div>
              <div className="distance-unit">米</div>
            </div>
            <Tag
              color={currentStatusColor}
              className="distance-status-tag"
            >
              {distanceInfo?.message || '等待距离数据...'}
            </Tag>
          </div>
        </div>

        <div className="control-section">
          <Title level={5}>
            <Space>
              <FolderOutlined />
              采集会话
            </Space>
          </Title>
          <Space.Compact className="session-create">
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
            <div className="session-selector">
              <Select
                placeholder="选择已有会话"
                value={sessionId}
                onChange={onSelectSession}
                size="small"
                options={sessions.map(s => ({ label: s, value: s }))}
              />
              <Tooltip title="刷新会话列表">
                <Button
                  icon={<ReloadOutlined />}
                  size="small"
                  onClick={onRefreshSessions}
                  aria-label="刷新会话列表"
                />
              </Tooltip>
            </div>
          )}

          {sessionId && (
            <Tag color="success" className="current-session-tag">
              当前会话：{sessionId}
            </Tag>
          )}
        </div>

        <div className="control-section">
          <Title level={5}>
            <Space>
              <DatabaseOutlined />
              数据类型
            </Space>
          </Title>
          <Space direction="vertical" size={6} className="data-type-list">
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

        <div className="control-section action-section">
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
          <div className="control-voice-section">
            <div className={`control-voice-indicator ${voiceActive ? 'active' : ''}`}>
              {voiceActive ? (
                <AudioOutlined className="control-voice-mic-icon active" />
              ) : (
                <AudioMutedOutlined className="control-voice-mic-icon" />
              )}
              <div>
                <div className={`control-voice-status-text ${voiceActive ? 'active' : ''}`}>
                  {voiceActive ? '正在接收语音...' : '等待语音输入'}
                </div>
                <Text type="secondary" className="control-voice-meta">
                  {connected ? '语音识别已启用' : '未连接服务器'}
                </Text>
              </div>
            </div>
            <div className="control-voice-commands">
              <Text type="secondary" className="command-label">支持指令：</Text>
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
            <Text>
              已采集 <Text strong className="accent-number">{captureCount}</Text> 组
            </Text>
          </div>
          <List
            size="small"
            className="history-list"
            dataSource={captureHistory}
            renderItem={item => (
              <List.Item
                onClick={() => item.hasImage && onViewImage?.(item.filename)}
                className={item.hasImage ? 'history-item clickable' : 'history-item'}
              >
                <span className="history-id">
                  <CheckCircleOutlined />
                  <Text type="secondary">{item.id}</Text>
                </span>
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
