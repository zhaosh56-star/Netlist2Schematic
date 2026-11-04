# SA Python 布局与布线

这套工具把 SPICE 结构网表转换为布局 JSON、彩色 SVG 和黑白 SVG。Python
脚本位于当前目录，`analog-canvas-main` 提供随仓库保留的符号定义和验收样例。
本地 `archive/` 如有原始 TXT 或实验记录，仅用于归档，不是运行依赖，也不进入 Git。
无需启动网页编辑器，也无需安装 Node.js、pnpm 或仿真器。

## 一次默认 flow 用到哪些代码

入口是 `run_flow.py`，默认执行 `layered + structured`；包含失败恢复时，运行可能涉及下列
**22 个 Python 文件**（按当前工作区源码核对）：

| 环节 | 文件 | 作用 |
| --- | --- | --- |
| 总入口 | `run_flow.py` | 解析命令参数，组织流程，保存 JSON 和两种 SVG |
| 网表解析 | `parse-netlist.py`、`netlist_core.py` | 统一读取器件及连接关系 |
| 分层 | `device_leveling.py` | 建立导通图，计算网络与器件层级 |
| 结构识别 | `circuit_structures.py`、`vf2_match.py` | 新布局识别有角色和父子关系的拓扑结构；VF2 保留给旧布局参考 |
| 结构化优化与比较 | `structured_placement.py`、`aesthetic_flow.py`、`layout_quality.py` | 生成结构种子、低成本 SA、预算管理和真实布线候选评分 |
| 布局与基础布线 | `place-layered.py`、`place-and-route.py` | 旧布局参考、分层挂位、符号坐标及快速布线估价 |
| 最终布线 | `sa_routing.py`、`orthogonal_router.py`、`routing_rules.py` | 统一几何规则、正交图 A*、拆网重布、原引脚连接检查及最终统计 |
| 失败恢复 | `local_rescue.py`、`spacing_retry.py`、`fallback_bus.py` | 保持布局局部救援；扩大物理间距重试一次；必要时构造大画布总线图 |
| 最终安全简化 | `simplify_routes.py` | 已验收路线的有限局部捷径；减少折角且不增加线长、交叉数 |
| SVG 输出 | `sa_render.py` | 使用同一份最终线段生成彩色和黑白图 |
| 公共支持 | `sa_common.py`、`sa_geometry.py`、`sa_saved.py` | 模块加载、资源路径、JSON、符号检查和固定布局恢复 |

还需要 `symbol-geometry.json`、原项目的 `packages/components/definitions/`、
所选网表及其本地 include 文件。`.venv/` 和 `.python/distribution/` 是当前运行环境，
两者都要保留；原项目网页代码不会在这个 Python 流程中执行。

`render-clean.py` 是独立重绘工具，不被默认入口调用。`tests/`、`examples/`、
`requirements.txt` 和 `setup_python.ps1` 分别用于验证、输入样例和重建环境。
示例输入保留在 `examples/`，生成物位于 `output/`；对比运行使用不同输出目录，
不要把可覆盖的生成物当作回归测试输入。

## 直接运行

当前工作区已经配置好 **Python 3.12.10 / NetworkX 3.7**。打开 PowerShell：

```powershell
Set-Location D:\gongzuo
.\.venv\Scripts\python.exe .\run_flow.py .\analog-canvas-main\netlists\native-common-source\circuit.spi --out-dir .\output\common-source
```

默认使用结构化分层布局，`--iterations 20000`、种子 `20261029`，每张图的
正常优化预算为 `--quality-seconds 300`。预算包括候选生成和最终布线；最多比较四个
通过最终路由器评估的候选，并选取已验收结果中综合评分最好的一份。有合格结果就退出恢复链；
完全失败时默认再尝试60秒局部救援、60秒扩距布线、60秒总线后备，优化预算合计约480秒。
选定最终结果后默认独立执行最多5秒安全简化；它不占用原SA、A*或恢复链预算。
按各项预算上限合计约485秒；总线后备不执行安全简化。
资源检查、文件输出及系统调度可能另有少量耗时。结果位于：

- `output/common-source/circuit-placement.json`：位置、朝向、层级、结构、布线和指标。
- `output/common-source/circuit-layered.svg`：彩色原理图。
- `output/common-source/circuit-clean.svg`：黑白原理图，可直接用浏览器打开。

