# 03 数据合同

这些合同在阶段 0 落成 `src/zlscan/contracts/` 下的代码与测试。下面的字段是规格；Python 写法是示意，实现时可调整命名，但语义不得改。改语义要记入 DECISIONS.md。

**状态说明**：凡标"草案"的，须与 FPGA 侧共同冻结后才算定稿；在此之前可以实现，但格式版本号保持 `0.x`。

## 1. 带证据的参数

```python
class Evidence(StrEnum):
    PROJECT_TEXT = "project_text"      # 项目文本
    PHOTO = "photo_observation"        # 图片观察
    SCHEMATIC = "schematic_nominal"    # 原理图标称
    DATASHEET = "datasheet"            # 外部手册
    DERIVED = "derived"                # 设计推导
    ASSUMED = "assumed"                # 假设
    MEASURED = "measured"              # 实测

class Param(BaseModel, Generic[T]):
    value: T | None            # 未知为 None，绝不用 0 代替
    unit: str
    evidence: Evidence | None  # 值未知（None）时为 None；二者必须同时有或同时无
    source_id: str | None      # 资料编号，如 "S05"
    locator: str | None        # 页码、行号、图号
    uncertainty: float | None
    valid_for: str | None      # 适用范围
    measured_at: str | None
    method: str | None
    status: Literal["confirmed", "pending", "withdrawn"]
```

- 校验规则（`contracts/param.py`）：`value is None` 当且仅当 `evidence is None`（未知的值没有证据）；证据为项目文本、图片观察、原理图标称、外部手册时必须有 `source_id`；证据为实测时必须有 `measured_at` 与 `method`。
- `hardware_constrained` 模式下，`value is None` 或 `evidence == ASSUMED` 的必需参数导致校验失败。
- 派生量自动带 `DERIVED`，并记录它依赖哪些参数；依赖里有 `ASSUMED`，报告要显示出来。

## 2. 时间

- 全局时间刻度：64 位有符号整数，单位皮秒，类型名 `Tick`。范围约 ±9.2×10⁶ 秒，足够。
- 每个时钟域有：标识、标称频率（有理数）、相位、相对全局时间的映射。
- 周期性事件用"域内整数计数"表示，全局时刻由映射换算。周期不是整数皮秒时用相位累加器，不逐拍舍入。
- 不用浮点秒做时间主键。浮点秒只用于显示和物理计算的中间量。
- 传给浏览器的 `Tick` 用十进制字符串。

每个样本有四个时间，分别存放，不合并：

| 字段 | 含义 |
|---|---|
| `sample_time` | 模拟输入被采样的时刻 |
| `adc_output_time` | ADC 引脚上该样本有效的时刻 |
| `fpga_capture_time` | FPGA 捕获的时刻 |
| `host_arrival_time` | 主机收到的时刻；只描述传输，不作曝光时间 |

延迟分五类，各自建模、各计一次：纯传输延迟、滤波器相位（由状态推进自然产生）、ADC 孔径与流水线、数字接收延迟、队列与网络延迟。

## 3. 源样本身份

```python
class SampleId(NamedTuple):
    run_id: str
    branch_id: str
    session_id: int
    epoch: int
    clock_domain: int
    channel: int
    sample_index: int      # 该时钟域内的采样计数
```

行号、镜面号、包序号在复位后会重复，不能当主键。

功能采集 RTL 的源样本计数与 ZLRAW first_sample_index 统一为 64 位。复位清零时，协议包装层必须
创建新 epoch 并将中断时期标为不完整；单纯断连不能丢弃源计数高位。不得在主机把 64 位样本序号
转成 32 位临时值。行号仍为 32 位，计数耗尽前须结束或切换时期，不能静默回绕。

变换（抽取、聚合、压缩、重采样）要记录：输入样本范围、选点或权重规则、输出项。完整性检查看源样本身份的覆盖，而不是总数。

## 4. 事件

