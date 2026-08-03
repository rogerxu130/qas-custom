# Transfer first-class teacher alert

## Goal

When a student transfers their active Full-Term enrolment to another weekly
timeslot, the receiving teacher needs to know that the student is attending
that class for the first time. The alert must be shown only for that first
destination session.

## Scope

- Applies only to the existing School Admin Full-Term transfer workflow.
- Does not apply to Trial, Makeup, Pay-as-you-go, or a student's initial
  enrolment.
- Marks the first eligible destination attendance row produced by the transfer.
- Shows an advance alert on the teacher home session card and an individual
  alert alongside the student in the class detail screen.

## Data model

Add a read-only `qas_first_class_after_transfer` Check field to `Class
Attendance Entry`. This persists the one-session event with the attendance
record itself, instead of deriving it from historical enrolment data every time
the teacher portal is opened.

## Transfer behaviour

The transfer preview already returns destination sessions in chronological
order. The first session id in that list is passed to the destination-attendance
creator. Its row is marked whether the row is newly created or reactivated.
Later destination attendance rows are unmarked. Cancelled rows are invisible to
teachers, so a later re-transfer can safely mark the newly relevant first row.

## Teacher portal behaviour

- Home cards show an amber summary when a session has one or more students in
  their first class after a transfer.
- Session detail shows an amber preparation callout and an individual badge:
  `First class in this class`.
- Existing Trial, Makeup, and Pay-as-you-go preparation indicators remain
  separate and unchanged.

## Verification

- Backend tests cover marker creation and reactivation in the transfer helper.
- Backend tests cover the teacher portal payload and summary count.
- The teacher portal build verifies the Vue templates compile.
