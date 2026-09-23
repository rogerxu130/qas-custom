# Phase C 家庭 PAYG 后端核心实施计划

日期：2026-09-23。设计源为 `qas-parent-portal` 仓库 commit `943a72c` 的 `docs/superpowers/specs/2026-09-23-modular-classroom-and-payg-design.md`（当前共享工作树没有该文件，不使用断链相对链接）。本计划事实核对最初基于 `172afed`，经 `876b045` 测试修复后从 `876b045`/当时最新 HEAD 制订；实际实施仍须从最新 `origin/main` 与站点元数据复核。本计划只写实施顺序，不授权部署或历史数据转换。下述“全量剩余权益换新卡、源卡保留”的细化若与原设计的“换课程”简述有歧义，以本计划明确的数据保护规则为实现准则，并在实施前同步设计文档。

## 已核对的仓库事实和命名

基线为共享工作树 `172afed`：`session_resources.py` 已提供只读/可选 `FOR UPDATE` 的课次、名额、冲突查询；`direct_enrollment._context` 锁 Student→多个 Course Sessions→Weekly Timeslot→Term；`concentrated_makeup` 锁 Student→Course Sessions；`adhoc_booking` 仍是 60 天、100 条、试听价格/Customer 金额余额和自己的取消/锁定逻辑。`class_attendance.create_attendance_entry` 能保存 `source_doctype`/`source_document`，但现有重复检查并无数据库唯一约束，取消 Adhoc 还会删除点名行。新次卡不得调用这些 Adhoc 财务/删除路径。

`Course` 已有 `pay_as_you_go_fee`，它是旧 Adhoc 单次价格，不能改含义。新十次卡标准售价采用独立 `QAS PAYG Product`（Link Course、Currency 标准卡价、固定 `sessions_per_card=10`），避免把已有单次价格当卡价。新 DocTypes 明确命名 `QAS PAYG Product`、`QAS PAYG Card`、`QAS PAYG Entry`、`QAS PAYG Booking`、`QAS PAYG Operation`；模块目录的 Frappe 文件名依次是 `qas_payg_product`、`qas_payg_card`、`qas_payg_entry`、`qas_payg_booking`、`qas_payg_operation`，Python 控制器类依次为 `QASPAYGProduct` 等，实施时用 `frappe.model.naming.make_autoname` 或各 JSON 的独立 `autoname` 前缀。不要新增名为 `parent` 的字段到卡/操作：Frappe 子表使用 `parent`/`parenttype`/`parentfield`，本计划用 `family_parent`；`Sales Invoice` 现有 `parent` 自定义字段保持原义。新独立 DocType 不是 child table。

`Sales Invoice` 现有 `qas_invoice_type` 选项不含 PAYG；`Sales Invoice Item` 有 `qas_line_type`、`student`、`enrollment`、`course`、`term`、`course_session`，没有已核实的行级 `source_doctype/source_document`。新字段必须通过幂等 Custom Field patch 增加，如 `qas_source_doctype`（Link DocType）、`qas_source_document`（Data）、`qas_source_operation`（Link QAS PAYG Operation），并给 invoice type/line type 添加 `PAYG Card` 与 `PAYG Exchange` 选项；先在目标站点读 Custom Field/Property Setter，若已被其他发布占用则调整迁移，不直接覆盖。行字段不得误用 `enrollment` 或 `student`。`new_invoice_draft` 只初始化**未保存**的草稿，不设来源、项目、不提交事务；PAYG 业务命令负责通知 guard、金额、权限和持久化。

本计划的 Purchase Operation 是 UI/API 的购买串联对象，由管理员先创建/获取，再把其 ID 分别交给 Invoice Draft 和 Issue；两项动作各有独立 `request_key` 幂等键，不得各自创建另一 Purchase Operation。Exchange Operation 关联源卡与新目标卡；发票/付款事件不改权益。数据库唯一键和服务端幂等是必须同时存在的。旧 `Adhoc Booking`、既有发票和历史点名不迁移、不删改。新卡不创建长期 Enrollment、不发 Makeup Voucher/补偿卡、不自动发票/收款联动、不自动取消预约，也不因缺席自动退次。

## 全局事务锁序（所有任务遵守）

| 顺序 | 锁对象与规则 | 适用 |
| --- | --- | --- |
| 1 | 任何涉及 Student 的预约、取消、锁定、attendance 写先锁 `Student`；多个学生按 name 排序 | 预约、取消、定时锁定、换课/缩期需锁相关预约时 |
| 2 | Booking/Operation 已存在记录或唯一请求键占位；多个 Booking 按 name 排序，同类 Operation 按 name 排序 | 幂等、取消、锁定、换课、开票 |
| 3 | `QAS PAYG Card`，源卡/目标卡或多个候选卡按 name 排序 | 扣次、退次、转卡、续期 |
| 4 | `Course Sessions` 按 name 排序 | 名额、冲突与课堂参与 |
| 5–8 | `Weekly Timeslot`→`Term`→`Classroom`→active `Class Attendance Entry` | 课次校验和参与写入 |

取消/锁定可先**只读** Booking 找 student，再从 Student 开始按表加锁并重读 Booking；换课/缩期可先只读候选 Booking/Student，按排序锁 Student→Booking→Operation→Card，锁 Card 后再次查询预约防幻读。Issue/Invoice 无 Student 时从 Operation 开始。共享课堂入口接收“已锁 Student”的上下文或自行先锁 Student，绝不在持有 Session/Card/Booking 后反向锁 Student。所有写路径（含旧 Adhoc 的新接入边界）用同一顺序；真实 MariaDB 测试需检验死锁、最后名额和最后次数。

