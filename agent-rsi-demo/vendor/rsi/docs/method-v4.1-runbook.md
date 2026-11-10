# method-v4.1 操作与验收

2026-11-09 更新。本手册对应 `feat/method-v4.1`，取代旧 v4 手册中有关校准、停机、保护指标和工作台启动的说明。方法依据为工作区的 `harness-opt/docs/method-v4.1.md`。离线验证不代表真实模型、服务或方法效果已通过验收。

## 入口与生命周期

工作台「新建迭代 → 自动优化」登记起点、输入、题池和 mode，创建 `split_mode=deferred` 的新迭代。已有手动迭代或旧 v4 状态不能转换；状态版本为 `v4-state-4`。

1. 填写优化时间预算、校准遍数（2–5，默认 3）、产品保护指标容忍量，以及可选 δ、模型配置与测量费用预算。主指标使用该输入的官方评分协议。
2. 选择校准执行并发（默认 2，范围 1–16；1 为串行），预览并再次确认。P 与初始 D 并行；P 首遍完成后，在执行上限内派发其余独立遍次。D 初始按约 40 题整篇抽取（不足按可用题池），已有登记样本保持不变；P 完成后根据失败率扩充 D，只补测新增论文。顾问最多等待 10 分钟，失败或无效建议回退规则值。
3. 阅读匿名校准图、预测区间、参数规则值／当前值／来源／理由／置信度。修改值后先预览影响，再保存；未保存的修改会阻止启动。保存失败时保留错误，不能假装已应用。
4. 预览划分、预算、终评授权和配置，再确认启动。服务端只接受尚未消费且未过期的确认令牌；操作者、参数、原始校准、已有配置或目标进程变化后必须重新预览。
5. 运行页分概览、轮次、校准、时间与花费、配置与账本。点击候选查看假设、实现者检查、首测／复测、机制和保护指标，再看结论。

关闭向导后迭代仍保留；重新打开 pending 迭代可继续。校准已启动时使用原输入和原操作者恢复，不能修改 k 或替换模型配置。校准完成后复用 P/D，不再次派发已完成的种子测量；冻结后的验证集其余题补测一遍。

暂停发送 SIGINT，停止本地轮询并保留远端工作以便续跑。终止发送 SIGTERM，取消在途执行、归档 Agent，记为 `stopped`；这是终态，`v4 run` 会拒绝续跑并提示 `v4 test`（未授权终评时不要执行）。页面上「暂停（可续跑）」与红色「终止运行（不可恢复）」分开，终止需输入「终止」确认。用命令行停进程时同理：只想停下用 SIGINT。已终止的实验要继续，按下文「复用校准新建迭代」处理。远端取消请求不等于远端已确认停止，仍需检查回执。控制台重启后按运行绑定、锁和进程身份重新发现运行；PID 的 boot ID 和 start ticks 必须匹配，发送信号通过 pidfd 固定目标。

## 配置来源

| 项目 | 来源与修改规则 |
|---|---|
| 起点、任务池、mode、官方主指标、sealed 选择 | 新迭代／已有输入协议；不让浏览器指定服务地址或源码路径 |
| `time_budget_hours`、`calibration_k`、`safeguards`、δ | 用户给出；k 不是顾问可调整参数 |
| `spend_budget`、`price_per_1k_tokens` | 用户可选；同一货币单位，未知价格或用量不补零 |
| Agent profiles | 工作台已登记配置；未选择时用服务器默认；顾问默认复用分析配置 |
| 验证题数、每轮候选数、并发、筛选策略、时限与重试 | 校准规则先计算，顾问可在白名单内建议，用户可覆盖；参数必须满足跨字段及整篇划分约束 |
| 保护噪声系数、复测等方法参数 | 方法默认值，偏离时记录变体；合并和消融默认关闭 |
| 服务地址、凭据、路径、执行器配置 | 服务器私有配置；不能从网页请求覆盖 |
| 最终运行配置 | `run-config.json` 冻结，附逐项来源和哈希；运行中只读 |

费用区间只估计后续测量（含验证集基线补测），不含此前校准及分析／实现 Agent。它不是整场运行的硬性总费用上限。实际总费用未汇总时页面显示未知。校准阶段和冻结后优化阶段各自从阶段启动计时，校准耗时没有从优化阶段预算中扣除；规划总投入时需额外计入校准与顾问时间。

## 命令行

先通过工作台或现有创建 API 建立 deferred 迭代。下列变量由操作者设置为自己的私有配置及状态目录，不把真实路径、地址或密钥提交到仓库：

