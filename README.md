# 课表换班采录台

增强素材另有独立的教室空气读数入口：`python -m schedulehandoff air-import --db ledger.db fixtures/air-ok.json`。单次 CO2 读数仍只与固定阈值比较；换班期间的通风结论由下文的有界质量守恒评估给出，不估计单一“最优”通风量，也不由单次空气读数自动改写课表排班与代课资金。

## 换班期间通风评估

一节课换进新教室后 CO2 上升，单看阈值无法分辨人数变化、传感器偏移还是新风不足。通风评估把课表课节（起止时刻与 enrolled，enrolled 只是应到人数）与本地采录的空气证据合成**可复核的换气比例可证范围**：

- `air-venue`：教室体积（已折算）、活动源项范围（ppm/人/步，同场各教室共用同一活动系数）、相邻房间送回风关系（同一股回风在两侧平衡中共用）、室外背景。
- `air-calibrate`：传感器零点与响应滞后范围，对同一连续读数序列共用，不能逐读数切换。
- `air-doors`：入离室门计数。带时间戳的计数或绝对人数移动占用区间；只有时段总数的记录只压低占用上界，不凭总数制造逐分钟占用。
- `air-fanlog`：风机日志，直接界定该时段换气比例范围。
- `air-mandate`：事先签定的教室通风委托（`预防性巡检` 或 `达标公示`，含约定换气比例）。委托必须在采样前固定：该房间该时段已有读数后新委托被拒绝，登记后种类与阈值不可更改。
- `air-assess [--mandate ID]`：对证据有变化的委托重算当前结论。

离散模型（体积已折算）：`末值 = 初值 + 源项 - 换气比例 × (初值 - 室外值)`，相邻房间再加上共享回风项。求解器求的是**所有**符合人数、传感器与送回风证据的换气比例完整范围（精确有理数单纯形），并给出使结论两边都成立的两套完整解释。最小反例：室外 400、室内 800→900、源项 140–260，则换气比例可证范围 [0.1, 0.4]，单条曲线无法证明是否达到约定的 0.25——预防性巡检委托在存在低于约定值的相容解释时**派查**（派查不等于已证不达标），达标公示委托只在所有相容解释都不低于约定值时**可签发**（介于其间为**待证**，全部低于为**不签发**）。证据自相矛盾时记**证据冲突**；外来回风能解释邻室升高时，邻室不会被判成设备故障。

校准、门计数、课节时段或风机日志迟到更正时，只重算受影响期间的当前结论；旧来源、旧参数范围、冲突记录与原先的委托决定全部保留在 SQLite 与报告中。报告的 `ventilation` 段列出每段实际占用范围、室外背景、传感器共用校准、房间间流量守恒、换气比例可证范围与两套相容解释及历史。

示例（fixtures 即最小反例）：

```
python -m schedulehandoff room-import --db ledger.db fixtures/schedule-r1.json
python -m schedulehandoff air-venue --db ledger.db fixtures/air-venue.json
python -m schedulehandoff air-calibrate --db ledger.db fixtures/air-cal.json
python -m schedulehandoff air-mandate --db ledger.db fixtures/air-mandates.json
python -m schedulehandoff air-import --db ledger.db fixtures/air-samples-r1.json
python -m schedulehandoff air-assess --db ledger.db        # k ∈ [0, 0.725]：派查 + 待证
python -m schedulehandoff air-doors --db ledger.db fixtures/air-doors.json
python -m schedulehandoff air-assess --db ledger.db        # k ∈ [0.1, 0.4]：仍派查 + 待证
python -m schedulehandoff air-import --db ledger.db fixtures/air-samples-r2.json
python -m schedulehandoff air-doors --db ledger.db fixtures/air-doors-r2.json
python -m schedulehandoff air-fanlog --db ledger.db fixtures/air-fanlog.json
python -m schedulehandoff air-assess --db ledger.db        # 共用源项被邻室校准为 10：k = 0.25，免查 + 可签发
```

从本地排课桌面软件导出的课节快照核定教室占用；从教师名单与委托回执裁定代课资格；从班级通知回执判定换班是否生效。教室、教师、通知分别有独立状态，任何一方不满足都会改变生效结论。原始导出字节和 SHA-256 保留在 SQLite，JSON 报告包含来源与逐项业务决定。

入口：`room-import`、`substitute-assign`、`notice-evaluate`，均为 `--db ledger.db export.json`。`substitute-withdraw` 会撤回代课并重开已生效通知，`notice-resend` 只在当前版本和冻结名单内补回漏送班级。正常与失败样例见 `tests/test_workflows.py`，涵盖跨日占用、容量、资质、并行教学、旧版通知与重试恢复。

`plan-reschedule` 对多门受扰课程联合匹配教室和教师：搜索时同时考虑当前房间占用、容量、教师资质与忙时，以及计划内互斥，优先最大化可安排课数，再降低偏好与空座成本。`approve-reschedule` 仅允许完整计划在当前版本经批准后生成新的房间与代课实体；批准时还会重新核对计划产生后新增的教室与教师占用，旧计划遇到资源变化会记录拒绝结论。这些实体随后可被通知系统使用。部分计划保留待协调状态，重放同一批准不会重复落地。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m schedulehandoff demo`，`python -m schedulehandoff --help`，`python -m pytest -q`。所有演示和测试使用虚构本地文件，不访问网络。
