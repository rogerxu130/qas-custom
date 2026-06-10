# Term Rollover Design

## Purpose

Copy a reusable term setup from one term into another term without copying operational records.

## Scope

The rollover copies:

- `Weekly Timeslot`
- active full-term `Enrollment`

The rollover does not copy:

- `Course`
- `Course Sessions`
- `Attendance Record`
- `Session Homework`
- `Session Photo Post`
- `Session Photo Item`

`Course` is shared metadata across terms. New weekly timeslots continue to reference the original course.

## Rules

1. The source term and target term must already exist.
2. Source weekly timeslots are copied to the target term.
3. If an equivalent target weekly timeslot already exists, it is reused.
4. Only source enrollments with `status = Active` and `enrollment_type = Full-Term` are copied.
5. New enrollments point to the copied or reused target weekly timeslot.
6. New enrollment `enrollment_date` is set to the target term `start_date`.
7. Trial, pay-as-you-go, and cancelled enrollments are not copied.
8. The operation supports `dry_run` so an admin can preview counts before creating records.
9. If the target term already has a non-matching enrollment for the same student and weekly timeslot, it is reported as a conflict and left unchanged.

## Access

Only System Manager users may run the tool.
