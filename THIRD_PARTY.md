# 第三方代码来源

本项目在 Mini SWE Agent 上增加本机任务管理、上下文管理、仓库检索、多角色协作、任务卡和检查点续做。第三方 Agent 底座及练习题仓库不属于本项目的原创实现。

- `mini-swe-agent/`：Git 子模块，来源 [SWE-agent/mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)，固定提交 `04d809ceab9df28f9adaed044884180159172930`（2.4.6）。许可证见该目录 `LICENSE.md`（MIT）。
- `tasks/windowed-empty/repo/`：Git 子模块，来源 [more-itertools/more-itertools](https://github.com/more-itertools/more-itertools)，固定提交 `484401f5a056243be2508e2a6af598f7e4e99782`。许可证见该目录 `LICENSE`。
- 其余 `tasks/*/repo/` 和 `evaluation/` 中的冻结源码来自对应任务 `manifest.json` 或来源元数据声明的 more-itertools、boltons、Click、Requests、fast-deep-equal 等公开仓库。保留各目录原有许可证及版权声明；同一上游的不同任务可能保留不同版本。
- `experiments/` 包含开发诊断、补丁、报告和对应源码封存；第三方许可继续适用于其中的副本。模型生成补丁、已知题目上的成功和预设模型回复不等同于原创 Agent 框架或独立 benchmark 成绩。

本次私人归档未为原创代码另行指定开源许可证。需要公开发布时再确定原创部分的许可，并核对第三方声明。
