# 数字采集 RTL 协同

在 macOS 上用 Icarus Verilog 12.0 实跑。源码为上游 v12_0，对应提交 `4fd5291632232fbe1ba49b2c26bb6b2bf1c6c9cf`。构建放在临时目录。系统自带 Bison 2.3 无法编译 VHDL 前端，所以只构建了 Verilog 模拟所需组件。

- V5 采集窗协同覆盖 16 位回绕斜坡、交替码和 PRBS。每组的 15 个输出样本、样本编号、行号、行首行尾标志和 1 个被忽略的窗内同步均与 Python 参考一致。
- 冻结包 A 测试台报告 256 个 ADC 值及标签一致，有限停顿保序，故意过载触发溢出。
- 冻结包 B 测试台报告标签对齐、反压保序、行尾与帧首标志和溢出通过。

```bash
.venv/bin/zlscan cosim --iverilog /path/to/iverilog --vvp /path/to/vvp --out cosim_out
```

V5 使用 Icarus 自检测试台及 Python 对比驱动。覆盖单通道采集窗口行为。板级物理接口、CDC 时序和 SPICE 未执行。
