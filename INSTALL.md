# 安装指南

## 系统要求

- Python 3.10+
- Node.js 18+
- Windows 10+
- 奥比中光 Gemini 336L 深度相机

## 快速安装

### 1. 安装 Python 依赖

```bash
# 创建虚拟环境
python -m venv venv

# 激活虚拟环境（Windows）
.\venv\Scripts\activate

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

```bash
# Windows 需要管理员权限
python ../pyorbbecsdk-v2-main/scripts/env_setup/setup_env.py
```

也可以从已安装的包目录中运行 `shared/setup_env.py`。

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

### 3. Vosk 模型加载失败

- 确认模型路径正确。
- 检查模型文件是否完整。
- 尝试重新下载并解压模型。

### 4. WebSocket 连接失败

- 确认后端服务已启动。
- 检查 `8765` 端口是否被占用。
- 检查防火墙或安全软件设置。
