# ZLRAW 原始码流导入（S6-T01 部分）

2026-11-13。本轮没有取得实机数据，测试为合成文件。

```bash
zlscan ident --raw capture.zlraw --metadata metadata.json --out imported_raw
```

未知模板为 `docs/examples/raw_metadata.json`。先复制模板再填实际声明；
channel 是块掩码里的通道编号，epochs 是所含时期，不将未知通道角色猜为参考光。
`--raw` 与 `--csv` 互斥，侧车合同不同；均不执行参数辨识。仅支持 ZLRAW 0.1
草案，未冻结本机 FPGA 的实际输出协议；不尝试猜无头文件的编码、采样率或通道顺序。

导入先独占创建目录，将源逐字节复制到 raw.zlraw，再检查快照；副本 SHA256、
原始 session、各块时期/时钟域/通道/源样本身份、CRC 错误和缺样进入 run.json。
原始文件不改，副本不做二补码转换，不修改来源、不平移时期、不补样；源在复制中
变化则拒绝（留下取证副本，不发布成功报告）。失败后换新目录重试，不能覆盖旧目录。
原始路径名仅作来源线索，摘要绑定实际副本字节。

对坏 CRC、身份回退、截断等沿用原读取器的显式诊断；损坏块不进预览。码位宽、
空块及源索引溢出另加语义问题。不可读仍保存副本与报告，CLI 非零；
可读但缺块/坏载荷为 incomplete，只供查看。软件不推断未知的首尾损失，
无发现损坏时用 seen_range_checked，不能解读为整次采集完整。原读取器识别的
窗口外样本间隔继续保留，不把正常窗间间隔自动当丢包。

预览至多 256 行原始码，带各块身份，不将跨时期/跨域码展平成同步数组。
预览不是完整数据；后续重建应读取 raw.zlraw 并再次校验。块索引仍随块数增长，
本适配器不是未知长度内存有界采集器，单块载荷内存仍受原文件块大小影响。
Python JSON 保留大整数；前端消费超 2^53 身份时必须使用可精确解析的整数接口，
不能直接经过 JavaScript Number 再用于样本定位。

只有完整声明、同次采集、单时钟域、每块含全部声明通道、元数据与头一致且
完整性无问题时才可标记 declared_same_acquisition；这仍只是声明，
不是独立同步核验。不同 acquisition_id 或明确非同次标记 non_simultaneous；
其他缺项/冲突保持 unestablished。采样间隔仅保存侧车声明，不生成时间轴，
不将 nominal_hz 或相同索引当实测时基。时间对齐不确定度保持 null。

头部 origin 与 source_kind 分别保留。SIMULATED+declared_measurement（及反向冲突）
会被标记，不能升级成实测。所有导入 hard_acceptance_eligible=false、
parameter_upgrades=[]。即便头写 MEASURED 也只是输入声明，不证明实机来源。
尚缺真实协议/寄存器/接线确认、测量模板表适配、辨识和独立实测验证；
阶段6及完整 S6-T01 仍未完成。

软件验证：全量 1191 通过、2 跳过（77.85 s，Icarus 不可用）；Ruff/格式通过。CSV/ZLRAW/CLI 定向 32 项通过，2.22 s。所有输入均为合成测试，不是实际板卡性能证据。
