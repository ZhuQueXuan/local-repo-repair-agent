# 从 GitHub 获取与启动

仓库名称：**本地仓库修复 Agent / local-repo-repair-agent**。这是当前工程原型的私有源码与实验记录归档，暂未完成空白机器安装验收。

## 获取项目

```powershell
git clone --recurse-submodules https://github.com/ZhuQueXuan/local-repo-repair-agent.git
cd local-repo-repair-agent

# 若已经普通 clone，补取固定上游版本：
git submodule update --init --recursive

# 建立本机环境；原开发环境是 Windows、Python 3.10.6、Git Bash。
py -3.10 -m venv .venv
.venv\Scripts\python.exe -m pip install -e .\mini-swe-agent
.venv\Scripts\python.exe -m pip install -r configs/evaluation-requirements.txt -r configs/retrieval-evaluation-requirements.txt
New-Item -ItemType Directory -Force temp | Out-Null
Copy-Item .env.example .env
```

依赖命令供重新安装使用，本次未重新下载/安装依赖，也未验证全新电脑可用。`configs/model-requirements.txt` 等是原环境快照，可供排查版本；不要把依赖快照理解为当前上游最新版或平台通用锁文件。

先不填写私有模型密钥，可做不调用模型的演示：

```powershell
.venv\Scripts\python.exe -B scripts/document_agent_demo.py
```

在现有 Windows 环境中双击 `启动任务面板.cmd` 打开本机面板。Git Bash 默认路径是 `D:\Git\bin\bash.exe`；其他路径在启动前设置进程环境变量 `MINI_TASK_BASH`。真实运行前自行在本机 `.env` 配置模型凭据，字段名和路由见 `.env.example`、`configs/provider_priority.json`。不要将 `.env`、token 或个人账户数据提交到 Git。

## 上传范围

保留 `scripts/`、公共 `configs/`、`Project/`、题目与评价输入、示例，以及大部分正式 `experiments/` 诊断报告和复现材料。上游底座与一个原有独立题目仓库使用固定 Git 子模块，避免丢失来源或重复提交它们的 Git 历史；其余冻结题目源码直接保存，原版权/许可声明保留。

以下仍在原电脑，不随 GitHub 归档上传：

- `.env`、`.venv/`、所有层级的 `temp/`、缓存和实时 `data/`（包括当前52条任务记录、共享2016请求账本及本机服务认证信息）。实验封存中用于离线复现的历史副本单独保留，不会导入为新电脑的实时账本。
- `experiments/multi_agent_eval_prep_20261025/development-qualification.zip` 和 `qualification.zip`：两份重复的大体积资格核验封存，原件留在本机。其哈希清单和阶段记录保留，因此不能宣称 GitHub 副本包含全部封存文件。旧冻结本身已因后续开发失效，不可据此启动付费批次。
- `experiments/adjudication_panel_20261024/regression.zip`、`experiments/self_review_20261025/regression.zip`、`experiments/multi_agent_eval_prep_20261025/regression.zip`：原测试环境包内含上游配置测试生成的`.env`夹具。本次扫描没有命中两套真实凭据；为保持不上传环境文件的边界，这三份原包也完整留在本机，不修改封存内容。

GitHub 上不包含兄弟目录 `job/` 与 `bishe_improve/`。阶段文档中的本机绝对路径、跨项目链接和旧实验路径是历史记录，可能无法在新电脑直接打开。源文件按原字节保存，避免换行转换破坏题目和证据哈希。

新电脑的面板会创建自己的本机历史；不会显示原电脑的实时52条记录，也不会自动恢复旧失败或暂停身份。已有封存包含开发轨迹和历史机器路径，请保持仓库私有；未来公开展示时另做面向展示的整理。

本次上传仅检查提交范围、密钥和文件完整性；不追加模型请求，不重跑全量测试。当前功能和验证边界以 [阶段37](Project/37_过程_Harness阶段暂停与检查点续做.md) 为准。

后续开发新增了[阶段39工具/Skills](Project/39_过程_Harness只读工具注册与Skills扩展.md)与[阶段40执行环境](Project/40_过程_执行后端接口与Python_Node环境声明.md)。当前环境检查入口为面板“检查执行环境”，或`.venv\Scripts\python.exe -B scripts/execution_runtime.py --probe`。`MINI_TASK_BASH`统一指定本机Bash；可选`MINI_TASK_NODE`仅指定检查用Node，不改原JS桥。新电脑仍须自行建立环境、填写私有凭据；版本探测通过不代表空白机器或完整功能验收。