```python
class Origin(str, Enum):      SIMULATED = ...; MEASURED = ...
class Visibility(str, Enum):  OBSERVABLE = ...; ORACLE_ONLY = ...
class Derivation(str, Enum):  SENSOR = ...; INFERRED = ...; COMMAND = ...

class Event(BaseModel):
    kind: str                  # SOS、ENCODER、FACET_INDEX、OVERFLOW、LINE_END、CONFIG_APPLIED……
    origin: Origin
    visibility: Visibility
    derivation: Derivation
    clock_domain: int
    count: int                 # 域内计数
    tick: Tick                 # 换算后的全局时刻
    session_id: int
    epoch: int
    uncertainty_ticks: int | None
    capability_ref: str | None # 指向硬件画像里的能力声明
    related_sample: SampleId | None
    payload: dict
```

三个字段互相独立，不得合并成一个枚举。例：

- 仿真产生的行同步：`SIMULATED` + `OBSERVABLE` + `SENSOR`，可以交给重建，但不称为实机测得。
- 真值镜面号：`ORACLE_ONLY`，存入真值文件，重建不可读。
- 由行同步周期推断的镜面相位：`OBSERVABLE` + `INFERRED`，允许失锁。
- 控制命令：`COMMAND`，请求时刻、生效时刻、读回确认时刻分三条事件记录。

`OBSERVABLE` + `SENSOR` 事件要有 `capability_ref`；硬件画像里没有声明该传感器存在时，`hardware_constrained` 模式拒绝生成这类事件。

## 5. 原始码流（草案，`ZLRAW 0.1`）

一个采集会话由一个 JSON 会话头和若干二进制数据块组成。数据块小端序。

**会话头（JSON）**

```text
schema_version, session_id, created_at
hardware_hash, firmware_hash, wiring_rev, register_dump_hash, calibration_id
channel_roles            {0: "signal" | "reference" | "unused" | null, 1: …}
clock_domains            [{id, nominal_hz (分子/分母), description}]
coding                   "offset_binary" | "twos_complement"
bits_per_sample, bytes_per_sample
value_domain             "raw_code"   # 见第 8 节
windows                  光学窗口、数字保留窗口的定义
origin                   "SIMULATED" | "MEASURED"
```

**数据块头（固定 64 字节）**

| 偏移 | 长度 | 字段 |
|---:|---:|---|
| 0 | 4 | 魔数 `ZLR1` |
| 4 | 2 | 格式版本 |
| 6 | 2 | 头长度（=64） |
| 8 | 8 | `session_id` |
| 16 | 4 | `epoch` |
| 20 | 4 | 块序号 |
| 24 | 2 | 通道掩码 |
| 26 | 2 | 时钟域 |
| 28 | 2 | 标志位（行首、行末、帧首、帧末、溢出后首块……） |
| 30 | 2 | 保留，写 0 |
| 32 | 8 | 首样本的 `sample_index` |
| 40 | 4 | 样本数（每通道） |
| 44 | 4 | 载荷字节数 |
| 48 | 4 | 行号（会话内，仅供参考，不作主键） |
| 52 | 4 | 载荷 CRC-32 |
| 56 | 4 | 保留，写 0 |
| 60 | 4 | 头 CRC-32（覆盖 0–59 字节） |

载荷：通道交织的定宽样本。

**读取规则**：魔数错、版本未知、长度不符、CRC 错、块序号倒退或跳变、时期变化而无对应事件——一律报错或生成丢失记录，不静默拼接。

**兼容输入**：包 B 的无头格式（行优先、小端、16 位、仅保留区）通过适配器读入，适配器要求显式给出尺寸与码制。

**丢失记录**：`{layer, first_sample_index, last_sample_index, reason, certainty: "exact" | "estimated"}`。

CRC 只保护生成点之后的载荷，不证明 ADC 采样正确。

## 6. 标定文件

```text
header:
  format_version
  provenance          "MODEL" | "SIMULATED" | "MEASURED"
  calibration_id, created_at
  binds_to            {simulation_hash, hardware_hash, wiring_rev, firmware_hash, register_dump_hash,
                       channel_roles, gain_settings, epoch_range}
  valid_conditions    温度范围、扫描方向、有效时间等
  generator           算法名称与版本、输入数据的校验值
items（每项带 value、uncertainty、valid_range、method）:
  dark                逐列或逐列逐面
  white               逐列逐面
  transfer_lut        码值 → 相对光强；含外推策略
  delay               固定延迟（刻度）与标签对齐量
  geometry_x          逐面 x(t) 映射
  geometry_y          逐面 y 偏移
  facet_gain
```

