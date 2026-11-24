# 在本地网页使用 Python 算法

第二阶段已接入当前部署网页的 **Auto Layout** 按钮，也接入导入 SPICE 后的自动布局。网页继续使用原生编辑事务、版本保护、一次 Undo/Redo 和取消界面；布局与布线由独立的 Python 主 flow 执行。

## 本机入口

```text
http://127.0.0.1:4174/editor
```

`4174` 是接入 Python 的入口，原来的 `4173` 保留原项目算法。这两个端口读取同一套已部署静态文件；适配器不修改目标项目源码或生成文件。

浏览器存储按端口隔离。如果要使用 `4173` 上已有的电路，先从那里导出项目 JSON，再在 `4174` 用 File → Open/Import 打开；不会清空或搬动原网页的存储。

## 启动与停止

```powershell
Set-Location D:\gongzuo\.cache\worktrees\auto-canvas-huyangc

# 首次先按 README 准备隔离的 Node 运行库；Python 使用算法现有环境。
& D:\gongzuo\.venv\Scripts\python.exe -X utf8 -B `
  -m integrations.auto_canvas_huyangc.web_control open `
  --project-root D:\auto-canvas-huyangc --engine-root D:\gongzuo

# 只启动，不打开浏览器
& D:\gongzuo\.venv\Scripts\python.exe -X utf8 -B `
  -m integrations.auto_canvas_huyangc.web_control start `
  --project-root D:\auto-canvas-huyangc --engine-root D:\gongzuo

# 停止自己的任务与服务，不停止原来的 4173
& D:\gongzuo\.venv\Scripts\python.exe -X utf8 -B `
  -m integrations.auto_canvas_huyangc.web_control stop `
  --project-root D:\auto-canvas-huyangc
```

也可以在终端直接运行，保持该终端开启，按 Ctrl+C 停止：

```powershell
& D:\gongzuo\.venv\Scripts\python.exe -X utf8 -B `
  -m integrations.auto_canvas_huyangc.web_server `
  --project-root D:\auto-canvas-huyangc `
  --engine-root D:\gongzuo `
  --port 4174
```

支持 `--engine-seconds`、`--iterations`、`--seed` 配置；默认沿用主算法的 720 秒整图上限、20,000 次迭代及独立候选流程。完成即返回，不会等满预算。监听地址固定为本机 loopback，不会开放到局域网。

## 使用

1. 打开 `4174/editor`，导入原生 `.icproj.json` 或 SPICE 网表。
2. 打开要布置的 Cell，点击已有 **Auto Layout** 按钮。
3. 网页显示运行时间，Python 后台依次运行原候选和恢复流程。
4. 完整验收后，原生编辑器将位置、方向和线路作为一个事务应用；可继续编辑、保存，或一次 Undo 撤销。

运行时手动修改图纸，旧结果会显示 `The drawing changed … nothing was applied` 并被丢弃。切换项目或取消会终止网页 Worker；后台最长 8 秒租约到期后停止这个任务的进程树。很快完成的任务可能在租约到期前结束，但取消后的结果不会应用到图纸。

计算期间画布保留原图，按钮显示累计秒数；完整计算与验收结束后才一次性更新。失败时保留原图，并在原生状态及 Details 中显示原因，不表示点击没有生效。

第一版 JSON 适配的边界仍有效：尚不展开选中单元内的层次实例，不支持总线成员或未映射器件等。不支持的 Cell 会在原有 Details 中显示具体原因，保持原图；不会静默改回网页原算法。多 Cell 导入逐个计算，全部结束后由原生会话决定哪些合法结果可以作为一次撤销提交。

## 接口如何隔离

```text
原网页 Auto Layout 按钮
→ 原会话及 Worker 协议
→ 适配 Worker：提交与轮询本机任务
→ 原生模型验证与正式项目序列化
→ 第一阶段适配 CLI
→ 独立主算法 run_flow.py
→ 完整电气、几何及原生审计
→ 合并成一个原始版本的编辑事务并再次复验
→ 原网页会话检查 session/revision 后应用
```

