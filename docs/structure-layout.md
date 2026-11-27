# 结构布局增强实验

核对日期：2026-11-27。`--structure-layout auto|off`默认 **off**；关闭的是这套额外的校准关系和布局实验，不是关闭默认子结构识别、叶块保护或主列组织。当前默认能力见[支路组织](branch-organization.md)。

## 校准关系做什么

`layout_evidence`单独区分真实供电、信号驱动与内部公共源节点；多管共源只有在控制、支路和尾部证据唯一时确认输入对。缺少功能证据的内部共漏连接保留中性标签，共享尾源不能升格为专属尾源。校准不改网表，也不改原 `structs` 的含义。

`layout_relations`与`layout_intent`把对应堆栈、父子块及端口附件传给初排、SA和收尾。每个器件保持唯一最低层归属，父块引用子块，不通过共偏置将整个图合成大刚体；遇到分叉、汇合或归属歧义停止扩展。

## 三个阶段

| `--structure-phase` | 增强 |
| --- | --- |
| 1 | 局部块连续、支路顺序，暂不改有源层级 |
| 2 | 严格唯一R/L桥显示行、兼容同层关系及专属尾部 |
| 3（开启实验时默认） | 真实D/S、C/E对齐及无源附件整理 |

候选1保留参照，候选2/3使用关系。显示行在SA前完成，SA期间仍保持允许行；候选3反馈必须符合该计划。阶段3的局部试摆共享1秒／128案、估计线长115%护栏，不在试摆中调用A*。

水平微偏移可保存为 `placement[id].dx`，缺省0，按网格取整且不能跨相邻列中线。位置、引脚、障碍和标记统一使用偏移；扩距不缩放局部dx，最终重测。这里的dx与默认模块的自由 `local_offset`不是同一个字段。

## 选择与保存

最终合法候选统一按[折角→交叉→线长](candidate-selection.md)比较；实验历史的“结构优先、115%实际线长门槛”仅作偏好诊断，不再筛掉当前最终候选。静态试摆的估计护栏和所有几何硬检查仍保留。

`layout_evidence.consumption`区分只量测与实际用于布局；`branch_row_plan`记录显示行及原层级；`branch_alignment`、`attachment_placement.current`和`structure_layout.metrics`按最终坐标测量。仅有 `applied` 不保证某个器件实际移动或最终候选胜出。旧JSON重布、缩线、重绘不重新摆件。

```powershell
.\.venv\Scripts\python.exe .\run_flow.py .\examples\7.cir --structure-layout auto --out-dir .\output\7-structure
.\.venv\Scripts\python.exe .\run_flow.py .\examples\7.cir --structure-layout auto --structure-phase 1 --out-dir .\output\7-structure-phase1
```

可选对称若兼容开启，先对称再按结构对齐；不兼容region/readability/attachment等模式不能叠加。默认骨架策略不会隐式覆盖显式实验，具体约束由入口检查。

实现位于 `plugins/structure/`和`plugins/layout_shared/`，由 `layout/structured_placement.py`、`flow/aesthetic_flow.py`接入。早期实现细节见[设计归档](history/structure-layout-log.md)，逐阶段及全量结果见[历史实测](history/structure-layout-results.md)。这些历史发布门槛不是当前最终选图规则；本实验继续关闭。
