# 无源模块：先保有源框架，再插入附件

核对日期：2026-11-27。普通新网表的 `layered + structured + flexible` 默认启用 `--passive-skeleton auto`，主要用于候选2/3。有可用计划时先优化有源，再插入模块；没有适用计划则走原对应布局路径。默认调度与恢复见[候选流程](candidate-pipeline.md)，最终比较见[选图规则](candidate-selection.md)。

旧 `--attachment-layout passive|all` 和 `--passive-embedding auto` 是另外两套默认关闭的实验，**不需要开启它们才能使用本模块**。逐轮数字及旧默认值已移至[历史记录](history/passive-modules-log.md)。

## 先保有源，再插模块

```text
解析真实连接 → 识别模块和宿主端口
→ 电气层级及显示行计划 → 主列/区域初排 → 有源SA
→ 主列、叶块跨度与真实引脚对齐 → 必要的局部空间预留
→ 原模块插入/端部规整，失败时有界二维续插
→ 完整静态合法后，小范围二维择优
→ 最终A*、必要恢复、共享缩线、选图和输出
```

框架的辅助边只供布局估价，不导出为电气短路。R/L/C或V/I位于中间时，可以帮助确定显示关系；原电气 `levels`、节点与端口不因此改写。确认的有源主列、小结构、固定器件和支路身份继续受保护，不能因模块失败而丢掉骨架、重新随机混排。

## 模块由拓扑产生

- 唯一串联链按连接顺序排列；并联路径保留各自内部顺序和共同两端；嵌套串并联递归组合。
- 中间模块由两侧真实宿主限定接入通道；端部模块靠近供电、地或信号端，宿主可以是同网的多个真实端子。
- 有依据的独立V/I可复用模块方法，保留正负端和箭头。归属不明确、共享、固定及成对负载有各自边界，不能随意拆散。
- 公共供电、型号或器件名字不能单独建立模块归属；不读取GT坐标，也不写图号例外。

细节见[跨R/L/C支路](inline-passive-branches.md)与[源模块](source-module-layout.md)。

## 插入、间距与二维找位

模块用真实符号外形、引脚、固定出线和布线规则计算模板。优先紧凑间距，受阻时在有限预算内尝试标准或带额外通道余量的模板；符号尺寸和几何硬要求不缩小。公共轴可在预留阶段有限扩大，模块通过保存的 `local_offset` 使用细坐标，不必占MOS的整格中心。

### 端部模块二维找位

原插入静态合法后，`terminal_slots.py`在剩余局部额度内围绕真实端口、障碍边界寻找上下左右位置。模块整体平移，保留内部形状、方向及有源坐标和公共轴。原插入与端部整理共用原局部1秒／128次的剩余额度；无收益或时间耗尽保留已检查状态。

位置排序考虑多端节点接入余量、明显短通道风险、尾源居中及原线长/折角/交叉估价。不是“只要放得下就接受”，也不以静态合法冒充整图可布。

### 失败插入阶段的二维搜索

`packing_search.py`只在未完成插入时恢复本次真实检查点：已放模块、失败模块、待放队列、结构保护及实际坐标。端部可二维平移，中间模块继续服从两端通道；必要时回退失败模块和最多两个直接阻挡附件，组外保持不变。

有限搜索尝试紧凑、标准和加宽模板，共享截止时间及检查次数；最多四次局部事务。每个失败事务完整撤销。明确固定的归属不能改变；仅由 `unique_conduction_port`推得的宿主，失败时可在同节点最多四个真实端口中比较接入侧。

失败搜索按模块复杂度使用3／6／10秒、512／1536／3072次上限，仍受父阶段截止限制。JSON的 `passive_skeleton.insertion_search`区分时间、次数及位置耗尽。完整器件尚未插入时，不允许向下游提交缺件布局。

### 正常阶段的小范围二维择优

`normal_choice.py`在完整插入及端部规整之后、A*之前执行。全部模块共用1秒／64次完整试案；每模块检查有限位置并最多比较四份新的合法方案。保持有源行列、实际坐标、朝向、物理轴和模块内部形状，估计改善且完整静态检查通过才替换。

