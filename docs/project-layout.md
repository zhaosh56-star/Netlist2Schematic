# 项目目录与文件用途

日常只需要修改输入网表并运行根目录的 `run_flow.py`。源码留在根目录，按下面的职责组理解；保留这些文件可以继续使用完整 flow、旧布局比较和各脚本的独立入口。

逐步解释每个模块如何协作、每一步的算法与运行方式，见[当前flow逐步详解](flow-walkthrough.md)。

```text
Netlist2Schematic/
├─ run_flow.py                  # 统一入口
├─ *.py                         # 解析、分层、布局、布线、恢复、简化和渲染模块
├─ symbol-geometry.json         # 符号几何缓存，与符号定义共同使用
├─ requirements.txt             # 固定 Python 依赖
├─ setup_python.ps1             # 重建本地 Python 与虚拟环境
├─ README.md                    # 日常运行
├─ README-SA.md                 # 原说明入口，链接到 docs/
├─ docs/
│  ├─ flow-guide.md             # 完整算法、参数及运行说明
│  ├─ flow-walkthrough.md       # 从输入到出图的逐步讲解、代码与实测
│  ├─ structure-gallery.md      # 识别结构图鉴的类别、示例及复现说明
│  ├─ symmetry-layout.md        # 器件对称规整、选优边界与对照命令
│  └─ project-layout.md         # 本文
├─ examples/                    # 用户输入网表；不自动改写
├─ output/                      # 唯一的日常输出根目录，不进入 Git
│  ├─ 3/                        # examples/3.cir 的默认结果
│  └─ 5-dut/                    # examples/5.cir --cell dut 的默认结果
├─ tests/
│  ├─ test_*.py                 # 开发回归测试
│  └─ fixtures/                # 冻结网表、布局及基线；不能当作历史出图删除
├─ tools/
│  ├─ compare_layouts.py        # 验收已保存路线并生成新旧对照
│  ├─ compare_symmetry_layouts.py # 数据集及图1～7的器件对称开关对照
│  ├─ generate_structure_gallery.py # 生成已识别子结构的教学图鉴
│  └─ structure_gallery_cases.py    # 图鉴的最小示例及展示坐标
├─ analog-canvas-main/          # 上游符号、少量测试网表和许可证
├─ .venv/                       # 当前虚拟环境，不进入 Git
├─ .python/                     # 本地 Python 发行包，不进入 Git
└─ .cache/                      # 本地工具缓存，不进入 Git
```

## 完整 flow 的运行代码

| 职责 | 文件 | 用途 |
| --- | --- | --- |
| 统一入口 | `run_flow.py` | 组织所有阶段，写出 JSON 和两种 SVG |
| 网表解析 | `parse-netlist.py`、`netlist_core.py` | 统一器件、引脚、型号、本地模型和指定子电路 |
| 电流分层 | `device_leveling.py` | 通过 MOS/BJT 导通图计算层级，并为其他器件安排挂位依据 |
| 结构识别 | `circuit_structures.py`、`vf2_match.py` | 识别结构及角色；VF2 用于旧布局参考 |
| 结构化布局 | `structured_placement.py`、`aesthetic_flow.py`、`layout_quality.py` | 初排、SA、候选选择、时间预算和实际布线评分 |
| 无源灵活布局 | `passive_placement.py` | 新结构候选中的R/C/L/D跨行移动、无源交换、状态回滚及串联/并联/反馈软评分 |
| 器件对称规整 | `symmetry_layout.py` | SA及反馈后、A*前的局部/组合对称规整与测量；不要求导线镜像 |
| 基础布局与估价 | `place-layered.py`、`place-and-route.py` | 分层和自由布局参考、快速布线估价及符号位置 |
| 最终路由与验收 | `sa_routing.py`、`orthogonal_router.py`、`routing_rules.py` | 正交 A*、拆网重布、统一几何与真实引脚检查 |
| 失败恢复 | `local_rescue.py`、`spacing_retry.py`、`fallback_bus.py` | 局部救援、扩距重试、确定性总线后备 |
| 最终安全简化 | `simplify_routes.py`、`route_reattachment.py`、`route_patterns.py`、`route_tracks.py` | 按阶段调度路径缩短、叶支路重接、三段式和原轨道收紧；不运行SA/A*，线长/折角/交叉均不增加 |
| 图形输出 | `sa_render.py` | 从同一份已验收线路生成彩色与黑白 SVG |
| 可选网标展示 | `net_labels.py`、`net_label_core.py`、`net_label_validation.py` | 默认关闭；完整结果保存后独立生成、验收和重绘局部连线加同名网标的额外视图 |
| 共同支持 | `sa_common.py`、`sa_geometry.py`、`sa_saved.py` | 路径、动态模块加载、几何缓存、JSON和固定结果恢复 |
| 独立重绘 | `render-clean.py` | 读取并复验保存的路线，单独生成黑白 SVG |

