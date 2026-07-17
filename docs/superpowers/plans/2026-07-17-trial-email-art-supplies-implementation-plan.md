# Trial Email Art Supplies Notice Implementation Plan

1. Extend the shared Trial parent email renderer in `qas_custom/modules/notifications/commands.py` with a translated, HTML-escaped art-supplies notice below the class details table.
2. Update `qas_custom/tests/test_trial_parent_notifications.py` to assert the notice is present for default/manual and custom-copy renderings.
3. Run Python syntax validation, focused Trial parent notification tests, and diff checks.
4. Commit the implementation separately from the design and plan commits.
