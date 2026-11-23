# 本地审稿草稿与合入审批

译审员把一批设施术语更正交给同事审批时，线上词库仍会继续更新。这一层在
现有 `TermStore` 上增加两步：

1. 译审员用**现有更正批次的输入与校验规则**保存一份**草稿**；
2. 审批人稍后用**草稿编号**申请合入。

保存草稿不改变线上供数；合入是原子操作，只在不会覆盖别人不同修改、也不会
产生双方都没审过的供数结果时生效，否则逐项指出需要重审的位置。

```
termdb/review.py        三方合并与供数比较的纯规则
termdb/store.py         save_draft / draft / drafts / merge_draft 与持久化
tests/test_review.py    31 个端到端/边界/并发测试
tests/fuzz_review.py    10 种子 × 100 轮随机草稿，全量对照独立重算
demo/review_demo.py     可复现的保存、合入、冲突与供数危险示例
```

## 保存草稿

```python
draft_id = store.save_draft(Batch(
    entry_corrections=[
        EntryCorrection("set_body", "pump-hall", Coord("zh", "tech"),
                        "s10", text="泵-v2"),
    ],
    policy=None,            # 一个批次至多附带一个设施政策
))
```

- 输入、校验、单批次至多一个具体设施、同记录多条更正按序合成为一个最终
  状态——全部与直接更正相同；坏批次整批拒绝，不保存草稿。
- 草稿以**同一个已提交词库状态**为基线：词条以**完整值**（正文、公开标记、
  审核状态，缺失另记）快照，政策以整份设施政策快照；同时记录草稿侧在该
  基线上生效后的供数结果。
- 只有**真正改动**的内容进入草稿：净效果与基线相同的更正不算草稿改动。
- 保存后草稿不可修改；`draft(id)` 只读，重开库后仍可审批。
- **保存不改线上供数、不写批次台账、不推进共享状态计数**，也不发布词包。

## 合入审批

```python
result = store.merge_draft(draft_id)
# {'status': 'merged', 'seq': ..., 'records': ..., 'policy': ..., 'targets': ...}
```

审批**用当时的线上状态**判断，**绝不把草稿里的编辑命令重放到线上**。

### 完整词条与整份政策：三方合并

对草稿实际改动的每条完整词条、以及整份设施政策，都比较
`(基线, 草稿, 线上)`：

| 情况 | 结果 |
|---|---|
| 线上未改（线上 == 基线） | 采用草稿值 |
| 草稿未改（草稿 == 基线） | 沿用线上值 |
| 线上与草稿改成同一个值 | 保留该值 |
| 双方相对基线改成不同值 | **记录冲突** |

- “不存在”“撤回”“明确清空”是三种不同取值，绝不互相折叠。
- 记录冲突逐项给出：词条键 `(设施, 语种/语境, 出处标识)` 或政策设施，以及
  基线、草稿、线上三方完整值。
- 未被草稿改动的记录沿用线上值；一旦存在记录冲突，不再做供数检查，直接
  整单拒绝，线上词库逐字节不变。

### 用词结果：每个受影响目标比较四份结果

即使没有记录冲突，也要检查草稿会波及的每个目标的供数结果。检查范围按
现有局部重算规则（不遍历无关设施、不递归展开候选链）：

- **普通词条**改动：本设施内候选链**引用该坐标**的目标；
- **通用词**（`*`）改动：**各设施**中候选链引用该坐标的目标，**即使基线
  政策不允许通用词**（审批时政策可能已允许）；
- **政策**改动：该政策管辖的**全部**目标。

每个目标分别用现有判定引擎计算四份结果：**基线**、**仅草稿生效**、**当前
线上**、**拟合入后**。比较沿用现有结果类型 `supplied` / `explicit_clear` /
`conflict` / `blocked` / `no_available`；有正文时还比正文，同级文本冲突时
还比冲突正文集合。**出处证据变化本身不阻止合入。**

按“谁动了”决定必须保留的结果：

| 相对基线 | 必须保留 |
|---|---|
| 只有草稿改了结果 | 草稿的结果 |
| 只有线上改了结果 | 线上的结果 |
| 双方都改且结果相同 | 该结果 |
| 双方都改且结果不同 | 无法调和 → 供数冲突 |
| 拟合入结果 != 上述应保留结果 | **供数冲突** |