## 执行方式与测试命令

每个任务按“先写失败测试→只做最小实现→跑聚焦测试与必要旧用例→提交”执行；单任务提交只包含列出的文件。下文 `UT(MODULES)` 代表在 checkout 根目录运行：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" /Users/ranxu/Documents/Project/frappe-bench/env/bin/python -m unittest MODULES -v
```

这是仓库已有的无站点 mock 测试惯例。对真实索引、锁、事务和 DocType 需另在**明确指定的可丢弃测试站点**执行 `bench --site <test-site> migrate`、`bench --site <test-site> run-tests --app qas_custom --module qas_custom.tests.test_payg_site`；不得把 `qas-local.test` 文件存在当作可丢弃授权。站点测试 fixture 创建独立家长、两个学生、两张卡与排课，关闭真实通知/支付并回滚或清理。每次记录环境、通过/失败/未验证。下列代码为接近完整的断言骨架，实施时按 Frappe fixture 补齐。

## Task 1 — 普通课次写入边界与现有消费者

**文件**：改 `qas_custom/modules/course_schedule/session_resources.py`、`qas_custom/services/class_attendance.py`、`qas_custom/services/direct_enrollment.py`、`qas_custom/services/concentrated_makeup.py`、`qas_custom/services/adhoc_booking.py`；新增 `qas_custom/tests/test_course_session_booking_boundary.py`、`qas_custom/tests/test_adhoc_booking.py`；更新原 `test_course_session_resources.py`、`test_direct_enrollment.py`、`test_concentrated_makeup.py`。

**先失败测试**：给共享写入入口 `reserve_regular_place(student, session_id, source_doctype, source_document, enrollment_type, *, validate_business=None)` 测相同学生同课次、重叠课次、相邻不冲突、容量 0/满额、NDIS 上限、未开放 Term/Timeslot、取消/Leave 行不占位、同来源幂等；尤其测试**PAYG/新共享 reservation 调用**在同学生旧 Cancelled 参与行仍在时可重新约同课次，新行 `source_document` 指向新 Booking，旧行来源、点名及审计字段不变。另测显式 `reactivate_cancelled_duplicate=True` 的 Trial/转班调用仍复活原行、仍保持原 API 语义。模拟两个业务同时争最后一个位置，断言全局锁序。测试原 Enrollment/Makeup 的容量及错误结果没有改变：

```python
def test_shared_reservation_uses_one_lock_order_and_same_capacity(mock_db, fixture):
    reserve_regular_place("STU-1", "CS-1", "QAS PAYG Booking", "PB-1", "Pay-as-you-go")
    assert mock_db.locked == ["Student:STU-1", "Course Sessions:CS-1",
                              "Weekly Timeslot:WTS-1", "Term:TERM-1", "Classroom:ROOM-1"]
    assert fixture.active_students("CS-1") == {"STU-1"}
    with pytest.raises(PlaceUnavailable):
        reserve_regular_place("STU-2", "CS-1", "QAS PAYG Booking", "PB-2", "Pay-as-you-go")

def test_rebooking_after_cancel_keeps_old_attendance_source(site):
    old = site.cancelled_attendance(student="STU-1", session="CS-1", source="PB-OLD")
    new = reserve_regular_place("STU-1", "CS-1", "QAS PAYG Booking", "PB-NEW", "Pay-as-you-go")
    assert new.name != old.name
    assert site.attendance(old.name).source_document == "PB-OLD"
    assert site.attendance(new.name).source_document == "PB-NEW"
    site.assert_attendance_call("PB-NEW", prevent_student_duplicate=False)

def test_explicit_trial_reactivation_still_reuses_cancelled_row(site):
    old = site.cancelled_trial_attendance(student="STU-1", session="CS-2")
    row = create_attendance_entry(course_session="CS-2", student="STU-1",
                                  enrollment_type="Trial", source_doctype="Inquiry",
                                  source_document="INQ-2", prevent_student_duplicate=True,
                                  reactivate_cancelled_duplicate=True)
    assert row == old.name