- `recon` 默认拒绝 `MODEL`；只有显式传入 `allow_model_calibration=True`（仅 `legacy_*` 预设与专门的兼容测试使用）才接受。
- 研究模拟可用独立的 `simulation_hash` 标识采集配置。实测文件须匹配硬件、固件、接线和寄存器标识。两种模式均核对通道角色、增益与采集时期范围。
- 应用前检查 `binds_to` 与当前会话是否匹配；不匹配时拒绝，或在显式的研究开关下继续并在输出中标记。
- 某一项无法从给定数据估计时（例如用均匀场估送片向偏移），该项写 `status: "unidentifiable"`，不写数值。

`zlscan calibrate --timing` 可附同步注入观测 JSON。字段为 `raw_sha256`、整数皮秒 `ticks`、观测 `values` 及第 4 节的 `monitor` 事件。事件类型为 `injection_monitor`，须来自同一采集会话和时期的可观测传感器。标定结果带该文件校验值。双通道注入须声明 `channel`，重建仅将该通道的延迟按 ZLRAW 采样时钟换算成坐标偏移。

`calibrate --edge-registration` 使用边沿靶区实采码值与协议中的名义靶位拟合光学配准量，单独记录在 `optical_registration`。不具备可辨识边沿的通道记录原因。重建使用的是可观测独立注入产生的 `delay`，光学配准量只作诊断。

标定和重建的 `--quality` 接受 `{"raw_sha256": "...", "ranges": [{"event": {...}, "samples": 1}]}`。事件须可观测且携带 `related_sample`。样本数默认 1，范围从源样本序号起算。事件按会话、时期、时钟域及通道匹配到保留窗口，并按位合并饱和、限幅和丢样原因。标定遇到暗场、白场或用于拟合的阶梯内有无效样本时拒绝本次拟合。重建保留各原因并输出 NaN。输入码流与事件文件的校验值随结果保存。

逐面几何由 `calibrate --facets` 接入。文件包含 `timing`、`channel` 和 `grid`，可附 `slanted` 与 `slanted_dx_dy`。每个靶点记录采集行号 `line`、观测 ADC 窗内位置 `sample_column`（可为小数）以及已知靶标的 `target_x_m`。斜线靶的参考 x 取该行名义 y 对应的靶标位置。由操作者或上游检测器定位样本，标定程序按协议靶区、质量掩膜及同步证据核对后拟合。省略 `grid` 时，从协议给定的网格刻线附近自动检测暗条中心，取两侧半对比度交点的中点。搜索窗有告警、无分辨对比度或缺任一交点时，该条不进入拟合。

`timing` 包含 `raw_sha256`、`policy` 和 `lines`。策略记录 `facets`、`period_ticks`、`tolerance_ticks` 与 `phase_reference`。每行提供 `line`、可观测的 `line_sync` 传感器事件 `sync`，以及可选的 `facet_index` 传感器事件 `anchor`。锚点须与同行同步绑定，时间相同，能力与策略的相位参考相同，`payload.phase` 为该参考的相位编号。缺行、超差或换时期后，需新锚点才能恢复相位。事件同步刻度换算到首个保留样本进行核对。

`reconstruct --facets` 接收样片自身的 `timing` 对象。标定中的采集窗及策略必须匹配。扫描向按逐面三次曲线计算，曲线只在标定靶点覆盖范围内有效。无相位或对应模型时置位 8（`FACET_UNRESOLVED`）。无可辨识斜线时保留声明的名义行坐标。逐面映射以原始 ADC 时间为自变量，已包含同一采集条件下的固定偏移，使用时不重复叠加独立延迟。

带逐面同步的白场还会生成 `facet_gain`。每组在剔除异常列后，以扣暗的线性白场幅度相对全局白场估计通道增益。重建在透射率域除以该组增益。若同一通道有逐面增益记录，但某个锁定相位缺少白参考，则标记白参考无效；没有可辨识的逐面白场时，标定保留全局白参考。

