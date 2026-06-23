# 人体体态数据采集系统

基于奥比中光 Gemini 336L 深度相机的自动化人体体态数据采集系统。

## 项目简介

本系统是一款专业的人体体态数据采集工具，通过深度相机获取 RGB 彩色图像、深度图像和 3D 点云数据，支持语音控制操作，适用于人体姿态研究、健康监测等领域的数据采集工作。

## 功能特性

### 核心功能

| 功能 | 说明 |
|------|------|
| 距离检测 | 实时检测采集者与相机的距离，目标距离 1 米 |
| 多模态数据采集 | RGB 彩色图、深度图、3D 点云（PLY 格式） |
| 语音控制 | 支持"开始采集"、"停止采集"等语音指令 |
| 语音播报 | 操作引导和状态反馈的语音提示 |
| 实时预览 | GUI 中显示彩色和深度画面 |
| 小米风格界面 | 简约、大气、科技感的用户界面 |

### 技术模块

- **相机管理** (`backend/core/camera_manager.py`)：深度相机初始化、流控制、点云生成
- **数据采集** (`backend/core/data_collector.py`)：多模态数据保存、质量检测
- **深度分析** (`backend/core/depth_analyzer.py`)：人体检测、距离分析
- **点云处理** (`backend/core/point_cloud.py`)：点云过滤、降采样、PLY 文件操作
- **语音模块** (`backend/voice/`)：语音识别（Vosk）、语音合成（TTS）
- **WebSocket 服务** (`backend/server/ws_server.py`)：前后端实时通信
- **存储管理** (`backend/storage/`)：会话管理、文件操作

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
cd frontend && npm install
```

### 2. 下载语音模型

下载 `vosk-model-small-cn-0.22` 到 `models/` 目录。

### 3. 一键启动

```bash
go.bat
```

## 使用方法

### 启动脚本

| 脚本 | 功能 |
|------|------|
| `go.bat` | 一键启动前后端（推荐） |
| `run.bat` | 仅启动后端 |
| `run_frontend.bat` | 仅启动前端 |

### 访问地址

- 后端 WebSocket: `ws://localhost:8765`
- 前端界面: `http://localhost:3000`

### 语音指令

| 指令 | 动作 |
|------|------|
| "开始采集" | 开始数据采集 |
| "停止" | 停止当前采集 |
| "下一个" | 准备下一次采集 |
| "完成" | 结束采集会话 |

## 数据存储

数据存储在 `data/sessions/` 目录下：

| 数据类型 | 格式 | 说明 |
|----------|------|------|
| RGB 图像 | PNG | 彩色图像 |
| 深度数据 | NPZ | NumPy 压缩格式 |
| 点云数据 | PLY | 3D 点云文件 |

## 系统要求

- Python 3.8+
- Node.js 14+
- 奥比中光 Gemini 336L 深度相机
- Windows 10+ (需要管理员权限运行)

## 故障排除

| 问题 | 解决方案 |
|------|----------|
| 端口占用 | `taskkill /F /IM python.exe` |
| 前端错误 | `cd frontend && npm install` |
| 相机未检测 | 参考 INSTALL.md 配置相机驱动 |
| 语音模型加载失败 | 确认模型文件已下载到 models/ 目录 |

## 许可证

Apache License 2.0

## 项目结构

```
.
├── backend/                 # 后端代码
│   ├── core/               # 核心模块
│   │   ├── camera_manager.py
│   │   ├── data_collector.py
│   │   ├── depth_analyzer.py
│   │   └── point_cloud.py
│   ├── server/             # WebSocket 服务
│   ├── storage/           # 存储管理
│   └── voice/             # 语音模块
├── frontend/              # 前端 React 应用
├── models/                # 语音模型
├── data/                  # 采集数据
├── requirements.txt       # Python 依赖
└── go.bat               # 启动脚本
```