# 底片扫描仪全链路仿真资料包 V1.0

**先看结果，不必先装开发环境：解压后双击 `viewer/index.html`。**

本包针对你提供的激光底片扫描仪调研照片及 Smart ZYNQ SP2 原理图建立。目标是把“位置—透射光—PMT 电流—模拟电压—ADC 码—像素—整帧”串起来，并明确哪里是资料事实、哪里是待测假设。

## 建议阅读顺序

1. 打开 `docs/底片扫描仪_全链路仿真设计与操作指南.pdf`。19 页，含光路、公式、逐时刻示例、重建算法、FPGA 路线、预算、测量计划。
2. 打开 `viewer/index.html`：拖动时间滑块，比较当前光功率、PMT 电流、对数电压、ADC 输入和延迟后的数字码。每一步为一个 ADC 采样时刻，示例间隔 50 ns。
3. 查看 `results/default/` 内的实际生成结果，特别是浮点密度、无效掩膜、逐采样 CSV、密度阶梯误差与指标。
4. 运行 Python 后，按理想链路 → 光斑 → 模拟带宽 → 噪声 → 几何误差的顺序做实验；不要一开始就运行完整工业尺寸。
5. 用 `measurement_templates/` 和 `docs/参数证据与测量清单.csv` 收集实测数据，逐一替换假设。

## 验证状态：不要把教学模型当成已标定实机

- Python 端到端模型已执行，生成 256×128 裁剪图、65,536 个有效 ADC 样本及追踪日志。
- 14 项 Python 自动测试通过；已做无噪声时间步收敛实验和参数对照实验。
- RTL 源码、测试平台、Vivado XSim/Icarus 启动脚本与 SPICE 行为网表已提供，**本环境没有执行这些外部仿真器**。不能据此宣称 FPGA 已验证或已完成模拟电路设计。
- 未接实物、未测镜头/PMT/模拟板、未实现可直接下载到开发板的完整 Vivado 工程、ADC 引脚接收器、CDC、DMA 驱动或网络栈。
- 默认尺寸、635 nm、光功率、镜面数/转速、20 MSPS、PMT 参数与模拟带宽均是可替换的教学假设。125 MSPS 是照片中的 ADC 模块标称线索，不是已测实际采样率。
- 本包不包含未知的专用镜头处方、PMT 型号、ADC 模块原理图及模拟板电路；这些必须从实物或供应商获得。

更完整的运行与验证记录见 `docs/验证状态.md`。

## Windows 运行

安装 Python 3（推荐有对应依赖轮子的 3.11–3.13 环境）后，在解压目录双击 `run_windows.bat`。首次安装依赖需要联网。也可在终端执行：

```bat
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python run_demo.py
.venv\Scripts\python -m pytest -q
```

运行后打开 `results/default/viewer.html`。`viewer/index.html` 是交付时预生成的快照，不会随重新运行自动覆盖。

## Linux/macOS 运行

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run_demo.py
.venv/bin/python -m pytest -q
```

运行 `bash run_linux.sh` 也可以。已测试包版本记录在 `requirements-tested.txt`；不同平台/库版本不保证逐位一致。不存在必须依赖联网服务才能打开的结果页面。

## 接着做五个实验

以下命令中的 `python` 应替换为虚拟环境解释器，或先激活虚拟环境。

```sh
python run_demo.py --config config/ideal.json --out results/ideal
python run_demo.py --config config/lowlight_slow.json --out results/lowlight
python scripts/experiments.py
python scripts/convergence.py
python scripts/budget.py
```

`ideal` 用于排查坐标与码值；`lowlight_slow` 演示电流相关带宽；`experiments` 做对照；`convergence` 比较 4/8/16/32 倍模拟细分；`budget` 只做工业尺寸的解析资源预算，不声称运行了 7000×8600 整帧。

## 核心文件

| 目录或文件 | 内容 |
|---|---|
| `src/scanner_sim.py` | 光斑/透射率、扫描轨迹、光子统计、PMT、对数/滤波、ADC、几何与密度重建 |
| `config/*.json` | 教学、理想、低光动态和工业尺寸预算参数 |
| `results/default/first_lines_trace.csv` | 前两行采集窗口内逐 ADC 采样记录，含输入与总线标签 |
| `results/default/frame_events.csv` | 整帧各行 SOS、假设的光门、有效区及帧事件 |
| `results/default/adc_active_u16le.raw` | 单通道 16 位小端 ADC 原码，**不是密度像素** |
| `results/default/simulation_arrays.npz` | 浮点数组、参考场景、重建、白/遮光参考、无效掩膜 |
| `results/default/density_float32.tiff` | 仿真定量密度，需配合无效掩膜，不是认证检测输出 |
| `rtl/` | 标签对齐与有限 FIFO 教学模块、测试平台和仿真脚本；非完整 AD9268 接收器 |
| `spice/` | 对数/滤波和交流耦合风险的行为网表；非实际板级电路 |
| `measurement_templates/` | 实测数据空模板，表头不是虚构测量记录 |
| `references/` | 官方手册/软件入口、用途和 URL；第三方全文未重新分发 |
| `originals/` | 用户提供的四份原始资料副本 |

## 官方资料获取

打开 `references/index.md` 按用途阅读。联网环境可运行：

```sh
python scripts/fetch_references.py
```

它仅尝试下载索引中明确列出的官方公开 PDF，检查 PDF 文件头；失败会报告网址。厂商手册版权及许可归原提供方。**本包内没有宣称已经下载的第三方手册全文。**

## 重要工程约束

PMT 高压和激光是实物安全风险；本包没有安全认证控制逻辑。实物检查不得旁路联锁或凭未核实电平接线。

先确认 ADC 模块输入网络是否保留 DC，再确认 CMOS/LVDS、输出电平、DCO、实际采样率、芯片速度等级及开发板版本。绝不能直接把裸 AD9268 的 1.8 V CMOS 输出当成已匹配 3.3 V FPGA 输入。

对于对数前端，不要直接对 ADC 码做线性暗白相减，也不要先平均对数码再假装等价于平均透射光。对真实静态传递函数做逆变换或标定 LUT，并单独验证动态带宽、饱和与拖尾。

## 文件完整性

`MANIFEST_SHA256.txt` 记录交付文件的 SHA-256。修改参数、重新运行结果或下载参考资料后，相关文件校验值自然会改变。

生成日期：2026-11-10。详情以指南中的“已实现/未实现”边界为准。
