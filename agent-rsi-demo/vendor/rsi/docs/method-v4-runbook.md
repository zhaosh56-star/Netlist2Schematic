# method-v4 运行手册与实现记录

> v4.1 的校准、停机、保护指标与工作台操作已变化，请使用 [method-v4.1 运行手册](method-v4.1-runbook.md)。本文件保留旧 v4 的历史说明，不用于启动新的 v4.1 运行。

分支 `feat/method-v4`（从 `method-v3-clean` 拉出）。入口：`python -m rag_eval.optimizer.cli v4 <run|status|test|figure>`，源码在 `src/rag_eval/optimizer/v4/`。
方法文档：`harness-opt/docs/method-v4.md`（草稿 v1）；绑定方案：`harness-opt/docs/method-v4-sn-binding-plan.md`。
分工：本地修改、离线验证和审查；推送、服务器部署、真实模型演练及正式运行另需用户授权。服务器 Agent 按已授权范围拉取已审查提交、部署、运行和汇报。本次运行时补全仍是本地未提交改动，不能把当前 HEAD 当成交付版本。

---

## 1. 一次运行的流程

1. **校准**：种子搜索诊断只测确定性样本（默认 `diagnose_size=40`，不足则全取，`diagnose_k=1`）；同一父版本、样本和 k 的诊断结果复用于首轮分析，避免再测一次。种子小批和其余验证题分别跑 `calibration_k=2`，与诊断的 k 分别登记，无需等于迭代的 `k_base`。三项独立调度，实际执行仍受全局执行上限约束。由验证集 A/A 得出重跑方差、任务方差、恒定题比例、`k_val` 和 SE 下限。可行性卡片只是基于已观测耗时的提示，冷/热估计不构成服务器提速证据。
2. **每一轮**（当前父版本固定）：
   - 每个父版本按版本哈希确定搜索样本、小批和其余验证题；分析 Agent 可见选中的搜索案例及搜索分数，历史保留跨父版本标签，旧父版本有标记；看不到标准答案、验证或测试案例及分数。
   - 实现、测量和审查各有独立工作池，每池最多 `b` 个任务，空位补任务。实现槽位不再被慢测量占据；测量请求数与实际执行数分别受 `adapter.max_concurrent_measurements`、`max_concurrent_executions` 约束。后者统计本次优化持有的实际执行，直到收到终态回执；它不限制机器上的其他运行。
   - **阶段 0 冒烟**：默认搜索集 3 题、k=1、父子配对，检查能否产生轨迹；`smoke_behaviour_check=false` 时不做行为门槛，也不据此修复。显式开启后，行为预期不满足可给一次修复。
   - **阶段 1 小批**：按原父版本的小批测候选 k=1，与父版本缓存配对；Δ_mb < −`minibatch_kill`·SE_mb 或保护指标越界就淘汰。
   - **阶段 2 全验证集**：只补测其余题 k=`k_val`，合并小批计算 Δ_val，保护指标耗时单列为 `safeguards_s`。
   - **阶段 3 机制检查**：连最低门槛 `c_mech` 都不能通过，或保护指标否决时，省略审查 Agent；已有轨迹仍可做本地机械检查以反馈行为是否改变。仍可能采纳的候选进入机械检查和独立盲审，审查仍可否决刷分。证据按结构字段比较，保留全部案例对和 A/B 两侧，值截断有显式标记。
   - 有合格候选后可预取下一父版本的诊断；两名合格候选出现后可提前调度合并和测量，不必等全部卡片结束。合并仍须通过验证和保护指标，轮末按比较结果决定。
   - 推测性分析绑定**同一父版本和确切目标轮号**，输入包含当时已完成的结果，只把仍在处理或等待决定的卡片标为进行中。身份不匹配就丢弃，不可复用到新父版本；因此可能出现最终未使用的分析 Agent。
   - 默认 3 小时的**轮时间盒**到期即停止该轮继续启动卡片；未完成卡片保留来源父版本、划分、阶段和请求身份，带入下一轮。它们先在原父版本下完成资格判断；只有合格后才向已改变的当前父版本 rebase，再重新冒烟、小批、全验证和审查。旧父版本分数不能直接推进当前指针。
