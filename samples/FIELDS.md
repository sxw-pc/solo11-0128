# 离线扫码 JSON 字段说明

所有导入文件均为本地扫码枪导出的 JSON，顶层必须有非空字符串 `version`；`rows` 为记录数组。
原始字节按 SHA-256 去重，重复导入同一文件不会产生重复记录或重复赔偿。时间统一为
ISO-8601（如 `2026-01-01T09:30:00`），各设备时钟偏差由借出记录的 `clock_skew_seconds` 声明。

## 1. 借出 `equipment-checkout`（样例 `checkout.sample.json`）

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `kits[].id` / `kits[].items` | 否 | 套装配置：套装号与其标准序列号清单，供整套移交展开与配置核对 |
| `rows[].id` | 是 | 借用单标识，后续交接、归还均引用它 |
| `rows[].borrower` | 是 | 领用人；缺失则记录为 `待核/borrower_or_serial_missing` |
| `rows[].serials` | 是 | 实际借出的每件物品序列号；同单内重复或为空即待核 |
| `rows[].agreement` | 是 | `personal_custody`（个人保管）或 `team_shared`（团队共用），**借出时固定，缺件后不得更改** |
| `rows[].team` | 团队协议必填 | 共同承担的同课组教师列表；个人协议可省略，缺省为领用人 |
| `rows[].station` / `rows[].time` | 否 | 借出站点与时间，构成保管链的根事件 |
| `rows[].clock_skew_seconds` | 否 | 允许的设备时钟偏差（默认 300 秒）；偏差窗口内两事件顺序可互换 |

## 2. 交接 `equipment-handoff`（样例 `handoff.sample.json`）

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 交接记录标识 |
| `loan` | 是 | 关联借用单 |
| `giver` / `receiver` | 是 | 移交人与接收人；缺失即待核 |
| `scope` | 是 | `item`（单件）或 `kit`（整套）。整套时 `serials` 可留空，导入时按借用单配置展开为全部序列号 |
| `serials` | 单件必填 | 本次移交的序列号，必须属于该借用单 |
| `signed` | 是 | 是否已有接收人签名。`false` 的交接**不转移保管责任**，仅作为待证证据保留 |
| `signature_id` | 否 | 随交接一起提交的签名标识 |
| `station` / `time` | 否 | 交接站点与设备时间戳 |

已存在交接的当事人、序列号、站点、时间、范围属于不可变内容，重复导入若改动这些字段会报
`identity_conflict`；把 `signed` 由 `false` 变为 `true`（或通过签收接口补签）是受支持的迟到签名路径。

## 3. 迟到签收 `equipment-sign`（样例 `signature.sample.json`）

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `signature_id` | 是 | 签收单标识 |
| `handoff` | 是 | 被补签的交接记录 |
| `signed_by` | 否 | 签名教师，缺省取交接的 `receiver` |
| `station` / `time` | 否 | 签名发生的站点与时间，可晚于交接时间 |

补签会让对应交接进入保管链并重算责任；若改变结论，旧赔偿通知保持原样并由一张带版本的
**调整单（adjustment_order）** 冲销，案件号追加新的 `decision_seq`。

## 4. 归还 `equipment-return`（样例 `return.sample.json`）

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 归还记录标识 |
| `loan` | 是 | 归还所针对的借用单 |
| `serials` | 是 | 本次扫码的序列号数组。同站点同时间的重复序列号只计一次；同型号但序列号不同的物品是替代件，不能冒充原序列号 |
| `station` / `time` | 否 | 归还站点与时间 |
| `returner` | 否 | 具名归还人。留空表示无人值守站点扫码，只证明物品在该站点出现，不指认持有人 |

跨套混还时，序列号被显式链接到 `loan` 但不属于该套，会标记为 `foreign_serial_mixed_kit`，
不会结清它原本所属的借用单；同一序列号的一致性扫码只能结清一份借用，冲突时记
`double_settlement_blocked` 并要求站点签收区分。

## 5. 责任结论状态

- `已归还`：该序列号有相容的归还站点扫码，独立结清。
- `责任确定`：所有相容链末端落在同一责任人（个人协议），或落在团队成员集合内（团队协议共同承担）。
- `责任待核`：存在把缺件放在不同持有人手中的多条相容链；返回 `minimum_evidence`，列出最少还需哪张签收或哪个站点扫描才能区分，绝不把最后一条扫码的人记为赔偿人。
- `证据冲突`：已签交接或归还扫码在任何相容链中都无法满足（如移交人从未持有该物），返回 `conflict_core` 最小冲突证据。

## 6. 持久化表（SQLite）

`equipment_kit`（套装）、`equipment_loan`（借用）、`equipment_handoff`（交接）、
`equipment_signature`（签名）、`equipment_scan`（借出/归还站点扫码）、`serial_chain`
（每件序列号的全部相容链、相容持有人、排除证据与最少补证）、`custody_contract`
（借出时固定的协议）、`liability_case`（带版本的责任案件）、`liability_notice`
（赔偿通知，旧通知只冲销不改写）、`adjustment_order`（调整单）。
`report` 命令回读上述全部内容并给出每件序列号的中文裁决说明。