不用激活虚拟环境，直接调用 `.venv\Scripts\python.exe` 即可。也可在任意目录使用
解释器、入口和网表的绝对路径；资源定位不依赖当前工作目录。

### 结构化布局、旧算法与候选重放

```powershell
# 默认新布局；输入网表只读，不增删器件或改变连接。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir --layout-style structured --quality-seconds 300 --out-dir .\output\structured\3

# 保留旧布局算法及旧最终布线代价，用独立目录比较。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir --layout-style legacy --fallback off --out-dir .\output\legacy\3

# 重跑指定候选；编号取自 JSON 的 quality.selected_candidate 或 quality.candidates。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir --candidate-id 1 --seed 20261029 --iterations 20000 --quality-seconds 300 --out-dir .\output\replay\3

# 当前四份输入逐张运行；正常阶段各300秒，必要时分别进入三个60秒恢复阶段。
foreach ($caseId in 1..4) {
    .\.venv\Scripts\python.exe .\run_flow.py ".\examples\$caseId.cir" --quality-seconds 300 --out-dir ".\output\structured\$caseId"
}
```

候选记录保留编号、候选种子和停止原因。重放时使用相同网表、源子电路、种子和
迭代参数；预算过小可能无法完成所请求的候选。严格恢复已选器件坐标时，使用
下面的 `--reroute-placement`，它不重新运行布局搜索。结构化选择中保留旧布局作为
参考候选，最终选中结果由实际布线评分决定。

编号 `0` 是旧布局参考，`1`、`2` 是独立结构种子，最后的 `3` 可以是第三个结构
种子，也可以是依据前面最佳结果生成的局部布线反馈候选。因此 `--candidate-id 3`
需要先评估前三个候选来恢复反馈依据；其他编号仅对指定候选执行最终布线。
指定编号重放时默认 `--fallback off`；该候选没能完成验收就报告失败，不以其他编号替代。
显式添加 `--local-rescue auto` 可允许候选失败后在原坐标下局部救援；
显式添加 `--fallback auto` 则允许完整恢复链，后续可能扩距或重新排布。
恢复结果会明确记录其执行阶段。

## 失败后的局部救援与恢复链

默认 `--fallback auto` 按顺序执行，前一阶段已获得合格结果就不会进入下一阶段：

1. **正常优化，默认 300 秒。** 保留已通过验收的最佳候选；不因后续候选失败而丢弃成功结果。
2. **局部救援，额外默认60秒。** 使用最后实际失败候选的安全检查点，保持逻辑行列、朝向和物理坐标，
   冻结已完成网络，先尝试接通失败网和待布网络。对临时出线接口沿原方向添加普通导线，
   同时检查源端和目标端；不能接入的目标会在搜索前返回 `no_safe_goal`。
   必要时只对失败网及最多两个已完成的阻挡网作事务重布；其余网络不动，失败事务全部撤销。
   真实引脚、固定出线段、分支、器件和电源标记继续使用原几何保护，延长部分不享受额外豁免。
   正常候选只要已有合格结果，就不会执行这一阶段。无布局、无检查点、显式关闭或预算为0时跳过。
3. **扩距重试一次，额外默认60秒。** 复制原正常阶段最后实际进入最终布线的失败候选，保留 row、col、orient，
   将 `column_x/row_y` 的相邻间距乘以 `--spacing-scale 1.5`，按路由网格（默认 2 px）向上取整，首坐标不变。
   符号大小、halo 和引脚局部坐标不缩放。从空路线和空搜索历史开始重新 A*，不运行 SA。
   若没有可复用的失败布局，或设置 `--spacing-seconds 0`，跳过此阶段。
4. **确定性总线后备，额外默认60秒。** 保留有源器件上下导通层级，重新安排横坐标；
   每器件独占竖向区域，每引脚独占通道，多端信号网通过上方水平 bus 完整连接。
   MOS/BJT 保持正常或左右镜像；电源/地标记可以移动并使用折线连接，但符号和所属网络保持。
   此阶段不运行 SA 或全局 A*，版本为 `constructive-bus-v1`。

