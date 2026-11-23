# 展示次数对账库（本地、可重开、可补正）

术语库回答“屏幕上该显示什么词”。这一层回答另一个问题：

> 仍在使用的旧词包，实际承担了多少次术语展示？

它决定场馆是否该催促换版。整个库只落在本地 SQLite，不接网络、不连页面、
不调用发布端；屏幕台账可以迟到、可以补正、可以撤销。安装身份可直接从现有
`ScreenDevice` 的本地 `pack.json` 读取，即使发布端已经淘汰该版本也照常识别。

```
termdb/ledger.py     对账库
tests/test_ledger.py 33 个端到端/边界测试
tests/fuzz_ledger.py 12 种子 × 120 轮随机对账，全量对照独立重算
demo/ledger_demo.py  可复现的“迟到记录改变历史归属”示例
```

## 两类记录，同一条 UTC 毫秒时间轴

| 记录 | 键 | 内容 |
|---|---|---|
| 安装记录 `InstallRecord` | `(逻辑屏幕, 时刻)` | 当时生效的**完整词包身份** `(频道, 版本号, 包摘要)`，或**明确**标记身份未知，或撤销 |
| 读数 `Reading` | `(逻辑屏幕, 计数轮次, 设施, 目标语种, 语境, 时刻)` | 非负整数**累计**次数 |

要点：

- 安装记录是**安装事实**，不是“下发”或“开始下载”的通知。到达时屏幕已经
  在用该词包。它从自己的时刻起生效，直到下一条**有效**安装记录；首条之前
  身份未知。
- 身份是三段 `(channel, version, pack_id)`；不同频道即使版本号相同也是不同
  身份。
- 计数器重启换一个 `round_id`；**换播放器或换频道不会**自动重启采集器，
  仍属同一读数序列。
- 第一条读数只是基线（不估算它之前），最后一条之后也不估算。
- 所有时刻都是同一个 UTC 毫秒整数时间轴；不做时区换算，也不按时长拆分。

### 身份未知 ≠ 撤销

- **明确未知**记录：在它生效的时段里确实不知道装的是哪一版，覆盖到的区间
  记为 `identity_unknown`。
- **撤销**记录：这条安装事实不存在了；该时段回落到上一条有效记录，可能因此
  变成可归属。两者语义不同，报表里也分得开。

## 修订规则（每个键只取最高修订号）

| 情况 | 结果 |
|---|---|
| 批次内 / 与库内同键的较低修订 | 忽略 |
| 更高修订，内容不同 | 修订（可撤销） |
| 更高修订，内容相同 | 无副作用 |
| 同最高修订、内容相同（重送） | 无副作用，连提交号都不前进 |
| 同最高修订、内容冲突（批内或对库） | **拒收整批**，台账、读数与已对账结果一律不变 |

- 更高修订可把记录改成 `revoked`。撤销后仍以墓碑记住该修订号，迟到的更低
  修订**不能复活**旧值；只有更高修订能再次改写。
- 累计次数以**整批提交后的有效读数**判断是否倒退；只在同一计数轮次内比较，
  跨轮次从不比较。倒退、坏输入、冲突都使整批不生效。

## 区间与归属

同一计数序列相邻两条有效读数 `(a, x)`、`(b, y)` 给出 `[a, b)` 上的
`y - x` 次展示。归属规则：

1. 区间内**全程**能确认是同一个词包 → 归给该完整身份（`pack`）。
2. 区间内**只要有未知时段** → `identity_unknown`（优先于其它原因）。
3. 全程可识别但中途换过包（即使随后换回同一版）→ `cross_version`。
4. 恰好在 `b` 安装的新词包**不影响** `[a, b)`。
5. 零增量区间不进入报表，但它仍参与区间划分（仍会切分左右区间）。

## 报表

```python
report = ledger.report(start_ms, end_ms)   # 右端读数时刻落在 [start, end)
```

