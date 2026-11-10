# Claude Agent RSI：真实模拟电路图生成

目标是在 Agent loop 中不断**新增或修改生成方法的源码**，让最基础的摆件/布线逐步接近当前 gongzuo 项目的成图能力。不是只从固定参数表选择。Claude 分析 search 实测轨迹，提出单一举措，在隔离候选中写代码；原 RSI v4.1 核心执行校准、验证、复测和采用，未采用的代码不会前移当前版本。

## 已配齐的基础

- `engine/`：当前项目源码和真实器件资源。支持当前 SPICE 子集、MOS/BJT/R/C/L/D/源/运放代理、真实引脚与旋转镜像、电源/地标记、SA、结构化布局、A*、失败恢复、安全缩线、JSON 和双 SVG。
- `reference/engine/`：当前完整流程的冻结参考，同时为独立验收提供固定解析、几何规则和渲染。110 个符号 definitions 与 geometry 均本地提供，SVG 自带画布，无需另装网页应用。
- `vendor/rsi/`：你提供的 `feat/method-v4.1` 方法核心原字节副本（来源提交及哈希见 SOURCE_MANIFEST）。实际调用 `V4Run`、`V4Config`、`SystemAdapter` 与 `decide`，不用 SN 专属工作台 CLI。
- `circuit_adapter.py` / `coding_agent.py`：电路领域适配和真实源码编辑。每个候选有独立目录、内容哈希、diff、模型请求角色记录。分析/实现/可选建议评审可用不同模型。
- `benchmarks/`：v2 默认 3 个 search、8 个 validation、2 个封存 test；扩展 v2 套件共 32 个任务。验证覆盖无源、MOS、BJT、二极管、差分对、级联镜、反馈及 include 子电路。旧 `suite.json` 保留为快速接线用的小套件。
- `PROJECT_CONTEXT.md` 总结原项目决策和能力；`OPTIMIZATION_SCOPE.md` 给 Agent 提供举措范围、可编辑接口和硬要求；`CLAUDE_START.md` 可直接交给另一台电脑的 Claude 阅读。

初始入口 `engine/candidate_entry.py` 用基础自由网格 SA，默认不做结构摆件/安全缩线，但使用真实器件、最终合法布线及验收。完整能力已经随包提供，Agent 可以复用这些模块、改变调用决策，也可以在允许范围新增算法。参考入口使用当前完整结构化流程。

## 复制与安装

复制整个文件夹，或解压旁边的 `agent-rsi-demo-portable.zip`。不需要父目录 `gongzuo` 或 `RSI` checkout。需要 Python 3.12；NetworkX wheel 已随包提供，可离线安装：

```powershell
cd D:\你的路径\agent-rsi-demo
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-index --find-links .\wheels -r .\requirements.txt
```

## 用 Claude 真正改代码

第一次使用请先读 [如何建立并启动 Agent](AGENT_SETUP.md)：其中解释角色如何建立、怎样读取和修改源码，以及从环境配置到运行的完整步骤。

目标电脑安装并登录 Claude Code 后，在独立 PowerShell 终端运行：

```powershell
.\.venv\Scripts\python.exe .\run_demo.py --mode claude --rounds 3 --hours 1 --out runs\claude-01
```

程序会自动反复调用 Claude Code 的分析/实现角色。调用使用 `--print` 取得结构化提案和完整文件替换，由同一个本地检查器应用真正源码修改。不要在已有 Claude Code 交互会话中递归启动该命令，避免嵌套 CLI 会话。

使用 API 或多个模型时，复制 `agent-config.example.json` 为 `agent-config.local.json`，按账户可用型号修改 analyst/implementer/reviewer；密钥只存环境变量：

```powershell
$env:ANTHROPIC_API_KEY = '你的密钥'
.\.venv\Scripts\python.exe .\run_demo.py --mode api --config agent-config.local.json --rounds 3 --hours 1 --out runs\api-01
```

分析与实现默认使用同一个 Claude 型号；可以各指定不同 Claude，或把一个角色设为 `provider: "openai_compatible"` 并提供 `base_url`（包括 `/v1`）与 `api_key_env`。程序会追加 `/chat/completions`。reviewer 为 null 时不调用；配置时只作基于搜索上下文与diff的建议评审，不代替 RSI 的固定采用规则，也不放宽机制门槛。

新实验和 baseline 必须使用新的空输出目录，baseline 不能覆盖已有采用版本。默认每图 SA 200 步、每次生成最长180秒；`--iterations` 与 `--case-timeout` 可调整。`--hours` 覆盖本次命令的模型请求、回归、生成和验收；本地进程共用剩余预算并支持取消，收尾写入账本仍需少量时间。模型用量/费用依服务而定，当前没有伪造费用估计。

## 续跑、接续版本与失败修复

中断或一批轮次结束后，在注册总预算未耗尽时继续原状态；`--rounds` 表示再增加的轮数：

```powershell
.\.venv\Scripts\python.exe run_demo.py --resume --out runs\claude-01 --rounds 3
```

续跑保持原模型配置、套件、评分和总预算不变。需要新的预算、模型、套件，或从旧版本交付结果起步时，在新目录接续已采用源码：

```powershell
.\.venv\Scripts\python.exe run_demo.py --from-version runs\claude-01 --mode claude --hours 1 --rounds 3 --out runs\claude-02
```

也支持从 `versions/<hash前20位>/` 或完整 engine 目录起步；会重新核验源码哈希和兼容性。旧版实验可打开原报告或通过 `--from-version` 作为新实验起点，不原地混用新评分。跨电脑复制后也建议用新目录接续代码。导出采用版可用 `--stage export --out runs\claude-01 --export-to exported\best`。

