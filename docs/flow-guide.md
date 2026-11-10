# SA Python 布局与布线

这套工具把 SPICE 结构网表转换为布局 JSON、彩色 SVG 和黑白 SVG。Python
脚本位于项目根目录，`analog-canvas-main` 提供随仓库保留的符号定义和验收样例。
本地 `archive/` 如有原始 TXT 或实验记录，仅用于归档，不是运行依赖，也不进入 Git。
无需启动网页编辑器，也无需安装 Node.js、pnpm 或仿真器。

日常命令见 [README](../README.md)，全部文件的职责和保留范围见[项目目录说明](project-layout.md)。
当前783项完整回归通过；可选结构布局仍默认关闭，完整数据集与RLC专项的实际收益和未达标项见[分阶段实测](structure-layout-results.md)，缩线预算预留见[v6说明](route-scheduling-v6.md)，按链轮转见[v7说明](track-fairness-v7.md)，交叉净区预筛见[v8说明](candidate-clearance-v8.md)，可选自动预算见[预算说明与对照](auto-simplify-budget.md)。下文各历史版本的测试数量与实测数字保留当时口径。
希望先按执行顺序理解算法，可读[当前flow逐步详解](flow-walkthrough.md)，其中分别解释解析、分层、两类SA估价、最终A*、恢复链、v4简化及JSON记录。
下面的 PowerShell 命令均从项目根目录执行；日常输出集中在 `output/<网表标签>/`，不依赖历史出图。

## 一次默认 flow 用到哪些代码

入口是 `run_flow.py`，默认执行 `layered + structured`，下表列出原主线和恢复链的核心文件。
预算参数校验另外使用`candidate_budget.py`；显式结构布局、静态端口修复与展示使用的新增模块见[结构布局说明](structure-layout.md)，实验对称规整仍由`symmetry_layout.py`单独控制。

| 环节 | 文件 | 作用 |
| --- | --- | --- |
| 总入口 | `run_flow.py` | 解析命令参数，组织流程，保存 JSON 和两种 SVG |
| 网表解析 | `parse-netlist.py`、`netlist_core.py` | 统一读取器件及连接关系 |
| 分层 | `device_leveling.py` | 建立导通图，计算网络与器件层级 |
| 结构识别 | `circuit_structures.py`、`vf2_match.py` | 新布局识别有角色和父子关系的拓扑结构；VF2 保留给旧布局参考 |
| 可选证据校准 | `layout_evidence.py`、`layout_intent.py` | 仅结构布局auto构造校准的供电／输入对、对应支路和端口附件；独立对照测量使用同一清单 |
| 结构化优化与比较 | `structured_placement.py`、`aesthetic_flow.py`、`layout_quality.py` | 生成结构种子、低成本 SA、预算管理和真实布线候选评分 |
| 无源灵活布局 | `passive_placement.py` | R/C/L/D跨行试摆、当前行同步及串联/并联/反馈软评分 |
| 可选器件对称规整 | `symmetry_layout.py` | 仅显式 `--symmetry-layout auto` 时，候选2/3在A*前规整器件位置与朝向；局部轴、组合核心及独立对称指标 |
| 布局与基础布线 | `place-layered.py`、`place-and-route.py` | 旧布局参考、分层挂位、符号坐标及快速布线估价 |
| 最终布线 | `sa_routing.py`、`orthogonal_router.py`、`routing_rules.py` | 统一几何规则、正交图 A*、拆网重布、原引脚连接检查及最终统计 |
| 失败恢复 | `local_rescue.py`、`spacing_retry.py`、`fallback_bus.py` | 保持布局局部救援；扩大物理间距重试一次；必要时构造大画布总线图 |
| 最终安全简化 | `simplify_routes.py`、`route_reattachment.py`、`route_patterns.py`、`route_tracks.py` | 分阶段调度局部捷径、叶支路重接、三段式候选及原轨道收紧；各项均不增加 |
| 可选自动缩线预算 | `simplify_budget.py` | 按已合法线路的复杂度选5～30秒，一次执行；默认仍固定5秒 |
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
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir
```

默认使用结构化分层布局，`--iterations 20000`、种子 `20261029`，每张图的
正常优化预算为 `--quality-seconds 300`。预算包括候选生成和最终布线；默认最多比较编号1、2、3这三个
通过最终路由器评估的候选，并选取已验收结果中综合评分最好的一份。有合格结果就退出恢复链；
完全失败时默认再尝试60秒局部救援、60秒扩距布线、60秒总线后备，优化预算合计约480秒。
选定最终结果后默认独立执行最多5秒安全简化；它不占用原SA、A*或恢复链预算。
按各项预算上限合计约485秒；总线后备不执行安全简化。
资源检查、文件输出及系统调度可能另有少量耗时。结果位于：

- `output/3/3-placement.json`：位置、朝向、层级、结构、布线和指标。
- `output/3/3-layered.svg`：彩色原理图。
- `output/3/3-clean.svg`：黑白原理图，可直接用浏览器打开。

图5的命令为 `.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut`，
默认输出到 `output/5-dut/`，文件前缀为 `5-dut`。日常无需指定 `--out-dir`；比较参数或算法时再显式使用独立目录。

不用激活虚拟环境，直接调用 `.venv\Scripts\python.exe` 即可。也可在任意目录使用
解释器、入口和网表的绝对路径；资源定位不依赖当前工作目录。

## 无源器件灵活跨行布局

默认使用 `--passive-layout flexible`。可以把它理解成：先照原来的方法排好器件，再允许电阻、电容、电感和二极管在附近找更合适的空位。它们可以与MOS/BJT同行，但不能插进该行有源子结构的内部，也不能通过新无源动作挪动有源器件。MOS/BJT仍按导通层级上下排列，E/V/I仍保留初排行；这些器件原有的水平移动、组调整和允许朝向继续有效。

新策略只作用于含R/C/L/D的结构候选1～3。初始化增加一行、两列余量，SA期间不扩轴；一半提案沿用原动作，一半尝试无源二维移动或无源交换。串联共享端子距离、并联端点对齐及反馈端口距离提供软评分，按100单位归一化后分别乘0.5、1.0、0.25。相同关系去重后取平均，共享反馈器件可同时保留多条关系，但不会复制器件或形成刚体。最终A*、几何验收和真实候选评分均保持原规则。

```powershell
# 当前默认策略，输出到独立目录，保留原主图
.\.venv\Scripts\python.exe .\run_flow.py .\examples\1.cir --passive-layout flexible --out-dir .\output\1-flexible

