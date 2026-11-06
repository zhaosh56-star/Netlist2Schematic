# 子结构识别图鉴

在项目根目录执行以下命令，会生成可搜索、筛选和放大的本地图鉴：

```powershell
.\.venv\Scripts\python.exe .\tools\generate_structure_gallery.py
```

生成后打开 `output/structure-gallery/index.html`。全部图片、示例网表、保存的线路及识别记录集中在这一个目录，不依赖已有图1～7的输出。

目前默认识别器有15种 `kind`，其中4种是辅助关系。正、负反馈各画一例，因此主图共16张；再附旧VF2额外4种名称，共20张图。

## 默认结构化识别器

| 名称 | kind | 记录类别 | 示例说明 |
| --- | --- | --- | --- |
| 差分对 | `differential_pair` | high | 保留尾部供给证据；示例只突出两只输入管 |
| 电流镜 | `current_mirror` | high | 区分D=G参考管和输出管；支持多输出 |
| 交叉耦合对 | `cross_coupled` | high | 漏与对方栅交叉连接 |
| 互补支路 | `complementary_branch` | high | PMOS/NMOS共栅、共漏 |
| 互补输出级 | `output_pair` | high | PMOS/NMOS共漏、控制端不同，源端接电源轨 |
| 锁存核心 | `latch` | high | 由交叉耦合与互补支路组成父结构 |
| 折叠共栅核心 | `folded_core` | high | 输入、折叠和偏置支路共六只成员管 |
| 共偏置关系 | `common_bias_pair` | relation | 共源共栅，但没有参考管证据，不冒充电流镜 |
| 公共源支路 | `common_source_bank` | relation | 至少三只MOS共源，不任取两只当差分对 |
| 共栅关系 | `common_gate_pair` | relation | 当前仅在折叠核心识别中派生，示例保留完整上下文 |
| 有源串接支路 | `series_branch` | relation | 同型管D/S串接，帮助上下对齐 |
| 无源串联链 | `series_chain` | high | 保留无源连接次序 |
| 并联无源支路 | `parallel_passives` | high | R/C等连接同一对网络 |
| 运放反馈网络 | `feedback_network` | high | 分别示范接IN−的负反馈和接IN+的正反馈 |
| 运放反馈核心 | `opamp_core` | high | 运放及其反馈网络组成父块 |

`high`表示当前规则认出的结构块，`relation`表示辅助连接关系。图鉴只展示连接模式，不将识别结果解释为仿真通过或某种指定动态功能。

## 旧VF2附录

附录补充传输门、多路电流镜、同源同漏MOS组、二极管接法MOS负载。旧VF2还有差分对、电流镜、交叉耦合、CMOS互补名称，其连接示例与主图相应结构重叠，不再重复画一套。

默认新识别器的 `current_mirror`包含多输出镜，不额外返回“多路电流镜”这个kind。旧代码虽然定义了CAS_N/CAS_P变量，但它们没有登记在当前启用的 `TEMPLATES`中，不能将其当作已启用识别另列。

BJT目前支持解析、分层和绘图，MOS结构识别器没有对应BJT结构模板。

## 怎样看图

- 蓝框是本条实际识别记录的器件成员。
- 淡色器件是提供必要识别证据的上下文，例如差分对尾电流源。
- 单端信号出口显示网名；完整导线按网络着色，电源/地使用现有标记。
- 每张卡片有器件角色、识别条件、示例网表、黑白图及实际识别JSON。
- 一个示例可以同时包含父结构、子结构和辅助关系，卡片只突出当前讲解的那一条。

教学布局是固定安排，便于看清连接，不通过SA优化，也不作为默认自动摆件美观度的对照。布线仍使用当前最终路由器、必要的局部救援及安全简化，所有成功图必须通过原引脚连接和完整几何验收。注释与高亮只改显示，不改保存线路。

## 文件与复现

- [generate_structure_gallery.py](../tools/generate_structure_gallery.py)：实际调用识别、路由、验收及渲染，生成HTML和总清单。
- [structure_gallery_cases.py](../tools/structure_gallery_cases.py)：独立教学示例的网表、角色和展示坐标；生产布局算法不导入此文件。
- [circuit_structures.py](../circuit_structures.py)、[vf2_match.py](../vf2_match.py)：现有识别规则。
- `output/structure-gallery/manifest.json`：类型覆盖、源码哈希、每图成员、指标和输出路径。
- 每个示例子目录保存 `example.cir`、`recognition.json`、`example-placement.json`、高亮/彩色/黑白SVG。

局部重新生成某一张：

```powershell
.\.venv\Scripts\python.exe .\tools\generate_structure_gallery.py --only 07-folded-core
```

它会更新该示例并刷新已有总页；首次生成整套图鉴不要加 `--only`。图鉴使用本地生成物，不进入Git；生成工具和说明保留在源码中。
