# Netlist2Schematic

把 SPICE 网表转换成布局 JSON、彩色 SVG 和黑白 SVG。默认按电流层级和电路子结构布局，使用模拟退火优化位置，再进行正交 A* 布线、连接与几何验收，最后安全缩短绕线、减少多余折角。输入网表保持原样。

想理解每一步怎么执行、用哪些代码，以及SA估价、A*、失败恢复和最终简化的区别，请先读[从网表到原理图：当前flow逐步详解](docs/flow-walkthrough.md)。

结构布局改进通过 `--structure-layout auto` 试用，开发期间默认关闭。候选1保留参照，候选2/3按有拓扑证据的局部块和支路改善排列，继续使用原A*、完整验收和安全缩线。分阶段规则与对照命令见[结构布局说明](docs/structure-layout.md)。

想看目前识别的子结构是什么样，见[子结构图鉴说明](docs/structure-gallery.md)。运行 `.\.venv\Scripts\python.exe .\tools\generate_structure_gallery.py` 可生成20张带角色、网表及实际识别记录的示意图，入口为 `output/structure-gallery/index.html`。

## 日常运行

在项目目录打开 PowerShell。当前工作区已有运行环境时，直接执行：

```powershell
Set-Location D:\gongzuo
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir
```

结果统一放在 `output/3/`：

- `3-placement.json`：位置、朝向、线路、指标及验收结果。
- `3-layered.svg`：彩色图。
- `3-clean.svg`：黑白图，可直接用浏览器打开。

图5包含子电路，需明确选择 `dut`：

```powershell
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut
```

结果位于 `output/5-dut/`，文件名分别为 `5-dut-placement.json`、`5-dut-layered.svg` 和 `5-dut-clean.svg`。

所有日常生成物都在项目根目录的 `output/` 下，按输入名称分组；默认目录不附加布局模式。相同输入再次运行会更新同名结果。要比较不同参数，可用 `--out-dir .\output\3-compare` 指定独立目录。历史出图不作为代码或回归输入保留，需查看时重新生成。

## 首次配置环境

已有 Python 3.12 时：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
```

没有可用 Python 时，使用项目安装脚本：

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\setup_python.ps1
```

脚本安装本地 Python 3.12.10 并创建 `.venv/`，依赖固定为 NetworkX 3.7。符号资源已随仓库提供，无需启动 Analog Canvas 网页项目或安装 Node.js。

## 运行方式与预算

| 阶段 | 作用 | 默认独立预算 |
| --- | --- | ---: |
| 正常优化 | 结构化布局、SA、候选1～3的最终 A* 布线 | 300秒 |
| 局部救援 | 正常阶段全部失败后，保留布局、修复接口并局部重布 | 60秒 |
| 扩距重试 | 救援失败后，将原失败布局的间距扩大1.5倍重布一次 | 60秒 |
| 总线后备 | 扩距仍失败后，通过独立通道构造完整连接 | 60秒 |
| 安全简化 | 依次完成路径/支路简化、三段式及原轨道收紧；各项均不增加 | 5秒 |

SA 迭代期间不调用 A*。前一阶段成功就跳过后续恢复；总线后备不执行安全简化。各项上限合计约485秒，另有资源检查及文件输出耗时。每份成功结果都必须通过完整验收。

默认只生成和比较候选1、2、3，候选0的旧布局初始化、SA和A*均跳过。三种结构种子的编号、随机种子和每种约6666次提案预算保持原样，共享正常阶段300秒。候选3仍可用前面最佳结果的局部布线反馈版本。需要旧参考时显式加 `--candidate-id 0`；`--layout-style legacy` 和 `--mode baseline` 保留原有独立入口。指定候选1～3也不会先运行候选0的SA。

