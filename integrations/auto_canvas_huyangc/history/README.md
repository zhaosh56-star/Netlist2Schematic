# 适配历史记录

2026-11-27整理。当前安装、启动和支持范围以[README](../README.md)、[网页说明](../WEB.md)、[预算与草稿](../editable-drafts.md)为准。下面保留每一轮当时的状态与测试，不把“尚未接按钮”“候选1成功”或早期失败当作当前行为。

| 记录 | 用途 |
| --- | --- |
| [原生JSON接口](native-json-report.md) | 首轮输入输出、事务及32项测试 |
| [网页接口](web-integration-report.md) | Worker、服务、取消与原子回写 |
| [正式端口修复](web-port-fix-report.md) | 原生供电语义、端口顺序和补线回环 |
| [供电飞线与符号](web-supply-fix-report.md) | 命名连接、真实供电连通、VDD/VEE/GND图形 |
| [调度、草稿与附件额度](editable-drafts-log.md) | 早期60/300秒结果、正常优先及adaptive的当时验证 |

已执行完的 `plan.md`、`web-plan.md`与当前入口重复，已清理；分支所有权、只读目标项目、支持边界和复现方法保留在README与WEB，原计划可由Git历史恢复。`output/`中的截图与项目快照未提交，清理后不保证路径仍存在。
