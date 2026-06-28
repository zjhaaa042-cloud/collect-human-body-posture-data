const { contextBridge, ipcRenderer } = require('electron');
const fs = require('fs');
const path = require('path');

let wsToken = '';
try {
  const tokenPath = path.join(__dirname, '..', '.ws_token');
  if (fs.existsSync(tokenPath)) {
    wsToken = fs.readFileSync(tokenPath, 'utf-8').trim();
  }
} catch (e) {
  console.error('Failed to read WS token:', e);
}

contextBridge.exposeInMainWorld('electronAPI', {
  getAppVersion: () => ipcRenderer.invoke('get-app-version'),
  getAppPath: () => ipcRenderer.invoke('get-app-path'),
  onExitConfirm: (callback) => ipcRenderer.on('exit-confirm', callback),
  getWsToken: () => wsToken,
  closeWindow: () => ipcRenderer.invoke('close-window')
});
