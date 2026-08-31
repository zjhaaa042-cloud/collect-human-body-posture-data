const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const path = require('path');
const fs = require('fs');
const http = require('http');
const { spawn } = require('child_process');

let mainWindow;
let reactProcess = null;
let backendProcess = null;
let backendAuthToken = '';
let quitReady = false;

function isDevelopmentMode() {
  return process.argv.includes('--dev') || process.env.ELECTRON_DEV === 'true';
}

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

    let settled = false;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      callback(value);
    };

    reactProcess.stdout.on('data', (data) => {
      const output = data.toString();
      console.log(`React: ${output}`);
      if (output.includes('Local:') || output.includes('ready in')) {
        finish(resolve);
      }
    });

    reactProcess.stderr.on('data', (data) => {
      console.error(`React Error: ${data}`);
    });

    reactProcess.on('error', (err) => {
      console.error('Failed to start React:', err);
      finish(reject, err);
    });

    reactProcess.on('exit', (code) => {
      if (!settled) finish(reject, new Error(`Vite exited before becoming ready (${code})`));
    });

    const timeout = setTimeout(() => {
      finish(reject, new Error('Timed out waiting for Vite on http://localhost:3000'));
    }, 30000);
  });
}

function requestBackend(pathname, timeoutMs = 1500) {
  return new Promise((resolve, reject) => {
    const request = http.get({
      hostname: '127.0.0.1',
      port: 8765,
      path: pathname,
      timeout: timeoutMs
    }, (response) => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => { body += chunk; });
      response.on('end', () => {
        if (response.statusCode !== 200) {
          reject(new Error(`Backend returned HTTP ${response.statusCode}`));
          return;
        }
        try {
          resolve(JSON.parse(body));
        } catch (error) {
          reject(error);
        }
      });
    });
    request.on('timeout', () => request.destroy(new Error('Backend request timed out')));
    request.on('error', reject);
  });
}

async function backendIsReady() {
  try {
    const health = await requestBackend('/health');
    return health?.ok === true && health?.service === 'body-posture-backend';
  } catch {
    return false;
  }
}

function packagedRuntimePaths() {
  const runtimeDirectory = path.join(app.getPath('userData'), 'runtime');
  const dataDirectory = path.join(app.getPath('documents'), 'BodyPostureCollectorData');
  const assetDirectory = path.join(process.resourcesPath, 'backend-assets');
  return {
    runtimeDirectory,
    dataDirectory,
    assetDirectory,
    tokenFile: path.join(runtimeDirectory, '.ws_token'),
    configFile: path.join(runtimeDirectory, 'config.json'),
    logFile: path.join(runtimeDirectory, 'logs', 'app.log'),
    backendExecutable: path.join(process.resourcesPath, 'backend', 'body-posture-backend.exe')
  };
}

function writePackagedRenderStatus(success, detail) {
  if (!app.isPackaged) return;
  try {
    const paths = packagedRuntimePaths();
    fs.mkdirSync(paths.runtimeDirectory, { recursive: true });
    fs.writeFileSync(
      path.join(paths.runtimeDirectory, 'ui-render-status.json'),
      JSON.stringify({
        success,
        detail,
        app_version: app.getVersion(),
        recorded_at: new Date().toISOString()
      }, null, 2),
      'utf8'
    );
  } catch (error) {
    console.error('Failed to write packaged render status:', error);
  }
}

function ensurePackagedRuntime(paths) {
  fs.mkdirSync(paths.runtimeDirectory, { recursive: true });
  fs.mkdirSync(paths.dataDirectory, { recursive: true });
  fs.mkdirSync(path.dirname(paths.logFile), { recursive: true });
  const exampleConfig = path.join(paths.assetDirectory, 'config.example.json');
  if (!fs.existsSync(paths.configFile) && fs.existsSync(exampleConfig)) {
    fs.copyFileSync(exampleConfig, paths.configFile);
  }
  try {
    fs.rmSync(paths.tokenFile, { force: true });
  } catch {
    // A stale token cannot prevent backend startup; the backend overwrites it.
  }
}

async function waitForPackagedBackend(timeoutMs = 45000) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    if (backendProcess && backendProcess.exitCode !== null) {
      throw new Error(`后端进程提前退出，退出码 ${backendProcess.exitCode}`);
    }
    if (await backendIsReady()) {
      const payload = await requestBackend('/auth-token');
      backendAuthToken = payload?.token || '';
      if (!backendAuthToken) throw new Error('后端未返回认证令牌');
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error('等待本地采集后端启动超时');
}

