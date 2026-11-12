# 专用指标入口与评价约定

`zlscan metrics` 的原有双参考误差与完整性评价继续保留；增加可选 `--targets`，将
靶阶、斜边 MTF、均匀区噪声谱、逐面条带、几何与阶跃拖尾接入同一命令。

```bash
zlscan metrics --image reconstructed/density.npz \
  --truth evaluation_inputs/unblurred.npy --optical evaluation_inputs/optical.npy \
  --roi evaluation_inputs/roi.npy --targets evaluation_inputs/targets.json \
  --out quality_report
```

清单为 JSON；路径相对于清单所在目录，数组必须为无 pickle 的 `.npy`。
例（各项可单独使用，也可同次提交）：

```json
{
  "format": "zlscan-quality-targets/1",
  "evaluations": [
    {"name": "wedge", "kind": "wedge", "labels": "step_labels.npy",
     "levels_od": [0.2, 0.8, 1.4, 2.0], "max_abs_bias_od": 0.05, "max_std_od": 0.02},
    {"name": "scan_edge", "kind": "mtf", "direction": "scan",
     "region": "edge_region.npy", "distance_m": "edge_distance.npy",
     "bin_width_m": 0.0000125, "working_density_od": 1.0, "scan_speed_m_s": 500.0},
    {"name": "uniform_noise", "kind": "noise", "patches": [[2, 6, 3, 7], [6, 10, 3, 7]],
     "pitch_x_m": 0.002, "pitch_y_m": 0.002},
    {"name": "facet_bands", "kind": "banding", "facet_count": 4,
     "facet_phase": "observed_phase.npy", "edge_positions_m": "observed_edge.npy"},
    {"name": "grid", "kind": "geometry", "point_pixels": "grid_pixels.npy",
     "actual_xy_m": "observed_points.npy", "target_xy_m": "target_points.npy",
     "row_y_m": "observed_row_y.npy"},
    {"name": "tail", "kind": "step", "row": 4, "start_col": 2, "stop_col": 38,
     "edge_col": 10, "sample_period_s": 0.0000001, "tolerance_fraction": 0.02}
  ]
}
```

上述数字仅为清单格式示例，是**假设**，不代表本机硬件或产品要求。
MTF 的另一方向用 `direction: "feed"`，并提供该方向的法向距离数组。
每项都可增加 `region`（与密度图同形的布尔数组）。

`run.json` 包含两种参考的共掩膜误差、各项结果、输入文件 SHA-256；`mask.npz` 包含
全局 `region`、共有效 `valid` 和每项 `target_<name>` 掩膜。指标不把真值或靶标信息送入
标定与重建，整个报告标为 `contains_truth: true`。

各项支持规则：

- 靶阶 labels 是整数图，0 对应 levels_od[0]，范围外标签不参与。报告逐阶 OD 偏差、标准差、
  透射率信噪比和最高阶均值。零噪声或不足两点时不生成无穷大 SNR。
  没有显式阈值时可用范围为 null；某个中间阶未通过时不把两端连成连续可用范围。
- MTF 将 OD 转回线性透射率再求边沿响应，输出 cycles/m。靶片是否符合均匀半平面边沿、
  运动方向及工作密度由清单声明；缺连续距离箱或对比度不足时报告 not_computed。
- 噪声矩形为 [y0,y1,x0,x1]，右端不含；尺寸必须相同。只接受完全有效的矩形，输出两侧
  PSD（OD²·m²）和两轴频率。去各块均值，DC 为零；不能用缺样补零构造伪谱。
- 条带行均值由有效图像计算。facet_phase 为整数逐行向量，负值表示失锁；edge_positions_m
  是观测边沿逐行位置。输出逐面偏差及扣除组均值后的边缘粗糙度。
  全部声明的面均有观测时，另输出面周期谐波幅值与均方分量；缺面时周期谱明确不可计算。
- 几何 actual/target 是成对 N×2 物理坐标；point_pixels 是对应 N×2 [行,列] 整数索引，
  不在评价掩膜中的点不参与。row_y_m 是逐行位置，涉及行索引随报告保存；位置须有限且递增。
- 阶跃仅评价声明的一行连续区间，不插值跨过无效像素；输出尾面积、恢复样本数和恢复时间。
  像素序列的等效时间间隔必须由用户声明，不能从主机到达时间推导。

已有图像有效但某个请求指标无法计算时，仍保存报告与掩膜，并返回非零退出码。
文件缺失、格式错误、重复名称或畸形参数同样返回非零；已有输出目录不被覆盖。

解析回归包括两方向高斯 MTF、光斑×一阶带宽×采样孔径的组合 MTF、白噪声谱期望、
PSD 积分等于图像方差、已知偏差靶阶、线性网格偏移、周期条带和已知阶跃恢复。
这些是行为模型验证，不代表实机像质验收。
