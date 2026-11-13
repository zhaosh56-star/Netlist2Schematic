# 项目目录与文件用途

日常仍从项目根目录运行 `run_flow.py`。实际 Python 实现已经按职责放入 `netlist2schematic/`：主流程代码和可选插件分开，输入网表、出图、测试及独立 RSI 项目各有自己的目录。

根目录的六个 Python 脚本是兼容入口；读算法时应打开包内实现。旧文件名到新路径的完整对应表见 [source_map.json](../netlist2schematic/source_map.json)。本次分类不改变各实验开关的默认值。

```text
Netlist2Schematic/
├─ run_flow.py                  # 日常统一入口
├─ net_labels.py                # 独立生成或重绘网标视图的入口
├─ parse-netlist.py             # 只解析网表的兼容入口
├─ place-and-route.py           # baseline 布局兼容入口
├─ place-layered.py             # 分层布局兼容入口
├─ render-clean.py              # 已保存结果的独立重绘入口
├─ netlist2schematic/           # 主项目 Python 实现
│  ├─ flow/                    # 阶段调度、候选比较、时间预算
│  ├─ common/                  # 项目路径、符号几何、保存与恢复
│  ├─ netlist/                 # 网表解析和规范器件数据
│  ├─ recognition/             # 导通分层、结构与角色识别
│  ├─ layout/                  # 初排、SA、flexible 无源动作、质量测量
│  ├─ routing/                 # 正交 A*、场景、统一几何验收
│  ├─ recovery/                # 局部救援、扩距、确定性总线后备
│  ├─ postprocess/             # 安全缩线、候选生成、缩线预算与交叉取舍
│  ├─ rendering/               # SVG、文字及成品展示
│  ├─ plugins/                 # 可选增强及它们共用的布局关系
│  │  ├─ layout_shared/        # 跨插件共用的证据、行计划、附件及对齐
│  │  ├─ structure/            # 结构组织与选优
│  │  ├─ region/               # 功能区域初排及区域动作
│  │  ├─ readability/          # 无源、源器件、空隙及绕线风险实验
│  │  ├─ symmetry/             # 器件位置和朝向对称实验
│  │  ├─ attachment/           # 无源与源器件联合布局实验
│  │  ├─ net_labels/           # 局部实体连线＋同名网标的额外视图
│  │  └─ preflight/            # 最终 A* 前的静态端口修复实验
│  └─ source_map.json          # 旧文件名 → 当前实现路径
├─ symbol-geometry.json        # 符号几何缓存，仍保留在根目录
├─ analog-canvas-main/         # 上游符号定义、验收网表及许可证
├─ examples/                   # 日常输入网表
├─ test_data/                  # 本地数据集和 GT，不进入 Git
├─ output/                     # 主流程出图、诊断及对照报告，不进入 Git
├─ tests/                      # 主项目回归测试和冻结输入 fixtures/
├─ tools/                      # 对照、图鉴、调查和开发工具
├─ docs/                       # 算法、参数、实测及使用说明
├─ agent-rsi-demo/             # 独立 Agent 电路实验
├─ shinka-circuit-rsi/          # 独立 Shinka 电路实验
├─ ShinkaEvolve/               # 本地上游框架检出，不进入 Git
├─ dist/                       # 本地 ZIP、tar.gz 交付包，不是运行源码
├─ notes/                      # 手工笔记及未命名文本
├─ archive/                    # 本地历史材料，如原始 TXT，不是运行依赖
├─ requirements.txt            # 主项目依赖
├─ setup_python.ps1            # 本地 Python 和虚拟环境配置
├─ README.md / README-SA.md    # 日常使用与详细说明入口
├─ .venv/ / .python/           # 本地运行环境，不进入 Git
└─ .cache/                     # 本地工具缓存，不进入 Git
```

## 一次默认 flow 会用到哪里