本地主机用独立版本的虚拟资源地址加载现有构建，为其规划 Worker 提供 Python 实现。修改仅发生在内存响应中；既不手改有哈希的生成文件，也不 monkey-patch 编辑器控制器。旧 Service Worker 的不可变缓存不会把旧规划入口带进新页面。其他静态界面及编辑逻辑沿用目标构建。

这份 Worker 是项目接口适配，不是布局算法。更换算法只需更新 `--engine-root`；目标 Worker 协议发生变化时，更新本目录并重新运行真实浏览器测试。启动时找不到唯一已知 Worker 会拒绝，模型版本不一致也会明确拒绝。

## 任务、日志和边界

- 同时执行一个 Python 任务，最多四个未结束请求；不同标签页有独立随机任务 ID。
- 请求最多 32 MiB、32 个不同 Document；客户端不能提供命令、目录或执行预算。
- 布局服务检查 Host、Origin、同源会话凭据，拒绝跨站写入；没有对外 CORS。
- 任务目录：适配工作区 `.cache/auto-canvas-web/jobs/<ID>/`。包含原生输入、各单元算法输出、最终原生项目、合并编辑方案、诊断及 `web-job.json`。
- 日志与服务状态：`.cache/auto-canvas-web/port-4174*.log/json`。状态文件含仅供本机服务使用的会话凭据，不应上传。
- 网页返回失败不覆盖已有电路；关闭服务会停止自身运行任务，不影响其他 Python 进程或原来的 Node 服务。

## 验证命令

Python 和 Node 单测按 README / `tests/web-worker.test.mjs` 运行。真实浏览器测试直接驱动当前部署构建，不需要安装目标项目依赖：

```powershell
& D:\auto-canvas-huyangc\.local\preview\runtime\node.exe `
  .\integrations\auto_canvas_huyangc\tests\browser-smoke.mjs `
  --origin http://127.0.0.1:4174 `
  --input D:\auto-canvas-huyangc\.local\preview\examples\rc-lowpass.icproj.json `
  --out-dir D:\gongzuo\output\auto-canvas-adapter\my-web-test `
  --chrome 'C:\Program Files\Google\Chrome\Application\chrome.exe' `
  --jobs-root .cache\auto-canvas-web\jobs
```

输出目录需要为空。测试仅使用自行创建的无界面浏览器，并验证：真实按钮调用 Python、结果产生、一次撤销/重做、运行中编辑不被覆盖、取消不应用结果及后台有限结束。

## 637 点击后未更新的修复记录

实测曾发现两项适配问题：内部直流驱动节点被误当成供电网，以及将未分类正式供电端口改成电源帽后改变了原生导出器的端口排列。现在保留原始电气分类与接口身份，避免将普通驱动节点改名成供电；等价校验保持开启。

使用同一份实际网页输入重新点击验证：57.72 秒完成，正常 A*，选中候选 1。核心信号线长 4924、交叉 5、折角 29；加入正式端口引线后的原生画布指标为 5304、6、30。完整电气等价、原生线路审计、一次 Undo/Redo 均通过，没有使用历史合法成品替代本次计算。

报告与截图：`D:\gongzuo\output\auto-canvas-adapter\web-637-fresh-fix\`。

## 正式端口补线回环修复

用户 MOS 输入出现的 M6 上方、M8 下方小回环来自原生格式转换：新增端口引线离开同网干线后又在中途接回，重复保留了中间一圈。现改为从拟补路径的最后一个同网接点接出，同时复用算法真实的 `Pin.escape`，不再假设固定出线必定为 12。规则不依赖器件编号或网名，算法核心不变。

新增原生实际导线检查：每个信号网的全部真实端子和正式端口必须连通，原算法线路及固定出线必须完整覆盖，不得新增同网回环或孤立线段。检查分别发生在保存重读之后及网页合并事务之后。

同一冻结线路的格式转换对照中，11 个电气器件的位置、镜像、旋转保持一致；原生可见线长 1804→1728，交叉保持 0，折角保持 12。8 个信号网通过实际连通检查；M1/M2 共同 S 网包含 3 个真实端子，两个 G 仍分别保留 VIN1/VIN2 身份。另从真实网页重新点击完整布局，画布更新及一次 Undo/Redo 通过。对照：`D:\gongzuo\output\auto-canvas-adapter\mos-wire-investigation\index.html`。
