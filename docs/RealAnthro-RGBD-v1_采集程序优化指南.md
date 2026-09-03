# RealAnthro-RGBD-v1 采集程序优化指南

版本：1.5-hardware-and-write-chain-validated
状态：**实施版 / 当前状态**
状态基线：2026-08-22
适用范围：当前项目的数据采集、现场质检、人工复核和人体测量录入
明确不包含：SMPL-X 拟合、模型训练、数据集划分、数据发布、访问分级和 benchmark 封存

## 1. 当前结论

项目已经从“优化建议”进入“协议驱动采集程序已实现、两台相机硬件稳定性与隔离写入/恢复链均通过、等待正式场地和真实受试者 Pilot 验收”的阶段。

当前结论如下：

1. 默认采集矩阵已经固定为 `full31_no_lux`，包含 Gemini 336L 的 24 条条件和 D435i 的 7 条条件，共 31 条。
2. 每个 condition 固定采集 5 组同步帧，`F03` 固定为 anchor；一名受试者完成当前矩阵应产生 155 组同步帧。
3. RGB、raw depth、aligned depth 和设备要求的 IR 均按独立模态保存。raw depth 与 aligned depth 都同时保存**无损 uint16 PNG 和原始 uint16 NPY**，不能互相覆盖，也不能降位成 8 bit。
4. 正式目录已经实现为 `subjects/<subject_id>/cameras/C336L|CD435I/conditions/<condition_id>/attempts/<attempt_id>/`。
5. 条件级 QC 策略与人体测量定义均随受试者固化；只追加 attempt、原子提交、逐文件 SHA-256、崩溃恢复、数据集独占锁、人工复核和完成完整性复查均已实现。
6. 所有尚未由真实受试者 Pilot 冻结的启发式 WARN 都必须进入实名人工复核；复核必须查看从已提交目录、sidecar 和逐文件哈希完整验证后读取的 `F03` RGB 与 aligned depth，接受动作还会再次校验证据摘要。
7. M01、M03、M06、M09、M12 五项人工测量的软件门禁已经实现；其余测量项目为选填。
8. Gemini 336L 与 D435i 均已完成 100 次五帧 burst、10 分钟连续取流和一次完整 `DEV_ONLY` 写盘/F03 复核；两类 Windows 原子替换中断点也已用真实 staging/review sidecar 完成无损恢复。
9. 当前自动验证结果为后端/协议测试 113 项通过、前端测试 15 项通过、前端生产构建通过；这些结果证明软件基线可进入正式场地验收和 Pilot，不替代真实受试者验收。

### 1.1 实施状态总表

| 模块 | 当前状态 | 结论 |
|---|---|---|
| 条件注册表与 3/27/31/36 profile | 已实现并有自动测试 | 可用 |
| 默认 `full31_no_lux` | 已启用 | 当前唯一推荐采集矩阵 |
| `full36` | 代码已定义，协议目录返回 `available=false` | 缺照度计和可复现灯光，不得启用 |
| 协议工作台前端 | 已实现 | 按受试者、条件、测量、完成门禁执行 |
| 五帧同步 burst | 已实现 | `F03` 固定为 anchor |
| 无损 PNG 与只追加存储 | 已实现 | 可用 |
| 协议快照、SHA-256、崩溃恢复、独占锁 | 已实现并有自动测试/真机故障恢复 | Windows 短时锁采用有限原子重试，证据不复制、不删除 |
| 耐久提交后的状态协调与写入阻断 | 已实现并有自动测试 | 异常时显式返回并阻止继续写入 |
| Pilot WARN 实名人工复核 | 已实现 | 接受前必须核验落盘 `F03` |
| 已通过条件的受控补采 | 已实现 | 必须填写原因并明确旧数据是否作废 |
| Gemini 336L 真机 | 五帧 smoke、100/100 次 burst、10分钟连续预览均通过 | 硬件流稳定性已验收 |
| D435i | SDK/元数据环境已修复；100/100 次 burst、10分钟连续取流和 DEV_ONLY 写入链通过 | 硬件流与存储链已验收 |
| M01–M23 软件录入与校验 | 已实现 | M01、M03、M06、M09、M12 五项必填，其余选填 |
| 真实受试者 Pilot | 未执行 | QC 阈值尚未冻结 |

## 2. 当前冻结的采集范围

### 2.1 Profile

| profile | 条件数 | 当前用途 | 可用状态 |
|---|---:|---|---|
| `primary3` | 3 | Gemini 正面条件的三次独立重新站位，快速联调 | 可用 |
| `gemini27` | 27 | Gemini 完整矩阵 | 可用，但含未定量光照条件 |
| `full31_no_lux` | 31 | 两台相机、去除五个非标准光照条件 | **默认且推荐** |
| `full36` | 36 | 两台相机完整矩阵 | 当前禁用 |

