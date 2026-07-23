# Inquiry Link Existing Enrollment Design

Date: 2026-07-23

## Goal

Allow School Admin to mark a completed Trial Lesson Inquiry as converted by linking an Enrollment that staff already created manually. This closes the Inquiry conversion lifecycle without creating a second Enrollment, invoice, or set of attendance records.

## Current Behaviour

The existing `Convert` action requires a Course Session and performs a complete conversion transaction:

1. create an active full-term Enrollment;
2. create and link a prorated invoice;
3. create full-term attendance rows;
4. set `Inquiry.status = Converted`;
5. link the new Enrollment and invoice to the Inquiry.

That flow must remain unchanged for ordinary conversions. It is unsafe for a manually enrolled student because replaying it would duplicate operational and financial records.

## Confirmed Scope

Included:

- School Admin only.
- Trial Lesson Inquiries in `Completed` or `Follow-up` status.
- Select one existing `Planned` or `Active` Enrollment for the exact Inquiry student.
- Link the Inquiry and Enrollment in both directions where supported.
- Copy the Enrollment's existing invoice reference to the Inquiry when available.
- Record a human-readable conversion audit note.
- Refresh Inquiry lists, details, and School Admin dashboard data after success.

Out of scope:

- Campus Admin access.
- Creating or editing Enrollment, invoice, payment, store credit, or attendance data.
- Linking a Cancelled Enrollment.
- Marking an Inquiry converted without an Enrollment.
- Changing the existing create-new conversion workflow.
- Automatically choosing an Enrollment when more than one candidate exists.

## User Experience

In the School Admin Inquiry detail `Convert` section, show two explicit paths:

1. `Create New Enrollment` keeps the existing Course Session selector and conversion action.
2. `Link Existing Enrollment` loads eligible Enrollments for the Inquiry student and requires the operator to select one.

Each Enrollment option displays enough context to prevent a wrong link:

- Enrollment ID;
- term;
- course;
- campus or weekly timeslot;
- status;
- linked invoice, when present.

The link action remains disabled until a candidate is selected. The confirmation text states that no new Enrollment, invoice, or attendance will be created.

## Backend Design

Add a School Admin-only endpoint dedicated to this action, separate from the existing full conversion endpoint:

```text
POST qas_custom.api.school_admin.school_admin_link_inquiry_enrollment
```

Input:

```json
{
  "inquiry": "INQ-2026-00001",
  "enrollment": "ENR-2026-00001"
}
```

Validation:

1. The caller has the School Admin role.
2. The Inquiry exists and is a Trial Lesson Inquiry.
3. If the Inquiry is already `Converted` and references this same Enrollment, return the current detail as an idempotent success; if it references another Enrollment, block the action.
4. Otherwise, the Inquiry status is `Completed` or `Follow-up` and it has not already been converted.
5. The Inquiry has a Parent and Student.
6. The Enrollment exists and has status `Planned` or `Active`.
7. `Enrollment.student` exactly matches `Inquiry.student`.
8. If both Parent fields are populated, `Enrollment.parent` matches `Inquiry.parent`.
9. `Enrollment.source_inquiry` is empty or already equals this Inquiry.
10. No other converted Inquiry already references this Enrollment.

On success, within one transaction:

- set `Inquiry.status = Converted`;
- set `Inquiry.converted_enrollment = Enrollment.name`;
- set `Inquiry.converted_invoice` from the Enrollment's current invoice reference when available;
- set `Enrollment.source_inquiry = Inquiry.name` only when the field is empty;
- add an Inquiry Note stating that School Admin linked the existing Enrollment and that no new Enrollment, invoice, or attendance was created;
- commit once after all writes succeed.

The action returns the refreshed Inquiry detail.

## Enrollment Candidate Query

Reuse the existing School Admin Enrollment query rather than filtering a previously loaded frontend list. Query by the exact Inquiry student with:

- statuses `Planned,Active`;
- inactive terms included so an older manually created Enrollment remains linkable when necessary;
- backend limit sufficient for one student's complete history.

The UI must not offer Enrollments from another student or rely on global text search for identity matching.

## Financial And Notification Effects

This action has no financial mutation:

- no invoice is created, submitted, cancelled, or marked paid;
- no store credit is created or applied;
- no Enrollment fee or invoice amount is recalculated;
- no attendance row is created or modified.

No parent, teacher, Campus Admin, or School Admin email is sent. The audit trail is the Inquiry status/link fields and the Inquiry Note.

## Idempotency And Conflict Handling

- Repeating the same action after the Inquiry is already linked to the same Enrollment returns the current Inquiry detail without duplicating the audit note.
- Attempting to link a different Enrollment after conversion is blocked and requires a separate future correction workflow.
- An Enrollment already linked to another Inquiry is blocked.
- Any validation failure occurs before mutation; partial links are not committed.

## Frontend Data Refresh

After success, refresh:

- selected Inquiry detail;
- Inquiry list/search results;
- School Admin overview/dashboard counts;
- Enrollment list or search cache if currently loaded.

The existing create-new conversion continues to refresh invoices because it creates one. The link-existing path does not need an invoice-list refresh unless the selected Enrollment's existing invoice is displayed from cached data.

## Testing

Backend focused tests:

- links an eligible Enrollment and marks the Inquiry Converted;
- copies the existing Enrollment invoice reference when present;
- sets `Enrollment.source_inquiry` only when empty;
- idempotent repeat does not add another note;
- rejects another student's Enrollment;
- rejects a Parent mismatch when both records have Parent values;
- rejects Cancelled or unsupported Enrollment status;
- rejects an Enrollment linked to another Inquiry;
- rejects an Enrollment referenced by another converted Inquiry;
- rejects non-Trial and non-post-visit Inquiries;
- creates no Enrollment, invoice, attendance, store-credit, or email side effect.

Frontend verification:

- only School Admin Inquiry detail exposes the action;
- candidate rows show term/course/campus/status/invoice context;
- action is disabled until selection;
- success refreshes the Inquiry to `Converted` with linked Enrollment visible;
- existing create-new conversion remains unchanged;
- desktop and mobile layouts keep both conversion paths usable.

Run relevant Python tests, Python compilation, frontend production build using Node 20.19+, and `git diff --check` before release.

## Deployment

This design reuses existing Inquiry and Enrollment fields. No DocType, custom field, fixture, patch, or schema change is required, so no site migration is expected.
