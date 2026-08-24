# 人体体态数据采集系统

面向奥比中光 Gemini 336L 与 Intel RealSense D435i 的人体多模态形态数据采集工具。系统通过浏览器工作台按条件同步采集 RGB、原始/对齐深度和双红外，以姿态与质量分析辅助操作，并保存受试者、人体测量、条件进度、质量控制和逐帧标定信息。

> 正式采集协议：`RealAnthro-RGBD-v1.0`；旧 `data/sessions` 兼容格式：`v3`。项目主要面向 Windows 下 Gemini 336L 与 D435i 实机采集。

## 功能特性

- 每个条件同步采集 5 帧 RGB、原始深度、对齐深度、左红外和右红外。
- Gemini 336L 与 D435i 双相机适配，正式文件始终保持传感器原始方向。
- MediaPipe 姿态、人体分割和实时质量分仅用于操作提示，不作为协议硬门禁。
- 按条件动态应用 `1.5–4.0 m` 距离窗口、固定条件顺序和人工确认。
- 默认采用 `full31_no_lux` 条件矩阵，支持 F03 证据帧复核与条件重采。
- 匿名 `subject_id` 管理；M01–M13 人工测量必填，M14–M23 可留空。
- 逐帧保存内外参、深度单位、设备序列号、时间戳和质量快照。
- 原子提交、逐文件 SHA-256、独占锁和中断恢复，避免半成品混入数据集。

| 功能 | 说明 |
| --- | --- |
| 距离检测 | 实时检测采集对象与相机的距离，辅助保持合适采集位置 |
| 多模态采集 | 每个条件一次完成五帧五模态 burst |
| 实时预览 | 在界面中展示彩色画面和深度画面 |
| 受试者管理 | 支持新建、选择、恢复和完成匿名协议受试者 |
| 条件管理 | 展示条件进度、指定重采条件并保留 F03 复核证据 |
| 人工测量 | M01–M13 必填，M14–M23 选填 |
| 语音控制 | 可选且需显式启用；协议采集事务开始后不可中途取消 |
| RealAnthro 协议工作台 | 默认 `full31_no_lux`，五帧五模态、F03 复核、M01–M13 门禁 |
| 可审计存储 | 原子提交、逐文件 SHA-256、sidecar、独占锁和中断恢复 |

## 系统架构

```text
Gemini 336L / RealSense D435i
    │ RGB + 原始/对齐 Depth + 双 IR
    ▼
Python 后端 ── 预览质检/协议条件校验 ── 原子落盘
    │ WebSocket
    ▼
React + Ant Design 采集工作台
```

| 模块 | 技术 | 主要职责 |
| --- | --- | --- |
| 后端 | Python、asyncio、WebSocket | 相机控制、数据分析、采集和存储 |
| 视觉 | OpenCV、MediaPipe | 图像处理、姿态估计和人体分割 |
| 相机 | `pyorbbecsdk2`、`pyrealsense2` | RGB-D 帧同步、对齐和设备控制 |
| 前端 | React 18、Ant Design 5、Vite | 实时预览、协议条件、人工测量和复核 |
| 桌面端 | Electron 28 | 可选桌面应用封装 |
| 语音 | Vosk、Edge-TTS | 中文命令识别和语音提示 |

更完整的模块说明见 [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md)。

## 环境要求

