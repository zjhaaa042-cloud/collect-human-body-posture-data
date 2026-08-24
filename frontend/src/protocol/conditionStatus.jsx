import React from 'react';
import { CheckCircleOutlined, ClockCircleOutlined, CloseCircleOutlined } from '@ant-design/icons';

const STATUS = {
  captured: { color: 'success', icon: <CheckCircleOutlined />, text: '已采集' },
  complete: { color: 'success', icon: <CheckCircleOutlined />, text: '已采集' },
  passed: { color: 'success', icon: <CheckCircleOutlined />, text: '已采集' },
  pass: { color: 'success', icon: <CheckCircleOutlined />, text: '已采集' },
  success: { color: 'success', icon: <CheckCircleOutlined />, text: '已采集' },
  failed: { color: 'error', icon: <CloseCircleOutlined />, text: '失败' },
  retake: { color: 'warning', icon: <CloseCircleOutlined />, text: '需补采' },
  needs_retake: { color: 'warning', icon: <CloseCircleOutlined />, text: '需补采' },
  review_required: { color: 'warning', icon: <ClockCircleOutlined />, text: '待复核' },
  pending_review: { color: 'warning', icon: <ClockCircleOutlined />, text: '待复核' },
  awaiting_review: { color: 'warning', icon: <ClockCircleOutlined />, text: '待复核' },
  warn: { color: 'warning', icon: <ClockCircleOutlined />, text: '待复核' },
  in_progress: { color: 'processing', icon: <ClockCircleOutlined />, text: '采集中' },
  pending: { color: 'default', icon: <ClockCircleOutlined />, text: '待采集' }
};

export const statusKey = (value) => String(value || 'pending').toLowerCase();
export const isCaptured = (value) => ['captured', 'complete', 'passed', 'pass', 'success'].includes(statusKey(value));
export const conditionStatus = (value) => STATUS[statusKey(value)] || (isCaptured(value) ? STATUS.captured : STATUS.pending);
