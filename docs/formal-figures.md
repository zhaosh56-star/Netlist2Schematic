# 正式电路图导出

> 2026-11-27????????????????????????????????????????????????????[????](flow-guide.md)????????[????](validation-status.md)?????????????

正式出图已封装为 `netlist2schematic/plugins/formal_output/` 插件。默认开启，在主 flow 最后调用；用 `--formal-output off` 关闭。它复用现有字体排版与 VDD 合并规则，不重新运行布局、SA、A*、恢复或安全缩线。

## 主 flow 最后生成正式图

```powershell
Set-Location D:\gongzuo
.\.venv\Scripts\python.exe .\run_flow.py .\examples\7.cir

# 只应用正式字体与标注清理，保持独立 VDD 标记
.\.venv\Scripts\python.exe .\run_flow.py .\examples\7.cir `
  --formal-output auto --formal-power-rails off
```

执行顺序为：布局、SA、布线及恢复 → 原安全缩线 → 原 JSON 和两种 SVG 保存成功 → 其他独立展示插件 → 正式出图插件。首选正式图直接放在本次输出目录顶层，名为 `*-formal.svg`；`flow-result.json` 记录本次成品位置和状态。`formal/<指纹>/` 保留可回放的独立记录，包含 `*-formal.svg`、`*-formal.json`，以及可独立重绘的 VDD 方案记录。

正式插件自带 VDD 合并，不需要再加 `--power-rails auto`。若两者同时开启，它们分别基于原始完整成品输出独立视图，不会把 VDD 连续合并两遍。网标视图同样独立，正式版保留完整信号导线。

## 标号跟着器件，避免混淆

新版 `formal-schematic-v2` 只改文字位置，不移动器件和任何导线：

- 器件名围绕真实符号外框找邻近位置，文字外框离本器件不超过约38单位；若另一个器件反而更近，拒绝这个位置。
- 根据所有文字的可用空位一起避让，优先安排位置最少的标号，避免前面的名字占掉邻居唯一空位。
- VDD/VSS文字按真实供电符号定位，不能再用宿主MOS外框代替；供电文字允许最多约64单位以躲开小符号周围的引线。
- 文字避开器件、供电符号、导线与其他文字。找不到安全近位时明确记录排版失败，保留原生合法图，不能将标号挪到几百单位外冒充成功。
- 字体、数学下标及原始实例名保持；`style.labels` 保存所属器件、真实外框和距离，便于复核。

已有v1正式JSON重绘仍用冻结v1排版，保持字节一致；主动重新导出采用v2。原生JSON与完整线路始终保持原义。

当前主CLI和默认批量采用每候选独立120＋30＋30＋30秒、整图720秒外层保护，正式输出也受剩余总时间限制；不是早期每图120秒的共享调度。单独处理已有JSON不运行SA或A*。预算见[候选流程](candidate-pipeline.md)，批量命令见[批量说明](batch-flow.md)。

## 对已有 JSON 单独调用

```powershell
.\.venv\Scripts\python.exe -m netlist2schematic.plugins.formal_output.formal_output `
  --placement .\output\7\7-placement.json --out-dir .\output\formal\7

# 禁用 VDD 合并；字体、下标、去白圈与虚框照常处理
.\.venv\Scripts\python.exe -m netlist2schematic.plugins.formal_output.formal_output `
  --placement .\output\7\7-placement.json --power-rails off --out-dir .\output\formal\7-unmerged

# 用上一步打印的正式记录路径替换“正式记录.json”，按保存的方案复验和重绘
.\.venv\Scripts\python.exe -m netlist2schematic.plugins.formal_output.formal_output `
  --redraw 正式记录.json --out-dir .\output\formal\7-redrawn
```

也可在 Python 中调用：

```python
from netlist2schematic.plugins.formal_output.formal_output import generate, redraw

result = generate("output/7/7-placement.json", "output/formal/7", power_rails="auto")
print(result["artifacts"]["svg"])
redraw(result["artifacts"]["json"], "output/formal/7-redrawn")
```

独立命令也能用解释器、插件脚本及输入的绝对路径从其他工作目录启动。生成会复验原线路与供电方案；重绘复用保存的 VDD 连接，不重新选轨道或摆文字。源网表、include、符号资源变化，或保存方案损坏时会拒绝输出。正式图与原成品分开，主 flow 中插件失败会记录 `formal/plugin-diagnostics.json`，保留原成功结果。

目前沿用原 VDD 插件的适用边界：普通布线、局部救援和扩距结果可用；自定义 `power_connections`（例如旧总线后备）会明确拒绝，而不是改写供电连接。不能安全合并的普通供电组保留独立标记。

## 整批图集

使用已经验收的整批 `summary.json`：

```powershell
Set-Location D:\gongzuo
.\.venv\Scripts\python.exe -X utf8 -B .\tools\export_formal_dataset.py `
  --summary .\output\test-data2-terminal-2d-20261119\summary.json `
  --out-dir .\output\test-data2-formal
```

导出先调用现有的 `power_rails` 插件，复验原始完整线路，再局部合并能安全连接的 VDD。不能合并的保留独立标记，不移动器件或信号线。

整批工具现在也通过上述正式插件导出，避免单张命令、主 flow 和图集工具各维护一套收尾代码。字体排版实现仍共享 `rendering/formal_svg.py`，VDD 几何与验收共享原 `plugins/power_rails/`。