# 复现原来的无源固定行行为
.\.venv\Scripts\python.exe .\run_flow.py .\examples\1.cir --passive-layout fixed --out-dir .\output\1-fixed

# 单独验证新候选；候选重放默认关闭恢复，需恢复时显式指定 --fallback auto
.\.venv\Scripts\python.exe .\run_flow.py .\examples\1.cir --candidate-id 1 --passive-layout flexible --out-dir .\output\1-flexible-candidate1

# 图1～7及混合器件的完整fixed/flexible对照，支持完成后断点复用
.\.venv\Scripts\python.exe .\tools\compare_passive_layouts.py --jobs 2
```

对照入口为 `output/passive-flex-comparison/index.html`，机器可读记录为同目录 `comparison.json`。比较工具对图6使用已有的归一化副本，对图7和混合器件使用已有的显式模型声明包装输入，保留来源记录；原始网表不改写。每次完整运行使用独立目录，对保存结果再次检查连接、几何、两种SVG和独立重绘。

JSON中的 `requested_passive_layout` 是请求，`passive_layout_policy` 才是实际选中的策略。`passive_layout` 记录算法版本、可移动器件、固定行、规整评分和提案次数；合法提案还可能被SA拒绝，计数包含温度采样，不能当作最终接受的移动次数。旧JSON不需要补字段，已有坐标在重布、单独简化及重绘时保持原样。

当前默认只生成和评估候选1、2、3，共用300秒；候选0的初始化、SA和最终A*均跳过。指定 `--candidate-id 0` 才运行旧参考。候选1～3的种子、初排、提案数和局部SA上限保持原值；候选1的布线仍可能耗尽剩余预算，不保证三个候选平分时间。最后一个槽位按稳定编号3识别，若使用已有最佳布局的布线反馈版本，则继承来源策略，不另跑无源SA。新策略提供更多摆法，但不承诺每张图的每项美观指标都下降。

本次回归共343项全部通过，包括原有313项和新增30项。新增测试覆盖跨行/同行空位、无源交换、有源组内部保护、提案接受/撤销及温度采样后的状态同步、固定模式和无无源件兼容、平移/合法镜像、共享反馈关系，以及保存JSON不重新布局。另将修改前源码作为只读参考，对12个固定行种子运行300步SA，位置、朝向、物理轴、随机状态及完整退火结果一致。

以下是无源灵活布局v1配合最终简化v4、且当时默认仍包含候选0时，相同种子、迭代数及预算的冻结完整flow实测。指标已经过当时的最终安全简化和独立复验，Q越低越好；这里的两列是请求策略，实际选中策略另见备注及JSON。v5新增轨道收紧的后处理对照另见下文；默认跳过候选0不回写这组历史结果。

| 电路 | fixed：线长/交叉/折角 | flexible：线长/交叉/折角 | 真实Q：旧→新 | 备注 |
| --- | --- | --- | --- | --- |
| 图1 | 1134 / 1 / 8 | 968 / 0 / 13 | 15.442→13.032 | 五行变三行，器件中心高度424→212；画布407×660→456×448 |
| 图2 | 4044 / 11 / 19 | 4044 / 11 / 19 | 64.324→64.324 | 无R/C/L/D；位置、物理轴、线路及指标一致 |
| 图3 | 2262 / 2 / 15 | 2149 / 1 / 13 | 30.750→27.115 | 实际选中灵活布局 |
| 图4 | 2802 / 6 / 15 | 2802 / 6 / 15 | 43.752→43.752 | 无R/C/L/D；位置、物理轴、线路及指标一致 |
| 图5 | 8779 / 13 / 31 | 8779 / 13 / 31 | 151.855→151.855 | 两侧均候选0＋局部救援，实际策略均fixed；新候选未进入默认布线 |
| 图6 | 4230 / 5 / 30 | 4923 / 2 / 30 | 61.684→68.699 | 交叉减少，线长和综合评分退步 |
| 图7 | 2676 / 2 / 20 | 3794 / 2 / 32 | 34.852→52.034 | 线长、折角和综合评分退步 |
| 混合器件 | 1452 / 1 / 15 | 1480 / 1 / 19 | 20.246→21.164 | 本次综合评分略退步 |

16次默认运行均通过完整验收，输出保存在独立对照目录，原有主图没有覆盖。图1达到不再受五行限制且真实评分不劣的目标；该表同时说明，增加摆放自由度不能保证每个电路都更美观。需要原算法时使用 `--passive-layout fixed`。图5的额外候选1回放单独展示，不替换表中的默认流程结果。完整宽高、耗时、候选记录、来源哈希和双SVG可在对照页与 `comparison.json` 中查看。

图5额外候选1回放的两边均在正常A*截止后，经原有局部救援通过完整验收：fixed为5127 / 8 / 30、Q86.529，约311.78秒；flexible为5412 / 7 / 27、Q84.854，约307.05秒，实际策略为flexible。新策略在这份布局上减少交叉和折角，线长增加，综合评分略好。这里只跳过其他候选的最终布线，没有改正常300秒预算、救援60秒预算或预算调度；不进入扩距/总线重排。这2份额外结果与16份默认结果共18份，全部通过原引脚、几何、固定出线、双SVG一致和独立重绘检查。

### 结构化布局、旧算法与候选重放

默认结构化流程按1→2→3顺序生成并评估候选。三个种子直接使用统一网表解析结果，不再为了取得器件列表而先计算旧布局。`--iterations 20000` 对应每个结构种子最多 `20000 // 3 = 6666` 次提案，每种SA仍最多约12秒，并受共享300秒截止时间约束。选择 `--candidate-id 0` 时单独运行原旧参考，最多20000次旧SA；选择1、2或3时不运行0的初始化或SA。候选3回放需要评估来源候选1、2，以保留布线反馈语义。