```

**最小实现**：遵守上表 Student→Booking/Operation（若有）→Card（若有）→Course Sessions→Weekly Timeslot→Term→Classroom→active attendance 的顺序，不在锁课次后反向锁 Student/卡。共享函数锁后重查状态、教师安排（含 session `teacher_override`）、容量、`active_rows`、学生同时间冲突；保留 `concentrated_makeup_capacity` 专属判断与 Planned Enrollment 占位规则在对应调用方。**不要全局改变** `class_attendance.get_student_session_attendance_entry` 或 `create_attendance_entry` 的旧重复/复活语义。只在 PAYG/新共享 reservation 的去重查询过滤 active 行：active 阻止新写；Cancelled 历史行不占位。PAYG/shared reservation 必须已持 Student 和 Course Sessions 锁、完成 active-only 去重，随后调用 `create_attendance_entry(..., prevent_student_duplicate=False)` 新建行；安全性依赖这两个锁及同事务重检，禁止在未锁/未检时单独传 `False`。PAYG 重约不传 `reactivate_cancelled_duplicate=True`，不覆盖旧 source；同 source 的新预约幂等读取只返回 active 匹配行。`create_attendance_entry` 保留显式 `prevent_student_duplicate=True, reactivate_cancelled_duplicate=True` 分支，现有 Trial/转班继续复活原行。取消新卡预约只改行状态 `Cancelled`，不删除历史点名；已有 Adhoc 删除路径不改。Adhoc 的**确认写入**接入同一容量/冲突锁边界，但其试听价、Customer 余额、旧 DocType 和既有取消规则不动；这使旧入口不能绕过新卡名额。将选中的新/旧调用方转为共享边界时必须保持业务错误语义，任何尚未能统一的路径列在验收记录，不宣称全域并发保护。数据库唯一活动占位不能用 `student+session` 简单唯一索引，因为历史 Cancelled/Leave 行和重新预约；用 Student 行锁串行化所有采用新边界的入口，并在真实站点验证 PAYG Cancelled→重约、Trial/转班复活旧行及并发最后名额。

**运行**：`UT(qas_custom.tests.test_course_session_booking_boundary qas_custom.tests.test_course_session_resources qas_custom.tests.test_direct_enrollment qas_custom.tests.test_concentrated_makeup qas_custom.tests.test_adhoc_booking qas_custom.tests.test_cancelled_trial_attendance_reactivation qas_custom.tests.test_school_admin_enrollment_transfer)`；测试站点跑最后名额并发及两类 Cancelled 行写入。**提交**：`refactor: centralize regular session reservation boundary`。**验收**：新 PAYG 入口不能超卖/撞课/重复参与，Cancelled 后重约新建行；既有 Trial/转班显式复活原行，正式报名、补课及 Adhoc 收费/取消行为不变。

## Task 2 — 数据模型、索引和幂等键

**文件**：新增上述五个 `qas_custom/qas_custom/doctype/qas_payg_*/{__init__.py,*.json,*.py}`；新增 `qas_custom/patches/v2026_09_23_payg_indexes.py`；追加 `qas_custom/patches.txt`；新增 `qas_custom/tests/test_payg_schema.py` 和站点测试 `qas_custom/tests/test_payg_site.py` 的 schema 类。

**先失败测试**：元数据断言产品恰好 10 次、所有金额非负、卡联 family_parent/Customer/product/course、预约联**原卡**/学生/课次/参与行、流水和操作均有唯一操作键；卡状态含 `Transferred`，操作含 `source_card`/`target_card`，流水含 Transfer Out/In；测试 `frappe.get_meta`、数据库 `SHOW INDEX` 和重复 key 抛唯一错误。例：

```python
def test_payg_schema_indexes(self):
    for dt in ("QAS PAYG Product", "QAS PAYG Card", "QAS PAYG Entry",
               "QAS PAYG Booking", "QAS PAYG Operation"):
        self.assertTrue(frappe.db.table_exists(dt))
    self.assertUniqueIndex("QAS PAYG Operation", ("operation_type", "request_key"))
    self.assertUniqueIndex("QAS PAYG Entry", ("operation_key",))
    self.assertUniqueIndex("QAS PAYG Booking", ("request_key",))
    self.assertIn("Transferred", frappe.get_meta("QAS PAYG Card").get_field("status").options)
    self.assertTrue(frappe.get_meta("QAS PAYG Operation").has_field("source_card"))
    self.assertTrue(frappe.get_meta("QAS PAYG Operation").has_field("target_card"))
```

**最小实现**：产品 `course` 唯一、`standard_card_price` Currency、`sessions_per_card=10`（只读且服务端固定校验）、`invoice_item` Link Item、`enabled`；卡 `family_parent`,`customer`,`product`,`course`,`issued_on`,`expires_on`,`unit_price_snapshot`,`available_count`,`reserved_count`,`consumed_count`,`status`=Active/Paused/Transferred（已过期由日期判定，非清零）；Entry 只追加，`kind`=Issue/Reserve/Consume/Return/Transfer Out/Transfer In/Correction、`available_delta`,`reserved_delta`,`consumed_delta`,`card`,`booking`,`operation_key`,`actor`,`occurred_at`,`reason`；Booking `family_parent`,`student`,`card`（始终为预约原卡）、`course_session`,`attendance_entry`,`status`=Reserved/Locked/Cancelled/Completed、`cancellable_until`,`request_key`,`cancelled_at`,`cancel_reason`；Operation `operation_type`=Purchase/Exchange/ExpiryChange、`request_key`,`issue_request_key`,`invoice_request_key`,`source_card`,`target_card`,`family_parent`,`old/new course`,`old/new expiry`,`old/new price`,`quantity`,`price_delta`,`invoice`,`actor`,`reason`,`created_at`。Purchase Operation 是先创建的独立记录，两个动作接受其 ID；Exchange 保留源/目标卡和价格快照。所有 DocTypes 独立且后台只给 System Manager/School Admin 必要权限，家长经 API 读写。完整约束在控制器 `validate` 和业务命令，直接后台 save 也不得绕过负数/跨家庭/不平账检查。索引：Card `(family_parent,course,status,expires_on)`、Booking `(card,status,course_session)` 与 `(student,course_session,status)`、Entry `(card,creation)`、Operation 唯一 `(operation_type,request_key)`、Booking/Entry 单字段唯一，另对 Purchase 的 issue/invoice action key 建唯一约束（允许 NULL）；patch `frappe.db.add_index`/显式 SQL 检查先存在且在 MariaDB 测幂等。不要假设 Frappe `unique:1` 能建复合唯一，patch 要验证真实索引。

**运行**：`UT(qas_custom.tests.test_payg_schema)`；测试站点 migrate 两次、查索引。**提交**：`feat: add payg card ledger booking and operation schema`。**验收**：空库新增无历史重写，第二次 migrate 幂等，重复请求键数据库拒绝。

## Task 3 — 日期、选卡与账本守恒

**文件**：新增 `qas_custom/modules/payg/{__init__.py,rules.py,ledger.py}`、`qas_custom/tests/test_payg_rules.py`、`qas_custom/tests/test_payg_ledger.py`。

**先失败测试**：Australia/Brisbane 当地 Issue 日期，日历月加六（8 月 31→次年 2 月末、闰年、跨年），到期日当地 23:59:59 有效；Issue 当日有效，开课 72h 恰可约、取消严格大于 72h；卡发放/到期和上课日双有效；候选排序 `expires_on, issued_on, name`，跳过不能覆盖上课日的最早到期卡与 `Transferred` 源卡；预约预览选择的卡变化要报 refresh，不暗换。守恒测试：Issue 后 `(10,0,0)`；Reserve 后 `(9,1,0)`；Lock 后 `(9,0,1)`；取消两种状态都回 `(10,0,0)`；转出 7 次后源卡 `(0,0,3)`、目标卡 `(7,0,0)`；源卡旧预约随后 Return +1 仍不可预约；重复操作不变；过期仍保持数量，续期只变日期。

```python
def test_card_selection_skips_card_expiring_before_class():
    cards = [card("A", expiry="2026-10-01"), card("B", expiry="2026-12-01")]
    assert choose_card(cards, now=brisbane("2026-09-23 09:00"),
                       session_start=brisbane("2026-11-01 10:00")).name == "B"