3. **失败和停机**：单个候选的运行/轮询故障记作 `run_failed`，其他候选继续；默认连续 3 次运行故障才停为 `process_failure`，成功完成候选重置计数。预算耗尽、最大轮数、连续无采纳且无待接续卡片、或剩余时间不足平均一轮也可停机。`process_failure` / `budget_exhausted` 的显式重试保留现有请求身份，重试入口见命令和日志提示。
4. **消融剪枝**：在剩余优化预算内，逐个构造“去掉一个谱系节点”的版本（rebase 构造串行，避免工作树依赖冲突），构造后按 `ablation_parallel=3` 并行评测。剪枝建议汇总后再构造并测最终版本，不变差才用作终点。预算不足则跳过并记录原因；执行仍受同一实际执行上限约束。
5. **终点**（独立授权）：`v4 test` 只预览；用户另行明确授权后，`v4 test --confirm --budget F` 才对 sealed 测试选择评测一次，得出 Δ_test、按论文 bootstrap 区间、缩水量和部署建议。`v4 figure` 生成图。

运行时以优化总墙钟预算为截止时间（终点评测除外），不再因为固定的长评测超时继续无限轮询。Agent、工作树、测量槽位与执行准入的等待都检查剩余预算；单次桥接调用或外部进程是否立即响应取消仍取决于服务。`execution_timeout_seconds` 是执行服务的单次执行超时，`eval_timeout` 是适配器单次等待配置，均不等同于优化总预算。

优化完成或总预算截止会请求停止本次持有的执行、归档已知 Agent，并在 `sessions[].cancellation` 保存回执。`stop_requested` / `archive` 不等于远端停止已确认；`stop_confirmed=false` / `remote_stop_confirmed=false` 和未知/不可取消状态必须如实回报。可续跑的 `process_failure` 暂停保留远端执行和 Agent，只用 `stop_polling` 停止本地轮询；避免把仍需恢复的 Agent 归档。异常的 `BaseException` 中断也走 `stop_polling`，停止本地工作线程轮询、保留持久执行和 Agent 身份供续跑；真正强杀来不及清理时也依赖请求键回放，不能假定远端任务已经消失。

## Agent 诊断与实现反馈（2026-11-05）

- 分析 Agent 可用 `source --path <路径> [--query <字面文本>] [--start N --lines N]` 读取绑定父候选的冻结 Git 提交。只开放产品源码／测试目录中的普通 UTF-8 源文件；拒绝路径穿越、符号链接、数据和配置等路径。每次最多读取 1 MiB blob，返回最多 200 行、少于 32 KiB；省略有标记。工具只读且绑定任务/父节点，这不是对任意 shell 的文件系统沙箱保证。
- 卡片 `mechanism` 区分观测与因果猜测，`falsification` 写最便宜的判别检查；可在 `search_refs` 中引用本轮实际显示的 `c1` 等案例，源码假设可用空列表。实现者仅接收所引用案例。未提供字段的旧卡明确标注整轮上下文回退，不虚构引用。正文与映射先保存为同一快照，中断恢复不使用后来输入重新解释引用。
- 实现者先做便宜的源码、局部测试或 probe 检查，再决定实现或跳过。不强制每卡新增测试；证据不足时可跳过，也可明确记录不确定性后进行有依据的有限实验。无需再启动一个 Agent。
- 必须写入消息指定的检查报告：候选／卡片／父版本身份、`decision=implement|skip`、`reason=supported|refuted|already_satisfied|inconclusive`、简述与 1–32 条实际检查。报告有体积限制，缺失、跨候选或畸形报告明确失败；协调器不替 Agent 猜结论。有效 `skip` 记为 `preflight_skipped`，不冻结或测量本次实现。修复跳过保留上一尝试历史，页面指向本次修复候选。
- 报告来自实现者，不能据此宣称因果已独立证实。未测量阶段的检查结论可返回分析 Agent；验证反馈仅返回固定类别：行为改变但收益不足、未观测到预期变化、机制未知、运行无有效证据、保护指标阻止等，不返回验证数值、案例或审查者原文。实现/运行失败和检查不确定不等于假设已被推翻。
- 当前父版本的已采纳改动从谱系生成独立清单，不被旧历史的长度限制删除。旧尝试仍可按预算截断；投机分析使用其启动时已知的反馈，不保证看到后来才完成的结果。

