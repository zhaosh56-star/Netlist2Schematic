# Netlist2Schematic

把 SPICE 结构网表转换为布局 JSON、彩色 SVG 和黑白 SVG 的 Python 工具。
默认按电流层级组织器件，用模拟退火优化布局，再执行正交 A* 布线与连接、几何验收。

## 运行

在 Windows PowerShell 中进入仓库目录。已有 Python 3.12 时：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir --out-dir .\output\3
```

没有可用 Python 时，可以使用仓库的环境安装脚本：

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\setup_python.ps1
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir --out-dir .\output\3
```

脚本下载 Python 3.12.10 到当前目录并创建虚拟环境；依赖固定为 NetworkX 3.7。
符号资源已随仓库提供，不需要启动 Analog Canvas 网页项目或安装 Node.js。

输出包括：

- `3-placement.json`：器件位置、朝向、线路、指标及验收结果。
- `3-layered.svg`：彩色图。
- `3-clean.svg`：黑白图。

## 默认流程

| 阶段 | 处理方式 | 默认预算 |
| --- | --- | ---: |
| 正常优化 | 结构化布局、SA、最多四个候选的最终 A* 布线 | 300 秒 |
| 局部救援 | 正常候选全部失败后，保持布局，延长安全接口并小组重布 | 60 秒 |
| 扩距重试 | 局部救援仍失败后，物理间距扩大 1.5 倍重布一次 | 60 秒 |
| 总线后备 | 扩距仍失败后，用独立通道直接构造完整总线图 | 60 秒 |

SA 循环使用快速估价，不调用 A*。已有合格结果时不进入恢复阶段，默认最坏优化预算约480秒。
所有成功输出都要通过真实引脚连接、同网连通、异网分离及几何规则检查。
原网表按只读输入处理。

```powershell
# 指定子电路
.\.venv\Scripts\python.exe .\run_flow.py .\tests\fixtures\junction-clearance\5.cir --cell dut --out-dir .\output\5

# 直接使用确定性总线后备
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir --fallback only --out-dir .\output\bus\3

# 固定已有布局，跳过 SA，仅重新布线
.\.venv\Scripts\python.exe .\run_flow.py --reroute-placement .\output\3\3-placement.json --out-dir .\output\3-rerouted

# 固定布局重布失败时，显式允许局部救援；保持器件位置，不扩距或重新排布
.\.venv\Scripts\python.exe .\run_flow.py --reroute-placement .\tests\fixtures\junction-clearance\5-candidate2-placement.json --local-rescue auto --rescue-seconds 60 --out-dir .\output\local-rescue\5

# 使用保存的线路独立生成黑白图
.\.venv\Scripts\python.exe .\render-clean.py .\output\3\3-placement.json .\output\3-redraw.svg
```

完整参数、支持器件及边界见 [README-SA.md](README-SA.md)。

## 目录

- 根目录 Python 文件：解析、结构识别、布局、布线、渲染及公共支持。
- `examples/`：用户提供的输入网表，保留原始内容。
- `tests/`：回归测试和可独立使用的冻结输入、布局。
- `tools/compare_layouts.py`：已有布局的指标与 SVG 对比工具。
- `analog-canvas-main/`：从上游保留的符号定义、四份测试网表及原许可证。

`output/`、`.cache/`、`.venv/`、`.python/` 和本地 `archive/` 不进入 Git。
历史输出与已撤回实验只在本地归档；重新运行命令即可生成新结果。

## 验证与输入边界

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s .\tests -q
```

支持扁平网表与显式指定的子电路，不递归展开任意子电路。
连接验收不包含电路功能仿真；合法十字交叉可以非零。
固定布局重布保持保存坐标，完整 flow 会重新优化布局。

`examples/6.cir` 原始内容包含未配对的 `.ends`，解析器会如实报错；仓库保留该输入原样。
需要运行这类片段时，应在另存的输入副本中补全子电路声明或移除孤立结束标记。

## 上游资源

符号定义与四份测试网表来自 [Analog Canvas](https://github.com/cascode-ai/analog-canvas)。
保留的第三方许可与来源说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