密度重建对 ZLRAW 做两次逐块校验。第一次保留块元数据供时序与质量核对，第二次逐行投影。无头 ROI 按声明宽度逐行读取。输出行使用可随机寻址的磁盘暂存，因而支持非单调的送片坐标；最终逐行生成兼容原接口的 `density.npz`。帧级像素数组不常驻内存，块、行和质量事件元数据仍随输入规模增长。

### 6.1 对数拟合的参考再现诊断（S4-T02）

传递拟合保存参考的 T、观测/拟合码及逐点残差，顺序为暗、白、已知阶梯。
这描述参考再现误差，不等于独立验证误差、参数不确定度或实机准确度。
协议可显式声明 `transfer_reference_tolerance_code`（有限非负码值），未提供时状态为
`not_assessed`，不得默认为参考验收通过。提供时逐点绝对残差均在容差内为 `compatible`，
否则 `mismatch`，保存超限索引及最大残差。容差来自协议，不从仿真真参数或观测残差反推。
这是拟合解在给定容差下的检查，不宣称证明该模型族不存在其他可行解。

标定可保留不通过的拟合及诊断供检查；CLI 写出诊断后返回非零。重建拒绝使用所选通道中
明确 mismatch 的传递，不允许改成 MODEL 放行或只删掉坏参考点。缺少诊断的旧文件维持兼容，
新文件不声明容差则明确未评估，独立验证仍是另一门槛。拟合生成器升级到 v2，避免同源协议
的新旧诊断工件沿用相同标定 ID。新增字段不改变对数拟合公式或默认前向模型。

### 6.2 观测单调传递表

`calibrate --transfer-method monotonic` 为显式选择，默认仍是 log。只从暗、白及已知参考靶的
码值统计量建立通道级全局静态表；不读取前向模型参数/状态，不估计逐列平场或动态逆模型。
必须声明每个参考统计量的 `monotonic_code_half_width` 及参考相对透射率的
`monotonic_signal_bounds`，顺序为暗、白、阶梯；`monotonic_uncertainty_source` 必须给出来源解释。未取得不确定度就报告不可辨识，不默认为零。
这些是声明的区间，不自动当成标准差或像素噪声；来源与覆盖解释须随协议保存。

相同参考相对透射率（如白与 D=0）可合并，码区间必须相交；中心取中位数，区间保守取并集，
保留原始观测。矛盾参考或非单调中心不强行排序修正。相邻正透射率参考的码区间及信号区间
均分离时才支持插值；重叠边标为不可辨识，码区间闭集排除，不能跨越缺口。无可用段则拒绝。
中心插值是 code→log(T) 的分段线性假设，输出 LUT 的值为 1+T，以保持正线性信号合同；
1 是无量纲坐标偏移，不是猜测真实暗电流。归一化显式使用暗=1、白=2，只是观测参考定义，
不声称具有逐列平场校正。重建仍逐码线性化、扣暗归一化、合并后取密度。

不外推到最暗正参考以下，也不把暗端不可辨识区插成有限密度。传递表带 valid_code_ranges
和 excluded_code_ranges；表间的空隙即使 np.interp 能产生数值，也必须标为 OUTSIDE_CALIBRATION。
同时保留参考区间、未辨识边及各有效段的信号包络；不把参考不确定度声称为已传播的像素置信区间。
插值能再现参考不是独立验证。新模式使用标定文件 format_version=2，显式要求相对归一化和码支持字段；旧读者应拒绝未知版本，
当前读者也拒绝将这些语义标成 v1，防止老代码忽略缺口。默认 log 文件仍是 v1。
默认状态仍未独立验收；需要未参与拟合的数据检验静态偏差和覆盖，
有噪声/记忆时不能假定这是逐样本无偏逆。旧式对数容差检查不应用到该模式。

## 7. 随机数

- 白噪声与局部事件：密钥为（种子、物理源标识、时钟域、样本或细步序号），用计数器型生成器（`numpy.random.Philox` 或等价物）按块生成。同一序号永远得到同一随机数，与分块、重放、并行度无关。
- 有色过程（慢漂移、转速游动、1/f）：用带状态的递推，状态进检查点。
- 每个噪声源的配置写明单位，以及是谱密度还是某时间尺度上的积分方差。
- 同一路径上，已由计数抽样产生的散粒噪声不再叠加同源的高斯近似。

