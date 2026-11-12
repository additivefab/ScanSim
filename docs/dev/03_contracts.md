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

`zlscan calibrate --timing` 可附同步注入观测 JSON。字段为 `raw_sha256`、整数皮秒 `ticks`、观测 `values` 及第 4 节的 `monitor` 事件。事件类型为 `injection_monitor`，须来自同一采集会话和时期的可观测传感器。标定结果带该文件校验值。重建用 ZLRAW 记录的采样时钟把延迟换成样本坐标偏移。

标定和重建的 `--quality` 接受 `{"raw_sha256": "...", "ranges": [{"event": {...}, "samples": 1}]}`。事件须可观测且携带 `related_sample`。样本数默认 1，范围从源样本序号起算。事件按会话、时期、时钟域及通道匹配到保留窗口，并按位合并饱和、限幅和丢样原因。标定遇到暗场、白场或用于拟合的阶梯内有无效样本时拒绝本次拟合。重建保留各原因并输出 NaN。输入码流与事件文件的校验值随结果保存。

逐面几何由 `calibrate --facets` 接入。文件包含 `timing`、`channel` 和 `grid`，可附 `slanted` 与 `slanted_dx_dy`。每个靶点记录采集行号 `line`、观测 ADC 窗内位置 `sample_column`（可为小数）以及已知靶标的 `target_x_m`。斜线靶的参考 x 取该行名义 y 对应的靶标位置。由操作者或上游检测器定位样本，标定程序按协议靶区、质量掩膜及同步证据核对后拟合。

`timing` 包含 `raw_sha256`、`policy` 和 `lines`。策略记录 `facets`、`period_ticks`、`tolerance_ticks` 与 `phase_reference`。每行提供 `line`、可观测的 `line_sync` 传感器事件 `sync`，以及可选的 `facet_index` 传感器事件 `anchor`。锚点须与同行同步绑定，时间相同，能力与策略的相位参考相同，`payload.phase` 为该参考的相位编号。缺行、超差或换时期后，需新锚点才能恢复相位。事件同步刻度换算到首个保留样本进行核对。

`reconstruct --facets` 接收样片自身的 `timing` 对象。标定中的采集窗及策略必须匹配。扫描向按逐面三次曲线计算，曲线只在标定靶点覆盖范围内有效。无相位或对应模型时置位 8（`FACET_UNRESOLVED`）。无可辨识斜线时保留声明的名义行坐标。逐面映射以原始 ADC 时间为自变量，已包含同一采集条件下的固定偏移，使用时不重复叠加独立延迟。

带逐面同步的白场还会生成 `facet_gain`。每组在剔除异常列后，以扣暗的线性白场幅度相对全局白场估计通道增益。重建在透射率域除以该组增益。若同一通道有逐面增益记录，但某个锁定相位缺少白参考，则标记白参考无效；没有可辨识的逐面白场时，标定保留全局白参考。

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

## 12. 运行记录

每次正式运行写一个 `run.json`：配置快照及校验值、代码提交号、随机种子、依赖版本、命令行、起止时间、输出文件校验值、指标、证据状态汇总、配置模式、预设名。
