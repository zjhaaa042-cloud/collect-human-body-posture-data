const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  getAppVersion: () => ipcRenderer.invoke('get-app-version'),
  getAppPath: () => ipcRenderer.invoke('get-app-path'),
  onExitConfirm: (callback) => ipcRenderer.on('exit-confirm', callback),
  getWsToken: () => ipcRenderer.invoke('get-ws-token'),
  closeWindow: () => ipcRenderer.invoke('close-window')
});
