# Shared classroom foundation: pre-extraction baseline (2026-09-23)

This records the isolated baseline before extracting shared session resources. This task changes documentation only: no data, schema, API, or business behavior changes.

## Checkouts and runtimes

| Repository | Isolated path | State at verification |
| --- | --- | --- |
| `qas_custom` backend | `/private/tmp/qas-shared-foundation` | branch `codex/qas-shared-foundation`, `d3eec8ea4b2cc01ce6d2f0eb4691c47e3e4d3454` (based on fetched `origin/main`), clean before this document |
| Parent Portal frontend | `/Users/ranxu/.codex/worktrees/payg-foundation-plans/qas-parent-portal` | detached HEAD `943a72c4168f62906188f442a3d3d3a0d45635e4`, clean before verification |

Backend ran with Python 3.11.15 from `/Users/ranxu/Documents/Project/frappe-bench/env/bin/python`, importing the real Frappe environment and `qas_custom` from the isolated checkout. Frontend ran with Node v22.22.2 from `/Users/ranxu/.nvm/versions/node/v22.22.2/bin/node`; the reused Vite installation reported v8.0.3.

## Commands and observed results

Run from `/private/tmp/qas-shared-foundation`:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" /Users/ranxu/Documents/Project/frappe-bench/env/bin/python -c 'import qas_custom; print(qas_custom.__file__)'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" /Users/ranxu/Documents/Project/frappe-bench/env/bin/python -m unittest qas_custom.tests.test_concentrated_makeup qas_custom.tests.test_direct_enrollment qas_custom.tests.test_invoice_account_mutation -v
```

The import printed `/private/tmp/qas-shared-foundation/qas_custom/__init__.py`. The unittest command passed **43/43 tests** (`Ran 43 tests in 0.043s`, `OK`). The tests use the real Frappe import but mock database boundaries; they do not establish live database behavior.

The two additional modules named in the caller inventory were run from the same backend checkout and Python environment:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" /Users/ranxu/Documents/Project/frappe-bench/env/bin/python -m unittest qas_custom.tests.test_parent_makeup_session_roster qas_custom.tests.test_makeup_parent_notifications -v
```

This ran **21 tests: 20 passed and 1 errored** (`Ran 21 tests in 0.066s`, `FAILED (errors=1)`). All tests in `test_makeup_parent_notifications.py` passed. The error was `test_parent_list_hides_empty_sessions_and_admin_list_keeps_them` in `test_parent_makeup_session_roster.py`: `TypeError: unhashable type: 'dict'` at `qas_custom/modules/makeup/commands.py:685`. That test patches `commands.frappe.get_all` to always return session dictionaries; the production function also calls `get_all` for `Weekly Timeslot` names and passes that mocked result to `set(...)`. This is a baseline test-mock mismatch at the unchanged commit, not a verified site or database failure. No code was changed to resolve it.

Run from `/Users/ranxu/.codex/worktrees/payg-foundation-plans/qas-parent-portal`:

```sh
/Users/ranxu/.nvm/versions/node/v22.22.2/bin/node --test tests/*.test.js
```

The Node command passed **12/12 tests** (`# pass 12`, `# fail 0`). This detached checkout has no `node_modules`; invoking the existing Vite CLI alone initially failed to resolve `vite`, `@vitejs/plugin-vue`, `@tailwindcss/vite`, and `unplugin-icons/vite` from its config. The successful production build used the existing dependency directory through a temporary symlink. Reproduce that build from the frontend checkout with:

```sh
(
  set -e
  test ! -e node_modules && test ! -L node_modules
  ln -s /Users/ranxu/Documents/Project/qas-parent-portal/node_modules node_modules
  trap 'test -L node_modules && unlink node_modules' EXIT
  /Users/ranxu/.nvm/versions/node/v22.22.2/bin/node /Users/ranxu/Documents/Project/qas-parent-portal/node_modules/vite/bin/vite.js build
)
```

The build passed (`2184 modules transformed`, `built in 1.34s`). Vite warned that some generated chunks exceed 500 kB after minification. The temporary symlink was removed after verification. No package manifest or lockfile was changed. A future build in this detached checkout must recreate that link or otherwise supply dependencies; the checkout has no local installation. The absolute paths above are a machine-specific historical snapshot and must be adapted on another machine.

## Caller inventory

