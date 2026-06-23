# 安装指南

## 系统要求

- Python 3.10+
- Node.js 18+
- 奥比中光 Gemini 336L 相机

## 快速安装

### 1. 安装 Python 依赖

```bash
# 创建虚拟环境
python -m venv venv

# 激活虚拟环境
# Windows:
.\venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 2. 安装 pyorbbecsdk

```bash
pip install pyorbbecsdk2
```

如果安装失败，可以从源码安装：

```bash
cd ../pyorbbecsdk-v2-main
pip install -e .
```

### 3. 配置相机环境（首次使用）

```bash
# Windows (需要管理员权限)
python ../pyorbbecsdk-v2-main/scripts/env_setup/setup_env.py

# 或者从已安装的包
python -c "import pyorbbecsdk, os; print(os.path.dirname(pyorbbecsdk.__file__))")/shared/setup_env.py
```

### 4. 下载 Vosk 中文语音模型

1. 访问 https://alphacephei.com/vosk/models
2. 下载 `vosk-model-cn-0.22`
3. 解压到 `models/vosk-model-cn/` 目录

### 5. 安装前端依赖

```bash
cd frontend
npm install
```

## 启动系统

### 方式一：使用启动脚本（推荐）

```bash
# 启动后端
start.bat

# 新终端启动前端
start_frontend.bat
```

### 方式二：手动启动

```bash
# 终端1：启动后端
python start_backend.py

# 终端2：启动前端
cd frontend
npm run electron:dev
```

## 常见问题

### 1. pyorbbecsdk 安装失败

确保已安装 Visual C++ Build Tools：

```bash
pip install --upgrade pip setuptools wheel
pip install pyorbbecsdk2
```

### 2. 相机未检测到

- 检查 USB 连接（推荐 USB 3.0）
- 运行环境配置脚本
- 检查设备管理器中是否有未识别设备

### 3. Vosk 模型加载失败

- 确保模型路径正确
- 检查模型文件是否完整
- 尝试重新下载模型

### 4. WebSocket 连接失败

- 确保后端服务已启动
- 检查端口 8765 是否被占用
- 检查防火墙设置
