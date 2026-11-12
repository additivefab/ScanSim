# 观测单调传递表及可辨识支持

2026-11-13，S4-T02，D051。新增可显式选择的全局静态经验传递模型，默认对数拟合保留。
该模型只使用原始参考的码值统计量、已知参考相对透射率和声明的不确定度，不读前向参数/状态。
它不是逐列平场，也不是有记忆链路或散粒噪声的无偏逐样本逆。

## 合同与实现

协议需要 `monotonic_code_half_width`、`monotonic_signal_bounds` 和 `monotonic_uncertainty_source`，
顺序为暗、白、阶梯。区间来源缺失即不可辨识，不从相关像素偷偷估出极小 SEM，不填未知硬件值。
同值参考（白与 D=0）要求码区间有交集；中心取中位数、区间保守取并集，原始索引保留。
不一致或非单调中心不强行重排；分离的相邻正参考才支持中心插值，重叠码/信号区间排除。
内部缺口不因 np.interp 能算数值就变成支持，暗端也不外推；无可用段时拒绝拟合。
每段同时保存参考区间与保守信号包络。中心假设是分段线性 code→log(T)，不是由有限点唯一确定的真实曲线。

LUT 值为 1+T，随后显式扣无量纲暗参考 1、按白参考 2 归一化。这只是观测参考的相对坐标定义，
不猜测 PMT 的真实暗电流，也不将对数下限当作线性暗电流。逐码线性化、归一化、合并后取密度的顺序保留。
参考的不确定度未传播成像素置信区间，噪声与动态偏差仍需独立实验。

新模型使用 calibration format_version=2；旧读者的版本检查应拒绝它，当前读者也拒绝把
相对归一化/码支持标成 v1。默认 log 文件继续为 v1，不放宽硬件/时期绑定或真值隔离。
CLI/原始码流重建已接入；几何标定可独立使用，但不自动额外估计逐面增益。

```bash
zlscan calibrate --raw RAW --protocol PROTOCOL --transfer-method monotonic --out NEW_DIRECTORY
```

参考插值状态不等于验收通过，工件仍标记 reference_assessment=not_assessed。
只有未参与拟合数据能检验静态偏差；不可辨识的码区间保留 OUTSIDE_CALIBRATION 与 NaN。

## 同数据独立检验设计

复用 `calibration_matched_controls.md` 的同一批 36 个 raw，不重新生成或挑选验证样本。
每个表中心使用与原对数拟合相同的参考统计量；每个案例的验证文件仍独立于参考。
确定性区间为明确的假设 ADC 舍入 ±0.5 码、精确合成靶标相对值。
噪声案例用其余独立参考采集的统计量离散度（非 SEM）及 Bonferroni t 倍数，最小半宽 0.5 码；
这是正态近似下的研究区间，尤其在删失/下限附近不能当作已验证的置信覆盖。
验证数据和标签不参与区间估计或插值；不因某个结果较好自动选择模型。
原对数结果是此前未验收候选的诊断，不将失败评估工件重新放行。

```bash
.venv/bin/python tools/audit_observed_transfer.py --source PRIOR_CONTROL_RUN --out NEW_DIRECTORY
.venv/bin/python tools/audit_observed_transfer.py --verify NEW_DIRECTORY
```

需要同时报告仅有效区残差、有效重复数和无效覆盖；整张标定不可辨识的实例计为无法重建，
不只统计留下的成功实例。尚不能凭本实验关闭 P0 高密度或实机门槛。
最新全量 1165 项通过、2 项 RTL 因 Icarus 不可用跳过（75.85 s）；Ruff/格式通过。
18 个原对数控制的重新拟合/重建保持一致；正式观测表结果待干净提交复跑后补入。
