# 人体体态数据采集系统

面向奥比中光 Gemini 336L 与 Intel RealSense D435i 的人体多模态形态数据采集工具。系统通过一个统一的四步工作流完成受试者登记、双机八角度采集、人体测量和任务完成，并保存逐帧标定与时间同步审计信息。

> 默认工作台协议：`dual-rgbd-v2.2`（统一四步双机八角度）；`RealAnthro-RGBD-v1.0` 与旧 `data/sessions` 格式仅保留后端兼容。项目主要面向 Windows 下 Gemini 336L 与 D435i 实机采集。

## 功能特性

- 默认双机流程为 360° 每 45° 一组，共 8 组；每组由 Gemini 336L 与 D435i 各采 5 帧 RGB-D、原始 uint16 NPY、伪彩深度和带颜色点云数据。
- 双机实时预览同时显示两台相机的 RGB 与深度画面；正式文件始终保持传感器原始方向。
- MediaPipe 姿态、人体分割和实时质量分仅用于操作提示，不作为协议硬门禁。
- 距离可在登记时填写默认值，也可在每个角度采集前单独修改。
- 匿名 `subject_id` 管理；仅 M01、M03、M06、M09、M12 五项人工测量必填，其余项目可留空。
- Gemini 336L 是全身主视角；D435i 作为辅助 RGB-D 视角，2.5 m 处允许因硬件 FOV 产生的画幅裁切，不作为全身入框门禁。
- 逐帧保存内外参、深度单位、设备信息和时间戳。
- 原子提交、逐文件 SHA-256、独占锁和中断恢复，避免半成品混入数据集。

| 功能 | 说明 |
| --- | --- |
| 距离检测 | 实时检测采集对象与相机的距离，辅助保持合适采集位置 |
| 多模态采集 | 每个角度两台相机各完成五帧 RGB-D burst，并保存伪彩深度与 PLY |
| 实时预览 | 双机同时展示各自的彩色与深度画面 |
| 受试者管理 | 登记一次后在八角度、人体测量和完成步骤中复用；支持从原输出目录恢复 |
| 八角度管理 | 固定按 0°–315° 每 45° 顺序采集并展示进度 |
| 人工测量 | M01 身高、M03 肩峰间宽、M06 胸围、M09 腰围、M12 臀围必填，其余选填 |
| 语音控制 | 可选且需显式启用；协议采集事务开始后不可中途取消 |
| 统一工作台 | 受试者登记 → 双机八角度 → 人体测量 → 完成 |
| 可审计存储 | 原子提交、逐文件 SHA-256、sidecar、独占锁和中断恢复 |

## 系统架构

```text
Gemini 336L / RealSense D435i
    │ RGB + 原始/对齐 Depth
    ▼
Python 后端 ── 预览质检/协议条件校验 ── 原子落盘
    │ WebSocket
    ▼
React + Ant Design 采集工作台
```

| 模块 | 技术 | 主要职责 |
| --- | --- | --- |
| 后端 | Python、asyncio、WebSocket | WebSocket 接入、双机应用服务、相机控制、数据分析和原子存储 |
| 视觉 | OpenCV、MediaPipe | 图像处理、姿态估计和人体分割 |
| 相机 | `pyorbbecsdk2`、`pyrealsense2` | RGB-D 帧同步、对齐和设备控制 |
| 前端 | React 18、Ant Design 5、Vite | 实时预览、受试者登记、八角度进度、人工测量和完成 |
| 桌面端 | Electron 28 | 可选桌面应用封装 |
| 语音 | Vosk、Edge-TTS | 中文命令识别和语音提示 |

更完整的模块说明见 [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md)。

## 环境要求

- Windows 10/11 64 位。
- Python 3.10 或 3.11（D435i 运行时锁定在该范围）。
- Node.js `20.19+` 或 `22.12+`（Vite 8 的运行时要求）。
- 完整采集需要 Gemini 336L 与 D435i 两台相机、对应驱动及可用的 USB 3.x 接口。
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

## 打包为 Windows EXE

开发机完成依赖安装后，双击 `build_windows.bat`，或在 PowerShell 中运行：

```powershell
.\scripts\build_windows.ps1
```

脚本会使用 PyInstaller 生成内置 Python 后端，再构建前端并通过 Electron Builder 生成安装包：

```text
frontend/release/BodyPostureCollector-Setup-1.0.3.exe
```

安装后的应用不要求操作员另行安装 Python、Node.js 或执行 `go.bat`。应用会自动启动本地采集后端并等待健康检查；正常退出时也会安全关闭后端。默认数据目录为用户“文档”下的 `BodyPostureCollectorData`，双机流程仍可在界面中选择其他输出目录。

只需要快速验证未安装目录时，可运行：

```powershell
.\scripts\build_windows.ps1 -DirectoryOnly
```

