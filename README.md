# Netlist2Schematic

把 SPICE 网表转换成布局 JSON、彩色 SVG 和黑白 SVG。默认按电流层级和电路子结构布局，使用模拟退火优化位置，再进行正交 A* 布线、连接与几何验收，最后安全缩短绕线、减少多余折角。输入网表保持原样。

想理解每一步怎么执行、用哪些代码，以及SA估价、A*、失败恢复和最终简化的区别，请先读[从网表到原理图：当前flow逐步详解](docs/flow-walkthrough.md)。

结构布局改进通过 `--structure-layout auto` 试用，开发期间默认关闭。候选1保留参照，候选2/3按有拓扑证据的局部块和支路改善排列，继续使用原A*、完整验收和安全缩线。分阶段规则与对照命令见[结构布局说明](docs/structure-layout.md)。

独立试验开关包括 `--presentation clean`（只清理黑白成品展示）、`--candidate-budget reserved`（给后续候选预留时间）、`--port-preflight repair`（修复静态端口/标记冲突）。三者默认分别为legacy、sequential、off，不自动改变日常结果。已有JSON可用 `render-clean.py 原JSON 新SVG --presentation clean` 只重绘。

结构布局已完成完整对照及并联RLC专项复验；部分样例尚未满足15%线长或交叉门槛，结构策略继续默认关闭。结果与限制见[分阶段实现与实测结果](docs/structure-layout-results.md)。

想看目前识别的子结构是什么样，见[子结构图鉴说明](docs/structure-gallery.md)。运行 `.\.venv\Scripts\python.exe .\tools\generate_structure_gallery.py` 可生成20张带角色、网表及实际识别记录的示意图，入口为 `output/structure-gallery/index.html`。

## 日常运行

在项目目录打开 PowerShell。当前工作区已有运行环境时，直接执行：

```powershell
Set-Location D:\gongzuo
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir
```

结果统一放在 `output/3/`：

- `3-placement.json`：位置、朝向、线路、指标及验收结果。
- `3-layered.svg`：彩色图。
- `3-clean.svg`：黑白图，可直接用浏览器打开。

图5包含子电路，需明确选择 `dut`：

```powershell
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut
```

结果位于 `output/5-dut/`，文件名分别为 `5-dut-placement.json`、`5-dut-layered.svg` 和 `5-dut-clean.svg`。

所有日常生成物都在项目根目录的 `output/` 下，按输入名称分组；默认目录不附加布局模式。相同输入再次运行会更新同名结果。要比较不同参数，可用 `--out-dir .\output\3-compare` 指定独立目录。历史出图不作为代码或回归输入保留，需查看时重新生成。

## 首次配置环境

已有 Python 3.12 时：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
```

没有可用 Python 时，使用项目安装脚本：

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\setup_python.ps1
```

脚本安装本地 Python 3.12.10 并创建 `.venv/`，依赖固定为 NetworkX 3.7。符号资源已随仓库提供，无需启动 Analog Canvas 网页项目或安装 Node.js。

## 运行方式与预算

| 阶段 | 作用 | 默认独立预算 |
| --- | --- | ---: |
| 正常优化 | 结构化布局、SA、候选1～3的最终 A* 布线 | 300秒 |
| 局部救援 | 正常阶段全部失败后，保留布局、修复接口并局部重布 | 60秒 |
| 扩距重试 | 救援失败后，将原失败布局的间距扩大1.5倍重布一次 | 60秒 |
| 总线后备 | 扩距仍失败后，通过独立通道构造完整连接 | 60秒 |
| 安全简化 | 分阶段预留配额，进行路径/支路简化、三段式及轨道收紧；各项均不增加 | 5秒 |

SA 迭代期间不调用 A*。前一阶段成功就跳过后续恢复；总线后备不执行安全简化。各项上限合计约485秒，另有资源检查及文件输出耗时。每份成功结果都必须通过完整验收。

默认只生成和比较候选1、2、3，候选0的旧布局初始化、SA和A*均跳过。三种结构种子的编号、随机种子和每种约6666次提案预算保持原样，共享正常阶段300秒。候选3仍可用前面最佳结果的局部布线反馈版本。需要旧参考时显式加 `--candidate-id 0`；`--layout-style legacy` 和 `--mode baseline` 保留原有独立入口。指定候选1～3也不会先运行候选0的SA。