## 8. 数值域与图像输出

四种数值域分别定义、分别存储：`raw_code`、`linear`（线性信号或透射率）、`density`、`display`。每条数据流声明数值域、单位、位宽、标定标识、合并权重、无效数据策略。

图像输出必须随带：

| 项 | 内容 |
|---|---|
| 单位与数值域 | |
| 像素间距与坐标原点 | |
| 无效掩膜 | 按位的原因标志，见下 |
| 有效覆盖比例 | |
| 算法版本、标定标识 | |

无效原因标志（可同时置位）：

| 位 | 含义 |
|---:|---|
| 0 | 扣暗后透射率非正 |
| 1 | 白参考无效 |
| 2 | 超出标定查找表范围 |
| 3 | ADC 饱和 |
| 4 | PMT 或前端限幅告警 |
| 5 | 缺样（传输丢失） |
| 6 | 重采样支持不足 |
| 7 | 标定与会话不匹配 |

无效像素的密度值写 `NaN`，不写夹紧后的数。展示用的缺口填补是单独的文件。

## 9. 信号注册表

每条可显示的信号登记一次：

```text
signal_id, component_id, port_id
physical_domain      optical | analog | digital | mechanical | control
quantity, unit
value_type           scalar | vector | event | logic | sample_block
origin, visibility, derivation        # 同第 4 节
parameter_evidence   该信号依赖的参数里最弱的证据等级
model_id, model_version
clock_domain, native_step_policy
time_support, alignment_uncertainty
display_interpolation   none | hold | linear
recording_policy        full | summary | on_demand | none
```

## 10. 查询返回状态

任何对迹线或状态的查询，结果都带一个状态：

| 状态 | 含义 |
|---|---|
| `exact_record` | 来自原始记录 |
| `replayed` | 从检查点重放得到 |
| `interpolated` | 插值得到，注明方法 |
| `summary_only` | 只有摘要（最小、最大、首、末、计数） |
| `not_recorded` | 没有数据，前端显示空白 |
| `not_computed` | 该时间范围尚未仿真 |

## 11. 检查点内容

模拟状态；运动与漂移；随机过程状态；事件队列；各类计数；ADC 流水线在途样本；所有数字队列；数据格式的时期；写盘游标；控制命令日志位置；模型版本；配置校验值。

缺任何一项都可能使恢复后的运行偏离连续运行，所以"从任一检查点恢复后与不中断运行一致"是硬性测试。

### 11.1 数字传输状态（S3-T14）

传输检查点必须保存各级 FIFO 及入队时刻、当前服务项、下游阻塞项、字节占用、
待处理完成/到达事件堆、同刻事件序号、已经处理的时间水位、累计统计和尚未取走的输出。
同刻先处理到达，再处理服务完成；同类按入堆序号执行。该顺序保持原批处理语义，
不依赖未来到达事件提前多久提交。推进截止包括该时刻全部事件；恢复不重新编号。
传输检查点格式升级到 /2，旧 /1 需要其原提交运行，不能按新排序静默恢复。
保存与读取不改变原引擎，恢复需要同版本且完全一致的级联配置，不接受损坏校验和。

增量接口在推进到 t 后只接受到达时刻大于 t 的新输入；同刻输入必须在推进前一起提交。
每批 item_id 唯一，后续批次的 ID 高于此前最大 ID，避免长时间流式运行保留无限去重集合。
服务时间向上取整到整数 ps；分数 B/s 不得先截断成整数 B/s。
`drain()` 取走已交付/丢失记录与占用轨迹，保留队列、待处理事件和累计峰值/最大等待/溢出次数。
调用者必须持久化这些输出和写盘位置，单独的传输检查点不等于整链原子检查点。

### 11.2 ADC 输出链路状态（S3-T14）

链路检查点保存尚未到捕获边的码字与四时刻、源 run/branch/session/epoch、下一个源序号、
溢出标志偏移所需的历史/前瞻、已就绪但未取走的输出、时钟水位及输入结束状态。
`push` 接受连续源序号；时期改变不清空旧在途样本。源身份、通道顺序和时钟不能悄悄切换。
已关闭时间水位之前的输入被拒绝。新接口需要显式的延迟与传播值，未知值不当作零。

