# 最新安装包与离线双锁验证（S7-T05）

2026-11-13。本轮把当前 dynamic/timing/segmented 功能纳入实际安装验证。
新增可选离线输入模式，后端和运行依赖各自有 wheelhouse 与清单，所有包先校验
SHA256、Name/Version 和 basename 路径，再离线安装。旧项目 wheel 不参与当前构建。
后端 setuptools 84.2.1、wheel 0.48.0、packaging 26.3，来自本轮实际下载文件；
bootstrap pip 26.2.1 与 CPython 3.14.7 为本机记录值，没有冒充哈希锁。
运行目标为 macOS 15.7.6 的 CPython 3.14 x86_64 进程。

构建与运行 venv 分开，都在仓库外、不共享 system-site-packages；删除 PYTHONPATH、
PYTHONHOME、VIRTUAL_ENV 的源码污染入口，运行 -I 核对模块路径和实际安装前缀。
已知干净提交逐文件匹配固定 Git blob；dirty 开发尝试明确不作该声明。
构建后再次检查跟踪源未变；两份独立源副本在同一后端与 SOURCE_DATE_EPOCH 下构建，
要求 wheel SHA256 相同。150 个 Python 模块与 5 个运行资源都与源逐字节匹配。

当前仍是研究快照版本 0.1.0.dev0，不同源码点由提交+wheel SHA256+配套锁区分。
开发审计起于 dirty 的 389e16e，不能把它算干净正式构建。该开发尝试的两份 wheel
字节一致，3 后端包和 20 运行包离线安装、pip check、原短模拟/HTTP、静态三例、
动态四例、时基六例和分段重建/28 HTTP 及各新进程回读均通过。
这些合成与短模拟路径只说明软件可运行，不证明硬件精度或物理模型适用性。

新增合同红灯为缺少 wheelhouse 验证器。包保护检查覆盖缺 Python 模块/字节改变、
篡改依赖包、伪造版本、越出 house 路径、旧项目替换、可变源冒充固定提交和
离线参数不完整时不得回退网络；10 项检查通过（1.18 s）。全量
1485 通过/2 跳过（185.32 s，缺 Icarus 的 RTL 未执行），Ruff 通过。

另一台干净机器、Windows/Linux/其他 Python 版本尚未执行。
三包后端锁不等于宿主 Python/pip/OS/dev/外部仿真器全锁，项目许可与 CI 仍保留。
浏览器视觉、测量支持和阶段 3–7 其它门槛不因此升级。

干净提交 `631c542` 的正式构建已逐一核对 1212 个跟踪文件与固定 Git blob，
两份独立源码的 wheel SHA256 相同，150 模块/5 资源及全部已安装 CLI/新进程验证通过。
正式 wheel SHA256 为 be53ae4a8d22344b3d92a35c012d4d7f95720dc60ad4f9a070ddf9b81e7eaa50。
配套运行与构建锁保存在 install/locks/ 下带 631c542 的文件，旧锁保留。

[离线包](../../install/bundles/scansim-macos-x86_64-cpython314-631c542.tar.gz)
含 20 份运行 wheel 和 3 份构建 wheel（packaging 在两个组中各保留一份）、锁、清单、
5 个独立审计脚本与说明，共 35 个文件，56,005,792 字节。
包 SHA256：a397d019cc6822007b67cebfaaf25fdc794391bc6cd441db32e190a771f42932。
实际解包后每个成员核对原摘要，从解包 wheelhouse 在另一个全新 venv 离线安装，
pip check、-I 位置/版本及所有模块/资源再检查通过。新环境实际执行动态四例/时基六例
及新进程回读，完整结果与正式安装及开发原件逐字节相同；正式安装的分段 12 数组
与开发原件逐元素相同（含 NaN）。当前 150 Python/5 资源也与 dirty 开发尝试的源摘要相同。

[汇总](latest_install_audit.json)，正式与解包安装记录及完整案例/原日志/下载后端原件的
逐成员校验归档在 evidence_runs/2026-11-13_latest_install/record/；依赖 wheel 本体在上述
可携包中保留原字节和原许可证文件。没有把同机新 venv 说成另一台干净机器。