新自动运行登记 `method-v4`，只向 V4 分析角色开放 source 命令；旧登记缺少方法字段时按 V3 处理，不静默升级。升级前已开始的运行、旧 Agent 提示及缺失检查报告不自动迁移，请新建迭代/run。当前工作不启动真实模型或服务器评测；真实完成效率仍待演练验证。

## 2. 实现与方法文档的差异（也写在 `ledger.json` 的 `deviations` 里）

- 划分沿用 v2：搜索/验证按 60/40 切，比例硬编码；测试集就是迭代登记的 sealed 选择，不是方法文档默认的 40/30/30。
- 机械检查的 A/A 差距、审查者的对照对，都用**种子**的校准 A/A，不用当前父版本的 A/A（被采纳的父版本在小批上只跑过一次）。
- 消融按谱系节点逐个去掉；合并节点（两张卡）作为一个整体去掉。
- 回传给分析 Agent 的验证反馈只有机制档位和固定的结果类别，没有验证案例、分数或审查者的文字理由。实现前的源码／本地检查报告可提供简短文字，明确标注为实现者报告。

方法里写了、已经实现的部分：
- 小批按父版本重新抽取（抽样种子由父版本哈希决定），按论文分层；
- SE 按论文聚类（CR1），并取 A/A 下限；
- 终点区间用按论文的 bootstrap；
- 消融剪枝；
- 每个父版本的确定性搜索诊断样本、校准复用、后台诊断预取与有身份约束的推测分析；
- 可行性卡片分冷、热缓存两列。

## 3. 配置文件（预注册，运行中只读）

一个 JSON。未知键一律拒绝。首次 `run` 把原文副本（`v4-config.json`）、sha256 和解析后的全部取值写入 run 目录的 `cli-run.json`，之后字节有任何变化都拒绝续跑。方法常数改了默认值的，报告里列为"方法变体"。

| 键 | 默认 | 说明 |
|---|---|---|
| `time_budget_hours` | **必填** | 优化部分的墙钟预算，不含终点 |
| `min_meaningful_improvement` | null | 只用于可行性卡片对照 |
| `b` / `cards_per_analysis` | 4 / 2b | 并行实现数 / 每次分析的卡片数 |
| `smoke_size` / `smoke_repair_attempts` / `smoke_behaviour_check` | 3 / 1 / false | 默认只检查运行，行为门槛需显式开启 |
| `minibatch_fraction` / `minibatch_min` / `minibatch_kill` | 0.25 / 10 / 1 | |
| `c` / `c_mech` | 1 / 0.5 | 机制未确认 / 已确认时的采纳门槛系数 |
| `review` / `review_pairs` / `review_controls` / `review_min_effect` / `review_max_control_effect` | true / 6 / 2 / 4 / 1 | |
| `no_adoption_rounds` / `max_rounds` / `round_timebox_hours` | 3 / 50 / 3.0 | 时间盒单位为小时；null 关闭轮时间盒 |
| `max_consecutive_run_failures` | 3 | 连续运行故障停机阈值 |
| `max_concurrent_executions` | 3 | 本次优化实际执行上限，null 仍取 3 |
| `diagnose_size` / `diagnose_k` | 40 / 1 | 0 表示整个搜索集；样本由父版本确定 |
| `speculative_prefetches` / `speculative_analysis` | 2 / true | 每轮诊断预取数 / 下一轮推测分析 |
| `merge` / `tie_fraction` | true / 0.5 | |
| `ablation` / `ablation_kill` / `ablation_parallel` | true / 1 / 3 | 构造串行，评测并行 |
| `calibration_k` / `k_val` | 2 / null（由校准决定） | |
| `safeguard_tolerances` | 基础设施失败率 0.05，每题成本 0.5 | 其他保护指标用迭代协议里登记的 margin |
| `unobserved_safeguard_blocks` | true | 保护指标未观测到时不采纳（不会因此在小批阶段淘汰） |
| `endpoint_nodes` | `seed_final` | 或 `lineage`：测谱系所有节点 |
| `analyst_failure_threshold` / `analyst_max_failures` / `analyst_n_controls` | 0.5 / 12 / 3 | |
| `agent_estimate_minutes` | 45 | 可行性卡片里的 Agent 时间 |
| `adapter.*` | 见 `v4/config.py` 的 `ADAPTER_DEFAULTS` | 分析/实现/审查 Agent 的 profile（审查者默认用分析 Agent 的 profile）、轮询间隔、超时、证据预算、`infra_retry`、`execution_timeout_seconds` |

