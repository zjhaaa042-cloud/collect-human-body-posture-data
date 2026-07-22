# 人体体态数据采集系统

基于奥比中光 Gemini 336L 深度相机的多模态人体数据采集工具。系统通过浏览器工作台同步采集 RGB、原始深度、人体分割、姿态关键点和三维点云，并为腰围建模保存受试者、采集分组、质量控制与逐帧标定信息。

> 当前数据格式版本：`v3`。项目主要面向 Windows 和 Orbbec 实机采集场景。

## 功能特性

- RGB、16 位深度 NPZ、彩色深度预览和 PLY 点云同步采集。
- MediaPipe 人体姿态、二值 `mask`、`bbox` 与采集完整性检查。
- 距离、画面边距、人体深度覆盖、图像清晰度和姿态稳定性综合质检。
- 手动采集、稳定后自动连拍，以及不合格样本的强制采集与人工复核。
- 匿名 `subject_id`、`visit_id`、`capture_group_id` 和多角度采集标注。
- 腰围重复测量、协议版本、测量者和最终均值记录。
- 每次采集独立保存相机内参、方向、深度单位、序列号和标定版本。
- 中文语音命令、实时 RGB-D 预览、历史记录与会话管理。
- ASCII 与 binary little-endian PLY 读写。

## 系统架构

```text
Gemini 336L
    │ RGB + Depth
    ▼
Python 后端 ── 姿态/分割/质检 ── 数据落盘
    │ WebSocket
    ▼
React + Ant Design 采集工作台
```

| 模块 | 技术 | 主要职责 |
| --- | --- | --- |
| 后端 | Python、asyncio、WebSocket | 相机控制、数据分析、采集和存储 |
| 视觉 | OpenCV、MediaPipe | 图像处理、姿态估计和人体分割 |
| 相机 | `pyorbbecsdk2` | RGB-D 帧同步、对齐和设备控制 |
| 前端 | React 18、Ant Design 5、Vite | 实时预览、标注、质检和会话管理 |
| 桌面端 | Electron 28 | 可选桌面应用封装 |
| 语音 | Vosk、Edge-TTS | 中文命令识别和语音提示 |

更完整的模块说明见 [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md)。

## 环境要求

- Windows 10/11 64 位。
- Python 3.10–3.12；不推荐 Python 3.13。
- Node.js 18 LTS 或更高版本，推荐 Node.js 20/22 LTS。
- 奥比中光 Gemini 336L、对应驱动及可用的 USB 3.x 接口。
- 首次安装依赖和姿态模型时需要网络连接。

## 快速开始

### 1. 获取项目

```powershell
git clone https://gitee.com/ZhangJiaHuidjj/collect-human-body-posture-data.git
cd collect-human-body-posture-data
```

### 2. 安装依赖

推荐使用项目提供的安装脚本，它会创建 `.venv`、安装 Python/前端依赖，并下载 MediaPipe 姿态模型：

```powershell
.\install_deps.bat
```

如需手动安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
cd frontend
npm install
cd ..
```

语音识别为可选能力。启用时，将 Vosk 中文模型放到 `models/`，并确保 `config.json` 中的 `voice.model_path` 指向该目录。详细说明见 [INSTALL.md](INSTALL.md)。

### 3. 启动系统

```powershell
.\go.bat
```

启动后访问：

- 采集工作台：<http://localhost:3000>
- 后端健康检查：<http://localhost:8765/health>
- WebSocket：`ws://localhost:8765`

`go.bat` 会检查依赖并统一管理前后端进程；关闭前端后，后端和 Vite 服务也会自动退出。

## 开发模式

分别启动后端和前端：

```powershell
# 终端 1
.\.venv\Scripts\python.exe run_backend.py

# 终端 2
cd frontend
npm run start:no-open
```

构建前端：

```powershell
cd frontend
npm run build
```

运行后端测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 推荐相机安装

- Gemini 336L 默认按顺时针方向竖装，镜头中心建议离地 `0.85–0.95 m`，光轴保持水平。
- 在工作台中选择顺时针、逆时针或横装方向；设置会写入 `config.json`。
- 建议受试者与相机保持 `1.3–2.0 m` 距离，并确保头、手和脚没有被画面裁切。
- 同一会话开始采集后，不要更换相机、安装方向、分辨率、深度单位或标定版本。

系统会锁定首帧采集配置；如果配置发生变化，将要求新建会话，避免不同标定的数据混写。

## 标准采集流程

1. 连接相机并选择实际安装方向。
2. 填写匿名受试者 ID、访视 ID 和腰围重复测量值，然后新建会话。
3. 设置采集组、视角、姿态、衣着类型和相机高度。
4. 引导受试者进入推荐距离，等待综合质量达到采集标准。
5. 手动采集或开启自动连拍；强制采集的数据会进入待复核状态。
6. 在历史记录中将待复核样本标记为“保留”或“隔离”。
7. 完成会话。完成后的会话会被锁定，不能继续写入。

