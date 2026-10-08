# Duplicate trial intake stays reviewable

Date: 2026-10-07

A trial request must remain actionable in QAS when the matched student already has attendance in the requested session. Previously attendance creation raised a ValidationError during Inquiry insertion, so Make failed after writing the Google Sheets row.

For new Trial Lesson Inquiries, check existing student/session attendance during document validation, before after_insert attempts to create attendance. Reuse the existing attendance lookup. On a conflict, keep the parent/student links, campus, preferred course, requested appointment date/time, submission ID and raw form data. Clear the assigned course_session, set status to Needs Review and confirmation to Not Required, and append a reason identifying the requested session and conflicting attendance record. Existing referral review reasons must remain intact.

The normal webhook response returns status=created, inquiry_status=Needs Review and review_required=true. Repeat delivery of the same external submission continues to reuse that Inquiry. A distinct submission may create a separate reviewable Inquiry.

Manual assignment on an existing Inquiry retains the existing duplicate attendance error. Choosing an available session returns to Booked through the existing assignment flow. Cancellation targets only attendance owned by the new Inquiry, leaving the original booking untouched. A Cancelled Trial attendance row remains eligible for reactivation; other existing rows require review. Existing strict manual-creation duplicate guards remain unchanged.

Needs Review records have no assigned session, so they create neither attendance nor trial booking notifications and are ineligible for automatic trial invoicing. The existing Campus Admin detail already displays review_reason and supports assignment and cancellation. No frontend or schema changes are required.

Validation: regression tests exercise creation, metadata retention, attendance prevention, normal booking, cancelled-trial reactivation, conflicting assignment, alternative assignment, cancellation, duplicate webhook delivery, and Make response shape. Run alongside trial matching, attendance reactivation, invoicing and referral review tests. These are isolated local tests; production deployment and the historical failed Make run are separate operations.