正式 R4 交付模板显式设置 `b=3`、`max_concurrent_executions=3`、`diagnose_size=40`、`diagnose_k=1`、`round_timebox_hours=3`、`smoke_behaviour_check=false`；这是运行选择，不把类默认的 `b=4` 改写为 3。

## 4. 对迭代的要求（`v4 run` 启动前检查）

- `calibration_k` 默认 2，用于验证集 A/A；无需等于迭代的 `k_base`；
- 迭代协议里登记了保护指标（lcd-v2）；
- 验证集至少 2 题（方法要求至少 40 题，不足时可行性卡片会警告）；
- sealed 选择存在，并且从未被声明过：本迭代和其他任何迭代都没有对同一组 (bundle, sealed 题目) 声明过。不满足就拒绝启动；演练用 `--no-test-set`；
- `fresh_batch_count` 建议为 0，v4 不使用新鲜批次。

## 5. 命令

```bash
V4="$PY -m rag_eval.optimizer.cli v4"
COMMON="--config $CFG --state $STATE --iteration $IT --actor $ACTOR"
$V4 run    $COMMON --run-id $RUN --state-dir var/method-v4/$RUN --v4-config run.json   # 启动或续跑；--retry-failed；--no-test-set
$V4 status --state-dir var/method-v4/$RUN
$V4 test   $COMMON --state-dir var/method-v4/$RUN                                     # 只预览，不声明测试集
$V4 test   $COMMON --state-dir var/method-v4/$RUN --confirm --budget budget.json      # 需用户同意；只能做一次
$V4 figure --state-dir var/method-v4/$RUN --out curve.svg
```

退出码：0 成功；2 参数或校验失败，或 `test` 未加 `--confirm`；3 停在 `process_failure` / `budget_exhausted`；1 意外错误。

## 6. run 目录

| 文件 | 内容 |
|---|---|
| `cli-run.json` | 运行身份，`method: "v4"`，配置哈希和解析后取值 |
| `state.json` | `schema_version: "v4-state-3"`。完整状态：划分、论文映射、校准、谱系、轮次、每个候选的记录、历史标签、消融、时间。续跑从这里恢复 |
| `ledger.json` | 账本：配置、方法变体、偏离、校准与可行性卡片、所有轮次、谱系、消融、时间账（含按冷/热缓存拆分的阶段耗时）、停机原因 |
| `endpoint.json` | 终点报告（只在 `test --confirm` 之后才有） |
| `evidence/` | 分析正文及 `.cases.json` 原始文本／引用映射快照、每卡引用案例、完整已采纳清单、`preflight-*.json` 实现检查报告及 `.accepted.json` 接受快照；审查者的证据文件（`review-*.md`）和判定文件（`*.verdict.json`）；冒烟修复的反馈文件 |
| `run.lock` | 运行锁：被锁住就说明进程还活着 |

