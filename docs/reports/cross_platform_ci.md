# 三平台与最低/当前 Python CI（S7-T05）

## 最新提交回执：六目标通过

2026-11-13，提交 `5312ecd7239caf420aa2a2eb3a5371fd5cdbba9a` 的
[run 43445872468](https://github.com/additivefab/ScanSim/actions/runs/43445872468)
已结束且整体 `success`。Ubuntu、macOS、Windows 各 Python 3.11/3.14 共六个 job
均为 `success`，包含完整测试与隔离 wheel 审计。原始 GitHub API 回执保存在
`evidence_runs/2026-11-13_cross_platform_ci/record/5312ecd_acceptance.json`。
这次 CI 只确认软件矩阵和安装路径，不改变硬件、浏览器或实测来源门槛。

后续多工作点批次：085e914的六目标安装/资源与元数据验证已完成，见
[新批次报告](epoch_calibrations.md)。本文件下方保留6083281基线及早期平台修复历史。

## 当前结论：六目标实际通过

2026-11-13，[第四轮 run 43363085674](https://github.com/additivefab/ScanSim/actions/runs/43363085674) 六个 job 均 success，
固定源码 `6083281d38ecaa8bdea25534581d0a3c57c0635b`。全部原始 ZIP 已下载，摘要与 GitHub digest 一致、
CRC 与内部逐文件摘要核对完成。以下早期“待返回/失败”文字是按轮次保留的历史，
当前结论以本节与 `evidence_runs/2026-11-13_cross_platform_ci/record/fourth_acceptance.json` 为准。

| 目标 | 实际 Python | 通过 | 跳过 | 完整安装审计 |
|---|---|---:|---:|---|
| macos-latest | 3.11.9 | 1495 | 2 | 通过 |
| macos-latest | 3.14.10 | 1495 | 2 | 通过 |
| ubuntu-latest | 3.11.18 | 1497 | 0 | 通过 |
| ubuntu-latest | 3.14.10 | 1497 | 0 | 通过 |
| windows-latest | 3.11.9 | 1493 | 4 | 通过 |
| windows-latest | 3.14.10 | 1493 | 4 | 通过 |

Linux 无跳过，实际执行了两项 Icarus 功能 RTL 测试；macOS 跳过两项缺失 Icarus
测试；Windows 另跳过两项 POSIX 信号测试，共四项。Windows EOF 暂停/恢复、
跨进程写入排斥与可读前缀、原 JS 帮助函数断言、原理图 PDF 合同均实际通过。
冻结 B 在 Windows 使用明确标记的 NPZ 原字节内存适配，原14断言通过；
未经适配的原套件首轮清理失败仍保存，冻结源不改。

各目标实际核对1238个跟踪源码文件、150个 Python 模块与5个资源字节；
后端3包锁、目标自身19个外部运行依赖加项目 wheel 共20个运行锁项，
随后离线重复构建、全新隔离 venv 安装、pip check、CLI/HTTP、static、dynamic、
timing、segmented 以及新进程回读全部通过。每个目标20条安装审计命令退出0。
各目标两份独立源副本构建的 wheel 字节一致；不同 OS/Python 的 wheel 摘要
可能不同，不要求跨目标成品 ZIP 字节相同，完整摘要在 acceptance JSON。

实际镜像为 ubuntu26 / 20261101.186.1、macos26 / 20261102.0473.1、
win25-vs2026 / 20261103.298.1，具体 OS/架构与包版本在每个原工件。
UTF-8/Agg 与子 CLI 文本编码配置显式记录，安装驱动保留 -I；没有测试所有
桌面 OS、所有 Python 版本、浏览器交互或硬件。该批数据均为软件/合成输入，
实际测量取得标志仍 false、参数证据升级列表为空、硬件 check 仍 unresolved。

四轮24个原始 GitHub ZIP 共6382个内部文件均建摘要索引，
前三轮失败与本轮成功分别存入 `record/first_wave.tar.gz` 至 `fourth_wave.tar.gz`，
每个外层归档再次逐文件回读校验；GitHub 三天工件过期不会丢失这些原件。
本机最终1495通过/2跳过（201.56 s），Ruff/298文件格式通过。
另一台干净机器的软件安装与规定的六目标 CI 门槛已有证据；完整工具链/许可、
实机、浏览器与阶段3–7其它未闭合项仍保留。

## 实现与逐轮历史

2026-11-13。新增 `.github/workflows/ci.yml`，采用 YAML 允许的 JSON 表示，
由标准库校验矩阵合同，避免新增 YAML 依赖。Linux/Windows/macOS 的 latest 标签各覆盖
Python 3.11 与 3.14；实际版本、架构、镜像信息单独记录，标签不等于平台环境锁。
仓库 Actions 读取返回 enabled=true / allowed_actions=all，初始运行数 0。
本地 GitHub 连接器实际返回 Unknown tool，使用已配置的 GitHub CLI 读取/追踪；未改仓库策略。

官方 release/commit 从实际 GitHub API 核查，action.yml 确认使用 Node24，
固定至 checkout v7.0.1 / 3d3c42e5aac5ba805825da76410c181273ba90b1、
setup-python v7.1.0 / 6d4fd22b20465ccc0f7354476588e8df7c9ca0e0、
upload-artifact v7.0.2 / cf430e030ddbb5b0abf93d22962f4752f3646cd9。
只授予 contents:read；push/PR 的相关源码/构建文件变化及手动事件触发。
fail-fast=false，避免一个平台失败后丢掉其它平台的观测；artifact 名含 run attempt，
保留旧失败工件。日志、JUnit、包/系统环境、wheel 与实际 installed 示例结果上传，保留 3 天。

每个一次性云 runner 在 checkout 前关闭自动行尾转换并启用长路径，以保持原始冻结资料
字节；不会改用户本机 Git 配置。显式 PYTHONUTF8=1、PYTHONIOENCODING=utf-8 和 Agg。
Linux 安装 Icarus 以执行现有 RTL 回归，记录实际工具版本；其它平台缺失工具的跳过如实列出。
不将 Linux 的工具可用性写成 Windows/macOS 的同项已通过。

`python -m tools.ci_runner` 记录每个真实命令、退出码与完整日志，任何失败退出非零。
执行 dev/legacy/ident 安装、pip check、Ruff/格式、完整 pytest/JUnit，随后从已验证的
631c542 离线包只提取纯 Python 后端三包；不提取/复用 macOS 运行 wheels。
各目标以 pyproject 解析自己的运行 wheels，再用两份哈希输入驱动 native wheel 审计，
在仓库外的新环境运行 static/dynamic/timing/segmented、短模拟/HTTP 及独立进程回读。
干净源固定 Git blob、全 Python/资源字节、两份源码的重复 wheel 字节比较继续启用。

最初需要联网解析 dev 和目标运行 wheels，之后的后端安装/构建与独立运行安装离线。
记录依赖不等于所有阶段预先锁定，CPython/ensurepip/pip/OS/dev/外部工具全锁仍未完成。
跨平台 wheel 字节并未要求相同；每个目标只要求其自身的两份独立源副本重复构建一致。
该工作流不做浏览器交互或硬件测量，不升级真实密度/分辨率/速度或板级证据。

首个真实运行将用于发现平台问题；存在工作流或本地合同通过不等于 CI 已通过。
本轮合同红灯为 CI helper 缺失；命令失败留证、错误 archive 摘要、只提取后端而不是 Mac
运行包、六目标/工件保留策略和 shell 真换行等 15 项定向检查已通过（1.61 s）。


## 首轮真实结果与修复中状态

[首轮 run 43355893600](https://github.com/additivefab/ScanSim/actions/runs/43355893600)，
提交 `1ae11ddba9d68d4d1ef5197ad93a8cacc9cb6b94`：六个 job 均已结束。
Ubuntu 两版本 1488 通过/4 跳过，macOS 两版本 1486 通过/6 跳过；
四者均继续通过独立 wheel 重复构建、安装与实际示例。Windows 两版本各有 43 个
失败/错误及 2 跳过，未到安装审计，因此首轮整体失败。
每个 job 的原始 ZIP、JUnit 与元数据保留在本轮 output，后续归档，不覆盖首轮负证据。

Windows 问题归类：18 个 HDF5 只读句柄 fsync、2 个下游同因退出、9 个 byte-0
强制锁挡读、7 个大 Node argv、4 个 pdftotext -bbox 退出 99 错误（首轮缺实际版本，非 Poppler 的推断未证实）、
2 个 POSIX 信号差异、1 个冻结 B 原套件 NPZ 句柄泄漏。后者原 14 测试含 2 个
清理错误，原件未改；仅 Windows 测试子进程透明输出原字节 BytesIO 适配标志。
修复合同见 D074。CI 新增三平台实际 Poppler 安装，四个 PDF 合同仍执行原断言。
两 POSIX 信号用例在 Windows 明确跳过；EOF 安全暂停/恢复继续跨平台执行。
Windows 控制台信号能力仍未验收。

新源码尚需新一轮实际六目标结果；本机定向检查不替代 Windows 原生执行。

修复后本机定向 45 项通过（41.68 s），全量 1493 通过/2 跳过（204.95 s），
两项为 Icarus 不可用；Ruff 与 298 文件格式检查通过。NPZ 适配路径另在本机
执行冻结 B 的全部 14 项断言通过，明确不当作 Windows 原生结果。


## 第二轮 Windows 负证据

[run 43359591080](https://github.com/additivefab/ScanSim/actions/runs/43359591080)，
提交 `cf9561072a6b69594a1f902ed53a2998a4b89df8`。Windows 两版本
各 1487 通过/4 跳过/4 setup 错误；四个错误仍是原 PDF 的 pdftotext -bbox 退出99。
写入锁/实时读/可写 fsync、Node 原断言、NPZ 适配及新跨进程合同均已原生通过；
四跳过是 Icarus 两项与 POSIX 信号两项，不能算作这些能力在 Windows 通过。
该轮仍未走到 Windows wheel 安装审计。已安装 Chocolatey Poppler26.9.0，
但此前 PATH 命令没有记录实际选择版本，故不能确认根因就是非 Poppler。
D075 改为绝对路径、提前版本/bbox 预检并显示原 stderr，仍保留原合同。

D075 本机定向 10 项通过（0.78 s），全量 1494 通过/2 跳过（211.59 s），
Ruff 与 298 文件格式检查通过。四个 PDF 合同本机实际执行，第三轮原生结果仍待返回。


## 第三轮：Windows pytest 通过，安装审计仍失败

[run 43361011838](https://github.com/additivefab/ScanSim/actions/runs/43361011838)，
提交 `342af7b74597fe088755ac467024084f1e58a572`。Linux 两版本1496/0跳过，
macOS两版本1494/2跳过，四者完整安装审计继续通过。Windows 两版本
1492通过/4跳过，无 pytest 失败；实际 Poppler26.09.0 的绝对路径版本/bbox
预检与四个 PDF 合同均通过。此前 PATH 选择的版本没有记录，不能反推旧99退出
来自哪个程序。

Windows 已完成 wheel 构建、两份源重复构建路径、隔离环境安装与 pip check；
随后 `wheel_smoke.py` 以 -I 运行，用 cp1252 默认编码读取 UTF-8 JSON/中文
管道失败。完整安装审计仍失败，没有执行后续全部示例。-I 忽略 PYTHONUTF8，
不是已安装包不存在，也不应撤掉隔离。D076 补显式文件/管道编码，并检查保存工作台
同类问题；新一轮原生结果仍待取得。

前三轮每个原 ZIP 的 GitHub digest/CRC 及逐文件归档摘要均已核对，见
`evidence_runs/2026-11-13_cross_platform_ci/record/`。

D076 新增用例最初的夹具前置字段断言有误，1失败/1494通过/2跳过的本机全量
原日志保留；修正为有效运行中的非 ASCII 附加元数据后，旧保存读取源码在
cp1252 默认条件明确触发 UnicodeDecodeError，新实现定向22项通过（5.10 s）。
最终全量1495通过/2跳过（201.56 s），Ruff/298文件格式检查通过。
15个实现/安装驱动经移除新增编码及子CLI编码环境关键字后的 AST 对照等价，
这是静态范围检查，不替代数值或原生 Windows 验收。