| 环节 | 实现目录与主要模块 | 作用 |
|---|---|---|
| 总调度 | `flow/run_flow.py`、`flow/aesthetic_flow.py`、`flow/candidate_budget.py` | 读取参数、安排候选及预算、组织恢复和输出 |
| 解析 | `netlist/netlist_core.py`、`netlist/parse_netlist.py` | 读取扁平电路或显式指定的 cell，规范器件与引脚连接 |
| 分层与识别 | `recognition/device_leveling.py`、`circuit_structures.py`、`vf2_match.py` | 计算导通层级，识别有角色的子结构；VF2 保留给旧参考路径 |
| 布局 | `layout/structured_placement.py`、`passive_placement.py`、`layout_quality.py` | 结构初排、SA、无源跨行和真实布线质量测量 |
| 基础布局能力 | `layout/place_layered.py`、`layout/place_and_route.py` | Placement、真实引脚坐标、快速估价和旧布局参考；结构化流程也会复用 |
| 最终布线 | `routing/sa_routing.py`、`orthogonal_router.py`、`routing_rules.py` | 正交 A*、拆网重布、真实引脚连通和几何验收 |
| 失败恢复 | `recovery/local_rescue.py`、`spacing_retry.py`、`fallback_bus.py` | 正常阶段失败后按既定顺序恢复 |
| 安全缩线 | `postprocess/simplify_routes.py`、`route_reattachment.py`、`route_patterns.py`、`route_tracks.py` | 在已合法线路上生成、验收及提交更短的候选 |
| 缩线策略与预算 | `postprocess/simplify_budget.py`、`route_tradeoff.py` | 固定或自动预算，以及 strict／balanced 的接受规则 |
| 绘图 | `rendering/sa_render.py`、`presentation.py` | 同一份验收线路生成彩色和黑白 SVG，安排文字和端口展示 |
| 共同支持 | `common/sa_common.py`、`sa_geometry.py`、`sa_saved.py` | 定位资源、处理朝向几何、保存和恢复布局 |

表中的模块路径均相对 `netlist2schematic/`，同一行省略的重复目录前缀与该行首项相同。恢复和安全缩线属于默认主流程的组成部分；一次运行是否实际执行恢复，取决于前面的布线是否成功。将它们单独放目录只是为了便于维护。

`layout/passive_placement.py` 对应默认的 `--passive-layout flexible`。它与 `plugins/attachment/` 的可选联合布局是两项功能：前者提供无源跨行自由，后者尝试提前为附件腾出内部位置。

## 可选插件放在哪里

| 插件目录 | 对应入口／用途 |
|---|---|
| `plugins/structure/` | `--structure-layout auto`，按布局关系比较结构组织 |
| `plugins/region/` | `--region-layout auto`，按区域、子块和支路组织布局 |
| `plugins/readability/` | `--readability-layout auto`，无源排列、源归位、空隙及布线反馈实验 |
| `plugins/symmetry/` | `--symmetry-layout auto`，器件位置和朝向对称，不要求导线镜像 |
| `plugins/attachment/` | `--attachment-layout passive` 或 `all`，联合安排宿主支路、无源及源器件 |
| `plugins/net_labels/` | `--net-labels auto` 或根 `net_labels.py`，生成独立网标视图 |
| `plugins/preflight/` | `--port-preflight repair`，最终路由前修复可调整的静态冲突 |

`plugins/layout_shared/` 放多种布局实验共用的 `layout_evidence.py`、`layout_intent.py`、`layout_relations.py`、`branch_levels.py`、`branch_alignment.py` 和 `attachment_placement.py`。旧的 `attachment_placement.py` 是共享附件能力；新的联合布局实现是 `plugins/attachment/attachment_integration.py`。

插件目录表示可以按参数选择的功能模块，不是另一个需要安装的外部插件系统。具体默认值、互斥要求及验收范围仍以各功能说明和 CLI 为准；移动文件不会自动开启实验。

## 输入、输出、测试与资料

