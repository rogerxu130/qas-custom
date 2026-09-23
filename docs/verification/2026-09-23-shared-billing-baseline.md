# Shared billing refactor: pre-change baseline (2026-09-23)

This is the baseline for Invoice Plan Task 1. Inspection and tests used the clean `codex/qas-shared-foundation` worktree at `/private/tmp/qas-shared-foundation`, HEAD `a4e322addcd52e635cea0c9fc5b6ddafcbc4fa91`. The absolute checkout and interpreter paths below are machine-specific. The branch already contains the completed shared classroom foundation; this task records existing billing behavior only.

## Runtime and financial test result

Python 3.11.15 ran from the real Frappe bench virtual environment at `/Users/ranxu/Documents/Project/frappe-bench/env/bin/python` (machine-specific), with this checkout on `PYTHONPATH`. The command did not initialize or connect to a Frappe site; these are unit tests with mocked database boundaries except for the failing unmocked call described below.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" /Users/ranxu/Documents/Project/frappe-bench/env/bin/python -m unittest qas_custom.tests.test_invoice_account_mutation qas_custom.tests.test_invoice_amount_consistency qas_custom.tests.test_workshops qas_custom.tests.test_trial_invoice_dates qas_custom.tests.test_course_invoice_due_dates qas_custom.tests.test_school_admin_draft_invoice_adjustments -v
```

Result: **57 tests run, 56 passed, 1 errored** (`FAILED (errors=1)`), in 0.541s. The error is `test_parent_context_exposes_adjustment_as_independent_line` in `test_school_admin_draft_invoice_adjustments.py`. Its positive payable amount makes `build_parent_invoice_context` call `payment_url`, whose `settings()` calls `frappe.db.exists("DocType", "QAS Stripe Settings")`. With no site-bound `frappe.db`, Werkzeug raises `RuntimeError: object is not bound`. A focused rerun of that exact test produced the same error (`Ran 1 test`, `FAILED (errors=1)`). This is an existing test setup gap at the recorded HEAD; it does not establish an invoice, Stripe, or database behavior failure. No test or production code was changed to address it in this task.

### Test-isolation repair (2026-09-23)

A later test-only change patches `qas_custom.services.stripe_trial_payments.payment_url` in `test_parent_context_exposes_adjustment_as_independent_line`, the helper imported by `build_parent_invoice_context` when payable is positive. It returns a deterministic harmless URL so the line and amount assertions still exercise the presentation code without requiring site-bound Stripe settings. The isolated `test_school_admin_draft_invoice_adjustments` module passed **10/10 tests**. Rerunning the exact six-module command above passed **57/57 tests** (`Ran 57 tests`, `OK`). This repairs test isolation only; it changes no product behavior.

## Current caller and behavior inventory

| Path | Existing behavior at the recorded HEAD |
| --- | --- |
| `qas_custom.modules.billing.commands.get_or_create_course_invoice` | Queries `Sales Invoice` by customer and `docstatus=0`; adds parent, `qas_invoice_type="Course"`, and `status != "Cancelled"` filters only when those fields exist (and parent was supplied). Requests `fields=["name"]`, `order_by="modified desc"`, `limit=1`, then loads that draft. If none is found, calls `disable_sales_invoice_auto_notifications()` **only on the new-document path**, creates a Sales Invoice, sets customer, default dates, optional parent and type, and payment snapshot. It does not insert or commit. |
| `qas_custom.modules.billing.commands.create_prorata_invoice` | The direct enrollment and trial conversion callers reach the Course helper here. After adding the Course item and summary, it normalizes Course dates and reapplies the payment snapshot immediately before insert or save. The insert/save runs through `run_invoice_mutation_as_administrator` with `ignore_permissions=True`; this function does not explicitly commit. |
| `qas_custom.services.workshops.create_school_admin_workshop_invoice_data` | Requires a School Admin or System Manager role, active Workshop Enrollment, and no existing linked/source invoice with `docstatus < 2`. It calls the notification guard **before** draft lookup, so the guard also runs when reusing a draft. On a new invoice it sets customer, default dates, parent, Workshop type and source metadata. It appends a Workshop item unless already present, then applies the payment snapshot immediately before protected insert/save. It updates the enrollment invoice fields and explicitly calls `frappe.db.commit()` on both the normal and already-present-item return paths. |
| `qas_custom.services.workshops._find_draft_workshop_invoice` | Returns `None` if parent/customer is missing or the Sales Invoice type field is absent. Otherwise filters by customer, `docstatus=0`, `qas_invoice_type="Workshop"`; conditionally adds parent and `status != "Cancelled"`. Requests invoice names ordered by **`creation asc`**, `limit=1`, and returns the first name. This differs from the Course helper's `modified desc` choice. |
| `qas_custom.services.school_admin._create_term_enrollment_invoice` | Separate School Admin Course path: calls the guard before `_find_draft_family_invoice`, applies default dates only for a new draft, applies Course dates and payment snapshot after appending an enrollment item, then uses its own `_run_school_admin_invoice_mutation` wrapper for insert/save. Its draft lookup is term-aware and distinct from `get_or_create_course_invoice`. |
| `qas_custom.services.school_admin._create_school_admin_manual_invoice_doc` | Separate manual invoice path: calls the guard before `frappe.new_doc`, applies default dates, optional explicit due date and payment payload, then fills any missing payment snapshot fields before item/adjustment setup and protected insert. The function itself does not commit. |

`apply_default_invoice_dates` fills a missing posting date with today and a missing due date from invoice settings (`payment_due_days`, default 7), and fills missing payment-schedule row due dates with the invoice due date. Existing values remain unless `force=True`. `apply_invoice_payment_snapshot` reads invoice settings and fills only missing supported Sales Invoice snapshot fields (invoice message, accepted payment methods, bank account name/BSB/number/reference note), unless forced. Thus a newly created Course draft gets an early snapshot, while Course item creation and Workshop item creation also refresh still-empty fields immediately before persistence; existing nonempty values are preserved.

`run_invoice_mutation_as_administrator` saves the original session user, switches to `Administrator` for the callback, and restores the original user in `finally`, including after exceptions. The Course and Workshop insert/save calls still pass `ignore_permissions=True`. Workshop first checks `_require_school_admin`; the privilege switch is scoped to persistence, not an authorization grant to the caller. The School Admin service has a separate wrapper with the same switch-and-restore pattern.

The guard is more than a per-invoice flag: if the `Notification` DocType exists, `disable_sales_invoice_auto_notifications` clears the Sales Invoice notification cache, scans all Notification rows, and disables any Sales Invoice notification or one matching legacy invoice subject/message markers through `frappe.db.set_value(..., update_modified=False)`; it clears the cache again. When called as a Sales Invoice document hook with flags, it also resets `doc.flags.notifications` and `notifications_executed` before and after the scan. If the `Notification` DocType does not exist, the function returns before those flag/cache actions. `hooks.py` invokes it on Sales Invoice `before_insert` and `before_submit`; Sales Invoice `on_update`/`on_change` purge matching legacy Email Queue entries with statuses `Not Sent`, `Partially Sent`, and `Error`, including their recipient rows. Email Queue `before_insert` suppresses matching legacy invoice messages by marking the queue message and every recipient `Sent` to prevent delivery. These are global notification/email side effects, so a later refactor must preserve call timing and scope deliberately.

## Read-only environment inspection and limits

The bench has `qas-local.test` and `qas-restore.test` site configuration files (machine-specific). Both show `allow_tests=true`; `qas-local.test` shows `developer_mode=1`. There is no `currentsite.txt`. File presence and those flags do not designate either site, account records, or payment credentials as a disposable test setup. No site was connected to or mutated.

| Surface | Status | Limit |
| --- | --- | --- |
| Mocked financial unit tests with real Frappe imports | Verified after test isolation repair | Historical baseline: 56/57 passed; clean rerun: 57/57 passed. |
| Browser/portal invoice flows | Unverified | No browser session or designated disposable site/account. |
| Real invoice creation, reuse, submission, or database transaction | Unverified | No site connection or test records. |
| Real payment, Stripe, charge, or refund | Unverified | No payment action or disposable payment setup. |
| Real notification/email queue delivery or suppression | Unverified | Hook behavior inspected in code only; no email action. |
| Production behavior and deployment | Unverified | No production access or deployment check. |

The baseline documentation and later test-isolation repair made no schema, data, API, or billing behavior change. Neither pushed, deployed, connected to a site, sent email, or charged a payment method.