仅支持同 schema 和同配置身份续跑。旧 `v4-state-2` 状态不迁移，必须新建 run；不要修改旧状态的 schema 标签冒充兼容。

## 7. 工作台

绑定了 v4 运行的迭代，在工作台上打开的是 v4 页面（`apps/sn-workbench/src/auto/V4RunPage.tsx`），数据来自 `GET /api/iterations/{id}/auto-run`，由 `optimizer/v4/run_view.py` 投影。页面内容：

- 状态和下一步；
- 进度：时间、轮数、采纳数、划分大小、流程故障计数；
- 终点；
- 校准与可行性卡片；
- 谱系（标出被剪除的节点）；
- 每一轮：每个候选到达的阶段、Δ、机制档位、标签，轮末决定，合并结果；
- 消融。

右栏点开一个候选，可以看到：级联各阶段的数字、SE 的来源、采纳门槛、机械检查、审查者每对的判定（标出对照对）、保护指标、耗时与缓存命中、相关 Agent，以及 diff；另展示实现前检查的决定、依据和具体检查，标题明确标注“实现者报告”。

时间账里的 `total_wall_s` 是总墙钟；`candidate_sums_s` 是各候选阶段用时之和，包含重叠，不能直接当总运行时间相加。保护指标单列；冷/热/未知依据实际准备缓存回执，不从文件名或答案提示词修改猜测。准备来源清单保留 `::symbol`，符号范围不确定时 `touches_preparation=null`。

页面只读，不提供任何写操作，手动操作仍被 `auto_runs` 锁住。审查者 Agent 在会话列表里显示为"审查"。

## 8. 测试

```bash
PYTHONPATH=src:tests:. ../../.venv/bin/python -m pytest tests/optimizer/ -n 4
cd apps/sn-workbench && npx tsc --noEmit
# 浏览器：先启动 fixture 服务器，再跑浏览器测试（需要能运行的 Chromium）
PYTHONPATH=src:. python apps/sn-workbench/tests/auto_run_fixture.py --port 8097 --root /tmp/auto-fixture
WORKBENCH_TEST_URL=http://127.0.0.1:8097 node tests/auto-run-browser.mjs
```

- `test_v4_loop.py`：在确定性 mock 上测循环，覆盖级联、合并、两档门槛、对照失败、刷分否决、修复、保护指标、kill 后续跑、停机、按父版本抽小批、聚类 SE、消融。
- `test_v4_rehearsal.py`：在真实 v2 服务上跑完整流程，用合成分数，覆盖采纳、盲审、测试集只评一次、刷分否决、阶段 2 中 kill 后续跑、工作台接口。
- `test_v4_units.py`：测配置、判定文件解析、终点报告、节点集和图。

服务器演练另需授权，尚未开展；本地合成服务通过只能证明被测条件，不能推断实际速度。演练验收至少包括：同一父版本诊断无重复 scope；实际在途执行不超过 3；时间盒接续先原父资格再 rebase 重测；取消回执和远端停止的不确定性；中断前后请求键/Agent 身份无重复；总墙钟、阶段和保护指标耗时一致；工作台主页面及候选详情截图。推测性分析产生的不同父版本/轮号 Agent 应单独标明，不算同身份重复。

### Isolated deployment and Agent configuration

When the server loads a configuration file, generated Agent tool commands pin both `--state` and `--config` to that instance. This prevents an Agent launched by an older Paseo daemon environment from resolving the new state with the old workbench configuration. Existing saved message snapshots retain their original command; start a new run for the new deployment. Programmatic configurations without a source file retain the legacy tool default.

2026-11-05 delivery verification: 550 optimizer/workbench tests passed; after the explicit config binding fix, 104 affected configuration/context/Agent/rehearsal tests passed. Frontend typecheck/build passed. These are offline checks, not evidence of completed server execution.
