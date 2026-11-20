# 单文件盲测生成脚本

用 `build_evaluation.py` 把任意一批“GT 图片＋算法图片＋网表”封装成问卷。也能直接读取已有正式图集清单，不重新运行布局、SA、A* 或缩线。

脚本不内置具体图号、排除项或 VDD 换图要求，默认全部纳入，并采用你提供的图片版本。已有的 44 题历史问卷保留在 `output/blind-review/`；新评测默认输出到另一目录 `output/evaluation/`。

## 一条命令生成

```powershell
Set-Location D:\gongzuo
.\.venv\Scripts\python.exe -X utf8 -B .\build_evaluation.py `
  --manifest .\pairs.json --out-dir .\output\my-evaluation
```

清单 `pairs.json` 写出图片、网表与器件数量即可，路径相对清单所在目录。每题比较同一电路的两张图，数量和图名不限，无需来自本项目，也不要求有布局 JSON。图片支持 SVG、PNG、JPEG、WebP。

```json
{
  "format": "schematic-evaluation-pairs-v1",
  "dataset": "my-circuits",
  "cases": [
    {
      "tag": "example-1",
      "netlist": "inputs/example-1.cir",
      "gt": "inputs/example-1-gt.svg",
      "algorithm": "inputs/example-1-generated.png",
      "device_count": 8
    }
  ]
}
```

在 `cases` 中继续添加条目即可。`tag` 必须唯一，只用字母、数字、下划线或连字符；`device_count` 填实际器件数量，不包含 VDD/GND 符号，脚本不修改或重新解析网表。按 **≤5、6～10、11～15、≥16** 自动分组。模板见 `tools/blind_review/pairs.example.json`。

只把输出目录的 **`index.html`** 发给客户即可，可以单独复制、改文件名。图片、网表、样式和脚本全部内嵌，客户无需 Python、仓库或联网。`participant/index.html` 是相同内容的兼容副本，不需要再发一份。

## 客户答题与下载

客户用 Chrome 或 Edge 打开，选择 A 好、B 好、一样好、无法判断，原因选填；可展开网表、放大图片、跳题或随时结束。结束后只下载一个 HTML 报告并发回。报告包含 GT／算法偏好占比、分组统计和逐题选择、原因、图片与网表，原始反馈数据也内嵌其中，无需另交压缩包或 JSON。

答题时 A/B 顺序和组内题目顺序会随机，并保存在当前浏览器。需要清空时点击侧栏“开始新的评测”。最终报告揭示来源并锁定本次答卷，可以重复下载同一报告。这是界面盲测：为离线生成报告，问卷内含解码信息，不能向查看源代码的人保密。

## 直接使用正式图集

```powershell
.\.venv\Scripts\python.exe .\build_evaluation.py `
  --manifest .\output\formal-figures\manifest.json --out-dir .\output\new-study
```

这条命令纳入清单内的全部图，不沿用任何历史评测的排除或换图设置。若将来确实需要排除，可显式用 `--exclude example-1 example-2`，默认不排除。对于任意图片清单，想更换 VDD 版本，直接把该条目的 `algorithm` 指向想用的图片即可。

原生正式图集由 `tools/export_formal_dataset.py` 或 `tools/combine_formal_galleries.py` 生成，每个 `cases` 条目包含：

| 字段 | 含义 |
|---|---|
| `tag` | 唯一编号 |
| `source`、`source_sha256` | 原网表路径与 SHA-256 |
| `input_placement`、`placement_sha256` | 保存布局 JSON 路径与 SHA-256，用于准确统计器件 |
| `formal_svg`、`formal_svg_sha256` | 算法正式 SVG 路径与 SHA-256 |
| `dataset` | 可选的数据集名称 |
| `reference_svg` | 可选 GT 路径，缺省为网表同目录、同名的 `.svg`；也兼容 `gt_svg` |

清单内路径支持绝对路径或相对清单目录的路径。GT 仅作为评测图片，不用于指定算法坐标。图片、网表缺失或输入哈希变化时停止，不会编造图像或静默漏题。任意图片清单也可选填 `netlist_sha256`、`gt_sha256`、`algorithm_sha256` 来冻结输入；未填则在打包时记录实际哈希。

## 可选配置与 Python 调用

重复使用时可把路径与种子存成配置，再执行 `python build_evaluation.py --config my-config.json`：

```json
{
  "format": "blind-schematic-evaluation-config-v1",
  "manifest": "pairs.json",
  "out_dir": "evaluation",
  "seed": 20261119,
  "exclusions": [],
  "unmerged": []
}
```

配置路径相对**配置文件**解析，命令行路径相对工作目录解析。使用解释器、脚本和输入的绝对路径，也能从其他目录启动。配置模板在 `tools/blind_review/config.example.json`，先复制并填写实际路径。

必须提供 `--manifest` 或 `--config`，不自动复用某次历史数据。命令行设置覆盖配置；`--exclude` 或 `--unmerged` 后不跟编号可清空对应列表。`unmerged` 只适用于附带保存布局的原生正式图集，会按已有线路重绘未合并 VDD 的正式图，不重新寻路；普通图片始终使用提供的版本。

编号不区分大小写；未知或重复编号、错拼配置项会报错。新一轮评测建议用独立目录，避免混用反馈。生成器版本、输入哈希、实际设置、分组与问卷哈希记录在 `researcher/build-manifest.json`。相同输入和种子生成同一问卷及评测标识，客户的 A/B 分配仍各自随机。

也可在代码中调用：

```python
from tools.build_blind_review import build

result = build("pairs.json", "output/my-study", seed=20261119)
print(result["count"])
```

根目录 `build_evaluation.py` 是入口，生成逻辑在 `tools/build_blind_review.py`，页面与前端脚本在 `tools/blind_review/`。原工具命令继续兼容；正式图插件见 [formal-figures.md](formal-figures.md)。

## 多人汇总与快速验证

输出目录的 `researcher/index.html` 也是可单独复制的 HTML。打开后选择客户返回的报告 HTML，可多选，查看多人统计并导出一个汇总 HTML。同一评测者重复提交只保留较新的报告，不同问卷或图片指纹不匹配时拒绝导入；未作答不计作“无法判断”。重新打开汇总页时，再选择报告即可。

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s .\tests -p test_blind_review.py -q
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s .\tests -p test_evaluation_script.py -q
.\.venv\Scripts\python.exe -X utf8 -B .\tools\check_blind_review_browser.py `
  --study-dir .\output\my-evaluation --chrome "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

浏览器交互检查需要至少 5 题，生成器本身也支持更少题目。检查会在 `researcher/qa/` 生成标注 `SYNTHETIC` 的模拟报告，仅用于验证，不作为客户反馈；正常生成评测无需安装或运行该测试。
