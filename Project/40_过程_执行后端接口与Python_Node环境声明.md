# 执行后端接口与 Python / Node 环境声明

日期：2026-10-26。接续35框架第二组和39工具/Skills版本，功能优先，整体测试后置。本轮没有真实模型请求、依赖安装、Docker或新语言模型批次。

## 本版交付效果

- 原面板新增“本机执行环境 / 检查执行环境”：点击后只执行固定的Bash、Node、Git版本命令；Python直接记录当前进程。没有自动检查、安装或模型请求。
- 新的单/多角色运行会保存`execution-environment.json`，记录它自己的后端、Python路径/版本/依赖目录、Node选择与公共依赖清单。历史任务不补造这份信息。
- 原Windows进程启动/等待/超时/清理集中到执行后端，GitBash适配、原删除防护、环境变量筛选、pipefail、输出预算和提交信号仍保留在原层次。
- 检查点续做新增环境声明核对：Python、Bash/Git路径、主要Python依赖元数据或执行配置改变时，在建立子身份前拒绝；不跨环境自动恢复。可选Node尚未用于当前Python任务，不把Node可用与否当作它们的前置条件。

**Node可用不是JavaScript任务全面可用。** 当前正式自动目录只接受已登记Python任务。原fast-deep-equal JavaScript开发夹具和Python→Node桥、原字节/证据仍在，留给离线资格验证，不作为已验收的自动模型任务展示。

## 开源依据与方案取舍