`rg` over the backend Python tree found the six candidate helpers defined in `qas_custom/services/concentrated_makeup.py`; the following are all observed calls and test patch points for those helpers. Line numbers refer to the baseline SHA.

| Helper and definition | Production callers | Direct test references / patch points |
| --- | --- | --- |
| `session_context` (17) | `concentrated_makeup.py`: `validate_new_place` (101), `get_options` (136, 148), `accepts_voucher` (170), `validate_voucher_target` (178), `get_settings` (196) | `test_concentrated_makeup.py` (42, 84); `test_parent_makeup_session_roster.py` (87) |
| `active_rows` (23) | `concentrated_makeup.py`: `validate_configuration` (81), `validate_new_place` (104), `get_options` (137, 159), `get_settings` (197); `direct_enrollment.py`: capacity check (150) | `test_concentrated_makeup.py` (42, 158); `test_direct_enrollment.py` (134, 139, 163); `test_parent_makeup_session_roster.py` (90) |
| `overlaps` (41) | `concentrated_makeup.py`: `student_has_conflict` (55) | `test_concentrated_makeup.py` (19–22) |
| `student_has_conflict` (45) | `concentrated_makeup.py`: `validate_new_place` (107), `get_options` (149); `direct_enrollment.py`: capacity check (153) | `test_concentrated_makeup.py` (42); `test_direct_enrollment.py` (128, 163); `test_parent_makeup_session_roster.py` (89) |
| `classroom_capacity` (58) | `concentrated_makeup.py`: ordinary-place branch of `validate_new_place` (115), `get_settings` (197); `direct_enrollment.py`: capacity check (139) | `test_concentrated_makeup.py` (42, 158); `test_direct_enrollment.py` (126, 153) |
| `session_is_future` (62) | `concentrated_makeup.py`: `validate_configuration` (76), `validate_new_place` (105), `get_options` (137, 149); `direct_enrollment.py`: capacity check (132, 148) | `test_concentrated_makeup.py` (27–30, 42, 158); `test_direct_enrollment.py` (127, 158); `test_parent_makeup_session_roster.py` (88) |

The four helpers used by `direct_enrollment.py` are imported locally at line 110 from `qas_custom.services.concentrated_makeup`. Its tests patch attributes at that original module path, which is part of the current test seam.

Other `concentrated_makeup` entry points and integrations observed:

- `qas_custom/api/concentrated_makeup.py` imports the service and exposes `get_options`, `book`, `get_settings`, and `update_settings` through whitelisted API wrappers.
- `qas_custom/modules/makeup/commands.py` imports `lock_booking` (379) and `validate_new_place` plus `validate_voucher_target` (436) during voucher redemption. Its concentrated-only guard reads `concentrated_makeup_enabled` at 414.
- `qas_custom/qas_custom/doctype/course_sessions/course_sessions.py` calls `validate_configuration` on validation (7–9); `qas_custom/qas_custom/doctype/class_attendance_entry/class_attendance_entry.py` calls `validate_attendance` on validation (6–7).
- `qas_custom/services/admin_followups.py` imports `is_makeup_row` (11).
- `qas_custom/tests/test_concentrated_makeup.py` imports the service (7) and exercises configuration, attendance, booking, helper edge cases, and capacity rules. `qas_custom/tests/test_direct_enrollment.py` patches the original helper module (126–163). `qas_custom/tests/test_parent_makeup_session_roster.py` patches service helpers (75, 87–90). `qas_custom/tests/test_makeup_parent_notifications.py` patches `lock_booking`, `validate_new_place`, and `validate_voucher_target` (247–249).
- No `concentrated_makeup` or `session_resources` reference was found in `qas_custom/hooks.py`. No `session_resources` file or reference was found anywhere in the backend checkout at this SHA.

The existing rule is intentional: a concentrated makeup quota **may exceed classroom capacity**. The makeup branch enforces its own booking quota; the ordinary attendance branch checks classroom capacity. `test_configuration_allows_twenty_places_in_ten_seat_room` and `test_makeup_can_exceed_classroom_capacity_but_not_booking_limit` capture this behavior. The extraction must preserve it.

## Verification limits

Real database concurrency and lock behavior, browser UI, test-site end-to-end flows, and production behavior remain unverified. This baseline does not imply a Frappe Cloud or frontend deployment. The successful build required temporary reuse of the existing dependency directory, as described above. The additional backend run has one baseline test error; it is not a clean suite pass.
