# zlscan

激光底片扫描仪仿真软件。已实现可行性预算、数字采集、单通道物理仿真与本地工作台。阶段 3 的剩余验收见 [开发进度](docs/dev/PROGRESS.md)。

**物理适用范围（P0）**：现有带噪 `demo`/`nominal` 没有固定的对数前输入响应，统计量依赖数值时间格，
仅用于固定网格探索，不能用于高密度测量能力或实机精度判断。显式研究输入极点已实现，仍需统计收敛、
独立标定残差和实测响应验证。详见 [P0 物理复核](docs/reports/stochastic_log_p0.md)。

- 开发方案：`docs/plan/仿真软件开发方案_V5.0.md`
- 给编码代理的规则：`AGENTS.md`（Claude Code 经 `CLAUDE.md` 读取同一份）
- 当前进度：`docs/dev/PROGRESS.md`
- 待你决定的事项与待提供的资料：`docs/dev/DECISIONS.md`
- 启动、切换、验收代理时可粘贴的提示词：`docs/dev/prompts.md`

## 安装与自检

需要 Python 3.11 及以上。

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

实测波形入口（当前仅 CSV 导入，不含参数辨识）：
`zlscan ident --csv capture.csv --metadata metadata.json --out imported_scope`。
模板与同步/证据限制见 [导入说明](docs/reports/scope_measurement_import.md)。

当前 ZLRAW 0.1 草案可通过 `zlscan ident --raw capture.zlraw --metadata metadata.json --out imported_raw`
导入并保留原样快照；见 [码流导入限制](docs/reports/raw_measurement_import.md)。不代表已对接本机 FPGA。