`full36` 的真实数量是 36，而不是 39。`full31_no_lux` 是 `full36` 去除五个非标准光照条件后的严格子集。

程序唯一真源是 `backend/protocol/`。前端通过 `protocol_catalog` 和 `protocol_subject_state` 获取条件，不维护第二份角度或条件列表。

### 2.2 `full31_no_lux` 构成

| 相机 | 条件构成 | 条件数 |
|---|---|---:|
| Gemini 336L / `C336L` | 12 视角、4 个额外距离、2 个独立复位、2 个额外姿态、4 个日常服条件 | 24 |
| Intel RealSense D435i / `CD435I` | 4 个标准视角、1 个额外距离、2 个独立复位 | 7 |
| 合计 |  | 31 |

Gemini 与 D435i 必须分时工作。切换相机时先停止当前设备，再连接协议要求的设备；不得让两套主动深度系统长期同时对准受试者。

### 2.3 Condition、repeat 与 burst

Condition ID 由以下字段唯一决定：

```text
<camera>_D<distance_mm>_V<yaw>_<light>_<pose>_<clothing>_R<repeat>
```

示例：

```text
C336L_D2500_V000_LSTD_P1_CF_R01
CD435I_D3000_V090_LSTD_P1_CF_R01
```

规则：

- `R01/R02/R03` 是独立站位重复，不是 burst 内的帧编号。
- `R02` 和 `R03` 必须让受试者完全离开站位区，再重新进入并重新对齐脚位。
- `F01–F05` 是同一个 condition 内的五帧同步 burst。
- `F03` 是固定 anchor。当前实现不自动用其他帧替代 `F03`；若 `F03` 不可接受，应驳回并补采。
- 每次采集前必须重新完成现场确认，不能复用上一个 attempt 的勾选。

## 3. 设备实施状态

### 3.1 Gemini 336L

已验收的真实设备：

- 型号：Gemini 336L
- 协议代码：`C336L`
- 序列号：`CPCD7530002G`
- 已完成：真实设备五帧 burst smoke（最近一次实测再次通过）
- 已确认模态：RGB、raw depth、aligned depth、左 IR、右 IR
- 已确认元数据：彩色/深度内参、raw depth 到 color 的外参、depth scale 和设备身份

单次 smoke 只证明真实设备链路、双 IR 和标定元数据能够进入统一 `FrameBundle`；它本身不等于稳定性验收。

最近一次实测取得五组 RGB、raw depth、aligned depth、左 IR 和右 IR；主帧号为 `4/7/10/13/16`，相邻样本设备时间约 `100.119 ms`，五帧标定哈希稳定，`hardware_pass=true`。唯一 WARN 为按当前策略必须人工判断的 `HUMAN_CONTENT_MANUAL_REVIEW`。SDK 启动阶段仍打印一次参数类诊断日志，但未中断枚举、取流、标定或五帧验证；在后续10分钟运行中也未造成超时或数据错误。

随后完成了连续 100 次五帧 burst：`completed_bursts=100`、`hard_passed_bursts=100`，共取得 500 组同步帧；无缺帧、无硬 QC 失败，标定哈希始终为 `78a8c2...bfc3eb`，设备采样间隔稳定在约 `100.119 ms`。场景距离/几何和人体内容提示仍属于 Pilot WARN，不影响本次硬件通过结论。

10分钟连续预览也已完成：实际运行 `601.066 s`，取得 `17,947` 组完整帧，平均 `29.859 fps`；超时、缺失模态、标定变化、帧号倒退和设备时间戳倒退均为0。主机处理期间有7次跨过单个设备帧，约占0.039%，记录为 `HOST_SAMPLING_SKIPPED_DEVICE_FRAMES` WARN；它不影响按约100 ms独立取样的正式五帧 burst。机器可读报告位于 `reports/hardware/gemini_336l_stability_600s_2026-08-22.json`。

Gemini 仍未完成的场景验收：

1. 使用真实受试者验证各距离下全身完整入框。

### 3.2 D435i

当前软件状态：

- 主虚拟环境已切换为 Python `3.10.20` 与 `pyrealsense2 2.54.2.5684`；
- `requirements.txt` 已锁定该 SDK，`install_deps.bat` 和 `go.bat` 会拒绝不兼容的 Python 3.12/3.13；
- `RealSenseCameraAdapter` 已实现 RGB、raw depth、aligned depth、双 IR、内外参、depth scale、设备时间戳和流帧号的统一输出；
- 主时钟明确使用彩色流，raw depth 次之，复合 frameset 只作兜底，避免复合对象在不同嵌入流之间切换造成伪帧号倒退；
- 连接期首帧门禁只有在限定时间内同时取得 RGB、raw depth、aligned depth、左 IR 和右 IR 时才返回成功；
- 适配器有无 SDK、模拟 FrameBundle、设备路由和主时钟选择均有自动测试。

当前硬件状态：

