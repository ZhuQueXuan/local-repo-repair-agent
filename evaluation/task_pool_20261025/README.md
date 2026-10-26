# 扩题准备：一个JavaScript夹具与Python候选

2026-10-25。状态`offline_qualified_not_a_comparison_batch`。这是扩题与资格证据，不能直接启动付费批次；完整三角色真实验收、终态保留评分门控和新运行冻结仍待完成。既有六题准备包/751项原freeze不变。

## 已核验的JavaScript题

正式输入位于`../../tasks/fast-deep-equal-property-methods/`，fast-deep-equal3.1.3，MIT，npm gitHead`d807ffc5013e710deb1c63d463a03f729bcd144d`。问题来源[PR134](https://github.com/epoberezkin/fast-deep-equal/pull/134)、[PR142](https://github.com/epoberezkin/fast-deep-equal/pull/142)；`npm-source-metadata.json`保存公开包元数据，包SHA512与manifest绑定。原始发布index.js字节保留在`repo/lib/equal.js`，原LICENSE/README保留。

| 实际资格检查 | 原版 | 人工参考 |
| --- | --- | --- |
| 公开问题方法 | 0/2 | 2/2 |
| 保留问题方法 | 0/4 | 4/4 |
| 开发者编写的Node回归方法 | 4/4 | 4/4 |

本机Node24.21.0真正执行JS实现，Python只负责调用/收集；保护桥`fixture_bridge`和所有测试，候选只改`lib/`既有实现。未运行上游Mocha全套，没有npm install，没有真实Agent成绩，不能据此称通用JS/TS/Java支持已完成。人工参考在`reference.json`，保留方法在`evaluator/`，只用于开发侧资格/未来终态评分，不复制进Agent工作副本。现有本机Bash不是OS沙箱，不能保证对恶意读取父目录的程序保密。

完整红绿日志和副本记录见`../../temp/multi_agent_finish_20261025/js-qualification-7e2574c1258d46edb367b4d27f80968a/qualification.json`，正式副本随33成果封存；temp不是唯一留证。

## 尚未资格核验的Python候选

packaging的[873](https://github.com/pypa/packaging/issues/873)（wheel版本）、[934](https://github.com/pypa/packaging/issues/934)（marker比较）、[1399](https://github.com/pypa/packaging/issues/1399)（字符串转义）用于拓宽问题类别。目前没有固定修复前版本、确认许可证分发方案或运行原版/参考，不能作为可执行任务计数。

后续多语言题优先评估[SWE-PolyBench](https://github.com/amazon-science/SWE-PolyBench)、[Multi-SWE-Bench](https://github.com/multi-swe-bench/multi-swe-bench)、[SWE-bench Multilingual](https://www.swebench.com/multilingual)官方资源。需要逐项核对运行环境、数据许可和构建成本，本轮未下载这些题集或安装容器。用户选择的比较对象是最初单Agent与完整多Agent，S+不再是前置要求。
