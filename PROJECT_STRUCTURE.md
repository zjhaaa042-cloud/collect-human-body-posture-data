# 人体体态数据采集系统 — 项目结构文档

## 项目概述

基于奥比中光 Gemini 336L 深度相机的人体体态数据采集系统，支持 RGB 图像、深度数据、3D 点云的多模态采集，提供实时预览、语音控制和会话管理功能。

- **后端**: Python (asyncio + WebSocket)
- **前端**: React 18 + Ant Design 5 + Electron 28
- **相机**: 奥比中光 Gemini 336L (pyorbbecsdk2)
- **语音**: Vosk 语音识别 + Edge-TTS 语音合成

---

## 完整目录结构

```
body_posture_collector/
│
├── backend/                          # Python 后端
│   ├── __init__.py
│   ├── config/                       # 配置管理
│   │   ├── __init__.py
│   │   └── settings.py               # Pydantic 配置模型，读取 config.json
│   │
│   ├── core/                         # 核心业务逻辑
│   │   ├── __init__.py
│   │   ├── camera_manager.py         # 相机管理：初始化/流控制/帧获取/点云生成
│   │   ├── data_collector.py         # 数据采集：会话管理/图像保存/质量检查
│   │   ├── depth_analyzer.py         # 深度分析：人体检测/距离计算/状态判断
│   │   └── point_cloud.py            # 点云处理：PLY 读写/格式转换
│   │
│   ├── server/                       # 服务器
│   │   ├── __init__.py
│   │   └── ws_server.py              # WebSocket 服务器：消息路由/认证/预览/采集
│   │
│   ├── storage/                      # 存储管理
│   │   ├── __init__.py
│   │   ├── file_manager.py           # 文件管理：会话目录/文件查询
│   │   └── ply_writer.py             # PLY 写入：ASCII/二进制格式点云导出
│   │
│   ├── utils/                        # 工具类
│   │   ├── __init__.py
│   │   └── frame_processor.py        # 帧处理：JPEG 编码/深度可视化/缩放
│   │
│   └── voice/                        # 语音模块
│       ├── __init__.py
│       ├── command_parser.py         # 命令解析：语音指令识别与映射
│       ├── recognizer.py             # 语音识别：Vosk 中文语音识别
│       └── synthesizer.py            # 语音合成：Edge-TTS 文字转语音
│
├── frontend/                         # Electron + React 前端
│   ├── package.json                  # npm 依赖配置
│   ├── electron-main.js              # Electron 主进程：窗口管理/IPC/启动 React
│   ├── preload.js                    # 预加载脚本：安全暴露 IPC API
│   │
│   └── src/                          # React 源码
│       ├── App.js                    # 主应用：WebSocket 连接/状态管理/布局
│       ├── index.js                  # 入口文件
│       │
│       ├── components/               # UI 组件
│       │   ├── ControlPanel.js       # 控制面板：采集/会话/设置
│       │   ├── ControlPanel.css
│       │   ├── PreviewPanel.js       # 预览面板：RGB/深度实时预览
│       │   ├── PreviewPanel.css
│       │   ├── StatusBar.js          # 状态栏：距离/连接/采集状态
│       │   └── StatusBar.css
│       │
│       ├── services/                 # 服务层（预留）
│       │
│       └── styles/                   # 全局样式
│           ├── App.css
│           └── global.css
│
├── config/                           # 运行时配置
│   └── camera_params.json            # 相机参数：曝光/白平衡/亮度等
│
├── config.example.json               # 配置示例模板
│
├── data/                             # 采集数据目录
│   └── sessions/                     # 会话数据（按会话名分组）
│       └── <session_name>/
│           ├── metadata.json         # 会话元数据
│           ├── rgb_*.png             # RGB 图像
│           ├── depth_*.npz           # 深度数据（NumPy 格式）
│           ├── depth_vis_*.png       # 深度可视化图像
│           └── pointcloud_*.ply      # 3D 点云
│
├── models/                           # 语音模型
│   ├── .gitkeep
│   └── vosk-model-small-cn-0.22/    # Vosk 中文小型语音模型
│
├── temp/                             # 临时文件（运行时生成，不入库）
│   └── tts/                          # TTS 临时音频文件
│
├── tmp/                              # 临时目录（空）
├── Log/                              # SDK 日志
├── logs/                             # 应用日志
│
├── go.bat                            # 一键启动脚本（后端 + 前端）
├── run.bat                           # 启动脚本（备用）
├── run_backend.py                    # 后端启动入口
├── run_frontend.bat                  # 前端启动脚本（备用）
│
├── requirements.txt                  # Python 依赖
├── .gitignore                        # Git 忽略规则
├── README.md                         # 项目说明
├── INSTALL.md                        # 安装指南
├── START.txt                         # 快速启动说明
└── LICENSE                           # 许可证
```

