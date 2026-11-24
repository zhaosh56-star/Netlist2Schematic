# SA Python 运行说明

**当前默认（2026-11-24）：候选2、3各自按120秒正常运行＋30秒原候选修复＋30秒局部救援＋30秒1.5倍扩距重试依次执行，再按折角、交叉、信号线长比较合法结果。** 某候选成功就跳过自身后续恢复，但仍尝试另一个主候选；两者都失败才给候选1相同预算，最后才bus（最多60秒）。整图保护上限默认720秒，阶段预算不再按整图时间比例缩放。 见[候选独立预算](docs/candidate-pipeline.md)；下文历史实验时间保留当时口径。

主列与区域前置组织已默认启用（`--branch-layout auto`）：候选2、3先按连接关系组织导通列和局部区域，再由SA在框架内调整；没有器件数量触发门槛，保留现有无源模块与布线插件。用 `--branch-layout off` 关闭；旧JSON、baseline／legacy、显式关闭主列或其他布局实验不隐式叠加。规则及46张实测见[说明](docs/branch-organization.md)。

默认组织路径已补齐插入前骨架碰撞修复、插入失败后有限补试，以及整图正常阶段失败后的相关模块换通道。保持MOS逻辑行列与朝向，原有成功路径不进入失败恢复；总候选、预算和几何规则保持。C23已在正常阶段生成完整合法图，2497项完整回归通过。原理、额度、保存兼容与实测见[模块恢复说明](docs/branch-organization.md)。

单文件盲测已封装为根目录 `build_evaluation.py`：用 `--manifest` 提供任意图对清单或已有正式图集，即可生成问卷，不重跑布局和布线。不内置题目排除或 VDD 换图规则，直接采用你提供的图片。只发一个 `index.html`，客户答完只返回一个含 GT／算法占比和逐题细节的 HTML 报告。清单格式和命令见[评测脚本说明](docs/blind-evaluation.md)。

正式出图插件默认接入主 flow，生成 GT 风格字体、数学下标、无引脚白圈和结构虚框、局部合并 VDD 的正式 SVG，顶层文件名为 `*-formal.svg`。用 `--formal-output off` 关闭；`--formal-power-rails off` 可单独禁用合并。已有 JSON 可跳过布局与布线独立调用，见[正式出图说明](docs/formal-figures.md)。

可选的局部共享VDD插件：`--power-rails auto`，默认关闭。原成品保存后，额外尝试把同网、同排、通道空闲的供电端连接为共享横线和一个VDD标记；器件及信号线路不动。独立处理旧JSON、适用边界和对照见 [共享VDD说明](docs/power-rails.md)。

详细说明已集中到 [docs/flow-guide.md](docs/flow-guide.md)，包括完整流程、分层与结构化布局、A*和失败恢复、最终路线简化、支持的网表、几何规则及全部参数。

已登记、等待后续实施的MOS朝向优化和差分对显示行修复，见[待修改清单](docs/pending-improvements.md)。清单中的新增策略尚未实现，默认flow保持原样。

日常运行与安装命令见 [README.md](README.md)。各脚本和资源的用途见 [docs/project-layout.md](docs/project-layout.md)。

终端独立运行和整批出图见[批量运行说明](docs/batch-flow.md)。批量默认使用相同独立候选预算，整图保护上限720秒，自动正式出图；显式--budget quick仍提供短时整图验证。

当前实现位于 `netlist2schematic/`：`flow/`、`common/`、`netlist/`、`recognition/`、`layout/`、`routing/`、`recovery/`、`postprocess/`、`rendering/` 按主流程职责分类；`plugins/` 保存可选增强及共享关系。根目录保留六个兼容命令入口，旧文件名对应的新路径见 [source_map.json](netlist2schematic/source_map.json)。本地交付包收在 `dist/`，手工笔记收在 `notes/`，日常命令和实验默认值保持原样。

跨R/L/C且端点带G/B抽头的有源支路，默认无源骨架现能区分“导通宿主”和“控制扇出”，复用原主列及模块插入安排上下关系。C和含C串链仅建立显示关系，不改电气层级；单端附件先沿原流程插入，再使用剩余预算作二维找位，旧JSON不自动重摆。通用条件、保护顺序及实测见[跨无源支路说明](docs/inline-passive-branches.md)。

独立电压源V、电流源I也已接入默认两端模块插入：按唯一导通宿主、共同S/E节点或控制端串链安排，保留正负端、电流箭头和全部原节点；归属不明确的源沿用原路径。规则、代码、实测收益及限制见[源器件模块布局](docs/source-module-layout.md)。

