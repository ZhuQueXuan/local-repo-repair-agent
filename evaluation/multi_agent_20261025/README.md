# 六题输入准备包：尚不可启动付费实验

2026-10-25。6个此前未作为本项目策略调试题的问题，来自3个已熟悉的公开仓库；所有版本沿用已有、已核对的本地冻结源码，没有人工注入缺陷。公开源码/检查可能在模型训练中出现过，不声称私有题或SWE-bench成绩。

| 任务 | 官方问题/变化证据 | 基线 | 预选S+ |
| --- | --- | --- | --- |
| boltons-addlist-iterator | [PR458](https://github.com/mahmoud/boltons/pull/458) | 25.0.0 | 是 |
| click-abort-cause | [Issue2379](https://github.com/pallets/click/issues/2379) | 8.1.3 | 否 |
| requests-utf8-length | [PR6589](https://github.com/psf/requests/pull/6589) | 2.31.0；本机urllib3 2.8.0 | 是 |
| boltons-query-encoding | [PR455](https://github.com/mahmoud/boltons/pull/455) | 25.0.0 | 否 |
| click-flag-default-map | [Issue2632](https://github.com/pallets/click/issues/2632) | 8.1.3 | 是 |
| boltons-unicode-lines | [PR475](https://github.com/mahmoud/boltons/pull/475) | 25.0.0 | 否 |

`public/<task>/`只包含基线源码、原许可证、重新表述的需求和可见检查。`evaluator/<task>/`包含开发侧保留检查、参考适配和来源留证，不复制到工作副本。参考实现只用于题目红/绿资格核验，不是Agent产物。

`plan.json`预声明15个槽位：每题S/D各一次，预选3题另做S+一次；S/D前后顺序交错，S+三个位置轮换。所有槽位目前只有计划身份，没有正式run_id、模型轨迹或费用。候选选取发生在模型结果之前，不按后来哪组成功换题；开发者修正出题/参考适配的过程失败保留。

冻结范围是**离线输入准备包**，不是已可执行实验。状态固定为`offline_prepared_not_launchable`、`paid_launch_authorized=false`。现有S/D初修提示不一致，保留评价还没接终态门控，完整批次登记/取消/汇总入口和二进制素材的安全快照绑定也需接线；S+仍没有真实模型运行证据。准备工具不能构造模型或启动任务。

```powershell
.venv\Scripts\python.exe -B scripts/evaluation_preparation.py --verify
# 仅在新temp副本重新核验一道题的原始失败/参考修复，不调用模型
.venv\Scripts\python.exe -B scripts/evaluation_preparation.py --qualify boltons-addlist-iterator
```

基线和参考版本都真正运行公开、保留、相关上游子集。当前六题原版都能复现；参考版本12个公开/19个保留方法均通过，上游各题子集无失败（Requests有1个既有依赖跳过）。这里只检查相关子集，不代表各仓库全量测试。保留分数不会反馈给正在运行的模型，也不据此改写原模型成绩。

本包尚未接入正式任务目录/面板，不能通过面板误启动。保留是输入分发边界，**不是OS文件/网络隔离**；现有本机修复Bash没有防止恶意读取父目录的沙箱，不能保证对不可信任务保密。公开/保留检查和参考答案的字节、来源、预算与规则均按freeze.json固定；后续运行接线后必须生成新的运行级冻结，不能把本包当作完整的可启动清单。
