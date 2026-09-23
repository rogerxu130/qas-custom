# Term 4 migration code cleanup

## Outcome

Remove the completed Term 4 2026 one-off migration and timetable-preview executables from `qas_custom` without reverting migrated production data or removing the permanent stable-ID and class-editing behavior.

## Chosen approach

Create a new cleanup commit on top of the current `main`. Do not revert the original migration commits: those commits are interleaved with permanent naming, label, validation and reviewed-edit functionality, and reverting them could remove behavior that production now depends on.

Delete only these one-off artifacts:

- `qas_custom/services/term4_class_id_migration.py`
- `qas_custom/tests/test_term4_class_id_migration.py`
- `qas_custom/services/term4_timetable_preview.py`
- `qas_custom/tests/test_term4_timetable_preview.py`
- `qas_custom/services/term4_timetable_plan.json`

Keep historical design and handover documents as an audit trail.

## Permanent behavior to retain

- `WTS-.YYYY.-.#####` and `CS-.YYYY.-.#####` naming for new records.
- Readable Weekly Timeslot and Course Session labels and search data.
- Duplicate, room-conflict and populated-schedule safeguards.
- The reviewed Weekly Timeslot course-change service.
- Atomic combined course, teacher, schedule and other editable-field saves.
- Enrollment synchronization and session-label refresh behavior.
- Existing frontend integrations and APIs used by the School Admin UI.

## Data and deployment safety

The cleanup contains no database writes, patches, schema changes or migrations. It must not rename, delete or regenerate any production Weekly Timeslot, Course Session, Enrollment or Attendance record. Production rollback remains a backup/data-recovery concern, not a reason to keep a callable one-off migration module.

Once deployed, old Bench commands under `qas_custom.services.term4_class_id_migration` and `qas_custom.services.term4_timetable_preview` should fail because those modules no longer exist. This is intentional and removes accidental re-execution risk.

## Validation

- Search the repository for imports or API callers of the five deleted artifacts; any live dependency blocks deletion.
- Run the remaining stable-ID label, Weekly Timeslot validation and reviewed-course-change tests.
- Run the broader relevant Term/School Admin regression tests that do not depend on the deleted one-off modules.
- Compile changed/retained Python modules and run `git diff --check`.
- Confirm the diff contains only the five deletions plus this design document and the later implementation plan, if present.

## Delivery

Implementation and publishing are separate. The cleanup will first be implemented and tested locally in an isolated worktree based on current `origin/main`. It will be pushed to `origin/main` only after explicit publishing authorization. Frappe Cloud deployment must be reported separately from the GitHub push.
