# 02 架构

## 1. 仓库布局

```text
zlscan_v5/
  AGENTS.md  CLAUDE.md  pyproject.toml  README.md
  src/zlscan/
    contracts/     数据合同（时间、样本身份、事件、信号、标定头、码流头）
    evidence/      参数证据表、资料索引、禁止默认项
    hardware/      板卡画像、引脚表、电气规则检查
    config/        五棵配置树、约束求解、旧配置转换、预设
    budget/        可行性预算器
    engine/        状态推进、调度、随机数、检查点
    truth/         真值世界 ↓
      scene/  source/  scan/  psf/  collect/  detector/  afe/  adc/  timing/
    adc_link/      ADC 输出格式、位序、数据时钟、测试码
    digital/       位精确采集参考模型
    transport/     多级队列、服务曲线
    control/       状态机、命令、时期
    experiment/    实验时间线
    observation/   示波器、探头、测点模型
    calib/         标定（真值不可见）
    recon/         重建（真值不可见）
    metrics/       评价（可读真值）
    ident/         实测导入与参数辨识
    telemetry/     信号注册表、只读探针
    trace/         迹线、事件索引、摘要
    replay/        检查点重放
    causality/     样本链路、依赖窗口、像素反查
    api/           本地查询服务（第二级工作台）
    io/            码流、标定文件、图像、运行记录的读写
    report/        离线报告
    cli.py
  ui/
    viewer/        第一级：离线网页生成器与模板
    workbench/     第二级前端
  legacy/          包 A、包 B、审查包表格的冻结副本，只读
  cosim/           cocotb 测试台与 RTL
  spice/           网表与导出脚本
  tools/           一次性脚本（交叉比对、基准测试）
  tests/           unit/ contract/ legacy/ integration/ perf/
  evidence_runs/   每次正式运行的证据记录
  docs/
```

物理链模块统一放在 `zlscan.truth` 之下，是为了让"真值不可见"变成一条可以机械检查的导入规则。

## 2. 数据流

```text
配置（五棵树）──→ 实验时间线 ──→ 引擎
                                    │
                    truth 链逐细步推进，产生 ADC 转换样本
                                    ▼
                 adc_link → digital → transport → io（原始码流 + 事件流）
                                    │
          ┌─────────────────────────┴───────────────┐
          ▼                                         ▼
   calib（标定扫描 → 标定文件）            telemetry / trace / replay（只读探针）
          ▼                                         ▼
   recon（码流 + 标定文件 → 图像 + 掩膜）         ui
          ▼
   metrics（图像 + 真值 → 指标）
```

## 3. 导入边界（由测试强制）

| 包 | 允许导入 | 禁止导入 |
|---|---|---|
| `contracts` | 标准库、pydantic、numpy | 工程内其他任何包 |
| `truth.*` | `contracts`、`engine`、`config` | `calib`、`recon`、`metrics`、`ui` |
| `calib`、`recon` | `contracts`、`io`、`config` 中的 `instrument` 与 `hardware` 部分 | `truth`、`metrics`、`telemetry`、`replay`、`config` 中的 `truth` 部分 |
| `metrics` | 以上全部 | — |
| `telemetry`、`trace`、`replay`、`causality` | `contracts`、`engine`、`truth`（只读） | 不得被 `calib`、`recon` 导入 |
| `ui`、`api` | `telemetry`、`trace`、`replay`、`causality`、`io` | 不得写入运行数据 |

`tests/contract/test_import_boundaries.py` 用静态分析（解析 import 语句）检查上表。新增包时同步更新该测试。

## 4. 模块接口

每个有状态的物理或数字模块实现同一协议。精确签名在 `contracts/module.py` 定稿，下面是语义：

```python
class Module(Protocol):
    spec: ModuleSpec            # 实现规格，见下

    def initialize(self, cfg, rng_factory) -> State: ...
    def advance(self, state: State, inputs: Inputs, t0: Tick, n_steps: int) -> tuple[State, Outputs]: ...
    def checkpoint(self, state: State) -> bytes: ...
    def restore(self, blob: bytes) -> State: ...
    def observables(self) -> list[SignalDecl]: ...   # 向信号注册表声明可观察量
```