扩距可能打开拥挤通道，也可能增加线长或搜索耗时，不能保证这一阶段成功。总线后备以正确连接为目标，
会产生大画布、长线和较多合法交叉，不适用正常布局的美观验收目标。每个成功阶段仍必须通过完整几何验收。

```powershell
# 自动执行必要的恢复阶段；四个阶段使用独立预算。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut --fallback auto --quality-seconds 300 --rescue-seconds 60 --spacing-scale 1.5 --spacing-seconds 60 --fallback-seconds 60 --out-dir .\output\auto\5

# 严格观察正常算法的失败，默认同时关闭局部救援、扩距和总线后备。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut --fallback off --out-dir .\output\normal-only\5

# 只增加局部救援，保留原布局；失败后不会扩大间距或重新排布。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut --fallback off --local-rescue auto --rescue-seconds 60 --out-dir .\output\local-only\5

# 直接生成大画布后备图；跳过正常优化、局部救援和扩距，也不执行 SA/A*。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut --fallback only --iterations 0 --fallback-seconds 60 --out-dir .\output\fallback\5

# 显式允许指定候选失败后恢复；结果可能改变其物理间距或横向排布。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut --candidate-id 2 --fallback auto --out-dir .\output\replay-auto\5
```

`--fallback only` 不能与 `--candidate-id` 同时使用。上述 300 秒正常阶段预算针对默认结构化流程；
`legacy/baseline` 保留原迭代行为，严格复现时建议使用 `--fallback off`。

局部救援只在正常阶段全部失败后执行，因而不会让失败候选取代正常阶段已有的成功候选。
它不继承正常阶段的历史拥塞惩罚，也不扩大外围搜索范围。普通输入的 `--local-rescue` 缺省跟随
`--fallback` 策略；显式指定 `auto` 可与 `--fallback off` 配合。设为 `off` 或 `--rescue-seconds 0`
会跳过局部救援。接口选择依据方向与现场几何，不按器件编号、网名或电路文件名设置坐标。
局部能接通一条网仍不能算整图成功；所有待布网络及电源几何必须完成整体验收。

恢复只处理布线失败，不吞掉未知器件、模型错误、源文件变化或资源不匹配等输入错误。
支持范围内的有效符号几何会在阶段预算内尝试构造并验收；超大输入、损坏资源、非法固定端口或预算耗尽
仍可能失败，不能把“任意网表都必出图”作为接口承诺。所有阶段失败时只写诊断，不覆盖已有成功产物。

## 最终路线的安全简化

默认 `--simplify-routes safe --simplify-seconds 5`，在最终候选选定并通过完整验收后、生成JSON和两种SVG前执行。
它保持器件位置、真实引脚、固定出线段、电源几何和所有间距规则，只尝试有限的正交局部捷径。
每个被接受的方案必须通过完整连接与几何验收，且折角严格减少、总线长和交叉数都不增加。
最多四轮、512个候选，5秒到期保留最后已完整验收的结果；没有改进时保留原路线。

这一步不运行SA或A*，不改变正常阶段的候选胜者、恢复顺序、路由权重或 `routing_stage`。
总线后备直接跳过；`--simplify-routes off` 或 `--simplify-seconds 0` 也会跳过。
最终 `metrics`、质量评分和两种SVG均使用简化后的线路；原候选评分和原路由搜索诊断作为来源记录保留。

```powershell
# 新的完整flow默认包含安全简化。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\4.cir --out-dir .\output\safe\4

# 关闭安全简化，用于复现原路由器的结果。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\4.cir --simplify-routes off --out-dir .\output\before-simplification\4

# 对已保存的合法线路单独简化，不运行布局、SA、A*或任何恢复阶段。
.\.venv\Scripts\python.exe .\run_flow.py --simplify-placement .\output\structured\4\4-placement.json --simplify-seconds 5 --out-dir .\output\simplified\4
.\.venv\Scripts\python.exe .\run_flow.py --simplify-placement .\output\local-rescue\5\5-dut-placement.json --simplify-seconds 5 --out-dir .\output\simplified\5
```

`--simplify-placement JSON` 必须包含完整 `routes`，先按源网表和当前几何规则重新验收，不能依赖旧的 `valid=true`。
它与网表输入、`--reroute-placement` 三选一；默认输出到 `output/<名称>-simplified`，不覆盖输入JSON或网表。
失败时保留已有成功输出。已经安全简化的文件在版本、预算、规则、布局/资源和规范化线路指纹均匹配时，
会在完整输入验收后直接复用保存结果，使同配置重复处理保持稳定；改线路或参数会重新尝试。
`render-clean.py` 独立重绘只验收和绘制保存的最终线路，不再次简化。

