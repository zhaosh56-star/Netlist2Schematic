# 主 flow 运行指南

按2026-11-27源码核对。本文说明 Python 主算法；网页的原生项目转换和300秒调度见适配分支文档。算法原理见[流程详解](flow-walkthrough.md)，文件位置见[目录说明](project-layout.md)。

## 日常运行

```powershell
Set-Location D:\gongzuo
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut
.\.venv\Scripts\python.exe .\run_flow.py --help
```

入口按脚本所在项目定位资源，输入和显式输出路径按调用环境解析，因此也可从其他工作目录用绝对路径启动。默认结果在项目的 `output/<输入名[-cell]>/`，首选 `*-formal.svg`。对照必须使用独立 `--out-dir`，不要覆盖用于比较的原图。

没有环境时运行 `setup_python.ps1`，或自行用Python 3.12建立 `.venv` 并安装 `requirements.txt`。算法不要求启动网页项目；默认符号定义来自仓库内 `analog-canvas-main/packages/components/definitions`，几何缓存为根目录 `symbol-geometry.json`。`--analog-canvas-root` 可指定另一份只读资源根，缓存与资源须兼容。

## 默认值与可选策略

以下是**普通新网表**的有效默认值；保存布局操作和显式实验有不同适用边界。

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `--mode` / `--layout-style` | `layered` / `structured` | 分层、结构化布局 |
| `--iterations` / `--seed` | `20000` / `20261029` | 提案预算与随机种子，不保证每个候选执行20000步 |
| `--passive-layout` | `flexible` | 旧混合布局路径允许 R/C/L/D 跨行；模块路径先单独排有源 |
| `--passive-skeleton` | `auto` | 候选2/3先保有源骨架，再插入有依据的无源和源模块 |
| `--column-layout` / `--branch-layout` | `auto` / `auto` | 连续主列、短旁支和跨行区域顺序，无器件数门槛 |
| `--port-preflight` | `repair` | 最终 A* 前有限修复静态端口冲突 |
| `--simplify-routes` / `--simplify-seconds` | `safe` / `5` | 最终合法线路安全缩线；`auto`预算需显式指定 |
| `--route-tradeoff` | `strict` | 缩线不增加交叉；`balanced`允许有条件的少量稀疏交叉 |
| `--formal-output` / `--formal-power-rails` | `auto` / `auto` | 正式字体、就近标号和安全局部 VDD 合并 |
| `--net-labels` / `--power-rails` | `off` / `off` | 可选的额外网标图／共享 VDD 图，不替换原线路记录 |
| `--structure-layout` / `--region-layout` / `--symmetry-layout` | 全部 `off` | 独立布局实验 |
| `--readability-layout` / `--attachment-layout` / `--passive-embedding` | 全部 `off` | 旧附件及成品后试摆实验 |

`column/branch/passive-skeleton` 的默认值由入口根据模式解析，并非所有调用都强制开启。baseline、legacy、固定无源行或其他显式实验不能隐式叠加不兼容模块。`--candidate-budget sequential|reserved` 服务于旧非骨架候选路径，不改变默认独立候选预算。

### 无源器件灵活跨行布局

`flexible` 与默认模块插入不是同一个算法。在未采用骨架模块计划的结构候选中，R/C/L/D可换行、互换或进入有源同行的合法空位；不能插进不相关的小结构。串联、并联、反馈通过软评分维持关系。`fixed`用于旧固定行对照。

默认候选2/3若有可用模块计划，则先对有源骨架做SA，再将串并联无源及有明确归属的V/I作为模块插入；它们可以使用局部细坐标。纯无源或没有适用骨架计划时继续对应的混合布局路径。详见[无源模块](passive-modules.md)。

## 候选、时间与失败恢复

候选2和3分别拥有正常120秒、原候选修复30秒、局部救援30秒和1.5倍扩距30秒。每份成功就跳过自己的剩余恢复，但仍运行另一份；两者都失败才给候选1相同机会，再失败才bus最多60秒。整图外层720秒保护会限制所有阶段，阶段额度不随外层上限按比例重算。

