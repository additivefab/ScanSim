# zlscan

激光底片扫描仪仿真软件。已实现可行性预算、数字采集、物理仿真与本地工作台。阶段 3 的剩余验收见 [开发进度](docs/dev/PROGRESS.md)。

**物理适用范围（P0）**：现有带噪 `demo`/`nominal` 没有固定的对数前输入响应，统计量依赖数值时间格，
仅用于固定网格探索，不能用于高密度测量能力或实机精度判断。显式研究输入极点已实现，仍需统计收敛、
独立标定残差和实测响应验证。详见 [P0 物理复核](docs/reports/stochastic_log_p0.md)。

- 开发方案：`docs/plan/仿真软件开发方案_V5.0.md`
- 给编码代理的规则：`AGENTS.md`（Claude Code 经 `CLAUDE.md` 读取同一份）
- 当前进度：`docs/dev/PROGRESS.md`
- 待你决定的事项与待提供的资料：`docs/dev/DECISIONS.md`
- 启动、切换、验收代理时可粘贴的提示词：`docs/dev/prompts.md`

## 安装与自检

需要 Python 3.11 及以上。详细平台说明、隔离 wheel 验证和离线锁使用见 [安装与复现](docs/install.md)。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,legacy]"
.venv/bin/ruff check .
.venv/bin/pytest -q
.venv/bin/zlscan --help
```

## 运行仿真

```bash
.venv/bin/zlscan run --mode digital --out digital_out
.venv/bin/zlscan run --mode physical --lines 8 --out physical_out
.venv/bin/zlscan replay --run physical_out --start-step 11000 --steps 100 --out replay_out
.venv/bin/zlscan serve
.venv/bin/zlscan run --mode physical --stream --lines 8 --out dual_out
```

运行目录包含 `observable/raw.zlraw`、完整性报告和离线工作台 `viewer.html`。`run` 的最简重建输出为采集码值图与无效掩膜。以下命令从观测标定扫描生成密度图与独立成像工作台。

```bash
.venv/bin/zlscan run --mode calibration --preset nominal --lines 4 --out reference
.venv/bin/zlscan calibrate --raw reference/observable/raw.zlraw \
  --protocol reference/observable/scan_protocol.json --out calibration
.venv/bin/zlscan run --mode physical --preset nominal --lines 12 --out sample
.venv/bin/zlscan reconstruct --raw sample/observable/raw.zlraw \
  --capture sample/observable/capture.json --calibration calibration/calibration.json \
  --columns 40 --pitch-x-m 0.002 --pitch-y-m 0.002 --out reconstructed
.venv/bin/zlscan report --run reconstructed --raw sample/observable/raw.zlraw --out image.html
```

上面的网格和采样参数来自研究预设。验收结果见 [nominal 与旧包对照](docs/reports/nominal_vs_legacy.md)。

本工程不代表任何实机性能、激光安全或检测合规结论。

示波器波形 CSV 导入入口（可附候选观测模型）：
`zlscan ident --csv capture.csv --metadata metadata.json --out imported_scope`。
模板与同步/证据限制见 [导入说明](docs/reports/scope_measurement_import.md)。

当前 ZLRAW 0.1 草案可通过 `zlscan ident --raw capture.zlraw --metadata metadata.json --out imported_raw`
导入并保留原样快照；见 [码流导入限制](docs/reports/raw_measurement_import.md)。不代表已对接本机 FPGA。

标量测量模板表：`zlscan ident --table measurements.csv --metadata metadata.json --out imported_table`。
支持显式单位/不确定度及拟合/验证来源复用检查，见 [模板与限制](docs/reports/measurement_table_import.md)。
导入不等于辨识或独立实测验收。

示波器 CSV 可附加显式候选观测模型：`--model-waveform model.csv --observer observer.json --observe-channel CH1`。
支持声明的带宽、耦合与采样孔径；模型预测单独标记，不改测量。见 [观测模型与限制](docs/reports/scope_observation_model.md)。

静态线性辨识：测量表导入加 `--static-fit static.json`，显式映射拟合/留出记录，
输出可辨识参数/组合、条件区间和残差图；绘图需安装 `.[ident]`。
见 [辨识假设与使用边界](docs/reports/static_identification.md)，不自动升级实测参数。

已有单通道物理运行可通过 `zlscan serve --run physical_out` 只读打开；
`replay` 默认复用保存的自定义配置。研究源码与交付 ADC 分开，未记录内容保留空缺。
支持范围见 [已有运行工作台](docs/reports/saved_workbench.md)。


版本化研究配置入口（全为假设，不代表硬件参数）：

```sh
zlscan budget --config configs/research_scenario.json --out scenario_budget
zlscan run --mode physical --config configs/research_scenario.json --out scenario_run
zlscan serve --run scenario_run
```

示例预算因未知吞吐/内存返回 4；超限预算返回 1 并保留诊断。
可从 `scenario_run/inputs/resolved_scenario.json` 重跑，或用 replay 自动复用原配置。
字段、双通道及参考标定语义见 `docs/reports/research_scenario.md`。


未知终点物理录制（ASSUMED 研究模式）：

```sh
zlscan run --mode recording --config configs/physical_recording.json --commands first.jsonl --out recording_run
zlscan run --mode recording --resume recording_run --commands next.jsonl
```

命令文件逐行写 JSON，如 `{"op":"lines","count":2}`；EOF 只暂停，
`{"op":"finish"}` 才排空结束。未知尾部原位拒绝，可加 `--out recovered_run`
从提交前缀分支并保留原件。协议、安全停止和资源范围见 `docs/reports/recording_cli.md`。
