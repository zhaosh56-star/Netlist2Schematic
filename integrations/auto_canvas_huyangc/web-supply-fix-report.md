# 网页供电飞线修复

用户实际运行的 1874 图中，VCC 和 VSS 各残留一条灰色飞线。算法完整信号路线已通过验收；问题来自适配器将额外供电出口画成有文字的局部 junction，却未为文字登记原生 `net-label` 命名声明。网页因此不能把正式电源端口和这些局部出口视为完整的命名连接。

## 修改

- 局部供电标签增加有真实 owner、原网络名称及原 scope 的命名声明，不增加 `powerDomain`，不改变正式端口身份和顺序。规则不依赖数据集、实例编号或坐标。
- 新增 `visible-supplies.mjs`：按真实导线建立供电分支，验证每个分支有实际接通的同名接点；检查标签归属、网络身份及原始导入供电飞线。缺少引线、标签或命名错误都拒绝成功输出。
- 原生保存重读和网页最终原子事务分别检查供电；信号验收继续执行。结果中新增可选 `visibleSupplies`，旧 plan 缺少供电几何时明确记录未执行此新检查。
- 没有隐藏飞线、删除导入参考，也没有修改算法核心、目标项目、器件位置或信号线路。当前后台新任务已使用该修复，旧画布需重新点击 Auto Layout 或打开新生成的原生项目。

## 验证

- 同一份 1874 既有合法路线重新转换：VCC 的 3 个物理分支有 3 个命名接点，VSS 的 4 个物理分支有 4 个命名接点；供电飞线 2→0，原生电气等价和端口顺序通过。
- 实际浏览器从旧结果重新点击 Auto Layout：飞线总数及可见数量均为 2→0，一次 Undo/Redo 通过。浏览器截图及报告位于 `D:\gongzuo\output\auto-canvas-adapter\power-flightline-fix\browser\`。
- BJT637 的既有位置与路线重新导出转换，VCC/VEE/VSS 命名连接检查、原生保存及网页原子事务通过，供电飞线为 0。此项只验证转换，没有重跑 SA/A*，不计作新的布局收益。
- 56 项 Python 回归全部通过，61.770 秒，其中 12 项真实原生集成；15 项 Node 测试通过。涵盖命名供电、多负电源、漏标、错误网名、供电断线、孤立导线及平移／旋转。

目标库 `missingConnections` 和 `floatingPins` 主要按物理导线及既有电源符号计数，不完整统计这种无电源域的 net-label 表达，仍可能报告实体分支未直接相连。本次没有改写这两个原始计数，另外报告实际物理分支、命名接点和已完成的命名连接验收。

## 复现

在适配工作区使用已有实际输入：

```powershell
& D:\auto-canvas-huyangc\.local\preview\runtime\node.exe `
  .\integrations\auto_canvas_huyangc\tests\browser-smoke.mjs `
  --origin http://127.0.0.1:4174 `
  --input <旧原生项目.icproj.json> `
  --out-dir <新的空目录> `
  --chrome 'C:\Program Files\Google\Chrome\Application\chrome.exe' `
  --jobs-root .cache\auto-canvas-web\jobs `
  --single true --expect-no-flightlines true
```

`--expect-no-flightlines` 是浏览器测试断言，不是隐藏界面的开关。