端部RLC及独立V/I模块现在默认支持局部二维找位：在完整静态合法的原插入上，用剩余的1秒/128次局部预算尝试靠近真实接入端口的空位；整个串并联模块平移，保持有源框架、物理轴、符号朝向与原几何规则。中间插入模块及成对负载族保留原联合计划。代码、顺序、预算与C19/C31实测见[端部二维找位](docs/passive-modules.md#端部模块二维找位)。

默认骨架候选2/3现也在SA前规划对应支路的显示行：保留主列，局部短列整体移行。多输出电流镜的目标行不一致时，比较原行与各合法目标行的快速估价；共G本身不能强制同行。RCL与V/I模块继续按原流程插入；估价选择的收益与局限见[对应支路齐层](docs/corresponding-branch-rows.md)。

```powershell
Set-Location D:\gongzuo
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut
```

默认结果分别位于 `output/3/` 和 `output/5-dut/`。历史出图已不作为项目目录的一部分维护；固定回归输入及布局保留在 `tests/fixtures/`。

新网表的结构化主flow默认开启端口冲突预检修复（`--port-preflight repair`）：最终A*前检查固定出线、器件和电源标记，场景已合法时保留原位置与朝向；存在冲突时限量尝试无源朝向／邻格修复，以及有明确自身电源引线冲突的V/I原地旋转。需要旧行为时加 `--port-preflight off`。已有JSON的重布、单独缩线和独立重绘保持保存坐标，不自动修复；附件、区域、结构、对称等实验开关仍默认关闭。

信号线现已取消对供电标记矩形保护区的避让：A*、救援、缩线、预检和保存后复验统一允许经过该区域。真实电源引线、异网端子、器件本体，以及供电标记与器件重叠的检查继续执行。此规则默认生效，无需新增开关；旧输出文件不会自动更新。

新网表的结构化主flow也默认开启长列／短旁支规整（`--column-layout auto`）：候选1保留参照，候选2/3在布线前尝试连续长列和相邻短支路，再执行原串接对齐；两步共用局部预算，不增加A*次数。可用 `--column-layout off` 做对照。已有JSON、baseline／legacy及其他显式布局实验不隐式重摆或叠加；依据、取舍和21图实测见[结构布局结果](docs/structure-layout-results.md)。

安全缩线默认仍为固定5秒。需要按首次合法线路的复杂度自动分配5～30秒时，可加 `--simplify-seconds auto`；估计后只执行一次缩线，候选上限仍是512。使用方式和实测见[自动缩线预算说明](docs/auto-simplify-budget.md)。

新增的可读性实验保持关闭：`--readability-layout auto` 尝试无源成组、源器件归位和失效间距回收，`--route-tradeoff balanced` 允许用少量稀疏交叉换取明显缩短。两者独立启用，默认仍为 `off` 和 `strict`；实现边界、分阶段实测及未达标项见[可读性改进说明](docs/readability-improvement.md)，本地对照入口为 `output/readability-improvement/index.html`。

附件联合布局使用独立开关 `--attachment-layout off|passive|all`，默认 `off`。`passive` 为有明确端口关系的无源网络预留内部位置，`all` 再尝试源器件归位；候选1保持原参照，不与其他布局实验同时开启。它与默认 flexible 的区别是可以在初排时让宿主支路一起让位，并保护附件后续的相对位置。用法、验收及两阶段的实际结果见[附件联合布局说明](docs/attachment-integration.md)。

另有保守的无源整理 `--passive-embedding auto`，默认关闭：在原成品完成后，用独立5秒尝试一次无源局部重排和重布。MOS/BJT及其他非无源位置、朝向保持；只有线长、交叉、折角均不增加且附件组织改善才采用，否则恢复原图。它兼容默认长列策略，不需要开启旧附件实验；用法及边界见[说明](docs/attachment-integration.md)，本地最终对照为 `output/passive-embedding-goal/safe/index.html`。

普通新网表默认开启有源骨架＋无源模块的候选2、3，各自执行120＋30＋30＋30秒流程。两份都失败才尝试候选1，最后bus。合法候选共用5秒、512案安全缩线后，按 **折角少 → 交叉少 → 信号线长短** 选择。显式关闭passive-skeleton保留旧候选1→2→3路径；保存坐标操作不隐式重摆。见[独立候选调度](docs/candidate-pipeline.md)。