def test_ledger_conservation():
    state = apply_entries([entry("Issue", +10, 0, 0), entry("Reserve", -1, +1, 0),
                           entry("Consume", 0, -1, +1), entry("Return", +1, 0, -1)])
    assert (state.available, state.reserved, state.consumed) == (10, 0, 0)

def test_transfer_preserves_both_card_ledgers():
    source = apply_entries([entry("Issue", +10, 0, 0), entry("Reserve", -3, +3, 0),
                            entry("Consume", 0, -3, +3),
                            entry("Transfer Out", -7, 0, 0)])
    target = apply_entries([entry("Transfer In", +7, 0, 0)])
    assert (source.available, source.consumed) == (0, 3)
    assert (target.available, target.consumed) == (7, 0)
```

**最小实现**：纯规则函数用 `zoneinfo.ZoneInfo("Australia/Brisbane")` 与 calendar month clamp；业务 DATETIME 存储与 Frappe 站点时区转换用单一适配，提交前记录真实站点 `time_zone` 为 Brisbane，禁止混用服务器 UTC naive 时间。账本只允许命令在锁卡后追加，并同事务更新卡汇总；每卡三个缓存分别等于该卡所有 Entry delta 之和、均非负。不能假定每张新目标卡总和恒为 10：源卡起始 Issue +10，转出减去 available；目标卡以 Transfer In 取得实际剩余数量。每个 Exchange Operation 的 Transfer Out 与 Transfer In 数量相等、同事务提交，跨卡总权益不凭空增加；Return 始终记预约原卡，即使源卡 `Transferred`，也不改其 expiry/status。Correction 仅管理员有理由且经单独命令；不得直接改缓存。**运行**：`UT(qas_custom.tests.test_payg_rules qas_custom.tests.test_payg_ledger)`。**提交**：`feat: define payg date selection and ledger invariants`。**验收**：边界确定、重复流水不能增加权益、过期/转出不抹除历史。

## Task 4 — Issue、预览、确认和家长取消

**文件**：新增 `qas_custom/modules/payg/{issue.py,booking.py}`、`qas_custom/tests/test_payg_issue.py`、`qas_custom/tests/test_payg_booking.py`；Task 1 的共享服务只作必要接口补充。

**先失败测试**：先建/获取 Purchase Operation，再 `issue_card(operation_id, issue_request_key)`；同操作/同 action key 重试返回原卡，不同 action key 也不得对同 Operation 再发卡，另发十次必须另建 Purchase Operation；Operation 已先开票时 Issue 关联同一记录。两孩子同家庭各预约扣两次，别家/无权限拒绝；`confirmed_rules` 未确认拒绝；预览分页不受旧 60 天/100 条限制、只列已开放 Term 的适龄/级别匹配课次；提交重新校验预览卡、上课时间、72h、Teacher、名额/冲突；中途名单插入失败全部回滚；同请求重复确认只返回原 Booking。关键骨架：

```python
def test_confirmation_rechecks_preview_card_and_rolls_back(site):
    preview = payg.preview_booking(parent="P", student="S", session="CS")
    site.reserve_last_unit(preview["card"])
    with self.assertRaises(PreviewStale):
        payg.confirm_booking(parent="P", student="S", session="CS",
                             preview_card=preview["card"], request_key="req-1")
    self.assertEqual(site.booking_count("req-1"), 0)
    self.assertEqual(site.attendance_count("req-1"), 0)

def test_issue_uses_existing_purchase_operation_once(site):
    op = site.create_purchase_operation(request_key="purchase-1")
    card = issue_card(op.name, issue_request_key="issue-1")
    assert issue_card(op.name, issue_request_key="issue-1").name == card.name
    assert site.card_count_for_operation(op.name) == 1

def test_confirmation_uses_locked_active_only_reservation(site):
    old = site.cancelled_attendance(student="S", session="CS", source="PB-OLD")
    payg.confirm_booking(parent="P", student="S", session="CS",
                         preview_card="CARD-1", request_key="req-new")
    site.assert_lock_order("Student:S", "Course Sessions:CS")
    site.assert_attendance_call("req-new", prevent_student_duplicate=False)
    assert site.attendance(old.name).source_document == "PB-OLD"
