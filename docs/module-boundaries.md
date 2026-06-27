# QAS Module Boundaries

QAS uses a modular monolith architecture. Modules communicate through Python command/query functions, not internal REST calls. Browser and external clients use API endpoints; backend modules call each other in-process so workflows can keep one transaction and one debuggable call stack.

## Layering

```text
API adapters
  -> Workflows
    -> Domain modules
      -> Frappe / ERPNext DocTypes
```

API adapters include `services/campus_admin.py`, `services/school_admin.py`, `services/parent_portal_*`, and `services/teacher_portal.py`. They should handle authentication, permission checks, parameter parsing, and response shaping. They should not own business rules.

Workflows coordinate cross-module business processes. A workflow may call several domain modules and then commit the transaction.

Domain modules own one business area. Other modules should use their public commands/queries rather than directly editing the owned DocTypes.

## Current Modules

### Course Schedule

Path: `qas_custom/modules/course_schedule`

Owns:

- `Course`
- `Term`
- `Weekly Timeslot`
- `Course Sessions`

Responsibilities:

- Resolve sessions and timeslots.
- Query remaining sessions.
- Build session option payloads.

It should not create enrollments, invoices, inquiry notes, or attendance records.

### Inquiry

Path: `qas_custom/modules/inquiry`

Owns:

- `Inquiry`
- `Inquiry Note`

Responsibilities:

- Validate inquiry status transitions.
- Mark inquiry converted / inactive / follow-up outcomes.
- Add manual and system notes.

It should not calculate tuition, create invoices, or create full-term attendance entries.

### Enrollment

Path: `qas_custom/modules/enrollment`

Owns:

- `Enrollment`

Responsibilities:

- Create and validate student course membership records.
- Link generated invoice references back to enrollment as audit metadata.

It should not decide invoice pricing rules or submit invoices.

### Billing

Path: `qas_custom/modules/billing`

Owns:

- Sales invoice generation rules.
- QAS custom invoice header / item context.
- Store credit application rules when implemented.

Responsibilities:

- Resolve invoice customer and ERPNext Item.
- Generate draft course invoices from enrollment context.
- Write item-level student / enrollment / course context.

It should not change inquiry status or create attendance rows.

### Attendance

Path: `qas_custom/modules/attendance`

Owns:

- Class attendance entry creation and attendance state rules.

Responsibilities:

- Create attendance records for full-term, trial, makeup, adhoc, and holiday sources.
- Create and remove trial attendance entries linked from Inquiry bookings.
- Sync attendance outcomes to their source module through explicit commands.

It should not calculate invoices.

### Makeup

Path: `qas_custom/modules/makeup`

Owns:

- Parent leave request command rules.
- Makeup voucher redemption rules.

Responsibilities:

- Validate that a parent can request leave for one of their students.
- Create approved leave requests and expose the generated makeup voucher.
- Find sessions that can accept a voucher.
- Redeem a voucher for any child under the same parent account.
- Ask Attendance to create the makeup class visit row.

It should not own parent login/session parsing, teacher attendance marking, or invoice generation.

### Workflows

Path: `qas_custom/modules/workflows`

Owns cross-module processes, for example:

- trial conversion
- makeup booking
- adhoc booking
- invoice approval

The first refactored workflow is `trial_conversion`.

`trial_conversion` coordinates:

```text
Inquiry validation
-> Course Schedule session lookup
-> Enrollment creation
-> Billing draft invoice
-> Attendance entry creation
-> Inquiry converted status
-> Inquiry conversion note
-> transaction commit
```

## Compatibility Facades

Legacy service files may remain as facades while the system is being migrated. For example:

```text
services/billing_enrollment.py
```

now re-exports the workflow/domain commands used by existing API adapters. New business logic should be added to `modules/*`, not to the compatibility facade.

## Dependency Rules

- API adapters can call workflows and domain modules.
- Workflows can call domain modules.
- Domain modules should not call API adapters.
- Domain modules should avoid calling each other unless the dependency direction is clear.
- If a process needs multiple modules and one transaction, put orchestration in `modules/workflows`.
- Use internal Python functions for backend-to-backend module calls.
- Use REST only for browser/external system boundaries.

## Debugging Rule

When a production issue happens, start from the owning module:

- Lead / trial / school tour status: Inquiry.
- Course time / teacher / room / session: Course Schedule.
- Student is or is not formally in a class: Enrollment.
- Invoice amount / line context / store credit: Billing.
- Leave request / makeup voucher redeeming: Makeup.
- Teacher attendance / class visit status: Attendance.
- Multi-step process failed halfway: Workflow.
