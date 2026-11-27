# 历史设计与验证记录

2026-11-27整理。此目录保留有追溯价值的算法演进、逐轮指标、失败现场与用户视觉取舍，**不是当前运行指南或自动续跑任务**。默认开关、候选时间与最终排序请读[文档目录](../README.md)、[候选预算](../candidate-pipeline.md)、[选图规则](../candidate-selection.md)。

| 记录 | 内容 | 当前入口 |
| --- | --- | --- |
| [无源模块日志](passive-modules-log.md) | 紧凑间距、端部二维、adaptive预算、失败续插、正常择优及叶块保护 | [无源模块](../passive-modules.md) |
| [附件实验日志](attachment-integration-log.md) | 早期联合布局、成品嵌入和Goal逐轮记录 | [附件实验](../attachment-integration.md) |
| [支路组织日志](branch-organization-log.md) | 主列/区域前移、首轮失败及后续推广 | [支路组织](../branch-organization.md) |
| [旧候选调度](candidate-scheduling-log.md) | 历史40%／80%和共享修复、旧推广证据 | [独立候选预算](../candidate-pipeline.md) |
| [结构设计](structure-layout-log.md)、[实测](structure-layout-results.md) | 结构阶段、证据纠偏、父子保护及列策略 | [结构实验](../structure-layout.md) |
| [区域实验日志](region-layout-log.md) | 功能区、局部轴、整批与Goal取舍 | [区域实验](../region-layout.md) |
| [对称实验日志](symmetry-layout-log.md) | 器件对称与旧125%选优研究 | [对称实验](../symmetry-layout.md) |
| [可读性实验日志](readability-improvement-log.md) | 平衡缩线、无源组织、源归位、空带与风险估价 | [可读性实验](../readability-improvement.md) |
| [v6](route-scheduling-v6.md)、[v7](track-fairness-v7.md)、[v8](candidate-clearance-v8.md) | 缩线阶段配额、公平轮转和交叉净区预筛 | [当前安全缩线](../route-simplification.md) |
| [BJT与VEE验证](bjt-and-vee-validation.md) | 共用基础拓扑及负电源标记，包含早期637失败 | [当前范围](../flow-guide.md) |
| [早期图4至7](early-flow-examples.md) | 当时的候选、恢复及缩线指标 | [最新验证](../validation-status.md) |
| [早期待办](pending-improvements-log.md) | c36/c26朝向试摆、c43错层及当时建议 | [当前积压](../pending-improvements.md) |

每份日志可能先后出现不同默认值，这是历史文本，不应将某处“当前”“最新”脱离其版本理解。旧预算和命令只帮助解释当时实验；若要复现应先找到相应提交、输入哈希及冻结资源，而不是在当前代码上照抄并期待相同图。

`output/`及部分数据集未提交Git；日志记录的本地HTML和图片可被用户清理，因此将它们保留为路径索引。资料缺失时不重新生成伪历史成品。已经被当前指南替代的重复入口与一次性实施计划可从Git历史找回，不在日常目录再维护一份。
