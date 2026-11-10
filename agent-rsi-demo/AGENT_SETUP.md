# 如何建立并启动本项目的 Agent

本项目已经实现 Agent 的程序骨架。这里的“建立 Agent”是给现成模型配置角色、任务上下文、读源码/改源码能力和反馈循环；提升的是电路生成 workflow 的代码与决策，不需要训练 Claude 的模型权重。

## 1. 谁负责什么

| 组成 | 本项目的实现 | 职责 |
| --- | --- | --- |
| 分析 Agent `analyst` | `coding_agent.py` 的 `_ANALYST` 和 `analyse()` | 阅读搜索样例的实际图形、指标、错误及当前代码，提出一个可检验的新举措 |
| 实现 Agent `implementer` | `_IMPLEMENTER` 和 `implement()` | 根据假设卡补读源码，在候选目录实现代码修改，提交完整文件替换 |
| 可选评审 `reviewer` | `_REVIEWER` 和 `review()` | 对举措和 diff 提供建议；默认关闭 |
| 可用操作 | 受限源码读取、文件替换与 diff 记录 | 读取完整算法或已新增 helper；修改允许范围的生成代码 |
| 领域适配器 | `circuit_adapter.py` | 建立独立候选、提供上下文、执行真实出图、收集独立验收结果 |
| RSI 调度与决策 | `rsi_core.py` + `vendor/rsi` | 调用原 RSI v4.1 核心，完成校准、配对验证、复测、采用或保留父版本 |

两个 Agent 可以共用同一个 Claude 模型：它们是使用不同角色指令和上下文的两种调用，不要求手工创建两个 Claude 会话。配置不同模型后也可以分工。循环记忆来自已采用的代码、当前搜索轨迹和离散历史标签，不依赖一个始终保持打开的聊天会话。

## 2. 在另一台电脑建立环境

