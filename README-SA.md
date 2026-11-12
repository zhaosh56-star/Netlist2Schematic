# SA Python 运行说明

详细说明已集中到 [docs/flow-guide.md](docs/flow-guide.md)，包括完整流程、分层与结构化布局、A*和失败恢复、最终路线简化、支持的网表、几何规则及全部参数。

日常运行与安装命令见 [README.md](README.md)。各脚本和资源的用途见 [docs/project-layout.md](docs/project-layout.md)。

```powershell
Set-Location D:\gongzuo
.\.venv\Scripts\python.exe .\run_flow.py .\examples\3.cir
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut
```

默认结果分别位于 `output/3/` 和 `output/5-dut/`。历史出图已不作为项目目录的一部分维护；固定回归输入及布局保留在 `tests/fixtures/`。

安全缩线默认仍为固定5秒。需要按首次合法线路的复杂度自动分配5～30秒时，可加 `--simplify-seconds auto`；估计后只执行一次缩线，候选上限仍是512。使用方式和实测见[自动缩线预算说明](docs/auto-simplify-budget.md)。

新增的可读性实验保持关闭：`--readability-layout auto` 尝试无源成组、源器件归位和失效间距回收，`--route-tradeoff balanced` 允许用少量稀疏交叉换取明显缩短。两者独立启用，默认仍为 `off` 和 `strict`；实现边界、分阶段实测及未达标项见[可读性改进说明](docs/readability-improvement.md)，本地对照入口为 `output/readability-improvement/index.html`。

附件联合布局使用独立开关 `--attachment-layout off|passive|all`，默认 `off`。`passive` 为有明确端口关系的无源网络预留内部位置，`all` 再尝试源器件归位；候选1保持原参照，不与其他布局实验同时开启。它与默认 flexible 的区别是可以在初排时让宿主支路一起让位，并保护附件后续的相对位置。用法、验收及两阶段的实际结果见[附件联合布局说明](docs/attachment-integration.md)。