`advance(t)` 只发布捕获时刻不超过 t 且溢出标志已可确定的样本；缺少负偏移所需前瞻时
拒绝该推进，保持状态不变。调用 `finish()` 明确有限输入结束后，首尾未覆盖的溢出来源
按旧批处理值 False 保存，但 `overflow_valid=False`，不伪造边界观测；批处理和流式采用
同一边界语义。接收端校正偏移不允许使用循环回绕，将首段标志接到尾段。
ADC 流水线输出和上升/下降捕获边，均按时钟的完整半周期计数计算，再向下取整到 ps；
不得把两个已取整区间相加。有理数周期下两者可能相差 1 ps。超出有符号 64 位 Tick 则拒绝。

检查点绑定模型版本、链路配置、时钟和通道，内容校验失败即拒绝。源序号与时刻以
十进制字符串保存在检查点，避免跨工具转换丢失大整数。此组件不等于原子整链检查点。

### 11.3 数字采集状态（S3-T14）

增量采集保存未来行同步及排序序号、重叠窗 busy 截止、下一行号、未完成窗及观测数据片段、
已结束未取的窗口/忽略同步/跳过行、水位与下一源样本序号。按源格点的兼容模式必须与
现有 `acquire` 一致；显式捕获时钟模式按真实 `capture_time` 打开 FPGA 窗口，不能把
捕获边计数冒充源样本序号。上升/下降沿格点均按同一有理数半周期模型计算。

一个窗口可包含不同时期的片段，各片段保留源身份和时期，不统一改为窗口结束时的时期。
源输入/同步不能倒写已关闭时刻；首个保留位置在初始数据之前的窗标为跳过，有限输入
尾部的未完成窗明确截断，后续行号不能顶替。未取结果可 drain；调用者负责保存。
原子整链检查点、硬件复位与原始文件写盘游标仍需另外实现和验证。

### 11.4 文件提交与续写（S3-T14）

ZLRAW 写入检查点包含语义会话哈希、下一块序号、已刷盘的字节位置和该前缀 SHA-256。
创建新文件不得静默覆盖现有文件；恢复只能在取得独占写锁、核对前缀/会话/块边界及序号后追加。
少于提交位置、前缀损坏、会话不同或存在未知尾部时拒绝原地恢复，不能默默截断。
调用者可明确提供新的分支路径，只复制有效已提交前缀，保留原文件及其尾部不动。

数字记录器把管线状态、ZLRAW 游标、追加诊断日志游标及最后已写时期放入同一个检查点。
先 flush/fsync 两个数据文件，再生成配对状态；发布检查点文件采用同目录临时文件和原子替换。
实际写盘失败后禁止继续发布新检查点。恢复旧检查点时，未知尾部默认拒绝，或复制到新分支
再重放，不能在已有尾部后重复写旧包。原始 ADC 文件保持可观测代码；诊断日志含模型队列占用/精确损失等内部状态，
显式标记 SIMULATED/ORACLE_ONLY，禁止作为标定或重建事件输入。
这些软件检查不等于真实断电/存储硬件耐久性认证；模拟链与控制状态仍需另外组合。

## 12. 运行记录

每次正式运行写一个 `run.json`：配置快照及校验值、代码提交号、随机种子、依赖版本、命令行、起止时间、输出文件校验值、指标、证据状态汇总、配置模式、预设名。

### 11.5 控制历史检查点

控制检查点保存状态机、完整命令/复位/状态日志及其位置、时期和下一命令 ID；
恢复必须能从日志重演出同一状态和时期，校验和正确但内部历史不一致仍拒绝。
命令请求/生效/读回使用同一 ID，读回不得早于生效；同值的新请求不能继承旧确认。
控制操作按请求/状态/复位时刻非递减声明；未来生效及读回记录不改变操作水位。
所有拒绝必须在写日志/改状态之前完成。配置值复制保存，不受调用方之后的容器修改影响。
时刻为有符号 64 位 Tick，持久化为十进制字符串。命令可含未来生效/读回声明，
`readback_confirmed(..., at_tick=...)` 只查该时刻以前已声明的读回；未传时刻查看全部声明，
不能将其解释成实际硬件已确认。复位不得倒插到已声明的未来配置之前，暂不提供取消计划接口。
控制检查点仍是功能状态机证据，未接入物理参数生效或完整系统原子检查点。

