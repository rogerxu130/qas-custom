# Inquiry User Creation Race Fix Design

Date: 2026-07-23

## Problem

The Trial Inquiry webhook resolves a parent by email and creates a Frappe `User` only when no matching user is found. Two requests can nevertheless overlap between the lookup and insert. The later insert then raises `frappe.DuplicateEntryError`, Make.com receives HTTP 409, and the Inquiry is not created even though the required User now exists.

## Confirmed Behaviour

- Continue using the submitted normalized email as the Parent/User identity.
- Reuse an existing User and its linked Parent when available.
- If User insertion raises `DuplicateEntryError`, query the User again.
- If the User now exists, return and reuse it so Parent, Student, and Inquiry creation can continue.
- If no matching User exists after the duplicate error, re-raise the original exception rather than hiding an unrelated data problem.
- Do not send a welcome email or Parent Portal invitation.
- Do not change Make.com, Inquiry idempotency, finance, attendance, or notification behaviour.

## Implementation

Keep the change inside `_get_or_create_user_for_parent` in `qas_custom/services/inquiry.py`. Wrap only `user_doc.insert()` in a `DuplicateEntryError` handler and perform a fresh lookup through the same email-based resolution rule.

This is preferred over a database lock because User creation is infrequent and the duplicate key already provides the required concurrency boundary. It is preferred over blindly ignoring duplicate inserts because a fresh lookup verifies that a reusable User actually exists.

## Testing

Add focused unit coverage for:

1. normal new User creation remains unchanged;
2. a duplicate insert followed by a successful lookup returns the existing User;
3. a duplicate insert followed by no matching User re-raises `DuplicateEntryError`.

Run the focused test module, Python compilation, and `git diff --check`.

## Deployment

Backend-only code change. No DocType, custom field, patch, or schema change is required, so site migration is not required.
