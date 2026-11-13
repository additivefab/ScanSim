# 阶段 7 软件路径验收（受限范围）

2026-11-13，当前源码 `1ead19f`。

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

仍未关闭：

- S7-T03 的实测对齐、逐环节数值残差和完整报告模板。
- S7-T01 三套性能基准的优化前后成对记录；当前大幅面运行仍超过 900 秒目标。
- 完整 CPython/pip/外部工具锁、许可和硬件/浏览器验收。
- S7-T04 三维/桌面封装仍是可选范围，未引入依赖。

本文件只证明软件命令和回归路径，不把 47 项测试升级为阶段 7 完成。
