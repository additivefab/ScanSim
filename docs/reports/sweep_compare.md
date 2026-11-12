# 参数扫描与运行比较

`sweep` 的所有输入均为研究假设；仅支持 `demo`、`nominal` 两个研究预设。
数值字段采用点分路径（如 `source.power_w`），允许一或二维非空列表；整数字段保持整数。
每个点从独立预设配置运行完整物理链，保存原始码、观测元数据、重建与 `run.json`。
已有输出目录拒绝覆盖。失败退出非零；未运行完时不能将目录存在视为完整扫描。

```bash
zlscan sweep --config configs/sweep_demo.json --out /tmp/zlscan-sweep-example
zlscan compare --first /tmp/zlscan-sweep-example/point-0000 \
  --second /tmp/zlscan-sweep-example/point-0001 --out /tmp/zlscan-comparison.json
```

汇总 `sweep.json` 含各点参数、路径与运行清单；`summary.csv` 和 `curves.svg` 展示 ADC 饱和、
前端限幅、PMT 过限、丢块计数。所有计数来自对应运行，不代表密度误差或实机测量。
当前顺序执行；并行执行并非必需。

`compare` 报告运行清单字段差异，并分别对原始码流、完整性、传输丢失、重建文件和数字迹线
计算 SHA-256。缺失文件的 equal 为 null，状态为 missing_artifact。
这是文件字节比较；不能用不同文件哈希推断图像误差，不能用相同哈希证明物理模型正确。
输出必须在两个输入运行之外。S7-T03 的实测对齐、逐环节数值残差与报告模板仍待完成。
