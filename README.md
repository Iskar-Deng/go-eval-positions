# Go evaluation positions

## 人工填写 → 检测 → 收录

在 Sabaki 挑中一个局面后，在 VS Code 打开固定填写表 `scripts/selection.json`。
以后反复改写这一份即可，手数是已经走完的手数。
**只有 source_sgf、move_number、moves.A、moves.B 必填**：

```json
{
  "source_sgf": "outputs/games/某盘/annotated.sgf",
  "move_number": 60,
  "moves": {"A": "N5", "B": "E9", "C": null},
  "Y": {
    "edits": [
      {"from": null, "to": null},
      {"from": null, "to": null},
      {"from": null, "to": null}
    ]
  }
}
```

坐标仅演示格式，需自己挑选。相对 SGF 路径以仓库根目录为基准，与表单位置和终端当前目录无关。
行棋方从棋谱自动推断；当前统一使用 find-atari.json 中的中国规则、贴 7.5 目，
不继承原棋谱可能不同的规则或贴目。
Y 预留三组移动；不用的整组保持 `from/to` 都为 null。三组全空时只检查 X。
要移动当前棋盘的一颗子，填写任意一组：

```json
{"from": "L6", "to": "L7"}
```

无需填写历史手数：脚本会找到 `from` 当前棋子对应的原始落子，并把那一手改为 `to`。
X 保留原棋谱到所选手数的完整历史；Y 保留同一历史，仅修改这些落子坐标。
脚本不判断修改后的历史是否符合棋理，但需要能够按该历史还原棋盘并交给 KataGo。

```sh
.venv/bin/python scripts/review_sample.py check
```

检测不套用批量采样门槛。对每个已填的 A/B/C 分别计算当前方胜率、目差、是否打吃及目标棋块子数。
C 为空则不分析 C；有 Y 则再分析 Y 的同一组选点。搜索预算等复用 find-atari.json。
输出在仓库根目录的 `manual_reviews/runs/运行时间/`：`summary.md`、`analysis.json`、X.sgf、可选 Y.sgf 和配置快照。
`summary.md` 只包含 X/Y × A/B/C 的简表：合法性、胜率、目差、是否打吃、被打吃棋块子数。
已有的 `outputs/manual/` 不迁移、不修改；新运行才使用独立目录。
每次检测保留旧结果，latest.json 指向最近完成的一次。不会自动收录。

**X/Y 都以完整历史送入 KataGo，并以完整历史导出 SGF。**
Y 的历史是否自然、是否构成有效 minimal pair 仍由人工确认。

你确认后才执行：

```sh
.venv/bin/python scripts/review_sample.py accept
```

生成 `datasets/accepted/样例ID/`：固定为精简的 `sample.json + X.sgf`，有 Y 时再加 `Y.sgf`。
只维护这一份收录库：完整 X 集取全部目录，精细 X/Y 集取包含 Y.sgf 的目录。
最终 SGF 去掉评价评论和候选字母标签，但保留完整历史。接受时自动要求所有候选中只有 X/A 打吃，
不以胜率作为接受条件。`sample.json` 只保留来源与手数、统一的一份 A/B/C、X/Y 各选点的
胜率和目差，以及 X/A 被打吃棋块的大小和具体棋子。Y 的修改历史只保存在 `Y.sgf`，不在 JSON 重复。
有非法当前选点、填写表在检测后修改过、检测产物被修改时拒绝收录，要求重新 check。
测试集不自动去重合并不同人工选择；重复收录完全相同的表单会因目录已存在而报错。

