# 人体体态数据采集系统

基于奥比中光 Gemini 336L 深度相机的人体体态数据采集工具。系统支持 RGB 彩色图像、深度图像和 3D 点云数据采集，并提供实时预览、距离检测、会话管理和语音控制能力。

## 项目简介

本系统面向人体姿态研究、健康监测、动作采集等场景，前端提供可视化采集工作台，后端负责相机管理、数据处理、文件保存和 WebSocket 实时通信。

## 核心功能

| 功能 | 说明 |
| --- | --- |
| 距离检测 | 实时检测采集对象与相机的距离，辅助保持合适采集位置 |
| 多模态采集 | 支持 RGB 图像、深度数据和 PLY 点云保存 |
| 实时预览 | 在界面中展示彩色画面和深度画面 |
| 会话管理 | 支持新建、选择、完成采集会话 |
| 采集历史 | 展示最近采集记录，并可预览已有 RGB 图像 |
| 语音控制 | 支持“开始采集”“停止”“下一个”“完成”等语音指令 |

## 技术模块

- **相机管理**：`backend/core/camera_manager.py`，负责深度相机初始化、流控制和帧获取。
- **数据采集**：`backend/core/data_collector.py`，负责多类型数据保存和质量检查。
- **深度分析**：`backend/core/depth_analyzer.py`，负责人体检测和距离分析。
- **点云处理**：`backend/core/point_cloud.py`，负责点云生成、过滤和 PLY 写入。
- **语音模块**：`backend/voice/`，负责语音识别和语音提示。
- **WebSocket 服务**：`backend/server/ws_server.py`，负责前后端实时通信。
- **前端界面**：`frontend/`，React + Ant Design + Electron。

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
cd frontend
npm install
```

### 2. 下载语音模型

下载 Vosk 中文语音模型，并解压到 `models/` 目录。推荐目录结构：

```text
models/vosk-model-cn/
```

### 3. 启动系统

```bash
go.bat
```

## 手动启动

| 脚本 | 功能 |
| --- | --- |
| `go.bat` | 一键启动后端和前端 |
| `run.bat` | 仅启动后端 |
| `run_frontend.bat` | 仅启动前端 |

访问地址：

- 后端 WebSocket：`ws://localhost:8765`
- 前端界面：`http://localhost:3000`

## 语音指令

| 指令 | 动作 |
| --- | --- |
| “开始采集” | 开始一次数据采集 |
| “停止” | 停止当前操作 |
| “下一个” | 准备下一次采集 |
| “完成” | 结束当前采集会话 |

## 数据存储

采集数据默认保存到 `data/sessions/` 目录。

| 数据类型 | 格式 | 说明 |
| --- | --- | --- |
| RGB 图像 | PNG/JPG | 彩色图像 |
| 深度数据 | NPZ | NumPy 压缩格式 |
| 点云数据 | PLY | 3D 点云文件 |

## 系统要求

- Python 3.8+
- Node.js 14+
- Windows 10+
- 奥比中光 Gemini 336L 深度相机

## 故障排除

| 问题 | 处理方式 |
| --- | --- |
| 端口占用 | 检查并关闭占用 `8765` 或 `3000` 端口的进程 |
| 前端依赖缺失 | 进入 `frontend` 后重新运行 `npm install` |
| 相机未检测到 | 检查 USB 连接、相机驱动和设备管理器状态 |
| 语音模型加载失败 | 确认模型已下载并放置在正确目录 |

## 许可证

Apache License 2.0