当前固定布局的实测结果如下。每个候选都经过整图连接与几何验收，器件位置、朝向及固定出线保持原样：

| 电路 | 线长：简化前→后 | 折角：简化前→后 | 交叉：简化前→后 | 简化耗时 |
| --- | ---: | ---: | ---: | ---: |
| 图4 | 3160→3060 | 38→19 | 6→6 | 约0.47秒 |
| 图5 | 9188→9130 | 64→47 | 19→19 | 约2.27秒 |

图4中 M4/M5 的共同 G 网 `f3` 已直接连接；它们的 S 极属于不同网络，没有相连。
图5中 XM14.D 保留向上的固定出线，之后用 L 形接入原 `vout` 干线，去除了中间阶梯。
两例均在第二轮确认没有进一步的本地改进，完整验收中的穿体、重叠及间距违规均为0。
结果位于 `output/simplified/4/`、`output/simplified/5/`；总对照记录为
`output/simplified/comparison.json`。关闭简化时图1～4的线路、位置和指标保持不变；
重复处理与独立重绘也已验证。这里的收敛只针对有限直线/L形候选，不代表全局最少折角。

### 其他电路

```powershell
# RC / RLC：先用较小迭代数检查完整链路。
.\.venv\Scripts\python.exe .\run_flow.py .\analog-canvas-main\netlists\native-rc-filters\circuit.spi --iterations 500 --out-dir .\output\rc
.\.venv\Scripts\python.exe .\run_flow.py .\analog-canvas-main\netlists\native-rlc-filter\circuit.spi --iterations 500 --out-dir .\output\rlc

# 网表仅定义子电路时，用 --cell 明确选择。
.\.venv\Scripts\python.exe .\run_flow.py .\analog-canvas-main\fixtures\simulation-acceptance\ota-5t.spi --cell ota_5t --out-dir .\output\ota

# 自包含的 BJT、二极管、E 源结构示例。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\mixed-devices.cir --iterations 500 --out-dir .\output\mixed

# 原始自由布局 SA，用于和分层版本比较。
.\.venv\Scripts\python.exe .\run_flow.py .\analog-canvas-main\netlists\native-common-source\circuit.spi --mode baseline --iterations 500 --out-dir .\output\baseline
```

两个模式默认都输出三份文件。未提供 `--out-dir` 时，输出至当前工具目录的
`output/<电路名>-<模式>/`；常见的 `circuit.spi` 用其父目录名区分电路。
同一输出目录中再次运行会更新同名生成物；比较两次实验时请使用不同的输出目录。

## 已有布局只重布线

下面的命令直接复用 JSON 中的每个器件 row、col、orient，跳过 SA，用路由器
重新生成三份结果。新 JSON 中的 `layout_grid.column_x`、`layout_grid.row_y`
也按原值恢复，因此紧凑网格的实际器件坐标保持一致。旧 JSON 无物理坐标表时，
继续按原来的 150 网格恢复。示例输出到独立目录，保留原 JSON 和 SVG：

```powershell
.\.venv\Scripts\python.exe .\run_flow.py --reroute-placement .\output\3\3-placement.json --out-dir .\output\3-rerouted
.\.venv\Scripts\python.exe .\run_flow.py --reroute-placement .\output\2\2-placement.json --out-dir .\output\2-rerouted
.\.venv\Scripts\python.exe .\run_flow.py --reroute-placement .\output\structured\3\3-placement.json --out-dir .\output\structured\3-rerouted
```

不指定输出目录时，结果保存在 `output/<原 JSON 文件基名>-rerouted/`。
网表位置参数与 `--reroute-placement` 互斥；重布线保留原先的分层、结构和优化信息，
用 `rerouted_from` 记录输入 JSON。旧布局存在已保存的路由参数时按相同参数重布线。

`--reroute-placement` 不触发扩距或重新排布，因此不能与 `--fallback auto/only` 组合。
固定布局重布默认不启动局部救援；可以显式开启，救援预算从原 A* 失败后独立开始：

