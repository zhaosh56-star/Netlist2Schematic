# auto-canvas 原生项目适配

这是第一阶段的离线接口：输入 auto-canvas 的 `.icproj.json`，调用 Netlist2Schematic，再输出可继续编辑的 `.icproj.json` 和原生 SVG。网页按钮尚未接入。

## 代码放在哪里

- 算法主线：`main`，本轮基线 `19741f0`。
- 适配分支：`adapt/auto-canvas-huyangc`。
- 本地适配工作区：`D:\gongzuo\.cache\worktrees\auto-canvas-huyangc`。
- 本轮新增实现、测试和说明全部在 `integrations/`；核心 `netlist2schematic/` 没有适配改动。
- `D:\auto-canvas-huyangc` 作为只读协议、符号和编辑事务提供方，没有修改其网页、依赖、锁文件或 Git 分支。

主算法可以继续独立迭代。运行时用 `--engine-root` 指向要使用的算法检出目录；适配器为每次任务复制一份源码，逐文件记录 SHA256，并确认运行后字节不变。只在该任务的私有资源副本中生成与目标项目实际符号一致的引脚、图形和几何表，不修改算法原有资源。

## 处理流程

```text
原生项目 JSON
  → 目标项目的 parseProject 与 Symbol Resolver
  → 选中单元的器件、网络、端口和真实符号几何快照
  → 仅用于布局的网表与独立资源副本
  → 原算法 run_flow.py：原候选、SA、A*、恢复、缩线
  → 复验算法线路，转换成原生 typed edits
  → 原生 Edit Engine 应用事务
  → 电气图比较、真实引脚核对、原生 routing audit
  → 原生 serializeProject、重新解析复验
  → 可编辑项目 JSON ＋ 原生 SVG ＋ 报告
```

原项目当前使用 `useAutoLayout → Worker → planAutoLayout(document, resolver, options)`，然后经过会话、文档版本检查和 Edit Engine 提交。接按钮时应沿用这条提交边界。这个适配器先验证同一原生事务接口，没有用 SVG 替代可编辑数据，也没有调用目标项目自己的 `planAutoLayout` 来冒充 Python 算法结果。

原生文件 schema 与内存 model schema 是两套版本；实测分别为 78 和 58。适配器使用目标项目的读写函数和当前版本常量，不能把算法 placement JSON 改个扩展名当作原生项目。

## 一次准备

需要算法现有的 Python 环境以及 Node 24+。当前机器可以使用目标项目已有的 `.local\preview\runtime\node.exe`；也支持 `--node` 显式指定。

以下命令在适配工作区执行：

```powershell
Set-Location D:\gongzuo\.cache\worktrees\auto-canvas-huyangc

& D:\gongzuo\.venv\Scripts\python.exe -X utf8 -B `
  -m integrations.auto_canvas_huyangc.bootstrap_runtime `
  --project-root D:\auto-canvas-huyangc
```

准备命令从目标项目锁文件读取精确版本和完整性摘要，将所需运行依赖放在适配工作区 `.cache\auto-canvas-runtime`。首次准备需要网络，校验包摘要，不执行 npm 安装脚本，也不写目标项目的 `node_modules` 或锁文件。再次运行复用本地缓存；目标锁文件变化后需要重新准备匹配的缓存。

当前采用 Node 的源码加载桥及 `--experimental-transform-types` 读取目标 TypeScript 包。Node 可能输出实验特性提示；这是加载方式的提示，不是布局错误。正式服务化时可替换为目标项目构建后的包，算法接口不需要跟着改变。

## 运行一个原生项目

```powershell
Set-Location D:\gongzuo\.cache\worktrees\auto-canvas-huyangc

& D:\gongzuo\.venv\Scripts\python.exe -X utf8 -B `
  -m integrations.auto_canvas_huyangc `
  --project-root D:\auto-canvas-huyangc `
  --engine-root D:\gongzuo `
  --input D:\auto-canvas-huyangc\.local\preview\examples\rc-lowpass.icproj.json `
  --out-dir D:\gongzuo\output\auto-canvas-adapter\my-rc
