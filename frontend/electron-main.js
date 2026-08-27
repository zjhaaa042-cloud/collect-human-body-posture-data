const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');

let mainWindow;
let reactProcess = null;

function stopReactServer() {
  if (!reactProcess || reactProcess.exitCode !== null) return;
  if (process.platform === 'win32') {
    spawn('taskkill', ['/PID', String(reactProcess.pid), '/T', '/F'], {
      windowsHide: true,
      shell: false
    });
  } else {
    reactProcess.kill('SIGTERM');
  }
  reactProcess = null;
}

function startReactServer() {
  return new Promise((resolve, reject) => {
    const frontendPath = __dirname;
    const isWin = process.platform === 'win32';
    reactProcess = spawn(isWin ? 'npm.cmd' : 'npm', ['run', 'start:no-open'], {
      cwd: frontendPath,
      env: { ...process.env, PORT: '3000', BROWSER: 'none' },
      shell: false
    });

    reactProcess.stdout.on('data', (data) => {
      console.log(`React: ${data}`);
      if (data.includes('Local:') || data.includes('ready in')) {
        resolve();
      }
    });

    reactProcess.stderr.on('data', (data) => {
      console.error(`React Error: ${data}`);
    });

    reactProcess.on('error', (err) => {
      console.error('Failed to start React:', err);
      reject(err);
    });

    // Timeout after 30 seconds
    setTimeout(() => {
      resolve();
    }, 30000);
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 900,
    minHeight: 600,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      enableRemoteModule: false,
      preload: path.join(__dirname, 'preload.js'),
      sandbox: true
    },
    title: '体态数据采集系统',
    backgroundColor: '#0D0D0D',
    show: false
  });

  const isDev = process.env.ELECTRON_DEV === 'true';
  
  if (isDev) {
    console.log('Loading dev URL: http://localhost:3000');
    mainWindow.loadURL('http://localhost:3000').catch(err => {
      console.error('Failed to load URL:', err);
      showErrorPage();
    });
    mainWindow.webContents.openDevTools();
  } else {
    const indexPath = path.join(__dirname, 'build', 'index.html');
    console.log('Loading file:', indexPath);
    mainWindow.loadFile(indexPath).catch(err => {
      console.error('Failed to load file:', err);
      showErrorPage();
    });
  }

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
    console.log('Window shown');
  });

  mainWindow.webContents.on('did-fail-load', (event, errorCode, errorDescription) => {
    console.error('Failed to load:', errorCode, errorDescription);
    showErrorPage();
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
    stopReactServer();
  });
}

function showErrorPage() {
  if (mainWindow) {
    mainWindow.loadURL(`data:text/html,
      <html>
        <head><title>Error</title></head>
        <body style="background:#0D0D0D;color:#fff;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0">
          <div style="text-align:center">
            <h1 style="color:#FF6900">启动失败</h1>
            <p>请确保后端服务已启动</p>
            <p>然后重启前端</p>
          </div>
        </body>
      </html>
    `);
  }
}

app.whenReady().then(async () => {
  const isDev = process.env.ELECTRON_DEV === 'true';
  
  if (isDev) {
    console.log('Waiting for React server...');
    try {
      await startReactServer();
      console.log('React server ready');
    } catch (err) {
      console.error('React server failed:', err);
    }
  }
  
  createWindow();
});

app.on('window-all-closed', () => {
  stopReactServer();
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow();
  }
});

ipcMain.handle('get-app-version', () => {
  return app.getVersion();
});

ipcMain.handle('get-app-path', () => {
  return app.getAppPath();
});

ipcMain.handle('get-ws-token', () => {
  const candidates = [
    path.join(__dirname, '..', '.ws_token'),
    path.join(process.cwd(), '.ws_token'),
    path.join(app.getAppPath(), '..', '.ws_token')
  ];
  for (const tokenPath of candidates) {
    try {
      if (fs.existsSync(tokenPath)) {
        return fs.readFileSync(tokenPath, 'utf-8').trim();
      }
    } catch (error) {
      console.error(`Failed to read WS token at ${tokenPath}:`, error);
    }
  }
  return '';
});

ipcMain.handle('select-output-directory', async () => {
  if (!mainWindow) return '';
  const result = await dialog.showOpenDialog(mainWindow, {
    title: '选择双机采集数据输出文件夹',
    properties: ['openDirectory', 'createDirectory']
  });
  return result.canceled ? '' : (result.filePaths[0] || '');
});

ipcMain.handle('close-window', () => {
  if (mainWindow) {
    mainWindow.close();
  }
});