```

**最小实现**：提供 `create_or_get_purchase_operation(family_parent, product, purchase_request_key)`，管理员 UI 先取得 Operation ID；`issue_card(operation_id, issue_request_key)` 先校验 School Admin、Operation 的家庭/Customer/Product，锁 Operation 并记录本动作 key，已有卡则返回原卡（不同 key 必须有明确“already issued”结果），绝不新建第二 Purchase Operation；已有 Invoice 可为空或已存在。创建卡+Issue(+10) 与 operation 同事务，记录售价基准 `standard_card_price/10`。查询按 cursor `(session_date,name)` 分页，允许跨已开放学期全范围，返回 `has_more/next_cursor` 和每项可约原因，复用规则但不把预览当提交保证。确认、取消及所有 Student 写入严格按全局锁序：Student→Booking key→Card(s)→Course Sessions→Weekly Timeslot→Term→Classroom→active attendance；取消只读定位 Booking.student 后先锁 Student 再重读 Booking，不在持有 Card/Session 后反向锁 Student。提交时锁后重选卡并要求等于 `preview_card`，否则返回刷新；共享 reservation 在持 Student+Session 锁时完成 active-only 检查，然后调用 `create_attendance_entry(..., prevent_student_duplicate=False)`；不得让该 False 调用脱离锁和检查。Reserved 写 -1/+1 Entry、Booking、Class Attendance Entry(`enrollment_type="Pay-as-you-go"`, `source_doctype="QAS PAYG Booking"`) 同事务。家长取消按实时 `now < start-72h`，不依赖 Locked 状态；标 Cancelled、只追加一次 Return 到 Booking 原卡、Class Attendance Entry 改 Cancelled 保留历史点名和来源；前端通知用 after-commit，失败不重扣。异常只回滚当前 savepoint，不在共享服务里 `frappe.db.commit()`。

**运行**：`UT(qas_custom.tests.test_payg_issue qas_custom.tests.test_payg_booking qas_custom.tests.test_course_session_booking_boundary)`；站点做事务回滚/最后一次并发。**提交**：`feat: issue payg cards and reserve family sessions`。**验收**：卡与名单原子，家长超过取消界限无法自助退次，旧 Adhoc 未切换价格逻辑。

## Task 5 — 管理员取消与锁定任务

**文件**：新增 `qas_custom/modules/payg/cancellation.py`、`qas_custom/tasks/payg_booking_tasks.py`、`qas_custom/tests/test_payg_cancellation.py`；改 `qas_custom/hooks.py` 添加独立 scheduler 调用。

**先失败测试**：管理员可在 `<72h` 和已 Completed 后人工取消并退原卡一次；重复取消返回原结果；普通家长传 admin flag/Support View 写入被拒；原卡过期或已转出退次仍按原 expiry/status 不可约；锁定任务重跑无二次 Consume；取消与 lock 竞争账本仅一条 Return/Consume 合法序列，均先锁 Student；老师标 Absent 不返次；取消不生 Voucher/Store Credit/Invoice，不删除历史 `Class Attendance Entry`、marked_by/marked_at。

```python
def test_admin_cancel_after_lock_refunds_original_card_once(site):
    booking = site.locked_booking(card="CARD-1")
    first = cancel_by_admin(booking.name, reason="School closure", request_key="cancel-1")
    second = cancel_by_admin(booking.name, reason="School closure", request_key="cancel-1")
    self.assertEqual(first["card"], "CARD-1")
    self.assertEqual(first, second)
    self.assertEqual(site.entries(booking.name, kind="Return"), 1)
    self.assertTrue(site.attendance_exists(booking.attendance_entry))

def test_cancel_after_transfer_returns_to_original_disabled_card(site):
    source, target, old_booking = site.exchange_after_old_booking_completed()
    cancel_by_admin(old_booking.name, reason="Correction", request_key="return-old-1")
    self.assertEqual(site.card(source.name).available_count, 1)
    self.assertEqual(site.card(target.name).available_count, target.available_count)
    self.assertFalse(site.can_book_with(source.name))
```

**最小实现**：管理员后端角色校验和原因必填；取消与定时锁定都先只读定位 Booking.student，然后按全局顺序 Student→Booking→原 Card→Session→Timeslot→Term→Classroom→active attendance 加锁与复核；按 Booking 原卡返 Reserved 或 Consumed 桶，唯一键 `return:<booking>`，即使原卡已 Transferred 也只返该卡且不自动再转。把参加行设 Cancelled，但保留点名字段/历史，过去课次取消写清历史影响。定时任务批量游标取 due Reserved，每条独立事务/savepoint、锁后复核当前时间/状态，写 Consume `(0,-1,+1)` 与 Locked；任务失败可重试。实时取消门槛即使任务延迟仍生效。外部通知仅 after-commit。**运行**：`UT(qas_custom.tests.test_payg_cancellation)`；站点并发 Task 9。**提交**：`feat: audit payg cancellations and lock consumed sessions`。**验收**：所有取消只退原卡一次，无自动取消/补偿。

## Task 6 — 续期、缩期和整卡换课

**文件**：新增 `qas_custom/modules/payg/card_admin.py`、`qas_custom/tests/test_payg_card_admin.py`；正差价建票延至 Task 7，先返回待开票的 Exchange Operation；不开放外部换课 API。

**先失败测试**：过期卡人工续期只改 expiry 不改次数账/付款；缩短后任何有效预约上课日出界则阻止并列明；有有效未来预约禁止换课，取消后才可换。换课将**全部 source.available** 通过 Transfer Out/In 转入新的 target Card，源卡保留历史 consumed 与原 expiry，设 `Transferred` 不可预约；目标卡绑定新产品/课程，换课当天起六日历月、目标标准单次价快照。原标准单次价 snapshot 与目标当前标准卡价/10 差额乘转移次数，原 Invoice 折扣不参与。旧 source Booking 后续管理员取消仍 Return 到 source，source 不自动可约；管理员可对这笔新出现的 source.available 再做一笔**新的整笔转卡**。负差价只记录应退额、不自动 Store Credit；管理员权限与并发重查。

```python
def test_exchange_uses_saved_price_and_requires_no_active_future_booking(site):
    card = site.card(unit_price_snapshot="40", available=7, reserved=0, consumed=3)
    site.add_active_future_booking(card)
    with self.assertRaises(ActiveBooking): exchange_card(card.name, "NEW", "x-1")
    site.admin_cancel_active_booking(card)
    result = exchange_card(card.name, "NEW", "x-1", at="2026-09-23")
    self.assertEqual(result.price_delta, Decimal("7") * (Decimal("55") - Decimal("40")))
    self.assertEqual((result.source_card, result.target_card), (card.name, result.new_card.name))
    self.assertEqual((site.card(card.name).available_count, site.card(card.name).status), (0, "Transferred"))
    self.assertEqual((result.new_card.available_count, result.new_card.expires_on), (7, date(2027, 3, 23)))

