# Netlist2Schematic

把 SPICE 网表转换成布局 JSON、彩色 SVG 和黑白 SVG。默认按电流层级和电路子结构布局，使用模拟退火优化位置，再进行正交 A* 布线、连接与几何验收，最后安全缩短绕线、减少多余折角。输入网表保持原样。

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
| 正常优化 | 结构化布局、SA、最多四个候选的最终 A* 布线 | 300秒 |
| 局部救援 | 正常阶段全部失败后，保留布局、修复接口并局部重布 | 60秒 |
| 扩距重试 | 救援失败后，将原失败布局的间距扩大1.5倍重布一次 | 60秒 |
| 总线后备 | 扩距仍失败后，通过独立通道构造完整连接 | 60秒 |
| 安全简化 | 选定合法结果后，先完成原有路径收紧，再协同尝试支路重接和路径缩短；各项均不增加 | 5秒 |

SA 迭代期间不调用 A*。前一阶段成功就跳过后续恢复；总线后备不执行安全简化。各项上限合计约485秒，另有资源检查及文件输出耗时。每份成功结果都必须通过完整验收。

最终安全简化统一管理已有拉直/缩短动作与新支路重接：先让原有动作收紧到当前没有可接受改进，再从这一结果开始尝试重接，并轮流给两类动作各一次候选机会。每次接受后都基于最新线路重新分析，后续只接受完整验收通过、各项不退化且线长或折角严格改善的方案。两阶段共用5秒和512个候选的上限；如果原有动作阶段已耗尽预算，就保留已验收结果并记录尚未完成，不强行进入重接。最终同一份线路用于JSON和两种SVG，后续重绘读取保存结果。完整顺序见[运行说明](docs/flow-guide.md#最终路线的安全简化)。

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
