# 第三方代码来源

本项目在 Mini SWE Agent 上增加本机任务管理、上下文管理、仓库检索、多角色协作、任务卡和检查点续做。第三方 Agent 底座及练习题仓库不属于本项目的原创实现。

- `mini-swe-agent/`：Git 子模块，来源 [SWE-agent/mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)，固定提交 `04d809ceab9df28f9adaed044884180159172930`（2.4.6）。许可证见该目录 `LICENSE.md`（MIT）。
- `tasks/windowed-empty/repo/`：Git 子模块，来源 [more-itertools/more-itertools](https://github.com/more-itertools/more-itertools)，固定提交 `484401f5a056243be2508e2a6af598f7e4e99782`。许可证见该目录 `LICENSE`。
- 其余 `tasks/*/repo/` 和 `evaluation/` 中的冻结源码来自对应任务 `manifest.json` 或来源元数据声明的 more-itertools、boltons、Click、Requests、fast-deep-equal 等公开仓库。保留各目录原有许可证及版权声明；同一上游的不同任务可能保留不同版本。
- `experiments/` 包含开发诊断、补丁、报告和对应源码封存；第三方许可继续适用于其中的副本。模型生成补丁、已知题目上的成功和预设模型回复不等同于原创 Agent 框架或独立 benchmark 成绩。

本次私人归档未为原创代码另行指定开源许可证。需要公开发布时再确定原创部分的许可，并核对第三方声明。

## 1.3.0-rc1新增适配与复用

- 公开资料搜索适配复用 [deedy5/ddgs](https://github.com/deedy5/ddgs) 9.16.0（MIT），只接查询和返回元数据，不采用其完整研究工作流。新增依赖为 lxml 6.1.3（BSD-3-Clause）与 primp 2.0.1（MIT）；当前Windows安装包及SHA256固定在 `configs/reference-requirements.txt`。这些包提供搜索/解析/HTTP能力，不证明搜索结果权威或正文已读取。
- 隔离Python检查复用现有 pytest 8.3.5（MIT），不新写测试发现框架，不升级既有版本。程序只从当前项目环境选取登记的纯Python源码及公开包元数据/许可证，经哈希绑定后送入检查容器；不复制主机配置、凭据、其他包或原生扩展。包包括 pytest/pluggy/iniconfig/tomli/exceptiongroup（MIT）、packaging（Apache-2.0 OR BSD-2-Clause）、Pygments（BSD-2-Clause）、colorama（BSD）、typing_extensions（PSF-2.0）；具体版本见每个任务的运行环境绑定。`py.py`兼容入口随pytest本身的许可保留。
- 检查使用 Docker 官方 [python](https://hub.docker.com/_/python) `3.10-slim` 和 [node](https://hub.docker.com/_/node) `22-slim` 镜像，启动前记录实际image ID与官方RepoDigest，任务中禁止自动拉取。各镜像及其内含软件继续适用对应上游许可证；本仓库不分发镜像。容器接口遵循 [Docker run官方说明](https://docs.docker.com/reference/cli/docker/container/run/)。

已评估继续复用Mini SWE Agent与改用 [OpenHands SDK](https://github.com/OpenHands/software-agent-sdk) 两种路线；后者当前Python版本要求与工作区/Agent框架迁移成本均较高，本次不更换底座。新角色和循环继续接原路由、原费用记录及worker。相关选择和验证边界见[76](Project/76_实现与离线验收_用户工程资料协作与有限修复循环.md)。


## v1.3.0公开项目验收材料

`experiments/public_project_acceptance_20261031/`中的原始及模型修改后的inflection副本来自[jpvanhal/inflection](https://github.com/jpvanhal/inflection)，固定提交`663982efaa1a99f677e20c342a7a75dd9c041c8d`（0.3.1，MIT）。两个ZIP均保留上游LICENSE和原测试；修改仅用于本机工作流验收，不是本项目原创库或新运行时依赖。独立8项检查为本次工程验证材料；已知历史缺陷的结果不等于独立benchmark成绩。
