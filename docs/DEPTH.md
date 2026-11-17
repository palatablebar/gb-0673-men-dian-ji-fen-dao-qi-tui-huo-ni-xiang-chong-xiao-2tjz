# 深度登记 门店积分到期与退货逆向冲销

规模：中型。使用者：连锁零售会员运营专员。核心规则：消费先用最早到期批次；退款按原消费分配逆向恢复，已过期部分不得复活；账户合并只叠加归属，不改写历史。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 事件采信 | 执行并复核事件采信 | `POST /api/intake` | `app/modules/intake/rules.py` | `tests/test_intake.py` |
| M2 权益规则 | 执行并复核权益规则 | `POST /api/policy` | `app/modules/policy/rules.py` | `tests/test_policy.py` |
| M3 积分归批 | 执行并复核积分归批 | `POST /api/lots` | `app/modules/lots/rules.py` | `tests/test_lots.py` |
| M4 消费到期 | 执行并复核消费到期 | `POST /api/consumption` | `app/modules/consumption/rules.py` | `tests/test_consumption.py` |
| M5 退货回拨 | 执行并复核退货回拨 | `POST /api/refund` | `app/modules/refund/rules.py` | `tests/test_refund.py` |
| M6 余额对账 | 执行并复核余额对账 | `POST /api/reconcile` | `app/modules/reconcile/rules.py` | `tests/test_reconcile.py` |
| M7 期末归档 | 执行并复核期末归档 | `POST /api/archive` | `app/modules/archive/rules.py` | `tests/test_archive.py` |
| M8 账户合并 | 裁决并确认合并 | `POST /api/merge`、`POST /api/merges/{id}/settle` | `app/modules/merge/rules.py` | `tests/test_merge.py` |

## 账户合并规则

主账户与待合并账户各自保留原始事件、批次、消费和退款；合并只生成“同一会员”的可用余额投影，不覆盖历史归属。合并前按当前余额、原账户依据和历史重放共同裁决：

- 同一批次/消费/退款编号只计一次；同编号不同原值判 `MERGE_ID_CONFLICT`，暂缓合并。
- 直接改归属会让隔离事件改判、已成功事件丢失，或让到期归属发生迁移（余额相同也算），判 `HISTORY_UNSTABLE`，暂缓合并。
- 账户无任何事件、批次或隔离依据判 `UNKNOWN_ACCOUNT`；账户已属于另一组待对账/已生效合并判 `MERGE_OVERLAP` / `MERGE_ALREADY_SETTLED`。
- 合并组积分不守恒、原始行数不守恒或退货回拨审计未过判 `RECONCILE_FAILED`。

裁决为 `MERGE` 时状态为 `待对账`：合并余额当天可作为 `usable_balance` 使用，但 `settled_balance` 为空。`settle` 前按当前批次重算并复核，通过后写 `已生效` 与 `正式余额`；重复确认返回原记录，不新增余额。`fail` 把状态改为 `切换失败` 并回落 `rollback_balance`（合并前主账户余额），此后不能直接确认；`retry` 可按最新依据重算后重新进入 `待对账`。同一 `run_id`、主账户、待合并账户与规则版本生成同一 `merge_id`，重复执行不新增合并记录。

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | GB18030权益中文 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_gb18030_real` |
| TRAP-02 | BOM表头 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_bom_real` |
| TRAP-08 | 会员前导零 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_member_leading_zero` |
| TRAP-04 | 重复表头 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_repeated_header` |
| TRAP-05 | 缺列记录 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_column_bad` |
| TRAP-14 | 编号冲突 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_duplicate_conflict` |
| TRAP-18 | 空态 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_empty_three` |
| TRAP-07 | 哨兵 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/policy/rules.py` | `tests/test_policy.py::test_sentinel_real` |
| TRAP-11 | UTC归一 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/policy/rules.py` | `tests/test_policy.py::test_utc_real` |
| TRAP-06 | 全角积分 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/policy/rules.py` | `tests/test_policy.py::test_fullwidth_points` |
| TRAP-23 | 权益半开窗 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/policy/rules.py` | `tests/test_policy.py::test_rule_end_exclusive` |
| TRAP-24 | 合并改归属导致历史翻案 | 直接合并会复活/抹掉已判定的消费退款，财务无法按原账户核对 | 合并前按合并归属重放，结论有变即暂缓 | `app/modules/merge/rules.py` | `tests/test_merge.py::test_history_unstable_defers_and_keeps_old_balance` |
| TRAP-25 | 同一会员重复计余额 | 主账户与待合并账户按行相加，同一批次被计两次 | 按编号去重，同编号不同原值拒绝合并 | `app/modules/merge/rules.py` | `tests/test_merge.py::test_duplicate_lot_counted_once` |
| TRAP-26 | 重复执行新增余额 | 每次提交都生成新合并并再次累加 | 合并编号按批次+账户+规则版本固定，重复提交返回原记录 | `app/store.py` | `tests/test_merge.py::test_repeat_create_does_not_add_balance` |
| TRAP-27 | 未对账即生效 | 合并余额立刻写入正式余额，切换失败无法回退 | 待对账当天可用、确认后才正式生效，失败回落旧版余额 | `app/modules/merge/rules.py` | `tests/test_merge.py::test_switch_failure_restores_old_balance` |

## 对账规则

原始逻辑输入=采纳行+隔离行+空行+重复表头+同值重复。业务计算按 DATA-DICTIONARY.md 分别核验，不能把上级与下级重复加总。所有隔离保留编号、原值、原始行号和稳定原因码。

## 已知不覆盖

数据来自仓内样例或用户粘贴文件，不连接外部业务生产系统。平台不执行实际资金划转或行政审批。
