import React, { useEffect, useState } from 'react';
import { ApiOutlined, LinkOutlined, ReloadOutlined } from '@ant-design/icons';
import { Alert, Button, Descriptions, Form, Input, Modal, Space } from 'antd';

const cleanHost = (value) => String(value || '')
  .trim()
  .replace(/^https?:\/\//, '')
  .replace(/^wss?:\/\//, '')
  .replace(/\/$/, '') || 'localhost:8765';

const INITIAL = { checking: false, http: '未检测', token: '未检测', websocket: '未检测', detail: '尚未运行检测' };

export default function BackendDiagnostic({ open, connected, backendHost, onClose, onReconnect }) {
  const [hostInput, setHostInput] = useState(backendHost);
  const [result, setResult] = useState(INITIAL);

  useEffect(() => {
    if (open) setHostInput(backendHost);
  }, [backendHost, open]);

  const runCheck = async () => {
    const host = cleanHost(hostInput);
    const httpUrl = `http://${host}`;
    setResult({ checking: true, http: '检测中', token: '等待中', websocket: '等待中', detail: `正在检测 ${httpUrl}` });
    let token;
    try {
      const health = await fetch(`${httpUrl}/health`, { cache: 'no-store' });
      if (!health.ok) throw new Error(`HTTP ${health.status}`);
      setResult((previous) => ({ ...previous, http: `正常 (${health.status})`, detail: 'HTTP 正常，正在获取鉴权令牌' }));
      const auth = await fetch(`${httpUrl}/auth-token`, { cache: 'no-store' });
      if (!auth.ok) throw new Error(`鉴权接口 HTTP ${auth.status}`);
      token = (await auth.json()).token;
      if (!token) throw new Error('鉴权令牌为空');
      setResult((previous) => ({ ...previous, token: '正常', detail: '令牌正常，正在测试 WebSocket' }));
    } catch (error) {
      setResult((previous) => ({ ...previous, checking: false, http: previous.http === '检测中' ? '失败' : previous.http, token: '失败', websocket: '未检测', detail: `连接检测失败：${error.message}` }));
      return;
    }

    await new Promise((resolve) => {
      let finished = false;
      const socket = new WebSocket(`ws://${host}`);
      const finish = (websocket, detail) => {
        if (finished) return;
        finished = true;
        window.clearTimeout(timeout);
        try { socket.close(); } catch { /* 已关闭 */ }
        setResult({ checking: false, http: '正常', token: '正常', websocket, detail });
        resolve();
      };
      const timeout = window.setTimeout(() => finish('超时', 'WebSocket 鉴权超时，请检查防火墙与 8765 端口。'), 5000);
      socket.onopen = () => socket.send(JSON.stringify({ type: 'auth', token }));
      socket.onmessage = (event) => {
        try {
          if (JSON.parse(event.data).type === 'auth_success') finish('正常', '后端连接正常，可以保存并重新连接。');
        } catch { /* 等待有效鉴权响应 */ }
      };
      socket.onerror = () => finish('失败', 'WebSocket 连接失败，请确认后端地址和防火墙配置。');
      socket.onclose = () => finish('失败', 'WebSocket 在鉴权完成前关闭，请查看后端控制台。');
    });
  };

  const footer = [
    <Button key="open" icon={<LinkOutlined />} onClick={() => window.open(`http://${cleanHost(hostInput)}/health`, '_blank', 'noopener,noreferrer')}>打开健康页</Button>,
    <Button key="test" icon={<ApiOutlined />} loading={result.checking} onClick={runCheck}>检测连接</Button>,
    <Button key="save" type="primary" icon={<ReloadOutlined />} onClick={() => onReconnect(cleanHost(hostInput))}>保存并重连</Button>
  ];

  return (
    <Modal title="后端连接诊断" open={open} onCancel={onClose} footer={footer} destroyOnHidden={false}>
      <Space direction="vertical" size={12} className="backend-diagnostic">
        <Alert type={connected ? 'success' : 'warning'} showIcon message={connected ? '当前已连接后端' : '当前未连接后端'} description={`当前地址：http://${backendHost}`} />
        <Form layout="vertical">
          <Form.Item label="后端地址" htmlFor="backend-host" extra="示例：192.168.1.23:8765">
            <Input id="backend-host" value={hostInput} onChange={(event) => setHostInput(event.target.value)} />
          </Form.Item>
        </Form>
        <Descriptions size="small" bordered column={1}>
          <Descriptions.Item label="HTTP 健康检查">{result.http}</Descriptions.Item>
          <Descriptions.Item label="鉴权 token">{result.token}</Descriptions.Item>
          <Descriptions.Item label="WebSocket">{result.websocket}</Descriptions.Item>
          <Descriptions.Item label="说明">{result.detail}</Descriptions.Item>
        </Descriptions>
      </Space>
    </Modal>
  );
}
