# 课表换班采录台

从本地排课桌面软件导出的课节快照核定教室占用；从教师名单与委托回执裁定代课资格；从班级通知回执判定换班是否生效。教室、教师、通知分别有独立状态，任何一方不满足都会改变生效结论。原始导出字节和 SHA-256 保留在 SQLite，JSON 报告包含来源与逐项业务决定。

入口：`room-import`、`substitute-assign`、`notice-evaluate`，均为 `--db ledger.db export.json`。`substitute-withdraw` 会撤回代课并重开已生效通知，`notice-resend` 只在当前版本和冻结名单内补回漏送班级。正常与失败样例见 `tests/test_workflows.py`，涵盖跨日占用、容量、资质、并行教学、旧版通知与重试恢复。

`plan-reschedule` 对多门受扰课程联合匹配教室和教师：搜索时同时考虑当前房间占用、容量、教师资质与忙时，以及计划内互斥，优先最大化可安排课数，再降低偏好与空座成本。`approve-reschedule` 仅允许完整计划在当前版本经批准后生成新的房间与代课实体；这些实体随后可被通知系统使用。部分计划保留待协调状态，重放同一批准不会重复落地。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m schedulehandoff demo`，`python -m schedulehandoff --help`，`python -m pytest -q`。所有演示和测试使用虚构本地文件，不访问网络。
