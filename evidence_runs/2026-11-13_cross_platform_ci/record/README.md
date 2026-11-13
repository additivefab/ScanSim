# CI 原始证据归档

`first_wave.json` 至 `fourth_wave.json` 给出固定源码提交、实际 run、归档 SHA-256
及每个归档成员的字节数/摘要。对应 tar.gz 包含原始 GitHub artifact ZIP、
实际 API 元数据、原始日志及本机验证记录。归档解包内容均重新逐文件计算摘要。

`*_wave_members.json` 对每个原始 artifact ZIP 的内部文件单独建立摘要索引；
全部 ZIP CRC 检查通过，整个 ZIP 的摘要与实际 GitHub API 公布的 digest 一致。
展开后的目录是便于分析的本地副本；交付保存原 ZIP，不以重新打包的内容替代它。

第一轮 run 43355893600 / 1ae11dd：整体失败，四目标通过、两 Windows 各43错误。
第二轮 run 43359591080 / cf95610：整体失败，四目标通过、两 Windows 各4 PDF错误。
后轮结果不覆盖前轮负证据。精确用例数、跳过原因和 wheel 摘要在包内 results.json。

这些工件证明指定软件与环境的执行结果，不证明浏览器交互、实机参数或扫描性能。
GitHub 工件仅保留3天，本目录归档随仓库持久保留。

第三轮 run43361011838 / 342af7b：四目标通过，Windows pytest通过而隔离 smoke编码失败。
第四轮 run43363085674 / 6083281：六目标全通过，详见 fourth_acceptance.json。
共24个原 ZIP、6382个内部文件；19个外部运行依赖加项目 wheel 为20个运行锁项。
