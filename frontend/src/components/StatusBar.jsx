import React from 'react';
import { CameraOutlined, CheckCircleOutlined, CloseCircleOutlined, FolderOutlined, SaveOutlined } from '@ant-design/icons';
import { Space, Tag, Typography } from 'antd';
import './StatusBar.css';

const { Text } = Typography;

export default function StatusBar({ connected, cameraConnected, subjectId, progress = {} }) {
  return (
    <div className="status-bar" role="status" aria-live="polite">
      <Space size="middle" wrap>
        <Tag icon={connected ? <CheckCircleOutlined /> : <CloseCircleOutlined />} color={connected ? 'success' : 'error'} bordered={false}>
          {connected ? '采集服务已连接' : '采集服务未连接'}
        </Tag>
        <Tag icon={<CameraOutlined />} color={cameraConnected ? 'success' : 'warning'} bordered={false}>
          {cameraConnected ? '摄像头已连接' : '摄像头未连接'}
        </Tag>
        <span className="status-item">
          <SaveOutlined />
          <Text type="secondary">协议进度 <Text strong className="accent-number">{progress.captured ?? 0}/{progress.expected ?? 0}</Text></Text>
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
