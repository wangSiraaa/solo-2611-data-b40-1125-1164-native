# 春季菜园交接：土史、分畦、移箱与交单

这个模块在现有 `extcontract` 合同、原子 ingest 和 `publish()` 快照之上实现菜园交接，
不把作物史绑定在可更改的地名或箱子当前坐标上。

```bash
PYTHONPATH=. python3 -m extcontract.garden \
  --json examples/garden/report.json \
  --order examples/garden/handover-H2-BOXES.json

PYTHONPATH=. python3 -m pytest tests/test_garden.py -q
```

也可以从仓库根直接 `import garden`。

## 1. 旧茬算在哪？

算在**稳定土批来源**上，而不是新畦名、格名或箱当前摆放位置上。

- 种植行用稳定字段号 `source_id / season_rank / crop_family`。
- 分畦图记录格、位置节点、相邻关系、过道和实际覆盖。
- 取土登记记录“从哪格、哪批、取多少、留田还是装入哪个真实封底箱”。
- 格或箱若混入多个来源，`composition_of()` 会返回全部来源；每个来源的 `-1`（上季）
  和 `-2`（上上季）都必须存在。
- 任一来源在这两季中种过本户所报作物族，或任一季次缺失，该格/箱均不可签给该户。

因此，乙格上上季更正为豆后：

| 位置 | 土的来源 | 报豆的资格 |
|---|---|---|
| 甲格 `A` | `SOIL-A` | 可签 |
| 乙格 `B` | `SOIL-B` | 失格 |
| 一号箱 `BOX-1` | `SOIL-A` | 可签 |
| 二号箱 `BOX-2` | `SOIL-B` | 失格 |

即使把 `BOX-1` 移到乙旁，它仍继承 `SOIL-A`，不会改用乙格当前位置的履历。

## 2. 接入真实 publish 快照

内置三份合同：

- `garden-planting-r1`
- `garden-map-r1`
- `garden-soil-r1`

```python
from extcontract.garden import GardenLedger, GardenPlanner

ledger = GardenLedger()
ledger.ingest_files(initial_documents())   # 三份 extcontract 文件
ledger.ingest("planting", correction_doc)  # 更高 revision 的更正
world = ledger.build_world()               # 内部读取三份 publish()
planner = GardenPlanner(world, move_capacity=2)
```

`ledger.revision_basis()` 给每张交单附上当前 publish 中每行的有效 revision；
`Evidence` 保留 extcontract 字段级 `sources` 行出处。地名显示字段 `display_name`
可以升 revision 改名，但 `lot_id/source_id` 不变，资格判断不变。

## 3. 经营方式与交单

报名申请给出户号、作物族、相连处数和方式：

```python
planner.sign_application("H1", "豆", places=2, mode="land", shift=1)
planner.move_box("BOX-1", "PB", shift=2)
planner.sign_application("H2", "豆", places=2, mode="box", shift=2)
```

规则：

- `mode="land"`：只在未签格中找相连集合；签后按地界整季独占。
- `mode="box"`：只在未交真实箱号中找相连箱；每只箱的搬运路径必须通过宽度足够的
  准许通道，并占用一个 shift 搬运位。
- 报名班签署的交单默认由下一班兑现：`fulfillment_shift=shift+1`，因此搬运位按兑现班
  计算；显式 `move_box(..., shift=...)` 立即消耗该班一个搬运位。
- 箱一旦签署/交走，真实箱号即被占用；旧位置变空也不能再开给第二户。
- 一次申请必须属于同一种经营方式，不能把一块田和一只箱混成一种经营。
- 枚举所有相连子集；格数上限 10、户数上限 3。

交单 JSON 至少包含：

- `boundary`：已签稳定地界；或 `box_ids`：真实箱号；
- `source_ids`：每处继承的土批；
- `aisle_id/routes`：准许过道和搬运路径；
- `moves_required`：下一班所需搬运位；
- `revisions`：签署时各 publish 行的有效版本。

## 4. 规范情景的确定结果

排畦人采用确定性策略：在都可行时优先土地，并按稳定 ID 字典序选择第一组相连两处。

1. 初始两季均为禾：第一户 `H1` 签相邻甲、乙格，交单 `HO-H1-LAND`。
2. 更正乙格上上季为豆：乙格和二号箱失格；只剩甲格和一号箱，均不足同方式的相邻两处。
3. 恢复乙记录为禾并把过道本班交位收窄到 1：资格虽恢复，两处申请仍因
   `handoff_capacity=1` 不能兑现。
4. 下一班会把一号箱移到乙旁 `PB`；它与二号箱相邻且仍继承甲土。移动已消耗二班一个搬运位，
   所以若要求两只箱都在二班当场搬走，同班会因只剩一个搬运位而不可兑现；报名可先签
   `[BOX-1, BOX-2]`，交单默认由三班用新的两个搬运位兑现（`fulfillment_shift=3`）。
   第三户 `H3` 没有剩余的相邻两处。

查询占用时分别使用：

```python
planner.occupancy_by_boundary()  # 已交地界 -> H1/HO-H1-LAND
planner.occupancy_by_box()       # 真实箱号 -> H2/HO-H2-BOXES
planner.query("A")
planner.query("BOX-1")
planner.who_can_be_served([{"household_id": "H3", "places": 2}])
```

## 5. 纯字典输入

不想构造 extcontract 文件时，可直接使用：

```python
world = GardenWorld.from_dict({
    "sources": [{"source_id": "S1", "amount": "1"}],
    "plantings": [
        {"source_id": "S1", "season_rank": -1, "crop_family": "禾"},
        {"source_id": "S1", "season_rank": -2, "crop_family": "禾"},
    ],
    "lots": [
        {"lot_id": "A", "kind": "cell", "x": 0, "y": 0},
        {"lot_id": "B", "kind": "cell", "x": 1, "y": 0},
    ],
    "aisles": [{"aisle_id": "AISLE", "width": 2}],
})
```

混合土只需在 `coverage` 中列出每个来源；数量用精确整数字符串或 `"1/2"` 这类分数，
不使用浮点。
