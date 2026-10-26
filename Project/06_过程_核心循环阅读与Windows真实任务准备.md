# 过程｜核心循环阅读与 Windows 真实任务准备

日期：2026-10-07。

用户确认继续推进，Docker 等真正需要时再配置。本轮完成关键源码阅读、一个 Windows 兼容真实仓库任务的固定与失败复现。未启动 Docker、修改系统功能或发送付费模型请求。

## Agent 如何连续工作

可以把它理解为一个反复执行的流程：收到修复要求 → 模型提出命令 → 电脑执行命令 → 把输出交给模型 → 模型决定下一步。购物车首题中的读文件、运行测试、改代码、再测试，就是这个流程的六次实际决策。

本项目固定 Mini SWE Agent 2.4.6 的上游代码。在 [default.py](../mini-swe-agent/src/minisweagent/agents/default.py) 中，核心的一步只有：

```python
return self.execute_actions(self.query())
```

这行代码连接三个分工。模型负责选择下一条动作；运行环境负责真正执行；Agent 负责保存消息、控制循环和停止。模型输出一句“已经修好”并不能代替独立测试。

| 先读哪个位置 | 它解决的问题 | 与首题的对应 |
| --- | --- | --- |
| `DefaultAgent.run()`，88 行起 | 建立系统和任务消息，循环执行，处理停止与保存轨迹 | 为什么提交前能连续工作 |
| `step()`，126 行起 | 把模型查询和动作执行连起来 | 六个步骤共用同一条调用路径 |
| `query()`，130 行起 | 检查步数、费用、时间，再请求模型，将回复追加到历史 | 模型为什么能看到先前的报错 |
| `execute_actions()`，154 行起 | 执行动作，把命令输出包装成观察消息后追加到历史 | 第三步测试错误如何影响第四步修复 |
| `save()`，182 行起 | 保存配置、调用统计、消息和退出状态 | 如何复盘它到底做了什么 |

`run()` 每一步结束时保存轨迹；模型或执行错误也会有对应记录。`query()` 的上游费用限制在新请求前检查已累计费用，因此本项目模型适配另加了请求前的保守预算检查。当前模型接入用 [run_deepseek_smoke.py](../scripts/run_deepseek_smoke.py) 中的 `BudgetedDeepSeekModel`；运行命令用同文件中的 `GitBashEnvironment`。上游核心循环没有修改。

消息历史目前只是持续追加的对话与执行输出。还没有在本项目加入长期记忆、检索或多 Agent；以后是否需要，依据任务失败证据决定。当前阶段先理解现有循环和测试证据。

## 为什么第二题选真实仓库小问题

购物车题很适合验证模型接口，但代码只有几行。下一步需要模型面对真实目录、库函数、原有测试和兼容性约束。选题先控制环境复杂度，让修复过程本身成为观察对象。

本轮选择纯 Python 库 more-itertools 的 [issue #462](https://github.com/more-itertools/more-itertools/issues/462)：`windowed` 对空输入产生了一个全是填充值的窗口。维护者在 [PR #645 的说明](https://github.com/more-itertools/more-itertools/pull/645)中确认空输入应不产生窗口。仅核对问题与预期行为，未读取修复差异或向 Agent 提供参考补丁。

例如，把 `[1, 2, 3]` 按两个元素一组滑动，应得到 `(1, 2)`、`(2, 3)`；输入里没有元素时，不应凭空多出一组。与此同时，短的非空输入原本可以补齐，窗口宽度为零也有既定行为，这些都需要保留。

任务固定在 v8.13.0，commit `484401f5a056243be2508e2a6af598f7e4e99782`。源码与许可证保留在 [tasks/windowed-empty/repo/](../tasks/windowed-empty/repo/)，33 个原有文件的 SHA256 记在 [manifest.json](../tasks/windowed-empty/manifest.json)。下载先落入主项目 temp，经版本与状态核对后归为正式任务输入。未改动上游文件，也未安装该库；测试直接导入指定副本中的代码。

## 本轮实际验证结果

| 检查 | 修复前结果 | 能说明什么 |
| --- | --- | --- |
| 原有 WindowedTests | 6 项通过 | 旧测试未发现空输入问题 |
| 新增行为检查 | 5 通过、5 失败 | 空元组、空列表、空迭代器、填充值和步长组合均复现问题；正常行为检查通过 |
| 全量上游单元测试与 doctest | 659 通过、1 失败，共 660 项 | 保留全仓库背景，已有一个与本题不同的计时失败 |
| 新工作副本独立复测 | 原有 6 项通过，新增 5 通过、5 失败；测试哈希一致 | 任务能从固定输入重新准备，导入位置正确 |

全量失败为 `TimeLimitedTests.test_zero_limit`。未修改的代码在本机零时限下得到非空输出；本轮尚未定位其根因。不能把这项原有失败记成 Agent 新引入的问题，也不能宣称全部上游测试通过。本题局部通过需要原有 6 项加新增 10 项全部通过，且原有测试未改；全量结果仍需另报。

正式证据：[baseline.json](../experiments/windowed_task_preparation_20261007/baseline.json)、[work_preflight.json](../experiments/windowed_task_preparation_20261007/work_preflight.json)。这是一题的修复前输入验证，还不是 Agent 性能 baseline。历史公开问题可能被模型见过，因此未来即使修复成功，也只作为工程闭环证据。

## 可复现入口与下一步

- [任务说明与运行方法](../tasks/windowed-empty/README.md)：固定输入、测试命令、评分口径。
- [prepare_windowed_task.py](../scripts/prepare_windowed_task.py)：先核对原始源码，再为每次运行在任务 temp 中生成独立副本。
- [check_windowed_task.py](../scripts/check_windowed_task.py)：检查指定副本，新增测试从正式位置读取，原有测试核对哈希，结果先写 temp。

下一步在新副本上接入现有 DefaultAgent 与 DeepSeek，独立限定新批次调用次数、费用、上下文和执行时间，再保存修复轨迹与补丁、复测结果。本题目前尚未运行模型；购物车的首批 8 次尝试仍保持结束状态，不复用其余额或清空账本。

当前继续使用 Windows Python；需要 Bash 命令时用已有 Git Bash。正式进入要求 Linux 容器的 SWE-bench 评测时再配置 Docker。该决定已更新快速接手入口，旧的系统功能脚本保留为未执行的历史准备。
