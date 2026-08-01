# Teacher Portal Session Time Sorting Design

## Goal

Show a teacher's sessions in chronological order: first by session date, then by the actual class start time.

## Cause

The Teacher Portal session payload was sorted by the raw text representation of `start_time`. Historical values such as `9:00:00` are not zero-padded, so text sorting incorrectly places them after `10:40:00`, `1:00:00`, and `2:40:00`.

## Change

Keep the existing Teacher Portal query and role scoping unchanged. Replace only the final payload sort key with a helper that parses `start_time` using Frappe's time parser and compares its numeric seconds-from-midnight value.

- Valid times sort ascending within a date, including unpadded historical values.
- Missing or invalid times sort after valid times on the same date.
- The session identifier provides a stable final tie-breaker.

## Verification

Add a focused unit test covering `9:00`, `10:40`, `13:00`, and `14:40`, plus a missing/invalid time case. Run the focused backend test and the normal backend syntax check. No schema, notification, financial, or permission behaviour changes are involved.