```

默认选择项目的顶层 Document。选择某个已有单元可加 `--cell amplifier`，或改用 `--document-id <ID>`；不会递归展开其中的子单元实例，其他 Document 保持原样。

默认使用原算法的 20,000 次迭代、种子 `20261029`、整图算法上限 720 秒，以及其内部独立候选预算。可以通过 `--iterations`、`--seed`、`--engine-seconds` 调整；已完成即退出，不会等满预算。原生读取、导出复验各有独立有限时限，超时只结束本次适配器启动的进程树。

输出目录必须为空，并且不能覆盖输入所在目录。主要产物：

| 文件 | 用途 |
| --- | --- |
| `*-layout.icproj.json` | 用 auto-canvas 打开、继续编辑的结果 |
| `*-layout.svg` | 用目标项目自己的渲染器绘制的同一结果 |
| `native-plan.json` | 带源哈希、文档版本和真实引脚预期坐标的编辑事务 |
| `native-validation.json` | 原生电气等价、保存重读、引脚和线路检查 |
| `adapter-report.json` | 阶段耗时、使用的算法源码哈希、最终状态与指标 |
| `engine-result/` | 原算法 placement JSON、彩色和干净版 SVG、诊断 |
| `private/`、`logs/` | 本次快照、隔离算法副本和排错日志 |

未通过转换或验收时明确返回失败和诊断，不生成冒充成功的原生结果。不支持的输入不修改原文件。

## 保留与转换的内容

- 原器件 ID、reference、模型绑定、参数、导入来源和终端连接保留；用于布局的短 ID 只存在于适配映射中。
- 模型类型来自原生绑定及符号证据，不从器件编号猜测。布局网表保留模型差异和安全的数值参数；最终原生文件保留完整原参数。
- 按实际可见引脚转换。MOS 隐藏的体端不会被错误地当成第四根可见导线，原始体端电气绑定仍保留。
- 同网 T 接变成原生 junction；异网十字交叉保持跨越，不增加电气结点。纯共线切段不影响转换。
- 保留标量正式端口及接口声明。信号端口接到原网络；供电接口可改用原生供电帽显示，记录该显示变更，不删除接口身份。
- 只有节点 `0` 使用带节点 0 语义的 Ground。独立的 VSS、VEE 等低电源使用向下的命名电源帽，不能因此与 0 短接。
- 位置、旋转、镜像按目标几何转换；不能表示的坐标不会静默取整。必要时减小原生网格公约数，不缩放符号或导线。
- 原生审计发现供电帽或正式端口的入口冲突时，仅对这些单端标记进行有限移位试案，不移动已完成的电气器件，也不修改算法。仍无法合法转换时失败。

`engine_metrics` 是算法原来的信号线路指标；`nativeAudit` 还统计供电引线及正式端口支线，线长和折角可能更多。二者不能直接当作算法优化前后对比。

## 第一版支持边界

已接入 R/C/L/D、独立 V/I、三可见端 MOS/BJT，以及原算法已有的三可见端 E 运放代理。支持原生标量接口、选中已有单元、保留未选中单元；模型与实际引脚必须有明确类型和正交几何。

以下情况目前明确拒绝，不跳过器件或猜测连接：

- 选中单元内尚需展开的层次实例，或未知器件绑定。
- 总线成员端口、耦合电感、需要另行约定的原生布局约束。
- 同一 Logical Net 包含多个需要合并的 Base Net，或网络身份存在冲突。
- 四个实际可见端的 MOS，或和当前核心器件接口不匹配的符号。
- 同类器件混用不同引脚几何、图形接点与网格落点不重合、非整数最终坐标。
- 端口或供电标记无法在原生规则下合法接入，或原生事务超过 1024 项编辑。

这不是对目标项目全部输入的兼容承诺。遇到以上输入会报告具体边界，后续可在适配层增加对应能力，不需要把项目字段加入主算法。

## 测试与实测

```powershell
Set-Location D:\gongzuo\.cache\worktrees\auto-canvas-huyangc
$env:AUTO_CANVAS_PROJECT_ROOT = 'D:\auto-canvas-huyangc'
$env:AUTO_CANVAS_RUNTIME = "$PWD\.cache\auto-canvas-runtime"
$env:NETLIST2SCHEMATIC_ENGINE_ROOT = 'D:\gongzuo'

& D:\gongzuo\.venv\Scripts\python.exe -X utf8 -B -m unittest discover `
  -s integrations\auto_canvas_huyangc\tests `
  -t . -v
```

32 项测试通过，其中 25 项覆盖边界与转换，7 项经过真实原生包。未设置上述环境或未准备缓存时，7 项集成测试会明确跳过。

2026-11-24，当前主算法、20,000 次迭代、默认候选与预算的四个独立实测：

| 原生输入 | 选中候选 | 算法信号线长／交叉／折角 | 原生全可见线长／交叉／折角 | 适配全程 |
| --- | --- | --- | --- | --- |
| RC 低通 | 3 | 60／0／0 | 96／0／0 | 5.10 秒 |
| SKY130 共源级 | 1 | 338／0／4 | 402／0／4 | 5.46 秒 |
| BJT，VSS/VEE/0 独立 | 1 | 200／0／1 | 272／0／1 | 5.17 秒 |
| 三单元文件，选择 amplifier | 2 | 148／0／1 | 272／0／2 | 5.23 秒 |

四例均通过原生电气等价、准确引脚、保存重读和线路审计。候选 1 由原算法的原有后备调度选中，适配器没有为了接入重选候选或调整布局算法。小例不能代表复杂电路的成功率或耗时。

本机结果页：`D:\gongzuo\output\auto-canvas-adapter\index.html`。结果文件在忽略的 `output/`，不会把运行缓存和原生项目源数据塞进 Git；测试夹具和复现命令随适配代码提交。

## 下一步接网页时的边界

网页发起任务时传入原生项目快照、选中 Document 和 revision；后台调用此适配器；返回编辑方案后，仍由现有会话与 Edit Engine 检查版本再提交。算法更新只更换 `--engine-root` 指向的发布版本，项目协议更新只改本目录。任务服务、按钮、取消与网页回传属于下一阶段，不在此次 JSON 接口验证中提前改动。
