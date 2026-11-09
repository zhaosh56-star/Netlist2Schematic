# 结构布局：按阶段改善功能块、支路与附件

新策略通过 `--structure-layout auto|off` 控制，开发和对照期间默认 `off`。关闭时保留原布局算法；`--symmetry-layout` 继续默认关闭。新策略不要求导线镜像，不修改网表，也不放宽引脚连接和几何安全规则。

## 为什么需要独立的布局关系

“这是电流镜”和“这两只负载适合放在对应支路上”是两件事。新关系层复用已确认的功能角色，另外记录有拓扑依据的BJT输入对、重复管和成对负载，不把共电源或共偏置直接升级为差分对。器件只能拥有一个最低层局部块；父结构可以协调子块，但共享一个成员不意味着多个结构必须合成一个巨大刚体。

候选1保留原摆法作为本轮参照，候选2、3启用新策略。候选数、SA和A*权重、恢复预算保持原值。新指标单独保存，原 `quality.score` 的含义不变。

## 分阶段交付

1. 局部块完整性：改善初排顺序、组内穿插及对应支路次序；这一阶段不改有源行号。
2. 主支路行计划：对唯一、无分叉的R/L串链识别上下关系，并在SA之前安排合法行号；电容、二极管和栅基极反馈不当作主通路。
3. 引脚与附件规整：按真实D/S、C/E接点对齐，专属尾源尽量居中，负载和反馈附件围绕所属结构排列。

各阶段有独立版本、测试和对照结果，只有实际接入并执行的步骤才记录为已应用。旧JSON、固定布局重布、单独简化和独立重绘只使用保存的位置，不重新摆件。

阶段二已接入：`branch_row_plan`保存显示行计划、原电气层级、严格上下关系、同层关系、R/L挂位和拒绝原因。`levels`仍表示原算法的电气层级，两者不是同一个字段。唯一R/L桥预留整数挂位，SA期间无源仍然可以灵活移动。专属尾源在不违背其它导通约束时靠近输入块，共享或归属冲突的尾源跳过。

可用 `--structure-phase 1` 单独重放第一阶段；阶段编号仅在 `--structure-layout auto` 时生效。候选1以及所有关闭策略保持原算法。

## 怎样选择结果

正常路由结束后，以本轮原综合评分最佳的合法候选为参考。结构选优只考虑实际线长不超过参考115%的结果；参考零交叉时新结果也必须零交叉，否则最多多1处交叉。存在结构改善时先比较组内穿插和角色次序，再比较归一化跨度，最后按原综合评分择优；没有改善时交付原评分胜者。

这些限制针对本轮简化前候选。发布前另外比较相同输入、参数和v5简化后的off/auto最终图：逐图线长不超过115%，单图交叉满足上述限制，整个验证集的总交叉不增加，不增加失败或总线降级。未通过门槛时保留默认关闭，不用局部成功代替全部验收。

## PowerShell复现

```powershell
# 原策略
.\.venv\Scripts\python.exe .\run_flow.py .\examples\4.cir --structure-layout off --out-dir .\output\structure-layout-comparison\manual\4-off

# 新策略；使用独立目录保护已有图片
.\.venv\Scripts\python.exe .\run_flow.py .\examples\4.cir --structure-layout auto --out-dir .\output\structure-layout-comparison\manual\4-auto

# 全数据集完整对照：图1～7及当前GT；不支持输入明确跳过
.\.venv\Scripts\python.exe .\tools\compare_structure_layouts.py --jobs 4

# 只看代表样例
.\.venv\Scripts\python.exe .\tools\compare_structure_layouts.py --cases data20 data21 data16 data24 --out-dir .\output\structure-layout-comparison\stage1
```

对照工具冻结源文件、模型声明、符号资源和代码签名。每个运行使用独立目录；中断后可以复用相同签名的结果。成功图独立复验真实引脚、固定出线、同网连通、异网分离、两种SVG一致性及独立重绘。GT仅作为视觉参考，未将其斜线或坐标当作算法规则或电气正确性的证明。

`comparison.json` 记录实际线长、交叉、折角、结构穿插、次序冲突、跨度、画幅、候选、耗时和恢复阶段；`index.html` 提供GT/off/auto三栏浏览。既有成功产物和原始网表保持独立保存。
