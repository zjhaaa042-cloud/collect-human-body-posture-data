import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { isReviewRequiredStatus, unwrapPayload } from '../protocol/protocolUtils.mjs';

const EMPTY_CAMERA = {
  connected: false,
  device_present: false,
  devices: [],
  device: {},
  message: '摄像头状态未知'
};

const normalizeReviewPreview = (payload = {}, fallback = {}) => {
  const source = payload.review_preview || payload.preview || payload;
  return {
    ...source,
    subject_id: source.subject_id || payload.subject_id || fallback.subject_id,
    condition_id: source.condition_id || payload.condition_id || fallback.condition_id,
    attempt_id: source.attempt_id || payload.attempt_id || fallback.attempt_id,
    anchor_frame: source.anchor_frame || payload.anchor_frame || 'F03'
  };
};

export default function useCollectorSocket({ backendHost, message }) {
  const socketRef = useRef(null);
  const reviewPreviewRequestRef = useRef(null);
  const activeSubjectIdRef = useRef('');
  const selectedConditionIdRef = useRef('');
  const [connected, setConnected] = useState(false);
  const [previewData, setPreviewData] = useState(null);
  const [previewStatus, setPreviewStatus] = useState('disconnected');
  const [distanceInfo, setDistanceInfo] = useState(null);
  const [cameraStatus, setCameraStatus] = useState(EMPTY_CAMERA);
  const [isCameraConnecting, setIsCameraConnecting] = useState(false);
  const [isCameraDisconnecting, setIsCameraDisconnecting] = useState(false);
  const [catalog, setCatalog] = useState({ profiles: [], measurements: [], default_profile_id: null });
  const [subjects, setSubjects] = useState([]);
  const [protocolState, setProtocolState] = useState(null);
  const [protocolLoading, setProtocolLoading] = useState(false);
  const [protocolError, setProtocolError] = useState('');
  const [busyAction, setBusyAction] = useState('');
  const [lastCaptureResult, setLastCaptureResult] = useState(null);
  const [completionReport, setCompletionReport] = useState(null);
  const [reviewPreview, setReviewPreview] = useState(null);
  const [reviewPreviewLoading, setReviewPreviewLoading] = useState(false);
  const [reviewPreviewError, setReviewPreviewError] = useState('');

  useEffect(() => {
    let active = true;
    let reconnectTimer;
    let catalogTimer;
    let previewWatchdogTimer;
    let socket;
    let cameraConnected = false;
    let previewExpectedAt = 0;
    let lastPreviewAt = 0;
    let lastPreviewRestartAt = 0;

    const sendOn = (target, type, payload = {}) => {
      if (target?.readyState === WebSocket.OPEN) {
        target.send(JSON.stringify({ type, ...payload }));
      }
    };

    const applyReturnedState = (payload) => {
      const nextState = payload?.state || payload?.subject_state;
      if (nextState?.subject_id) {
        activeSubjectIdRef.current = nextState.subject_id;
        setProtocolState(nextState);
      }
    };
    const markReconciliationRequired = (payload) => {
      if (!payload?.reconciliation_required) return;
      setProtocolState((previous) => (
        previous && (!payload.subject_id || previous.subject_id === payload.subject_id)
          ? { ...previous, reconciliation_required: true }
          : previous
      ));
    };

    const handleMessage = (event) => {
      let packet;
      try {
        packet = JSON.parse(event.data);
      } catch {
        setProtocolError('服务端返回了无法解析的数据');
        return;
      }
      const payload = unwrapPayload(packet);
      switch (packet.type) {
        case 'auth_success':
          setConnected(true);
          setPreviewStatus('waiting');
          setProtocolLoading(true);
          setProtocolError('');
          sendOn(socket, 'start_preview');
          sendOn(socket, 'get_camera_status');
          sendOn(socket, 'get_protocol_catalog');
          sendOn(socket, 'get_protocol_subjects');
          if (activeSubjectIdRef.current) {
            sendOn(socket, 'select_protocol_subject', {
              subject_id: activeSubjectIdRef.current
            });
            if (selectedConditionIdRef.current) {
              sendOn(socket, 'set_protocol_preview_condition', {
                subject_id: activeSubjectIdRef.current,
                condition_id: selectedConditionIdRef.current
              });
            }
          }
          catalogTimer = window.setTimeout(() => setProtocolLoading(false), 4000);
          break;
        case 'preview_frame':
          setPreviewData(payload);
          if (payload.color || payload.depth) {
            lastPreviewAt = Date.now();
            setPreviewStatus('live');
          }
          if (payload.distance) setDistanceInfo(payload.distance);
          break;
        case 'distance_update':
          setDistanceInfo(payload);
          break;
        case 'camera_status':
          cameraConnected = Boolean(payload.connected);
          setCameraStatus(payload);
          setIsCameraConnecting(false);
          setIsCameraDisconnecting(false);
          if (cameraConnected) {
            previewExpectedAt = previewExpectedAt || Date.now();
            setPreviewStatus(lastPreviewAt ? 'live' : 'waiting');
            if (payload.action === 'connect') sendOn(socket, 'start_preview');
          } else {
            previewExpectedAt = 0;
            lastPreviewAt = 0;
            setPreviewData(null);
            setDistanceInfo(null);
            setPreviewStatus('disconnected');
          }
          break;
        case 'camera_operation': {
          const operation = String(payload.state || '').toLowerCase();
          setIsCameraConnecting(operation === 'connecting');
          setIsCameraDisconnecting(operation === 'disconnecting');
          if (operation === 'connecting') {
            setPreviewData(null);
            setDistanceInfo(null);
            setPreviewStatus('connecting');
          } else if (operation === 'disconnecting') {
            setPreviewData(null);
            setDistanceInfo(null);
            setPreviewStatus('disconnecting');
          }
          break;
        }
        case 'protocol_catalog':
          window.clearTimeout(catalogTimer);
          setCatalog({
            ...payload,
            profiles: payload.profiles || [],
            measurements: payload.measurements || [],
            default_profile_id: payload.default_profile_id || null
          });
          setProtocolLoading(false);
          setProtocolError('');
          break;
        case 'protocol_subject_list':
          setSubjects(payload.subjects || []);
          break;
        case 'protocol_subject_list_changed':
          sendOn(socket, 'get_protocol_subjects');
          break;
        case 'protocol_subject_state':
          if ((payload.state || payload)?.subject_id) {
            activeSubjectIdRef.current = (payload.state || payload).subject_id;
          }
          setProtocolState(payload.state || payload);
          setBusyAction('');
          break;
        case 'protocol_capture_result':
          applyReturnedState(payload);
          markReconciliationRequired(payload);
          setLastCaptureResult(payload);
          if (payload.reconciliation_required) {
            setReviewPreview(null);
            setReviewPreviewError(payload.error || payload.message || '状态账本待恢复，禁止继续操作');
          } else if (payload.review_preview?.source === 'verified_committed_files') {
            setReviewPreview(normalizeReviewPreview(payload, {
              condition_id: payload.condition_id,
              attempt_id: payload.attempt_id
            }));
            setReviewPreviewError('');
          } else if (isReviewRequiredStatus(payload.quality_status) || payload.qc?.manual_review_required) {
            setReviewPreview(null);
          }
          setReviewPreviewLoading(false);
          setBusyAction('');
          if (payload.reconciliation_required) {
            message.error(payload.error || payload.message || '数据已落盘，但状态账本待恢复；请重启采集服务');
          } else if (isReviewRequiredStatus(payload.quality_status) || payload.qc?.manual_review_required) {
            message.warning(payload.message || '采集已保存，等待人工复核');
          } else if (payload.success === false) {
            message.error(payload.error || payload.message || '条件采集失败');
          } else {
            message.success(payload.message || '5 帧同步 burst 采集完成');
          }
          break;
        case 'protocol_review_result':
          applyReturnedState(payload);
          markReconciliationRequired(payload);
          if (payload.success !== false) {
            setLastCaptureResult(null);
            setReviewPreview(null);
            setReviewPreviewError('');
          }
          setBusyAction('');
          payload.success === false
            ? message.error(payload.error || '人工复核提交失败')
            : message.success(payload.message || (payload.decision === 'REJECT' ? '已驳回，本条件进入补采' : '已接受本次 attempt'));
          break;
        case 'protocol_review_preview':
        {
          const requestContext = reviewPreviewRequestRef.current || {};
          reviewPreviewRequestRef.current = null;
          setReviewPreviewLoading(false);
          if (payload.success === false) {
            setReviewPreview(null);
            setReviewPreviewError(payload.error || payload.message || '已保存的 F03 证据加载失败');
          } else if (payload.source !== 'verified_committed_files') {
            setReviewPreview(null);
            setReviewPreviewError('服务端返回的证据未经过落盘文件哈希验证，禁止接受');
          } else {
            setReviewPreview(normalizeReviewPreview(payload, requestContext));
            setReviewPreviewError('');
          }
          break;
        }
        case 'anthropometry_result':
          applyReturnedState(payload);
          markReconciliationRequired(payload);
          setBusyAction('');
          payload.success === false
            ? message.error(payload.error || '人体测量保存失败')
            : message.success(payload.message || '人体测量已保存');
          break;
        case 'protocol_completion_result':
          applyReturnedState(payload);
          markReconciliationRequired(payload);
          setCompletionReport(payload.report || null);
          setBusyAction('');
          payload.success === false || String(payload.report?.status).toUpperCase() === 'CORRUPTED'
            ? message.error(payload.error || (String(payload.report?.status).toUpperCase() === 'CORRUPTED' ? '完整性检查失败：记录已标记为 CORRUPTED' : '尚未通过完成门禁'))
            : message.success(payload.message || '受试者采集已完成');
          break;
        case 'error':
          if (reviewPreviewRequestRef.current) {
            reviewPreviewRequestRef.current = null;
            setReviewPreviewLoading(false);
            setReviewPreviewError(payload.message || packet.message || '已保存的 F03 证据加载失败');
          }
          setBusyAction('');
          setIsCameraConnecting(false);
          setIsCameraDisconnecting(false);
          setProtocolLoading(false);
          setProtocolError(payload.message || packet.message || '服务端处理失败');
          message.error(payload.message || packet.message || '服务端处理失败');
          break;
        default:
          break;
      }
    };

    const connect = async () => {
      if (!active) return;
      let token = '';
      try {
        token = await window.electronAPI?.getWsToken?.() || '';
      } catch {
        // Browser/fetch fallback below also covers an Electron IPC restart.
      }
      if (!token) {
        try {
          const response = await fetch(`http://${backendHost}/auth-token`, {
            cache: 'no-store'
          });
          if (response.ok) token = (await response.json()).token || '';
        } catch {
          // 连接关闭回调统一提供重试反馈。
        }
      }
      if (!active || !token) {
        setConnected(false);
        reconnectTimer = window.setTimeout(connect, 3000);
        return;
      }
      socket = new WebSocket(`ws://${backendHost}`);
      socketRef.current = socket;
      socket.onopen = () => sendOn(socket, 'auth', { token });
      socket.onmessage = handleMessage;
      socket.onclose = () => {
        reviewPreviewRequestRef.current = null;
        setReviewPreviewLoading(false);
        setConnected(false);
        cameraConnected = false;
        previewExpectedAt = 0;
        lastPreviewAt = 0;
        setPreviewData(null);
        setDistanceInfo(null);
        setCameraStatus({ ...EMPTY_CAMERA, message: '采集服务已断开，预览已清除' });
        setPreviewStatus('disconnected');
        setBusyAction('');
        setIsCameraConnecting(false);
        setIsCameraDisconnecting(false);
        if (active) reconnectTimer = window.setTimeout(connect, 3000);
      };
      socket.onerror = () => setConnected(false);
    };

    setConnected(false);
    setPreviewData(null);
    setDistanceInfo(null);
    setCameraStatus({ ...EMPTY_CAMERA, message: '正在连接采集服务' });
    setPreviewStatus('disconnected');
    setProtocolState(null);
    setLastCaptureResult(null);
    setCompletionReport(null);
    setReviewPreview(null);
    setReviewPreviewError('');
    previewWatchdogTimer = window.setInterval(() => {
      const now = Date.now();
      const baseline = lastPreviewAt || previewExpectedAt;
      if (
        active
        && socket?.readyState === WebSocket.OPEN
        && cameraConnected
        && baseline
        && now - baseline > 3500
        && now - lastPreviewRestartAt > 3000
      ) {
        lastPreviewRestartAt = now;
        setPreviewData(null);
        setDistanceInfo(null);
        setPreviewStatus('recovering');
        sendOn(socket, 'start_preview');
        sendOn(socket, 'get_camera_status');
      }
    }, 1000);
    connect();
    return () => {
      active = false;
      window.clearTimeout(reconnectTimer);
      window.clearTimeout(catalogTimer);
      window.clearInterval(previewWatchdogTimer);
      socket?.close();
      if (socketRef.current === socket) socketRef.current = null;
    };
  }, [backendHost, message]);

  const send = useCallback((type, payload = {}, action = '') => {
    if (socketRef.current?.readyState !== WebSocket.OPEN) {
      message.warning('未连接到采集服务');
      return false;
    }
    if (action) setBusyAction(action);
    socketRef.current.send(JSON.stringify({ type, ...payload }));
    return true;
  }, [message]);

  const connectCamera = useCallback((deviceId) => {
    setIsCameraConnecting(true);
    setPreviewData(null);
    setDistanceInfo(null);
    setPreviewStatus('connecting');
    if (!send('connect_camera', { device_id: deviceId || '' })) setIsCameraConnecting(false);
  }, [send]);

  const requestReviewPreview = useCallback((conditionId, attemptId) => {
    if (!conditionId || !attemptId) return false;
    setReviewPreviewLoading(true);
    setReviewPreviewError('');
    reviewPreviewRequestRef.current = {
      subject_id: protocolState?.subject_id,
      condition_id: conditionId,
      attempt_id: attemptId
    };
    const sent = send('get_protocol_review_preview', {
      subject_id: protocolState?.subject_id,
      condition_id: conditionId,
      attempt_id: attemptId
    });
    if (!sent) {
      reviewPreviewRequestRef.current = null;
      setReviewPreviewLoading(false);
      setReviewPreviewError('未连接到采集服务，无法加载已保存的 F03 证据');
    }
    return sent;
  }, [protocolState?.subject_id, send]);

  const disconnectCamera = useCallback(() => {
    setIsCameraDisconnecting(true);
    setPreviewData(null);
    setDistanceInfo(null);
    setPreviewStatus('disconnecting');
    if (!send('disconnect_camera')) setIsCameraDisconnecting(false);
  }, [send]);
  const refreshCamera = useCallback(() => send('get_camera_status'), [send]);
  const refreshProtocol = useCallback(() => {
    setProtocolLoading(true);
    send('get_protocol_catalog');
    send('get_protocol_subjects');
  }, [send]);
  const createSubject = useCallback((payload) => {
    setCompletionReport(null);
    setReviewPreview(null);
    setReviewPreviewError('');
    return send('create_protocol_subject', payload, 'create-subject');
  }, [send]);
  const selectSubject = useCallback((subjectId) => {
    activeSubjectIdRef.current = subjectId || '';
    selectedConditionIdRef.current = '';
    setLastCaptureResult(null);
    setCompletionReport(null);
    setReviewPreview(null);
    setReviewPreviewError('');
    return send('select_protocol_subject', { subject_id: subjectId }, 'select-subject');
  }, [send]);
  const selectPreviewCondition = useCallback((conditionId) => {
    if (!protocolState?.subject_id || !conditionId) return false;
    selectedConditionIdRef.current = conditionId;
    return send('set_protocol_preview_condition', {
      subject_id: protocolState.subject_id,
      condition_id: conditionId
    });
  }, [protocolState?.subject_id, send]);
  const captureCondition = useCallback((conditionId, confirmations, retake = {}) => {
    setCompletionReport(null);
    setReviewPreview(null);
    setReviewPreviewError('');
    return send('capture_protocol_condition', {
      subject_id: protocolState?.subject_id,
      condition_id: conditionId,
      confirmations,
      ...retake
    }, 'capture');
  }, [protocolState?.subject_id, send]);
  const reviewCapture = useCallback((conditionId, attemptId, decision, reason) => {
    setCompletionReport(null);
    const evidenceToken = (
      decision === 'ACCEPT'
      && reviewPreview?.source === 'verified_committed_files'
      && String(reviewPreview?.condition_id) === String(conditionId)
      && String(reviewPreview?.attempt_id) === String(attemptId)
    ) ? reviewPreview.evidence_token : undefined;
    return send('protocol_review_capture', {
      subject_id: protocolState?.subject_id,
      condition_id: conditionId,
      attempt_id: attemptId,
      decision,
      reason,
      evidence_token: evidenceToken
    }, 'review');
  }, [protocolState?.subject_id, reviewPreview, send]);
  const saveAnthropometry = useCallback((records, equipment) => {
    setCompletionReport(null);
    return send('save_anthropometry', {
      subject_id: protocolState?.subject_id,
      records,
      equipment
    }, 'measurements');
  }, [protocolState?.subject_id, send]);
  const completeSubject = useCallback(() => send('complete_protocol_subject', {
    subject_id: protocolState?.subject_id
  }, 'completion'), [protocolState?.subject_id, send]);

  const controlActions = useMemo(() => ({
    lastCaptureResult,
    completionReport,
    reviewPreview,
    reviewPreviewLoading,
    reviewPreviewError,
    connectCamera,
    disconnectCamera,
    refreshCamera,
    refreshProtocol,
    createSubject,
    selectSubject,
    selectPreviewCondition,
    requestReviewPreview,
    captureCondition,
    reviewCapture,
    saveAnthropometry,
    completeSubject
  }), [
    captureCondition, completeSubject, completionReport, connectCamera, createSubject,
    disconnectCamera, lastCaptureResult, refreshCamera, refreshProtocol,
    requestReviewPreview, reviewCapture, reviewPreview, reviewPreviewError,
    reviewPreviewLoading, saveAnthropometry, selectPreviewCondition, selectSubject
  ]);

  return {
    connected, previewData, previewStatus, distanceInfo, cameraStatus,
    isCameraConnecting, isCameraDisconnecting,
    catalog, subjects, protocolState, protocolLoading, protocolError, busyAction, lastCaptureResult, completionReport,
    reviewPreview, reviewPreviewLoading, reviewPreviewError,
    send,
    connectCamera,
    disconnectCamera,
    refreshCamera,
    refreshProtocol,
    createSubject,
    selectSubject,
    selectPreviewCondition,
    requestReviewPreview,
    captureCondition,
    reviewCapture,
    saveAnthropometry,
    completeSubject,
    controlActions
  };
}