```bash
# 输入 JSON 例子：不含服务地址或凭据。
# {"time_budget_hours":8,"calibration_k":3,
#  "safeguards":{"tokens_per_task_ratio":0.5,"answer_produced_rate":0.02}}
python -m rag_eval.optimizer.cli v4 calibrate \
  --config "$V41_SERVER_CONFIG" --state "$V41_WORKBENCH_STATE" \
  --iteration "$V41_ITERATION" --actor "$V41_ACTOR" --run-id "$V41_RUN_ID" \
  --state-dir "$V41_RUN_DIR" --v4-config "$V41_INPUT_JSON"

python -m rag_eval.optimizer.cli v4 proposal show --state-dir "$V41_RUN_DIR"
python -m rag_eval.optimizer.cli v4 proposal set 'max_concurrent_executions=2' --state-dir "$V41_RUN_DIR"

python -m rag_eval.optimizer.cli v4 run \
  --config "$V41_SERVER_CONFIG" --state "$V41_WORKBENCH_STATE" \
  --iteration "$V41_ITERATION" --actor "$V41_ACTOR" --run-id "$V41_RUN_ID" \
  --state-dir "$V41_RUN_DIR" --from-proposal "$V41_RUN_DIR/proposal.json" --no-test-set

python -m rag_eval.optimizer.cli v4 status --state-dir "$V41_RUN_DIR"
```

校准可选 `--calibration-concurrency 2`（优先于配置文件顶层 `calibration_concurrency`）、`--no-advisor`、`--history <ledger.json>`（可重复），以及 `--spend-budget`／`--price-per-1k-tokens`。恢复校准重复原命令和并发值；旧交付中没有调度记录的校准继续串行恢复，不迁移在途身份。`calibrate` **不支持 `--retry-failed`**；恢复优化的 `run --from-proposal` 才支持该参数。不能编辑冻结文件绕过身份检查。已有失败校准延长新执行时限的方法见下文。

校准并发与建议书中的优化执行并发是两个参数，均为单次运行的执行上限，不是服务器总并发或模型请求上限。提高并发会叠加 runner 内部并发；没有新增自动容量探测；上限不是保证占满的槽位数，实际并行受剩余 P 遍次与 D 批次数约束。校准状态轮询间隔不超过 5 秒，已有更短设置保留。页面展示完成遍数／题数和测量调度累计用时（不含顾问、停机时间；硬中断可能丢失未落盘时段），各执行耗时可能重叠，不能直接相加；耗时预测仍受并发争用影响。

工作台启动要求配置由真实配置文件加载（服务器保存可信 `_config_path`），同一迭代只能绑定一个自动运行。校准／运行会产生真实调用，本文只说明操作，不构成运行授权。

## 运行工件

| 文件 | 作用 |
|---|---|
| `calibration.json` | P/D 身份、阶段结果、全 k 方差、实际计时依据、未知量与假设、内容哈希 |
| `calibration-recovery.json` | 绑定原校准身份的恢复时限；只用于新执行步骤，不增加重试次数 |
| `proposal.json` | 规则／顾问／用户值、范围、理由、置信度、simulation ID |
| `proposal.agent.json`、`advisor-intent.json` | 顾问产出和固定截止时间；迟到产出不覆盖回退决策 |
| `run-config.json`、`resolved-config.json`、`cli-run.json` | 不可变划分／配置及运行身份；冻结中断可以幂等恢复 |
| `baseline-remainder.json` | 冻结后 V\\P 的基线补测，用于恢复时复用 |
| `state.json`、`ledger.json` | 轮次、测量／复测、判定、人工审查标记、预测、时间账及取消回执 |
| `confirmation.json`、`launch.json`、`process.json` | 一次性确认、原始启动输入、拥有的进程身份 |
| `startup-error.json` | 启动预检失败的安全错误码及运行身份；未启动或旧状态也能显示，不改冻结配置；新预检通过或新启动前清除 |
| `process.log`、`run.lock`、`control.lock` | 子进程日志、运行独占、操作串行化 |

这些目录可能包含内部诊断和案例映射，不应整体发布。公开页面投影使用计数与匿名行号；缺分保持缺失，不能变成零分。

## 已知限制与服务器验收

- P/D 并行及可配置校准并发已实现；可选并发扩展探测尚未实现，优化并发建议缺实测时仍标为假设。实际提速和服务器资源承载尚待同缓存条件下的比较。
- 基础设施重试使用现有整作用域执行接口，健康题可能物理重跑，但先前健康分数保留，重试不计为独立复测样本。
- 固定开销／每题耗时不可识别时明确采用零开销假设；无缓存回执时冷／热值未知。
- 浏览器工具和 brief 的可见范围不是操作系统级文件沙箱。
- 当前没有真实服务器模型／顾问演练证据，预测准确性与模型稳定性尚待验证。
- 校准和候选图属于验证过程观察，不证明解决了验证集过拟合。跨节点累计 Δ 是估计；正式终点评测另行授权。

