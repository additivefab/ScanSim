# 示波器观测候选模型（S6-T02）

2026-11-13。仅研究候选模型和软件解析验证；没有取得仪器传递函数或实机波形。
这不修复 PMT—对数链的物理统计问题，不关闭 P0，也不升级密度能力。

```bash
zlscan ident --csv scope.csv --metadata scope_metadata.json --out compared_scope \
  --model-waveform model.csv --observer observer.json --observe-channel CH1
```

model.csv 提供 time_s/voltage_v 列，代表调用者的测点模型电压和模型时间坐标，
不是测量文件。三个附加参数成组使用，仅 `--csv` 支持；--observe-channel 只选
一个电压通道。未知配置模板在 `docs/examples/scope_observer.json`，必需参数
未填写时明确拒绝；模板本身不能用作已知仪器配置。配置有 parameter_source
说明数据来源，整个响应形状仍明确是研究近似，不因写了来源就成为实测传递函数。

模型顺序：探头理想衰减 → 探头一阶低通 → 可选 AC 一阶高通 → 示波器本体
一阶低通 → 带宽限制一阶低通 → 导出倍率补偿 → 采样。
probe_ratio 正值；probe_compensated 指导出是否已恢复测点倍率，显式选 true/false。
若 true，模型和测量都只恢复一次；若 false，预测对应未补偿的仪器输入幅值。
和测量声明中已知倍率、补偿、耦合或带宽限制冲突时拒绝，不悄悄覆盖。

probe_bandwidth_hz/scope_bandwidth_hz/bandwidth_limit_hz 均必须显式声明。
null 未知，0 明确理想旁路/关闭限制，正值为候选一阶截止频率；标称 −3 dB
带宽不能确定真实响应形状。限带与本体串联也是公开假设，不表示任何厂商的
实际电路。DC 不使用 AC 拐点；AC 必须明确正 ac_corner_hz。
不包含探头负载、仪器噪声、量化、时钟抖动或器件非线性。

源结点间显式线性插值。将所有极点组成状态空间后，以增广矩阵指数推进
各线性输入区间；包括相等极点，不把中间滤波输出再当折线来近似下一级。
initial_state 明确选 zero 或首输入 steady，不自动推断前史、预热或逐行重置。
源每增加结点只影响其声明的折线，不重新初始化响应。该算法不为未知硬件
生成时间常数，也不能替代源波形本身的数值收敛。

aperture_s=0 为点采样，正值为以采样时刻为中心的矩形平均；矩形孔径同样为
候选形状。各窗口从左端连续状态局部积分，避免大前缀积分相减导致精度损失。
正孔径使用浮点表示的左右端间距归一化以保留常数；若时间坐标精度已不足以
分辨孔径，返回空值及 aperture_below_time_resolution。孔径或点超出源支持
范围返回 outside_model_support，不夹边、不外推、不丢弃对应原始测量点。
每侧最多 2 万结点/采样、孔径积分最多 200 万区间；这是离线研究比较器，
未建立大波形吞吐或工作台实时指标。

时间按 model_time=scope_time+time_offset_s 映射，偏移必须显式声明，
不会从相关峰或残差自动拟合。alignment_uncertainty_s 未知时保持 null，
不能默认零；非空也只是用户声明，不是传播后的残差置信区间。输出的
nominal_residual_v 是测量导出量减模型预测，仅名义比较。元数据缺项及
采样间隔问题仍保留在外层导入报告；附件不能把这些问题变成硬验收通过。
导入的其他通道和 synchronization 状态不变，跨通道同步永远不由此建立；
非同次多路不会变成逐点同步波形。

附件记录模型波形/配置摘要、配置全文、插值方式、采样方式、无效原因、
模型标签及对齐不确定度。原始测量、原文件及输入配置不改。
始终 MODEL_ESTIMATE、hard_acceptance_eligible=false、parameter_upgrades=[]。
不把模型值写进实测数组，不把名义零残差当独立验证或可辨识性证据。
真实仪器响应、观测噪声、动态辨识与实测叠加验收仍需后续数据。

软件验证：全量 1225 通过、2 跳过（79.67 s，Icarus 不可用），Ruff/格式通过。观测/三类导入/CLI 定向 63 项通过（2.43 s）；解析响应和合成导入不是实机传递函数验证。

干净实现提交 `2906364` 的新进程定向复跑 63 项通过（2.26 s），记录于 `evidence_runs/2026-11-13_scope_observation/record/`。仅软件及解析验证，不是本机仪器传递函数验证。
