# 最终安全缩线

当前实现为 `orthogonal-shortcuts-v10`，默认 `--simplify-routes safe --simplify-seconds 5 --route-tradeoff strict`。它在已经完整合法的线路上做后处理，不调用SA或A*，不改变器件位置、固定出线或电源连接。默认候选流程在选图前共享5秒／512试案；选中后不再额外给胜者重复缩线。

## 四类操作与调度

| 阶段 | 新操作 | 累计时间份额 | 累计试案上限 |
| --- | --- | ---: | ---: |
| 1 | 直线及两种L替换连续折线路径 | 25% | 128 |
| 2 | 叶支线改接已有同网树，允许合法支点移动 | 50% | 256 |
| 3 | 三段正交路径替换，补直线/L覆盖不到的形状 | 75% | 384 |
| 4 | 整段轨道向内收，缩短外围大圈 | 100% | 512 |

时间比例按初始复验后的剩余时间计算，始终受总截止限制。前阶段收敛或到配额就让后阶段开始，未使用的时间和次数向后留用。新操作先试，再和已启用操作轮转；每次接受后基于最新合法路线重新生成候选。

自由链优先级为绕行余量（路径长度减两端曼哈顿距离）、路径长度，再以折角、坐标及网名稳定排序。轨道候选按链轮转，不让一个大网独占生成时间；同一几何状态去重，接受后可重新检查受新占用影响的试案。所有合格链都可能被检查，但预算不足时不保证每条都实际试到。

当前还包含三端T形候选：三个不同真实出口中，两端已对齐、第三端可投影到干线内部时，尝试更直接的T接。它与普通直线/L候选交替使用第一阶段的原额度，不新增第五份预算；固定出线保留，候选仍经过同一完整验收。

## 怎样接受一条新线

先合并同网重叠与人工共线切段，保护真实引脚和固定段，拒绝断线、误接、穿体、新增环路等。线段间距与真实交点净区先预筛；通过预筛后仍执行独立完整验收，不降低几何要求。

默认strict要求整图**线长、折角、交叉均不增加，且线长或折角至少一项下降**。失败或截止时丢弃未完成试案，只保留最后已验证结果。阶段配额耗尽不写作收敛；没有进一步候选也不意味着数学全局最优。

显式 `balanced`允许相对本次缩线开始的交叉累计净增最多2处，新增或移动交点间隔至少 `2 × cross_clearance`。增加交叉的替换还要求折角不增加、替换路径缩短至少20%、省线至少 `max(2S, 150×净增交叉数)`，且线长＋150×交叉＋15×折角下降。S是当前物理节距。该策略不会改A*的代价或默认几何，详见[可读性实验](readability-improvement.md)。

## 命令与记录

```powershell
# 只缩线，不重新布局/SA/A*；原JSON不覆盖
.\.venv\Scripts\python.exe .\run_flow.py --simplify-placement .\output\7\7-placement.json --out-dir .\output\7-shortened
# 显式允许有条件的稀疏交叉
.\.venv\Scripts\python.exe .\run_flow.py --simplify-placement .\output\7\7-placement.json --route-tradeoff balanced --out-dir .\output\7-balanced
# 从网表出图时关闭缩线
.\.venv\Scripts\python.exe .\run_flow.py .\examples\7.cir --simplify-routes off --out-dir .\output\7-no-shortcuts
```

`route_simplification`记录版本、阶段配额、操作次数、实际改网、前后指标、耗时和停止原因。`quota_yield`、`global_timeout`、`candidate_budget`与真正的`converged`分开；原`routing_stage`保持，不能把局部救援成品改记为正常布线。

`--simplify-seconds auto`是可选复杂度估计，选择5～30秒，但仍最多512试案，见[自动预算](auto-simplify-budget.md)。极短预算、初始复验耗时或候选次数先耗尽时，不保证四类算子都产生实际改动。

保存重绘不重新简化；主动处理旧版JSON会按版本和指纹检查是否可复用。输入已是缩线成品时，追加处理应标为“继续优化”，不能冒充原始A*前后对照。

## 实现与历史

主入口是 `postprocess/simplify_routes.py`；同目录的 `route_reattachment.py`、`route_patterns.py`、`route_tracks.py`、`route_junctions.py`产生候选，`route_tradeoff.py`判断取舍，`simplify_budget.py`负责预算。这些文件均位于 `netlist2schematic/`。

详细旧版试验和图23案例分别见 [v6调度](history/route-scheduling-v6.md)、[v7公平轮转](history/track-fairness-v7.md)、[v8交叉净区预筛](history/candidate-clearance-v8.md)。它们解释演进，不代表要把三个插件再执行三遍。
