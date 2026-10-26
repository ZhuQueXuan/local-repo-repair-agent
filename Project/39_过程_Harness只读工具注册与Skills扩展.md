# Harness 只读工具注册、Skills 与本地 MCP 接入

日期：2026-10-26。接续35框架的第二组，功能优先，仅做必要检查；不启动真实模型或新题效果批次。

## 这一版解决什么问题

之前文档角色和审查角色各自定义工具参数、暴露模型工具列表、执行权限校验和记录调用。新增能力需要在多个位置接线，也没有可按需读取的项目工作方法包。

本版用一个小注册接口描述名称、参数、角色、只读/提交性质、输出上限和传输方式。原有Broker继续负责不可变快照、身份、交接范围和证据检查；注册不产生额外文件权限。新增的MCP变更概览用于定位改动，不输出可引用的证据ID；Skills用于提示怎么查阅与审查，不授予执行权限，也不代表完成验收。

本轮只覆盖新建的普通双/三角色任务，**默认关闭**。单Agent的Bash路径、S+与诊断不接此扩展。原默认运行仍使用原工具集合，不将当前扩展成绩外推到这些分支。

## 开源方案核对与取舍

先核对官方GitHub项目、源文档与许可证，未复制其实现或安装整个新框架：

| 方案 | 本轮参考和匹配程度 | 使用许可及改造取舍 |
| --- | --- | --- |
| [PydanticAI工具定义](https://github.com/pydantic/pydantic-ai/blob/main/pydantic_ai_slim/pydantic_ai/tools.py) | 工具定义、参数Schema和执行边界分开，适合当前两个已有Broker | [MIT](https://github.com/pydantic/pydantic-ai/blob/main/LICENSE)。本项目已有Pydantic严格参数校验，保留它并增加薄注册层；引入整个Agent框架会重复循环、路由和记账，本轮不安装 |
| [Agent Skills规范](https://github.com/agentskills/agentskills)、[格式](https://agentskills.io/specification) | `SKILL.md`元数据与正文，先提供目录，模型按名称/哈希读取正文 | 官方规范仓库[Apache-2.0](https://github.com/agentskills/agentskills/blob/main/LICENSE)。本轮两个方法包自行撰写，支持格式的有限子集；不实现通用技能市场、外部安装或脚本执行 |
| [官方MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 长期通用客户端的优先复用候选；stdio适合本机只读能力 | [MIT](https://github.com/modelcontextprotocol/python-sdk/blob/main/LICENSE)。本机未安装；核对时主分支[要求Python>=3.11](https://github.com/modelcontextprotocol/python-sdk/blob/main/pyproject.toml)，现有环境3.10.6。不据此断言所有旧SDK版本都不兼容；本轮避免升级主环境，实现固定工具的有限协议子集，未来可用独立工具环境/兼容SDK替换传输层 |

上述官方仓库和源文档于本轮在线查阅，作为当前接口与许可依据；未以星数或未经核实的维护频率作结论。MCP实现依据[2025-06-18生命周期](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)、[stdio传输](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)、[Tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)。没有宣称该日期是最新协议或获得完整兼容认证。

## 用户如何使用

1. 在原任务面板选择“真实Agent运行”和普通双角色或三角色。
2. 按需要勾选新增的“启用只读工具与Skills扩展（实验）”。不勾选保持原行为；任务卡、阶段暂停仍有自己的独立开关。
3. 开启后，文档角色可以按需读取`document-consultation`，审查角色可以读取`evidence-review`并调用`read_change_summary`。模型是否采用这些方法仍由后续真实验收判断。
4. 任务会显示扩展已启用；事件区有“只读工具调用记录”，成果中可打开各阶段`*-tool-registry.json`查看工具、结果哈希、拒绝原因类型和MCP往返记录。
5. 同时启用阶段暂停时，续做继承该配置；不能在同一个暂停身份上偷偷换工具集合。原暂停记录、已完成阶段及链路费用保持。

勾选后真正创建模型任务仍会产生原模型调用。本轮没有在正式库创建试验任务。

## 程序怎样工作

### 注册与执行

`ToolSpec`和`ToolRegistry`统一参数、角色、只读/提交性质、传输类型、结果字节上限及模型Schema导出。只有程序显式注册的工具可调用。

`CapabilityBroker`在原Broker外增加注册检查和调用凭证。原工具调用仍交回原`DocumentBroker`或`ReviewEvidenceBroker`，身份、快照、授权文档和已发证据仍由原实现决定。调用前后检查取消、期限、快照与本轮能力源文件哈希；哈希变化停止，不迁移到新版本继续执行。

结果凭证包含角色/阶段、参数哈希、输出哈希/字节、完成或拒绝、传输类型。Skills/MCP自身不伪造原Broker的证据事件序号。原有证据事件与新注册凭证分开保存，避免破坏旧引用。

### Skills：先目录，再正文

两个方法包放在`skills/`，使用名称、描述、`metadata.version`和`allowed-tools`。正文仅在模型显式调用`read_skill(skill, sha256)`时进入模型上下文。程序需要读取文件以校验元数据与完整哈希，这不是延迟到调用时才首次访问磁盘。

只允许固定名称、固定角色和1.0.0版本；`allowed-tools`必须是调用角色已经拥有的工具子集，不是新增授权。拒绝路径/外部目录/自注册工具，不执行Skill脚本，不递归加载Markdown引用。单文件最多8192字节；读取时同时核对目录哈希和原运行冻结版本。

### 实际 stdio MCP 调用

模型调用`read_change_summary(candidate_id)`；协调程序核对候选和交接授权后，读取已有不可变diff，将补丁文本、候选/源码树/补丁哈希传给固定本机MCP工具`change_overview`。不传主机目录、API密钥或用户提供的可执行命令。

客户端实际启动受信任Python子进程，逐步执行`initialize`→`notifications/initialized`→`tools/list`→`tools/call`。校验服务工具定义、协议、响应ID、版本绑定和文本/结构结果一致。执行的是已核对哈希的内置服务源码，避免文件在检查后被换成另一份执行内容。环境仅传Windows必要目录和临时目录，不继承模型凭据；不使用shell。

服务仅解析收到的unified diff，返回改动文件、增删行及hunk数量，最多预览20文件。它不打开仓库文件、不执行仓库命令、不连接网络。单补丁上限512KiB，同时受原快照读取限制；请求帧1MiB，响应帧16KiB，执行不超过8秒且不能超过当前角色剩余时间。取消/超时会结束所属子进程，不继续下个工具；正常完成也关闭进程。

这是一个固定工具的MCP生命周期/Tools子集，未实现HTTP、远程服务、资源/提示市场、sampling、elicitation、任意外部服务安装或完整MCP一致性测试。只读提示不是OS沙箱，当前本机修复Bash也仍不是OS沙箱。

变更概览只是导航。它没有`evidence_id`，不能代替`read_diff`、`read_lines`或程序测试报告；缺陷判断和修复验收继续沿用原标准。

## 代码入口

| 文件 | 主要类/函数与职责 |
| --- | --- |
| `scripts/harness_tool_registry.py` | `ToolSpec`、`ToolRegistry`：显式注册、角色/Schema、输出与传输声明 |
| `scripts/harness_capabilities.py` | `CapabilityBroker`、`configure_capabilities`：可选包装原Broker、按调用留证、冻结版本与只读扩展 |
| `scripts/harness_skills.py`、`skills/*/SKILL.md` | `SkillCatalog`：受控方法包、元数据/正文、允许工具引用与哈希 |
| `scripts/harness_mcp_client.py`、`scripts/harness_mcp_server.py` | 实际stdio握手、发现、调用与收尾；固定diff导航工具 |
| `scripts/review_model.py`、`scripts/collaboration_live.py` | 给本轮开启的角色暴露注册Schema/技能目录，保存阶段凭证 |
| `scripts/collaboration_store.py`、`scripts/collaboration_panel.py` | 新建时布尔开关、幂等绑定、可见状态；没有新增数据库表 |
| `scripts/harness_checkpoints.py`、`scripts/task_service_worker.py` | 续做继承开关；源码、Skills、页面资源纳入新运行冻结 |
| `scripts/capability_panel.js`、`scripts/task_panel.html`、`scripts/task_service.py` | 默认关闭的开关、运行标记、JS资源与原认证接口 |
| `scripts/document_agent_demo.py` | 原合成演示增加可选Skill/MCP动作，仍无真实模型入口 |

## 必要检查与实际限制

- 最终带版本绑定的五阶段离线链通过：文档4、初修2、首审4、返工2、最终核查3，共15个合成平台尝试（包含原演示的1次越权Bash回复拒绝）。修复passed、审查approve；Skill按需加载，审查两个阶段实际启动MCP子进程完成三次往返。角色工具集合没有给修复者新增本轮只读工具，也没有给文档/审查者暴露Bash。
- 一条文档阶段后暂停的链路通过：新子身份继承开关，原查阅不重跑，旧payload保持，剩余修复/审查通过；来源链2段/9次合成尝试。此链在后续源代码绑定补充前执行，保留其当时冻结输入；最后源代码绑定另由上述完整链和定向边界核对，不冒充每次改动后全部重跑。
- 定向核对角色权限、候选绑定、Skill角色/哈希、真实MCP调用、快照字节保持、无伪造证据、取消、补丁哈希拒绝与hunk中类似文件头的文本。原有6项工具/文档/调度兼容检查通过，没有跑全量回归或新增正式测试文件。
- 隔离HTTP实际核对新开关、认证、同键幂等/配置冲突、默认关闭、布尔与不支持模式拒绝。临时服务无Supervisor/worker，创建的两条临时任务均取消；未发模型请求。
- 页面仅做HTTP/资源检查、JavaScript语法和Node假DOM下开关/切换/历史显示核对。Work设置关闭浏览器控制，本轮未实际点击；不能把假DOM说成浏览器验收。
- 第一次链路在受限环境的Git Bash本机对象创建处失败，第一次HTTP检查因本机套接字权限失败，均留证；改用允许的本机权限后通过，未修改权限策略或伪造成功。预设模型回复不是新模型能力成绩；实际源码、验收及补丁重放由本机执行，原全量659/660计时差异继续保留。

过程材料位于`temp/harness_tools_20261026/`，源码、最终链路、暂停链、失败记录和核对材料封存于`experiments/harness_tools_20261026/`。最终保护收尾以该目录核对文件为准；正式运行仍52条、共享2016请求，本轮0真实模型调用。

收尾复核：5374份原配置/数据/评价文件哈希保持，52条正式payload逐字保持，实时附加表未新建；原36封存10份文件保持。8765空闲面板经原shutdown正常更新，健康接口和新JS资源已加载，未创建正式任务。没有新增正式测试文件或全量回归；两套凭据扫描及压缩包成员/外层哈希留在本轮封存核对材料。

## 当前边界与下一项

已完成第二组里的首个只读工具/Skills/MCP增量。真实模型是否正确选用技能、是否改善审查、任意外部MCP兼容性、页面点击和异常使用仍待统一测试。

原框架第二组还包含执行环境边界与Python/Node环境声明，尚未因本版自动完成。下一步先把现有Windows执行方式和环境信息整理成明确接口，再进入第三组模型协议/角色适配。旧baseline/失败/冻结/账本保留，不恢复旧付费身份，不自动安装Docker或更换底座。