首次服务器演练使用新 deferred 迭代与 `--no-test-set`。逐项核对冻结值与实际调用一致、P/D 复用、复测独立尝试、保护指标配对与噪声门槛、暂停／续跑／停止回执、预测与实际耗时。服务器 agent 只拉取、部署、运行、报告，不现场改代码。当前 98 道 sealed 题需先做访问历史审计，不能默认用作终评。

## 本地资源预算

WSL 约 7.8 GiB。所有 pytest、前端构建、类型检查、浏览器验证共用 `/tmp/v41-pytest.lock`，每次只跑一批；拿锁后 `MemAvailable` 至少 2.5 GiB。pytest 使用 `-n 1`，BLAS／OMP 线程均为 1；Node 堆上限 1536 MiB、Expo `--max-workers 1`。每批限定超时，不跑 SN 全量测试，不终止无关进程。测试证据见 `method-v4.1-progress.md` 和本地交付包。

## Agent 模型配置预检（2026-11-06 增量）

`adapter.analyst_profile` 和 `adapter.implementer_profile` 的内置缺省仍为 `pi-flash`；`reviewer_profile`、`calibrator_profile` 未指定时继承分析配置。内置值不保证在服务器白名单中。向导“服务器默认”不代表自动挑选任意可用模型：预览会校验四个角色的最终 profile，失效时在校准执行前报错；用户应显式选择已登记的分析／实现配置，或由管理员设置可信 `optimizer_v4.adapter` 默认值。顾问／审查可另选或继承分析配置。

确认页显示实际 profile ID，确认令牌绑定所选 profile 定义及远端 workspaceId。子进程加载磁盘配置后还会校验批准的摘要，防止工作台内存配置与启动时文件不一致；失配时在测量前停止，需加载正确服务器配置后重新预览。此检查仅验证配置授权与一致性，不证明 bridge、凭据或模型服务健康。

CLI `calibrate`（启用顾问时）与 `run --from-proposal` 同样执行角色配置预检；显式 `--no-advisor` 可做纯校准测量。顾问回退新增 `stage` 和 `error_type`；白名单失配使用稳定错误码 `agent_profile_not_allowed` 与可操作说明。其他异常不回显任意原始消息，以免泄漏案例或服务凭据。

旧 fallback 工件不重写、不删除、不自动重试。已完成校准的模型配置身份仍不可修改；需要不同模型配置时按新迭代流程处理，真实重跑须用户授权。不要编辑 calibration.json／proposal.json 绕过恢复约束。

## 部分完成后的校准恢复（2026-11-07）

校准阶段总预算与单个 execution 时限同时生效。默认每个 execution 7200 秒，包含该批次所有分区的排队／准备／生成／评分；内部预留 `max(300秒, 10%时限)` 收尾（极短时限最多预留一半），因此默认分区调度截止点为6480秒。分区还受自身时限与剩余批次时间的较小值约束。48小时阶段预算不会自动放宽2小时的 execution 时限。

新校准向导可设置每批执行时限并在确认页查看。失败校准保留原题单、k、并发、adapter身份及已派发预算；在“恢复时延长执行时限”填写更长秒数并预览确认，或对**原 calibrate 命令**追加：

```bash
--recovery-execution-timeout-seconds 21600
```

21600秒是此次40分区现场可采用的保守起点，并非容量保证。根据实际服务吞吐调整，不能把P/D并行时间重复相加。恢复参数必须大于原时限且不超过86400秒；首次在运行锁内写入 `calibration-recovery.json`，绑定原校准身份，之后不可改写。后续重复原命令即可读取该记录。它只改变**之后新建步骤**（恢复重试和剩余P独立遍次）的预算；已登记／已派发步骤按原预算恢复。它不增加重试额度，不重置独立测量计数。未使用的既有infra_retry可继续执行；耗尽仍有截止缺口时保持未完成，不能靠反复点击无限重试。

此次首波观察到的8个并行分区不等于后续峰值：原校准上限8指execution数，每个execution内部parallel=4。k=5时，P首遍完成后剩余4遍可同时派发；若D仍在运行，可有5个execution、最多20个分区重叠。实际并发与吞吐需在服务器核验；恢复保持原并发身份，不在旧工件中直接把8改成4。6小时是每次新执行的上限，不是整段校准的预计完成时间。