def test_late_admin_return_stays_on_transferred_source(site):
    source, old_booking = site.exchanged_source_with_completed_booking()
    cancel_by_admin(old_booking.name, reason="Correction", request_key="late-return-1")
    self.assertEqual(site.card(source.name).available_count, 1)
    self.assertEqual(site.card(source.name).status, "Transferred")
    self.assertFalse(site.can_book_with(source.name))
```

**最小实现**：`change_expiry` 先只读相关 Booking/Student，按全局顺序锁 Student→Booking→Operation→Card 并重查，核对所有有效预约的上课日/时间；保留旧/新值和原因，绝不建票。`exchange_card` 同序重检无有效**未来**预约（整笔转出当下 source.available，不拆成可预约的残余份额）；旧已完成/锁定历史 Booking 不搬移、不删除，其 Return 仍指向 source。创建全新 target Card（同 family/Customer，新 product/course、换课日、六个月 expiry、新 unit_price_snapshot），source 写 Transfer Out `-quantity`，target 写 Transfer In `+quantity`，源卡标 Transferred；Operation 保存 `source_card`,`target_card`, quantity、旧/新价格与 delta。源卡的 expiry、已消耗流水、历史参与原样保留；Transferred 卡即使因以后 Return 有 available 也不可预约，但允许管理员针对其全部新 available 再建新的 Exchange Operation/target Card。正差价在 Task 7 完成前只允许内部测试，不开放 API，Task 7 的外层用例须让换课和 draft 同事务成功或一起回滚。Transfer Out/In 唯一键分别用同一 operation ID 加方向，保证重试不重复转；目标卡总数可以是 1–10，不强制 10。**运行**：`UT(qas_custom.tests.test_payg_card_admin qas_custom.tests.test_payg_rules qas_custom.tests.test_payg_ledger)`。**提交**：`feat: audit payg expiry and whole-card transfer`。**验收**：源卡/预约历史保持、目标卡只得实际剩余权益，无自动取消、退款或补卡。

## Task 7 — 行级发票来源与购买/换课草稿

**文件**：新增 `qas_custom/patches/v2026_09_23_payg_invoice_fields.py` 并追加 `patches.txt`；新增 `qas_custom/modules/billing/payg_drafts.py`、`qas_custom/tests/test_payg_invoice_drafts.py`；只在必要处改 `qas_custom/modules/billing/drafts.py`、`commands.py`、`qas_custom/services/school_admin.py`、`qas_custom/services/workshops.py` 以保护旧边界。

**先失败测试**：UI/API 先建一个 Purchase Operation，`create_payg_draft(operation_id, invoice_request_key)` 与 `issue_card(operation_id, issue_request_key)` 分别接受同一 ID；先票后卡、先卡后票均只关联该 Operation；两个动作各自重复只得原 Invoice/卡，不同 action key 也不能给同操作再建第二份。两个家庭不混草稿，同家庭其他 Course/Workshop 草稿不会被暗中追加；不同 PAYG 操作显式合并时各行独立来源；正差价草稿按 source→target 转移数量算出的 operation.price_delta，负差价无 draft；建票失败必须回滚 source Transfer Out、target Transfer In、target Card 和 Exchange Operation；提交/付款/取消 Invoice 不自动发卡、改卡或取消预约；沿用通用付款期限、通知 guard、付款 snapshot，管理员可调折扣后审核发送。

```python
def test_purchase_draft_has_one_line_source_and_no_entitlement_side_effect(site):
    op = site.create_purchase_operation(request_key="buy-1")
    invoice = create_payg_draft(op.name, invoice_request_key="invoice-1")
    line = invoice.items[0]
    self.assertEqual((line.qas_source_doctype, line.qas_source_document),
                     ("QAS PAYG Operation", op.name))
    self.assertFalse(site.card_for_operation(op.name))
    self.assertEqual(create_payg_draft(op.name, invoice_request_key="invoice-1").name, invoice.name)

def test_both_purchase_orders_share_one_operation(site):
    op = site.create_purchase_operation(request_key="buy-2")
    invoice = create_payg_draft(op.name, invoice_request_key="invoice-2")
    card = issue_card(op.name, issue_request_key="issue-2")
    self.assertEqual(site.operation(op.name).invoice, invoice.name)
    self.assertEqual(site.operation(op.name).target_card, card.name)
    op2 = site.create_purchase_operation(request_key="buy-3")
    card2 = issue_card(op2.name, issue_request_key="issue-3")
    invoice2 = create_payg_draft(op2.name, invoice_request_key="invoice-3")
    self.assertEqual(site.operation(op2.name).target_card, card2.name)
    self.assertEqual(site.operation(op2.name).invoice, invoice2.name)
