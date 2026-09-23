# PAYG Task 3 rule and ledger verification (2026-09-24)

This change adds pure date, card-selection, and Entry-delta functions. It does not connect to a Frappe site, migrate schema, create bookings, or write ledger rows.

The rule adapter currently interprets Frappe naive DATETIME values as Australia/Brisbane local wall time; aware datetimes are converted to Australia/Brisbane. No site was connected during this task, so the target site's actual `time_zone=Australia/Brisbane` setting and DATETIME storage/read behavior remain **unverified**. Checking that setting and the round trip of a stored DATETIME on the target site is a gate before enabling PAYG writes. If the site uses another convention, the adapter must be corrected before use.

The pure tests cover six-month calendar clamping, issue and expiry-day boundaries, exact 72-hour booking and strict parent cancellation, active-card selection and preview consistency, entry shapes and nonnegative balances, duplicate operation keys, source/target transfer conservation, late Return to the source, and the fact that expiry or a date-only renewal does not alter Entry rows or folded balances.

`choose_card` is for preview selection only. Task 4 confirmation must call `confirm_preview_card` with the nonempty card name shown to the parent; that helper re-selects and raises `PreviewStale` if eligibility or ordering has changed. Passing no preview card cannot confirm a booking.

At the initial Task 3 commit, the focused command below passed **62/62 tests**. After the additional expiry and transfer-card assertions, the same command passed **64/64 tests**. With the explicit confirmation contract, it passed **65/65 tests**:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" /Users/ranxu/Documents/Project/frappe-bench/env/bin/python -m unittest qas_custom.tests.test_payg_rules qas_custom.tests.test_payg_ledger qas_custom.tests.test_payg_schema qas_custom.tests.test_course_session_booking_boundary qas_custom.tests.test_course_session_resources -q
```

These are site-free tests. They do not establish MariaDB concurrency, Frappe transaction rollback, migration idempotence, or live-site permissions.
