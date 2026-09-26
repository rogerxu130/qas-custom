# Marketing website trial notifications — local delivery

Date: 2026-09-26. Implemented from latest fetched main in isolated checkouts. Not pushed or deployed; no real notifications sent and no production settings changed.

## Use after deployment

Desktop: School Admin → Manage → Marketing notifications.
Mobile: School Admin → Settings → Marketing notifications.
Enter one agency email, check Enable website trial notifications, then save. Default is disabled and blank. The recipient does not need a QAS account. The bilingual panel includes an English email preview with an example name.

Only first-time website trial webhook applications are eligible, including saved Needs Review enquiries. The email subject is “New website trial enquiry”; its body is a fixed notice and the parent/contact name. No customer email, phone, student details, submission payload, attachment, internal identifier or admin link is sent. Visits, direct enrollment, manual creation, updates and duplicate submissions do not trigger it. Enabling never scans old applications.

## Backend deployment

Deploy qas_custom before the frontend, run the normal Frappe migration and restart workers/scheduler. Migration synchronizes two standard app DocTypes: Marketing Notification Settings and Marketing Trial Notification. No backfill/data patch is required. The scheduler hook recovers pending/failed queue handoffs every five minutes. Outbound-email and scheduler environment guards remain active.

Marketing Trial Notification is an internal read-only delivery ledger for School Admin/System Manager. Pending/Failed entries can recover automatically. Queued means handed to Frappe Email Queue, not confirmed delivery: follow its Email Queue link to inspect SMTP delivery and use the normal Email Queue retry for delivery errors. Do not delete successful delivery records to retry mail. Skipped means disabled, recipient changed, or outbound email blocked at handoff. Already queued emails may still arrive after disabling. The recipient and parent name are captured at ingestion; changing the recipient does not redirect old pending mail.

The worker uses current locking reads for the delivery and settings, and creates Email Queue plus its Queued marker in one transaction without SMTP. This avoids duplicate queue creation when workers overlap. A failure to record the delivery itself is logged separately without rolling back the application; inspect Error Log for “Marketing trial notification could not be recorded” if a new application has no delivery entry while enabled.

## Validation

- 106 backend unit/regression tests passed: 26 new marketing tests plus existing inquiry notifications/submission data, direct enrollment, school visit conversion and parent notifications.
- Standalone regression harness uses mocked frappe.db and an Australia/Brisbane system-settings fixture. No live database or SMTP service was used. Concurrent MariaDB/SMTP end-to-end validation remains a deployment-stage check.
- 37 existing frontend tests passed.
- Production build passed using the bundled Node runtime. Existing large-chunk warnings remain.
- Playwright on the actual local School Admin page at 1440 and 390 px, with all API calls mocked: default off, required/invalid email, CSRF save, sanitized static preview, failed save preserving edits, disable, English/Chinese, failed load/reload, read-only support view and no horizontal overflow passed.
- Python compilation and git diff whitespace checks passed.

Browser smoke script: /private/tmp/playwright-test-marketing.js. Test screenshots: /private/tmp/marketing-settings-1440.png and /private/tmp/marketing-settings-390.png.

## Publication boundaries

When publication is requested, fetch main again and integrate only these task commits. Push required backend/frontend commits to their origin/main branches, backend first, verify remote SHAs, and report Frappe Cloud migration and Netlify status separately. Original working directories contain unrelated uncommitted work and must be preserved.
