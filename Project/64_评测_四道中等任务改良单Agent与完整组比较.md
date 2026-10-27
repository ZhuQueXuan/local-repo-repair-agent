# 四道中等任务改良单Agent与完整组比较

日期：2026-10-28。按用户明确授权，固定63版，选择4道未用于本项目调试的相互约束行为问题，只比较改良单Agent与完整多Agent，每题每组一次，共8次真实运行。**独立补丁通过：改良单Agent0/4、完整多Agent0/4；完整三角色成功0/4。** 本批两组修复通过数相同。全部成功、失败、错误和费用保留，没有按结果补跑，旧成绩不改。

## 题目范围和复杂度

“中等”是结果产生前按相互影响的行为和状态预先分类，不是官方难度分、模型实测难度或大型跨文件开发。4题涉及同一模块中的多方法/多入口或多约束联动，比63的单一边界问题宽；不以补丁行数或必须修改多个文件强行规定解法。三题来自boltons、一题来自Click，仓库此前熟悉，具体行为未作为项目调试题；公开问题可能在模型训练中出现，不称训练未见或陌生仓库评测。

| 固定任务 | 相互约束 | 官方依据 |
| --- | --- | --- |
| Click资源退出异常 | Context退出/显式关闭/嵌套调用、原异常与traceback、LIFO及失败后清理和复用 | [Issue2447](https://github.com/pallets/click/issues/2447)、[PR3058](https://github.com/pallets/click/pull/3058) |
| BarrelList切片与删除 | iter_slice与读/删入口、跨内部段、正反/扩展步长、边界和后续变更 | [PR471](https://github.com/mahmoud/boltons/pull/471)、[PR472](https://github.com/mahmoud/boltons/pull/472) |
| JSONL定位与结束边界 | 构造参数归一、字节/文本换行、当前位置、正反迭代与EOF | [PR464](https://github.com/mahmoud/boltons/pull/464)、[官方变更记录](https://github.com/mahmoud/boltons/blob/master/CHANGELOG.md) |
| Table不规则文本渲染 | 推导列宽、短/非字符串表头、不齐行、隐藏表头、截断与内容不变 | [官方变更记录](https://github.com/mahmoud/boltons/blob/master/CHANGELOG.md)、[模块源码](https://github.com/mahmoud/boltons/blob/master/boltons/tableutils.py) |

原Click8.1.3和boltons25.0.0源码逐字节复用，原BSD-3-Clause许可保留，无人工注入缺陷。上游有维护和具体修复记录；复用已有公开投影、隔离资格与独立重放入口，不引入另一个评测框架、环境或依赖。题目范围明确排除新模块/联网服务/依赖安装和无关功能扩展，适配当前只接受已有实现文件修改的边界。

每题3项公开方法、5项保留方法；参考修复只用于开发侧资格检查。原始/参考均实际执行隔离子进程，网络阻断；原始公共3项全失败，参考公共3/保留5全部通过。兼容方法可以在原始版本通过，保留方法不是都必须红。

| 资格 | 原始公开/保留/上游 | 参考公开/保留/上游 |
| --- | --- | --- |
| Click资源退出异常 | 0/3、1/5、23/23 | 3/3、5/5、23/23 |
| BarrelList切片与删除 | 0/3、0/5、2/2 | 3/3、5/5、2/2 |
| JSONL定位与结束边界 | 0/3、1/5、2/2 | 3/3、5/5、2/2 |
| Table不规则文本渲染 | 0/3、0/5、3/3 | 3/3、5/5、3/3 |

上游登记子集分别为23、2、2、3项，共30项/版本，不是两仓库全套。JSONL的无换行尾部测试使用有限读取次数的流，在原版无限循环时给出明确失败，不让资格检查挂死；不修改模型运行时限。BarrelList只删除内存序列元素，没有文件删除操作。

## 先固定的系统条件

源码基线`32b6aa0918567a4038f33c15c2bc177ce77f1535`，即63交付提交。本轮只改[comparison_inputs.py](../scripts/comparison_inputs.py)的4题精确登记和[comparison_batch.py](../scripts/comparison_batch.py)的`medium_four`设计，复用已有两条分派分支。模型、提示、工具、上下文、读取控制、费用、时限和审查策略没有改动。

| 条件 | 改良单Agent | 完整多Agent |
| --- | --- | --- |
| 主流程 | 现有adaptive_history StatefulAgent | 文档、修复、首审，最多一次必要返工/末审 |
| 基础上下文 | 原1KB展示/12KB目标历史压缩、原文档案、有界任务状态 | 同一基础加原角色保护 |
| 附加能力 | 不开任务卡、文档/审查角色、检索、Skills/MCP | 原任务卡、文档/审查、只读Skills/MCP注册和修复收口 |
| 时限 | 300秒 | 初修300秒；父1800秒、文档120/首审120/返工180/末审90秒 |
| 模型/路由 | 原北邮优先、DeepSeek备用 | 同左，原父任务切换继承 |
| 评分机会 | 终态最终导出补丁 | 终态最终导出补丁，失败时最后合法封存候选 |
| 分母 | 固定4槽位，各一次 | 固定4槽位，各一次 |

温度0、16KB修复请求硬限、原32KB审查/6KB文档交接、权限/文件保护和共享记账保持。完整组总时间、提示、控制和返工机会更多，因此结果是两个现有系统的实际效果/成本比较；没有做总预算对齐，不能把差异单独归因于Agent数量。

任务1/3按改良→完整，任务2/4按完整→改良，串行交错，未随机化。全部8个身份在首次模型调用前登记，不按结果选题/重试/追加身份。输入冻结684份，计划SHA256 `802b492eadf22ec9b241f7716e681596841b461704400dac2ea785f9e4672c85`。fixed-conditions与用户本次8槽位授权先保存；原正式worker空闲与无排队、批次锁、唯一claimed/禁止再派发及明确计费拒绝停止门控保持，不探测余额。

## 离线接线

复用已有23项服务生命周期/取消/来源检查通过；5组窄门控验证旧设计保持、四题公共ZIP字节和保留排除、8个脚本化分派/配置绑定、缺组或混入raw/活动评分/重复派发拒绝、8个失败仍留分母及新计费拒绝停止。0模型调用；其中合成失败仅用于门控，不是本轮真实模型成绩。没有新增测试文件或把62的432/301当作本轮新全回归。

题目、公共检查和登记上游给两组相同；保留和参考排除在模型工作副本、提示、快照之外。Bash没有OS沙箱，不能声称对恶意越级读取保密。

## 八次真实运行结果

| 任务 | 组别/新身份前缀 | 流程 | 独立补丁 | 公开/保留/上游 |
| --- | --- | --- | --- | --- |
| Click资源退出异常 | 改良单Agent/a6b0eeeb | TimeExceeded | 未解决 | ValueError: Final patch cannot replay |
| Click资源退出异常 | 完整多Agent/4716b187 | not_completed/unavailable | 未解决 | No exported or legally sealed final patch; retained in denominator |
| BarrelList切片与删除 | 完整多Agent/13ff9c95 | not_completed/unavailable | 未解决 | No exported or legally sealed final patch; retained in denominator |
| BarrelList切片与删除 | 改良单Agent/e38b6339 | TimeExceeded | 未解决 | ValueError: Final patch cannot replay |
| JSONL定位与结束边界 | 改良单Agent/67b49f2c | TimeExceeded | 未解决 | ValueError: Final patch cannot replay |
| JSONL定位与结束边界 | 完整多Agent/c8f6e4ff | not_completed/unavailable | 未解决 | No exported or legally sealed final patch; retained in denominator |
| Table不规则文本渲染 | 完整多Agent/550eba9b | not_completed/unavailable | 未解决 | No exported or legally sealed final patch; retained in denominator |
| Table不规则文本渲染 | 改良单Agent/af74fa4d | LimitsExceeded | 未解决 | ValueError: Final patch cannot replay |

所有模型终态后才统一开始保留评分，在干净副本重放最终导出补丁或最后合法封存候选。无候选、失败、超时、服务错误和取消均在原8槽位分母。正常提交、独立补丁通过与完整协作成功分开报告；即使合法部分候选通过，也不把失败角色改成成功。失败工作目录diff仅供诊断，不补造或替代候选。

完整组单独通过的题：无。改良单Agent单独通过的题：无。

- Click资源退出异常，改良单Agent：未附加原因说明，按下列退出状态判断；退出TimeExceeded，保护类型None，正常提交False。
  trajectory.json：221动作、0个含测试命令的Bash动作、0个提交命令、0次修复控制拒绝、0次格式纠正。
- Click资源退出异常，完整多Agent：Stage failure: RepairNoProgress；文档information_not_found，3630字节/3引用，完整三角色False。
  登记审查报告0份、发现0条；角色状态：consulting=succeeded；repairing=failed；reviewing=skipped；revising=skipped；final_review=skipped。
  consulting-trajectory.json：10动作、0个含测试命令的Bash动作、0个提交命令、0次修复控制拒绝、0次格式纠正。
  repairing-trajectory.json：66动作、5个含测试命令的Bash动作、0个提交命令、12次修复控制拒绝、0次格式纠正。
- BarrelList切片与删除，完整多Agent：Stage failure: RepairNoProgress；文档None，None字节/0引用，完整三角色False。
  登记审查报告0份、发现0条；角色状态：consulting=failed；repairing=failed；reviewing=skipped；revising=skipped；final_review=skipped。
  consulting-trajectory.json：12动作、0个含测试命令的Bash动作、0个提交命令、0次修复控制拒绝、1次格式纠正。
  repairing-trajectory.json：24动作、2个含测试命令的Bash动作、0个提交命令、4次修复控制拒绝、0次格式纠正。
- BarrelList切片与删除，改良单Agent：未附加原因说明，按下列退出状态判断；退出TimeExceeded，保护类型None，正常提交False。
  trajectory.json：225动作、0个含测试命令的Bash动作、0个提交命令、0次修复控制拒绝、0次格式纠正。
- JSONL定位与结束边界，改良单Agent：未附加原因说明，按下列退出状态判断；退出TimeExceeded，保护类型None，正常提交False。
  trajectory.json：229动作、0个含测试命令的Bash动作、0个提交命令、0次修复控制拒绝、0次格式纠正。
- JSONL定位与结束边界，完整多Agent：Stage failure: RepairNoProgress；文档information_not_found，3140字节/2引用，完整三角色False。
  登记审查报告0份、发现0条；角色状态：consulting=succeeded；repairing=failed；reviewing=skipped；revising=skipped；final_review=skipped。
  consulting-trajectory.json：11动作、0个含测试命令的Bash动作、0个提交命令、0次修复控制拒绝、0次格式纠正。
  repairing-trajectory.json：22动作、1个含测试命令的Bash动作、0个提交命令、3次修复控制拒绝、0次格式纠正。
- Table不规则文本渲染，完整多Agent：Stage failure: RepairNoProgress；文档information_not_found，3526字节/4引用，完整三角色False。
  登记审查报告0份、发现0条；角色状态：consulting=succeeded；repairing=failed；reviewing=skipped；revising=skipped；final_review=skipped。
  consulting-trajectory.json：10动作、0个含测试命令的Bash动作、0个提交命令、0次修复控制拒绝、0次格式纠正。
  repairing-trajectory.json：21动作、5个含测试命令的Bash动作、0个提交命令、3次修复控制拒绝、0次格式纠正。
- Table不规则文本渲染，改良单Agent：Agent触发运行保护：context_bytes；退出LimitsExceeded，保护类型context_bytes，正常提交False。
  trajectory.json：17动作、3个含测试命令的Bash动作、0个提交命令、0次修复控制拒绝、0次格式纠正。

“登记发现”是审查模型的意见，不自动证明是真实缺陷或有效纠错。是否增加独立修复通过、是否正常返工和最终检查分别留证；没有额外用隐藏检查反馈指导模型。文档合法交接/工具注册也不等于内容有用或实际调用。详细意见、响应、角色轨迹和原输出均在归档。

本批实际未走到审查：完整组4次均RepairNoProgress，BarrelList文档另因document_repeated_evidence_limit失败后按原降级规则继续，其余3次文档合法提交information_not_found。改良单Agent的Click、BarrelList、JSONL分别221/225/229个Bash动作后TimeExceeded，未提交；表格17动作后context_bytes停止。

最终8个工作副本的实现diff均0字节：4个单Agent导出空补丁，4个完整组无合法封存候选。因此Final patch cannot replay是空补丁/未解决，不是有效候选被重放设施损坏；本批没有实际进入“合法最终补丁的保留方法执行”，保留资格验证与最终无候选评分分开。

改良组大量使用`cd "$(pwd)"`前缀、复合sed/grep、临时输出及Python打印源码；现有简单重复读守卫不能覆盖这些形式。例如Click同一复合读取重复137次。完整组更早返回读取/停滞纠正，4次初修分别66/24/22/21动作，但没有变成实现交付。表格单Agent确实尝试按固定行位置改写，工具回执显示写入前断言失败；同条复合命令继续执行检查并打印退出码，最后整体返回0，公开检查实际仍失败。以上来自原命令/回执和最终字节差异，不推测模型内部原因，也不在本批中途补修。

本批先暴露出执行闭环瓶颈，不能据此认定这些问题超出了模型的代码推理能力。后续优先离线核对“读到必要内容→形成实现变更→真实验证→提交”的接线，而不是继续提高付费任务难度；这只是后续建议，本批没有追加修复或真实调用。

## 费用与实际耗时

| 指标 | 改良单Agent四题 | 完整多Agent四题 |
| --- | --- | --- |
| 独立补丁通过 | 0/4 | 0/4 |
| 正常提交 | 0/4 | 0/4（初修） |
| 平台尝试 | 696 | 164 |
| 已知价格估算/元 | 4.335308 | 1.337402 |
| 未知费用占位/元 | 0.4 | 0.4 |
| 实际运行累计/秒 | 978.002 | 291.797 |

共860次平台尝试，已知估算5.672710元，未知费用占位0.80元；完整费用未知，不把占位加到估算当确认扣费。耗时取原worker收尾metrics，包含本机准备/检查/收尾，排除队列等待和后续保留评分，不退回创建至结束计时。缺失运行计时：改良0、完整0；若有缺失，上表只能表示已知部分。

计费停止记录`None`。8个身份北邮首试均500，按原规则转DeepSeek备用，未出现明确余额不足/计费拒绝。新服务失败与备用切换均保留，具体平台类型/状态码见manifest；没有余额探测，不把连接错误或500猜作余额/认证问题。

本批两组修复通过数相同。这些是4道预设相互约束问题各一次的描述性结果，不代表真实中型工程整体表现、统计稳定率或角色数量的因果结论。不能只依据“approve次数多”或“调用工具多”判断收益，应同时看修复、协作是否完整，以及费用和时限机会的差异。

## 旧结果保护和交付

正式53条payload、原3177条共享请求、7941份旧experiments/evaluation文件逐项保持；共享请求现4037，684份冻结输入在终态评分后再次核对。原46/58/61/63比较和59/60/62开发复验分别保留，不混合分母或回填旧成绩；旧冻结因本轮登记源码变化已失配，不复用发起旧身份。

正式成果`experiments/medium_comparison_20261028/`保存8身份、成功/失败、角色报告/响应、合法候选/补丁、独立评分、资格、固定条件/授权、费用增量和原工具证据。全部ZIP成员与外层SHA256核对并递归扫描两份真实凭据和令牌模式；不上传正式数据库或整份共享账本。没有操作医疗、异机、依赖安装、8765服务或文件删除。

本批到此结束，不追加模型运行让结果更好看。若依据这些题继续修改策略，这些题即转为开发回归题；新的效果结论需另选题/冻结，不把本批已用题继续叫新题。
