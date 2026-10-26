# split_after 末尾空分组开发任务

来源：[more-itertools问题658](https://github.com/more-itertools/more-itertools/issues/658)。固定v8.13.0，commit `484401f5a056243be2508e2a6af598f7e4e99782`。repo保留原始错误代码；文件哈希在manifest，输入与独立检查也已冻结。

修复前新增10项检查5通过、5失败，原有4项针对测试通过。DeepSeek首轮9次调用修复成功，新增10项与原有4项全部通过，补丁在新副本重放通过；全量659/660通过，保留既有计时失败，无新增失败。

正式结果：[运行汇总](../../experiments/local_baseline_20261007/split-after-boundary/summary.json)、[补丁](../../experiments/local_baseline_20261007/split-after-boundary/repair.diff)。本题属于公开开发样本，不能替代独立留出集或SWE-bench。

从主项目根目录运行`python -B scripts/run_task_batch.py --task split-after-boundary`只做预检。加`--run`会请求模型，但本批次已关闭，入口会拒绝重复付费运行。新副本在本任务temp中，有独立Git起始提交；所有新模型调用共用主项目5元总账。
