# SA Python 运行说明

详细说明已集中到 [docs/flow-guide.md](docs/flow-guide.md)，包括完整流程、分层与结构化布局、A*和失败恢复、最终路线简化、支持的网表、几何规则及全部参数。

日常运行与安装命令见 [README.md](README.md)。各脚本和资源的用途见 [docs/project-layout.md](docs/project-layout.md)。

当前实现位于 `netlist2schematic/`：`flow/`、`common/`、`netlist/`、`recognition/`、`layout/`、`routing/`、`recovery/`、`postprocess/`、`rendering/` 按主流程职责分类；`plugins/` 保存可选增强及共享关系。根目录保留六个兼容命令入口，旧文件名对应的新路径见 [source_map.json](netlist2schematic/source_map.json)。本地交付包收在 `dist/`，手工笔记收在 `notes/`，日常命令和实验默认值保持原样。

```powershell
Set-Location D:\gongzuo
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut
```

默认结果分别位于 `output/3/` 和 `output/5-dut/`。历史出图已不作为项目目录的一部分维护；固定回归输入及布局保留在 `tests/fixtures/`。

新网表的结构化主flow默认开启端口冲突预检修复（`--port-preflight repair`）：最终A*前检查固定出线、器件和电源标记，场景已合法时保留原位置与朝向；存在冲突时限量尝试无源朝向／邻格修复，以及有明确自身电源引线冲突的V/I原地旋转。需要旧行为时加 `--port-preflight off`。已有JSON的重布、单独缩线和独立重绘保持保存坐标，不自动修复；附件、区域、结构、对称等实验开关仍默认关闭。

信号线现已取消对供电标记矩形保护区的避让：A*、救援、缩线、预检和保存后复验统一允许经过该区域。真实电源引线、异网端子、器件本体，以及供电标记与器件重叠的检查继续执行。此规则默认生效，无需新增开关；旧输出文件不会自动更新。

默认失败恢复已取消总线重排：当次局部救援、扩距仍失败后，保留当前器件行列、朝向及合法局部连接，用同名网标接通剩余局部组。允许多端网有超过四组，逐组验收原始端子；必要时只扩大物理间距。算法不读取历史布局或GT坐标。新结果记录 `routing_stage: labeled_recovery`、`route_representation: wires_and_labels`；独立重绘会重新验收。`--label-seconds 60` 控制此阶段；旧 `--fallback only` 不再生成新图。详见[恢复链说明](docs/flow-guide.md#失败后的局部救援与恢复链)。

新网表的结构化主flow也默认开启长列／短旁支规整（`--column-layout auto`）：候选1保留参照，候选2/3在布线前尝试连续长列和相邻短支路，再执行原串接对齐；两步共用局部预算，不增加A*次数。可用 `--column-layout off` 做对照。已有JSON、baseline／legacy及其他显式布局实验不隐式重摆或叠加；依据、取舍和21图实测见[结构布局结果](docs/structure-layout-results.md)。

安全缩线默认仍为固定5秒。需要按首次合法线路的复杂度自动分配5～30秒时，可加 `--simplify-seconds auto`；估计后只执行一次缩线，候选上限仍是512。使用方式和实测见[自动缩线预算说明](docs/auto-simplify-budget.md)。

新增的可读性实验保持关闭：`--readability-layout auto` 尝试无源成组、源器件归位和失效间距回收，`--route-tradeoff balanced` 允许用少量稀疏交叉换取明显缩短。两者独立启用，默认仍为 `off` 和 `strict`；实现边界、分阶段实测及未达标项见[可读性改进说明](docs/readability-improvement.md)，本地对照入口为 `output/readability-improvement/index.html`。

附件联合布局使用独立开关 `--attachment-layout off|passive|all`，默认 `off`。`passive` 为有明确端口关系的无源网络预留内部位置，`all` 再尝试源器件归位；候选1保持原参照，不与其他布局实验同时开启。它与默认 flexible 的区别是可以在初排时让宿主支路一起让位，并保护附件后续的相对位置。用法、验收及两阶段的实际结果见[附件联合布局说明](docs/attachment-integration.md)。

另有保守的无源整理 `--passive-embedding auto`，默认关闭：在原成品完成后，用独立5秒尝试一次无源局部重排和重布。MOS/BJT及其他非无源位置、朝向保持；只有线长、交叉、折角均不增加且附件组织改善才采用，否则恢复原图。它兼容默认长列策略，不需要开启旧附件实验；用法及边界见[说明](docs/attachment-integration.md)，本地最终对照为 `output/passive-embedding-goal/safe/index.html`。

普通新网表默认开启 **`--passive-skeleton auto`**：先跑候选2、3，完成有源主列后插入串联、并联及嵌套串并联小模块。两份都失败才运行原候选1兜底；成功候选共用原5秒、512案安全缩线后选图，优先主列完整与附件组织，折角作为软代价。用 `--passive-skeleton off` 恢复原候选1→2→3。保存坐标操作和其他显式布局实验不隐式叠加。具体预算、选图和复现见[候选优先策略](docs/preferred-candidates.md)与[无源模块插入](docs/passive-modules.md)。
