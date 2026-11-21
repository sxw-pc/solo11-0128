# 课表换班采录台

从本地排课桌面软件导出的课节快照核定教室占用；从教师名单与委托回执裁定代课资格；从班级通知回执判定换班是否生效。教室、教师、通知分别有独立状态，任何一方不满足都会改变生效结论。原始导出字节和 SHA-256 保留在 SQLite，JSON 报告包含来源与逐项业务决定。

入口：`room-import`、`substitute-assign`、`notice-evaluate`，均为 `--db ledger.db export.json`。`substitute-withdraw` 会撤回代课并重开已生效通知，`notice-resend` 只在当前版本和冻结名单内补回漏送班级。正常与失败样例见 `tests/test_workflows.py`，涵盖跨日占用、容量、资质、并行教学、旧版通知与重试恢复。

`plan-reschedule` 对多门受扰课程联合匹配教室和教师：搜索时同时考虑当前房间占用、容量、教师资质与忙时，以及计划内互斥，优先最大化可安排课数，再降低偏好与空座成本。`approve-reschedule` 仅允许完整计划在当前版本经批准后生成新的房间与代课实体；批准时还会重新核对计划产生后新增的教室与教师占用，旧计划遇到资源变化会记录拒绝结论。这些实体随后可被通知系统使用。部分计划保留待协调状态，重放同一批准不会重复落地。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m schedulehandoff demo`，`python -m schedulehandoff --help`，`python -m pytest -q`。所有演示和测试使用虚构本地文件，不访问网络。

增强素材提供 `macro-plan` 与 `macro-reconcile`，把确认过的课表经模拟桌面自动化写回排课软件时的安全边界落在两个入口上。两个入口使用 `--db ledger.db export.json`；样例见 `samples/` 与 `tests/test_rpa_macro.py`。

**计划（`macro-plan`）** 在执行前冻结操作合同，每行录制字段：

- `id`：本次宏运行标识；`project`/`window`：允许操作的项目与窗口身份。
- `base_version`：发布前项目版本；`target_summary`：本轮目标摘要。二者冻结后用于执行期核对。
- `contract`：`draft`（可覆盖的草稿保存）或 `publish`（不可逆的正式发布，会触发班级通知）。合同在执行前固定，同一 `id` 的录制一旦采入即冻结，事后把正式发布改叫草稿或换成另一项目会触发 `identity_conflict`。
- `steps`：`kind ∈ open/type/click/verify`。`open/type/verify` 分类为可重复输入；`click` 必须带 `action`：`save_draft` 分类为可覆盖草稿保存，`publish` 分类为不可逆发布。草稿合同不得含发布点击且至少一次草稿保存；发布合同必须恰好一次发布点击。

**恢复（`macro-reconcile`）** 对每行恢复评估给出下一步（结果 `next`：`继续`/`结束`/`待复核`），字段：

- `id`：本次恢复评估标识；`run`：引用已计划的宏；`operator`：操作者标识（独占操作权的持有者）。
- `events`：模拟窗口事件，`kind ∈ window_focus/screen_state/step_ok/save_click/publish_click/save_ack/publish_ack/frame_gap/popup/local_export`，可带 `window`、`project`、`state`（每步读屏状态，如 `draft:v3`）、`at`（ISO 时间）。
- `export`：可选的带版本只读发布导出，字段 `project`、`published_version`、`published_summary`、`holder`（导出记录的发布权持有者）、`generated_at`。

判定规则：看到对应回执（`publish_ack`/`save_ack`）即完成；任何事件的窗口或项目与冻结身份不符、录制缺帧或弹窗遮挡，都停在待复核。草稿合同下保存回执丢失时，读屏确认仍在同一草稿（`draft:<base_version>`）即可受控再保存。发布合同下点击已发生而回执缺失时，本地事件无法分辨“点击已生效、回执丢了”与“点击未生效”，必须查只读导出：目标摘要已发布则完成且不再点击；仍为 `base_version` 且导出未显示他人持有发布权时，取得本项目独占租约（`automation_lease`，跨进程原子获取）后允许一次受控重试；导出早于点击（滞后）、版本已前进到别人的修订、导出显示他人持有发布权或租约被别的进程持有，都停在待复核。缺少导出时不得再点击。

原始录制字节、版本核对（每次恢复评估的 `version_check`）、操作权持有者与每次恢复决定都入库；`report` 的 `macro_replay` 按宏运行回放冻结合同、租约持有者与各次恢复决定，可说明“为什么没有第二次点击”。
