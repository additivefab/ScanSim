# 安装与复现

当前包为研究开发快照 `0.1.0.dev0`，核心物理适用性和未完成项见
[开发进度](dev/PROGRESS.md)。包版本由 `src/zlscan/__init__.py` 单一提供，
CLI 与 wheel 元数据由安装验证核对。升级包版本不会升级任何硬件证据。

## 从源码安装

要求 Python 3.11 及以上。以下为使用方式；本轮实际环境是 macOS 15.7.6、
Python 3.14.7、Python 进程 x86_64。Windows/Linux 与其他 Python 版本矩阵未执行。

macOS/Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,legacy,ident]"
.venv/bin/zlscan --version
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q
```

Windows（命令未在 Windows 实跑）：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev,legacy,ident]"
.venv\Scripts\zlscan.exe --version
.venv\Scripts\python.exe -m pytest -q
```

基本仿真使用基础依赖；ident 增加 Matplotlib 残差图，legacy 增加旧包测试
所需绘图依赖，dev 增加 pytest/Hypothesis/Ruff。RTL 协同另外需要 Icarus/vvp，
SPICE/板级综合工具不包含在 wheel 中。缺失外部工具的测试会明确跳过，
跳过不表示该项验证通过。`zlscan check` 的未决硬件画像正常返回 4；
安装正确也不会把缺失接线、寄存器和测量数据变成已验证。

## 构建与隔离验证 wheel

在源码仓库内运行：

```bash
python3 tools/audit_wheel_install.py --out evidence_runs/my_wheel/output
```

工具复制 Git 跟踪文件的当前内容到仓库外的新目录，排除可编辑安装、
旧 build/egg-info 和未跟踪文件；构建时记录原提交/脏状态及完整文件摘要。
正式证据要求从干净提交执行。所有运行 HTML/JSON/脚本等资源逐字节
比较源快照与 wheel，避免源码目录掩盖资源遗漏。

构建及下载阶段需要网络。运行验证在仓库外另建无 system-site-packages
的 venv，使用 `--no-index --require-hashes` 从 wheelhouse 离线安装
基础运行依赖和 ident 可选依赖；20 个发行包是本轮实际解析结果，
将来的依赖集合由对应构建的锁记录。通过 Python -I、包导入位置和
CLI/元数据版本一致性核对可编辑源码未参与运行。

工具实际执行：五项资源摘要、`pip check`、CLI 帮助/版本、硬件未决
返回码、数字闭环、短 nominal 标定/物理/重建/离线报告、HTTP 根页面及
运行清单/manifest，以及静态辨识的相容、失配、共线不可辨识三例。
HTTP 验证检查响应及字节，不执行浏览器 JavaScript，也不算界面视觉验收。
短模拟及合成辨识只证明软件路径，不证明物理统计、密度精度或板级性能。

输出有 audit.json、requirements.lock、wheelhouse_manifest.json、源码摘要、
wheel、smoke.json 和逐步日志。work_dir.txt 指向保留在仓库外的构建、
wheelhouse 和环境。progress.json 记录已完成命令，不代替实际进程状态。
输出目录独占创建，重跑选择新目录，保留失败记录。

## 使用配套离线工件

锁文件绑定特定平台/Python/构建 wheel 的版本与 SHA256；与对应 wheelhouse
配套使用。另一个安装目录可执行：

```bash
python3 -m venv offline_env
offline_env/bin/python -m pip install --no-index --find-links /path/to/wheelhouse --require-hashes -r /path/to/requirements.lock
offline_env/bin/zlscan --version
offline_env/bin/python -m pip check
```

新构建由工具生成配套锁。当前记录只覆盖同一主机上的隔离环境，
系统 Python、内核及系统库仍来自该主机；另一台干净机器、Windows/Linux
与其他 Python 版本尚未验收。构建后端版本会在 wheel 元数据/日志中记录，
构建工具链与 dev/外部仿真工具的完整锁定、发布许可及 CI 仍待完成。

当前正式参考锁：`install/locks/macos-x86_64-cpython314-runtime-ident.lock`；
对应工件/来源/平台汇总见 `docs/reports/wheel_install_audit.json`。
这份锁只对应记录中的特定 wheel；配套 wheelhouse 位于审计 work_dir。
源码重新构建时，工具会生成该构建对应的新锁。