文字使用 GT 的字体回退列表，正文大小 15.116，后缀下标大小 11.48816。器件变量为粗斜体，后缀为直立下标；CLK 等普通端口名称不强制拆成下标。保留实例和真实网络身份。没有安装的字体由浏览器依次回退，与 GT 的机制一致。

去掉器件端口白圈、结构虚框和指标标题，保留真实分支黑点和实际电源符号。文字单独寻找邻近位置，检查与器件、线路、其他文字的估计包围盒重叠；改字前后逐项比较全部非文字 SVG 几何，包括器件变换、电源线、信号线和分支点。

输出目录：

- `index.html`：21 张正式图，图片内嵌，可点击放大，支持打印。
- `comparison.html`：GT 与正式版对照，图片同样内嵌。
- `figures/c26.svg`～`c46.svg`：用于报告或论文的独立矢量图。
- `test_data2-formal-svg.zip`：图集和 SVG 的打包下载。
- `records/`、`manifest.json`：原始输入指纹、供电合并验收和文字位置记录。

本轮使用当前最新的 21 张已验收正常布线结果：14 张安全合并，共减少 28 个供电标记。输入网表、原始 JSON、器件位置和信号线路保持原样。尺寸与指标记录仍对应原始物理布局，不能把 VDD 展示合并当作信号布线优化。

## 重跑另一组数据并汇总

先按主 flow 默认预算重新运行 `test_data`，然后将完成后的清单交给导出工具。这里没有使用快速验证的 60/120 秒覆盖参数。

```powershell
.\.venv\Scripts\python.exe -X utf8 -B .\tools\run_dataset.py .\test_data `
  --budget default --start-number 1 --out-dir .\output\test-data-formal-flow-20261119

.\.venv\Scripts\python.exe -X utf8 -B .\tools\export_formal_dataset.py `
  --summary .\output\test-data-formal-flow-20261119\manifest.json `
  --dataset-name test_data --out-dir .\output\test-data-formal

.\.venv\Scripts\python.exe -X utf8 -B .\tools\combine_formal_galleries.py `
  --galleries .\output\test-data-formal .\output\test-data2-formal `
  --out-dir .\output\formal-figures
```

每轮原生 flow 使用新的输出目录。汇总工具复制正式图到共同目录的 `test_data/`、`test_data2/`，保留各自的记录；总首页可按数据集筛选，GT 对照和全部 SVG 图包也放在共同目录。复制前后检查各 SVG 哈希，不重新选择供电合并方案或文字位置。

2026-11-19 已完成上述整批运行：`test_data` 的 25 张全部通过，21 张正常布线、4 张局部救援，没有失败或总线后备；原生运行耗时约 34 分钟。与上一轮 `test_data2` 的 21 张正式图合成 `output/formal-figures/`，共 46 张，24 张完成局部 VDD 合并，减少 44 个供电标记。逐张检查了字体、完整器件标注、去白圈与虚框，以及正式 SVG 对原生信号和器件几何的一致性。总目录的 `summary.csv` 保存原生指标，`verification.json` 保存核对结果。

对于密集区域的文字，先使用原有邻近位置；仅在这些位置都有遮挡时，增加围绕接点的二维候选位置。该排字补充不调用 SA 或 A*，不移动器件、原始端口或任何导线，已有合法文字位置保持不动。

## 插件封装验证

本次通过 154 项针对性回归，包括默认绕过、主 flow 成功落盘后的调用、插件失败保留原成品、VDD 关闭、源/include 变化拒绝、篡改供电方案拒绝及独立重绘。46 份已保存布局通过插件重新导出后，正式 SVG 与之前版本逐字节相同；其中三份真实结果的固定方案重绘也逐字节一致。C07 的关闭合并验证保留全部 8 个供电标记。

另用一个全新的 RC 电路实际运行了带 `--formal-output auto` 的完整 flow，正常产生原 JSON、两种 SVG 及额外正式图。以上没有重新跑整批 SA/A*；检查记录保存在 `output/formal-plugin-validation/`。

## 历史验证：就近标号与当时120秒整图预算

本地对照页为 `output/default-flow-120s/index.html`，指标记录为同目录的 `label-comparison.json`：

- 对最新46张合法成品仅重排文字，沿用各自已有的VDD方案，不重跑布局、SA或A*，总排版约17.31秒。
- 按文字包围盒到真实符号外框的距离衡量，“比自身器件更靠近另一器件”的标号由39个降为0个；最大器件名字距由155降到约17.56单位。
- 46张的输入JSON及全部非文字电气几何一致。图7、23、24、36的GT／旧版／新版做了浏览器视觉检查。
- 图23另从原网表完整运行默认命令，未覆盖时间或正式图开关，约72.54秒完成，normal阶段、候选3；最终线长5270、交叉7、折角28。原始引脚、几何、原生彩色/黑白一致性及正式JSON独立重绘均通过。
- 批量脚本另用一张自包含运放RC网表验证：只执行一次主flow，约3.4秒完成，自动生成正式图并直接用于报告。46张文字对照不能当作46张全流程重跑的成功率。

本轮2545项完整回归全部通过（0失败、0错误），完整回归收据与源码/测试指纹保存在 `output/default-flow-120s/regression/unit-tests-result.json`。原始数据和已有成功成品不被这些对照覆盖。
