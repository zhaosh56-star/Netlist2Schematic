# 独立区域布局实验

核对日期：2026-11-27。`--region-layout auto|off`默认 **off**。当前默认已使用[主列与支路组织](branch-organization.md)，这与本页的独立区域实验是不同入口；不要因这里off就认为日常图没有区域或主列规则。

## 识别与摆放

区域计划表达“区域—子块—支路—附件”。在完整端口证据下识别时控双支路、折叠主级和采样控制区：真正串接优先对齐，左右对应保持身份，折叠/分叉/反馈保留独立局部轴。共电源、偏置或时钟不能单独把多个区域合为刚体。

候选2/3在SA前确定区域顺序及初排，保留原有源层级；SA增加有界区域动作，flexible无源仍可在合法位置微调。候选1保持原参照。候选3的反馈必须兼容区域计划，不能拿旧摆法覆盖已提交的组织关系。

无适用证据时继续原路径，歧义或未知单独记录。`layout_regions.applied`只说明参与布局，不证明选中了该候选或实现了整图美观。

## 兼容与使用

```powershell
.\.venv\Scripts\python.exe .\run_flow.py .\test_data\11-15\strongarm-comparator.cir --cell dut --region-layout auto --out-dir .\output\strongarm-region
```

只对新布局生效；保存位置重布、缩线和重绘只恢复坐标与量测。与structure、symmetry、readability、attachment等不兼容的显式布局实验不能同时开启，也不隐式叠加默认骨架/支路组织。

最终合法候选一律按[折角→交叉→线长](candidate-selection.md)选图；区域选择器的原115%和结构优先建议仅作诊断。局部试摆本身的估计护栏仍有效，不能把“取消最终旧排序”理解为取消静态安全检查。

## 代码与证据

- `netlist2schematic/plugins/region/region_layout.py`：区域证据、归属、接口和量测。
- `netlist2schematic/plugins/region/region_placement.py`：初排、区域动作及局部整理。
- `layout/structured_placement.py`与`flow/aesthetic_flow.py`：接入、兼容反馈及统一最终选择。
- `tools/compare_region_layouts.py`：同参对照工具，可能运行多次完整flow，阅读文档时不必执行。

早期72次批跑、后续Goal与原局部规整实测见[区域实验日志](history/region-layout-log.md)。该批次存在视觉和交叉取舍，不能以部分识别成功声明实验已全部达标。默认保持关闭，历史命令的预算及报告路径不代表当前新运行。
