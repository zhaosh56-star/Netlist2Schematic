# 完整电路生成基础

这里是当前 gongzuo 电路项目的独立源码和符号资源快照，保留真实器件符号、引脚几何、SPICE 解析、SA 摆件、正交布线、原始几何验收、失败恢复、安全缩线与双 SVG 渲染。无需父目录源码或原 Analog Canvas Web 应用。

- `candidate_entry.py --profile baseline`：从自由网格基础 SA 开始（默认 200 次），最终仍使用真实路由和完整合法性检查，不执行安全缩线。
- `candidate_entry.py --profile reference`：当前默认结构化分层流程（默认 20000 次），作为能力目标；实验结构增强仍遵循原项目默认关闭。
- `--iterations` 显式指定时可覆盖两个 profile 的默认步数；每次 `--out` 必须为空目录。`--seed` 控制随机性，子电路用 `--cell` 选择。
- 输出原生 `*-placement.json`、`*-baseline.svg` 或 `*-layered.svg`、`*-clean.svg`，以及生成回执 `generation.json`。回执和候选自报分数不能替代外部独立验收。

安装 `requirements.txt`（networkx==3.7）后，例如：

```powershell
python candidate_entry.py --input analog-canvas-main/netlists/native-rc-filters/circuit.spi --out output/rc-baseline --profile baseline
python candidate_entry.py --input analog-canvas-main/fixtures/simulation-acceptance/ota-5t.spi --cell ota_5t --out output/ota-reference --profile reference --iterations 500
python -m unittest discover -s tests -q
```

`examples/` 保留原样例（包括原本格式不合法的 6.cir，不静默修复），`test_data/` 保留 25 个测试网表，`tests/fixtures/` 保留冻结回归输入。网表解析与生成范围遵循源码；不做电路功能仿真。器件符号和许可证见 `THIRD_PARTY_NOTICES.md` 与 `analog-canvas-main/LICENSE.md`。