```powershell
.\.venv\Scripts\python.exe .\run_flow.py --reroute-placement .\tests\fixtures\junction-clearance\5-candidate2-placement.json --local-rescue auto --rescue-seconds 60 --out-dir .\output\local-rescue\5
```
普通布局沿用 A*；总线布局按保存的 `constructive-bus-v1` 通道重建信号线，保留全部物理坐标、朝向及
`power_connections` 的 anchor 和折线路径，不重新运行 SA/A*，也不重新摆放电源标记：

```powershell
.\.venv\Scripts\python.exe .\run_flow.py --reroute-placement .\output\fallback\5\5-dut-placement.json --out-dir .\output\fallback\5-rerouted
.\.venv\Scripts\python.exe .\render-clean.py .\output\fallback\5\5-dut-placement.json .\output\fallback\5-redraw.svg
```

旧 JSON 没有显式电源几何时保留原 stub/标记规则；总线 JSON 必须保留支持的 `fallback.version`
和 `power_connections` 列表。缺失、为 null 或路径不合法时会报错，不会悄悄退回旧电源 stub。

## 参数及独立脚本

| 参数 | 含义 | 默认值 |
| --- | --- | --- |
| `--mode` | `layered` 固定层级；`baseline` 自由网格布局 | `layered` |
| `--layout-style` | `structured` 使用结构化候选选择；`legacy` 保留旧分层算法；用于 `layered` 模式 | `structured` |
| `--quality-seconds` | `structured` 每张图的候选优化及最终布线时间预算，必须为正数 | `300` |
| `--candidate-id` | 重放一个结构化候选编号，编号以保存的 JSON 为准 | 自动比较候选 |
| `--fallback` | `auto`：必要时局部救援、扩距及总线恢复；`off`：默认仅正常流程；`only`：直接总线构造 | 网表输入为 `auto`；指定候选为 `off` |
| `--local-rescue` | `auto`：正常阶段全部失败后尝试局部救援；`off`：禁用；显式开启可与 `--fallback off` 配合 | 跟随 `--fallback`；固定布局重布为 `off` |
| `--rescue-seconds` | 独立局部构造、搜索和完整验收预算；0表示跳过 | `60` |
| `--simplify-routes` | `safe`：在最终结果选定后安全简化；`off`：保留原路由线路 | `safe` |
| `--simplify-seconds` | 独立安全简化预算；0表示跳过，不占用SA/A*或恢复链预算 | `5` |
| `--simplify-placement` | 对已有合法 `routes` 单独简化；不重新布局、布线或恢复 | 不启用 |
| `--spacing-scale` | 扩距时相邻物理行列间距的倍率，必须大于 1 | `1.5` |
| `--spacing-seconds` | 独立扩距 A* 的预算；0 表示跳过扩距阶段 | `60` |
| `--fallback-seconds` | 独立总线构造和完整验收预算，必须大于 0 | `60` |
| `--iterations` | 非负整数，控制 SA 提案预算；0 表示只比较初排候选；结构化分配到多个种子 | `20000` |
| `--seed` | 随机种子 | `20261029` |
| `--out-dir` | 生成物目录 | 工具目录下的 `output/` 子目录 |
| `--cell` | 选择一个 `.subckt` | 顶层器件 |
| `--reroute-placement` | 从已有 JSON 恢复位置，跳过 SA，只重布线 | 不启用 |
| `--analog-canvas-root` | 原项目目录，内部应含 `packages/components/definitions` | 相邻 `analog-canvas-main` |

结构化模式为旧参考候选保留 `iterations` 次提案，另外三个结构种子共同分配约
`iterations` 次提案；这些是上限，时间预算可能提前停止。`annealing.steps_completed`
记录实际完成的步数。保存的物理坐标和最终路线可精确恢复；时间预算触发时，相同
种子能执行的步数会受机器负载影响，不能只凭种子推断被截断的位置。

也可通过 `ANALOG_CANVAS_ROOT` 环境变量设置资源根。六个落地脚本保留独立入口：

