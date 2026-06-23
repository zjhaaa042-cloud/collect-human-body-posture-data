import React from 'react';
import { Card, Tag, Typography, Space, Tooltip } from 'antd';
import { VideoCameraOutlined, EyeOutlined, FullscreenOutlined } from '@ant-design/icons';
import './PreviewPanel.css';

const { Text } = Typography;

const DistanceIndicator = ({ distanceInfo }) => {
  if (!distanceInfo) return null;

  const statusColors = {
    optimal: '#00C853',
    too_close: '#FF1744',
    too_far: '#FFD600',
    no_data: '#808080',
    no_human: '#808080'
  };

  const statusLabels = {
    optimal: '距离合适',
    too_close: '太近',
    too_far: '太远',
    no_data: '无数据',
    no_human: '未识别到人体'
  };

  const color = statusColors[distanceInfo.status] || '#808080';
  const label = statusLabels[distanceInfo.status] || '未知';
  const distanceM = distanceInfo.distance_mm ? (distanceInfo.distance_mm / 1000).toFixed(2) : '--';

  return (
    <div className="distance-indicator" style={{ borderColor: color }}>
      <div>
        <span className="distance-value" style={{ color }}>{distanceM}</span>
        <span className="distance-unit">米</span>
      </div>
      <Tag color={color} className="distance-tag">
        {label}
      </Tag>
    </div>
  );
};

const PreviewPanel = ({ previewData, distanceInfo, isCapturing }) => {
  const colorSrc = previewData?.color
    ? `data:image/jpeg;base64,${previewData.color}`
    : null;

  const depthSrc = previewData?.depth
    ? `data:image/jpeg;base64,${previewData.depth}`
    : null;

  return (
    <div className="preview-panel">
      <Card
        title={
          <div className="preview-header">
            <Space>
              <VideoCameraOutlined />
              <span>实时预览</span>
            </Space>
            <Tooltip title="全屏">
              <FullscreenOutlined style={{ cursor: 'pointer', color: 'var(--text-secondary)' }} />
            </Tooltip>
          </div>
        }
        className="preview-card"
        bordered={false}
      >
        <div className="preview-container">
          <div className="preview-window">
            <div className="preview-label">
              <EyeOutlined /> 彩色画面
            </div>
            {colorSrc ? (
              <img src={colorSrc} alt="Color Preview" className="preview-image" />
            ) : (
              <div className="preview-placeholder">
                <VideoCameraOutlined />
                <Text type="secondary">等待画面...</Text>
              </div>
            )}
            {isCapturing && (
              <div className="capture-overlay">
                <div className="capture-indicator" />
                <Text className="capture-text">采集中...</Text>
              </div>
            )}
          </div>

          <div className="preview-window">
            <div className="preview-label">
              <EyeOutlined /> 深度画面
            </div>
            {depthSrc ? (
              <img src={depthSrc} alt="Depth Preview" className="preview-image" />
            ) : (
              <div className="preview-placeholder">
                <VideoCameraOutlined />
                <Text type="secondary">等待画面...</Text>
              </div>
            )}
          </div>
        </div>

        <DistanceIndicator distanceInfo={distanceInfo} />
      </Card>
    </div>
  );
};

export default PreviewPanel;