- 按**指定右端读数时间范围**选取区间；区间整体计入，**不按时长拆分**。
- `attributed`：按 `(屏幕, 设施, 语种, 语境, 完整身份)` 汇总的正向次数。
- `unattributed`：按 `(屏幕, 设施, 语种, 语境, 原因)` 汇总；原因为
  `identity_unknown` 或 `cross_version`。
- `increments`：每笔**非零**增量的区间 `[a, b)`、所属计数轮次 `round_id`、
  次数，以及完整身份或原因，供运营逐笔核对 `total = attributed + unattributed`。
- 整份报表只读取**同一次已提交状态**（`state_seq` 一起返回）。

## 增量维护（补报不重算全库）

对账结果以区间段（segment）物化：

- **读数更正**只重建与更正时刻相邻的区间：把该时刻与其前后邻居之间的局部
  区间重新切分/合并，其它区间和屏幕不动；单调性也只查更正时刻的左右邻居。
- **安装更正**只重建本屏幕中与“生效状态可能变化时段”相交的区间
  `[change, next_install)`，**包括两端都在该时段之外、但内部穿过它的区间**。
- 报表只读区间段表，不重读原始遥测；补报成本取决于更正邻域的大小，而非台账
  规模。`commit_log.segments_touched` 记录每次提交触碰的区间数，测试据此断言
  局部性（例如 400 条读数的序列中，改一条读数只触碰 ≤ 4 段）。

## 本地使用

```python
from termdb import (
    CountLedger, LedgerBatch, InstallRecord, Reading, identity_from_device,
)

ledger = CountLedger.open("counts.db")     # 与术语库并排的独立文件

# 从设备已安装词包读取完整身份（无网络；发布端淘汰该版本也可读）
identity = identity_from_device(device)

ledger.apply(LedgerBatch(
    installs=[InstallRecord.installed("lobby", 1_700_000_000_000, 1,
                                      identity)],
    readings=[
        Reading.cumulative("lobby", "round-1", "pump-hall", "en", "tech",
                           1_700_000_010_000, 1, 0),
        Reading.cumulative("lobby", "round-1", "pump-hall", "en", "tech",
                           1_700_000_020_000, 1, 37),
    ],
))

print(ledger.report(start_ms, end_ms))
```

其它入口：

- `InstallRecord.unknown(screen, t, rev)`：明确身份未知。
- `InstallRecord.revoke(screen, t, rev)` / `Reading.revoke(...)`：撤销。
- `ledger.effective_installs(screen)` / `ledger.effective_readings(...)` /
  `ledger.segments(...)`：核对当前有效记录与物化区间。
- `ledger.commits()`：每次提交写入的安装/读数条数与触碰区间数。
- `CountLedger.open_near("terms.db")`：在术语库旁打开对账库（仅便利，二者
  不共享表或事务）。

## 可复现示例：迟到记录改变历史归属

```bash
python3 -I demo/ledger_demo.py            # 临时目录跑完整流程
python3 -I demo/ledger_demo.py /tmp/keep  # 保留 SQLite 以便检查
```

示例按顺序展示：

1. 首批发安装记录与累计读数，报表按完整身份汇总；
2. **迟到**的安装记录落在某区间内部（`1500`），使该区间变为
   `cross_version`，而其后区间仍归给新词包；
3. **迟到**的读数（`2500`）把一段区间切成两段并各自归属；
4. 明确未知时段的次数单列，不算给最新版；
5. 按右端读数时间选范围、不拆分；
6. 同修订冲突与累计倒退都被整批拒绝，台账/区间/报表不变；
7. 撤销安装记录（区别于“身份未知”）让旧词包重新覆盖历史；
8. 重开后报表与提交号逐字节一致。

## 与现有部分的关系

`TermStore` 的查询、批次、台账、局部重算与离线同步全部保持原样；对账库是
并排的独立 SQLite 文件，只读取设备本地的已安装词包身份，**不写入**术语库、
不接触发布端或页面。原有测试与示例照常运行。

```bash
python3 -I -m unittest discover -s tests -v   # 123 个测试
python3 -I tests/fuzz_ledger.py               # 12 种子 × 120 轮随机对账
python3 -I demo/ledger_demo.py                # 迟到记录改变归属示例
```