```powershell
.\.venv\Scripts\python.exe .\parse-netlist.py .\examples\mixed-devices.cir
.\.venv\Scripts\python.exe .\device_leveling.py .\examples\mixed-devices.cir
.\.venv\Scripts\python.exe .\vf2_match.py .\analog-canvas-main\fixtures\simulation-acceptance\ota-5t.spi --cell ota_5t
.\.venv\Scripts\python.exe .\place-layered.py .\examples\mixed-devices.cir --iterations 500 --out-dir .\output\layered-demo
.\.venv\Scripts\python.exe .\place-and-route.py .\examples\mixed-devices.cir --iterations 500 --out-dir .\output\baseline-demo
.\.venv\Scripts\python.exe .\render-clean.py .\output\common-source\circuit-placement.json .\output\common-source\redraw.svg
```

新 JSON 记录源网表、所选子电路和最终线段，独立重绘无需重新运行 SA。
旧 JSON 没有源信息时，仍按同目录下的 `<base>.cir` 推断并重新布线。
所有保存的走线均重新按当前规则验收，不信任旧 `valid=true`；旧结果不合格时，
可使用上述 `--reroute-placement` 修复。独立重绘恢复保存的线宽等参数。
输出路径不能与输入 JSON 或源网表相同。

## 支持的网表

- M：四端 MOS；Q：NPN/PNP，可带衬底和参数。
- R、C、L、D：两端无源件与二极管；V、I：独立源。
- E：输出负端为地别名的线性受控源，以三端运放代理符号显示。
- X：明确含 `nfet/pfet` 型号的四端 MOS 写法，包括样例中的 SKY130 器件。
- 本地 `.model`、`.include` / `.inc`、`+` 续行、常见仿真指令和 `.control` 块。

型号极性由本地声明或明确的 `nmos/pmos/nfet/pfet/npn/pnp` 名称规则判断。
网络名不区分大小写，实例 ID 保留输入写法。数值、参数及表达式保留用于结构读取，
不执行电气仿真或参数求值。

子电路必须显式选择，不自动递归展开。未知 X、其他器件类型、无法判断的型号、
非接地参考或非线性 E 源会报出源文件及行号，并以非零状态退出，不会静默漏画。
本轮只使用 MOS 子结构模板；BJT 能分层和绘图，不参与 MOS 模板匹配。
MOS 体端、BJT 衬底和 E 输出负端沿用默认符号的可见端子投影，不单独布线。

## 结果与算法约束

`layered` 保留 MOS/BJT 的导通层级和左右镜像约束，按电流路径从上往下排列。
`legacy` 保留原有分层 SA 的能量项、权重和网格，便于复现旧流程。
指定固定位置的器件在初始化、移动和交换时均受到保护。

`structured` 先按连接拓扑识别高置信结构，保留器件角色和父子结构块。差分对需要
独立共同源节点、两支路和尾部供给证据；仅共地、共电源或公共源器件组不会直接
当作差分对。电流镜区分二极管接法的参考管与输出管，互补支路区分上、下管；
交叉耦合和互补支路可组成锁存父块，折叠核心保留输入、折叠和偏置支路角色。
普通共偏置和串接关系可以重叠存在，不抢占高置信结构的器件归属，也不单独画框。
结构规则不依赖电路文件名或 M 编号。

初排按上下支路对齐并安排结构块，随后使用整块移动、块交换、支路调整、整体
镜像及块内微调。逻辑行列仍保留，物理坐标按符号大小、halo 和布线通道压缩，
不再统一采用 150 间距。含 E 运放代理的电路按真实 IN+/IN− 端口识别输出反馈
路径、串联链和局部并联/接地支路，使无源网络围绕运放布局，不借增加层号拉长环路。

新 SA 用合法动作的能量增量采样标定初温，初始上坡接受率目标为 70%；停滞时
有限重热。JSON 记录采样、接受率、重热和停止原因，便于检查优化是否充分。
线长、交叉、折角、支路错位和结构分散分别计分，不再使用结构面积乘整图线长
的耦合项。最终综合评分基于实际绘制线段，包含线长、交叉、折角、结构对齐和
超过端点包围盒跨度的绕行；长直干线不会仅因长而受到额外惩罚。

