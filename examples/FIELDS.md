# 离线桌面导出 JSON 字段说明（两校区联合换班）

所有文件均为本地桌面软件导出的 UTF-8 JSON，顶层必须带字符串 `version`。
原始字节与 SHA-256 保存在 SQLite `export_file`，重复导入同一文件不重复占用。

## 1. 通行路线 `desktop_travel_route`（命令 `route-import`）

带版本的已核定通行路线。同 `id` 可以用**更高版本**续发；同版本重复导入必须逐字一致，
否则记 `identity_conflict`。状态为 `已关闭` 的新版本用于关路。

| 字段 | 含义 |
| --- | --- |
| `id` | 路线身份（起终点+服务的稳定标识，换版本不变） |
| `from` / `to` | 起点、终点地点；查询时双向等价，`{from,to}` 集合匹配 |
| `service` | 合法服务名，如 `步行`、`校车接送` |
| `minutes` | 该服务沿此路线所需分钟数 |
| `status` | 可省略，默认 `已核定`；填 `已关闭` 即关闭该路线 |

同一起终点+服务存在多个版本时，取**最高版本的已核定路线**；同点移动视为 0 分钟、不需要路线。

## 2. 班级到课服务 `desktop_class_service`（命令 `service-import`）

接单时已经确定的班级到课服务合同，落库即冻结。重复导入必须逐字一致；
试图改服务（如把 `校车接送` 改成 `步行` 以放行）直接抛 `identity_conflict`，不能临时改服务放行。

| 字段 | 含义 |
| --- | --- |
| `class` | 班级标识，作为合同主键 |
| `service` | 约定服务名，班级行程链按此服务选路线 |

## 3. 课节快照 `desktop_schedule`（命令 `room-import`）新增字段

| 字段 | 含义 |
| --- | --- |
| `class` | 课节所属班级，班级行程链以此分组 |
| `location` | 教室地点（校区/点位），班级按此行走 |
| `remote` | 该课节是否远程；远程课班级仍按教室地点行走 |

## 4. 教师代课委托 `desktop_staff_roster`（命令 `substitute-assign`）新增字段

| 字段 | 含义 |
| --- | --- |
| `teaching_location` | 教师实际授课地点；远程授课时为教师所在点位（如居家点） |
| `remote` | 教师是否远程授课；教师链按 `teaching_location` 行走，不按教室地点 |
| `start` / `end` | 显式授课时段（缺省回读教室时段） |

## 5. 联合调课请求 `desktop_reschedule_request`（命令 `plan-reschedule`）

- `rooms[].location`：候选教室地点。
- `rows[].class`：受扰课所属班级。
- `rows[].teaching_location`：教师实际授课点（远程课填教师点位；缺省取教室地点）。
- `rows[].remote`：远程课标记。
- 生成的 `assignments[]` 回写 `klass`、`room_location`、`teaching_location`、`remote`，
  批准后原样进入 `room_slot` 与 `substitution`，因此通知阶段能继续按地点核链。

## 6. 行程判定与状态机

- 行程模式：库内存在任一已核定路线即启用；未导入任何路线时保留旧排课行为。
- 教师链：所有 `已委托` 代课 + 本计划分配，按教师分组、按开始时间排序，
  相邻课节用**步行**路线（教师实际授课点之间）核分钟数。
- 班级链：所有 `可用` 教室占用（带 `class`）+ 本计划分配，按班级分组、
  相邻课节用**接单冻结的服务**选路线（教室地点之间）。
- 判定结果（`itinerary_check` 与计划体 `itinerary`）：

| reason | 含义 |
| --- | --- |
| `itinerary_feasible` | 课间分钟数 ≥ 路线所需分钟数（恰好相等也算可行） |
| `cannot_reach_next_lesson` | 有路线但赶不上 |
| `route_missing` | 该起终点+服务没有已核定路线，保留待协调 |
| `service_not_frozen` | 班级没有接单服务合同 |
| `itinerary_location_missing` | 课节缺地点字段，无法核链 |

每条记录含 `chain`（teacher/class）、`subject`（教师或班级）、`from_lesson`/`to_lesson`、
`from_location`/`to_location`、`service`、`route_id`、`route_version`、
`gap_minutes`、`required_minutes`，来源可追溯。跨日相邻课节按完整 ISO 时间戳做差。

## 7. 计划与批准的因果链

1. `plan-reschedule` 用行程约束在回溯搜索中剪枝；赶不上的组合进入 `eliminations`（含原因与路线证据）。
2. 任一受扰课无可行组合 → 计划 `待协调`（`unassigned_lessons`）。
3. `approve-reschedule` 只接受 `可确认` 计划；待协调计划直接 `plan_incomplete` 拒绝，
   因此不会生成 `room_slot`/`substitution`。
4. 批准时按**最新已生效课节与路线版本**重算两条链：
   - `lesson_added_since_plan`：计划后增课，出现新的不可行相邻对；
   - `route_closed_since_plan`：所用路线被关闭或已无已核定路线；
   - `route_version_changed_since_plan`：同路线换版后分钟数变化导致赶不上；
   - `itinerary_infeasible_since_plan`：其他重算不通过。
5. 没有 `room_slot`（可用）与 `substitution`（已委托），`notice-evaluate` 判
   `schedule_unresolved`（拒绝生效），回执齐全也不能换班生效。

## 8. 样例运行顺序

```bash
python -m schedulehandoff route-import --db ledger.db examples/01-routes.json
python -m schedulehandoff service-import --db ledger.db examples/02-services.json
python -m schedulehandoff room-import --db ledger.db examples/03-room-orig.json
python -m schedulehandoff room-import --db ledger.db examples/04-room-west.json
python -m schedulehandoff substitute-assign --db ledger.db examples/05-staff-l0.json
python -m schedulehandoff substitute-assign --db ledger.db examples/06-staff-lw.json
python -m schedulehandoff plan-reschedule --db ledger.db examples/07-plan.json
python -m schedulehandoff approve-reschedule --db ledger.db examples/08-approval.json
python -m schedulehandoff notice-evaluate --db ledger.db examples/09-notice.json
python -m schedulehandoff report --db ledger.db --out report.json
```

把 `02-services.json` 的服务换成 `校车接送`（同一张课表、同样 10 分钟课间）即可观察
`待协调` 结论；校车合同需要 20 分钟，且不能通过改服务放行。
