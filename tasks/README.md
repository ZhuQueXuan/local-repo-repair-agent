# 本地修复任务与用途

这些任务固定修复前的公开仓库源码、问题描述和独立行为检查，用于本项目的开发与小样本评测。它们不是 SWE-bench，也不是保证模型训练未见过的私有题。模型能读取工作副本中的检查；评估使用模型不能修改的冻结原件，并要求保护上游测试和在新副本重放补丁。

| 目录 | 类型 | 本项目用途 | 来源 |
| --- | --- | --- | --- |
| `windowed-empty` | 空输入边界 | 开发与回归；已参与调试 | [more-itertools #462](https://github.com/more-itertools/more-itertools/issues/462) |
| `split-after-boundary` | 分割边界 | 开发与回归；已参与调试 | [more-itertools #658](https://github.com/more-itertools/more-itertools/issues/658) |
| `indexed-set-removal` | 集合删除后的切片 | 开发与回归；已参与策略调试 | [boltons #423](https://github.com/mahmoud/boltons/pull/423) |
| `omd-copy` | 复制协议、多值与对象引用 | 13轮首次新题评测；15轮已用于进展反馈设计，后续为开发/回归 | [boltons #410](https://github.com/mahmoud/boltons/pull/410) |
| `file-pathlike` | 文件接口与PathLike兼容 | 13轮首次新题评测；15轮已用于进展反馈设计，后续为开发/回归 | [boltons #383](https://github.com/mahmoud/boltons/issues/383)、[关联修复](https://github.com/mahmoud/boltons/pull/416) |
| `requests-json-pickle` | 响应解析、异常与JSON兼容层的序列化边界 | 17轮新增不同仓库的公开开发题，关闭/开启检索均修复并重放通过；后续为开发/回归 | [Requests #6628](https://github.com/psf/requests/issues/6628) |
| `click-shared-parameter` | 参数装饰器复用与自定义类型 | 18轮固定检索首版后的新题验证；两版修复/重放通过，后续若调参则转开发/回归 | [Click #2294](https://github.com/pallets/click/issues/2294) |
| `click-custom-command` | 父级注册、自定义类型与命令构造 | 18轮新题验证；两版修复/重放通过，检索无调用收益且输入增加 | [Click #2416](https://github.com/pallets/click/issues/2416) |
| `barrel-sort` | 大容器插入后排序 | 18轮新题验证；两版修复/重放通过，先通过公共insert触发拆分 | [boltons发布记录](https://github.com/mahmoud/boltons/blob/master/CHANGELOG.md#2600) |
| `fast-deep-equal-property-methods` | JavaScript普通方法名属性及无原型对象 | 33轮新语言离线红绿资格；真实Node执行、保护Python桥，无模型成绩；不是上游Mocha全套 | [PR134](https://github.com/epoberezkin/fast-deep-equal/pull/134)、[PR142](https://github.com/epoberezkin/fast-deep-equal/pull/142) |

每题的 `manifest.json` 记录仓库版本、源码哈希、描述与检查哈希、验收命令及评测范围；`repo/` 是正式冻结输入，`temp/` 保存一次性工作副本。不能在冻结原件上直接修复。

新题的四次运行及其顺序在 `../configs/new_task_evaluation_20261010.json` 中预先声明。使用同一DeepSeek模型、300秒时限和共享15元累计预算，原版适配基线与上一轮候选各运行一次。不根据本轮结果调整策略或重跑选优；程序检查307个输入的哈希，运行前校验、运行后复核。

以上15元是13轮的历史条件。当前费用上限已取消，共享记账和其他保护保留。15轮使用新配置`../configs/execution_progress_trial_20261010.json`，对已有候选关闭/开启执行进展反馈各运行一次；两题已经参与离线设计，本轮只称开发/回归机制对照。后续独立评测另选新题，旧冻结结果保留。

这里的“新题评测”只表示题目此前未参与本项目的策略调试。两道新题均来自已有boltons仓库，不能证明跨仓库、跨语言泛化。若后续使用它们改进策略，应将后续用途视为开发/回归，重新选取新的评测任务，不能沿用首次评测的独立性。

17轮Requests题固定v2.31.0，6项外部检查及选定上游离线子集；不等于Requests全量、真实HTTP或进程池端到端验证。本题参与检索开发，不能称为留出评价。运行输入和真实结果在`../experiments/repository_retrieval_dev_20261014/`；导出器在两次运行结束后修正换行，旧freeze保持原样，后续完整运行使用新配置身份。

18轮三题各做关闭/开启检索一次，算法与17轮逐字节一致。两个Click任务共享8.1.3源码；boltons仓库此前见过，均为公开题/检查可见，不是私有留出。扩展检查使用明示子集，Click保留test_bytes_args的既有pytest兼容失败；排序题初次大输入未触发故障，补充插入条件后才复现，均在冻结与模型运行前完成。六次结果全部保存于`../experiments/repository_retrieval_validation_20261014/`，不能覆写身份重跑选优。

19轮将click-custom-command、barrel-sort用于片段发送策略开发/回归，固定索引/排序，对always/on_change各一次。四个身份均结束，补丁全部验收/重放通过；旧排序由上下文保护停止，不算正常提交，新方式两题均Submitted。成果在`../experiments/retrieval_delivery_trial_20261015/`。这两题后续不得称作新留出；本轮不追加调参重跑，费用沿用无上限共享记账及保护。


2026-10-25：后续六题尚在独立[evaluation准备区](../evaluation/multi_agent_20261025/README.md)，未加入本目录/面板。来自3个熟悉仓库，原版/人工参考的12公开及19保留方法已核验，751输入和15个计划槽位固定；仍缺统一首修、二进制快照及终态保留评分/批次入口，状态不可启动。保留检查/参考不复制到公开副本，不保证OS层保密；本轮0模型请求。原9个任务目录和8个catalog任务不变，详见[31](../Project/31_过程_多Agent新题筛选与离线冻结准备.md)。