```powershell
# 默认新布局；输入网表只读，不增删器件或改变连接。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir --layout-style structured --quality-seconds 300

# 保留旧布局算法及旧最终布线代价，用独立目录比较。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir --layout-style legacy --fallback off --out-dir .\output\3-legacy

# 重跑指定候选；编号取自 JSON 的 quality.selected_candidate 或 quality.candidates。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir --candidate-id 1 --seed 20261029 --iterations 20000 --quality-seconds 300 --out-dir .\output\3-replay

# 显式运行候选0旧参考；默认不会运行它
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir --candidate-id 0 --out-dir .\output\3-legacy-reference

# 当前四份输入逐张运行；正常阶段各300秒，必要时分别进入三个60秒恢复阶段。
foreach ($caseId in 1..4) {
    .\.venv\Scripts\python.exe .\run_flow.py ".\examples\$caseId.cir" --quality-seconds 300
}
```

候选记录保留编号、候选种子和停止原因。重放时使用相同网表、源子电路、种子和
迭代参数；预算过小可能无法完成所请求的候选。严格恢复已选器件坐标时，使用
下面的 `--reroute-placement`，它不重新运行布局搜索。结构化默认在候选1～3中按实际布线的原Q选优，
旧参考仅通过显式候选0回放运行。默认 `--symmetry-layout off`，日常不加参数便跳过额外器件对称规整和S对称选优。
只有显式 `--symmetry-layout auto` 才保持候选1的原摆法、规整2/3，并在本轮原Q参考125%的实际线长范围内，按高置信关系满足情况、网格精度的位置残差、原Q依次选择。详细顺序、限制及显式auto/off实验见[器件对称布局说明](symmetry-layout.md)。

编号 `0` 是显式回放的旧布局参考，`1`、`2` 是默认独立结构种子，最后的 `3` 可以是第三个结构
种子，也可以是依据前面最佳结果生成的局部布线反馈候选。因此 `--candidate-id 3`
需要先评估候选1、2来恢复反馈依据；其他编号仅对指定候选执行最终布线。
指定编号重放时默认 `--fallback off`；该候选没能完成验收就报告失败，不以其他编号替代。
显式添加 `--local-rescue auto` 可允许候选失败后在原坐标下局部救援；
显式添加 `--fallback auto` 则允许完整恢复链，后续可能扩距或重新排布。
恢复结果会明确记录其执行阶段。

本轮默认跳过0的回归共362项全部通过。另对8种电路、fixed/flexible两种策略、3种结构种子共48套布局逐项比较，跳过0前后的位置、朝向、物理轴、随机状态、能量和元数据一致。编号3的反馈判断使用实际ID，不再依赖生成序列中的下标；全部失败时仍保存最后实际进入布线的候选现场，不要求存在0。正常选优的 `quality` 及正常失败诊断记录 `candidate_policy`、`candidate_limit`、`legacy_reference_enabled`，恢复结果可从前序正常阶段诊断追溯该策略。

新的完整flow实测保存在独立的 `output/structured-only-candidates/index.html`，机器记录为同目录 `smoke-summary.json`。运行时通过guard禁止调用旧参考初始化，图5和图7均直接从候选1开始最终A*，剩余正常预算约298秒：

| 电路 | 实际布线候选 | 选中 | 最终阶段 | 总耗时 | 线长/交叉/折角 |
| --- | --- | --- | --- | ---: | --- |
| 图5 dut | 1、2 | 2 | local_rescue | 约310.03秒 | 6304 / 10 / 38 |
| 图7 | 1、2、3 | 2 | normal | 约249.01秒 | 3714 / 2 / 30 |

