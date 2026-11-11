# 阶段 0：证据、硬件画像、基线冻结、观测合同

估算 2.5–4 人周。本阶段不写任何物理模型，只把地基打好：仓库、冻结的旧包、可重跑的回归、统一的证据表、硬件画像、数据合同。

**进入条件**：无。

## 任务

按顺序做。每个任务完成后更新 PROGRESS.md。

### S0-T01 仓库骨架

- `git init`（若尚未初始化），建 `.gitignore`。
- `pyproject.toml`：`src/` 布局，包名 `zlscan`，入口 `zlscan`，运行与开发依赖按 `05_coding_standards.md`。
- 建空包目录（`02_architecture.md` 第 1 节），每个包只放 `__init__.py`。
- `cli.py`：列出全部子命令，未实现的返回非零并提示"尚未实现"。
- pytest 配置与标记（`slow`、`legacy`、`perf`）；ruff 配置。
- `README.md`：三五行说明与安装命令。

**验收**：`pip install -e ".[dev]"`、`ruff check .`、`pytest -q`、`zlscan --help` 都能运行；`zlscan run` 返回非零。

### S0-T02 冻结旧包

复制到 `legacy/`，此后只读：

| 目标 | 来源 | 取哪些 |
|---|---|---|
| `legacy/package_a/` | `../` | `README_先读.md`、`run_demo.py`、`requirements*.txt`、`run_*.{sh,bat}`、`MANIFEST_SHA256.txt`、`config/`、`src/`、`scripts/`、`tests/`、`rtl/`、`spice/`、`results/`、`viewer/`、`measurement_templates/`、`references/`，以及 `docs/` 下除 `仿真软件开发方案_*.md` 之外的文件 |
| `legacy/package_b/` | `../ZL_OEM_SCANNER_SIM/` | 全部，除 `docs/仿真软件开发方案_V3.md` |
| `legacy/review/` | `../ZL_SCANNER_V3_REVIEW_20261111/` | `tables/`、`configs/`、`scripts/`、`docs/项目资料审查记录.md` |
| `legacy/originals/` | `../originals/` | 全部 |

不要复制 `../zlscan_v5/` 自身。生成 `legacy/MANIFEST.json`（每个文件的相对路径、大小、SHA-256）。

**验收**：一个测试校验 `legacy/` 与清单一致；清单外多出或缺少文件时失败。

用户已决定（D019）：原始资料与两套包的全部内容，包括 `results/`、`examples/`，都纳入 git。提交前用 `du -sh legacy` 记录体积；单个文件超过 50 MB 时停下来报告，不要自行引入大文件存储方案。

### S0-T03 遗留回归

- `tests/legacy/` 下建回归：在子进程里以 `legacy/package_a`、`legacy/package_b` 为工作目录运行它们各自的入口和自带测试，输出到临时目录。
- 断言 `06_testing.md` 第 4 节的数值。先重跑确认本机得到的值，再写断言；容差与环境写进证据记录。
- 包 B 的 `ideal_regression` 单独重跑一次，把实际得到的值补进 `06_testing.md`。
- 每次运行写 `evidence_runs/` 记录（`06_testing.md` 第 7 节）。

**验收**：`pytest -m legacy` 通过；证据记录齐全（命令、提交号、配置校验值、种子、环境、日志、判据）。数值对不上时停下来报告，不改断言去凑。

### S0-T04 外部仿真器

- 检查 `iverilog`、`vvp`、`ngspice` 是否可用。不可用时用 Homebrew 安装 `icarus-verilog` 与 `ngspice`——用户已同意（D020），许可仅限这两个软件。安装后把版本号记入证据记录。
- 可用后，运行两套包的 RTL 测试台与 SPICE 网表，结果无论通过与否都写证据记录。
- 安装失败时，把错误信息报告给用户，状态记为"未执行"，不要改用其他安装渠道。

**验收**：每个测试台与网表在 `evidence_runs/` 里有一条记录，状态为通过、失败或未执行之一。

### S0-T05 统一参数证据表

- 合并三张表：包 A 的 `docs/参数证据与测量清单.csv`、包 B 的 `templates/实测参数记录表.csv`、审查包的 `tables/requirements.json` 与 `configs/硬件配置草案_*.json`。
- 落成 `src/zlscan/evidence/parameters.json`，每项用 `Param` 结构。
- 把 `07_evidence_rules.md` 第 2 节的禁止默认项落成 `evidence/forbidden_defaults.json`，并写测试。