截止证据分为批次未启动、分区被截止杀掉、runner记录的partition_deadline。已有官方工件仍须完整校验；健康首测分数保留，截止空槽由同一逻辑尝试的重试补齐。ALCE会对错误／缺失答案计算数值0：有明确截止证据时，此数字保存在原始评分与诊断中，不作为健康模型表现，不把缺题计为0或缩小题单。正常作答／no_answer的有效官方0分仍保留。取消、未知缺工件、评分损坏不能按截止重试绕过校验。

恢复目前沿用整scope执行接口，**健康题可能物理重跑，但新分数不覆盖其首个有效分数**。准备缓存能减少这部分成本，实际命中依赖完整内容键。`preparation-cache.json`顶层`miss`只表示读取时未命中；应看`store.status=stored/already_cached/store_failed`及缓存条目，不能据miss断言未发布。

支持边界：此次`failed/finished_with_errors`有逐分区截止证据，可按上述路径恢复。外层监督器硬超时且`run_state=None`、没有可信逐分区证据时，仍拒绝推断缺失原因；需保留工件进一步处理。本修复不自动补ALCE引用模型评分，不操作封存集，不自动启动服务器运行。


## Agent 创建拒绝与当前分析恢复（2026-11-08）

`not_attempted / SCOPE_MISMATCH` 表示创建前目录校验拒绝；原请求键会继续读取旧拒绝，单独修配置不能使它重新创建。新入口只允许恢复当前轮尚未保存分析卡的 analyst 请求：持原 run 锁，核对原运行／任务／操作者与请求归属，按当前部署授权准备唯一新回执，保留原回执及其错误。预览和确认均不创建 Agent；普通续跑才派发。`unknown`、已有会话、已创建、`AGENT_ALREADY_EXISTS` 等不允许通过此入口重发，需另行核查远端事实。

```bash
# V41_FAILED_INTENT 必须是本轮 analyst 的完整 intent ID，不能用旧 calibrator ID。
python -m rag_eval.optimizer.cli v4 recover-agent-creation \
  --config "$V41_SERVER_CONFIG" --state "$V41_WORKBENCH_STATE" \
  --iteration "$V41_ITERATION" --actor "$V41_ACTOR" --run-id "$V41_RUN_ID" \
  --state-dir "$V41_RUN_DIR" --intent-id "$V41_FAILED_INTENT"
# 确认只准备唯一后继；重复同一确认返回已有后继。
python -m rag_eval.optimizer.cli v4 recover-agent-creation \
  --config "$V41_SERVER_CONFIG" --state "$V41_WORKBENCH_STATE" \
  --iteration "$V41_ITERATION" --actor "$V41_ACTOR" --run-id "$V41_RUN_ID" \
  --state-dir "$V41_RUN_DIR" --intent-id "$V41_FAILED_INTENT" --confirm
# 随后按原 run --from-proposal 命令续跑，或网页重新预览续跑。
# 不加 --retry-failed，不重跑校准，不改冻结参数或已完成基线。
```

恢复 CLI 不迁移目录、不创建远端 workspace。部署必须先满足真实目录关系：DB 中 task.workspace_id 对应 location、服务器 config.workspace.location 与远端 workspace canonical directory 一致。创建命令 expectedCwd 从 DB 工作区记录读取，配置加载时绑定该本地工作区；不能只给手工连通性测试替换 expectedCwd。`scripts/sn-eval` 从该目录自身的 src 加载代码，所以该目录也必须部署本次版本。

如果错误引用了别的工作台共享的远端 workspace，不应改写共享 workspace 的目录。可保留原本地评测目录、task／iteration／state 身份，在确认此目录仅归目标工作台使用后，将它升级到交付 SHA，为同一目录登记专用远端 workspace，更新目标工作台私有 config 的 agent_creation.workspaceId。远端 ID 是部署绑定，不是 P/D 或优化参数；新后继按新绑定准备，旧创建记录保持旧绑定。只重启目标工作台加载配置，重新预览确认；原共享工作区、其他服务和旧回执不动。目录被其他服务使用时，先另拟迁移路径，不直接改 DB 身份或覆盖共享代码。

每次部署均须串行构建 apps/sn-agent-bridge（npm ci、npm run build，确认 dist/cli.js）与 apps/sn-workbench（npm ci、npm run typecheck、npm run build -- --max-workers 1）。dist 不在 Git 内，检出新 SHA 不代表构建产物存在。协调者、Agent brief 中的 sn-eval 路径及其实际 Python 导入都应指向本次 SHA 和原绝对 state；不要把新 checkout 下的同名相对目录当成旧状态。

