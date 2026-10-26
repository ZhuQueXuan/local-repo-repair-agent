# Windows 真实仓库任务：空输入滑动窗口

这是一道选定的历史开源问题，用来从教学代码过渡到真实仓库修复。它不是 SWE-bench 题目，也不是独立、未见过的能力评测；历史问题可能已出现在模型训练资料中。

## 固定输入

- 仓库：more-itertools，MIT 许可证保留在 `repo/LICENSE`。
- 基础版本：v8.13.0，commit `484401f5a056243be2508e2a6af598f7e4e99782`。
- 问题来源：[issue #462](https://github.com/more-itertools/more-itertools/issues/462)。空输入、正窗口宽度返回了一个全部由填充值组成的窗口；本任务要求没有窗口。
- 维护者在 [PR #645 的说明](https://github.com/more-itertools/more-itertools/pull/645)中确认了目标行为。本项目未读取修复差异，也未把参考补丁提供给 Agent。
- `issue.md` 是提供给 Agent 的任务描述；`manifest.json` 保存版本、源码 SHA256 和评分范围。`repo/` 保留未修复的原始代码。

## 本地复现

从 `shixi_project` 根目录使用 PowerShell 执行：

```powershell
& '.\.venv\Scripts\python.exe' -B scripts/check_windowed_task.py --full-suite
& '.\.venv\Scripts\python.exe' -B scripts/prepare_windowed_task.py
```

第一个命令检查原始代码，当前退出码 1 是复现到失败的预期结果。第二个命令校验固定源码的 33 个文件，然后在本任务 `temp/run_时间/repo/` 中创建新副本，输出实际路径；不安装依赖、不调用模型。工作副本不携带 Git 历史。

在输出的工作目录中，Agent 可以执行：

```text
python -B -m unittest tests.test_more.WindowedTests -v
python -B -m unittest discover -s task_checks -v
```

独立复测使用项目根目录的维护工具，并传入副本路径：

```powershell
& '.\.venv\Scripts\python.exe' -B scripts/check_windowed_task.py --repo '工作副本的绝对路径' --full-suite
```

工具从正式位置读取新增检查，并核对工作副本中原有测试的 SHA256；修改测试不能算通过。报告默认写入主项目 `temp/`，核验后再保留为正式实验成果。

## 修复前结果与评分范围

| 检查 | Windows / Python 3.10.6 实际结果 |
| --- | --- |
| 原有 WindowedTests | 6 项通过 |
| 新增行为检查 | 10 项中 5 通过、5 失败，失败均对应空输入 |
| 全部上游单元测试及 doctest | 660 项中 659 通过、1 失败 |

全量检查的既有失败是 `TimeLimitedTests.test_zero_limit`：零时限下预期没有输出，但本机得到了一些输出。失败发生在未修改的代码上，与滑动窗口题不同；原因尚未定位，不能据此宣布 Windows 是唯一原因。保留此失败，不修改或跳过测试。

本题局部通过条件是 6 项原有窗口测试和 10 项新增检查全部通过，且原有测试未变。全量检查另行报告：若既有计时失败仍在，则不能写成全仓库全部通过；其他新增失败必须进一步分析。

原始成绩见 [baseline.json](../../experiments/windowed_task_preparation_20261007/baseline.json)。2026-10-07已完成DeepSeek首个真实仓库批次：9次调用，本题16项独立检查通过，原有全量计时失败仍在，原始源码与测试未改。补丁在全新副本重放通过，见[模型运行结果](../../experiments/windowed_agent_20261007/summary.json)与[08过程记录](../../Project/08_过程_首个真实仓库任务修复与独立验收.md)。该批次及购物车首批均已结束，不继续花费余额。
