# 课表换班采录台

从本地排课桌面软件导出的课节快照核定教室占用；从教师名单与委托回执裁定代课资格；从班级通知回执判定换班是否生效。教室、教师、通知分别有独立状态，任何一方不满足都会改变生效结论。原始导出字节和 SHA-256 保留在 SQLite，JSON 报告包含来源与逐项业务决定。

入口：`room-import`、`substitute-assign`、`notice-evaluate`，均为 `--db ledger.db export.json`。`substitute-withdraw` 会撤回代课并重开已生效通知，`notice-resend` 只在当前版本和冻结名单内补回漏送班级。`room-correct` 以新版本更正课节时间，旧 `room_slot` 标为 `已更正` 并保留，不覆盖原始证据。正常与失败样例见 `tests/test_workflows.py`，涵盖跨日占用、容量、资质、并行教学、旧版通知与重试恢复。

`plan-reschedule` 对多门受扰课程联合匹配教室和教师：搜索时同时考虑当前房间占用、容量、教师资质与忙时，以及计划内互斥，优先最大化可安排课数，再降低偏好与空座成本。`approve-reschedule` 仅允许完整计划在当前版本经批准后生成新的房间与代课实体；批准时还会重新核对计划产生后新增的教室与教师占用，旧计划遇到资源变化会记录拒绝结论。这些实体随后可被通知系统使用。部分计划保留待协调状态，重放同一批准不会重复落地。

## 离线配电与切换核证

单次电流不超过铭牌值，并不说明故障时正在上课的教室能经联络开关安全转接到另一电源。配电入口采集馈线、分相归属、断路器、联络开关、房间负载与各测量来源，并据此重算一条**全楼一致**的开关操作序列：

```
room-import      rooms.json        # 现行课表（决定此刻哪些教室必须保持供电）
power-topology   floor.json        # 馈线/分相/断路器/联络开关/房间负载/原始测量
power-fault      fault.json        # 故障段与已跳闸设备
power-service    services.json     # 故障前冻结的服务约定（普通授课/在线考试）
power-plan       plan.json         # 按当前 room_slot + 服务约定重算候选序列
power-execution  execution.json    # 设备合闸回执与实际电流读数
power-correct    correction.json   # 迟到电流更正（可推翻已有证明）
power-proof      proof.json        # 只凭执行证据出具/拒绝“已完成切换”证明
```

每一步都检查：电源不得反送故障段、联络开关先断后合、分相与公共中性线电流、设备启动浪涌。终态电流可承受不代表途中合闸时也可承受。负载分基本照明、可延迟投影机与不可中断考试设备；某类负载能否暂卸由**故障前**的教室服务约定决定，方案不会事后改称可卸载。若若干教室共用备用馈线，搜索在整栋楼同一状态下评估，避免“每室各自可行、拼起来同相过载”。

计划动作与真正执行分开：`power-proof` 只认设备回执、单调时间戳与实测电流，断电时长按执行时间戳计算；缺失回执、跳过步骤、实测超限或迟到更正都会拒绝证明。`room_slot` 一旦新增或更正，已有计划与证明即失效，需重算。电路证据从不回写课表、教师或通知回执。详见 `tests/test_distribution.py` 的合成楼层验收。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m schedulehandoff demo`，`python -m schedulehandoff power-demo`，`python -m schedulehandoff --help`，`python -m pytest -q`。所有演示和测试使用虚构本地文件，不访问网络。
