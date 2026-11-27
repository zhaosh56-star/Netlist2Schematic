# 原生网页规划接口 — 第二轮报告

> 历史记录，2026-11-27归档。本文的预算、算法版本、支持范围和验证状态仅代表当时。当前入口见[README](../README.md)、[WEB](../WEB.md)及[草稿预算](../editable-drafts.md)。

## Summary

独立本地网页入口已接通：`http://127.0.0.1:4174/editor`。已有 Auto Layout 按钮调用 Python 主 flow，输出通过原编辑器的版本检查与单次原生事务应用。原来的 4173、目标项目源码、生成构建文件及主算法源码不变。

代码仍位于 `adapt/auto-canvas-huyangc` 的 `integrations/auto_canvas_huyangc/`，没有把项目接口放进算法核心，也没有调用目标项目原规划算法代替 Python。

## Steps

| 步骤 | 状态 | 结果 |
| --- | --- | --- |
| Worker 协议接入 | done | 原按钮与导入后布局继续走原会话；适配 Worker 提交、轮询 Python 任务 |
| 本机后台任务 | done | 单任务执行、四请求容量、独立目录、有限预算及断连租约 |
| 原生模型与单次编辑 | done | 验证内存模型、正式序列化；合并标记修正事务，再次验证原始 revision 的一次提交 |
| 静态构建隔离 | done | 读取现有部署，使用独立版本资源地址；不手改生成文件和原目标仓库 |
| 生命周期 | done | Python 启动／停止管理器；仅自己的认证接口关闭，不杀未知进程 |
| 接口与实际浏览器 | done | 46 项 Python、5 项 Node，RC 与 MOS 各四个实际 UI 验收步骤 |

## Review findings addressed

本轮没有单独 review.md。实现中补齐了：合并标记修正为一次事务的复验、不同请求结果不得串用、进程树的租约取消、过期会话与源码根识别、跨站／错误 Host／非 ASCII 凭据拒绝，以及旧静态缓存不应加载原规划 Worker。

## Acceptance results

```text
python -X utf8 -B -m unittest discover -s integrations/auto_canvas_huyangc/tests -t . -v
→ Ran 46 tests in 38.593s, OK。真实原生集成环境已开启，无跳过。

node --test integrations/auto_canvas_huyangc/tests/web-worker.test.mjs
→ 5 passed, 0 failed。

实际 Chrome，使用原 .local/preview/web 构建、默认 20000 次迭代参数：
RC 原生输入：4 个步骤通过，Auto Layout applied (revision 2)。
MOS 共源级：4 个步骤通过，Auto Layout applied (revision 2)。
每例均：真实 Python 作业 → 一次 Undo/Redo → 运行中 Undo 后旧结果被丢弃 → 取消不覆盖图纸。

web_control start / stop / start → 成功；4174 在后台运行。
原目标 Git status → main...origin/main，工作区干净。
主算法 tracked diff → 空。
```

浏览器报告与导出前后项目、截图：

- `D:/gongzuo/output/auto-canvas-adapter/web-browser-check-2/`
- `D:/gongzuo/output/auto-canvas-adapter/web-browser-mos/`

后台作业在本适配工作区 `.cache/auto-canvas-web/jobs/`，保留每个单元的算法报告、原生验收与最终合并编辑方案。取消测试中的小作业可能在 8 秒租约前完成；浏览器仍没有应用该结果。另用真实睡眠 Python 子进程验证了失联租约确实结束运行任务，不只改变状态文字。

## Investigation results

1. 当前部署由原生 local-host 静态服务托管，主要交互已经通过 Worker 与原生会话隔离，因此无需改编辑器控制器或重新安装整个项目依赖即可接入。
2. 源文件模型与内存模型不同；浏览器模型经目标库自己的验证及序列化，不自行拼装文件 schema。
3. 第一阶段标记入口修正可能包含额外事务；直接只返回最初 native-plan 会遗漏这些修正。本轮将其合并，再验证电气、位姿、引脚、线路和保存重读，才返回一次提交的 edits。
4. 当前 Service Worker 对导航采用网络优先。新增版本资源命名空间及 no-store 规划响应可让新页面使用正确 Worker，不清空用户项目或浏览器恢复数据。
5. 原会话已有防止旧结果覆盖新编辑、项目切换及一次撤销逻辑。两例真实浏览器均验证有效，无需在适配层另写一套画布状态更新。

## Deviations and open questions

- 没有接替原来的 4173，使用独立 4174，便于比较与回退。不同端口的浏览器存储隔离；已有项目需要导出后在新入口打开。
- 初版 PowerShell 启动方式受到本机脚本执行策略限制，改为跨平台 Python 管理器；没有修改系统执行策略。
- Web 取消界面即时结束；后台最多 8 秒无心跳后结束进程。快于租约完成的结果也不会应用到已取消页面。
- 依赖已有部署的 Worker 消息协议；未来目标项目更新协议或模型时，需要重新运行本轮协议与浏览器测试。
- 第一阶段的器件、层次和总线边界未扩大；不支持的单元明确拒绝，不回退到网页原算法。
- 本轮没有运行目标项目全套 pnpm 构建／测试：没有目标源码或依赖改动，实际测试直接使用其已部署构建及原生包。

## 后续现场修复与最终回归

- 供电判定保留原生明确分类，不把普通内部 DC 驱动节点改名为 VDD；正式端口的原始身份、供电域与导出顺序保持不变。
- 正式端口补线使用真实固定出口，并只保留最后一个同网接点之后的支线，去掉离开后又接回原网的小回环。
- 原生保存重读和网页合并事务都新增实际导线检查，覆盖端子连通、原线路覆盖、额外回环及孤立线段。
- 最终完整回归：54 项 Python 测试全部通过，48.228 秒，其中 10 项使用真实原生包；11 项 Node 协议／导线测试全部通过。
- 真实 637 输入重新从按钮运行，57.719 秒完成正常 A*；MOS 用户输入另做真实按钮、一次 Undo/Redo 和固定线路转换对照。固定对照中 11 个电气器件位姿不变，8 个信号网连通，原生线长 1804→1728，交叉 0、折角 12 保持不变。
- 网页任务使用 `D:\gongzuo` 主算法快照，163 份源码指纹一致。适配使用目标符号几何，当前关闭额外正式 SVG 插件，网页渲染尚不等同于主项目正式 SVG 样式。

现场说明见 `web-port-fix-report.md` 和 `WEB.md`；运行截图、项目快照与日志继续只保存在忽略的本地输出／缓存目录。