- 已识别设备：RealSense D435i，序列号 `243722074968`；
- 固件版本 `5.15.1.55`，USB 描述符为 `3.2`；Windows 的 RGB、Depth、IMU 和 USB 复合设备节点均报告正常；
- 原始故障由三层因素叠加：Windows 未注册 UVC 帧元数据、项目使用了与固件不匹配的 SDK `2.58.3`、Codex 工作区沙箱不允许 USB 视频帧进入测试进程；
- 已用 RealSense 官方 `realsense_metadata_win10.ps1` 为当前设备写入元数据注册项，并通过设备硬复位重新枚举；
- 官方 Viewer `2.54.2` 中 RGB、depth 及两者并行预览均正常；
- 最终主环境中 100/100 次完整五模态 burst 全部硬通过，共验证 500 组 RGB、raw depth、aligned depth、左 IR、右 IR；设备采样间隔范围 `95.565–104.202 ms`，均值 `100.039 ms`；
- 10 分钟连续验收实际运行 `602.813 s`，取得 `17,981` 组完整帧，平均 `29.828 fps`；超时、缺失模态、标定变化、主/分流帧号倒退和主/分流时间戳倒退均为 0；主机处理期间跨过 7 个设备帧（约 `0.039%`），记为 `HOST_SAMPLING_SKIPPED_DEVICE_FRAMES` WARN，`hardware_pass=true`；
- 完整 `DEV_ONLY` attempt 已验证 25 个图像文件、3 个 sidecar、F03 双证据哈希、REJECT 复核和严格恢复审计；
- 机器可读报告位于 `reports/hardware/d435i_bursts_100_2026-08-22.json`、`reports/hardware/d435i_stability_600s_2026-08-22.json` 和 `reports/hardware/d435i_dev_only_write_chain_2026-08-22.json`。

硬件与存储链现已通过，但仍不得用 Gemini 数据复制或伪装成 `CD435I` 条件；D435i 的 2.5/3.0 m 真实全身入框仍需在最终场地独立完成。

原始失败诊断报告 `reports/hardware/d435i_preflight_failure_2026-08-22.json` 继续保留为故障证据。通过报告不会覆盖或删除失败记录。

通过 Codex 执行真机命令时必须显式允许在工作区沙箱外读取 USB 相机；普通用户直接运行 `go.bat` 不受该 Codex 沙箱限制。不能把沙箱内的 `Frame didn't arrive` 直接判定为硬件故障。

### 3.3 光照与人体测量设备

当前无照度计和可复现固定灯具，所以：

- 只运行 `full31_no_lux`；
- 不采集 `LLOW/LBRI/LSL45`；
- `full36` 在协议目录中保持不可用；
- 标准光照 `LSTD` 不应被解释为已完成定量 lux 实验。

五项必填人工测量需要：

| 工具 | 主要对应项 | 当前状态 |
|---|---|---|
| 标准身高计 | M01 | 未到位 |
| Anthropometer 或大型滑动卡尺 | M03 | 未到位 |
| 无弹性人体测量软尺 | M06、M09、M12 | 未到位 |

工具未到位时可以进行 `DEV_ONLY` 联调，但不能把人体测量或受试者任务标记为合格完成。

## 4. 当前操作流程与硬门禁

当前主流程已经由协议工作台驱动：

1. 创建受试者，填写 `subject_id`、`operator_id`、年龄段、性别类别、服装尺码和内部采集同意。
2. 服务端为受试者固化 profile、条件顺序、人体测量定义和采集策略快照。
3. 连接当前 condition 指定的真实相机；设备必须返回可追溯序列号或 UID。
4. 前端显示当前/下一条件以及可执行的中文朝向、姿态、服装和距离提示。
5. 操作员逐项确认距离标线、朝向/姿态/服装和全身入框；`repeat_id > 1` 时还必须确认受试者已经离场后重新进入。
6. 现场确认绑定服务端生成的一次性 `confirmation_nonce`；nonce 使用后失效。
7. 相机和采集独占锁通过后，受试者稳定 2 秒，再采集五帧 burst。
8. 服务端完成客观 QC、无损落盘、逐文件哈希和原子提交，然后返回 attempt 状态。
9. `WARN` 进入实名 `F03` 人工复核；`FAIL` 进入补采；`PASS` 可直接成为 accepted attempt。
10. 完成 M01、M03、M06、M09、M12 五项人工测量。
11. 完成报告重新检查 accepted attempt、复核记录、文件、哈希、模态和人体测量后，才允许关闭受试者。

前端没有取消 RGB 或 Depth 的正式采集选项，也没有绕过缺项直接完成受试者的主操作。

## 5. 已实现的数据契约

### 5.1 FrameBundle

Gemini 和 D435i 适配器都输出统一的 `FrameBundle`，主要字段包括：

```text
color
depth_raw
depth_aligned
infrared.left / infrared.right
depth_scale
device_timestamp
frame_number
host_timestamp_ns
stream_timestamps
stream_frame_numbers
intrinsics
extrinsics
camera_metadata
```