按采样时刻切分时期时，对每条逐样本元数据同步切片；标量描述保留，长度不匹配的数组拒绝。

### 11.6 物理记录运行器的统一恢复根

`PhysicalRecorder` 独占推进一个或多个共用采样/扫描时基的物理通道，组合连续模拟状态、
计数器随机源身份、控制历史与未来命令、数字子链/真实在途载荷、已提交 raw/诊断日志游标。
所有推进/命令/检查点经过同一互斥边界；两个数据文件 fsync 后只发布一个外层检查点。
恢复校验基础配置、模型版本、当前生效命令与模拟配置、细步/下一样本边界、控制及数字身份。
拒绝检查点损坏、内部错配和未提交尾部，显式分支只复制提交前缀且保留原件。

参数命令当前只支持逐通道 `source.power_w` 和 `detector.gain`，实际在声明的整数细步
前更新对应模型配置；不重置前端或输入响应。请求在当前细步声明，生效按非递减细步计划，
与 ADC 采样同一步时命令先生效；检查点保存尚未生效命令。读回是显式研究声明，不是实测。
暂不支持的参数直接拒绝，不能只换时期标签而不更新模型。

`reset_control()` 明确只是逻辑控制复位：新时期、受中断时期标记，模拟参数/历史和数字在途
数据保留；`start()` 恢复功能扫描状态，未建立真实器件稳定时间。存在未来生效命令时拒绝复位，
不静默取消或推迟。源计数继续，数字旧时期数据不改写为新时期。硬件复位仍未验证。
记录器尚不包含探针迹线文件游标，主 CLI/工作台需继续接入，不据此关闭完整系统门槛。

## 示波器导入合同 v1（S6-T01，部分）

`ident --csv ... --metadata ... --out ...` 读取严格表头 CSV 与 JSON 声明。
显式指定 time_column/time_unit(s/ms/us/ns)、各通道 column/unit(V/mV/uV/A/mA/uA)；
转换仅为 SI 单位，不重复应用探头倍率；probe_compensated 必须声明才能具备完整元数据。
设备、测点、倍率、耦合、带宽限制（0 明确表示关闭，null 未知）、采样间隔、触发、
时间原点、session/epoch、同次采集及 acquisition_id 缺失时保留 null 并列出缺项。
CSV 时间严格递增、值有限；不插值、不对齐。声明采样间隔以显式绝对容差检查，
不一致只允许查看。只有完整元数据、相同非空 acquisition_id 与同次声明，才标为
声明同次采集（不是独立核验）；其他多路不赋予同步。
导入永远不自动升级参数或通过硬验收。保存原文件 SHA256、声明及 SI 数据；不覆盖既有输出。
当前仅示波器 CSV；FPGA 原始码流与测量模板另行适配，不算 S6-T01 全部完成。

## ZLRAW 导入合同 v1（S6-T01）

`ident --raw ... --metadata ... --out ...` 仅接受当前 ZLRAW 0.1 草案，不猜未知 FPGA
布局。输出目录独占创建；先逐字节复制为 raw.zlraw 快照，对该副本计算 SHA256、
逐块 CRC/身份检查及有限预览，不修改原件、不改编码或时钟/时期/64 位源索引。
run.json 保存原 session、块索引、错误及缺样；坏 CRC 块不进入预览。
不可读文件也保留副本与诊断，CLI 返回失败；可读但有缺口仅供查看，不补齐。
同步仅在声明完整且单时钟域、每块包含全部声明通道、身份一致且完整性无问题时
标为 declared_same_acquisition。其他状态不强行对齐，不由相同索引推断同步。
原头 origin 与侧车来源分别保留；SIMULATED 不能被侧车 declared_measurement 覆盖。
硬验收永远 false、参数升级为空。没有预期起止样本时只声明 seen_range_checked，
不证明文件首尾无损失或本机 FPGA 协议已冻结。预览最多 256 行，块索引仍随块数增长，
该适配器不声称未知长度采集内存有界。