- Windows 10/11 64 位。
- Python 3.10 或 3.11（D435i 运行时锁定在该范围）。
- Node.js 18 LTS 或更高版本，推荐 Node.js 20/22 LTS。
- 默认 `full31_no_lux` 完整采集需要 Gemini 336L 与 D435i 两台相机、对应驱动及可用的 USB 3.x 接口；仅联调精简 profile 时才可只用其中一台。
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
.\.venv\Scripts\python.exe -m pytest -q
```

## 推荐相机安装

- 相机镜头中心建议离地 `0.85–0.95 m`，光轴保持水平，并使用稳定的 USB 3.x 连接。
- Gemini 横装或竖装预览方向通过 `config.json` 的 `camera.orientation` 设置；该方向只影响操作员预览，不改变正式原始数据。
- 受试者站位按当前条件的动态距离提示执行，协议覆盖 `1.5–4.0 m`，并确保头、手和脚完整入镜。
- 仅在条件矩阵要求时切换 Gemini 与 D435i；同一受试者内，每种型号首次提交后会锁定设备序列号、流配置、深度单位和标定指纹，后续不一致将拒绝写入。

正式协议会校验当前条件要求的相机型号，并把实际设备和标定快照逐帧落盘；遇到指纹不一致时，应停止当前受试者并排查，不要用另一台同型号设备继续混采。

## 标准采集流程

1. 准备并检查 Gemini 336L 与 D435i；完整 Full-31 必须两台均可用，再按下一条件提示激活对应相机。
2. 使用匿名 `subject_id` 新建协议受试者，并选择默认的 `full31_no_lux` 条件矩阵。
3. 按工作台顺序核对相机、距离、角度、姿态和衣着条件；由操作员人工确认后采集。
4. 每个条件一次采集 5 帧五模态数据；如果系统要求，检查 F03 证据帧并决定通过或重采。
5. 完成全部条件后填写人工测量：M01–M13 必填，M14–M23 选填且允许留空。
6. 检查条件进度和测量门禁，完成受试者；完成后记录锁定，不再追加采集。

人工测量必须使用匿名编号并按界面定义的姿态、解剖点和单位执行。必填项不得用 `0`、占位值或估算值代替；暂时无法获得的选填项应直接留空。

旧 `realanthro-capture-v1.0` 活动受试者的冻结策略不会被静默改写：可以继续处理既有 attempt 的复核、人工测量和完成，但不能新增图像。正式续采应创建新的 v1.1 协议受试者。

## Legacy v3 数据格式（兼容）

旧会话只读兼容数据位于 `data/sessions/<session_id>/`；正式 RealAnthro 采集不写入此目录：

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
| `camera` | `width`、`height`、`fps`、`orientation` | 相机流；`orientation` 仅影响 Gemini 操作预览 |
| `camera` | `calibration_version`、`params_file` | 标定版本和设备参数文件 |
| `storage` | `output_dir` | 正式协议根输出路径 |
| `storage` | `save_*`、`quality_check` | 旧版兼容字段，不会关闭正式协议的五个必存模态 |
| `distance` | `min_distance_mm`、`max_distance_mm` | 预览初始值；正式采集由当前条件距离动态覆盖 |
| `distance` | `min_edge_margin`、`min_quality_score` | 姿态/质量提示阈值，不是正式协议硬门禁 |
| `voice` | `enabled`、`model_path` | 语音功能和 Vosk 模型 |
| `gui` | `preview_fps`、`jpeg_quality` | 预览帧率和画质 |

相机曝光、增益和白平衡等设备参数位于 `config/camera_params.json`。
正式协议固定保存 RGB、原始深度、对齐深度、左 IR 和右 IR；不能通过 `save_*` 关闭任一模态。

## 语音命令

| 指令 | 动作 |
| --- | --- |
| “开始采集” | 仅在协议语音控制已启用时，采集当前条件 |
| “停止” | 不会中断已经开始的五帧原子采集事务 |
| “完成” | 仅在协议语音控制已启用时，尝试完成当前受试者 |

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

历史旧会话数据位于 `data/sessions/`；旧版单帧和自动连拍写入入口已停用，正式 RealAnthro 采集不得混用旧目录。

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
| `pyorbbecsdk2` 安装失败 | 安装对应 Orbbec SDK/运行库后重试，参考 [INSTALL.md](INSTALL.md) |
| 姿态模型不可用 | 重新运行 `install_deps.bat`，确认 `models/pose_landmarker_full.task` 存在 |
| 姿态或质量提示不可用 | 检查 MediaPipe 模型，并确认画面中有完整人体；正式条件仍需人工确认 |
| 图像质量不达标 | 调整距离、光照、相机高度和人体边缘余量 |
| Windows 原子改名短时拒绝访问 | 当前会对锁错误 5/32/33 有限重试；若仍失败会保留 staging，使用 DEV_ONLY 恢复脚本审计，禁止手工复制覆盖 |
| 语音模型加载失败 | 确认模型已下载并放置在正确目录 |

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
