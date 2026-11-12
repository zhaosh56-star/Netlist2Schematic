# 无源与源器件融入布局：验证方法与运行记录

本轮把“相关附件能否进入主电路附近的合法空位”单独做成一个布局策略。对照工具只运行策略、保存结果和独立验收，不修改网表、引脚连接、原图或主流程默认值。

当前文档先记录验证方法；算法真实结果、已完成数量和剩余问题将在正式批次后补充。工具就绪不代表算法已经通过验收，也不代表默认开启。

## 三种模式如何比较

| 模式 | 本轮作用 |
|---|---|
| `off` | 关闭新附件融入策略，作为本轮基线 |
| `passive` | 仅启用无源附件融入，独立验收第一阶段 |
| `all` | 无源附件与有明确归属的源器件共同参与，独立验收第二阶段 |

三种模式都使用结构化主流程、现有 `flexible` 无源策略及 `strict` 安全缩线；`readability-layout`、`region-layout`、`structure-layout`、`symmetry-layout`、端口预检和网标插件均显式关闭。这样能区分本轮附件布局的收益与之前的缩线、对称等实验。

默认每张图使用种子 `20261029`、20,000 次 SA 迭代、正常阶段 300 秒、局部救援／扩距／总线后备各 60 秒、缩线 5 秒。三侧保持相同并发数，默认同时运行 2 个任务，最多 3 个。并发会影响按墙钟计时的候选是否来得及执行，因此报告同时保存并发数、实际尝试的候选和最终恢复阶段，不将计时差异直接归因于布局算法。

## 先验证重点，再跑完整集合

完整集合沿用已有清单发现方式：图 1～7 加 `test_data` 下的网表。图 6、7 继续使用既有归档输入和来源记录，不临时改写模型或网表。不能解析的输入明确保留原始错误，不悄悄删除，也不计作成功。

重点样例是 `data10`、`data12`、`data14`、`data15`，额外验证种子 `20261030`、`20261031`。按当前 32 份输入计算：基础 96 次，重点额外 24 次，共 120 次完整运行，包含原本不能解析的输入。

```powershell
# 只生成任务清单；不运行布局、布线或测试
.\.venv\Scripts\python.exe -X utf8 -B .\tools\compare_attachment_integration.py --plan-only

# 第一阶段：四个重点，只对照 off/passive，暂不增加额外种子
.\.venv\Scripts\python.exe -X utf8 -B .\tools\compare_attachment_integration.py --cases data10 data12 data14 data15 --core-seeds --policies off passive --jobs 2 --out-dir .\output\attachment-integration-comparison\passive-focus

# 显式候选 2 的探针：确认新策略真正进入初排、SA 与最终布线
.\.venv\Scripts\python.exe -X utf8 -B .\tools\compare_attachment_integration.py --cases data10 data12 data14 data15 --core-seeds --policies off passive --candidate-id 2 --jobs 2 --out-dir .\output\attachment-integration-comparison\candidate2-probe

# 全部三策略，同参对照与重点额外种子
.\.venv\Scripts\python.exe -X utf8 -B .\tools\compare_attachment_integration.py --jobs 2

# 读取进度，不启动新任务
.\.venv\Scripts\python.exe -X utf8 -B .\tools\compare_attachment_integration.py --status-only

# 独立运行项目完整单元测试，保存原始日志和测试数量
.\.venv\Scripts\python.exe -X utf8 -B .\tools\compare_attachment_integration.py --regressions-only
```

显式候选探针可以确认动作是否执行，但不能替代正常候选选择的发布验收。将命令中的 `--candidate-id 2` 改为 `3` 可验证第三个候选，必须使用另一个输出目录。

## 每份结果检查什么

工具调用原有完整验收，独立检查原始引脚连接、固定出线保留、同网连通、异网分离和几何规则；检查彩色与黑白 SVG 的线路一致，再从保存的 JSON 独立重绘，比较黑白 SVG 是否完全一致。

附件关系从原始网表和原行分配建立共同计划，再根据三侧的实际坐标重新测量。报告记录附件归属距离、真实嵌入成员、结构违规、有源支路顺序，以及线长、交叉、折角、逐网绕行、尺寸、运行时间和恢复阶段。没有可测关系时标记未知或不适用，不能当作零违规获得优势。

每个阶段分别与 `off` 比较：

