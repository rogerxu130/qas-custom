# School Visit Fluent Form Webhook Design

## Goal

Accept a School Visit booking from the existing Fluent Form and create a normal `School Visit` Inquiry.  The request is an appointment inquiry, not a trial-class booking: it must not be attached to a Course Session or create attendance, a trial invoice, or a class roster entry.

## Scope

- Add a dedicated, guest-accessible webhook API for the School Visit Fluent Form.
- Reuse the existing Inquiry, Parent, Student, Campus, notification, and raw-submission mechanisms.
- Create an Inquiry with status `Booked` when the required appointment information is valid.
- Preserve the original form body on the Inquiry for School Admin review.
- Protect the API with the existing inquiry webhook secret and make delivery idempotent.

The public form itself and the School Admin/Campus Admin screens are not changed in this work.

## Alternatives considered

1. **Dedicated School Visit adapter (chosen).**  Expose a clearly named School Visit endpoint which maps the Fluent fields and then delegates to the existing Inquiry creation core.  This gives Fluent/Make a stable contract while retaining the tested shared family-matching and notification behaviour.
2. **Reuse the generic inquiry webhook directly.**  This is marginally less code, but it requires the external automation to supply all internal field names and the inquiry type correctly.  A future Fluent field-label change would be easier to misconfigure.
3. **Create a separate School Visit doctype.**  This would duplicate the existing Inquiry lifecycle, dashboard queues, and notifications without a user-facing benefit.

## API contract

The new endpoint will be exposed through `qas_custom.api.inquiry` and accepts the existing webhook secret via `X-QAS-Webhook-Secret` (or the current compatible token aliases).  It accepts the normal Frappe `payload` JSON body used by Make/Fluent integrations.

The adapter accepts these canonical values and common Fluent aliases:

| Form field | Stored value |
| --- | --- |
| Parent Name | `parent_name` / `contact_name` |
| Student Name | `student_name` |
| Student DOB | `date_of_birth` |
| Email | `email` / `contact_email` |
| Phone/Mobile | `phone` / `contact_phone` |
| Campus to visit | `campus` |
| Date for the visit | `appointment_date` |
| Time to arrive | `appointment_time` |
| Class that interests you | `preferred_course` |
| Fluent Form ID and submission ID | `form_id`, `submission_id` |

The adapter always sets `inquiry_type` to `School Visit`, `create_parent` to true, and source to `Fluent Form`.  It does not accept a Course Session from the form contract.

## Creation behaviour

1. Validate the webhook token and derive a stable external submission ID from the Fluent form ID and submission ID.
2. If that ID already belongs to an Inquiry, return the existing Inquiry as a successful duplicate delivery.  No family, student, or Inquiry is created again.
3. Match an existing Parent using the existing email/user rules; otherwise create the Parent.  Email and mobile are retained as the contact details.
4. Match an existing Student using the existing Parent plus date-of-birth rule; otherwise create it.  A Student is only created if the submitted Student Name is present.  This preserves the existing School Visit behaviour for a parent who is visiting before they provide child details.
5. Resolve the submitted Campus and optional preferred Course.
6. Store the chosen visit date and arrival time as the appointment.  When contact name, contact method, Campus, date, and time are all valid, create the Inquiry as `Booked`.  Missing or unmatched information creates `Needs Review` with a staff-readable reason rather than silently inventing data.
7. Leave `course_session` empty.  Consequently the existing Inquiry controller will not create attendance, send teacher/session notifications, or queue a trial invoice.
8. Save the entire received Fluent payload in `raw_webhook_payload`, together with the source URL, form ID, submission ID, and submitted time when supplied.
9. Use the existing Inquiry after-insert notification path, which sends the School Admin notification for a School Visit as well.

## Error handling

- Invalid or absent webhook secrets receive a permission error.
- Missing form/submission identity receives a validation error because idempotency cannot be guaranteed.
- Unsupported campus, invalid date/time, or missing appointment information creates a visible `Needs Review` item rather than a false booking.
- The endpoint returns a compact object with the Inquiry ID, status, and `duplicate` flag so Make can safely retry failed deliveries.

## Verification

- A valid Fluent School Visit form creates/reuses Parent and Student, creates a `Booked` School Visit Inquiry, and stores its appointment/campus/preferred course.
- The resulting Inquiry has no Course Session and creates no attendance or invoice.
- The same `form_id` and `submission_id` sent twice returns the original Inquiry with `duplicate: true`.
- An unrecognised Campus or missing arrival time creates `Needs Review` with an explanatory reason.
- Invalid webhook token is rejected.
- A School Admin notification is queued for both booked and review-required School Visits.
