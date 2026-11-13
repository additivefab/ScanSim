# 阶段 3/4 软件验收包（受限范围）

2026-11-13，基于提交 `f10ab51`。本文件只闭合可在当前软件环境直接复跑的部分，不能替代实机、浏览器或完整阶段门槛。

## 本轮实际执行

```text
.venv/bin/python -m pytest -q --tb=short \
  tests/unit/test_chain.py tests/unit/test_observability_stack.py \
  tests/unit/test_recording_replay.py tests/unit/test_recording_capture.py \
  tests/unit/test_component_views.py tests/unit/test_quality_metrics.py \
  tests/unit/test_geometry_calibration.py tests/unit/test_epoch_calibrations.py \
  tests/unit/test_radiometric_calibration.py tests/unit/test_metric_targets.py \
  tests/unit/test_geometry_identification.py tests/unit/test_radiometric_identification.py
```

结果：148 passed，46.93 s。Ruff check 通过，313 个文件 format check 通过。

覆盖了链分块/检查点、观察无扰动、物理录制恢复与捕获 v2、重放、组件视图、质量指标、几何/逐时期标定、辐射标定、几何辨识和辐射辨识。测试输出原文保存在 `evidence_runs/2026-11-13_stage34_acceptance/output/`。

## 已有交叉证据

- `docs/reports/nominal_vs_legacy.md`：nominal 标定/重建、冻结旧包码流对照、隔离真值修改和 2³ 因子实验。
- `docs/reports/stage3_dual_frames.md`：4000×5000 与 7000×8600 双通道实际运行、完整性和内存记录。
- `docs/reports/epoch_calibrations.md`：逐时期标定集合、谱系 v3 和流式重建。
- `docs/reports/geometry_identification.md`、`docs/reports/radiometric_identification.md`：条件辨识、退化、留出及不确定性限制。

## 当前结论

软件路径的定向回归、nominal/旧包对照、标定/重建和条件辨识均有可复跑证据。阶段 3/4 总门槛仍保持未关闭，原因是：

1. 双通道大幅面端到端实测为 1277.5 s 和 1108.2 s，均超过假设的 900 s 目标。
2. 第二级工作台 W01–W11 的浏览器验收尚未执行；Node/HTTP/静态检查不等价于浏览器验收。
3. 完整标准实验、误差分解和全部物理敏感性/独立性门槛尚未汇成单一闭合报告。
4. 阶段 5–6 的实测资料、外部工具和硬件身份仍缺失。

本包证明软件范围内的当前行为可复跑，不把 148 项测试或 CI 绿灯升级为完整阶段 3/4 完成。