```

**最小实现**：patch 幂等新增行级字段及上述 Select 选项，检查 Property Setter 对原选项的覆盖；不要修改现有 `qas_line_type` 默认。Purchase Operation 必须经 Task 4 `create_or_get_purchase_operation` 显式建立，`create_payg_draft(operation_id, invoice_request_key)` 只锁/读取它，**不能自行创建 Purchase Operation**；Issue 同理。建票动作 key 与 Issue 动作 key 独立，但 Operation ID 相同。新建**专属** `new_invoice_draft(customer,parent,invoice_type="PAYG Card")`，避免 `get_or_create_course_invoice` 的按家庭复用；填 ERPNext 合法 Item（优先产品专属 `invoice_item`，否则按当前 `get_invoice_item(course)` 验证）、`qty=1`,`rate=standard_card_price` 或正 delta、描述、course、行来源；调用已有通知 guard、payment snapshot 与 scoped Administrator persistence，不擅自 submit/send/commit。正 Exchange 的外层业务命令须包住**源卡 Transfer Out、目标卡 Transfer In、目标卡创建、Operation、draft**，任何建票失败一起回滚；负 delta 只留审计，不建票。Operation 上存 Invoice Link，读取时也检查行来源，阻断一对多伪关联；显式人工合并需延续行来源且检查折扣、税、Customer 兼容。若 Invoice 已被人工取消，不自动删除权益，只显示管理员处理状态；重建草稿必须新操作/明确更正链。**运行**：`UT(qas_custom.tests.test_payg_invoice_drafts qas_custom.tests.test_invoice_draft_factory qas_custom.tests.test_workshop_draft_boundary qas_custom.tests.test_course_invoice_due_dates qas_custom.tests.test_parent_invoice_portal_actions)`；站点真实草稿折扣/税/付款可见性测试。**提交**：`feat: link payg purchase and exchange drafts by invoice line`。**验收**：先后顺序均可、重复零新增、无付款权益联动、旧 Course/Workshop 草稿行为不变。

## Task 8 — 家长/管理员 API、老师名单与课堂内容授权

**文件**：新增 `qas_custom/api/payg_portal.py`、`qas_custom/api/school_admin_payg.py`、`qas_custom/tests/test_payg_api.py`、`qas_custom/tests/test_payg_content_access.py`；按需要改 `qas_custom/services/teacher_portal.py`、`qas_custom/services/parent_feed.py`、`qas_custom/api/parent_portal.py`、`qas_custom/services/parent_classroom_messages.py`（如实际文件名不同先核对）；保留原 API 签名。

**先失败测试**：家长仅见自身家庭卡/学生/预约，源卡 `Transferred` 不出现在可预约卡列表，但历史和迟来 Return 仍可见；管理员 API 返回 Purchase Operation ID 供 Issue/Invoice 共用，换课响应含 `source_card`,`target_card`,`transferred_quantity`,`price_delta`，后续源卡 Return 可发起另一整笔转卡。所有写入拒 Support View；管理员可 Issue、变更期限、换课、取消、建草稿，Campus Admin 不因前端按钮获得权限；老师当前名单显示新来源学生且点名不改权益；家长有**已参加**课次的照片、视频、作业、私密留言权限，卡过期不撤销已完成课堂历史访问，取消未来预约不获其后内容；媒体 URL 后端再次检查归属，换 `source_doctype` 不能越权。保留旧 `parent_portal_get_invoices`/Store API 权限与响应，新增 PAYG 导航组合只引用既有 API。

```python
def test_content_requires_participation_not_course_ownership(site):
    site.issue_card(parent="P", course="C")
    with self.assertRaises(frappe.PermissionError): site.photo_content("P", unbooked_session="CS-1")
    site.complete_booking(parent="P", session="CS-2", student="S")
    site.expire_card("P")
    self.assertTrue(site.photo_content("P", booked_session="CS-2"))
    site.cancel_future_booking(parent="P", session="CS-3")
    with self.assertRaises(frappe.PermissionError): site.photo_content("P", unbooked_session="CS-3")
