# PROGRESS — 当前状态与交接

这是活文档。每次会话结束前更新。接手的代理从这里开始读。

## 当前状态

| 项 | 值 |
|---|---|
| 当前阶段 | 阶段 0（进行中） |
| 进行中的任务 | S0-T05（`Param` 合同已完成，统一参数表待做） |
| 下一个任务 | 完成 S0-T05，然后 S0-T06。S0-T04 被 Q012 挡住，不影响它们 |
| 代码 | 仓库骨架：33 个包目录、`cli.py`（13 个子命令，均未实现）、`io/manifest.py`、`contracts/param.py`、`evidence/{records,legacy_runner,sim_tools}.py`、197 项测试（其中 40 项遗留回归，约 35 秒）；`legacy/` 已冻结（230 个文件，72.5 MB） |
| git | 已初始化（分支 main），仅本地，无远端；作者身份是临时的，见 DECISIONS.md Q011 |
| 最近一次全量测试 | 2026-11-11 `pytest -q -m "not slow"`：163 通过（另有 34 项慢测试，含遗留回归，上一次完整运行 159/159 通过） |

## 阶段总览

| 阶段 | 内容 | 状态 | 门槛通过日期 |
|---|---|---|---|
| 0 | 证据、硬件画像、基线冻结、观测合同 | 进行中 | — |
| 1 | 可行性预算器 | 未开始 | — |
| 2 | 数字最小闭环、第一级工作台 | 未开始 | — |
| 3 | 合并基线物理模型、回放、第二级工作台 | 未开始 | — |
| 4 | 标定、重建、指标、成像视图 | 未开始 | — |
| 5 | 正向模型增强 | 未开始 | — |
| 6 | 板级验证、实测辨识（依赖资料） | 未开始 | — |
| 7 | 性能、三维、产品化（可选） | 未开始 | — |

## 阶段 0 任务清单

| 任务 | 状态 | 结果或备注 |
|---|---|---|
| S0-T01 仓库骨架 | 完成 | 验收四项均实际运行并通过；`zlscan run` 返回 3 |
| S0-T02 冻结旧包 | 完成 | `tools/freeze_legacy.sh` 复制，`legacy/MANIFEST.json` 记 230 个文件。冻结前后都用两套包自带的交付清单核对：包 A 源 76/76、包 B 源 142/142、审查包 27/27 一致；冻结副本里包 A 的 72 项一致、另 4 项 `originals/` 在 `legacy/originals/` 一致，包 B 142/142 一致。反向验证过：多出文件、被改文件都会使契约测试失败 |
| S0-T03 遗留回归 | 完成 | 两套包在临时副本里重跑，与作者交付的指标逐项比较：包 A 7 项判据、包 B 21 项判据全部通过，两套自带测试各 14/14 通过。数值与交付时（NumPy 2.3.5）逐位一致，只有包 A 的 MAE 差 7e-18（求和顺序）；断言规则：整数严格相等，浮点相对容差 1e-12。证据记录见 `evidence_runs/2026-11-11_legacy_package_{a,b}/`（提交号 5f22a3d，树干净）。补得 `ideal_regression` 的值 2.0255271769153482e-05，已写入 `06_testing.md`。`legacy/` 在整个过程中保持与清单一致 |
| S0-T04 外部仿真器 | **未执行（被 Q012 挡住）** | 用户同意了安装并允许升级已装软件（D020、D022），但 Homebrew 报 `Your Command Line Tools are too outdated` 后拒绝安装，修复需要 sudo 和用户密码。没有任何副作用：12 个已装软件版本不变、新软件一个未装。详见 `docs/reports/simulator_install_attempt.md`。已为 7 个用例（2 个 RTL 测试台、3 个可由 ngspice 运行的网表、LTspice 网表、Vivado 脚本）各写一条 `not_executed` 证据记录（`evidence_runs/2026-11-11_sim_*`，提交号 ded24b9，树干净）。工具装好后：先在 `sim_tools.py` 里写并验证运行路径（`tools/record_simulators.py` 在工具齐全时会拒绝运行），再重新记录 |
| S0-T05 统一参数证据表 | 进行中 | `Param` 合同已完成（14 项测试；规格修正见 D023）。待做：把三张表合并成 `parameters.json`、落成 `forbidden_defaults.json`，并写覆盖性测试 |
| S0-T06 硬件画像 | 未开始 | |
| S0-T07 交叉比对 | 未开始 | |
| S0-T08 数据合同代码 | 未开始 | |
| S0-T09 原始码流读写 | 未开始 | |
| S0-T10 迹线存储基准 | 未开始 | |
| S0-T11 第一级工作台骨架 | 未开始 | |

状态取值：未开始、进行中、完成、未执行（写明原因）、阻塞（写明等什么）。

## 环境备忘

写文档时在本机观察到的情况，接手时请重新确认：

- macOS；Python 3.14.7；系统 Python 里有 NumPy 2.4.2、SciPy 1.18.1，没有 pytest。
- 有 `git`、`ruff`、`brew`、`pdftotext`。
- 工程虚拟环境 `.venv`（已建，已 `pip install -e ".[dev]"`）：numpy 2.5.4、scipy 1.18.1、pydantic 2.14.3、h5py 3.16.0、tifffile 2026.11.8、Pillow 13.0.0、pytest 9.2.0、hypothesis 6.170.15、ruff 0.17.4。注意 numpy 比此前复现旧包基准时用的系统 Python（2.4.2）新，S0-T03 要留意随机序列是否受影响。
- `pyproject.toml` 里 ruff 排除了 `legacy/`、`docs/`、`evidence_runs/`：这个版本的 ruff 会格式化 Markdown 里的代码块，而规格文档里的是示意代码。
- 命令都用 `.venv/bin/…` 前缀，或先 `source .venv/bin/activate`。
- 没有 `iverilog`、`ngspice`、Vivado；Homebrew 安装失败，见上表 S0-T04 与 DECISIONS.md Q012。
- 这是 Intel 版 Homebrew（`/usr/local`），shell 是 arm64，Python 是 x86_64；Homebrew 已不再支持 Intel macOS。
- 建议在工程根目录建虚拟环境 `.venv`，不要往系统 Python 里装包。

## 会话日志

每次会话追加一条，最新的在最上面。格式：

```text
### YYYY-MM-DD  代理名称
- 做了什么（任务号）
- 实际运行的命令与结果（通过 / 失败 / 未执行，附关键输出）
- 改了哪些规格或决策
- 未提交的内容（应为无）
- 下一步建议
- 遗留问题与风险
```

### 2026-11-11  文档编写（Claude）

- 写了 `AGENTS.md`、`CLAUDE.md` 和 `docs/dev/` 下的全部说明文档；把方案 V5.0 复制到 `docs/plan/`。
- 没有写任何代码，没有运行任何测试，没有初始化 git。
- 文档中的遗留基准值来自此前在本机对两套旧包的重跑；重跑当时没有记录代码版本与配置校验值，S0-T03 要重新确认并补齐证据记录。
- 下一步：从 S0-T01 开始。
- 遗留问题：见 DECISIONS.md 的"待用户决定"。
- 用户随后回答了两项：全部入库（D019）；两个仿真器都装（D020）。仿真器尚未安装。