关键约束：

- `depth_raw` 保持设备原始深度坐标系；
- `depth_aligned` 对齐到 RGB，宽高必须和 RGB 一致；
- 两者始终分别保存，不能以 aligned depth 覆盖 raw depth；
- 深度图必须是 `uint16 H×W`；
- RGB 必须是 `uint8 H×W×3`；
- IR 必须是 `uint8` 或 `uint16 H×W`；
- depth scale、内参、外参、流 profile、时间戳和帧号进入元数据与 QC。

### 5.2 文件格式

| 模态目录 | 文件格式 | 数据类型 | 说明 |
|---|---|---|---|
| `rgb/` | PNG | `uint8` | 无损保存彩色帧 |
| `depth_raw/` | PNG | `uint16` | 原始深度，不做 8 bit 可视化转换 |
| `depth_aligned/` | PNG | `uint16` | 对齐 RGB 的深度，仍保留原始深度数值 |
| `depth_raw_npy/` | NPY | `uint16` | 与 raw depth PNG 逐像素一致，使用清单中的深度比例换算毫米 |
| `depth_aligned_npy/` | NPY | `uint16` | 与 aligned depth PNG 逐像素一致，使用清单中的深度比例换算毫米 |
| `ir_left/` | PNG | `uint8` 或 `uint16` | 左 IR |
| `ir_right/` | PNG | `uint8` 或 `uint16` | 右 IR |

所有文件写入后都会进行非空、可解码、shape 和 dtype 回读校验，再记录大小与 SHA-256。

### 5.3 实际目录

默认 `storage.output_dir=data` 时，协议数据根为：

```text
data/
  realanthro_rgbd_v1/
    collections/
      default_collection/
        .protocol_store.lock
        manifests/
          S0001.jsonl
        subjects/
          S0001/
            .staging/
              <attempt_id>/
            meta/
              subject_state.json
              protocol_snapshot.json
              subject_completion_report.json
              reviews/
                <attempt_id>/
                  <review_id>.json
              anthropometry/
                anthropometry_0001.json
            cameras/
              C336L/
                conditions/
                  <condition_id>/
                    attempts/
                      <attempt_id>/
                        rgb/
                        depth_raw/
                        depth_aligned/
                        ir_left/
                        ir_right/
                        capture.json
                        qc.json
                        commit.json
              CD435I/
                conditions/
                  <condition_id>/
                    attempts/
                      <attempt_id>/
                        ...
```

相机目录必须直接使用协议代码 `C336L` 或 `CD435I`。文件基础名为：

```text
<subject_id>_<condition_id>_F01.png
...
<subject_id>_<condition_id>_F05.png
```

模态由父目录表达，同一帧各模态共享相同基础名。前端不得自行拼接目录或文件名。

### 5.4 CaptureAttempt

`status` 表示存储事务状态，`quality_status` 表示 QC 结果，两者不能混用：

```json
{
  "attempt_id": "A<UTC timestamp>_<random>",
  "condition_id": "C336L_D2500_V000_LSTD_P1_CF_R01",
  "status": "COMMITTED",
  "quality_status": "WARN",
  "review_status": "PENDING",
  "anchor_frame": "F03",
  "prior_accepted_attempt_id": null,
  "retake_reason": null,
  "target_attempt_id": null,
  "invalidate_prior": false,
  "files": [],
  "frames": []
}
```

存储事务状态可能包括 `PENDING`、`COMMITTED`、`ABORTED` 和 `WRITE_FAILED`；QC 结果是 `PASS`、`WARN` 或 `FAIL`。

## 6. 只追加、原子提交与恢复

### 6.1 原子提交

当前提交顺序：

1. `begin_capture_attempt` 生成全局唯一 attempt ID，并把状态写为 `PENDING`。
2. 在受试者 `.staging/<attempt_id>/` 中写入所有模态。
3. 每个 PNG 完成编码、写盘、`fsync` 和解码回读校验。
4. 写入 `capture.json` 和 `qc.json`。
5. 最后写入 `commit.json`，其中包含 attempt 身份、QC 状态和文件清单。
6. 使用同一文件系统内的原子重命名，把整个 staging attempt 移到最终 `cameras/.../attempts/<attempt_id>/`。
7. 更新受试者状态和 append-only manifest。

没有有效 `commit.json` 的 attempt 不计入完成数。写盘失败会保留审计状态和 staging 证据，不静默删除。

### 6.2 协议快照

创建受试者时立即写入 `meta/protocol_snapshot.json`。快照包含：

- profile 和全部有序 condition；
- 每个 condition 的条件 ID、必需模态、完整 QC policy 内容、必需检查数量、策略规范化 SHA-256；
- M01–M23 的完整定义、字段、单位、必填性、有效范围、复测阈值和设备要求；
- 五帧、`F03` 和 WARN 人工复核策略；
- 协议版本、采集策略版本和内容 SHA-256。