- 每图最终线长不超过关闭版的 115%。
- 关闭版零交叉的图仍为零；其它图最多增加一处交叉。
- 同一验证集合的总交叉数不增加。
- 不新增几何失败、结构违规或总线后备降级。
- 同样的源网表、资源、种子、预算和缩线策略，才能组成有效对照。

`passive` 通过不意味着 `all` 通过；两套门槛分别记录。数值门槛通过后仍须检查 GT 与成品，确认附件更贴近对应支路、有源核心没有被打乱。工具始终不会自动修改默认策略。

## 断点续跑与旧结果保护

输出保存在 `output/attachment-integration-comparison/`。每个输入、种子和模式下面再按源文件、资源、代码、运行参数的指纹分目录，目录内包含命令、日志、生成物和 `run-record.json`。比较汇总持续写入 `comparison.json` 与 `index.html`。

再次运行相同命令，只复用完全相同指纹且重新验收通过的结果；不同代码或参数创建新目录，不覆盖旧成功图。`--retry-failed` 重新尝试失败记录，`--reuse-only` 只检查已有缓存，缺失项明确记为“未运行”，不会偷偷补跑。任务中断留下的 `running` 记录不能当作成功。

`--off-reference-report <旧comparison.json>` 可额外核对旧关闭版与当前关闭版的网表、参数、位置、线路和指标。旧代码结果只作兼容证据，不冒充当前版本新跑结果，也不绕过指纹检查。正常优化使用截止时间，遇到候选执行数量变化时必须结合日志说明。

