# 附件布局实验与当前模块方案的区别

核对日期：2026-11-27。日常默认使用[有源骨架＋无源模块](passive-modules.md)。本文的 `--attachment-layout` 与 `--passive-embedding`继续默认关闭；它们保留用于研究对照，不是启用当前无源能力的必要参数。

## 联合附件布局

| 模式 | 作用 |
| --- | --- |
| `--attachment-layout off` | 不启用这套实验；不等于关闭默认无源模块 |
| `--attachment-layout passive` | 按端口关系，为无源串并联附件安排内部位置，必要时宿主一起让位 |
| `--attachment-layout all` | 在passive基础上加入有明确归属的电压源、电流源 |

实验用于structured＋flexible的候选2/3，候选1保留旧参照。先识别无源串联、并联、桥接及单端归属，初始化时安排必要显示行列，再用原SA和附件软评分调整；收尾以真实引脚和静态几何检验。共同供电或型号本身不能建立归属，多宿主歧义不得强行合并成大刚体。

有源导通先后、固定器件、小结构以及原始端口保持；SA中不动态插行。局部试摆使用共享额度，之后仍需A*和完整验收。最终候选排序统一是[折角→交叉→线长](candidate-selection.md)，旧附件选择器的结构偏好只作诊断，不再覆盖实际胜者。

```powershell
.\.venv\Scripts\python.exe .\run_flow.py .\examples\7.cir --attachment-layout passive --out-dir .\output\7-attachment-passive
.\.venv\Scripts\python.exe .\run_flow.py .\examples\7.cir --attachment-layout all --out-dir .\output\7-attachment-all
```

不与structure、region、readability、symmetry的开启模式叠加；默认策略解析会保持兼容路径。显式强开不兼容骨架参数会报错。固定布局重布、简化和重绘不重新安排附件。

实现位于 `netlist2schematic/plugins/attachment/`，包括 `attachment_integration.py`、`attachment_local.py`、`attachment_sources.py`、`attachment_selection.py`。共享端口和对齐能力位于 `plugins/layout_shared/`。

## 成品后的无源试摆

`--passive-embedding auto`先取得完整合法成品，再用独立5秒尝试一次无源局部重排和真实重布。MOS/BJT及V/I/E等非无源位置、朝向和公共轴保持，只有组织改善且实际线长、交叉、折角均不增加才采用；无收益、失败或超时交付原图。

```powershell
.\.venv\Scripts\python.exe .\run_flow.py .\examples\7.cir --passive-embedding auto --out-dir .\output\7-embedding
```

它与“先搭有源再插模块”不同，包含一次受限重布，不是默认安全缩线的一部分。实现为 `plugins/passive_embedding/passive_embedding.py` 与 `safe_postroute.py`。保存JSON记录试案和实际采用情况，不能把未采用的局部预览记为成品收益。

## 历史与未完成事项

早期Goal逐轮记录、用户视觉反馈、停止状态及当时对照数据保留在[附件实验日志](history/attachment-integration-log.md)。该日志中的“当前Goal”、旧300秒预算、旧选优门槛和骨架默认off均是当时状态；不是待自动继续执行的任务，也不是当前默认说明。

当前实验继续关闭。本轮只审阅文档，没有启动旧Goal、全量出图或改变任何实验默认值。