**SA 的迭代热路径不调用 A\***：旧布局用快速估价布线，新布局用低成本几何估价。
候选布局冻结后才执行最终正交图 A*；结构化流程最多真实布线四个候选。
默认交叉代价相当于 150 单位线长，折角相当于 15 单位；`legacy` 保留 500/30。
最终候选用真实路由比较，逐网记录线长、端点跨度、绕行系数与交叉等反馈数据。
若前面的最佳结果有严重绕行（逐网绕行系数至少 2.5，且超出端点跨度的线长至少
500），最后一个候选槽优先尝试扩宽相关行间的局部通道，再重新布线验收；没有
合适反馈位置时仍使用独立结构种子。反馈候选只有评分更好才替代默认流程的最佳结果。
预算耗尽后保留已验证的最佳候选；若尚无合格候选，`auto` 进入上述恢复流程，`off` 则报告失败。
最终路由按端子数、
跨度和网名确定顺序，每网逐步接入已有同网树，允许共享干线。需要时拆掉最多两个
阻塞网络重布，整个场景最多四轮；冲突历史参与代价，避免反复占用同一通道。

默认线宽 2.5，笔画边缘净距至少 6；按 2 单位网格向上取整后，异网平行线中心距
至少 10。器件 halo 保持 12，只有指定引脚的固定出线段可以穿过自己的 halo。
引脚与同网分支点有 12 单位保护区。电源 stub 和电源/地符号提前进入障碍模型，
不作为最终绘图时才添加的自由通道。

异网共线重叠、端点接触、T 型误接、穿体和间距不足都是硬错误。允许普通十字交叉，
但两条线必须直线穿过，且距真实引脚、拐角、分支和线端至少 12；交点不画圆点，
不用跨线符号。同网真实分支继续画圆点。纯共线切点不影响碰撞分类和交叉统计。

两种 SVG 使用同一份通过完整验收的最终线段，最终结果不再经过旧的局部挪线后处理。
检查包括原引脚、连通性、正交性、异网间距、合法交叉净区和固定障碍物。任一项不满足
均不会标为成功。

外围通道可依次扩大至包围盒外 150、300、600；单分支搜索最多展开 200,000 个状态，
坐标图最多 100,000 个节点。单次 A* 固定本次器件位置；`auto` 后续可能扩距或重新排布，但不降低几何间距。
最终仍失败时以非零状态退出，并写 `routing-diagnostics.json` 列出各阶段、网络、位置或预算原因，
不覆盖已有成功产物。预算内未找到路线不等于数学上无解。重布线耗时记录在 `validation.routing_seconds`。

JSON 的主要字段：

- `placement`、`levels`、`attach`、`structs`、`sym_pairs`：布局与结构。
- `layout_style`、`layout_grid`：布局策略及物理行列坐标；缺失时兼容旧网格。
- `structs[*].roles`、`confidence`、`parent`、`children`、`owner`、`layout`：结构角色、置信类别、父子块、唯一器件归属和对齐建议。
- `candidate_id`、`candidate_seed`、`annealing`：所选布局种子、退火温度、接受率及停止原因。
- `quality`：预算、总耗时、停止原因、所选候选、综合评分、各项分数、逐网绕行指标和全部已尝试候选的验收结果。
- `routes`：最终正交线段；`metrics`：这些实际绘制线段的统计。
- `optimization_metrics`、`energy`：SA 优化时粗布线的统计及目标值，可能与最终指标不同。
- `source_netlist`、`source_sha256`、`source_cell`、`mode`、`seed`、`iterations`：复现输入及原始字节哈希。
- `validation`：最终几何检查、路由参数、搜索统计和耗时。
- `routing_stage`：`normal`、`local_rescue`、`spacing_retry` 或 `bus_fallback`；`recovery.stages` 保存各阶段结果、耗时和失败诊断。
- `local_rescue`：构造版本、来源候选、安全接口延长、被重布网络、耗时和实际最终质量；检查点运行对象不写入JSON。
- `route_simplification`：版本、独立预算、简化前后指标、改变的网络、候选计数、耗时及停止原因；`quality_before` 保留原质量与原候选记录，`quality_after` 对应最终线路。
- `quality.score/details` 在安全简化后重新计算；`quality.candidates` 仍描述原候选选择，`candidate_metrics_stage: before_route_simplification` 明确其来源阶段。`validation.search` 保留原路由搜索记录。
- `spacing_retry`：来源候选、扩距倍率、原/新物理轴和原优化记录；扩距后最终指标重新计算。
- `fallback`：总线版本、pitch、器件顺序、bus 坐标和构造参数。
- `power_connections`：逐端口保存 `component_id/pin/net/symbol/anchor/segments`；anchor 是 glyph **真实引脚的世界坐标**，
  glyph 原点按 `anchor − 本地引脚坐标` 计算（VDD 引脚为 `(0,20)`，ground 为 `(0,-10)`）。segments 是从原器件引脚
  到该 anchor 的完整有序正交路径。恢复、验收和两种 SVG 共用这份电源几何。