若只修正开启分支，可以显式导入历史关闭图，避免再次消耗相同的完整预算：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B .\tools\compare_attachment_integration.py --cases data10 data12 data14 data15 --core-seeds --policies off passive --jobs 2 --compatible-off-report .\output\attachment-integration-comparison\passive-focus\comparison.json --off-compatibility-audit .\output\attachment-integration-comparison\analysis\off-compatibility.json --out-dir .\output\attachment-integration-comparison\passive-focus-v2
```

此方式要求：关闭相容审计描述的源码必须与当前冻结根引擎完全匹配；该输入的原始连接、种子、并发、全部预算和符号资源一致；保存线路再次通过完整几何、双 SVG 与精确重绘检查。报告保留旧运行指纹和旧耗时，明确标注“历史 off 复验”，新目录只写导入记录，不把它说成本次新跑，也不把无截止时间的 SA 状态一致扩大解释为完整限时路由相同。源码变动后必须先更新相容审计，否则导入会拒绝。

## 当前状态

对照工具已就绪，针对性工具回归通过。主流程接口稳定后，先跑重点样例，再决定是否进入完整批次。正式运行前不宣称附件已成功融入，也不将纯粹识别到关系当作图形改善。

关闭兼容审计已完成一次：从提交 `15fddca` 独立提取旧引擎，与本轮当前源码隔离快照比较。两侧均使用默认入口参数、种子 `20261029`、90 次迭代、不设置截止时间，不调用 A*。32 份输入中，29 份可解析输入的全部三个候选，共 87 个状态完全一致；其余 3 份输入的错误类型和错误文字一致。

比较包括器件位置、朝向、当前行、占位表、物理行列轴、水平偏移、固定约束、RNG 状态、估价指标、层级和端口连接顺序。耗时不参与相等判断。详细证据见 `output/attachment-integration-comparison/analysis/off-compatibility.json`，其中记录实际被测源码哈希和两份完整原始状态。

该结果证明被测快照关闭新策略后的初排与 SA 行为兼容；它不冒充完整布线、恢复链和按墙钟截止的最终出图逐字节一致证明。后续源码变化仍须在冻结版本重新验证。

## 第一轮重点样例实测

`output/attachment-integration-comparison/passive-focus/index.html` 保存了四个重点、同一基准种子的 `off/passive` 正常流程对照。8 次均完成，全部处于正常路由阶段；完整几何、保存重绘及输入／运行源码指纹检查通过，没有发生总线降级。

| 样例 | 线长 off → passive | 交叉 | 折角 | 完整内嵌成员 | 最终新模式候选 | 发布门槛 |
|---|---:|---:|---:|---:|---:|---|
| data10 | 2184 → 2116 | 2 → 1 | 22 → 20 | 0 → 4／4 | 3 | 本例通过 |
| data12 | 4366 → 4680 | 0 → 4 | 32 → 38 | 0 → 3／7 | 2 | 零交叉要求未通过 |
| data14 | 1904 → 1562 | 1 → 2 | 22 → 15 | 0 → 1／1 | 3 | 本例通过 |
| data15 | 2664 → 2551 | 0 → 1 | 15 → 16 | 0 → 0／1 | 1 | 交叉及结构违规未通过 |

总线长为 11118 → 10909，降低约 1.88%；总折角 91 → 89，但总交叉 3 → 8。**因此这一轮没有通过发布门槛，不能据此默认开启，也没有进入完整集合或 `all` 模式的验收。**

逐图看过 GT 和两侧成品后的结论：

- **data10：**原先散落在左侧的 C1/R1 已形成核心内部的横向串链，上部两只电阻也进入对应支路之间，组织明显改善。C2 的上方位置和偏左的 ISS 仍有后续空间。
- **data12：**上部电阻和部分中间电容更有组织，但 Rm/Cm/Lm 仍在右侧竖向分散，没有完成 GT 中完整 RLC 横向桥接；外围绕线和新增交叉不能忽略。
- **data14：**M2—L1—M1 的上下主支路更清楚，局部输入输出附件更紧凑；横向对齐及 L3 的电源侧位置仍不等同于 GT。
- **data15：**最终保留的是原算法候选 1，并非成功采用新的附件摆法。完整内嵌仍为零，结构违规从 3 增至 5；不能把线长下降归功于附件融入。

分析记录位于 `passive-focus/analysis/focus-summary.json`，可读表格位于 `focus-table.csv`，三栏视觉快照位于 `analysis/visual/`。保留这一批作为真实的失败与收益证据；后续修正须用独立目录重新对照，不覆盖它。

## 第二轮重点样例实测

第二轮修正了接地附件候选中的真实引脚方向：不再把信号固定出线放进自身电源标记。几何规则、A*、预算与缩线策略保持原值。冻结版本重新通过 32 输入／87 候选的关闭相容审计后，明确导入第一轮的四份关闭结果，并独立重新验收；只新跑四份 `passive` 完整流程。

结果位于 `output/attachment-integration-comparison/passive-focus-v2/index.html`。8 条记录全部合法，其中 **4 次新运行、4 次历史关闭版复验**，均为正常路由，没有恢复降级，运行源码与输入指纹未变。

| 样例 | 线长 off → passive | 交叉 | 折角 | 完整内嵌成员 | 结果 |
|---|---:|---:|---:|---:|---|
| data10 | 2184 → 2116 | 2 → 1 | 22 → 20 | 0 → 4／4 | 保留第一轮改善，本例门槛通过 |
| data12 | 4366 → 3660 | 0 → 1 | 32 → 22 | 0 → 5／7 | RLC／C0 完整进入核心，零交叉门槛仍未过 |
| data14 | 1904 → 1562 | 1 → 2 | 22 → 15 | 0 → 1／1 | 保留第一轮改善，本例门槛通过 |
| data15 | 2664 → 2551 | 0 → 1 | 15 → 16 | 0 → 0／1 | 仍选原算法候选 1，交叉及结构违规未过 |

data12 已完成本阶段关键功能验证：Rm、Cm、Lm 在两条有源支路之间形成横向串链，C0 保留其并联局部路径，Csw 位于下部输入对之间；结构、端口方向和有源顺序违规均为零。上部两只电阻仍在核心上方，没有把它们算进完整内嵌数量。

四图总线长 11118 → 9889，减少约 11.05%；折角 91 → 73；交叉 3 → 5。因此结论是：**第一阶段的通用内嵌功能已有真实可用结果，但发布门槛仍未全部通过，默认继续关闭。** 这不是完整集合验收，也没有把源器件 helper 测试当作 `all` 模式完整 flow 成功。

第二轮的 `analysis/focus-summary.json`、`focus-table.csv` 和 `analysis/visual/` 保留了实际候选、历史基线来源及四张 GT 对照快照。与第一轮相比，data10、data14、data15 的成品没有变化，不能重复计为新的收益。

随后补齐了一个保存测量问题：新增显示挂位的运行时属性需要以 `slot_contract` 保存，独立恢复后才能按相同语义重新统计；该修补只恢复测量上下文，不重新摆件或改线路。第二轮出图保留为此修补前的测试快照，不将未重跑的性能记录标成修补后新运行。

阶段一完整项目回归已通过 **1167 项测试，用时 157.043 秒**，日志和退出状态分别保存在 `output/attachment-integration-comparison/analysis/tests-passive-v2.log` 与 `tests-passive-v2.json`。这是实际完整测试结果；源器件 helper 的单独测试不等于 `all` 模式整图验证。发布开关继续保持 `off`。
