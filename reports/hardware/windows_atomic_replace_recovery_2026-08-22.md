# Windows 原子替换故障与恢复记录（2026-08-22）

## 结论

Gemini 336L 的 `DEV_ONLY` 真机演练命中了两种 Windows 短时文件锁：

1. 25 个图像与 `commit.json` 已写完后，`.staging/<attempt_id>` 提升到最终 attempt 目录时出现 `WinError 5`；
2. F03 复核 sidecar 已耐久写入后，更新 `manifests/S9001.jsonl` 的临时文件替换出现 `WinError 5`。

两次均未删除或覆盖证据。修复后，完整 staging 和 durable review sidecar 都通过逐文件哈希、sidecar、协议快照与 manifest 链审计，并分别恢复为 `COMMITTED` / `REJECTED`；最终审计错误均为 0。

## 实施修复

- 文件和 attempt 目录的 `os.replace` 共用有限 Windows 重试；
- 只重试锁占用错误 `5/32/33`，等待序列为 `0.1/0.25/0.5/1.0/2.0 s`；
- 路径冲突、源缺失和其他错误立即失败；
- 不使用复制、删除或覆盖 final 的降级路径；
- 重试耗尽后保留 staging，并沿用 `WRITE_FAILED` / 启动恢复账本；
- `DEV_ONLY` 工具遇到 `PENDING_RECONCILE` 时执行严格恢复，不再继续使用空状态；
- 新增 `scripts/recover_dev_only_run.py`，只允许项目内含 `dev_only` 的隔离路径。

## 验证

- 自动测试：后端/协议 `72 passed`，包含目录锁耗尽保留 staging、文件锁重试成功；
- 正常 Gemini 写入链：25 文件、3 sidecar、F03 RGB/对齐深度证据、REJECT 复核，审计错误 0；
- staging 恢复报告：`reports/hardware/gemini_336l_dev_only_recovery_staging_2026-08-22.json`；
- review sidecar 重放报告：`reports/hardware/gemini_336l_dev_only_recovery_review_2026-08-22.json`；
- 最终正常链报告：`reports/hardware/gemini_336l_dev_only_write_chain_pass_2026-08-22.json`。

本结论仅证明存储与恢复链；测试画面均按 `REJECT` 处理，不能计入正式人体数据集。