合法候选共享5秒／512试案安全缩线，然后按**折角、交叉、信号线长**逐项比较；不是按Q加权分选图。完整规则以[候选预算](candidate-pipeline.md)、[选图规则](candidate-selection.md)为准。

```powershell
# 加长每候选正常阶段，也提高外层保护
.\.venv\Scripts\python.exe .\run_flow.py .\examples\7.cir --quality-seconds 180 --time-seconds 900
# 显式回放一个候选，默认关闭失败恢复
.\.venv\Scripts\python.exe .\run_flow.py .\examples\7.cir --candidate-id 2 --out-dir .\output\7-candidate2
# 只允许正常流程，明确同时关闭恢复与原候选修复
.\.venv\Scripts\python.exe .\run_flow.py .\examples\7.cir --fallback off --local-rescue off --repair-seconds 0 --out-dir .\output\7-normal-only
```

恢复依赖本次运行的真实检查点；不会读历史成功图给陌生网表兜底。缺少安全现场会跳过相应恢复。解析错误、资源错误、损坏JSON和输入变化直接失败。布线失败不代表数学无解，也不会把未验收路线标为成功。

## 最终路线的安全简化

当前版本 `orthogonal-shortcuts-v10`：直线/L形、支线重新接入、三段替换、整段轨道内收分阶段共享预算，第一类中也轮转尝试三端T形整理。每次接受都基于最新线路，候选先预筛再完整验收。默认strict要求线长、折角、交叉均不增加，且线长或折角至少一项下降；超时保留已经验证的结果。

```powershell
.\.venv\Scripts\python.exe .\run_flow.py .\examples\7.cir --simplify-routes off --out-dir .\output\7-unsimplified
.\.venv\Scripts\python.exe .\run_flow.py .\examples\7.cir --simplify-seconds auto --out-dir .\output\7-auto-budget
```

详见[缩线原理、记录与边界](route-simplification.md)。关闭后处理不会把旧JSON先前已经缩短的路线恢复成原始A*线路。

## 重布、缩线与重绘

| 操作 | 是否重摆件 | 是否重新寻路 |
| --- | --- | --- |
| 从网表重新运行 | 是，应用当前策略 | 是 |
| `--reroute-placement <JSON>` | 保留保存坐标和朝向 | 是 |
| `--simplify-placement <JSON>` | 否 | 不运行SA/A*，仅局部安全缩线 |
| `render-clean.py <JSON> <SVG>` | 否 | 否，只复验和重绘保存方案 |

```powershell
.\.venv\Scripts\python.exe .\run_flow.py --reroute-placement .\output\3\3-placement.json --out-dir .\output\3-reroute-check
.\.venv\Scripts\python.exe .\run_flow.py --simplify-placement .\output\3\3-placement.json --out-dir .\output\3-simplify-check
.\.venv\Scripts\python.exe .\render-clean.py .\output\3\3-placement.json .\output\3\3-redraw.svg
```

输入模式互斥。已有JSON必须能恢复其网表、资源和保存解释条件；检测到不兼容变化时明确拒绝。旧JSON不自动套用新布局或新的VEE解释。保存位置操作默认不运行端口修复，也不自动扩距或转bus；重布可显式开启保持位置的局部救援。

## 支持的网表

| 输入 | 当前范围 |
| --- | --- |
| `M` | D/G/S/B及明确MOS型号；可见符号保留D/G/S |
| `Q` | C/B/E、可选基底及明确NPN/PNP型号；可见符号保留C/B/E |
| `X` | 已支持的nfet/pfet四端MOS，或型号／`.model`明确为NPN/PNP的三／四端BJT |
| `R C L D` | 两端元件，保留值／型号及连接 |
| `V I` | 独立源，保留正负端和源参数 |
| 普通 `E` | 线性VCVS代理，输出负端必须是受支持地参考，画OUT/IN+/IN− |
| 命名 `E … opamp` | `Ename [VDD VSS] IN+ IN- OUT opamp [参数]`；三可见信号端，可选供电连接保存在数据中 |

