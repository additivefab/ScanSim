# 三平台与最低/当前 Python CI（S7-T05）

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
强制锁挡读、7 个大 Node argv、4 个非 Poppler pdftotext -bbox 错误、
2 个 POSIX 信号差异、1 个冻结 B 原套件 NPZ 句柄泄漏。后者原 14 测试含 2 个
清理错误，原件未改；仅 Windows 测试子进程透明输出原字节 BytesIO 适配标志。
修复合同见 D074。CI 新增三平台实际 Poppler 安装，四个 PDF 合同仍执行原断言。
两 POSIX 信号用例在 Windows 明确跳过；EOF 安全暂停/恢复继续跨平台执行。
Windows 控制台信号能力仍未验收。

新源码尚需新一轮实际六目标结果；本机定向检查不替代 Windows 原生执行。

修复后本机定向 45 项通过（41.68 s），全量 1493 通过/2 跳过（204.95 s），
两项为 Icarus 不可用；Ruff 与 298 文件格式检查通过。NPZ 适配路径另在本机
执行冻结 B 的全部 14 项断言通过，明确不当作 Windows 原生结果。