目录包输出到 `frontend/release/win-unpacked`。打包产物仍需在目标采集电脑上安装 Gemini 336L、D435i 对应的系统驱动；Python SDK 和运行库已包含在应用中。

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
- 默认双机流程的 Gemini 距离提示跟随登记时的会话默认距离（建议 `2.5 m`）；临时修改单角度距离时，以输入的毫米值人工对位并留痕。Gemini 需确保头、手和脚完整入镜；D435i 尽量覆盖躯干、腰臀并保持稳定，不为追求全身入镜而单独改变同组站位。
- 旧版条件矩阵会按条件切换 Gemini 与 D435i，并校验设备指纹；默认双机八角度流程则同时保存两路帧元数据。

旧版 RealAnthro 协议会校验当前条件要求的相机型号和设备指纹；遇到指纹不一致时，应停止当前受试者并排查，不要用另一台同型号设备继续混采。

## 默认双机八角度流程

1. 在“1 受试者登记”填写匿名编号、选择任意可写 Windows 文件夹；服装备注和默认距离均可选。登记信息只填写一次，后续步骤直接复用。
2. 在“摄像头连接”中依次连接 Gemini 336L 和 D435i。两台都连接后，实时预览自动切换为双画面。
3. 进入“2 双机八角度”，按 `0°、45°、90°、135°、180°、225°、270°、315°` 顺时针依次转身。每个角度仍可修改距离，确认 Gemini 全身入镜且两路画面稳定后采集；D435i 画幅裁切不作为全身入框门禁。
4. 系统等待 2 秒后并发抓取两路各 5 帧，记录每对帧的主机时间差；成功后自动进入下一角度。
5. 八组完成后进入“3 人体测量”，完成 M01、M03、M06、M09、M12 五项必填项目；其余项目可留空。
6. 在“4 完成”确认八个角度与人体测量均通过门禁，结束受试者任务。数据位于所选文件夹的 `body_posture_dual_v2/subjects/<subject_id>/`。

关闭并重新启动工作台后，会自动尝试恢复最近的双机任务；也可在“1 受试者登记”重新选择原输出文件夹、填写原编号并点击“继续已有任务”。

双机为**主机时钟近同步**，不是硬件触发同步：采集若发现任一对帧主机时间差大于 75 ms，会拒绝该组。未完成双机外参标定和实机时差验收前，不应直接融合两个相机的点云或宣称严格同步。

上述 D435i 画幅策略仅适用于默认 `dual-rgbd-v2.2` 流程；旧版 RealAnthro 兼容接口保持原有门禁。

### 默认双机五项必填测量的复测与归约

以下归约和复核规则适用于默认 `dual-rgbd-v2.2` 流程。旧版 RealAnthro 兼容接口继续使用其冻结语义，不按本节规则重算历史数据。

| 项目 | 前两次允许差值 |
| --- | ---: |
| M01 身高 | `1.0 cm` |
| M03 肩峰间宽 | `1.0 cm` |
| M06 胸围 | `2.0 cm` |
| M09 腰围 | `1.5 cm` |
| M12 臀围 | `1.5 cm` |

这些是当前项目的现场复测触发阈值，不是对手工测量“无误差”的承诺。每次重复测量前需重新定位尺具和姿势。前两次差值小于或等于阈值时取两次均值；超过阈值时先做第三次，再取差值最小的两次均值，若两对并列最接近则取三次均值。如果第三次后最接近两次仍超阈值，系统保留原始三次、归约值和 `REVIEW_REQUIRED` 标记，不阻断当前受试者保存；现场有条件时优先重测。

胸围、腰围和臀围应在受试者放松站立、不说话的正常呼气末读数。系统保留每次原始读数，不用最终均值覆盖原始数据。

## 旧版标准采集流程（仅后端兼容）

旧条件矩阵不再显示在默认工作台中，避免与双机八角度形成两套并列流程。以下内容仅用于维护历史数据和旧接口。

1. 准备并检查 Gemini 336L 与 D435i；完整 Full-31 必须两台均可用，再按下一条件提示激活对应相机。
2. 使用匿名 `subject_id` 新建协议受试者，并选择默认的 `full31_no_lux` 条件矩阵。
3. 按工作台的任务组完成连续条件。每个 attempt 由操作员进行一次“就位确认”；界面会只突出相对上一条件变化的相机、距离、角度、姿态、衣着或重新站位项，后台仍保存完整确认留痕。
4. 每个条件一次采集 5 帧五模态数据；如果系统要求，检查 F03 证据帧并决定通过或重采。
5. 完成全部条件后填写人工测量：M01、M03、M06、M09、M12 五项必填，其余选填且允许留空。
6. 检查条件进度和测量门禁，完成受试者；完成后记录锁定，不再追加采集。

人工测量必须使用匿名编号并按界面定义的姿态、解剖点和单位执行。必填项不得用 `0`、占位值或估算值代替；暂时无法获得的选填项应直接留空。

### 降低现场操作负担的约定

