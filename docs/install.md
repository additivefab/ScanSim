# 安装与复现

当前包为研究开发快照 `0.1.0.dev0`，核心物理适用性和未完成项见
[开发进度](dev/PROGRESS.md)。包版本由 `src/zlscan/__init__.py` 单一提供，
CLI 与 wheel 元数据由安装验证核对。升级包版本不会升级任何硬件证据。

## 从源码安装

要求 Python 3.11 及以上。以下为使用方式；本轮实际环境是 macOS 15.7.6、
Python 3.14.7、Python 进程 x86_64。另已在 GitHub 干净 runner 完成 Linux/Windows/macOS ×
Python 3.11/3.14 六目标的完整测试与隔离安装，实际版本及跳过原因见
[跨平台 CI 报告](reports/cross_platform_ci.md)。其它 OS/版本不据此宣称已验证。

macOS/Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,legacy,ident]"
.venv/bin/zlscan --version
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q
```

Windows（PowerShell；CI 用 setup-python 后的 python 模块入口，未单独验收 py 启动器）：

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
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

## 用锁定后端离线重复构建

新增四项成对输入，不能只提供其中一项后退回在线解析：

```bash
python tools/audit_wheel_install.py --out /absolute/new/audit \
  --build-wheelhouse /path/to/build_wheels --build-manifest /path/to/build_manifest.json \
  --runtime-wheelhouse /path/to/runtime_wheels --runtime-manifest /path/to/runtime_manifest.json
```

清单格式为 `{"normalized-name":{"filename":"name-version-tag.whl","version":"...","sha256":"..."}}`。
文件名只可指向给定 house 内的 basename，包 Name/Version 与 SHA256 都要匹配。
后端清单不接受项目 wheel；运行清单的旧项目项明确排除，新项目由当前源快照构建。
工具生成 build.lock 与 external-runtime.lock，在独立 builder venv 离线安装后端，
用 --no-build-isolation/--no-deps/--no-index 构建当前项目。运行依赖也离线按哈希校验，
再生成包含本次项目 wheel 的 requirements.lock；缺包/错摘要直接失败。

干净运行逐文件核对固定提交的 Git blob；全部 Python 模块和 5 项资源均按字节检查。
两份独立的跟踪源副本用相同 SOURCE_DATE_EPOCH 和后端构建，要求 wheel 字节相同。
安装验证还包含 dynamic/timing/segmented 的实际 CLI 和独立进程回读。
三份后端包的锁不包含宿主 CPython、ensurepip 自带 pip、内核/系统库或外部仿真工具，
这些实际版本另外记录。重复构建是本机同一工具链的结果，不代表跨平台字节一致。

## 使用配套离线工件

锁文件绑定特定平台/Python/构建 wheel 的版本与 SHA256；与对应 wheelhouse
配套使用。另一个安装目录可执行：

```bash
python3 -m venv offline_env
offline_env/bin/python -m pip install --no-index --find-links /path/to/wheelhouse --require-hashes -r /path/to/requirements.lock
offline_env/bin/zlscan --version
offline_env/bin/python -m pip check
```

新构建由工具生成配套锁。旧 macOS 参考包覆盖原主机；新增六目标 CI 在各自
干净 runner 上实际解析目标依赖、按新哈希锁离线构建和安装，不能混用不同目标
的二进制依赖。见跨平台 CI 报告与归档中的各目标锁。系统 Python/内核/系统库
来自实际 runner，CPython/OS/dev/外部工具完整锁与发布许可审计仍待完成。

当前正式参考锁：`install/locks/macos-x86_64-cpython314-runtime-ident.lock`；
对应工件/来源/平台汇总见 `docs/reports/wheel_install_audit.json`。
这份锁只对应记录中的特定 wheel；配套 wheelhouse 位于审计 work_dir。
源码重新构建时，工具会生成该构建对应的新锁。

## 当前已核验的离线包

[macOS CPython 3.14 x86_64 / 631c542](../install/bundles/scansim-macos-x86_64-cpython314-631c542.tar.gz)
携带完整运行/构建 wheelhouse、配套锁、摘要与合成示例审计脚本。先解包并进入目录，
选择一个尚不存在的新环境目录，执行包内 README 的 --no-index/--require-hashes 安装命令。
实际包 SHA256 和新环境重装/辨识结果见 [最新核验](reports/latest_install.md)。
该包绑定源码点 631c542；复现相同 wheel 字节需相同源码点、SOURCE_DATE_EPOCH 和已记录
构建环境。后续文档提交或其它源码点的重建会产生自己的配套锁，不复用此 wheel 的摘要。
旧 reference 锁仍保留为历史；新锁为 install/locks/ 中带 631c542 的 build/runtime-ident 文件。

Windows 升级前先关闭旧录制写入进程。D074 将强制锁移至文件保留区域，
历史文件格式不变，但旧、新版本的活跃写入器不能混用；完成关闭后可按检查点恢复。
JSON/HTML 工件使用 UTF-8，安装审计继续保留 -I；上面的 UTF-8 环境也用于
子 CLI 的文本输出，不应依赖系统代码页。

## 多工作点标定软件路径

085e914已在六个CI目标实际安装，包含calibration-set公开命令及谱系v3。
多份由自身参考扫描生成的标定可组合为自包含集合，命令保留原绑定且拒绝覆盖文件；
reconstruct仍用--calibration入口，捕获v2须明确给出每个时期的完整epoch_bindings。
零匹配/歧义/未记录会话身份均拒绝；当前录制操作历史尚未自动导出v2，
须由采集端/操作者提供，不能从模型补增益或寄存器。见[使用与验证报告](reports/epoch_calibrations.md)。
旧单表/v1仍可用，旧v3未记录的坐标来源返回not_recorded。