供数冲突逐项给出目标（设施 + 语种/语境）和四个供数结果（基线、草稿、线上、
拟合入后）。

#### 示例：通用词改动的供数危险

基线政策不允许通用设施 `gate` 使用通用词，`gate` 在 `en/doc` 没有自己的
记录，因此**不供数**。草稿把通用词 `generic-device` 改成
`generic-device-v2`。审批前线上放开 `gate` 的通用词，当前显示**旧的**通用
正文。虽然两边没有改同一条记录，若直接合入，`gate` 的显示会变成
`generic-device-v2`——这正是没人审过的供数结果，因此拒绝：

```
refused: no record conflict, but supply at gate / en/doc would move
  base   = None (no_available)
  draft  = None (no_available)
  live   = 'generic-device' (supplied)
  merged = 'generic-device-v2' (supplied)
```

## 拒绝与成功

- **拒绝**：抛出 `MergeConflict`，`record_conflicts` / `supply_conflicts` 至少
  一类非空；线上词库、结果缓存、重算计数、批次台账一律不变，草稿仍为
  `saved`，可再次审批。
- **成功**：词条、政策与全部受影响查询在**同一事务**内一起生效；草稿状态改为
  `merged` 并持久记录合入时的提交号。
- **重复审批已合入草稿**：抛出 `DraftAlreadyMerged`，不写任何更正。
- 审批只更新术语库，**不自动发布词包**；原有直接更正、在线查询、词包同步与
  展示对账接口均保持不变。

## 与并发在线更正一致

审批依据和生效结果必须对应**同一次线上状态**。存储用一个随每次已接受批次与
草稿合入在同一事务内推进的**共享状态计数**（`meta.state_counter`）来判断：

- 决定前，若另一连接已提交，先重载内存状态再判断；
- 决定后进入 `BEGIN IMMEDIATE`，再次核对计数；若期间有更正提交，则放弃本次
  尝试、重载并**重新做完整判断**（包括重新检查记录与供数冲突）；
- 因此用不同连接提交在线更正和审批时，结果等同于其中一个完整操作先完成：
  后提交的一方要么看到前者的结果并据此合并，要么记冲突，**绝不覆盖核对之后
  才发生的更正**。

## 局部性

- 保存草稿只重算草稿波及的目标，并额外纳入“基线政策禁止但审批时可能允许”
  的通用词候选；不复制整库，也不逐设施/逐语境判定无依赖关系。
- 合入只重算被检查的目标集合本身；未涉及的设施与候选链不重算。
- 检查范围由坐标 → 目标的反向索引直接得出，成本随改动坐标数与其引用目标数
  增长，而非随词库规模增长。

## 本地使用与可复现示例

```python
from termdb import Batch, EntryCorrection, Coord, TermStore, MergeConflict
from termdb import DraftAlreadyMerged

store = TermStore.open("terms.db")

# 译审员
draft_id = store.save_draft(Batch([
    EntryCorrection("set_body", "pump-hall", Coord("zh", "tech"),
                    "s10", text="泵-v2"),
]))

# 审批人（可能是另一次打开、另一进程）
try:
    print(store.merge_draft(draft_id))
except MergeConflict as exc:
    for c in exc.record_conflicts:
        print("record", c["facility"], c.get("coord"),
              c.get("source_id"), c["base"], c["draft"], c["live"])
    for c in exc.supply_conflicts:
        print("supply", c["facility"], c["target"],
              c["base"], c["draft"], c["live"], c["merged"])
except DraftAlreadyMerged:
    print("already merged; nothing written")
```

其它只读入口：

- `store.draft(draft_id)`：草稿的基线、完整改动、政策、每个目标的基线与草稿
  供数结果，以及合入状态。
- `store.drafts()`：全部草稿的编号、状态与合入提交号。

```bash
python3 -I demo/review_demo.py              # 临时目录跑完整流程
python3 -I demo/review_demo.py /tmp/keep    # 保留 SQLite 以便检查
python3 -I tests/fuzz_review.py             # 10 种子 × 100 轮随机草稿
python3 -I -m unittest discover -s tests    # 123 个测试
```

示例按顺序展示：保存草稿且线上不变、重开后仍可审批、合入生效且无关记录
不动、重复审批无写入、记录冲突列出三方值、通用词供数危险列出四份结果、直接
更正照常工作。
