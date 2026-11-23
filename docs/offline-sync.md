# 离线词包同步（本地发布端 / 本地设备，无网络服务）

场馆导览屏有时只能短暂联网。这一层在原有术语库之上增加**离线词包**：

- 屏幕断网时继续用本地最后一份**完整安装**的词包供数；
- 重新连上后**接着同一次下载**继续，不必重取已收齐的页；
- 只有全部页都到齐且校验通过，才**整体替换**本地词包——任何中断都不会
  留下半套写入或半套删除。

发布端与设备都用本地接口模拟（两个 SQLite 文件 + 一个设备目录），
不实现任何网络服务或屏幕界面。

## 领域对象

| 对象 | 说明 |
|---|---|
| `OfflinePublisher(path, store)` | 发布端。只读 `TermStore`，自身状态存放在独立 SQLite 文件。 |
| `ScreenDevice.open(dir, channel, publisher)` | 绑定一个频道的设备。`dir/pack.json` 是已安装词包，`dir/device.db` 是下载日志。 |
| 版本（version） | 某频道的一次**成功发布**。版本号在该频道内单调递增，`1` 起。 |
| 词包身份（pack id） | 由 `(格式, 频道, 版本, 全部条目文字)` 派生的 SHA-256，用来核对“装出来的包就是目标版本”。 |
| 下载身份（download id） | 由**清单**（频道、模式、基版、目标版本、各页哈希、整包摘要）派生的 SHA-256。同一清单永远得到同一身份。 |

## 本地使用方式

### 1. 发布

```python
from termdb import OfflinePublisher, TermStore

store = TermStore.open("terms.db")            # 原有在线订正/查询能力不变
pub = OfflinePublisher("publisher.db", store)

# 清单是集合：每条为 (设施, 目标语种, 语境)
triples = [
    ("pump-hall", "en", "tech"),
    ("pump-hall", "zh", "tech"),
    ("gate", "zh", "tech"),
]
meta = pub.publish("north-wing", triples)
print(meta["version"], meta["pack_id"], meta["entries"])
```

发布语义：

- 发布在**同一个已提交状态**上读取整份清单；读取期间术语库若被提交修改，
  整次发布记为 `rejected`，频道指针与既有版本完全不变。
- 清单按**集合**处理：重复三元组自动合并，顺序不影响内容。
- **未知设施或未知坐标**直接拒绝整次发布，不产生新版本。
- 已知坐标如果当前**不能供数**（`explicit_clear` / `conflict` /
  `blocked` / `no_available`），**不会**出现在该版本里；发布端绝不替
  冲突挑一个赢家。
- 版本只包含**当前可用的显示文字**：`{设施, 坐标, 文字}`。不包含原词条、
  出处标识、审核状态或证据轨迹。
- 每次成功发布产生该频道下一个版本；此后对术语库的更正**不会**改写任何
  已发布版本。
- 发布失败（含未知坐标、术语库事务进行中、读取期间被修改）只记一笔
  `rejected` 台账，**不改变既有版本或其保留范围**。
- 每个频道只保留最近 `KEEP_VERSIONS`（= 3）个成功版本的可同步数据，
  更早的版本连同依赖它们的下载页一并淘汰。

### 2. 设备同步

```python
from termdb import ScreenDevice

dev = ScreenDevice.open("screen-state", "north-wing", pub)

manifest = dev.sync()          # 用已安装版本身份协商
if manifest["mode"] == "none":
    ...                        # 已是最新版，无需传输
else:
    for n in range(manifest["page_count"]):
        dev.accept_page(pub.fetch_page(manifest["download"], n))
    dev.install(manifest["download"])
```

协商规则：

| 设备状态 | 结果 |
|---|---|
| 已安装版本 == 频道最新版 | `mode == "none"`，不传输 |
| 有已安装版本且该版本仍保留 | `mode == "delta"`，只含两版之间的净变化 |
| 没有已安装版本 / 基版无法识别 / 基版已淘汰 | 自动改为 `mode == "full"` 全量词包 |

