# 课表换班采录台

从本地排课桌面软件导出的课节快照核定教室占用；从教师名单与委托回执裁定代课资格；从班级通知回执判定换班是否生效。教室、教师、通知分别有独立状态，任何一方不满足都会改变生效结论。原始导出字节和 SHA-256 保留在 SQLite，JSON 报告包含来源与逐项业务决定。

入口：`room-import`、`substitute-assign`、`notice-evaluate`，均为 `--db ledger.db export.json`。`substitute-withdraw` 会撤回代课并重开已生效通知，`notice-resend` 只在当前版本和冻结名单内补回漏送班级。正常与失败样例见 `tests/test_workflows.py`，涵盖跨日占用、容量、资质、并行教学、旧版通知与重试恢复。

`plan-reschedule` 对多门受扰课程联合匹配教室和教师：搜索时同时考虑当前房间占用、容量、教师资质与忙时，以及计划内互斥，优先最大化可安排课数，再降低偏好与空座成本。`approve-reschedule` 仅允许完整计划在当前版本经批准后生成新的房间与代课实体；批准时还会重新核对计划产生后新增的教室与教师占用，旧计划遇到资源变化会记录拒绝结论。这些实体随后可被通知系统使用。部分计划保留待协调状态，重放同一批准不会重复落地。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m schedulehandoff demo`，`python -m schedulehandoff --help`，`python -m pytest -q`。所有演示和测试使用虚构本地文件，不访问网络。

增强素材新增 `equipment-checkout` 与 `equipment-return`，采录离线课堂设备扫码借出与归还，并由保管责任链引擎裁定缺件责任。入口使用 `--db ledger.db export.json`；可运行样例见 `tests/test_equipment.py`。

## 器材保管责任链

设备不再按借出/归还序列号集合的简单差异报 `serial_mismatch`。每件序列号都从借出根事件出发，结合**已签**单件/整套交接、站点扫码与允许的设备时钟偏差，枚举所有相容保管链（见 `schedulehandoff/custody.py`）：

- 未签名交接不转移保管责任，只作为待证证据；迟到签名经 `equipment-sign` 补入后重算。
- 同型号替代件按序列号区分，不能冒充原序列号；同站点同时间的重复扫码只计一次。
- 时钟偏差窗口内顺序可互换，所有线性扩展都会被实际遍历，结论不依赖文件行序。
- 多条相容链把缺件放在不同持有人手中时一律 `责任待核`，并在 `minimum_evidence` 给出最少还需哪张签收或哪个站点扫描；已签交接在任何链中都无法满足时为 `证据冲突`，给出最小冲突证据，绝不把最后扫码人记为赔偿人。
- 跨套混还从两套借出记录一起核对：显式链接且与套装一致的扫码才结清借用，混入异物只记录不结清，同一序列号不能同时结清两份借用。

两种使用协议在借出时固定（`custody_contract`）：`personal_custody` 下未完成逐件签收移交的物品由领用人负责，唯一相容持有人即赔偿人；`team_shared` 下同课组教师对缺件共同承担，直到每件独立归还。新证据改变结论时，已发给教师的旧赔偿通知保留当时依据并由带版本的调整单（`adjustment_order`）冲销，案件以 `decision_seq` 递增，重复导入同一扫码文件不会重复开赔偿。课表/通知是否生效与器材赔偿完全无关。

新增入口：`equipment-checkout`、`equipment-handoff`、`equipment-sign`、`equipment-return`，均为 `--db ledger.db export.json`。离线扫码样例与逐字段说明在 `samples/`（`checkout.sample.json`、`handoff.sample.json`、`signature.sample.json`、`return.sample.json`、`FIELDS.md`）。`report` 输出的 `equipment` 段回读每件序列号的相容链、相容持有人、责任合同、案件版本、通知、调整单及中文裁决说明。测试见 `tests/test_custody_engine.py`（纯链引擎）与 `tests/test_equipment_custody.py`（两种协议、单件/整套移交、同型号替代、跨套混还、重复扫码、时钟偏差、迟到签名、旧通知冲销与重放）。
