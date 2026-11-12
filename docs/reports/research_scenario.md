# 版本化研究配置：预算、运行与保存重放

2026-11-13，S3-T11。只支持明确标为假设的研究场景，不修改设备硬件画像。
新增输入格式 `zlscan.research-scenario/1`，模型版本必须显式写为 `chain-0.3`。
示例在 `configs/research_scenario.json`；所有预设值和覆盖值都标 ASSUMED。
它仍是多面镜研究模型，不是已验证的 ZL80 工作点。

```sh
zlscan budget --config configs/research_scenario.json --out scenario_budget
zlscan run --mode physical --config configs/research_scenario.json --out scenario_run
zlscan serve --run scenario_run
zlscan replay --run scenario_run --start-step 11000 --steps 100 --out scenario_replay
zlscan run --mode physical --config scenario_run/inputs/resolved_scenario.json --out scenario_repeat
```

示例预算有未知硬件吞吐与内存项，返回 4；不是运行失败。预算存在 FAIL 时返回 1，
并保存诊断；禁止忽略状态只看命令是否生成文件。研究模式不因预测超限而改写输入。
`hardware_constrained` 拒绝该假设场景，返回 1；`check` 的真实硬件未知项不被补齐。

配置必须给出 `preset` 和正整数 `lines`。`chain` 可覆盖 ChainConfig 的既有标量、
脉冲和组件字段，组件内省略项沿用明确选择的研究预设；不得覆盖 evidence/scenes/timeline。
单位由字段名和组件合同规定：长度 m、功率 W、频率 Hz、波长 nm、电流 A、
电压 V、时间 s（sync_offset_ps 为 ps），光斑采用 FWHM。
严格拒绝未知字段、重复 JSON 键、非有限数、布尔冒充整数、字符串冒充数值、
不合法范围及不支持的模型版本；不静默夹紧或猜测硬件值。

`scenes` 可替换完整场景表，每项必须给 `width_m`、`height_m` 和 `off_film_T`。
数据源为二维非负 density 数组，或 `transmittance_npy` 与其 SHA256；
后者相对配置文件所在目录解析，只接受非空实数二维 0..1 数组，禁止 pickle。
载入以摘要对应的字节创建独立场景快照，运行不跟随源文件后续变化。
`timeline` 的阶段按完整行无间隙排列、不重叠、总长等于 lines，且引用已知场景。
没有 timeline 时沿用既有主场景规则。

`secondary` 可对主链覆盖参数，显式配置两个模拟通道；两链共享场景和时间线，
必须扫描、时钟与 ADC 编码一致。有随机行抖动时按现有引擎要求共用 seed；
独立/公共随机源的进一步分解仍未完成。第二通道不会自动被称为激光参考。
双通道仍走预先规划总行数的有限流式路径，不是未知长度在线采集。

`run --config` 与 --preset/--lines/--stream 互斥；不能用于 digital 模式。
配置运行拒绝覆盖已有输出目录。无 --config 的旧命令保持原行为。
calibration 模式仅接受一个通道，lines 表示每个标准参考靶的行数；
它以标准六靶场景/时间线替代输入场景，因此请求链哈希与实际运行哈希明确区分。
预算中的 lines 解释为物理样片行数，不是六靶标定总行数。

运行目录保留：

- `inputs/scenario.json`：用户输入原字节及 SHA256；
- `inputs/resolved_0/`（及双通道 resolved_1）：完整请求链、场景和内容指纹；
- `inputs/resolved_scenario.json`：展开全部组件参数、引用目录内场景的可运行配置；
- `run.json/scenario_configuration`：输入摘要、请求链哈希、实际配置哈希、
  单/双通道指纹、请求模式和行数语义。

自包含配置可复制随整个 inputs 目录重跑，不依赖原始外部 NPY 路径。
其输入字节摘要与原请求不同，所以配置的证据哈希可不同；相同物理条件与种子
仍应逐码一致。单通道工作台/CLI replay 使用实际运行的原配置和检查点，
不会重新套用预设或请求标定前的场景。双通道 saved/replay 入口仍未实现。

预算从同一解析结果投影名义多面镜几何、主通道光子统计和对数前端静态电平；
线性前端不套对数公式，相关行保留未知。第二通道只贡献通道数/数据率，
其不同光学参数不由主通道表代替。预算不包含 PSF、畸变、门控、时域恢复、
杂散/收光分布修正及排队细节；报告注明近似。未映射硬件项清为空值，
不继承设备表中的标称值或另收一套可冲突的物理覆盖值。
这不是物理模型的性能验收，也不关闭带噪对数的 P0 定量门槛。

验证记录在本文件末尾更新。浏览器交互、跨平台及独立实测不由 CLI 审计替代。


开发验证：起点全量 1258/2（83.11 s）；新增合同红灯 22 失败/5 通过。
主定向 199 项通过（8.93 s），包含双通道、参考标定、来源快照、无覆盖拒绝及
外部 NPY 改变后的自包含逐码复现。期间保留两次错误测试预期及修正说明：
超限预算应为 FAIL/1；参考标定用 fine_steps 和模式语义验证，不假设不存在的 lines 字段。
实际 CLI 开发审计及新进程重读通过：7,500 源采样、40 箱观测摘要，
自包含重跑与原码一致、检查点窗口一致，20 个原目录文件摘要不变。
这里的 5,589 个未支持源位置包括未保留采样，不能全部称为传输丢包。

第一次全量 1290/2（81.65 s）后，自查补极低采样率的零细步整行边界；
先以回归复现空列表 IndexError（1 失败/18 通过），再改为创建输出前明确拒绝。
最终全量 1291 通过/2 跳过（81.87 s，Icarus 不可用），Ruff/格式通过（251 文件）。
干净提交正式记录随后更新；不把审计工具成功误写为其超限负例通过。

正式复跑绑定干净实现 `53a3c9b6465adfd5c1a6f2730d1aa4a6e1b45169`：公共 CLI 审计/新进程重读一致，
预算、运行配置指纹一致；自包含重跑与原 7,500 采样逐码一致，检查点窗口一致，
20 个原目录文件摘要不变。正常预算未知项返回 4，超限/非法 ADC/硬件约束负例
各返回 1；这些失败结论被保留，并非因审计成功而消除。

同提交 wheel 在 checkout 外构建，20 发行包按配套哈希锁离线安装到新 venv；
五资源、实际 --config 预算/物理运行指纹一致、参考标定/重建/报告、HTTP、
保存运行加载、静态辨识正负案例及复读通过。wheel SHA256：
`2b277ce96493395a1039fa04a21ae13f2a03a8634fcab07d7fcd2c6f954c7d1b`。

配套记录/锁/轮子清单/CLI 响应摘要/开发全量与零细步修复前日志在
`evidence_runs/2026-11-13_scenario_config/record/`。旧 wheel 与锁均保留。
仅 macOS 15.7.6 / Python 3.14.7 / x86_64 同机隔离短流程，
不称另一机器、浏览器或硬件验收；未对新依赖集合声称全量测试通过。