推荐每名受试者采集 `0°、45°、90°、135°、180°、225°、270°、315°` 八个角度。同一角度的连续帧应使用同一个 `capture_group_id`。

### 腰围测量约定

- 使用匿名编号，不在目录或元数据中记录姓名。
- 受试者自然站立、腹部放松，在平静呼气末测量。
- 测量截面为最低肋骨与髂嵴中点高度的水平截面。
- 推荐重复测量 3 次；系统保存原始值并自动计算均值。
- 贴身或统一衣着优先，宽松衣物应如实标注。

## 数据格式

数据默认保存在 `data/sessions/<session_id>/`：

```text
data/sessions/<session_id>/
├── rgb/             # RGB PNG
├── depth/           # 原始 uint16 NPZ 和伪彩预览 PNG
├── pointcloud/      # PLY 场景点云，坐标单位为毫米
├── pose/            # 2D/3D 姿态关键点 JSON
├── mask/            # 人体二值 mask PNG
├── calibration/     # 每次采集的相机与标定快照 JSON
└── metadata.json    # 会话、真值、分组、QC 和文件索引
```

### 关键约定

- 训练深度模型必须读取 NPZ 中的 `depth` 和 `depth_scale`。
- 伪彩深度 PNG 经过逐帧归一化，只能用于查看，不能作为模型输入。
- PLY 当前是整幅场景点云；训练前应结合人体 mask 提取前景。
- PLY 坐标单位为毫米；需要米制输入时应除以 `1000`。
- 数据集必须按 `subject_id` 划分训练、验证和测试集，不能按图片随机划分。
- 连续帧属于同一 `capture_group_id`，不能视为独立受试者样本。
- 强制采集或未通过自动质检的数据默认标记为 `needs_review`。

## 配置

运行时配置位于 `config.json`。仓库提供 [config.example.json](config.example.json) 作为参考。

| 配置组 | 常用字段 | 说明 |
| --- | --- | --- |
| `camera` | `width`、`height`、`fps`、`orientation` | 相机流和安装方向 |
| `camera` | `calibration_version`、`params_file` | 标定版本和设备参数文件 |
| `storage` | `output_dir`、`save_*`、`quality_check` | 输出路径、数据类型和基础质检 |
| `distance` | `min_distance_mm`、`max_distance_mm` | 推荐采集距离 |
| `distance` | `min_edge_margin`、`min_quality_score` | 人体完整性和质量阈值 |
| `voice` | `enabled`、`model_path` | 语音功能和 Vosk 模型 |
| `gui` | `preview_fps`、`jpeg_quality` | 预览帧率和画质 |

相机曝光、增益和白平衡等设备参数位于 `config/camera_params.json`。

## 语音命令

| 指令 | 动作 |
| --- | --- |
| “开始采集” | 开始一次采集 |
| “停止” | 停止当前操作 |
| “下一个” | 准备下一次采集 |
| “完成” | 完成并锁定当前会话 |

## 常见问题

| 问题 | 处理方式 |
| --- | --- |
| 未检测到相机 | 关闭 OrbbecViewer 等占用程序，检查驱动、USB 3.x 连接和设备管理器 |
| `pyorbbecsdk2` 安装失败 | 安装对应 Orbbec SDK/运行库后重试，参考 [INSTALL.md](INSTALL.md) |
| 姿态模型不可用 | 重新运行 `install_deps.bat`，确认 `models/pose_landmarker_full.task` 存在 |
| 自动采集不可用 | 检查 MediaPipe 模型，并确认画面中有完整人体 |
| 图像质量不达标 | 调整距离、光照、相机高度和人体边缘余量 |
| 端口被占用 | 释放 `3000` 和 `8765`，或在配置中修改后端端口 |
| 前端依赖缺失 | 在 `frontend` 下重新运行 `npm install` |
| 语音识别不可用 | 检查 Vosk 模型路径；不需要语音时可设置 `voice.enabled=false` |

## 数据与隐私

采集结果可能包含可识别的人体 RGB 图像。请在获得受试者授权后采集，并按照适用的隐私、伦理和数据安全要求保存、传输及删除数据。提交代码时不要将 `data/`、身份映射表、鉴权令牌或本地配置一并上传。

## 参与开发

欢迎通过 Issue 或 Pull Request 提交问题与改进。提交前请：

1. 保持采集格式向后兼容，或明确说明格式版本升级。
2. 为存储、标定或质检逻辑补充测试。
3. 运行后端测试和前端生产构建。
4. 不提交真实受试者数据、模型大文件、日志或本地密钥。

## 许可证

本项目基于 [Apache License 2.0](LICENSE) 开源。
