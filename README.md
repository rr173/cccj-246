# 设施术语供数库（reopenable, locally correctable）

一个仅依赖 Python 标准库（SQLite 持久化）的设施术语供数库。它按
**设施 / 目标语种 / 语境** 查询当前用词，并在“没有词”时区分三种本质不同的
情况。不做任何外部翻译，也不保存无限历史——只保留当前词条状态和有限的批次台账。

## 判定结果（无词的三种情况必须可区分）

| outcome | 含义 |
|---|---|
| `supplied` | 有非空正文正常供数，附带可核对的出处证据 |
| `explicit_clear` | 合规的**明确清空**在某坐标生效，停止一切更低级回退 |
| `conflict` | 同一候选坐标上有不同文本的同级冲突，保留冲突，**绝不试更低来源** |
| `blocked` | 候选坐标存在，但每个来源都被政策判定为不合格 |
| `no_available` | 整条候选链上没有任何可用记录（与“被政策挡住”明确区分） |

## 核心规则

1. **候选坐标固定、显式、按优先级排列**：每个目标 `(语种, 语境)` 在建库时
   给定一条有序坐标链，可指向其他语种/语境，**引擎永不递归展开**。
2. **词条身份**是 `(设施, 语种, 语境, 出处标识)`。
3. **先政策、后出处**：先按设施政策判定资格，再在候选坐标上只取合规的
   最高优先级出处。政策只决定资格，不改变任何词条的语种。
   - `allow_generic`：是否允许使用通用设施 `*` 的通用词；
   - `blocked`：该设施禁止回退到的坐标；
   - `require_public_reviewed`：公开词必须已通过审核，否则不合格。
4. 正文三态：`text`（非空）、`clear`（明确清空）、`withdrawn`（撤回）。
   - 非空正文正常供数；
   - 合规的明确清空在该坐标生效并**停止回退**；不合规的清空不停止回退；
   - 撤回只移除该出处；该坐标还有其他可用记录就用，否则才继续回退。
5. 同一坐标上多个同级来源：文本一致则共同作为证据；文本不同则判 `conflict`，
   保留全部冲突文本，低级坐标标记为 `not_reached`。
6. **审核只决定资格**（`reviewed` / `revoked`），绝不改动语种或语境。

## 更正批次

- 一个批次可含多条词条更正（改正文 `set_body`、`clear`、`withdraw`、
  改审核 `set_review`），并可附带**一个**设施的政策更正。
- 同一批次允许对同一记录连续多条更正，按顺序施加，整批作为一个最终状态
  **同时可见**。
- 批次先整体校验：任何坏更正都会被拒绝，记一笔 `rejected` 台账，
  但当前版本与缓存完全不动，上一版继续供数。
- 文字没变但来源变了（撤回旧出处、同文新增出处），输出文本不变，
  但 `evidence` 出处证据同步更新，可核对。

## 局部重算（不重算无关设施/语境）

- 单词条更正：只重算其固定候选链**真正访问到该坐标**的目标；
  候选链从不包含它的语境不会被遍历。
- 通用词（`*`）更正：扇出到所有政策允许通用词的设施。
- 政策变更：重算受该政策支配的全部语境；其他设施不动。
- 此前缺失或被政策挡住的高优先级候选，在修订后会被重新判定并可能成为赢家。
- 每次重算都有计数（`recompute_stat`），测试据此断言重算范围精确。

## 离线词包同步（断网可读、续传、整包校验）

屏幕有时只能短暂联网。`termdb/sync.py` 在术语库之上增加离线词包：发布端
用本地接口模拟，设备把词包和下载日志落在本地目录；断网时继续读最后一份
完整安装的词包，重连后接着同一次下载，全部页校验通过才原子替换。

```python
from termdb import OfflinePublisher, ScreenDevice, TermStore

store = TermStore.open("terms.db")
pub = OfflinePublisher("publisher.db", store)
pub.publish("north-wing", [
    ("pump-hall", "en", "tech"),
    ("pump-hall", "zh", "tech"),
    ("gate", "zh", "tech"),
])

dev = ScreenDevice.open("screen-state", "north-wing", pub)
manifest = dev.sync()                      # none / full / delta
if manifest["mode"] != "none":
    for n in range(manifest["page_count"]):
        dev.accept_page(pub.fetch_page(manifest["download"], n))
    dev.install(manifest["download"])
print(dev.lookup("pump-hall", "en/tech"))  # 断网也可读
```

要点：