这些模块包含默认流程、必要时的恢复路径及显式开启的可选视图；`render-clean.py` 是额外的独立重绘工具。一次成功运行不一定执行所有阶段，但这些文件都有用途，不能仅凭当次未调用就删除。

根目录保留带连字符的旧脚本名，是为了保留已有命令和动态加载路径。未来若要改成 Python 包，应一起修改导入、独立入口、资源路径和测试，避免只移动文件导致 flow 失效。

## 资源与运行环境

`symbol-geometry.json` 和 `analog-canvas-main/packages/components/definitions/` 共同决定器件图形、引脚方向、旋转镜像及障碍包围盒。不能删除其中一份，或者用不匹配的符号定义替换。上游完整网页工程不参与 Python flow；仓库只跟踪需要的符号、四份测试网表和许可证。本地若保留完整上游检出，可继续作为只读参考。

当前 `.venv/` 可能引用 `.python/distribution/` 内的解释器。使用这一环境时，两者都应保留。它们不进入 Git；在新机器或移动项目后，按 [运行说明](flow-guide.md#环境重建与验证) 重建环境。`.cache/` 不是算法依赖，但可能含本地 Git 等工具，清理前应确认工具可从其他位置运行。

`examples/` 是用户维护的输入，包含其本地 include 文件时也必须保留。工具不会为方便布局增删电压源或其他器件，也不会自动改写输入。

## 输入、输出和开发资料

日常运行的生成物统一写在项目根目录 `output/<网表标签>/`。普通标签为文件名加可选的 `-子电路名`，如 `3`、`5-dut`；名为 `circuit.spi` 的输入用父目录名区分。该目录不附加 `layered` 或 `baseline`，所以比较模式时需显式设置不同的 `--out-dir`。

每个成功目录主要保留 `<名称>-placement.json`、`<名称>-layered.svg`（自由布局时为 `-baseline.svg`）和 `<名称>-clean.svg`。失败时可写 `routing-diagnostics.json`，不会把冲突路线标记成功，也不会覆盖已有成功图。`--reroute-placement` 默认使用 `output/<名称>-rerouted/`，`--simplify-placement` 默认使用 `output/<名称>-simplified/`，保护原始保存结果。

运行顺序是先选定正常阶段或恢复阶段的完整合法结果，再进入统一安全简化，最后写JSON与双SVG。`simplify_routes.py` 负责预算、阶段调度、验收和提交当前最佳结果：先用 `chain_warmup` 完成原有路径收紧，再以 `coordinated_refinement` 协同执行叶支路重接和路径缩短；已有v3动作稳定后，才进入 `three_segment_refinement`。新阶段逐候选轮流尝试三段式及已有两类动作，每次接受后都从最新线路重新提取拓扑，避免回写旧结果覆盖前一步收益。`route_reattachment.py` 只按拓扑生成重接候选；`route_patterns.py` 只为至少两个内部折角的自由链生成有限的横—竖—横或竖—横—竖候选，两者都不能自行提交线路。全部阶段共用原5秒、四轮、512个候选预算；前阶段耗尽预算时保留已验收结果并记录未完成，不强行放行新步骤。正常A*、局部救援、扩距和后备顺序不因此改变。当前含有 `routes` 的成功JSON独立重绘时只复验和绘制保存线路，不重跑这些优化。

v5在以上v4动作全部稳定后增加 `track_compaction_refinement`。`route_tracks.py` 保留自由链两端，单次平移内部轨道及两个相邻拐点，并保持正交和原段次序；它与三类旧动作轮流生成候选，提交仍完全由 `simplify_routes.py` 控制。`v4_completed/v4_metrics` 保存进入新阶段前的已验收成果，阶段不重置原有预算。`tools/compare_route_tracks.py` 用保存的合法线路比较v4/v5，不运行SA或A*，输出位于独立的 `output/track-compaction-v5/`。

`tests/fixtures/` 中的布局 JSON 和冻结网表是有意保留的测试输入，包括图1～4的旧布局基线、图2/3固定布局及图5候选2失败场景。删除历史 `output/` 不影响这些回归输入。`tests/` 和 `tools/` 用于开发验证，不在日常输入目录中混放测试电路或对比报告。

历史批次目录和调查报告都属于可重新生成的运行产物，不作为源码资料维护。实测指标保留在[运行说明](flow-guide.md)，复现使用 `examples/` 或 `tests/fixtures/`，不依赖历史出图。

当前 `output/5-dut/` 已从 `examples/5.cir --cell dut`完整重跑，由候选0经局部救援及v4简化出图；此前冻结候选2结果仍作为历史对照解读。`output/6/` 来自归一化输入副本，副本和说明保留在该目录；`examples/6.cir` 原始内容仍有未配对的 `.ends`，不能把图当作该原始输入直接运行的结果。各结果来源以本地 `output/README.md` 和各 JSON 为准。

v4最初更新 `output/7/` 主图，原网表和器件位置未改；线长2812→2676、交叉3→2、折角24→20，之前 `rz` 的146短桥继续保留。随后又完整重跑了图4、6、5，详见[实测记录](flow-walkthrough.md#examples)。`output/route-improvement-1-7/` 的既有批量对比页仍记录v3。v4已通过313项测试及9套保存布局的固定位置对照，后者旧v3阶段指标一致，最终各项不增加并且重复稳定。

## 常用命令

```powershell
Set-Location D:\gongzuo

# 日常生成
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut

# 回归测试
.\.venv\Scripts\python.exe -B -m unittest discover -s .\tests -q

# 已生成图1～4后，与 tests/fixtures/aesthetic-baseline/ 对比
.\.venv\Scripts\python.exe .\tools\compare_layouts.py
```

对比工具默认读取 `output/1/` 至 `output/4/`，把 `comparison.json`、`comparison.html` 和必要 SVG 放在 `output/` 内。它只复验已保存线路，不会自动运行布局或布线。

## 可选结构布局文件

新增源码按职责独立保存：`layout_relations.py`负责布局关系，`branch_levels.py`负责显示行计划，`attachment_placement.py`负责无源附件，`branch_alignment.py`负责真实引脚与dx，`structure_selection.py`负责候选选择。展示、预算与静态端口预检分别在`presentation.py`、`candidate_budget.py`、`placement_preflight.py`，主流程仍由`run_flow.py`协调。

对照入口为`tools/compare_structure_layouts.py`，生成物集中在`output/structure-layout-comparison/`。其中stage1／stage2保留阶段验证，first-pass保存64次完整对照，parallel-refinement保存后续RLC专项。原网表、GT、符号及已有主图不移动。当前651项回归全部通过，结构策略仍默认off；结果和限制见[实测说明](structure-layout-results.md)。
