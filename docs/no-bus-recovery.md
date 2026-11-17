# 保留当次布局的无总线恢复

新网表默认不再自动生成大画布总线图。遇到困难连接时，先尽量保留已有导线，再用相同网络名称连接不同的局部组。

## 默认执行顺序

1. **正常出图。** 保留当前候选2、3及失败时候选1的布局、SA、A*、验收和选择规则。正常阶段已有合法成品时，直接使用原结果。
2. **局部救援。** 使用本次失败现场继续尝试，不重新安排器件。
3. **有限扩距。** 在同一份独立预算内，依次尝试默认1.5倍和4倍公共行列间距。器件行列、朝向及符号尺寸保持。
4. **网标恢复。** 使用本次候选及已完成的安全线路，继续连接容易的网络；困难网络让出搜索机会。无法完整连线的网络分成局部组，每组引出真实短线并标注原网名。必要时再扩大当前布局的公共间距。
5. **独立验收与输出。** 完整布线按原规则简化、验收；网标恢复按“实体线路＋同名标签”的规则单独验收并输出两种SVG。

默认预算为正常300秒、局部救援60秒、扩距60秒、网标恢复60秒，文件读写、最终复验和普通布线简化另计。所有恢复均为有限尝试，不能保证任意损坏输入或任意预算内都能出图。

## 不读取历史成品

恢复算法只接收**这次运行**产生的候选位置、原始引脚、已完成网络和失败分支。没有历史文件也能工作；不从GT、旧输出或缓存中查找替代图。测试报告读取GT仅用于展示和统计，不将GT坐标传给布局器。

扩距只改变公共物理轴。网标记录中的 `source_placement` 和 `final_placement` 分别保存恢复前后位置，便于核对行列、朝向与实际坐标。若在扩距阶段后进入网标恢复，还需查看 `spacing_retry` 的来源记录。

## 这里的“正确连接”怎样检查

网标恢复结果明确保存 `route_representation: wires_and_labels`，不能假装所有端子都由一条连续实体线连接。

- 从真实引脚和实际线段重新计算每个物理连通组，检查没有遗漏端子或固定出线。
- 每个需要跨组连接的局部组必须有一个真实接入的标签，文字与内部网络身份完全一致。
- 把同名标签视为等价连接后，原始端子的分组必须与网表一致。
- 实体导线、标签短引线、文字、器件、电源线路及标记继续接受各自的几何检查。
- 保存重绘会重新检查源网表、include及符号资源指纹，不相信旧的 `valid: true`。

网标可见线长与完整布线线长不是同一种指标。恢复结果的 `quality.score` 为 `null`，单独记录可见线路、标签数量和省略连接方式。

## 命令

```powershell
# 默认：正常算法优先；失败后局部救援、扩距、网标恢复
.\.venv\Scripts\python.exe .\run_flow.py .\examples\7.cir --out-dir .\output\7

# 调整最后网标恢复阶段的独立预算
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut --label-seconds 60 --out-dir .\output\5-dut

# 禁用自动恢复，观察正常算法的结果
.\.venv\Scripts\python.exe .\run_flow.py .\examples\7.cir --fallback off --out-dir .\output\7-normal-only

# 独立复验并重绘保存的网标方案，不重新摆件或选标签
.\.venv\Scripts\python.exe .\render-clean.py .\output\5-dut\5-dut-placement.json .\output\5-dut\independent-clean.svg
```

`--fallback` 只提供 `auto|off`。旧 `--fallback-seconds` 为网标预算兼容别名；新命令建议用 `--label-seconds`。历史总线JSON仍可读取和重绘，**新网表的自动生成路径没有总线阶段**。

## 实现文件与回归

| 文件 | 作用 |
|---|---|
| `netlist2schematic/flow/run_flow.py` | 恢复顺序、预算、保存与输出 |
| `netlist2schematic/recovery/spacing_retry.py` | 保持逻辑位置的物理扩距 |
| `netlist2schematic/recovery/labeled_recovery.py` | 当前现场继续布线、局部连接与安全标签构造 |
| `netlist2schematic/recovery/labeled_geometry.py` | 连通等价、几何、依赖及保存方案复验 |
| `tests/test_labeled_recovery.py` | 多端、多组、篡改、预算与重绘回归 |
| `tools/run_no_bus_dataset.py` | 从当前输入串行重跑两份数据集并独立验收 |

本轮完整回归为2285项通过，两份数据集46张全部通过独立验收、总线为0。新生成结果、实际阶段、GT对照及日志见 [无总线数据集对照](../output/no-bus-datasets/index.html)；清单保留每次命令与时间预算。[整批实测说明](no-bus-results.md)同时记录了补充验证及标签偏多的限制。
