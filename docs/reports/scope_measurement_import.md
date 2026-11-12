# 示波器 CSV 导入（S6-T01 部分交付）

2026-11-13。仅软件导入合同；没有取得或导入实机波形，不产生实测参数。

```bash
zlscan ident --csv capture.csv --metadata docs/examples/scope_metadata.json --out imported_scope
```

先复制模板到自己的文件，按实际导出修改 time_column/time_unit、channels 的 column/unit。
其余未知项保留 null。CSV 使用 UTF-8，单表头，所有数据行长度一致，时间列严格递增。
允许时间单位 s/ms/us/ns，幅值 V/mV/uV/A/mA/uA；不支持的单位拒绝，不猜采样率。
单次查看导入限制 64 MiB、100 万行，不能替代未知长度 FPGA 流式读取。

probe_ratio 仅登记；probe_compensated 表示导出幅值已经应用探头倍率。false 时保留
导出量并加问题标记，不静默重复乘倍率。带宽 0 表示明确关闭限制，null 表示未知。
采样间隔与 time_tolerance_s 均明确时才检查实际时间差；容差是导入声明，
不代表仪器时基不确定度。time_origin 只保存说明，不搬移时间轴。

run.json 保存原 CSV 和元数据 SHA256、声明和 SI 采样，不插值、不摘要、不自动对齐。
缺元数据列出 missing_metadata；时间差矛盾列入 issues。即便同一 CSV 有两列数据，
也必须完整元数据、同次声明、相同非空 acquisition_id 且无问题才能标为
declared_same_acquisition。该状态仅代表声明，不是独立同步核验。不同采集或明确
非同次标为 non_simultaneous，缺项标为 unestablished。不提供跨文件拼接为同步功能。

source_kind 必须由数据来源明确声明；默认 unknown。synthetic 测试不能升级为
实测。所有导入 hard_acceptance_eligible=false、parameter_upgrades=[]；完整元数据
也不能代替观测模型、独立验证或可辨识性。现有 calibrate 不自动消费该文件。
FPGA 协议适配、测量模板表、辨识和工作台叠加仍待实现，S6-T01/阶段6未完成。

本轮软件验证：全量 1176 通过、2 跳过（78.33 s，Icarus 不可用）；Ruff 与格式检查通过。合成导入/CLI 定向 18 项通过，不代表实机验收。
