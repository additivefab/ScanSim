# 官方资料与证据索引
核对日期：2026-11-10。用户原件随包保存；第三方手册不重打包，提供官方地址及下载脚本。网页可能更新，使用时核对文档修订号。

## U1 | 用户提供：激光扫描仪调研学习记录.docx
第1页扫描光学；第2页PMT/高压/对数前端/示波器；第3页ADC模块丝印。
../originals/激光扫描仪调研学习记录.docx

## U2 | 用户提供：Smart ZYNQ SP2 V1.3/V1.3B schematic
第1页资源；第2/5页网口与PL连接；第10页J5/J6及版本差异；第12页时钟约束。
../originals/SmartZynq_SP2_Schematic_V1d3B_20260413.pdf.pdf

## R1 | Analog Devices AD9268 Rev.A datasheet
第9页转换速率、流水线延迟；第10-11页时序；模拟输入、数字接口与SPI章节。模块丝印匹配，具体装配/模式仍待确认。
https://www.analog.com/media/en/technical-documentation/data-sheets/ad9268.pdf

## R2 | Hamamatsu Photomultiplier Tubes: Basics and Applications, 4th edition
第2章原理，第3章连接/高压，第4章灵敏度、暗电流、噪声、线性度；不是用户PMT具体型号手册。
https://www.hamamatsu.com.cn/content/dam/hamamatsu-photonics/sites/documents/99_SALES_LIBRARY/etd/PMT_handbook_v4E.pdf

## R3 | Analog Devices AD8304 Rev.A datasheet — 仅作对数放大原理参考
第1及9页电流对数关系和低电流带宽下降。没有证据证明用户前端使用AD8304，也不建议据此直接选型。
https://www.analog.com/media/en/technical-documentation/data-sheets/ad8304.pdf

## R4 | Sill Optics: F-theta lenses technical guide
扫描场、平场聚焦、机械与光学扫描角；等效模型不替代实际镜头处方。
https://www.silloptics.de/en/service/sill-technical-guide/laser-optics/f-theta-lenses

## R5 | AMD UG900: Vivado logic simulation
RTL功能仿真、xvlog/xelab/xsim、波形分析。请匹配本机Vivado版本。
https://docs.amd.com/r/en-US/ug900-vivado-logic-simulation

## R6 | AMD PG021: AXI DMA
S2MM、流接口、内存接口、TLAST/长度与DMA示例设计。
https://docs.amd.com/r/en-US/pg021_axi_dma

## R7 | AMD UG585: Zynq-7000 Technical Reference Manual
PS/PL、HP接口、DDR、EMIO Ethernet；不能把PL连接的PHY当成已接好PS MIO网口。
https://docs.amd.com/r/en-US/ug585-zynq-7000-SoC-TRM

## R8 | ngspice official documentation
行为源、瞬态分析、控制命令；下载入口为官方站download.html。
https://ngspice.sourceforge.io/docs.html

## R9 | Analog Devices LTspice official download
模拟电路仿真工具与官方模型。
https://www.analog.com/en/resources/design-tools-and-calculators/ltspice-simulator.html

## R10 | Ansys OpticStudio: Exploring Non-Sequential Mode
非序列光线追迹、散射、反射、导光组件/杂散光。
https://optics.ansys.com/hc/en-us/articles/42661670424851-Exploring-Non-Sequential-Mode-in-OpticStudio

## R11 | MathWorks: Types of Sample Time
可选Simulink多速率建模路线，不是本包必需依赖。
https://www.mathworks.com/help/simulink/ug/types-of-sample-time.html

## R12 | SciPy gaussian_filter
PSF近似卷积。
https://docs.scipy.org/doc/scipy/reference/generated/scipy.ndimage.gaussian_filter.html

## R13 | SciPy lfilter
一阶递推低通滤波的执行实现。
https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.lfilter.html

## R14 | Icarus Verilog official repository
可选免费RTL仿真器，-g2012与vvp使用说明。
https://github.com/steveicarus/iverilog

## R15 | Python official downloads
Python安装入口；本包实际测试Python 3.13.5。
https://www.python.org/downloads/
