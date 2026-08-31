export const reconnectDelayMs = (attempt) => (
  Math.min(10000, 1000 * (2 ** Math.min(Math.max(0, attempt), 4)))
);

export const backendUrls = (backendHost, pageProtocol = window.location.protocol) => {
  const secure = pageProtocol === 'https:';
  return {
    token: `${secure ? 'https' : 'http'}://${backendHost}/auth-token`,
    socket: `${secure ? 'wss' : 'ws'}://${backendHost}`
  };
};

export const sendPacket = (socket, type, payload = {}) => {
  if (socket?.readyState !== WebSocket.OPEN) return false;
  socket.send(JSON.stringify({ type, ...payload }));
  return true;
};