旧受试者恢复时优先使用自己的协议快照，不依赖之后可能变化的当前注册表。

### 6.3 独占与并发

- `.protocol_store.lock` 使用操作系统级非阻塞独占锁；同一数据集根已有实例时，第二个实例拒绝启动。
- 每个受试者使用进程内 `RLock` 保护状态和存储事务。
- WebSocket 服务使用 `capture_lock` 和 `camera_lock`，避免并发 burst、采集中切换相机和检查后设备被替换。
- 一次性 `confirmation_nonce` 防止现场确认跨 attempt 复用。

### 6.4 崩溃恢复

程序启动时自动执行快速恢复；完整审计可重新计算已提交文件哈希。恢复能够：

- 把没有持久提交的 `PENDING` attempt 标记为 `ABORTED`；
- 校验完整 staging 树并原子提升为最终 commit；
- 导入已经原子移动但尚未来得及写回状态的 final attempt；
- 在新 PASS 补采恢复后重建 supersession，确保新的 accepted attempt 正确取代旧 accepted attempt；
- 把损坏或不完整的记录标记为 `WRITE_FAILED`；
- 重放已落盘的人工复核记录；
- 恢复只追加的人体测量 revision；
- 重放已经耐久写入 manifest 的 `SUBJECT_COMPLETED`，并在状态尚未协调时用完成事件栅栏阻止所有后续写入；
- 重建 completion report；
- 隔离单个无法读取的受试者并报告其错误，不让它导致全部受试者列表不可用；
- 保留恢复动作和错误审计，不通过删除证据“修复”状态。

### 6.5 耐久提交后的状态协调

采集目录、复核记录或人体测量 revision 一旦完成耐久写入，后续状态文件或 manifest 回写失败时，程序不会把已经存在的证据误报为普通失败，也不会自动再采一份：

- 首先在同一进程内尝试协调，成功则返回 `bookkeeping_status=RECOVERED`；
- 无法立即协调时返回 `bookkeeping_status=PENDING_RECONCILE`；服务端和前端把该受试者标记为“需要协调”，禁止继续采集、复核、人体测量和完成操作；
- 完成事件已经耐久提交、仅 completion report 写入失败时返回 `bookkeeping_status=REPORT_PENDING_REBUILD`；受试者仍保持完成态，报告由读取/恢复路径重建；
- 持续异常必须先排除磁盘或权限问题并重新启动执行恢复，不能靠操作员重复点击绕过。

## 7. QC 与实名人工复核

### 7.1 客观硬检查

当前硬检查覆盖：

- burst 必须恰好五帧；
- RGB、raw depth、aligned depth、左 IR、右 IR 必须齐全；
- 图像 dtype、维度、通道和 burst 内 shape 必须一致；
- aligned depth 的尺寸必须与 RGB 一致；
- 彩色/深度内参、raw depth 到 color 外参和 depth scale 必须完整有效；
- 五帧标定快照必须一致；
- 各流设备时间戳和流帧号必须存在；
- 时间戳偏差、主帧号和各流帧号递增、burst 设备时间间隔必须通过硬阈值；
- raw/aligned depth 有效率低于硬下限时失败；
- 相机代码、设备序列号、condition 顺序、确认 nonce 和写盘事务必须匹配。

出现任一硬失败时，`quality_status=FAIL`，该 attempt 保留但不能成为 accepted attempt。

### 7.2 Pilot 启发式 WARN

尚未由真实受试者 Pilot 冻结的指标包括：

- RGB 亮度；
- Laplacian 模糊度；
- raw/aligned depth 首选有效率；
- 标称距离窗口占比；
- 距离窗口最大区域几何；
- burst 首选时间间隔；
- 人体、全身、朝向、姿态、服装和遮挡的人工判断。

当前没有经 Pilot 冻结的人体分割或关键点模型，因此 `HUMAN_CONTENT_MANUAL_REVIEW` 会产生 WARN。只要存在 WARN 且没有硬 FAIL，condition 状态进入 `REVIEW_REQUIRED`。

### 7.3 复核闭环

人工复核规则已经实现：

1. WARN attempt 已经先完成持久提交。
2. 前端按 `subject_id + condition_id + attempt_id` 请求复核证据。
3. 服务端先校验 attempt 的受试者/condition/attempt 路径归属，再校验 `capture.json`、`qc.json`、`commit.json`、文件清单、大小和 SHA-256；随后从 committed attempt 读取 `F03` RGB 与 aligned depth 生成预览，并标记来源为 `verified_committed_files`。
4. 只有两张落盘证据均成功加载时，前端才允许 `ACCEPT`；内存中的即时预览不能单独作为接受依据。
5. 复核人来自受试者的 `operator_id`，必须填写复核原因。
6. `ACCEPT` 把 WARN attempt 标记为 `review_status=ACCEPTED`，condition 才进入 `CAPTURED`。
7. `REJECT` 写入实名审计记录并让 condition 进入 `NEEDS_RETAKE`。
8. 一个 WARN attempt 只能复核一次；待复核期间禁止直接反复补采。
9. 证据响应包含绑定 attempt、sidecar 和 `F03` 文件内容的 `evidence_sha256`，并签发 10 分钟有效的一次性证据 token。
10. `ACCEPT` 时服务端重新读取并校验落盘证据，只有摘要仍与 token 绑定值一致才写入复核；成功后 token 立即消费。
11. FAIL 不得通过 WARN 人工复核接口改成接受。