所以二维能力**不只是失败才触发**：正常择优负责比较少量更好的位置，失败搜索负责把尚未放齐的模块补齐。两者各有调用条件与账本；都不在试摆里执行A*。完整最终连接仍需随后验收，估价改善不保证每张图实际线长都下降。

## 可选的独立附件预算接口

算法API接受 `layout_budget='legacy'|'adaptive'`。主CLI默认仍为legacy，未新增同名CLI参数；网页适配明确选择adaptive。调用 `select_layout()`、`create_candidates()`或内部 `_normal_result()`及后续 `primary_recovery.repair()`时应使用同一策略。

adaptive仅在必要插入因时间或次数耗尽时补试，不把位置枚举已经耗尽的同一轮无限重跑。设模块数M、模块内不同器件数G，`K=M+ceil(max(0,G-M)/3)`：

| 复杂度 | 正常补试 | 恢复插入 |
| --- | --- | --- |
| K≤2 | 最多3秒／512次 | 最多10秒／2048次 |
| K=3～5 | 最多6秒／1536次 | 最多15秒／4096次 |
| K≥6 | 最多10秒／3072次 | 最多20秒／8192次 |

全部模块共用额度；恢复插入最多使用该候选修复份额的60%，跨倍率不重置，父截止时间优先。该API不改变主CLI独立候选预算，也不意味着自动采用网页300秒调度。报告在 `normal_packing_budget`、`recovery_packing_budget`中。

## 失败与恢复

最终布线失败后，恢复可有限尝试模块通道调整；`module_access.py`先检查最近树接点的一小组直线/L接法，再回到原A*救援。原始 `Pin.escape`、halo和交叉净区不修改，整个网与整图都必须重新验收。正常A*成功时不运行这项失败接线救援。

候选2/3各自恢复；均无合法结果才候选1，再失败才bus。不会复用历史合法图片或把一份候选的布局和另一份的路线混合。固定布局重布、缩线和独立重绘只恢复保存坐标，不重新插入。

## 代码入口

下列路径相对 `netlist2schematic/`：

| 文件 | 职责 |
| --- | --- |
| `plugins/passive_skeleton/plan.py`、`modules.py`、`source_modules.py` | 拓扑计划、串并联模块和源角色 |
| `plugins/passive_skeleton/candidates.py` | 有源SA、行计划及后续插入的调度 |
| `plugins/passive_skeleton/placement.py`、`module_geometry.py`、`module_connections.py` | 预留、模板几何、实际端口及内部连线 |
| `plugins/passive_skeleton/terminal_slots.py`、`normal_choice.py` | 原端部规整与正常二维择优 |
| `plugins/passive_skeleton/packing_search.py`、`packing_recovery.py` | 失败续插和恢复现场 |
| `common/layout_budget.py`、`plugins/passive_skeleton/packing_budget.py` | 可选adaptive额度与共享账本 |
| `plugins/passive_skeleton/module_access.py` | 失败救援中的安全短接 |
| `plugins/columns/local_groups.py` | 有源叶块跨度保护，避免后续列规划拉散结构 |

```powershell
# 当前默认，无需额外实验开关
.\.venv\Scripts\python.exe .\run_flow.py .\examples\7.cir --out-dir .\output\7-modules
# 明确关闭模块路径作对照
.\.venv\Scripts\python.exe .\run_flow.py .\examples\7.cir --passive-skeleton off --out-dir .\output\7-mixed
```

## 最近验证与限制

2026-11-26的 `2405d49` 完整回归2656项通过；两数据集47图全部成功、无bus，45图与上批逐项保持位置/轴/线路，BJT637与OTA＋CMFB变化。信号总线长103568→99382，交叉115→112，折角578→571。BJT637选中候选3普通布线，3938／5／31；早期“附件放不齐”的结论已由后续修复覆盖。

这是已完成批次的证据，不保证陌生网表必成功或每个指标都改善。墙钟截止会影响候选是否完成；局部静态改善也可能产生最终线路取舍。完整范围、协议差异及历史链接见[验证状态](validation-status.md)与[归档](history/passive-modules-log.md)。