async function startPackagedBackend() {
  if (!app.isPackaged) return;
  if (await backendIsReady()) {
    const payload = await requestBackend('/auth-token');
    backendAuthToken = payload?.token || '';
    if (!backendAuthToken) throw new Error('现有后端不可用于本应用');
    console.log('Using existing local backend');
    return;
  }

  const paths = packagedRuntimePaths();
  ensurePackagedRuntime(paths);
  if (!fs.existsSync(paths.backendExecutable)) {
    throw new Error(`未找到后端运行文件：${paths.backendExecutable}`);
  }

  const backendLog = fs.openSync(path.join(paths.runtimeDirectory, 'backend-process.log'), 'a');
  backendProcess = spawn(paths.backendExecutable, [], {
    cwd: paths.runtimeDirectory,
    windowsHide: true,
    shell: false,
    stdio: ['ignore', backendLog, backendLog],
    env: {
      ...process.env,
      PYTHONUTF8: '1',
      BODY_POSTURE_PACKAGED: '1',
      BODY_COLLECTOR_SHUTDOWN_WHEN_IDLE: '1',
      BODY_POSTURE_ASSET_ROOT: paths.assetDirectory,
      BODY_POSTURE_CONFIG_FILE: paths.configFile,
      BODY_POSTURE_TOKEN_FILE: paths.tokenFile,
      BODY_POSTURE_DATA_DIR: paths.dataDirectory,
      BODY_POSTURE_LOG_FILE: paths.logFile
    }
  });
  fs.closeSync(backendLog);
  backendProcess.on('error', (error) => console.error('Backend process error:', error));
  backendProcess.on('exit', (code) => {
    console.log(`Backend exited with code ${code}`);
    backendProcess = null;
  });
  await waitForPackagedBackend();
  console.log('Packaged backend ready');
}

async function stopPackagedBackend() {
  const processToStop = backendProcess;
  backendProcess = null;
  backendAuthToken = '';
  if (!processToStop || processToStop.exitCode !== null) return;

  // Match the Python launcher: an in-flight capture/commit may need time to
  // finish its fsync and atomic promotion before the backend can exit.
  const deadline = Date.now() + 130000;
  while (processToStop.exitCode === null && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  if (processToStop.exitCode !== null) return;

  if (process.platform === 'win32') {
    await new Promise((resolve) => {
      const killer = spawn('taskkill', ['/PID', String(processToStop.pid), '/T', '/F'], {
        windowsHide: true,
        shell: false
      });
      killer.once('exit', resolve);
      killer.once('error', resolve);
    });
  } else {
    processToStop.kill('SIGTERM');
  }
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

  const isDev = isDevelopmentMode();
  
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
    mainWindow.webContents.once('did-finish-load', async () => {
      try {
        const rendered = await mainWindow.webContents.executeJavaScript(
          "Boolean(document.querySelector('#root')?.childElementCount)"
        );
        if (!rendered) {
          throw new Error('React root is empty after loading the packaged page');
        }
        writePackagedRenderStatus(true, 'React root rendered');
        console.log('Packaged React page rendered');
      } catch (error) {
        writePackagedRenderStatus(false, String(error?.message || error));
        console.error('Packaged page render check failed:', error);
        dialog.showErrorBox('界面加载失败', '应用资源未能正确加载，请重新安装最新版本。');
        showErrorPage();
      }
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
  const isDev = isDevelopmentMode();
  
  if (isDev) {
    console.log('Waiting for React server...');
    try {
      await startReactServer();
      console.log('React server ready');
    } catch (err) {
      console.error('React server failed:', err);
      dialog.showErrorBox('前端启动失败', String(err?.message || err));
      app.quit();
      return;
    }
  }

  try {
    await startPackagedBackend();
  } catch (err) {
    console.error('Backend failed:', err);
    dialog.showErrorBox('采集后端启动失败', String(err?.message || err));
    app.quit();
    return;
  }
  
  createWindow();
});

app.on('window-all-closed', () => {
  stopReactServer();
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('before-quit', (event) => {
  if (quitReady || !backendProcess) return;
  event.preventDefault();
  stopPackagedBackend().finally(() => {
    quitReady = true;
    app.quit();
  });
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
  if (backendAuthToken) return backendAuthToken;
  const candidates = [
    ...(app.isPackaged ? [packagedRuntimePaths().tokenFile] : []),
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