模型 JSON/schema/语法错误默认最多修复2次；候选通过固定兼容性测试和 search smoke 后才能进入验证，执行失败默认还有1次针对性修复。失败原始响应、补丁、堆栈、路由诊断和修复反馈会保存；只有公开兼容性/search 诊断会返回 Agent，验证/封存结果不进入提案上下文。鉴权错误会明确失败，可修复凭据后续跑。

每个源码版本默认执行44项冻结兼容性检查（解析、几何规则、路由），结果按版本缓存。`--regression full` 可使用全部783项；`--regression off` 是显式实验变体，会记录在运行身份中。全局预算或验收基础设施超时不会缓存成候选零分。

## 查看和衡量图形

打开 `runs/claude-01/report.html`：展示基础版、最终采用版、参考版的真实 SVG 与逐图指标。`method/ledger.json` 保留原 RSI 决策；`workspaces/*/change.diff` 和 `agent-report.json` 保留源码举措；`measurements/` 保留每个版本/样例/种子的真实产物、日志及独立验收结果。

测量冻结的当前 gongzuo 参考能力：

```powershell
.\.venv\Scripts\python.exe .\run_demo.py --stage reference --out runs\claude-01 --reference-iterations 20000 --case-timeout 600
```

结束优化后才测封存集：相同任务/种子同时测初始版、最终采用版，可选当前参考，保存配对分差；结果不回流给提案 Agent：

```powershell
.\.venv\Scripts\python.exe .\run_demo.py --stage endpoint --out runs\claude-01 --reference --case-timeout 600
```

需要更多电路时，在新实验使用 `--suite benchmarks/suite.full.v2.json`；额外任务属于封存 test。扩大训练范围应在新套件中预先划分，不能把已观测终评当成未见测试。v2 重新划定代表性验证集，不将旧 v1 成绩当作未见证据。`--stage inspect --out runs\claude-01` 可另外测齐最终版本的搜索/验证图，不修改采用指针。

## 评价与允许举措

独立进程用原始网表重建器件/可见引脚，递归冻结 include/inc 文件，固定线宽2.5、异网净距6、halo12、交叉净区12，复验连接、器件重叠、正交、穿体、固定出线和供电。随后检查候选实际 SVG 的符号、线路、供电图元与合法几何一致，防止空图、缺件、改线、隐藏或裁切实际电路；固定重绘副本仍保留供审计。报告显示通过检查的候选原生 SVG。

几何 `Q = 线长/100 + 1.5×交叉 + 0.15×折角 + 0.5×超HPWL线长/100 + 结构错位 + 结构分散`。v2 总Q再加展示惩罚：`2×标签互撞 + 标签碰体 + 0.2×标签碰线 + 2×标签裁切 + 0.05×max(0,画布/电路面积比−2)`，越小越好；RSI 主分数 `100/(100+总Q)`，非法图计0。可读性采用确定性的文本包围盒估计，不冒充人类或视觉模型审美评分。

A/A 校准波动，候选和父版本使用匹配样例/种子，默认须 `Δ>0 且 Δ≥SE`，再经过复测。任意验证图非法不能采用；逐图交叉不得增加，线长最大允许增加15%。合法十字交叉允许存在，验收不代表电路仿真。完整规则见 OPTIMIZATION_SCOPE。

Agent 可以改解析前处理、层级/结构证据、候选种子、SA目标和动作、无源近邻、路由顺序与避障、恢复及安全缩线，可以新增 `engine/strategies/*.py`。模型可先返回 read_files 请求，读取完整基础 SA、解析、渲染及已新增 helper 后继续改码。固定验收/评分、数据集、符号资源、RSI核心禁止修改；精确 allowlist 由实现调用提供。候选源码执行是本机 Python 进程，编辑与哈希边界不声称是操作系统安全沙箱。

## 先做不调用模型的接线检查

```powershell
.\.venv\Scripts\python.exe .\run_demo.py --mode smoke --rounds 1 --out runs\smoke-01 --reference
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m unittest discover -s engine\tests -q
```

`smoke` 使用明确标记 synthetic 的固定源码举措（启用 reference、再尝试 clean 展示）检查多轮、采用或拒绝与续跑；不调用真实模型，不证明自主创新。`runs/ready-v2/report.html` 为新版接线示例；旧 `runs/ready/` 使用旧评分，仅作历史查看。

如果只想先生成一张当前能力的真实图：

```powershell
.\.venv\Scripts\python.exe engine\candidate_entry.py --input engine\examples\3.cir --profile reference --out runs\one-circuit
```

本次测试与真实工件见 VALIDATION.md。此环境未配置 Claude API key，也没有可用 Claude CLI，真实模型多轮验收仍需在目标电脑运行；不能把离线 smoke 或模拟响应当作真实模型证据。符号及许可证见 engine/THIRD_PARTY_NOTICES.md。



## 交付打包与本机验证边界

本机仅完成交付前针对性验证和不调用模型的两轮接线演示，不在本机展开真实Claude长时间优化或32例全套实验；详见 [VALIDATION.md](VALIDATION.md)。示例报告供查看，目标电脑请使用新输出目录启动。

修改后可重新生成干净的可移植包：

```powershell
python package_demo.py
```

打包器包含完整运行源码、器件资源、离线依赖、测试和ready-v2示例，排除私有模型配置、虚拟环境、其他开发运行及临时回归副本；包含逐文件SHA256清单。
