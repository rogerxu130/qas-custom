# Trial invoice due dates

The approved fix extends class-relative deadlines to trial invoices and repairs existing unpaid trial invoices.

- Reuse `course_invoice_due_date` without the full-term mid-term flag: when the lesson is at least seven calendar days after posting, due date is lesson date minus seven days. Otherwise allow three calendar days from the original posting date.
- Use the booked Course Sessions date for automatic trial invoices and replacement drafts.
- When the existing trial job runs after rescheduling, update an eligible linked invoice using the same rule.
- The migration repairs trial invoices sourced from Inquiry, including drafts and submitted outstanding invoices. Skip cancelled invoices/inquiries, returns, paid invoices, missing bookings, QAS payment plans and multiple-installment payment schedules.
- Update the invoice, its single payment schedule, and submitted invoice GL/Payment Ledger due dates together. Recalculate submitted status and add an audit comment. Do not change amounts or queue reminder emails. Repeated execution is idempotent.

## Deployment and verification

This is a backend-only change. It has not been deployed or applied to production data.

Before migration, preview affected records with `bench --site SITE execute qas_custom.modules.billing.trial_invoice_dates.repair_trial_invoice_dates`. The default is read-only. The registered September 17 patch applies the correction during `bench --site SITE migrate`.

After migration, inspect a future unpaid trial invoice, its payment schedule, portal status and overdue-reminder eligibility. A trial on October 12 posted September 1 should be due October 5, with overdue eligibility beginning October 6 if still unpaid. Confirm paid and payment-plan invoices are unchanged.

Validation: 43 unit tests covering trial dates, trial automation, existing course dates and overdue reminders pass. The standalone runner mocks Frappe's translation lookup because it has no initialized site/cache. Database migration and UI verification still require a deployed Frappe site.
