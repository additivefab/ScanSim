# 阶段 7 软件路径验收（受限范围）

2026-11-14，当前源码 `592e611`。

本轮实际运行：

```text
pytest tests/unit/test_sweep_compare.py tests/unit/test_cli.py \
       tests/unit/test_wheel_audit.py tests/unit/test_evidence_records.py
```

结果：47 passed，5.73 s；Ruff check 通过。

已确认：

- `sweep` 对一维/二维参数建立笛卡尔点，每个点独立写完整 `run.json`，并输出汇总 CSV/SVG。
- 参数类型、路径、有限性和已有输出目录保护有回归。
- `compare` 保留输入清单，区分相同输出、缺失文件和未比较项。
- CI/安装审计已有当前固定源码的六目标实工件。
- 阶段 3/4 统一软件门禁已通过，包含两个全幅性能工件：4000×5000 为 861.80 s，7000×8600 为 771.82 s；结果见 `evidence_runs/2026-11-13_stage34_software_gate/acceptance.json`。
- `legacy_cross_compare.md` 已保留光功率、PMT 阳极电流、对数级电压、ADC 输入和 ADC 码的逐环节静态残差，以及动态阶跃 RMS/边沿差异；这些是软件/冻结旧包对照，不是实测设备残差。

仍未关闭：

- S7-T03 的实测对齐和实测逐环节数值残差；软件对照已有，完整实测报告模板仍缺。
- S7-T01 三套性能基准的交互绘制与冷/热缓存指标；两项大幅面运行性能门槛已通过，但浏览器交互指标仍缺。
- 完整 CPython/pip/外部工具锁、许可和硬件/浏览器验收。
- S7-T04 三维/桌面封装仍是可选范围，未引入依赖。

本文件只证明软件命令和回归路径，不把 47 项测试升级为阶段 7 完成。
