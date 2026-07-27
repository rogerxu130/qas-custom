# Reactivate Cancelled Enrollment Attendance

## Problem

When a full-term Enrollment is reactivated and a School Admin runs **Create Attendance**, existing future `Class Attendance Entry` rows belonging to that same Enrollment may still be `Cancelled`. The normal attendance-preparation path returns those existing rows unchanged. The class-transfer path already restores equivalent rows to `To be started`, creating inconsistent behaviour.

## Scope

Update only the normal full-term enrollment attendance-preparation path.

For every eligible course session selected by the enrollment's weekly timeslot and effective start date:

- If no attendance row exists for this exact Enrollment and session, create one as `To be started`.
- If an attendance row exists for this exact Enrollment and session with status `Cancelled`, restore it to `To be started`.
- If the existing row is not cancelled, retain it unchanged.

## Safety rules

- Only rows with `source_doctype = Enrollment` and `source_document` equal to the current Enrollment may be restored.
- Do not alter attendance created by another Enrollment, Trial Inquiry, leave voucher, or manual booking.
- Do not alter `Present`, `Late`, `Absent`, `Leave`, or other non-cancelled attendance.
- Do not include cancelled Course Sessions or sessions before the requested/enrollment start date.
- Preserve the attendance record and audit identity; restoring status is not a delete-and-recreate operation.

## Implementation

Add a narrowly scoped helper for full-term enrollment attendance preparation that returns counts for `created`, `reactivated`, and `retained`. Use it from `_create_enrollment_attendance_entries` rather than the create-only helper. Existing API responses remain compatible by continuing to expose the total number of eligible attendance entries.

## Verification

Tests will cover:

1. An active enrollment with a same-source cancelled future row restores it to `To be started`.
2. A same-source non-cancelled row remains unchanged.
3. A cancelled row belonging to another source/enrollment is not reassigned or restored.
4. A new eligible session still receives a new `To be started` row.
