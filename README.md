# 课表换班采录台

从本地排课桌面软件导出的课节快照核定教室占用；从教师名单与委托回执裁定代课资格；从班级通知回执判定换班是否生效。教室、教师、通知分别有独立状态，任何一方不满足都会改变生效结论。原始导出字节和 SHA-256 保留在 SQLite，JSON 报告包含来源与逐项业务决定。

入口：`room-import`、`substitute-assign`、`notice-evaluate`，均为 `--db ledger.db export.json`。`substitute-withdraw` 会撤回代课并重开已生效通知，`notice-resend` 只在当前版本和冻结名单内补回漏送班级。正常与失败样例见 `tests/test_workflows.py`，涵盖跨日占用、容量、资质、并行教学、旧版通知与重试恢复。

`plan-reschedule` 对多门受扰课程联合匹配教室和教师：搜索时同时考虑当前房间占用、容量、教师资质与忙时、计划内互斥，以及教师与班级两条行程链的版本化通行路线，优先最大化可安排课数，再降低偏好与空座成本。课节导出新增所属班级、教室地点、教师实际授课点（远程课为教师所在点位）与远程标记；`route-import` 导入带版本的已核定路线（可续版、可关闭），`service-import` 导入接单时冻结的班级到课服务，重复导入必须逐字一致，不能靠临时改服务放行。

教师链按实际授课点、班级链按教室地点分别排序核链：相邻课节按选定服务的已核定路线比较课间分钟数与所需分钟数（恰好相等可行），缺路线、缺服务或缺地点一律保留待协调，远程教师按实际授课点行走、班级仍按教室地点行走。赶不上的组合带相邻课节、路线 id/版本与分钟数进入淘汰原因；班级行程无解会把计划压成 `待协调`。`approve-reschedule` 仅允许完整计划在当前版本经批准后生成新的房间与代课实体；批准时按最新已生效课节与路线版本重算双链，计划后增课（`lesson_added_since_plan`）、关路（`route_closed_since_plan`）或路线换版（`route_version_changed_since_plan`）拒绝旧计划。待协调计划无法落地 `room_slot`/`substitution`，`notice-evaluate` 因此只能判 `schedule_unresolved`，回执齐全也不生效。部分计划保留待协调状态，重放同一导入不会重复落地。字段与离线 JSON 样例见 `examples/FIELDS.md`，行程专项测试见 `tests/test_travel_itinerary.py`。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m schedulehandoff demo`，`python -m schedulehandoff --help`，`python -m pytest -q`。所有演示和测试使用虚构本地文件，不访问网络。