---

## 核心模块说明

### 后端模块

| 模块 | 文件 | 职责 |
|------|------|------|
| **相机管理** | `core/camera_manager.py` | 奥比中光 SDK 封装，RGB/深度流初始化，帧同步对齐(D2C_HW)，点云生成(XYZRGB)，Mock 模式支持 |
| **数据采集** | `core/data_collector.py` | 会话创建/选择/完成，RGB(PNG)/深度(NPZ+可视化PNG)/点云(PLY) 保存，图像质量检查(亮度/模糊/深度覆盖) |
| **深度分析** | `core/depth_analyzer.py` | 深度范围过滤(300-3000mm)，形态学运算，人体轮廓检测，距离计算与历史平滑(5帧平均) |
| **点云处理** | `core/point_cloud.py` | 点云数据结构封装，坐标变换 |
| **WebSocket 服务器** | `server/ws_server.py` | 前后端实时通信，Token 认证，消息路由(预览/采集/会话/语音/退出) |
| **文件管理** | `storage/file_manager.py` | 会话目录管理，文件列表查询 |
| **PLY 写入** | `storage/ply_writer.py` | ASCII/二进制 PLY 格式点云导出，无效点过滤 |
| **帧处理** | `utils/frame_processor.py` | JPEG 编码，深度图可视化，分辨率缩放 |
| **语音识别** | `voice/recognizer.py` | Vosk 中文语音识别，麦克风音频流处理 |
| **语音合成** | `voice/synthesizer.py` | Edge-TTS 文字转语音，临时文件管理 |
| **命令解析** | `voice/command_parser.py` | 语音指令解析(开始采集/停止/下一个/完成) |
| **配置管理** | `config/settings.py` | Pydantic 配置模型，从 config.json 加载 |

### 前端模块

| 模块 | 文件 | 职责 |
|------|------|------|
| **Electron 主进程** | `electron-main.js` | 窗口创建，React 开发服务器管理，IPC 通信 |
| **预加载脚本** | `preload.js` | 安全暴露 electronAPI (版本/路径/Token/关闭窗口) |
| **主应用** | `App.js` | WebSocket 连接管理(含认证)，全局状态，布局控制 |
| **控制面板** | `components/ControlPanel.js` | 采集按钮，会话管理，设置面板 |
| **预览面板** | `components/PreviewPanel.js` | RGB/深度实时预览显示 |
| **状态栏** | `components/StatusBar.js` | 距离状态，连接状态，采集计数 |

---

## WebSocket 消息协议

### 客户端 → 服务端

| 消息类型 | 参数 | 说明 |
|----------|------|------|
| `auth` | `token` | 认证握手（连接后首条消息） |
| `start_preview` | — | 开始实时预览 |
| `stop_preview` | — | 停止实时预览 |
| `capture_single` | `options?` | 单次采集 |
| `create_session` | `session_name` | 创建新会话 |
| `select_session` | `session_name` | 选择已有会话 |
| `finish_session` | — | 完成当前会话 |
| `get_sessions` | — | 获取会话列表 |
| `get_captures` | — | 获取当前会话采集列表 |
| `get_capture_image` | `filename` | 获取采集图像(base64) |
| `get_distance` | — | 获取当前距离信息 |
| `speak` | `text` | 语音合成播放 |
| `exit_app` | — | 退出应用 |

