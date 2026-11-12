# wheel 资源修复与同机隔离安装（S7-T05 部分）

2026-11-13。旧 `4dafc01` 的实际构建 wheel `zlscan-0.0.0-py3-none-any.whl`
不含 `zlscan/api/static/index.html`；其它四项运行资源在包内。
旧 wheel SHA256 为 `4a800a6c312c97b4059497a72cee28d84c9ab796fbe38e5c1633f3af2bdc53a0`。
新增资源合同测试得到 1 失败/4 通过，准确定位该遗漏；这次是实际压缩包
证据，不再沿用旧评审的风险推测。未称旧 wheel 的 HTTP 已独立执行。

修复：显式纳入 API 静态 HTML；所有五项运行资源均有打包声明覆盖测试。
包版本改为单一来源 `0.1.0.dev0`，setuptools 从 __version__ 提取；
ZLRAW、标定格式和物理模型版本均不因此变化。

新增 `tools/audit_wheel_install.py`：从 Git 跟踪文件的干净目录快照构建，
逐项比对 wheel 资源/版本；下载 wheelhouse 后生成版本+SHA256 全依赖锁，
在仓库外的新 venv 离线按锁安装基础运行及 ident，执行 `tools/wheel_smoke.py`
和独立静态辨识三案例。详见 [安装与复现](../install.md)。

开发试运行最初在验证脚本误以字典覆盖 http 模块处中断，未算通过；
已改用显式 HTTPConnection，并保留失败日志。修复后全流程重新执行通过：
20 个发行包离线哈希安装、pip check、5 项资源摘要、CLI/导入位置、
数字/短物理/标定/重建/报告、HTTP 页面与清单/manifest（均 200）、
三静态正/负案例及新进程重读。`check` 保留未决及退出码 4。

实际环境：macOS 15.7.6 / Python 3.14.7 / Python 进程 x86_64。
此为同机隔离 venv，未继承 system-site-packages，未从源码导入；
不是另一台干净机器、浏览器交互或 Windows/Linux 验收。
物理链 P0、其它阶段门槛、构建工具链/dev/外部工具全锁定、许可/CI
及跨平台交付继续保留。耗时/短模拟不作整机性能证明。
正式干净提交证据、配套目标锁及汇总在验证完成后附后。

开发环境全量 1245 通过、2 跳过（168.36 s，Icarus 不可用）；Ruff/格式通过。资源/CLI 定向 12 项通过（9.25 s）。隔离运行环境执行的是另列短流程/静态案例，不将开发全量数写成新依赖集合全量测试。
