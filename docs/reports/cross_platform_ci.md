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
