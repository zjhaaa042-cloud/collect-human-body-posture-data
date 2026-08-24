import React, { useEffect, useMemo, useState } from 'react';
import { ApiOutlined, ReloadOutlined } from '@ant-design/icons';
import { Alert, Button, Select, Space, Tag, Tooltip, Typography } from 'antd';

const { Text, Title } = Typography;

export default function CameraConnection({
  connected,
  cameraStatus = {},
  isConnecting,
  isDisconnecting,
  requiredCameraCode,
  onConnect,
  onDisconnect,
  onRefresh
}) {
  const [selectedDeviceId, setSelectedDeviceId] = useState('');
  const devices = cameraStatus.devices || [];
  const options = useMemo(() => devices.map((device, index) => ({
    value: device.id,
    cameraCode: device.camera_code || '',
    label: `${device.name || 'RGB-D Camera'} · ${device.serial_number || device.uid || index + 1}${device.camera_code ? ` · ${device.camera_code}` : ''}`
  })), [devices]);
  const requiredOptions = useMemo(
    () => (requiredCameraCode
      ? options.filter((option) => option.cameraCode === requiredCameraCode)
      : options),
    [options, requiredCameraCode]
  );

  useEffect(() => {
    if (!requiredOptions.some((option) => option.value === selectedDeviceId)) {
      setSelectedDeviceId(requiredOptions[0]?.value || '');
    }
  }, [requiredOptions, selectedDeviceId]);

  const cameraConnected = Boolean(cameraStatus.connected);
  const cameraBusy = Boolean(isConnecting || isDisconnecting);
  const requiredName = requiredCameraCode === 'CD435I' ? 'Intel RealSense D435i' : 'Orbbec Gemini 336L';

  return (
    <section className="camera-connection" aria-labelledby="camera-heading">
      <div className="section-heading-row">
        <Title level={3} id="camera-heading"><ApiOutlined /> 摄像头连接</Title>
        <Tag color={cameraBusy ? 'processing' : (cameraConnected ? 'success' : 'warning')}>
          {isConnecting ? '连接中' : (isDisconnecting ? '断开中' : (cameraConnected ? '已连接' : '未连接'))}
        </Tag>
      </div>
      {requiredCameraCode && (
        <Alert
          type={requiredOptions.length ? 'info' : 'error'}
          showIcon
          message={requiredOptions.length ? `当前条件要求：${requiredName}` : `未检测到当前条件必需的 ${requiredName}`}
          description={requiredOptions.length
            ? '只列出与当前条件匹配的设备；连接后请等待预览与距离数据稳定，再开始 5 帧 burst。'
            : '请插入该型号设备并刷新。不能用另一台相机代替或复制数据。'}
        />
      )}
      <div className="camera-device-summary">
        <Text strong>{cameraStatus.device?.name || '尚未连接设备'}</Text>
        <Text type="secondary">{cameraStatus.message || '等待摄像头状态'}</Text>
      </div>
      <div className="camera-device-field">
        <label className="field-label" htmlFor="camera-device">可用摄像头</label>
        <Select
          id="camera-device"
          aria-label="可用摄像头"
          className="camera-device-select"
          placeholder="选择摄像头设备"
          value={selectedDeviceId || undefined}
          options={requiredOptions}
          onChange={setSelectedDeviceId}
          disabled={!connected || cameraConnected || cameraBusy || !requiredOptions.length}
          notFoundContent={connected ? '未检测到可选摄像头' : '采集服务未连接'}
        />
      </div>
      <Space.Compact block className="camera-actions">
        <Button type="primary" loading={isConnecting} disabled={!connected || cameraConnected || cameraBusy || !requiredOptions.length} onClick={() => onConnect(selectedDeviceId)}>连接所选设备</Button>
        <Button loading={isDisconnecting} disabled={!connected || !cameraConnected || cameraBusy} onClick={onDisconnect}>断开</Button>
        <Tooltip title="刷新摄像头状态">
          <Button icon={<ReloadOutlined />} disabled={!connected || cameraBusy} onClick={onRefresh} aria-label="刷新摄像头状态" />
        </Tooltip>
      </Space.Compact>
    </section>
  );
}