两例均通过完整验收、固定出线、双SVG一致、独立重绘及源/代码哈希检查。图7与旧版同ID2的保存布局在位置、物理轴、种子、SA能量和估价指标上完全一致。图5的候选1仍消耗大部分搜索预算，2超时后进入原有救援，3未执行；跳过0不意味着每个新候选都能获得等额时间，也不保证整次运行按省下的旧开销等量缩短。原网表、原主图及旧对比报告保留。

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
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut --fallback auto --quality-seconds 300 --rescue-seconds 60 --spacing-scale 1.5 --spacing-seconds 60 --fallback-seconds 60

# 严格观察正常算法的失败，默认同时关闭局部救援、扩距和总线后备。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut --fallback off --out-dir .\output\5-dut-normal-only

# 只增加局部救援，保留原布局；失败后不会扩大间距或重新排布。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut --fallback off --local-rescue auto --rescue-seconds 60 --out-dir .\output\5-dut-local-only

# 直接生成大画布后备图；跳过正常优化、局部救援和扩距，也不执行 SA/A*。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut --fallback only --iterations 0 --fallback-seconds 60 --out-dir .\output\5-dut-bus

# 显式允许指定候选失败后恢复；结果可能改变其物理间距或横向排布。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut --candidate-id 2 --fallback auto --out-dir .\output\5-dut-replay
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
可显式使用 `--simplify-seconds auto`：根据这次已合法布线的合并线段、异网比较量、折角、分支、多端网和绕行余量，开跑前一次选定5、10、15、20或30秒。默认仍固定5秒；显式数值仍是固定预算，只有auto受30秒上限约束。小图自然收敛就提前结束，512候选上限保持原样。
它保持器件位置、真实引脚、固定出线段、电源几何和所有间距规则，只尝试有限的正交局部捷径。
每个被接受的方案必须通过完整连接与几何验收，且总线长、折角数和交叉数都不增加；
线长或折角至少一项严格减少。折角不变时，线长必须减少超过浮点比较容差 `EPS`，不接受仅改变坐标而没有实际改善的方案。
最多四轮、512个候选，5秒到期保留最后已完整验收的结果；没有改进时保留原路线。

当前构造版本为 `orthogonal-shortcuts-v8`。v1要求折角严格减少，会漏掉“大拱压成小拱、折角数量相同”的有效缩短；v2补充了等折角缩短线长，v3增加主动重选叶支路接入点，v4增加通用三段式收紧，v5补充保留原折线路径形状的轨道收紧；v6为后续操作预留配额，并优先处理绕行余量大的自由链；v7让轨道内收按自由链轮转，并提前筛掉明确的异网几何冲突；v8为四类候选统一补充真实交叉点的结构净区预筛。
单网快速筛选和整图最终接受仍使用同一项线长/折角改进判据，并保留完整几何检查和交叉数检查。
v1～v7保存的简化记录不会阻止v8重新尝试；同一新版、规则、预算及线路指纹匹配时仍按原方式复验后复用。

四类候选由同一调度器分阶段管理。保留操作的启用顺序，阶段收敛或用完配额后进入下一阶段，避免小折线候选耗尽预算，让外围大圈始终得不到处理：

1. `chain_warmup` 只执行 `chain_shortcut`，累计使用初始复验后剩余时间的25%、最多128个候选。
2. `coordinated_refinement` 先尝试 `leaf_reattachment`，再与 `chain_shortcut` 逐候选轮转，累计截止为50%、256个候选。支线重接只剪下能证明仅含一个真实终端的非固定叶支路，保留固定出线后向同网树生成直线或L形候选。
3. `three_segment_refinement` 先尝试 `three_segment_shortcut`，再与已有两类动作轮转，累计截止为75%、384个候选。
4. `track_compaction_refinement` 先尝试 `track_compaction`，再与已有三类动作轮转，共用剩余时间和最多512个候选。前阶段没用完的时间和名额留给后面。
5. 每次接受后更新当前线路并重建候选，优先给下一类动作机会。相同整图状态下，规范化后重复的候选只检查一次；任何网络修改后重新允许评估。未完整验收或越过阶段截止的试案不会提交，也不会永久缓存为拒绝。
6. 最多四个调度阶段，仍受原5秒与512个候选总上限约束。阶段截止只让出，原始总截止不延长；全局超时保留最后完整合法结果。初始复验已经耗尽预算时，后续阶段可能没有执行机会，报告明确标记。

自由链先按“现有长度减去两端曼哈顿距离”排序，再按路径长度、折角、坐标和网名稳定排序；绕行余量只是优先级，不表示所有多出的长度都能安全删掉。没有按图号、器件名或网名设置的接法。

重接候选在第一次合法接入保留树时结束，不需要接回旧分支点。每个候选暂时冻结其他网络；合并同网线段和裁去无锚悬尾后，仍须检查所有真实引脚、固定出线、电源几何、异网分离及环路。不能证明可安全拆下、会遗漏端子、增加环路或者不能通过完整验收的候选都不提交。候选依据连接拓扑和坐标生成，不按图号、器件编号或网名设置接法。

三段式候选的触发条件是：已有合法结果且进入三段式阶段，找到一段内部没有真实引脚、分支点或固定出线的连续自由链，并且该链至少有两个内部折角。两个端点保持原位，分别尝试“横—竖—横”和“竖—横—竖”，适用于两端网，也适用于多端网中的无分叉线段。这个条件只代表进入尝试队列；不依据图号、网络名称或器件编号，也不把候选当作必改线路。