最终线长去除同网重复区间；折角数为同网线段图中仅连接两条不同方向线段的节点数。
交叉统计根据最大连续直线和真实结构点计算，不受人工切段影响。重叠、穿体和贴线
必须通过验收；`validation.valid` 表示全部硬约束通过，允许的普通十字交叉可以非零。
`metrics.wirelength` 统计信号线；显式电源几何另有 `metrics.power_wirelength`。电源引线也检查穿体、异网及
marker 冲突；只有准确连接自身符号引脚的末端接近通道可进入自己的 marker halo。

`symbol-geometry.json` 保留 TXT 中的朝向编号，并已依据原项目默认符号修正组合
旋转/镜像的坐标、方向和包围盒。更新原项目符号后可运行 `sa_geometry.py` 重建，
然后执行下述回归测试。使用其他资源根时，重建命令也接受
`--analog-canvas-root`；运行入口会检查缓存与所选符号库是否一致。

## 环境重建与验证

在当前目录缺少环境时，运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\setup_python.ps1
.\.venv\Scripts\python.exe -m unittest discover -s .\tests -v
.\.venv\Scripts\python.exe -m pip check
```

设置只作用于这个 PowerShell 子进程，不修改系统执行策略。脚本从 Python 的 NuGet
发行包下载固定版本，在 `.python/` 解压、检查 Python 可执行文件签名，再建立
`.venv/` 并安装 `requirements.txt`。不修改系统 PATH，也不要求管理员权限。
移动整个目录后应重建虚拟环境，并重新运行入口生成记录了新源路径的 JSON。

测试覆盖统一解析和拒绝边界、器件层级、固定行/位置、所有符号朝向、原引脚连接、
异网间距/接触/交叉净区、多障碍和拆网重布、预算耗尽、失败文件保护、跨工作目录、
旧 JSON、物理坐标恢复、独立重绘、结构角色、改名及重排行后结构等价、整体块动作、
自适应退火、最终评分和候选选择。固定输入与旧布局作为独立测试基线保存在
`tests/fixtures/`，测试不依赖可覆盖的 `output/` 生成物。

原有完整回归为200项；局部救援另增接口、检查点透传和恢复选择回归。验收还包括独立预算、电源有序路径和真实锚点、
power 穿体/marker 审计、禁止后备调用 SA/A*、JSON 保存恢复及确定性。正常流程另有 RC、RLC、共源级、
指定 OTA 子电路及混合器件样例，历史结果位于 `output/structured/smoke/`。

图5冻结候选2的历史局部救援实测（安全简化前）从已复验的失败检查点开始，不重新运行 SA：约11.6秒完成整图验收，
信号线长9188、交叉19、折角64，穿体、重叠和间距违规均为0。器件逻辑位置、朝向和物理坐标不变，
原来已完成的5个网络保持原线路，本例只需初始续布，没有拆网。
结果及对照记录保存在 `output/local-rescue/5/`，正常图1～4的完整线路一致性记录位于
`output/local-rescue/regression/comparison.json`。生成物不随Git提交；上面的固定布局重布命令可重新生成，
其总耗时还包含原A*失败前的搜索，60秒仅指失败后新增的救援阶段。
复现该阶段的原始路线时添加 `--simplify-routes off`；当前默认还会执行后续安全简化。

`validation.valid` 证明几何硬约束通过，并不表示图形达到全部美观目标。评估新旧
布局应同时比较最终线长、交叉、折角、结构跨度/对齐和逐网绕行，并查看 SVG。
四图总线长降低至少 20%、总交叉不增加和逐图综合评分不劣是验收目标；实际是否
达到应以对应运行生成的对比数据为准，不能用 SA 的 `energy` 或运行成功代替。
上述美观指标针对布局优化；局部救援首先恢复完整合法连接，不保证得到最短线路或最少交叉。
