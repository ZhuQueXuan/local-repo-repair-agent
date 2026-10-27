# 两道新题三组系统比较

日期：2026-10-28。按用户明确授权，固定当前62版和条件，对两道此前未作为本项目调试题的公开问题，执行原版单Agent、改良单Agent和完整多Agent各一次，共6次真实运行。**独立补丁通过：原版单Agent0/2、改良单Agent2/2、完整多Agent2/2；完整三角色成功2/2。** 本批完整组与改良单Agent修复通过数相同，未显示修复数量的额外收益。每组只有两道题且各一次，结论仅适用于本批观察。

## 题目与离线资格

| 问题 | 官方依据 | 原始源码 | 范围 |
| --- | --- | --- | --- |
| Click空字符串默认值帮助 | [Issue2500](https://github.com/pallets/click/issues/2500)、[PR2724](https://github.com/pallets/click/pull/2724) | 原Click8.1.3字节包 | 显示标量空字符串默认值，保持隐藏/自定义标签/普通与动态默认值 |
| boltons小数秒解析 | [官方变更记录](https://github.com/mahmoud/boltons/blob/master/CHANGELOG.md)、[官方timeutils源码](https://github.com/mahmoud/boltons/blob/master/boltons/timeutils.py) | 原boltons25.0.0字节包 | 小数秒补至6位或截断，保留日期/整秒及原格式往返 |

两个仓库此前已用于本项目，具体问题此前没有参与策略调试；公开源码或问题可能被模型训练见过。这里的“新题”不代表训练未见或陌生仓库，也不是官方SWE-bench成绩。两题分别来自不同仓库，但仍是可修范围窄的小样本。

本轮复用原Mini-SWE-Agent底座、公共投影、批次锁和独立重放入口。Click与boltons官方维护记录可核对，原BSD-3-Clause许可及LICENSE随字节副本保留。无需引入另一个评测框架、安装依赖或转换仓库；直接换框架会改变既有Windows运行和计费条件，不能帮助保持这轮比较条件。

每题2项公开方法、3项保留方法。原始/参考版本均在隔离子进程执行，网络被阻断。保留中的兼容检查允许原始版本通过，不要求每个方法都红。

| 资格 | 原始公开/保留/上游 | 参考公开/保留/上游 |
| --- | --- | --- |
| Click空字符串默认值帮助 | 0/2、2/3、25/25 | 2/2、3/3、25/25 |
| boltons小数秒解析 | 0/2、1/3、4/4 | 2/2、3/3、4/4 |

上游为登记的相关子集：Click默认值/信息字典共25项，boltons时间工具4项，不是两仓库全套。参考修复与保留检查只在评测侧，排除在模型工作副本、公共ZIP和提示之外；本地Bash没有OS沙箱，不能保证对恶意越级读取保密。

## 固定版本与三组条件

源码基线`f18d479f7152fea97107ca730f2b3ee85e016902`，即62修复交付提交；本轮仅在[comparison_inputs.py](../scripts/comparison_inputs.py)登记两道题、在[comparison_batch.py](../scripts/comparison_batch.py)补第三组调度及分母统计，没有修改任何模型运行、提示模板、读取控制、上下文、时限、执行环境或价格规则。改良单Agent复用已存在的`adaptive_history`配置，无临时追加能力。

| 条件 | 原版单Agent | 改良单Agent | 完整多Agent |
| --- | --- | --- | --- |
| 主流程 | raw DefaultAgent2.4.6 | 现有adaptive_history StatefulAgent | 前置文档、修复、首审，最多一次必要返工/末审 |
| 输出与历史 | 原完整输出/历史 | 1KB输出展示、12KB目标压缩、原文档案 | 同一上下文基础加原角色保护与受控收口 |
| 状态 | 无附加任务状态 | 原有界任务状态与简单重复读取保护 | 任务卡、进展识别及62复合读取控制 |
| 文档/审查/Skills/MCP | 关闭 | 关闭 | 原只读文档/审查及Skills/MCP注册，是否调用由模型决定 |
| 修复时间 | 300秒 | 300秒 | 初修300秒，原父时限1800秒 |
| 后续机会 | 一次单Agent | 一次单Agent | 文档120/首审120/返工180/末审90秒，原分支规则 |
| 模型与费用 | 原北邮优先、DeepSeek备用、共享账本 | 同左 | 同左 |

三组公共问题、源码、公开检查和上游子集一致，温度0、16KB修复请求硬限及原提交约束保持。改良组不含完整组专有的复合读取/验证收口控制，也不含检索、任务卡或独立审查。这个比较回答“现有改良单Agent与完整系统各表现如何”，仍不能把差异单独归因于多Agent：角色数、提示、总时间、控制策略和修改机会同时不同。

题1按原版→改良→完整，题2按完整→改良→原版串行运行；不是完全随机化，改良组都在中间位置。所有身份先登记，再开始第一个模型，避免按结果增补身份。失败、超时、服务错误、取消和无候选都留在原6槽位分母；只运行一次，无按结果选优重跑。

计划`temp/comparison_batches/c63_1028/plan.json`，SHA256 `ce4ff396c4b851ea539e6463f1bfd2d9d84be097c5453b41a36360f6083b1ab3`；452份冻结输入。授权记录和fixed-conditions在首个真实调用前保存，明确只授权这6次。原正式worker空闲/无排队任务、批次独占、唯一claimed及计费拒绝停止门控保持；不探测余额。

## 离线接线检查

已有23项服务生命周期/取消/来源检查实际通过，另5组窄检查验证旧双组设计保持、公共ZIP字节和保留排除、三条分派分支和现有配置绑定、缺组/未知组/活动评分/重复启动拒绝、所有6个失败仍保留分母及计费停止。调度检查用脚本化终态worker，0模型调用，不是模型解题成绩；本轮没有新增测试文件或把62的432/301当作本轮完整回归。

## 全部六次终态

| 问题 | 组别/新身份前缀 | 流程结果 | 独立补丁 | 公开/保留/上游 |
| --- | --- | --- | --- | --- |
| Click空字符串默认值帮助 | 原版单Agent/0f81a1de | LimitsExceeded | 未解决 | ValueError: Final patch cannot replay |
| Click空字符串默认值帮助 | 改良单Agent/eddd9ff9 | Submitted | 通过 | public2/2；withheld3/3；upstream25/25 |
| Click空字符串默认值帮助 | 完整多Agent/a3eb84d7 | completed/approve | 通过 | public2/2；withheld3/3；upstream25/25 |
| boltons小数秒解析 | 完整多Agent/90c39231 | completed/approve | 通过 | public2/2；withheld3/3；upstream4/4 |
| boltons小数秒解析 | 改良单Agent/36f6b269 | Submitted | 通过 | public2/2；withheld3/3；upstream4/4 |
| boltons小数秒解析 | 原版单Agent/0cd5ff5a | LimitsExceeded | 未解决 | public2/2；withheld1/3；upstream4/4 |

正常提交、终态补丁通过、完整协作成功分别计数。所有6个worker终态后才开始保留评分；独立程序取最终导出补丁，或完整组失败时最后合法封存候选，在干净副本重放。没有合法候选仍未解决；不从失败工作目录补造候选。上下文保护停止留下的补丁按预声明规则评估，但不能算正常Submitted。

逐次运行观察：

- Click空字符串默认值帮助，原版单Agent：Agent触发运行保护：context_bytes；退出LimitsExceeded，保护类型context_bytes，正常提交False。
  trajectory.json：7动作、0个含测试命令的Bash动作、0个提交命令、0次修复控制拒绝。
- Click空字符串默认值帮助，改良单Agent：记录正常结束；退出Submitted，保护类型None，正常提交True。
  trajectory.json：17动作、2个含测试命令的Bash动作、1个提交命令、0次修复控制拒绝。
- Click空字符串默认值帮助，完整多Agent：Model collaboration ended. Repair acceptance is from independent checks and clean patch replay.；文档answered，交接3539字节/4引用，完整三角色True。
  consulting-trajectory.json：14动作、0个含测试命令的Bash动作、0个提交命令、0次修复控制拒绝。
  repairing-trajectory.json：9动作、2个含测试命令的Bash动作、1个提交命令、0次修复控制拒绝。
  reviewing-trajectory.json：11动作、0个含测试命令的Bash动作、0个提交命令、0次修复控制拒绝。
- boltons小数秒解析，完整多Agent：Model collaboration ended. Repair acceptance is from independent checks and clean patch replay.；文档information_not_found，交接3520字节/3引用，完整三角色True。
  consulting-trajectory.json：12动作、0个含测试命令的Bash动作、0个提交命令、0次修复控制拒绝。
  repairing-trajectory.json：9动作、2个含测试命令的Bash动作、1个提交命令、0次修复控制拒绝。
  reviewing-trajectory.json：7动作、0个含测试命令的Bash动作、0个提交命令、0次修复控制拒绝。
- boltons小数秒解析，改良单Agent：记录正常结束；退出Submitted，保护类型None，正常提交True。
  trajectory.json：12动作、1个含测试命令的Bash动作、1个提交命令、0次修复控制拒绝。
- boltons小数秒解析，原版单Agent：Agent触发运行保护：context_bytes；退出LimitsExceeded，保护类型context_bytes，正常提交False。
  trajectory.json：5动作、1个含测试命令的Bash动作、0个提交命令、0次修复控制拒绝。

动作计数只描述轨迹，不把含测试关键字的命令数当成独立通过数。原版Click未产生改动，0字节补丁的Final patch cannot replay代表没有修复，不是有效补丁被评测损坏。原版boltons已产生部分补丁，公开2/2、上游4/4，但先转int再转回字符串丢失了小数的前导零，保留仅1/3；其上下文停止和补丁错误分别留证。

完整组两题均正常提交，首审approve，无返工或降级；两次审查各有一次“必须恰好调用一个工具”的格式纠正，随后继续。两次修复均9个Bash动作，没有触发62新增的读取控制拒绝，所以不能把本批成功直接归因于那处守卫。Skills/MCP虽注册，但轨迹没有实际选择它们。

工具选择、角色状态、格式纠正及重复命令详manifest和原始trajectory。文档合法提交或information_not_found不代表提供了有用的问题依据；工具已注册不等于实际使用或产生收益。

## 耗时与费用

| 指标 | 原版 | 改良单Agent | 完整多Agent |
| --- | --- | --- | --- |
| 独立补丁通过 | 0/2 | 2/2 | 2/2 |
| 正常提交 | 0/2 | 2/2 | 2/2（初修） |
| 平台尝试 | 14 | 31 | 56 |
| 已知价格估算/元 | 0.073192 | 0.173206 | 0.741034 |
| 未知费用占位/元 | 0.2 | 0.2 | 0.2 |
| 运行累计耗时/秒 | 40.968 | 65.109 | 228.703 |

共101次平台尝试，已知价格估算0.987432元，未知费用占位0.60元；完整费用未知，不把占位与估算相加称作确认扣费。耗时取三组原worker收尾metrics中的total_elapsed_seconds，含本机准备/检查/收尾，排除排队等待与后续保留评分，不是纯模型延迟。完整组初步汇总曾错误退回“创建至结束”而包含排队，得到491.168秒；本轮已按原worker计时纠正为228.703秒，初步审计另存留证，原轨迹和评分未改。

六个身份北邮首试均500，按原固定策略转DeepSeek备用；没有发生余额不足/计费拒绝，不把500解释为余额或认证问题。所有失败均原样记账，计费停止记录`None`，具体服务错误见manifest。

本批完整组与改良单Agent修复通过数相同，未显示修复数量的额外收益。原版与改良组的差异主要对应现有上下文/任务状态组合；完整组增加的角色和控制也有额外成本，不能仅用角色数量解释结果。本轮两题/组各一次，不能估计稳定率或据此保证后续题表现。

## 保护和归档

正式53条payload、原3076条共享请求、7675份旧experiments/evaluation文件逐项保持；共享请求现3177，452冻结输入在终态评分后再次核对。46/58/61旧比较、59/60/62旧题开发复验不混合分母或回填成绩，没有恢复旧身份。医疗、异机、依赖和8765服务均未操作。

正式归档`experiments/three_arm_20261028/`含全部6身份、原始轨迹、合法候选/补丁、失败、资格红绿、独立评分、固定条件/授权、费用增量及审计。成员与外层SHA256核对，递归扫描两份真实凭据及令牌模式；正式数据库和整份共享账本不上传。仓库元数据接线是本轮仅有功能源码变动，旧packet全部保留。

本批执行结束，不追加运行修饰结果。这两题后续若用于修改策略，应转为开发回归题；下一次新效果比较需另选题、另冻结。
