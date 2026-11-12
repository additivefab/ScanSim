# zlscan

激光底片扫描仪仿真软件。已实现可行性预算、数字采集、单通道物理仿真与本地工作台。阶段 3 的剩余验收见 [开发进度](docs/dev/PROGRESS.md)。

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
.venv/bin/zlscan serve
```

运行目录包含 `observable/raw.zlraw`、完整性报告和离线工作台 `viewer.html`。当前重建输出为采集码值图与无效掩膜。已标定的光学密度图属于阶段 4。

本工程不代表任何实机性能、激光安全或检测合规结论。
