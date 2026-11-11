# zlscan

激光底片扫描仪仿真软件。当前处于阶段 0（仓库骨架、基线冻结、数据合同），尚无物理模型。

- 开发方案：`docs/plan/仿真软件开发方案_V5.0.md`
- 给编码代理的规则：`AGENTS.md`（Claude Code 经 `CLAUDE.md` 读取同一份）
- 当前进度：`docs/dev/PROGRESS.md`
- 待你决定的事项与待提供的资料：`docs/dev/DECISIONS.md`
- 启动、切换、验收代理时可粘贴的提示词：`docs/dev/prompts.md`

## 安装与自检

需要 Python 3.11 及以上。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/ruff check .
.venv/bin/pytest -q
.venv/bin/zlscan --help
```

未实现的子命令会以非零状态退出，并提示计划在哪个阶段实现。

本工程不代表任何实机性能、激光安全或检测合规结论。
