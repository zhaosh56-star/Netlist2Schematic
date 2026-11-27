# Netlist2Schematic

将 SPICE 网表转换为可复验的布局 JSON、彩色 SVG、黑白 SVG 和正式 SVG。算法根据真实连接识别子结构、组织有源支路，再插入无源及源器件模块，经 SA、正交 A*、完整连接与几何验收、安全缩线后出图。原网表不改动，GT 不参与坐标生成。

**文档按 2026-11-27 的源码核对。** 从[文档目录](docs/README.md)进入；理解算法看[通俗流程详解](docs/flow-walkthrough.md)，查参数看[运行指南](docs/flow-guide.md)。历史实验与当时的测试数字已移至 [docs/history/](docs/history/README.md)，不能用作当前默认值。

## 运行

在项目目录打开 PowerShell：

```powershell
Set-Location D:\gongzuo
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir
# 仅定义子电路时，明确选择要画的单元
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut
# 串行处理整个目录，完成一张即进入下一张
.\.venv\Scripts\python.exe .\tools\run_dataset.py .\test_data2 --out-dir .\output\test-data2-new
```

第一条命令的结果在 `output/3/`：

| 文件 | 用途 |
| --- | --- |
| `3-formal.svg` | 首选正式图：就近标号、统一字体、去白圈和调试框、安全合并局部 VDD |
| `3-placement.json` | 位置、朝向、真实线路、指标、候选及验收记录 |
| `3-layered.svg`、`3-clean.svg` | 原生彩色分析图和黑白图，保留完整信号线路 |
| `flow-result.json` | 成品路径、正式图状态和实际耗时 |
| `formal/<指纹>/` | 正式展示方案，可独立复验及重绘 |

指定子电路时目录和文件名带单元名，例如 `output/5-dut/`。同名成功运行会更新日常结果；对照时用独立 `--out-dir`。失败不会覆盖已有成功产物。正式展示插件失败时应检查其状态，不能把旧文件当作本轮新图。

## 当前默认流程

```text
候选2：正常120秒 → 必要时原候选修复30秒 → 局部救援30秒 → 扩距重试30秒
候选3：同样独立运行
→ 两者都失败，才尝试候选1；仍失败才总线后备（最多60秒）
→ 合法候选共享5秒安全缩线，按折角少 → 交叉少 → 信号线短选图
→ 复验、保存原生结果、正式出图
```

时间均为上限，提前完成立即继续；整图外层保护默认 **720 秒**。不是每张固定运行 720 秒，也不是两个候选共享 120 秒。[候选预算](docs/candidate-pipeline.md)解释恢复对象、跳过条件和显式回放；[选图规则](docs/candidate-selection.md)说明指标优先级。网页适配有自己的 300 秒调度，见适配分支的 `integrations/auto_canvas_huyangc/WEB.md`，不套用这里的阶段预算。

默认新布局开启有源骨架与无源模块、主列及支路组织、端口冲突预检和正式出图。`flexible` 仍是默认无源移动策略；有可用模块计划时，候选2/3先优化有源骨架，再成组插入附件。实验 `structure`、`region`、`symmetry`、`readability`、`attachment`、`passive-embedding` 均默认关闭；默认支路组织与旧 `region` 实验是不同入口。

安全缩线默认 `safe`、固定5秒、`strict`，不增加交叉；可显式使用 `--simplify-seconds auto` 或 `--route-tradeoff balanced`。额外网标视图和独立共享 VDD 视图默认关闭；**正式图内部的安全 VDD 合并默认开启**，用 `--formal-power-rails off` 单独关闭。完整开关表见[运行指南](docs/flow-guide.md#默认值与可选策略)。

## 安装与已有结果

已有 Python 3.12 时：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
```

没有可用 Python 时可运行 `setup_python.ps1`，它准备本地 Python 和虚拟环境。主算法依赖见 `requirements.txt`；符号资源已随仓库保留，不需要启动网页服务或安装 Node.js。

```powershell
# 固定位置重新布线
.\.venv\Scripts\python.exe .\run_flow.py --reroute-placement .\output\3\3-placement.json
# 保留布局，只缩短已保存的合法线路
.\.venv\Scripts\python.exe .\run_flow.py --simplify-placement .\output\3\3-placement.json
# 只复验、重绘，不运行布局或寻路
.\.venv\Scripts\python.exe .\render-clean.py .\output\3\3-placement.json .\output\3\3-redraw.svg
```

固定位置操作不会自动套用新布局策略；要应用新的支路或无源摆法，应从原网表重新运行。支持扁平网表和显式 `--cell`，不递归展开任意 X 子电路；器件与解析边界见[网表范围](docs/flow-guide.md#支持的网表)。几何验收不代替电路功能仿真，也不等同于美观验收。

## 代码与资料

- `netlist2schematic/`：主算法、恢复、后处理及插件；根目录保留兼容入口。[文件用途](docs/project-layout.md)
- `tests/fixtures/`：必要的回归输入，不能当作历史出图清理。
- `output/`：本地成品和对照页，不随 Git 提交；文档中的历史路径可能在清理后不存在。
- `agent-rsi-demo/`、`shinka-circuit-rsi/`、`ShinkaEvolve/`：独立研究项目，不参与主 flow。它们的说明与冻结引擎单独维护。
- [正式出图](docs/formal-figures.md)、[共享 VDD](docs/power-rails.md)、[网标](docs/net-labels.md)、[单文件盲测](docs/blind-evaluation.md)：展示与交付工具。

算法测试需主动执行，不会随出图自动运行：`.\.venv\Scripts\python.exe -B -m unittest discover -s .\tests -q`。最近已记录的验证见[验证状态](docs/validation-status.md)。

符号等资源来自 [Analog Canvas](https://github.com/cascode-ai/analog-canvas)，许可证和来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