`route_patterns.py` 从原有轨道、器件及标记halo外侧的网格边界、异网走线与固定出线的间距/交叉净区边界生成轨道，每个坐标轴最多保留12个有希望的候选，去重后构造不超过三段的正交路径。原引脚固定段不移动，也不调用SA或A*。候选仍须保留全部固定段、不得增加同网环路，并通过整图连接和几何验收及同一项指标改进判据；候选全部被挡住、没有实际收益或预算用完，就保留原结果。

`route_tracks.py` 处理另一类情况：原线是四段、五段或更长的自由折线，减少段数暂时行不通，但原来的轨道可以向内移动。它保留链两端，每次平移一个内部水平段或竖直段，同时移动两端拐点并调整相邻段；不移动任何真实引脚、固定出线或内部分支。候选坐标来自器件和电源/地标记保护区外侧、已有线路间距及净区边界、原路径和端点坐标，每条链最多24个候选，优先处理绕行较大的链。折角不增加且线长严格减少才进入后续筛选；最终仍独立检查固定段覆盖、同网环、原始引脚连接、全部几何及整图交叉。图6右侧大圈可以收窄的关键边界来自电源标记保护区，不能仅参考器件本体。生产算法没有图号、网名、器件名或特定坐标分支。

v7的轨道候选按链轮转：每轮每条自由链最多取一个有界候选，遇到明显冲突也立即把机会交给下一条链。对当前不可变走线共用规范化占用与线段查询缓存，通过原 `Scene.edge_check()` 提前排除共轨、净距不足、引脚及固定出线冲突。普通拐角和人工共线切点不套用更强的节点净区，合法十字与同网复用继续保留。预筛通过仍不代表成功；新分支、连接、环路和最终整图指标继续由原完整验收裁决。接受任何改动后重新生成候选与缓存。详见[v7说明和图23对照](track-fairness-v7.md)。

v8在四类候选共用的提交路径补充交叉净区预筛。先合并同网走线、裁剪无锚悬尾，检查固定段、环路与形状改进；再用完整候选和真实引脚提取规范拓扑，与最后已接受整图的异网拓扑检查真实十字交点。交点距双方真实折角、分支或引脚小于原净区时提前拒绝，公式与独立最终验收一致。没有交点的普通10单位平行拐角仍可行，临时出口和人工切点不额外变成保护点。预筛通过仍需原完整验收及整图指标比较，只有真正提交后才更新占用缓存。`candidate_precheck`记录检查次数及部分冲突位置，`crossing_rejected`单独统计早期拒绝，不冒充完成了整图验收。详见[v8说明及顶部绕线实测](candidate-clearance-v8.md)。

完整执行顺序为：正常候选选择 → 若正常阶段全失败则按原顺序局部救援、扩距、总线后备 → 选定并验收整图结果 → 统一安全简化 → JSON和彩色/黑白SVG。总线后备跳过简化。`--simplify-placement` 先重新验收已保存的线路，再进入同一简化器，不运行布局、SA、A*或恢复链。
保存阶段使用简化器返回的最新线路与指标，不再回用简化前的路由结果；JSON和两种SVG共用这一份结果。包含 `routes` 的成功JSON独立重绘时，只复验与绘制保存线路，不再次运行优化。

这一步不运行SA或A*，不改变正常阶段的候选胜者、恢复顺序、路由权重或 `routing_stage`。
总线后备直接跳过；`--simplify-routes off` 或 `--simplify-seconds 0` 也会跳过。
最终 `metrics`、质量评分和两种SVG均使用简化后的线路；原候选评分和原路由搜索诊断作为来源记录保留。

自动预算由 `simplify_budget.py` 估计，不调用SA或A*。特征提取自身最多0.5秒，超时仅分配保守的30秒上限并如实标记特征未完成，不修改几何规则。JSON中的 `budget_seconds`始终是实际传给缩线器的数值；`budget_selection`保存预算模式、估计版本与配置、输入线路指纹、特征和估计耗时。自动保存结果回放时先检查策略和几何指纹，命中后复用历史预算，不对已经缩短的路线重估或继续优化；自动与固定模式也不会混用缓存。旧固定预算JSON继续兼容。详见[自动预算说明与同输入对照](auto-simplify-budget.md)。

```powershell
# 新的完整flow默认包含安全简化。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\4.cir

# 可选自动预算；默认命令仍为固定5秒。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\4.cir --simplify-seconds auto --out-dir .\output\4-auto-budget

# 关闭安全简化，用于复现原路由器的结果。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\4.cir --simplify-routes off --out-dir .\output\4-unsimplified

# 对已保存的合法线路单独简化，不运行布局、SA、A*或任何恢复阶段。
.\.venv\Scripts\python.exe .\run_flow.py --simplify-placement .\output\4\4-placement.json --simplify-seconds 5 --out-dir .\output\4-simplified
.\.venv\Scripts\python.exe .\run_flow.py --simplify-placement .\output\5-dut\5-dut-placement.json --simplify-seconds 5 --out-dir .\output\5-dut-simplified
```

`--simplify-placement JSON` 必须包含完整 `routes`，先按源网表和当前几何规则重新验收，不能依赖旧的 `valid=true`。
它与网表输入、`--reroute-placement` 三选一；默认输出到 `output/<名称>-simplified`，不覆盖输入JSON或网表。
失败时保留已有成功输出。已经安全简化的文件在版本、预算、规则、布局/资源和规范化线路指纹均匹配时，
会在完整输入验收后直接复用保存结果，使同配置重复处理保持稳定；改线路或参数会重新尝试。
`render-clean.py` 独立重绘只验收和绘制保存的最终线路，不再次简化。