- 发布清单是 `(设施, 语种, 语境)` 三元组**集合**；同一次成功发布读取
  术语库的**同一个已提交状态**，每频道生成下一个版本号。未知坐标拒绝
  发布；当前不供数的坐标不进版本；只下发显示文字，不含出处/审核/证据。
- 更正术语不改写已发布版本；失败的发布不改变频道与既有版本的保留范围。
  每频道只保留最近 3 个成功版本。
- 差量只含净变化：文字新增/改变写入，原先有文字而现在不供数或退出清单
  的坐标删除；同文换源不传；净变化为空也能切到目标版本。
- 每页最多 128 条；同一次下载的频道/基版/目标/页数/页内容固定，重开
  发布端或出现新版本都不会让原页变成新内容。所需版本已淘汰时明确要求
  重新协商，绝不拼接不同下载的页。
- 设备重开可续传且不重取已收页；缺页、损坏、夹入其他下载的页、同页号
  内容相悖都拒绝安装。每页有哈希、整包有摘要，并提供可核对的 pack id。
- 安装为单文件原子替换：中断不会暴露半套写入/删除；差量必须基版精确
  匹配；迟到下载不回退；重复安装同一词包是 no-op。

```bash
python3 -I demo/sync_demo.py          # 可复现的端到端离线同步示例
python3 -I tests/fuzz_sync.py         # 随机同步模糊测试
```

详见 [docs/offline-sync.md](docs/offline-sync.md)。

## 展示次数对账（旧词包实际承担了多少术语展示）

`termdb/ledger.py` 在术语库**旁边**增加一个可重开、可补正的本地计数对账库，
用来回答“仍在使用的旧词包实际承担了多少次术语展示”，再决定是否催促换版。
它只落在本地 SQLite，不接网络、不连页面；安装身份可直接从现有
`ScreenDevice` 的本地词包读取，发布端已淘汰该版本也照常识别。

- **安装记录**以 `(逻辑屏幕, 时刻)` 为键，内容是当时生效的完整词包身份
  `(频道, 版本号, 包摘要)`、明确的“身份未知”，或撤销；它是安装事实，不是
  下发通知。
- **读数**以 `(屏幕, 计数轮次, 设施/语种/语境, 时刻)` 为键，是非负整数累计
  次数；换轮次即计数器重启，换播放器或频道不自动重启。
- 每个键只取最高修订；较低修订忽略，同最高修订内容冲突则**拒收整批**，
  重送无副作用；更高修订可撤销，并记住修订号防止旧值复活。
- 相邻有效读数 `(a,x)`、`(b,y)` 给出 `[a,b)` 的 `y-x` 次展示；首条只作基线，
  末条之后不估算。全程同一词包才归给它；只要有未知时段就是
  `identity_unknown`，全程可识别但换过包（含换回）就是 `cross_version`。
- 报表按右端读数时间范围 `[起点, 终点)` 选取、不按时长拆分，给出可归属次数
  （按屏幕/三元组/完整身份）、不可归属次数（按屏幕/三元组/原因）与逐笔非零
  增量，且只读取同一次已提交状态。
- 补报是增量的：读数更正只影响相邻区间，安装更正只影响本屏幕与生效状态变化
  相交的区间（含两端在外、中途穿过的区间）；报表不重读原始遥测。

```python
from termdb import CountLedger, LedgerBatch, InstallRecord, Reading

ledger = CountLedger.open("counts.db")
ledger.apply(LedgerBatch(
    installs=[InstallRecord.installed("lobby", 0, 1, ("north", 3, "pack"))],
    readings=[
        Reading.cumulative("lobby", "r1", "pump-hall", "en", "tech", 10, 1, 0),
        Reading.cumulative("lobby", "r1", "pump-hall", "en", "tech", 20, 1, 37),
    ],
))
print(ledger.report(0, 2 ** 62))
```

详见 [docs/display-accounting.md](docs/display-accounting.md)，或用
`python3 -I demo/ledger_demo.py` 看“迟到记录改变历史归属”的可复现示例。

## 审稿草稿与合入审批（本地，不打断线上供数）

译审员把一批设施术语更正交给同事审批时，线上词库仍会继续更新。
`TermStore.save_draft` / `merge_draft` 在现有更正批次之上增加本地两步流程：

```python
draft_id = store.save_draft(Batch([
    EntryCorrection("set_body", "pump-hall", Coord("zh", "tech"),
                    "s10", text="泵-v2"),
]))                                  # 线上供数不变，草稿可重开、不可改
result = store.merge_draft(draft_id)  # 审批人之后按草稿编号合入
```