约束：

- `advance` 是纯函数式的：相同的状态、输入、起始时刻、步数，得到相同结果。不得读全局状态或墙上时钟。
- `advance(s, x[0:a+b])` 与先 `advance(s, x[0:a])` 再 `advance(s', x[a:a+b])` 的结果一致（数字量逐位一致，浮点量在声明的容差内）。这是分块一致性和重放的基础。
- `checkpoint` 必须包含恢复所需的一切，包括随机过程的状态。
- 不在行起点重置状态。

`ModuleSpec` 是每个物理模型的实现规格，实现前先写：

| 字段 | 内容 |
|---|---|
| 接口 | 输入输出名称与单位 |
| 状态 | 状态变量与初值 |
| 方程 | 数学形式与离散化方法 |
| 参数 | 每个参数的名称、单位、证据状态 |
| 适用范围 | 带宽、幅度、步长要求 |
| 保真度 | `L0` 理想、`L1` 行为、`L2` 数据驱动 |
| 验证 | 对应测试的名称 |

## 5. 引擎

- **多速率**：光学与模拟按细步；ADC 按采样时钟；数字按接收拍；系统服务与控制按事件。
- **顺序推进**：有状态链按时间顺序算。不做逐行并行。并行只用于独立实验、参数扫描、无状态的预计算。
- **分块**：按行块流式推进，块的终状态是下一块的初状态；结果逐块写盘，内存占用与幅面无关。
- **空白区**：只有线性环节且输入恒定时可以解析推进；非线性环节逐点算。两种做法的一致性有专门测试。
- **检查点**：默认每行一个，另在配置变化、阶段切换、异常处加密。
- **随机数**：见 `03_contracts.md` 第 7 节。
- **同刻事件**：按固定且带版本号的优先级排序，定义在 `engine/schedule.py`。

## 6. 配置

五棵树，各是一个 pydantic 模型：

| 树 | 内容 | 谁能读 |
|---|---|---|
| `truth` | 正向物理模型的参数 | `truth.*`、`metrics`、只读探针 |
| `instrument` | 仪器自认为的参数（标称值、标定结果） | `calib`、`recon` |
| `hardware` | 板卡、模块、接线、电平、通道角色 | 全部 |
| `experiment` | 实验时间线、产品需求、性能测试算例 | 全部 |
| `observation` | 测量仪器与测点 | `ident`、`observation` |

规则：

- `instrument`、`hardware` 里的未知值不得从 `truth` 补齐。
- 每个参数是一个带证据的值，结构见 `03_contracts.md` 第 1 节。
- 两种模式：`research`、`hardware_constrained`。后者缺资料即拒绝。
- 预设：`legacy_a`、`legacy_b`（只为复现旧结果）、`nominal`（新默认模型；方案中写作 `v4_nominal`）。
- 约束求解：面数、转速、焦距、像素间距、采样率、每像素样本数、占空比之间有硬约束，配置层指定自变量，其余推导，冲突报错。

## 7. 保真度与替换

每个物理环节至少有一个 L0 实现，用于排查坐标与码值。L1 是参数化行为模型。L2 是查表或拟合，数据来自实测、SPICE 或光学软件。配置里按环节选择实现；主流程不因替换而改变。

## 8. 命令行

`zlscan budget | check | run | sweep | calibrate | calibration-set | reconstruct | metrics | ident | compare | report | cosim | replay | serve`

每个子命令随对应阶段实现；未实现的子命令给出明确的"尚未实现"提示并返回非零状态，不得静默成功。


2026-11-13：持久研究探针文件格式由 `io/probe_store.py` 管理，engine 在同一生产边界
连接 Chain 输出及统一根；trace 仍只负责摘要/显示计算，不为文件提交扩宽导入白名单。
研究探针文件全部 ORACLE_ONLY，calib/recon 不读取它们；工作台从 engine 只读组合。
