# 未知终点物理录制 CLI

2026-11-13，S3-T11/S3-T14，D060。公共入口连接现有 PhysicalRecorder 统一根；
不预先给出总行数，不建立整次扫描同步列表/队列计划。原 physical/calibration/
有限双通道 --stream 路径保持原义，新模式叫 recording。

## 使用

示例 `configs/physical_recording.json` 全部为 ASSUMED 研究配置，含两个模拟通道。
它不是 ADC 本机配置、实测吞吐或参考探测器已安装的声明。
配置必须明确模型版本、预设、链路延迟/传播、采集窗、每级有限容量/速率、身份及
chunk_steps；物理组件/场景复用研究配置解析，未知硬件不被填数。
没有 lines 字段。可选有限物理时间线在最后阶段后沿用末阶段，遵守原 Chain 规则。

把下面两行保存为 `first.jsonl`：

```json
{"op":"lines","count":2}
{"op":"checkpoint"}
```

```sh
zlscan run --mode recording --config configs/physical_recording.json --commands first.jsonl --out recording_run
```

输入文件 EOF 会保存统一根并暂停，不排空。也可以用 --commands - 或省略 --commands
逐行读取 stdin。返回 JSONL，每个确认均包含提交根摘要和已提交细步/保留样本/丢失块计数。
输入总行数可随会话任意继续，无须重写配置：

```sh
zlscan run --mode recording --resume recording_run --commands next.jsonl
```

`next.jsonl` 可以是：

```json
{"op":"lines","count":2}
{"op":"finish"}
```

只有 finish 排空在途数据并进入 DONE；它是终止命令，后续输入不再执行。
原位恢复不再读取外部配置/场景文件，改用目录内的完整物理场景/数字定义快照。
原始输入及其摘要也保留。初次创建拒绝已有输出目录。

存在未知未提交尾部时原位拒绝；显式分支会只复制提交前缀，并完整保留故障目录：

```sh
zlscan run --mode recording --resume recording_run --out recovered_run --commands next.jsonl
```

分支路径表示文件恢复目标；源样本 run/branch/session 身份保持原值，不重新标记历史。
分支恢复目录必须不存在。失败中间目录不自动删除或复用。

## 命令与边界

| op | 内容与语义 |
|---|---|
| lines | 正整数 count；逐行生成物理同步，再分 chunk_steps 推进。必须在整数细步行边界；没有提前构造整幅同步表 |
| advance | 正整数 steps，可用十进制字符串；只推进物理源，不暗添行同步 |
| sync | ticks 列表，非负整数 ps 或十进制字符串；不能改写已推进时间 |
| request | channel、parameter、value、effective_step；可带 readback_step/readback_value。只支持已有 source.power_w 与 detector.gain，不伪造硬件读回 |
| reset/start | 原功能控制复位/重启，不重置模拟历史、源计数或在途数据；不代表真实硬件复位 |
| checkpoint | fsync raw/journal，原子发布统一根；不排空 |
| finish | 排空并结束；未来未生效控制命令仍会阻止结束 |

初始 open、每个成功命令、EOF 暂停都会发布/确认提交根。原始写入或检查点失败返回 1，
保留最后有效根及故障尾部，不发布部分物理状态。SIGINT/SIGTERM 在生产块边界暂停，
常见返回码为 130/143；空闲读命令时也可暂停。强制 kill 只保留最近已发布根。
命令确认不是精确一次网络协议：确认丢失后应检查统一根/细步/控制记录，再决定续发，
不能盲目重发已经生效的命令。

`run.json` 是最后提交快照：committed 不表示进程仍存活，paused 不表示 EOF 已排空，
complete 只表示生产/排空完成，不表示零丢样或物理指标通过。最终权威恢复点是
checkpoint.json；发布根后、更新人读摘要前崩溃，摘要可能较旧。
raw.zlraw 是 SIMULATED 可观测码流；events.jsonl/控制/根/定义/场景均为 ORACLE_ONLY。

## 内存范围

每次物理推进不超过 chunk_steps，输入命令至多 65,536 字节；未来同步默认最多 128 个，
控制事件默认最多 1024 个，均为可显式配置的软件资源限额，不是硬件参数。
超额明确拒绝，不丢弃控制历史，并预留重启/结束控制空间。
要求每级队列容量有限，消费者批次及时排出，原始载荷与诊断追加到磁盘。
固定场景本身、用户给出的有限阶段/注入列表及有界在途状态仍占内存。
恢复会线性扫描/校验磁盘提交前缀，不把它全量载入内存。
样本/行号/容器计数仍受其位宽限制；没有声称数学无限、任意大场景恒定内存或实机长片通过。

## 已运行验证

- 起点全量 1291 通过/2 跳过（84.15 s）。新入口合同红灯 15 失败；实现后 15 通过（7.42 s）。
- 扩展与既有物理记录/研究配置/导入边界合计 208 通过（36.66 s）；最终新增 20 项通过（33.92 s），
  包括实际子进程 stdin EOF、SIGINT 生产中暂停、SIGTERM 空闲暂停、恢复、未知尾部分支、
  非法定义/命令拒绝、注入磁盘错误后不发布部分根，以及控制预算保留重启/结束空间。
- 2000 次逐行生成、分块推进：早/中/晚检查点大小跨度小于 2048 B，晚期活跃 Python
  分配相对早期增长小于 512 KiB（容许解释器缓存）；这是给定小场景的 live-allocation
  回归，不是 OS RSS、磁盘缓存或任意输入峰值内存基准。
- `tools/audit_recording_cli.py` 实际公共 CLI 连续/暂停/新进程恢复；运行中 kill 子进程，
  等待退出后确认未知尾部原位拒绝、原件摘要不变，再从旧前缀分支继续。
  开发审计与新进程 --verify 均通过，连续/原位恢复/故障分支的 raw、journal、
  完整终态根逐字节一致。进程 kill 不等于物理断电或完整存储故障保证。

最终全量 1312 通过/2 跳过（116.21 s，Icarus 不可用），Ruff/格式通过（254 文件）。
干净提交正式复跑及 wheel 安装结果在本文件末尾更新。

仍缺探针迹线文件游标、recording 工作台加载、跨时期可观测元数据到密度重建、
真实硬件/复位及任意断电存储测试；此前带噪对数 P0 适用性限制照常保留。
这些缺口不因公共录制入口跑通而关闭，阶段 3–7 目标仍未全部达到。