默认 `--passive-layout flexible`：结构候选1～3中的R/C/L/D可以跨行、与其他无源件交换，也能使用MOS/BJT同行块外的合法空位。串联、并联和反馈关系用软评分保持规整，不锁成刚体；MOS/BJT的导通层级、原有组动作和镜像规则不变。`--passive-layout fixed` 恢复旧的无源固定行行为。候选0、legacy、baseline及不含R/C/L/D的电路沿用原算法；读取保存JSON重布、简化或重绘时不重新摆件。详细规则及对比命令见[无源灵活布局](docs/flow-guide.md#无源器件灵活跨行布局)。

默认 `--symmetry-layout off`：日常命令不加该参数时，跳过额外的器件对称规整和S对称选优，候选按原Q选择。需要试用时显式添加 `--symmetry-layout auto`：候选1保留原摆法，候选2、3在SA和布线反馈之后、最终A*之前，尝试让明确对应的器件位置与朝向更对称。可有多个局部轴，也可组成较大的对称核心；不要求导线镜像，暂不增加上下串管同轴目标。开启后每候选最多1秒，计入原300秒，不新增A*次数；在本轮原Q最佳合法候选125%的实际线长范围内优先比较器件对称，同等对称程度再用原Q择优。规则、记录和显式auto/off实验见[器件对称布局说明](docs/symmetry-layout.md)。

`--passive-layout` 控制SA中的无源动作；可选的对称收尾是后续独立阶段。当前默认已跳过对称收尾；若还需要旧的无源固定行SA，使用 `--passive-layout fixed`。

最终安全简化先完成已有的拉直/缩短和叶支路重接，在这个已验收结果稳定后，才启用通用三段式候选。对于内部至少两个折角、且没有其他引脚、分支或固定出线的连续线段，尝试“横—竖—横”或“竖—横—竖”，减少两端小台阶或不必要绕行。候选按当前轨道、障碍和间距边界生成，不按图号或器件编号设置。新阶段与已有两类动作轮流尝试，每次接受后都从最新线路重新分析；只有完整验收通过，线长、折角、交叉均不增加，且线长或折角至少一项严格改善，才替换。全部阶段共用原来的5秒、四轮及512个候选上限；前阶段预算耗尽时保留已验收结果，不强行进入后续动作。最终同一份线路用于JSON和两种SVG，含线路的JSON重绘时只复验和绘制保存结果。完整顺序见[运行说明](docs/flow-guide.md#最终路线的安全简化)。

当前v5在全部v4动作稳定后，再尝试原路径轨道收紧：保留自由链两端，移动内部水平或竖直段及其拐点，让四段及更长的外围折线向内收，不要求先把它改成三段。候选来自器件、电源/地标记和线路的几何边界，逐条完整验收，交叉限制保持原样。新阶段仍共用上述5秒/四轮/512候选；`--simplify-routes off` 可关闭整个后处理，旧v4记录会按新版重新尝试。

v4已通过313项测试及9套固定布局的v3/v4对照。最初更新图7主图：线长2812→2676、交叉3→2、折角24→20，后处理约0.93秒；原网表和器件位置不变。随后又按当前完整flow重跑了图4、6、5，最新来源、阶段及指标见[流程详解](docs/flow-walkthrough.md#examples)。先前的本地批量对比页仍记录v3历史结果。

```powershell
# 固定已有位置，跳过布局，仅重新布线；默认写到 output/3-rerouted/
.\.venv\Scripts\python.exe .\run_flow.py --reroute-placement .\output\3\3-placement.json

# 只简化保存的合法路线，跳过SA、A*和恢复；默认写到 output/3-simplified/
.\.venv\Scripts\python.exe .\run_flow.py --simplify-placement .\output\3\3-placement.json

# 独立重绘保存的路线，不再优化
.\.venv\Scripts\python.exe .\render-clean.py .\output\3\3-placement.json .\output\3\3-redraw.svg

# 运行回归测试；测试输入独立保存在 tests/fixtures/
.\.venv\Scripts\python.exe -B -m unittest discover -s .\tests -q
```

完整参数、器件范围、几何规则、恢复过程和复现命令见[运行说明](docs/flow-guide.md)。各文件用途和目录保留范围见[项目目录说明](docs/project-layout.md)。原来的[README-SA.md](README-SA.md)保留为说明入口。

支持扁平网表和显式指定的子电路，不递归展开任意 X 实例。连接验收检查当前支持器件的可见引脚，不执行电路功能仿真。`examples/6.cir` 原始内容有未配对的 `.ends`，会如实报错；如需运行，应另存副本并补齐子电路声明或移除此孤立标记。

## 上游资源

符号定义和四份测试网表来自 [Analog Canvas](https://github.com/cascode-ai/analog-canvas)。保留的许可证与来源说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。`output/`、`.cache/`、`.venv/`、`.python/` 和本地 `archive/` 均不进入 Git。
