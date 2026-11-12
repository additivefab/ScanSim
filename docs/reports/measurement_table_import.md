# 标量测量模板表导入（S6-T01）

2026-11-13。仅软件适配；本轮数据均为合成测试，没有新实机测量。

```bash
zlscan ident --table measurements.csv --metadata metadata.json --out imported_table
```

复制 `docs/examples/measurement_table.csv` 与 `docs/examples/table_metadata.json`
后填写；模板中数值和设备条件为空，不是机器规格。`--table`、`--csv`、`--raw`
三者互斥。CSV 为 UTF-8 单表头，列名必须恰好包含模板中的全部字段；次序可调整。
最大 32 MiB / 10 万行，不用于替代长波形流式文件。源 CSV 与侧车只读，
报告记录二者 SHA256；输出目录不能覆盖。

每行一项标量观察，以 measurement_id 唯一标识，reference_id 关联参考目标。
允许 quantity/unit：voltage(V/mV/uV)、current(A/mA/uA)、time(s/ms/us/ns)、
frequency(Hz/kHz/MHz)、density(OD)、transmittance(1)、adc_code(code)。
只做单位转换，不把密度转透射率或码值反演，也不计算均值或合并观测。
ADC 原始码不应先平均再反对数；本表不能授权这类处理。

value、uncertainty 与所有未取得元数据留空即 null，不补零。uncertainty
与 value 使用同一 unit；uncertainty_kind 显式为 standard、expanded 或
absolute_bound，uncertainty_source 记录依据。expanded 还需 coverage_factor；
缺项可查看且列入 missing_metadata，不将标准差自动当成置信区间或绝对界限，
不将扩展不确定度除以覆盖因子再冒充已知分布。factor 与类型不匹配会标记。
传输到 SI 后继续保留声明、单位与覆盖因子；超出 [0,1] 的透射率标记并保留，
不裁剪。负不确定度、非有限数、单位维度不符、重复身份及损坏表格拒绝。

侧车记录设备、测点、探头倍率、耦合、带宽限制、采样间隔、触发和时间原点；
未知保留 null，不猜测。每行 session/epoch/acquisition_id 标识采集来源。
同一侧车条件只适用于同一套仪器设置；条件不同请拆为不同表，不能混标。
本表为标量，synchronization 固定 not_applicable_scalar_table，
不会因 simultaneous=true 生成逐点同步或时间轴。

侧车 source_files 显式列出 `{"path":"capture.zlraw","sha256":"64位小写摘要"}`。
路径相对侧车目录，源文件分块核验摘要；哈希不符或打不开时导入失败。
行 source_sha256 只在与实际核验来源匹配时 source_verified=true；
缺来源文件或没有登记则保留未核验。摘要相符只证明字节身份，不能证明实机来源。

partition 为 fit/validation/unassigned；空也保留未知。fit 与 validation
复用同一 source_sha256，或同一 (session,epoch,acquisition_id)，
会输出 leakage_detected 和具体记录；不同文件名不能规避采集身份重复。
目前未声明子文件区段，因此同源文件的任何复用均保守视为泄漏。
缺来源核验、采集身份、未分用途或没有两类数据时为 not_assessed；
完整声明中未发现复用为 no_declared_reuse_detected，仍不是统计独立证明。
不同文件也可能有公共噪声、漂移或同一仪器偏差，不能由哈希排除。

始终 independence_established=false、hard_acceptance_eligible=false、
parameter_upgrades=[]。当前不直接调用 calibrate 或拟合，不自动将
standard/expanded 转成单调表所需界限。后续必须明确参考量、可辨识参数、
观测模型、拟合分区和验证准则，再接入辨识；不能把表导入算作阶段6实测验收。

现已具备波形 CSV、当前 ZLRAW 草案及标量模板表三类导入接口；
本机 FPGA 真实协议、仪器厂商专有导出和实测独立验证仍未取得/未完成。

软件验证：全量 1211 通过、2 跳过（76.62 s，Icarus 不可用）；Ruff/格式通过。三类导入/CLI 定向 51 项通过（2.22 s）；未知模板实际 CLI 导入保留 null、未评估分区。所有测试均合成输入，不是实机验收。
