# 人体体态数据采集系统

面向奥比中光 Gemini 336L 与 Intel RealSense D435i 的人体多模态形态数据采集工具。系统支持 RGB、原始/对齐深度、双红外和 3D 点云数据采集，并提供实时预览、距离检测、会话管理和语音控制能力。

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
| RealAnthro 协议工作台 | 默认 `full31_no_lux`，五帧五模态、F03 复核、M01–M13 门禁 |
| 可审计存储 | 原子提交、逐文件 SHA-256、sidecar、独占锁和中断恢复 |

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

RealAnthro 协议采集默认保存到：

```text
data/realanthro_rgbd_v1/collections/default_collection/
```

每次协议 attempt 位于：

```text
subjects/<subject_id>/cameras/<camera_code>/conditions/<condition_id>/attempts/<attempt_id>/
```

| 数据类型 | 格式 | 说明 |
| --- | --- | --- |
| RGB 图像 | PNG | 五帧，`F03` 为复核 anchor |
| 原始/对齐深度 | 无损 uint16 PNG | 两种深度独立保存，不降位 |
| 左/右红外 | PNG | 两路物理 IR 独立保存 |
| sidecar | JSON | `capture.json`、`qc.json`、`commit.json` |

旧会话模式仍使用 `data/sessions/`；正式 RealAnthro 采集不得混用旧会话目录。

## 真机验收与 DEV_ONLY

以下命令均使用项目 `.venv`。相机验收应顺序执行，不要让两个进程同时占用同一设备。

```powershell
# 五帧 burst（示例：D435i）
.\.venv\Scripts\python.exe scripts\verify_protocol_camera.py --backend realsense --bursts 100 --summary-only

# 10 分钟稳定性
.\.venv\Scripts\python.exe scripts\verify_camera_stability.py --backend realsense --duration-seconds 600

# 隔离写入、F03 证据、REJECT 复核与严格审计
.\.venv\Scripts\python.exe scripts\verify_dev_only_capture.py --backend realsense --acknowledge-dev-only
```

`DEV_ONLY` 数据只允许写入路径名含 `dev_only` 的项目内隔离目录，固定以 `REJECT` 结束，不会进入正式数据集。中断后的隔离运行可使用 `scripts/recover_dev_only_run.py` 做哈希恢复与审计。

## 系统要求

- Python 3.10 或 3.11
- Node.js 18+
- Windows 10+
- 奥比中光 Gemini 336L 与 Intel RealSense D435i

## 故障排除

| 问题 | 处理方式 |
| --- | --- |
| 端口占用 | 检查并关闭占用 `8765` 或 `3000` 端口的进程 |
| 前端依赖缺失 | 进入 `frontend` 后重新运行 `npm install` |
| 相机未检测到 | 检查 USB 连接、相机驱动和设备管理器状态 |
| Gemini 在设备管理器存在但 SDK 暂时返回 0 台 | 关闭占用相机的程序，完整重插/断电后等待枚举，再运行 burst 验收脚本；不要删除 staging 数据 |
| D435i 能枚举但无视频帧 | 按 `INSTALL.md` 运行官方 Windows 帧元数据脚本并重连设备 |
| pyrealsense2 无可用安装包 | 使用 Python 3.10/3.11；本项目锁定 SDK 2.54.2 |
| Windows 原子改名短时拒绝访问 | 当前会对锁错误 5/32/33 有限重试；若仍失败会保留 staging，使用 DEV_ONLY 恢复脚本审计，禁止手工复制覆盖 |
| 语音模型加载失败 | 确认模型已下载并放置在正确目录 |

## 许可证

Apache License 2.0