不依据X实例名字猜型号，不递归展开任意X子电路，不支持开关等未声明器件。普通E的地参考限制与命名opamp契约不同，不能靠随意交换节点来凑解析。

支持续行、`.model`及本地include，错误尽量携带源文件和行号。扁平输入读取顶层器件；`--cell dut`读取该`.subckt`的内容，不展开其内部未知X实例。未选择cell不会自动选择文件中第一个子电路。

当前兼容一个有限导出格式：全文没有`.subckt`时，结尾唯一、未命名的孤立`.ends`可在内存中作为结束符处理并给出警告。命名错配、未闭合和其他层次错误继续失败，原文件不改写。旧文档中“examples/6必然报孤立.ends错误”的结论已过时。

## BJT与MOS共用基础结构

公共拓扑接口将MOS的D/G/S与BJT的C/B/E对应为导通上端、控制端和导通下端，器件家族、极性及真实端口仍分别保存。共同支持差分对、电流镜、交叉耦合和串接关系；仅共电源或共偏置不是配对证据。MOS专用锁存、折叠复合规则继续独立。

父结构不会取消其内部小结构保护。当前主列布局还保护可信叶块的跨度，避免“没有插入第三只管，但把一对管拉得很远”。对应显示行、跨R/L/C关系及源模块的边界见[设计文档目录](README.md#当前默认算法的设计点)。

## VEE负电源标记

VDD/VCC使用向上盖帽；VEE使用向下单横杠盖帽；0/GND使用地符号。VEE不改名为0，也不与GND或VSS短接。实际网络身份和符号显示角色分开保存。

新JSON保存 `supply_marker_policy: negative-supply-cap-v1`；缺少该字段的旧JSON按旧策略恢复。显示、障碍和连接验收使用同一真实标记接点，不能用供电标记列表序号猜连接。历史BJT/VEE实测在[归档](history/bjt-and-vee-validation.md)，其中637当时失败不代表最新版本仍失败。

## 连接、几何与结果解读

默认线宽2.5、网格2、异网平行线中心距至少10，器件halo及交叉净区通常为12。合法十字交叉允许通过，不画跨线桥或异网圆点；误接、共轨、穿体、贴线和拥挤交叉继续禁止。同网共享干线和真实T接允许，统计忽略纯共线切段。

**信号线现在不单独避让供电标记的矩形保护区。** 真实电源引线、异网端子、器件本体和标记与器件重叠仍受检查；不能将这条放行解释为所有电源几何都忽略。

| 字段 | 读法 |
| --- | --- |
| `placement`、`layout_grid` | 逻辑格、物理轴、朝向和局部偏移 |
| `levels`与显示行计划 | 原电气层级和用于绘图的行不是同一概念 |
| `candidate_pipeline`、`recovery.stages` | 每候选真实阶段、预算、跳过和失败原因 |
| `quality.routing_selection` | 最终排序与胜者；`quality.score`仍是诊断Q分 |
| `routes`、`metrics`、`validation` | 实际保存信号线、最终统计及完整验收 |
| `optimization_metrics`、`energy`、`annealing` | SA估计、提案和接受率，不是最终A*指标 |
| `route_simplification` | 缩线前后、版本、候选数、改动及停止原因 |
| `power_connections` | 独立电源引线、真实标记接点及连接 |

`metrics.wirelength`是信号线长度，不等于包含供电的所有可见线长。VDD合并、网标及正式展示另存指标，不冒充A*收益。`validation.valid`证明当前支持可见引脚与几何规则通过，不证明电路仿真正确或图形已经最美观。

## 验证与维护

日常批量命令见[批量运行](batch-flow.md)。完整回归可主动运行 `.\.venv\Scripts\python.exe -B -m unittest discover -s .\tests -q`；不要为单纯阅读或修改文档启动全量出图。

最新已经记录的回归范围见[验证状态](validation-status.md)，旧版本数字留在[历史目录](history/README.md)。修改符号资源、解析条件或布局算法后，应重新验证相应范围，不能只靠旧JSON的 `valid=true` 宣称当前版本成功。