- **草稿**沿用现有批次的输入与校验规则，以同一个已提交状态为基线，保存
  本批更正生效后的完整词条值、整份设施政策及各受影响目标的基线与草稿供数
  结果；保存不改变线上供数、不写台账、不发布词包。
- **审批用当时的线上状态**，绝不重放草稿里的编辑命令。每条完整词条与整份
  政策三方合并：线上没改就采用草稿值，线上和草稿改成同一值就保留，双方相对
  基线改成不同值记为**记录冲突**（指出词条键或政策设施及三方值）；区分
  词条不存在、撤回和明确清空。
- 即使没有记录冲突也**检查用词**（普通词按本设施引用该坐标的目标、通用词
  按各设施引用该坐标的目标、政策按其管辖的全部目标），每个目标比较基线 /
  仅草稿 / 当前线上 / 拟合入后的供数结果；只有一侧改结果则保留该侧，双方
  改且不同或拟合入改变应保留结果即为**供数冲突**（指出目标及四个结果）。
  出处证据变化本身不阻止合入。
- 成功时词条、政策与查询结果同一事务生效，并持久记录草稿已合入；重复审批
  已合入草稿不再写入。拒绝时线上词库逐字节不变，草稿仍可再次审批。
- 审批依据与生效结果对应同一次线上状态：不同连接提交在线更正与审批时，
  结果等同其中一个完整操作先完成，绝不覆盖核对后才发生的更正。
- 保留原有直接更正、在线查询、词包同步与展示对账接口；批准草稿不自动发布
  词包。重算只覆盖受影响目标，不复制整库、不逐个判定无依赖的设施或语境。

```bash
python3 -I demo/review_demo.py     # 可复现的保存/合入/冲突/供数危险示例
python3 -I tests/fuzz_review.py    # 10 种子 × 100 轮随机草稿
```

详见 [docs/review-drafts.md](docs/review-drafts.md)。

## 目录

```
termdb/
  model.py    领域模型与常量（词条身份、三态、政策、批次）
  engine.py   从零判定引擎（无缓存依赖，先资格后最高级出处）
  store.py    SQLite 存储：原子批次、局部重算、台账、重开、审稿草稿
  review.py   审稿草稿的三方合并与供数比较规则
  sync.py     离线词包：发布端快照、分页下载、差量、设备原子安装
  ledger.py   展示次数对账：修订合并、区间归属、增量维护、报表
tests/
  oracle.py          独立从零重写的参考判定器（不导入 engine）
  test_termdb.py     17 个端到端/边界/回归测试
  fuzz_scopes.py     随机差分模糊：每批后全量对照 oracle 并核对重算范围
  test_review.py     31 个草稿保存/合入/并发测试
  fuzz_review.py     10 种子 × 100 轮随机草稿，全量对照独立重算
  test_sync.py       42 个离线同步端到端测试
  fuzz_sync.py       10 种子 × 60 轮随机同步模糊
  test_ledger.py     33 个对账库端到端/边界/回归测试
  fuzz_ledger.py     12 种子 × 120 轮随机对账，全量对照独立重算
demo/demo.py         可运行的小参考，串联题目点名的五类核对
demo/review_demo.py  可复现的审稿草稿保存与合入示例
demo/sync_demo.py    可复现的离线词包同步示例
demo/ledger_demo.py  可复现的迟到记录改变历史归属示例
docs/review-drafts.md 草稿规则、合入判定、并发语义与本地示例
docs/offline-sync.md 离线同步协议、本地使用方式与可复现示例
docs/display-accounting.md 对账库规则、增量维护、使用方式与示例
```

## 运行

```bash
python3 -I -m unittest discover -s tests -v   # 123 个测试
python3 -I tests/fuzz_scopes.py              # 12 种子 × 400 随机批次
python3 -I tests/fuzz_review.py              # 10 种子 × 100 随机草稿轮
python3 -I tests/fuzz_sync.py                # 10 种子 × 60 随机同步轮
python3 -I tests/fuzz_ledger.py              # 12 种子 × 120 随机对账轮
python3 -I demo/demo.py                      # 端到端小参考
python3 -I demo/review_demo.py               # 审稿草稿保存与合入示例
python3 -I demo/sync_demo.py                 # 离线词包同步示例
python3 -I demo/ledger_demo.py               # 迟到记录改变归属示例
```

## 持久化与重开

所有状态落在单个 SQLite 文件（WAL 模式）：固定候选坐标、设施政策、当前词条、
结果缓存、重算计数、批次台账。`TermStore.open(path)` 重开后，查询结果、
台账与计数与关闭前逐字节等价，并可继续接受更正批次。
