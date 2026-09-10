import React, { useEffect, useState } from 'react';
import {
  AudioMutedOutlined,
  AudioOutlined,
  CameraOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  FolderOutlined,
  SaveOutlined,
  SoundOutlined
} from '@ant-design/icons';
import { Space, Tag, Tooltip, Typography } from 'antd';
import { voiceStatusPresentation } from '../collector/voiceState.mjs';
import './StatusBar.css';

const { Text } = Typography;

export default function StatusBar({
  connected,
  cameraConnected,
  voiceStatus,
  subjectId,
  progress = {}
}) {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (!voiceStatus?.capture_armed) return undefined;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [voiceStatus?.capture_armed, voiceStatus?.expires_at]);

  const voice = voiceStatusPresentation(voiceStatus, now);
  const voiceIcon = voice.state === 'error' || voice.state === 'off'
    ? <AudioMutedOutlined />
    : voice.state === 'speaking' || voice.state === 'output-only'
      ? <SoundOutlined />
      : <AudioOutlined />;
  const voiceActive = ['armed', 'speaking', 'listening'].includes(voice.state);
  const accessibleVoiceText = voice.state === 'armed'
    ? '采集语音待命已开启'
    : voice.text;
  return (
    <div className="status-bar">
      <span className="status-sr-only" role="status" aria-live="polite" aria-atomic="true">
        {accessibleVoiceText}
      </span>
      <Space size="middle" wrap>
        <Tag icon={connected ? <CheckCircleOutlined /> : <CloseCircleOutlined />} color={connected ? 'success' : 'error'} bordered={false}>
          {connected ? '采集服务已连接' : '采集服务未连接'}
        </Tag>
        <Tag icon={<CameraOutlined />} color={cameraConnected ? 'success' : 'warning'} bordered={false}>
          {cameraConnected ? '摄像头已连接' : '摄像头未连接'}
        </Tag>
        <span className="status-item status-voice-section">
          <span className={`status-voice-indicator ${voiceActive ? 'active' : ''}`} aria-hidden="true">
            {[0, 1, 2, 3].map((index) => (
              <span className="status-voice-bar" key={index} />
            ))}
          </span>
          <Tooltip title={voiceStatus?.last_error || '口令：开始采集、重复提示、取消采集'}>
            <Tag icon={voiceIcon} color={voice.color} bordered={false}>
              {voice.text}
            </Tag>
          </Tooltip>
        </span>
        <span className="status-item">
          <SaveOutlined />
          <Text type="secondary">八角度进度 <Text strong className="accent-number">{progress.captured ?? 0}/{progress.expected ?? 8}</Text></Text>
        </span>
      </Space>
      <Space size="middle" wrap>
        {subjectId && (
          <span className="status-item"><FolderOutlined /><Text type="secondary">受试者：<Text strong>{subjectId}</Text></Text></span>
        )}
        <Text type="secondary" className="version-text">RealAnthro Collector v1.0</Text>
      </Space>
    </div>
  );
}
