# Trial Email Art Supplies Notice

## Goal

Tell parents that art supplies are included whenever QAS emails them about a trial class.

## Scope

Add the following fixed sentence to the shared Trial parent email template:

> All art supplies are provided for the trial class.

The notice must appear in every email rendered by that template:

- Trial Class Booked
- Trial Class Rescheduled
- Automatic 24-hour Trial Reminder
- School or Campus Admin manual Trial Reminder

This change does not alter SMS/contact-status behavior, reminder scheduling, recipients, subjects, or delivery logic.

## Design

Render the sentence as a visually distinct informational paragraph directly below the trial class details table and before the existing contact/change instructions. Put the sentence in the shared `_trial_class_reminder_email_message` template so all four email flows inherit the same wording without duplicated copy.

Pass the sentence through the existing translation helper and HTML escaping behavior, consistent with the rest of the template. The production English sentence is exactly:

> All art supplies are provided for the trial class.

## Verification

Update the shared template content test to verify the notice appears in both the default/manual rendering and a rendering with custom heading and intro text. Because booking, reschedule, and automatic reminder emails all call the same template, also retain the existing tests that cover their copy selection and shared renderer usage.

Run the focused Trial parent notification tests and Python syntax validation before release.