- 条件按日常服、Gemini 标准视角、Gemini 变量条件和 D435i 条件分组；成功采集后工作台自动推进至下一项。
- `WARN` attempt 已安全落盘，会进入待复核队列；可先完成连续采集，再集中读取每项已保存且经哈希验证的 F03 证据进行接受或驳回。
- 操作员编号、器材编号和器材确认均不再是采集或人体测量门禁；五项必填测量的双次读数和完成门禁不变。器材日检接口保留给需要额外审计的部署，但默认界面不要求填写。
- `primary3` 仅适合联调和培训；`full31_no_lux` 才是当前正式采集矩阵。不要把轻量 profile 数据混入正式数据集。

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
| 原始/对齐深度 | 无损 uint16 PNG + uint16 NPY | 两种深度独立保存；NPY 与 PNG 逐像素一致，不降位 |
| 左/右红外 | PNG | 两路物理 IR 独立保存 |
| sidecar | JSON | `capture.json`、`qc.json`、`commit.json` |

历史旧会话数据位于 `data/sessions/`；旧版单帧和自动连拍写入入口已停用，正式 RealAnthro 采集不得混用旧目录。

双机八角度协议保存到创建任务时选择的任意 Windows 文件夹下：

```text
body_posture_dual_v2/subjects/<subject_id>/
├── session_manifest.json
└── angles/angle_000_front/capture_01_<UTC时间>/
    ├── camera_gemini_336l/
    │   ├── rgb_color/frame_01.png
    │   ├── depth_raw_uint16/frame_01.png
    │   ├── depth_raw_npy/frame_01.npy
    │   ├── depth_raw_color/frame_01.png
    │   ├── depth_aligned_uint16/frame_01.png
    │   ├── depth_aligned_npy/frame_01.npy
    │   ├── depth_aligned_color/frame_01.png
    │   └── pointcloud_color_xyz_mm/frame_01.ply
    ├── camera_realsense_d435i/     # 目录结构相同
    └── capture_manifest.json
```

`depth_*_uint16` 与 `depth_*_npy` 都保存传感器原始 `uint16` 量化值，并应逐像素一致；NPY 使用 `allow_pickle=False` 读取。毫米值为 `array * depth_scale_mm_per_unit`，比例记录在 `capture_manifest.json` 的逐帧元数据和 NPY 文件记录中。`depth_*_color` 仅用于查看。PLY 为带 RGB 颜色、单位毫米的二进制点云，按每 4 像素取一个点控制文件体积。每个新 attempt 另有 `commit.json`，用于中断后的哈希恢复。

RGB 在相机适配层统一为 `uint8 H×W×3`、RGB 通道顺序和 sRGB 色彩语义；预览 JPEG 与落盘 PNG 只在调用 OpenCV 编码器前转换为 BGR，并执行编码后回读校验。OpenCV 用户读取 PNG 后得到的是 BGR，训练或显示前应转换回 RGB。

三类数据的“对齐”分为两层：RGB 与 `depth_aligned` 在像素坐标、分辨率和内参上严格一致；`depth_raw` 必须保留深度相机原始坐标，不能伪装成 RGB 像素对齐。三者来自同一 SDK 帧集，并由帧号、时间戳及 raw-depth-to-color 外参建立可审计关联；任一契约不满足都会中止采集。

升级前已经提交的 attempt 不会回填 NPY；旧数据继续按原清单读取。升级后新增的双机和 RealAnthro attempt 都强制包含 raw/aligned 两类 NPY。

## 真机验收与 DEV_ONLY

以下命令均使用项目 `.venv`。相机验收应顺序执行，不要让两个进程同时占用同一设备。

```powershell
# 五帧 burst（示例：D435i）
.\.venv\Scripts\python.exe scripts\verify_protocol_camera.py --backend realsense --bursts 100 --summary-only

# 10 分钟稳定性
.\.venv\Scripts\python.exe scripts\verify_camera_stability.py --backend realsense --duration-seconds 600

# 隔离写入、F03 证据、REJECT 复核与严格审计
.\.venv\Scripts\python.exe scripts\verify_dev_only_capture.py --backend realsense --acknowledge-dev-only

# 双机近同步 + PNG/NPY/PLY/标定/哈希完整链（空场景即可）
.\.venv\Scripts\python.exe scripts\verify_dual_capture.py --acknowledge-dev-only
```

`DEV_ONLY` 数据只允许写入路径名含 `dev_only` 的项目内隔离目录，固定以 `REJECT` 结束，不会进入正式数据集。中断后的隔离运行可使用 `scripts/recover_dev_only_run.py` 做哈希恢复与审计。
双机脚本默认写入 `data/dev_only_validation/dual_npy_smoke/`，并在 `reports/hardware/dual_npy_smoke.json` 记录最大主机时间差、80 个文件和 20 组 NPY/PNG 一致性结果。

## 系统要求

- Python 3.10 或 3.11
- Node.js 20.19+ 或 22.12+
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
| 双机任务提示“待恢复/完整性异常” | 不要删除 `.staging` 或手工修改清单；重新打开原任务会自动校验完整 commit。只有完整且哈希一致的数据会被恢复，异常证据会原样保留并锁定写入 |
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
