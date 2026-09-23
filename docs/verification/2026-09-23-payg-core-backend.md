# PAYG backend verification — 2026-09-24

This is **site-free compatibility evidence**, not PAYG release approval. The commands below ran from `/private/tmp/qas-shared-foundation` with `/Users/ranxu/Documents/Project/frappe-bench/env/bin/python`. No migration, database write, push, or deployment was performed for this verification.

## Verified locally

The Task 9 PAYG/legacy command suite passed **193 tests**:

```sh
/Users/ranxu/Documents/Project/frappe-bench/env/bin/python -m unittest \
  qas_custom.tests.test_payg_rules qas_custom.tests.test_payg_ledger \
  qas_custom.tests.test_payg_issue qas_custom.tests.test_payg_booking \
  qas_custom.tests.test_payg_cancellation qas_custom.tests.test_payg_card_admin \
  qas_custom.tests.test_payg_invoice_drafts qas_custom.tests.test_payg_api \
  qas_custom.tests.test_payg_content_access qas_custom.tests.test_payg_read_models \
  qas_custom.tests.test_adhoc_booking qas_custom.tests.test_direct_enrollment \
  qas_custom.tests.test_workshops qas_custom.tests.test_invoice_draft_factory \
  qas_custom.tests.test_parent_password_reset_context \
  qas_custom.tests.test_campus_admin_accounts
```

The affected attendance, Trial, Makeup, parent/teacher classroom, Workshop, Store, invoice, and notification suite passed **208 tests**:

```sh
/Users/ranxu/Documents/Project/frappe-bench/env/bin/python -m unittest \
  qas_custom.tests.test_attendance_lock_order \
  qas_custom.tests.test_cancelled_trial_attendance_reactivation \
  qas_custom.tests.test_parent_classroom_messages \
  qas_custom.tests.test_parent_invoice_portal_actions \
  qas_custom.tests.test_parent_leave_makeup_choice \
  qas_custom.tests.test_parent_makeup_session_roster \
  qas_custom.tests.test_school_admin_draft_invoice_adjustments \
  qas_custom.tests.test_school_admin_session_content \
  qas_custom.tests.test_school_admin_session_roster \
  qas_custom.tests.test_school_admin_makeup_cancellation \
  qas_custom.tests.test_school_admin_trial_payment_status \
  qas_custom.tests.test_teacher_portal_session_ordering \
  qas_custom.tests.test_teacher_session_completion_reminders \
  qas_custom.tests.test_trial_invoice_automation \
  qas_custom.tests.test_trial_invoice_dates \
  qas_custom.tests.test_trial_parent_notifications \
  qas_custom.tests.test_workshop_draft_boundary \
  qas_custom.tests.test_store_order_pickup \
  qas_custom.tests.test_store_support_view \
  qas_custom.tests.test_invoice_amount_consistency \
  qas_custom.tests.test_invoice_payment_plans \
  qas_custom.tests.test_makeup_parent_notifications
```

These tests verify the pure rules and mocked command contracts: ten-unit card accounting and operation keys; reservation source `QAS PAYG Booking` without `Adhoc Booking` or `Enrollment`; old Adhoc discovery's 60-day/100-row window, Trial Class Fee, Customer balance, original attendance source and cancellation; PAYG source-card Returns and exchange/invoice linkage; parent/admin API role checks; and parent media participation gates. The new PSU Go reset-link context retains Parent identity checks and rejects Teacher/Campus Admin portal values. Mocked savepoint, lock-call, and rollback assertions describe intended control flow, **not actual MariaDB transaction behavior**.

The first affected suite run had one test-harness error: invoice PDF formatting queried uninitialized Frappe local date settings. The test now fixes the date formatter locally; the full **208-test** rerun passed. No production invoice formatting code changed.

`python -m unittest qas_custom.tests.test_payg_site` reported **2 skipped tests** because `QAS_PAYG_TEST_SITE=1` was not set. A read-only `qas-local.test` query returned `[[0]]` for the count of `tabQAS PAYG Booking` in `information_schema.tables`; this local site has not received the PAYG schema. The checked-out cached `origin/main` ref was `d3eec8ea4b2cc01ce6d2f0eb4691c47e3e4d3454`; it was not fetched, so it is not evidence of the latest remote state. `patches.txt` lists `v2026_09_23_payg_indexes` followed by `v2026_09_23_payg_invoice_fields`; actual execution and resulting indexes/custom fields have not been checked on a migrated site.

## Still unverified before release

- Migration and repeat migration on a dedicated PAYG test site, including actual MariaDB unique indexes, custom fields, current data baseline, and Brisbane DATETIME round trips.
- Independent-connection concurrency for last card unit, last seat, student time conflict, parent cancel versus scheduled lock, Issue versus invoice retry, and exchange versus late Return. No same-connection mocks are treated as concurrency proof.
- Real transaction rollback after injected attendance, Entry, target Card, Transfer In, and Invoice failures; ledger/cache conservation and booking-to-attendance invariants after those failures.
- Parent, Teacher, School Admin, Campus Admin, and Support View API calls under real site identities; mixed teacher roster/attendance and private media behavior in the actual teacher consumer.
- Full UI smoke tests, Frappe Cloud migration, frontend deployment, and rollback drill. The drill must retain every PAYG Card, Booking, Entry, and Operation and must not restore an old database snapshot over new records.

PAYG writes and publication remain gated on those site checks. GitHub delivery and Frappe Cloud/Netlify deployment are separate steps and are not claimed here.
