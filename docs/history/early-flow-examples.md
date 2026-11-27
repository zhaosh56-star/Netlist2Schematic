# 早期图4至7运行记录

> 2026-11-27归档。仅保留历史证据，不代表当前默认行为；当前说明见[文档入口](../README.md)。

## 16. 图4、5、6、7本轮实际走过哪些步骤

下表是2026年11月5日本地结果，不是所有电路的性能承诺。生成物不进入Git，删除后可按相同输入重新生成。

| 图 | 本次输入及动作 | 实际成功阶段 | 最终线长 / 交叉 / 折角 |
| --- | --- | --- | --- |
| 4 | 当前 `examples/4.cir`完整重跑，选中候选3 | normal；正常阶段约264秒，简化约0.92秒 | 2802 / 6 / 15 |
| 5 | 当前 `examples/5.cir --cell dut`完整重跑，候选0 | 正常约300秒失败；local_rescue约14.59秒成功，再简化约2.74秒 | 8830 / 12 / 32 |
| 6 | 已有 `output/6/input/6.cir`修正副本完整重跑，选中候选1 | normal约300秒；后续候选超时仍保留已有成功结果，简化5秒 | 4230 / 5 / 30 |
| 7 | 在已有合法布局和路线基础上升级v4简化，未重跑完整SA/A* | 沿用原normal来源，新增简化约0.93秒 | 2676 / 2 / 20 |

图6的原始 `examples/6.cir`含孤立 `.ends`，运行副本只省略这一行，器件和连接未改；这不是入口自动纠正语法的功能。图7原网表缺少可明确判断部分型号极性的声明，已有wrapper和 `model-polarities.inc`提供绘图用极性声明，原网表不改。该声明不是完整仿真模型。

复现当前样例的完整输入方式：

```powershell
.\.venv\Scripts\python.exe .\run_flow.py .\examples\4.cir
.\.venv\Scripts\python.exe .\run_flow.py .\examples\5.cir --cell dut
.\.venv\Scripts\python.exe .\run_flow.py .\output\6\input\6.cir --out-dir .\output\6
.\.venv\Scripts\python.exe .\run_flow.py .\output\7\input\7.cir --out-dir .\output\7
```

最后一条是图7的完整重跑命令，不是上表仅用0.93秒做后处理的那次操作。恢复中用哪个候选以及最终位置，应以新运行JSON为准。

图5旧主图来自冻结候选2，本次来自完整流程中实际失败的候选0，不能把两者当作同一固定布局上的纯算法对照。对照新旧代码时，要明确固定的是网表、种子、坐标，还是连路线也固定。

<a id="files"></a>
