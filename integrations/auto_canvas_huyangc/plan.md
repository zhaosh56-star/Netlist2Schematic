# auto-canvas-huyangc 原生JSON适配

本轮只实现离线原生项目JSON的输入、布局调用、原生项目输出及复验，不修改网页自动布局按钮。

## 分支与所有权

- 算法主线：`main`，基线提交`19741f0`；候选2／3的120＋30＋30＋30秒调度已独立提交。
- 适配分支：`adapt/auto-canvas-huyangc`，独立Git worktree。
- 本轮代码只位于`integrations/auto_canvas_huyangc/`及该目录测试，不修改核心`netlist2schematic/`。
- `D:/auto-canvas-huyangc`仅作为原生格式、符号和事务实现的只读提供方，不切换、提交或推送该仓库。
- 原始输入不覆盖。运行文件与临时依赖位于忽略的`.cache/`和`output/`，不写入目标项目的依赖或锁文件。

## 已确认的接入契约

项目当前通过`useAutoLayout → Worker → planAutoLayout(document, resolver, options)`返回`AutoLayoutResult {edits, groups, diagnostics}`。编辑器检查项目会话及Document版本，再通过Edit Engine原子提交；因此SVG不能作为可编辑布局接口。

项目原生`.icproj.json`的文件schema与内存model schema不同，必须使用`@icm/project-protocol`解析、保存，不能把算法的placement JSON冒充项目文件。项目的Symbol Resolver提供真实可见端口及几何；输出须保留原器件身份、模型参数、端子连接和其他Document。

## 实施步骤

1. 读取原生项目，通过目标项目的协议及符号接口生成版本化快照。
2. 将快照转换成算法输入与独立符号资源工作区。核心源码保持原字节，资源及字段转换由适配层负责。
3. 按原主flow运行，转换最终已验收的位置和线路；通过原生事务接口应用到选定Document。
4. 用原生解析、编辑、保存及电气图比较复验；输出原生JSON、原生SVG、算法SVG及运行报告。
5. 覆盖最小RC、共源级及保存重载；不支持的符号、层次、约束或无法保持连接的输入明确拒绝，不降级为SVG冒充原生项目。

## 依赖与测试

优先使用目标项目已有的Node 24及已构建包。源码运行桥接只在适配工作区准备目标锁定版本的外部运行库，不安装或升级目标仓库依赖，不调用任何LLM服务。

测试包括字段和几何转换、ID与网络完整性、错误输入、输入不变、版本检查，以及原生读写的真实集成。使用项目自带的小电路验证接口，不借用GT坐标或历史成品生成布局。