`route_simplification` 的 `scheduling` 为 `reserved_stages_v6`，v7沿用四阶段配额，另用 `track_generation` 标明链间轮转与线段预筛策略。`stages` 记录累计截止与候选配额、实际耗时、尝试次数及 `converged/quota_yield/global_timeout/candidate_budget/not_started` 状态。只有自然收敛才写入对应的 `warmup_completed`、`v3_completed`、`v4_completed` 及阶段指标，达到配额不算完成。`operation_order` 列出四类算子，`operations` 保存各算子计数，`accepted_changes` 按提交顺序记录所属阶段和实际指标变化；轨道收紧另存原/新轨道坐标及链长。原 `routing_stage`、候选选择和路由搜索来源记录继续保留。`converged/no_improvement` 只表示本版有限候选没有进一步合格改法，不代表全局最短。[v6说明与当时图23实测](route-scheduling-v6.md)继续保留历史口径。

### v5原轨道收紧实测与复现

本轮完整回归354项全部通过，包含此前343项和新增11项轨道收紧测试。新增测试覆盖四段收紧、旋转/镜像/平移、改名和切段、分支/固定出线保护、必要绕障、合法交叉增加仍拒绝、确定性、截止时间、v4先完成及共享预算。原有三段式测试额外核对 `v4_metrics`，并由完整几何验收确认后续新增收紧没有穿过标记或增加交叉。

使用图1～7及混合器件的8份保存主图，再加入无源灵活布局对照里的图6、图7，共10份合法线路，直接做后处理。没有重新布局或布线；验证时通过patch禁止调用SA、A*及恢复链。所有结果均保持器件位置、朝向、物理轴、固定出线及原始输入不变，最终线长、交叉和折角均不增加，并通过完整验收、双SVG一致、独立重绘及同配置缓存回放检查。对照页为 `output/track-compaction-v5/index.html`，数据为同目录 `comparison.json`。

| 保存输入 | 线长：前→后 | 交叉：前→后 | 折角：前→后 | 简化耗时 | v5轨道收紧 |
| --- | ---: | ---: | ---: | ---: | --- |
| 图6 flexible | 4923→4627 | 2→2 | 30→30 | 约2.067秒 | 1次，`pc`外侧竖轨808→660 |
| 图7 flexible | 3794→3714 | 2→2 | 32→30 | 约2.387秒 | 3次，收紧`a`的内部轨道 |

两份输入的 `v4_metrics` 均严格等于原保存指标，随后才发生新动作。图6的660由电源标记保护区右边界658加2网格步长得到，并非按图号写死。其他主图没有退步；其中图2保存来源为v3，4712→4044的改善发生在原v4阶段，新动作未介入，应与新增收益分开理解。本轮保留了原有交叉不增加条件，因此仍可能存在需要额外合法交叉才能明显缩短的绕线。

```powershell
# 重现这10份保存线路的对照，不执行SA或A*
.\.venv\Scripts\python.exe .\tools\compare_route_tracks.py

# 只对图6现有flexible线路应用正式v5后处理
.\.venv\Scripts\python.exe .\run_flow.py --simplify-placement .\output\passive-flex-comparison\default\6\flexible\9c2c4b068513c363\6-placement.json --out-dir .\output\track-compaction-v5\6-flexible
```

旧版固定布局的历史实测结果如下。每个候选都经过整图连接与几何验收，器件位置、朝向及固定出线保持原样：

| 电路 | 线长：简化前→后 | 折角：简化前→后 | 交叉：简化前→后 | 简化耗时 |
| --- | ---: | ---: | ---: | ---: |
| 图4 | 3160→3060 | 38→19 | 6→6 | 约0.47秒 |
| 图5 | 9188→9130 | 64→47 | 19→19 | 约2.27秒 |

图4中 M4/M5 的共同 G 网 `f3` 已直接连接；它们的 S 极属于不同网络，没有相连。
图5中 XM14.D 保留向上的固定出线，之后用 L 形接入原 `vout` 干线，去除了中间阶梯。
两例均在第二轮确认没有进一步的本地改进，完整验收中的穿体、重叠及间距违规均为0。
这些数值是历史固定输入的实测记录，历史批次目录和对比报告不再保留。
整理后的图4、图5当前结果分别集中在 `output/4/` 和 `output/5-dut/`；这些生成物不随Git提交。
关闭简化时图1～4的线路、位置和指标保持不变；
重复处理与独立重绘也已验证。当前收敛只针对有限的直线、L形、叶支路接点和三段式候选，不代表全局最短线长或最少折角。

图7的近桥探测说明了相同折角时缩短线长的作用：Rz、Cc 相连引脚都向上出线，原 `rz` 网上绕到外围通道，线长为446、折角为2。
保留两个固定出线段，把中间横线移到出线末端，`rz` 线长降到146，折角仍为2；其余网络固定时，整图线长3818→3518、交叉4→4、折角27→27，完整验收通过。
上述3518是一条局部候选的验证结果。v2随后对图7保存线路整体安全简化，历史实测整图线长3818→3482、交叉4→4、折角27→27，耗时约0.29秒。
其中 `rz` 缩短300，`inn` 缩短24，`vbias_p` 缩短12；所有器件位置、朝向、固定出线及原网表保持一致，完整验收和独立重绘均通过。
v3在这个v2结果上继续运行的历史实测为：先确认旧路径简化已经稳定，再重接 `a` 网的长叶支路，整图线长3482→2812、交叉4→3、折角27→24，耗时约0.50秒。
`a` 网线长1226→556，Rz—Cc 的146短桥保持不变。另从v1旧结果完整重放，记录依次为旧路径动作处理 `rz`、`inn`、`vbias_p`，再由支路重接处理 `a`，最终指标相同。
图7主图保存在 `output/7/`，v3历史对照为其 `analysis/simplification-v3-comparison.json`；当前版本的最终指标以输出JSON为准。