解压整个可移植包，进入 `agent-rsi-demo`。安装 Python 3.12 后执行：

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-index --find-links .\wheels -r .\requirements.txt
Copy-Item .\agent-config.example.json .\agent-config.local.json
```

打开 `agent-config.local.json`，确认 `roles.analyst.model` 和 `roles.implementer.model` 是你的账户可用的模型 ID。开始时可以让两个角色使用同一个 Claude，`reviewer` 保持 `null`。

## 3. 选择接入方式并启动

### 方式 A：使用已经登录的 Claude Code

按 [Claude Code 官方安装说明](https://code.claude.com/docs/en/setup)安装 CLI，并完成登录：

```powershell
claude --version
claude auth login
claude auth status
```

认证命令见[官方 CLI 参考](https://code.claude.com/docs/en/cli-reference)与[认证说明](https://code.claude.com/docs/en/authentication)。本项目通过已登录 CLI 调用模型，这种方式不要求另外设置本项目的 API 密钥。

在独立 PowerShell 终端中启动真实的 Agent 循环：

```powershell
.\.venv\Scripts\python.exe .\run_demo.py --mode claude --config .\agent-config.local.json --rounds 3 --hours 1 --out runs\claude-first
```

程序自动建立分析和实现调用，传入角色指令、项目上下文及搜索证据，应用真实源码修改，然后交给 RSI 评估。每个新实验使用新的空输出目录。若你正在与交互式 Claude Code 对话，请在另一个独立终端运行这条命令，避免嵌套启动 CLI。

### 方式 B：直接使用模型 API

保留示例中的 `provider: "anthropic"` 和 `api_key_env: "ANTHROPIC_API_KEY"`，确认模型 ID 可用，然后执行：

```powershell
$env:ANTHROPIC_API_KEY = '你的 API 密钥'
.\.venv\Scripts\python.exe .\run_demo.py --mode api --config .\agent-config.local.json --rounds 3 --hours 1 --out runs\api-first
```

API 密钥从环境变量读取。分析和实现角色可以配置不同模型；OpenAI 兼容接口的配置例子见 [README](README.md)。先用一个模型跑通两个角色，再换模型做对照即可。

## 4. Agent 怎样读代码、加入新举措

每轮程序提供 [项目上下文](PROJECT_CONTEXT.md)、[优化范围](OPTIMIZATION_SCOPE.md)、当前源码目录及部分完整源码。分析角色还会收到搜索样例的实际 SVG、布局/线路、错误与指标，以及之前假设的离散结果标签。

模型需要补读源码时，可先返回：

```json
{"read_files": ["engine/place-and-route.py", "engine/strategies/new_placement.py"]}
```

程序检查路径、读取完整文件并继续调用模型。文件必须已经存在；新 helper 创建后，后续轮次也能补读和修改。默认最多 4 次读取回合，合并上下文最多 400000 字符，由 `max_source_rounds`、`max_context_chars` 控制。

分析角色最终提交假设卡，例如：

```json
{
  "failure_mode_desc": "搜索样例中反馈支路距离较远，形成外围绕行",
  "change_hypothesis": "增加反馈器件近邻初排，保留原始引脚和完整几何约束，检验实际线长是否下降",
  "ref": "feedback-neighbour-seed"
}
```

实现角色随后返回 `summary` 与 `edits`；每个 edit 包含 `path` 和修改后完整 Python 源码 `content`。程序先检查允许路径、文件大小和语法，再写候选文件，并记录 diff 和前后哈希。模型可以新增 `engine/strategies/*.py` 并在入口接入，也可以修改允许的现有生成模块。

```text
当前采用版本 → 搜索证据 → 分析 Agent 提出举措 → 实现 Agent 修改候选源码
      ↑                                             ↓
      └── 采用新版本或保留父版本 ← RSI 配对验证/复测 ← 实际出图与独立验收
```

验证集和封存测试的案例结果不会交给提案 Agent。任意验证图非法都不能通过采用守护；评审角色的建议也不能替代固定验收。

## 5. 怎样确认 Agent 确实工作了

- `agent/analysis-*.json`：实际提出了什么失败归因与新举措。
- `workspaces/*/change.diff`、`agent-report.json`：实际改了哪些代码、补读了哪些源码、调用了哪个角色和模型。
- `method/ledger.json`：候选如何验证、是否复测、为何采用或未采用。
- `report.html`：实际生成的电路图、基础版/采用版/参考版的指标和差距。

`--mode smoke` 使用固定源码举措验证接线，不调用模型。它适合先检查环境，不能作为真实 Agent 自主改进的证据。

v2 的每个候选还要通过44项固定兼容性测试和搜索smoke。格式或语法错误默认最多修复2次，执行失败默认最多修复1次；失败响应、日志及诊断会保留。可读性评价使用候选原生SVG中的标签碰撞、遮挡、裁切和画布留白，不是视觉模型审美打分。封存终评会配对测量初始版和采用版。

## 续跑或接着上一版继续

```powershell
# 同一实验，继续3轮；保留原模型/套件/总预算
.\.venv\Scripts\python.exe run_demo.py --resume --out runs\claude-first --rounds 3
# 新预算或新模型：继承上一版已采用源码，在新目录重新校准
.\.venv\Scripts\python.exe run_demo.py --from-version runs\claude-first --mode claude --hours 1 --out runs\claude-next
```

总时间预算会传到模型、回归、出图和验收进程；中断会停止本地拥有的进程。已经耗尽注册预算时用 `--from-version` 新开实验。旧v1运行仅保留历史报告或作为源码起点，不与新版评分混用。

## 6. 如果你想自己扩展 Agent

- 改角色的工作方式：修改 `coding_agent.py` 中对应角色指令及调用逻辑；电路优化方向优先写进 `OPTIMIZATION_SCOPE.md`。
- 换模型或让分析/实现用不同模型：修改 `agent-config.local.json` 的现有角色配置。
- 增加一种生成算法：让实现角色新增 helper，并接入候选入口；不必新增 Agent 角色。
- 新增一个真正的 Agent 角色：除了模型配置，还需要在 `coding_agent.py` 定义职责、输入/输出，在 `CircuitAdapter` 对应阶段接入调用。仅在 JSON 中增加角色名称不会自动执行它。

修改 Agent 或固定实验材料后，请启动新的实验目录，保持旧实验的源码身份和证据可追溯。固定验收、评分、输入网表和符号资源的边界仍按优化范围文档执行。

## 可直接给目标电脑 Claude 的说明

> 请先阅读 AGENT_SETUP.md、PROJECT_CONTEXT.md、OPTIMIZATION_SCOPE.md 和 CLAUDE_START.md。我要用现有 RSI v4.1 框架，让分析/实现 Agent 持续提出并实现新的电路图生成举措，最终接近冻结的 gongzuo 参考能力。先检查 Python、模型接入和角色配置，然后告诉我在独立终端运行的实际命令；保持固定验收和原始网表，保留每轮代码 diff、测量和采用理由。不要将 smoke 的预设举措称为真实模型创新。