```

**最小实现**：薄 whitelisted API 调用 PAYG 命令/查询，服务端解析 `frappe.session.user→Parent` 与 `Student.guardian`，不能信请求中的 family_parent；admin role 校验复用现有 School Admin helper，Support View 写拒绝。管理员端点先 `create_or_get_purchase_operation`，再以同一 Operation ID 分别执行 Issue 和 Invoice；换课响应展示新目标卡、源卡历史和可能的正差价草稿/负差价人工处理提示。家长查询展示卡的可预约/占用/已消耗、`Transferred` 源卡历史与迟来返次，但选卡函数永不选源卡。老师名单只调整来源映射与显示计数、使用 `QAS PAYG Booking` 区分 Adhoc，无新 Enrollment。`parent_feed` 的课次参与事实查询需包括新 attendance source，并在原媒体端点鉴权，私密留言按 student+family 授权。PAYG 发票视图复用现有提交后可见规则，Store 不扩大支付范围。**运行**：`UT(qas_custom.tests.test_payg_api qas_custom.tests.test_payg_content_access qas_custom.tests.test_teacher_portal_session_ordering qas_custom.tests.test_school_admin_session_roster qas_custom.tests.test_parent_classroom_messages)`；测试站点分别以 Parent/Teacher/School Admin/Support View 真身份调用。**提交**：`feat: expose payg family and admin APIs with classroom access`。**验收**：权限服务端执行，已参与历史可读，未参与和取消未来课不可读。

## Task 9 — 旧 Adhoc 与全链路回归、并发及放行

**文件**：扩充 `qas_custom/tests/test_payg_site.py`、`qas_custom/tests/test_adhoc_booking.py`；新增 `docs/verification/2026-09-23-payg-core-backend.md`；仅对发现的回归改最小受影响代码和现有测试 `test_direct_enrollment.py`、`test_workshops.py`、`test_school_admin_session_roster.py`、`test_invoice_draft_factory.py` 等。不得改旧 Adhoc 数据定义或猜测金额换次数。

**先失败测试**：旧 `adhoc_portal_*` 的 60 天/100 条、试听价格、Customer balance、`Adhoc Booking` 来源、旧取消仍按历史语义展示/执行；新 PAYG 端点只收新卡；旧发票/Workshop/Trial/Makeup/Store 核心流程保持。MariaDB 用两个**独立连接/事务**和 barrier 同时抢同一张卡最后一次、同课次最后席、同学生两个冲突课次、家长取消与锁定、Issue/开票重试、换课与旧预约迟来取消；每种只允许一次成功，断言每张 Card 缓存等于其各项 Entry delta 总和、每个 Transfer Out/In 成对且数量一致、Booking 与 active Attendance 一一对应。PAYG Cancelled 历史参与行仍在，重约同课次生成不同新行且不覆盖旧 source；旧 Trial/转班显式复活调用仍复活原行。错误注入在名单插入、Entry 插入、目标卡创建、Transfer In、Invoice 插入后分别验证整事务 rollback。不要用同连接 unittest mock 代替 DB 并发证据。

```python
def test_two_connections_cannot_spend_last_unit(self):
    results = run_in_separate_transactions(
        lambda conn, key: confirm_with_last_unit(conn, request_key=key), ("a", "b"))
    self.assertEqual(sum(result.ok for result in results), 1)
    self.assertEqual(card.available_count, 0)
    self.assertEqual(active_bookings_for_card(card.name), 1)
    self.assertEqual(active_attendance_for_card(card.name), 1)
```

**最小实现**：先在指定测试站点核对 `origin/main`、patch 顺序、索引、实际自定义字段与现有记录数量；跑 migration 两次，查看 MariaDB deadlock/lock wait 日志，统一全局锁序或限制重试到有幂等键的命令。只补经证实的兼容 bug。验证老师端消费者即使不在本前端仓库，也用 API 合约/测试账户跑混合名单、点名、内容/私密媒体。记录基线、测试证据、失败与未验证。回退演练：先关闭新 PAYG 写入口与任务，保留**源卡/目标卡、Exchange Operation、全部 Booking/Entry** 可查询，恢复代码兼容后再修复；绝不恢复旧数据库快照覆盖新增业务，也不得让旧 Adhoc 入口消费目标卡。部署窗口按实际请求另定，GitHub push、Frappe Cloud migrate 与前端部署分别验收。

**运行**：`UT(qas_custom.tests.test_payg_rules qas_custom.tests.test_payg_ledger qas_custom.tests.test_payg_issue qas_custom.tests.test_payg_booking qas_custom.tests.test_payg_cancellation qas_custom.tests.test_payg_card_admin qas_custom.tests.test_payg_invoice_drafts qas_custom.tests.test_payg_api qas_custom.tests.test_payg_content_access qas_custom.tests.test_adhoc_booking qas_custom.tests.test_direct_enrollment qas_custom.tests.test_workshops qas_custom.tests.test_invoice_draft_factory)`；站点 `bench --site <test-site> run-tests --app qas_custom --module qas_custom.tests.test_payg_site`；按设计 §13 的受影响权限、排课、请假补课、试听、老师/家长课堂、Workshop、Store、财务、通知、报告界面冒烟。**提交**：`test: verify payg transactions and legacy booking compatibility`。**验收**：单元、真实 MariaDB 并发/回滚、测试站点主流程、回退演练都有证据；缺任何一项写“未验证”，不能宣称可发布。

## 依赖与发布前自查

顺序是 Task 1 共享写入→Task 2 schema→Task 3 纯规则/账本→Task 4 Purchase Operation/Issue/家长预约→Task 5 取消/锁定→Task 6 续期与源卡到新卡整笔转移→Task 7 财务→Task 8 API/内容→Task 9 集成验证。Task 6 正差价只落待建票 Exchange Operation，Task 7 负责同事务建草稿或失败回滚源/目标转移；Task 7 完成前不开放换课 API。Task 4 完成前不开放任何新预约入口，Task 8 权限通过前不开放 Portal。每一步独立提交可回退，schema 保持前向兼容。

规则追踪：家庭共享/跨学期/适龄/容量/冲突/Cancelled 后重约新行→1、3、4；十次与最早到期/六日历月/72h→2、3、4；预览重检/事务/幂等/全局锁序→2、4、5、9；返原卡/锁定/无券→3、5、6；续期缩期/源卡转目标卡/差价→2、3、6、7；先发卡先开票/同一 Purchase Operation/付款分离/行来源→2、4、7；老师名单/课堂内容/权限与转卡响应→8；旧 Adhoc 与全回归/回退→1、9。已确认的非目标——长期 Enrollment、补偿卡、自动发票/付款联动、自动取消、删除历史点名——在所有任务里保持禁止。

**必须在实施初期实测的高风险点**：现有未迁移的预约写路径是否全部采用 Student 起始的全局锁序；生产站点 `Sales Invoice Item` Custom Field/Property Setter 与 MariaDB 唯一索引现状；Completed 后管理员取消、换课后历史预约迟来 Return 留在 Transferred 源卡的运营展示与二次转卡审计。测试站点证据不支持时先停止对应入口并更新设计/验收，不作未经确认的数据迁移或放宽权限。
