# 文档目录

核对日期：2026-11-27。当前说明对应主算法 `2405d49`；网页适配另外对应适配分支 `8a65a26`。这两个提交是本轮文档审阅的代码基线，不要求日常运行固定检出它们。

## 从哪里开始

| 要做的事 | 文档 |
| --- | --- |
| 安装、跑第一张图 | [项目 README](../README.md) |
| 理解每一步、查用到了哪些代码 | [通俗流程详解](flow-walkthrough.md) |
| 查参数、默认值、器件范围和保存兼容 | [运行指南](flow-guide.md) |
| 查候选2/3/1与恢复阶段的时间 | [候选独立预算](candidate-pipeline.md) |
| 理解为什么选中某个候选 | [选图规则](candidate-selection.md) |
| 批量跑目录、短预算验证与断点续跑 | [批量运行](batch-flow.md) |
| 在代码中定位模块 | [目录与文件用途](project-layout.md) |

## 当前默认算法的设计点

| 主题 | 说明 |
| --- | --- |
| 主列、小结构和区域顺序 | [支路组织](branch-organization.md) |
| 没有直接电源起点的局部支路 | [对应支路显示行](corresponding-branch-rows.md) |
| 无源件位于两段有源支路之间 | [跨 R/L/C 的显示关系](inline-passive-branches.md) |
| 先保有源，再插串并联模块 | [无源模块与二维找位](passive-modules.md) |
| 电压源、电流源参与模块 | [源器件布局](source-module-layout.md) |
| 已经布通后去掉多余绕线 | [安全缩线](route-simplification.md)、[可选自动预算](auto-simplify-budget.md) |
| 给别人讲解设计点 | [小电路前后对照](design-points-examples.md)、[子结构图鉴](structure-gallery.md) |

`plugins/` 是代码分类，不代表其中功能都默认关闭。主列、支路、无源骨架、端口预检和正式出图已经用于默认新布局；下方的独立实验开关则继续关闭。

## 展示与交付

- [正式图](formal-figures.md)：默认生成 `*-formal.svg`，内部安全合并局部 VDD。
- [独立共享 VDD 视图](power-rails.md)：`--power-rails auto` 显式生成额外版本。
- [网标视图](net-labels.md)：`--net-labels auto`，局部实体连线加同名网标；必须已有完整合法线路。
- [单 HTML 盲测](blind-evaluation.md)：任意图对清单生成问卷和可下载报告。

## 默认关闭的实验

| 开关 | 当前说明 |
| --- | --- |
| `--structure-layout auto` | [结构关系增强](structure-layout.md) |
| `--symmetry-layout auto` | [器件对称](symmetry-layout.md) |
| `--region-layout auto` | [独立区域布局](region-layout.md) |
| `--readability-layout auto` | [无源、源及空隙整理](readability-improvement.md) |
| `--attachment-layout passive` / `all` | [早期联合附件布局](attachment-integration.md) |
| `--passive-embedding auto` | [合法成品后的无源试摆](attachment-integration.md#成品后的无源试摆) |

实验策略可能与默认骨架路径互斥，不能把所有开关同时打开。最终候选排序统一见[选图规则](candidate-selection.md)；历史报告中的 Q、结构优先、115%／125%门槛不再覆盖当前最终排序。局部试摆自己的估计护栏仍可能保留，两者不要混淆。

## 状态、历史与适配

- [验证状态](validation-status.md)：最近已经完成的测试、数据集范围与限制，不需要重跑历史批次才能阅读。
- [待改进清单](pending-improvements.md)：仍待重新验证的设计建议，不能作为已上线功能。
- [历史记录](history/README.md)：逐轮试验、旧调度、旧版缩线和结构实验。带日期和版本的数字只证明对应版本。
- `adapt/auto-canvas-huyangc` 分支的 `integrations/auto_canvas_huyangc/README.md`、`WEB.md`：原生项目输入输出与网页部署。该分支的文档不要求复制进 `main` 才能使用主算法。

`output/` 和数据集是本地资料，Git 检出不保证包含历史 HTML、图片或源路径。历史记录保留索引和结论，不能承诺旧报告仍可点击。清理输出时保留你要交付的成品；回归夹具和许可证不要删除。

后续更新约定：运行方式写进当前指南；逐轮指标放进历史记录；默认开关以 `flow/run_flow.py` 的解析和策略解析函数为准；主 flow 与网页适配分别说明预算。不要再把完整流水账追加到 README。
