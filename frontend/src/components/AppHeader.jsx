import React from 'react';
import {
  AimOutlined,
  ApiOutlined,
  AudioMutedOutlined,
  AudioOutlined,
  MutedOutlined,
  PoweroffOutlined,
  SoundOutlined
} from '@ant-design/icons';
import { Button, Space, Tooltip, Typography } from 'antd';

const { Title, Text } = Typography;

export default function AppHeader({
  voiceStatus,
  onToggleVoiceOutput,
  onToggleVoiceRecognition,
  onOpenDiagnostics,
  onExit
}) {
  const outputEnabled = voiceStatus?.output_enabled !== false;
  const recognitionEnabled = voiceStatus?.recognition_enabled !== false;
  return (
    <header className="app-header">
      <div className="logo">
        <div className="logo-icon" aria-hidden="true"><AimOutlined /></div>
        <div className="logo-copy">
          <Title level={1} className="logo-text">RealAnthro 采集工作台</Title>
          <Text className="logo-subtitle">Dual-camera eight-angle RGB-D acquisition</Text>
        </div>
      </div>
      <Space size={8}>
        <Tooltip title={outputEnabled ? '关闭语音播报' : '开启语音播报'}>
          <Button
            type="text"
            icon={outputEnabled ? <SoundOutlined /> : <MutedOutlined />}
            className={`header-btn ${outputEnabled ? '' : 'header-btn-inactive'}`}
            aria-label={outputEnabled ? '关闭语音播报' : '开启语音播报'}
            aria-pressed={outputEnabled}
            onClick={() => onToggleVoiceOutput?.(!outputEnabled)}
          />
        </Tooltip>
        <Tooltip title={recognitionEnabled ? '关闭麦克风语音命令' : '开启麦克风语音命令'}>
          <Button
            type="text"
            icon={recognitionEnabled ? <AudioOutlined /> : <AudioMutedOutlined />}
            className={`header-btn ${recognitionEnabled ? '' : 'header-btn-inactive'}`}
            aria-label={recognitionEnabled ? '关闭麦克风语音命令' : '开启麦克风语音命令'}
            aria-pressed={recognitionEnabled}
            onClick={() => onToggleVoiceRecognition?.(!recognitionEnabled)}
          />
        </Tooltip>
        <Tooltip title="后端连接诊断">
          <Button
            type="text"
            icon={<ApiOutlined />}
            className="header-btn"
            aria-label="打开后端连接诊断"
            onClick={onOpenDiagnostics}
          />
        </Tooltip>
        <Tooltip title="退出系统">
          <Button
            type="text"
            icon={<PoweroffOutlined />}
            className="header-btn exit-btn"
            aria-label="退出采集系统"
            onClick={onExit}
            danger
          />
        </Tooltip>
      </Space>
    </header>
  );
}