`accept_page` 的校验（任何一条不满足都抛 `SyncError`，且不写日志）：

- 页必须属于**本设备协商过的那次下载**（下载身份、频道一致）；
- 页号必须在 `0..page_count-1`；
- 每页最多 128 条；
- 页内容必须匹配协商时记录的**该页哈希**；
- 重复领取同一页是幂等的；同一页号内容相悖则拒绝。

`install` 的保证：

- 缺页、页损坏、整包摘要不符、目标版本内容身份不符 → 拒绝安装，磁盘不变；
- 全量安装后，本地**只保留目标版本的坐标**（旧坐标全部消失）；
- 差量安装要求**当前安装版恰好是该差量的基版**（版本号与基版 pack id
  都要一致），否则拒绝而不是合并；
- 迟到或重复的下载（目标版本 ≤ 已安装版本）→ `noop`，绝不回退；
- 安装是**单文件原子替换**：新词包整体写入临时文件后 `rename` 到位，
  因此任何时刻中断，重开后读到的要么是完整旧包，要么是完整新包；
- 完整安装后重开设备、即使没有发布端，也能用 `lookup()` 离线读取。

### 3. 断点续传

下载日志（已收页、下载身份、页哈希）随设备目录持久化。重开设备后：

```python
dev = ScreenDevice.open("screen-state", "north-wing", pub)
pending = dev.pending_downloads()      # 含 received / missing
manifest = dev.sync()                  # 幂等地拿回同一个 download id
for n in dev.missing_pages(manifest["download"]):
    dev.accept_page(pub.fetch_page(manifest["download"], n))
```

同一份清单总是给出同一个 `download` 身份与逐字节相同的页；发布端重开、
或期间又发布了更新的版本，都不会改变既有下载的页内容。

### 4. 差量的净变化规则

设基版条目映射为 `old`，目标版本为 `new`（键是 `(设施, 坐标)`）：

- `new[k] != old[k]` → 下发新文字（新增或改变）；
- `k ∈ old` 但 `k ∉ new`（当前不供数，或已退出清单）→ 下发空文字，
  设备安装时删除该坐标；
- `new[k] == old[k]` → **不下发**：只换出处、或中间改过但最终文字相同，
  都不产生差量行；
- 净变化为空时仍然产生一个 0 页的差量，设备安装后版本号前进到目标版本。

### 5. 保留窗口与重新协商

频道只保留最近三个成功版本。任何需要已淘汰版本的下载（目标已被淘汰，
或差量的基版已被淘汰）在取页时都会得到明确的重新协商信号：

```python
from termdb import RenegotiateError

try:
    pub.fetch_page(download_id, 0)
except RenegotiateError:
    manifest = pub.negotiate("north-wing", None)   # 全量重新开始
```

设备侧 `dev.sync()` 会自动完成这一步：基版不可识别时改要全量包。
发布端绝不会把不同下载的页拼在一起。

## 可复现示例

```bash
python3 -I demo/sync_demo.py            # 用临时目录跑完整流程
python3 -I demo/sync_demo.py /tmp/keep  # 保留 SQLite 与设备目录以便检查
```

示例按顺序演示：发布快照（只含显示文字）→ 断网 → 重连续传 → 更正不
改写已发版本 → 保留基版走差量 → 同文换源不产生差量行 → 三版本保留窗口
与重新协商 → 损坏/缺页拒绝安装 → 无发布端离线重开读取。

对应的自动化覆盖：

```bash
python3 -I -m unittest discover -s tests -v   # 59 个测试（含 42 个同步测试）
python3 -I tests/fuzz_sync.py                 # 10 种子 × 60 轮随机同步
```

## 不变的部分

`TermStore.query()` / `apply_batch()` / 台账 / 局部重算 / 重开等价性完全
保持原样；离线层只读当前查询结果，不修改术语库。原有 `demo/demo.py`、
`tests/test_termdb.py`、`tests/fuzz_scopes.py` 照常运行。