图7的 `inp` 两端都水平出线，v3遗留路线在两个出线口各上下折12后才横接竖干线。直接竖连会碰到 `g3` 固定出线；横—竖—横候选则可以避开它。此前只替换这一条线的局部验证中，沿原竖轨可使整图2812→2764、折角24→20、交叉仍为3；更近的合法边界轨道候选得到2716、20折角和3交叉。这些是局部探测值，不代表v4整图优化后的最终指标。

v4对图7保存结果进行完整后处理的实测为：先复现并保留v3阶段结果，再以三段式收紧 `inp`，整图2812→2716；随后三段式收紧 `a` 到2696，再由已有 `chain_shortcut` 继续收紧 `a` 到2676。最终整图线长2812→2676、交叉3→2、折角24→20，耗时0.927秒。`inp` 线长376→280、折角6→2，`a` 线长556→516，之前 `rz` 的146短桥保留。完整验收通过，原网表、器件位置和朝向均不变。

v4最初只将图7主图更新，其余样例当时仅作只读回归。此后又按当前完整flow重跑图4、6、5，最新运行来源和指标见[流程详解的实测记录](flow-walkthrough.md#examples)。`output/route-improvement-1-7/` 的既有批量对比页仍是v3记录，不能当作v4的批量出图结果。

### 其他电路

```powershell
# RC / RLC：先用较小迭代数检查完整链路。
.\.venv\Scripts\python.exe .\run_flow.py .\analog-canvas-main\netlists\native-rc-filters\circuit.spi --iterations 500 --out-dir .\output\rc
.\.venv\Scripts\python.exe .\run_flow.py .\analog-canvas-main\netlists\native-rlc-filter\circuit.spi --iterations 500 --out-dir .\output\rlc

# 网表仅定义子电路时，用 --cell 明确选择。
.\.venv\Scripts\python.exe .\run_flow.py .\analog-canvas-main\fixtures\simulation-acceptance\ota-5t.spi --cell ota_5t --out-dir .\output\ota

# 自包含的 BJT、二极管、E 源结构示例。
.\.venv\Scripts\python.exe .\run_flow.py .\examples\mixed-devices.cir --iterations 500

# 原始自由布局 SA，用于和分层版本比较。
.\.venv\Scripts\python.exe .\run_flow.py .\analog-canvas-main\netlists\native-common-source\circuit.spi --mode baseline --iterations 500 --out-dir .\output\baseline
```

两个模式默认都输出三份文件。未提供 `--out-dir` 时，输出至当前工具目录的
`output/<网表标签>/`；标签为文件名加可选的 `-子电路名`，常见的 `circuit.spi` 用其父目录名区分电路。
默认目录不再附加布局模式；比较 `layered`、`baseline` 或其他参数时请显式使用不同的 `--out-dir`。
同一输出目录中再次运行会更新同名生成物；比较两次实验时请使用不同的输出目录。

## 已有布局只重布线

下面的命令直接复用 JSON 中的每个器件 row、col、orient，跳过 SA，用路由器
重新生成三份结果。新 JSON 中的 `layout_grid.column_x`、`layout_grid.row_y`
也按原值恢复，因此紧凑网格的实际器件坐标保持一致。旧 JSON 无物理坐标表时，
继续按原来的 150 网格恢复。示例输出到独立目录，保留原 JSON 和 SVG：

```powershell
.\.venv\Scripts\python.exe .\run_flow.py --reroute-placement .\output\3\3-placement.json --out-dir .\output\3-rerouted
.\.venv\Scripts\python.exe .\run_flow.py --reroute-placement .\output\2\2-placement.json --out-dir .\output\2-rerouted
```

不指定输出目录时，结果保存在 `output/<原 JSON 名称去掉-placement>-rerouted/`。
网表位置参数与 `--reroute-placement` 互斥；重布线保留原先的分层、结构和优化信息，
用 `rerouted_from` 记录输入 JSON。旧布局存在已保存的路由参数时按相同参数重布线。

`--reroute-placement` 不触发扩距或重新排布，因此不能与 `--fallback auto/only` 组合。
固定布局重布默认不启动局部救援；可以显式开启，救援预算从原 A* 失败后独立开始：

```powershell
.\.venv\Scripts\python.exe .\run_flow.py --reroute-placement .\tests\fixtures\junction-clearance\5-candidate2-placement.json --local-rescue auto --rescue-seconds 60 --out-dir .\output\5-candidate2-rerouted
```

该冻结回归命令输出 `5-candidate2-placement.json`、`5-candidate2-layered.svg` 和 `5-candidate2-clean.svg`，
保持原 JSON 的文件名前缀；源子电路仍为 `dut`。该冻结回归结果的源网表继续指向 `tests/fixtures/junction-clearance/5.cir`。
当前 `output/5-dut/` 已由后续完整重跑更新，真实源为 `examples/5.cir --cell dut`，本次在正常阶段候选0超时后经局部救援完成；不能与冻结候选2的固定布局回归混同。
普通布局沿用 A*；总线布局按保存的 `constructive-bus-v1` 通道重建信号线，保留全部物理坐标、朝向及
`power_connections` 的 anchor 和折线路径，不重新运行 SA/A*，也不重新摆放电源标记：

```powershell
.\.venv\Scripts\python.exe .\run_flow.py --reroute-placement .\output\5-dut-bus\5-dut-placement.json --out-dir .\output\5-dut-bus-rerouted
.\.venv\Scripts\python.exe .\render-clean.py .\output\5-dut-bus\5-dut-placement.json .\output\5-dut-bus\5-dut-redraw.svg
```

旧 JSON 没有显式电源几何时保留原 stub/标记规则；总线 JSON 必须保留支持的 `fallback.version`
和 `power_connections` 列表。缺失、为 null 或路径不合法时会报错，不会悄悄退回旧电源 stub。

## 参数及独立脚本

| 参数 | 含义 | 默认值 |
| --- | --- | --- |
| `--mode` | `layered` 固定层级；`baseline` 自由网格布局 | `layered` |
| `--layout-style` | `structured` 使用结构化候选选择；`legacy` 保留旧分层算法；用于 `layered` 模式 | `structured` |
| `--passive-layout` | 新结构候选中R/C/L/D使用 `flexible` 跨行或 `fixed` 固定行；保存模式不重新摆件 | `flexible` |
| `--symmetry-layout` | `off` 跳过额外器件对称规整和S选优，按原Q选择；显式 `auto` 才规整结构候选2/3并启用对称选优；不要求导线镜像 | `off` |
| `--quality-seconds` | `structured` 每张图的候选优化及最终布线时间预算，必须为正数 | `300` |
| `--candidate-id` | 重放一个结构化候选编号，编号以保存的 JSON 为准 | 自动比较候选 |
| `--fallback` | `auto`：必要时局部救援、扩距及总线恢复；`off`：默认仅正常流程；`only`：直接总线构造 | 网表输入为 `auto`；指定候选为 `off` |
| `--local-rescue` | `auto`：正常阶段全部失败后尝试局部救援；`off`：禁用；显式开启可与 `--fallback off` 配合 | 跟随 `--fallback`；固定布局重布为 `off` |
| `--rescue-seconds` | 独立局部构造、搜索和完整验收预算；0表示跳过 | `60` |
| `--simplify-routes` | `safe`：在最终结果选定后安全简化；`off`：保留原路由线路 | `safe` |
| `--simplify-seconds` | 固定秒数或`auto`（按线路复杂度分配5～30秒）；0表示跳过，仍独立于SA/A*及恢复预算 | `5` |
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
.\.venv\Scripts\python.exe .\render-clean.py .\output\3\3-placement.json .\output\3\3-redraw.svg
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
候选布局冻结后才执行最终正交图 A*；默认结构化流程最多真实布线候选1、2、3，显式候选0回放只布线旧参考。
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

最近一次完整回归为313项，耗时约128秒，包括通用三段式候选、叶支路重接、先保留v3再协同优化的执行顺序、四方向/镜像/平移/改名、交叉与障碍保护、旧版缓存升级、默认输出目录、局部救援、接口和恢复选择回归。另有9套保存布局的固定位置v3/v4对照，均通过完整验收，确认旧v3阶段指标完全一致、最终线长/折角/交叉不增加且重复处理稳定。验收还包括独立预算、电源有序路径和真实锚点、
power 穿体/marker 审计、禁止后备调用 SA/A*、JSON 保存恢复及确定性。正常流程另有 RC、RLC、共源级、
指定 OTA 子电路及混合器件样例；可用前面的命令重新生成结果，不依赖历史运行目录。

图5冻结候选2的历史局部救援实测（安全简化前）从已复验的失败检查点开始，不重新运行 SA：约11.6秒完成整图验收，
信号线长9188、交叉19、折角64，穿体、重叠和间距违规均为0。器件逻辑位置、朝向和物理坐标不变，
原来已完成的5个网络保持原线路，本例只需初始续布，没有拆网。
历史结果及对照报告不再作为目录资料保留；固定候选网表及布局保存在 `tests/fixtures/junction-clearance/`。
上面的固定布局重布命令可重新生成，
其总耗时还包含原A*失败前的搜索，60秒仅指失败后新增的救援阶段。
复现该阶段的原始路线时添加 `--simplify-routes off`；当前默认还会执行后续安全简化。

`validation.valid` 证明几何硬约束通过，并不表示图形达到全部美观目标。评估新旧
布局应同时比较最终线长、交叉、折角、结构跨度/对齐和逐网绕行，并查看 SVG。
四图总线长降低至少 20%、总交叉不增加和逐图综合评分不劣是验收目标；实际是否
达到应以对应运行生成的对比数据为准，不能用 SA 的 `energy` 或运行成功代替。
上述美观指标针对布局优化；局部救援首先恢复完整合法连接，不保证得到最短线路或最少交叉。

当前 `output/6/` 如保留图6结果，其源为同目录 `input/6.cir` 的归一化副本，旁边附有归一化说明。
用户的 `examples/6.cir` 原始内容仍有未配对的 `.ends`，工具直接读取时会报错；没有把旧图冒充该原始输入的新运行结果。
