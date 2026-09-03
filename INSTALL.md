# 安装指南

## 系统要求

- Python 3.10 或 3.11（D435i 固件 5.15.1.55 对应 SDK 2.54.2）
- Node.js 20.19+ 或 22.12+（Vite 8 要求）
- Windows 10+
- 奥比中光 Gemini 336L 与 Intel RealSense D435i

## 快速安装

### 1. 安装 Python 依赖

```bash
# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境（Windows）
.\.venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

### 2. 安装 pyorbbecsdk

```bash
pip install pyorbbecsdk2
```

如果安装失败，可从源码安装：

```bash
cd ../pyorbbecsdk-v2-main
pip install -e .
```

### 3. 配置相机环境（首次使用）

#### 3.1 Gemini 336L

```bash
# Windows 需要管理员权限
python ../pyorbbecsdk-v2-main/scripts/env_setup/setup_env.py
```

也可以从已安装的包目录中运行 `shared/setup_env.py`。

#### 3.2 D435i Windows 帧元数据

D435i 固件 `5.15.1.55` 使用项目锁定的 `pyrealsense2 2.54.2.5684`。首次连接一台新的 RealSense 设备时，以管理员权限运行官方元数据脚本：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\realsense\v2.54.2\realsense_metadata_win10.ps1 -op install
```

在 Windows UAC 中手动确认“是”，随后重新插拔相机或执行设备硬复位。该注册项与设备实例绑定，更换 D435i 后需要重新执行。不要用延长超时替代此配置。

### 4. 下载 Vosk 中文语音模型

1. 访问 <https://alphacephei.com/vosk/models>
2. 下载中文模型，例如 `vosk-model-cn-0.22`
3. 解压到 `models/vosk-model-cn/` 目录

### 5. 安装前端依赖

```bash
cd frontend
npm install
```

## 启动系统

### 方式一：一键启动（推荐）

```bash
go.bat
```

### 方式二：手动启动

```bash
# 终端 1：启动后端
run.bat

# 终端 2：启动前端
run_frontend.bat
```

也可以进入 `frontend` 目录后运行：

```bash
npm run electron:dev
```

该命令通过 Electron 的 `--dev` 模式启动唯一一个 Vite 子进程并等待其就绪；不会读取旧的 `frontend/build` 页面。

## 构建 Windows 安装包

首次构建需要联网安装 PyInstaller 和 Electron Builder 依赖：

```powershell
.\scripts\build_windows.ps1
```

生成文件：

```text
frontend\release\BodyPostureCollector-Setup-1.0.3.exe
```

安装包包含前端、Electron、本项目 Python 后端及相机 Python SDK。安装后的默认可写数据目录为 `%USERPROFILE%\Documents\BodyPostureCollectorData`，运行配置和日志位于 Electron 用户数据目录。硬件厂商的 Windows USB/相机驱动仍需在采集电脑上正确安装。

## 首次采集前验收

两台相机顺序验收，避免同一设备被多个进程占用。以下示例以 D435i 为例；Gemini 将 `realsense` 改为 `orbbec`，并换成对应序列号。

```powershell
# 100 次五帧 burst，完整明细原子写入 JSON
.\.venv\Scripts\python.exe scripts\verify_protocol_camera.py `
  --backend realsense --device-id 243722074968 --bursts 100 `
  --output reports\hardware\d435i_bursts_100.json --summary-only

# 10 分钟连续取流
.\.venv\Scripts\python.exe scripts\verify_camera_stability.py `
  --backend realsense --device-id 243722074968 --duration-seconds 600 `
  --output reports\hardware\d435i_stability_600s.json

# 不进入正式数据集的完整写入/F03 复核链
.\.venv\Scripts\python.exe scripts\verify_dev_only_capture.py `
  --backend realsense --device-id 243722074968 --acknowledge-dev-only

# 两台相机同时连接，验证一个双机五帧组及 PNG/NPY 一致性
.\.venv\Scripts\python.exe scripts\verify_dual_capture.py `
  --acknowledge-dev-only
```

`verify_dev_only_capture.py` 只允许项目内路径名含 `dev_only` 的隔离目录，并固定以 `REJECT` 结束。若 Windows 在原子提交或复核账本更新时中断，程序保留 staging/review sidecar；使用 `scripts/recover_dev_only_run.py --help` 查看安全恢复命令，不要手工复制或覆盖 attempt。

新采集的 RealAnthro 和双机数据会同时保存 raw/aligned `uint16 PNG` 与 `uint16 NPY`。NPY 不预乘深度比例；请读取清单里的 `depth_scale_mm_per_unit` 换算毫米。

## 常见问题

### 1. pyorbbecsdk 安装失败

确认已安装 Visual C++ Build Tools，并尝试升级安装工具：

```bash
pip install --upgrade pip setuptools wheel
pip install pyorbbecsdk2
```

### 2. 相机未检测到

- 检查 USB 连接，建议使用 USB 3.0 接口。
- 运行相机环境配置脚本。
- 在设备管理器中确认相机设备已正确识别。
- 若 Gemini 的 RGB/Depth 在设备管理器中均为正常，但 SDK 暂时返回 0 台，关闭所有相机程序，完整重插/断电并等待枚举后再运行 burst 验收；不要因此清理已有 staging。

### 3. Windows 原子替换短时拒绝访问

协议存储只对 Windows 锁占用错误 `5/32/33` 做约 3.85 秒的有限原子重试，不使用复制/删除降级。重试耗尽时，完整 staging 会保留并在状态中标记 `WRITE_FAILED`，后续由严格哈希恢复工具处理。

### 4. Vosk 模型加载失败

- 确认模型路径正确。
- 检查模型文件是否完整。
- 尝试重新下载并解压模型。

### 5. WebSocket 连接失败

- 确认后端服务已启动。
- 检查 `8765` 端口是否被占用。
- 检查防火墙或安全软件设置。
