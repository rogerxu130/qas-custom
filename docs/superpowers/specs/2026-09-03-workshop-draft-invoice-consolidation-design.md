# Workshop Draft Invoice Consolidation Design

## Goal

Keep one editable Workshop draft invoice per family while preserving a separate line item and Workshop Enrollment link for every Workshop booking.

## Automatic Reuse

When School Admin creates an invoice for an Active Workshop Enrollment, the backend searches for the oldest non-cancelled Draft Sales Invoice with the same Parent, Customer, and `qas_invoice_type = Workshop`.

- If one exists, the new Workshop line is appended to it.
- Otherwise, a new Workshop Draft Invoice is created.
- Course, Trial, Store, and other invoice types are never reused.
- A Workshop Enrollment already linked to an invoice remains idempotent and is not appended twice.

## Manual Consolidation

The Invoices bulk toolbar exposes `Consolidate Workshop drafts` when two or more selected rows are Draft Workshop invoices.

The backend performs authoritative validation before changing data:

- at least two unique invoices;
- all invoices are Draft and not cancelled;
- all have `qas_invoice_type = Workshop`;
- all belong to the same Parent and Customer;
- no source invoice contains an unsupported non-adjustment tax or charge row.

After confirmation, the oldest selected invoice becomes the target. Invoice items and editable QAS Adjustment rows are copied into it, every linked Workshop Enrollment is repointed to the target, and the redundant Draft invoices are deleted in one transaction. The target retains its due date and parent-facing message; copied fixed-value discounts retain their amounts. The operation returns the target invoice and counts.

## Invoice Description

The Draft Invoice editor renders Parent-facing line descriptions with a multiline textarea so Workshop title, student, campus, and dates do not visually collapse together.

## Failure Handling

All backend mutations run under a savepoint. Any validation, save, relinking, or delete error rolls back the consolidation. The frontend shows the returned reason and refreshes the invoice list only after success.

## Verification

- Automatic creation reuses only a matching Workshop draft.
- Existing Workshop Enrollment lines are not duplicated.
- Manual consolidation rejects mixed families, submitted invoices, and mixed invoice types.
- Successful consolidation copies items and adjustments, relinks Workshop Enrollments, and deletes redundant drafts.
- Frontend production build and relevant backend unit tests pass.