默认 `--passive-layout flexible`：结构候选1～3中的R/C/L/D可以跨行、与其他无源件交换，也能使用MOS/BJT同行块外的合法空位。串联、并联和反馈关系用软评分保持规整，不锁成刚体；MOS/BJT的导通层级、原有组动作和镜像规则不变。`--passive-layout fixed` 恢复旧的无源固定行行为。候选0、legacy、baseline及不含R/C/L/D的电路沿用原算法；读取保存JSON重布、简化或重绘时不重新摆件。详细规则及对比命令见[无源灵活布局](docs/flow-guide.md#无源器件灵活跨行布局)。

默认 `--symmetry-layout off`：日常命令不加该参数时，跳过额外的器件对称规整和S对称选优，候选按原Q选择。需要试用时显式添加 `--symmetry-layout auto`：候选1保留原摆法，候选2、3在SA和布线反馈之后、最终A*之前，尝试让明确对应的器件位置与朝向更对称。可有多个局部轴，也可组成较大的对称核心；不要求导线镜像，暂不增加上下串管同轴目标。开启后每候选最多1秒，计入原300秒，不新增A*次数；在本轮原Q最佳合法候选125%的实际线长范围内优先比较器件对称，同等对称程度再用原Q择优。规则、记录和显式auto/off实验见[器件对称布局说明](docs/symmetry-layout.md)。

`--passive-layout` 控制SA中的无源动作；可选的对称收尾是后续独立阶段。当前默认已跳过对称收尾；若还需要旧的无源固定行SA，使用 `--passive-layout fixed`。

安全简化默认 `--simplify-routes safe`：直线/L形、支路重接、三段替换和轨道内收按阶段逐步启用，共享5秒和512个候选上限。每阶段为后续操作预留配额，新操作先试，再与已启用操作轮转；每次接受后都从最新线路重新分析。只有完整验收通过，线长、折角、交叉均不增加，且线长或折角至少一项严格改善，才替换。超时保留当前已验收结果。最终同一份线路用于JSON和两种SVG，含线路的JSON重绘时只复验和绘制保存结果。完整顺序见[运行说明](docs/flow-guide.md#最终路线的安全简化)。

当前缩线版本为v8，包含[v6配额调度](docs/route-scheduling-v6.md)、[v7轨道公平轮转](docs/track-fairness-v7.md)和[v8交叉净区预筛](docs/candidate-clearance-v8.md)。保留原几何规则和独立完整验收，`--simplify-routes off` 可关闭后处理。显式添加 `--simplify-seconds auto` 可按初始合法线路复杂度选择5～30秒，默认仍固定5秒；详见[自动预算](docs/auto-simplify-budget.md)。

v4已通过313项测试及9套固定布局的v3/v4对照。最初更新图7主图：线长2812→2676、交叉3→2、折角24→20，后处理约0.93秒；原网表和器件位置不变。随后又按当前完整flow重跑了图4、6、5，最新来源、阶段及指标见[流程详解](docs/flow-walkthrough.md#examples)。先前的本地批量对比页仍记录v3历史结果。

```powershell
# 固定已有位置，跳过布局，仅重新布线；默认写到 output/3-rerouted/
.\.venv\Scripts\python.exe .\run_flow.py --reroute-placement .\output\3\3-placement.json

# 只简化保存的合法路线，跳过SA、A*和恢复；默认写到 output/3-simplified/
.\.venv\Scripts\python.exe .\run_flow.py --simplify-placement .\output\3\3-placement.json

# 独立重绘保存的路线，不再优化
.\.venv\Scripts\python.exe .\render-clean.py .\output\3\3-placement.json .\output\3\3-redraw.svg

# 运行回归测试；测试输入独立保存在 tests/fixtures/
.\.venv\Scripts\python.exe -B -m unittest discover -s .\tests -q
```

完整参数、器件范围、几何规则、恢复过程和复现命令见[运行说明](docs/flow-guide.md)。各文件用途和目录保留范围见[项目目录说明](docs/project-layout.md)。原来的[README-SA.md](README-SA.md)保留为说明入口。

支持扁平网表和显式指定的子电路，不递归展开任意 X 实例。连接验收检查当前支持器件的可见引脚，不执行电路功能仿真。`examples/6.cir` 原始内容有未配对的 `.ends`，会如实报错；如需运行，应另存副本并补齐子电路声明或移除此孤立标记。

## 独立Agent实验

`agent-rsi-demo/` 提供独立的电路生成Agent实验，包含起跑引擎、冻结参考、评测与源码改进循环。它不参与主flow的日常运行；使用方法见[Agent环境与启动说明](agent-rsi-demo/AGENT_SETUP.md)，已验证范围见[验证记录](agent-rsi-demo/VALIDATION.md)。模型配置和运行产物不进入Git，smoke仅用于接线验证，不代表真实模型自主优化。

## 上游资源

符号定义和四份测试网表来自 [Analog Canvas](https://github.com/cascode-ai/analog-canvas)。保留的许可证与来源说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。`output/`、`.cache/`、`.venv/`、`.python/` 和本地 `archive/` 均不进入 Git。
