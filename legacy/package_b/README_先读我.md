# ZL 底片扫描仪：端到端仿真与实施资料包

版本：2026-11-10。性质：可运行的行为级研究工程，不是已标定的实机数字孪生，也不是可直接下载到 FPGA 的固件。

## 从这里开始

**不用安装任何软件也能先看结果**：双击 `examples/ad9268_50m_5x/viewer.html`。此页面完全离线，能逐 ADC 采样回放第一行、显示模拟电压/码字/像素标签，并回放整幅图像逐行形成的过程。

先读 `docs/底片扫描仪仿真设计与实施指南.pdf` 或同名 HTML；重点关注真实证据、ADC 模块输入耦合、模拟拖尾、像素与采样时钟的区别。然后安装 Python，修改 JSON，重新运行。

## Windows

安装 Python 3.13（本工程实际测试环境为 3.13.5；代码要求 3.11+）。进入解压目录，运行 `run_windows.bat`；首次安装依赖需要联网，此后的仿真与回放无需联网。脚本使用虚拟环境，不要求 PowerShell 激活脚本。

也可以在终端依次执行：

```bat
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe run_sim.py --config configs/ad9268_50m_5x.json --output examples/ad9268_50m_5x
```

## Linux / macOS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python run_sim.py --config configs/ad9268_50m_5x.json --output examples/ad9268_50m_5x
```

依赖仅为 NumPy、SciPy、Pillow、tifffile；不需要 MATLAB、Zemax、Vivado 才能跑通系统级模型。

## 配置选择

| 配置 | 用途 | 是否已实际运行 |
|---|---|---|
| `ideal_regression.json` | 64×16，无噪声/模糊/延迟，仅验证公式与量化 | 是 |
| `demo_log.json` | 512×128，对数前端，10 MS/s，一次采样一个像素 | 是；通用抽象，不是实机设置 |
| `demo_linear.json` | 同样光电条件，改为线性前端 | 是 |
| `ad9268_50m_5x.json` | 256×32，50 MS/s，每5个样本形成1像素，CMOS 12拍参考 | 是；模块模拟接口仍待实测 |
| `reference_budget.json` | 200×250 mm、50 μm、4000×5000 像素的大帧预算 | 只算预算，未运行完整大帧 |

大帧先运行 `python run_sim.py --config configs/reference_budget.json --estimate-only`。不要一开始就打开 `--trace-all`；全帧逐时钟 CSV 可能非常大。默认保存第一行完整 ADC 数据和每行关键事件，微步数据覆盖第一行开始成像附近。

## 关键文件

`src/scanner.py` 是光电、模拟、ADC和重建模型；`src/viewer.py` 生成离线回放；`tests/` 是14项自动测试。
`rtl/` 是延迟标签/FIFO/testbench 骨架；`spice/` 是行为级模拟前端网表；`optics/` 是一阶光学几何计算器。
`templates/` 有实测参数表和明确标记为合成数据的标定例子；`tools/` 有对数静态标定拟合和步长收敛检查。
`reconstruct_capture.py` 可读取已经校验、仅包含有效区的行优先二进制 ADC 数据，按外部标定重建图像。
`originals/` 保留用户提供的四份原始资料；`docs/官方资料索引.md` 提供外部原厂资料入口，没有擅自补造缺失的扫描仪原理图。

## 你最先需要补齐的实机资料

优先确认 AD9268 模块的 SMA 输入是否直流耦合、实际增益/量程、数字电平转换、输出模式和时钟；随后确认 PMT 型号/高压/电流限制、对数放大器电路和最弱光动态响应、SOS 与扫描镜及送片编码器关系。填写 `templates/实测参数记录表.csv`。

14项 Python 测试已通过，但 HDL 和 SPICE 在本次环境中未执行。查看 `docs/验证状态.md`，不要把 Python 通过误读为硬件设计已通过。