**验收**：三张表的每一行在合并表里都能找到去向；未知项为 `None`；"禁止默认项"测试通过。

### S0-T06 硬件画像

- 导入审查包的 `pin_registry.json`（120 行）与硬件配置草案，落成 `hardware/` 下的模型。
- 把审查包 `scripts/` 的 20 项规则测试移植为本工程的测试。
- 逐行复核引脚表：用 `pdftotext -layout` 提取 `legacy/originals/` 中 SP2 原理图第 10、12 页的文本，与表中每一行比对；不一致处列成报告，不自行改表。
- `zlscan check`：对硬件画像与配置做规则检查，三态输出。

**验收**：20 项规则测试通过；`zlscan check` 对草案配置给出"未决"且列出缺失项；引脚表复核报告写入 `docs/reports/pin_registry_review.md`，说明核对了多少行、多少行一致、多少行无法从文本确认。

### S0-T07 两套旧包的交叉比对

- 在 `tools/legacy_cross_compare.py` 里把两套包配置到尽可能相同的物理条件，逐环节比对：光功率、阳极电流、对数电压、ADC 码。
- 重做并入库两项一次性实验：单步耗时；包 B 的配准消融（固定评价区域与掩膜，报告均方误差、偏差、方差）。
- 写 `docs/reports/legacy_cross_compare.md`：哪些环节对得上；对不上的每一处原因（已知的有：低通离散化方式、片外处理、延迟补偿、对数极性、抗混叠阶数）。

**验收**：报告里每个差异都有解释或明确标"原因未明"；脚本可重跑；证据记录齐全。

### S0-T08 数据合同落成代码

- 按 `03_contracts.md` 实现 `contracts/`：`Param`、`Tick` 与时钟域、`SampleId`、`Event`、信号声明、标定文件头、查询状态、无效标志位、`ModuleSpec` 与模块协议。
- 导入边界测试（`02_architecture.md` 第 3 节）。

**验收**：合同往返测试、导入边界测试通过；时钟域的相位累加在 10⁹ 拍后无累计漂移（对 20、50、125 MSPS 以及一个不能整除皮秒的频率各测一次）。

### S0-T09 原始码流读写

- 实现 `ZLRAW 0.1`（`03_contracts.md` 第 5 节）的读写器与包 B 无头格式的适配器。
- 写 `docs/dev/raw_stream_format.md`：给 FPGA 侧看的独立格式说明，标明"草案，待共同冻结"。

**验收**：写入再读出逐位一致；坏魔数、坏 CRC、长度不符、序号倒退或跳变、时期变化都被检出并生成丢失记录或报错；包 B 的 `raw_roi_u16le.bin` 能经适配器读入。

### S0-T10 迹线存储基准

- 用 HDF5 分块数据集做一个小基准：写入若干路探针的摘要与一段原始窗口，测按时间窗读取的延迟与文件大小。
- 结果写入 `docs/reports/trace_store_benchmark.md` 与 DECISIONS.md。

**验收**：基准脚本可重跑；报告含机器、数据规模、冷热缓存条件。结论只针对所测规模。

### S0-T11 第一级工作台骨架

- `ui/viewer/`：给定一个最小的运行目录（可以只有会话头和几条事件），生成自包含离线网页，含组件树、空的波形区、时间轴、证据标签。
- 未知数据显示为"未知"，不显示为零。

**验收**：`08_workbench_spec.md` 的 W12、W13 通过；用一个故意含 `None` 的配置生成网页，页面上对应位置显示"未知"。

## 阶段门槛

全部满足后才进入阶段 1：

- [ ] S0-T01 至 S0-T11 完成，或明确记为"未执行"并有用户认可的原因。
- [ ] 所有未知硬件参数登记为空值；没有任何 ADC 到板端的接线被虚构。
- [ ] `pytest -m legacy` 可一键重跑且通过，证据记录完整。
- [ ] 导入边界、合同往返、码流读写、禁止默认项四组测试通过。
- [ ] 交叉比对报告与引脚表复核报告已写。
- [ ] PROGRESS.md 记录了实际运行的命令与结果。

## 本阶段不做

物理模型、预算器、数字链、任何标定与重建。