- `examples/` 保存日常输入，连同其本地 include 文件一起维护。工具不为布局方便增删器件或改写网表。
- `test_data/` 保存本地数据集与 GT，供对照研究使用。算法不会读取 GT 坐标来指定摆法。
- `output/<网表标签>/` 保存日常 JSON、彩色 SVG、黑白 SVG及失败诊断。普通标签为文件名加可选的子电路名，如 `3`、`5-dut`；模式比较需使用不同的 `--out-dir`。
- `tests/fixtures/` 是冻结的回归输入，包含网表、布局和失败现场。它们不是历史出图，不能作为清理输出的一部分删除。
- `tools/` 保存开发对照和图鉴工具，例如 `compare_layouts.py`、`compare_attachment_integration.py` 和 `generate_structure_gallery.py`。按工具说明选择只读复验或完整出图；有些对照工具会运行多次完整 flow。
- `docs/` 保存解释与实测记录；历史版本的性能数字、测试数量和停止状态仍按当时口径保留。
- `dist/` 只存放交付包，`notes/` 只存放手工笔记。运行主 flow 不依赖这两个目录中的文件。

当前含线路的成功 JSON 在固定布局重布、单独缩线或独立重绘时，继续恢复保存坐标；目录整理不要求重新布局。既有成功图仍保留在原输出位置。各轮对照报告是否完成、是否通过发布门槛，应读取对应记录，不能因源码已经归档到插件目录就认定实验已发布。

## 资源与独立实验项目

`symbol-geometry.json` 和 `analog-canvas-main/packages/components/definitions/` 共同决定图形、引脚、旋转镜像及障碍几何，仍沿用原资源位置。上游网页工程不参与日常 Python flow；保留的第三方资源与许可证见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

`agent-rsi-demo/` 和 `shinka-circuit-rsi/` 各有自己的代码、依赖、引擎／参考和运行目录，日常主 flow 不导入这两套独立项目。它们的引擎快照不是本次主包源码的重复副本清理目标。`ShinkaEvolve/` 是本地独立上游检出，也继续留在原位置。

根目录原有的 `agent-rsi-demo-portable.zip`、`shinka-circuit-rsi.zip`、`shinka-circuit-rsi.tar.gz` 收入 `dist/`。若需要以后也将新包输出到这里，可从各独立项目目录显式指定位置：

```powershell
# 在 agent-rsi-demo/ 内执行
python package_demo.py --archive ..\dist\agent-rsi-demo-portable.zip

# 在 shinka-circuit-rsi/ 内执行
python tools/package_demo.py --out ..\dist
```

这些命令是按需打包，不是主 flow 的前置步骤。

`.venv/` 可能引用 `.python/distribution/`，因此两者一起保留；移动项目或换机器时按 [运行说明](flow-guide.md#环境重建与验证) 重建环境。`.cache/` 可含本地 Git 等工具，清理前应确认工具已经有其他可用位置。

## 常用入口保持不变

```powershell
Set-Location D:\gongzuo

# 日常生成
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut

# 已有 JSON 的独立重绘
.\.venv\Scripts\python.exe .\render-clean.py .\output\3\3-placement.json .\output\3\3-redraw.svg

# 开发回归入口；不会因目录整理而自动执行
.\.venv\Scripts\python.exe -B -m unittest discover -s .\tests -q
```

逐步理解算法见 [当前flow逐步详解](flow-walkthrough.md)；参数、预算和环境说明见 [flow-guide.md](flow-guide.md)。通过 [source_map.json](../netlist2schematic/source_map.json) 可以从旧文档中的模块名找到当前源码。

## 本次目录整理的验证

54个实现模块均可导入，六个根命令入口可从其他工作目录调用。两组共199项针对性回归通过，覆盖解析、布局插件接口、保存恢复、网标、源码指纹，以及短小RC例子的入口和重绘；没有启动数据集批量出图或完整1197项测试。

除路径与动态加载适配涉及的 `sa_common.py`、`place_and_route.py`、`net_labels.py` 外，其余51个模块在统一导入命名后，算法语法树与整理前一致。119份已跟踪网表及符号文件与整理前提交逐字节一致，移入 `dist/` 和 `notes/` 的文件也保留原内容。
