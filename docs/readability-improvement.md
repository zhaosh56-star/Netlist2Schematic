# 可读性布局实验与平衡缩线

核对日期：2026-11-27。这两项独立控制：`--readability-layout off`与`--route-tradeoff strict`是默认值。前者改变新候选布局，后者只改变安全缩线的接受条件；不必同时开启。

## 平衡缩线

`--route-tradeoff balanced`允许用少量分散交叉换取明显短线，复用原直线/L、支线重接、三段替换和轨道内收。相对本次缩线起点净增最多2处交叉，交点必须稀疏，并达到路径缩短比例、省线绝对量和原路由代价下降条件。位置与A*算法不变，完整几何验收继续执行。

具体门槛、预算与记录见[安全缩线](route-simplification.md)，不要将“允许合法交叉”理解成允许端点误接或拥挤交叉。

```powershell
.\.venv\Scripts\python.exe .\run_flow.py --simplify-placement .\output\7\7-placement.json --route-tradeoff balanced --out-dir .\output\7-balanced
```

## 可读性布局

`--readability-layout auto`在structured＋flexible候选2/3中启用以下实验。`--readability-phase 2|3|4|5`选择执行到哪一阶段，默认5；阶段1对应独立的balanced缩线，不是该参数值。

| 阶段 | 尝试 |
| --- | --- |
| 2 | 精确并联、串链、跨支路、两臂汇合、单端附件；唯一R/L显示行 |
| 3 | 有明确服务节点的尾源、偏置源或控制端源就近 |
| 4 | 附件归位后回收失效预留行带及空隙 |
| 5 | 初排和候选3反馈中估计出口通道与长分支绕线风险 |

候选1为参照；新附件初排与软项不把整个无源网锁成刚体。所有收尾共用1秒／128次试摆，位置、朝向、占位和物理轴一起提交或撤销；SA和试摆不调用A*。候选3不能用不兼容的旧反馈覆盖已接受的计划。

```powershell
.\.venv\Scripts\python.exe .\run_flow.py .\examples\7.cir --readability-layout auto --out-dir .\output\7-readability
```

该实验不依赖识别出完整大区域，但与structure、region、symmetry、attachment的开启模式互斥。保存位置操作不重新摆件。默认的骨架模块与此实验是不同路径，不能把它的off报告当作所有最新默认算法都关闭。

## 指标、选图与历史

附件规整、源端距离、空隙及出口风险单独记录，Q保留原含义。最终合法候选使用[折角→交叉→线长](candidate-selection.md)，旧readability建议仅作诊断。历史115%门槛及逐图视觉要求属于当时发布对照，不覆盖当前最终排序。

代码在 `netlist2schematic/plugins/readability/`，共享附件及显示行位于 `plugins/layout_shared/`，balanced位于 `postprocess/route_tradeoff.py`。`tools/compare_readability.py`会执行多次完整flow，需主动运行。

早期72次运行、成功与总线分类、各阶段取舍及未通过项见[可读性实验日志](history/readability-improvement-log.md)。它们仍有参考价值，但不代表当前代码又完成了一轮对照；两个开关继续采用off／strict。