复核记录独立写入 `meta/reviews/<attempt_id>/<review_id>.json`，包含 reviewer、原因、策略、时间、内容哈希和文件哈希；启动恢复可重放该决定。

## 8. 补采与旧数据处理

普通 FAIL 或人工驳回会创建新的 attempt，失败记录始终保留。

对已经 `CAPTURED` 的条件再次采集时，前后端共同强制以下字段：

```json
{
  "retake_reason": "发现原 F03 标签或遮挡问题",
  "target_attempt_id": "<current accepted_attempt_id>",
  "invalidate_prior": true
}
```

`invalidate_prior` 必须明确二选一：

- `true`：旧 accepted attempt 已确认无效，立即标记 `INVALIDATED`；
- `false`：只追加额外复采，旧 accepted attempt 暂时继续有效。

新 attempt 永远写入新的目录。若选择保留旧数据且新 attempt 失败，旧 accepted attempt 仍然有效；若新 attempt 通过，则记录 supersession 关系。程序不会无理由、无目标 attempt 地一键替换旧数据。

## 9. 人体测量实施状态

### 9.1 已实现的软件规则

- M01、M03、M06、M09、M12 必填，且每个定义中的每个 `field_name` 都必须完成；
- 其余 M02、M04–M05、M07–M08、M10–M11、M13–M23 均可整项为空，整项为空时前端不发送该项目；
- 选填项目一旦填写，仍必须满足该项目全部字段和复测规则；
- 每个字段至少填写 `m1`、`m2`；
- 身高、宽度和长度类前两次差值超过 0.5 cm 时强制 `m3`；
- 围度类前两次差值超过 1.0 cm 时强制 `m3`；
- M02 没有强制第三测阈值，前两次差值超过 0.5 kg 只产生 WARN；
- 三次读数取最接近两次的均值；
- 原始读数、最终值、来源读数序号、操作员和记录时间全部保存；
- 人体测量使用 append-only revision 和 SHA-256，不覆盖旧 revision。

### 9.2 设备门禁

默认工作台保存五项必填测量时无需提交操作员或器材信息。仅启用严格器材审计策略的兼容部署需要额外提交：

```json
{
  "stadiometer_id": "HEIGHT01",
  "scale_id": "SCALE01",
  "tape_id": "TAPE01",
  "anthropometer_id": "ANTHRO01",
  "equipment_check_confirmed": true
}
```

严格器材审计策略下，四个工具编号都必须是有效的 1–64 位标识，并确认已经完成零点或校准状态检查；默认双机工作台不启用这项附加门禁。

## 10. 当前 WebSocket 契约

协议命令使用当前已实现的消息名，不使用计划阶段的抽象命名。

| 客户端消息 | 关键字段 | 服务端结果 |
|---|---|---|
| `get_protocol_catalog` | 无 | `protocol_catalog` |
| `get_protocol_subjects` | 无 | `protocol_subject_list` |
| `create_protocol_subject` | `subject_id`、`profile_id`、`metadata` | 完整 subject state |
| `select_protocol_subject` | `subject_id` | `protocol_subject_state` |
| `get_camera_status` | 无 | `camera_status` |
| `connect_camera` | `device_id` | `camera_status` |
| `capture_protocol_condition` | `subject_id`、`condition_id`、`confirmations`，补采时增加原因字段 | `protocol_capture_result`，随后更新 state |
| `get_protocol_review_preview` | `subject_id`、`condition_id`、`attempt_id` | `protocol_review_preview` |
| `protocol_review_capture` | `subject_id`、`condition_id`、`attempt_id`、`decision`、`reason` | `protocol_review_result` |
| `save_anthropometry` | `subject_id`、`records`、`equipment` | `anthropometry_result` |
| `complete_protocol_subject` | `subject_id` | `protocol_completion_result` |

采集确认结构：

```json
{
  "distance_marker": true,
  "pose_view_clothing": true,
  "full_body_visible": true,
  "repositioned": true,
  "nonce": "<confirmation_nonce>"
}
```

`repositioned` 只在 `repeat_id > 1` 时强制。所有写命令显式携带 `subject_id`；服务端不允许前端无条件修改完成状态。

## 11. 完成与完整性门禁

受试者完成必须同时满足：

