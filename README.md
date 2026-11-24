# 溶洞游线照明与客流接续采录台

导游和值班台在一个合成班次内补录点名、门区步进计数、巡线和应急照明自检。值班台据此审核成组放行、洞段落位、洞内换手、班务交接与关洞清点。数据、人员标识、容量和照明判据均为合成模型。

## 运行

使用 Python 3.11+，在项目目录执行。仅使用 Python 标准库与自带的 SQLite，无需安装依赖。

```powershell
python -B -m caveflow --db demo.sqlite demo
python -B -m unittest discover -s tests -v
```

`demo` 在同一数据库上可重试。其末尾预期为 `seal_disputed`。`seal-final` 原始签署成功，随后迟到的点名更正交换了两名访客的到场身份，人数和当时的签署凭据都保持可核查。

```powershell
python -B -m caveflow --db shift.sqlite init fixtures/shift.json
python -B -m caveflow --db shift.sqlite load fixtures/01-opening.json
python -B -m caveflow --db shift.sqlite roster sheet-a --at 6
python -B -m caveflow --db shift.sqlite zone echo --at 6
python -B -m caveflow --db shift.sqlite gate --at 0
python -B -m caveflow --db shift.sqlite load fixtures/02-dispatch.json
python -B -m caveflow --db shift.sqlite receipt dispatch-amber-early
python -B -m caveflow --db shift.sqlite status --at 9
python -B -m caveflow --db shift.sqlite log
python -B -m caveflow --db shift.sqlite rebuild
```

## 采录接口

`record FILE` 接受一个 UTF-8 JSON 事件，`load FILE` 接受按到达顺序排列的非空事件数组。每个事件包含 `id`、`shift`、`kind`、`at` 和 `data`。`at` 是班次分钟，范围 0 到 1440。同一分钟的操作以采录顺序为准。现场证据可晚到，操作时间只向前推进。`status` 查询需落在最后一次操作分钟及以后。`roster`、`zone`、`gate` 可在指定历史分钟使用当前已收证据重新判断。

| `kind` | `data` 字段 | 决定 |
| --- | --- | --- |
| `roster.sheet` | `sheet guide members` | 固定点名对象和登记导游。 |
| `roster.mark` | `sheet person version mark` | 人员逐一递增修订，标记 `present`、`absent` 或 `excused`。 |
| `gate.anchor` | `entries exits` | 第 0 分钟建立计数原点。 |
| `gate.read` | `step version entries exits mode` | 逐步采集两个方向的累计值，`mode` 为 `normal` 或 `reset`。 |
| `gate.bridge` | `read tail_in tail_out base_in base_out witnesses` | 双人见证单个复位读数修订，分别记录旧刻度尾值和新刻度基值。 |
| `patrol.note` | `zone version state capacity inspector trace` | 留存巡检状态、承载人数和证据标识。 |
| `light.round` | `round zone tester` | 登记指定洞段在指定分钟的整轮自检。 |
| `light.check` | `round channel version reading` | 逐渠道归属同一轮次，渠道为 `marker` 或 `reserve`，判读为 `pass`、`weak` 或 `fail`。 |
| `escort.dispatch` | `group sheet guide` | 冻结点名和人数，检查前厅容量后入洞。 |
| `escort.move` | `group guide destination` | 当前代管人申请相邻洞段落位。 |
| `escort.offer` | `group guide receiver` | 记录指定洞段的护送换手提议。 |
| `escort.resolve` | `offer guide response` | 接方接收或拒绝，发起方可撤回。`response` 为 `accept`、`decline` 或 `withdraw`。 |
| `escort.exit` | `group guide` | 当前代管人在前厅确认整组出洞。 |
| `desk.offer` | `desk receiver` | 当前值班台给指定接方提交班务交接清单。 |
| `desk.accept` / `desk.withdraw` | `offer desk` | 接方核对清单并接单，提交方可以撤回。 |
| `close.seal` | `desk` | 当前值班台依据点名、护送和两向计数签署关洞。 |

`version` 从 1 开始连续增加。点名取截至查询分钟的最高版本。门区步号初次采录后固定分钟，同一洞段同一巡线分钟可修订，同一自检轮的渠道也可修订。巡线 `open` 使用额定容量，`restricted` 使用更低正容量，`closed` 容量为零。

## 裁决与恢复

- 洞段取最新有效巡线与最新自检轮的容量下限。照明一轮需要两个渠道，只剩弱备用且标志灯通过时最多两人。新轮缺项、失败、过期或巡线封闭会阻止落位。已进洞组继续留在台账并报告超容。
- 访客和登记导游计入冻结人数。换手接方是模型内的接续岗位。换手不会改变当时入洞的人员名册与门区计数口径。导游及访客不能同时归属两支在洞组，待签换手期间暂停该组位移和出洞。
- 门区按顺序重建可信步前缀。关洞要求计数覆盖签署分钟，入向与出向分别对账，无在洞组、未结换手、待签班务交接或出发时点的名单身份差异。关洞签署凭据永久保留，晚到证据按签署分钟及各组出发分钟出具新的复核状态。
- 班务交接清单冻结在洞组、占位、自检与巡线凭据、门区截点及已离场名单争议。接方签认时重查门区原截点和当前现场责任，清单变化需由当前值班台重新提交。交班和导游换手是两个不同的责任转移。
- 业务拒绝返回可查询回执，调用退出码为 0。无效结构、版本跳步或异文重用事件 ID 以退出码 2 回滚整批。相同事件可重试并读到原回执。`rebuild` 在单个事务内用同一配置和原始事件到达顺序重新生成投影及回执。日志不可用时重算回滚，保留之前的可查询投影。

编号夹具覆盖开班、两支队伍、洞内和班务交接、迟到步号与见证复位、关洞签署及等人数身份交换。`bad-packet.json` 覆盖完整坏批回滚。每个 SQLite 文件对应一套班次和固定拓扑，使用合成事件文件进行离线操作演练。

## Synthetic air probe review

After loading the opening evidence, run `python -B -m caveflow --db training.sqlite air-review fixtures/air-samples.json`. The packet identifies each raw reading and its zero calibration. The report checks the calibration interval and the zone evidence at the reading minute, then gives the corrected value with the supporting patrol and lighting records.