## 运行

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/find_atari.py games/Bailing/01/1.sgf
```

所有筛选/采样参数在 `configs/find-atari.json`，可复制后用 `--config` 指定。
命令行只接受 SGF、配置、输出根目录（`--output-dir`）、引擎脚本（`--engine`）路径。
KataGo 底层线程、缓存、设备参数仍在 `configs/katago-analysis.cfg`。

| 配置项 | 含义 |
| --- | --- |
| rules | 显式分析规则覆盖；当前为 `chinese`；null 才读取 SGF RU |
| komi | 显式分析贴目覆盖；当前为 `7.5`；null 才读取 SGF KM |
| visits | 全盘搜索及每个定向搜索预算 |
| max_loss | A、B 距已评估点最高 scoreLead 的最大损失（目） |
| b_count / b_pool | 最多展示 B 数 / 定向复查 B 数 |
| strict_not_top | true 严格排除全盘搜索首选的 A |
| min_ab_distance | 每个展示的 A–B 最小曼哈顿距离，含等号 |
| min_target_stones | 被打吃的单个连通棋块最少棋子数 |
| winrate_min | 较低一方胜率 ≥ 此值才采样（默认 0.25） |
| stop_winrate_min | 较低一方胜率 ≤ 此值就停止后续扫描（默认 0.05），保留此前样例 |
| start / end | 检查手数范围（已落子手数），null 至末尾 |
| max_samples | 保存局面数上限，null 不限制 |
| query_timeout_seconds / shutdown_timeout_seconds | 引擎响应/退出超时 |

当前所有新分析统一使用 `rules=chinese, komi=7.5`。棋谱只用于恢复走法和盘面；
输出仍同时保存原始 SGF 元信息与实际分析规则、贴目。

## 筛选

逐手还原主线，寻找当前行棋方所有打吃选点，不限实战落子。
从 start 开始每个局面先全盘分析，即使没有打吃也检查停止阈值。
局面胜率取当前行棋方的 `rootInfo.winrate`：达到 95% 或降至 5% 时停止该盘，
记录 stop.json 并保留此前样例；不再等待胜率回到区间。
未达到停止阈值但不在 25%–75% 时跳过候选搜索，继续下一局面。
区间内才寻找至少 2 子的打吃目标并做后续分析。
完整历史交给 KataGo，policy 检查劫/超级劫等合法性。
每个合法 A、最多 b_pool 个距离合适的非打吃 B、首选点分别定向搜索。
B 排除所有打吃点，包括只打吃单子的点。必须同时存在接近最佳目差的 A 和 B。
非首选 A 优先；首选 A 若有接近的 B 仍可保留。首选按全盘搜索 order=0 定义。

先为优先 A 选择远处的 B，再保留与所有这些 B 都足够远的其他 A。
因此每个显示的 A 与每个显示的 B 都满足距离限制，但不保证列出所有可行组合。
曼哈顿距离=横向差+纵向差（横 3、竖 3 即 6）。距离不能保证不是同一场战斗。
先后手、战斗关系和 Y 扰动仍由人工判断。500 visits 只是粗筛，评价存在噪声。

## 输出

批量目录：`outputs/games/日期__赛事__轮次__黑棋__白棋__棋谱哈希/`。
单盘目录：`outputs/single/UTC运行时间/games/日期__赛事__轮次__黑棋__白棋__棋谱哈希/`。
缺失元信息用 unknown，特殊字符清理；批量运行按棋谱哈希断点续跑。
`--output-dir` 改变整个输出根目录。

- `annotated.sgf`：Sabaki 打开，候选落子之前看 A/A1/A2 和 B1/B2/B3。
  评论搜索 `ATARI_SAMPLE`，含局面胜率、目差、候选胜率、目标棋块及气。
  不额外加变化分支，点击候选即可试下。原历史和已有变化保留。
- `samples.jsonl`：每局面一行，含历史、棋盘、A/B、分析值、配置及棋谱元信息。
- `config.json`：配置快照；批量模式下每盘保存一份，用于判断能否跳过。
- `.complete`：该盘已完整处理的断点续跑标记。
- `metadata.json`：棋谱元信息、源文件/哈希、实际分析规则和贴目。
- `engine.log`：模型/引擎运行信息。

原棋谱不修改；只在输出副本覆盖同点 LB 标签。已找到的样例即时保存。
输入应为单盘 SGF，不是多盘 collection。暂不支持中途摆子、非交替落子、自杀走子、
超过 19 路棋盘；终局不采样。

测试：`.venv/bin/python -m unittest discover -s tests`