1. 当前协议快照中的每个 condition 都有一个有效 accepted attempt；
2. accepted attempt 的 `quality_status` 为 PASS，或为已经实名 `ACCEPT` 的 WARN；
3. 每个 accepted attempt 都是 `COMMITTED`，有五帧、所需全部模态和有效 `commit.json`；
4. 每个文件的路径、大小和 SHA-256 重新验证通过；
5. accepted WARN 的复核文件存在且哈希一致；
6. M01、M03、M06、M09、M12 全部字段完成，人体测量 revision 存在且哈希一致；
7. completion report 成功生成。

完成后再次发现文件缺失或哈希错误时，报告状态为 `CORRUPTED`，不能继续显示为绿色成功。完成后的受试者禁止继续写入 attempt、复核或人体测量；即使完成状态文件回写失败，只要耐久 `SUBJECT_COMPLETED` 事件存在，完成事件栅栏也会立即阻止后续写入并由恢复流程重放完成态。

SMPL-X、发布级别、访问策略、数据集划分和封存均不参与当前完成门禁。

## 12. 已完成的验证

### 12.1 自动测试覆盖

当前验证结果：

- Python 后端与协议测试：`113 passed`；
- 前端测试：`15 passed`；
- 前端 production build：通过；
- Python 语法编译与补丁空白检查：通过。

开发/验收环境应使用 `pip install -r requirements-dev.txt` 安装包含 `pytest` 的测试依赖，再执行 `python -m pytest -q`；正式采集运行依赖仍由 `requirements.txt` 管理。

测试覆盖：

- 3/27/31/36 profile 数量、唯一性和严格子集关系；
- condition、subject、frame 和 modality 命名；
- M01–M23 必填/选填、双侧字段、第三测和最近两次均值；
- Gemini 与 RealSense 适配器统一 FrameBundle；
- 五帧、双 IR、无损 PNG、逐文件哈希和 commit；
- 协议快照独立恢复；
- 数据集独占锁；
- pending、staging、final commit、人工复核和人体测量的崩溃恢复；
- Windows 文件/目录原子替换短时锁重试、重试耗尽后 staging 保留；
- 文件已耐久提交但状态回写失败时的同进程协调、显式阻断和启动恢复；
- WARN 接受/驳回、补采、旧数据作废和 supersession；
- 经哈希验证的 `F03` 证据 token、伪造摘要拒绝和重新加载后接受；
- completion gate、耐久完成事件重放、完成后写锁、报告重建和完成后数据损坏检测；
- WebSocket 条件顺序、五帧采集、F03 复核预览和人体测量往返；
- 前端必填测量、复核状态和受控补采数据结构。

### 12.2 真实设备验证

| 项目 | 状态 | 备注 |
|---|---|---|
| Gemini `CPCD7530002G` 枚举与连接 | 已通过 | USB 真机 |
| Gemini 五帧 RGB/raw depth/aligned depth/双 IR | 已通过 | 100/100 次 burst，500组同步帧 |
| Gemini 10 分钟连续预览 | 已通过 | 601.066秒，17,947组，0超时/硬失败 |
| Gemini 内参、外参、depth scale | 已通过 smoke | 已进入 FrameBundle/QC |
| D435i SDK 导入 | 已通过 | Python 3.10.20 / `pyrealsense2 2.54.2.5684` |
| D435i `243722074968` 真机枚举 | 已通过 | 固件 5.15.1.55，USB 3.2 |
| D435i IMU 单流 | 已通过 | 证明设备与 SDK 基础通信存在 |
| D435i RGB/Depth 首帧 | 已通过 | 官方 Viewer 与最终 Python 主环境均出帧 |
| D435i 五帧与标定 | 已通过 | 100/100 burst，500组五模态帧，硬失败为0 |
| D435i 10分钟连续取流 | 已通过 | 602.813秒，17,981组，29.828 fps，0超时/硬失败/时钟倒退；7个主机采样跨帧WARN（约0.039%） |
| D435i DEV_ONLY 写入链 | 已通过 | 25图像文件、3 sidecar、F03 双证据哈希、REJECT 复核、严格审计错误0 |
| Gemini DEV_ONLY 写入链 | 已通过 | 25图像文件、3 sidecar、F03 双证据哈希、REJECT 复核、严格审计错误0 |
| Windows 中断恢复演练 | 已通过 | 完整 staging 提升和 durable review sidecar 重放各1次，均恢复为 COMMITTED/REJECTED，审计错误0 |

### 12.3 机器可读报告与复验入口

关键报告：

- `reports/hardware/d435i_bursts_100_2026-08-22.json`
- `reports/hardware/d435i_stability_600s_2026-08-22.json`
- `reports/hardware/d435i_dev_only_write_chain_2026-08-22.json`
- `reports/hardware/gemini_336l_dev_only_write_chain_pass_2026-08-22.json`
- `reports/hardware/gemini_336l_dev_only_recovery_staging_2026-08-22.json`
- `reports/hardware/gemini_336l_dev_only_recovery_review_2026-08-22.json`
- `reports/hardware/windows_atomic_replace_recovery_2026-08-22.md`

