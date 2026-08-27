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

const statusColors = {
  optimal: '#1F9D8A',
  too_close: '#C84A4A',
  too_far: '#C58A12',
  no_data: '#8796A1',
  no_human: '#8796A1',
  body_incomplete: '#C58A12',
  quality_low: '#C58A12',
  unstable: '#2F6F9F'
};

const statusLabels = {
  optimal: '距离合适',
  too_close: '太近',
  too_far: '太远',
  no_data: '无数据',
  no_human: '未识别到人体',
  body_incomplete: '全身未完整入镜',
  quality_low: '数据质量不足',
  unstable: '请保持姿态稳定'
};

const DistanceIndicator = ({ distanceInfo }) => {
  if (!distanceInfo) return null;

  const status = distanceInfo.status || 'no_data';
  const color = statusColors[status] || '#8796A1';
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
      <Tag color={color} className="distance-tag">
        {distanceInfo.capture_quality?.score != null
          ? `${label} · ${Math.round(distanceInfo.capture_quality.score)}分`
          : label}
      </Tag>
    </div>
  );
};

const imageSource = (value) => value ? `data:image/jpeg;base64,${value}` : null;

const CameraPreview = ({ cameraCode, preview, placeholderText, isCapturing }) => {
  const cameraName = cameraCode === 'CD435I' ? 'D435i' : 'Gemini 336L';
  const colorSrc = imageSource(preview?.color);
  const depthSrc = imageSource(preview?.depth);
  return (
    <section className="camera-preview-group" aria-label={`${cameraName} 实时预览`}>
      <div className="camera-preview-title"><Text strong>{cameraName}</Text><Tag color={preview?.available ? 'success' : 'default'}>{preview?.available ? '实时' : '无画面'}</Tag></div>
      <div className="preview-window">
        <div className="preview-label"><EyeOutlined /> RGB 彩色画面</div>
        {colorSrc ? <img src={colorSrc} alt={`${cameraName} RGB 彩色预览`} className="preview-image" /> : <div className="preview-placeholder"><VideoCameraOutlined /><Text type="secondary">{placeholderText}</Text></div>}
        {isCapturing && <div className="capture-overlay" role="status" aria-live="assertive"><div className="capture-indicator" /><Text className="capture-text">双机采集中...</Text></div>}
      </div>
      <div className="preview-window">
        <div className="preview-label"><EyeOutlined /> 深度画面</div>
        {depthSrc ? <img src={depthSrc} alt={`${cameraName} 深度预览`} className="preview-image" /> : <div className="preview-placeholder"><VideoCameraOutlined /><Text type="secondary">{placeholderText}</Text></div>}
      </div>
    </section>
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

  const dualPreviews = previewData?.cameras;
  const colorSrc = imageSource(previewData?.color);
  const depthSrc = imageSource(previewData?.depth);
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
        {dualPreviews ? (
          <div className="dual-preview-container">
            <CameraPreview cameraCode="C336L" preview={dualPreviews.C336L} placeholderText={placeholderText} isCapturing={isCapturing} />
            <CameraPreview cameraCode="CD435I" preview={dualPreviews.CD435I} placeholderText={placeholderText} isCapturing={isCapturing} />
          </div>
        ) : <div className="preview-container">
          <div className="preview-window"><div className="preview-label"><EyeOutlined /> RGB 彩色画面</div>{colorSrc ? <img src={colorSrc} alt="RGB 彩色预览" className="preview-image" /> : <div className="preview-placeholder"><VideoCameraOutlined /><Text type="secondary">{placeholderText}</Text></div>}</div>
          <div className="preview-window"><div className="preview-label"><EyeOutlined /> 深度画面</div>{depthSrc ? <img src={depthSrc} alt="深度预览" className="preview-image" /> : <div className="preview-placeholder"><VideoCameraOutlined /><Text type="secondary">{placeholderText}</Text></div>}</div>
        </div>}

        <DistanceIndicator distanceInfo={distanceInfo} />
      </Card>
    </div>
  );
};

export default PreviewPanel;
