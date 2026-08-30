import React from 'react';
import { AimOutlined, ApiOutlined, PoweroffOutlined } from '@ant-design/icons';
import { Button, Space, Tooltip, Typography } from 'antd';

const { Title, Text } = Typography;

export default function AppHeader({ onOpenDiagnostics, onExit }) {
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