复验脚本：

- `scripts/verify_protocol_camera.py`：只读 burst 验收，可原子输出完整 JSON；
- `scripts/verify_camera_stability.py`：连续取流与逐流时钟统计；
- `scripts/verify_dev_only_capture.py`：隔离写入、F03 证据、REJECT 复核与严格审计；
- `scripts/recover_dev_only_run.py`：只允许 `dev_only` 路径的中断恢复与严格审计。

## 13. 明确未完成项

以下三项是开始合格规模化采集前的明确阻塞项；100次 burst、10分钟稳定性和两台相机 DEV_ONLY 写入/恢复链已经完成，不再列为待办。

### 13.1 正式场地与真实全身入框预验收

D435i 的 SDK、Windows 元数据、首帧门禁、100次 burst、10分钟稳定性和 DEV_ONLY 写入链已经通过。Gemini 的同级验收也已通过。除非出现新的 USB 2.x 降速或取流回归，不再重复硬件耐久测试，也不得通过延长超时或取消首帧门禁掩盖问题。

完成标准：

- 在最终相机高度、俯仰角、背景和地面标线下，用真人/合规人体替身验证全身完整入框；
- Gemini 覆盖 1.5/2.0/2.5/3.0/4.0 m，D435i 覆盖 2.5/3.0 m；
- 检查头顶、双手、双脚余量和 aligned depth 覆盖，不用 DEV_ONLY 空场景替代；
- 固化相机安装照片、光心地面投影和 `BODY_CENTER` 标线位置。

### 13.2 真实受试者 Pilot 与阈值冻结

建议流程：

1. 后端级 2–3 次 `DEV_ONLY` 写入/恢复演练已经完成；在正式场地再做一次操作员 UI 演练；
2. 执行 8–12 名真实受试者 `full31_no_lux` Pilot；
3. 统计单人耗时、条件失败率、WARN 率、人工接受/驳回率、补采率、磁盘量和测量复测率；
4. 用 Pilot 分布和人工判断冻结亮度、模糊度、depth 有效率、距离区域、burst 间隔等启发式阈值；
5. 固化新的 `qc_policy_version` 和阈值依据后，才能把相关指标从 Pilot 启发式转为正式策略。

### 13.3 人体测量工具到位

必须取得并编号：

- 标准身高计；
- Anthropometer 或大型滑动卡尺；
- 无弹性人体测量软尺。

同时完成零点/校准检查、操作员 SOP 演练和一名受试者的五项必填字段复测验收。

照度计和固定灯具不是 `full31_no_lux` 的阻塞项；只有将来决定启用 `full36` 时才需要另行验收。

## 14. 下一步执行顺序

1. 让人体测量工具到位并建立工具编号、校准记录和 SOP。
2. 在最终场地完成两台相机各距离的真人/合规人体替身全身入框与标线验收。
3. 通过前端完成一次操作员主导的 `DEV_ONLY` 演练，验证相机切换、现场确认、F03 复核和补采操作体验。
4. 执行 8–12 名真实受试者 Pilot。
5. 根据 Pilot 结果冻结 QC policy，再决定是否进入规模化 `full31_no_lux` 采集。
6. 只有另行取得照度计和可复现灯具后，才讨论 `full36`。

在第 1–4 项完成前，当前程序已具备受控 Pilot 的软件/设备基础，但不应宣称已经具备规模化合格数据采集条件。

## 15. 契约防漂移清单

后续修改必须保持：

- 深度始终同时以无损 `uint16 PNG` 和原始 `uint16 NPY` 保存；
- raw depth 与 aligned depth 始终独立；
- 正式相机目录只使用 `C336L` 和 `CD435I`；
- 正式 attempt 始终位于 `cameras/<camera_code>/conditions/<condition_id>/attempts/<attempt_id>/`；
- `status=COMMITTED` 与 `quality_status=PASS/WARN/FAIL` 分开表达；
- `F03` 固定为 anchor；
- Pilot WARN 必须实名复核经哈希验证的落盘 `F03`；
- 人工接受必须使用短时一次性证据 token，并在写入决定前重新验证证据摘要；
- 已通过条件补采必须绑定 accepted attempt、填写原因并明确是否作废旧数据；
- 每名受试者保留包含完整条件级 QC policy 和测量定义的不可变协议快照；
- M01、M03、M06、M09、M12 构成人工测量完成门禁；严格审计部署可另加工具编号与设备检查确认；
- 崩溃恢复不静默删除证据，第二采集实例不能并发写同一数据集；
- `PENDING_RECONCILE` 和耐久完成事件栅栏不得被前端或接口绕过；
- SMPL-X 和发布治理继续留在当前采集范围之外。
