# Family Draft Invoice Consolidation Design

## Goal

Allow a School Admin to manually consolidate compatible Draft Sales Invoices for the same family, regardless of whether the invoices originated from Courses, Workshops, Trials, Store Orders, or manual billing. Automatic invoice reuse remains limited to Workshop invoice creation.

This design supersedes the Workshop-only restriction in `2026-09-03-workshop-draft-invoice-consolidation-design.md` for the manual bulk action.

## Chosen Approach

The existing bulk action becomes a general `Consolidate drafts` operation. The frontend performs an early eligibility check and the backend repeats all checks authoritatively before changing data.

Alternative approaches were rejected:

- Keeping the Workshop-only rule does not solve accidental duplicate drafts from other billing workflows.
- Automatically merging every new family invoice is too aggressive because Course, Trial, Store, and manual billing can have different review and submission timing.

## Eligibility

At least two unique invoices must be selected. Every selected invoice must:

- be a non-cancelled Draft;
- belong to the same Customer, with no conflicting non-empty Parent links;
- use the same Company and Currency;
- have compatible non-adjustment tax and charge rows;
- have no submitted payment or other state that makes a Draft unsafe to delete.

Invoice type is not an eligibility condition. Workshop, Course, Trial, Store Order, and manual Draft invoices may be combined when the financial checks pass.

For non-adjustment taxes and charges, the invoices must have the same effective structure. The target keeps one copy of that shared structure. Editable QAS Adjustment rows, including discount-template adjustments, are copied from every source invoice so their values are preserved. A mismatch is rejected with a specific message instead of guessing.

## Consolidation Transaction

The oldest selected invoice becomes the target. Within one database savepoint, the backend:

1. copies all invoice item rows from the other selected drafts into the target;
2. copies editable QAS Adjustment rows;
3. recalculates student summary and payment snapshot fields;
4. saves the target and obtains its recalculated total;
5. repoints supported source records that reference any selected invoice, including Enrollment, Workshop Enrollment, Store Order, and Inquiry invoice links;
6. deletes the redundant Draft invoices;
7. commits only after every step succeeds.

If selected invoices have different `qas_invoice_type` values, the target is labelled `Other`. If all types match, that type is retained. The target retains its due date, remarks, and parent-facing invoice message; those header fields from removed drafts are not concatenated automatically.

Any validation, save, relinking, or deletion failure rolls the operation back and returns a useful error message.

## User Interface

When two or more invoices are selected, the bulk toolbar shows `Consolidate drafts`.

- The button is enabled when the visible records pass the basic Draft and family checks.
- A disabled button has a visible reason directly beneath the toolbar.
- Confirmation states that the oldest invoice will be retained and the other selected drafts removed.
- After success, the invoice list refreshes and opens the retained invoice.

The operation is deliberately manual across invoice types. Workshop invoice creation continues automatically reusing only an existing Workshop Draft for the same family.

## Verification

Backend tests cover:

- same-family mixed-type Draft invoices are accepted;
- different Customer, conflicting Parent, Company, or Currency values are rejected;
- submitted or cancelled invoices are rejected;
- incompatible taxes or charges are rejected;
- items and adjustments are copied once;
- Enrollment, Workshop Enrollment, Store Order, and Inquiry links are repointed;
- redundant drafts are deleted only after the target saves;
- an error rolls back the whole transaction.

Frontend verification covers the updated wording, eligibility explanation, API call, refresh behavior, and a production build.
