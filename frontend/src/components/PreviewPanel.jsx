import React, { useRef, useCallback } from 'react';
import { Button, Card, Tag, Typography, Space, Tooltip } from 'antd';
import {
  VideoCameraOutlined,
  EyeOutlined,
  FullscreenOutlined,
  RadarChartOutlined
} from '@ant-design/icons';
import './PreviewPanel.css';

const { Text } = Typography;

const statusTagColors = {
  optimal: 'success',
  too_close: 'error',
  too_far: 'warning',
  no_data: 'default',
  no_human: 'default'
};

const statusLabels = {
  optimal: '距离合适',
  too_close: '太近',
  too_far: '太远',
  no_data: '无数据',
  no_human: '未识别到人体'
};

const DistanceIndicator = ({ distanceInfo }) => {
  if (!distanceInfo) return null;

  const status = distanceInfo.status || 'no_data';
  const label = statusLabels[distanceInfo.status] || '未知状态';
  const distanceM = distanceInfo.distance_mm ? (distanceInfo.distance_mm / 1000).toFixed(2) : '--';

  return (
    <div className={`distance-indicator distance-status-${status}`} role="status" aria-live="polite">
      <div className="distance-summary">
        <RadarChartOutlined />
        <div>
          <span className="distance-value">{distanceM}</span>
          <span className="distance-unit">米</span>
        </div>
      </div>
      <Tag color={statusTagColors[status] || 'default'} className="distance-tag">
        {label}
      </Tag>
    </div>
  );
};

const PreviewPanel = ({ previewData, previewStatus, distanceInfo, isCapturing, cameraStatus }) => {
  const panelRef = useRef(null);

  const handleFullscreen = useCallback(() => {
    if (!panelRef.current) return;
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else {
      panelRef.current.requestFullscreen();
    }
  }, []);

  const colorSrc = previewData?.color
    ? `data:image/jpeg;base64,${previewData.color}`
    : null;

  const depthSrc = previewData?.depth
    ? `data:image/jpeg;base64,${previewData.depth}`
    : null;
  const cameraConnected = Boolean(cameraStatus?.connected);
  const placeholderText = previewStatus === 'connecting'
    ? '正在连接摄像头并等待首帧...'
    : previewStatus === 'disconnecting'
      ? '正在安全断开摄像头...'
      : cameraConnected
        ? (previewStatus === 'recovering' ? '预览暂时中断，正在自动恢复...' : '等待画面...')
        : (cameraStatus?.message || '摄像头未连接，请在右侧点击连接摄像头');

  return (
    <div className="preview-panel" ref={panelRef}>
      <Card
        title={
          <div className="preview-header">
            <Space>
              <VideoCameraOutlined />
              <span>实时预览</span>
            </Space>
            <Tooltip title="全屏预览">
              <Button
                type="text"
                icon={<FullscreenOutlined />}
                className="preview-action"
                onClick={handleFullscreen}
                aria-label="切换全屏预览"
              />
            </Tooltip>
          </div>
        }
        className="preview-card"
        variant="borderless"
      >
        <div className="preview-container">
          <div className="preview-window">
            <div className="preview-label">
              <EyeOutlined /> RGB 彩色画面
            </div>
            {colorSrc ? (
              <img src={colorSrc} alt="RGB 彩色预览" className="preview-image" />
            ) : (
              <div className="preview-placeholder">
                <VideoCameraOutlined />
                <Text type="secondary">{placeholderText}</Text>
              </div>
            )}
            {isCapturing && (
              <div className="capture-overlay" role="status" aria-live="assertive">
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
              <img src={depthSrc} alt="深度预览" className="preview-image" />
            ) : (
              <div className="preview-placeholder">
                <VideoCameraOutlined />
                <Text type="secondary">{placeholderText}</Text>
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
