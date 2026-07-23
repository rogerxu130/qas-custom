# Announcement True SMTP BCC Correction

## Problem

The previous announcement implementation passed parent addresses through Frappe's
standard `bcc` argument in batches of 50. Frappe then merged To, CC, and BCC into
Email Queue recipient rows and executed one SMTP `sendmail` call per recipient.
The result looked like BCC configuration in application code, but it still created
individual sends, exhausted the Gmail sending limit, and marked recipients sent as
soon as the messages were queued.

## Approved delivery behaviour

- Build batches of at most 50 unique parent email addresses.
- Send one SMTP message per batch.
- Show `queenslandartschool@gmail.com` in the visible To header.
- Keep all parent addresses out of the MIME headers and include them only in the
  SMTP envelope recipient list.
- Use the configured outgoing Frappe Email Account and the normal Frappe email
  formatting.
- Mark all recipient rows in a batch `Sent` only after that batch's SMTP call
  succeeds.
- Mark only that batch `Failed` if its SMTP call fails, then continue with later
  batches.
- Preserve the existing outbound-email environment guard.

## Implementation

The announcement background job continues to deduplicate recipients and create
50-address batches. A dedicated batch sender uses Frappe's `QueueBuilder` only to
construct the MIME message and resolve the configured outgoing Email Account. It
then calls the account's SMTP session once with this envelope:

```text
To: queenslandartschool@gmail.com
BCC envelope: up to 50 unique parent addresses
```

The standard Frappe Email Queue sender is deliberately bypassed for this feature
because it expands BCC recipients into individual SMTP transactions.

## Verification

Automated tests must prove:

- 120 unique parents produce batch sizes 50, 50, and 20;
- duplicate addresses are sent once while all linked audit rows are updated;
- failure of one SMTP batch does not stop later batches;
- a batch produces exactly one SMTP `sendmail` call;
- the visible To address is the school account;
- the MIME message contains no Bcc header;
- recipient rows are updated only after the batch sender returns successfully.

No DocType or database schema change is required, so site migration is not needed.