预检在 HTTP 预览、确认与 CLI 实际启动时检查角色授权、bridge 构建及真实目录映射，最多分页检查远端工作区、总预算 30 秒。这只是当时部署快照，不证明模型回复、实现候选的 worktree 注册或未来稳定性，实际创建前仍校验。运行中的请求拒绝会保存 agent_request 故障及安全错误码／intent ID；网页不会为这类失败旋转测量键。网页确认后子进程预检失败会写 startup-error.json，并在停止状态显示原因，即使尚无 state.json；完成的校准和基线不变。

## 复用校准新建迭代与分析卡格式（2026-11-09）

**卡片格式。** 自动运行（task 绑定 method-v3/v4 运行）的分析卡一律按 lcd-v2 机制合同校验（`slice_predicate`／`manipulation`／`slice_effect`，`predictions` 可为 `[]`），与 brief 一致；不再读取迭代冻结的 `gate_rule`。此前「新建迭代 → 自动优化」未传 gate_rule，迭代被冻结为默认 lcd-v1，按 brief 写的卡全部被拒。新建自动迭代现记录 `protocol.method=method-v4.1` 与 RULE_V2；旧迭代记录不改，但校验同样走 lcd-v2。`proposal draft` 先做格式校验（立即返回 `invalid_card`），再做泄漏扫描；每次草稿结果（只记结果类别）在运行页当前轮显示。

**Agent 工具命令。** brief 中的 `sn-eval` 命令带 `env SN_WORKBENCH_PYTHON=<服务进程解释器>`，Agent 不再依赖继承环境或评测目录下的 `.venv`。

**拒绝原因。** Agent 创建被存储层拒绝时，`failure_detail` 为 `<角色> <代码> (<类型>): <原因> intent_id=…`；已知代码如 `CANDIDATE_NOT_EVALUATED`、`PROFILE_UNBOUND`、`REQUEST_KEY_CONFLICT`，其余为 `AGENT_REQUEST_REFUSED`。原因限 240 字，绝对路径只保留末段，形如 token/key 的值被遮蔽。

**过期证据。** 若某轮分析证据已落盘但从未有 analyst 创建意图（派发被拒或中断），而历史随后改变，新内容另存 `round-<轮>-<父>-<摘要>.md` 并使用新请求键；旧文件不动。已派发过的分析仍复用原文件。

**复用校准新建迭代。** 条件：同起点 commit、输入／bundle、mode、执行与准备策略、case_retry、题池、划分种子与 sealed、k、Agent 设置，且操作者与原校准相同；原校准完整、原运行目录保留（新迭代通过 `candidate_links` 引用原 root 的执行，原目录不可删除）。

1. 网页新建自动迭代，题单与原迭代完全相同。资料准备缓存按内容自动命中，无需操作。
2. 向导「复用已有校准」列出兼容来源；预览显示全部身份核对、挂接执行数、P/D 与剩余基线题数、用户改过的建议项。确认不调用模型、不产生费用。
3. 导入后在建议书页核对带入的值（用户修改保持 `user`，顾问说明保留，不重跑顾问），再预览冻结并启动。冻结后验证集剩余题与原来完全相同时复用原剩余基线，否则补测差集。

命令行等价操作（预览不写任何文件）：

```bash
python -m rag_eval.optimizer.cli v4 import-calibration \
  --config "$V41_SERVER_CONFIG" --state "$V41_WORKBENCH_STATE" \
  --iteration "$NEW_ITERATION" --actor "$V41_ACTOR" --from-iteration "$OLD_ITERATION"
# 全部 ok 后，用输出的 plan_sha256 确认；可重复执行，结果收敛。
python -m rag_eval.optimizer.cli v4 import-calibration \
  --config "$V41_SERVER_CONFIG" --state "$V41_WORKBENCH_STATE" \
  --iteration "$NEW_ITERATION" --actor "$V41_ACTOR" --from-iteration "$OLD_ITERATION" \
  --confirm "$PLAN_SHA256"
```

`--state` 是工作台 state 目录（其中的 `workbench.sqlite` 由代码定位），不是数据库文件。默认运行目录为 `<state>/automatic-v41/<sha256(新迭代)[:24]>`，与网页一致。导入先写 `calibration-import.json` 意图，再登记 P/D、绑定、挂接执行、写建议书与校准；中途失败时校准和启动均被拒绝，重复同一确认即可补完。原迭代、原运行目录与原回执只读。导入后种子不再测 P/D；每个新运行仍会在 smoke 样本上测一次种子，这是正常轮次开销。

