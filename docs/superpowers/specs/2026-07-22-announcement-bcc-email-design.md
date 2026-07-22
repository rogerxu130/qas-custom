# Announcement BCC Email Delivery

**Date:** 2026-07-22
**Scope:** School Announcement email delivery for all audience types

## Goal

Replace the existing one-email-per-family Announcement delivery loop with simple BCC delivery while retaining Parent Portal visibility and family-level recipient resolution.

## Confirmed Delivery Model

Publishing continues to resolve and deduplicate recipients by Parent/Family. Each matched family keeps one `School Announcement Recipient` record because those records control Parent Portal visibility.

Email delivery changes as follows:

1. collect Recipient rows whose `email_status` is `Queued`;
2. remove empty addresses and deduplicate by normalized email address;
3. split the unique addresses into batches of at most 50;
4. create one Frappe Email Queue message per batch;
5. place the batch addresses in `BCC` only, with no public `To` or `CC` recipients.

For example, 230 unique family email addresses produce five queued emails: four BCC messages with 50 addresses and one with 30.

## Existing Behavior Preserved

- Announcement audience resolution remains unchanged for All Parents, Term, Term + Course, Course Session, and Single Student.
- Families are deduplicated by the existing Parent-based rules.
- Every matched family can see the published Announcement in Parent Portal through its Recipient row.
- Families without an email remain Portal recipients and retain `Failed / No parent email found.` when email was requested.
- The existing email subject, body fallback, and Parent Portal link remain unchanged.
- Save Draft never sends email.
- Repeating Publish on an already Published Announcement does not enqueue another delivery job.
- Staging outbound-email protection remains unchanged.

## Recipient Email Status

The current Recipient status values are retained without schema changes:

- `Queued`: waiting for the Announcement email job;
- `Sent`: the Recipient's BCC batch was successfully added to Frappe Email Queue;
- `Failed`: Frappe could not create the BCC email for that batch;
- `Skipped`: outbound email is disabled by environment controls;
- `Not Requested`: email was not selected when publishing.

`Sent` continues to mean accepted into Frappe's delayed Email Queue, not confirmed SMTP delivery. Individual delivery, bounce, open, or unsubscribe tracking is outside this scope.

If one BCC batch cannot be queued, every Recipient row represented by that batch is marked `Failed`, and the job continues with the remaining batches. No automatic per-address retry is added.

## Idempotency

The job reads only Recipient rows currently marked `Queued`. After a batch is added to Frappe Email Queue, all Recipient rows represented by its addresses are changed to `Sent` in the same database transaction.

Rows sharing the same normalized email address are included once in BCC and are updated together. Re-running the job therefore skips completed batches and processes only rows still marked `Queued`.

## Implementation

Update `send_school_announcement_email_job` to:

- load all queued Recipient rows without the existing 1,000-row cap;
- group rows by trimmed, case-insensitive email address;
- use batches of 50 unique addresses;
- call `sendmail_or_skip` once per batch with `recipients=[]` and `bcc=batch`;
- update all Recipient rows associated with each batch after the queue call;
- commit after each batch so progress is preserved if a later batch fails.

The publish request still creates the Recipient snapshot and enqueues one background job after commit. No frontend change is required.

## Testing

Backend tests verify:

- 120 unique queued addresses produce BCC batches of 50, 50, and 20;
- `To` and `CC` remain empty;
- duplicate email addresses appear once in BCC while all associated Recipient rows are updated;
- missing-email rows are not passed to BCC;
- a successful batch marks its rows `Sent`;
- a failed batch marks its rows `Failed` and later batches continue;
- a rerun processes only rows still marked `Queued`;
- email subject, body, and Announcement reference remain unchanged;
- Python syntax and focused Announcement tests pass.

## Deployment

This is a `qas_custom` backend-only change. After the commit is pushed, the user must update the QAS Custom app in Frappe Cloud. No DocType, field, fixture, patch, or schema changes are required, so site migrate is not required.
