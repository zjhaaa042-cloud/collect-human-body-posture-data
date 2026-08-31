# 当前项目架构

## 主数据流

```text
React 工作台
  └─ collector transport / reducer / command façade
       └─ WebSocket 认证与消息路由
            └─ DualWorkflowService（活动任务与状态转换）
                 ├─ Gemini / D435i CameraAdapter
                 ├─ DualCameraCaptureCoordinator
                 └─ DualSessionStore
                      └─ 原子 staging → commit → final → 状态账本
```

默认产品流程是 `dual-rgbd-v2.2` 双机八角度采集。RealAnthro 和 Legacy v3 继续保留兼容入口，但新双机逻辑不得再放入旧协议巨型模块。

## 模块边界

- `backend/application/`：应用级编排，绑定活动受试者、相机互斥锁和存储事务，不处理 WebSocket 序列化。
- `backend/core/`：SDK 无关的相机适配、近同步采集和算法逻辑。旧 `ProtocolStore` 暂保留兼容导入路径。
- `backend/storage/`：原子 I/O、数据集租约、路径安全、PLY 与文件完整性原语。
- `backend/server/`：认证、HTTP 健康检查、WebSocket 消息路由和前端消息封装。
- `frontend/src/collector/`：连接地址、退避策略、双机状态 reducer 与本地恢复记录。
- `frontend/src/components/`：采集工作台展示与交互，不直接解析底层 WebSocket 包。

## 存储事务

正式双机采集按以下顺序提交：

1. 在受试者 `.staging/<attempt_id>` 写入 PNG、NPY、PLY，并逐文件计算 SHA-256。
2. 写入包含逐帧时间、标定、深度比例和文件清单的 `capture_manifest.json`。
3. 最后写入 `commit.json`，再次绑定清单哈希和完整文件清单。
4. 校验 PNG/NPY 逐像素一致、所有路径未越界、文件大小与哈希一致。
5. 原子提升 staging 目录，再更新 `session_manifest.json`。

若第 5 步失败，final attempt 会在重新打开任务时恢复到账本；不完整 staging 会被保留，并通过 `reconciliation_required` 锁定任务，禁止继续写入。

## 深度格式

- `depth_raw_uint16/*.png` 与 `depth_raw_npy/*.npy`：设备原始深度坐标系。
- `depth_aligned_uint16/*.png` 与 `depth_aligned_npy/*.npy`：对齐到 RGB 的深度坐标系。
- NPY 固定为二维 C-order `uint16`，使用 `np.load(path, allow_pickle=False)`。
- NPY/PNG 保存量化值；毫米值为 `array * depth_scale_mm_per_unit`。
- 伪彩 PNG 只用于操作员查看，不能作为训练深度输入。

## RGB 色彩与 RGB-D 对齐契约

- 相机适配器必须输出 RGB 顺序的 `uint8 H×W×3` 数组，并显式声明 `rgb_color_order=RGB`、`rgb_transfer=sRGB`；未知三通道格式不允许猜测。
- OpenCV 编码前才执行 RGB→BGR；无损 PNG 写入后会立即回读并逐像素校验，预览 JPEG 也检查编码成功和颜色通道语义。
- `depth_aligned` 必须与 RGB 宽高相同，且 aligned intrinsics 与 color intrinsics 一致。
- `depth_raw` 保持原始深度坐标系，通过 raw-to-color 外参与 RGB 关联；它不要求、也不应冒充逐像素 RGB 对齐。
- RGB、raw depth、aligned depth 必须来自同一帧集，raw/aligned 帧号相同，三条流的时间差不得超过 75 ms。契约失败会在写盘前中止 burst。

## 兼容策略

- 现有 WebSocket 消息名和主要字段保持不变；双机状态只新增可选完整性与恢复字段。
- 历史 attempt 不回填 NPY，也不重写已有哈希清单。
- 新代码可以读取无 `commit.json`、无 NPY 的旧双机 final attempt；只有带新存储特性的 attempt 才强制校验 NPY。
- Legacy v3 继续保留原有压缩 NPZ，不重复新增 NPY。
