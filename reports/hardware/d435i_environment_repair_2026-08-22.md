# D435i 环境修复与复验记录（2026-08-22）

## 结论

D435i 硬件正常，项目主环境已经修复为可用组合：

- 设备：Intel RealSense D435i，序列号 `243722074968`
- 固件：`5.15.1.55`
- USB：`3.2`
- Python：`3.10.20`
- pyrealsense2：`2.54.2.5684`
- Windows UVC 帧元数据：已启用

最终结果已扩展为 100/100 次五模态 burst 硬通过、10 分钟连续五模态取流通过，并完成一次隔离 `DEV_ONLY` 写入/F03 复核闭环。

## 根因

故障不是单一因素：

1. Windows 当前 D435i 设备实例缺少 `MetadataBufferSizeInKB0/1/2` 注册项。
2. 原项目环境为 Python 3.13 / `pyrealsense2 2.58.3.10794`，与固件 `5.15.1.55` 的推荐 SDK 代际不一致。
3. Codex 工作区沙箱允许枚举设备和启动 pipeline，但阻止 USB 视频帧进入进程；同一条彩色单流命令在沙箱外立即成功。
4. 适配器曾把 RealSense 复合 frameset 的帧号/时间戳当作主时钟；复合对象可能切换所代表的嵌入流，造成伪非单调。

## 已执行修复

- 下载并验证官方 Viewer `2.54.2.5684`：
  - 路径：`tools/realsense/v2.54.2/Intel.RealSense.Viewer.exe`
  - SHA-256：`8E9436E63969C0877CA659C684E1F15DD6C4CB0E46EB83D1CC31FF67A90CF3A9`
  - Authenticode：Intel Corporation，有效
- 使用官方 `realsense_metadata_win10.ps1` 写入当前设备的 Windows 元数据注册项；MI_00 为 `Metadata0/1/2=5`，MI_03 为 `Metadata0=5`。
- 设备硬复位并重新枚举。
- `requirements.txt` 锁定 `pyrealsense2==2.54.2.5684`。
- 标准 `.venv` 重建为 Python 3.10；原 Python 3.13 环境保留在 `.venv-py313-backup`。
- `install_deps.bat` 与 `go.bat` 增加 Python 3.10/3.11 门禁。
- RealSense 主时钟改为彩色流，raw depth 次之，复合 frameset 仅作兜底；所有分流原始时钟仍进入 QC。
- 连续稳定性工具增加逐流帧号、逐流时间戳和异常样本统计。

## 验证结果

### 官方 Viewer

- RGB 单流：通过
- Depth 单流：通过
- RGB + Depth 并行：通过
- 未执行固件更新

### 最终项目主环境

- Python 自动测试：`72 passed`
- D435i 五模态 burst：`100/100` 硬通过，共 500 组同步帧
- 五种必需模态：RGB、raw depth、aligned depth、左 IR、右 IR 均存在
- 10 分钟连续取流：
  - 实际时长：`602.813 s`
  - 完整帧集：`17,981`
  - 平均帧率：`29.828 fps`
  - 超时：`0`
  - 缺失/形状/标定失败：`0`
  - 主时钟倒退：`0`
  - 各流帧号/时间戳倒退：`0`
  - 主机采样跨过设备帧：`7`（约 `0.039%`，WARN，不是设备超时或缺模态）
  - `hardware_pass=true`
- `DEV_ONLY` 写入链：
  - 25 个图像文件（5帧 × 5模态）
  - `capture.json`、`qc.json`、`commit.json` 完整
  - F03 RGB/对齐深度经逐文件 SHA-256 验证后读取
  - 空场景按策略 `REJECT`，状态为 `NEEDS_RETAKE`
  - 严格恢复审计错误：`0`

机器可读报告：

- `reports/hardware/d435i_bursts_100_2026-08-22.json`
- `reports/hardware/d435i_stability_600s_2026-08-22.json`
- `reports/hardware/d435i_dev_only_write_chain_2026-08-22.json`

原始失败证据继续保留：`reports/hardware/d435i_preflight_failure_2026-08-22.json`。

## 尚未完成

- 2.5 m / 3.0 m 真实全身入框验收
- 8–12 名真实受试者 Pilot 与 QC 阈值冻结
