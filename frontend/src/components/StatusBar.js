import React, { useState, useEffect, useRef } from 'react';
import { Space, Tag, Typography } from 'antd';
import {
  AudioOutlined,
  SaveOutlined,
  FolderOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined
} from '@ant-design/icons';
import './StatusBar.css';

const { Text } = Typography;

const VoiceIndicator = ({ active }) => {
  const [bars, setBars] = useState([2, 2, 2, 2, 2]);
  const intervalRef = useRef(null);

  useEffect(() => {
    if (active) {
      intervalRef.current = setInterval(() => {
        setBars([
          Math.random() * 5 + 1,
          Math.random() * 5 + 1,
          Math.random() * 5 + 1,
          Math.random() * 5 + 1,
          Math.random() * 5 + 1
        ]);
      }, 50);
    } else {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
      setBars([2, 2, 2, 2, 2]);
    }
    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
      }
    };
  }, [active]);

  return (
    <div className={`status-voice-indicator ${active ? 'active' : ''}`}>
      {bars.map((height, index) => (
        <div
          key={index}
          className="status-voice-bar"
          style={{
            height: `${height * 3}px`,
            transition: 'height 0.05s ease'
          }}
        />
      ))}
    </div>
  );
};

const StatusBar = ({ connected, captureCount, sessionId, voiceActive }) => {
  return (
    <div className="status-bar">
      <div className="status-left">
        <Space size="middle">
          <div className="status-item">
            {connected ? (
              <Tag icon={<CheckCircleOutlined />} color="success" bordered={false}>
                服务已连接
              </Tag>
            ) : (
              <Tag icon={<CloseCircleOutlined />} color="error" bordered={false}>
                服务未连接
              </Tag>
            )}
          </div>

          <div className="status-item status-voice-section">
            <VoiceIndicator active={voiceActive} />
            <Tag 
              icon={voiceActive ? <AudioOutlined /> : <CheckCircleOutlined />} 
              color={voiceActive ? "processing" : "success"} 
              bordered={false}
            >
              {voiceActive ? '正在聆听...' : '语音就绪'}
            </Tag>
          </div>

          <div className="status-item">
            <SaveOutlined />
            <Text type="secondary">
              已采集: <Text strong style={{ color: '#FF6900' }}>{captureCount}</Text> 组
            </Text>
          </div>
        </Space>
      </div>

      <div className="status-right">
        <Space size="middle">
          {sessionId && (
            <div className="status-item">
              <FolderOutlined />
              <Text type="secondary">
                会话: <Text strong>{sessionId}</Text>
              </Text>
            </div>
          )}

          <div className="status-item">
            <Text type="secondary" className="version-text">
              体态数据采集系统 v1.0
            </Text>
          </div>
        </Space>
      </div>
    </div>
  );
};

export default StatusBar;