先核对了上游GitHub的[LocalEnvironment接口](https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/environments/local.py)、[环境文档](https://mini-swe-agent.com/latest/advanced/environments/)和[MIT许可](https://github.com/SWE-agent/mini-swe-agent/blob/main/LICENSE.md)，再对照本地固定2.4.6源码。官方已用环境对象将Agent动作与执行方式分开，当前项目也已有GitBash适配，因此复用原接口比引入另一套完整Runtime更匹配，且可保留原循环、上下文、工具权限和费用记录。

本轮在线核对的是当前官方接口/文档；实际底座仍固定`04d809ceab9df28f9adaed044884180159172930`，没有追随主分支升级。新增的是本项目的薄执行后端，不拷贝另一个框架、不新增依赖。容器等后端以后可按同一职责接入，本版本仅实现本机Windows路径；没有把接口抽象说成已具备容器或跨平台隔离。

## 使用方法

在原[本机面板](http://127.0.0.1:8765/)点击“检查执行环境”。页面分别显示路径、版本和缺失/未检查状态，并注明这是当前面板进程的环境，不能替代某条历史运行的证明。检查结果缓存于该服务进程，带检查时间，重启后重新检查。

命令行可选择只看声明或实际运行版本命令：

```powershell
# 读取路径/当前Python/依赖元数据，不启动版本检查子进程
.venv\Scripts\python.exe -B scripts/execution_runtime.py

# 实际执行固定的Bash / Node / Git版本命令
.venv\Scripts\python.exe -B scripts/execution_runtime.py --probe
```

`MINI_TASK_BASH`仍是进程环境变量，未设置时沿用`D:\Git\bin\bash.exe`。现有命令入口统一读取同一声明；解析为实际可执行路径后再使用，不能在HTTP请求中传命令或路径。

`MINI_TASK_NODE`只为当前环境检查指定Node可执行文件，未设置时从PATH查找。本版没有把这个变量自动接入受保护的JavaScript题目桥，也没有更改原桥源码。Python始终使用当前进程/worker解释器，原命令环境把该解释器目录放到PATH前面；不提供从页面切换Python/安装环境的入口。

新建任务的环境声明仅记录可执行文件路径与当前Python、安装元数据，不会偷偷执行额外版本探测。Bash/Node/Git状态因此可能是`not_probed`；人工检查所得`available`也只表示对应版本命令成功，不证明全部依赖和任务可用。

## 实现与职责

| 文件 | 入口与作用 |
| --- | --- |
| `scripts/execution_backend.py` | `ExecutionBackend`协议、`ProcessResult`、`LocalProcessBackend.run_argv`、`WindowsGitBashBackend.run_shell`与`terminate_owned_process`。统一显式argv、等待、期限/回调与所属进程收尾，不接受动态插件命令注册 |
| `scripts/execution_runtime.py` | `load_execution_config`、`bash_executable`、`runtime_report`、`runtime_identity`：严格声明、路径解析、固定版本探测、依赖记录与续做比较 |
| `configs/execution_backends_v1.json` | 唯一本机后端、Python最低3.10/当前进程、可选Node、固定依赖清单、不安装/无OS隔离等声明；路径覆盖仅用既有/列明的进程变量 |
| `scripts/run_deepseek_smoke.py` | 原`GitBashEnvironment.execute`保留删除/子环境/输出/提交钩子，进程执行交给后端，序列化补充后端信息 |
| `scripts/run_task_batch.py`、`scripts/run_windowed_agent.py` | 原命令入口改为同一个Bash选择函数；不改变策略、费用与任务输入 |
| `scripts/collaboration_live.py` | 独立验收argv接后端；保留原超时异常语义，新增环境声明成果 |
| `scripts/task_service_worker.py` | 单Agent同样保存声明；新后端源码/配置/页面纳入新运行冻结，原进程清理入口保留导入兼容 |
| `scripts/harness_checkpoints.py` | 保存/加载检查点时加入当前Python任务的环境声明身份；配置改变拒绝，不迁移旧身份 |
| `scripts/task_service_store.py` | 自动目录明确限制`language=python`；原无language字段按Python解释，不改题目manifest |
| `scripts/task_service.py`、`scripts/execution_panel.js`、`scripts/task_panel.html` | 原认证下GET声明、POST显式检查、缓存及只用textContent显示；空对象之外的命令/路径输入直接拒绝 |

`ExecutionBackend`接收显式argv、cwd、env、timeout和检查回调。修复者的动作仍通过原GitBash环境及权限/状态钩子；本机验收进程通过argv接口使用已有取消/期限回调。原修复Shell仍按原动作/命令边界检查取消，未宣称本版为每个任意Shell命令新增即时中断能力。

后端不重置费用、角色或任务。输出预算仍由原`ToolOutputBudget`处理；没有把原全文采集替换成新截断。超时仍返回原`TimeoutExpired`字段与失败返回码，提交标记仍交给上游处理。本轮执行后端新建的子进程使用Windows隐藏窗口参数。

环境声明记录Python的prefix/base_prefix/site-packages和选定包的安装元数据、四份原公共依赖清单哈希。Node不做npm install，不探测在线包源，不根据Node版本自动开放新任务。

续做比较包括声明配置哈希、平台、Python路径/版本/目录、Bash/Git路径、所列Python包元数据和依赖清单；**不是完整二进制或所有传递依赖的可信证明**，不保证恶意篡改可被此声明发现，也不实现跨电脑环境迁移。实际Bash/Node版本探测与声明匹配各有自己的范围，不混为一项全面验收。

## 必要检查的实际结果

- 实际版本命令：Python3.10.6（当前进程）、GNU Bash5.2.37、Node24.21.0、Git2.49.0.windows.1。只证明当前机器此时的声明/探测结果。
- 本机后端实际核对中文输出/退出码、Python选择、pipefail、删除拒绝、Shell超时、提交信号、敏感环境变量排除、序列化、Node缺失时Python不受影响、环境声明变化和所属argv进程取消退出。
- 一条三角色合成链：文档后暂停，改Bash声明被拒且不产生子身份，恢复声明后新子身份完成初修→审查→返工→最终核查。原payload保持，完成查阅不重跑，环境声明已存；最终repair passed、approve，来源两段共15个合成尝试。模型回复/用量为预设，本机源码、测试/重放、MCP子进程实际执行，不是新真实模型成绩；原全量659/660计时差异保持。
- 原有8项执行/导入/输出/取消相关兼容断言通过。后续核对发现原JS开发夹具会进入通用目录，因此额外增加Python目录条件，并用隔离HTTP与原2项目录/幂等断言核对。该目录小修晚于上述链，保留各自当时冻结输入，不把早先链说成最后代码的全套回归。
- 隔离HTTP核对GET不启动进程、资源与认证、拒绝请求中命令/路径、固定版本探测、缓存时间、Python自动目录、无新增任务。没有Supervisor/worker/模型；临时服务已关闭。
- 页面做Node假DOM下按按钮才检查、成功/失败收尾和脚本语法核对；**本轮未实际点击浏览器**。Work设置已关闭浏览器控制，未修改设置或绕过限制。
- 按用户要求没有完整默认/上游适配回归，也没有新增正式测试项目。此前适配中的6项环境差异不能因本轮基本检查被视为解决，整体兼容与真实使用后续集中验收。

过程材料位于`temp/harness_runtime_20261026/`，源码/声明/必要核对与完整合成链封存于`experiments/harness_runtime_20261026/`，保护与加载收尾以核对文件为准。未改旧任务输入、既有封存、正式52条payload或2016请求共享账本；0真实模型调用。

收尾复核：5374份原配置/数据/评价文件哈希及52条正式payload保持，共享账本2016请求，实时附加表未创建。8765空闲服务经原停止接口正常更新，健康与新JS资源已核对，未创建正式任务。39封存另核对外层哈希；本轮源码与归档成员做两套私有凭据扫描，结果留在封存核对文件。

## 下一项

第二组的首版工具/Skills/MCP和本机执行接口已有实现及有限接线证据。接下来按35第三组推进模型能力与角色配置适配；不把它等同于模型真实效果已验收。通用JS评分/服务、容器、不可信代码隔离、异机安装和完整系统测试仍后置，不恢复旧付费身份或冻结批次。
