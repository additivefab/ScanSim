# FPGA 最小验证骨架，不是可直接上板的扫描仪固件

功能：ADC 流水延迟对应的元数据延迟；同一时钟域 FIFO；背压时的数据保持；行末/帧首标签；溢出计数。
不含：真实 ADC DDR/LVDS 接收、IDELAY/ISERDES、异步 FIFO、SOS 去毛刺与亚稳态防护、
扫描电机控制、AXI DMA、PS 软件、以太网发送或可用整板 XDC。
本次环境没有 Icarus/Vivado，RTL 未编译或执行；Python 的12项通过不能替代 RTL 仿真通过。

## 运行

Linux/WSL：`bash rtl/run_iverilog.sh`。成功时应看到 `PASS: ...` 并得到 `rtl/scanner.vcd`。
有 GTKWave 时打开 VCD，重点观察 sample_valid、delayed_valid、adc_data、delayed_tag、
fifo.out_valid、ready、out_last、overflow_sticky。
Vivado：打开 Tcl Console，进入 rtl 目录，`source run_xsim.tcl`，运行 behavioral simulation。
官方 UG900 有 xsim 波形和调试说明。测试脚本不综合、不下载开发板。

## 时序约定

输入数据和标签在负沿设置，在正沿进入各自 L 级移位寄存器；L 个周期后同时到达 FIFO 输入。
FIFO 下一接收沿写入，所以存在额外队列接收时序，不能把模块寄存器数直接当作整机端到端延迟。
实际 ADC 的“第几个采样沿产生第几个码”以数据手册、DCO 和测试模式为准，不能直接套用 L=12。
真实接口常使用 ADC 源同步时钟捕获，多位总线不允许每位各串两个触发器跨时钟域。

## 包边界

此示例 in_last 每行一次。若连接普通 AXI DMA，必须相应地将一行定义为一个 DMA 包并配置描述符，
或另行把多行合成一个包后再发 TLAST。不能把 video 的行末、帧首约定和任意 DMA 包约定混为一谈。
最少增加连续行数检查、缺 SOS、额外 SOS、复位中断、异步时钟、FIFO 回绕、全帧错误状态测试。
ADC 不能因 TREADY=0 自行停下来；FIFO 溢出时应作废并重扫该帧，不得静默丢一个像素后继续拼图。