### 服务端 → 客户端

| 消息类型 | 数据 | 说明 |
|----------|------|------|
| `preview_frame` | `color, depth, distance` | 实时预览帧 |
| `capture_result` | `success, paths, error` | 采集结果 |
| `session_created` | `session_id` | 会话创建成功 |
| `session_list` | `sessions[]` | 会话列表 |
| `capture_list` | `captures[], count` | 采集列表 |
| `capture_image` | `filename, image(base64)` | 采集图像 |
| `distance_update` | `distance_mm, status, message` | 距离更新 |
| `voice_activity` | `active` | 语音活动状态 |
| `session_finished` | `capture_count` | 会话完成 |
| `exit_confirm` | `message` | 退出确认 |
| `error` | `message` | 错误信息 |

---

## 启动流程

```
go.bat
├── [1] start "Backend" cmd /c "python run_backend.py"
│         └── WebSocketServer.start()
│               ├── 生成 auth_token → .ws_token
│               ├── 初始化相机 (Gemini 336L / Mock)
│               ├── 启动语音系统 (Vosk + Edge-TTS)
│               └── 监听 ws://localhost:8765
│
└── [2] start "Frontend" cmd /c "cd frontend && npm start"
          └── Electron → BrowserWindow
                ├── preload.js 读取 .ws_token
                └── React App 连接 ws://localhost:8765
                      └── 发送 auth token 认证
```

---

## 数据采集流程

```
用户操作 (按钮/语音)
    │
    ├── capture_single
    │     ├── 检查采集锁 (防止并发)
    │     ├── 语音提示 "开始采集"
    │     ├── 获取相机帧 (RGB + Depth)
    │     ├── 人体检测 (深度分析)
    │     ├── 生成点云 (可选)
    │     ├── 图像质量检查 (亮度/模糊/深度覆盖)
    │     ├── 保存文件 (RGB/Depth/Pointcloud)
    │     └── 广播采集结果
    │
    └── create_session → capture × N → finish_session
          └── data/sessions/<session_name>/
                ├── metadata.json
                ├── rgb_001.png
                ├── depth_001.npz
                ├── depth_vis_001.png
                └── pointcloud_001.ply
```

---

## 依赖说明

### Python (requirements.txt)

| 包 | 用途 |
|----|------|
| `numpy` | 数值计算，深度数据处理 |
| `opencv-python` | 图像处理，质量检查 |
| `pyorbbecsdk2` | 奥比中光相机 SDK |
| `websockets` | WebSocket 服务器 |
| `vosk` | 中文语音识别 |
| `edge-tts` | 语音合成 |
| `pyaudio` | 麦克风音频输入 |
| `pygame` | 音频播放 |
| `pydantic` | 配置数据验证 |
| `loguru` | 日志管理 |
| `open3d` | 点云处理 |

### 前端 (package.json)

| 包 | 用途 |
|----|------|
| `react` / `react-dom` | UI 框架 |
| `antd` | UI 组件库 |
| `electron` | 桌面应用框架 |
| `react-scripts` | 构建工具 |

---

## 配置说明

运行时配置从 `config.json` 加载（参考 `config.example.json`）：

| 配置组 | 关键参数 | 说明 |
|--------|----------|------|
| `camera` | `width=1280, height=800, fps=30` | 相机分辨率和帧率 |
| `voice` | `enabled, model_path, tts_voice` | 语音系统开关和模型 |
| `storage` | `output_dir, save_rgb/depth/pointcloud` | 存储路径和保存选项 |
| `distance` | `target_distance_mm=1000, tolerance_mm=200` | 目标距离和容差 |
| `gui` | `preview_fps=20, jpeg_quality=50` | 预览参数 |
